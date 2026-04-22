"""LCD1602 I2C display driver via PCF8574 backpack.

Uses the system smbus module (python3-smbus, installed via apt).
All public methods are synchronous/blocking — callers use asyncio.to_thread().

Hardware: Freenove LCD1602 with PCF8574 I2C backpack.
Default I2C address: 0x27 (PCF8574T). Some boards use 0x3F (PCF8574AT).
Confirmed on Robot HAT+ 5 I2C bus 1 at address 0x27.

PCF8574 → HD44780 pin mapping (4-bit mode):
  P0 = RS   P1 = RW   P2 = E    P3 = BL (backlight)
  P4 = D4   P5 = D5   P6 = D6   P7 = D7
"""

from __future__ import annotations

import threading
import time

try:
    import smbus2 as smbus  # preferred: installed in venv via pip install smbus2
except ImportError:
    try:
        import smbus  # type: ignore[no-redef]  # fallback: system python3-smbus (apt)
    except ImportError:
        smbus = None  # type: ignore[assignment]

import logging

logger = logging.getLogger(__name__)

# PCF8574 bit masks
_RS = 0x01   # Register Select: 0=command, 1=data
_RW = 0x02   # Read/Write: always 0 (write)
_EN = 0x04   # Enable strobe
_BL = 0x08   # Backlight

# LCD row offsets (HD44780 DDRAM addresses)
_ROW_OFFSETS = (0x00, 0x40)

# LCD commands
_CMD_CLEAR      = 0x01
_CMD_HOME       = 0x02
_CMD_ENTRY_MODE = 0x06   # increment, no shift
_CMD_DISPLAY_ON = 0x0C   # display on, cursor off, blink off
_CMD_FUNC_SET   = 0x28   # 4-bit, 2 lines, 5x8 font
_CMD_SET_DDRAM  = 0x80


class _LCD1602:
    """Low-level PCF8574 → HD44780 LCD1602 driver (4-bit mode)."""

    def __init__(self, bus: int, address: int) -> None:
        if smbus is None:
            raise RuntimeError(
                "smbus module not found. Install with: sudo apt-get install python3-smbus"
            )
        self._bus = smbus.SMBus(bus)
        self._addr = address
        self._backlight = _BL
        self._init()

    def _write_i2c(self, data: int) -> None:
        self._bus.write_byte(self._addr, data)

    def _pulse_enable(self, data: int) -> None:
        self._write_i2c(data | _EN)
        time.sleep(0.0005)
        self._write_i2c(data & ~_EN)
        time.sleep(0.0001)

    def _send_nibble(self, nibble: int, mode: int) -> None:
        data = (nibble & 0xF0) | mode | self._backlight
        self._write_i2c(data)
        self._pulse_enable(data)

    def _send_byte(self, byte: int, mode: int) -> None:
        self._send_nibble(byte & 0xF0, mode)
        self._send_nibble((byte << 4) & 0xF0, mode)

    def command(self, cmd: int) -> None:
        self._send_byte(cmd, 0)

    def write_char(self, char: str) -> None:
        self._send_byte(ord(char), _RS)

    def set_cursor(self, row: int, col: int) -> None:
        # row: 0-based; col: 0-based
        addr = _CMD_SET_DDRAM | (_ROW_OFFSETS[row] + col)
        self.command(addr)

    def clear(self) -> None:
        self.command(_CMD_CLEAR)
        time.sleep(0.002)

    def set_backlight(self, on: bool) -> None:
        self._backlight = _BL if on else 0
        self._write_i2c(self._backlight)

    def _init(self) -> None:
        logger.debug("LCD: running HD44780 init sequence (4-bit mode)")
        time.sleep(0.05)
        # 4-bit init sequence (from HD44780 datasheet)
        for _ in range(3):
            self._send_nibble(0x30, 0)
            time.sleep(0.005)
        self._send_nibble(0x20, 0)
        time.sleep(0.001)
        # Configure: 4-bit, 2 lines, 5x8 font
        self.command(_CMD_FUNC_SET)
        self.command(_CMD_DISPLAY_ON)
        self.command(_CMD_CLEAR)
        time.sleep(0.002)
        self.command(_CMD_ENTRY_MODE)
        logger.debug("LCD: init sequence complete")


class LCDDisplay:
    """Async-friendly LCD1602 wrapper.

    All methods are blocking — call via asyncio.to_thread() from async code.
    A threading.Lock serialises concurrent show/scroll calls.
    """

    def __init__(self, bus: int = 1, address: int = 0x27) -> None:
        self._lcd = _LCD1602(bus, address)
        self._lock = threading.Lock()
        logger.info("LCD1602 initialised at I2C bus %d address 0x%02X", bus, address)

    def show(self, line: int, text: str) -> None:
        """Write text to line 1 or 2 (1-based). Pads/truncates to 16 chars."""
        row = (line - 1) & 1
        padded = text[:16].ljust(16)
        logger.debug("LCD line %d: %r", line, padded)
        with self._lock:
            self._lcd.set_cursor(row, 0)
            for ch in padded:
                try:
                    self._lcd.write_char(ch)
                except Exception:
                    self._lcd.write_char("?")

    def scroll(
        self,
        line: int,
        text: str,
        delay: float,
        stop: threading.Event,
    ) -> None:
        """Scroll text left across line. Exits when stop is set or text exhausted.

        For text <= 16 chars: displays statically until stop is set.
        For text > 16 chars: slides left one char at a time.
        """
        if len(text) <= 16:
            logger.debug("LCD scroll line %d (static, %d chars): %r", line, len(text), text)
            self.show(line, text)
            stop.wait()
            return

        logger.debug("LCD scroll line %d (%d chars, delay=%.2fs): %r", line, len(text), delay, text)
        padded = " " * 16 + text + " " * 16
        for i in range(len(padded) - 15):
            self.show(line, padded[i : i + 16])
            if stop.wait(delay):
                logger.debug("LCD scroll line %d stopped at step %d", line, i)
                return
        logger.debug("LCD scroll line %d complete", line)

    def clear(self) -> None:
        """Blank both lines."""
        with self._lock:
            self._lcd.clear()

    def close(self) -> None:
        """Clear display and turn off backlight."""
        with self._lock:
            try:
                self._lcd.clear()
                self._lcd.set_backlight(False)
            except Exception:
                pass
        logger.info("LCD1602 closed")
