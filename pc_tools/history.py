"""Change history: every action is logged here, and settings changes can be undone."""

import json
import os
from datetime import datetime


HISTORY_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")), "PCAssistant", "history.json"
)

# Undo logic lives next to the action it reverses. Each module registers a handler for its own kind
# of change with @undo_handler("kind"); a handler takes the saved undo data and returns
# (message, completed). completed=False means the user still has to finish it (e.g. reinstall an app).
UNDO_HANDLERS = {}


def undo_handler(kind):
    def register(func):
        UNDO_HANDLERS[kind] = func
        return func
    return register


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp, HISTORY_FILE)


def record_change(description, undo=None):
    """Log an action. `undo` is data for undo_change(), or None if the action can't be undone."""
    history = load_history()
    history.append({
        "id": (max((c["id"] for c in history), default=0) + 1),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": description,
        "undo": undo,
        "undone": False,
    })
    _save_history(history[-200:])


def get_change_history(count=15):
    history = load_history()
    if not history:
        return "No changes have been made by PC Assistant yet."
    lines = ["Changes made by PC Assistant (newest first):"]
    for c in reversed(history[-max(1, min(int(count or 15), 50)):]):
        if c.get("undone"):
            status = f"undone {c['undone']}" if isinstance(c["undone"], str) else "undone"
        elif c.get("undo"):
            status = "can be undone"
        else:
            status = "can't be undone"
        lines.append(f"  #{c['id']} [{c['time']}] {c['description']} ({status})")
    return "\n".join(lines)


def undo_change(change_id):
    history = load_history()
    change = next((c for c in history if c["id"] == int(change_id)), None)
    if change is None:
        return f"No change #{change_id} in the history."
    if change.get("undone"):
        return f"Change #{change_id} was already undone."
    undo = change.get("undo")
    if not undo:
        return f"Change #{change_id} ({change['description']}) can't be undone automatically."

    handler = UNDO_HANDLERS.get(undo["type"])
    if handler is None:
        return f"Don't know how to undo change #{change_id}."
    message, completed = handler(undo)
    if not completed:
        return message

    change["undone"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_history(history)
    return f"Undid change #{change_id}: {message}"
