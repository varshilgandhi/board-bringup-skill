#!/usr/bin/env python3
"""ESP32 I2C bring-up check, run through MicroPython on the board.

The board must be running MicroPython (see SKILL.md for flashing). This script talks to its
raw REPL over the serial port, so there is nothing to compile on the laptop.

Per bus it checks, in order:
1. idle line levels - SDA/SCL must read high when nobody drives them
   (low = missing pull-up, short to GND, or a device holding the bus)
2. bus scan at 100 kHz
3. found addresses against the board profile (expected / missing / unexpected)

Exit code 0 when every bus is up, 1 when something is broken.
"""
import argparse
import json
import sys

try:
    import serial
except ImportError:
    print("pyserial is missing. Install it with:  pip install pyserial")
    sys.exit(2)

from mpy_repl import RawRepl

# Common 7-bit addresses -> what usually lives there. Used for unknown buses and unexpected finds.
KNOWN_ADDR = {
    0x0C: "AK8963 magnetometer", 0x10: "VEML7700 light sensor", 0x15: "CST816 touch",
    0x18: "ES8311 codec / LIS3DH accel", 0x19: "LIS3DH accel (alt)", 0x1D: "ADXL345 accel (alt)",
    0x20: "PCF8574 / MCP23017 IO expander", 0x23: "BH1750 light sensor", 0x27: "PCF8574 LCD backpack",
    0x29: "VL53L0X distance / TSL2591", 0x34: "AXP2101 / AXP192 power management", 0x38: "FT6x36 touch / AHT20",
    0x3C: "SSD1306 OLED", 0x3D: "SSD1306 OLED (alt)", 0x40: "ES7210 mic ADC / INA219 / HTU21D / SHT20",
    0x44: "SHT3x humidity", 0x48: "ADS1115 ADC / TMP102", 0x51: "PCF85063 / PCF8563 RTC",
    0x53: "ADXL345 accel", 0x57: "AT24C32 EEPROM", 0x5A: "MPR121 touch / MLX90614", 0x5D: "GT911 touch",
    0x62: "SCD40 CO2", 0x68: "MPU6050 / DS3231 RTC / ICM-20948", 0x69: "MPU6050 (alt)",
    0x6A: "QMI8658 / LSM6DS IMU", 0x6B: "QMI8658 / LSM6DS IMU (alt)", 0x76: "BME280 / BMP280",
    0x77: "BME280 / BMP280 (alt) / BMP180",
}

# Runs on the board. Kept small: the raw REPL echoes nothing, prints come back as stdout.
BOARD_CODE = """
import machine, json
sda, scl = {sda}, {scl}
ids = {ids}
r = {{}}
def lvl(p, pull):
    return machine.Pin(p, machine.Pin.IN, pull).value()
r['sda_idle'] = lvl(sda, None); r['scl_idle'] = lvl(scl, None)
r['sda_pullup'] = lvl(sda, machine.Pin.PULL_UP); r['scl_pullup'] = lvl(scl, machine.Pin.PULL_UP)
try:
    i2c = machine.I2C(0, sda=machine.Pin(sda), scl=machine.Pin(scl), freq=100000, timeout=50000)
    r['found'] = i2c.scan()
    r['ids'] = {{}}
    for a, reg in ids:
        if a in r['found']:
            try:
                r['ids'][str(a)] = i2c.readfrom_mem(a, reg, 1)[0]
            except Exception:
                r['ids'][str(a)] = -1
except Exception as e:
    r['error'] = repr(e)
print('@@' + json.dumps(r) + '@@')
"""


def check_bus(repl, bus):
    who = {int(a, 16): (int(v["reg"], 16), int(v["value"], 16)) for a, v in bus.get("who_am_i", {}).items()}
    ids = [(a, reg) for a, (reg, _) in who.items()]
    out, err = repl.exec(BOARD_CODE.format(sda=bus["sda"], scl=bus["scl"], ids=ids))
    if err.strip():
        return {"status": "fail", "detail": "board-side error: " + err.strip().splitlines()[-1]}
    r = json.loads(out.split("@@")[1])
    name = f"I2C {bus.get('bus', '')} (SDA=GPIO{bus['sda']}, SCL=GPIO{bus['scl']})".replace("  ", " ")
    res = {"bus": name, "raw": r}

    stuck = [(line, r[f"{line}_idle"], r[f"{line}_pullup"]) for line in ("sda", "scl") if not r[f"{line}_idle"]]
    for line, idle, pulled in stuck:
        if pulled:
            res.update(status="fail", detail=f"{line.upper()} has no pull-up resistor (reads low until the "
                                             f"chip's weak internal pull-up is on)",
                       fix="fit 4.7k pull-ups from SDA and SCL to 3.3V (or check they are populated)")
        else:
            res.update(status="fail", detail=f"{line.upper()} is held low even with the pull-up on",
                       fix=f"short to GND, a device holding the bus (power-cycle the board), "
                           f"or GPIO{bus[line]} is the wrong pin for {line.upper()}")
        return res
    if "error" in r:
        res.update(status="fail", detail="scan failed on the board: " + r["error"])
        return res

    found = sorted(r["found"])
    expected = {int(k, 16): v for k, v in bus.get("expected", {}).items()}
    res["found"] = [{"addr": f"0x{a:02x}", "likely": expected.get(a) or KNOWN_ADDR.get(a, "unknown device")}
                    for a in found]
    wrong = []
    for f in res["found"]:
        a = int(f["addr"], 16)
        if a in who:
            got = r.get("ids", {}).get(str(a), -1)
            f["id_check"] = "matched" if got == who[a][1] else f"reg 0x{who[a][0]:02x} = 0x{got & 0xff:02x}, expected 0x{who[a][1]:02x}"
            if got != who[a][1]:
                wrong.append(f["addr"])
    res["missing"] = [{"addr": f"0x{a:02x}", "device": d} for a, d in sorted(expected.items()) if a not in found]
    if not found:
        res.update(status="fail", detail="lines idle high but no device answered",
                   fix="device not powered (check its supply/enable pin), SDA and SCL swapped, or wrong pins")
    elif wrong:
        res.update(status="warn", detail=f"device(s) at {', '.join(wrong)} answered but their ID register is wrong",
                   fix="a different chip sits at that address than the board profile says (other part fitted,\n"
                       "or a second-source chip). Check the part marking against the schematic.")
    elif res["missing"]:
        res.update(status="warn", detail=f"{len(found)} device(s) answered, {len(res['missing'])} expected one(s) did not",
                   fix="a missing device is usually unpowered (a PMIC rail or enable pin not switched on yet),\n"
                       "at a different address (ADDR pin strap), or not fitted on this board revision")
    else:
        res.update(status="ok", detail=f"{len(found)} device(s) answered")
    return res


def load_profile(path):
    with open(path) as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(description="ESP32 I2C bring-up check (board must run MicroPython)")
    ap.add_argument("--port", required=True)
    ap.add_argument("--board", help="board profile JSON (boards/*.json)")
    ap.add_argument("--sda", type=int)
    ap.add_argument("--scl", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.board:
        profile = load_profile(args.board)
        buses = profile["i2c"]
    elif args.sda is not None and args.scl is not None:
        profile, buses = {"name": "custom pins"}, [{"bus": "", "sda": args.sda, "scl": args.scl}]
    else:
        ap.error("give --board or both --sda and --scl")

    try:
        repl = RawRepl(args.port)
        repl.enter()
    except (serial.SerialException, TimeoutError) as e:
        print(f"[FAIL] MicroPython REPL: {e}")
        print("       -> is MicroPython flashed? run uart_check.py - the boot log should say MicroPython.")
        sys.exit(1)

    results = []
    try:
        for bus in buses:
            results.append(check_bus(repl, bus))
    finally:
        repl.close()

    if args.json:
        print(json.dumps({"board": profile["name"], "buses": results}, indent=2))
    else:
        icon = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
        print(f"Board: {profile['name']}")
        for r in results:
            print(f"{icon[r['status']]} {r.get('bus', 'I2C')}: {r['detail']}")
            for f in r.get("found", []):
                idc = f" (ID {f['id_check']})" if f.get("id_check") else ""
                print(f"         found   {f['addr']}  {f['likely']}{idc}")
            for m in r.get("missing", []):
                print(f"         missing {m['addr']}  {m['device']}")
            if r.get("fix"):
                for line in r["fix"].splitlines():
                    print(f"       -> {line}")
        up = all(r["status"] != "fail" for r in results)
        print("\nI2C: " + ("UP" if up else "NOT UP - fix the first [FAIL] above and run again"))
    sys.exit(0 if all(r["status"] != "fail" for r in results) else 1)


if __name__ == "__main__":
    main()
