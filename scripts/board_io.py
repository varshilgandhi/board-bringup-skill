#!/usr/bin/env python3
"""ESP32 bring-up checks for on-board parts that need no extra wiring, run through MicroPython.

  display  - SPI write path: init the SPI display and draw three colour bands (red / green / blue,
             top to bottom). The laptop cannot see the screen, so the person confirms what shows.
  buttons  - GPIO input path: watch the board's buttons for a few seconds and report each press.
  sensors  - read real values from the I2C chips the profile lists (power chip, motion sensor, clock),
             to show they work and not only that they answer. Writes only harmless setup registers
             (ADC enable, sensor on); no power rails are touched.
  radio    - Wi-Fi + Bluetooth LE scan: proves the RF path and antenna. Reports counts, best signal
             and channels only - never network names or device addresses.

Pins come from the board profile (boards/*.json). Exit code 0 when the check passed on the board
side; for the display the human still has to confirm the colours.
"""
import argparse
import json
import re
import sys

try:
    import serial
except ImportError:
    print("pyserial is missing. Install it with:  pip install pyserial")
    sys.exit(2)

from mpy_repl import connect

# ST7789 over 4-wire SPI. Bands are written in 4-line chunks to keep the board's RAM use small.
DISPLAY_CODE = """
import time, json
from machine import Pin, SPI
c = {cfg}
# miso=None matters: left out, MicroPython falls back to a default MISO pin, which on the C6 is GPIO2 -
# the display's MOSI. The pad then turns into an input and the panel never sees the data.
miso = Pin(c['miso']) if c.get('miso') is not None else None
spi = SPI(1, baudrate=c['freq_hz'], polarity=0, phase=0, sck=Pin(c['sck']), mosi=Pin(c['mosi']), miso=miso)
cfg_str = str(spi)
cs = Pin(c['cs'], Pin.OUT, value=1); dc = Pin(c['dc'], Pin.OUT, value=0)
rst = Pin(c['rst'], Pin.OUT, value=1); bl = Pin(c['backlight'], Pin.OUT, value=0)
def cmd(op, data=b''):
    cs(0); dc(0); spi.write(bytes([op]))
    if data:
        dc(1); spi.write(data)
    cs(1)
rst(0); time.sleep_ms(20); rst(1); time.sleep_ms(120)
cmd(0x01); time.sleep_ms(150)              # software reset
cmd(0x11); time.sleep_ms(120)              # sleep out
cmd(0x3A, b'\\x55')                        # 16 bits per pixel
cmd(0x36, b'\\x00')                        # memory order: top to bottom, RGB
cmd(0x21 if c['invert'] else 0x20)         # colour inversion (IPS panels need it on)
cmd(0x29)                                  # display on
W, H, OX, OY = c['width'], c['height'], c['offset_x'], c['offset_y']
def band(y0, y1, color):
    x0, x1, ya, yb = OX, OX + W - 1, OY + y0, OY + y1 - 1
    cmd(0x2A, bytes([x0 >> 8, x0 & 255, x1 >> 8, x1 & 255]))
    cmd(0x2B, bytes([ya >> 8, ya & 255, yb >> 8, yb & 255]))
    cs(0); dc(0); spi.write(b'\\x2c'); dc(1)
    rows = bytes([color >> 8, color & 255]) * (W * 4)
    for _ in range((y1 - y0) // 4):
        spi.write(rows)
    cs(1)
t = time.ticks_ms()
third = (H // 3) // 4 * 4
band(0, third, 0xF800); band(third, 2 * third, 0x07E0); band(2 * third, H - (H - 2 * third) % 4, 0x001F)
ms = time.ticks_diff(time.ticks_ms(), t)
bl(1)
print('@@' + json.dumps({{'bytes': W * H * 2, 'ms': ms, 'spi': cfg_str}}) + '@@')
"""

BUTTONS_CODE = """
import time, json
from machine import Pin
names = {names}
pins = {{n: Pin(p, Pin.IN, Pin.PULL_UP) for n, p in names.items()}}
idle = {{n: p.value() for n, p in pins.items()}}
last = dict(idle); presses = {{n: 0 for n in names}}
end = time.ticks_add(time.ticks_ms(), {ms})
while time.ticks_diff(end, time.ticks_ms()) > 0:
    for n, p in pins.items():
        v = p.value()
        if v != last[n]:
            if v != idle[n]:
                presses[n] += 1
            last[n] = v
    time.sleep_ms(10)
print('@@' + json.dumps({{'idle': idle, 'presses': presses}}) + '@@')
"""

RADIO_CODE = """
import time, json
r = {{}}
try:
    import network
    w = network.WLAN(network.STA_IF); w.active(True)
    t = time.ticks_ms(); nets = w.scan(); r['wifi_ms'] = time.ticks_diff(time.ticks_ms(), t)
    r['wifi'] = [[n[2], n[3]] for n in nets]          # channel, RSSI only
    w.active(False)
except Exception as e:
    r['wifi_error'] = repr(e)
try:
    import bluetooth
    ble = bluetooth.BLE(); ble.active(True)
    seen = {{}}
    def irq(ev, data):
        if ev == 5:                                     # _IRQ_SCAN_RESULT
            seen[bytes(data[1])] = max(data[3], seen.get(bytes(data[1]), -200))
    ble.irq(irq); ble.gap_scan({ms}, 30000, 30000)
    time.sleep_ms({ms} + 300)
    r['ble'] = list(seen.values())                     # RSSI only
    ble.active(False)
except Exception as e:
    r['ble_error'] = repr(e)
print('@@' + json.dumps(r) + '@@')
"""

# Generic register plan, run on the board: [name, addr, writes, reads, wait_ms]
# writes: [reg, value, "set" | "or"]; reads: [reg, nbytes]. Decoding happens on the laptop.
SENSORS_CODE = """
import time, json
from machine import I2C, Pin
i2c = I2C(0, sda=Pin({sda}), scl=Pin({scl}), freq=100000)
out = {{}}
for name, addr, writes, reads, wait in {plan}:
    try:
        for reg, val, mode in writes:
            if mode == 'or':
                val |= i2c.readfrom_mem(addr, reg, 1)[0]
            i2c.writeto_mem(addr, reg, bytes([val]))
        time.sleep_ms(wait)
        out[name] = [list(i2c.readfrom_mem(addr, reg, n)) for reg, n in reads]
    except Exception as e:
        out[name] = repr(e)
print('@@' + json.dumps(out) + '@@')
"""


def s16(lo, hi):
    v = lo | hi << 8
    return v - 65536 if v & 0x8000 else v


def bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


CHARGE_STATE = {0: "trickle charging", 1: "pre-charging", 2: "charging (constant current)",
                3: "charging (constant voltage)", 4: "charge done", 5: "not charging"}


# Each sensor type: plan entries for the board, and a decoder that turns raw bytes into a result line.
def plan_axp2101(addr):
    # 0x30: ADC channel enable (bit0 VBAT, bit2 VBUS). 0x00/0x01 status, 0x34/0x38 ADC, 0xA4 fuel gauge.
    return [["pmic", addr, [[0x30, 0x05, "or"]], [[0x00, 1], [0x01, 1], [0x34, 2], [0x38, 2], [0xA4, 1]], 50]]


def decode_axp2101(raw):
    r = raw["pmic"]
    st1, st2, vbat, vbus, pct = r[0][0], r[1][0], r[2], r[3], r[4][0]
    vbat_mv = (vbat[0] & 0x3F) << 8 | vbat[1]
    vbus_mv = (vbus[0] & 0x3F) << 8 | vbus[1]
    usb = bool(st1 & 0x20)
    bat = bool(st1 & 0x08)
    parts = [f"USB power {'present' if usb else 'NOT present'} ({vbus_mv / 1000:.2f} V)"]
    if bat:
        parts.append(f"battery {vbat_mv / 1000:.2f} V, {pct}%, {CHARGE_STATE.get(st2 & 0x07, 'unknown state')}")
    else:
        parts.append("no battery connected")
    status = "ok"
    fix = None
    if not usb:
        status, fix = "warn", "the board is running but the power chip does not see USB power - check VBUS wiring"
    elif not 4400 <= vbus_mv <= 5500:
        status, fix = "warn", f"USB voltage {vbus_mv / 1000:.2f} V is outside 4.4-5.5 V - weak port, long cable or hub"
    return status, "power: " + ", ".join(parts), fix


def plan_qmi8658(addr):
    # CTRL1 0x02: register auto-increment, little endian. CTRL2 0x03: accel +-2 g. CTRL7 0x08: accel on.
    return [["imu", addr, [[0x02, 0x40, "set"], [0x03, 0x03, "set"], [0x08, 0x01, "set"]], [[0x35, 6]], 100]]


def decode_qmi8658(raw):
    b = raw["imu"][0]
    g = [s16(b[i], b[i + 1]) / 16384 for i in (0, 2, 4)]
    mag = sum(x * x for x in g) ** 0.5
    axis = max(range(3), key=lambda i: abs(g[i]))
    facing = f"{'+' if g[axis] > 0 else '-'}{'XYZ'[axis]}"
    line = f"motion: gravity {mag:.2f} g (x {g[0]:+.2f}, y {g[1]:+.2f}, z {g[2]:+.2f}), {facing} points up"
    if 0.85 <= mag <= 1.15:
        return "ok", line, None
    if mag < 0.05:
        return "fail", line, "reads zero - sensor not switched on, or the setup writes did not reach it"
    return "warn", line, "gravity should read about 1.00 g on a still board - wrong range setting or a damaged sensor"


def plan_pcf85063(addr):
    # 0x04 = seconds (bit7 OS = the clock stopped at some point). Read twice, 1.5 s apart.
    return [["rtc_a", addr, [], [[0x04, 1]], 0], ["rtc_b", addr, [], [[0x04, 1]], 1500]]


def decode_pcf85063(raw):
    a, b = raw["rtc_a"][0][0], raw["rtc_b"][0][0]
    step = (bcd(b & 0x7F) - bcd(a & 0x7F)) % 60
    if step not in (1, 2):
        return "fail", f"clock: seconds went {bcd(a & 0x7F)} -> {bcd(b & 0x7F)} in 1.5 s, not ticking",             "the 32.768 kHz crystal is not running - check it is fitted and soldered"
    if a & 0x80:
        return "warn", f"clock: ticking (+{step} s in 1.5 s), but its stop flag is set",             "it lost power at some point (no backup battery) - set the time once; normal on a new board"
    return "ok", f"clock: ticking (+{step} s in 1.5 s)", None


SENSORS = {"axp2101": (plan_axp2101, decode_axp2101), "qmi8658": (plan_qmi8658, decode_qmi8658),
           "pcf85063": (plan_pcf85063, decode_pcf85063)}


def run_sensors(repl, bus, sensors):
    plan, decoders = [], []
    for name, cfg in sensors.items():
        if cfg["type"] not in SENSORS:
            continue
        p, d = SENSORS[cfg["type"]]
        entries = p(int(cfg["addr"], 16))
        plan += entries
        decoders.append((name, cfg["type"], [e[0] for e in entries], d))
    try:
        raw = result_of(*repl.exec(SENSORS_CODE.format(sda=bus["sda"], scl=bus["scl"], plan=repr(plan)), timeout=15))
    except RuntimeError as e:
        return [("fail", f"board-side error: {e}", None)]
    out = []
    for name, typ, keys, decode in decoders:
        errs = [raw[k] for k in keys if isinstance(raw.get(k), str)]
        if errs:
            out.append(("fail", f"{name} ({typ}): could not read it: {errs[0]}", "it answered the scan - check the profile address"))
            continue
        try:
            out.append(decode(raw))
        except (KeyError, IndexError, TypeError) as e:
            out.append(("fail", f"{name} ({typ}): unexpected reply ({e!r})", None))
    return out


def result_of(out, err):
    if err.strip():
        raise RuntimeError(err.strip().splitlines()[-1])
    return json.loads(out.split("@@")[1])


def run_display(repl, cfg):
    try:
        r = result_of(*repl.exec(DISPLAY_CODE.format(cfg=repr(cfg)), timeout=20))
    except RuntimeError as e:
        return "fail", f"board-side error: {e}", "check the SPI pin numbers in the board profile"
    pins = dict(re.findall(r"(sck|mosi|miso)=(-?\d+)", r["spi"]))
    used = [v for v in pins.values() if v != "-1"]
    if len(used) != len(set(used)):
        return "fail", f"SPI pins clash on the board: {r['spi']}", "give every SPI signal its own GPIO (miso=None if unused)"
    kbps = r["bytes"] * 8 / max(r["ms"], 1)
    return ("ok", f"{r['bytes']} bytes sent over SPI in {r['ms']} ms ({kbps / 1000:.1f} Mbit/s, {r['spi']}). "
                  "The screen should show RED on top, GREEN in the middle, BLUE at the bottom.",
            "black screen: backlight pin or panel power. White/garbage: CS, DC or RST pin wrong.\n"
            "colours look like cyan/magenta/yellow: flip 'invert'. Red and blue swapped: RGB/BGR order.\n"
            "image shifted with a stripe of noise: offset_x/offset_y.")


def run_buttons(repl, names, seconds):
    try:
        r = result_of(*repl.exec(BUTTONS_CODE.format(names=repr(names), ms=int(seconds * 1000)),
                                 timeout=seconds + 10))
    except RuntimeError as e:
        return [("fail", f"board-side error: {e}", None)]
    out = []
    for n, gpio in names.items():
        idle, count = r["idle"][n], r["presses"][n]
        level = "high" if idle else "low (active-high button, or held down when the check started)"
        if count:
            out.append(("ok", f"{n} (GPIO{gpio}): {count} press(es) seen, idles {level}", None))
        else:
            out.append(("fail", f"{n} (GPIO{gpio}): no press seen in {seconds:.0f} s, idles {level}",
                        "pressed during the window? if yes: wrong GPIO in the profile, or the button\n"
                        "is wired to another chip (e.g. a PMIC power key) and not to the ESP32"))
    return out


def run_radio(repl, seconds):
    try:
        r = result_of(*repl.exec(RADIO_CODE.format(ms=int(seconds * 1000)), timeout=seconds + 30))
    except RuntimeError as e:
        return [("fail", f"board-side error: {e}", None)]
    out = []
    if "wifi_error" in r:
        out.append(("fail", f"Wi-Fi: could not start the radio: {r['wifi_error']}", None))
    elif r["wifi"]:
        best = max(rssi for _, rssi in r["wifi"])
        chans = sorted({ch for ch, _ in r["wifi"]})
        out.append(("ok", f"Wi-Fi: {len(r['wifi'])} network(s) seen, strongest {best} dBm, "
                          f"channels {', '.join(map(str, chans))} (scan {r['wifi_ms']} ms)", None))
    else:
        out.append(("warn", "Wi-Fi: radio started but no network seen",
                    "no access point nearby, or antenna problem (a phone hotspot next to the board is a quick test)"))
    if "ble_error" in r:
        out.append(("warn", f"Bluetooth: not available in this firmware: {r['ble_error']}", None))
    elif r["ble"]:
        out.append(("ok", f"Bluetooth LE: {len(r['ble'])} device(s) seen in {seconds:.0f} s, "
                          f"strongest {max(r['ble'])} dBm", None))
    else:
        out.append(("warn", f"Bluetooth LE: radio started but nothing seen in {seconds:.0f} s",
                    "nothing advertising nearby, or antenna problem (a phone with Bluetooth on is a quick test)"))
    # A strong best signal with a working scan says the antenna is fine; a very weak best one is worth a note.
    if r.get("wifi") and max(rssi for _, rssi in r["wifi"]) < -85:
        out.append(("warn", "Wi-Fi: every network is very weak (< -85 dBm)",
                    "check the antenna (chip antenna area not covered by metal, u.FL antenna plugged in if the board has one)"))
    return out


def main():
    ap = argparse.ArgumentParser(description="ESP32 on-board SPI display and button checks (MicroPython)")
    ap.add_argument("check", choices=["display", "buttons", "radio", "sensors"])
    ap.add_argument("--port", required=True)
    ap.add_argument("--board", required=True, help="board profile JSON (boards/*.json)")
    ap.add_argument("--seconds", type=float, default=None, help="button watch / BLE scan window")
    args = ap.parse_args()

    with open(args.board) as fh:
        profile = json.load(fh)
    try:
        repl = connect(args.port)
    except (serial.SerialException, TimeoutError) as e:
        print(f"[FAIL] MicroPython REPL: {e}")
        sys.exit(1)

    icon = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    try:
        if args.check == "display":
            results = [run_display(repl, profile["spi_display"])]
        elif args.check == "sensors":
            results = run_sensors(repl, profile["i2c"][0], profile.get("sensors", {}))
        elif args.check == "radio":
            results = run_radio(repl, args.seconds or 5)
        else:
            args.seconds = args.seconds or 20
            print(f"Press each button once now ({', '.join(profile['buttons'])}) - "
                  f"watching for {args.seconds:.0f} s. Short presses only.", flush=True)
            results = run_buttons(repl, profile["buttons"], args.seconds)
    finally:
        repl.close()

    for status, detail, fix in results:
        print(f"{icon[status]} {args.check}: {detail}")
        if fix and (status != "ok" or args.check == "display"):
            for line in fix.splitlines():
                print(f"       -> {line}")
    sys.exit(0 if all(s != "fail" for s, _, _ in results) else 1)


if __name__ == "__main__":
    main()
