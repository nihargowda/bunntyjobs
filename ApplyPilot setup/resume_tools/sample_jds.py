#!/usr/bin/env python3
"""Real job descriptions for a role, from the jobs your scan found, to tailor a resume against.

  python3 sample_jds.py "product owner"            -> up to 5 descriptions whose title matches (case-insensitive pattern)
  python3 sample_jds.py "business analyst" 3       -> up to 3
  python3 sample_jds.py "product owner" --terms    -> just the skills and terms that recur across them

Highest-scored jobs first. Each description is cut to 2,500 characters. Needs at least one scan
(`applypilot run discover`); before that, paste two or three job descriptions instead.
"""
import collections
import re
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".applypilot" / "applypilot.db"
# common job-description terms worth matching in a resume (only ever used if the resume can back them up)
TERMS = ["roadmap", "backlog", "user stories", "acceptance criteria", "stakeholder", "agile", "scrum", "sprint", "jira",
         "confluence", "sql", "python", "excel", "tableau", "power bi", "a/b test", "experimentation", "kpi", "metrics",
         "dashboard", "requirements", "uat", "api", "integration", "saas", "b2b", "b2c", "customer discovery",
         "user research", "prioritiz", "go-to-market", "cross-functional", "product strategy", "analytics", "figma",
         "data-driven", "process improvement", "root cause", "documentation", "compliance", "machine learning", "ai",
         "payments", "security", "cloud", "aws", "azure", "salesforce", "workflow", "release", "launch", "okr"]


def descriptions(pattern, n):
    if not DB.exists():
        sys.exit(f"No job database at {DB} yet. Run a scan first, or paste job descriptions instead.")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute("SELECT title, COALESCE(full_description, description, '') FROM jobs "
                     "WHERE length(COALESCE(full_description, description, '')) > 400 "
                     "ORDER BY COALESCE(fit_score, 0) DESC, discovered_at DESC").fetchall()
    rx = re.compile(pattern, re.I)
    return [(t, d) for t, d in rows if rx.search(t or "")][:n]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    pattern, n = args[0], int(args[1]) if len(args) > 1 else 5
    found = descriptions(pattern, 50 if "--terms" in sys.argv else n)
    if not found:
        sys.exit(f"No scanned jobs with a title matching '{pattern}'. Paste job descriptions instead.")
    if "--terms" in sys.argv:
        # whole words, except the stems meant to cover several forms ("prioritiz" -> prioritize / prioritization)
        stems = {"prioritiz", "a/b test"}
        pats = {t: re.compile(r"\b" + re.escape(t) + ("" if t in stems else r"\b"), re.I) for t in TERMS}
        freq = collections.Counter(t for _, d in found for t, rx in pats.items() if rx.search(d))
        print(f"Terms in {len(found)} '{pattern}' descriptions (share of postings):")
        for term, k in freq.most_common(25):
            print(f"  {k * 100 // len(found):3d}%  {term}")
        return
    for i, (title, desc) in enumerate(found, 1):
        text = re.sub(r"\s+", " ", desc)[:2500]
        print(f"=== {i}. {title}\n{text}\n")


if __name__ == "__main__":
    main()
