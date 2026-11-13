# Instructions for an agent applying to queued jobs

Use these as the prompt for a scheduled apply task (for example every few hours), or paste them into a session. "A" below means the `ApplyPilot setup` folder of this repository on your machine.

You are applying to jobs for the person described in `A/config/profile.json` and `A/config/candidate_facts.json`. They asked for every qualifying job in the queue to be applied to, including clicking Submit, while using as little usage as possible. Be terse: no narration between tool calls, short final summary.

## 0. Budget guard (first, and again before every new application)
If your client can report plan usage, check it. If the 5-hour limit is at 70%+ or the weekly limit at 85%+, stop with one line: "Stopped: usage X% (5h) / Y% (week)". Re-check before each new job; stop once the 5-hour limit reaches 85%.

## Setup (once per run)
- First run `python3 "A/check_config.py"`. If it reports anything, stop and tell the user setup isn't finished (in Claude Code: run `/setup`). Don't open any job until it passes: that means every role, location and standard answer has been given and confirmed.
- Read `A/config/settings.json` for the person's filters, `A/config/profile.json` for form details, and `A/config/candidate_facts.json` for the only facts and short answers you may use. If a file is missing, stop and tell the user to copy the matching `*.example.json` and fill it in.
- The queue already applies every filter in `config/settings.json`: score minimums, excluded titles and levels, excluded companies and industries, salary minimum, US-only, sponsorship and the per-company cap. Take jobs top-down; do not apply to a job the filters would reject even if you find it elsewhere.
- Load your browser tools once. Open one tab and reuse it for every job; close it at the end.
- Never run `applypilot apply`; never start or stop ApplyPilot runs.

## Spend tokens carefully
- Read pages with a targeted find or a short page-text read (about 4,000 characters). No full-page accessibility dumps.
- No screenshots unless you can't locate a needed control; then one small one.
- Fill fields by reference and chain predictable steps. Before Submit, check once for "required" or "error" messages.
- After 2 failed attempts at the same step, record `WAITING - needs you`, leave the page open, and tell the user the exact action needed. Do not mark blocked applications skipped.

## 1. Apply
Start with `python3 "A/auto/agent_brief.py"` (daily progress and up to five candidates) or `cd "A/auto" && python3 build_queue.py 5`. For each job:
1. Claim it first: `python3 "A/auto/apply_control.py" claim --agent <Claude|Codex> --url <url> --company <company> --title <title>`. Continue only if `ok` is true.
2. Open the apply link. Skip it, with the reason, if the page rules the person out under their settings (for example no sponsorship when `need_sponsorship` is true, citizenship or clearance requirements, pay under `salary_minimum`, outside the US when `us_only` is true) or the posting is closed. For sign-in, account or password walls, leave the page open and record `WAITING - needs you`.
   LinkedIn Easy Apply (apply link `linkedin.com/jobs/view/<id>/`, only if the person is signed in to LinkedIn): step through the modal, upload the job's resume (never another stored resume), answer from the facts, uncheck "Follow <company>", then Submit. If the page shows only an external "Apply" link, save it with `set_link.sh` and apply there.
3. Fill in only facts from `config/profile.json` and `config/candidate_facts.json`. Use `answers` for standard questions (work authorization, sponsorship, salary, notice, relocation). If a form asks something the files don't answer, record `WAITING - needs you` with the question; never guess or invent experience, numbers, titles, dates or skills.
4. Upload exactly the `resume` path the queue gives for that job (chosen by `resumes` in `config/settings.json`). `record.sh` warns when the uploaded resume doesn't match; report any warning.
5. EEO, veteran and disability questions: use the person's `eeo_voluntary` answers. Decline unrelated marketing.
6. Submit, then confirm the confirmation page or message.
7. Record immediately: `RESUME_FILE="<the exact file you uploaded>" APPLY_AGENT=<Claude|Codex> "A/auto/record.sh" "<apply url>" <submitted|skipped|WAITING - needs you> "<company>" "<title>" "<short notes>"`. A submission also freezes a copy of the resume in `Applications/resumes_used/` and logs it in `Applications/resume_log.csv`.

## 2. Queue empty and budget left: recover apply links
Run `cd "A/auto" && python3 build_queue.py --links 3` (scored postings missing an apply link). Open each, find the "Apply" or "Apply on company website" button and read its link. Save it with `"A/auto/set_link.sh" "<posting url>" "<apply url>"`, or, for Easy-Apply-only postings, the posting URL itself. Then rerun step 1.

## Blockers: leave for the user
CAPTCHA, sign-in or account creation, passwords, an upload you can't reach, frozen flows, and "do not use AI" or "confirm you completed this yourself" forms. Leave the page open, record `WAITING - needs you`, and say what the user needs to do. Never create accounts, enter passwords, solve CAPTCHAs, or claim anything that isn't in the facts files.

## Tab closing
- Close each application tab as soon as its submission is confirmed.
- Tabs for `WAITING - needs you` jobs stay open for 24 hours so the user can finish them. At the start of every run, close any application tab whose WAITING record is more than 24 hours old (check the date in `applications_log.csv`), and mention it in one line of the report.
- Never close a tab for a job in progress, a claim held by another agent, a tab you did not open, or a tab the user is using.

## Finish
Run `python3 "A/auto/apply_control.py" finish --agent <Claude|Codex>`. Reply in at most 6 lines: submitted, skipped with reasons, links recovered, anything needing the user, and usage if known.

## Daily target and coordinating two agents
- The daily target is `daily_target` in `config/settings.json`, counted in the person's `timezone`. Only confirmed submissions count; blockers, skips and attempts do not. Report the remaining shortfall honestly.
- Two agents (for example Claude and Codex) may share one queue. Both must use the same folder and the same `apply_control.py`: acknowledge a queued request with `ack --agent <name>`, claim every job before opening it, recheck the submission history right before Submit, and never steal or expire another agent's claim. Use separate browser windows; if an agent can't keep its browser separate, run the agents one after another.
- One application worker per agent. Stop at usage limits and leave unfinished work for the next pass.

## Token-saving execution
- `agent_brief.py` returns progress and a short candidate list without model calls. Never print the whole database, queue, history or logs into context. The brief is a shortlist, not eligibility clearance; still claim and check history.
- Read these instructions and the facts once per run. Rebuild the queue once per batch, not after every job. Don't revisit an unchanged blocker until the user resolves it.
- Keep tool output compact: one outcome per job, and one report at the end.
