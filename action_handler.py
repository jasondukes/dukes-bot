"""
action_handler.py - orchestrates action detection and execution.
"""
import json
import logging
import os
import re
import time
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

from action_detector import detect_action_intent, is_trusted_contact
from actions import (
    create_calendar_event,
    create_reminder,
    add_to_note,
    check_calendar_availability,
    get_weather_for_scheduling,
)

BASE_DIR             = Path(__file__).parent
PENDING_FILE         = BASE_DIR / "pending_actions.json"
PENDING_TTL          = 30 * 60   # 30 minutes

_OLLAMA_URL = None
_MODEL      = "llama3.2:3b"

_CONFIRMATION_WORDS = frozenset([
    "yes", "yep", "yeah", "yup", "sure", "ok", "okay",
    "sounds good", "do it", "go ahead", "perfect", "great",
    "absolutely", "definitely", "please", "yes please",
])


def configure(ollama_url: str):
    global _OLLAMA_URL
    _OLLAMA_URL = ollama_url


# ---------------------------------------------------------------------------
# Pending-action persistence
# ---------------------------------------------------------------------------

def _load_pending() -> dict:
    try:
        with open(PENDING_FILE) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            logger.critical(f"pending_actions.json: unexpected root type {type(raw).__name__}, resetting")
            return {}
        now       = time.time()
        validated = {}
        required  = {"action_type", "details", "created_at", "expires_at"}
        for phone, entry in raw.items():
            if not isinstance(entry, dict):
                logger.warning(f"pending_actions: invalid entry for {phone!r}, skipping")
                continue
            if not required.issubset(entry.keys()):
                logger.warning(f"pending_actions: entry for {phone!r} missing keys, skipping")
                continue
            # Discard entries that expired more than 60 minutes ago
            if entry.get("expires_at", 0) < now - 3600:
                logger.debug(f"pending_actions: stale entry for {phone!r}, discarding")
                continue
            validated[phone] = entry
        return validated
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.critical(f"pending_actions.json parse failure: {e}, resetting to empty")
        return {}


def _save_pending(data: dict):
    tmp = str(PENDING_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PENDING_FILE)


def _store_pending(phone: str, action: dict):
    pending = _load_pending()
    pending[phone] = {
        **action,
        "created_at": time.time(),
        "expires_at": time.time() + PENDING_TTL,
    }
    _save_pending(pending)


def _pop_pending(phone: str) -> dict | None:
    pending = _load_pending()
    entry   = pending.pop(phone, None)
    if entry:
        _save_pending(pending)
    return entry


def expire_pending_actions() -> list[dict]:
    """
    Remove and return all pending actions older than PENDING_TTL.
    Caller should create a reminder for each returned item.
    """
    now     = time.time()
    pending = _load_pending()
    expired = []
    for phone, entry in list(pending.items()):
        if entry.get("expires_at", 0) < now:
            expired.append({"phone": phone, **entry})
            del pending[phone]
    if expired:
        _save_pending(pending)
    return expired


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _llm(user_prompt: str, system_prompt: str = "") -> str | None:
    if not _OLLAMA_URL:
        return None
    try:
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": user_prompt})
        resp = requests.post(
            _OLLAMA_URL,
            json={"model": _MODEL, "messages": msgs, "stream": False},
            timeout=10,
        )
        return resp.json()["message"]["content"].strip() or None
    except Exception:
        return None


def _generate_clarifying_question(ambiguous_reason: str) -> str:
    reply = _llm(f"Ask a brief clarifying question about: {ambiguous_reason}")
    if reply:
        return reply
    return f"Just to clarify: {ambiguous_reason}?"


def _generate_success_message(action_type: str, details: dict,
                               contact_name: str | None = None) -> str:
    title = details.get("title") or details.get("note") or "that"
    reply = _llm(
        f"Confirm in one casual sentence that you just completed a {action_type} action for: '{title}'.",
        system_prompt=(
            "You are the bot owner. Write a single casual text message confirmation. "
            "Keep it brief and natural. No em-dashes."
        ),
    )
    if reply:
        return reply
    fallbacks = {
        "calendar": f"Done! Added '{title}' to the calendar.",
        "reminder": f"Got it, reminder set for '{title}'.",
        "note":     f"Added to the note!",
    }
    return fallbacks.get(action_type, "Done!")


# ---------------------------------------------------------------------------
# Internal logic helpers
# ---------------------------------------------------------------------------

def _is_confirmation(text: str) -> bool:
    return text.strip().lower() in _CONFIRMATION_WORDS


def _is_weekend_date(date_hint: str) -> bool:
    from actions import _parse_date_hint
    dt = _parse_date_hint(date_hint)
    return dt is not None and dt.weekday() >= 5


def _weather_note(date_hint: str) -> str:
    """Return a weather context string if relevant, else empty string."""
    w = get_weather_for_scheduling(date_hint)
    if "error" in w:
        return ""
    parts = [f"{w['temp_high']}°F"]
    if w["condition"] != "sunny":
        parts.append(w["condition"])
    if w["rain_chance"] > 30:
        parts.append(f"{w['rain_chance']}% rain chance")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def handle_action(phone: str, text: str,
                  contact_name: str | None,
                  focus_type: str | None) -> str | None:
    """
    Returns a reply string if an action was handled, or None to fall through
    to normal generate_reply processing.
    """
    # 1. Trusted contacts only
    if not is_trusted_contact(phone):
        return None

    # 2. Check for pending confirmation first
    pending = _load_pending()
    if phone in pending and _is_confirmation(text):
        entry      = _pop_pending(phone)
        action_type = entry.get("action_type")
        details     = entry.get("details", {})

        if action_type == "calendar":
            result = create_calendar_event(
                title            = details.get("title", "Event"),
                date_str         = details.get("date_hint", ""),
                duration_minutes = details.get("duration_minutes", 60),
                calendar         = details.get("calendar", "Personal"),
            )
        elif action_type == "reminder":
            result = create_reminder(
                title     = details.get("title", "Reminder"),
                due_date  = details.get("date_hint"),
                list_name = details.get("list_name", "Personal"),
            )
        elif action_type == "note":
            result = add_to_note(
                note_name = details.get("list_name", ""),
                items     = details.get("items", []),
            )
        else:
            return None

        if result["success"]:
            return _generate_success_message(action_type, result["details"] or details,
                                             contact_name)
        # Action failed → fall back reminder
        create_reminder(
            title     = f"Follow-up needed: {details.get('title', 'action')}",
            list_name = "Personal",
        )
        return "Noted! I'll make sure the owner sees this when they're back."

    # 3. Detect action intent
    action = detect_action_intent(text)
    if action is None:
        return None

    action_type = action.get("action_type")
    details     = action.get("details", {})

    # 4. Ambiguous → ask clarifying question and store pending
    if details.get("is_ambiguous"):
        reason   = details.get("ambiguous_reason", "the request")
        question = _generate_clarifying_question(reason)
        _store_pending(phone, {
            "action_type": action_type,
            "details":     details,
            "status":      "awaiting_clarification",
        })
        return question

    # 5. Calendar actions
    if action_type == "calendar":
        date_hint = details.get("date_hint", "")
        avail     = check_calendar_availability(date_hint)

        weather_note = ""
        if date_hint and _is_weekend_date(date_hint):
            weather_note = _weather_note(date_hint)

        title       = details.get("title") or "Event"
        time_label  = date_hint or "that time"

        if not avail["available"]:
            next_slot = avail["suggested_slots"][0] if avail["suggested_slots"] else "another time"
            _store_pending(phone, {
                "action_type": action_type,
                "details":     {**details, "date_hint": next_slot},
                "status":      "awaiting_confirmation",
            })
            return f"Looks like you're busy then. Want me to try {next_slot}?"

        # Available → ask confirmation
        confirm_msg = f"I can block {time_label} for '{title}', does that work?"
        if weather_note:
            confirm_msg += f" (Weather: {weather_note})"
        _store_pending(phone, {
            "action_type": action_type,
            "details":     details,
            "status":      "awaiting_confirmation",
        })
        return confirm_msg

    # 6. Reminder actions - create immediately
    if action_type == "reminder":
        result = create_reminder(
            title     = details.get("title", "Reminder"),
            due_date  = details.get("date_hint"),
            list_name = details.get("list_name", "Personal"),
        )
        if result["success"]:
            return _generate_success_message("reminder", result["details"] or details,
                                             contact_name)
        create_reminder(title=f"Follow-up needed: {details.get('title', 'reminder')}")
        return "Noted! I'll make sure the owner sees this when they're back."

    # 7. Note actions
    if action_type == "note":
        items     = details.get("items") or []
        note_name = details.get("list_name", "")

        if not items or not note_name:
            reason   = details.get("ambiguous_reason") or "which note and what items to add"
            question = _generate_clarifying_question(reason)
            _store_pending(phone, {
                "action_type": action_type,
                "details":     details,
                "status":      "awaiting_clarification",
            })
            return question

        result = add_to_note(note_name, items)
        if result["success"]:
            # Also create a reminder if this looks like a task
            if "to do" in note_name.lower() or "task" in note_name.lower():
                for item in items:
                    create_reminder(title=item, list_name="Personal")
            return _generate_success_message("note", result["details"] or details,
                                             contact_name)
        create_reminder(title=f"Follow-up needed: add to {note_name}")
        return "Noted! I'll make sure the owner sees this when they're back."

    # 8. Weather check standalone
    if action_type == "weather_check":
        date_hint = details.get("date_hint", "today")
        w = get_weather_for_scheduling(date_hint)
        if "error" not in w:
            cond = w["condition"]
            temp = w["temp_high"]
            rain = w["rain_chance"]
            return f"Looking like {cond} and {temp}°F, {rain}% chance of rain."
        return None

    return None
