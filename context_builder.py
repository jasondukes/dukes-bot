"""
context_builder.py - builds situational context for trusted-contact responses.
Queries Calendar and Reminders to give the model awareness of what the owner
is currently doing before generating a reply.
"""
import os
import subprocess
from datetime import datetime
from actions import VALID_CALENDARS


# ---------------------------------------------------------------------------
# Calendar - events happening now (±2 hours)
# ---------------------------------------------------------------------------

def _get_current_calendar_events() -> str | None:
    _as_cals = "{" + ", ".join(f'"{c}"' for c in sorted(VALID_CALENDARS)) + "}"
    script = f"""
tell application "Calendar"
    set now to current date
    set windowStart to now - 7200
    set windowEnd to now + 7200
    set result to ""
    repeat with calName in {_as_cals}
        try
            set cal to first calendar whose name is calName
            set theEvents to (every event of cal whose start date < windowEnd and end date > windowStart)
            repeat with e in theEvents
                set result to result & (summary of e) & "|"
            end repeat
        end try
    end repeat
    return result
end tell
"""
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=5)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        items = [x.strip() for x in res.stdout.strip().split("|") if x.strip()]
        if not items:
            return None
        return "Currently: " + "; ".join(items)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reminders - due today from Personal list
# ---------------------------------------------------------------------------

def _get_pending_reminders() -> str | None:
    script = """
tell application "Reminders"
    set today to current date
    set todayStart to today - (time of today)
    set todayEnd to todayStart + 86399
    set result to ""
    try
        set rl to list "Personal"
        set theReminders to (every reminder of rl whose completed is false and due date >= todayStart and due date <= todayEnd)
        repeat with r in theReminders
            set result to result & (name of r) & "|"
        end repeat
    end try
    return result
end tell
"""
    try:
        res = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=5)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        items = [x.strip() for x in res.stdout.strip().split("|") if x.strip()]
        if not items:
            return None
        return "Pending reminders: " + ", ".join(items)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_context() -> str | None:
    """
    Returns a context prefix string for injection before generate_reply(),
    or None if nothing useful is available.
    """
    parts = []

    cal = _get_current_calendar_events()
    if cal:
        parts.append(cal)

    rem = _get_pending_reminders()
    if rem:
        parts.append(rem)

    if not parts:
        return None

    return "[Context: " + " | ".join(parts) + "]"
