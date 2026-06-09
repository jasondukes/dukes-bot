"""
session_state.py - manages per-contact session state and Focus awareness.
Import this module; do not run directly (test block at bottom for __main__).
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR          = Path(__file__).parent
SESSION_STATE_FILE = BASE_DIR / "session_state.json"
FOCUS_STATE_FILE   = BASE_DIR / "focus_state.json"

SESSION_RESUME_WINDOW = 30 * 60  # 30 minutes in seconds; resume silently within this window


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(s):
    """Parse ISO string to datetime, return None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _seconds_between(earlier_iso, later_iso):
    """Return seconds between two ISO strings, or None if either is missing."""
    a = _parse_iso(earlier_iso)
    b = _parse_iso(later_iso)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


class SessionState:
    """
    Persists to SESSION_STATE_FILE with this shape:
    {
      "sessions": {
        "+1...": {
          "phone": "+1...",
          "focus_when_started": "Driving",
          "focus_activated_at": "2026-04-17T09:00:00",
          "session_start": "2026-04-17T09:00:00",
          "last_message_at": "2026-04-17T09:05:00",
          "session_start_sent": false,
          "opt_out_message_sent": false
        }
      },
      "opted_out_permanent": ["+1...", "+2..."]
    }
    """

    def __init__(self):
        self._data = {"sessions": {}, "opted_out_permanent": []}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self):
        try:
            with open(SESSION_STATE_FILE) as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                logger.warning("session_state.json: unexpected root type, resetting")
                return
            sessions = loaded.get("sessions", {})
            opted    = loaded.get("opted_out_permanent", [])
            if not isinstance(sessions, dict) or not isinstance(opted, list):
                logger.warning("session_state.json: invalid structure, resetting")
                return
            self._data["sessions"]             = sessions
            self._data["opted_out_permanent"]  = opted
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"session_state.json load error: {e}; using empty defaults")

    def save(self):
        tmp = str(SESSION_STATE_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, SESSION_STATE_FILE)

    # ------------------------------------------------------------------
    # Focus state (reads focus_state.json written by focus_daemon)
    # ------------------------------------------------------------------

    def get_focus(self):
        """
        Returns dict: {"focus": str|None, "activated_at": str|None, "previous": str|None}
        focus=None means no active Focus. Validates schema and checksum.
        """
        try:
            # verify integrity checksum if present
            try:
                from input_sanitizer import verify_checksum
                if not verify_checksum(FOCUS_STATE_FILE):
                    logger.warning("focus_state.json checksum mismatch; treating focus as inactive")
                    return {"focus": None, "activated_at": None, "previous": None}
            except ImportError:
                pass

            with open(FOCUS_STATE_FILE) as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning("focus_state.json: unexpected root type; treating focus as inactive")
                return {"focus": None, "activated_at": None, "previous": None}

            # validate focus value type and length
            focus_val = data.get("focus")
            if focus_val is not None:
                if not isinstance(focus_val, str) or len(focus_val) > 100:
                    logger.warning(f"focus_state.json: invalid focus value {focus_val!r}; treating as null")
                    focus_val = None
                else:
                    # Strip control characters from focus name
                    focus_val = re.sub(r'[\x00-\x1f\x7f]', '', focus_val).strip() or None

            return {
                "focus":        focus_val,
                "activated_at": data.get("activated_at"),
                "previous":     data.get("previous"),
            }
        except Exception:
            return {"focus": None, "activated_at": None, "previous": None}

    def is_bot_active(self):
        return self.get_focus().get("focus") is not None

    # ------------------------------------------------------------------
    # Per-contact session access
    # ------------------------------------------------------------------

    def get_session_for_contact(self, phone):
        """
        Returns the session dict for this phone, initialising defaults if absent.
        Does NOT save; caller must call save() after mutations.
        """
        sessions = self._data["sessions"]
        if phone not in sessions:
            sessions[phone] = {
                "phone":               phone,
                "focus_when_started":  None,
                "focus_activated_at":  None,
                "session_start":       None,
                "last_message_at":     None,
                "session_start_sent":  False,
                "opt_out_message_sent": False,
            }
        return sessions[phone]

    def _is_new_focus_session(self, session, focus_info):
        """
        True if the current focus's activated_at differs from what's stored,
        indicating the Focus was turned off and back on.
        """
        return session.get("focus_activated_at") != focus_info.get("activated_at")

    # ------------------------------------------------------------------
    # Session-start logic
    # ------------------------------------------------------------------

    def should_send_session_start(self, phone):
        """
        True if we should send the bot-active opener for this contact.
        Conditions:
          a) Bot is active
          b) Either no prior session, OR the Focus was toggled and the gap
             since last_message_at exceeds SESSION_RESUME_WINDOW (30 min)
          c) Haven't already sent it for the current focus activation
        """
        if not self.is_bot_active():
            return False

        focus_info = self.get_focus()
        session    = self.get_session_for_contact(phone)

        # No prior session at all → send
        if session["session_start"] is None:
            return True

        # Same focus activation → only send if not yet sent
        if not self._is_new_focus_session(session, focus_info):
            return not session["session_start_sent"]

        # New focus session (focus was toggled) - check gap
        gap = _seconds_between(session["last_message_at"], focus_info.get("activated_at"))
        if gap is None or gap > SESSION_RESUME_WINDOW:
            return True  # long gap or unknown → send fresh start

        return False  # came back within 30 min → resume silently

    def mark_session_start_sent(self, phone):
        focus_info = self.get_focus()
        session    = self.get_session_for_contact(phone)
        now        = _now_iso()

        session["focus_when_started"]  = focus_info.get("focus")
        session["focus_activated_at"]  = focus_info.get("activated_at")
        session["session_start"]       = now
        session["session_start_sent"]  = True
        session["opt_out_message_sent"] = False  # reset for new session
        self.save()

    # ------------------------------------------------------------------
    # Opt-out / opt-in
    # ------------------------------------------------------------------

    def mark_opted_out(self, phone):
        opted_out = self._data["opted_out_permanent"]
        if phone not in opted_out:
            opted_out.append(phone)
        session = self.get_session_for_contact(phone)
        session["opt_out_message_sent"] = False
        self.save()

    def mark_opted_in(self, phone):
        opted_out = self._data["opted_out_permanent"]
        if phone in opted_out:
            opted_out.remove(phone)
        self.save()

    def is_opted_out(self, phone):
        return phone in self._data["opted_out_permanent"]

    def should_send_opt_out_message(self, phone):
        """True if opted out AND we haven't sent the opt-out confirmation yet this session."""
        if not self.is_opted_out(phone):
            return False
        session = self.get_session_for_contact(phone)
        return not session.get("opt_out_message_sent", False)

    def mark_opt_out_message_sent(self, phone):
        session = self.get_session_for_contact(phone)
        session["opt_out_message_sent"] = True
        self.save()

    # ------------------------------------------------------------------
    # Last-message tracking
    # ------------------------------------------------------------------

    def update_last_message(self, phone):
        session = self.get_session_for_contact(phone)
        session["last_message_at"] = _now_iso()
        self.save()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil, tempfile

    # Redirect files to a temp dir so we don't touch real state
    tmp_dir = tempfile.mkdtemp()
    _orig_session = SESSION_STATE_FILE
    _orig_focus   = FOCUS_STATE_FILE

    import session_state as _mod
    _mod.SESSION_STATE_FILE = os.path.join(tmp_dir, "session_state.json")
    _mod.FOCUS_STATE_FILE   = os.path.join(tmp_dir, "focus_state.json")

    # Write a fake focus_state.json
    with open(_mod.FOCUS_STATE_FILE, "w") as f:
        json.dump({
            "focus": "Driving",
            "activated_at": "2026-04-17T09:00:00",
            "previous": None,
        }, f)

    ss = _mod.SessionState()
    phone = "+15551234567"

    print("--- is_bot_active ---")
    assert ss.is_bot_active() is True, "should be active"
    print("  PASS: is_bot_active=True")

    print("--- get_focus ---")
    f = ss.get_focus()
    assert f["focus"] == "Driving"
    print("  PASS:", f)

    print("--- should_send_session_start (no prior session) ---")
    assert ss.should_send_session_start(phone) is True
    print("  PASS: True")

    print("--- mark_session_start_sent ---")
    ss.mark_session_start_sent(phone)
    assert ss.should_send_session_start(phone) is False
    print("  PASS: False after mark")

    print("--- update_last_message ---")
    ss.update_last_message(phone)
    session = ss.get_session_for_contact(phone)
    assert session["last_message_at"] is not None
    print("  PASS:", session["last_message_at"])

    print("--- opt-out flow ---")
    assert ss.is_opted_out(phone) is False
    assert ss.should_send_opt_out_message(phone) is False
    ss.mark_opted_out(phone)
    assert ss.is_opted_out(phone) is True
    assert ss.should_send_opt_out_message(phone) is True
    ss.mark_opt_out_message_sent(phone)
    assert ss.should_send_opt_out_message(phone) is False
    print("  PASS: opt-out lifecycle")

    print("--- opt-in flow ---")
    ss.mark_opted_in(phone)
    assert ss.is_opted_out(phone) is False
    print("  PASS: opted back in")

    print("--- new focus session, long gap → should send ---")
    # Simulate focus turning off and back on after 35 min
    session["last_message_at"]    = "2026-04-17T09:05:00"
    session["focus_activated_at"] = "2026-04-17T09:00:00"
    session["session_start_sent"] = True
    ss.save()
    # New activation 35 min later
    with open(_mod.FOCUS_STATE_FILE, "w") as f:
        json.dump({
            "focus": "Driving",
            "activated_at": "2026-04-17T09:40:00",
            "previous": None,
        }, f)
    ss2 = _mod.SessionState()
    assert ss2.should_send_session_start(phone) is True
    print("  PASS: True (35-min gap)")

    print("--- new focus session, short gap → resume silently ---")
    with open(_mod.FOCUS_STATE_FILE, "w") as f:
        json.dump({
            "focus": "Driving",
            "activated_at": "2026-04-17T09:10:00",  # only 5 min after last_message
            "previous": None,
        }, f)
    ss3 = _mod.SessionState()
    session3 = ss3.get_session_for_contact(phone)
    session3["last_message_at"]    = "2026-04-17T09:05:00"
    session3["focus_activated_at"] = "2026-04-17T09:00:00"  # old activation
    session3["session_start_sent"] = True
    ss3.save()
    assert ss3.should_send_session_start(phone) is False
    print("  PASS: False (5-min gap, resume silently)")

    # Cleanup
    shutil.rmtree(tmp_dir)
    print("\nAll tests passed.")
