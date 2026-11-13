# Local patches for ApplyPilot (optional)

Modified copies of ApplyPilot files. Editing these copies changes nothing: install them over the live files to use them. They add US-only filtering, your title and pay filters during the scan, and model rotation.

| Copy | Live file |
|---|---|
| `pipeline.sh` | `~/.applypilot/pipeline.sh` (launchd job named by `launchd_label` in `config/settings.json`) |
| `scorer.py` | `~/.applypilot-venv/lib/python3.11/site-packages/applypilot/scoring/scorer.py` |
| `us_only.py` | `~/.applypilot-venv/lib/python3.11/site-packages/applypilot/us_only.py` (copy of `auto/us_only.py`; keep identical) |
| `jobspy.py`, `workday.py`, `smartextract.py` | `.../applypilot/discovery/` (each `_location_ok` now rejects non-US places first, remote or not) |
| `llm.py` | `~/.applypilot-venv/lib/python3.11/site-packages/applypilot/llm.py` (gives up on a model when it asks to wait over 90 s) |

The OpenRouter pass runs only when `OPENROUTER_API_KEY=` is set in `~/.applypilot/.env` (read every cycle, no restart needed) and at most once a day; `~/.applypilot/.openrouter-last-day` records the day it last ran.

Scoring rotates through several free Gemini and Groq models (`GEMINI_MODELS`, `GROQ_MODELS`); a model that reports its quota used up is listed in `~/.applypilot/.exhausted-YYYY-MM-DD` and skipped until the next day.

Your filters reach the patches through `~/.applypilot/settings.py` and `~/.applypilot/config/settings.json`, which `setup.sh` copies from this repository. Re-run `setup.sh` after changing `config/settings.json`. With `us_only` on, discovery, the scorer's pre-filter, `auto/prefilter.py` and `auto/build_queue.py` all use `us_only.non_us_location`.

Back up each live file before installing a patch over it.

After changing `pipeline.sh`, restart the job: `launchctl kickstart -k gui/$(id -u)/<your launchd_label>`
