"""
Local Windows Toast reminders for overdue/due-today tasks (architecture_v2.md
§Phase 7.4). Purely local -- winotify calls into the Windows notification
API directly, no network, no external service.

A reminder-check failure (winotify missing, toast API error, malformed
todo.md date) must never crash the dashboard process this runs inside --
every public function here degrades to "no toast fired" rather than raising.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp_server.todo import TodoItem, parse_todo

logger = logging.getLogger(__name__)

REMINDERS_FILE = "reminders_sent.json"
_RENOTIFY_AFTER_HOURS = 24


def load_sent_reminders(data_dir: Path | str) -> dict[str, str]:
    """Load reminders_sent.json ({task_id: last_notification_iso}). Returns {} if
    not found or malformed -- a corrupt tracking file must not block reminders."""
    path = Path(data_dir) / REMINDERS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("reminders_sent.json is unreadable; treating as empty.")
        return {}


def save_sent_reminder(data_dir: Path | str, task_id: str, sent_at: str) -> None:
    """Atomically update task_id's last-notified timestamp."""
    data_dir = Path(data_dir)
    path = data_dir / REMINDERS_FILE
    sent = load_sent_reminders(data_dir)
    sent[task_id] = sent_at
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(sent, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _parse_due_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_due_tasks(todo_path: Path | str) -> list[dict]:
    """Return open (not done/deleted/blocked) tasks whose due_date is today or
    earlier, sorted oldest-overdue first.

    Returns:
        List of dicts (not TodoItem instances, so this is directly JSON-
        serialisable / passable to fire_toast without further conversion).
    """
    todo = parse_todo(todo_path)
    today = date.today()
    due: list[tuple[date, TodoItem]] = []
    for item in todo.items:
        if item.done or item.status in ("done", "deleted", "blocked"):
            continue
        if not item.due_date:
            continue
        parsed = _parse_due_date(item.due_date)
        if parsed is None or parsed > today:
            continue
        due.append((parsed, item))
    due.sort(key=lambda pair: pair[0])
    return [
        {"id": item.id, "description": item.description, "due_date": item.due_date,
         "priority": item.priority, "status": item.status}
        for _, item in due
    ]


def fire_toast(task: dict) -> bool:
    """Fire a Windows Toast notification for a due/overdue task.

    Returns:
        True on success, False if winotify is not available or the call
        raises for any other reason -- never propagates the exception.
    """
    try:
        from winotify import Notification

        priority = task.get("priority") or "MEDIUM"
        description = (task.get("description") or "")[:80]
        notif = Notification(
            app_id="Meeting Agent",
            title=f"Task Due: {priority}",
            msg=description,
            duration="short",
        )
        notif.show()
        return True
    except ImportError:
        logger.warning("winotify is not installed; skipping toast notification.")
        return False
    except Exception as exc:  # noqa: BLE001 - a notification failure must never crash the caller
        logger.warning("fire_toast failed for task %s: %s", task.get("id"), exc)
        return False


def check_and_notify(data_dir: Path | str, todo_path: Path | str) -> int:
    """Fire a toast for each due task not already notified within the last
    `_RENOTIFY_AFTER_HOURS`. Returns the count of notifications actually fired.
    """
    data_dir = Path(data_dir)
    due_tasks = get_due_tasks(todo_path)
    if not due_tasks:
        return 0

    sent = load_sent_reminders(data_dir)
    now = datetime.now()
    fired = 0

    for task in due_tasks:
        task_id = task.get("id")
        if not task_id:
            continue
        last_sent_raw = sent.get(task_id)
        if last_sent_raw:
            try:
                last_sent = datetime.fromisoformat(last_sent_raw)
                if now - last_sent < timedelta(hours=_RENOTIFY_AFTER_HOURS):
                    continue
            except ValueError:
                pass  # malformed timestamp -- treat as never notified

        if fire_toast(task):
            fired += 1
        save_sent_reminder(data_dir, task_id, now.isoformat())

    return fired
