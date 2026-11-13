#!/bin/sh
# Save the company's own apply link for a job that only had a LinkedIn/Indeed posting.
# Usage: set_link.sh "<posting url from build_queue.py --links>" "<apply url>"      (use "none" if the posting has no outside apply link)
DB="$HOME/.applypilot/applypilot.db"; HERE="$(cd "$(dirname "$0")" && pwd)"
[ -n "$1" ] && [ -n "$2" ] || { echo "usage: set_link.sh <posting url> <apply url|none>"; exit 1; }
if [ "$2" = "none" ]; then echo "$1" | sed 's/[?#].*//; s:/$::' >> "$HERE/tried.txt"; echo "marked: no outside apply link"; exit 0; fi
P=$(printf %s "$1" | sed "s/'/''/g"); A=$(printf %s "$2" | sed "s/'/''/g")
sqlite3 "$DB" "UPDATE jobs SET application_url='$A' WHERE url='$P';" && echo "saved apply link"
