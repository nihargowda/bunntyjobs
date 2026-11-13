#!/bin/bash
# ApplyPilot job-search setup. Fill in config/ first (see README.md), then run from this folder:  bash setup.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
CFG="$HERE/config"

# 0. Your details must be filled in first
missing=0
for f in settings.json profile.json candidate_facts.json searches.yaml env.txt; do
  if [ ! -f "$CFG/$f" ]; then
    example="${f%.*}.example.${f##*.}"
    echo "Missing config/$f  ->  cp \"config/$example\" \"config/$f\"  and fill it in."
    missing=1
  fi
done
[ "$missing" = 1 ] && exit 1
RESUME=$(cd "$HERE" && python3 -c 'from settings import SETTINGS; print(SETTINGS["resumes"]["default"])')
[ -f "$HERE/$RESUME" ] || { echo "Default resume not found: $RESUME (set resumes.default in config/settings.json)"; exit 1; }
RESUME_TXT="${RESUME%.*}.txt"
[ -f "$HERE/$RESUME_TXT" ] || { echo "Add a plain-text copy of your resume next to it: $RESUME_TXT (the AI scorer reads it)"; exit 1; }

# 1. Python 3.11+ (installs via Homebrew if missing)
PY=""
for v in python3.13 python3.12 python3.11 python3.14; do command -v $v >/dev/null && PY=$v && break; done
# any other python3 that is 3.11 or newer (e.g. newer Linux / WSL releases)
if [ -z "$PY" ] && command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY=python3; fi
if [ -z "$PY" ]; then
  [ "$(uname)" = "Darwin" ] || { echo "Install Python 3.11 or newer first (Ubuntu/WSL: sudo apt install -y python3 python3-venv)."; exit 1; }
  command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh  then re-run."; exit 1; }
  brew install python@3.12; PY=python3.12
fi
echo "Using $($PY --version)"

# 2. Install ApplyPilot into its own environment
$PY -m venv "$HOME/.applypilot-venv"
source "$HOME/.applypilot-venv/bin/activate"
pip install -q --upgrade pip
pip install -q applypilot
pip install -q --no-deps python-jobspy
pip install -q pydantic tls-client requests markdownify regex curl_cffi

# 3. Copy your profile, searches, resume, keys and filters to where ApplyPilot and the local patches read them
mkdir -p "$HOME/.applypilot/config"
cp "$CFG/profile.json" "$CFG/searches.yaml" "$HOME/.applypilot/"
cp "$HERE/$RESUME" "$HOME/.applypilot/resume.pdf"
cp "$HERE/$RESUME_TXT" "$HOME/.applypilot/resume.txt"
cp "$CFG/env.txt" "$HOME/.applypilot/.env"
cp "$HERE/settings.py" "$HOME/.applypilot/settings.py"
cp "$CFG/settings.json" "$HOME/.applypilot/config/settings.json"
echo "$HERE" > "$HOME/.applypilot/repo_path"  # lets the background pipeline find this folder
grep -qE "^(GEMINI|GROQ|OPENROUTER)_API_KEY=.+" "$HOME/.applypilot/.env" || echo "!! No API key in config/env.txt yet - add one, then re-run this script."

# 4. Check setup
applypilot doctor || true
echo ""
echo "Done. Re-run this script whenever you change config/ (it copies your settings again)."
echo "To find and score jobs:  source ~/.applypilot-venv/bin/activate && applypilot run --validation lenient"
echo "Then build the apply queue:  python3 \"$HERE/auto/build_queue.py\""
echo "Optional: install the local patches (see local-patches/README.md) to apply your filters during the scan."
