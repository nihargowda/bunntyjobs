#!/usr/bin/env python3
"""Live job-search dashboard for ApplyPilot.

Run:  python3 dashboard.py [--log-dir DIR ...] [--port 8765]
Then open http://localhost:8765  — the page refreshes itself every 3 seconds.

Reads ~/.applypilot/applypilot.db (read-only) plus every pipeline *.log in the log
directories, so you can watch discovery, scoring, and drafting as they happen.
Nothing here changes ApplyPilot's data.
"""
import argparse, calendar, glob, gzip, hashlib, json, os, re, secrets, signal, sqlite3, subprocess, threading, time
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "auto"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_url import app_key, is_submitted, normalize  # noqa: E402  (shared with auto/apply_control.py)
import apply_control
from settings import SETTINGS, rx  # noqa: E402

APP = Path.home() / ".applypilot"
DB = APP / "applypilot.db"
MIN_SALARY = 100_000
GOOD_SCORE = 8
LOG_DIRS = [APP / "logs"]
APPLYPILOT = Path.home() / ".applypilot-venv" / "bin" / "applypilot"
TOKEN = secrets.token_hex(16)  # Run/Stop requests must carry this, so other web pages can't trigger them
# Find-and-draft presets only. There is deliberately no "apply" option.
PRESETS = {
    "full":     ("Full run: find → score → draft", ["run", "--min-score", "8", "--validation", "lenient"]),
    "discover": ("Find new jobs only",              ["run", "discover"]),
    "score":    ("Enrich + score",                  ["run", "enrich", "score"]),
    "drafts":   ("Draft resumes + cover letters (8+)", ["run", "tailor", "cover", "pdf", "--min-score", "8", "--validation", "lenient"]),
    "stream":   ("Score + draft together (streaming)", ["run", "score", "tailor", "cover", "pdf", "--stream", "--min-score", "8", "--validation", "lenient"]),
}


def running_procs():
    """ApplyPilot pipeline processes (and the shells that chain them) currently running."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if "applypilot run" in cmd and "dashboard.py" not in cmd and int(pid) != os.getpid():
            m = re.search(r"applypilot run([^;>\n'\"]*)", cmd)
            procs.append({"pid": int(pid), "what": ("run" + (m.group(1) if m else "")).strip(),
                          "main": "/bin/applypilot run" in cmd and not cmd.lstrip().startswith(("/bin/zsh", "/bin/sh", "/bin/bash", "zsh", "sh", "bash"))})
    return procs


def start_run(preset):
    if preset not in PRESETS:
        return False, "Unknown run type"
    if any(p["main"] for p in running_procs()):
        return False, "A run is already in progress. Stop it first."
    logdir = APP / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / f"{preset}-{datetime.now():%Y%m%d-%H%M%S}.log"
    args = " ".join(f"'{a}'" for a in PRESETS[preset][1])
    cmd = f"'{APPLYPILOT}' {args} >> '{log}' 2>&1; echo \"EXIT: $?\" >> '{log}'"
    subprocess.Popen(["/bin/sh", "-c", cmd], cwd=APP, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, f"Started: {PRESETS[preset][0]}"


def stop_runs():
    procs = running_procs()
    # stop the chaining shells first so they don't launch the next step, then the pipelines
    for p in sorted(procs, key=lambda p: p["main"]):
        try:
            os.kill(p["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return bool(procs), (f"Stopped {sum(p['main'] for p in procs)} run(s)" if procs else "Nothing was running")
WEST = rx(SETTINGS["region_priority"][:1])  # your first preferred region (settings: region_priority)


def parse_salary(s):
    if not s:
        return None, None
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]
    if not nums:
        return None, None
    mult = 2080 if "hour" in s.lower() else 12 if "month" in s.lower() else 1
    nums = [n * mult for n in nums]
    return min(nums), max(nums)


KNOWN_HOSTS = {"amazon.jobs": "Amazon", "careers.google.com": "Google", "apply.careers.microsoft.com": "Microsoft",
               "careers.microsoft.com": "Microsoft", "metacareers.com": "Meta", "jobs.apple.com": "Apple"}
# ATS hosts where the company is the first path segment, e.g. jobs.lever.co/<company>/...
PATH_ATS = ("jobs.lever.co", "jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io",
            "jobs.smartrecruiters.com", "apply.workable.com", "ats.rippling.com", "jobs.jobvite.com")
# ATS hosts where the company is the subdomain, e.g. <company>.applytojob.com
SUB_ATS = ("myworkdayjobs.com", "applytojob.com", "avature.net", "icims.com", "breezy.hr", "bamboohr.com",
           "recruitee.com", "teamtailor.com", "pinpointhq.com", "jazzhr.com", "contacthr.com")
# Aggregators / job boards that say nothing about the employer
NO_INFO = ("grnh.se", "adp.com", "appcast.io", "recruitics.com", "dayforcehcm.com", "paycomonline.net", "ultipro.com",
           "governmentjobs.com", "myworkdaysite.com", "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
           "lnkd.in", "talent.com", "app.jobvite.com", "jobs2careers", "click.", "taleo.net", "successfactors", "oraclecloud.com")
NOT_NAMES = {"the", "we", "our", "this", "job", "position", "role", "company", "who", "what", "overview", "summary",
             "description", "us", "you", "team", "corporate", "a", "an", "about", "join", "here", "there", "every", "work"}


def tidy(name):
    name = re.sub(r"[-_]+", " ", name).strip()
    name = re.sub(r"\s+(careers?|jobs|external|inc)$", "", name, flags=re.I)
    return name.title() if name.islower() or name.isupper() and len(name) > 4 else name


def company_from_link(u):
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", u or "")
    if not m:
        return ""
    host, path = m.group(1).lower(), (m.group(2) or "/")
    if host in KNOWN_HOSTS or host.removeprefix("www.") in KNOWN_HOSTS:
        return KNOWN_HOSTS.get(host) or KNOWN_HOSTS[host.removeprefix("www.")]
    if any(h in host for h in NO_INFO):
        return ""
    if host == "secure.collage.co":  # secure.collage.co/jobs/<company>/...
        path = path.removeprefix("/jobs")
    if host in PATH_ATS or host == "secure.collage.co":
        seg = path.strip("/").split("/")[0]
        return tidy(seg) if len(seg) > 2 else ""  # short ids like workable's /j/<id> say nothing
    for a in SUB_ATS:
        if host.endswith("." + a):
            sub = re.sub(r"^(careers?|jobs)-", "", host[: -len(a) - 1].split(".")[0])
            sub = re.sub(r"(us|global)$", "", sub) if host.endswith("avature.net") else sub
            return tidy(sub)
    parts = [x for x in host.split(".") if x not in ("www", "careers", "jobs", "apply", "career", "job", "hire", "recruiting")]
    return tidy(parts[-2]) if len(parts) >= 2 else ""


DESC_PATTERNS = [
    re.compile(r"\bAt ([A-Z][\w&.'’-]*(?: [A-Z][\w&.'’-]*){0,3}),"),
    re.compile(r"^\W*(?:About|Join) (?:us at )?([A-Z][\w&.'’-]*(?: [A-Z][\w&.'’-]*){0,3})\b"),
    re.compile(r"^\W*([A-Z][\w&.'’-]*(?: [A-Z][\w&.'’-]*){0,3}) (?:is|has been) (?:a|an|the|one of)\b"),
    re.compile(r"\bjoin(?:ing)? (?:the )?([A-Z][\w&.'’-]*(?: [A-Z][\w&.'’-]*){0,2}) team\b"),
]


def company_from_desc(desc):
    head = re.sub(r"[*#_]+", "", (desc or "")[:700])
    for rx in DESC_PATTERNS:
        m = rx.search(head)
        if m:
            name = m.group(1).strip(" .,'’")
            if name.split()[0].lower() not in NOT_NAMES and len(name) > 1:
                return name
    return ""


def resolve_company(r):
    """Return (company, how) where how is 'listed', 'link', or 'guess'."""
    keys = r.keys()
    if "company" in keys and (r["company"] or "").strip():
        return r["company"].strip(), "listed"
    if r["strategy"] == "workday_api" and r["site"]:
        return r["site"], "listed"
    for u in (r["application_url"], r["url"]):
        c = company_from_link(u)
        if c:
            return c, "link"
    c = company_from_desc(r["full_description"] or r["description"])
    return (c, "guess") if c else ("", "")


STAGE_RE = re.compile(r"STAGE:\s*(\w+)")
SCORE_RE = re.compile(r"^(\d\d:\d\d:\d\d)(?: - INFO -)? \[(\d+)/(\d+)\] score=(\d+)\s+(.*)$")
SEARCH_RE = re.compile(r'^(\d\d:\d\d:\d\d) - INFO - \["(.+?)" in (.+?)\s*\[tier \d+\]\] (\d+) results -> (\d+) new')
DRAFT_RE = re.compile(r"^(\d\d:\d\d:\d\d) - INFO - \d+/\d+ \[(\w+)\].*\| (.+)$")
WAIT_RE = re.compile(r"^(\d\d:\d\d:\d\d) (?:- WARNING - )?LLM rate limited \(HTTP (\d+)\)\. Waiting (\d+)s")
APPS_CSV = Path(__file__).resolve().parent / "Applications" / "applications_log.csv"
# Every agent records into the one shared log (see auto/APPLICATION_AGENT_COORDINATION.md).
APPS_CSVS = {"All agents": APPS_CSV}
EVENT_RE = re.compile(r"^(\d\d:\d\d:\d\d) - (WARNING|ERROR) - (.*)$")
QPROG_RE = re.compile(r"(?:\[([^\]]+)\] )?Progress: (\d+)/(\d+) (queries|employers)[^(]*\((\d+) new")
ITEM_RE = re.compile(r"\[(\d+)/(\d+)\]")
TIME_RE = re.compile(r"^(\d\d:\d\d:\d\d)")


def friendly(msg):
    if "rate limited" in msg:
        w = re.search(r"\d+s", msg)
        return "Gemini is busy, waiting and retrying" + (f" ({w.group(0)})" if w else "")
    if "LLM error" in msg:
        m = re.search(r"'([^']+)'", msg)
        return "Couldn't score: " + (m.group(1) if m else msg)
    if "429" in msg and "LinkedIn" in msg:
        return "LinkedIn is rate-limiting the search"
    if "jobKeysMap" in msg:
        return "ZipRecruiter scraper failed"
    return msg


def open_logs():
    """Log files currently held open by a running ApplyPilot process (i.e. steps that are really running)."""
    pids = [str(p["pid"]) for p in running_procs()]
    if not pids:
        return set()
    out = subprocess.run(["lsof", "-Fn", "-p", ",".join(pids)], capture_output=True, text=True).stdout
    return {ln[1:] for ln in out.splitlines() if ln.startswith("n") and ln.endswith(".log")}


def read_activity():
    logs = []
    for d in LOG_DIRS:
        logs += glob.glob(str(Path(d) / "*.log"))
    tasks, live, recent, events, scanned = [], {}, [], [], [0]
    live_files = {os.path.realpath(f) for f in open_logs()}
    now = datetime.now().timestamp()
    for p in sorted(set(logs), key=os.path.getmtime):
        try:
            lines = Path(p).read_text(errors="replace").splitlines()
        except OSError:
            continue
        mtime = os.path.getmtime(p)
        ended = any(("EXIT:" in l) or ("Pipeline complete" in l) or ("Pipeline finished" in l) for l in lines[-6:])
        is_today = datetime.fromtimestamp(mtime).date() == datetime.now().date()
        t = {"name": Path(p).stem, "stage": None, "done": 0, "total": 0, "unit": "", "new": None, "now": "", "nowt": "",
             "detail": "", "active": not ended and (os.path.realpath(p) in live_files or (now - mtime) < 45),
             "updated": datetime.fromtimestamp(mtime).strftime("%H:%M:%S"), "mtime": mtime}
        for ln in lines:
            if m := SEARCH_RE.match(ln):
                tm, q, loc, n, new = m.groups()
                t["now"], t["nowt"] = f"Searched “{q}” in {loc.strip()}: {n} listings, {new} new", tm
                t["stage"] = t["stage"] or "discover"
                if is_today:
                    scanned[0] += int(n)
            elif m := DRAFT_RE.match(ln):
                tm, res, title = m.groups()
                verb = {"tailor": "Tailored resume", "cover": "Wrote cover letter"}.get(t["stage"], "Drafted")
                t["now"], t["nowt"] = f"{verb}: {title.strip()}" + ("" if res in ("APPROVED", "OK") else f" ({res.lower()})"), tm
            elif m := WAIT_RE.match(ln):
                t["now"], t["nowt"] = f"Paused: AI provider rate limit, retrying in {max(1, int(m.group(3)) // 60)} min", m.group(1)
            if "STREAMING MODE" in ln:
                t["name"] += " (streaming)"
            if m := STAGE_RE.search(ln):
                t["stage"], t["done"], t["total"], t["detail"] = m.group(1), 0, 0, ""
            elif m := SCORE_RE.match(ln):
                tm, i, n, sc, title = m.groups()
                t["done"], t["total"], t["unit"] = int(i), int(n), "jobs scored"
                if t["stage"] in (None, "discover", "enrich"):
                    t["stage"] = "score"
                if int(sc) > 0:
                    live[title.strip()] = int(sc)
                recent.append({"t": tm, "score": int(sc), "title": title.strip()})
                t["now"], t["nowt"] = (f"Scored {sc}/10: {title.strip()}" if int(sc) > 0 else f"Score failed: {title.strip()}"), tm
            elif m := QPROG_RE.search(ln):
                label, i, n, unit, new = m.groups()
                t["done"], t["total"], t["unit"], t["new"] = int(i), int(n), unit + " searched", int(new)
                t["detail"] = f"{label} · " if label else ""
            elif m := EVENT_RE.match(ln):
                events.append({"t": m.group(1), "level": m.group(2), "msg": friendly(m.group(3))[:140], "src": t["name"]})
            elif (m := ITEM_RE.search(ln)) and t["stage"] in ("tailor", "cover", "pdf", "enrich"):
                t["done"], t["total"], t["unit"] = int(m.group(1)), int(m.group(2)), "jobs " + {"tailor": "tailored", "cover": "with cover letters", "pdf": "rendered", "enrich": "enriched"}[t["stage"]]
        tasks.append(t)
    # de-dupe repeated rate-limit chatter, newest first
    seen, ev = set(), []
    for e in sorted(events, key=lambda e: e["t"], reverse=True):
        k = (e["msg"][:40], e["src"])
        if k not in seen:
            seen.add(k); ev.append(e)
    tasks.sort(key=lambda t: (not t["active"], -t["mtime"]))
    for t in tasks:
        t.pop("mtime")
    return {"tasks": tasks[:6], "recent": sorted(recent, key=lambda r: r["t"], reverse=True)[:14],
            "events": ev[:6], "running": any(t["active"] for t in tasks), "scanned_today": scanned[0]}, live


def read_applications():
    """Today's rows from every agent's applications_log.csv (APPS_CSVS), latest status per job, tagged with who logged it."""
    import csv
    out = {"rows": [], "updated": None, "dupes": 0}
    today = apply_control.day()
    rank = {"applied": 3, "needs": 2, "skipped": 1}
    latest, mtimes = {}, []
    for by, path in APPS_CSVS.items():
        if not path.exists():
            continue
        mtimes.append(path.stat().st_mtime)
        mine = {}  # this log's latest row per job
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                for i, r in enumerate(csv.DictReader(fh)):
                    if (r.get("date") or "").strip() != today:
                        continue
                    st = (r.get("status") or "").strip().lower()
                    kind = "applied" if st.startswith("submit") or st == "applied" else "needs" if "needs you" in st or st.startswith("filled") else "skipped" if st.startswith("skip") else st
                    key = app_key(r.get("company"), r.get("title"))
                    # A later blocker must never erase a verified submission.
                    if key in mine and mine[key]["kind"] == "applied" and kind != "applied":
                        continue
                    mine[key] = {"i": i, "company": (r.get("company") or "").strip(), "title": (r.get("title") or "").strip(),
                                 "kind": kind, "status": (r.get("status") or "").strip(), "notes": (r.get("notes") or "").strip(), "by": [by]}
        except OSError:
            continue
        for key, row in mine.items():
            prev = latest.get(key)
            if prev is None:
                latest[key] = row
                continue
            if by not in prev["by"]:
                prev["by"].append(by)
            if prev["kind"] == "applied" and row["kind"] == "applied" and prev["status"] != row["status"]:
                out["dupes"] += 1  # both agents logged a submission for the same job
            if rank.get(row["kind"], 0) > rank.get(prev["kind"], 0):
                row["by"] = prev["by"]
                latest[key] = row
    out["updated"] = datetime.fromtimestamp(max(mtimes)).strftime("%I:%M %p") if mtimes else None
    out["rows"] = sorted(latest.values(), key=lambda x: -x["i"])
    return out


_READY = {"mtime": None, "urls": {}}


def ready_jobs():
    """Jobs in the apply queue (auto/queue.json), by cleaned link -> (company, title); re-read when it changes."""
    p = apply_control.ROOT / "auto" / "queue.json"
    try:
        m = p.stat().st_mtime
        if m != _READY["mtime"]:
            urls = {}
            for j in json.loads(p.read_text()):
                for k in (j.get("url"), j.get("apply")):
                    if k:
                        urls[normalize(k)] = (j.get("company") or "", j.get("title") or "")
            _READY.update(mtime=m, urls=urls)
    except (OSError, ValueError):
        pass
    return _READY


def pipeline_events(since=""):
    """Real jobs moving through the pipeline, for the Live workflow tab: found, scored (and where they
    went next), claimed by an agent, and today's logged outcomes. Times are UTC 'YYYY-MM-DD HH:MM:SS'.
    Replays at most the last 15 minutes; overlaps the cursor by 2 minutes (the page drops repeats)."""
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    floor = fmt(time.time() - (900 if since else 600))
    since = re.sub(r"[^0-9: -]", "", since or "")[:19]
    try:
        since = fmt(calendar.timegm(time.strptime(since, "%Y-%m-%d %H:%M:%S")) - 120) if since else ""
    except ValueError:  # malformed cursor: replay from the floor instead of dropping the request
        since = ""
    since = max(since, floor)
    ts = lambda c: f"replace(substr({c},1,19),'T',' ')"
    rq = ready_jobs()
    out = {"now": fmt(time.time()), "found": [], "scored": [], "claims": [], "outcomes": [], "more": {}}
    try:  # between pipeline cycles there is no -wal/-shm file and plain read-only opening fails
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM jobs LIMIT 1")
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        cols = "url, application_url, title, company, site, strategy, fit_score, score_reasoning"
        for kind, col, lim in (("found", "discovered_at", 25), ("scored", "scored_at", 40)):
            q = f"FROM jobs WHERE {ts(col)} > ?"
            out["more"][kind] = conn.execute(f"SELECT COUNT(*) {q}", (since,)).fetchone()[0]
            for r in conn.execute(f"SELECT {cols}, {ts(col)} AS t {q} ORDER BY t DESC LIMIT {lim}", (since,)):
                e = {"t": r["t"], "url": r["url"], "company": resolve_company(r)[0] or r["site"] or "", "title": r["title"] or "(untitled)"}
                if kind == "scored":
                    why, s = r["score_reasoning"] or "", r["fit_score"]
                    if why.startswith(("Auto-filtered (no AI)", "excluded:")):
                        e.update(route="excluded", why=why.split("\n")[0][:120])
                    elif s is None or s <= 0:
                        e.update(route="failed")
                    elif s < GOOD_SCORE:
                        e.update(route="low", score=s)
                    else:
                        inq = any(normalize(u) in rq["urls"] for u in (r["url"], r["application_url"]) if u)
                        e.update(score=s, route="apply" if inq else "pending" if (rq["mtime"] or 0) < calendar.timegm(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S")) else "held")
                out[kind].append(e)
            out["more"][kind] -= len(out[kind])
    finally:
        conn.close()
    # Claims: an agent has started a job. Stored in your local time (settings: timezone).
    try:
        with apply_control.connect() as c:
            for r in c.execute("SELECT agent, url, created FROM claims ORDER BY created DESC LIMIT 15"):
                t = fmt(time.mktime(time.strptime(r["created"][:19], "%Y-%m-%dT%H:%M:%S")))
                if t > since:
                    co, ti = rq["urls"].get(normalize(r["url"]), ("", ""))
                    out["claims"].append({"t": t, "url": r["url"], "agent": r["agent"], "company": co, "title": ti})
    except (sqlite3.Error, ValueError):
        pass
    # Today's latest logged outcome per job, from every agent's log.
    import csv
    latest = {}
    for by, path in APPS_CSVS.items():
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    if (r.get("date") or "").strip() == apply_control.day():
                        k = app_key(r.get("company"), r.get("title"))
                        latest.pop(k, None)
                        latest[k] = {"company": (r.get("company") or "").strip(), "title": (r.get("title") or "").strip(),
                                     "status": (r.get("status") or "").strip(), "by": by}
        except OSError:
            continue
    out["outcomes"] = list(latest.values())[-25:]
    return out


def application_ledger():
    """Every confirmed submission (status submitted/applied) in every agent's applications log,
    one entry per job (app_key), dated by the first day it was submitted. This is the single
    source for every 'applied' number on the page."""
    import csv
    ledger = {}
    for by, path in APPS_CSVS.items():
        if not path.exists():
            continue
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    day = (r.get("date") or "").strip()
                    if not is_submitted(r.get("status")) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                        continue
                    k = app_key(r.get("company"), r.get("title"))
                    if not k[1]:
                        continue
                    if k not in ledger or day < ledger[k]["date"]:
                        ledger[k] = {"date": day, "company": (r.get("company") or "").strip(),
                                     "title": (r.get("title") or "").strip(), "by": by}
        except OSError:
            continue
    return ledger


TITLE_NOISE = {"and", "of", "the", "for", "a", "an", "to", "in", "at", "hybrid", "remote", "onsite", "on", "site", "eligible"}


def title_words(title):
    t = re.sub(r"\(.*?\)", " ", (title or "").lower())  # locations, pay ranges, (Remote) ...
    t = re.sub(r"\bpm\b", "product manager", t)
    t = re.sub(r"\bsr\b\.?", "senior", t)
    return {w for w in re.findall(r"[a-z0-9]+", t) if w not in TITLE_NOISE}


def title_sim(a, b):
    A, B = title_words(a), title_words(b)
    return 2 * len(A & B) / (len(A) + len(B)) if A and B else 0.0


def day_gap(a, b):
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return 99


DB_ONLY_BY_DAY = {}  # applications marked only in the database, by day; refreshed on every full rebuild


def apply_ledger(jobs):
    """Mark exactly one job row per application as Applied, so the Today tiles, Daily table,
    Insights total and Jobs filter all count the same applications.

    Applications = every confirmed submission in the agents' logs (application_ledger), plus any job
    marked applied in the database that no log entry accounts for. Log entries are tied to database
    jobs by exact company + title first, then by same company, dates within a day and mostly the same
    title words (agents reword titles: "Senior PM, ..." vs "Senior Product Manager, ...")."""
    ledger = application_ledger()
    index = {}
    for n, jb in enumerate(jobs):
        index.setdefault(app_key(jb["company"], jb["title"]), []).append(n)
    claimed, unmatched = set(), []
    for k, ev in ledger.items():  # 1. exact identity
        cands = [n for n in index.get(k, []) if n not in claimed]
        pick = next((n for n in cands if jobs[n]["_db_applied"]), cands[0] if cands else None)
        if pick is None:
            unmatched.append((k, ev))
            continue
        claimed.add(pick)
        jobs[pick]["status"], jobs[pick]["dA"] = "Applied", ev["date"]
    # 2. reworded titles: pair leftover log entries with leftover database applications
    pairs = []
    loose = [n for n, jb in enumerate(jobs) if jb["_db_applied"] and n not in claimed]
    for u, (k, ev) in enumerate(unmatched):
        for n in loose:
            jb = jobs[n]
            if app_key(jb["company"], "")[0] != k[0] or day_gap(jb["dA"], ev["date"]) > 1:
                continue
            sim = title_sim(jb["title"], ev["title"])
            if sim >= 0.6:
                pairs.append((-sim, day_gap(jb["dA"], ev["date"]), u, n))
    used_u = set()
    for _, _, u, n in sorted(pairs):
        if u in used_u or n in claimed:
            continue
        used_u.add(u); claimed.add(n)
        jobs[n]["status"], jobs[n]["dA"] = "Applied", unmatched[u][1]["date"]
    # 3. logged applications to jobs ApplyPilot never saw (e.g. found by Codex): give each a row
    for u, (k, ev) in enumerate(unmatched):
        if u in used_u:
            continue
        jobs.append({"title": ev["title"], "company": ev["company"], "chow": "listed", "location": "", "remote": False,
                     "west": False, "salary": "", "smin": None, "smax": None, "site": "applications log",
                     "url": "log:" + "|".join(k), "apply_url": "", "found": ev["date"], "score": None, "live": False,
                     "reason": f"Logged as submitted by {ev['by']}; not in the ApplyPilot database.", "resume": "",
                     "cover": "", "resume_pdf": "", "cover_pdf": "", "status": "Applied", "desc": "", "sfail": False,
                     "dF": None, "dS": None, "dT": None, "dC": None, "dA": ev["date"], "_db_applied": False})
    # 4. applied in the database but in no log: still an application, unless it's another listing of one
    #    already counted (same apply link, or same company + mostly the same title within a day)
    counted = [jobs[n] for n in claimed] + [jb for jb in jobs if jb["site"] == "applications log"]
    for n, jb in enumerate(jobs):
        if n in claimed or not jb.get("_db_applied"):
            continue
        co = app_key(jb["company"], "")[0]
        dup = any((jb["apply_url"] and jb["apply_url"] == c["apply_url"]) or
                  (app_key(c["company"], "")[0] == co and day_gap(c["dA"], jb["dA"]) <= 1 and title_sim(c["title"], jb["title"]) >= 0.6)
                  for c in counted)
        if dup:
            jb["status"], jb["dA"] = "Duplicate listing", None
        else:
            jb["status"] = "Applied"
            jb["reason"] = "Marked applied in the ApplyPilot database but missing from the applications log; please confirm. " + jb["reason"]
            counted.append(jb)
    DB_ONLY_BY_DAY.clear()
    for n, jb in enumerate(jobs):
        if jb.get("_db_applied") and jb["status"] == "Applied" and n not in claimed and jb["dA"]:
            DB_ONLY_BY_DAY[jb["dA"]] = DB_ONLY_BY_DAY.get(jb["dA"], 0) + 1
    for jb in jobs:
        jb.pop("_db_applied", None)
    return jobs


def load_jobs(live_scores):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    conn.close()
    jobs = []
    for r in rows:
        lo, hi = parse_salary(r["salary"])
        desc = re.sub(r"\s+", " ", re.sub(r"[*#_]+", "", r["full_description"] or r["description"] or "")).strip()
        title = r["title"] or "(untitled)"
        loc = r["location"] or ""
        # Scores come only from the database. (Matching log scores by title copied one job's
        # score onto every job with the same title, inflating the counts.)
        score, live = r["fit_score"], False
        filtered = (r["score_reasoning"] or "").startswith(("Auto-filtered (no AI)", "excluded:"))  # set aside by a rule, not by AI
        if filtered:
            score = None
        failed = score is not None and score <= 0  # -1 / 0 mean the AI call failed, not a real score
        if failed:
            score = None
        db_applied = bool(r["applied_at"] or (r["apply_status"] or "").lower() in ("applied", "submitted"))
        if r["tailored_resume_path"] or r["cover_letter_path"]:
            status = "Drafted"
        elif filtered:
            status = "Filtered out"
        elif score is not None and score >= GOOD_SCORE:
            status = "Good fit"
        elif score is not None:
            status = "Scored"
        else:
            status = "Not scored"
        company, how = resolve_company(r)
        jobs.append({"title": title, "company": company, "chow": how,
                     "location": loc, "remote": "remote" in loc.lower(), "west": bool(WEST.search(loc)),
                     "salary": r["salary"] or "", "smin": lo, "smax": hi,
                     "site": r["site"] or r["strategy"] or "other", "url": r["url"],
                     "apply_url": r["application_url"] or r["url"], "found": (r["discovered_at"] or "")[:16].replace("T", " "),
                     "score": score, "live": live, "reason": r["score_reasoning"] or "",
                     "resume": r["tailored_resume_path"] or "", "cover": r["cover_letter_path"] or "",
                     "resume_pdf": pdf_of(r["tailored_resume_path"]), "cover_pdf": pdf_of(r["cover_letter_path"]),
                     "status": status, "desc": desc[:1500], "sfail": failed,
                     "dF": local_day(r["discovered_at"]), "dS": local_day(r["scored_at"], naive_utc=True),
                     "dT": local_day(r["tailored_at"]), "dC": local_day(r["cover_letter_at"]),
                     "dA": local_day(r["applied_at"], naive_utc=True) if db_applied else None, "_db_applied": db_applied})
    return apply_ledger(jobs)


DRAFT_DIRS = (APP / "tailored_resumes", APP / "cover_letters",
              Path(__file__).resolve().parent / "Applications" / "resumes_used")  # frozen copies of resumes sent


def pdf_of(path):
    if not path:
        return ""
    pdf = Path(path).with_suffix(".pdf")
    return str(pdf) if pdf.exists() else ""


def safe_draft_file(path):
    """Only serve files that live inside ApplyPilot's draft folders."""
    try:
        f = Path(path).resolve()
    except (OSError, ValueError):
        return None
    if f.suffix.lower() not in (".pdf", ".txt") or not f.is_file():
        return None
    return f if any(d.resolve() in f.parents for d in DRAFT_DIRS) else None


def local_day(ts, naive_utc=False):
    """'YYYY-MM-DD' in this Mac's time zone. UTC timestamps are converted; naive ones are taken as local."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)[:10]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    elif naive_utc or dt > datetime.now() + timedelta(minutes=10):  # written in UTC without a zone (e.g. SQLite datetime('now'))
        dt = dt.replace(tzinfo=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d")


def stage_counts(jobs):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    enriched = conn.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND length(full_description) > 200").fetchone()[0]
    paths = [r[0] for r in conn.execute("SELECT tailored_resume_path FROM jobs WHERE tailored_resume_path IS NOT NULL")]
    conn.close()
    pdfs = sum(1 for x in paths if Path(x).with_suffix(".pdf").exists())
    return {"discover": len(jobs), "enrich": enriched,
            "score": sum(1 for j in jobs if j["score"] and j["score"] > 0),
            "tailor": sum(1 for j in jobs if j["resume"]), "cover": sum(1 for j in jobs if j["cover"]), "pdf": pdfs,
            "good": sum(1 for j in jobs if j["score"] and j["score"] >= GOOD_SCORE)}


TRACKER = Path(__file__).resolve().parent / "tracker"
STATUS_JSON = Path(__file__).resolve().parent / "Applications" / "application_status.json"


def inbox_status():
    """Application statuses from email (tracker/mail_status.py, run each morning), for the Inbox card."""
    import sys
    if str(TRACKER) not in sys.path:
        sys.path.insert(0, str(TRACKER))
    try:
        import mail_status
        state = mail_status.load(mail_status.Paths())
    except Exception as e:  # tracker missing or file mid-write: show the card empty rather than break the page
        return {"error": type(e).__name__, "todo": [], "counts": {}, "recent": [], "updated": None}
    apps = state.get("apps", {})
    counts = {}
    for a in apps.values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    slim = lambda a: {k: a.get(k) for k in ("company", "title", "status", "status_date", "deadline", "deadline_text", "url", "in_log")} \
        | {"resume": (a.get("resume") or {}).get("name"),
           "resume_file": str(TRACKER.parent / a["resume"]["file"]) if (a.get("resume") or {}).get("file") else None,
           "resume_guess": "by date" in (a.get("resume") or {}).get("source", "")}
    recent = sorted((a for a in apps.values() if a["status"] in ("rejected", "offer")), key=lambda a: a["status_date"], reverse=True)[:6]
    return {"todo": [slim(a) for a in mail_status.upcoming(state)], "counts": counts, "recent": [slim(a) for a in recent],
            "updated": state.get("updated")}


LEADS_JSON = Path(__file__).resolve().parent / "Applications" / "recruiter_leads.json"


def recruiter_leads():
    """Recruiter outreach threads and where each stands (tracker/recruiters.py, run every 4 hours)."""
    try:
        state = json.loads(LEADS_JSON.read_text())
    except (OSError, ValueError):
        return {"leads": [], "counts": {}, "updated": None}
    keep = ("thread_id", "status", "note", "title", "recruiter", "location", "pay", "type", "last_seen", "url", "draft_url")
    leads = sorted(state.get("leads", {}).values(), key=lambda l: l.get("last_seen", ""), reverse=True)
    counts = {}
    for l in leads:
        counts[l["status"]] = counts.get(l["status"], 0) + 1
    return {"leads": [{k: l.get(k) for k in keep} for l in leads[:25]], "counts": counts, "updated": state.get("updated")}


PIPELINE_SH = APP / "pipeline.sh"
PATCH_COPY = Path(__file__).resolve().parent / "local-patches" / "pipeline.sh"
OR_STAMP = APP / ".openrouter-last-day"
PASS_RE = re.compile(r"^== \S+ (\d\d:\d\d):\d\d score \((\w+)(?: (fallback))?(?: ([^,)]+))?")
_or_key = {"ok": None, "info": {}, "checked": None}  # OpenRouter's view of the key, refreshed in the background


def env_keys():
    """Which provider keys are set in ~/.applypilot/.env. Only names and set/unset leave this function, never values."""
    keys = {}
    try:
        for ln in (APP / ".env").read_text().splitlines():
            k, sep, v = ln.partition("=")
            if sep and v.strip():
                keys[k.strip()] = v.strip()
    except OSError:
        pass
    return keys


def check_openrouter_key():
    """Ask OpenRouter about the saved key (GET /api/v1/key: free, not counted against the model request limit)."""
    import urllib.request, urllib.error
    while True:
        key = env_keys().get("OPENROUTER_API_KEY")
        if key:
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.load(r).get("data", {})
                _or_key.update(ok=True, info={k: d.get(k) for k in ("limit", "usage", "is_free_tier", "expires_at")})
            except urllib.error.HTTPError as e:
                _or_key.update(ok=False, info={"error": f"HTTP {e.code}"})
            except Exception as e:
                _or_key.update(ok=None, info={"error": type(e).__name__})
        else:
            _or_key.update(ok=None, info={})
        _or_key["checked"] = datetime.now().strftime("%H:%M")
        time.sleep(600)


def scoring_passes(lines):
    """Today's scoring passes from the pipeline log: provider, model, AI-scored count, and how each ended."""
    passes, cur = [], None
    for ln in lines:
        if ln.startswith("== "):
            if "starting discover" in ln or "discover finished" in ln:
                continue  # discovery runs in the background, so its lines can land in the middle of a pass
            if cur and not cur["done"]:  # the next step began without this pass logging "Done:" (e.g. a restart)
                cur["done"] = cur["cut"] = True
            m = PASS_RE.match(ln)
            cur = None
            if m:
                tm, prov, fb, model = m.groups()
                cur = {"t": tm, "provider": prov + (" fallback" if fb else ""), "model": (model or "").strip(),
                       "scored": 0, "errors": 0, "limited": False, "done": False, "cut": False}
                passes.append(cur)
        elif cur:
            if SCORE_RE.match(ln):  # prefilter skips log score=-1, which SCORE_RE doesn't match
                cur["scored"] += 1
            elif "not scored (LLM error)" in ln:
                cur["errors"] += 1
            elif "LLM quota exhausted" in ln:
                cur["limited"] = True
            elif " - Done: " in ln:
                cur["done"] = True
    return passes


def pipeline_day_lines(logdir, date):
    """Merge dated markers and results kept in a prior cycle's file after midnight."""
    events = []
    for offset in (1, 0):
        fileday = date - timedelta(days=offset)
        path = Path(logdir) / f"pipeline-{fileday:%Y%m%d}.log"
        current_day, previous_seconds = fileday, None
        try:
            content = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in content:
            marker = re.match(r"^== (\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d)", line)
            timed = re.match(r"^(\d\d:\d\d:\d\d)", line)
            if marker:
                current_day = datetime.strptime(marker[1], "%Y-%m-%d").date()
                clock = marker[2]
            elif timed:
                clock = timed[1]
            else:
                continue
            h, m, sec = map(int, clock.split(":"))
            seconds = h * 3600 + m * 60 + sec
            if not marker and previous_seconds is not None and previous_seconds - seconds > 43200:
                current_day += timedelta(days=1)
            previous_seconds = seconds
            if current_day == date:
                # Markers precede their own output when timestamps tie.
                events.append((seconds, 0 if marker else 1, len(events), line))
    return [e[3] for e in sorted(events)]


def scoring_health():
    """Checks for the background scoring pipeline (launchd pipeline.sh), shown as the Scoring health card."""
    checks = []
    add = lambda name, state, detail: checks.append({"name": name, "state": state, "detail": detail})
    today = datetime.now().strftime("%Y-%m-%d")
    lines = pipeline_day_lines(APP / "logs", datetime.now().date())
    steps = [l for l in lines if l.startswith("== ")]

    running = subprocess.run(["pgrep", "-f", str(PIPELINE_SH)], capture_output=True).returncode == 0
    last = steps[-1][3:] if steps else "no steps logged today"
    add("Pipeline running", "ok" if running else "err",
        f"Last step: {last}" if running else f"pipeline.sh isn't running (launchd {SETTINGS['launchd_label']})")

    try:
        same = PIPELINE_SH.read_bytes() == PATCH_COPY.read_bytes()
        add("pipeline.sh matches the committed copy", "ok" if same else "warn",
            "Live file = local-patches/pipeline.sh" if same else "Live ~/.applypilot/pipeline.sh differs from local-patches/pipeline.sh")
    except OSError as e:
        add("pipeline.sh matches the committed copy", "warn", f"Can't compare: {e.strerror}")

    keys = env_keys()
    names = [("Gemini", "GEMINI_API_KEY"), ("OpenRouter", "OPENROUTER_API_KEY"), ("Groq", "GROQ_API_KEY")]
    missing = [n for n, k in names if k not in keys]
    add("API keys saved in .env", "warn" if missing else "ok",
        ("Missing: " + ", ".join(missing)) if missing else "Gemini, OpenRouter and Groq keys are set (values never shown)")

    if "OPENROUTER_API_KEY" in keys:
        i = _or_key["info"]
        if _or_key["ok"]:
            lim = f"${i['limit']:g} limit" if i.get("limit") is not None else "no limit"
            exp = f", expires {str(i['expires_at'])[:10]}" if i.get("expires_at") else ""
            add("OpenRouter accepts the key", "ok", f"${(i.get('usage') or 0):g} used of {lim}{exp} · checked {_or_key['checked']}")
        elif _or_key["ok"] is False:
            add("OpenRouter accepts the key", "err", f"Rejected ({i.get('error')}) · checked {_or_key['checked']}")
        else:
            add("OpenRouter accepts the key", "warn", f"Not checked yet ({i.get('error', 'checking')})")

    passes = scoring_passes(lines)
    stamp = OR_STAMP.read_text().strip() if OR_STAMP.exists() else ""
    if "OPENROUTER_API_KEY" in keys:
        if stamp == today:
            add("OpenRouter's daily pass", "ok", "Had today's try; next one tomorrow")
        else:
            add("OpenRouter's daily pass", "wait", "Not yet today; runs after the next Gemini pass")

    for prov in ("gemini", "openrouter", "openrouter fallback", "groq"):
        ps = [p for p in passes if p["provider"] == prov]
        if not ps:
            continue
        n, lastp = sum(p["scored"] for p in ps), ps[-1]
        label = {"gemini": "Gemini", "openrouter": "OpenRouter", "openrouter fallback": "OpenRouter fallback", "groq": "Groq"}[prov]
        model = f" ({lastp['model']})" if lastp["model"] and not lastp["model"].startswith("up to") else ""
        if not lastp["done"]:
            state, how = "run", "running now"
        elif lastp["limited"]:
            state, how = ("warn" if lastp["scored"] == 0 else "ok"), "stopped at its rate limit"
        elif lastp["cut"]:
            state, how = "ok", "cut short (pipeline restarted)"
        else:
            state, how = "ok", "finished"
        errs = lastp["errors"]
        add(f"{label} scoring today{model}", state,
            f"{n} jobs AI-scored in {len(ps)} pass{'es' if len(ps) != 1 else ''}; last pass {lastp['t']}, {how}"
            + (f", {errs} error{'s' if errs != 1 else ''}" if errs else ""))

    # every agent records into the shared log; a job counted twice means two agents submitted it
    apps = read_applications()
    per = {b: sum(b in r["by"] for r in apps["rows"]) for b in APPS_CSVS}
    add("Applications logged today", "err" if apps["dupes"] else "ok",
        " · ".join(f"{b}: {n}" for b, n in per.items())
        + (f" · {apps['dupes']} job(s) submitted by BOTH agents" if apps["dupes"] else " · no job submitted twice"))
    tried = {}
    for b, csvp in APPS_CSVS.items():
        try:
            tried[b] = {l.strip() for l in (csvp.parent.parent / "auto" / "tried.txt").read_text(errors="replace").splitlines() if l.strip()}
        except OSError:
            pass
    if len(tried) == 2:
        (a, ta), (b, tb) = tried.items()
        gap = f"{len(ta - tb)} tried by {a} only, {len(tb - ta)} by {b} only"
        add("Tried lists in sync", "ok" if not (ta ^ tb) else "warn",
            "Both agents skip the same jobs" if not (ta ^ tb) else f"{gap}; either agent could retry a job the other already did")

    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        left = conn.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL").fetchone()[0]
        conn.close()
        add("Backlog", "ok" if left < 100 else "wait", f"{left:,} jobs with a description still waiting for a score")
    except sqlite3.Error:
        pass
    return checks


DESCS = {}  # url -> description; sent one at a time when a job is opened, not with every refresh
_cache = {"sig": None, "body": b"", "gz": b"", "ver": ""}
_lock = threading.Lock()


def data_signature():
    """Changes whenever the database, a log, or the set of running processes changes (and at least every 30s)."""
    parts = []
    for f in (DB, Path(str(DB) + "-wal"), *APPS_CSVS.values(), STATUS_JSON, LEADS_JSON, apply_control.STATE):
        try:
            st = f.stat(); parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    for d in LOG_DIRS:
        for p in glob.glob(str(Path(d) / "*.log")):
            try:
                parts.append(f"{p}:{os.path.getmtime(p)}")
            except OSError:
                pass
    parts.append(",".join(str(p["pid"]) for p in running_procs()))
    parts.append(str(int(time.time() // 30)))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


REBUILD_EVERY = 30  # seconds; a rebuild takes ~4s of CPU and the DB/logs change every few seconds while agents run


def cached_data():
    with _lock:
        sig = data_signature()
        pids = ",".join(str(p["pid"]) for p in running_procs())
        # rebuild at most every REBUILD_EVERY seconds, except right away when a run starts or stops (Run/Stop buttons)
        due = time.time() - _cache.get("built", 0) >= REBUILD_EVERY or pids != _cache.get("pids")
        if sig != _cache["sig"] and due:
            snap = snapshot()
            for j in snap["jobs"]:
                DESCS[j["url"]] = j.pop("desc", "")
            snap["ver"] = sig
            body = json.dumps(snap, separators=(",", ":")).encode()
            _cache.update(sig=sig, body=body, gz=gzip.compress(body, 5), ver=sig, built=time.time(), pids=pids)
        return _cache


def application_status():
    """Small status payload: no jobs database, pipeline logs, or process scans."""
    counts = {}
    for event in application_ledger().values():
        date = event["date"]
        counts[date] = counts.get(date, 0) + 1
    for date, n in DB_ONLY_BY_DAY.items():  # + database-only applications, so this equals the full page's count
        counts[date] = counts.get(date, 0) + n
    return {"apps": read_applications(), "inbox": inbox_status(), "recruiters": recruiter_leads(),
            "applicationControl": apply_control.status(), "appliedByDay": counts,
            "today": apply_control.day()}


def snapshot():
    act, live = read_activity()
    jobs = load_jobs(live)
    return {"jobs": jobs, "stages": stage_counts(jobs), "activity": act, "now": datetime.now().strftime("%I:%M:%S %p"),
            "minSalary": MIN_SALARY, "goodScore": GOOD_SCORE,
            "procs": [p for p in running_procs() if p["main"]], "health": scoring_health(),
            **application_status()}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/pipeline":
                body, ctype = json.dumps(pipeline_events(qs.get("since", [""])[0]), separators=(",", ":")).encode(), "application/json"
            elif u.path == "/workflow":
                body, ctype = json.dumps(apply_control.workflow_status()).encode(), "application/json"
            elif u.path == "/application-status":
                body, ctype = json.dumps(application_status(), separators=(",", ":")).encode(), "application/json"
            elif u.path == "/data":
                c = cached_data()
                if qs.get("v", [""])[0] == c["ver"]:
                    body, ctype = json.dumps({"same": True, "now": datetime.now().strftime("%I:%M:%S %p")}).encode(), "application/json"
                elif "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(len(c["gz"])))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(c["gz"])
                    return
                else:
                    body, ctype = c["body"], "application/json"
            elif u.path == "/file":
                f = safe_draft_file(qs.get("path", [""])[0])
                if not f:
                    self.send_error(404); return
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf" if f.suffix.lower() == ".pdf" else "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'inline; filename="{f.name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            elif u.path == "/job":
                url = qs.get("url", [""])[0]
                body, ctype = json.dumps({"desc": DESCS.get(url, "")}).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                opts = "".join(f'<option value="{k}">{v[0]}</option>' for k, v in PRESETS.items())
                body, ctype = PAGE.replace("__TOKEN__", TOKEN).replace("__TZ__", SETTINGS["timezone"]).replace("<!--OPTS-->", opts).encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404); return
        except sqlite3.OperationalError as e:  # DB busy mid-write; the page just retries
            body, ctype = json.dumps({"error": str(e)}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin") or ""
        if host not in ("localhost", "127.0.0.1") or (origin and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", origin)):
            self.send_error(403); return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            req = {}
        if req.get("token") != TOKEN:
            self.send_error(403); return
        if self.path == "/apply-now":
            try:
                msg = apply_control.request(req.get("agents", []))
                ok = True
            except ValueError as e:
                ok, msg = False, str(e)
            # Application requests are served independently of the jobs cache.
        elif self.path == "/run":
            ok, msg = start_run(req.get("preset", ""))
        elif self.path == "/stop":
            ok, msg = stop_runs()
        else:
            self.send_error(404); return
        body = json.dumps({"ok": ok, "msg": msg}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Search Live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#030504;--card:#0a0f0c;--card2:#0e1511;--ink:#e6f4ea;--muted:#7d9686;--line:#16241b;--chip:#111b15;
--g:#22e07a;--g2:#13a856;--gdim:#0f3a22;--glow:0 0 0 1px rgba(34,224,122,.08),0 0 24px rgba(34,224,122,.06);
--mid:#d6c64a;--low:#4b5d51;--warn:#e0b84a;--err:#ff6b6b}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
body{background-image:radial-gradient(ellipse 80% 40% at 50% -10%,rgba(34,224,122,.12),transparent 70%),linear-gradient(rgba(34,224,122,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(34,224,122,.025) 1px,transparent 1px);background-size:auto,32px 32px,32px 32px;background-attachment:fixed}
.mono{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}
.wrap{max-width:1280px;margin:0 auto;padding:22px 16px 64px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}
h1{margin:0;font-size:24px;font-weight:800;letter-spacing:-.02em}h1 b{color:var(--g)}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.live{display:inline-flex;align-items:center;gap:9px;border:1px solid var(--gdim);background:rgba(34,224,122,.06);padding:7px 14px;border-radius:999px;font-size:12.5px;font-weight:600;color:var(--g)}
.note{font-size:12.5px;color:var(--warn);background:rgba(224,184,74,.07);border:1px solid rgba(224,184,74,.25);border-radius:9px;padding:8px 12px;margin-bottom:12px}
.drw{display:grid;grid-template-columns:330px 1fr;gap:14px;min-height:560px}@media (max-width:860px){.drw{grid-template-columns:1fr}}
.drl{display:flex;flex-direction:column;gap:8px;min-width:0}
.drl input{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:8px 12px;font:inherit;outline:none}
#drlist{overflow:auto;max-height:620px;display:flex;flex-direction:column;gap:6px;padding-right:4px}
.dri{display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:start;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px;cursor:pointer;transition:all .15s}
.dri:hover{border-color:var(--g2)}.dri.sel{border-color:var(--g);background:rgba(34,224,122,.07)}
.dri .t{font-size:13px;line-height:1.3}.dri .m{color:var(--muted);font-size:11.5px;margin-top:3px}
.dri .ok{font:600 10px "JetBrains Mono",monospace;color:var(--g);margin-top:4px}.dri .ok.miss{color:var(--muted)}
.drv{display:flex;flex-direction:column;min-width:0;background:var(--bg);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.drh{padding:12px 14px;border-bottom:1px solid var(--line)}.drh .t{font-weight:700}.drh .links{margin:6px 0 0;gap:14px;font-size:12.5px}
.tabs{display:flex;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tabs button{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:7px;padding:5px 12px;font:600 12px Inter,sans-serif;cursor:pointer}
.tabs button.on{background:var(--g);border-color:var(--g);color:#02140a}.tabs .sp{flex:1}.tabs .fmt{display:flex;gap:4px}.tabs a{font-size:12px;margin-left:8px}
.drbody{flex:1;min-height:480px;display:flex}.drbody iframe{flex:1;border:0;background:#fff;min-height:620px;width:100%}
.drbody pre{flex:1;margin:0;padding:18px 20px;white-space:pre-wrap;font:13px/1.6 Inter,sans-serif;color:var(--ink);overflow:auto;max-height:640px}
.dchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.dchip{background:var(--chip);border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:5px 11px;font:600 12px "JetBrains Mono",monospace;cursor:pointer;transition:all .15s}
.dchip:hover{border-color:var(--g2);color:var(--ink)}.dchip.sel{background:var(--g);border-color:var(--g);color:#02140a;box-shadow:0 0 14px rgba(34,224,122,.35)}
.dchip.today::after{content:" •";color:var(--g)}.dchip.sel.today::after{color:#02140a}
.dk{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}@media (max-width:900px){.dk{grid-template-columns:repeat(4,1fr)}}@media (max-width:520px){.dk{grid-template-columns:repeat(2,1fr)}}
.dk>div{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.dk .n{font:700 24px/1.1 "JetBrains Mono",monospace;font-variant-numeric:tabular-nums}.dk .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-top:5px}
.dk .hi .n,.dk>div.hi .n{color:var(--g);text-shadow:0 0 14px rgba(34,224,122,.3)}.dk>div.bad .n{color:var(--warn)}
table.dt td{padding:8px 10px}table.dt th{cursor:default}table.dt th:first-child,table.dt td:first-child{white-space:nowrap}
table.dt tr{cursor:pointer}table.dt tr:hover td{background:rgba(34,224,122,.04)}table.dt tr.sel td{background:rgba(34,224,122,.1)}
.cell{display:grid;grid-template-columns:44px 1fr;align-items:center;gap:8px;min-width:110px}
.cell b{font:700 12.5px "JetBrains Mono",monospace;text-align:right;font-variant-numeric:tabular-nums}.cell b.z{color:var(--low);font-weight:500}
.cell i{display:block;height:7px;border-radius:9px;background:linear-gradient(90deg,var(--g2),var(--g));min-width:0}.cell i.w{background:var(--warn);opacity:.7}
.cos{display:grid;grid-template-columns:repeat(2,1fr);column-gap:28px}@media (max-width:760px){.cos{grid-template-columns:1fr}}
.cos .row{display:grid;grid-template-columns:minmax(0,170px) 1fr 70px;align-items:center;gap:10px;padding:6px 6px;border-radius:8px;cursor:pointer;font-size:13px}
.cos .row:hover{background:rgba(34,224,122,.06)}.cos .row.sel{background:rgba(34,224,122,.12)}
.cos .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.cos .track{background:var(--chip);border-radius:999px;height:8px;overflow:hidden}.cos .fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--g2),var(--g));transition:width .8s}
.cos .v{text-align:right;font:700 12px "JetBrains Mono",monospace}.cos .v small{color:var(--g);font-weight:600}
.co{font-weight:600;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}
.guess{color:var(--muted);font:600 11px "JetBrains Mono",monospace;margin-left:3px}
.today{border-color:var(--gdim);background:linear-gradient(180deg,rgba(34,224,122,.06),var(--card) 55%)}
.tk{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}@media (max-width:980px){.tk{grid-template-columns:repeat(4,1fr)}}@media (max-width:520px){.tk{grid-template-columns:repeat(2,1fr)}}
.tk>div{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px 14px;position:relative;overflow:hidden}
.tk .n{font:700 28px/1.05 "JetBrains Mono",monospace;font-variant-numeric:tabular-nums;transition:color .3s}
.tk .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em;margin-top:6px}
.tk .s{color:var(--g);font:600 11px "JetBrains Mono",monospace;margin-top:3px;min-height:15px}
.tk>div.hi{border-color:var(--g2)}.tk>div.hi .n{color:var(--g);text-shadow:0 0 16px rgba(34,224,122,.35)}.tk>div.bad .n{color:var(--warn)}
.tk>div.bump{animation:bump 1.2s ease}@keyframes bump{0%{box-shadow:0 0 0 0 rgba(34,224,122,.7);border-color:var(--g)}100%{box-shadow:0 0 0 14px rgba(34,224,122,0)}}
.now,.apps{margin-top:14px}.nh{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}
.nl{display:grid;grid-template-columns:14px 92px 1fr auto;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
.nl:last-child{border-bottom:0}.nl .dt{width:8px;height:8px;border-radius:50%;background:var(--g);animation:pulse 1.6s infinite}.nl .dt.off{background:var(--low);animation:none}
.nl .st{font:700 11px "JetBrains Mono",monospace;color:var(--g);text-transform:uppercase}.nl .tx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.nl .tm{color:var(--muted);font:500 11.5px "JetBrains Mono",monospace}
.ap{display:grid;grid-template-columns:96px 1fr;gap:10px;align-items:start;padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}.ap:last-child{border-bottom:0}
.ap .k{font:700 10.5px "JetBrains Mono",monospace;text-transform:uppercase;padding:3px 7px;border-radius:5px;text-align:center}
.ap .k.applied{background:var(--g);color:#02140a}.ap .k.needs{background:rgba(224,184,74,.15);color:var(--warn)}.ap .k.skipped{background:var(--chip);color:var(--muted)}
.ap .nt{color:var(--muted);font-size:12px;margin-top:2px}
.tabbar{display:flex;gap:4px;margin:0 0 14px;border-bottom:1px solid var(--line);overflow-x:auto;position:sticky;top:0;z-index:5;background:var(--bg);padding-top:4px}
.tabbar button{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);padding:10px 14px;font:600 13.5px Inter,sans-serif;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:7px;margin-bottom:-1px;transition:color .15s}
.tabbar button:hover{color:var(--ink)}.tabbar button.on{color:var(--g);border-bottom-color:var(--g)}
.tabbar .bd{font:700 10.5px "JetBrains Mono",monospace;padding:1px 6px;border-radius:999px;background:var(--chip);color:var(--muted)}.tabbar .bd:empty{display:none}
.tabbar .bd.hot{background:var(--g);color:#02140a}.tabbar .bd.warn{background:rgba(224,184,74,.18);color:var(--warn)}.tabbar .bd.err{background:rgba(255,107,107,.18);color:var(--err)}
.pane{display:none}.pane.on{display:block;animation:fadein .2s ease}@keyframes fadein{from{opacity:0}to{opacity:1}}
.pane>section+section,.pane>section+.grid2,.pane>section+.grid3,.pane>.kpis+section,.pane>.kpis+.grid3{margin-top:12px}
.pane>.grid2:first-child,.pane>.grid3:first-child{margin-top:0}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ctl select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:8px 10px;font:500 13px Inter,sans-serif;max-width:270px}
.btn{border-radius:9px;padding:8px 16px;font:700 13px Inter,sans-serif;cursor:pointer;border:1px solid transparent;transition:all .15s}
.btn.run{background:var(--g);color:#02140a;box-shadow:0 0 18px rgba(34,224,122,.35)}.btn.run:hover{filter:brightness(1.1)}
.btn.stop{background:transparent;color:var(--err);border-color:rgba(255,107,107,.45)}.btn.stop:hover{background:rgba(255,107,107,.1)}
.btn:disabled{opacity:.35;cursor:not-allowed;box-shadow:none;filter:none}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--card2);border:1px solid var(--g2);color:var(--ink);padding:10px 18px;border-radius:10px;font-size:13px;opacity:0;transition:all .3s;z-index:9;box-shadow:var(--glow)}
.toast.on{opacity:1;transform:translateX(-50%)}
.live.idle{color:var(--muted);border-color:var(--line);background:transparent}
.dot{width:8px;height:8px;border-radius:50%;background:var(--g);box-shadow:0 0 0 0 rgba(34,224,122,.7);animation:pulse 1.6s infinite}
.idle .dot{background:var(--muted);animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 10px rgba(34,224,122,0)}100%{box-shadow:0 0 0 0 rgba(34,224,122,0)}}
.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--glow)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.kpi{position:relative;overflow:hidden}.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--g);opacity:.0}
.kpi.hi::before{opacity:1}
.kpi .n{font:700 30px/1.05 "JetBrains Mono",monospace;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.kpi.hi .n{color:var(--g);text-shadow:0 0 18px rgba(34,224,122,.35)}
.kpi .l{color:var(--muted);font-size:12px;font-weight:500;margin-top:8px;text-transform:uppercase;letter-spacing:.06em}
.kpi .d{color:var(--g);font:600 11.5px "JetBrains Mono",monospace;margin-top:3px;min-height:16px}
.flow{display:grid;grid-template-columns:repeat(6,1fr);gap:0;margin-top:12px;padding:20px 18px}
@media (max-width:980px){.flow{grid-template-columns:repeat(3,1fr);row-gap:22px}}@media (max-width:560px){.flow{grid-template-columns:repeat(2,1fr)}}
.stp{position:relative;padding:0 10px;text-align:center}
.stp::after{content:"";position:absolute;top:21px;left:calc(50% + 26px);right:calc(-50% + 26px);height:2px;background:var(--line)}
.stp:last-child::after{display:none}.stp.done::after{background:linear-gradient(90deg,var(--g2),var(--g));box-shadow:0 0 8px rgba(34,224,122,.4)}
.nd{width:44px;height:44px;border-radius:50%;margin:0 auto;display:grid;place-items:center;border:2px solid var(--line);background:var(--bg);color:var(--muted);font:700 15px "JetBrains Mono",monospace;position:relative;z-index:1;transition:all .4s}
.stp.done .nd{border-color:var(--g);color:#02140a;background:var(--g);box-shadow:0 0 16px rgba(34,224,122,.45)}
.stp.run .nd{border-color:var(--g);color:var(--g);box-shadow:0 0 0 0 rgba(34,224,122,.6);animation:pulse 1.6s infinite}
.stp .sn{margin-top:10px;font-weight:700;font-size:13.5px}.stp .sd{color:var(--muted);font-size:11.5px;line-height:1.35;margin-top:2px;min-height:31px}
.stp .sv{font:700 22px "JetBrains Mono",monospace;margin-top:6px;font-variant-numeric:tabular-nums}.stp.done .sv,.stp.run .sv{color:var(--g)}
.stp .sb{display:inline-block;margin-top:6px;font:600 10.5px "JetBrains Mono",monospace;padding:2px 8px;border-radius:5px;background:var(--chip);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.stp.part .nd{border-color:var(--g2);color:var(--g)}.stp.part .sv{color:var(--ink)}.stp.part::after{background:linear-gradient(90deg,var(--g2),var(--line))}
.stp.run .sb{background:rgba(34,224,122,.14);color:var(--g)}.stp.done .sb{color:var(--g)}
.stp .pg{height:4px;border-radius:9px;background:var(--chip);overflow:hidden;margin:8px auto 0;width:80%}.stp .pg>div{height:100%;background:var(--g);box-shadow:0 0 8px rgba(34,224,122,.6);transition:width .8s}
.grid2{display:grid;grid-template-columns:1.3fr 1fr;gap:12px;margin-top:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px}
@media (max-width:980px){.grid2,.grid3{grid-template-columns:1fr}}
h2{font-size:11.5px;margin:0 0 14px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;display:flex;justify-content:space-between;align-items:center}
h2 .c{color:var(--g);letter-spacing:0;text-transform:none;font:600 12px "JetBrains Mono",monospace}
.task{padding:12px 0;border-bottom:1px solid var(--line)}.task:last-of-type{border-bottom:0}.task:first-child{padding-top:0}
.task .hd{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.task .nm{font-weight:600}.task .st{font:600 11px "JetBrains Mono",monospace;padding:2px 8px;border-radius:6px;background:var(--chip);color:var(--muted);text-transform:uppercase}
.task.on .st{background:rgba(34,224,122,.12);color:var(--g)}
.task .meta{color:var(--muted);font-size:12.5px;margin-top:3px}
.prog{height:6px;border-radius:999px;background:var(--chip);overflow:hidden;margin-top:8px}
.prog>div{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--g2),var(--g));box-shadow:0 0 12px rgba(34,224,122,.6);transition:width .8s cubic-bezier(.2,.8,.2,1)}
.task:not(.on) .prog>div{background:var(--low);box-shadow:none}
.feed{list-style:none;margin:0;padding:0;max-height:330px;overflow:auto}
.feed li{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px;animation:slide .5s ease}
.feed li:last-child{border-bottom:0}.feed .tm{color:var(--muted);font:500 11.5px "JetBrains Mono",monospace;width:62px;flex:none}
.feed .tt{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@keyframes slide{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.hl{display:flex;gap:12px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}.hl:last-child{border-bottom:0}
.hl .k{font:700 10.5px "JetBrains Mono",monospace;text-transform:uppercase;padding:2px 8px;border-radius:5px;flex:none;width:44px;text-align:center;background:var(--chip);color:var(--muted)}
.hl .k.ok{background:rgba(34,224,122,.12);color:var(--g)}.hl .k.warn{background:rgba(224,184,74,.15);color:var(--warn)}.hl .k.err{background:rgba(255,107,107,.15);color:var(--err)}.hl .k.run{color:var(--g);border:1px solid var(--g2)}
.hl .d{color:var(--muted);font-size:12px;margin-top:2px}
.ev{font-size:12.5px;color:var(--muted);padding:4px 0;display:flex;gap:8px}.ev .tm{font:500 11.5px "JetBrains Mono",monospace;flex:none}
.ev.ERROR .tm{color:var(--err)}.ev.WARNING .tm{color:var(--warn)}
.sc{display:inline-grid;place-items:center;min-width:30px;height:24px;border-radius:6px;font:700 12.5px "JetBrains Mono",monospace;padding:0 6px;flex:none;border:1px solid transparent}
.sc.s-hi{background:var(--g);color:#02140a;box-shadow:0 0 12px rgba(34,224,122,.45)}
.sc.s-mid{background:rgba(214,198,74,.12);color:var(--mid);border-color:rgba(214,198,74,.3)}
.sc.s-low{background:var(--chip);color:var(--muted);border-color:var(--line)}
.sc.none{background:transparent;color:var(--low);border:1px dashed var(--line)}
.ring{display:flex;align-items:center;gap:18px}.ring svg{flex:none}.ring circle{transition:stroke-dasharray .9s cubic-bezier(.2,.8,.2,1),stroke-dashoffset .9s}
.legend{display:grid;gap:8px;font-size:13px;flex:1}.legend span.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:8px}
.legend .row{display:flex;justify-content:space-between;gap:12px}.legend .v{font:700 13px "JetBrains Mono",monospace}
.hist{display:flex;align-items:flex-end;gap:6px;height:170px}
.hist .col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font:500 11px "JetBrains Mono",monospace;color:var(--muted)}
.hist .b{width:100%;border-radius:5px 5px 2px 2px;transition:height .8s cubic-bezier(.2,.8,.2,1)}.hist .c{margin-bottom:4px;color:var(--ink);font-weight:700}
.bars .row{display:grid;grid-template-columns:96px 1fr 44px;align-items:center;gap:10px;margin:9px 0;font-size:13px}
.bars .track{background:var(--chip);border-radius:999px;height:8px;overflow:hidden}
.bars .fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--g2),var(--g));transition:width .8s cubic-bezier(.2,.8,.2,1)}
.bars .v{text-align:right;font:700 12.5px "JetBrains Mono",monospace}
.empty{color:var(--muted);font-size:13px;padding:24px 0;text-align:center}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}
.filters input[type=search],.filters select{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:8px 12px;font:inherit;outline:none}
.filters input[type=search]:focus,.filters select:focus{border-color:var(--g2);box-shadow:0 0 0 3px rgba(34,224,122,.12)}
.filters input[type=search]{flex:1;min-width:200px}.filters label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px;cursor:pointer}
input[type=checkbox]{accent-color:var(--g)}
.tbl{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;padding:10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none;text-transform:uppercase;letter-spacing:.08em}
th.sorted{color:var(--g)}th.sorted::after{content:" ↓"}th.sorted.asc::after{content:" ↑"}
td{padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr.job{cursor:pointer;transition:background .15s}tr.job:hover td{background:rgba(34,224,122,.04)}
tr.flash td{animation:flash 2.4s ease}@keyframes flash{0%{background:rgba(34,224,122,.18)}100%{background:transparent}}
.t{font-weight:600}.muted{color:var(--muted)}a{color:var(--g);text-decoration:none;font-weight:600}a:hover{text-decoration:underline}
.pill{display:inline-block;font:600 11px "JetBrains Mono",monospace;padding:3px 9px;border-radius:6px;background:var(--chip);color:var(--muted);white-space:nowrap;text-transform:uppercase}
.pill.Good{background:rgba(34,224,122,.12);color:var(--g)}.pill.Drafted{background:rgba(34,224,122,.2);color:var(--g);border:1px solid var(--g2)}
.pill.Applied{background:var(--g);color:#02140a}.pill.Scored{color:var(--ink)}
.tag{font:700 10px "JetBrains Mono",monospace;color:var(--g);margin-left:6px;text-transform:uppercase}
.wtag{font:600 10px "JetBrains Mono",monospace;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:6px}
tr.detail td{background:#060a08;font-size:13px;padding:14px 16px}.reason{margin-bottom:10px}.links{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}
.desc{color:var(--muted);max-height:200px;overflow:auto;line-height:1.6}
.more{display:block;margin:14px auto 0;background:transparent;border:1px solid var(--g2);color:var(--g);border-radius:9px;padding:8px 18px;font:600 13px Inter,sans-serif;cursor:pointer}
.more:hover{background:rgba(34,224,122,.08)}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:var(--gdim);border-radius:8px}::-webkit-scrollbar-track{background:transparent}
@media (max-width:640px){.hide-sm{display:none}.kpi .n{font-size:24px}}

.pp-wrap{position:relative;border-radius:18px;padding:6px 4px 2px;margin-top:14px;overflow-x:auto;background:radial-gradient(ellipse 60% 70% at 50% 50%,rgba(34,224,122,.07),transparent 70%),repeating-linear-gradient(0deg,transparent 0 23px,rgba(34,224,122,.035) 23px 24px),repeating-linear-gradient(90deg,transparent 0 23px,rgba(34,224,122,.035) 23px 24px)}
.pp-svg{display:block;width:100%;min-width:660px;height:auto;user-select:none}
.pp-node{cursor:pointer;outline:none}.pp-node .box{fill:#0b1610;stroke:#24452f;stroke-width:1.5;transition:stroke .3s,filter .3s}
.pp-node:hover .box,.pp-node.sel .box{stroke:var(--g)}.pp-node.sel .box{filter:drop-shadow(0 0 10px rgba(34,224,122,.35))}.pp-node:focus-visible .box{stroke:var(--g);stroke-width:3}
.pp-node.bad .box{stroke:#5a2a2a}.pp-node.bad.sel .box,.pp-node.bad:hover .box{stroke:var(--err);filter:drop-shadow(0 0 10px rgba(255,107,107,.3))}
.pp-title{fill:var(--ink);font:700 13px Inter,system-ui,sans-serif;text-anchor:middle}
.pp-count{fill:var(--g);font:800 24px Inter,system-ui,sans-serif;text-anchor:middle}.pp-node.bad .pp-count{fill:var(--err)}
.pp-cap{fill:var(--muted);font:500 10.5px Inter,system-ui,sans-serif;text-anchor:middle}
.pp-lbl{font:700 11px Inter,system-ui,sans-serif;text-anchor:middle;fill:var(--g)}.pp-lbl.no{fill:var(--err)}
.pp-emoji{font-size:28px;text-anchor:middle;dominant-baseline:central}
.pp-pipe{fill:none;stroke:#132a1d;stroke-width:14;stroke-linecap:round}.pp-pipe.no{stroke:#2a1414}
.pp-flow{fill:none;stroke:var(--g);stroke-width:2.5;stroke-linecap:round;stroke-dasharray:2 12;animation:ppflow 1.1s linear infinite;opacity:.75}.pp-flow.no{stroke:var(--err);opacity:.5}
@keyframes ppflow{to{stroke-dashoffset:-14}}
.pp-bob{animation:ppbob 2.4s ease-in-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ppbob{50%{transform:translateY(-4px) rotate(-8deg)}}
.pp-blink{animation:ppblink 4s infinite;transform-box:fill-box;transform-origin:center}@keyframes ppblink{0%,92%,100%{transform:scaleY(1)}95%{transform:scaleY(.1)}}
.pp-spin{animation:ppspin 3s linear infinite;transform-box:fill-box;transform-origin:center}.pp-idle .pp-spin{animation-duration:14s;opacity:.4}@keyframes ppspin{to{transform:rotate(360deg)}}
.pp-glow{animation:ppglow 1.4s ease-in-out infinite}.pp-idle .pp-glow{animation:none;opacity:.4}@keyframes ppglow{50%{opacity:.25}}
.pp-gate{animation:ppgate 2.8s ease-in-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ppgate{50%{transform:scale(1.04)}}
.pp-pop{animation:pppop .6s ease-out;transform-box:fill-box;transform-origin:center}@keyframes pppop{40%{transform:scale(1.25)}}
.pp-wob{animation:ppwob .5s ease-in-out;transform-box:fill-box;transform-origin:50% 100%}@keyframes ppwob{25%{transform:rotate(-12deg)}75%{transform:rotate(9deg)}}
.pp-tok .pg{fill:#dff7e9;stroke:#8fd3ad;stroke-width:1;transition:fill .25s,stroke .25s}.pp-tok.yes .pg{fill:#7dffb4;stroke:var(--g)}.pp-tok.no .pg{fill:#ffb3b3;stroke:var(--err)}
.pp-tok .ln{stroke:#6c8f7a;stroke-width:1.4;stroke-linecap:round}.pp-tok .ey{fill:#0b1610}
.pp-tl{font:600 10px Inter,system-ui,sans-serif;fill:var(--ink);text-anchor:middle;paint-order:stroke;stroke:#030504;stroke-width:3px;stroke-linejoin:round}
.pp-feed{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--card2)}
.pp-row{display:grid;grid-template-columns:92px 22px minmax(0,1fr) auto;gap:8px;align-items:center;padding:7px 12px;border-bottom:1px solid var(--line);font-size:12.5px;animation:ppin .45s ease-out}.pp-row.old{opacity:.6}
.pp-row .pp-t{font:500 11px "JetBrains Mono",ui-monospace,monospace;color:var(--muted)}.pp-j{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pp-chip{font-size:11px;padding:2px 8px;border-radius:99px;white-space:nowrap;border:1px solid var(--line);color:var(--muted)}.pp-chip.ok{color:var(--g);border-color:#1d5a37}.pp-chip.bad{color:var(--err);border-color:#5a2a2a}.pp-chip.warn{color:var(--warn);border-color:#5a4a20}.pp-chip.info{color:#9fd8b6}
@keyframes ppin{from{opacity:0;transform:translateY(-6px);background:rgba(34,224,122,.12)}}
@media(max-width:640px){.pp-row{grid-template-columns:64px 20px minmax(0,1fr)}.pp-row .pp-chip{grid-column:3}}
.pp-legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:10px;font-size:12px;color:var(--muted)}.pp-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.wf-detail{padding:16px;background:var(--card2);border:1px solid var(--line);border-radius:10px;margin-top:14px;line-height:1.7}.wf-detail b{color:var(--g)}
.wf-event{padding:12px 0;border-bottom:1px solid var(--line);line-height:1.6}.wf-event small{color:var(--muted);display:block}
@media(prefers-reduced-motion:reduce){.pp-flow,.pp-bob,.pp-blink,.pp-spin,.pp-glow,.pp-gate{animation:none}}

</style></head><body><div class="wrap">
<div class="top"><div><h1>Job Search <b>Live</b></h1><div class="sub">PM · PO · Product &amp; Business Analyst — Remote + US, West Coast preferred · find &amp; draft only</div></div>
<div class="ctl"><select id="preset"><!--OPTS--></select><button class="btn run" id="runb">▶ Run</button><button class="btn stop" id="stopb">■ Stop</button>
<div class="live" id="live"><span class="dot"></span><span id="livetxt">Connecting…</span></div></div></div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div id="applyingnotice" role="status" aria-live="polite" hidden style="margin:12px 0;padding:12px 16px;border:1px solid var(--g2);border-radius:10px;background:var(--card2)"></div>
<nav class="tabbar" id="tabbar">
  <button data-tab="today">Today<span class="bd" id="bd-today"></span></button>
  <button data-tab="apps">Applications<span class="bd" id="bd-apps"></span></button>
  <button data-tab="daily">Daily</button>
  <button data-tab="jobs">Jobs<span class="bd" id="bd-jobs"></span></button>
  <button data-tab="drafts">Drafts<span class="bd" id="bd-drafts"></span></button>
  <button data-tab="insights">Insights</button>
  <button data-tab="workflow">Live workflow</button>
  <button data-tab="health">Health<span class="bd" id="bd-health"></span></button>
</nav>
<div class="pane" data-pane="today">
<section class="card today" id="today">
  <h2><span>Today <span class="muted" id="tdate" style="letter-spacing:0;text-transform:none;font-weight:500"></span></span><span class="c" id="tpm"></span></h2>
  <div style="margin:12px 0"><span role="group" aria-label="Choose application agents" style="display:inline-flex;gap:14px;margin-right:14px"><label><input type="checkbox" id="applyclaude" checked> Claude</label><label><input type="checkbox" id="applycodex" checked> Codex</label></span><button class="btn run" id="applynow">Apply now</button><button class="btn" id="agentnotifications" style="margin-left:10px">Enable notifications</button><span id="applyprogress" style="margin-left:12px"></span><div class="muted" id="applyagents" style="margin-top:8px"></div></div>
  <div class="tk" id="tk"></div>
  <div class="now"><div class="nh">Right now</div><div id="nowlist"></div></div>
  <div class="apps"><div class="nh"><a href="#apps" style="float:right;font-size:11px;text-transform:none;letter-spacing:0">Inbox &amp; follow-ups →</a>Applications today <span class="muted" id="appupd" style="font-weight:500;text-transform:none;letter-spacing:0"></span></div><div id="applist"></div></div>
</section>
<section class="card flow" id="flow"></section>
</div>
<div class="pane" data-pane="apps">
<section class="card">
  <h2><span>Inbox: application status</span><span class="c" id="ibc"></span></h2><div id="inbox"></div>
</section>
<section class="card" style="margin-top:12px">
  <h2><span>Recruiter emails</span><span class="c" id="rcc"></span></h2><div id="recruit"></div>
</section>
</div>
<div class="pane" data-pane="daily">
<section class="card">
  <h2><span>Day by day</span><span class="c" id="daylbl"></span></h2>
  <div class="dchips" id="dchips"></div>
  <div class="dk" id="dk"></div>
  <div class="tbl" style="margin-top:16px"><table class="dt"><thead><tr><th>Day</th><th>Found</th><th>PM / PO found</th><th>Scored</th><th>Scored 8+</th><th>Score failed</th><th>Drafted</th><th>Applied</th></tr></thead><tbody id="drows"></tbody></table></div>
  <div class="muted" style="font-size:11.5px;margin-top:8px">Click a day to see its numbers above. The Jobs tab is then filtered to everything that happened that day: <a href="#jobs">see that day's jobs →</a>. Each column's bars are scaled to its busiest day.</div>
</section>
</div>
<div class="pane" data-pane="jobs">
<section class="card">
  <h2><span>Top companies</span><span class="c" id="cocount"></span></h2><div class="cos" id="cos"></div>
  <div class="muted" style="font-size:11.5px;margin-top:10px">Click a company to filter the table. <span class="guess">~</span> = company worked out from the job link or description, not listed by the job board.</div>
</section>
<section class="card" style="margin-top:12px">
  <h2><span>All jobs</span><span class="c" id="count"></span></h2>
  <div class="filters">
    <input type="search" id="q" placeholder="Search title, company, location, score notes…">
    <select id="fstatus"><option value="">All statuses</option><option>Good fit</option><option>Scored</option><option>Not scored</option><option>Filtered out</option><option>Drafted</option><option>Applied</option><option>Duplicate listing</option></select>
    <select id="fscore"><option value="">Any score</option><option value="5">5+</option><option value="6">6+</option><option value="7">7+</option><option value="8">8+</option><option value="9">9+</option></select>
    <select id="floc"><option value="">Anywhere</option><option value="remote">Remote</option><option value="west">Preferred region</option><option value="onsite">On-site / hybrid</option></select>
    <select id="fco"><option value="">All companies</option></select>
    <select id="frole"><option value="pm">PM / PO roles</option><option value="analyst">Analyst roles</option><option value="product">All product roles</option><option value="">All titles</option></select>
    <label><input type="checkbox" id="fpay"> $100k+ or not listed</label>
  </div>
  <div class="tbl"><table><thead><tr>
    <th data-k="score">Score</th><th data-k="title">Job</th><th data-k="company">Company</th><th data-k="location" class="hide-sm">Location</th>
    <th data-k="smax">Pay</th><th data-k="site" class="hide-sm">Source</th><th data-k="found" class="hide-sm">Found</th><th data-k="status">Status</th>
  </tr></thead><tbody id="rows"></tbody></table></div>
  <button class="more" id="more" hidden>Show more</button>
</div>
<div class="pane" data-pane="drafts">
<section class="card" id="drafts">
  <h2><span>Drafts</span><span class="c" id="drc"></span></h2>
  <div class="note">⚠ Drafted by ApplyPilot with lenient checking and not fact-checked. Read every line against your real resume before you use it.</div>
  <div class="drw">
    <div class="drl"><input type="search" id="drq" placeholder="Filter drafts…"><div id="drlist"></div></div>
    <div class="drv">
      <div class="drh" id="drh"><span class="muted">Pick a draft on the left.</span></div>
      <div class="tabs" id="drtabs" hidden><button data-t="resume" class="on">Resume</button><button data-t="cover">Cover letter</button><span class="sp"></span><span class="fmt"><button data-f="pdf" class="on">PDF</button><button data-f="txt">Text</button></span><a id="dropen" target="_blank" rel="noopener">Open in new tab ↗</a></div>
      <div class="drbody" id="drbody"></div>
    </div>
  </div>
</div>
<div class="pane" data-pane="insights">
<section class="kpis" id="kpis"></section>
<section class="grid3">
  <div class="card"><h2>Scoring coverage</h2><div class="ring"><svg width="132" height="132" viewBox="0 0 132 132" id="ring"></svg><div class="legend" id="ringleg"></div></div></div>
  <div class="card"><h2>Fit score</h2><div id="hist"></div></div>
  <div class="card"><h2>Pay (top of range)</h2><div class="bars" id="pay"></div></div>
</div>
<div class="pane" data-pane="workflow">
<section class="card"><h2><span>Job pipeline · live</span><span class="c" id="wf-updated">Connecting…</span></h2>
<div class="muted">Each card is a real job, moving the moment ApplyPilot finds it, the scorer rates it, the rules check routes it, or an agent applies. Station numbers are today (your time zone); click any station for details.</div>
<div class="pp-wrap"><svg class="pp-svg" id="pp-svg" viewBox="0 0 1000 395" role="img" aria-label="Job pipeline: job search, scorer, rules check, then apply and applied, or reject"></svg></div>
<div class="pp-legend"><span><i style="background:#dff7e9"></i>just found</span><span><i style="background:#7dffb4"></i>passed the rules</span><span><i style="background:#ffb3b3"></i>rejected</span><span id="wf-summary"></span></div>
<div class="wf-detail" id="wf-detail" aria-live="polite">Select a station to see its live status.</div>
<h2 style="margin-top:18px"><span>Live feed · real jobs as they move</span><span class="c">newest first</span></h2><div class="pp-feed" id="pp-feed"><div class="empty">Watching for jobs… new ones appear here the moment they're found, scored or applied to.</div></div>
</section><section class="card" style="margin-top:16px"><h2><span>Recent application outcomes</span><span class="c">Today · shared log</span></h2><div id="wf-events"></div></section>
</div>
<div class="pane" data-pane="health">
<section class="card">
  <h2><span>Scoring health</span><span class="c" id="hlc"></span></h2><div id="health"></div>
</section>
<section class="grid2">
  <div class="card"><h2><span>Pipeline activity</span><span class="c" id="actc"></span></h2><div id="tasks"></div><div style="margin-top:12px" id="events"></div></div>
  <div class="card"><h2><span>Latest scores</span><span class="c" id="scc"></span></h2><ul class="feed" id="feed"></ul></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sc=s=>s==null?'<span class="sc none">–</span>':s<=0?'<span class="sc none" title="Gemini failed on this one">!</span>':`<span class="sc ${s>=8?'s-hi':s>=6?'s-mid':'s-low'}">${s}</span>`;
const barCol=s=>s>=8?'#22e07a':s>=6?'#d6c64a':'#2a3a30';
const money=n=>n==null?'':'$'+Math.round(n/1000)+'k';
const RX={pm:/product (manag|owner|lead)|product management/i,analyst:/analyst/i,product:/product|analyst/i};
const notMkt=t=>!/marketing/i.test(t);
let D=null,sortK='score',asc=false,open=new Set(),seen=null,prevN={},limit=150,rate={};

// Count-up animation that always lands on the real value: a newer update cancels the running one,
// and hidden pages (where browsers pause animations) get the number set directly.
const animIds={};
function anim(el,to){const id=el.id,from=prevN[id]??0;prevN[id]=to;cancelAnimationFrame(animIds[id]);
  if(from===to||document.hidden){el.textContent=to.toLocaleString();return}
  const t0=performance.now();const f=t=>{const p=Math.min(1,(t-t0)/700),e=1-Math.pow(1-p,3);el.textContent=Math.round(from+(to-from)*e).toLocaleString();if(p<1)animIds[id]=requestAnimationFrame(f)};animIds[id]=requestAnimationFrame(f)}
document.addEventListener('visibilitychange',()=>{if(document.hidden)return;for(const id in prevN){cancelAnimationFrame(animIds[id]);const e=$(id);if(e)e.textContent=prevN[id].toLocaleString()}});

let prevT={};
function today(){
  const T=D.today,J=D.jobs,A=D.activity,apps=(D.apps&&D.apps.rows)||[];
  const x=dayStats()[T]||{};  // same numbers as today's row in the Daily table
  const found=x.found||0,fpm=x.pm||0,scored=x.scored||0,spm=x.spm||0,good=x.good||0,gpm=x.gpm||0,failed=x.failed||0,drafted=x.drafted||0,applied=x.applied||0;
  const needs=apps.filter(a=>a.kind==='needs').length,skip=apps.filter(a=>a.kind==='skipped').length;
  const tiles=[['ts','Scanned',A.scanned_today||0,'listings checked',''],['tf','Found',found,`${fpm} PM / PO`,''],['tsc','Scored',scored,`${spm} PM / PO`,''],
    ['tg','Scored 8+',good,`${gpm} PM / PO`,'hi'],['tx','Score failed',failed,'',failed?'bad':''],['td','Drafted',drafted,'resume + letter','hi'],['ta','Applied',applied,needs||skip?`${needs} need you · ${skip} skipped`:'','hi']];
  $('tdate').textContent='· '+new Date(T+'T12:00:00').toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'});
  if(!$('ts'))$('tk').innerHTML=tiles.map(([id,l,,,c])=>`<div class="${c}" id="${id}b"><div class="n" id="${id}">0</div><div class="l">${l}</div><div class="s" id="${id}s"></div></div>`).join('');
  tiles.forEach(([id,,v,sub,c])=>{if(prevT[id]!=null&&v>prevT[id]){const b=$(id+'b');b.classList.remove('bump');void b.offsetWidth;b.classList.add('bump')}prevT[id]=v;anim($(id),v);$(id+'s').textContent=sub;$(id+'b').className=c+($(id+'b').classList.contains('bump')?' bump':'')});
  $('tpm').textContent=`${gpm} PM / PO jobs scored 8+ today`;
  const label={discover:'Searching',enrich:'Enriching',score:'Scoring',tailor:'Drafting',cover:'Drafting',pdf:'Drafting'};
  const act=A.tasks.filter(t=>t.active);
  let rows=act.map(t=>`<div class="nl"><span class="dt"></span><span class="st">${label[t.stage]||'Working'}</span><span class="tx" title="${esc(t.now)}">${esc(t.now||(t.total?`${t.done} / ${t.total} ${t.unit}`:'starting…'))}</span><span class="tm">${esc(t.nowt||t.updated)}</span></div>`);
  if(apps.length){const a=apps[0];rows.push(`<div class="nl"><span class="dt${a.kind==='applied'?'':' off'}"></span><span class="st">Applying</span><span class="tx">${esc(a.kind==='applied'?'Submitted':a.kind==='needs'?'Filled, waiting on you':'Skipped')}: ${esc(a.title)} · ${esc(a.company)}</span><span class="tm">${esc(D.apps.updated||'')}</span></div>`)}
  $('nowlist').innerHTML=rows.join('')||'<div class="empty" style="padding:10px 0">Nothing running right now.</div>';
  $('appupd').textContent=D.apps&&D.apps.updated?'· log updated '+D.apps.updated:'';
  $('applist').innerHTML=apps.slice(0,8).map(a=>`<div class="ap"><span class="k ${a.kind}">${a.kind==='needs'?'needs you':a.kind}</span><div><b>${esc(a.title)}</b> · ${esc(a.company)}${(a.by||[]).map(b=>`<span class="wtag">${esc(b)}</span>`).join('')}${a.notes?`<div class="nt">${esc(a.notes)}</div>`:''}</div></div>`).join('')||'<div class="empty" style="padding:10px 0">No applications logged today yet.</div>';
}
const TABS=['today','apps','daily','jobs','drafts','insights','health','workflow'];
function showTab(t,push){if(!TABS.includes(t))t='today';
  document.querySelectorAll('.tabbar button').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.dataset.pane===t));
  try{localStorage.setItem('tab',t)}catch(e){}
  if(push!==false&&location.hash!=='#'+t)history.replaceState(null,'','#'+t);window.scrollTo({top:0})}
$('tabbar').addEventListener('click',e=>{const b=e.target.closest('button');if(b)showTab(b.dataset.tab)});
window.addEventListener('hashchange',()=>showTab(location.hash.slice(1),false));
showTab(location.hash.slice(1)||(()=>{try{return localStorage.getItem('tab')}catch(e){return null}})()||'today',false);
function badge(id,txt,cls){const e=$(id);if(!e)return;e.textContent=txt||'';e.className='bd'+(cls?' '+cls:'')}
function badges(){
  const I=D.inbox||{},H=D.health||[],apps=(D.apps&&D.apps.rows)||[];
  const needs=apps.filter(a=>a.kind==='needs').length,todo=(I.todo||[]).length;
  badge('bd-today',(D.activity&&D.activity.running)?'live':'',(D.activity&&D.activity.running)?'hot':'');
  badge('bd-apps',todo?todo+' to do':'',todo?'hot':'');
  const err=H.filter(c=>c.state==='err').length,warn=H.filter(c=>c.state==='warn').length;
  badge('bd-health',err?err+' down':warn?warn+' check':'',err?'err':warn?'warn':'');
  badge('bd-drafts',String(D.jobs.filter(j=>j.resume||j.cover).length||''));
  badge('bd-jobs',D.jobs.length.toLocaleString());
}
function render(){
  const J=D.jobs,A=D.activity,scored=J.filter(j=>j.score!=null&&j.score>0),good=scored.filter(j=>j.score>=D.goodScore);
  const drafted=J.filter(j=>j.resume||j.cover).length,applied=D.appliedByDay?Object.values(D.appliedByDay).reduce((a,b)=>a+b,0):J.filter(j=>j.status==='Applied').length;
  const pm=J.filter(j=>RX.pm.test(j.title)&&notMkt(j.title)).length,west=J.filter(j=>j.west).length,cos=new Set(J.filter(j=>j.company).map(j=>j.company.toLowerCase())).size;
  const K=[['k1','Jobs found',J.length],['k2','PM / PO roles',pm],['k3','West Coast',west],['k8','Companies',cos],['k4','Scored',scored.length],['k5','Scored 8+',good.length,1],['k6','Drafts ready',drafted,1],['k7','Applied (you)',applied]];
  if(!$('k1'))$('kpis').innerHTML=K.map(([id,l,,hi])=>`<div class="card kpi${hi?' hi':''}"><div class="n" id="${id}">0</div><div class="l">${l}</div><div class="d" id="${id}d"></div></div>`).join('');
  K.forEach(([id,,v])=>{const old=prevN[id];if(old!=null&&v>old){$(id+'d').textContent='+'+(v-old).toLocaleString()+' just now';clearTimeout(rate[id]);rate[id]=setTimeout(()=>$(id+'d').textContent='',8000)}anim($(id),v)});

  const S=D.stages,runSt=new Set(A.tasks.filter(t=>t.active).map(t=>t.stage)),tk=st=>A.tasks.find(t=>t.active&&t.stage===st);
  const FL=[['discover','Discover','Search job boards & company sites',S.discover,'jobs found'],['enrich','Enrich','Fetch full job descriptions',S.enrich,'with details'],
    ['score','Score','Gemini rates fit 1–10 vs. your resume',S.score,`scored · ${S.good} at 8+`],['tailor','Tailor','Custom resume for each 8+ job',S.tailor,'resumes'],
    ['cover','Cover letter','Draft a letter for each 8+ job',S.cover,'letters'],['pdf','PDF','Render drafts as PDFs',S.pdf,'PDFs']];
  const REF={enrich:S.discover,score:S.enrich,tailor:S.good,cover:S.good,pdf:S.tailor};
  $('flow').innerHTML=FL.map(([k,n,d,v,u],i)=>{const run=runSt.has(k),t=tk(k),ref=REF[k];const pct=t&&t.total?t.done/t.total*100:0;
    const done=!run&&v>0&&(ref==null||v>=ref*0.95),part=!run&&!done&&v>0;
    const lbl=run?'running':done?'done':part?`${Math.round(v/ref*100)}% done`:'waiting';
    return `<div class="stp ${run?'run':done?'done':part?'part':''}"><div class="nd">${done?'✓':i+1}</div><div class="sn">${n}</div><div class="sd">${d}</div><div class="sv">${v.toLocaleString()}</div><div class="muted" style="font-size:11.5px">${u}</div>
      <span class="sb">${lbl}</span>${run?`<div class="pg"><div style="width:${pct}%"></div></div><div class="muted mono" style="font-size:10.5px;margin-top:4px">${t&&t.total?t.done+' / '+t.total:''}</div>`:''}</div>`}).join('');
  $('live').className='live'+(A.running?'':' idle');
  $('livetxt').textContent=(A.running?'LIVE · ':'IDLE · ')+D.now;
  const on=A.tasks.filter(t=>t.active).length;$('actc').textContent=on?on+' running':'idle';
  $('tasks').innerHTML=A.tasks.length?A.tasks.map(t=>{const pct=t.total?t.done/t.total*100:0;
    const what=t.total?`${t.detail}${t.done.toLocaleString()} / ${t.total.toLocaleString()} ${t.unit}`:'starting…';
    return `<div class="task${t.active?' on':''}"><div class="hd"><span class="nm">${esc((t.stage||'pipeline').replace(/^./,c=>c.toUpperCase()))} <span class="muted mono" style="font-size:11.5px;font-weight:500">· ${esc(t.name)}</span></span><span class="st">${t.active?'running':'done '+t.updated}</span></div>
      <div class="meta">${esc(what)}${t.new!=null?` · <span style="color:var(--g)">${t.new.toLocaleString()} new jobs</span>`:''}</div><div class="prog"><div style="width:${pct}%"></div></div></div>`}).join(''):'<div class="empty">No pipeline logs found.</div>';
  $('events').innerHTML=(A.events||[]).slice(0,4).map(e=>`<div class="ev ${e.level}"><span class="tm">${e.t}</span><span>${esc(e.msg)}</span></div>`).join('');
  $('scc').textContent=scored.length?`${good.length} of ${scored.length} at 8+`:'';
  $('feed').innerHTML=(A.recent||[]).map(r=>`<li><span class="tm">${r.t}</span>${sc(r.score)}<span class="tt">${esc(r.title)}</span></li>`).join('')||'<div class="empty">Scores will appear here as they come in.</div>';

  const segs=[['Scored 8+',good.length,'#22e07a'],['Scored < 8',scored.length-good.length,'#13603a'],['Not scored yet',J.length-scored.length,'#16241b']];
  const R=54,C=2*Math.PI*R;let off=0;
  $('ring').innerHTML=`<circle cx="66" cy="66" r="${R}" fill="none" stroke="#111b15" stroke-width="12"/>`+segs.map(([,v,c])=>{const len=J.length?v/J.length*C:0;const s=`<circle cx="66" cy="66" r="${R}" fill="none" stroke="${c}" stroke-width="12" stroke-dasharray="${len} ${C-len}" stroke-dashoffset="${-off}" transform="rotate(-90 66 66)"/>`;off+=len;return s}).join('')+
    `<text x="66" y="66" text-anchor="middle" font-size="24" font-weight="700" font-family="JetBrains Mono,monospace" fill="#22e07a">${J.length?Math.round(scored.length/J.length*100):0}%</text><text x="66" y="84" text-anchor="middle" font-size="10.5" letter-spacing="1" fill="#7d9686">SCORED</text>`;
  $('ringleg').innerHTML=segs.map(([l,v,c])=>`<div class="row"><span><span class="sw" style="background:${c}"></span>${l}</span><span class="v">${v.toLocaleString()}</span></div>`).join('');

  if(!scored.length)$('hist').innerHTML='<div class="empty">No scores yet</div>';
  else{const h={};for(let i=1;i<=10;i++)h[i]=0;scored.forEach(j=>h[Math.max(1,Math.min(10,j.score))]++);const mx=Math.max(1,...Object.values(h));
    if(!$('hb1'))$('hist').innerHTML='<div class="hist">'+Object.keys(h).map(s=>`<div class="col"><span class="c" id="hc${s}"></span><div class="b" id="hb${s}" style="height:0;background:${barCol(+s)};${+s>=8?'box-shadow:0 0 14px rgba(34,224,122,.5)':''}"></div><span>${s}</span></div>`).join('')+'</div>';
    Object.entries(h).forEach(([s,v])=>{$('hb'+s).style.height=Math.max(3,v/mx*135)+'px';$('hc'+s).textContent=v||''})}

  const PB=[['Not listed',j=>j.smax==null],['Under $100k',j=>j.smax!=null&&j.smax<100e3],['$100–150k',j=>j.smax>=100e3&&j.smax<150e3],['$150–200k',j=>j.smax>=150e3&&j.smax<200e3],['$200k+',j=>j.smax>=200e3]];
  const pv=PB.map(([l,f])=>[l,J.filter(f).length]),pmx=Math.max(1,...pv.map(p=>p[1]));
  if(!$('pf0'))$('pay').innerHTML=pv.map(([l],i)=>`<div class="row"><span>${l}</span><div class="track"><div class="fill" id="pf${i}" style="width:0"></div></div><span class="v" id="pv${i}"></span></div>`).join('');
  pv.forEach(([,v],i)=>{$('pf'+i).style.width=v/pmx*100+'%';$('pv'+i).textContent=v.toLocaleString()});
  today();
  inbox();
  recruit();
  health();
  badges();
  drafts();
  daily();
  companies();
  rows();
}

let drSel=null,drTab='resume',drFmt='pdf',drSig='';
const RC={needs_you:['needs you','err'],drafted:['drafted · ready to send','ok'],match:['match','warn'],read:['read','wait'],sent:['sent','run'],not_match:['not a match','']};
function recruit(){const R=D.recruiters||{};const C=R.counts||{};const L=R.leads||[];
  $('rcc').textContent=R.updated?'checked '+new Date(R.updated).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'not checked yet';
  const order=['needs_you','drafted','match','read','sent','not_match'];
  const rank=s=>order.indexOf(s);
  $('recruit').innerHTML=`<div class="d" style="font-size:13px;margin-bottom:8px">${order.filter(k=>C[k]).map(k=>`${RC[k][0]}: <b>${C[k]}</b>`).join(' · ')||'No recruiter emails yet.'}</div>`
    +[...L].sort((a,b)=>rank(a.status)-rank(b.status)).map(l=>{const [lab,cls]=RC[l.status]||[l.status,''];
      const link=l.status==='drafted'&&l.draft_url?`<a href="${esc(l.draft_url)}" target="_blank" rel="noopener">open draft in Gmail</a>`:(l.url?`<a href="${esc(l.url)}" target="_blank" rel="noopener">open email</a>`:'');
      return `<div class="hl"><span class="k ${cls}" style="width:auto;min-width:44px">${esc(lab)}</span><div><div><b>${esc(l.title||'(role not named)')}</b>${l.location?' · '+esc(l.location):''}${l.pay?' · '+esc(l.pay):''}${l.type?` <span class="wtag">${esc(l.type)}</span>`:''}</div>
        <div class="d">${esc((l.recruiter||'').replace(/<.*>/,'').trim())} · ${esc((l.last_seen||'').slice(0,10))}${l.note?' · '+esc(l.note):''}${link?' · '+link:''}${l.status==='drafted'?' · attach your resume before sending':''}</div></div></div>`}).join('');}
function inbox(){const I=D.inbox||{};const C=I.counts||{};
  $('ibc').textContent=I.updated?'checked '+new Date(I.updated).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'not checked yet';
  const row=a=>{const when=a.deadline?` · <b>${a.status==='interview'?'on':'due'} ${esc(a.deadline)}</b>`:'';
    return `<div class="hl"><span class="k ${a.status==='rejected'?'err':a.status==='offer'?'ok':'warn'}">${esc(a.status==='interview'?'intvw':a.status==='assessment'?'test':a.status==='rejected'?'no':a.status)}</span><div><div>${a.url?`<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.company)} — ${esc(a.title)}</a>`:`${esc(a.company)} — ${esc(a.title)}`}${when}${a.in_log===false?' <span class="wtag">not in log</span>':''}</div>
      <div class="d">${a.deadline_text?esc(a.deadline_text)+' · ':''}Resume: ${a.resume?(a.resume_file?`<a href="${fileUrl(a.resume_file)}" target="_blank">${esc(a.resume)}</a>`:esc(a.resume))+(a.resume_guess?' (best guess from date; check)':''):'not recorded'}</div></div></div>`};
  const order=['interview','assessment','offer','consideration','rejected','applied'];
  $('inbox').innerHTML=(I.todo&&I.todo.length?`<div class="nh">Needs you</div>${I.todo.map(row).join('')}`:'<div class="empty" style="padding:10px 0">No interviews or assessments waiting.</div>')
    +`<div class="nh" style="margin-top:12px">All applications heard from</div><div class="d" style="font-size:13px">${order.filter(k=>C[k]).map(k=>`${k}: <b>${C[k]}</b>`).join(' · ')||'none yet'}</div>`
    +(I.recent&&I.recent.length?`<div class="nh" style="margin-top:12px">Latest decisions</div>${I.recent.map(row).join('')}`:'');}
function health(){const H=D.health||[];const bad=H.filter(c=>c.state==='err'||c.state==='warn').length;
  $('hlc').textContent=bad?`${bad} need${bad===1?'s':''} a look`:'all good';
  $('health').innerHTML=H.map(c=>`<div class="hl"><span class="k ${c.state}">${{ok:'ok',warn:'check',err:'down',wait:'wait',run:'live'}[c.state]||c.state}</span><div><div>${esc(c.name)}</div><div class="d">${esc(c.detail)}</div></div></div>`).join('')||'<div class="empty">No checks yet.</div>';}
const fileUrl=p=>'/file?path='+encodeURIComponent(p);
function drafts(){
  const q=($('drq').value||'').toLowerCase();
  const L=D.jobs.filter(j=>j.resume||j.cover).sort((a,b)=>(b.score||0)-(a.score||0)||String(b.dT||'').localeCompare(String(a.dT||'')));
  $('drc').textContent=`${L.length} drafted · ${L.filter(j=>j.status==='Applied').length} applied`;
  const F=L.filter(j=>!q||(j.title+' '+j.company+' '+j.location).toLowerCase().includes(q));
  const sig=F.map(j=>j.url+j.resume+j.cover+j.resume_pdf+j.cover_pdf).join('|')+drSel;
  if(sig!==drSig){drSig=sig;
    $('drlist').innerHTML=F.map(j=>`<div class="dri${drSel===j.url?' sel':''}" data-u="${esc(j.url)}">${sc(j.score)}<div><div class="t"><b>${esc(j.title)}</b></div><div class="m">${esc(j.company||j.site)} · ${esc(j.location)}</div>
      <div class="ok${j.resume?'':' miss'}">${j.resume?'✓ resume':'… resume'}${j.cover?' · ✓ cover letter':' · … cover letter'}${j.status==='Applied'?' · applied':''}</div></div></div>`).join('')||'<div class="empty">No drafts yet — they appear here as ApplyPilot writes them.</div>';}
  if(!drSel&&F.length)showDraft(F[0].url,true);
}
function showDraft(u,quiet){drSel=u;const j=D.jobs.find(x=>x.url===u);if(!j)return;drSig='';if(!quiet)drafts();
  document.querySelectorAll('.dri').forEach(e=>e.classList.toggle('sel',e.dataset.u===u));
  $('drh').innerHTML=`<div class="t">${sc(j.score)} ${esc(j.title)}</div><div class="muted" style="font-size:12.5px;margin-top:4px">${esc(j.company||j.site)} · ${esc(j.location)}${j.salary?' · '+esc(j.salary):''}</div>
    <div class="links"><a href="${esc(j.url)}" target="_blank" rel="noopener">View posting ↗</a>${j.apply_url&&j.apply_url!==j.url?`<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">Apply page ↗</a>`:''}</div>`;
  $('drtabs').hidden=false;drBody();}
function drBody(){const j=D.jobs.find(x=>x.url===drSel);if(!j)return;
  document.querySelectorAll('#drtabs [data-t]').forEach(b=>b.classList.toggle('on',b.dataset.t===drTab));
  document.querySelectorAll('#drtabs [data-f]').forEach(b=>b.classList.toggle('on',b.dataset.f===drFmt));
  const txt=drTab==='resume'?j.resume:j.cover,pdf=drTab==='resume'?j.resume_pdf:j.cover_pdf;
  const path=drFmt==='pdf'&&pdf?pdf:txt;
  if(!path){$('drbody').innerHTML=`<div class="empty" style="flex:1">No ${drTab==='resume'?'resume':'cover letter'} yet for this job.</div>`;$('dropen').removeAttribute('href');return}
  $('dropen').href=fileUrl(path);
  if(path.endsWith('.pdf'))$('drbody').innerHTML=`<iframe src="${fileUrl(path)}#view=FitH" title="draft"></iframe>`;
  else{$('drbody').innerHTML='<pre>Loading…</pre>';fetch(fileUrl(path)).then(r=>r.text()).then(t=>{const pre=$('drbody').querySelector('pre');if(pre)pre.textContent=t})}
}
let selDay=null;
let todayStr=new Intl.DateTimeFormat('en-CA',{timeZone:'__TZ__',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const dayName=d=>{const t=new Date(d+'T12:00:00');const diff=Math.round((new Date(todayStr+'T12:00:00')-t)/864e5);
  return (diff===0?'Today':diff===1?'Yesterday':t.toLocaleDateString(undefined,{weekday:'short'}))+' · '+t.toLocaleDateString(undefined,{month:'short',day:'numeric'})};
// The one place day counts are made; the Today tiles and the Daily table both read it.
function dayStats(){const m={};const g=d=>m[d]=m[d]||{found:0,pm:0,scored:0,spm:0,good:0,gpm:0,failed:0,drafted:0,applied:0};
  D.jobs.forEach(j=>{const pm=RX.pm.test(j.title)&&notMkt(j.title);if(j.dF){const x=g(j.dF);x.found++;if(pm)x.pm++}
    if(j.dS){const x=g(j.dS);if(j.score>0){x.scored++;if(pm)x.spm++;if(j.score>=D.goodScore){x.good++;if(pm)x.gpm++}}else if(j.sfail)x.failed++}
    if(j.dT||j.dC){g(j.dT||j.dC).drafted++}if(j.status==='Applied'&&j.dA)g(j.dA).applied++});
  if(D.appliedByDay){Object.values(m).forEach(x=>x.applied=0);Object.entries(D.appliedByDay).forEach(([d,n])=>g(d).applied=n)}
  g(todayStr);return m}
function daily(){
  const m=dayStats(),days=Object.keys(m).sort().reverse(),cur=selDay||todayStr,x=m[cur]||{found:0,pm:0,scored:0,good:0,failed:0,drafted:0,applied:0};
  $('daylbl').textContent=selDay?'showing '+dayName(selDay):'showing today';
  $('dchips').innerHTML=`<button class="dchip${!selDay||selDay===todayStr?' sel':''}" data-d="">Today</button>`+days.filter(d=>d!==todayStr).slice(0,20).map(d=>`<button class="dchip${selDay===d?' sel':''}" data-d="${d}">${dayName(d).split(' · ')[1]}</button>`).join('');
  const T=[['Found',x.found],['PM / PO found',x.pm],['Scored',x.scored],['Scored 8+',x.good,'hi'],['Score failed',x.failed,x.failed?'bad':''],['Drafted',x.drafted,'hi'],['Applied',x.applied,'hi']];
  $('dk').innerHTML=T.map(([l,v,c])=>`<div class="${c||''}"><div class="n">${v.toLocaleString()}</div><div class="l">${l}</div></div>`).join('');
  const K=['found','pm','scored','good','failed','drafted','applied'],mx={};K.forEach(k=>mx[k]=Math.max(1,...days.map(d=>m[d][k])));
  $('drows').innerHTML=days.map(d=>`<tr data-d="${d}" class="${selDay===d?'sel':''}"><td><b>${dayName(d)}</b></td>`+K.map(k=>{const v=m[d][k];
    return `<td><div class="cell"><b class="${v?'':'z'}">${v.toLocaleString()}</b><i class="${k==='failed'?'w':''}" style="width:${v/mx[k]*100}%"></i></div></td>`}).join('')+'</tr>').join('');
}
function companies(){
  const J=D.jobs,m={};J.forEach(j=>{if(!j.company)return;const k=j.company;(m[k]=m[k]||{n:0,g:0}).n++;if(j.score>=D.goodScore)m[k].g++});
  const top=Object.entries(m).sort((a,b)=>b[1].n-a[1].n);const known=J.filter(j=>j.company).length;
  $('cocount').textContent=`${known.toLocaleString()} of ${J.length.toLocaleString()} jobs have a company`;
  const mx=Math.max(1,...top.slice(0,1).map(t=>t[1].n)),cur=$('fco').value;
  $('cos').innerHTML=top.slice(0,16).map(([c,v])=>`<div class="row${cur===c?' sel':''}" data-co="${esc(c)}"><span class="nm" title="${esc(c)}">${esc(c)}</span><div class="track"><div class="fill" style="width:${v.n/mx*100}%"></div></div><span class="v">${v.n}${v.g?` <small>★${v.g}</small>`:''}</span></div>`).join('')||'<div class="empty">No company names yet</div>';
  const opts=top.map(([c,v])=>`<option value="${esc(c)}">${esc(c)} (${v.n})</option>`).join('');
  if($('fco').dataset.sig!==String(top.length)){$('fco').innerHTML='<option value="">All companies</option>'+opts;$('fco').value=cur;$('fco').dataset.sig=String(top.length)}
}

function rows(){
  const J=D.jobs,co=$('fco').value,q=$('q').value.toLowerCase(),st=$('fstatus').value,ms=$('fscore').value,lc=$('floc').value,ro=$('frole').value,pay=$('fpay').checked;
  let r=J.filter(j=>(!q||(j.title+' '+j.company+' '+j.location+' '+j.reason).toLowerCase().includes(q))&&(!st||j.status===st)&&(!co||j.company===co)
    &&(!ms||(j.score!=null&&j.score>=+ms))&&(!pay||j.smax==null||j.smax>=D.minSalary)
    &&(!lc||(lc==='remote'?j.remote:lc==='west'?j.west:!j.remote))&&(!ro||(RX[ro].test(j.title)&&notMkt(j.title)))
    &&(!selDay||j.dF===selDay||j.dS===selDay||j.dT===selDay||j.dC===selDay||j.dA===selDay));
  r.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(x==null&&y==null)return 0;if(x==null)return 1;if(y==null)return -1;if(typeof x==='string'){x=x.toLowerCase();y=y.toLowerCase()}return (x<y?-1:x>y?1:0)*(asc?1:-1)});
  const now=new Set(J.filter(j=>j.score!=null).map(j=>j.url));const fresh=seen?new Set([...now].filter(u=>!seen.has(u))):new Set();seen=now;
  $('count').textContent=`${r.length.toLocaleString()} of ${J.length.toLocaleString()}`+(selDay?` · ${dayName(selDay)}`:'');
  document.querySelectorAll('th').forEach(th=>{th.classList.toggle('sorted',th.dataset.k===sortK);th.classList.toggle('asc',th.dataset.k===sortK&&asc)});
  $('more').hidden=r.length<=limit;$('more').textContent=`Show more (${(r.length-limit).toLocaleString()} left)`;
  $('rows').innerHTML=r.slice(0,limit).map(j=>{const id=j.url;
    const pay=j.smin!=null?(j.smin===j.smax?money(j.smax):money(j.smin)+'–'+money(j.smax)):'<span class="muted">—</span>';
    let h=`<tr class="job${fresh.has(id)?' flash':''}" data-id="${esc(id)}"><td>${sc(j.score)}</td><td><div class="t">${esc(j.title)}${j.live?'<span class="tag">● new</span>':''}</div></td><td><div class="co" title="${j.chow==='guess'?'Guessed from the description':j.chow==='link'?'From the apply link':''}">${j.company?esc(j.company)+(j.chow==='guess'||j.chow==='link'?'<span class="guess">~</span>':''):'<span class="muted">—</span>'}</div></td><td class="hide-sm">${esc(j.location)}${j.west?'<span class="wtag">WEST</span>':''}</td><td class="mono" style="font-size:12.5px">${pay}</td><td class="hide-sm">${esc(j.site)}</td><td class="hide-sm muted mono" style="font-size:11.5px">${esc(j.found.slice(5))}</td><td><span class="pill ${j.status==='Good fit'?'Good':j.status.split(' ')[0]}">${esc(j.status)}</span></td></tr>`;
    if(open.has(id))h+=`<tr class="detail"><td></td><td colspan="7">${j.reason?`<div class="reason"><b style="color:var(--g)">Why this score:</b> ${esc(j.reason)}</div>`:''}
      <div class="links"><a href="${esc(j.url)}" target="_blank" rel="noopener">View posting ↗</a>${j.apply_url&&j.apply_url!==j.url?`<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">Apply page ↗</a>`:''}${j.resume?`<a href="#drafts" data-open="${esc(id)}">View drafts</a>`:''}</div>
      <div class="desc" data-desc="${esc(id)}">${DESC[id]!=null?esc(DESC[id])+(DESC[id].length>=1500?'…':''):'Loading description…'}</div></td></tr>`;
    return h}).join('')||'<tr><td colspan="8" class="empty">No jobs match these filters.</td></tr>';
}

$('drlist').addEventListener('click',e=>{const d=e.target.closest('.dri');if(d)showDraft(d.dataset.u)});
$('drq').addEventListener('input',()=>{drSig='';drafts()});
$('drtabs').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.t)drTab=b.dataset.t;if(b.dataset.f)drFmt=b.dataset.f;drBody()});
document.addEventListener('click',e=>{const a=e.target.closest('[data-open]');if(!a)return;e.preventDefault();showTab('drafts');showDraft(a.dataset.open)});
$('dchips').addEventListener('click',e=>{const b=e.target.closest('.dchip');if(!b)return;const d=b.dataset.d;if(!d){selDay=selDay===todayStr?null:todayStr}else selDay=selDay===d?null:d;limit=150;daily();rows()});
$('drows').addEventListener('click',e=>{const tr=e.target.closest('tr');if(!tr)return;const d=tr.dataset.d;selDay=selDay===d?null:d;limit=150;daily();rows()});
['q','fco','fstatus','fscore','floc','frole','fpay'].forEach(id=>$(id).addEventListener('input',()=>{limit=150;D&&rows()}));
$('cos').addEventListener('click',e=>{const r=e.target.closest('.row');if(!r)return;$('fco').value=$('fco').value===r.dataset.co?'':r.dataset.co;limit=150;companies();rows();$('fco').scrollIntoView({behavior:'smooth',block:'center'})});
$('more').addEventListener('click',()=>{limit+=150;rows()});
document.querySelectorAll('th').forEach(th=>th.addEventListener('click',()=>{const k=th.dataset.k;if(sortK===k)asc=!asc;else{sortK=k;asc=['title','location','site'].includes(k)}rows()}));
$('rows').addEventListener('click',e=>{if(e.target.closest('a'))return;const tr=e.target.closest('tr.job');if(!tr)return;const id=tr.dataset.id;open.has(id)?open.delete(id):open.add(id);rows();if(open.has(id))loadDesc(id)});
const TOKEN='__TOKEN__';
function toast(m){$('toast').textContent=m;$('toast').classList.add('on');clearTimeout(toast.t);toast.t=setTimeout(()=>$('toast').classList.remove('on'),4000)}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,token:TOKEN})});return r.ok?r.json():{ok:false,msg:'Request refused ('+r.status+')'}}
let lastAgentStates={};
function applicationNotifications(ac){
  const requests=ac.requests||[],active=requests.filter(r=>r.state==='active');
  const banner=$('applyingnotice');
  banner.hidden=active.length===0;
  banner.textContent=active.map(r=>r.agent+' is applying (reported '+String(r.updated||'').slice(11,16)+')').join(' · ');
  for(const r of requests){
    const key=ac.day+':'+r.agent,previous=lastAgentStates[key];
    if(r.state==='active'&&previous!=='active'){
      const message=r.agent+' has picked up your request and is applying.';
      let seen=false;const eventKey='applying-notified:'+key+':'+r.updated;
      try{seen=sessionStorage.getItem(eventKey)==='yes';sessionStorage.setItem(eventKey,'yes')}catch(e){}
      if(!seen){
        toast(message);
        if('Notification' in window&&Notification.permission==='granted'){
          try{new Notification(r.agent+' is applying',{body:'Your application request has been acknowledged. Check the dashboard for confirmed submissions.',tag:key})}catch(e){}
        }
      }
    }else if(previous==='active'&&r.state==='finished'){
      toast(r.agent+' finished this application batch.');
    }
    lastAgentStates[key]=r.state;
  }
}
$('agentnotifications').addEventListener('click',async()=>{
  if(!('Notification' in window)){toast('Browser notifications are unavailable here. Dashboard alerts are always enabled.');return}
  try{const permission=await Notification.requestPermission();toast(permission==='granted'?'Agent notifications enabled.':'Dashboard alerts remain enabled.');$('agentnotifications').textContent=permission==='granted'?'Notifications enabled':'Enable notifications'}catch(e){toast('Use dashboard alerts in this browser.')}
});
let applySending=false;
function selectedAgents(){return ['Claude','Codex'].filter(a=>$(a==='Claude'?'applyclaude':'applycodex').checked)}
function updateApplyButton(){$('applynow').disabled=applySending||selectedAgents().length===0}
['applyclaude','applycodex'].forEach(id=>$(id).addEventListener('change',updateApplyButton));
$('applynow').addEventListener('click',async()=>{const agents=selectedAgents();if(!agents.length)return;applySending=true;updateApplyButton();try{const d=await post('/apply-now',{agents});toast(d.msg)}catch(e){toast('Could not queue applications. Please try again.')}finally{applySending=false;updateApplyButton();setTimeout(refreshApplications,800)}});
$('runb').addEventListener('click',async()=>{$('runb').disabled=true;const p=$('preset');const d=await post('/run',{preset:p.value});toast(d.msg);setTimeout(tick,800)});
$('stopb').addEventListener('click',async()=>{if(!confirm('Stop all running ApplyPilot steps? Everything found and scored so far is already saved. You can press Run to continue later.'))return;$('stopb').disabled=true;const d=await post('/stop',{});toast(d.msg);setTimeout(tick,800)});
function controls(){const ac=D.applicationControl||{};applicationNotifications(ac);$("applyprogress").textContent=`${ac.submitted||0} / ${ac.target||50} confirmed today · ${ac.remaining||0} to target`;$("applyagents").textContent=(ac.requests||[]).map(r=>r.agent+": "+r.state).join(" · ")||"Ready to queue requests. Agents must acknowledge before work starts.";const busy=(D.procs||[]).length>0;$('runb').disabled=busy;$('stopb').disabled=!busy;
  $('runb').title=busy?'A run is in progress. Stop it first.':'Start the selected step';$('stopb').title=busy?(D.procs.map(p=>p.what).join('\n')):'Nothing running'}

let workflowData=null,wfBusy=false,ppSel='search';
const PP={tok:[],t0:0,acc:0,prev:null,lastScoreChange:0};
const ppNS='http://www.w3.org/2000/svg';
function ppNode(id,cx,cy,icon,title,cls=''){return `<g class="pp-node ${cls}" id="pp-${id}" data-pp="${id}" tabindex="0" role="button" aria-label="${title}"><rect class="box" x="${cx-70}" y="${cy-65}" width="140" height="130" rx="18"/>${icon}<text class="pp-title" x="${cx}" y="${cy+4}">${title}</text><text class="pp-count" id="pp-${id}-n" x="${cx}" y="${cy+31}">–</text><text class="pp-cap" id="pp-${id}-c" x="${cx}" y="${cy+47}"></text><text class="pp-cap" id="pp-${id}-c2" x="${cx}" y="${cy+59}"></text></g>`}
function ppBuild(){const svg=$('pp-svg');if(svg.dataset.built)return;svg.dataset.built=1;
 const P={p1:'M155,200 L235,200',p2:'M375,200 L442,200',p3:'M500,142 C500,95 560,95 650,95',p4:'M790,95 L845,95',p5:'M500,258 C500,315 560,315 650,315'};
 const pipe=(id,no)=>`<path class="pp-pipe${no?' no':''}" d="${P[id]}"/><path class="pp-flow${no?' no':''}" id="pp-${id}" d="${P[id]}"/>`;
 const robot=`<g transform="translate(305,165)"><circle class="pp-spin" r="23" fill="none" stroke="var(--g)" stroke-width="2" stroke-dasharray="5 6" opacity=".7"/><line x1="0" y1="-12" x2="0" y2="-19" stroke="var(--g)" stroke-width="2"/><circle class="pp-glow" cy="-21" r="3.2" fill="var(--g)"/><rect x="-15" y="-12" width="30" height="24" rx="7" fill="#163d27" stroke="var(--g)" stroke-width="1.5"/><circle class="pp-blink" cx="-6" cy="-2" r="3" fill="var(--g)"/><circle class="pp-blink" cx="6" cy="-2" r="3" fill="var(--g)"/><path d="M-5,6 Q0,9 5,6" stroke="var(--g)" stroke-width="1.5" fill="none" stroke-linecap="round"/></g>`;
 const gate=`<g class="pp-node" id="pp-gate" data-pp="gate" tabindex="0" role="button" aria-label="Rules check"><polygon class="box pp-gate" points="500,142 558,200 500,258 442,200"/><text class="pp-cap" x="500" y="186">rules check</text><text class="pp-count" id="pp-gate-n" x="500" y="211" style="font-size:20px">≥ 8</text><text class="pp-cap" x="500" y="226">⚖️ + rules</text></g>`;
 svg.innerHTML=`${pipe('p1')}${pipe('p2')}${pipe('p3')}${pipe('p4')}${pipe('p5',1)}
 <text class="pp-lbl" x="585" y="82">✓ fits</text><text class="pp-lbl no" x="590" y="340">✗ below 8 / excluded</text>
 <text class="pp-cap" x="400" y="300">score ≥ 8 · US · sponsorship OK</text><text class="pp-cap" x="400" y="314">pay ≥ $80k · not a Lead role</text>
 <g id="pp-toks"></g><g id="pp-fx"></g>
 ${ppNode('search',85,200,'<text class="pp-emoji pp-bob" x="85" y="165">🔎</text>','Job search')}
 ${ppNode('scorer',305,200,robot,'Scorer')}${gate}
 ${ppNode('apply',720,95,'<text class="pp-emoji pp-bob" x="720" y="60">🚀</text>','Apply queue')}
 ${ppNode('applied',915,95,'<text class="pp-emoji" id="pp-party" x="915" y="60">🎉</text>','Applied')}
 ${ppNode('reject',720,315,'<text class="pp-emoji" id="pp-bin" x="720" y="280">🗑️</text>','Rejected','bad')}
 <rect x="865" y="148" width="100" height="5" rx="2.5" fill="#132a1d"/><rect id="pp-bar" x="865" y="148" width="0" height="5" rx="2.5" fill="var(--g)"/>`;
 svg.addEventListener('click',e=>{const n=e.target.closest('[data-pp]');if(n){ppSel=n.dataset.pp;ppUpdate()}});
 svg.addEventListener('keydown',e=>{const n=e.target.closest('[data-pp]');if(n&&(e.key==='Enter'||e.key===' ')){e.preventDefault();ppSel=n.dataset.pp;ppUpdate()}});
 requestAnimationFrame(ppLoop)}
function ppStats(){const w=workflowData||{},x=(D&&typeof dayStats==='function')?dayStats()[todayStr]||{}:{};
 const waiting=D?D.jobs.filter(j=>j.dF===todayStr&&!j.dS).length:0,rs=w.requests||[],st=a=>(rs.find(r=>r.agent===a)||{}).state||'idle';
 return {found:x.found||0,pm:x.pm||0,scored:x.scored||0,good:x.good||0,failed:x.failed||0,waiting,rej:Math.max(0,(x.scored||0)-(x.good||0)),
  ready:w.cached_candidates,claims:w.claims_today||0,submitted:w.submitted||0,target:w.target||50,blocked:w.blocked||0,claude:st('Claude'),codex:st('Codex'),gs:(D&&D.goodScore)||8}}
function ppSet(id,n,c,c2){const t=(k,v)=>{const e=$('pp-'+id+k);if(e&&e.textContent!==String(v))e.textContent=v};t('-n',n);t('-c',c||'');t('-c2',c2||'')}
function ppPop(id,cls){const e=$(id);if(!e)return;e.classList.remove(cls);void e.getBBox();e.classList.add(cls)}
function ppUpdate(){if(!$('pp-svg'))return;ppBuild();const s=ppStats(),f=n=>Number(n).toLocaleString();
 ppSet('search',f(s.found),f(s.pm)+' PM / PO roles',LV.now.found?'now: '+ppCut(LV.now.found,20):'today');
 ppSet('scorer',f(s.scored),f(s.waiting)+' waiting in line',LV.now.scored?'now: '+ppCut(LV.now.scored,20):s.failed?f(s.failed)+" couldn't be scored":'');
 $('pp-gate-n').textContent='≥ '+s.gs;
 ppSet('apply',s.ready==null?'?':f(s.ready),'ready to apply','Claude '+s.claude+' · Codex '+s.codex);
 ppSet('applied',f(s.submitted),'of '+s.target+' today','');
 ppSet('reject',f(s.rej),'scored below '+s.gs,'kept in the Jobs tab');
 $('pp-bar').setAttribute('width',Math.min(100,100*s.submitted/s.target));
 const p=PP.prev,now=performance.now();
 if(p&&s.scored>p.scored)PP.lastScoreChange=now;if(p&&s.submitted>p.submitted)ppPop('pp-applied-n','pp-pop');if(p&&s.rej>p.rej)ppPop('pp-reject-n','pp-pop');
 PP.prev=s;
 $('pp-svg').classList.toggle('pp-idle',!((D&&(D.procs||[]).length)||now-PP.lastScoreChange<300000));
 document.querySelectorAll('#pp-svg [data-pp]').forEach(n=>{n.classList.toggle('sel',n.dataset.pp===ppSel);n.setAttribute('aria-pressed',String(n.dataset.pp===ppSel))});
 const why={search:`<b>Job search</b> · ApplyPilot scans job boards for PM, PO and analyst roles in the US (remote or on-site). ${f(s.found)} found today, ${f(s.pm)} of them PM / PO.`,
  scorer:`<b>Scorer</b> · The AI scorer rates each job 1–10 against your approved resume. ${f(s.scored)} scored today, ${f(s.waiting)} of today's finds still waiting${s.failed?`, ${f(s.failed)} couldn't be scored (retried later)`:''}.`,
  gate:`<b>Rules check</b> · A job goes on to applying only if it scores ${s.gs}+ <i>and</i> passes your rules: US-based, no "no sponsorship" / citizenship / clearance requirement, pay at least $80k when listed, not a Lead role, and not an excluded company or industry.`,
  apply:`<b>Apply queue</b> · ${s.ready==null?'Queue unavailable':f(s.ready)+' jobs ready'}. Claude: ${s.claude} · Codex: ${s.codex}. ${f(s.claims)} jobs claimed today. A batch of 10 is queued for the agents whenever 10 are ready.`,
  applied:`<b>Applied</b> · ${f(s.submitted)} confirmed submissions today of the ${s.target} target${s.blocked?` · ${f(s.blocked)} need your help`:''}. Only a verified confirmation page counts.`,
  reject:`<b>Rejected</b> · ${f(s.rej)} jobs scored below ${s.gs} today. Nothing is sent for them; they stay listed in the Jobs tab.`};
 $('wf-detail').innerHTML=why[ppSel]}
function ppSpawn(segs,cls,label,fate){const svg=$('pp-svg');if(!svg||PP.tok.length>24||matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const g=document.createElementNS(ppNS,'g');g.setAttribute('class','pp-tok '+cls);g.style.opacity=0;
 g.innerHTML='<g class="bd"><rect class="pg" x="-9" y="-11" width="18" height="22" rx="4"/><line class="ln" x1="-5" y1="4" x2="5" y2="4"/><line class="ln" x1="-5" y1="7.5" x2="2" y2="7.5"/><circle class="ey" cx="-3.5" cy="-3" r="1.6"/><circle class="ey" cx="3.5" cy="-3" r="1.6"/></g><text class="pp-tl" y="-17"></text>';
 g.querySelector('text').textContent=label;$('pp-toks').appendChild(g);PP.tok.push({g,fate,segs,i:0,d:0,wait:0})}
function ppBurst(x,y,colors){const fx=$('pp-fx');for(let i=0;i<14;i++){const c=document.createElementNS(ppNS,'circle');c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r',2+Math.random()*2.5);c.setAttribute('fill',colors[i%colors.length]);fx.appendChild(c);
 const a=Math.random()*Math.PI*2,r=25+Math.random()*30;c.animate([{transform:'translate(0,0)',opacity:1},{transform:`translate(${Math.cos(a)*r}px,${Math.sin(a)*r-12}px)`,opacity:0}],{duration:900+Math.random()*400,easing:'cubic-bezier(.2,.7,.3,1)'}).onfinish=()=>c.remove()}}
function ppArrive(t){if(t.fate==='found'){ppPop('pp-scorer','pp-pop');return}
 if(t.fate==='applied'){ppPop('pp-party','pp-pop');ppBurst(915,60,['#22e07a','#7dffb4','#e0b84a','#e6f4ea'])}
 else if(t.fate==='yes'){ppPop('pp-apply','pp-pop');ppBurst(650,95,['#22e07a','#7dffb4'])}else{ppPop('pp-bin','pp-wob')}}
function ppLoop(ts){requestAnimationFrame(ppLoop);const svg=$('pp-svg');if(!svg||document.hidden||!svg.getClientRects().length){PP.t0=ts;return}
 const dt=Math.min(.05,(ts-(PP.t0||ts))/1000);PP.t0=ts;
 LV.gap-=dt;if(LV.gap<=0&&LV.q.length){ppRelease(LV.q.shift());LV.gap=LV.q.length>30?.15:LV.q.length>10?.35:.8}
 PP.tok=PP.tok.filter(t=>{if(t.wait>0){t.wait-=dt;return true}
  const path=$('pp-'+t.segs[t.i]),L=path.getTotalLength();t.d+=dt*95;
  if(t.d>=L){t.i++;t.d=0;if(t.i>=t.segs.length){ppArrive(t);t.g.animate([{opacity:1},{opacity:0}],{duration:250}).onfinish=()=>t.g.remove();return false}
   t.g.style.opacity=0;t.wait=.35;ppPop('pp-gate','pp-pop');return true}
  const pt=path.getPointAtLength(t.d);t.g.style.opacity=1;t.g.setAttribute('transform',`translate(${pt.x},${pt.y})`);t.g.firstChild.setAttribute('transform',`rotate(${Math.sin(ts/150+t.d)*6})`);return true})}
// Live feed: real jobs from /pipeline, released one at a time so each can be followed.
const LV={cursor:'',seen:new Set(),q:[],out:null,busy:false,gap:0,now:{}};
const ppCut=(s,n)=>s&&s.length>n?s.slice(0,n-1)+'…':(s||'');
const ppTime=t=>t?new Date(t.replace(' ','T')+'Z').toLocaleTimeString([], {hour:'numeric',minute:'2-digit',second:'2-digit'}):new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit',second:'2-digit'});
async function pollPipeline(){const svg=$('pp-svg');if(LV.busy||document.hidden||!svg||!svg.getClientRects().length)return;LV.busy=true;
 try{const r=await fetch('/pipeline?since='+encodeURIComponent(LV.cursor),{cache:'no-store'});if(!r.ok)return;const p=await r.json();if(p.error)return;
  LV.cursor=p.now;if(LV.seen.size>5000)LV.seen.clear();
  const ev=[...p.found.map(e=>({...e,kind:'found'})),...p.scored.map(e=>({...e,kind:'scored'})),...p.claims.map(e=>({...e,kind:'claim'}))].sort((a,b)=>a.t<b.t?-1:a.t>b.t?1:0);
  for(const e of ev){const k=e.kind+'|'+e.url;if(!LV.seen.has(k)){LV.seen.add(k);LV.q.push(e)}}
  if(p.more.found>0&&LV.q.length)LV.q.push({kind:'more',n:p.more.found});
  const ok=p.outcomes.map(o=>({...o,kind:'outcome',k:o.company+'|'+o.title+'|'+o.status}));
  if(!LV.out){LV.out=new Set(ok.map(o=>o.k));ok.slice(-5).forEach(o=>ppFeed({...o,old:true}))}
  else ok.forEach(o=>{if(!LV.out.has(o.k)){LV.out.add(o.k);LV.q.push(o)}});
  if(LV.q.length>80){const drop=LV.q.length-80;LV.q.splice(0,drop);LV.q.unshift({kind:'more',n:drop})}
 }catch(e){}finally{LV.busy=false}}
setInterval(pollPipeline,3000);
function ppRelease(e){const name=e.company||'Unknown company',who=ppCut(name,22);ppFeed(e);
 if(e.kind==='found'){LV.now.found=name;ppSpawn(['p1'],'',who,'found')}
 else if(e.kind==='scored'){LV.now.scored=name;
  if(e.route==='failed'){ppPop('pp-scorer','pp-wob');return}
  const go=e.route==='apply'||e.route==='pending';ppSpawn(['p2',go?'p3':'p5'],go?'yes':'no',who+(e.score?' · '+e.score:''),go?'yes':'no')}
 else if(e.kind==='claim'){ppPop('pp-apply','pp-pop');ppBurst(720,95,['#22e07a','#7dffb4'])}
 else if(e.kind==='outcome'&&/^(submit|applied)/i.test(e.status))ppSpawn(['p4'],'yes',who,'applied');
 ppUpdate()}
function ppFeed(e){const f=$('pp-feed');if(!f)return;let icon,txt,chip,cls='';
 const job=`<b>${esc(e.company||'Unknown company')}</b> · ${esc(e.title||'')}`;
 if(e.kind==='found'){icon='🔎';txt=job;chip='found';cls='info'}
 else if(e.kind==='scored'){icon='🤖';txt=job;
  [chip,cls]={low:[`scored ${e.score} → rejected`,'bad'],excluded:[ppCut((e.why||'excluded by a rule').replace(/^(Auto-filtered \(no AI\)|excluded):\s*/,'filtered: '),48),'bad'],failed:[`couldn't score · will retry`,'warn'],
    apply:[`scored ${e.score} → apply queue`,'ok'],pending:[`scored ${e.score} → rules check`,'ok'],held:[`scored ${e.score} · held back by rules`,'warn']}[e.route]||['scored','info']}
 else if(e.kind==='claim'){icon='🚀';txt=e.company?job:esc(ppCut(e.url,60));chip=e.agent+' started applying';cls='ok'}
 else if(e.kind==='outcome'){const s=e.status.toLowerCase();icon=/^(submit|applied)/.test(s)?'🎉':/needs|waiting|filled/.test(s)?'✋':'⏭️';txt=job;chip=`${e.status} · ${e.by}`;cls=icon==='🎉'?'ok':icon==='✋'?'warn':'bad'}
 else {icon='➕';txt=`${Number(e.n).toLocaleString()} more jobs moved through too fast to show one by one`;chip='';}
 const row=document.createElement('div');row.className='pp-row'+(e.old?' old':'');
 row.innerHTML=`<span class="pp-t">${e.old?'earlier':ppTime(e.t)}</span><span>${icon}</span><span class="pp-j">${txt}</span>${chip?`<span class="pp-chip ${cls}">${esc(chip)}</span>`:'<span></span>'}`;
 f.prepend(row);const em=f.querySelector('.empty');if(em)em.remove();while(f.children.length>40)f.lastChild.remove()}
function renderWorkflow(w){workflowData=w;ppUpdate();
 $('wf-updated').textContent='Updated '+new Date(w.observed_at).toLocaleTimeString();
 $('wf-summary').textContent='· '+w.submitted+' confirmed today · '+w.remaining+' to the daily target · refreshes every 3s';
 $('wf-events').innerHTML=(w.events||[]).map(e=>`<div class="wf-event"><b>${esc(e.company)}</b> · ${esc(e.title)}<span class="wtag">${esc(e.status)}</span><small>${esc(e.notes)}</small></div>`).join('')||'<div class="empty">No application outcomes recorded today.</div>';
}
async function pollWorkflow(){if(wfBusy)return;wfBusy=true;try{const r=await fetch('/workflow',{cache:'no-store'});if(!r.ok)throw Error('offline');const w=await r.json();renderWorkflow(w);applicationNotifications(w);if(D){D.applicationControl=w;controls()}}catch(e){$('wf-updated').textContent='Disconnected · showing last received update'}finally{wfBusy=false}}
setInterval(()=>{if(!document.hidden)pollWorkflow()},3000);pollWorkflow();

const DESC={};
async function loadDesc(id){if(DESC[id]!=null)return;try{const r=await fetch('/job?url='+encodeURIComponent(id));DESC[id]=(await r.json()).desc||'No description saved for this job.'}catch(e){DESC[id]='Could not load the description.'}const el=document.querySelector(`.desc[data-desc="${CSS.escape(id)}"]`);if(el)el.textContent=DESC[id]+(DESC[id].length>=1500?'…':'')}
async function tick(){try{const r=await fetch('/data?v='+(D&&D.ver||''),{cache:'no-store'});const d=await r.json();if(d.error)return;if(d.same){$('livetxt').textContent=$('livetxt').textContent.replace(/\d\d:\d\d:\d\d [AP]M/,d.now);return}D=d;applicationsVersion='';todayStr=d.today;render();controls()}catch(e){$('livetxt').textContent='DASHBOARD SERVER STOPPED';$('live').className='live idle'}}
let applicationsBusy=false,applicationsVersion='';
async function refreshApplications(){
  if(!D||applicationsBusy)return;
  applicationsBusy=true;
  try{
    const r=await fetch('/application-status',{cache:'no-store'});
    if(!r.ok)throw new Error('Status request failed');
    const a=await r.json();if(a.error)throw new Error(a.error);
    const version=JSON.stringify(a);if(version===applicationsVersion)return;
    applicationsVersion=version;Object.assign(D,a);todayStr=a.today;
    today();inbox();recruit();daily();badges();controls();
    if($('k7'))anim($('k7'),Object.values(a.appliedByDay).reduce((x,y)=>x+y,0));
    $('appupd').title='Application status refreshed';
  }catch(e){applicationsVersion='';$('appupd').textContent=' · status refresh delayed; retrying'}
  finally{applicationsBusy=false}
}
(async function loop(){await tick();await refreshApplications();setTimeout(loop,30000)})();
(async function applicationLoop(){await refreshApplications();setTimeout(applicationLoop,3000)})();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", action="append", default=[], help="folder of pipeline *.log files to follow (repeatable)")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    LOG_DIRS += [Path(d) for d in a.log_dir]
    print(f"Job dashboard live at http://localhost:{a.port}  (Ctrl+C to stop)")
    threading.Thread(target=check_openrouter_key, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
