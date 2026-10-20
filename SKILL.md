---
name: board-bringup
description: Bring up a new ESP32-family board plugged in over USB and say in plain words what works and what is missing - USB enumeration, USB-serial chip, driver, port, permission, busy port, boot log, baud, chip id. Use when someone plugs in an ESP32 / ESP32-S3 / C3 / C6 board and asks "is it working", "why no serial output", "port not showing", "permission denied on ttyUSB0", "garbage on serial monitor", "esptool failed to connect", or wants a board bring-up check. Covers the UART link, I2C bus scans, an SPI display, buttons and a Wi-Fi/BLE scan (board side through MicroPython); SPI loopback comes next.
---

# Board bring-up (ESP32, v1: UART, I2C, SPI display, buttons)

Goal: the engineer plugs the board in and gets one plain answer - **UART up**, or the exact
broken link and the command that fixes it. Walk the chain in order; stop at the first break.

## Run it

One command runs everything in order and ends with a summary:

```bash
python3 scripts/bringup.py                 # UART, then I2C, sensors + radio if the board runs MicroPython
python3 scripts/bringup.py --interactive   # also SPI display (person looks) and buttons (person presses)
```

It never writes to flash. Without MicroPython on the board it stops after UART and says why. The
board profile is matched by chip type, or pass `--board boards/<board>.json`.

Each part also runs on its own:

```bash
python3 scripts/uart_check.py            # finds the port, resets the board, reads the boot log, runs esptool chip-id
python3 scripts/uart_check.py --port /dev/ttyUSB0 --json
```

Needs `pyserial`. `esptool` is optional (`uv tool install esptool` or `pip install esptool`); without
it the chip-id step is skipped, not failed.

Exit 0 = UART up. The script resets the board (RTS pulse, DTR low, so it boots from flash) and
esptool resets it again at the end. It never writes to flash.

## What to do with the result

Read the report to the user in plain words. Lead with the verdict, then the first `[FAIL]` and its
fix. Don't paste the whole boot log; quote the lines that matter (reset reason, app name, any
panic/brownout/invalid header line).

When a fix needs `sudo` or a re-login, the user must run it - suggest `! <command>` so it runs in the
session. After a `dialout` group change, the current session can use `sg dialout -c "..."` to re-run
without logging out.

Ask the user to plug in or unplug the board when a step needs it; never assume.

## The chain and what each break means

| Step | Broken looks like | Usual cause, in order of likelihood |
|---|---|---|
| USB | nothing on USB | charge-only cable, dead port/hub, board not powered, wrong USB-C port on two-port boards |
| USB | kernel "device descriptor read error" | bad cable, weak power, hub |
| USB-serial chip | known VID:PID | see table in `scripts/uart_check.py` (`KNOWN_USB`) |
| Driver | on USB but no tty | module not loaded; **brltty grabbing CH340** on Ubuntu; Windows: missing CP210x/CH340 driver |
| Permission | "Permission denied" | user not in `dialout`, or added but not re-logged in |
| Busy | can't open | serial monitor still open (Arduino IDE, screen, idf.py monitor), ModemManager probing |
| Boot log | silent | no auto-reset (press RST), log on other pins, console disabled, no power |
| Boot log | junk at every baud | wrong baud set by the app, loose GND, TX/RX not crossed |
| Boot log | `DOWNLOAD` boot mode | BOOT/IO0 (IO9 on C3/C6) held low |
| Boot log | `invalid header` | empty or corrupt flash |
| Boot log | brownout / repeated ROM banner | weak supply - cable, port, or board draws too much |
| Chip id | esptool "Failed to connect" | auto-reset missing: hold BOOT, tap RST, release BOOT |

## Two kinds of board

- **Native USB** (VID `303a`, e.g. ESP32-C3/C6/S3 USB-Serial/JTAG): shows as `/dev/ttyACM*`, driver
  `cdc_acm`, baud does not matter, no extra chip. Keep the port open through the reset or the ROM
  lines are lost - the script does this.
- **Bridge chip** (CP210x, CH340/CH9102, FTDI): shows as `/dev/ttyUSB*` (CH9102 as `ttyACM*`), the
  script tries bauds 115200, 74880, 460800, 921600, 230400, 57600, 9600 and picks the cleanest text.

## Before anything writes to flash

The I2C/SPI checks overwrite the board's program. Before that: get the user's OK,
then back up the whole flash and verify it:

```bash
esptool --port PORT flash-id                       # size
esptool --port PORT read-flash 0 ALL backup.bin
esptool --port PORT verify-flash 0 backup.bin      # must say "digest matched"
```

Keep backups outside the repo - the `nvs` partition can hold Wi-Fi credentials. Erasing is the
user's call; if the harness blocks it, give them the exact command to run with `! ...`.

## I2C (needs MicroPython on the board)

The board-side checks run through MicroPython's raw REPL over the same serial port, so nothing is
compiled on the laptop (an ESP-IDF install is 3-5 GB; MicroPython is a 2 MB image).

1. Download the official image for the chip from `https://micropython.org/download/<BOARD>/`
   (`ESP32_GENERIC_C6`, `ESP32_GENERIC_S3`, `ESP32_GENERIC_C3`, `ESP32_GENERIC`), into an empty
   directory. Check it with `esptool image-info FILE` - the chip type must match.
2. `esptool --port PORT erase-flash && esptool --port PORT write-flash 0 FILE`
3. `uart_check.py` should now show a `MicroPython v...` line in the boot log.
4. Scan:

```bash
python3 scripts/i2c_scan.py --port PORT --board boards/<board>.json   # pins + expected devices
python3 scripts/i2c_scan.py --port PORT --sda 21 --scl 22             # any pins, no profile
```

| Result | Meaning |
|---|---|
| line low, high with pull-up on | no pull-up resistor fitted |
| line low even with pull-up | short to GND, a device holding the bus, or wrong pin |
| lines high, nothing answers | device unpowered, SDA/SCL swapped, or wrong pins |
| expected address missing | device unpowered (PMIC rail / enable pin), other ADDR strap, not fitted |
| ID register wrong | a different chip is at that address than the profile says |

Pins for a board: the vendor schematic, or the board's folder in an open firmware that supports it
(e.g. `xiaozhi-esp32/main/boards/`). Record the real scan in `boards/<board>.json` with `verified`.

## On-board parts, no wiring (needs MicroPython)

```bash
python3 scripts/board_io.py display --port PORT --board boards/<board>.json   # SPI write path
python3 scripts/board_io.py buttons --port PORT --board boards/<board>.json   # GPIO inputs
python3 scripts/board_io.py sensors --port PORT --board boards/<board>.json   # values from I2C chips
python3 scripts/board_io.py radio --port PORT --board boards/<board>.json     # Wi-Fi + BLE scan
```

- **sensors** reads values from the chips in the profile's `sensors` block (AXP2101 power chip,
  QMI8658 motion sensor, PCF85063 clock): USB voltage and battery state, gravity ~1.00 g on a still
  board, seconds ticking. Answering a scan only proves the chip is there; a value proves it works.
- **radio** scans Wi-Fi and BLE. Only counts, channels and signal strength leave the board - never
  network names or device addresses, so the output is safe to paste in a post or a ticket.

- **display** draws red / green / blue bands top to bottom on an ST7789. The laptop can't see the
  screen: ask the user what they see, and offer concrete choices ("whole screen blue" vs "blue on
  top") - a vague answer like "blue" fits several faults. Map the answer with the fix lines the
  script prints. It also fails on its own if two SPI signals share a pin.
- **buttons** watches the profile's buttons. Run it in the background, tell the user it has
  started, then ask them to press. A press is any change from the idle level, so active-high
  buttons (PWR on the C6 board idles low) count too. Warn about long presses on power keys.

Known trap: on MicroPython, `SPI(...)` without `miso=` picks a default MISO pin - on the C6 that is
GPIO2, the display's MOSI on the Waveshare board. The pad becomes an input and the panel gets no
data (seen: whole screen stayed blue). Always pass `miso=None` when nothing is read back.

## Tested on

- Waveshare ESP32-C6 Touch LCD 1.83 (ESP32-C6 rev v0.2, native USB-Serial/JTAG), Ubuntu, kernel 7.0.
  UART: up, permission/busy/unplugged failures checked. I2C: 6 devices on GPIO7/8, two confirmed by
  ID register; swapped pins and unconnected pins give the right failure. SPI display: bands confirmed
  by eye after the MISO fix. Buttons: BOOT and PWR presses seen.
  Radio: Wi-Fi and BLE scans both see networks/devices. `bringup.py` end to end in ~13 s.
  Restored the original firmware from backup (verify-flash digest matched); `bringup.py` then
  runs UART and skips the board-side checks with the reason.
  Sensors: USB 5.15 V, no battery; gravity 1.03 g; clock ticking with its stop flag set.
- Not yet tested on a bridge-chip board (CP210x/CH340) or on Windows.
