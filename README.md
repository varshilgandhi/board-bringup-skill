# board-bringup

A [Claude Code](https://claude.com/claude-code) skill for the first hour with a new ESP32 board.
Plug the board in, ask Claude to check it, and get a plain answer: what works, and for the first
thing that doesn't, the exact fix.

```
==== Bring-up summary ====
[ OK ] UART         ESP32-C6 (QFN40) (revision v0.2) on /dev/ttyACM0
[ OK ] I2C          6 device(s) on 1 bus(es)
[WARN] Sensors      2/3 read OK
[ OK ] Radio        Wi-Fi: 27 network(s) seen; Bluetooth LE: 41 device(s) seen in 5 s
[SKIP] SPI display  needs a person to look - run with --interactive
[SKIP] Buttons      needs a person to press - run with --interactive
```

## What it checks

- **UART link, from the laptop side:** is the board on USB, which USB-serial chip, driver loaded,
  which port, can you open it (Linux `dialout`), is something else holding it, the boot log after a
  reset, the baud rate, the chip id (esptool).
- **On the board, through MicroPython:** I2C line levels and bus scan against the devices the board
  should have (with ID registers), real readings from those chips (power, motion, clock), an SPI
  display, buttons, and a Wi-Fi / Bluetooth scan.

It never writes to flash. Board-side checks need MicroPython on the board; SKILL.md says how to back
up the current firmware first.

## Use it

```bash
pip install pyserial esptool
git clone https://github.com/varshilgandhi/board-bringup-skill ~/.claude/skills/board-bringup
```

Then in Claude Code: *"I just plugged in my ESP32, check it."*

Or without Claude:

```bash
python3 scripts/bringup.py                 # everything, in order, with a summary
python3 scripts/bringup.py --interactive   # plus the screen and button checks
```

Tests (offline, no board needed): `python3 -m unittest discover tests`

## Status

Tested on one board: Waveshare ESP32-C6 Touch LCD 1.83, Ubuntu. Classic ESP32 boards with a
CP210x/CH340 chip, Windows and macOS are not tested yet. See [ROADMAP.md](ROADMAP.md) - help welcome.

MIT licence.
