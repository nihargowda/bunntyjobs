#!/usr/bin/env python3
"""Set aside unscored jobs that could never be applied to, so the free AI tiers only score real candidates.

Usage: python3 prefilter.py          -> marks them, prints counts
       python3 prefilter.py --undo   -> puts every job it marked back in the scoring line

A job is set aside (no AI used) when its title doesn't match scan_title_keep in config/settings.json, its
listing refuses visa sponsorship (only when need_sponsorship is on), it's in an excluded industry, it's
outside the US (when us_only is on), or it's on an account-only site. It gets fit_score 1 with score_reasoning "Auto-filtered (no AI): <reason>".
Every change is appended to prefiltered.txt so --undo can reverse it exactly.
"""
import re, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
from us_only import non_us_location
from settings import SETTINGS as S, rx  # noqa: E402
DB = Path.home() / ".applypilot" / "applypilot.db"
LOG = HERE / "prefiltered.txt"
TAG = "Auto-filtered (no AI): "

# same rules build_queue.py applies before applying
src = (HERE / "build_queue.py").read_text()
ns = {"re": re}
for name in ("SPON", "NO_AUTOMATION"):
    m = re.search(rf"^{name} = .*?(?=^\S)", src, re.S | re.M)
    exec(m.group(0), ns)
SPON, NO_AUTOMATION = ns["SPON"], ns["NO_AUTOMATION"]
EXCLUDE = rx(S["excluded_industries"])
DESC_EXCLUDE = rx(S.get("description_exclude", []))  # checked against the whole description
RELEVANT = rx(S["scan_title_keep"]) if S["scan_title_keep"] else None  # None: every title is relevant

conn = sqlite3.connect(DB, timeout=30)

if "--undo" in sys.argv:
    urls = [l.split("\t")[0] for l in LOG.read_text().splitlines() if l.strip()] if LOG.exists() else []
    n = 0
    for u in urls:
        n += conn.execute("UPDATE jobs SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL WHERE url=? AND score_reasoning LIKE ?",
                          (u, TAG + "%")).rowcount
    conn.commit()
    LOG.unlink(missing_ok=True)
    print(f"restored {n} jobs to the scoring line")
    sys.exit()


def reason(title, desc, apply_url, location=""):
    place = non_us_location(location) if S["us_only"] else None
    if place:
        return f"outside the US ({place})"
    if RELEVANT and not RELEVANT.search(title):
        return "title doesn't match scan_title_keep"
    if S["need_sponsorship"] and SPON.search(desc):
        return "listing refuses visa sponsorship or needs citizenship/clearance"
    if EXCLUDE.search(title + " " + desc[:2500]):
        return "excluded industry"
    if DESC_EXCLUDE.search(desc):
        return "description asks for experience you excluded"
    if apply_url and any(x in apply_url for x in NO_AUTOMATION):
        return "account-only application site"
    return None


now = datetime.now(timezone.utc).isoformat()
marked, counts = [], {}
rows = conn.execute("SELECT url, title, full_description, application_url, location FROM jobs "
                    "WHERE full_description IS NOT NULL AND fit_score IS NULL").fetchall()
for url, title, desc, apply_url, loc in rows:
    why = reason(title or "", desc or "", apply_url or "", loc or "")
    if why:
        conn.execute("UPDATE jobs SET fit_score=1, score_reasoning=?, scored_at=? WHERE url=? AND fit_score IS NULL",
                     (TAG + why, now, url))
        marked.append(f"{url}\t{why}\n")
        counts[why] = counts.get(why, 0) + 1
conn.commit()
with open(LOG, "a") as fh:
    fh.writelines(marked)
print(f"checked {len(rows)} unscored jobs; set aside {len(marked)}; left for AI scoring {len(rows) - len(marked)}")
for k, v in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {v:5d}  {k}")
