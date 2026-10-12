"""Vivace / STOVEItaly pellet stove control via the local ESP32 bridge.

Called by Home Assistant (shell_command.stove_control):
  python3 stove_control.py on | off | temp <5.0-35.0> | fan <1-5>

Each command:
  1. takes a lock so commands never overlap,
  2. reads the CURRENT settings frame (GET /read?byte=49) and checks its CRCs,
  3. changes only the requested field, adds the fixed marker and CRC-16/MODBUS,
  4. sends GET /serialflush?str=<171 bytes, 3-digit, comma-joined>,
  5. waits (max 20 s) until the stove reports the new value,
  6. logs the result to stove_log/events.jsonl (event "command").
The HA call returns immediately ("queued"); a detached worker does steps 1-6.
If several requests for the same field queue up, only the newest is sent.
Power commands are limited to one every 2 minutes (protects the igniter).
"python3 stove_control.py --last" prints the newest command result as JSON.
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

BRIDGE = "192.168.1.50"  # IP of the stove's Wi-Fi module - change this
HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(HERE, "stove_log", "control.lock")

# settings frame (byte=49) / command frame layout, "version 0" stoves
ON_OFF, FAN, TEMP_A, TEMP_B = 4, 6, 7, 11


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_settings():
    with urllib.request.urlopen(f"http://{BRIDGE}/read?byte=49", timeout=8) as r:
        data = json.load(r)["data"]
    f = [int(x) for x in data.split(",") if x != ""][:171]
    if len(f) != 171 or f[0] != 49 or f[2] != 166:
        raise ValueError(f"unexpected settings frame header {f[:4]}")
    a, b = crc16_modbus(f[3:167]), crc16_modbus(f[0:169])
    if f[167:169] != [a & 0xFF, a >> 8] or f[169:171] != [b & 0xFF, b >> 8]:
        raise ValueError("settings frame failed CRC check")
    return f


def apply(frame, action, value):
    f = list(frame)
    if action in ("on", "off"):
        f[ON_OFF] = 1 if action == "on" else 0
    elif action == "temp":
        v = round(float(value) * 10)
        if not 50 <= v <= 350:
            raise ValueError("temperature must be 5.0-35.0 °C")
        f[TEMP_A], f[TEMP_A + 1] = v & 0xFF, v >> 8
        f[TEMP_B], f[TEMP_B + 1] = v & 0xFF, v >> 8
    elif action == "fan":
        v = int(float(value))
        if not 1 <= v <= 5:
            raise ValueError("fan must be 1-5 (0 is not accepted by the stove)")
        f[FAN] = v
    else:
        raise ValueError(f"unknown action {action!r}")
    return f


def build_command(target):
    f = list(target)
    f[0], f[1], f[2], f[3] = 50, 0, 166, 0
    f[167], f[168] = 132, 12
    crc = crc16_modbus(f[0:169])
    f[169], f[170] = crc & 0xFF, crc >> 8
    return f


def send(frame):
    s = ",".join(f"{b:03d}" for b in frame)
    url = f"http://{BRIDGE}/serialflush?" + urllib.parse.urlencode({"str": s})
    with urllib.request.urlopen(url, timeout=8) as r:
        return r.status


def confirmed(now, target):
    # compare the fields commands may change; ignore header and CRC bytes
    return now[4:167] == target[4:167]


def log(entry):
    try:
        sys.path.insert(0, HERE)
        from stove_logger import append  # shares the 10 MB cap
        append([entry])
    except Exception:
        pass


FIELD = {"on": "power", "off": "power", "temp": "temp", "fan": "fan"}
POWER_COOLDOWN = 120  # seconds between two power commands actually sent to the stove
LOG_FILE = os.path.join(HERE, "stove_log", "events.jsonl")


def state_path(name):
    return os.path.join(os.path.dirname(LOCK_FILE), name)


def queue(action, value):
    """Validate, record as the newest request for its field, start a detached worker, return at once."""
    if action not in FIELD:
        print(f"unknown action {action!r}")
        return 1
    try:
        apply([0] * 171, action, value)  # validates the value
    except Exception as e:
        print(f"error: {e}")
        return 1
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    field = FIELD[action]
    seq = str(time.time_ns())
    tmp = state_path(f"pending_{field}.tmp")
    with open(tmp, "w") as f:
        json.dump({"seq": seq, "action": action, "value": value}, f)
    os.replace(tmp, state_path(f"pending_{field}"))
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", field, seq],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print("queued")
    return 0


def worker(field, seq):
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(state_path(f"pending_{field}")) as f:
                req = json.load(f)
        except (OSError, ValueError):
            return 1
        if req["seq"] != seq:
            return 0  # a newer request for this field exists; its own worker will handle it
        action, value = req["action"], req["value"]
        entry = {"event": "command", "ts": datetime.now().isoformat(timespec="seconds"),
                 "action": action, "value": value}
        try:
            current = read_settings()
            target = apply(current, action, value)
            if confirmed(current, target):
                entry["result"] = "already set"
            elif field == "power" and time.time() - last_power_sent() < POWER_COOLDOWN:
                wait = int(POWER_COOLDOWN - (time.time() - last_power_sent()))
                entry["result"] = f"refused: wait {wait} s between power changes"
            else:
                if field == "power":
                    with open(state_path("last_power_sent"), "w") as f:
                        f.write(str(time.time()))
                entry["http"] = send(build_command(target))
                deadline = time.time() + 20
                while time.time() < deadline:
                    time.sleep(2)
                    try:
                        if confirmed(read_settings(), target):
                            entry["result"] = "confirmed"
                            break
                    except Exception:
                        pass
                else:
                    entry["result"] = "not confirmed within 20 s"
        except Exception as e:
            entry["result"] = f"error: {e}"
    log(entry)
    return 0 if entry["result"] in ("confirmed", "already set") else 1


def last_power_sent():
    try:
        with open(state_path("last_power_sent")) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def last_command():
    """Print the newest "command" event as JSON (for HA's command_line sensor)."""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "command":
            print(json.dumps({"action": e.get("action"), "value": e.get("value"),
                              "result": e.get("result"), "ts": e.get("ts")}))
            return 0
    print(json.dumps({"action": None, "value": None, "result": "none yet", "ts": None}))
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--worker"] and len(args) == 3:
        return worker(args[1], args[2])
    if args[:1] == ["--last"]:
        return last_command()
    if not args:
        print(__doc__)
        return 1
    return queue(args[0], args[1] if len(args) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
