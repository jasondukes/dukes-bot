"""
action_detector.py - detects calendar/reminder/note action intent and
manages the trusted-contact cache.
"""
import json
import logging
import os
import re
import sqlite3
import requests
from actions import VALID_NOTES

logger = logging.getLogger(__name__)

BASE_DIR           = os.path.expanduser("~/jason-bot")
TRUSTED_CACHE_FILE = os.path.join(BASE_DIR, "trusted_contacts.json")
ADDRESSBOOK_BASE   = os.path.expanduser("~/Library/Application Support/AddressBook/Sources")

# ---------------------------------------------------------------------------
# Contact name definitions
# ---------------------------------------------------------------------------

# Full name → short name for contacts requiring surname disambiguation.
# Use lowercase "firstname lastname" keys to prevent matching unrelated contacts
# who share a first name. Replace these with your own family/household contacts.
TRUSTED_FULL_NAMES: dict[str, str] = {
    # "contact full name":        "Name1",    # e.g. "jane doe": "Jane"
    # "contact full name":        "Name2",    # e.g. "john doe jr": "John"
    # "your own full name":      "Me",        # owner's own number for testing
}

# First-name-only matching for contacts that are unambiguous in your address book.
# Replace with your own trusted contacts' first names.
TRUSTED_FIRST_NAMES: list[str] = [
    # "Alice", "Bob",
]

# All trusted first names (for external consumers)
TRUSTED_NAMES: list[str] = list(TRUSTED_FULL_NAMES.values()) + TRUSTED_FIRST_NAMES

# Phones not in AddressBook that should be trusted (manual overrides).
# Populate via JASON_BOT_TRUST_OVERRIDES env var if needed.
# Format: "10digitphone:Name,10digitphone:Name"
MANUAL_TRUST_OVERRIDES: dict[str, str] = {}

# Add short names here that receive full voice responses during Focus mode.
# e.g. frozenset({"Partner", "Contact1", "Me"})
FOCUS_RESPONSE_NAMES: frozenset[str] = frozenset({"Me"})

# Add short names here that MUST resolve at startup; bot exits loudly if missing.
# e.g. frozenset({"Partner", "Contact1"})
REQUIRED_CONTACTS: frozenset[str] = frozenset()

_OLLAMA_URL = None
_MODEL      = "llama3.2:3b"
_TIMEOUT    = 10

# normalized phone (10 digits) -> first name
_trusted_cache: dict[str, str] = {}


def configure(ollama_url: str):
    global _OLLAMA_URL
    _OLLAMA_URL = ollama_url


# ---------------------------------------------------------------------------
# Trusted-contact cache
# ---------------------------------------------------------------------------

def _normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) >= 10 else digits


def _build_trusted_contacts() -> dict[str, str]:
    """
    Query AddressBook for trusted contacts.
    Uses full-name matching (ZFIRSTNAME + ZLASTNAME) for contacts requiring surname
    disambiguation to prevent ambiguity with other people sharing the same first name.
    Uses first-name-only matching for other unambiguous contacts.
    Returns {norm_10digit_phone: first_name}.
    """
    result: dict[str, str] = {}

    try:
        for source_dir in os.listdir(ADDRESSBOOK_BASE):
            db_path = os.path.join(ADDRESSBOOK_BASE, source_dir, "AddressBook-v22.abcddb")
            if not os.path.exists(db_path):
                continue
            try:
                conn = sqlite3.connect(db_path)
                c    = conn.cursor()

                # --- Full-name query for surname-disambiguated contacts ---
                full_name_keys = list(TRUSTED_FULL_NAMES.keys())
                placeholders   = ",".join("?" * len(full_name_keys))
                c.execute(f"""
                    SELECT p.ZFULLNUMBER,
                           COALESCE(p.ZLABEL, '') AS label,
                           LOWER(TRIM(r.ZFIRSTNAME || ' ' || COALESCE(r.ZLASTNAME, ''))) AS full_name
                    FROM ZABCDPHONENUMBER p
                    JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                    WHERE LOWER(TRIM(r.ZFIRSTNAME || ' ' || COALESCE(r.ZLASTNAME, '')))
                          IN ({placeholders})
                      AND p.ZFULLNUMBER IS NOT NULL
                """, full_name_keys)

                per_name: dict[str, list[tuple[str, str]]] = {}
                for full_number, label, full_name in c.fetchall():
                    norm = _normalize_phone(full_number)
                    if norm:
                        per_name.setdefault(full_name, []).append((norm, label))

                for full_name, phone_entries in per_name.items():
                    short_name = TRUSTED_FULL_NAMES[full_name]
                    if len(phone_entries) > 1:
                        phones_only = [p for p, _ in phone_entries]
                        logger.warning(
                            f"Multiple numbers for {full_name!r}: {phones_only}; "
                            "registering all, verify the correct one in Contacts"
                        )
                    for norm, _ in phone_entries:
                        result[norm] = short_name

                # --- First-name query for other trusted contacts ---
                if TRUSTED_FIRST_NAMES:
                    fn_ph = ",".join("?" * len(TRUSTED_FIRST_NAMES))
                    c.execute(f"""
                        SELECT p.ZFULLNUMBER, r.ZFIRSTNAME
                        FROM ZABCDPHONENUMBER p
                        JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                        WHERE r.ZFIRSTNAME IN ({fn_ph})
                          AND p.ZFULLNUMBER IS NOT NULL
                    """, TRUSTED_FIRST_NAMES)
                    for full_number, first_name in c.fetchall():
                        norm = _normalize_phone(full_number)
                        if norm and first_name and norm not in result:
                            result[norm] = first_name

                conn.close()
            except Exception as e:
                logger.debug(f"AddressBook query error for {db_path}: {e}")
                continue
    except Exception as e:
        logger.debug(f"AddressBook source enumeration error: {e}")

    result.update(MANUAL_TRUST_OVERRIDES)
    return result


def load_trusted_contacts():
    """Rebuild trusted cache from AddressBook at every startup for accuracy."""
    global _trusted_cache
    _trusted_cache = _build_trusted_contacts()
    try:
        with open(TRUSTED_CACHE_FILE, "w") as f:
            json.dump(_trusted_cache, f, indent=2)
    except Exception as e:
        logger.debug(f"Failed to save trusted contacts cache: {e}")


def refresh_trusted_contacts():
    load_trusted_contacts()


def validate_trusted_contacts():
    """
    Verify all required contacts from REQUIRED_CONTACTS are in the cache.
    Raises SystemExit if any required contact is missing.
    """
    found   = set(_trusted_cache.values())
    missing = REQUIRED_CONTACTS - found
    if missing:
        logger.critical(
            f"REQUIRED contacts not resolved from AddressBook: {sorted(missing)}. "
            "Check that the configured trusted contacts exist in Contacts "
            "with those exact full names."
        )
        raise SystemExit(f"Missing required contacts: {sorted(missing)}")

    logger.debug(
        f"Trusted contacts resolved: "
        f"{len([p for p in _trusted_cache if _trusted_cache[p] in FOCUS_RESPONSE_NAMES])} "
        f"focus-response, {len(_trusted_cache)} total"
    )


def is_trusted_contact(phone: str) -> bool:
    norm = _normalize_phone(phone)
    return norm in _trusted_cache


def is_focus_response_contact(phone: str) -> bool:
    """True if this contact receives voice responses during Focus mode."""
    norm = _normalize_phone(phone)
    name = _trusted_cache.get(norm)
    return name in FOCUS_RESPONSE_NAMES


# ---------------------------------------------------------------------------
# Action intent detection
# ---------------------------------------------------------------------------

_NOTES_OPTIONS = " | ".join(f'"{n}"' for n in sorted(VALID_NOTES))
_SYSTEM_PROMPT = (
    "Analyze this text message and determine if it is requesting a calendar event, "
    "reminder, or note action. Return ONLY valid JSON or the word NONE.\n\n"
    "Required JSON format:\n"
    "{\n"
    '  "action_type": "calendar" | "reminder" | "note" | "weather_check" | "none",\n'
    '  "confidence": 0.0-1.0,\n'
    '  "details": {\n'
    '    "title": "extracted title or null",\n'
    '    "date_hint": "extracted date/time hint or null",\n'
    f'    "list_name": {_NOTES_OPTIONS} | null,\n'
    '    "items": ["item1", "item2"] or null,\n'
    '    "is_ambiguous": true | false,\n'
    '    "ambiguous_reason": "explanation if ambiguous or null"\n'
    "  }\n"
    "}\n\n"
    "Rules:\n"
    "- confidence >= 0.7 only if the message clearly requests an action\n"
    "- is_ambiguous = true if the time, date, or intent is unclear\n"
    "- For notes, list_name must match one of the listed options exactly or be null\n"
    "- Return the word NONE (not JSON) if there is no action request\n"
)


def detect_action_intent(text: str) -> dict | None:
    """
    Returns parsed action dict if confidence >= 0.7, else None.
    """
    if not _OLLAMA_URL:
        return None

    try:
        resp = requests.post(
            _OLLAMA_URL,
            json={
                "model":    _MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": text},
                ],
                "stream": False,
            },
            timeout=_TIMEOUT,
        )
        content = resp.json()["message"]["content"].strip()

        if content.upper() == "NONE" or not content:
            return None

        content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
        content = re.sub(r'\s*```\s*$',       '', content, flags=re.MULTILINE)

        match = re.search(r'\{.*\}', content, re.DOTALL)
        if not match:
            return None

        parsed = json.loads(match.group(0))

        if parsed.get("action_type") == "none":
            return None
        if float(parsed.get("confidence", 0)) < 0.7:
            return None

        if "details" not in parsed:
            parsed["details"] = {}

        return parsed

    except Exception:
        return None
