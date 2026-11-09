#!/usr/bin/env python3
"""Dual workspace for Claude Code + ChatGPT Codex.

Task cards (tasks/<status>/<id>.json), a shared log (tasks/activity.jsonl), an
agent runner that drives both CLIs on your subscriptions, and a local dashboard.

    python3 ws.py serve                 # dashboard + auto-runner on http://127.0.0.1:8766
    python3 ws.py new "Title" --to codex --details "..."
    python3 ws.py next --who claude     # claim the oldest card for claude
    python3 ws.py --help                # everything else

Standard library only.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
LOG = TASKS / "activity.jsonl"
RUNS = TASKS / "runs"
TRASH = TASKS / "_trash"
DISCUSSIONS = ROOT / "shared" / "discussions"
STATUSES = ("todo", "doing", "waiting", "done")
AGENTS = ("claude", "codex")
ASSIGNEES = AGENTS + ("any",)
WHO = AGENTS + ("me",)
MODES = ("solo", "discuss")
VERDICTS = ("AGREE", "REVISE", "ASK")
SHORT = {"claude": "Claude", "codex": "Codex", "me": "Owner"}
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "tool-cache", "dist", "build", "out", "cache", "lib"}
PR_INSTRUCTIONS = """The owner approved opening a pull request for the code changes from this task:
{repos}

For each repository:
1. If it is on the default branch, create a new branch with a short descriptive name.
2. Commit only the changes that belong to this task, with a clear message. Leave out scratch files, logs and
   reproductions unless they are meant to ship (for example new tests).
3. Push the branch and open a pull request with `gh pr create`: a short title, and a body that says what changed,
   why, and how it was tested. If a PR is already open for this branch (an earlier round), just push: the PR
   updates itself. Report its URL.
4. Finish with `ws.py done`, putting the PR URL(s) in --result.
The code may have been written by the other AI in an earlier round of this card. Read the earlier rounds and the
diff before you commit, so the PR describes the work accurately.
If pushing or `gh` fails (for example not logged in), don't work around it: use `ws.py wait` with the error."""


class WSError(Exception):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config():
    return json.loads((ROOT / "workspace.json").read_text())


def ensure_dirs():
    for s in STATUSES:
        (TASKS / s).mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    TRASH.mkdir(exist_ok=True)
    (ROOT / "shared").mkdir(exist_ok=True)
    LOG.touch(exist_ok=True)


@contextmanager
def locked():
    """Cross-process lock around every card change (both AIs may run ws.py at once)."""
    ensure_dirs()
    with open(TASKS / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------- log

def log(who, msg, task=None, status=None, files=None, project=None):
    entry = {"t": now(), "who": who, "task": task, "project": project,
             "status": status, "msg": msg, "files": files or None}
    entry = {k: v for k, v in entry.items() if v}
    ensure_dirs()
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_log(limit=None, task=None):
    if not LOG.exists():
        return []
    entries = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task is None or e.get("task") == task:
            entries.append(e)
    return entries[-limit:] if limit else entries


# ---------------------------------------------------------------- cards

def _path(card):
    return TASKS / card["status"] / f"{card['id']}.json"


def _find(task_id):
    for s in STATUSES:
        p = TASKS / s / f"{task_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise WSError(f"No task with id {task_id}")


def _write(card):
    p = _path(card)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _move(card, status):
    old = _path(card)
    card["status"] = status
    _write(card)
    if old != _path(card):
        old.unlink(missing_ok=True)


def _event(card, who, event, note=""):
    item = {"t": now(), "who": who, "event": event}
    if note:
        item["note"] = note
    card["history"].append(item)


def _check(value, allowed, what):
    if value not in allowed:
        raise WSError(f"{what} must be one of {', '.join(allowed)} (got {value!r})")


def list_cards(status=None):
    ensure_dirs()
    cards = []
    for s in ([status] if status else STATUSES):
        for p in (TASKS / s).glob("*.json"):
            try:
                cards.append(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return sorted(cards, key=lambda c: c["created_at"])


def get(task_id):
    return _find(task_id)


def new_task(title, to="any", details="", project="", by="me", auto=True, mode="solo", max_turns=None, where=None):
    title = title.strip()
    if not title:
        raise WSError("A task needs a title")
    _check(to, ASSIGNEES, "--to")
    _check(by, WHO, "--from")
    _check(mode, MODES, "--mode")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32].strip("-")
    tid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2) + (f"-{slug}" if slug else "")
    card = {
        "id": tid, "title": title, "details": details.strip(), "project": project.strip(),
        "to": to, "from": by, "status": "todo", "auto": bool(auto), "mode": mode,
        "created_at": now(), "claimed_by": None, "started_at": None, "finished_at": None,
        "need": "", "result": "", "files": [], "history": [],
    }
    where = _parse_where(where, tid, slug)
    if where:
        card["where"] = where
    if mode == "discuss":
        # Both AIs take turns on this card until one accepts the other's proposal as-is.
        # "to" always names whose turn it is, so the runner's normal claiming works unchanged.
        turns = int(max_turns or load_config().get("discussion_max_turns", 8))
        if turns < 2:
            raise WSError("A discussion needs at least 2 turns")
        card.update(max_turns=turns, turn=to, thread=[])
    with locked():
        _event(card, by, "created", f"discussion, {to} goes first" if mode == "discuss" else f"assigned to {to}")
        _write(card)
    if mode == "discuss":
        first = "either goes first" if to == "any" else f"{SHORT[to]} goes first"
        log(by, f"New discussion (Claude + Codex, {first}): {title}", task=tid, status="todo", project=card["project"])
    else:
        log(by, f"New task for {to}: {title}", task=tid, status="todo", project=card["project"])
    return card


# ---------------------------------------------------------------- where the work happens
# Default: the workspace itself (deliverables in shared/). A card can instead say
#   {"kind": "github", "repo": "owner/name", "path": "shared/<id>/<name>", "branch": "ws/..."}: a fresh clone on a new branch
#   {"kind": "folder", "path": "/abs/path"}: a folder on this Mac the owner picked; the agents may write there

def _parse_where(where, tid, slug):
    if not where or where.get("kind", "workspace") == "workspace":
        return None
    kind = where.get("kind")
    if kind == "github":
        raw = (where.get("repo") or "").strip()
        m = re.fullmatch(r"(?:https?://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?", raw)
        if not m:
            raise WSError(f"GitHub repo should look like owner/name (got {raw!r})")
        owner, name = m.groups()
        return {"kind": "github", "repo": f"{owner}/{name}", "path": f"shared/{tid}/{name}",
                "branch": f"ws/{tid.split('-')[2]}-{slug[:40]}".rstrip("-")}
    if kind == "folder":
        raw = (where.get("path") or "").strip()
        if not raw:
            raise WSError("Pick a folder for this task")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise WSError(f"Use a full folder path, like ~/code/app (got {raw!r})")
        path = path.resolve()
        if not path.is_dir():
            raise WSError(f"There's no folder at {path}")
        return {"kind": "folder", "path": str(path)}
    raise WSError(f"Unknown place to work: {kind!r}")


def where_dir(card):
    """Absolute folder the work happens in, or None for the workspace default."""
    w = card.get("where")
    if not w:
        return None
    return ROOT / w["path"] if w["kind"] == "github" else Path(w["path"])


def _where_repo(card):
    """The git repo the owner pointed this card at (the GitHub clone, or the picked folder if it's in git)."""
    d = where_dir(card)
    if not d or not d.is_dir():
        return None
    top = _git(d, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else None


def where_label(card):
    w = card.get("where")
    if not w:
        return "the workspace"
    return f"github.com/{w['repo']} (branch {w['branch']})" if w["kind"] == "github" else w["path"]


def prepare_where(card):
    """Make sure the place to work exists before an agent starts: clone a GitHub repo onto its own branch."""
    w = card.get("where")
    if not w or w["kind"] != "github":
        if w and not Path(w["path"]).is_dir():
            raise WSError(f"The folder for this task is gone: {w['path']}")
        return card
    dest = ROOT / w["path"]
    if (dest / ".git").exists():
        return card
    dest.parent.mkdir(parents=True, exist_ok=True)
    gh = shutil.which("gh") or next((str(p) for p in [Path.home() / ".local/bin/gh", Path("/opt/homebrew/bin/gh")] if p.exists()), None)
    cmd = ([gh, "repo", "clone", w["repo"], str(dest), "--", "--quiet"] if gh
           else ["git", "clone", "--quiet", f"https://github.com/{w['repo']}.git", str(dest)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        raise WSError(f"Cloning {w['repo']} took over 10 minutes and was stopped.")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip()[-600:]
        raise WSError(f"Couldn't clone github.com/{w['repo']}. Check the name and that `gh auth status` is logged in "
                      f"with access to it.\n{tail}")
    base = _git(dest, "branch", "--show-current") or ""
    if _git(dest, "checkout", "-q", "-b", w["branch"]) is None:
        raise WSError(f"Cloned {w['repo']} but couldn't create branch {w['branch']}")
    with locked():
        cur = _find(card["id"])
        cur["where"]["base"] = base
        _event(cur, "me", "cloned", f"github.com/{w['repo']} into {w['path']} on {w['branch']} (from {base or '?'})")
        _write(cur)
    log("me", f"Cloned github.com/{w['repo']} for this task, branch {w['branch']}", task=card["id"], project=card["project"])
    card["where"]["base"] = base
    return card


def _where_section(card, out_dir):
    w = card.get("where")
    if not w:
        return ""
    if w["kind"] == "github":
        return f"""
## Where to work: GitHub repo {w['repo']}
The owner chose to build this directly against GitHub. A fresh clone is at {w['path']}/ on branch `{w['branch']}`
(made from `{w.get('base') or 'the default branch'}`). Do all the code work in that clone, on that branch.
Commit as you go with clear messages, but don't push and don't open a PR: when you finish, the workspace asks
the owner whether to make a pull request. Add --repo {w['path']} when you run done.
Notes or reports for the owner (not part of the repo) go in {out_dir}/ as usual.
"""
    repo = _where_repo(card)
    git = (f"\nIt is a git repo ({_rel(repo)}, branch `{_git(repo, 'branch', '--show-current') or '?'}`). Don't push. "
           f"If it's on its default branch, make a new branch before committing. Add --repo {repo} when you run done.\n"
           if repo else "\n")
    return f"""
## Where to work: {w['path']}
The owner chose to work directly in this folder on their Mac, so you may create and change files there for this task.
This card is the explicit permission RULES.md asks for, for this folder only; change nothing else outside the workspace.{git}Notes or reports for the owner (not part of the project) go in {out_dir}/ as usual.
"""


# ---------------------------------------------------------------- Finder (macOS)

PICK_FOLDER = """on run argv
  activate
  set msg to "Choose the folder this task should work in"
  if (item 1 of argv) is not "" then
    try
      return POSIX path of (choose folder with prompt msg default location (POSIX file (item 1 of argv)))
    on error number n
      if n is -128 then error number -128 -- cancelled: don't ask twice
    end try
  end if
  return POSIX path of (choose folder with prompt msg)
end run"""


def pick_folder(start=""):
    """Show the Mac's own "Choose folder" window and return the full path ("" if cancelled)."""
    if sys.platform != "darwin":
        raise WSError("The folder picker only works on a Mac. Type the path instead.")
    start = str(Path(start).expanduser()) if start and Path(start).expanduser().is_dir() else ""
    try:
        r = subprocess.run(["osascript", "-e", PICK_FOLDER, start], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return ""
    if r.returncode != 0:  # -128 is "User canceled"
        if "-128" in r.stderr:
            return ""
        raise WSError(f"Couldn't open the folder picker: {r.stderr.strip()[-300:]}")
    path = r.stdout.strip()
    return path.rstrip("/") if len(path) > 1 else path


def reveal(task_id):
    """Open the folder a card works in (the picked folder or the GitHub clone) in a Finder window."""
    card = _find(task_id)
    d = where_dir(card)
    if not d:
        d = ROOT / "shared" / card["id"]
        if not d.is_dir() and card["project"]:
            d = ROOT / "shared" / re.sub(r"[^A-Za-z0-9_-]+", "-", card["project"])
    if not d.is_dir():
        raise WSError(f"Nothing to open yet: {d} doesn't exist" + (" (it's cloned when the task starts)" if card.get("where") else ""))
    subprocess.run(["open", str(d)], check=False)
    return {"ok": True, "path": str(d)}


def is_discussion(card):
    return card.get("mode") == "discuss"


def agent_turns(card):
    return sum(1 for m in card.get("thread", []) if m["who"] in AGENTS)


def standing_proposal(card):
    """The latest full proposal in a discussion (the one an AGREE would accept).

    An owner follow-up after an agreement reopens the discussion: the old result can't be accepted as-is,
    someone has to propose a version that answers the follow-up first.
    """
    for m in reversed(card.get("thread", [])):
        if m.get("followup"):
            return None
        if m.get("proposal"):
            return m
    return None


def latest_proposal(card):
    """The latest proposal, including one agreed before a follow-up."""
    for m in reversed(card.get("thread", [])):
        if m.get("proposal"):
            return m
    return None


def _extend_turns(card):
    used = agent_turns(card)
    card["max_turns"] = max(card["max_turns"], used + int(load_config().get("discussion_extend_turns", 4)))


def _write_discussion(card, final, how):
    """Save the agreed result plus the full transcript as one readable file."""
    DISCUSSIONS.mkdir(parents=True, exist_ok=True)
    path = DISCUSSIONS / f"{card['id']}.md"
    parts = [f"# {card['title']}", how, "## Agreed result", final.strip()]
    if card["details"]:
        parts += ["## Brief", card["details"]]
    parts.append("## Discussion")
    n = 0
    for m in card.get("thread", []):
        if m["who"] in AGENTS:
            n += 1
            parts.append(f"### Turn {n}: {SHORT[m['who']]} ({m.get('verdict', '')})")
        else:
            parts.append(f"### {SHORT.get(m['who'], m['who'])}" + (" (follow-up)" if m.get("followup") else ""))
        parts.append(m.get("message") or "(no message)")
        if m.get("proposal"):
            parts += ["**Proposal:**", m["proposal"]]
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return str(path.relative_to(ROOT))


def claim(task_id, who, force=False):
    _check(who, AGENTS, "--who")
    with locked():
        card = _find(task_id)
        if card["status"] != "todo":
            raise WSError(f"{task_id} is {card['status']}, not todo")
        if card["to"] not in (who, "any") and not force:
            raise WSError(f"{task_id} is assigned to {card['to']} (use --force to take it anyway)")
        card.update(claimed_by=who, started_at=now(), need="", repos_before=_repo_snapshot(card))
        _event(card, who, "claimed")
        _move(card, "doing")
    log(who, f"Started: {card['title']}", task=task_id, status="doing", project=card["project"])
    return card


def claim_next(who, auto_only=False, discussions=False):
    """Discussion turns are only taken by the runner, which knows how to record a turn."""
    _check(who, AGENTS, "--who")
    with locked():
        for card in list_cards("todo"):
            if is_discussion(card) and not discussions:
                continue
            if card["to"] in (who, "any") and (card["auto"] or not auto_only):
                card.update(claimed_by=who, started_at=now(), need="")
                if not is_discussion(card):
                    card["repos_before"] = _repo_snapshot(card)
                if auto_only:
                    card["runner_pid"] = os.getpid()  # lets a restarted dashboard spot runs it lost
                _event(card, who, "claimed")
                _move(card, "doing")
                break
        else:
            return None
    started = f"Taking turn {agent_turns(card) + 1} of the discussion" if is_discussion(card) else "Started"
    log(who, f"{started}: {card['title']}", task=card["id"], status="doing", project=card["project"])
    return card


def finish(task_id, who, result="", files=(), repos=()):
    _check(who, WHO, "--who")
    files = list(files)
    with locked():
        card = _find(task_id)
        if card["status"] == "done":
            raise WSError(f"{task_id} is already done")
        result, note = result.strip(), ""
        if repos:
            card["repos"] = sorted(set(card.get("repos", [])) | {str(r) for r in repos})
        if is_discussion(card):
            # Owner ends a discussion: with no text of their own, the latest proposal stands.
            st = standing_proposal(card) or latest_proposal(card)
            if result:
                card["thread"].append({"t": now(), "who": who, "message": f"Final decision: {result}"})
                how = f"Decided by {SHORT[who]} after {agent_turns(card)} turns."
            elif st:
                result, note = st["proposal"], f"accepted {st['who']}'s latest proposal"
                how = f"No full agreement; {SHORT[who]} accepted {SHORT[st['who']]}'s latest proposal after {agent_turns(card)} turns."
            else:
                result, how = "Ended by owner before any proposal.", "Ended by the owner."
            files.append(_write_discussion(card, result, how))
        elif not result and who != "me":
            raise WSError("--result is required")
        card.update(result=result or "Marked done by owner.", finished_at=now(), need="")
        card["files"] = sorted(set(card["files"]) | set(files))
        if who == "me" and card.get("pr", {}).get("status") == "ask":
            card["pr"]["status"] = "declined"  # owner closed it without a PR
        _event(card, who, "done", note)
        changes = _needs_pr(card) if who in AGENTS else []
        if changes:
            _ask_pr(card, who, changes)
        else:
            _move(card, "done")
    if changes:
        log(who, f"Done, but it changed code: {card['need'].splitlines()[0]}", task=task_id, status="waiting",
            files=list(files), project=card["project"])
    else:
        log(who, f"Done: {card['title']}", task=task_id, status="done", files=list(files), project=card["project"])
    return card


def wait(task_id, who, need):
    _check(who, AGENTS, "--who")
    with locked():
        card = _find(task_id)
        card["need"] = need.strip()
        _event(card, who, "waiting on owner", need.strip())
        _move(card, "waiting")
    log(who, f"Needs you: {need.strip()}", task=task_id, status="waiting", project=card["project"])
    return card


def reply(task_id, note):
    """Owner answers a waiting card; it goes back to the same AI.

    On a discussion you can also add a note between turns; the next AI to speak sees it.
    """
    note = note.strip()
    with locked():
        card = _find(task_id)
        if is_discussion(card) and card["status"] in ("todo", "doing"):
            card["thread"].append({"t": now(), "who": "me", "message": note})
            _event(card, "me", "added a note", note)
            _write(card)
            log("me", f"Note to the discussion: {note}", task=task_id, status=card["status"], project=card["project"])
            return card
        if card["status"] != "waiting":
            raise WSError(f"{task_id} is {card['status']}, not waiting")
        if is_discussion(card):
            card["thread"].append({"t": now(), "who": "me", "message": note})
            card["to"] = card.get("turn") or card["claimed_by"] or card["to"]
            _extend_turns(card)
        else:
            card["to"] = card["claimed_by"] or card["to"]
        card.update(claimed_by=None, need="", auto=True)
        _event(card, "me", "replied", note)
        _move(card, "todo")
    log("me", f"Replied: {note}", task=task_id, status="todo", project=card["project"])
    return card


def reassign(task_id, to, note=""):
    _check(to, ASSIGNEES, "--to")
    with locked():
        card = _find(task_id)
        if card["status"] == "doing":
            raise WSError(f"{task_id} is being worked on. Stop it first.")
        prev = card["claimed_by"] or card["to"]
        card.update(to=to, claimed_by=None, need="", finished_at=None)
        if is_discussion(card):
            card["turn"] = to
            _extend_turns(card)
            if note.strip():
                card["thread"].append({"t": now(), "who": "me", "message": note.strip()})
        _event(card, "me", f"handed from {prev} to {to}", note.strip())
        _move(card, "todo")
    log("me", f"Handed to {to}" + (f": {note.strip()}" if note.strip() else ""), task=task_id, status="todo",
        project=card["project"])
    return card


def set_auto(task_id, auto=True):
    with locked():
        card = _find(task_id)
        card["auto"] = auto
        _write(card)
    return card


def set_hidden(task_ids, hidden=True):
    """Hide finished cards from the dashboard's Completed list and activity log (for demos and screen recordings).
    Nothing is deleted or moved, and no log line is written, so the title doesn't show up anywhere new."""
    out = []
    with locked():
        for task_id in task_ids:
            card = _find(task_id)
            if hidden and card["status"] != "done":
                raise WSError(f"{task_id} is {card['status']}: only finished cards can be hidden")
            if hidden:
                card["hidden"] = True
            else:
                card.pop("hidden", None)
            _write(card)
            out.append(card)
    return out


def remove(task_id):
    """Cards are never deleted, only moved to tasks/_trash."""
    with locked():
        card = _find(task_id)
        if card["status"] == "doing":
            raise WSError(f"{task_id} is being worked on. Stop it first.")
        shutil.move(str(_path(card)), TRASH / f"{task_id}.json")
    log("me", f"Removed: {card['title']}", task=task_id, project=card["project"])


def _reopen(card, to, note, by="me"):
    """Put a finished card back in the queue with a new request; earlier results stay on the card."""
    if is_discussion(card):
        card["thread"].append({"t": now(), "who": by, "message": note, "followup": True})
        card["turn"] = to
        _extend_turns(card)
    else:
        card.setdefault("followups", []).append({"t": now(), "who": by, "note": note, "result_before": card["result"]})
    card.update(to=to, claimed_by=None, need="", result="", finished_at=None, auto=True)
    card.pop("hidden", None)  # it's active again, so it shows in the main column
    _move(card, "todo")


def follow_up(task_id, note, to=None):
    """Ask for more on a finished task. The same card reopens, so the whole story stays in one place."""
    note = note.strip()
    if not note:
        raise WSError("A follow-up needs a message")
    if to:
        _check(to, ASSIGNEES, "--to")
    with locked():
        card = _find(task_id)
        if card["status"] != "done":
            raise WSError(f"{task_id} is {card['status']}, not done (use reply for a card that needs you)")
        if not to:
            # Discussions: whoever wrote the agreed result revises it first. Solo: the same AI as before.
            last = latest_proposal(card) if is_discussion(card) else None
            to = last["who"] if last else card["claimed_by"] or card["to"]
        card.pop("pr", None)  # new work, so check for code changes again when it's done
        _event(card, "me", f"follow-up for {to}", note)
        _reopen(card, to, note)
    log("me", f"Follow-up for {SHORT.get(to, to)}: {note}", task=task_id, status="todo", project=card["project"])
    return card


def pr_decide(task_id, yes, note=""):
    """Owner's answer to "this task changed code, make a PR?"."""
    note = note.strip()
    with locked():
        card = _find(task_id)
        pr = card.get("pr") or {}
        if card["status"] != "waiting" or pr.get("status") != "ask":
            raise WSError(f"{task_id} isn't asking about a PR")
        if yes:
            agent = load_config().get("pr_agent") or card["claimed_by"] or card["to"]  # who pushes (workspace.json)
            repos = "\n".join(f"- {r['path']}: {r['summary']}" for r in pr["repos"])
            pr["status"] = "approved"
            _event(card, "me", "approved a PR", note)
            _reopen(card, agent, PR_INSTRUCTIONS.format(repos=repos) + (f"\n\nOwner's note: {note}" if note else ""))
        else:
            pr["status"] = "declined"
            card.update(need="", finished_at=now())
            _event(card, "me", "no PR", note)
            _move(card, "done")
    log("me", f"Approved a PR, over to {SHORT[card['to']]}" if yes else "No PR needed", task=task_id,
        status=card["status"], project=card["project"])
    return card


# ---------------------------------------------------------------- code changes -> PR question

def _git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _rel(path):
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve_repo(path):
    p = Path(path).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def _candidate_repos(card):
    """Git repos this task may have touched: under its shared/ folders, around its files, or named with --repo."""
    found = {_resolve_repo(r) for r in card.get("repos", [])}
    if _where_repo(card):
        found.add(_where_repo(card))
    for f in card["files"]:
        p = (ROOT / f).resolve()
        for parent in [p, *p.parents]:
            if parent == ROOT or ROOT not in parent.parents:
                break
            if (parent / ".git").exists():
                found.add(parent)
                break
    tops = {ROOT / "shared" / card["id"]}
    if card["project"]:
        tops.add(ROOT / "shared" / re.sub(r"[^A-Za-z0-9_-]+", "-", card["project"]))
    for top in tops:
        if not top.is_dir():
            continue
        for d, subdirs, _ in os.walk(top):
            if (Path(d) / ".git").exists():
                found.add(Path(d).resolve())
                subdirs[:] = []
                continue
            depth = len(Path(d).relative_to(top).parts)
            subdirs[:] = [s for s in subdirs if depth < 3 and s not in SKIP_DIRS and not s.startswith(".")]
    return sorted(found)


def _fingerprint(repo):
    """HEAD plus the size and modified time of every tracked file that differs from it.

    Cheap even for big repos with binary files, and still notices more edits to a file that was
    already changed before the task started."""
    h = hashlib.sha1()
    changed = (_git(repo, "diff", "HEAD", "--name-only", "-z") or "") + "\0" + \
              (_git(repo, "ls-files", "--others", "--exclude-standard", "-z") or "")
    for name in changed.split("\0"):
        if not name:
            continue
        try:
            st = (Path(repo) / name).stat()
            h.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{name}\0gone\n".encode())
    return (_git(repo, "rev-parse", "HEAD") or "") + ":" + h.hexdigest()


def _repo_snapshot(card):
    """State of each candidate repo when work starts, so only changes made during the task count."""
    snap = {}
    for r in _candidate_repos(card):
        try:
            snap[str(r)] = _fingerprint(r)
        except Exception as exc:  # a broken repo must never stop the card from being claimed
            print(f"[ws] skipped repo {r}: {exc}", file=sys.stderr)
    return snap


def _repo_changes(repo, explicit):
    """What would go into a PR: edited tracked files, commits on no remote, and (if named with --repo) new files."""
    status = _git(repo, "status", "--porcelain")
    if status is None:
        return ""
    lines = [l for l in status.splitlines() if l.strip()]
    edited = sum(1 for l in lines if not l.startswith("??"))
    new = sum(1 for l in lines if l.startswith("??")) if explicit else 0
    ahead = 0
    if _git(repo, "remote"):
        n = _git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
        ahead = int(n) if n and n.isdigit() else 0
    parts = [f"{edited} edited file{'s' * (edited != 1)}" if edited else "",
             f"{new} new file{'s' * (new != 1)}" if new else "",
             f"{ahead} unpushed commit{'s' * (ahead != 1)}" if ahead else ""]
    summary = ", ".join(p for p in parts if p)
    if summary:
        branch = _git(repo, "branch", "--show-current")
        summary += f" (branch {branch})" if branch else ""
    return summary


def _needs_pr(card):
    """Repos this task changed and hasn't shipped. Skipped for discussions and after the owner decided."""
    if is_discussion(card) or card.get("pr", {}).get("status") in ("approved", "declined"):
        return []
    before = card.get("repos_before")
    started = datetime.fromisoformat(card["started_at"]).timestamp() if card.get("started_at") else 0
    explicit = {str(_resolve_repo(r)) for r in card.get("repos", [])}
    where = str(_where_repo(card) or "")
    out = []
    for repo in _candidate_repos(card):
        key = str(repo)
        try:
            if key not in explicit:
                if before is not None:
                    if key in before and before[key] == _fingerprint(repo):
                        continue  # untouched during this task
                else:  # card claimed before snapshots existed: fall back to "git index written since the start"
                    idx = repo / ".git" / "index"
                    if not idx.exists() or idx.stat().st_mtime < started:
                        continue
            summary = _repo_changes(repo, key in explicit or key == where)
        except Exception as exc:  # a broken repo must never stop the card from finishing
            print(f"[ws] skipped repo {repo}: {exc}", file=sys.stderr)
            continue
        if summary:
            out.append({"path": _rel(repo), "summary": summary})
    return out


def _ask_pr(card, who, changes):
    where = "; ".join(f"{c['path']} ({c['summary']})" for c in changes)
    card["pr"] = {"status": "ask", "repos": changes}
    card["need"] = (f"This task changed code that isn't in a pull request yet: {where}. "
                    f"Should {SHORT[load_config().get('pr_agent') or who]} make a PR and push it?")
    _event(card, who, "changed code, asked about a PR", where)
    _move(card, "waiting")


# ---------------------------------------------------------------- agent runner

def resolve_bin(candidates):
    for c in candidates:
        c = os.path.expanduser(c)
        found = shutil.which(c) if os.sep not in c else (c if os.access(c, os.X_OK) else None)
        if found:
            return found
    return None


def clean_env(cfg):
    prefixes = tuple(cfg.get("clean_env_prefixes", []))
    return {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}


def _tail(path, chars=3000):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return text[-chars:].strip()


def last_run(task_id):
    runs = sorted(RUNS.glob(f"{task_id}--*.log"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def build_prompt(agent, card, cfg):
    other = "codex" if agent == "claude" else "claude"
    label, other_label = cfg["agents"][agent]["label"], cfg["agents"][other]["label"]
    history = "\n".join(
        f"- {h['t']} {h['who']}: {h['event']}" + (f" ({h['note']})" if h.get("note") else "")
        for h in card["history"])
    logs = "\n".join(json.dumps(e, ensure_ascii=False) for e in read_log(limit=15, task=card["id"]))
    previous = ""
    prev = last_run(card["id"])
    if prev:
        prev_agent = prev.name.split("--")[1]
        out = _tail(prev.with_suffix(".last.txt")) or _tail(prev)
        previous = f"\n## Output from the previous attempt (by {prev_agent})\n{out}\n"
    out_dir = f"shared/{re.sub(r'[^A-Za-z0-9_-]+', '-', card['project']) if card['project'] else card['id']}"
    rounds = ""
    if card.get("followups"):
        parts = []
        for i, f in enumerate(card["followups"], 1):
            parts.append(f"### Result of round {i}\n{f['result_before'] or '(none)'}\n\n"
                         f"### {'Owner' if f['who'] == 'me' else SHORT[f['who']]}'s follow-up{' (DO THIS NOW)' if i == len(card['followups']) else ''}\n{f['note']}")
        rounds = ("\n## Earlier rounds on this card\nThis task was finished before and the owner asked for more on the same card.\n"
                  "Build on the earlier work; the latest follow-up below is what to do now.\n\n" + "\n\n".join(parts) + "\n")
    return f"""You are {label} ("{agent}"), one of two AI workers in a shared workspace with {other_label} ("{other}").
The owner ("me") assigns work through task cards. Read RULES.md in this folder before doing anything else.

You have been assigned task {card['id']}. It is already claimed for you (status: doing). Do the work now.

## Task
Title: {card['title']}
Project: {card['project'] or '-'}
Details:
{card['details'] or '(none, the title is the whole brief)'}
{_where_section(card, out_dir)}{rounds}
## Card history
{history}

## Shared log for this task (oldest first)
{logs or '(empty)'}
{previous}
## When you finish
- Save deliverables under {out_dir}/
- Run: python3 ws.py done {card['id']} --who {agent} --result "<2-4 sentence summary>" --file <path>
  If you changed code in a git repository, add --repo <path to the repo> (repeat for each). The workspace then
  asks the owner whether to make a pull request. Don't push or open a PR unless a follow-up above says the owner approved it.
- If you need a decision or information from the owner, run
  python3 ws.py wait {card['id']} --who {agent} --need "<your question>"  and then stop.
- Log progress worth knowing: python3 ws.py log --who {agent} --task {card['id']} --msg "..."
- To hand a sub-task to {other}: python3 ws.py new "<title>" --to {other} --from {agent} --details "..." --project "{card['project']}"
"""


def other_agent(agent):
    return "codex" if agent == "claude" else "claude"


def build_discussion_prompt(agent, card, cfg):
    other = other_agent(agent)
    label, other_label = cfg["agents"][agent]["label"], cfg["agents"][other]["label"]
    standing = standing_proposal(card)
    agreed = None if standing else latest_proposal(card)  # set when a follow-up reopened an agreed result
    turn_no = agent_turns(card) + 1
    lines, n = [], 0
    for m in card["thread"]:
        if m["who"] in AGENTS:
            n += 1
            head = f"### Turn {n}: {cfg['agents'][m['who']]['label']} ({m.get('verdict', '')})"
        else:
            head = "### The owner, follow-up after your agreement (answer this now)" if m.get("followup") else "### The owner"
        body = m.get("message") or "(no message)"
        if m.get("proposal"):
            body += "\n\n" + ("[made the proposal shown in full below]" if m is standing or m is agreed
                              else "[made a proposal, since replaced by a later one]")
        lines.append(f"{head}\n{body}")
    if standing:
        on_table = f"Proposed by {cfg['agents'][standing['who']]['label']}:\n\n{standing['proposal']}"
        whose = "yours" if standing["who"] == agent else f"{other}'s"
    elif agreed:
        on_table = ("(none since the owner's follow-up. The result you both agreed before, which the follow-up "
                    f"builds on, is below. Write a complete revised version that answers the follow-up.)\n\n{agreed['proposal']}")
        whose = None
    else:
        on_table, whose = "(none yet)", None
    if whose == f"{other}'s":
        agree_rule = (f"- AGREE means you would sign off on {other}'s proposal above exactly as written. That ends the\n"
                      f"  discussion and it becomes the final result. If you want any change at all, however small,\n"
                      f"  use REVISE and write the full revised proposal, so {other} can accept or challenge it.")
    else:
        agree_rule = (f"- You can't AGREE yet: there is no proposal from {other} on the table. Use REVISE.")
    return f"""You are {label} ("{agent}"). The owner has asked you and {other_label} ("{other}") to discuss a task
and keep going until you both agree on one result. You take turns. This is turn {turn_no} of at most {card['max_turns']}.
Read RULES.md in this folder first. Its red lines apply here too.

## Task
Title: {card['title']}
Project: {card['project'] or '-'}
Details:
{card['details'] or '(none, the title is the whole brief)'}
{_where_section(card, f"shared/discussions/{card['id']}")}
## Discussion so far
{chr(10).join(lines) if lines else '(nothing yet: you go first)'}

## Current proposal on the table
{on_table}

## How to take your turn
- First turn: do the work and write your best complete proposal.
- Later turns: review the current proposal on its merits. Check its claims, test code, or look things up when
  that helps. You can read and edit files in this folder; keep working files in shared/discussions/{card['id']}/.
  Answer each point the other side raised. Concede when they are right and push back, with reasons, when they
  are not. Don't agree just to be polite, and don't argue over wording that doesn't change anything.
{agree_rule}
- If you can't go on without information or a decision only the owner has, use ASK and put the question in
  your message.
- Don't run `ws.py done`, `ws.py wait` or `ws.py new` for this card. The workspace records your reply and
  passes it on. If you AGREE, don't change any files.

## Reply format
End with your reply in exactly this format (it is parsed):

## Message
<your reply to {other}: what you checked, what you agree or disagree with and why, what you changed>

## Proposal
<the complete result you propose, never a diff or "same as before except". Leave this section out if you AGREE.>

VERDICT: AGREE or REVISE or ASK
"""


def parse_turn(text):
    """Split an agent's reply into message, proposal and verdict. Lenient about markdown decoration."""
    found = list(re.finditer(r"^[\s*_#>`]*VERDICT[\s*_`]*:[\s*_`]*(AGREE|REVISE|ASK)\b", text, re.I | re.M))
    verdict = found[-1].group(1).upper() if found else "REVISE"
    body = text[:found[-1].start()] if found else text
    pm = re.search(r"^#{1,6}\s*\**Proposal\**:?\s*$", body, re.I | re.M)
    message, proposal = (body[:pm.start()], body[pm.end():]) if pm else (body, "")
    message = re.sub(r"^#{1,6}\s*\**Message\**:?\s*$", "", message, count=1, flags=re.I | re.M)
    return {"verdict": verdict, "message": message.strip(), "proposal": proposal.strip()}


def _same(a, b):
    return " ".join(a.split()) == " ".join(b.split())


def _exec(agent, card, cfg, prompt, register=None):
    """Run one agent CLI headlessly. Returns (exit code, timed out?, run log path, last message path)."""
    spec = cfg["agents"][agent]
    exe = resolve_bin(spec["bin"])
    if not exe:
        raise WSError(f"{spec['label']} CLI not found (looked for {', '.join(spec['bin'])})")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_log = RUNS / f"{card['id']}--{agent}--{stamp}.log"
    last_msg = run_log.with_suffix(".last.txt")
    cmd = [exe] + [a.replace("{last_message_file}", str(last_msg)) for a in spec["args"]]
    d = where_dir(card)
    if d and ROOT not in d.parents:  # a folder outside the workspace: let the CLI write there too
        extra = ["--add-dir", str(d)]
        cmd = cmd[:-1] + extra + cmd[-1:] if cmd[-1] == "-" else cmd + extra
    with open(run_log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                text=True, env=clean_env(cfg), start_new_session=True)
        if register:
            register(proc)
        proc.stdin.write(prompt)
        proc.stdin.close()
        timed_out = False
        try:
            code = proc.wait(timeout=cfg.get("timeout_minutes", 60) * 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            code = proc.wait()
            timed_out = True
            out.write(f"\n[ws] timed out after {cfg.get('timeout_minutes', 60)} minutes\n")
    return code, timed_out, run_log, last_msg


def _park_failed(cur, agent, cfg, code, timed_out, run_log):
    if timed_out:
        reason = f"Timed out after {cfg.get('timeout_minutes', 60)} minutes."
    elif code < 0:
        reason = "Stopped by you."
    else:
        reason = f"{cfg['agents'][agent]['label']} exited with code {code} (out of quota? check the output)."
        tail = _tail(run_log, 4000)
        limit = re.search(r"usage limit|rate limit|out of (?:credits|quota)", tail, re.I)
        if limit:
            retry = re.search(r"try again (?:at|in) [^\n.]+", tail[limit.start():], re.I)
            when = f" It says: {retry.group(0)}." if retry else ""
            reason = f"{cfg['agents'][agent]['label']} is out of usage on your subscription.{when}"
    cur["need"] = f"{reason} Retry, reply, or hand it to the other AI.\n\nLast output:\n{_tail(run_log, 800)}"
    _event(cur, agent, "run failed", reason)
    _move(cur, "waiting")


def run_agent(agent, card, cfg, register=None):
    """Run one agent CLI on an already-claimed card and settle the card afterwards."""
    log(agent, f"Auto-run started ({cfg['agents'][agent]['label']} CLI)", task=card["id"], status="doing",
        project=card["project"])
    code, timed_out, run_log, last_msg = _exec(agent, card, cfg, build_prompt(agent, card, cfg), register)
    result = _tail(last_msg) or _tail(run_log)
    with locked():
        cur = _find(card["id"])
        if cur["status"] != "doing" or cur["claimed_by"] != agent:
            return cur  # the agent already settled the card itself (done / wait)
        changes = []
        if code == 0:
            cur.update(result=cur["result"] or result, finished_at=now())
            _event(cur, agent, "done", "settled by runner")
            changes = _needs_pr(cur)
            if changes:
                _ask_pr(cur, agent, changes)
            else:
                _move(cur, "done")
        else:
            _park_failed(cur, agent, cfg, code, timed_out, run_log)
    if changes:
        msg = f"Done, but it changed code: {cur['need']}"
    else:
        msg = "Done (auto-run)" if code == 0 else f"Run ended without finishing (exit {code})"
    log(agent, msg, task=card["id"], status=cur["status"], project=cur["project"])
    return cur


def run_discussion_turn(agent, card, cfg, register=None):
    """One turn of a discussion: the agent replies, then the card goes to the other AI, the owner, or done."""
    other = other_agent(agent)
    code, timed_out, run_log, last_msg = _exec(agent, card, cfg, build_discussion_prompt(agent, card, cfg), register)
    text = _tail(last_msg, 200000) or _tail(run_log, 200000)
    with locked():
        cur = _find(card["id"])
        if cur["status"] != "doing" or cur["claimed_by"] != agent:
            return cur
        if code != 0:
            cur["turn"] = agent
            _park_failed(cur, agent, cfg, code, timed_out, run_log)
            msg = f"Turn ended without a reply (exit {code})"
        else:
            turn = parse_turn(text)
            standing = standing_proposal(cur)
            theirs = standing if standing and standing["who"] != agent else None
            verdict = turn["verdict"]
            if verdict == "REVISE" and theirs and turn["proposal"] and _same(turn["proposal"], theirs["proposal"]):
                verdict = "AGREE"  # repeated the other side's proposal word for word: that is agreement
            if verdict == "AGREE" and not theirs:
                verdict = "REVISE"  # nothing from the other side to accept yet
            entry = {"t": now(), "who": agent, "verdict": verdict, "message": turn["message"],
                     "proposal": "" if verdict == "AGREE" else turn["proposal"]}
            if not entry["message"] and not entry["proposal"]:
                entry["message"] = text.strip()[-4000:] or "(empty reply)"
            cur["thread"].append(entry)
            used = agent_turns(cur)
            if verdict == "AGREE":
                how = (f"Agreed by Claude and Codex after {used} turns: {SHORT[theirs['who']]} proposed it and "
                       f"{SHORT[agent]} accepted it as written.")
                cur["files"] = sorted(set(cur["files"]) | {_write_discussion(cur, theirs["proposal"], how)})
                cur.update(result=theirs["proposal"], finished_at=now(), need="", turn=None)
                _event(cur, agent, "agreed", f"accepted {other}'s proposal")
                _move(cur, "done")
                msg = f"Agreed with {SHORT[other]} after {used} turns. Discussion done."
            elif verdict == "ASK":
                cur.update(need=turn["message"] or "(asked for input without a question)", turn=agent)
                _event(cur, agent, "asked the owner", turn["message"][:300])
                _move(cur, "waiting")
                msg = f"Discussion paused, needs you: {turn['message'][:200]}"
            elif used >= cur["max_turns"]:
                extend = cfg.get("discussion_extend_turns", 4)
                cur.update(turn=other, need=(
                    f"No agreement after {used} turns. Read the discussion, then reply with a decision or guidance "
                    f"(they get {extend} more turns), or Mark done to accept the latest proposal."))
                _event(cur, agent, "no agreement yet", f"turn limit of {cur['max_turns']} reached")
                _move(cur, "waiting")
                msg = f"No agreement after {used} turns. Needs you."
            else:
                cur.update(to=other, turn=other, claimed_by=None)
                did = "proposed a revision" if entry["proposal"] else "replied"
                _event(cur, agent, f"turn {used}: {did}", f"over to {other}")
                _move(cur, "todo")
                msg = f"Turn {used}: {did}, over to {SHORT[other]}"
    log(agent, msg, task=card["id"], status=cur["status"], project=cur["project"],
        files=cur["files"] if cur["status"] == "done" else None)
    return cur


def run_card(agent, card, cfg, register=None):
    try:
        card = prepare_where(card)
    except WSError as exc:
        return wait(card["id"], agent, str(exc))
    run = run_discussion_turn if is_discussion(card) else run_agent
    return run(agent, card, cfg, register)


class Runner:
    """Picks up auto-run cards for each idle agent; one task per agent at a time."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.busy = {}  # agent -> {"task": id, "proc": Popen | None}
        self.mu = threading.Lock()

    def available(self, agent):
        return resolve_bin(self.cfg["agents"][agent]["bin"]) is not None

    def state(self):
        with self.mu:
            return {a: {"label": self.cfg["agents"][a]["label"], "available": self.available(a),
                        "task": self.busy.get(a, {}).get("task")} for a in AGENTS}

    def tick(self):
        for agent in AGENTS:
            with self.mu:
                if agent in self.busy or not self.available(agent):
                    continue
                card = claim_next(agent, auto_only=True, discussions=True)
                if not card:
                    continue
                self.busy[agent] = {"task": card["id"], "proc": None}
            threading.Thread(target=self._run, args=(agent, card), daemon=True).start()

    def _run(self, agent, card):
        def register(proc):
            with self.mu:
                self.busy[agent]["proc"] = proc
        try:
            run_card(agent, card, self.cfg, register)
        except Exception as exc:  # keep the runner alive no matter what
            try:
                wait(card["id"], agent, f"Runner error: {exc}")
            except WSError:
                pass
        finally:
            with self.mu:
                self.busy.pop(agent, None)

    def stop(self, task_id):
        with self.mu:
            for info in self.busy.values():
                if info["task"] == task_id and info["proc"]:
                    os.killpg(info["proc"].pid, signal.SIGTERM)
                    return True
        # Not run by us (e.g. an interactive session claimed it): park it for the owner.
        card = _find(task_id)
        if card["status"] == "doing":
            wait(task_id, card["claimed_by"], "Stopped by you.")
        return False

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                print(f"[ws] runner: {exc}", file=sys.stderr)
            time.sleep(self.cfg.get("poll_seconds", 4))


# ---------------------------------------------------------------- dashboard server

def make_handler(runner, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _trusted(self):
            # Block other websites and DNS rebinding from driving your agents.
            if self.headers.get("Host") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).netloc in allowed_hosts

        def do_GET(self):
            if not self._trusted():
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, {"cards": list_cards(), "log": read_log(limit=150), "agents": runner.state(),
                                        "pr_agent": load_config().get("pr_agent")})
            m = re.fullmatch(r"/api/tasks/([\w-]+)/output", path)
            if m:
                run = last_run(m.group(1))
                return self._send(200, {"file": run.name if run else None, "text": _tail(run, 20000) if run else ""})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._trusted() or self.headers.get("X-WS") != "1":
                return self._send(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/tasks":
                    card = new_task(body.get("title", ""), body.get("to", "any"), body.get("details", ""),
                                    body.get("project", ""), "me", body.get("auto", True),
                                    body.get("mode", "solo"), body.get("max_turns"), body.get("where"))
                    return self._send(200, card)
                if path == "/api/pick-folder":
                    return self._send(200, {"path": pick_folder(body.get("start", ""))})
                m = re.fullmatch(r"/api/tasks/([\w-]+)/(\w+)", path)
                if not m:
                    return self._send(404, {"error": "not found"})
                tid, action = m.groups()
                if action == "start":
                    result = set_auto(tid, True)
                elif action == "stop":
                    runner.stop(tid)
                    result = {"ok": True}
                elif action == "reply":
                    result = reply(tid, body.get("note", ""))
                elif action == "reassign":
                    result = reassign(tid, body.get("to", "any"), body.get("note", ""))
                elif action == "done":
                    result = finish(tid, "me", body.get("note", ""))
                elif action == "followup":
                    result = follow_up(tid, body.get("note", ""), body.get("to") or None)
                elif action == "pr":
                    result = pr_decide(tid, bool(body.get("yes")), body.get("note", ""))
                elif action == "reveal":
                    result = reveal(tid)
                elif action == "hide":
                    result = set_hidden([tid], bool(body.get("hidden", True)))[0]
                elif action == "remove":
                    remove(tid)
                    result = {"ok": True}
                else:
                    return self._send(404, {"error": f"unknown action {action}"})
                self._send(200, result)
            except (WSError, ValueError) as exc:
                self._send(400, {"error": str(exc)})

    return Handler


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def requeue_orphans():
    """Cards a previous dashboard was running when it stopped go back to todo, so the run resumes.

    The next attempt's prompt includes the log and the interrupted run's output, so it continues
    rather than starting over. Cards claimed by an interactive session have no runner_pid and stay put.
    """
    moved = []
    with locked():
        for card in list_cards("doing"):
            pid = card.get("runner_pid")
            if pid and pid != os.getpid() and not _alive(pid):
                agent = card["claimed_by"]
                card.update(to=agent, claimed_by=None, runner_pid=None)
                if is_discussion(card):
                    card["turn"] = agent
                _event(card, "me", "re-queued", "the dashboard stopped while this was running")
                _move(card, "todo")
                moved.append(card)
    for card in moved:
        log("me", f"Re-queued after a dashboard restart: {card['title']}", task=card["id"], status="todo",
            project=card["project"])
    return moved


def serve(port, auto=True):
    ensure_dirs()
    cfg = load_config()
    if auto:
        for card in requeue_orphans():
            print(f"  re-queued interrupted run: {card['title']}")
    runner = Runner(cfg)
    if auto:
        threading.Thread(target=runner.loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(runner, port))
    state = runner.state()
    print(f"Dual workspace: http://127.0.0.1:{port}")
    for a, s in state.items():
        print(f"  {s['label']:<14} {'ready' if s['available'] else 'CLI NOT FOUND'}")
    print("  auto-run " + ("on" if auto else "off") + ". Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- CLI

def print_card(card, full=False):
    print(f"[{card['status']}] {card['id']}")
    print(f"  {card['title']}")
    print(f"  to: {card['to']}  claimed by: {card['claimed_by'] or '-'}  project: {card['project'] or '-'}"
          f"  auto: {'yes' if card['auto'] else 'no'}")
    if card.get("where"):
        print(f"  where: {where_label(card)}" + (f"  (clone: {card['where']['path']})" if card['where']['kind'] == 'github' else ""))
    if full:
        for key in ("details", "need", "result"):
            if card[key]:
                print(f"\n{key.upper()}:\n{card[key]}")
        if card["files"]:
            print("\nFILES:\n" + "\n".join(f"  {f}" for f in card["files"]))
        for i, f in enumerate(card.get("followups", []), 1):
            print(f"\nROUND {i} RESULT:\n{f['result_before']}\n\nFOLLOW-UP {i} ({SHORT.get(f['who'], f['who'])}):\n{f['note']}")
        if card.get("pr"):
            print(f"\nPR: {card['pr']['status']} " + "; ".join(f"{r['path']} ({r['summary']})" for r in card["pr"]["repos"]))
        if card.get("thread"):
            print(f"\nDISCUSSION ({agent_turns(card)} of {card['max_turns']} turns, next: {card.get('turn') or '-'}):")
            for m in card["thread"]:
                print(f"\n  -- {SHORT.get(m['who'], m['who'])}" + (f" ({m['verdict']})" if m.get("verdict") else ""))
                print("  " + (m.get("message") or "").replace("\n", "\n  "))
                if m.get("proposal"):
                    print("  PROPOSAL:\n  " + m["proposal"].replace("\n", "\n  "))
        print("\nHISTORY:")
        for h in card["history"]:
            print(f"  {h['t']} {h['who']}: {h['event']}" + (f" ({h['note']})" if h.get("note") else ""))
        entries = read_log(task=card["id"])
        if entries:
            print("\nLOG:")
            for e in entries:
                print(f"  {e['t']} {e['who']}: {e['msg']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ws.py", description="Dual workspace for Claude Code + ChatGPT Codex")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="dashboard + auto-runner")
    s.add_argument("--port", type=int)
    s.add_argument("--no-auto", action="store_true", help="dashboard only, never start agents")

    s = sub.add_parser("new", help="create a task card")
    s.add_argument("title")
    s.add_argument("--to", default="any", choices=ASSIGNEES)
    s.add_argument("--details", default="")
    s.add_argument("--project", default="")
    s.add_argument("--from", dest="by", default="me", choices=WHO)
    s.add_argument("--manual", action="store_true", help="don't auto-run; wait for someone to claim it")
    s.add_argument("--discuss", action="store_true",
                   help="Claude and Codex take turns until they agree (--to picks who goes first)")
    s.add_argument("--max-turns", type=int, help="turn limit for --discuss before it comes back to you")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--github", metavar="OWNER/NAME", help="work in a fresh clone of this GitHub repo, on a new branch")
    g.add_argument("--folder", metavar="PATH", help="work directly in this folder on your Mac")

    s = sub.add_parser("list", help="list cards")
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--to", choices=ASSIGNEES)

    s = sub.add_parser("show", help="show one card with its history and log")
    s.add_argument("id")

    s = sub.add_parser("next", help="claim the oldest todo card for you")
    s.add_argument("--who", required=True, choices=AGENTS)

    s = sub.add_parser("claim", help="claim a specific todo card")
    s.add_argument("id")
    s.add_argument("--who", required=True, choices=AGENTS)
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("done", help="finish a card")
    s.add_argument("id")
    s.add_argument("--who", required=True, choices=WHO)
    s.add_argument("--result", default="", help="required for normal tasks")
    s.add_argument("--file", action="append", default=[])
    s.add_argument("--repo", action="append", default=[], help="a git repo you changed code in (owner gets asked about a PR)")

    s = sub.add_parser("wait", help="park a card until the owner answers")
    s.add_argument("id")
    s.add_argument("--who", required=True, choices=AGENTS)
    s.add_argument("--need", required=True)

    s = sub.add_parser("reply", help="(owner) answer a waiting card; it goes back to the same AI")
    s.add_argument("id")
    s.add_argument("note")

    s = sub.add_parser("followup", help="(owner) ask for more on a finished card; the same card reopens")
    s.add_argument("id")
    s.add_argument("note")
    s.add_argument("--to", choices=ASSIGNEES, help="defaults to the same AI (discussions: whoever wrote the result)")

    s = sub.add_parser("pr", help="(owner) answer the PR question on a card that changed code")
    s.add_argument("id")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", action="store_true", help="make the PR and push it")
    g.add_argument("--no", action="store_true", help="no PR, mark the card done")
    s.add_argument("--note", default="")

    s = sub.add_parser("hide", help="(owner) hide finished cards from the dashboard (nothing is deleted)")
    s.add_argument("ids", nargs="*")
    s.add_argument("--all", action="store_true", help="every finished card")
    s.add_argument("--undo", action="store_true", help="show them again")

    s = sub.add_parser("handoff", help="(owner) give a card to the other AI")
    s.add_argument("id")
    s.add_argument("--to", required=True, choices=ASSIGNEES)
    s.add_argument("--note", default="")

    s = sub.add_parser("log", help="append to the shared log")
    s.add_argument("--who", required=True, choices=WHO)
    s.add_argument("--msg", required=True)
    s.add_argument("--task")
    s.add_argument("--status")
    s.add_argument("--file", action="append", default=[])

    s = sub.add_parser("tail", help="print the latest log lines")
    s.add_argument("-n", type=int, default=20)

    s = sub.add_parser("run", help="run an agent CLI on a card right now (blocks until it finishes)")
    s.add_argument("id")
    s.add_argument("--agent", choices=AGENTS, help="defaults to the card's assignee")

    a = p.parse_args(argv)
    try:
        if a.cmd == "serve":
            serve(a.port or load_config().get("port", 8766), auto=not a.no_auto)
        elif a.cmd == "new":
            print_card(new_task(a.title, a.to, a.details, a.project, a.by, auto=not a.manual,
                                mode="discuss" if a.discuss else "solo", max_turns=a.max_turns,
                                where={"kind": "github", "repo": a.github} if a.github
                                else {"kind": "folder", "path": a.folder} if a.folder else None))
        elif a.cmd == "list":
            for c in list_cards(a.status):
                if not a.to or c["to"] == a.to:
                    print_card(c)
        elif a.cmd == "show":
            print_card(get(a.id), full=True)
        elif a.cmd == "next":
            card = claim_next(a.who)
            if card:
                card = prepare_where(card)
                print_card(card, full=True)
            else:
                print(f"No todo cards for {a.who}.")
        elif a.cmd == "claim":
            print_card(prepare_where(claim(a.id, a.who, a.force)), full=True)
        elif a.cmd == "done":
            print_card(finish(a.id, a.who, a.result, a.file, a.repo))
        elif a.cmd == "followup":
            print_card(follow_up(a.id, a.note, a.to))
        elif a.cmd == "pr":
            print_card(pr_decide(a.id, a.yes, a.note))
        elif a.cmd == "hide":
            ids = [c["id"] for c in list_cards("done") if a.undo == bool(c.get("hidden"))] if a.all else a.ids
            for c in set_hidden(ids, not a.undo):
                print(("shown: " if a.undo else "hidden: ") + c["title"])
        elif a.cmd == "wait":
            print_card(wait(a.id, a.who, a.need))
        elif a.cmd == "reply":
            print_card(reply(a.id, a.note))
        elif a.cmd == "handoff":
            print_card(reassign(a.id, a.to, a.note))
        elif a.cmd == "log":
            print(json.dumps(log(a.who, a.msg, a.task, a.status, a.file), ensure_ascii=False))
        elif a.cmd == "tail":
            for e in read_log(limit=a.n):
                print(f"{e['t']} {e['who']:<6} {e.get('task', '')[:24]:<24} {e['msg']}")
        elif a.cmd == "run":
            card = get(a.id)
            agent = a.agent or (card["to"] if card["to"] in AGENTS else "claude")
            if card["status"] == "todo":
                card = claim(a.id, agent, force=True)
            elif card["status"] != "doing" or card["claimed_by"] != agent:
                raise WSError(f"{a.id} is {card['status']} (claimed by {card['claimed_by']}); hand it off first")
            print_card(run_card(agent, card, load_config()), full=True)
    except WSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
