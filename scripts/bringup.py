#!/usr/bin/env python3
"""One command for the whole ESP32 bring-up: UART first, then the board-side checks.

  python3 scripts/bringup.py                       # finds the port and the board profile
  python3 scripts/bringup.py --interactive         # also the checks a person has to see or press

Order: UART chain (uart_check). If the UART is not up, stop there - nothing else can be checked.
If the board runs MicroPython: I2C scan, radio scan, SPI display (with --interactive) and buttons
(with --interactive). Without MicroPython the board-side checks are skipped with the reason; this
script never writes to flash.

Exit code 0 when nothing failed.
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_io  # noqa: E402
import i2c_scan  # noqa: E402
import uart_check  # noqa: E402
from mpy_repl import connect  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[INFO]", "skip": "[SKIP]", "look": "[LOOK]"}


def step_data(rep, name):
    merged = {}
    for s in rep.steps:
        if s["step"] == name:
            merged.update(s["data"])
    return merged


def pick_profile(path, chip):
    """Explicit --board wins. Otherwise the one profile whose chip matches; none or several -> None."""
    if path:
        with open(path) as fh:
            return json.load(fh), path, "given"
    if not chip:
        return None, None, "chip unknown"
    matches = []
    for p in sorted(glob.glob(os.path.join(ROOT, "boards", "*.json"))):
        with open(p) as fh:
            prof = json.load(fh)
        if chip.upper().startswith(prof.get("chip", "?").upper()):
            matches.append((prof, p))
    if len(matches) == 1:
        return matches[0][0], matches[0][1], "matched by chip type"
    return None, None, f"{len(matches)} profiles match {chip} - pass --board"


def connect_retry(port, seconds=8):
    """The board was just reset by esptool; give MicroPython a moment to boot."""
    end, last = time.time() + seconds, None
    while time.time() < end:
        try:
            return connect(port)
        except Exception as e:  # port re-appearing, REPL not ready yet
            last = e
            time.sleep(0.5)
    raise last


def main():
    ap = argparse.ArgumentParser(description="ESP32 board bring-up, all checks in order")
    ap.add_argument("--port")
    ap.add_argument("--board", help="board profile JSON (default: match by chip type)")
    ap.add_argument("--interactive", action="store_true", help="also run display (look) and buttons (press)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sections = []          # (name, status, one-line summary)
    details = []           # (status, line, fix) printed under the summary

    def section(name, status, summary):
        sections.append((name, status, summary))

    # 1. UART chain
    rep, log, port = uart_check.run_checks(args.port)
    for s in rep.steps:
        details.append((s["status"], f"UART  {s['step']}: {s['detail']}", s["fix"]))
    chip = step_data(rep, "Chip id").get("chip", "")
    boot = step_data(rep, "Boot log")
    if not rep.ok:
        first = next(s for s in rep.steps if s["status"] == "fail")
        section("UART", "fail", f"{first['step']}: {first['detail']}")
        return finish(args, sections, details, log)
    section("UART", "ok", f"{chip or 'chip ?'} on {port}")

    # 2. Board-side checks need MicroPython
    if "micropython" not in boot:
        why = f"board runs {boot.get('app', 'unknown firmware')}, not MicroPython"
        for name in ("I2C", "Sensors", "Radio", "SPI display", "Buttons"):
            section(name, "skip", why)
        details.append(("info", "Board-side checks need MicroPython on the board. Flashing it ERASES the "
                                "current program - back up first (see SKILL.md).", None))
        return finish(args, sections, details, log)

    profile, ppath, how = pick_profile(args.board, chip)
    if profile:
        details.append(("info", f"Board profile: {profile['name']} ({os.path.relpath(ppath, ROOT)}, {how})", None))
    else:
        details.append(("info", f"No board profile ({how}). I2C/display/buttons need pins; radio still runs.", None))

    try:
        repl = connect_retry(port)
    except Exception as e:
        section("MicroPython", "fail", f"no REPL on {port}: {e}")
        return finish(args, sections, details, log)

    try:
        # I2C
        if profile and profile.get("i2c"):
            buses = [i2c_scan.check_bus(repl, b) for b in profile["i2c"]]
            worst = "fail" if any(b["status"] == "fail" for b in buses) else \
                    "warn" if any(b["status"] == "warn" for b in buses) else "ok"
            n = sum(len(b.get("found", [])) for b in buses)
            section("I2C", worst, f"{n} device(s) on {len(buses)} bus(es)")
            for b in buses:
                details.append((b["status"], f"I2C   {b.get('bus', '')}: {b['detail']}", b.get("fix")))
                for f in b.get("found", []):
                    idc = f" (ID {f['id_check']})" if f.get("id_check") else ""
                    details.append(("info", f"        found   {f['addr']}  {f['likely']}{idc}", None))
                for m in b.get("missing", []):
                    details.append(("info", f"        missing {m['addr']}  {m['device']}", None))
        else:
            section("I2C", "skip", "no I2C pins in a board profile")

        # Values from the I2C chips (only when the scan found the bus healthy)
        i2c_ok = sections[-1][0] == "I2C" and sections[-1][1] != "fail"
        if profile and profile.get("sensors") and i2c_ok:
            res = board_io.run_sensors(repl, profile["i2c"][0], profile["sensors"])
            worst = "fail" if any(s == "fail" for s, _, _ in res) else "warn" if any(s == "warn" for s, _, _ in res) else "ok"
            section("Sensors", worst, f"{sum(s == 'ok' for s, _, _ in res)}/{len(res)} read OK")
            details += [(s, f"Chip  {d}", f) for s, d, f in res]
        elif profile and profile.get("sensors"):
            section("Sensors", "skip", "I2C bus is not up")

        # Radio
        res = board_io.run_radio(repl, 5)
        worst = "fail" if any(s == "fail" for s, _, _ in res) else "warn" if any(s == "warn" for s, _, _ in res) else "ok"
        section("Radio", worst, "; ".join(d.split(",")[0] for _, d, _ in res))
        details += [(s, f"Radio {d}", f) for s, d, f in res]

        # Display and buttons need a person
        if not args.interactive:
            section("SPI display", "skip", "needs a person to look - run with --interactive")
            section("Buttons", "skip", "needs a person to press - run with --interactive")
        else:
            if profile and profile.get("spi_display"):
                st, d, fix = board_io.run_display(repl, profile["spi_display"])
                section("SPI display", "look" if st == "ok" else st,
                        "sent - check the screen shows RED / GREEN / BLUE top to bottom" if st == "ok" else d)
                details.append((st, f"SPI   {d}", fix))
            else:
                section("SPI display", "skip", "no display in the board profile")
            if profile and profile.get("buttons"):
                secs = 20
                print(f">>> Press each button once now ({', '.join(profile['buttons'])}) - "
                      f"watching for {secs} s. Short presses only.", flush=True)
                res = board_io.run_buttons(repl, profile["buttons"], secs)
                worst = "fail" if any(s == "fail" for s, _, _ in res) else "ok"
                section("Buttons", worst, f"{sum(s == 'ok' for s, _, _ in res)}/{len(res)} seen")
                details += [(s, f"GPIO  {d}", f) for s, d, f in res]
            else:
                section("Buttons", "skip", "no buttons in the board profile")
    finally:
        repl.close()
    return finish(args, sections, details, log)


def finish(args, sections, details, log):
    failed = any(s == "fail" for _, s, _ in sections)
    if args.json:
        print(json.dumps({"ok": not failed,
                          "sections": [{"name": n, "status": s, "summary": m} for n, s, m in sections],
                          "details": [{"status": s, "line": l, "fix": f} for s, l, f in details],
                          "boot_log": log}, indent=2))
    else:
        for status, line, fix in details:
            print(f"{ICON[status]} {line}")
            if fix and status in ("fail", "warn"):
                for f in fix.splitlines():
                    print(f"         -> {f}")
        print("\n==== Bring-up summary ====")
        for name, status, summary in sections:
            print(f"{ICON[status]} {name:<12} {summary}")
        print("\n" + ("Nothing failed." if not failed else "Fix the first [FAIL] above and run again."))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
