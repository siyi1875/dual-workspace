# Dual Workspace: Claude Code + ChatGPT Codex

One folder where you assign tasks to Claude, to Codex, or to whichever is free.
They pick the tasks up, keep one shared log, and hand work to each other. Both run on
your subscriptions (Claude login and ChatGPT login). No API keys.

## What you need

- macOS (the Finder buttons and the folder picker use macOS; the rest is plain Python)
- Python 3.9 or newer, nothing to install from pip
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code), signed in with your Claude subscription
- The Codex CLI, signed in with your ChatGPT account (the ChatGPT desktop app includes it)
- Optional: the [GitHub CLI](https://cli.github.com) (`gh`), for GitHub-repo tasks and pull requests

## Start it

```bash
git clone https://github.com/<you>/dual-workspace.git
cd dual-workspace
python3 ws.py serve
```

The `tasks/` and `shared/` folders are created on first run. They hold your cards, logs and deliverables, and
`.gitignore` keeps them out of git, so your own projects never end up in the repo.

Open http://127.0.0.1:8766, type a task, and click **Assign** (or press ⌘↵). The small buttons under the task
box set **who** works on it (Claude, Codex, Either, or Both discuss), **where**, and an optional **project**.

- **Where** (Workspace by default): where the work happens. Pick it from the second button under the task box.
  - **Workspace**: files go in `shared/` here, as before.
  - **GitHub repo** (`owner/name` or a github.com link): the workspace makes a fresh copy in
    `shared/<task>/<repo>` on a new `ws/...` branch before the AI starts. The AI commits there, and when it's done
    you get "Make a pull request?". Use this for projects you want to build straight on GitHub.
  - **Folder**: the AI works directly in a project already on your Mac. Picking **Folder** opens the Mac's
    "Choose folder" window (or press **Browse…**), so you don't have to type the path. That card is the
    AI's permission to change files there, and only there.
  - Every card with a place to work has **Open in Finder** in its **Where** section. Workspace cards have
    **Open folder in Finder** under their files.
- **Auto-start** (the switch next to Assign, on by default): the dashboard runs the agent's CLI headlessly
  (`claude -p` / `codex exec`). Each AI works on one task at a time, so Claude and Codex can work in parallel.
- **Needs you**: an AI asked a question, or a run failed (for example, out of quota). Reply
  to send it back to the same AI, or **Hand to** the other one. The new AI gets the card
  history, the log and the previous attempt's output, so it continues instead of starting over.
- **Completed** (right column): click a task to see its result and files. Type in **Follow up on this task**
  and send it: the same card reopens with your request, so earlier results and the new round stay together.
  For a discussion, both AIs pick it up again, starting with whoever you choose.
- **Pull requests**: when a task changed code in a git repo, the card comes to **Needs you** with
  "Make a pull request?". **Yes, make the PR & push** sends it to Claude (set by `pr_agent` in `workspace.json`)
  to push a branch and open the PR, even if Codex wrote the code. **No PR, mark done** closes it. Nothing is pushed until you say yes.
- **Day / Night**: the sun/moon button in the top-right corner. Until you press it, the page follows your Mac's appearance.
- **Hide finished tasks** (for demos and recordings): hover a completed task and press the eye, or press **Hide all**. Hidden tasks also drop out of the activity log, and while anything is hidden, general notes are held back too. Nothing is deleted: **Show** at the bottom of Completed brings them back, and a follow-up un-hides that task.

### Let them discuss until they agree

Pick **Both discuss** from the Who button. A third button appears where you choose who **goes first** and set a turn limit (8 by default).

1. The first AI does the work and writes a full proposal.
2. The other AI reviews it on its merits (it can check facts, run code, look things up), replies point by point, and either
   **agrees** to it exactly as written or sends a **revised** full proposal.
3. They keep taking turns. The discussion ends only when one accepts the other's latest proposal word for word,
   so the result is something both signed off on.
4. The agreed result goes on the card and into `shared/discussions/<id>.md`, with the whole transcript.

**View discussion** on the card shows the conversation as it happens. You can **Add note** at any time and the next
AI to speak will see it. If they hit the turn limit, or one asks you something, the card moves to **Needs you**:
reply with guidance (they get 4 more turns), or **Accept latest proposal** to end it.

Leave the terminal open while tasks run. Ctrl+C stops the dashboard.

## Use it from the apps too

Open this folder as a project in the Claude desktop app and in the Codex tab of the ChatGPT app.
Both read their rules on start (`CLAUDE.md` / `AGENTS.md` → `RULES.md`), so you can say:

- "check tasks": it claims its next card and does it
- "hand the rest to Codex": it creates a card for the other AI
- anything else: it does it and logs the key steps to the shared log

## Files

| Path | What it is |
|------|------------|
| `RULES.md` | Shared rules both AIs follow (log format, handoffs, red lines) |
| `CLAUDE.md` / `AGENTS.md` | Entry points for Claude / Codex, both pointing at `RULES.md` |
| `ws.py` | The whole system: CLI, runner, dashboard server (Python standard library only) |
| `dashboard.html` | The dashboard page |
| `workspace.json` | Port, timeouts, and the exact CLI commands and permission flags for each AI |
| `tasks/{todo,doing,waiting,done}/` | Task cards (one JSON file each) |
| `tasks/activity.jsonl` | Shared log, one line per event |
| `tasks/runs/` | Full output of every automatic run |
| `shared/` | Deliverables |

## CLI cheatsheet

```text
python3 ws.py new "Title" --to codex --details "..." --project x   # assign
python3 ws.py new "Title" --discuss --to claude --max-turns 8      # both discuss, Claude first
python3 ws.py new "Title" --github owner/name                       # work in a fresh clone, on a new branch
python3 ws.py new "Title" --folder ~/code/my-app                    # work directly in a folder on this Mac
python3 ws.py list [--status waiting]                             # board in text
python3 ws.py show <id>                                           # card + history + log
python3 ws.py reply <id> "Yes, go with option B"                  # answer a waiting card
python3 ws.py followup <id> "Also add tests" [--to codex]         # more work on a finished card (same card)
python3 ws.py pr <id> --yes | --no                                # answer "make a PR?"
python3 ws.py hide <id>... | --all [--undo]                        # hide finished cards from the dashboard
python3 ws.py handoff <id> --to claude --note "Codex is out of quota"
python3 ws.py run <id>                                            # run an agent now, in this terminal
python3 ws.py tail -n 30                                          # latest log lines
```

## Permissions (what auto-runs are allowed to do)

Set in `workspace.json`:

- **Claude**: `--permission-mode auto`. Safe actions run, and risky ones are blocked by Claude Code's classifier.
- **Codex**: `--sandbox workspace-write` with network on. It can write only inside this folder.

To make either AI more or less careful, change those flags. Runs time out after `timeout_minutes`.

## License

MIT. See [LICENSE](LICENSE).
