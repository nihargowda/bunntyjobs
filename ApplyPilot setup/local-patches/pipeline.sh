#!/bin/bash
# Continuous ApplyPilot pipeline: scan -> score -> enrich, forever.
# Run by launchd (label set in config/settings.json, launchd_label). Logs: ~/.applypilot/logs/pipeline-YYYYMMDD.log
# - Discovery (job-board scan) starts every 3 hours, in the background, unless one is already running.
# - Each cycle scores first (jobs that already have descriptions), then enriches new listings for at most
#   ENRICH_MAX seconds, so slow or failing enrichment can never hold up scoring.
# - Scoring uses every free key each cycle: Gemini first (each model capped per cycle),
#   then OpenRouter once a day (up to OPENROUTER_DAILY jobs, only if OPENROUTER_API_KEY is in .env;
#   if its model is rate-limited, the rest goes to OPENROUTER_FALLBACK),
#   then Groq; Gemini and Groq each rotate through several free models (see GEMINI_MODELS / GROQ_MODELS).
# - After scoring, auto/apply_trigger.py queues an application batch whenever 10 more jobs are ready.
# Never runs `applypilot apply`.

A="$HOME/.applypilot"
source "$HOME/.applypilot-venv/bin/activate"
cd "$A" || exit 1
# the repository's "ApplyPilot setup" folder, written by setup.sh
REPO=$(cat "$A/repo_path" 2>/dev/null)
mkdir -p "$A/logs"
GROQ_KEY=$(grep '^GROQ_API_KEY=' "$A/.env" | cut -d= -f2-)
# Free models take turns (one at a time; never parallel). Each model has its own daily allowance, so rotating
# multiplies free scoring. A model that reports its quota used up is skipped until tomorrow (.exhausted-DATE).
GEMINI_MODELS="gemini-3.5-flash-lite gemini-3.1-flash-lite gemini-3.5-flash gemini-3.6-flash gemini-3.7-flash gemini-3.8-flash"
GEMINI_LIMIT=20   # jobs per Gemini model per cycle (free tier is ~20 requests/day per model)
GROQ_MODELS="openai/gpt-oss-120b openai/gpt-oss-20b qwen/qwen3.8-27b"
GROQ_LIMIT=40     # jobs per Groq model per cycle (1,000 requests/day and 8,000 tokens/min per model)
# OpenRouter free models allow 50 requests/day (1,000/day once $10 of credits has been bought). One pass a day,
# kept under that limit so it never hits the daily cap and sits in a long rate-limit wait. Raise to ~900 with credits.
OPENROUTER_DAILY=45
OPENROUTER_MODEL=google/gemma-4-31b-it:free
OPENROUTER_FALLBACK=google/gemma-4-26b-a4b-it:free   # used for the rest of the day's jobs if the first model is rate-limited
OPENROUTER_RETRY_COST=5   # requests a rate-limited pass burns on retries before it stops (llm.py _MAX_RETRIES)
OPENROUTER_STAMP="$A/.openrouter-last-day"   # date of the last OpenRouter pass
ENRICH_MAX=600   # seconds of enrichment per cycle; the rest continues next cycle
last_discover=0
cycle=0

log() { echo "== $(date '+%Y-%m-%d %H:%M:%S') $*" >> "$A/logs/pipeline-$(date +%Y%m%d).log"; }

# score_with LABEL MODEL LIMIT [URL KEY]: one capped scoring pass; marks the model used up for today if it says so
score_with() {
  local label=$1 model=$2 limit=$3 url=$4 key=$5 out
  local done_file="$A/.exhausted-$(date +%Y-%m-%d)"
  grep -qxF "$model" "$done_file" 2>/dev/null && return
  log "score ($label $model, up to $limit)"
  out=$(mktemp)
  if [ -n "$url" ]; then
    APPLYPILOT_SCORE_LIMIT=$limit LLM_URL=$url LLM_API_KEY="$key" LLM_MODEL=$model nice -n 10 applypilot run score > "$out" 2>&1
  else
    APPLYPILOT_SCORE_LIMIT=$limit LLM_MODEL=$model nice -n 10 applypilot run score > "$out" 2>&1
  fi
  cat "$out" >> "$L"
  if grep -q "quota exhausted\|404 Not Found" "$out"; then echo "$model" >> "$done_file"; log "$model used up for today"; fi
  rm -f "$out"
}

while true; do
  L="$A/logs/pipeline-$(date +%Y%m%d).log"
  now=$(date +%s)
  if [ $((now - last_discover)) -ge 10800 ] && ! pgrep -f "applypilot run discover" >/dev/null; then
    log "starting discover"
    ( nice -n 10 applypilot run discover >> "$L" 2>&1; log "discover finished (exit $?)" ) &
    last_discover=$now
  fi

  python3 "$A/exclude_lead.py" >> "$L" 2>&1   # never score Lead roles
  # set aside jobs that can never be applied to (wrong title, no sponsorship, excluded industry, account-only site)
  [ -n "$REPO" ] && python3 "$REPO/auto/prefilter.py" >> "$L" 2>&1

  cycle=$((cycle + 1))
  for m in $GEMINI_MODELS; do score_with gemini "$m" "$GEMINI_LIMIT"; done
  # read each cycle, so adding the key to .env takes effect without restarting the pipeline
  OPENROUTER_KEY=$(grep '^OPENROUTER_API_KEY=' "$A/.env" | cut -d= -f2-)
  today=$(date +%Y-%m-%d)
  if [ -n "$OPENROUTER_KEY" ] && [ "$(cat "$OPENROUTER_STAMP" 2>/dev/null)" != "$today" ]; then
    echo "$today" > "$OPENROUTER_STAMP"
    log "score (openrouter $OPENROUTER_MODEL, up to $OPENROUTER_DAILY today)"
    T=$(mktemp)
    APPLYPILOT_SCORE_LIMIT=$OPENROUTER_DAILY LLM_URL=https://openrouter.ai/api/v1 LLM_API_KEY="$OPENROUTER_KEY" LLM_MODEL=$OPENROUTER_MODEL \
      nice -n 10 applypilot run score > "$T" 2>&1
    cat "$T" >> "$L"
    # rate-limited: give the rest of today's allowance (minus what the retries used) to the fallback model
    if grep -q "LLM quota exhausted" "$T"; then
      left=$((OPENROUTER_DAILY - $(grep -c '\] score=[0-9]' "$T") - OPENROUTER_RETRY_COST))
      if [ "$left" -gt 0 ]; then
        log "score (openrouter fallback $OPENROUTER_FALLBACK, up to $left)"
        APPLYPILOT_SCORE_LIMIT=$left LLM_URL=https://openrouter.ai/api/v1 LLM_API_KEY="$OPENROUTER_KEY" LLM_MODEL=$OPENROUTER_FALLBACK \
          nice -n 10 applypilot run score >> "$L" 2>&1
      fi
    fi
    rm -f "$T"
  fi
  if [ -n "$GROQ_KEY" ]; then
    for m in $GROQ_MODELS; do score_with groq "$m" "$GROQ_LIMIT" https://api.groq.com/openai/v1 "$GROQ_KEY"; done
  fi

  # every 10 newly ready 8+ jobs, queue an application batch for Claude and Codex (free; same as Apply now)
  [ -n "$REPO" ] && python3 "$REPO/auto/apply_trigger.py" >> "$L" 2>&1

  # enrich new listings after scoring, capped so it can't hold up the next scoring pass
  log "enrich (up to ${ENRICH_MAX}s)"
  nice -n 10 applypilot run enrich >> "$L" 2>&1 &
  enrich_pid=$!
  ( sleep "$ENRICH_MAX"; kill "$enrich_pid" 2>/dev/null && log "enrich stopped after ${ENRICH_MAX}s; continues next cycle" ) &
  watchdog=$!
  wait "$enrich_pid"
  kill "$watchdog" 2>/dev/null

  left=$(sqlite3 -readonly "$A/applypilot.db" "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL;" 2>/dev/null)
  log "cycle $cycle done; unscored with description: $left"
  sleep 60
done
