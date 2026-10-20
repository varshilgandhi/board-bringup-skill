"""Offline tests for boot-log parsing. Run: python3 -m unittest discover tests

esp32c6_xiaozhi_ok.log is a real capture from the dev board. The classic ESP32 logs below are
hand-written in the standard ROM format; replace them with real captures when a classic
ESP32 board is on the bench.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import uart_check as u  # noqa: E402

LOGS = os.path.join(os.path.dirname(__file__), "logs")

CLASSIC_OK = """ets Jun  8 2016 00:22:57

rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
configsip: 0, SPIWP:0xee
mode:DIO, clock div:2
entry 0x400805e4
I (29) boot: ESP-IDF v5.1 2nd stage bootloader
I (220) app_init: Project name:     blink
"""

CLASSIC_DOWNLOAD = """ets Jun  8 2016 00:22:57

rst:0x1 (POWERON_RESET),boot:0x3 (DOWNLOAD_BOOT(UART0/UART1/SDIO_REI_REO_V2))
waiting for download
"""

CLASSIC_BROWNOUT_LOOP = """ets Jun  8 2016 00:22:57

rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
Brownout detector was triggered

ets Jun  8 2016 00:22:57

rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
Brownout detector was triggered
"""

CLASSIC_EMPTY_FLASH = """ets Jun  8 2016 00:22:57

rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
invalid header: 0xffffffff
"""


class Interpret(unittest.TestCase):
    def test_real_c6_capture(self):
        with open(os.path.join(LOGS, "esp32c6_xiaozhi_ok.log"), encoding="utf-8", errors="replace") as fh:
            log = fh.read()
        f = u.interpret(log)
        self.assertEqual(f["rom"], "esp32c6-20220919")
        self.assertEqual(f["app"], "xiaozhi")
        self.assertEqual(f["idf"], "v6.1")
        self.assertFalse(f["download_mode"])
        self.assertEqual(f["boot_count"], 1)
        self.assertEqual(f["problems"], [])

    def test_classic_ok(self):
        f = u.interpret(CLASSIC_OK)
        self.assertEqual(f["reset_reason"], "0x1 (POWERON_RESET)")
        self.assertFalse(f["download_mode"])
        self.assertEqual(f["problems"], [])

    def test_download_mode(self):
        self.assertTrue(u.interpret(CLASSIC_DOWNLOAD)["download_mode"])

    def test_brownout_loop(self):
        p = " ".join(u.interpret(CLASSIC_BROWNOUT_LOOP)["problems"])
        self.assertIn("brownout", p)
        self.assertIn("rebooted 2 times", p)

    def test_empty_flash(self):
        self.assertIn("no valid program", " ".join(u.interpret(CLASSIC_EMPTY_FLASH)["problems"]))


class TextScore(unittest.TestCase):
    def test_clean_log_scores_high(self):
        self.assertGreater(u.text_score(CLASSIC_OK.encode()), 0.95)

    def test_wrong_baud_junk_scores_low(self):
        junk = bytes([0x00, 0xF8, 0x80, 0xFE, 0x9C, 0x1C, 0xE0, 0xFC] * 40)
        self.assertLess(u.text_score(junk), 0.5)

    def test_empty(self):
        self.assertEqual(u.text_score(b""), 0.0)


if __name__ == "__main__":
    unittest.main()
