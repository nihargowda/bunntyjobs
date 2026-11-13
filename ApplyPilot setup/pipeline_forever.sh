#!/bin/sh
# Keep ApplyPilot finding, scoring and drafting jobs non-stop (find & draft only, never `applypilot apply`).
# Start:  sh pipeline_forever.sh applypilot run forever      (the extra words let the dashboard's Stop button find it)
# Every minute it starts whichever step is needed and not already running (by this or any other session):
#   search every 2h · enrich new listings · score unscored jobs · draft resumes + cover letters for new 8+ jobs
# Logs go to ~/.applypilot/logs/ so the dashboard shows them live. Runs only while this process is alive.

AP="$HOME/.applypilot-venv/bin/applypilot"
APP="$HOME/.applypilot"
LOGS="$APP/logs"
DB="$APP/applypilot.db"
DRAFTS=0            # 1 = draft resumes + cover letters for 8+ jobs. Off since 2026-10-24 (tailor/cover are patched off in the venv)
SEARCH_EVERY=7200   # seconds between job searches
RETRY_AFTER=600     # wait this long before re-running a step that just finished with work left (rate limits etc.)
mkdir -p "$LOGS"
cd "$APP" || exit 1

q() { sqlite3 -readonly "$DB" "$1" 2>/dev/null || echo 0; }
running() { pgrep -f "$1" >/dev/null; }
age() {  # seconds since the newest file matching the glob(s) was written; huge if none
  newest=$(ls -t $@ 2>/dev/null | head -1)
  [ -n "$newest" ] && echo $(( $(date +%s) - $(stat -f %m "$newest") )) || echo 999999
}
start() {  # start <name> <applypilot args...>
  name=$1; shift
  log="$LOGS/$name-$(date +%Y%m%d-%H%M%S).log"
  echo "$(date +%H:%M:%S) starting $name: applypilot $*"
  ( nice -n 10 "$AP" "$@" >> "$log" 2>&1; echo "EXIT: $?" >> "$log" ) &
}

echo "$(date +%H:%M:%S) pipeline_forever started (pid $$)"
while true; do
  # If the Mac background job (launchd label from config/settings.json -> ~/.applypilot/pipeline.sh) is installed,
  # it already searches, enriches and scores; doing it here too just doubles the AI rate limits. Then only draft.
  managed=0
  label=$(cd "$(dirname "$0")" && python3 -c 'from settings import SETTINGS; print(SETTINGS["launchd_label"])' 2>/dev/null)
  launchctl list 2>/dev/null | grep -q "${label:-com.applypilot.pipeline}" && managed=1

  # 0. free pre-filter: set aside new jobs that could never be applied to, before any AI scores them
  python3 "$(dirname "$0")/auto/prefilter.py" >/dev/null 2>&1

  # 1. search for new jobs every 2 hours
  if [ $managed -eq 0 ] && ! running "applypilot run discover|[a]gency_search.py" && \
     [ "$(age "$LOGS"/discover-*.log /private/tmp/claude-501/*/*/scratchpad/discover*.log)" -gt $SEARCH_EVERY ]; then
    start discover run discover
  fi

  # 2. fetch full descriptions for new listings
  pending_enrich=$(q "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND detail_error IS NULL")
  if [ $managed -eq 0 ] && [ "$pending_enrich" -gt 0 ] && ! running "applypilot run enrich|applypilot run.* enrich" && \
     [ "$(age "$LOGS"/enrich-*.log)" -gt $RETRY_AFTER ]; then
    start enrich run enrich
  fi

  # 3. score everything that has a description but no score
  unscored=$(q "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL")
  if [ $managed -eq 0 ] && [ "$unscored" -gt 0 ] && ! running "run_scoring|applypilot run score|applypilot run enrich score" && \
     [ "$(age "$LOGS"/score-*.log)" -gt $RETRY_AFTER ]; then
    start score run score
  fi

  # 4. draft a tailored resume + cover letter for every new 8+ job
  undrafted=$(q "SELECT COUNT(*) FROM jobs WHERE fit_score >= 8 AND full_description IS NOT NULL AND ((tailored_resume_path IS NULL AND COALESCE(tailor_attempts,0) < 5) OR (tailored_resume_path IS NOT NULL AND cover_letter_path IS NULL AND COALESCE(cover_attempts,0) < 5)) AND applied_at IS NULL")
  if [ "$DRAFTS" = 1 ] && [ "$undrafted" -gt 0 ] && ! running "applypilot run tailor|applypilot run cover"; then
    start drafts run tailor cover pdf --min-score 8 --validation lenient
  fi

  sleep 60
done
