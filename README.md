# stoveitaly-local

Local control of STOVEItaly / Vivace pellet stoves over the stove's Wi-Fi module.
Plain HTTP on your LAN, no cloud.

- [PROTOCOL.md](PROTOCOL.md): endpoints, frame layout, checksum, byte maps
- `tools/stove_command.py`: read the stove, send commands
- `tools/crc_polysearch.py`: recover the CRC from captured frames
- `homeassistant/`: package with sensors, set point, fan, on/off, event log, dashboard card

Tested on one stove. If yours differs, open an issue with a few `/read?byte=81` and
`/read?byte=49` responses.

## Status

| | |
|---|---|
| Status read (temperatures, fans, pellets, igniter, errors) | works |
| Set point, fan speed | works |
| On/off | works |
| Chrono program (weekly schedule, 3 slots/day) | layout known from the app, no writer, not tested |
| Chrono activation (off / on / external thermostat) | byte known, not tested |
| Auto-off | byte known, not tested |
| Clock sync, cleaning reset | separate frames in the app, not decoded |
| Error codes | from the forum, not seen yet |

## Quick start

```
$ curl -s 'http://192.168.1.50/read?byte=81'
{"data":"81,0,113,0,0,207,97,95,50,1,0,1,215,0,0,8,10,0,0,8,0,2,1,1,2,0,..."}

$ python3 tools/stove_command.py 192.168.1.50 --set-temp 22.0          # dry run
$ python3 tools/stove_command.py 192.168.1.50 --set-temp 22.0 --send
```

## Home Assistant

Copy `stove_control.py` and `stove_logger.py` into your config directory and
`packages/vivace_stove.yaml` into `config/packages/`. Set your stove's IP in the
package and in `stove_control.py`, enable packages
(`homeassistant: packages: !include_dir_named packages`), restart. The dashboard card
is in `dashboard_card.yaml`.

Like the app, button presses are batched and only the final value is sent. Power
changes are limited to one every 2 minutes.

## How this was worked out

Felipe and govenants on the
[HACF forum](https://forum.hacf.fr/t/recuperation-donnee-depuis-une-requete-http/31735)
found `/read?byte=81`, mapped most bytes and found the `/serialflush` write. Replaying
captured app requests works, but the checksum was unknown, so you couldn't build new
commands.

Ten days of Home Assistant polling gave about 58,000 distinct status frames. For
equal-length frames `crc(a) ^ crc(b) == crc0(a ^ b)`, which removes init and final
XOR. Brute-forcing all 65,536 polynomials left one: CRC-16/MODBUS.

The write format was confirmed by decompiling the Flutter app (VivaceTheStove 1.12.1)
with [blutter](https://github.com/worawit/blutter).

## Safety

A command rewrites all settings, not just one field. Always build it from a fresh
`byte=49` read; the tools here do. The HTTP API has no authentication, and the stove's
`STOVE-...` access point is open. No affiliation with STOVEItaly. Use at your own risk.

## License

MIT
