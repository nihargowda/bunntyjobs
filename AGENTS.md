# Instructions for coding agents (Codex and others)

Before running any script, scan, dashboard or application, run `python3 "ApplyPilot setup/check_config.py"`.
If it reports problems, don't run anything else: ask the person for the missing details and fill in `ApplyPilot setup/config/`
by following `.claude/commands/setup.md`. Never invent a value, never put API keys in the chat, and ask before running `setup.sh`.

Never commit or share the person's files in `config/` (other than the `*.example.*` templates), `resumes/` or `Applications/`.
When applying, follow `ApplyPilot setup/auto/apply_task_prompt.md` and `ApplyPilot setup/auto/APPLICATION_AGENT_COORDINATION.md`.
