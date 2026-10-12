"""Vivace pellet stove event logger.

Called by Home Assistant (shell_command.stove_log) every 30 s with the raw
bridge frames:  python3 stove_logger.py <frame81> <frame49>
("none" when a frame is unavailable). Keeps a 10-minute ring buffer and
appends one JSON line per event to stove_log/events.jsonl:

  logger_started    baseline snapshot (first run / after state reset)
  error             error code changed to non-zero (includes 10 min before)
  error_followup    snapshots for 10 min after an error
  error_cleared     error code back to 0
  stove_on/off      byte 9 changed
  igniter_on/off    byte 96 changed
  bridge_offline    no data from the bridge
  bridge_online     data is back

events.jsonl never exceeds MAX_BYTES: oldest lines are dropped first.
"""
import json
import os
import sys
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stove_log")
LOG_FILE = os.path.join(LOG_DIR, "events.jsonl")
STATE_FILE = os.path.join(LOG_DIR, "state.json")

MAX_BYTES = 10 * 1024 * 1024  # hard cap for events.jsonl
TRIM_TO = 8 * 1024 * 1024  # when trimming, keep the newest ~8 MB
RING_SIZE = 20  # 20 x 30 s = 10 minutes of context
FOLLOWUP = 20  # snapshots logged after an error

ERRORS = {
    1: "Fire went out", 2: "Ignition failed", 3: "Insufficient air intake",
    4: "Probe Tc1 fault", 5: "Probe Tc2 fault", 6: "Line sync error",
    7: "EEPROM error", 8: "EEPROM error", 9: "Pressure sensor fault",
    10: "Power cut", 16: "Power cut",
}


def parse(frame):
    if not frame or frame == "none":
        return None
    try:
        b = [int(x) for x in frame.split(",") if x != ""]
    except ValueError:
        return None
    return b if len(b) > 110 else None


def trim_zeros(b):
    """Raw frame without the long run of trailing zeros."""
    if b is None:
        return None
    end = len(b)
    while end and b[end - 1] == 0:
        end -= 1
    return b[:end]


def u16(b, i):
    return b[i + 1] * 256 + b[i]


def decode(b):
    """Verified byte map (see configuration.yaml / project memory)."""
    code = b[26]
    return {
        "on": b[9],
        "program": b[10],
        "set_temp": u16(b, 12) / 10,
        "room_temp": u16(b, 34) / 10,
        "structure_temp": b[38],
        "smoke_temp": u16(b, 42),
        "air_flow": u16(b, 54),
        "pellet_level": b[64],
        "power": b[99],
        "motor": b[100],
        "conv_fan": b[101],
        "smoke_fan": b[102],
        "igniter": b[96],
        "error_code": code,
        "error": "OK" if code == 0 else f"E{code} {ERRORS.get(code, 'Unknown code')}",
    }


def snapshot(b81, b49):
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "decoded": decode(b81) if b81 else None,
        "raw81": trim_zeros(b81),
        "raw49": trim_zeros(b49),
    }


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


def append(entries):
    def encode():
        return "".join(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n" for e in entries).encode()

    data = encode()
    if len(data) > TRIM_TO:  # never happens with real frames; keep the cap absolute anyway
        for e in entries:
            e.pop("before", None)
        data = encode()
    size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    if size + len(data) > MAX_BYTES:
        with open(LOG_FILE, "rb") as f:
            lines = f.read().splitlines(keepends=True)
        kept, total = [], 0
        budget = min(TRIM_TO, MAX_BYTES - len(data))
        for line in reversed(lines):
            if total + len(line) > budget:
                break
            kept.append(line)
            total += len(line)
        tmp = LOG_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.writelines(reversed(kept))
            f.write(data)
        os.replace(tmp, LOG_FILE)
    else:
        with open(LOG_FILE, "ab") as f:
            f.write(data)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    args = sys.argv[1:] + ["none", "none"]
    b81, b49 = parse(args[0]), parse(args[1])
    snap = snapshot(b81, b49)
    state = load_state()
    entries = []

    def event(name, **extra):
        entries.append({"event": name, "ts": snap["ts"], **extra, "now": snap})

    if state is None:
        state = {"ring": [], "followup": 0, "online": b81 is not None}
        event("logger_started")
    else:
        prev = next((s["decoded"] for s in reversed(state["ring"]) if s["decoded"]), None)
        online = b81 is not None
        if online != state["online"]:
            event("bridge_online" if online else "bridge_offline")
            state["online"] = online
        cur = snap["decoded"]
        if cur and prev:
            if cur["error_code"] != prev["error_code"]:
                if cur["error_code"]:
                    event("error", previous_error=prev["error"], before=state["ring"])
                    state["followup"] = FOLLOWUP
                else:
                    event("error_cleared", previous_error=prev["error"])
            if cur["on"] != prev["on"]:
                event("stove_on" if cur["on"] else "stove_off")
            if cur["igniter"] != prev["igniter"]:
                event("igniter_on" if cur["igniter"] else "igniter_off")
        if state["followup"] > 0 and not any(e["event"] == "error" for e in entries):
            state["followup"] -= 1
            event("error_followup", remaining=state["followup"])

    state["ring"] = (state["ring"] + [snap])[-RING_SIZE:]
    if entries:
        append(entries)
    save_state(state)


if __name__ == "__main__":
    main()
