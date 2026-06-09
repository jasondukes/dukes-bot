"""
actions.py - AppleScript-backed Calendar, Reminders, Notes, and weather actions.
All functions return {"success": bool, "message": str, "details": dict}.
"""
import os
import re
import subprocess
from datetime import datetime, timedelta

import requests

VALID_CALENDARS  = set(
    os.environ.get("BOT_CALENDARS", "Personal,Work,Family,Other").split(",")
)
VALID_NOTES      = set(
    os.environ.get("BOT_NOTES",
        "Grocery List,To Do List,Watch List,Home List").split(",")
)
HOUR_MIN, HOUR_MAX = 9, 20   # 9 AM – 8 PM


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize_as_str(s: str) -> str:
    """Escape backslashes and double-quotes for safe AppleScript string interpolation."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _run_applescript(script: str, timeout: int = 15) -> tuple[bool, str]:
    """Run an AppleScript string. Returns (success, stdout_or_stderr)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, result.stderr.strip()
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"AppleScript timeout after {timeout}s"


def _as_date_str(dt: datetime) -> str:
    return dt.strftime("%-m/%-d/%Y %I:%M:%S %p")


def _parse_date_hint(hint: str) -> datetime | None:
    """
    Convert a natural-language date hint to a datetime.
    Returns None if unparseable.
    """
    if not hint:
        return None

    hint_l = hint.lower().strip()
    now    = datetime.now()
    today  = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- Extract time ---
    hour, minute = 12, 0   # default noon

    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', hint_l, re.IGNORECASE)
    if time_match:
        hour   = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if time_match.group(3).lower() == 'pm' and hour != 12:
            hour += 12
        elif time_match.group(3).lower() == 'am' and hour == 12:
            hour = 0
    else:
        bare_time = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?(?!\s*[ap]m)', hint_l)
        if bare_time:
            hour   = int(bare_time.group(1))
            minute = int(bare_time.group(2) or 0)

    if 'noon' in hint_l:
        hour, minute = 12, 0
    elif 'midnight' in hint_l:
        hour, minute = 0, 0
    elif 'morning' in hint_l and not time_match:
        hour = 9
    elif 'afternoon' in hint_l and not time_match:
        hour = 14
    elif ('evening' in hint_l or 'tonight' in hint_l) and not time_match:
        hour = 18

    # --- Extract date ---
    base: datetime | None = None

    if 'today' in hint_l or 'tonight' in hint_l:
        base = today
    elif 'tomorrow' in hint_l:
        base = today + timedelta(days=1)
    else:
        day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        for i, day in enumerate(day_names):
            if day in hint_l:
                days_ahead = i - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                base = today + timedelta(days=days_ahead)
                break

    if base is None:
        # Try "April 18", "Apr 18", etc.
        months = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'june': 6, 'july': 7, 'august': 8, 'september': 9,
            'october': 10, 'november': 11, 'december': 12,
        }
        for mname, mnum in months.items():
            m = re.search(rf'\b{mname}\s+(\d{{1,2}})\b', hint_l)
            if m:
                try:
                    candidate = today.replace(month=mnum, day=int(m.group(1)))
                    if candidate < today:
                        candidate = candidate.replace(year=today.year + 1)
                    base = candidate
                    break
                except ValueError:
                    pass

    if base is None:
        # Try M/D or MM/DD
        m = re.search(r'\b(\d{1,2})/(\d{1,2})\b', hint_l)
        if m:
            try:
                candidate = today.replace(month=int(m.group(1)), day=int(m.group(2)))
                if candidate < today:
                    candidate = candidate.replace(year=today.year + 1)
                base = candidate
            except ValueError:
                pass

    if base is None:
        return None

    result = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Enforce 9 AM – 8 PM
    if result.hour < HOUR_MIN:
        result = result.replace(hour=HOUR_MIN, minute=0)
    elif result.hour >= HOUR_MAX:
        result = result.replace(hour=HOUR_MAX - 1, minute=0)

    return result


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def check_calendar_availability(date_str: str, duration_minutes: int = 60) -> dict:
    """
    Returns {"available": bool, "conflicts": [str, ...], "suggested_slots": [str, ...]}
    """
    dt = _parse_date_hint(date_str)
    if dt is None:
        return {"available": True, "conflicts": [], "suggested_slots": []}

    dt_end    = dt + timedelta(minutes=duration_minutes)
    start_str = _as_date_str(dt)
    end_str   = _as_date_str(dt_end)

    _as_cals = "{" + ", ".join(f'"{_sanitize_as_str(c)}"' for c in sorted(VALID_CALENDARS)) + "}"
    script = f"""
tell application "Calendar"
    set startTime to date "{start_str}"
    set endTime to date "{end_str}"
    set conflictData to ""
    repeat with calName in {_as_cals}
        try
            set cal to first calendar whose name is calName
            set theEvents to (every event of cal whose start date < endTime and end date > startTime)
            repeat with e in theEvents
                set conflictData to conflictData & (summary of e) & "|" & ((start date of e) as text) & "~"
            end repeat
        end try
    end repeat
    return conflictData
end tell
"""
    ok, output = _run_applescript(script)
    if not ok:
        return {"available": True, "conflicts": [], "suggested_slots": []}

    conflicts = []
    if output:
        for entry in output.split("~"):
            entry = entry.strip()
            if entry:
                parts = entry.split("|", 1)
                conflicts.append(parts[0] if parts else entry)

    suggested = []
    if conflicts:
        for delta_h in (1, 2, 24):
            candidate = dt + timedelta(hours=delta_h)
            if HOUR_MIN <= candidate.hour < HOUR_MAX:
                suggested.append(candidate.strftime("%-I:%M %p" + (", %A" if delta_h == 24 else "")))

    return {
        "available": len(conflicts) == 0,
        "conflicts": conflicts,
        "suggested_slots": suggested,
    }


def create_calendar_event(title: str, date_str: str,
                          duration_minutes: int = 60,
                          calendar: str = "Personal") -> dict:
    if calendar not in VALID_CALENDARS:
        calendar = "Personal"

    dt = _parse_date_hint(date_str)
    if dt is None:
        return {"success": False, "message": f"Couldn't parse date: {date_str!r}", "details": {}}

    # Enforce hour bounds
    if dt.hour < HOUR_MIN:
        return {"success": False, "message": f"Can't schedule before {HOUR_MIN} AM.", "details": {}}
    if dt.hour >= HOUR_MAX:
        return {"success": False, "message": f"Can't schedule after {HOUR_MAX - 12} PM.", "details": {}}

    avail = check_calendar_availability(date_str, duration_minutes)
    if not avail["available"]:
        next_avail = avail["suggested_slots"][0] if avail["suggested_slots"] else "another time"
        return {
            "success":      False,
            "message":      f"Looks like you're busy then. Want me to try {next_avail}?",
            "details":      {"conflicts": avail["conflicts"]},
            "conflict":     True,
            "next_available": next_avail,
        }

    dt_end    = dt + timedelta(minutes=duration_minutes)
    start_str = _as_date_str(dt)
    end_str   = _as_date_str(dt_end)

    safe_title    = _sanitize_as_str(title)
    safe_calendar = _sanitize_as_str(calendar)

    script = f"""
tell application "Calendar"
    set targetCalendar to first calendar whose name is "{safe_calendar}"
    set startDate to date "{start_str}"
    set endDate to date "{end_str}"
    tell targetCalendar
        make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate}}
    end tell
end tell
"""
    ok, out = _run_applescript(script)
    if ok:
        friendly = dt.strftime("%-I:%M %p on %A, %B %-d")
        return {
            "success": True,
            "message": f"Added '{title}' at {friendly}.",
            "details": {"datetime": dt.isoformat(), "calendar": calendar},
        }
    return {"success": False, "message": f"Calendar error: {out}", "details": {}}


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def create_reminder(title: str, due_date: str | None = None,
                    list_name: str = "Personal") -> dict:
    safe_title = _sanitize_as_str(title)
    safe_list  = _sanitize_as_str(list_name)

    if due_date:
        dt = _parse_date_hint(due_date)
        if dt:
            due_str = _as_date_str(dt)
            script = f"""
tell application "Reminders"
    set targetList to list "{safe_list}"
    set newReminder to make new reminder at end of reminders of targetList with properties {{name:"{safe_title}", due date:date "{due_str}"}}
    set alarmList to {{make new reminder alarm at end of alarms of newReminder with properties {{trigger offset:minutes to date ("{due_str}") - (current date)}}}}
end tell
"""
        else:
            # Couldn't parse date; create without due date
            due_date = None

    if not due_date:
        script = f"""
tell application "Reminders"
    set targetList to list "{safe_list}"
    make new reminder at end of reminders of targetList with properties {{name:"{safe_title}"}}
end tell
"""

    ok, out = _run_applescript(script)
    if ok:
        msg = f"Reminder set: '{title}'"
        if due_date:
            msg += f" (due {due_date})"
        return {"success": True, "message": msg, "details": {"list": list_name}}
    return {"success": False, "message": f"Reminders error: {out}", "details": {}}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def add_to_note(note_name: str, items: list[str]) -> dict:
    if note_name not in VALID_NOTES:
        return {
            "success": False,
            "message": f"'{note_name}' is not a recognised note. Valid: {', '.join(sorted(VALID_NOTES))}",
            "details": {},
        }
    if not items:
        return {"success": False, "message": "No items to add.", "details": {}}

    safe_name    = _sanitize_as_str(note_name)
    html_bullets = "".join(f"<br>- {_sanitize_as_str(item)}" for item in items)

    script = f"""
tell application "Notes"
    set theNote to first note of default account whose name is "{safe_name}"
    set body of theNote to (body of theNote) & "{html_bullets}"
end tell
"""
    ok, out = _run_applescript(script)
    if ok:
        item_list = ", ".join(f"'{i}'" for i in items)
        return {
            "success": True,
            "message": f"Added {item_list} to {note_name}.",
            "details": {"note": note_name, "items": items},
        }
    return {"success": False, "message": f"Notes error: {out}", "details": {}}


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

_WMO_TO_CONDITION = {
    0: "sunny",
    1: "sunny", 2: "cloudy", 3: "cloudy",
    45: "cloudy", 48: "cloudy",
}


def _wmo_condition(code: int) -> str:
    return _WMO_TO_CONDITION.get(code, "rainy")


def get_weather_for_scheduling(date_str: str) -> dict:
    """
    Returns {"date": "YYYY-MM-DD", "condition": "sunny|cloudy|rainy",
             "temp_high": int, "rain_chance": int} or {"error": str}.
    Uses Open-Meteo (no API key needed).
    """
    dt = _parse_date_hint(date_str)
    if dt is None:
        return {"error": f"Couldn't parse date: {date_str!r}"}

    target_date = dt.strftime("%Y-%m-%d")

    lat = os.environ.get('BOT_LATITUDE', '0.0')
    lon = os.environ.get('BOT_LONGITUDE', '0.0')
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,precipitation_probability_max"
        "&temperature_unit=fahrenheit"
        f"&timezone={os.environ.get('BOT_TIMEZONE', 'UTC')}"
    )
    try:
        resp = requests.get(url, timeout=8)
        data = resp.json()
        daily = data.get("daily", {})
        times  = daily.get("time", [])
        if target_date not in times:
            return {"error": f"No forecast available for {target_date}"}
        idx = times.index(target_date)
        try:
            condition = _wmo_condition(int(daily["weathercode"][idx]))
            temp_high = max(-100, min(150, int(float(daily["temperature_2m_max"][idx]))))
            rain_pct  = max(0, min(100, int(float(daily["precipitation_probability_max"][idx]))))
        except (ValueError, TypeError, IndexError) as exc:
            return {"error": f"Unexpected weather data format: {exc}"}
        return {
            "date":        target_date,
            "condition":   condition,
            "temp_high":   temp_high,
            "rain_chance": rain_pct,
        }
    except Exception as e:
        return {"error": str(e)}
