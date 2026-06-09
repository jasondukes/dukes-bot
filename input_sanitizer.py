"""
input_sanitizer.py - centralized input validation and sanitization.
All functions are pure (no I/O, no logging side-effects except via the logger).
"""
import hashlib
import os
import re
import unicodedata
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Control character and Unicode cleanup
# ---------------------------------------------------------------------------

# Control chars below 0x20 except \n (0x0a), \r (0x0d), \t (0x09)
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# Unicode direction overrides, zero-width chars, soft-hyphen, line/paragraph seps
_UNICODE_JUNK_RE = re.compile(
    r'[\u00ad'           # soft hyphen
    r'\u200b-\u200d'     # zero-width space/non-joiner/joiner
    r'\u200e-\u200f'     # LRM / RLM
    r'\u202a-\u202e'     # bidirectional embedding/override chars (incl. U+202E RTL override)
    r'\u2060-\u2069'     # word joiner + bidi isolates
    r'\u2028-\u2029'     # line/paragraph separators
    r'\ufeff]'           # BOM / zero-width no-break space
)

# AppleScript keywords that must never appear in bot output sent via argv
_AS_KEYWORDS_RE = re.compile(
    r'\b(tell\s+application|do\s+shell\s+script|run\s+script|osascript)\b',
    re.IGNORECASE,
)

_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


def strip_control_chars(text: str) -> str:
    """
    Remove control chars, Unicode direction overrides, zero-width chars,
    and apply NFKC normalization. Safe to call on any untrusted string.
    """
    if not text:
        return text
    text = _CTRL_RE.sub('', text)
    text = _UNICODE_JUNK_RE.sub('', text)
    text = unicodedata.normalize('NFKC', text)
    return text


def sanitize_for_applescript(text: str) -> str:
    """
    Sanitize bot output before it is passed as an osascript argv argument.
    (Text is passed as argv, not interpolated into the script string, so
    quote/backslash escaping is NOT done here - that's _sanitize_as_str in actions.py.)
    Removes control chars, direction overrides, and AppleScript injection keywords.
    """
    text = strip_control_chars(text)
    if _AS_KEYWORDS_RE.search(text):
        logger.warning(f"AppleScript keyword in outgoing message, removing: {text!r:.80}")
        text = _AS_KEYWORDS_RE.sub('[removed]', text)
    return text


# ---------------------------------------------------------------------------
# Phone validation
# ---------------------------------------------------------------------------

def validate_phone(phone: str) -> str | None:
    """
    Accept phone numbers and email iMessage handles from chat.db.
    Rejects empty strings, strings with control characters, or junk.
    Returns the original string if valid, None otherwise.
    """
    if not phone or len(phone) > 300:
        return None
    if _CTRL_RE.search(phone) or _UNICODE_JUNK_RE.search(phone):
        return None
    # Email-style iMessage handle (e.g. user@icloud.com)
    if '@' in phone:
        return phone
    # Phone number: must contain at least 7 digits
    digits = re.sub(r'\D', '', phone)
    if len(digits) < 7:
        return None
    return phone


# ---------------------------------------------------------------------------
# URL stripping
# ---------------------------------------------------------------------------

def strip_urls(text: str) -> str:
    """Remove http/https URLs before passing text to Ollama."""
    cleaned = _URL_RE.sub('', text)
    if cleaned != text:
        logger.debug("URLs stripped from incoming message before processing")
    return re.sub(r'\s+', ' ', cleaned).strip()


# ---------------------------------------------------------------------------
# Weather value sanitization
# ---------------------------------------------------------------------------

def sanitize_weather_str(val: str, max_len: int = 50) -> str:
    if not isinstance(val, str):
        return str(val)[:max_len]
    val = re.sub(r'[^\x20-\x7e]', '', val)
    return val[:max_len]


# ---------------------------------------------------------------------------
# Integrity checksum
# ---------------------------------------------------------------------------

def compute_file_checksum(path: str) -> str:
    """Return SHA-256 hex digest of the file at path."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def write_checksum(path: str, checksum: str):
    """Atomically write checksum to path + '.checksum'."""
    checksum_path = path + '.checksum'
    tmp = checksum_path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(checksum)
    os.replace(tmp, checksum_path)


def verify_checksum(path: str) -> bool:
    """
    Return True if checksum matches OR if no checksum file exists yet
    (first-run backward compat). Return False only if checksum file
    exists AND doesn't match.
    """
    checksum_path = path + '.checksum'
    if not os.path.exists(checksum_path):
        return True
    try:
        stored = open(checksum_path).read().strip()
        actual = compute_file_checksum(path)
        return stored == actual
    except Exception:
        return True  # don't block on checksum errors
