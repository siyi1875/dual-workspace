# Workspace rules (shared by Claude and Codex)

This folder is a shared workspace for two AI workers and one owner:

| Name     | Who                                   | Reads on start |
|----------|---------------------------------------|----------------|
| `me`     | The owner. Assigns work, makes calls. | -              |
| `claude` | Claude Code (Claude subscription)     | `CLAUDE.md`    |
| `codex`  | Codex in ChatGPT (ChatGPT subscription) | `AGENTS.md`  |

Neither AI has any special role. The owner decides who does what, task by task.

## How work moves

Work lives in **task cards**: JSON files under `tasks/`, one folder per status.

```
tasks/todo/      assigned, not started
tasks/doing/     someone is working on it
tasks/waiting/   blocked on the owner (a question, an approval, a failed run)
tasks/done/      finished, with a result
tasks/activity.jsonl   shared log: one JSON line per event, newest last
tasks/runs/      raw output of every automatic run
shared/          deliverables (one sub-folder per project or task)
```

Always change cards through `ws.py`. Never edit the JSON by hand. The script
keeps the folders, the card history and the log in sync, and it uses a lock so
both AIs can work at the same time.

## Before you start

1. Read the last 20 lines of `tasks/activity.jsonl` to see what happened recently.
2. If you were given a task id, read the card: `python3 ws.py show <id>`.
   It includes the card history and every log line for that task, so you can
   pick up where the other AI stopped.
3. If you were asked to "check tasks" with no id, claim one:
   `python3 ws.py next --who <you>` (it prints the card, or says there is none).

## While you work

| When                                | Run |
|-------------------------------------|-----|
| You make progress worth knowing     | `python3 ws.py log --who <you> --task <id> --msg "..."` |
| You need a decision or info from me | `python3 ws.py wait <id> --who <you> --need "<the question>"`, then stop |
| You finish                          | `python3 ws.py done <id> --who <you> --result "<2-4 sentences>" --file <path>` (add `--repo <path>` for each git repo you changed code in) |
| Part of the job suits the other AI  | `python3 ws.py new "<title>" --to <other> --from <you> --details "..." --project "..."` |

- Log every meaningful step, including work the owner asked for directly in chat
  rather than through a card (use `--task` only when there is a card).
- Put deliverables in `shared/<project-or-task-id>/` and pass them with `--file`.
- A result must stand on its own. The owner and the other AI read it without
  your chat history.

## Handing work to the other AI

- Delegate at most 3 sub-tasks per task, and never hand a task back to the AI that
  gave it to you. If you are stuck, use `wait` and let the owner decide.
- Write the card so a fresh session can do it: goal, inputs (file paths),
  what done looks like, where to save the output.

## Discussions (both AIs work it out together)

Some cards are **discussions**: you and the other AI take turns on the same card
until one of you accepts the other's proposal exactly as written. The dashboard
runs these turns. Each turn's prompt shows the whole conversation so far and the
proposal on the table, and tells you how to reply.

- Judge the proposal on its merits. Check claims, test code, look things up.
  Concede when the other AI is right, and push back with reasons when it isn't.
  Don't agree just to be polite, and don't argue over wording that changes nothing.
- Every proposal is the complete result, never a diff.
- `AGREE` accepts the other AI's current proposal as final. If you want any change,
  however small, send `REVISE` with the full new version instead.
- `ASK` pauses the discussion for a question only the owner can answer.
- After the turn limit the card comes to the owner, who can steer and extend it.
- `ws.py next` skips discussion cards, and you never run `done` or `wait` on one.
  Keep working files in `shared/discussions/<id>/`. The agreed result and the
  transcript are saved to `shared/discussions/<id>.md`.

## Follow-ups and pull requests

- A follow-up reopens the **same card**. Your prompt then shows every earlier result and the owner's
  follow-ups in order. Build on that work and do the latest follow-up; don't start over.
- When you finish a task that changed code in a git repo, the workspace notices (or you name the repo
  with `--repo`) and asks the owner whether to make a pull request. Don't push or open a PR yourself
  unless the card's latest follow-up says the owner approved it. When it does, follow the steps it gives
  and put the PR URL in your result.

## Where the work happens

- By default you work in this workspace and save deliverables under `shared/`.
- A card can name another place. Your prompt then has a "Where to work" section, and `ws.py show` prints `where:`.
  - **GitHub repo**: the workspace clones it into `shared/<id>/<name>` on a new `ws/...` branch before you start
    (`ws.py next` and `ws.py claim` do this too). Do the code work and commit there. Don't push, and pass `--repo` when done.
  - **Folder**: work directly in that folder on the owner's Mac. The card is your explicit permission to change files
    there, and only there.

## Red lines

- Never publish, post, send messages or buy anything. Prepare it, then `wait` for the owner.
- Never delete files. Move them to `_to_delete/` instead.
- Never write passwords, API keys, cookies or tokens into cards, logs or `shared/`.
- Never change files outside this folder unless the card explicitly says so.
- Never edit `ws.py`, `workspace.json` or these rules unless the card is about that.
