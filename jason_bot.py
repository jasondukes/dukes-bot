#!/usr/bin/env python3
import sqlite3
import os
import stat
import sys
import time
import re
import json
import random
import atexit
import signal
import logging
import subprocess
import requests
from logging.handlers import RotatingFileHandler
from session_state import SessionState
from message_batcher import MessageBatcher
import focus_messages
import action_handler
from action_detector import (
    load_trusted_contacts, is_trusted_contact,
    is_focus_response_contact,
    validate_trusted_contacts,
    configure as configure_action_detector,
)
from actions import create_reminder
from context_builder import get_current_context
from input_sanitizer import (
    strip_control_chars, sanitize_for_applescript,
    validate_phone, strip_urls,
)
from safety_filter import (
    is_controversial,
    is_asking_bot_opinion_about_person,
    is_impairment_question,
    contains_impairment_content,
    get_controversy_deflection,
    get_impairment_deflection,
    get_opinion_deflection,
)
from security_filter import (
    is_injection_attempt,
    get_injection_response,
    sanitize_model_output,
    contains_hallucinated_specifics,
)

# ---------------------------------------------------------------------------
# Logging - stdout + rotating file, level from LOG_LEVEL env var
# ---------------------------------------------------------------------------

LOG_FILE = os.path.expanduser("~/jason-bot/jason_bot.log")
_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "DEBUG").upper(), logging.DEBUG)

logger = logging.getLogger("jason_bot")
logger.setLevel(_log_level)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_fmt)

logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)

# restrict log file to owner read/write only
try:
    os.chmod(LOG_FILE, stat.S_IRUSR | stat.S_IWUSR)
except Exception:
    pass


OLLAMA_URL = os.environ.get(
    "OLLAMA_URL",
    "http://localhost:11434/api/chat"
)
MY_PHONE = os.environ.get("BOT_MY_PHONE", "+1XXXXXXXXXX")

focus_messages.configure(OLLAMA_URL)
action_handler.configure(OLLAMA_URL)
configure_action_detector(OLLAMA_URL)
load_trusted_contacts()
validate_trusted_contacts()

OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL",     "gemma4:31b")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "gemma4:31b")

# Replace [OWNER_NAME] and [OWNER_DESCRIPTION] with the bot owner's name and persona.
BOT_SYSTEM_PROMPT = (
    "You are [OWNER_NAME], [OWNER_DESCRIPTION]. "
    "You text casually and naturally. Keep messages short: 1-3 sentences. "
    "You text casually. Use contractions and informal language. "
    "Avoid corporate speak and formal punctuation. "
    "You ask follow-up questions. No em-dashes. No formal "
    "punctuation. Never sign your name. Never write as anyone else. You are "
    "texting with someone whose identity you do not know; do NOT assume "
    "you are talking to your partner or family members unless they identify "
    "themselves first. Do NOT use terms of endearment with people you don't know."
)
POLL_INTERVAL    = 3
DB_PATH          = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_BASE = os.path.expanduser("~/Library/Application Support/AddressBook/Sources")
LOCKFILE         = "/tmp/jason_bot.lock"

LAST_READ = -1

conversation_histories = {}
seen_rowids        = set()
contact_name_cache = {}
sent_messages      = {}   # text -> sent_at epoch float

SENT_DEDUP_WINDOW = 30   # seconds - ignore our own echoes within this window

# per-contact rate limiting
_msg_timestamps:    dict[str, list[float]] = {}
_rate_blocked_until: dict[str, float]      = {}
_RATE_WINDOW    = 60.0
_RATE_WARN      = 10
_RATE_BLOCK     = 20
_RATE_BLOCK_DUR = 300.0  # 5-minute block


def _check_rate(phone: str) -> bool:
    """Return True if message should be processed; False if rate-limited."""
    now = time.time()
    if _rate_blocked_until.get(phone, 0) > now:
        return False
    timestamps = _msg_timestamps.get(phone, [])
    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
    timestamps.append(now)
    _msg_timestamps[phone] = timestamps
    count = len(timestamps)
    if count > _RATE_BLOCK:
        _rate_blocked_until[phone] = now + _RATE_BLOCK_DUR
        logger.critical(f"Rate-limit BLOCK: contact sent {count} msgs/60s, 5-min block applied")
        return False
    if count > _RATE_WARN:
        logger.warning(f"Rate-limit WARNING: contact sent {count} msgs/60s, skipping")
        return False
    return True


# Replace with the owner's actual distinctive slang words
SLANG_WORDS = ["OWNER_SLANG_1", "OWNER_SLANG_2", "OWNER_SLANG_3"]

EMOJI_FALLBACKS = [
    "Haha pretty good! What's up?",
    "Not bad! What's going on?",
    "Doing well! What's up with you?",
    "Good! What are you up to?",
]

UNKNOWN_FALLBACKS = [
    "Good! What's up?",
    "Not bad! What's going on with you?",
    "Doing pretty well. What's up?",
]

_VALID_INTENTS = frozenset({
    "greeting_opener", "greeting_checkin", "farewell", "casual_question",
    "casual_chat", "personal_question", "plan_making", "logistics",
    "recommendation", "emotional_support", "gratitude", "family_update",
    "work_talk", "humor", "news_reaction", "unknown",
})
_VALID_TONES = frozenset({"casual", "urgent", "playful", "serious", "sad", "excited"})

_INVENTED_FACT_RE = re.compile(
    r'\b(email I sent|calendar I sent|I texted you about this|as I mentioned)\b',
    re.IGNORECASE,
)
_CONTRADICT_RE = re.compile(
    r'\b(messed up|broken|doesn\'?t work|not working).{0,60}\bbut\b.{0,60}\b(drive|use|using|works?|working|fixed)\b'
    r'|\b(works?|working|fixed).{0,60}\bbut\b.{0,60}\b(messed up|broken|doesn\'?t work|not working)\b',
    re.IGNORECASE,
)

CLASSIFIER_SYSTEM_PROMPT = (
    'You are a message classifier. Analyze the incoming text message and return ONLY a JSON object with these fields:\n'
    '- intent: one of [\n'
    '    greeting_opener,  greeting_checkin,  farewell,\n'
    '    casual_question,  casual_chat,       personal_question,\n'
    '    plan_making,      logistics,         recommendation,\n'
    '    emotional_support,gratitude,         family_update,\n'
    '    work_talk,        humor,             news_reaction,\n'
    '    unknown\n'
    '  ]\n'
    '- tone: one of [casual, urgent, playful, serious, sad, excited]\n'
    '- use_name: true if it would feel natural to address the sender by name in the reply '
    '(only true for greetings, emotional messages, or personal topics), false otherwise\n\n'
    'Intent definitions:\n'
    '- greeting_opener: saying hello, starting a conversation ("hey", "hi", "yo")\n'
    '- greeting_checkin: asking how someone is ("what\'s up?", "how are you?", "how\'s it going?")\n'
    '- farewell: ending the conversation ("bye", "talk later", "take care")\n'
    '- casual_question: genuine info-seeking that isn\'t personal ("did you see X?", "what do you think of Y?")\n'
    '- casual_chat: small talk, random observations, statements that invite a reaction\n'
    '- personal_question: asking about the other person\'s life, health, or feelings\n'
    '- plan_making: scheduling, coordinating, proposing to meet up\n'
    '- logistics: practical info (addresses, timing, directions, confirmations)\n'
    '- recommendation: asking for or giving a suggestion\n'
    '- emotional_support: sharing feelings, problems, or seeking comfort\n'
    '- gratitude: thanking someone\n'
    '- family_update: news about relatives, household, or home life\n'
    '- work_talk: job, career, meetings, or work stress\n'
    '- humor: jokes, funny observations, teasing, memes\n'
    '- news_reaction: reacting to something in the news or pop culture\n\n'
    'Important examples - classify these exactly as shown:\n'
    '- "I need to talk to you" → emotional_support\n'
    '- "I need to talk to you about something" → emotional_support\n'
    '- "can we talk" → emotional_support\n'
    '- "something happened" → emotional_support\n'
    '- "are you okay" → personal_question\n'
    '- "you okay?" → personal_question\n\n'
    'Return only valid JSON, no other text.'
)


# ---------------------------------------------------------------------------
# Single-instance lockfile
# ---------------------------------------------------------------------------

def _release_lock():
    try:
        os.unlink(LOCKFILE)
    except Exception:
        pass


def _handle_signal(signum, frame):
    _release_lock()
    sys.exit(0)


def acquire_lock():
    if os.path.exists(LOCKFILE):
        old_pid = None
        is_running = False
        try:
            with open(LOCKFILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            is_running = True
        except PermissionError:
            is_running = True
        except (ValueError, ProcessLookupError):
            pass

        if is_running:
            logger.error(f"Another instance is already running (PID {old_pid}). Exiting.")
            sys.exit(1)
        else:
            logger.debug(f"Removing stale lockfile (PID {old_pid})")

    with open(LOCKFILE, "w") as f:
        f.write(str(os.getpid()))

    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)


# ---------------------------------------------------------------------------
# AddressBook lookup
# ---------------------------------------------------------------------------

def _normalize_phone(phone):
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) >= 10 else digits


def get_contact_name(phone):
    if phone in contact_name_cache:
        return contact_name_cache[phone]

    normalized = _normalize_phone(phone)

    # Fast path: use the trusted cache (rebuilt from AddressBook at startup)
    from action_detector import _trusted_cache
    if normalized in _trusted_cache:
        name = _trusted_cache[normalized]
        contact_name_cache[phone] = name
        return name

    # Slow path: general AddressBook scan for non-trusted contacts
    name = None
    try:
        for source_dir in os.listdir(ADDRESSBOOK_BASE):
            db_path = os.path.join(ADDRESSBOOK_BASE, source_dir, "AddressBook-v22.abcddb")
            if not os.path.exists(db_path):
                continue
            try:
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute("""
                    SELECT p.ZFULLNUMBER, r.ZFIRSTNAME
                    FROM ZABCDPHONENUMBER p
                    JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
                    WHERE r.ZFIRSTNAME IS NOT NULL
                """)
                for full_number, first_name in c.fetchall():
                    if _normalize_phone(full_number or '') == normalized:
                        name = first_name
                        break
                conn.close()
            except Exception:
                continue
            if name:
                break
    except Exception as e:
        logger.debug(f"contact lookup error: {e}")

    contact_name_cache[phone] = name
    return name


# ---------------------------------------------------------------------------
# Stage 1 - intent classifier
# ---------------------------------------------------------------------------

_DICT_WORDS = None


def _load_dict():
    global _DICT_WORDS
    if _DICT_WORDS is None:
        try:
            with open('/usr/share/dict/words') as f:
                _DICT_WORDS = {w.strip().lower() for w in f}
        except Exception:
            _DICT_WORDS = set()
    return _DICT_WORDS


def _looks_garbled(text):
    word_list = _load_dict()
    if not word_list:
        return False
    words = re.findall(r'[a-zA-Z]{5,}', text)
    if not words:
        return False
    unknown = [w for w in words if w.lower() not in word_list]
    return len(unknown) > 0


_CHECKIN_RE = re.compile(
    r'^(what\'?s up|whats up|what up|what\'?s going on|whats going on'
    r'|how are you|how\'?s it going|how is it going|how you doing'
    r'|how\'?s everything|hows everything|how have you been|how\'?s life'
    r'|how\'?s things|how are things)\b',
    re.IGNORECASE,
)


def _greeting_subtype(text):
    if _CHECKIN_RE.match(text.strip()):
        return "greeting_checkin"
    return "greeting_opener"


def classify_message(text):
    clean_text   = strip_control_chars(text)
    user_content = clean_text
    if _looks_garbled(clean_text):
        user_content = ("Note: this message may contain typos or autocorrect errors. "
                        "Classify based on overall intent.\n") + clean_text
        logger.debug("garbled text detected, adding typo note to classifier")

    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "stream": False,
    }
    for attempt in range(2):
        try:
            response = requests.post(OLLAMA_URL, json=payload, timeout=15)
            content = response.json()["message"]["content"].strip()
            if not content:
                raise ValueError("empty response from classifier")
            content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
            content = re.sub(r'\s*```\s*$',       '', content, flags=re.MULTILINE)
            result  = json.loads(content.strip())
            # Validate fields to prevent prompt injection via classifier output
            if result.get("intent") not in _VALID_INTENTS:
                result["intent"] = "unknown"
            if result.get("tone") not in _VALID_TONES:
                result["tone"] = "casual"
            result["use_name"] = bool(result.get("use_name", False))
            return result
        except Exception as e:
            logger.debug(f"classifier error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                time.sleep(5)
    return None


def build_context_string(classifier_result, contact):
    intent   = classifier_result.get("intent", "unknown")
    tone     = classifier_result.get("tone", "casual")
    use_name = classifier_result.get("use_name", False)

    logger.debug(f"classifier: intent={intent}, tone={tone}, use_name={use_name}")

    context = f"Respond as the owner would to a {intent} message with {tone} tone. Keep it casual, brief, and natural."

    if use_name:
        name = get_contact_name(contact)
        if name:
            context += f" You're texting {name}."

    context += (
        " Ask a short follow-up question at the end unless the message is clearly a closing "
        "statement (goodbye, thanks, ok, sounds good)."
    )

    return context


# ---------------------------------------------------------------------------
# Stage 2 - response generator
# ---------------------------------------------------------------------------

def generate_reply(contact, message_text, context_str=None):
    if contact not in conversation_histories:
        conversation_histories[contact] = []

    conversation_histories[contact].append({
        "role": "user",
        "content": message_text,
    })

    # scan recent turns for injected instructions before calling model
    recent_user_text = " ".join(
        m["content"] for m in conversation_histories[contact][-5:]
        if m["role"] == "user"
    )
    if is_injection_attempt(recent_user_text):
        logger.critical(f"Injection pattern in conversation history for {contact}, clearing history")
        conversation_histories[contact] = []
        return None

    recent = conversation_histories[contact][-10:]

    messages = [{"role": "system", "content": BOT_SYSTEM_PROMPT}]
    if context_str:
        messages.append({"role": "system", "content": context_str})
    messages.extend(recent)

    try:
        response = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
        }, timeout=180)
        reply = response.json()["message"]["content"].strip()
        reply = strip_control_chars(reply)
        reply = clean_reply(reply)

        history_reply = "I need a sec to think about that." if _INVENTED_FACT_RE.search(reply) else reply
        if history_reply != reply:
            logger.debug(f"invented fact detected, neutralizing history entry: {reply!r:.80}")
        conversation_histories[contact].append({
            "role": "assistant",
            "content": history_reply,
        })
        if len(conversation_histories[contact]) > 20:
            conversation_histories[contact] = conversation_histories[contact][-20:]

        if _CONTRADICT_RE.search(reply):
            but_idx = reply.lower().find(' but ')
            if but_idx > 0:
                reply = reply[:but_idx].rstrip(' ,') + '.'
                logger.debug(f"self-contradiction detected, truncated at 'but': {reply!r:.80}")

        return reply
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return None


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

def limit_slang(text):
    # Normalize all word-boundary haha variants to lowercase "haha"
    text = re.sub(r'\b(?:ha){2,}\b', 'haha', text, flags=re.IGNORECASE)
    # Keep only the first occurrence if multiple survive
    lower = text.lower()
    first = lower.find('haha')
    if first != -1:
        second = lower.find('haha', first + 4)
        while second != -1:
            text = text[:second] + text[second + 4:]
            lower = text.lower()
            second = lower.find('haha', first + 4)

    for word in SLANG_WORDS:
        if "'" in word:
            pattern = re.compile(r'(?<!\w)' + re.escape(word) + r'(?!\w)', re.IGNORECASE)
        else:
            pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            for match in reversed(matches[1:]):
                text = text[:match.start()] + text[match.end():]
    return re.sub(r'\s+', ' ', text).strip()


def cap_length(text, max_chars=200):
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    last_sentence = max(window.rfind('.'), window.rfind('!'), window.rfind('?'))
    if last_sentence > 0:
        return text[:last_sentence + 1]
    last_space = window.rfind(' ')
    truncated = window[:last_space] if last_space > 0 else window
    return truncated if truncated[-1] in '.!?' else truncated + '.'


def cap_exclamations(text, max_excl=2):
    parts = text.split('!')
    if len(parts) - 1 <= max_excl:
        return text
    result = parts[0]
    for i, part in enumerate(parts[1:], 1):
        if i <= max_excl:
            result += '!' + part
        else:
            result += ('.' if part.strip() else '') + part
    return result


def clean_reply(text):
    text = text.replace("\u2014", " ").replace("\u2013", " ").replace("--", " ")
    text = re.sub(r'Reacted\s+\S+\s+to\s+"[^"]*"', '', text)
    text = re.sub(r'(Liked|Loved|Laughed at|Emphasized|Questioned|Disliked)\s+"[^"]*"', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if text and not re.search(r'[a-zA-Z]', text):
        return random.choice(EMOJI_FALLBACKS)
    text = cap_exclamations(text)
    text = limit_slang(text)
    text = cap_length(text)
    return text


# ---------------------------------------------------------------------------
# Unknown-contact safety filter
# ---------------------------------------------------------------------------

_SANITIZE_NAMES_RE = re.compile(
    r'\b(familymember1|familymember2|familymember3)\b',
    re.IGNORECASE,
)
_SANITIZE_ENDEARMENT_RE = re.compile(
    r'\b(sweetheart|honey|babe|baby|my love)\b',
    re.IGNORECASE,
)
_SANITIZE_IMPERATIVE_RE = re.compile(
    r'\bcan you (get them|put them to bed|check on|pick (them|up))\b',
    re.IGNORECASE,
)

UNKNOWN_EMOTIONAL_FALLBACKS = [
    "Sorry, can we pick this up in a bit?",
    "Yeah. Let me think on that.",
    "Fair enough. Give me a sec.",
]


def sanitize_for_unknown(text, emotional_context=False):
    triggered = (
        _SANITIZE_NAMES_RE.search(text)
        or _SANITIZE_ENDEARMENT_RE.search(text)
        or _SANITIZE_IMPERATIVE_RE.search(text)
    )
    if triggered:
        logger.debug(f"sanitize_for_unknown triggered, replacing reply: {text!r:.80}")
        if emotional_context:
            return random.choice(UNKNOWN_EMOTIONAL_FALLBACKS)
        return random.choice(UNKNOWN_FALLBACKS)
    return text


# ---------------------------------------------------------------------------
# Bot tagging - every outgoing message gets " #bot" appended
# ---------------------------------------------------------------------------

def tag_as_bot(text: str) -> str:
    if not text.endswith("#bot"):
        return text.rstrip() + " #bot"
    return text


# ---------------------------------------------------------------------------
# iMessage send
# ---------------------------------------------------------------------------

def send_imessage(phone, text):
    text = sanitize_for_applescript(text)  # strip control chars and AppleScript injection keywords
    text = tag_as_bot(text)                # always last - no message ever goes out untagged
    script = """
on run argv
    set textMessage to item 1 of argv
    set targetBuddy to item 2 of argv
    tell application "Messages"
        set targetService to id of 1st account whose service type = iMessage
        set theBuddy to participant targetBuddy of account id targetService
        send textMessage to theBuddy
    end tell
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, text, phone],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.error(f"send_imessage error: {result.stderr.strip()}")
        else:
            sent_messages[text] = time.time()
            logger.info(f"Sent to {phone}: {text[:80]}...")
    except subprocess.TimeoutExpired:
        logger.warning(f"send_imessage timeout for {phone}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_last_rowid():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        c.execute("SELECT MAX(ROWID) FROM message")
        row = c.fetchone()
        conn.close()
        return row[0] if row and row[0] else 0
    except Exception as e:
        logger.error(f"get_last_rowid failed: {e}")
        return 0


def get_new_messages(after_rowid):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT
            m.ROWID,
            m.text,
            m.is_from_me,
            m.date,
            h.id AS contact,
            MIN(cmj.chat_id) AS chat_id,
            m.is_from_me AS raw_is_from_me
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE m.ROWID > ?
          AND m.text IS NOT NULL
          AND m.text != ''
          AND m.is_from_me = 0
        GROUP BY m.ROWID
        ORDER BY m.ROWID ASC
    """, (after_rowid,))
    rows = c.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _check_group_chat(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM chat_handle_join WHERE chat_id = ?", (chat_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


def _process_contact(contact, text, session_state):
    """Run safety checks → classify → generate_reply → sanitize → send."""
    contact_name      = get_contact_name(contact)
    focus_response_ok = is_focus_response_contact(contact)

    if not focus_response_ok:
        logger.debug(f"Non-focus-response contact {contact} ({contact_name or 'unknown'}), total silence")
        return

    text = strip_urls(text)
    if not text:
        return

    emotional_context = False

    # 0. Prompt injection check — always first
    if is_injection_attempt(text):
        logger.warning(f"Injection attempt from {contact}: {text!r:.60}")
        send_imessage(contact, get_injection_response())
        session_state.update_last_message(contact)
        return

    # 1a. Safety: impairment question
    if is_impairment_question(text):
        logger.info(f"Impairment question from {contact}, deflecting")
        send_imessage(contact, get_impairment_deflection())
        session_state.update_last_message(contact)
        return

    # 1b. Safety: opinion about a person
    if is_asking_bot_opinion_about_person(text):
        logger.info(f"Person-opinion request from {contact}, deflecting")
        send_imessage(contact, get_opinion_deflection())
        session_state.update_last_message(contact)
        return

    # 2. Safety: controversial topic
    if is_controversial(text):
        logger.info(f"Controversial content from {contact}, deflecting")
        send_imessage(contact, get_controversy_deflection())
        session_state.update_last_message(contact)
        return

    # 3. About-intent short-circuit
    if focus_messages.detect_about_intent(text):
        logger.info(f"About-intent detected for {contact}")
        send_imessage(contact, focus_messages.generate_about_response())
        session_state.update_last_message(contact)
        return

    # 4. Action handler - focus-response contacts are all trusted
    focus_type   = session_state.get_focus().get("focus") if session_state else None
    action_reply = action_handler.handle_action(contact, text, contact_name, focus_type)
    if action_reply is not None:
        logger.info(f"Action handled for {contact} ({contact_name}): {action_reply!r:.80}")
        send_imessage(contact, action_reply)
        session_state.update_last_message(contact)
        return

    # 5. Build situational context (calendar/reminders/recent messages)
    situation_context = None
    try:
        situation_context = get_current_context()
        if situation_context:
            logger.debug(f"Context injected for {contact_name}: {len(situation_context)} chars")
    except Exception as e:
        logger.debug(f"Context build failed: {e}")

    context_str       = None
    classifier_result = classify_message(text)
    if classifier_result:
        raw_intent = classifier_result.get("intent", "unknown")
        if raw_intent == "greeting":
            classifier_result["intent"] = _greeting_subtype(text)
        context_str = build_context_string(classifier_result, contact)
        emotional_context = (
            classifier_result.get("intent") == "emotional_support"
            or classifier_result.get("tone") == "serious"
        )
        logger.debug(f"context_str: {context_str!r}")
    else:
        logger.debug("classifier failed, proceeding without context")

    if situation_context:
        context_str = situation_context + "\n" + (context_str or "")

    reply = generate_reply(contact, text, context_str)
    logger.debug(f"generate_reply returned: {reply[:80]!r}")
    if not reply:
        logger.warning("No reply generated.")
        return

    reply = sanitize_model_output(reply)

    if contains_impairment_content(reply):
        logger.warning(f"Model output contained impairment content, replacing: {reply!r:.80}")
        reply = get_impairment_deflection()

    if contains_hallucinated_specifics(reply, text, situation_context or ""):
        logger.warning(f"Hallucinated specifics detected, replacing: {reply!r:.80}")
        reply = random.choice(UNKNOWN_FALLBACKS)

    if is_controversial(reply):
        logger.warning(f"Model generated controversial content, replaced: {reply!r:.80}")
        reply = get_controversy_deflection()

    # Strip family names, endearments, and household imperatives before sending.
    reply = sanitize_for_unknown(reply, emotional_context)

    logger.info(f"reply to {contact_name}: {reply[:80]!r}")
    send_imessage(contact, reply)
    session_state.update_last_message(contact)


def _app_is_running(app_name: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to return (name of processes) contains "{app_name}"'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().lower() == "true"
    except subprocess.TimeoutExpired:
        return False


def _launch_app_silently(app_name: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{app_name}" to launch'],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set visible of process "{app_name}" to false'],
            capture_output=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        pass


def _wait_for_app(app_name: str, test_script: str, timeout: int = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["osascript", "-e", test_script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5)
    return False


_APP_TESTS = {
    "Calendar":  'tell application "Calendar" to return name of first calendar',
    "Reminders": 'tell application "Reminders" to return name of first list',
    "Notes":     'tell application "Notes" to return name of first account',
}


def ensure_apps_running():
    for app_name, test_script in _APP_TESTS.items():
        if not _app_is_running(app_name):
            logger.info(f"{app_name} not running, launching silently")
            _launch_app_silently(app_name)
        ready = _wait_for_app(app_name, test_script, timeout=5)
        if ready:
            logger.info(f"{app_name} is ready")
        else:
            logger.warning(f"{app_name} not available; scheduling actions may fail")


def main():
    global LAST_READ

    ensure_apps_running()
    acquire_lock()

    LAST_READ = get_last_rowid()
    logger.info(f"DukesBot started (PID {os.getpid()}). Watching for messages from {MY_PHONE} only.")
    logger.info(f"Starting from ROWID {LAST_READ}")

    session_state = SessionState()
    batcher       = MessageBatcher()

    while True:
        try:
            # --- Purge sent-echo window ---
            cutoff = time.time() - SENT_DEDUP_WINDOW
            for k in list(sent_messages.keys()):
                if sent_messages[k] < cutoff:
                    del sent_messages[k]

            # --- Expire pending actions ---
            for expired in action_handler.expire_pending_actions():
                title = expired.get("details", {}).get("title", "action")
                create_reminder(title=f"Follow up needed: {title}", list_name="Personal")
                logger.info(f"Expired pending action converted to reminder: {title!r}")

            if not session_state.is_bot_active():
                logger.debug("Focus inactive, bot idle")
                time.sleep(POLL_INTERVAL)
                continue

            # --- Receive new messages ---
            new_messages = get_new_messages(LAST_READ)
            if new_messages:
                LAST_READ = max(msg["ROWID"] for msg in new_messages)

            for msg in new_messages:
                # sqlite3.Row.ROWID may arrive as None or non-int
                try:
                    rowid = int(msg["ROWID"])
                except (TypeError, ValueError):
                    logger.warning(f"Invalid ROWID {msg['ROWID']!r}, skipping")
                    continue

                text    = msg["text"] or ""
                contact = msg["contact"] or ""

                # validate handle before any processing
                if not validate_phone(contact):
                    logger.warning(f"Invalid contact handle {contact!r}, skipping")
                    continue

                text = strip_control_chars(text)
                if not text:
                    continue

                _preview = "[redacted]" if is_trusted_contact(contact) else repr(text[:60])
                logger.debug(f"ROWID={rowid} is_from_me={msg['raw_is_from_me']} text={_preview}")

                # drop oversized messages before any processing
                if len(text) > 5000:
                    logger.warning(f"Oversized message ({len(text)} chars), skipping")
                    continue
                if len(text) > 1000:
                    logger.warning(f"Long message ({len(text)} chars), truncating to 1000")
                    text = text[:1000]

                # Dedup guards
                if rowid in seen_rowids:
                    logger.debug(f"Skipping already-seen ROWID {rowid}")
                    continue
                seen_rowids.add(rowid)
                if len(seen_rowids) > 1000:
                    seen_rowids.difference_update(sorted(seen_rowids)[:500])

                if text in sent_messages:
                    age = time.time() - sent_messages[text]
                    logger.debug(f"Skipping own-echo ROWID {rowid} (sent {age:.1f}s ago)")
                    continue

                history = conversation_histories.get(contact, [])
                if history and history[-1]["role"] == "assistant" and history[-1]["content"] == text:
                    logger.debug(f"Skipping own-echo ROWID {rowid} (matches last assistant turn)")
                    continue

                # Group chat filter - always first, no exceptions
                if _check_group_chat(msg["chat_id"]) > 1:
                    logger.debug(f"Skipping group chat from {contact}")
                    continue

                # Focus-response gate - total silence for non-focus-response contacts
                if not is_focus_response_contact(contact):
                    logger.debug(f"Non-focus-response contact {contact}, total silence")
                    continue

                if not _check_rate(contact):
                    continue

                logger.info(f"Incoming from {contact}")

                # --- STOP / START (bypass batcher, immediate) ---
                stripped = text.strip().upper()
                if stripped == "STOP":
                    session_state.mark_opted_out(contact)
                    focus_type = session_state.get_focus().get("focus") or "active"
                    send_imessage(contact, focus_messages.generate_opt_out_message(focus_type))
                    batcher.clear(contact)
                    continue

                if stripped == "START":
                    session_state.mark_opted_in(contact)
                    send_imessage(contact, "You're back! AutoReply Bot is on again.")
                    continue

                # --- Opted-out check ---
                if session_state.is_opted_out(contact):
                    if session_state.should_send_opt_out_message(contact):
                        focus_type = session_state.get_focus().get("focus") or "active"
                        send_imessage(contact, focus_messages.generate_opt_out_message(focus_type))
                        session_state.mark_opt_out_message_sent(contact)
                    else:
                        logger.debug(f"Opted-out contact {contact}, silently skipping")
                    continue

                # --- Session start notification (immediate, before batching) ---
                if session_state.should_send_session_start(contact):
                    focus_type   = session_state.get_focus().get("focus") or "active"
                    contact_name = get_contact_name(contact)
                    trusted      = is_focus_response_contact(contact)
                    start_msg    = focus_messages.generate_session_start(
                        focus_type, contact_name, trusted=trusted
                    )
                    send_imessage(contact, start_msg)
                    session_state.mark_session_start_sent(contact)

                batcher.add_message(contact, text, rowid)

            # --- Process contacts whose batch window has elapsed ---
            for contact in batcher.get_ready_contacts():
                batched_text = batcher.get_batched_text(contact)
                if not batched_text:
                    continue
                # injection check on combined batch before processing
                if is_injection_attempt(batched_text):
                    logger.warning(f"Injection pattern in batched text for {contact}, discarding batch")
                    continue
                logger.info(f"Processing batch for {contact}")
                _process_contact(contact, batched_text, session_state)

        except Exception as e:
            logger.error(f"Error in main loop: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
