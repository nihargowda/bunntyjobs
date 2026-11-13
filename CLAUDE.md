# Instructions for Claude in this repository

This is a job-search automation kit. It only works once the person using it has filled in their own details in `ApplyPilot setup/config/`. The code holds no personal values.

## Before running anything, check the setup

At the start of every session, and before running any script, scan, dashboard or application, run:

```bash
python3 "ApplyPilot setup/check_config.py"
```

- If it reports problems, do not run anything else. Tell the person setup isn't finished, and run the setup interview below (the same steps as the `/setup` command).
- If it reports the config is complete, carry on with what they asked.

## Setup interview

Follow `.claude/commands/setup.md`. In short:

1. Ask for the details a group at a time, in plain questions: base resume, contact, every role they want, every location, work authorization, experience, pay and availability, the common application questions, EEO answers, and daily target. Never guess or invent a value; if they don't know, leave it for later.
2. Read their resume to propose facts and highlights, then confirm each one with them before saving. Only use what the resume and the person confirm.
3. Ask whether they want one generic resume or a tailored version per role family. For tailored versions, rewrite the base resume against real job descriptions for each role, check it with `resume_tools/check_facts.py` (nothing added that isn't in the base), make the PDF with `resume_tools/to_pdf.py`, get their approval, and map each version to its titles in `config/settings.json`.
4. Write `config/settings.json`, `config/profile.json`, `config/candidate_facts.json` and `config/searches.yaml`, starting from the matching `*.example.*` template so the structure stays the same.
5. Ask them to add their API keys to `config/env.txt` themselves (copy `env.example.txt`), so keys never appear in the chat.
6. Run `check_config.py` again until it passes, then show a short summary of what was saved.
7. Ask before running `bash "ApplyPilot setup/setup.sh"`: it installs software and copies their settings into `~/.applypilot`.

## Rules

- Never commit, print in full, or upload the files in `config/` (except the `*.example.*` templates), `resumes/` or `Applications/`. They are gitignored; keep them that way.
- Applications go out only with facts from `config/profile.json` and `config/candidate_facts.json`. Follow `ApplyPilot setup/auto/apply_task_prompt.md` when applying.
- Never create accounts, enter passwords, or solve CAPTCHAs on job sites. Leave those for the person.
- If the person changes a detail later, update the config file and re-run `check_config.py` and `setup.sh`.
