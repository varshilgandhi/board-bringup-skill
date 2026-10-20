#!/usr/bin/env python3
"""ESP32 UART bring-up check.

Walks the host-side chain in order and stops at the first broken link:
USB enumeration -> USB-serial chip -> driver -> port -> permission -> busy
-> boot log (reset + capture + baud) -> chip id (esptool).

Prints a plain-words report. --json prints the same data as JSON.
Exit code 0 when the UART link is up, 1 when something is broken.

Needs: pyserial. esptool is optional (chip id step is skipped without it).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import grp  # not on Windows
except ImportError:
    grp = None

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("pyserial is missing. Install it with:  pip install pyserial")
    sys.exit(2)

IS_LINUX = sys.platform.startswith("linux")

# (vid, pid) -> (chip, linux driver, kind). kind "native" = USB serial built into the ESP chip,
# "bridge" = separate USB-to-UART chip on the board.
KNOWN_USB = {
    (0x303A, 0x1001): ("Espressif USB-Serial/JTAG (built into ESP32-C3/C5/C6/H2/S3/P4)", "cdc_acm", "native"),
    (0x303A, 0x0002): ("Espressif USB-OTG CDC (ESP32-S2/S3 ROM)", "cdc_acm", "native"),
    (0x10C4, 0xEA60): ("Silicon Labs CP210x", "cp210x", "bridge"),
    (0x10C4, 0xEA70): ("Silicon Labs CP2105", "cp210x", "bridge"),
    (0x1A86, 0x7523): ("WCH CH340", "ch341", "bridge"),
    (0x1A86, 0x7522): ("WCH CH340K", "ch341", "bridge"),
    (0x1A86, 0x5523): ("WCH CH341", "ch341", "bridge"),
    (0x1A86, 0x55D4): ("WCH CH9102", "cdc_acm", "bridge"),
    (0x1A86, 0x55D3): ("WCH CH343", "cdc_acm", "bridge"),
    (0x0403, 0x6001): ("FTDI FT232R", "ftdi_sio", "bridge"),
    (0x0403, 0x6010): ("FTDI FT2232", "ftdi_sio", "bridge"),
    (0x0403, 0x6014): ("FTDI FT232H", "ftdi_sio", "bridge"),
    (0x0403, 0x6015): ("FTDI FT231X", "ftdi_sio", "bridge"),
}
KNOWN_VIDS = {vid for vid, _ in KNOWN_USB}

# Order matters: the ESP32 ROM default first, then the ESP8266-style ROM rate, then common app rates.
BAUDS = [115200, 74880, 460800, 921600, 230400, 57600, 9600]

BOOT_MARKERS = [b"ESP-ROM:", b"ets ", b"rst:0x", b"boot:0x", b"I (", b"W (", b"E (", b"entry 0x"]


# ---------------------------------------------------------------- report helpers

class Report:
    def __init__(self):
        self.steps = []

    def add(self, name, status, detail, fix=None, data=None):
        self.steps.append({"step": name, "status": status, "detail": detail, "fix": fix, "data": data or {}})
        return status == "ok" or status == "warn" or status == "info"

    @property
    def ok(self):
        return all(s["status"] != "fail" for s in self.steps)


ICON = {"ok": "[ OK ]", "fail": "[FAIL]", "warn": "[WARN]", "info": "[INFO]", "skip": "[SKIP]"}


def print_report(rep, bootlog):
    for s in rep.steps:
        print(f"{ICON[s['status']]} {s['step']}: {s['detail']}")
        if s["fix"]:
            for line in s["fix"].splitlines():
                print(f"       -> {line}")
    if bootlog:
        print("\n--- boot log (first 40 lines) ---")
        print("\n".join(bootlog.splitlines()[:40]))
        print("---")
    print("\nUART: " + ("UP" if rep.ok else "NOT UP - fix the first [FAIL] above and run again"))


# ---------------------------------------------------------------- linux helpers

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def usb_devices_sysfs():
    """Every USB device the kernel sees, with or without a serial port. Linux only."""
    devs = []
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = int(open(os.path.join(d, "idVendor")).read(), 16)
            pid = int(open(os.path.join(d, "idProduct")).read(), 16)
        except (OSError, ValueError):
            continue
        product = ""
        try:
            product = open(os.path.join(d, "product")).read().strip()
        except OSError:
            pass
        devs.append({"path": d, "vid": vid, "pid": pid, "product": product})
    return devs


def kernel_usb_errors():
    """Recent kernel lines that point to a bad cable or weak power."""
    for cmd in (["journalctl", "-k", "--since", "-15min", "--no-pager", "-q"], ["dmesg"]):
        code, out = run(cmd)
        if code == 0 and out:
            pat = re.compile(r"device descriptor read|unable to enumerate|not accepting address|"
                             r"over-current|cannot reset|disabled by hub \(EMI\?\)", re.I)
            return [l for l in out.splitlines() if pat.search(l)][-5:]
    return []


def tty_driver(dev):
    name = os.path.basename(dev)
    p = f"/sys/class/tty/{name}/device/driver"
    return os.path.basename(os.path.realpath(p)) if os.path.exists(p) else None


def by_id_path(dev):
    for link in glob.glob("/dev/serial/by-id/*"):
        if os.path.realpath(link) == os.path.realpath(dev):
            return link
    return None


def port_holders(dev):
    """PIDs + names of processes holding the port open. Sees own processes without root."""
    target = os.path.realpath(dev)
    holders = []
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) == target:
                pid = fd.split("/")[2]
                name = open(f"/proc/{pid}/comm").read().strip()
                if int(pid) != os.getpid() and (pid, name) not in holders:
                    holders.append((pid, name))
        except OSError:
            continue
    if not holders and shutil.which("fuser"):
        code, out = run(["fuser", dev])
        for pid in out.split(":")[-1].split():
            pid = pid.strip("mce")
            if pid.isdigit() and int(pid) != os.getpid():
                holders.append((pid, "?"))
    return holders


def service_active(name):
    if not shutil.which("systemctl"):
        return False
    return run(["systemctl", "is-active", name])[1].strip() == "active"


# ---------------------------------------------------------------- steps

def step_usb(rep, want_port):
    if want_port and not os.path.exists(want_port) and not want_port.upper().startswith("COM"):
        have = [p.device for p in serial.tools.list_ports.comports() if p.vid]
        rep.add("Port", "fail", f"{want_port} does not exist",
                fix=("ports with a USB device behind them: " + ", ".join(have)) if have else
                    "no USB serial ports at all - run without --port to see why")
        return None, None, None
    ports = [p for p in serial.tools.list_ports.comports() if p.vid]
    if want_port:
        ports = [p for p in ports if os.path.realpath(p.device) == os.path.realpath(want_port)] or \
                [p for p in serial.tools.list_ports.comports() if p.device == want_port]
    esp = [p for p in ports if p.vid in KNOWN_VIDS]
    if esp:
        p = esp[0]
        chip, driver, kind = KNOWN_USB.get((p.vid, p.pid), (f"unknown chip from a known maker", None, "bridge"))
        rep.add("USB", "ok", f"board found: {p.vid:04x}:{p.pid:04x} {p.description}",
                data={"vid": p.vid, "pid": p.pid, "serial": p.serial_number})
        rep.add("USB-serial chip", "ok", chip + (" - no separate driver chip, baud does not matter"
                                                 if kind == "native" else ""),
                data={"chip": chip, "kind": kind})
        if len(esp) > 1:
            rep.add("Ports", "warn", "more than one ESP-type port: " + ", ".join(x.device for x in esp),
                    fix=f"checking {p.device}. Pass --port to pick another.")
        return p, driver, kind
    if ports and want_port:
        p = ports[0]
        rep.add("USB", "warn", f"{p.device} is {p.vid:04x}:{p.pid:04x} {p.description}, not a known ESP32 USB chip")
        return p, None, "bridge"

    # Nothing with a serial port. Is the board on USB at all, just without a driver?
    if IS_LINUX:
        for d in usb_devices_sysfs():
            if d["vid"] in KNOWN_VIDS:
                chip, driver, _ = KNOWN_USB.get((d["vid"], d["pid"]), ("unknown", None, ""))
                brl = " brltty is running and often grabs CH340 chips." if (
                    d["vid"] == 0x1A86 and (service_active("brltty") or run(["pgrep", "brltty"])[0] == 0)) else ""
                rep.add("USB", "ok", f"board found: {d['vid']:04x}:{d['pid']:04x} {d['product']} ({chip})")
                rep.add("Driver", "fail", f"the board is on USB but has no serial port.{brl}",
                        fix=(f"sudo modprobe {driver}  (then unplug and replug)" if driver else "") +
                            ("\nsudo systemctl mask brltty-udev.service && sudo apt remove brltty" if brl else ""))
                return None, None, None
        errs = kernel_usb_errors()
        if errs:
            rep.add("USB", "fail", "the laptop saw something on USB but it failed to start: " + errs[-1].strip(),
                    fix="try another USB cable (short, known data cable), another USB port, and no hub.\n"
                        "if the board has its own power input, check that too.")
        else:
            rep.add("USB", "fail", "no board seen on USB at all",
                    fix="1. is the board's power LED on? if not: cable or USB port is dead.\n"
                        "2. many cables only charge - try a cable you have flashed with before.\n"
                        "3. some boards have two USB-C ports (USB and UART) - try the other one.")
    else:
        rep.add("USB", "fail", "no ESP32 serial port found",
                fix="check Device Manager > Ports (COM & LPT). A yellow '!' means the driver is missing:\n"
                    "CP210x -> Silicon Labs VCP driver, CH340/CH9102 -> WCH CH341SER driver.\n"
                    "Nothing at all -> try another cable (many only charge) and another USB port.")
    return None, None, None


def step_driver(rep, port, expected):
    if not IS_LINUX:
        return True
    drv = tty_driver(port.device)
    if not drv:
        return rep.add("Driver", "fail", "no driver bound to the port", fix=f"sudo modprobe {expected}")
    if expected and drv != expected:
        return rep.add("Driver", "warn", f"{drv} (expected {expected})")
    return rep.add("Driver", "ok", f"{drv} loaded")


def step_port(rep, port):
    dev = port.device
    link = by_id_path(dev) if IS_LINUX else None
    rep.add("Port", "ok", dev + (f" (stable name: {link})" if link else ""), data={"device": dev, "by_id": link})
    if not IS_LINUX:
        return True
    if os.access(dev, os.R_OK | os.W_OK):
        return rep.add("Permission", "ok", "this user can open the port")
    st = os.stat(dev)
    group = grp.getgrgid(st.st_gid).gr_name
    user = os.environ.get("USER") or os.getlogin()
    in_db = user in grp.getgrgid(st.st_gid).gr_mem
    if in_db:
        return rep.add("Permission", "fail",
                       f"you were added to '{group}' but this login session does not have it yet",
                       fix=f"log out and back in (or reboot). For this terminal only: newgrp {group}")
    return rep.add("Permission", "fail", f"port belongs to group '{group}' and you are not in it",
                   fix=f"sudo usermod -aG {group} $USER   then log out and back in")


def step_busy(rep, port):
    holders = port_holders(port.device) if IS_LINUX else []
    if holders:
        names = ", ".join(f"{n} (pid {p})" for p, n in holders)
        mm = any(n == "ModemManager" for _, n in holders)
        return rep.add("Busy", "fail", f"port is open in another program: {names}",
                       fix="sudo systemctl stop ModemManager  (it probes new serial ports)" if mm else
                           "close that serial monitor (Arduino IDE, screen, minicom, idf.py monitor) and retry")
    try:
        s = serial.Serial(port.device, 115200, timeout=0.1, exclusive=True) if IS_LINUX else \
            serial.Serial(port.device, 115200, timeout=0.1)
        s.close()
    except serial.SerialException as e:
        return rep.add("Busy", "fail", f"could not open the port: {e}",
                       fix="close any serial monitor that has it open and retry")
    return rep.add("Busy", "ok", "port is free")


def reset_board(s):
    # EN is driven by RTS (through the auto-reset transistors on bridge boards, or directly
    # by the USB-Serial/JTAG peripheral). DTR low keeps IO0/IO9 high, so the chip boots from flash.
    s.dtr = False
    s.rts = True
    time.sleep(0.1)
    s.rts = False


def capture(dev, baud, seconds, do_reset=True):
    """Open, reset, read. Keeps the port open through the reset so the ROM lines are not lost.
    If the USB device drops (native USB boards can re-enumerate), reopen and keep reading."""
    buf = b""
    s = serial.Serial(dev, baud, timeout=0.05)
    if do_reset:
        reset_board(s)
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            buf += s.read(4096)
        except (serial.SerialException, OSError):
            try:
                s.close()
            except Exception:
                pass
            s = None
            while s is None and time.time() - t0 < seconds:
                try:
                    s = serial.Serial(dev, baud, timeout=0.05)
                except (serial.SerialException, OSError):
                    time.sleep(0.02)
            if s is None:
                break
    if s:
        s.close()
    return buf


def text_score(buf):
    """0..1, how much the bytes look like a real boot log rather than wrong-baud junk."""
    if not buf:
        return 0.0
    printable = sum(1 for b in buf if 32 <= b < 127 or b in (9, 10, 13))
    score = printable / len(buf)
    if any(m in buf for m in BOOT_MARKERS):
        score = min(1.0, score + 0.2)
    return score


def read_boot_log(dev, kind, seconds):
    """Returns (baud or None, raw bytes, tried list)."""
    if kind == "native":
        buf = capture(dev, 115200, seconds)
        return 115200, buf, [(115200, len(buf), round(text_score(buf), 2))]
    tried = []
    best = (None, b"", 0.0)
    for baud in BAUDS:
        buf = capture(dev, baud, seconds)
        sc = text_score(buf)
        tried.append((baud, len(buf), round(sc, 2)))
        if sc > best[2]:
            best = (baud, buf, sc)
        if sc >= 0.95 and len(buf) > 40:
            break
    return best[0], best[1], tried


def interpret(log):
    """Pull the facts an engineer looks for out of an ESP boot log."""
    f = {}
    m = re.search(r"ESP-ROM:(\S+)", log)
    if m:
        f["rom"] = m.group(1)
    m = re.search(r"rst:(0x[0-9a-f]+) \(([^)]*)\)", log)
    if m:
        f["reset_reason"] = f"{m.group(1)} ({m.group(2)})"
    m = re.search(r"boot:(0x[0-9a-f]+) \(([^)]*)\)", log)
    if m:
        f["boot_mode"] = f"{m.group(1)} ({m.group(2)})"
        f["download_mode"] = "DOWNLOAD" in m.group(2).upper()
    if "waiting for download" in log:
        f["download_mode"] = True
    for key, pat in [("app", r"Project name:\s+(\S+)"), ("app_version", r"App version:\s+(\S+)"),
                     ("idf", r"ESP-IDF:\s+(\S+)"), ("chip_rev", r"Chip rev:\s+(\S+)"),
                     ("sku", r"SKU=(\S+)"), ("micropython", r"(MicroPython v\S+ on [^;]+)"), ("console_pins", r"(GPIO \d+ and \d+ are used as console UART I/O pins)")]:
        m = re.search(pat, log)
        if m:
            f[key] = m.group(1)
    problems = []
    if re.search(r"invalid header|No bootable app|flash read err|Checksum failure", log, re.I):
        problems.append("flash has no valid program (empty or corrupt) - flash firmware")
    if re.search(r"[Bb]rownout", log):
        problems.append("brownout: supply voltage dropped - weak USB port/cable or the board draws too much")
    if re.search(r"Guru Meditation|abort\(\) was called|Backtrace:|panic", log):
        problems.append("the program crashed (panic) - firmware bug, not a wiring problem")
    boots = len(re.findall(r"ESP-ROM:|ets [A-Z][a-z]{2} ", log))
    if boots > 1:
        problems.append(f"the board rebooted {boots} times during capture (boot loop)")
    f["boot_count"] = boots
    f["problems"] = problems
    return f


def step_boot_log(rep, port, kind, seconds):
    try:
        baud, raw, tried = read_boot_log(port.device, kind, seconds)
    except serial.SerialException as e:
        rep.add("Boot log", "fail", f"could not read the port: {e}")
        return ""
    log = raw.decode("utf-8", "replace")
    if not raw:
        rep.add("Boot log", "fail", "the board printed nothing after a reset",
                fix="1. press the RST/EN button once while this runs (auto-reset may be missing).\n"
                    "2. the firmware may log on other pins - plug a USB-UART adapter on the console TX/RX pins.\n"
                    "3. power: is the power LED on?" if kind != "native" else
                    "the USB link works, so the chip is alive. The firmware may send its log to the\n"
                    "UART pins instead of USB, or the console is turned off. esptool (next step) proves the chip.",
                data={"tried": tried})
        return ""
    score = text_score(raw)
    if score < 0.8:
        rep.add("Boot log", "fail", f"got {len(raw)} bytes but they are junk at every speed tried",
                fix="usually a wrong baud the firmware picked, or noise from a loose GND/TX wire.\n"
                    "check GND is shared and TX/RX are crossed (board TX -> adapter RX).",
                data={"tried": tried})
        return log
    facts = interpret(log)
    baud_txt = "baud does not apply (native USB)" if kind == "native" else f"baud {baud}"
    rep.add("Boot log", "ok", f"{len(raw)} bytes of clean text, {baud_txt}", data={"tried": tried, **facts})
    if facts.get("download_mode"):
        rep.add("Boot mode", "warn", "the chip started in DOWNLOAD (flashing) mode, not your program",
                fix="BOOT/IO0 (IO9 on C3/C6) is held low. Release the BOOT button, check nothing pulls it low.")
    for p in facts["problems"]:
        rep.add("Boot log", "warn", p)
    summary = ", ".join(f"{k}={facts[k]}" for k in ("rom", "reset_reason", "app", "app_version", "idf", "sku", "micropython")
                        if k in facts)
    if summary:
        rep.add("Firmware", "info", summary)
    if kind == "native" and "rom" not in facts and not re.search(r"ets [A-Z]", log):
        rep.add("Boot log", "info", "ROM lines missing at the top (normal if the USB port re-enumerated)")
    return log


def find_esptool():
    for name in ("esptool", "esptool.py"):
        if shutil.which(name):
            return [shutil.which(name)]
    if run([sys.executable, "-c", "import esptool"])[0] == 0:
        return [sys.executable, "-m", "esptool"]
    return None


def step_chip_id(rep, port):
    tool = find_esptool()
    if not tool:
        rep.add("Chip id", "skip", "esptool not installed",
                fix="pip install esptool   (or: uv tool install esptool)")
        return
    code, out = run(tool + ["--port", port.device, "chip-id"], timeout=60)
    facts = {}
    for key, pat in [("chip", r"Chip type:\s+(.+)"), ("features", r"Features:\s+(.+)"),
                     ("crystal", r"Crystal frequency:\s+(.+)"), ("usb_mode", r"USB mode:\s+(.+)"),
                     ("mac", r"BASE MAC:\s+(\S+)"), ("mac", r"^MAC:\s+(\S+)")]:
        m = re.search(pat, out, re.M)
        if m and key not in facts:
            facts[key] = m.group(1).strip()
    if code == 0 and "chip" in facts:
        rep.add("Chip id", "ok", f"{facts['chip']}, MAC {facts.get('mac', '?')}", data=facts)
        return
    if "Failed to connect" in out or "No serial data received" in out:
        rep.add("Chip id", "fail", "esptool could not talk to the bootloader",
                fix="hold BOOT, tap RST/EN, release BOOT, then run again (auto-reset circuit missing or weak).\n"
                    "if it still fails: TX/RX swapped or the chip is not powered.")
    else:
        last = [l for l in out.strip().splitlines() if l.strip()][-1:] or ["no output"]
        rep.add("Chip id", "fail", f"esptool failed: {last[0]}")


# ---------------------------------------------------------------- main

def run_checks(want_port=None, seconds=4.0, chip_id=True):
    """Run the UART chain. Returns (Report, boot log text, port device or None)."""
    rep = Report()
    log = ""
    port, driver, kind = step_usb(rep, want_port)
    if port and step_driver(rep, port, driver) and step_port(rep, port) and step_busy(rep, port):
        log = step_boot_log(rep, port, kind, seconds)
        if chip_id:
            step_chip_id(rep, port)
            # esptool leaves the chip running the app after its hard reset; nothing else to restore.
    return rep, log, (port.device if port else None)


def main():
    ap = argparse.ArgumentParser(description="ESP32 UART bring-up check")
    ap.add_argument("--port", help="serial port to check (default: find it)")
    ap.add_argument("--seconds", type=float, default=4.0, help="how long to read the boot log per try")
    ap.add_argument("--no-chip-id", action="store_true", help="skip the esptool step")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args()

    rep, log, _ = run_checks(args.port, args.seconds, not args.no_chip_id)

    if args.json:
        print(json.dumps({"uart_up": rep.ok, "steps": rep.steps, "boot_log": log}, indent=2))
    else:
        print_report(rep, log)
    sys.exit(0 if rep.ok else 1)


if __name__ == "__main__":
    main()
