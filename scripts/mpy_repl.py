"""Minimal MicroPython raw-REPL client, shared by the board-side checks (I2C, SPI, GPIO)."""
import time

import serial


class RawRepl:
    """Minimal MicroPython raw-REPL client (same protocol as mpremote / pyboard.py)."""

    def __init__(self, port):
        # Open with DTR and RTS low: on USB-Serial/JTAG and auto-reset boards that does not reset the chip.
        self.s = serial.Serial()
        self.s.port, self.s.baudrate, self.s.timeout = port, 115200, 0.1
        self.s.dtr = False
        self.s.rts = False
        self.s.open()
        self.pending = b""

    def read_until(self, marker, timeout):
        """Return bytes up to and including marker. Bytes after it stay in self.pending."""
        end = time.time() + timeout
        while marker not in self.pending and time.time() < end:
            self.pending += self.s.read(256)
        if marker not in self.pending:
            raise TimeoutError(f"no {marker!r} from the board (got {self.pending[-80:]!r})")
        cut = self.pending.index(marker) + len(marker)
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return out

    def enter(self):
        self.s.write(b"\r\x03\x03")          # stop whatever is running
        time.sleep(0.2)
        self.s.reset_input_buffer()
        self.pending = b""
        self.s.write(b"\r\x01")              # raw REPL
        self.read_until(b"raw REPL; CTRL-B to exit\r\n>", 3)

    def exec(self, code, timeout=10):
        self.s.write(code.encode())
        self.s.write(b"\x04")
        self.read_until(b"OK", 3)
        out = self.read_until(b"\x04", timeout)[:-1]
        err = self.read_until(b"\x04", timeout)[:-1]
        self.read_until(b">", 2)
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def close(self):
        try:
            self.s.write(b"\x02")            # back to the normal REPL
        finally:
            self.s.close()


def connect(port):
    """Open the port and enter the raw REPL. Raises serial.SerialException or TimeoutError."""
    repl = RawRepl(port)
    repl.enter()
    return repl
