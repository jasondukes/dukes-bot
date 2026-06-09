#!/usr/bin/env python3
"""
Build response_patterns.db from training_data_v4.json.
Run once (or re-run to rebuild): python3 build_response_db.py
"""
import sqlite3
import json
import re
import os
from collections import defaultdict
from pathlib import Path

BASE_DIR      = Path(__file__).parent
TRAINING_DATA = BASE_DIR / "training_data_v4.json"
PATTERNS_DB   = BASE_DIR / "response_patterns.db"

INTENTS = [
    "greeting_opener", "greeting_checkin",
    "casual_question", "personal_question", "plan_making",
    "emotional_support", "logistics", "humor", "unknown",
]

# ---------------------------------------------------------------------------
# Rule-based intent classifier
# ---------------------------------------------------------------------------

# greeting_opener: short messages that START with a hello-style word (≤ 8 words).
# Anchored at ^ so "hey can you send me your address" doesn't qualify.
_OPENER_RE = re.compile(
    r'^(hey+|hi+|hello|howdy|yo+|sup|hiya|heya|what\'?s good|good morning|good afternoon|good evening|good night)\b',
    re.IGNORECASE,
)

# greeting_checkin: short messages asking how someone is doing (≤ 10 words).
_CHECKIN_RE = re.compile(
    r'^(what\'?s up|whats up|what up|what\'?s going on|whats going on'
    r'|how are you|how\'?s it going|how is it going|how you doing'
    r'|how\'?s everything|hows everything|how have you been|how\'?s life'
    r'|how\'?s things|how are things)\b',
    re.IGNORECASE,
)

_EMOTIONAL_RE = re.compile(
    r'\b(sad|depressed|anxious|stressed|worried|scared|overwhelmed'
    r'|devastated|heartbroken|upset|crying|grieving|grieved)\b'
    r"|i'?m (feeling|sad|stressed|anxious|worried|scared|upset|overwhelmed)"
    r'|\b(so hard|tough time|rough (day|week|night|time)|not doing well'
    r'|having a hard time|really hard|this sucks)\b'
    r'|\b(so sorry|sorry to hear|that\'?s awful|that\'?s terrible|that\'?s rough)\b',
    re.IGNORECASE,
)
_PERSONAL_Q_RE = re.compile(
    r'\bhow are you feeling\b|\bhow\'?s your\b|\bhow are (the kids|your kids|your family)\b'
    r'|\bare you (ok|okay|alright|doing ok|feeling ok)\b|\byou (ok|okay|alright)\b'
    r'|\bhow\'?s .{1,25} doing\b|\bhow is .{1,25} doing\b'
    r'|\bhow\'?s (your health|your back|your head|everyone)\b',
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r'\b(wanna|want to|let\'?s|can we|you free|are you free|you available'
    r'|are you around|hang out|hangout|get together|meet up'
    r'|come over|coming over|join us|join me|down to|up for)\b'
    r'|\b(tonight|tomorrow|this weekend|next week|this week|saturday|sunday'
    r'|monday|tuesday|wednesday|thursday|friday)\b'
    r'|\b(dinner|lunch|coffee|drinks|beer|brunch|happy hour|party|get-together)\b',
    re.IGNORECASE,
)
_HUMOR_RE = re.compile(
    r'\b(lol|lmao|lmfao|haha|hahaha|rofl|omg that\'?s|so funny|too funny'
    r'|that\'?s hilarious|hilarious|cracking up|dying|dead 💀)\b'
    r'|[😂🤣]',
    re.IGNORECASE,
)
_LOGISTICS_RE = re.compile(
    r'\b(can you|could you|please send|need you to|send me|give me'
    r'|what\'?s the address|what time|when is|where is|how do i|how do you'
    r'|what\'?s your (number|email|address)|do you have|can i get|could i get)\b',
    re.IGNORECASE,
)
_CASUAL_Q_RE = re.compile(r'\?')


def classify_intent(user_msg):
    t = user_msg.strip()
    word_count = len(t.split())

    # Openers and checkins must be short - prevents "hey can you send me X" matching
    if word_count <= 8 and _OPENER_RE.match(t):
        return "greeting_opener"
    if word_count <= 10 and _CHECKIN_RE.match(t):
        return "greeting_checkin"

    if _EMOTIONAL_RE.search(t):   return "emotional_support"
    if _PERSONAL_Q_RE.search(t):  return "personal_question"
    if _PLAN_RE.search(t):        return "plan_making"
    if _HUMOR_RE.search(t):       return "humor"
    if _LOGISTICS_RE.search(t):   return "logistics"
    if _CASUAL_Q_RE.search(t):    return "casual_question"
    return "unknown"


def length_bucket(text):
    n = len(text)
    if n <= 50:   return "short"
    if n <= 150:  return "medium"
    return "long"


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS response_patterns (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            intent                TEXT NOT NULL,
            user_message          TEXT NOT NULL,
            bot_response        TEXT NOT NULL,
            message_length_bucket TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intent ON response_patterns (intent)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Loading {TRAINING_DATA} ...")
    with open(TRAINING_DATA, "r") as f:
        data = json.load(f)

    conn = init_db(PATTERNS_DB)
    conn.execute("DELETE FROM response_patterns")

    counts = defaultdict(int)
    batch = []

    for conv in data:
        messages = conv.get("messages", [])
        for i, msg in enumerate(messages):
            if msg["role"] != "assistant":
                continue
            if i == 0 or messages[i - 1]["role"] != "user":
                continue
            user_msg   = messages[i - 1]["content"].strip()
            bot_resp = msg["content"].strip()
            if not user_msg or not bot_resp:
                continue
            intent = classify_intent(user_msg)
            bucket = length_bucket(bot_resp)
            batch.append((intent, user_msg, bot_resp, bucket))
            counts[intent] += 1

    conn.executemany(
        "INSERT INTO response_patterns (intent, user_message, bot_response, message_length_bucket)"
        " VALUES (?, ?, ?, ?)",
        batch,
    )
    conn.commit()
    conn.close()

    total = sum(counts.values())
    print(f"\nDone. {total:,} pairs stored in {PATTERNS_DB}\n")
    print("Pairs per intent:")
    for intent in INTENTS:
        print(f"  {intent:<22} {counts[intent]:>6,}")


if __name__ == "__main__":
    main()
