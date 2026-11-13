#!/usr/bin/env python3
"""Find the company's own apply link for 7+ LinkedIn jobs, with no browser and no AI.

Usage: python3 recover_links.py [--limit N] [--dry-run]

For each qualifying LinkedIn posting that has no apply link yet (newest scores first):
  1. Read LinkedIn's public (signed-out) job page.
     - "No longer accepting applications"  -> added to dead.txt (never opened by an agent)
     - Easy Apply only (applied on LinkedIn) -> the LinkedIn job page is saved as the apply link (Easy Apply is
       allowed since 2026-10-28; you are signed in to LinkedIn in Chrome)
     - otherwise the company name and full description are saved to the database, so build_queue.py's
       sponsorship / citizenship check sees the whole listing, not LinkedIn's short snippet
  2. Look the job up on the company's public job board (Greenhouse, Lever, Ashby, Workable APIs).
     A link is saved only when exactly one open job there has the same title; otherwise the job is left
     for an agent's normal link recovery.
Board lookups are cached in ats_boards.json so each company is fetched at most once a day.
"""
import argparse
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from job_url import normalize as norm  # noqa: E402
from us_only import non_us_location  # noqa: E402  (USA and remote-USA only, 2026-10-29)

DB = Path.home() / ".applypilot" / "applypilot.db"
CACHE = HERE / "ats_boards.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
FDE_MIN = 6  # Forward Deployed Product Manager roles qualify at 6+, same as build_queue.py


def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1_500_000).decode("utf-8", "replace")


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def linkedin_page(job_id):
    """Return dict(state=closed|easy|offsite|unknown, company, description) from the public job page."""
    s = fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}")
    company = text_of((re.search(r'class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', s, re.S) or [None, ""])[1])
    desc = text_of((re.search(r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', s, re.S) or [None, ""])[1])
    if re.search(r"No longer accepting applications", s, re.I):
        state = "closed"
    elif "public_jobs_apply-link-onsite" in s:
        state = "easy"
    elif "public_jobs_apply-link-offsite" in s:
        state = "offsite"
    else:
        state = "unknown"
    return {"state": state, "company": company, "description": desc}


def slugs(company):
    """Likely job-board names for a company, e.g. 'Acme Health, Inc.' -> acmehealth, acme-health, acme."""
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|technologies|technology|the)\b\.?", " ",
                  company.lower())
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    out = ["".join(words), "-".join(words), words[0]]
    return list(dict.fromkeys(s for s in out if len(s) >= 3))


def board_jobs(kind, slug):
    """Open jobs on one public board as [(title, apply_url)], or None if the board doesn't exist."""
    try:
        if kind == "greenhouse":
            d = json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
            return [(j["title"], j["absolute_url"], (j.get("location") or {}).get("name", "")) for j in d.get("jobs", [])]
        if kind == "lever":
            d = json.loads(fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
            return [(j["text"], j.get("applyUrl") or j["hostedUrl"], (j.get("categories") or {}).get("location", ""))
                    for j in d] if isinstance(d, list) else None
        if kind == "ashby":
            d = json.loads(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
            return [(j["title"], j.get("applyUrl") or j["jobUrl"], j.get("location") or "") for j in d.get("jobs", [])]
        if kind == "workable":
            d = json.loads(fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"))
            return [(j["title"], j["url"], ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x))
                    for j in d.get("jobs", [])]
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 400):
            return None
        raise
    except (ValueError, KeyError, TypeError):
        return None
    return None


def title_key(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (t or "").lower())
    t = re.sub(r"\b(sr|snr)\b\.?", "senior", t)
    t = re.split(r"\s[-–|,]\s", t)[0]  # drop " - Remote", " | NYC" style suffixes
    return " ".join(re.findall(r"[a-z0-9]+", t))


def same_place(board_loc, job_loc):
    """True when the board job's location fits the posting's (shared city/state word, or either is remote/blank).
    Guards against a different company that happens to use the same board name."""
    b, j = (board_loc or "").lower(), (job_loc or "").lower()
    if not b or not j or "remote" in b or "remote" in j:
        return True
    stop = {"usa", "us", "united", "states", "of", "america", "the", "and", "hybrid", "on", "site", "onsite"}
    words = lambda t: {w for w in re.findall(r"[a-z]{2,}", t) if w not in stop}
    return bool(words(b) & words(j))


def find_on_boards(company, title, cache, job_loc=""):
    """The single board job with the same title (and a matching location), as an apply URL, else None."""
    today = date.today().isoformat()
    want = title_key(title)
    for slug in slugs(company):
        for kind in ("greenhouse", "lever", "ashby", "workable"):
            key = f"{kind}:{slug}"
            hit = cache.get(key)
            if not hit or hit.get("day") != today:
                try:
                    jobs = board_jobs(kind, slug)
                except Exception:
                    continue  # network trouble: try again another run
                cache[key] = hit = {"day": today, "jobs": jobs}
                time.sleep(0.3)
            if hit["jobs"] is None or (hit["jobs"] and len(hit["jobs"][0]) < 3):
                if hit["jobs"] is not None:  # cached before locations were stored: refetch next run
                    cache.pop(key, None)
                continue
            same = {u for t, u, loc in hit["jobs"] if title_key(t) == want and same_place(loc, job_loc)}
            if len(same) == 1:
                return same.pop()
            if len(same) > 1:
                return None  # several openings with that title: let an agent pick the right one
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=60, help="postings to check this run (default 60)")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    a = ap.parse_args()

    tried = {norm(l.strip()) for l in (HERE / "tried.txt").read_text().splitlines() if l.strip()} \
        if (HERE / "tried.txt").exists() else set()
    dead = {norm(l.strip()) for l in (HERE / "dead.txt").read_text().splitlines() if l.strip()} \
        if (HERE / "dead.txt").exists() else set()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    conn = sqlite3.connect(DB, timeout=30)
    # some ApplyPilot versions have no company column; add it so names found on LinkedIn can be saved
    if "company" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs ADD COLUMN company TEXT")
        conn.commit()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT url, title, company, location, full_description FROM jobs WHERE applied_at IS NULL "
        "AND (fit_score>=7 OR (fit_score>=? AND (title LIKE '%forward deployed product%' OR title LIKE '%forward-deployed product%'))) "
        "AND url LIKE '%linkedin.com/jobs/view/%' AND (application_url IS NULL OR application_url IN ('','None')) "
        "ORDER BY fit_score DESC, scored_at DESC", (FDE_MIN,)).fetchall()
    rows = [r for r in rows if norm(r["url"]) not in tried and norm(r["url"]) not in dead
            and not non_us_location(r["location"])][: a.limit]

    counts = {"checked": 0, "closed": 0, "easy apply": 0, "link found": 0, "no board match": 0, "page error": 0}
    new_dead, new_tried = [], []
    for r in rows:
        job_id = (re.findall(r"(\d{8,})", r["url"]) or [None])[-1]
        if not job_id:
            continue
        counts["checked"] += 1
        try:
            page = linkedin_page(job_id)
        except Exception:
            counts["page error"] += 1
            time.sleep(2)
            continue
        time.sleep(1.5)  # stay well under LinkedIn's public rate limits
        if page["state"] == "closed":
            counts["closed"] += 1
            new_dead.append(norm(r["url"]))
            continue
        if page["state"] == "easy":
            counts["easy apply"] += 1
            if not a.dry_run:
                conn.execute("UPDATE jobs SET application_url=? WHERE url=?",
                             (f"https://www.linkedin.com/jobs/view/{job_id}/", r["url"]))
                if len(page["description"]) > len(r["full_description"] or ""):
                    conn.execute("UPDATE jobs SET full_description=? WHERE url=?", (page["description"], r["url"]))
                conn.commit()
            continue
        company = (r["company"] or "").strip() or page["company"]
        if not a.dry_run:
            if page["company"] and not (r["company"] or "").strip():
                conn.execute("UPDATE jobs SET company=? WHERE url=?", (page["company"], r["url"]))
            if len(page["description"]) > len(r["full_description"] or ""):
                conn.execute("UPDATE jobs SET full_description=? WHERE url=?", (page["description"], r["url"]))
        link = find_on_boards(company, r["title"], cache, r["location"] or "") if company else None
        if link:
            counts["link found"] += 1
            print(f"link: {company} | {r['title'][:60]} -> {link}")
            if not a.dry_run:
                conn.execute("UPDATE jobs SET application_url=? WHERE url=?", (link, r["url"]))
        else:
            counts["no board match"] += 1
        if not a.dry_run:
            conn.commit()

    if not a.dry_run:
        if new_dead:
            with open(HERE / "dead.txt", "a") as fh:
                fh.writelines(u + "\n" for u in new_dead)
        if new_tried:
            with open(HERE / "tried.txt", "a") as fh:
                fh.writelines(u + "\n" for u in new_tried)
        CACHE.write_text(json.dumps(cache))
    print("recover links:", json.dumps(counts))


if __name__ == "__main__":
    main()
