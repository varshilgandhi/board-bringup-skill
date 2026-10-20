"""Offline tests for board_io result handling. Run: python3 -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import board_io as b  # noqa: E402


class FakeRepl:
    def __init__(self, out, err=""):
        self.reply = (out, err)

    def exec(self, code, timeout=10):
        return self.reply


def display_reply(spi):
    return '@@{"bytes": 136320, "ms": 61, "spi": "%s"}@@' % spi


class Display(unittest.TestCase):
    def test_miso_on_mosi_pin_is_caught(self):
        # Real reply from the C6 when miso was left out: MicroPython put MISO on GPIO2 = MOSI, screen stayed blue.
        spi = "SPI(id=1, baudrate=20000000, polarity=0, phase=0, bits=8, firstbit=0, sck=1, mosi=2, miso=2)"
        status, detail, _ = b.run_display(FakeRepl(display_reply(spi)), {})
        self.assertEqual(status, "fail")
        self.assertIn("clash", detail)

    def test_no_miso_is_fine(self):
        spi = "SPI(id=1, baudrate=20000000, polarity=0, phase=0, bits=8, firstbit=0, sck=1, mosi=2, miso=-1)"
        status, detail, _ = b.run_display(FakeRepl(display_reply(spi)), {})
        self.assertEqual(status, "ok")
        self.assertIn("17.9 Mbit/s", detail)

    def test_board_error(self):
        status, detail, _ = b.run_display(FakeRepl("", "Traceback\nValueError: invalid pin"), {})
        self.assertEqual(status, "fail")
        self.assertIn("invalid pin", detail)


class Buttons(unittest.TestCase):
    def test_active_high_press_counted(self):
        out = '@@{"idle": {"BOOT": 1, "PWR": 0}, "presses": {"BOOT": 1, "PWR": 1}}@@'
        res = b.run_buttons(FakeRepl(out), {"BOOT": 9, "PWR": 18}, 60)
        self.assertEqual([r[0] for r in res], ["ok", "ok"])

    def test_no_press(self):
        out = '@@{"idle": {"BOOT": 1}, "presses": {"BOOT": 0}}@@'
        self.assertEqual(b.run_buttons(FakeRepl(out), {"BOOT": 9}, 60)[0][0], "fail")


class Radio(unittest.TestCase):
    def test_counts_only(self):
        out = '@@{"wifi": [[1, -38], [6, -70], [6, -80]], "wifi_ms": 2495, "ble": [-55, -90]}@@'
        res = b.run_radio(FakeRepl(out), 5)
        self.assertEqual([r[0] for r in res], ["ok", "ok"])
        self.assertIn("3 network(s)", res[0][1])
        self.assertIn("-38 dBm", res[0][1])
        self.assertIn("channels 1, 6", res[0][1])

    def test_all_weak_is_flagged(self):
        out = '@@{"wifi": [[1, -90]], "wifi_ms": 2495, "ble": []}@@'
        statuses = [r[0] for r in b.run_radio(FakeRepl(out), 5)]
        self.assertEqual(statuses, ["ok", "warn", "warn"])

    def test_no_bluetooth_in_firmware(self):
        out = '@@{"wifi": [[1, -40]], "wifi_ms": 2495, "ble_error": "ImportError()"}@@'
        self.assertEqual(b.run_radio(FakeRepl(out), 5)[1][0], "warn")


class Sensors(unittest.TestCase):
    # Raw bytes are hand-built from the datasheet layouts, not captured from a board.
    def test_axp2101_usb_and_battery(self):
        raw = {"pmic": [[0x28], [0x02], [0x10, 0x04], [0x13, 0x88], [80]]}   # 4100 mV bat, 5000 mV VBUS
        status, line, _ = b.decode_axp2101(raw)
        self.assertEqual(status, "ok")
        self.assertIn("5.00 V", line)
        self.assertIn("battery 4.10 V, 80%, charging (constant current)", line)

    def test_axp2101_no_battery_low_vbus(self):
        raw = {"pmic": [[0x20], [0x05], [0x00, 0x00], [0x10, 0x68], [0]]}    # 4200 mV VBUS
        status, line, fix = b.decode_axp2101(raw)
        self.assertEqual(status, "warn")
        self.assertIn("no battery", line)
        self.assertIn("4.20 V", fix)

    def test_qmi8658_flat(self):
        raw = {"imu": [[0, 0, 0, 0, 0x00, 0x40]]}                            # z = +16384 = +1 g
        status, line, _ = b.decode_qmi8658(raw)
        self.assertEqual(status, "ok")
        self.assertIn("+Z points up", line)

    def test_qmi8658_all_zero(self):
        self.assertEqual(b.decode_qmi8658({"imu": [[0] * 6]})[0], "fail")

    def test_rtc_ticking_and_wraps(self):
        self.assertEqual(b.decode_pcf85063({"rtc_a": [[0x59]], "rtc_b": [[0x00]]})[0], "ok")

    def test_rtc_stopped(self):
        self.assertEqual(b.decode_pcf85063({"rtc_a": [[0x12]], "rtc_b": [[0x12]]})[0], "fail")

    def test_rtc_stop_flag(self):
        self.assertEqual(b.decode_pcf85063({"rtc_a": [[0x92]], "rtc_b": [[0x93]]})[0], "warn")


class Profile(unittest.TestCase):
    def test_c6_matches_waveshare_profile(self):
        import bringup
        prof, _, how = bringup.pick_profile(None, "ESP32-C6 (QFN40) (revision v0.2)")
        self.assertEqual(prof["chip"], "ESP32-C6")
        self.assertEqual(how, "matched by chip type")

    def test_unknown_chip_has_no_profile(self):
        import bringup
        prof, _, _ = bringup.pick_profile(None, "ESP32-S3 (QFN56) (revision v0.2)")
        self.assertIsNone(prof)


if __name__ == "__main__":
    unittest.main()
