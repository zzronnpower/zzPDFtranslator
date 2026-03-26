from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime

from app.config import settings


def _ensure_file(path: str, title: str, intro: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{intro}\n")


def ensure_dev_tracking_files() -> None:
    agents_dir = os.path.join(settings.project_root, "agents")
    chatlog_dir = os.path.join(settings.project_root, "chatlog")
    os.makedirs(agents_dir, exist_ok=True)
    os.makedirs(chatlog_dir, exist_ok=True)
    os.makedirs(settings.logs_dir, exist_ok=True)

    _ensure_file(
        os.path.join(agents_dir, "README.md"),
        "Agents",
        "This folder stores project agent notes and execution hints.",
    )
    _ensure_file(
        os.path.join(chatlog_dir, "CHATLOG.md"),
        "Chatlog",
        "Auto-updated high-level development session notes.",
    )
    _ensure_file(
        os.path.join(settings.logs_dir, "code_changes.log"),
        "Code Change Log",
        "Auto-updated snapshots when code state changes.",
    )


def _run_git(args: list[str]) -> str:
    try:
        out = subprocess.check_output(
            ["git", *args],
            cwd=settings.project_root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""


def _append_markdown_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def record_code_snapshot_if_changed(event: str = "app_startup") -> None:
    ensure_dev_tracking_files()
    status = _run_git(["status", "--porcelain"])
    staged_stat = _run_git(["diff", "--cached", "--shortstat"])
    unstaged_stat = _run_git(["diff", "--shortstat"])
    head = _run_git(["rev-parse", "--short", "HEAD"])
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    state_hash = hashlib.sha256(f"{head}\n{status}\n{staged_stat}\n{unstaged_stat}".encode("utf-8")).hexdigest()

    state_file = os.path.join(settings.logs_dir, ".code_state")
    previous_hash = ""
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            previous_hash = f.read().strip()

    if previous_hash == state_hash:
        return

    now = datetime.utcnow().isoformat() + "Z"
    status_lines = len([line for line in status.splitlines() if line.strip()])
    staged_text = staged_stat or "0 files changed"
    unstaged_text = unstaged_stat or "0 files changed"
    entry = (
        f"- {now} | event={event} | branch={branch or 'unknown'} | head={head or 'none'} | "
        f"changed_paths={status_lines} | staged=({staged_text}) | unstaged=({unstaged_text})\n"
    )

    _append_markdown_line(os.path.join(settings.logs_dir, "code_changes.log"), entry)

    chatlog_entry = (
        f"- {now} `{event}` on `{branch or 'unknown'}` (`{head or 'none'}`), "
        f"changed paths: {status_lines}.\n"
    )
    _append_markdown_line(os.path.join(settings.project_root, "chatlog", "CHATLOG.md"), chatlog_entry)

    with open(state_file, "w", encoding="utf-8") as f:
        f.write(state_hash)
