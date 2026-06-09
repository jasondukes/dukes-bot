"""
message_batcher.py - buffers incoming messages per contact and releases
them as a single joined string after a quiet window of BATCH_WINDOW seconds.

Persists to message_buffer.json so partial batches survive restarts.
"""
import json
import os
import time
from pathlib import Path

BASE_DIR           = Path(__file__).parent
BUFFER_FILE        = BASE_DIR / "message_buffer.json"
BATCH_WINDOW       = 25  # seconds of silence before a contact's batch is ready


class MessageBatcher:
    """
    Buffer structure (in memory and on disk):
    {
      "+1...": {
        "messages": [
          {"rowid": 123, "text": "hey", "received_at": 1713344400.0},
          ...
        ],
        "last_received": 1713344400.0
      }
    }
    """

    def __init__(self):
        self._buffer = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        try:
            with open(BUFFER_FILE) as f:
                self._buffer = json.load(f)
        except FileNotFoundError:
            self._buffer = {}
        except Exception as e:
            print(f"[message_batcher] load error: {e}")
            self._buffer = {}

    def _save(self):
        tmp = str(BUFFER_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._buffer, f, indent=2)
        os.replace(tmp, BUFFER_FILE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_message(self, phone, text, rowid):
        if phone not in self._buffer:
            self._buffer[phone] = {"messages": [], "last_received": 0.0}

        self._buffer[phone]["messages"].append({
            "rowid":       rowid,
            "text":        text,
            "received_at": time.time(),
        })
        self._buffer[phone]["last_received"] = time.time()
        self._save()

    def get_ready_contacts(self):
        """
        Return list of phones whose last message arrived more than
        BATCH_WINDOW seconds ago, i.e. their typing burst has ended.
        """
        now   = time.time()
        ready = []
        for phone, entry in self._buffer.items():
            if entry["messages"] and (now - entry["last_received"]) >= BATCH_WINDOW:
                ready.append(phone)
        return ready

    def get_batched_text(self, phone):
        """
        Return all buffered messages for this contact joined with ' | ',
        then clear the buffer for that contact.
        Returns empty string if nothing buffered.
        """
        entry = self._buffer.pop(phone, None)
        self._save()
        if not entry or not entry["messages"]:
            return ""
        return " | ".join(m["text"] for m in entry["messages"])

    def clear(self, phone):
        """Discard buffered messages for a contact without returning them."""
        self._buffer.pop(phone, None)
        self._save()

    def has_pending(self, phone):
        entry = self._buffer.get(phone)
        return bool(entry and entry["messages"])
