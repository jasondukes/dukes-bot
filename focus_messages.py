"""
focus_messages.py - generates Focus-aware messages via llama3.2:3b.

Import this module; do not run directly.
OLLAMA_URL is imported from jason_bot at runtime to avoid circular imports;
callers must pass it in, or set it via configure().
"""
import re
import requests

# Caller must invoke configure(url) once at startup, or pass url= to each call.
_OLLAMA_URL    = None
_CLASSIFIER    = "llama3.2:3b"
_MSG_TIMEOUT   = 10  # seconds


def configure(ollama_url: str):
    global _OLLAMA_URL
    _OLLAMA_URL = ollama_url


def _call(system: str, user: str) -> str | None:
    """Single llama3.2:3b call. Returns content string or None on failure."""
    try:
        resp = requests.post(
            _OLLAMA_URL,
            json={
                "model":    _CLASSIFIER,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "stream": False,
            },
            timeout=_MSG_TIMEOUT,
        )
        content = resp.json()["message"]["content"].strip()
        # Strip em-dashes regardless of what the model outputs
        content = content.replace("\u2014", " ").replace("\u2013", " ").replace("--", " ")
        return content if content else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session start
# ---------------------------------------------------------------------------

_SESSION_START_SYSTEM = (
    "Write a single text message, maximum 2 sentences. "
    "Must include: (1) the owner is [driving/asleep/unavailable], (2) this is their bot, "
    "(3) reply STOP to stop. "
    "Nothing else. No offers to help. No lists of features. No enthusiasm. "
    "Just the facts, casual tone. Never use em-dashes."
)

_SESSION_START_FALLBACKS = {
    "Driving":     "Hey, the owner is driving right now. I'm their bot filling in. Reply STOP to wait for the real thing.",
    "Sleep":       "Hey, the owner is asleep. Their bot here. Reply STOP if you'd rather wait.",
    "AutoRespond": "Hey, the owner is unavailable right now. I'm their bot. Reply STOP to wait for them directly.",
}
_SESSION_START_DEFAULT_FALLBACK = (
    "Hey, the owner is not available right now. I'm their bot. Reply STOP to wait for them directly."
)


def generate_session_start(focus_type: str, contact_name: str | None = None,
                           trusted: bool = False) -> str:
    if trusted and contact_name:
        name_hint = (
            f" You're texting {contact_name}, someone the owner knows well - "
            f"a close friend or family member. Use their name naturally and be warm and familiar."
        )
    elif contact_name:
        name_hint = f" The contact's name is {contact_name}; use it naturally once."
    else:
        name_hint = ""

    if focus_type == "Driving":
        user = (
            f"The owner is driving.{name_hint} "
            "Say: (1) they're driving, (2) this is their bot, (3) reply STOP to stop. "
            "Two sentences max. Casual."
        )
    elif focus_type == "Sleep":
        user = (
            f"The owner is asleep.{name_hint} "
            "Say: (1) they're asleep, (2) this is their bot, (3) reply STOP to stop. "
            "Two sentences max. Casual."
        )
    else:
        user = (
            f"The owner is unavailable.{name_hint} "
            "Say: (1) they're unavailable, (2) this is their bot, (3) reply STOP to stop. "
            "Two sentences max. Casual."
        )

    result = _call(_SESSION_START_SYSTEM, user)
    if result:
        return result

    if trusted and contact_name:
        fallbacks = {
            "Driving": f"Hey {contact_name}, the owner is driving. I'm their bot. Reply STOP to wait for the real thing.",
            "Sleep":   f"Hey {contact_name}, the owner is asleep. Their bot here. Reply STOP to wait for them.",
        }
        return fallbacks.get(focus_type,
            f"Hey {contact_name}, the owner is unavailable. I'm their bot. Reply STOP to wait for them directly."
        )

    return _SESSION_START_FALLBACKS.get(focus_type, _SESSION_START_DEFAULT_FALLBACK)


# ---------------------------------------------------------------------------
# Opt-out message (hardcoded, no LLM)
# ---------------------------------------------------------------------------

_OPT_OUT_MESSAGES = {
    "Driving": (
        "The owner is driving right now and will read your message once done. "
        "Reply START anytime to resume AutoReply Bot."
    ),
    "Sleep": (
        "The owner is asleep and will see your message when they wake up. "
        "Reply START anytime to resume AutoReply Bot."
    ),
    "AutoRespond": (
        "The owner is unavailable right now and will follow up when back. "
        "Reply START anytime to resume AutoReply Bot."
    ),
}
_OPT_OUT_DEFAULT = (
    "The owner is not available right now and will follow up when back. "
    "Reply START anytime to resume AutoReply Bot."
)


def generate_opt_out_message(focus_type: str) -> str:
    return _OPT_OUT_MESSAGES.get(focus_type, _OPT_OUT_DEFAULT)


# ---------------------------------------------------------------------------
# About response
# ---------------------------------------------------------------------------

_ABOUT_SYSTEM = (
    "You are the bot owner. Someone asked about your AI chatbot. "
    "Respond in your natural casual texting voice. "
    "Always include ALL required facts. "
    "Keep it to 3-4 short texts worth of content."
)

_ABOUT_USER = (
    "Write a response explaining what this chatbot is. You MUST include ALL of the following facts: "
    "(1) This is an AI built by the owner, running on their home server. "
    "(2) It learned to text like them by training on their own iMessage history. "
    "(3) It does NOT train on or store the sender's messages; only the owner's messages were used. "
    "(4) It does NOT use OpenAI, Anthropic, Claude, ChatGPT, or any cloud AI. "
    "(5) Everything runs locally on the owner's Linux machine at home. "
    "(6) It uses a model called Llama (open source, made by Meta), fine-tuned by the owner. "
    "Sound like you're just casually explaining it to a friend, not writing documentation."
)

_ABOUT_FALLBACK = (
    "Yeah, this is an automated bot running locally on "
    "the owner's home server. It uses a fine-tuned Llama "
    "model to respond during Focus modes. No cloud APIs, "
    "all local. Reply STOP to turn it off."
)


def generate_about_response() -> str:
    result = _call(_ABOUT_SYSTEM, _ABOUT_USER)
    return result if result else _ABOUT_FALLBACK


# ---------------------------------------------------------------------------
# About-intent detection
# ---------------------------------------------------------------------------

_ABOUT_PHRASES = [
    "what are you",
    "are you a bot",
    "are you ai",
    "are you real",
    "who are you",
    "is this a bot",
    "is this ai",
    "are you chatgpt",
    "are you claude",
    "artificial intelligence",
    "how do you work",
    "what is dukesbot",
    "tell me about yourself",
]


def detect_about_intent(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _ABOUT_PHRASES)
