"""Offline tests for the MicroPython raw-REPL client (mpy_repl). Run: python3 -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import mpy_repl as m  # noqa: E402


class FakeSerial:
    """Hands back a fixed byte stream in chunks, like a USB serial port does."""

    def __init__(self, data, chunk=256):
        self.data, self.chunk = data, chunk

    def read(self, n):
        out, self.data = self.data[:min(n, self.chunk)], self.data[min(n, self.chunk):]
        return out


def repl_with(data, chunk=256):
    r = m.RawRepl.__new__(m.RawRepl)
    r.s, r.pending = FakeSerial(data, chunk), b""
    return r


class ReadUntil(unittest.TestCase):
    def test_bytes_after_marker_are_kept(self):
        # The whole reply arrives in one chunk. An earlier version dropped everything after "OK".
        r = repl_with(b'OK@@{"found": [52]}@@\r\n\x04\x04>')
        self.assertEqual(r.read_until(b"OK", 1), b"OK")
        self.assertEqual(r.read_until(b"\x04", 1), b'@@{"found": [52]}@@\r\n\x04')
        self.assertEqual(r.read_until(b"\x04", 1), b"\x04")
        self.assertEqual(r.read_until(b">", 1), b">")

    def test_marker_split_across_chunks(self):
        r = repl_with(b"raw REPL; CTRL-B to exit\r\n>", chunk=3)
        self.assertTrue(r.read_until(b"raw REPL; CTRL-B to exit\r\n>", 1).endswith(b">"))

    def test_timeout(self):
        with self.assertRaises(TimeoutError):
            repl_with(b"nothing useful").read_until(b"\x04", 0.05)


if __name__ == "__main__":
    unittest.main()
