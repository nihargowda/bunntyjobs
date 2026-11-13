#!/bin/bash
# Run job scans one region at a time, because each scan searches a single country's job boards.
#
#   bash scan.sh            -> US only (config/searches.yaml)
#   bash scan.sh canada uk  -> the regions you name (config/searches_<region>.yaml)
#   bash scan.sh all        -> US, then every config/searches_*.yaml
#
# Run setup.sh once first. Works on macOS, Linux and Windows (inside WSL).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
CFG="$HERE/config"
[ -f "$HOME/.applypilot-venv/bin/activate" ] || { echo "Run setup.sh first."; exit 1; }
source "$HOME/.applypilot-venv/bin/activate"

regions=("$@")
[ ${#regions[@]} -eq 0 ] && regions=(us)
if [ "${regions[0]}" = "all" ]; then
  regions=(us)
  for f in "$CFG"/searches_*.yaml; do [ -f "$f" ] && regions+=("$(basename "$f" .yaml | sed 's/^searches_//')"); done
fi

for r in "${regions[@]}"; do
  f="$CFG/searches_$r.yaml"; [ "$r" = "us" ] && f="$CFG/searches.yaml"
  [ -f "$f" ] || { echo "No search file for '$r' ($f). Skipping."; continue; }
  cp "$f" "$HOME/.applypilot/searches.yaml"
  echo ""; echo "=== Scanning: $r ($(date '+%H:%M')) ==="
  applypilot run --validation lenient || echo "!! The $r scan stopped with an error; moving on to the next region."
done

# leave the US searches in place as the default
cp "$CFG/searches.yaml" "$HOME/.applypilot/searches.yaml"
echo ""; echo "Scans done. Build your apply queue with:  python3 \"$HERE/auto/build_queue.py\""
