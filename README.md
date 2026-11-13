# Job search automation (ApplyPilot add-ons)

Scripts around [ApplyPilot](https://pypi.org/project/applypilot/) that find jobs on the big boards, score them against your resume with free AI models, filter them by your own rules, and keep a clean queue that a browser agent (Claude or Codex) can apply from. A tracker reads application emails, and a local dashboard shows the pipeline.

**New here? Start with [INSTRUCTIONS.md](INSTRUCTIONS.md)** for a step-by-step guide.

Nothing personal is in the code. Your details and filters live in `ApplyPilot setup/config/`, and your filled-in copies are gitignored.

## What you need

- macOS with Python 3.11+ (setup installs it through Homebrew if missing). The scan's background job uses `launchd`.
- At least one free AI key: Gemini or Groq (OpenRouter optional).
- Your resume as a PDF plus a plain-text copy.
- Optional: Node.js and the `career-ops` submodule, for H-1B sponsor rankings when you need sponsorship (`git submodule update --init`).

## Set up

**With Claude Code:** clone the repository, open Claude Code in it, and run `/setup` (or just ask it to run anything: it checks your setup first). Claude asks for your details, every role and location you want, and the answers application forms usually need; reads your resume; offers to write a tailored resume for each type of role, checked against your original so nothing is invented; fills in `config/` for you; and asks before installing anything.

**By hand:**

1. Copy each template in `ApplyPilot setup/config/` and fill it in:

   | Copy | To | What goes in it |
   |---|---|---|
   | `settings.example.json` | `settings.json` | Your filters: time zone, score minimums, titles and levels to skip, salary floor, excluded companies, preferred regions, US-only, sponsorship, daily target, which resume to send for which titles |
   | `profile.example.json` | `profile.json` | Contact details, work authorization, availability, pay expectations and EEO answers used on forms |
   | `candidate_facts.example.json` | `candidate_facts.json` | The only facts and short answers the agents may use; keep them true and matching your resume |
   | `searches.example.yaml` | `searches.yaml` | Job-board searches and locations |
   | `env.example.txt` | `env.txt` | Your API keys |

   ```bash
   cd "ApplyPilot setup/config"
   for f in *.example.*; do cp -n "$f" "${f/.example/}"; done
   ```

2. Put your resumes in `ApplyPilot setup/resumes/` (gitignored). The tools in `ApplyPilot setup/resume_tools/` help tailor them: `sample_jds.py` pulls real job descriptions for a role from your scans, `check_facts.py` confirms a tailored version adds nothing that isn't in your base resume, and `to_pdf.py` turns the text into a one-page PDF. The default is `resumes/resume.pdf` with a plain-text `resumes/resume.txt` next to it. For role-specific versions, list them under `resumes.variants` in `settings.json`, each with a title pattern; the first match is sent.

3. Check your config with `python3 "ApplyPilot setup/check_config.py"`; it lists anything missing or still an example value. Then run setup. It checks your config, installs ApplyPilot into `~/.applypilot-venv`, and copies your profile, searches, resume, keys and settings to `~/.applypilot`:

   ```bash
   bash "ApplyPilot setup/setup.sh"
   ```

   Re-run it whenever you change anything in `config/`.

## Use it

- Find and score jobs: `source ~/.applypilot-venv/bin/activate && applypilot run --validation lenient`
- Build the apply queue with your rules: `python3 "ApplyPilot setup/auto/build_queue.py"` (prints the top jobs and writes `auto/queue.json`; each job carries the resume to send)
- Dashboard: `python3 "ApplyPilot setup/dashboard.py"`
- Apply: give a browser agent `ApplyPilot setup/auto/apply_task_prompt.md` as its instructions. It claims each job, fills forms only from your config, uploads the assigned resume, records every outcome with `auto/record.sh`, and leaves anything that needs you (sign-ins, CAPTCHAs, unanswered questions) as `WAITING - needs you`.
- Two agents at once: see `ApplyPilot setup/auto/APPLICATION_AGENT_COORDINATION.md`.
- Continuous scanning: install the optional patches in `ApplyPilot setup/local-patches/` (see its README) and run `pipeline.sh` as a launchd job.

## Privacy

- Only the `*.example.*` templates in `config/` are committed. Your `settings.json`, `profile.json`, `candidate_facts.json`, `searches.yaml` and `env.txt` are gitignored, as are `resumes/`, `Applications/` (your application history) and the queue and state files the scripts write.
- Everything runs on your machine. Job pages are fetched to check they're still open; your details only go to the job sites you apply to.

## Tests

```bash
cd "ApplyPilot setup/tracker" && python3 -m unittest discover
cd "../auto" && python3 -m unittest test_resume_selection test_reliability
```

## Notes

- Fit scores come from AI models and can be wrong. Review what the agents send, especially early on.
- Submitting applications automatically is your responsibility: check each job site's terms, and keep every answer in your facts files true.
