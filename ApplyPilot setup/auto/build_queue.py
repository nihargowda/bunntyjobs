#!/usr/bin/env python3
"""List ApplyPilot jobs that can be applied to now, using the rules in config/settings.json.

Usage: python3 build_queue.py [N]        -> prints the top N (default 10) and writes the full queue.json
       python3 build_queue.py --links N  -> top N scored jobs that still need an apply link
Ordered by fit score, then preferred titles, H-1B sponsor strength (only when need_sponsorship is on), and highest
posted pay; staffing firms, region and easy one-page forms (Greenhouse, Lever, Ashby, Workable...) only break ties.
Dead postings (404 / "no longer accepting") are dropped here for free and remembered in dead.txt, so no agent opens them.
Skips: already applied (DB) or already tried (tried.txt), excluded titles, levels, industries and companies,
no-sponsorship or clearance listings (when you need sponsorship), pay under your minimum, no apply link, non-US roles
(when us_only is on), and sites that need an account or that automation can't use.
"""
import json, re, sqlite3, subprocess, sys, collections, urllib.parse, urllib.request, concurrent.futures as cf
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import dashboard as d  # parse_salary, resolve_company, DB
from settings import SETTINGS as S, rx, words, resume_for

from job_url import normalize as norm
from us_only import non_us_location
tried = {l.strip() for l in (HERE / "tried.txt").read_text().splitlines() if l.strip()} if (HERE / "tried.txt").exists() else set()

SPON = re.compile(r"(not|unable to|cannot|will not|won.t|no)\s+(be\s+)?(able to\s+)?(provide\s+|offer\s+|pursue\s+)?(visa\s+|employment\s+|immigration\s+)?sponsor|"
                  r"not eligible for (visa )?sponsorship|without (the need for |requiring )?(current or future )?(visa |employer |employment |company )?sponsorship|"
                  r"must be (a )?u\.?s\.? citizen|citizenship.{0,20}required|security clearance|green card holders?|\bU\.?S\.? persons?\b|\bITAR\b|work in Canada", re.I)
NEED_SPONSORSHIP = bool(S["need_sponsorship"])
EXCLUDE = rx(S["excluded_industries"])
DESC_EXCLUDE = rx(S.get("description_exclude", []))  # checked against the whole description
EXCLUDE_TITLE = rx(S["title_exclude"])
# application sites that need an account first (applicant-tracking systems and the largest employers' own portals)
NO_AUTOMATION = ("myworkday", "icims", "taleo", "avature", "successfactors", "oraclecloud", "adp.com", "ultipro", "dayforce",
                 "paycomonline", "csod.com", "peopleadmin", "governmentjobs", "brassring", "smartrecruiters",
                 "amazon.jobs", "careers.google", "google.com/about/careers", "careers.microsoft", "linkedin.com/signup")

MIN_SCORE = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--min=")), S["min_score"]))
# one title pattern may qualify at its own score (settings: special_min_score)
SPECIAL = rx(S["special_min_score"].get("title_pattern", ""))
SPECIAL_MIN = int(S["special_min_score"].get("min_score", MIN_SCORE))
SENIOR = rx(S["senior_title_pattern"])
SENIOR_MIN = int(S["senior_min_score"])
LEVELS = rx(S["title_exclude_levels"])
SALARY_MIN = int(S["salary_minimum"] or 0)
EXCLUDED_COMPANIES = words(S["excluded_companies"])
COMPANY_CAP, CAP_DAYS = int(S["company_cap"]), int(S["company_cap_days"])
PREFERRED = rx(S["preferred_title_pattern"])
STAFFING = words(S["staffing_firms"])
REGIONS = [rx(p) for p in S["region_priority"]]


def score_ok(title, score):
    """Meets the general minimum, or the special title's own minimum; senior titles need senior_min_score."""
    special = bool(SPECIAL.search(title))
    if score < (SPECIAL_MIN if special else MIN_SCORE):
        return False
    return special or not SENIOR.search(title) or score >= SENIOR_MIN


# Application-form platforms. When a listing has no employer name, the resolved "company" can be the platform that
# hosts the form (Paylocity, Paradox, ...). Its name must never stand in for the employer: that would show the
# platform's own H-1B record. So read the employer from the link when the platform puts it there, else leave it blank.
PLATFORMS = {"paylocity", "paradox", "clearcompany", "greenhouse", "grnh", "lever", "ashby", "ashbyhq", "workable",
             "icims", "workday", "myworkdayjobs", "appcast", "smartrecruiters", "jobvite", "bamboohr", "breezy", "recruitee",
             "jazzhr", "applytojob", "adp", "ultipro", "ukg", "taleo", "successfactors", "oracle", "indeed", "linkedin",
             "ziprecruiter", "glassdoor", "dice", "teamtailor", "pinpointhq", "rippling", "dayforce", "dayforcehcm",
             "paycom", "paycor", "phenom", "eightfold", "hirebridge", "jobs", "careers", "apply", "olivia"}
EMPLOYER_IN_LINK = [
    r"paylocity\.com/Recruiting/Jobs/Details/\d+/([^/?#]+)",
    r"paradox\.ai/co/([^/?#]+)",
    r"apply\.clearcompany\.com/([^/?#]+)",
    r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)",
    r"jobs\.lever\.co/([^/?#]+)",
    r"jobs\.ashbyhq\.com/([^/?#]+)",
    r"apply\.workable\.com/([^/?#]+)",
    r"jobs\.smartrecruiters\.com/([^/?#]+)",
    r"([a-z0-9-]+)\.bamboohr\.com",
    r"([a-z0-9-]+)\.breezy\.hr",
    r"([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com",
    r"careers-([a-z0-9-]+)\.icims\.com",
]


def employer(r):
    """The hiring company for a job row: the resolved name, unless that is just the form platform's name."""
    name = d.resolve_company(r)[0] or ""
    if name and co_key(name) not in PLATFORMS and co_key(name).split(" ")[0] not in PLATFORMS:
        return name
    for link in (r["application_url"] or "", r["url"] or ""):
        for pat in EMPLOYER_IN_LINK:
            m = re.search(pat, link, re.I)
            if m and m.group(1).lower() not in PLATFORMS:
                raw = urllib.parse.unquote(m.group(1))
                return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"[-_]+", " ", raw)).strip()
    return ""


def co_key(name):
    words_ = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in {"inc", "llc", "ltd", "corp", "co", "company", "the"}]
    return " ".join(words_)


def recent_applications():
    """Submitted applications per company key in the last CAP_DAYS days, from Applications/applications_log.csv."""
    import csv, datetime
    out = collections.Counter()
    log = HERE.parent / "Applications" / "applications_log.csv"
    if not log.exists():
        return out
    cutoff = (datetime.date.today() - datetime.timedelta(days=CAP_DAYS)).isoformat()
    with open(log, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("status") or "").strip().lower() == "submitted" and (r.get("date") or "") >= cutoff:
                out[co_key(r.get("company"))] += 1
    return out


SHOW = next((int(x) for x in sys.argv[1:] if x.isdigit()), 10)
EASY = ("linkedin.com/jobs/view",  # LinkedIn Easy Apply
        "greenhouse.io", "grnh.se", "lever.co", "ashbyhq.com", "workable.com", "rippling.com", "bamboohr.com",
        "breezy.hr", "recruitee.com", "teamtailor.com", "pinpointhq.com", "jazzhr.com", "applytojob.com")


def region_priority(location):
    for i, pattern in enumerate(REGIONS):
        if pattern.search(location or ""):
            return i
    return len(REGIONS)


DEAD_TXT = re.compile(r"no longer (accepting|available|open)|position (has been )?(filled|closed)|job (is )?(closed|expired|not found)|"
                      r"this job (posting )?(has )?(expired|been removed)|page (you.re looking for )?(cannot be|can.t be|not) found", re.I)
dead_file = HERE / "dead.txt"
dead = {l.strip() for l in dead_file.read_text().splitlines() if l.strip()} if dead_file.exists() else set()
# apply pages whose own text rules the job out (no sponsorship, citizenship, clearance), found for free below
screened_file = HERE / "screened.txt"
screened = {l.split("\t")[0] for l in screened_file.read_text().splitlines() if l.strip()} if screened_file.exists() else set()

# H-1B sponsor tier from DOL LCA/PERM filings, via career-ops' h1b-sponsor plugin (local index, nothing leaves the machine).
# Only used when need_sponsorship is on; ranks within buckets and never drops a job. Cached per company until the index is refreshed.
H1B_ROOT = HERE.parent.parent / "career-ops"
H1B_CHECK = H1B_ROOT / "plugins" / "h1b-sponsor" / "check.mjs"
H1B_META = H1B_ROOT / "data" / "h1b" / "index.ndjson.gz.meta.json"
TIER_CACHE = HERE / "h1b_tiers.json"
TIER_RANK = {"strong": 0, "moderate": 1, "staffing-shop": 1, "unknown": 2, "weak": 3, "none": 4}


def h1b_tiers(companies):
    """{company: tier}. Unresolvable or unchecked companies are left out (treated as 'unknown')."""
    if not NEED_SPONSORSHIP:
        return {}
    try:
        index = json.loads(H1B_META.read_text()).get("sha256", "")
    except Exception:
        return {}  # index not installed: rank as before
    try:
        cache = json.loads(TIER_CACHE.read_text())
    except Exception:
        cache = {}
    index = index + ":v2-legal-name-fallback"
    if cache.get("index") != index:
        cache = {"index": index, "tiers": {}}
    tiers = cache["tiers"]

    def run(args):
        out = subprocess.run(["node", str(H1B_CHECK), *args], cwd=H1B_ROOT, capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout)

    def toks(s):
        t = re.findall(r"[a-z0-9]+", (s or "").lower())
        return t[1:] if t[:1] == ["the"] else t

    def look(name):
        """The tier for a company name. Job boards use short names ("Microsoft") while DOL records the legal entity
        ("Microsoft Corporation"), and an exact short-name match can be a tiny unrelated filer. So when the plain
        name comes back weak, none or unmatched, also check the biggest filers whose names start with the same words,
        and keep the strongest tier found."""
        try:
            tier = run([name, "--json"]).get("friendlinessTier")
        except Exception:
            tier = None
        if tier in ("strong", "moderate", "staffing-shop"):
            return name, tier
        try:
            q = toks(name)
            found = run([name, "--search", "--json"]).get("results", [])  # highest-volume filers first
            alts = [r["name"] for r in found if q and toks(r["name"])[:len(q)] == q and r["name"].lower() != name.lower()][:3]
            for alt in alts:
                t2 = run([alt, "--json"]).get("friendlinessTier")
                if TIER_RANK.get(t2, 9) < TIER_RANK.get(tier, 9):
                    tier = t2
        except Exception:
            pass
        return name, tier

    missing = sorted({n for n in companies if n and n not in tiers})
    if missing:
        with cf.ThreadPoolExecutor(8) as ex:
            for name, tier in ex.map(look, missing):
                if tier:
                    tiers[name] = tier
        TIER_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return tiers


def page_problem(url):
    """'dead' if the posting is gone, 'screened: <words>' if the apply page itself refuses sponsorship or needs
    citizenship/clearance (only checked when you need sponsorship), else None. Network errors count as fine."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/130 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=8) as r:
            page = r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return "dead" if e.code in (404, 410) else None
    except Exception:
        return None
    if DEAD_TXT.search(page):
        return "dead"
    if not NEED_SPONSORSHIP:
        return None
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))
    m = SPON.search(text)
    return f"screened: {m.group(0)[:60]}" if m else None


def is_dead(url):
    """True if the posting is gone (kept for older callers)."""
    return page_problem(url) == "dead"


def title_problem(title, score, location):
    """Reason a job is ruled out by its title, score or location, else None."""
    if not score_ok(title, score): return "below your score minimum for this title"
    if LEVELS.search(title): return "excluded level (title_exclude_levels)"
    if S["us_only"] and non_us_location(location): return "outside the US"
    if EXCLUDE_TITLE.search(title): return "excluded title (title_exclude)"
    return None


c = sqlite3.connect(f"file:{d.DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
FLOOR = min(MIN_SCORE, SPECIAL_MIN)

if "--links" in sys.argv:  # scored jobs with no apply link yet: open the posting, copy its Apply link, save with set_link.sh
    rows = c.execute("SELECT * FROM jobs WHERE fit_score>=? AND applied_at IS NULL AND "
                     "(application_url IS NULL OR application_url IN ('','None')) ORDER BY fit_score DESC, scored_at DESC", (FLOOR,)).fetchall()
    rows = [r for r in rows if norm(r["url"]) not in tried and norm(r["url"]) not in dead
            and not title_problem(r["title"] or "", r["fit_score"], r["location"])]
    company = {r["url"]: employer(r) for r in rows}
    tiers = h1b_tiers(company.values())
    tier = lambda r: tiers.get(company[r["url"]], "unknown")
    pay = {r["url"]: (lambda lo, hi: hi or lo)(*d.parse_salary(r["salary"])) for r in rows}
    rows.sort(key=lambda r: (-r["fit_score"], not PREFERRED.search(r["title"] or ""), TIER_RANK[tier(r)], -(pay[r["url"]] or 0),
                             not bool(STAFFING.search(company[r["url"]])), region_priority(r["location"])))
    print(f"NEED LINK: {len(rows)}  showing top {min(SHOW, len(rows))}")
    for i, r in enumerate(rows[:SHOW], 1):
        print(f"{i}. [{r['fit_score']} h1b:{tier(r)}] {(r['title'] or '')[:60]} | {company[r['url']]} | {r['url']}")
    sys.exit()
q, why, seen = [], collections.Counter(), set()
for r in c.execute("SELECT * FROM jobs WHERE fit_score>=? AND applied_at IS NULL ORDER BY fit_score DESC, scored_at DESC", (FLOOR,)):
    a = (r["application_url"] or "").strip()
    desc = r["full_description"] or r["description"] or ""
    k = norm(a)
    lo, hi = d.parse_salary(r["salary"])
    if k in tried: why["already tried"] += 1; continue
    if k in dead: why["posting gone"] += 1; continue
    if k in screened: why["apply page refuses sponsorship / needs citizenship"] += 1; continue
    title = r["title"] or ""
    problem = title_problem(title, r["fit_score"], r["location"])
    if problem: why[problem] += 1; continue
    if EXCLUDE.search(title + " " + desc[:2500]): why["excluded industry"] += 1; continue
    if DESC_EXCLUDE.search(desc): why["description asks for experience you excluded (description_exclude)"] += 1; continue
    if NEED_SPONSORSHIP and SPON.search(desc): why["no sponsorship / clearance"] += 1; continue
    # pay floor is in USD, so it only applies to US jobs (Canada/Europe pay is in other currencies)
    if SALARY_MIN and hi is not None and hi < SALARY_MIN and not non_us_location(r["location"]): why[f"pay below ${SALARY_MIN:,}"] += 1; continue
    if not a or a == "None": why["no apply link"] += 1; continue
    if "--include-account-sites" not in sys.argv and any(x in a for x in NO_AUTOMATION): why["account needed / not automatable"] += 1; continue
    company = employer(r)
    if EXCLUDED_COMPANIES.search(company): why["company excluded in settings"] += 1; continue
    dup = (company.lower(), re.sub(r"\W+", " ", title.lower()).strip())
    if k in seen or (company and dup in seen): why["duplicate listing"] += 1; continue
    seen.update({k, dup})
    q.append({"score": r["fit_score"], "title": title, "company": company, "location": r["location"],
              "salary": r["salary"], "pay": hi or lo, "apply": a, "url": r["url"], "easy": any(x in a for x in EASY),
              "preferred": bool(PREFERRED.search(title)), "resume": resume_for(title)})

# fetch each apply page once (in parallel, free): drop gone postings and pages whose own text refuses
# sponsorship; both are remembered so no agent ever opens them
with cf.ThreadPoolExecutor(16) as ex:
    problems = dict(zip((id(j) for j in q), ex.map(lambda j: page_problem(j["apply"]), q)))
gone = [j for j in q if problems[id(j)] == "dead"]
refused = [j for j in q if (problems[id(j)] or "").startswith("screened")]
if gone:
    with open(dead_file, "a") as fh:
        fh.writelines(norm(j["apply"]) + "\n" for j in gone)
    why["posting gone"] += len(gone)
if refused:
    with open(screened_file, "a") as fh:
        fh.writelines(f"{norm(j['apply'])}\t{problems[id(j)]}\n" for j in refused)
    why["apply page refuses sponsorship / needs citizenship"] += len(refused)
q = [j for j in q if not problems[id(j)]]

tiers = h1b_tiers(j["company"] for j in q)
for j in q:
    j["h1b"] = tiers.get(j["company"], "unknown")
# optional hard filter (settings: h1b_skip_tiers, e.g. ["none"]): drop US jobs at employers whose DOL filing history
# is in those tiers. Only US jobs, since the data is US filings; "unknown" (name not matched) is never dropped.
SKIP_TIERS = set(S.get("h1b_skip_tiers") or []) - {"unknown"}
if NEED_SPONSORSHIP and SKIP_TIERS:
    before = len(q)
    q = [j for j in q if not (j["h1b"] in SKIP_TIERS and not non_us_location(j["location"]))]
    if before - len(q):
        why[f"US employer with H-1B history: {'/'.join(sorted(SKIP_TIERS))}"] += before - len(q)
# Highest fit score first; within a score, preferred titles first, then the strongest H-1B sponsors (when you need
# sponsorship), then highest posted pay, then jobs that don't post pay. Staffing / region / easy form only break remaining ties.
q.sort(key=lambda j: (-j["score"], not j["preferred"], TIER_RANK[j["h1b"]], -(j["pay"] or 0), not bool(STAFFING.search(j["company"])),
                      region_priority(j["location"]), not j["easy"]))
# company cap: keep the best COMPANY_CAP jobs per company (counting applications already submitted); staffing firms exempt
done, kept = recent_applications(), []
for j in q:
    key = co_key(j["company"])
    if key and not STAFFING.search(j["company"]):
        if done[key] >= COMPANY_CAP:
            why[f"company already has {COMPANY_CAP} applications in {CAP_DAYS} days"] += 1
            continue
        done[key] += 1
    kept.append(j)
q = kept
# optional mix (settings: preferred_share, e.g. 0.75): interleave preferred and other titles so roughly that share of
# the queue, from the top down, is preferred titles. Each group keeps its own best-first order.
SHARE = S.get("preferred_share")
if SHARE:
    pref, other, mixed = [j for j in q if j["preferred"]], [j for j in q if not j["preferred"]], []
    while pref or other:
        want_pref = sum(j["preferred"] for j in mixed) < float(SHARE) * (len(mixed) + 1)
        mixed.append((pref if (want_pref and pref) or not other else other).pop(0))
    q = mixed
(HERE / "queue.json").write_text(json.dumps(q, indent=1))
print("skipped:", dict(why))
if NEED_SPONSORSHIP:
    print("h1b tiers:", dict(collections.Counter(j["h1b"] for j in q)))
print(f"TO APPLY: {len(q)}  ({sum(j['easy'] for j in q)} easy one-page forms)  showing top {min(SHOW, len(q))}")
for i, j in enumerate(q[:SHOW], 1):
    pay = f"${j['pay']/1000:.0f}k" if j["pay"] else "pay n/a"
    print(f"{i}. [{j['score']} {pay}{' easy' if j['easy'] else ''} h1b:{j['h1b']}] {j['title'][:55]} | {j['company']} | {(j['location'] or '')[:30]} | {j['apply']}")
