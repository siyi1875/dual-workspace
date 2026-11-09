# Claude Code in the dual workspace

You are `claude`, one of two AI workers here. The other is `codex` (ChatGPT).
The shared rules below apply to you. Follow them on every task.

@RULES.md

## Claude-specific notes

- Your name in every `ws.py` command is `--who claude`.
- When the owner says "check tasks", run `python3 ws.py next --who claude` and do the card it gives you.
- When the owner asks for something directly in chat, do it and log the key steps with
  `python3 ws.py log --who claude --msg "..."` so Codex can see what happened.
