#!/usr/bin/env python3
"""Which resume went with which application.

Every submission gets a row in Applications/resume_log.csv and the exact file sent is frozen in
Applications/resumes_used/<sha8>-<name>, so an interview months later can be prepared from what the
company actually saw, even if the resume has changed since.

  resumes.py record --company X --title Y --url U [--resume FILE] [--agent Claude|Codex]
  resumes.py lookup <company or title words>
  resumes.py backfill      # best guess for past submissions that were logged without a resume
"""
import argparse
import csv
import hashlib
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Paths, app_key, norm, submitted_apps, today  # noqa: E402

FIELDS = ["date", "company", "title", "url", "agent", "resume", "file", "sha256", "source"]


def label(path, paths):
    """Short human name: 'Default resume', 'Product_Owner', ..."""
    p = Path(path)
    if p.resolve() == paths.approved.resolve():
        return "Default resume"
    m = re.search(r"Resume_(.+)$", p.stem)
    return m.group(1) if m else f"{p.parent.name}/{p.name}"


def snapshot(path, paths):
    """Copy the file into resumes_used/ once per distinct content; returns (sha256, path relative to root)."""
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    dest = paths.snapshots / f"{sha[:8]}-{Path(path).name}"
    if not dest.exists():
        shutil.copy2(path, dest)
    return sha, str(dest.relative_to(paths.root))


def rows(paths):
    try:
        with open(paths.resume_log, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def record(company, title, url="", resume=None, agent="", source="recorded", when=None, paths=None):
    """Append one row (skipped if this application already has a row for the same file)."""
    paths = paths or Paths()
    if not resume:
        resume, source = paths.approved, "default (the agent didn't say which resume)"
    resume = Path(resume)
    if not resume.exists():
        raise FileNotFoundError(f"resume not found: {resume}")
    sha, snap = snapshot(resume, paths)
    k = app_key(company, title)
    if any(app_key(r["company"], r["title"]) == k and r["sha256"] == sha for r in rows(paths)):
        return None
    row = {"date": when or today().isoformat(), "company": company, "title": title, "url": url, "agent": agent,
           "resume": label(resume, paths), "file": snap, "sha256": sha, "source": source}
    new = not paths.resume_log.exists()
    paths.resume_log.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.resume_log, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def guess(app, paths, extra_dirs=()):
    """Best guess for a past submission: a resume file named in the notes, else the default resume."""
    notes = app["notes"].lower()
    for d in extra_dirs:  # e.g. another folder with role-specific resumes
        for f in Path(d).glob("*Resume*.pdf"):
            if f.stem.lower() in notes or (label(f, paths).lower() in notes and label(f, paths) != f.name):
                return f, "from the notes"
    if re.search(r"approved (one|resume)|default resume|generic resume", notes):
        return paths.approved, "from the notes"
    if paths.approved.exists():
        return paths.approved, "default resume (not recorded at the time)"
    return None, "unknown"


def backfill(paths=None, extra_dirs=()):
    paths = paths or Paths()
    have = {app_key(r["company"], r["title"]) for r in rows(paths)}
    added = unknown = 0
    for k, a in sorted(submitted_apps(paths).items(), key=lambda kv: kv[1]["date"]):
        if k in have:
            continue
        f, how = guess(a, paths, extra_dirs)
        if f is None:
            unknown += 1
            continue
        record(a["company"], a["title"], resume=f, agent="", source=f"inferred {how}", when=a["date"], paths=paths)
        added += 1
    return added, unknown


def lookup(query, paths=None):
    paths = paths or Paths()
    words = norm(query).split()  # every word must appear in the company or title: "amazon claims"
    return [r for r in rows(paths) if all(w in norm(f"{r['company']} {r['title']}").split() for w in words)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--company", required=True)
    r.add_argument("--title", required=True)
    r.add_argument("--url", default="")
    r.add_argument("--resume", default="")
    r.add_argument("--agent", default="")
    sub.add_parser("backfill")
    lk = sub.add_parser("lookup")
    lk.add_argument("query", nargs="+")
    a = ap.parse_args(argv)
    paths = Paths()
    if a.cmd == "record":
        row = record(a.company, a.title, a.url, a.resume or None, a.agent, paths=paths)
        print(f"resume logged: {row['resume']} ({row['file']})" if row else "resume already logged for this application")
    elif a.cmd == "backfill":
        added, unknown = backfill(paths)
        print(f"backfilled {added} past submissions; {unknown} left unknown")
    else:
        found = lookup(" ".join(a.query), paths)
        for x in found:
            print(f"{x['date']}  {x['company']} — {x['title']}\n    resume: {x['resume']}  ->  {x['file']}\n    how we know: {x['source']}")
        if not found:
            print("no submission found for that")


if __name__ == "__main__":
    main()
