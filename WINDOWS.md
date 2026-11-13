# Running this kit on Windows (WSL)

The kit's scripts are written for macOS/Linux, so on Windows they run inside **WSL** (the Linux that's built into Windows).
Your config files in `ApplyPilot setup/config/` are already filled in. Only three things are left for you: your resume, an AI key,
and turning on `answers_confirmed` once you've reviewed the answers.

## 1. Install WSL (once)

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

Restart when asked. Ubuntu opens and asks you to create a Linux username and password (this is only for WSL).

## 2. Install the basics inside Ubuntu (once)

In the Ubuntu window:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git unzip poppler-utils
```

## 3. Unzip the kit into your Linux home folder

Your Windows Downloads folder is visible from Ubuntu under `/mnt/c/Users/<your Windows user name>/Downloads`.

```bash
cd ~
unzip "/mnt/c/Users/<your Windows user name>/Downloads/job-search-automation.zip"
cd ~/job-search-automation/"ApplyPilot setup"
```

Keep the kit in your Linux home (`~`), not under `/mnt/c`; it runs much faster there.

## 4. Your resume (already added)

Your resume is already in `resumes/resume.pdf` and `resumes/resume.txt`. To swap in a newer version later:

### Replacing it

Put your PDF at `resumes/resume.pdf` and make the plain-text copy the AI scorer reads:

```bash
cp "/mnt/c/Users/<your Windows user name>/Downloads/<your resume>.pdf" resumes/resume.pdf
pdftotext -layout resumes/resume.pdf resumes/resume.txt
```

## 5. Add a free AI key

Get a free key from Gemini (https://aistudio.google.com/apikey) or Groq (https://console.groq.com/keys), then:

```bash
nano config/env.txt
```

Paste it after `GEMINI_API_KEY=` (or `GROQ_API_KEY=`), then press Ctrl+O, Enter, Ctrl+X to save and exit.

## 6. Check, then install

```bash
python3 check_config.py
bash setup.sh
```

`check_config.py` must say the config is complete before `setup.sh` will do anything useful.

## 7. Find and score jobs

```bash
bash scan.sh all          # US, then Canada, UK, Ireland, Netherlands, Germany
bash scan.sh us           # US only
bash scan.sh canada uk    # just the regions you name
```

A first scan can take hours because job boards slow down repeated requests. Each region scans its own country's boards.

## 8. Build the apply queue and open the dashboard

```bash
python3 auto/build_queue.py
python3 dashboard.py
```

Open http://127.0.0.1:8765 in your normal Windows browser.

## 9. Apply

Applications are filled in Chrome by Claude, following `auto/apply_task_prompt.md`, using only the facts in your config.
Claude never enters passwords, creates accounts or solves CAPTCHAs; those are left as "WAITING - needs you".

## Optional: scan automatically on a schedule

Windows has no `launchd`, so use **Task Scheduler** → Create Basic Task → Action "Start a program":

- Program: `wsl.exe`
- Arguments: `-e bash -lc "cd ~/job-search-automation/'ApplyPilot setup' && bash scan.sh all"`

## H-1B sponsor history (recommended, since you need sponsorship)

This ranks US jobs by each employer's real H-1B filing history (Department of Labor LCA and PERM records, FY2020 to now)
and skips US employers with no filings at all (`h1b_skip_tiers` in `config/settings.json`). The data is a free ~8 MB
download, and lookups run on your PC; nothing about which companies you check is sent anywhere.

Inside Ubuntu, once:

```bash
sudo apt install -y nodejs npm
node -v                       # needs v22.13 or newer
rmdir ~/job-search-automation/career-ops
git clone --depth 1 https://github.com/career-ops-hq/career-ops.git ~/job-search-automation/career-ops
cd ~/job-search-automation/career-ops
node plugins/h1b-sponsor/install-h1b-index.mjs
node plugins/h1b-sponsor/check.mjs "Microsoft" --summary     # quick test
```

If `node -v` shows an older version than v22.13, install a current one first:
`curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs`

The data is refreshed every quarter. To update: `node plugins/h1b-sponsor/install-h1b-index.mjs --force`

`auto/build_queue.py` then shows each job's tier (`h1b:strong`, `moderate`, `staffing-shop`, `weak`, `none`,
`unknown`), puts strong sponsors first within the same fit score, and drops US jobs whose employer is `none`.
`unknown` means the company name couldn't be matched, so those jobs are kept. Canada and Europe jobs are never dropped
by this (the data is US-only).

## Changes made for this setup

- `ApplyPilot setup/scan.sh` (new): runs scans one region at a time.
- `auto/build_queue.py`: the USD pay floor applies to US jobs only.
- `tracker/recruiters.py`, `tracker/mail_status.py`: macOS-only notifications are skipped elsewhere instead of crashing.
- `auto/build_queue.py`: new `preferred_share` setting (0.75) mixes the queue so about 3 of every 4 jobs are
  Azure / Cloud / Systems / Entra / IAM titles and the rest are other security roles.
- `auto/build_queue.py`, `auto/prefilter.py`, `settings.py`: new `description_exclude` setting, checked against the whole
  job description. Yours skips any posting that mentions DevSecOps, AppSec / application security, product security,
  SAST/DAST, secure code review or secure SDLC.
- `auto/build_queue.py`, `settings.py`: new `h1b_skip_tiers` setting (yours: `["none"]`) skips US employers with no H-1B filings.
- Leave the optional `local-patches/` uninstalled: their location filter drops all non-US jobs, which would block your Canada and Europe searches.
