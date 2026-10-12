# Protocol notes

Everything here was observed on one stove (STOVEItaly / Vivace pellet stove, Wi-Fi
module firmware recent enough to expose the HTTP API). Offsets are 0-based.
"Verified" means checked against our own data. "From the app" means read in the
decompiled VivaceTheStove 1.12.1 code but not exercised on our stove.

## Transport

The Wi-Fi module is an ESP32 (Espressif MAC prefix). It does two things at the same time:

- Access point `STOVE-<serial>`, open network, module at `192.168.4.1`.
- Client on your home Wi-Fi with whatever IP DHCP gives it.

The app uses the AP when the phone is connected to it and goes through the
STOVEItaly cloud otherwise. The HTTP API below answers on both addresses.

| Request | What it does |
|---|---|
| `GET /read?byte=81` | status frame (live values) |
| `GET /read?byte=49` | settings frame (on/off, set point, fan, schedule...) |
| `GET /serialflush?str=...` | writes a settings frame (see "Commands") |
| `GET /` | 403, empty body |

There is no authentication. Reads return JSON:

```
{"data":"81,0,113,0,0,207,97,95,50,1,0,1,215,0,..."}
```

`data` holds about 355 decimal values separated by commas, with a trailing comma.
Only the first `data[2] + 5` values are the frame. The rest is buffer and usually zeros.

The app also knows `/getBeaconTemp`, `/getBeaconMacAddress`, `/getBeaconOffset` and
`/beaconOffset?beaconOffset=<n>`. These are for an optional wireless room-temperature
probe. We have no probe and didn't touch them.

## Frame layout

```
offset  0      1   2      3 .. len         len+1..len+2   len+3..len+4
        cmd    0   len    payload          CRC-A (LE)     CRC-B (LE)
```

| Frame | cmd | len | total size |
|---|---|---|---|
| status | 81 | 113 | 118 bytes |
| settings | 49 | 166 | 171 bytes |
| command (write) | 50 | 166 | 171 bytes |

## Checksum

Both CRCs are plain CRC-16/MODBUS: reflected poly `0xA001`, init `0xFFFF`, no final
XOR, stored low byte first.

```python
def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc
```

- CRC-A covers bytes `3 .. len`, so the payload only.
- CRC-B covers bytes `0 .. len+2`, so everything before it, CRC-A included. On read
  frames CRC-B is constant for a given header, because CRC-A cancels the payload out.
  It's `223,183` for status frames and `68,203` for settings frames.

Verified on 58,447 distinct status frames and every settings frame we recorded.
The app's own `calcCRC` is the same function.

On **command** frames there is no CRC-A. Bytes 167-168 hold the fixed value `132,12`
(`0x0C84`), whatever the payload. That's why the forum noticed only bytes 169-170
changing between commands.

## Status frame (`/read?byte=81`)

| Offset | Meaning | Notes |
|---|---|---|
| 5-7 | counter, seconds | 24-bit LE. Not an uptime we could match to anything. |
| 9 | on/off as commanded | Flips within a poll of the command, even while the stove is still hot. It isn't "flame burning". |
| 10 | program | 0 off, 1 on, 2 external thermostat |
| 11 | room fan setting | 1-5 |
| 12-13 | set point ×10 | 16-bit LE. Verified: 25.7 °C reads as `1,1`. |
| 20 | auto-off | 0/1 |
| 26 | error code | Always 0 on our stove so far. See the table below. |
| 34-35 | room temperature ×10 | 16-bit LE. 25.5 °C reads as `255,0`, so only `255,255` means "no value". |
| 38 | structure temperature, °C | |
| 42-43 | smoke temperature, °C | 16-bit LE, goes above 255 °C at full power |
| 54-55 | combustion air flow, raw | 16-bit LE. **Not a temperature.** See below. |
| 64 | pellet level | The app shows it as "2/5 - 18 cm". Max seen: 38. |
| 96 | igniter | 1 for 2-9 min at each ignition or re-ignition |
| 99 | power level | 1-5 |
| 100 | auger (pellet motor) | raw |
| 101 | room fan speed | raw. About 65 at setting 1, 85 at 2, 130 at 4. |
| 102 | smoke fan speed | raw. Hits 255 during ignition. |
| 104 | ? | Small counter (0-3) that resets. Not an ignition counter here. |
| 106-107 | ? | Counter, resets irregularly, max about 182. Not a clock. |
| 114-115 | CRC-A | |
| 116-117 | CRC-B | |

Some community configs call bytes 54-55 "flow temperature". On our stove they follow
the smoke fan, with a correlation of 0.86 over about 58k samples: about 40 when off,
90 at power 1, 130 at power 5, 250-350 during the ignition blast. The igniter switching
on doesn't move the value. The smoke fan starting does, within the same 15 s poll.
So it reads like an air-flow sensor, in raw units.

## Settings frame (`/read?byte=49`) and commands

| Offset | Meaning | Values |
|---|---|---|
| 4 | on/off | 0/1 |
| 5 | program | 0 off, 1 on, 2 external thermostat |
| 6 | room fan | 1-5. The stove panel doesn't accept 0. |
| 7-8 | set point ×10 | 16-bit LE. The panel allows 5.0-35.0 °C. |
| 11-12 | set point ×10 | same value again |
| 19 | auto-off | 0/1 |
| 41-166 | weekly schedule | 7 days × 3 slots × 6 bytes, see below |
| 167-168 | CRC-A on reads, `132,12` on commands | |
| 169-170 | CRC-B | |

Schedule slot for day `d` (0 = Monday) and slot `s` (0-2) starts at `41 + 18*d + 6*s`:
`start hour, start min, stop hour, stop min, temp lo, temp hi` (temp ×10, LE). An
unused slot is six `255` bytes. This is from the app; our schedule is empty.

### Writing

A command is the settings frame you just read, with these changes:

1. byte 0 = `50`, bytes 1-3 = `0,166,0`
2. change only the field you want
3. bytes 167-168 = `132,12`
4. bytes 169-170 = CRC-16/MODBUS over bytes 0-168, LE

Then send all 171 values, zero-padded to 3 digits and comma-joined:

```
GET /serialflush?str=050,000,166,000,001,000,001,220,000,000,008,220,000,000,008,...,132,012,194,093
```

The commas are sent URL-encoded as `%2C`. That's what the app does, and the only form
we tested. The module answers
`200` with an empty body. The stove applies the change in 3-6 s. Re-read `byte=49`
to confirm.

This is also how the app does it: it keeps the last `byte=49` response in a buffer and
patches it. So **always build a command from a fresh read**. Every byte you don't
touch gets written back, including on/off. A stale copy can switch the stove on or
off, or bring back an old schedule.

Example from our stove, set point 21.5 -> 22.0 °C. The full frames are in `samples/`.

```
read  49,0,166,0,1,0,1,215,0,0,8,215,0,0,8,10,0,0,8,0,2,1,0,255, ... ,255,116,171,68,203
write 50,0,166,0,1,0,1,220,0,0,8,220,0,0,8,10,0,0,8,0,2,1,0,255, ... ,255,132,12,194,93
```

We diffed the settings frame before and after this command. Only bytes 7 and 11
changed, plus the CRC-A the stove recomputed.

The app has a second layout for stoves configured as "version 1": len 95, bytes 1 and
3 = 1, CRC over 0-353, 356 bytes sent, schedule at `72 + 40*d + 8*s`. Untested, since
we don't have one.

## Error codes

From the HACF thread. Not verified, because our stove never reported an error.

| Code | Meaning |
|---|---|
| 1 | Fire went out (no pellets, sawdust in the hopper) |
| 2 | Ignition failed |
| 3 | Not enough air intake (door, ash drawer, blocked flue) |
| 4 / 5 | Probe Tc1 / Tc2 disconnected or broken |
| 6 | Mains sync error |
| 7 / 8 | EEPROM error |
| 9 | Pressure sensor fault |
| 10 / 16 | Power cut |

## Open questions

- Bytes 5-7, 104 and 106-107 of the status frame.
- Whether the "version 1" layout exists in the wild and what uses it.
- The probe ("beacon") endpoints.
- Clock sync, cleaning reset and hour counters (`sendClock`, `sendClean`, `sendHours` in
  the app). They build their own short frames, not the 171-byte settings frame.
- Writing the chrono program, chrono activation (byte 5) and auto-off (byte 19). The
  layouts come from the app, but nothing has been written to a stove yet.
- Whether older Wi-Fi module firmware (before the HTTP API) speaks the same frames
  over raw TCP. The app has a socket fallback for it.
