# Running two application agents on one queue

Two agents (for example Claude and Codex) can apply in parallel only if they never work on the same job. Separate browser windows alone do not prevent duplicate submissions; the shared claim store does.

## Shared records

Both agents must use the same `ApplyPilot setup` folder, the live database at `~/.applypilot/applypilot.db`, and the history in `Applications/applications_log.csv`. Never write application state to a second copy of the repository.

## Required before opening or filling each application

1. Check the database and the full log by normalized application URL, company and title, and requisition ID when available. Any prior submitted record rules the job out, even when a later pending entry exists.
2. Claim the job atomically: `python3 auto/apply_control.py claim --agent <name> --url <url> --company <company> --title <title>`. Continue only if `ok` is true.
3. Recheck the history after claiming and immediately before Submit. Do not retry a pending Submit until its outcome has been verified.
4. Use separate agent-owned browser tabs or windows. Never edit or close the other agent's tabs.
5. Record confirmed submissions immediately in the shared log and database. Record blockers as `WAITING - needs you` with the exact reason; pending attempts are not submissions. Keep the claim for unresolved attempts.
6. Record which resume you uploaded: run `auto/record.sh` with `RESUME_FILE=<exact file uploaded>` and `APPLY_AGENT=<name>`.

## Daily target and requests

The daily target is `daily_target` in `config/settings.json`, counted in your `timezone`, combined across agents. Only confirmed submissions count.

At each run, read `python3 auto/apply_control.py status`. The dashboard's Apply now button queues a request for both agents; it does not open either agent. When an agent picks up a queued request it runs `ack --agent <name>`, and when it finishes its batch it runs `finish --agent <name>`.

One application worker per agent; no duplicate workers or parallel scoring loops. Stop at account usage limits and leave unfinished requests for the next pass.

To avoid two agents driving the browser at once, schedule their runs at different times (for example agent A at 7, 11, 15 and 19 o'clock, agent B at 9, 13 and 17).
