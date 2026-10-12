#!/usr/bin/env python3
"""Build (and optionally send) STOVEItaly / Vivace pellet-stove commands.

Protocol (ESP32 Wi-Fi bridge, "version 0" stoves):
  read settings : GET http://<ip>/read?byte=49  -> {"data":"49,0,166,..."}  (171 bytes used)
  send command  : GET http://<ip>/serialflush?str=050,000,166,...          (171 bytes, 3-digit, comma-joined)

A command is the CURRENT settings frame (fresh byte=49 read) with:
  [0]=50 (write), [1]=0, [2]=166, [3]=0,
  the field(s) you change,
  [167]=132, [168]=12 (fixed marker in commands),
  [169..170] = CRC-16/MODBUS over bytes 0..168, little-endian.
Fields: [4] on/off, [5] program (0 off / 1 on / 2 external thermostat), [6] fan 0-5,
        [7-8] and [11-12] set temperature x10 (16-bit little-endian, written twice),
        [19] auto-off, [41-166] weekly schedule.

Usage:
  stove_command.py <ip> --set-temp 22.0 [--send]
  stove_command.py <ip> --on | --off [--send]
Without --send it only prints the URL (dry run).
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_settings(ip):
    with urllib.request.urlopen(f"http://{ip}/read?byte=49", timeout=8) as r:
        data = json.load(r)["data"]
    frame = [int(x) for x in data.split(",") if x != ""][:171]
    if len(frame) != 171 or frame[0] != 49 or frame[2] != 166:
        raise ValueError(f"unexpected settings frame header: {frame[:4]}")
    # verify the stove's own checksums before trusting the content
    crc_a = crc16_modbus(frame[3:167])
    crc_b = crc16_modbus(frame[0:169])
    if frame[167:169] != [crc_a & 0xFF, crc_a >> 8] or frame[169:171] != [crc_b & 0xFF, crc_b >> 8]:
        raise ValueError("settings frame failed its CRC check; not building a command from it")
    return frame


def build_command(settings, on=None, set_temp=None):
    f = list(settings)
    f[0], f[1], f[2], f[3] = 50, 0, 166, 0
    if on is not None:
        f[4] = 1 if on else 0
    if set_temp is not None:
        v = round(set_temp * 10)
        if not 100 <= v <= 350:
            raise ValueError("set temperature must be between 10.0 and 35.0 °C")
        f[7], f[8] = v & 0xFF, v >> 8
        f[11], f[12] = v & 0xFF, v >> 8
    f[167], f[168] = 132, 12
    crc = crc16_modbus(f[0:169])
    f[169], f[170] = crc & 0xFF, crc >> 8
    return f


def command_url(ip, frame):
    s = ",".join(f"{b:03d}" for b in frame)
    return f"http://{ip}/serialflush?" + urllib.parse.urlencode({"str": s})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ip")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--set-temp", type=float)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    p.add_argument("--send", action="store_true", help="actually send (default: dry run)")
    a = p.parse_args()

    settings = read_settings(a.ip)
    on = True if a.on else False if a.off else None
    cmd = build_command(settings, on=on, set_temp=a.set_temp)
    changed = [i for i in range(169) if cmd[i] != settings[i]]
    print("current : on=%d program=%d fan=%d set=%.1f" % (settings[4], settings[5], settings[6], (settings[8] * 256 + settings[7]) / 10))
    print("changed bytes (excluding header/marker/CRC):", [i for i in changed if i not in (0, 167, 168)])
    url = command_url(a.ip, cmd)
    print(url)
    if a.send:
        with urllib.request.urlopen(url, timeout=8) as r:
            print("HTTP", r.status, r.read()[:200])
    else:
        print("(dry run, nothing sent)")


if __name__ == "__main__":
    sys.exit(main())
