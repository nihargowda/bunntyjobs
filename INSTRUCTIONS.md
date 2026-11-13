# How to use this job-search kit

A step-by-step guide. You don't need to code: Claude Code does the setup with you, asks for your details, and asks before installing anything.

## What it does

1. **Finds jobs** on Indeed, LinkedIn and Glassdoor for the roles and locations you choose.
2. **Scores each job** against your resume with free AI models.
3. **Filters them** by your rules: pay, seniority, companies to avoid, location, visa sponsorship.
4. **Keeps a queue** of the best matches, each with the resume to send.
5. **Applies for you** in your Chrome browser when you ask Claude to, using only the facts you gave it. Anything that needs you (a sign-in, a CAPTCHA, a question it can't answer) is left for you.
6. **Tracks results** from your Gmail and shows everything on a local dashboard.

## What you need

- A Mac. The background scanner uses macOS; most other parts also work on Linux.
- [Claude Code](https://claude.com/claude-code), signed in.
- Google Chrome, with the Claude in Chrome extension if you want Claude to fill in applications.
- Your resume as a PDF or Word file.
- A free AI key for scoring: [Gemini](https://aistudio.google.com/apikey) or [Groq](https://console.groq.com/keys). OpenRouter is optional.
- About 20 minutes for setup.

## Step 1: Get the kit

Open Terminal and run:

```bash
git clone https://github.com/aravindramesh2806/job-search-automation.git
cd job-search-automation
claude
```

## Step 2: Set it up with Claude

In Claude Code, type:

```
/setup
```

Claude will:

1. **Ask for your resume** and read it.
2. **Ask for your details** a few questions at a time: contact, work authorization, experience, pay, start date, relocation, and the questions application forms usually ask.
3. **Ask which roles and locations** you want, which titles, levels and companies to skip, and your salary minimum.
4. **Read everything back** for you to confirm. Nothing is guessed.
5. **Ask whether you want one resume or tailored versions.** If tailored, it rewrites your resume for each type of role against real job descriptions, checks that nothing was added that isn't in your original, makes a one-page PDF, and shows it to you for approval.
6. **Ask you to add your AI key** to `ApplyPilot setup/config/env.txt` yourself, so the key never appears in the chat. Copy `config/env.example.txt` to `config/env.txt` and paste it after `GEMINI_API_KEY=` or `GROQ_API_KEY=`.
7. **Ask before installing** the job scanner (it goes into `~/.applypilot-venv`).

If you ask Claude to run anything before setup is finished, it stops and starts this interview instead.

## Step 3: Find and score jobs

Ask Claude: "Run a job scan." Or run it yourself:

```bash
source ~/.applypilot-venv/bin/activate
applypilot run --validation lenient
```

A first scan can take a while, sometimes hours, because LinkedIn and others slow down repeated requests. Fewer searches in `config/searches.yaml` make it faster.

## Step 4: See your matches

```bash
python3 "ApplyPilot setup/auto/build_queue.py"
```

This prints your top jobs and saves the queue. To see everything in the browser:

```bash
python3 "ApplyPilot setup/dashboard.py"
```

Then open http://127.0.0.1:8765.

## Step 5: Apply

Open Chrome (signed in to LinkedIn if you want Easy Apply), then ask Claude:

> Apply to the top 5 jobs in the queue. Follow "ApplyPilot setup/auto/apply_task_prompt.md".

Claude claims each job, fills the form only from your details, uploads the right resume, submits, and logs the result. It stops at your usage limit. Jobs it can't finish are marked "WAITING - needs you" with the reason, so you can complete them yourself.

**Run it on a schedule (optional).** In the Claude desktop app, create a scheduled task that uses the same instruction, for example every few hours during the day. Keep the app and Chrome open while it runs.

## Step 6: Check your progress

- **Dashboard:** applications, waiting items, replies and the pipeline.
- **History:** every outcome is in `ApplyPilot setup/Applications/applications_log.csv`, and the exact resume sent with each application is saved in `Applications/resumes_used/`.
- **Email tracking (optional):** with Gmail connected to Claude, ask "check my job emails". It updates the status of each application and drafts replies to recruiters for you to send.

## Change something later

Tell Claude what changed, for example "add Seattle to my locations" or "my salary minimum is now $95,000". It updates the config, checks it, and re-runs setup. Or edit the files in `ApplyPilot setup/config/` and run `bash "ApplyPilot setup/setup.sh"` again.

## If something goes wrong

| Problem | What to do |
|---|---|
| "Setup is not finished" | Run `/setup`, or `python3 "ApplyPilot setup/check_config.py"` to see what's missing |
| The scan finds nothing | Check `config/searches.yaml` has your titles and locations, then re-run `setup.sh` |
| The scan is very slow | Normal for LinkedIn. Use fewer searches, or remove `linkedin` from `sites` |
| Jobs aren't being scored | Check your key in `config/env.txt`, re-run `setup.sh`, then `applypilot doctor` |
| An application says "WAITING - needs you" | Open the job link, finish the step it names (often a sign-in or a question), and submit it yourself |
| The dashboard won't open | Make sure `dashboard.py` is running and use http://127.0.0.1:8765 |

## Your privacy

- Your details, resumes and application history stay on your computer. The files in `ApplyPilot setup/config/` (other than the `.example` templates), `resumes/` and `Applications/` are never added to git.
- Your details only go to the job sites you apply to, and job descriptions go to the AI scoring service you chose.
- Don't share your `config/env.txt`; it holds your API keys.

## Good to know

- AI scores can be wrong. Look over what goes out, especially in the first few days.
- You're responsible for what's submitted. Keep every answer in your facts true, and follow each job site's terms.
- Claude never creates accounts, enters passwords or solves CAPTCHAs for you.
