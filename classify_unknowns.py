#!/usr/bin/env python3
"""
Reclassify response_patterns.db rows with intent='unknown' using llama3.2:3b.
Usage: python3 classify_unknowns.py
"""
import sqlite3
import json
import re
import os
import time
import requests
from collections import defaultdict
from pathlib import Path

BASE_DIR       = Path(__file__).parent
PATTERNS_DB    = BASE_DIR / "response_patterns.db"
OLLAMA_URL     = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
CLASSIFIER_MODEL = "llama3.2:3b"

BATCH_SIZE     = 20
SLEEP_BETWEEN  = 0.5   # seconds between batches
TIMEOUT        = 30    # seconds per API call
PROGRESS_EVERY = 100   # batches between progress prints

VALID_INTENTS = {
    "greeting_opener", "greeting_checkin", "casual_chat", "plan_making",
    "logistics", "emotional_support", "humor", "family_update", "work_talk",
    "recommendation", "gratitude", "farewell", "personal_question",
    "news_reaction",
}

SYSTEM_PROMPT = """\
You are classifying text message exchanges. For each pair below, assign ONE intent label from this list:
- greeting_opener (saying hello, first message)
- greeting_checkin (asking how someone is)
- casual_chat (small talk, random observations)
- plan_making (scheduling, coordinating meetups)
- logistics (practical info, directions, timing)
- emotional_support (someone sharing feelings or problems)
- humor (jokes, funny observations, teasing)
- family_update (news about relatives, household, or home life)
- work_talk (job, career, meetings, work stress)
- recommendation (asking for or giving suggestions)
- gratitude (thanking someone)
- farewell (goodbye, ending conversation)
- personal_question (asking about someone's life)
- news_reaction (reacting to something in the news or world)

Return ONLY a JSON array of objects with keys 'id' and 'intent'. Example:
[{"id": 123, "intent": "casual_chat"}, {"id": 124, "intent": "humor"}]

Pairs to classify:"""


def build_prompt(batch):
    lines = [SYSTEM_PROMPT]
    for row_id, user_msg, bot_resp in batch:
        u = user_msg.replace("\n", " ")[:150]
        j = bot_resp.replace("\n", " ")[:150]
        lines.append(f"ID: {row_id} | THEM: {u} | BOT: {j}")
    return "\n".join(lines)


def parse_response(content):
    """Extract a JSON array from the model response, stripping markdown fences."""
    content = content.strip()
    # Strip markdown code fences
    content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'\s*```\s*$',       '', content, flags=re.MULTILINE)
    # Find the JSON array even if there's surrounding text
    match = re.search(r'\[.*\]', content, re.DOTALL)
    if match:
        content = match.group(0)
    return json.loads(content)


def classify_batch(batch):
    """
    Call llama3.2:3b for a batch. Returns list of (id, intent) tuples,
    or None on complete failure.
    """
    prompt = build_prompt(batch)
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": CLASSIFIER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }, timeout=TIMEOUT)
        content = resp.json()["message"]["content"]
        results = parse_response(content)

        valid = []
        for item in results:
            row_id = item.get("id")
            intent = item.get("intent", "").strip().lower()
            if not isinstance(row_id, int):
                continue
            if intent not in VALID_INTENTS:
                intent = "unknown"
            valid.append((intent, row_id))
        return valid

    except Exception as e:
        return None, str(e)


def main():
    conn = sqlite3.connect(PATTERNS_DB)
    c = conn.cursor()

    c.execute("SELECT id, user_message, bot_response FROM response_patterns WHERE intent='unknown' ORDER BY id")
    rows = c.fetchall()
    total = len(rows)
    print(f"Found {total:,} rows with intent='unknown'. Processing in batches of {BATCH_SIZE}...")

    batches = [rows[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    num_batches = len(batches)

    updated = 0
    failed_ids = []

    for batch_num, batch in enumerate(batches, start=1):
        batch_ids = [r[0] for r in batch]

        result = classify_batch(batch)
        if result is None or (isinstance(result, tuple) and result[0] is None):
            err = result[1] if isinstance(result, tuple) else "unknown error"
            print(f"  [WARN] batch {batch_num} failed ({err}), skipping IDs {batch_ids[0]}..{batch_ids[-1]}")
            failed_ids.extend(batch_ids)
        else:
            updates = result if not isinstance(result, tuple) else []
            if updates:
                conn.executemany(
                    "UPDATE response_patterns SET intent=? WHERE id=?",
                    updates,
                )
                conn.commit()
                updated += len(updates)

        if batch_num % PROGRESS_EVERY == 0:
            processed = batch_num * BATCH_SIZE
            print(f"  Processed {min(processed, total):,}/{total:,} rows ({batch_num}/{num_batches} batches, {updated:,} updated, {len(failed_ids):,} failed)...")

        time.sleep(SLEEP_BETWEEN)

    conn.close()

    print(f"\nDone. {updated:,} rows reclassified. {len(failed_ids):,} rows left as 'unknown'.")
    if failed_ids:
        print(f"  Failed batch IDs (first 20): {failed_ids[:20]}")

    # Final distribution
    conn = sqlite3.connect(PATTERNS_DB)
    c = conn.cursor()
    c.execute("SELECT intent, COUNT(*) FROM response_patterns GROUP BY intent ORDER BY COUNT(*) DESC")
    rows = c.fetchall()
    conn.close()

    total_all = sum(r[1] for r in rows)
    print(f"\nFinal intent distribution ({total_all:,} total rows):")
    for intent, count in rows:
        bar = "█" * (count * 40 // total_all)
        print(f"  {intent:<22} {count:>6,}  {bar}")


if __name__ == "__main__":
    main()
