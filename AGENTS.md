# Codex in the dual workspace

You are `codex`, one of two AI workers here. The other is `claude` (Claude Code).

**Before doing anything else, read `RULES.md` in this folder and follow it on every task.**
It explains the task cards, the shared log, and the red lines.

## Codex-specific notes

- Your name in every `ws.py` command is `--who codex`.
- When the owner says "check tasks", run `python3 ws.py next --who codex` and do the card it gives you.
- When the owner asks for something directly in chat, do it and log the key steps with
  `python3 ws.py log --who codex --msg "..."` so Claude can see what happened.
