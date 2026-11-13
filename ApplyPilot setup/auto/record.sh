#!/bin/bash
# Record one application outcome.
# usage: record.sh "<apply-url>" <submitted|skipped|FILLED - needs you> "<company>" "<title>" "<notes>" [--log-only]
#  - always adds the URL to tried.txt (so later runs don't retry it) and a row to applications_log.csv
#  - on "submitted", also marks the job applied in the ApplyPilot database and logs the resume used
#    (set RESUME_FILE=<the exact PDF you uploaded> and APPLY_AGENT=Claude|Codex; without RESUME_FILE the
#     default resume from config/settings.json is logged and marked as an assumption)
HERE="$(cd "$(dirname "$0")" && pwd)"
URL="$1"; ST="$2"; CO="$3"; TI="$4"; NO="$5"
LOG="$HERE/../Applications/applications_log.csv"
K=$(python3 "$HERE/job_url.py" "$URL")
[ -n "$K" ] && echo "$K" >> "$HERE/tried.txt"
if [ "$ST" = "submitted" ] && [ -n "$K" ] && [ "$6" != "--log-only" ]; then
  # match by cleaned apply/posting link, else by company + title (only when exactly one job fits)
  python3 "$HERE/mark_applied.py" "$URL" "$CO" "$TI" || echo "Database update failed; the outcome will still be logged." >&2
fi
# csv module quotes fields so commas or quotes in company/title/notes can't shift columns
# first run: create the history folder and its header row
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] || echo "date,company,title,status,notes" > "$LOG"
# the date is today in your timezone (config/settings.json)
python3 -c 'import csv, sys, datetime; from zoneinfo import ZoneInfo; sys.path.insert(0, sys.argv[1]); from settings import SETTINGS; csv.writer(open(sys.argv[2], "a", newline="")).writerow([datetime.datetime.now(ZoneInfo(SETTINGS["timezone"])).date().isoformat()] + sys.argv[3:])' \
  "$HERE/.." "$LOG" "$CO" "$TI" "$ST" "$NO" || { echo "Could not write $LOG; nothing was logged." >&2; exit 1; }
echo "logged: $CO - $ST"
if [ "$ST" = "submitted" ]; then
  # 2026-11-02: flag submissions that didn't use the resume the queue assigned to that job
  python3 - "$HERE/queue.json" "$URL" "${RESUME_FILE:-}" <<'PY' || true
import json, sys, os
q, url, used = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    jobs = json.load(open(q))
except (OSError, ValueError):
    sys.exit(0)
for j in jobs:
    if j.get("apply") == url and j.get("resume"):
        if os.path.basename(used) != os.path.basename(j["resume"]):
            print(f"WARNING: resume mismatch. Queue assigned {os.path.basename(j['resume'])}, submission used {used or 'nothing recorded'}.", file=sys.stderr)
        break
PY
  python3 "$HERE/../tracker/resumes.py" record --company "$CO" --title "$TI" --url "$URL" \
    --resume "${RESUME_FILE:-}" --agent "${APPLY_AGENT:-}" || echo "Resume log failed; record it with tracker/resumes.py record." >&2
fi
