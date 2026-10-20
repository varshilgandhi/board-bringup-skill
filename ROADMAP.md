# Roadmap

v1 works end to end on one board (Waveshare ESP32-C6 Touch LCD 1.83, native USB, Linux).
Everything below is open. Pick one, open an issue saying you're on it, and send a PR.

## Not tested on real hardware yet

- **Classic ESP32 with a USB-serial chip (CP210x, CH340/CH9102, FTDI).** The code path exists
  (`/dev/ttyUSB*`, baud detection, brltty check) but has never run on such a board. A real boot log
  from one would also replace the hand-written classic logs in `tests/test_uart_check.py`.
- **Windows.** Port discovery uses pyserial and should work; the driver, permission and busy checks
  are Linux-only today. Needs: COM port listing with VID:PID, Device Manager hints that are right,
  and a run on a real machine.
- **macOS.** Same as Windows, `/dev/cu.*` ports.
- **ESP32-S3 / C3 / S2 boards.** Need a MicroPython image name per chip and a board profile.

## Checks still missing

- **SPI loopback** (MOSI wired to MISO with a jumper): proves the read side of SPI. Today only the
  write side is checked, through the on-board display.
- **Touch read** (CST816 on the C6 board): tap the screen, read the coordinates.
- **Audio** (ES8311 / ES7210): play a tone, record a second, check the level.
- **Generic GPIO check** for boards without a profile: which pins are free, which are strapping pins.

## Making it easier to start

- **More board profiles** in `boards/` - pins, expected I2C devices, ID registers. One JSON per board,
  verified on the real board (see the `verified` fields in the existing profile).
- **Flash MicroPython from the skill** with a confirm step and an automatic backup first.
