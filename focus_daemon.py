#!/usr/bin/env python3
"""
focus_daemon.py - polls macOS Focus state every 5 seconds and writes
~/jason-bot/focus_state.json whenever the state changes.

CPU budget: 5 000 ms sleep, < 5 ms processing = 0.1 % max. Well under 1 %.
"""
import json
import os
import subprocess
import time
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR        = Path(__file__).parent
FOCUS_STATE_FILE = BASE_DIR / "focus_state.json"
LOG_FILE         = BASE_DIR / "focus_daemon.log"
DND_ASSERTIONS   = os.path.expanduser("~/Library/DoNotDisturb/DB/Assertions.json")
DND_MODE_CFG     = os.path.expanduser("~/Library/DoNotDisturb/DB/ModeConfigurations.json")

POLL_INTERVAL = 5  # seconds

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)


# mode identifier → display name, parsed from ModeConfigurations.json
def load_mode_name_map():
    result = {}
    try:
        with open(DND_MODE_CFG) as f:
            data = json.load(f)
        for block in data.get("data", []):
            for mode_cfg in block.get("modeConfigurations", {}).values():
                mode = mode_cfg.get("mode", {})
                mid  = mode.get("modeIdentifier")
                name = mode.get("name")
                if mid and name:
                    result[mid] = name
    except Exception as e:
        logger.debug(f"mode name map load failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Focus detection - three methods, tried in order
# ---------------------------------------------------------------------------

def _detect_via_assertions(mode_name_map):
    """
    Primary: parse ~/Library/DoNotDisturb/DB/Assertions.json.
    storeAssertionRecords is present only when a focus IS active.
    Returns friendly focus name string, or None.
    """
    with open(DND_ASSERTIONS) as f:
        data = json.load(f)

    for block in data.get("data", []):
        for record in block.get("storeAssertionRecords", []):
            details = record.get("assertionDetails", {})
            mode_id = details.get("assertionDetailsModeIdentifier")
            if mode_id:
                name = mode_name_map.get(mode_id)
                if not name:
                    # Custom focus - use last path component as name
                    name = mode_id.split(".")[-1].title()
                return name

    return None


def _detect_via_shortcuts():
    """
    Tertiary fallback: run a Shortcuts automation named 'Get Current Focus'.
    Expected to print the focus name to stdout or nothing if off.
    Tight 3-second timeout to avoid blocking the poll loop.
    """
    try:
        result = subprocess.run(
            ["shortcuts", "run", "Get Current Focus"],
            capture_output=True, text=True, timeout=3
        )
        out = result.stdout.strip()
        return out if out else None
    except Exception:
        return None


def get_current_focus(mode_name_map):
    """Try Assertions.json, fall back to Shortcuts. Returns focus name or None."""
    try:
        return _detect_via_assertions(mode_name_map)
    except Exception as e:
        logger.debug(f"assertions detection failed: {e}")

    return _detect_via_shortcuts()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def read_saved_state():
    try:
        with open(FOCUS_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"focus": None, "activated_at": None, "previous": None}


def write_state(focus, previous):
    state = {
        "focus":        focus,
        "activated_at": datetime.now().isoformat(timespec="seconds"),
        "previous":     previous,
    }
    tmp = str(FOCUS_STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, FOCUS_STATE_FILE)
    try:
        from input_sanitizer import compute_file_checksum, write_checksum
        write_checksum(FOCUS_STATE_FILE, compute_file_checksum(FOCUS_STATE_FILE))
    except Exception as e:
        logger.debug(f"checksum write failed: {e}")
    return state


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def main():
    logger.info("focus_daemon started")
    mode_name_map = load_mode_name_map()
    logger.info(f"loaded {len(mode_name_map)} focus mode names: {list(mode_name_map.values())}")

    saved   = read_saved_state()
    current = saved.get("focus")

    while True:
        try:
            detected = get_current_focus(mode_name_map)
            # Normalize: None and empty string both mean "off"
            detected = detected or None

            if detected != current:
                previous = current
                current  = detected
                state    = write_state(current, previous)
                logger.info(
                    f"Focus changed: {previous!r} → {current!r}  "
                    f"(activated_at={state['activated_at']})"
                )
        except Exception as e:
            logger.error(f"poll error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
