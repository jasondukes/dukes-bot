"""
security_filter.py - prompt injection detection and model output sanitization.
"""
import re
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Leetspeak normalizer
# ---------------------------------------------------------------------------

_LEET_MAP = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's',
    '7': 't', '8': 'b', '@': 'a', '$': 's', '!': 'i',
    '+': 't', '|': 'i',
})


def normalize_text(text: str) -> str:
    return text.translate(_LEET_MAP).lower()


# ---------------------------------------------------------------------------
# Injection patterns
# ---------------------------------------------------------------------------

_RAW_PATTERNS = [
    r'ignore\s+(all\s+)?previous\s+instructions?',
    r'ignore\s+your\s+instructions?',
    r'disregard\s+your\s+previous',
    r'forget\s+your\s+instructions?',
    r'you\s+are\s+now\b',
    r'your\s+new\s+instructions?\s+are',
    r'pretend\s+you\s+are',
    r'act\s+as\s+if\s+you\s+have\s+no\s+restrictions?',
    r'\bjailbreak\b',
    r'\bdan\s+mode\b',
    r'\bdeveloper\s+mode\b',
    r'\bunrestricted\s+mode\b',
    r'bypass\s+your\s+filters?',
    r'override\s+your\b',
    r'your\s+true\s+self',
    r'ignore\s+the\s+above',
    r'system\s+prompt',
    r'you\s+are\s+an\s+ai\s+without',
    r'from\s+now\s+on\s+you\s+will',
    r'new\s+persona',
    r'roleplay\s+as',
    r'disregard\s+(all\s+)?previous',
    r'forget\s+(everything|all)\s+(you|i)',
    r'your\s+instructions?\s+have\s+(changed|been\s+(updated|replaced|overridden))',
    r'i\s+am\s+your\s+(creator|developer|owner|trainer)',
    r'admin\s+(override|access|mode)',
    r'maintenance\s+mode',
    r'debug\s+mode',
    r'override\s+(all\s+)?safety',
    r'disable\s+(your\s+)?(safety|filters?|restrictions?)',
    r'act\s+without\s+(any\s+)?(restrictions?|constraints?|limits?)',
    r'no\s+restrictions?\s+mode',
    r'you\s+have\s+no\s+(rules?|restrictions?|constraints?)',
    r'ignore\s+(all\s+)?(rules?|guidelines?|constraints?)',
]

_INJECTION_RE = re.compile(
    '|'.join(_RAW_PATTERNS),
    re.IGNORECASE,
)

# Unusual Unicode: soft hyphen, zero-width chars, RTL override, etc.
_INVISIBLE_RE = re.compile(
    r'[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u2028\u2029]'
)

# Long message with "instructions" or "rules" - classic system-prompt injection
_LONG_INSTRUCTIONS_RE = re.compile(
    r'\b(instructions?|rules?)\b',
    re.IGNORECASE,
)


def is_injection_attempt(text: str) -> bool:
    # check raw text for invisible Unicode before normalization
    if _INVISIBLE_RE.search(text):
        return True

    # Normalize for leetspeak before pattern matching
    normalized = normalize_text(text)

    if _INJECTION_RE.search(normalized):
        return True

    if len(text) > 500 and _LONG_INSTRUCTIONS_RE.search(normalized):
        return True

    return False


def get_injection_response() -> str:
    return "Nice try! I'm the owner's bot and I only do what I'm configured to do. What's up?"


# ---------------------------------------------------------------------------
# Response-side sanitization
# ---------------------------------------------------------------------------

_OUTPUT_RED_FLAGS = re.compile(
    r'^i\s+(am|will)\s+now\b'
    r'|as\s+an\s+unrestricted\s+ai'
    r'|my\s+true\s+purpose'
    r'|\bDAN\b'
    r'|without\s+restrictions',
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_model_output(text: str) -> str:
    """Replace model reply with canned response if manipulation red flags detected."""
    if _OUTPUT_RED_FLAGS.search(text):
        logger.warning(f"Model output manipulation detected, replacing: {text!r:.120}")
        return get_injection_response()
    return text


# ---------------------------------------------------------------------------
# Hallucination detection
# ---------------------------------------------------------------------------

_HALLUCINATION_RE = re.compile(
    r'\bremember when\b'
    r'|\blast time\b'
    r'|\byou told me\b'
    r'|\bshe said\b|\bhe said\b|\bthey said\b'
    r'|\bshe told me\b|\bhe told me\b'
    r'|\byou mentioned\b'
    r'|\bwe were\b|\bwe went\b|\bwe had\b',
    re.IGNORECASE,
)

_NAMED_PERSON_RE = re.compile(r'\b[A-Z][a-z]{2,}\b')


def contains_hallucinated_specifics(reply: str, incoming_text: str,
                                    context: str = "") -> bool:
    """
    True if the reply invents specific details not present in the incoming
    message or context: past events, attributions, or named people who
    weren't mentioned.
    """
    if _HALLUCINATION_RE.search(reply):
        return True

    # Check for named people in reply not present in incoming or context
    source = (incoming_text + " " + (context or "")).lower()
    names_in_reply = _NAMED_PERSON_RE.findall(reply)
    for name in names_in_reply:
        if name.lower() not in source:
            logger.debug(f"Hallucinated name detected: {name!r} not in source")
            return True

    return False
