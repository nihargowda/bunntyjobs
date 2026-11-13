#!/usr/bin/env python3
"""Mark a submitted application as applied in the ApplyPilot database, so the database and
Applications/applications_log.csv agree (the dashboard counts both).

  mark_applied.py "<apply url>" "<company>" "<title>"       called by record.sh on every "submitted"
  mark_applied.py --backfill [--dry-run]                     fix past log submissions missing from the database

Matching, most to least certain (never guesses between several jobs):
  1. the apply link or posting link, after dropping tracking parameters (job_url.normalize)
  2. company + job title, only when exactly one application link fits
Times are stored in UTC (like SQLite's datetime('now')); backfilled rows get noon local time (settings: timezone) on the log's date.
"""
import csv, re, sqlite3, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from job_url import normalize
import dashboard as d  # resolve_company
from settings import SETTINGS  # noqa: E402


def noon_utc(day):
    """Noon on that date in your timezone, as a UTC timestamp string (how SQLite stores times)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    t = datetime.fromisoformat(day + "T12:00").replace(tzinfo=ZoneInfo(SETTINGS["timezone"]))
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

DB = Path.home() / ".applypilot" / "applypilot.db"
LOG = HERE.parent / "Applications" / "applications_log.csv"
canon = lambda t: re.sub(r"\W+", " ", re.sub(r"\s*\(.*?\)", "", (t or "").lower())).strip()


def load(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT rowid, url, application_url, title, company, site, strategy, full_description, "
                        "description, applied_at FROM jobs").fetchall()


def find(rows, url, company, title):
    """Return (rowids, how). Several rowids only when they share one apply link (duplicate listings)."""
    if url:
        k = normalize(url)
        hit = [r["rowid"] for r in rows if k and (normalize(r["application_url"]) == k or normalize(r["url"]) == k)]
        if hit:
            return hit, "link"
    co, ti = (company or "").strip().lower(), canon(title)
    if not co or not ti:
        return [], "no match"
    hit = [r for r in rows if canon(r["title"]) == ti and
           co in {(r["company"] or "").strip().lower(), d.resolve_company(r)[0].strip().lower()}]
    links = {normalize(r["application_url"] or r["url"]) for r in hit}
    if hit and len(links) == 1:
        return [r["rowid"] for r in hit], "company+title"
    return [], ("ambiguous (%d jobs)" % len(links)) if hit else "no match"


def mark(conn, rowids, when_sql="datetime('now')"):
    q = ",".join("?" * len(rowids))
    n = conn.execute(f"UPDATE jobs SET applied_at={when_sql}, apply_status='applied' WHERE rowid IN ({q}) AND applied_at IS NULL",
                     rowids).rowcount
    conn.commit()
    return n


def main():
    conn = sqlite3.connect(DB, timeout=30)
    rows = load(conn)
    if "--backfill" in sys.argv:
        dry = "--dry-run" in sys.argv
        stats, seen = {}, set()
        for r in csv.DictReader(open(LOG, newline="", encoding="utf-8", errors="replace")):
            st = (r.get("status") or "").strip().lower()
            if not (st.startswith("submit") or st == "applied"):
                continue
            key = (r["company"].strip().lower(), canon(r["title"]))
            if key in seen:
                continue
            seen.add(key)
            ids, how = find(rows, "", r["company"], r["title"])
            already = ids and all(x["applied_at"] for x in rows if x["rowid"] in ids)
            tag = "already in db" if already else how
            stats[tag] = stats.get(tag, 0) + 1
            if ids and not already and not dry and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"].strip()):
                mark(conn, ids, f"'{noon_utc(r['date'].strip())}'")  # noon local time, stored as UTC
        print(("DRY RUN " if dry else "") + "log submissions:", sum(stats.values()), stats)
        return
    url, company, title = (sys.argv[1:4] + ["", "", ""])[:3]
    ids, how = find(rows, url, company, title)
    if not ids:
        print(f"not in the ApplyPilot database ({how}); logged in applications_log.csv only")
        return
    print(f"db rows marked applied: {mark(conn, ids)} (matched by {how})")


if __name__ == "__main__":
    main()
