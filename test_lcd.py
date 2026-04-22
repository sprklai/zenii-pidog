#!/usr/bin/env python3
"""Standalone LCD1602 test script.

Run this directly on the Pi to verify wiring and driver before starting the bridge:

  # Inside the venv (recommended):
  source /home/neil/pidog-zenii/.venv/bin/activate
  python3 test_lcd.py

  # Or with a custom address / bus:
  python3 test_lcd.py --address 0x3F --bus 1

Each test step is logged. If a step fails the script stops and explains what to check.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Logging setup — timestamps + level + message
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lcd_test")


# ---------------------------------------------------------------------------
# Step 0 — smbus availability
# ---------------------------------------------------------------------------
def check_smbus() -> object:
    log.info("━━━━ Step 0: checking smbus library ━━━━")
    try:
        import smbus2 as smbus
        log.info("✓ smbus2 imported successfully (version: %s)", getattr(smbus, "__version__", "unknown"))
        return smbus
    except ImportError:
        log.warning("smbus2 not found — trying system smbus (python3-smbus)")
    try:
        import smbus  # type: ignore[import]
        log.info("✓ system smbus imported successfully")
        return smbus
    except ImportError:
        log.error("✗ Neither smbus2 nor smbus found.")
        log.error("  Fix: source /home/neil/pidog-zenii/.venv/bin/activate && pip install smbus2")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 1 — open I2C bus
# ---------------------------------------------------------------------------
def open_bus(smbus, bus_num: int):
    log.info("━━━━ Step 1: opening I2C bus %d ━━━━", bus_num)
    try:
        bus = smbus.SMBus(bus_num)
        log.info("✓ I2C bus %d opened", bus_num)
        return bus
    except FileNotFoundError:
        log.error("✗ /dev/i2c-%d not found — I2C not enabled", bus_num)
        log.error("  Fix: sudo raspi-config → Interface Options → I2C → Enable → reboot")
        sys.exit(1)
    except PermissionError:
        log.error("✗ Permission denied on /dev/i2c-%d", bus_num)
        log.error("  Fix: sudo usermod -aG i2c $USER  then log out and back in")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — detect LCD on I2C bus
# ---------------------------------------------------------------------------
def detect_lcd(bus, address: int) -> None:
    log.info("━━━━ Step 2: scanning I2C bus for devices ━━━━")
    found = []
    for addr in range(0x03, 0x78):
        try:
            bus.read_byte(addr)
            found.append(addr)
            log.debug("  device found at 0x%02X", addr)
        except Exception:
            pass

    if found:
        log.info("✓ I2C devices found: %s", [f"0x{a:02X}" for a in found])
    else:
        log.error("✗ No I2C devices found on bus — check wiring (SDA/SCL/VCC/GND)")
        sys.exit(1)

    if address in found:
        log.info("✓ LCD found at target address 0x%02X", address)
    else:
        log.error(
            "✗ LCD not found at 0x%02X. Detected: %s",
            address,
            [f"0x{a:02X}" for a in found],
        )
        alt = 0x3F if address == 0x27 else 0x27
        if alt in found:
            log.error("  → Try --address 0x%02X (the other common PCF8574 address)", alt)
        else:
            log.error("  → Check VCC/GND wiring and contrast pot on backpack")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Low-level LCD driver (self-contained, no bridge import needed)
# ---------------------------------------------------------------------------
_RS = 0x01
_EN = 0x04
_BL = 0x08
_ROW = (0x00, 0x40)

_CMD_CLEAR      = 0x01
_CMD_ENTRY_MODE = 0x06
_CMD_DISPLAY_ON = 0x0C
_CMD_FUNC_SET   = 0x28
_CMD_SET_DDRAM  = 0x80


class _RawLCD:
    def __init__(self, bus, address: int) -> None:
        self._b = bus
        self._a = address
        self._bl = _BL

    def _w(self, data: int) -> None:
        self._b.write_byte(self._a, data)

    def _pulse(self, data: int) -> None:
        self._w(data | _EN)
        time.sleep(0.0005)
        self._w(data & ~_EN)
        time.sleep(0.0001)

    def _nibble(self, n: int, mode: int) -> None:
        d = (n & 0xF0) | mode | self._bl
        self._w(d)
        self._pulse(d)

    def _byte(self, b: int, mode: int) -> None:
        self._nibble(b & 0xF0, mode)
        self._nibble((b << 4) & 0xF0, mode)

    def cmd(self, c: int) -> None:
        self._byte(c, 0)

    def char(self, ch: str) -> None:
        self._byte(ord(ch), _RS)

    def cursor(self, row: int, col: int) -> None:
        self.cmd(_CMD_SET_DDRAM | (_ROW[row] + col))

    def clear(self) -> None:
        self.cmd(_CMD_CLEAR)
        time.sleep(0.002)

    def backlight(self, on: bool) -> None:
        self._bl = _BL if on else 0
        self._w(self._bl)

    def init(self) -> None:
        time.sleep(0.05)
        for _ in range(3):
            self._nibble(0x30, 0)
            time.sleep(0.005)
        self._nibble(0x20, 0)
        time.sleep(0.001)
        self.cmd(_CMD_FUNC_SET)
        self.cmd(_CMD_DISPLAY_ON)
        self.cmd(_CMD_CLEAR)
        time.sleep(0.002)
        self.cmd(_CMD_ENTRY_MODE)

    def write_line(self, row: int, text: str) -> None:
        padded = text[:16].ljust(16)
        self.cursor(row, 0)
        for ch in padded:
            try:
                self.char(ch)
            except Exception:
                self.char("?")


# ---------------------------------------------------------------------------
# Step 3 — initialise LCD
# ---------------------------------------------------------------------------
def init_lcd(bus, address: int) -> _RawLCD:
    log.info("━━━━ Step 3: initialising LCD controller ━━━━")
    lcd = _RawLCD(bus, address)
    try:
        lcd.init()
        log.info("✓ LCD HD44780 init sequence complete (4-bit mode, 2 lines)")
        return lcd
    except Exception as exc:
        log.error("✗ LCD init failed: %s", exc)
        log.error("  Check VCC level (3.3V or 5V) and SDA/SCL connections")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
def write_and_log(lcd: _RawLCD, row: int, text: str, label: str) -> None:
    log.info("  Writing line %d: %r", row + 1, text)
    lcd.write_line(row, text)
    log.info("  ✓ %s written to line %d", label, row + 1)


def scroll_line(lcd: _RawLCD, row: int, text: str, delay: float = 0.3) -> None:
    padded = " " * 16 + text + " " * 16
    steps = len(padded) - 15
    log.info("  Scrolling %d steps at %.2fs each ...", steps, delay)
    for i in range(steps):
        lcd.write_line(row, padded[i : i + 16])
        time.sleep(delay)
    log.info("  ✓ Scroll complete")


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------
def run_tests(lcd: _RawLCD) -> None:

    # ── Test 4: static text ──────────────────────────────────────────────
    log.info("━━━━ Step 4: static text test ━━━━")
    write_and_log(lcd, 0, "  LCD Test OK  ", "static line 1")
    write_and_log(lcd, 1, " Zenii  PiDog  ", "static line 2")
    log.info("  >>> CHECK: does the LCD show text? If not, turn the contrast pot on the back.")
    time.sleep(3)

    # ── Test 5: backlight toggle ─────────────────────────────────────────
    log.info("━━━━ Step 5: backlight toggle ━━━━")
    log.info("  Turning backlight OFF for 1s ...")
    lcd.backlight(False)
    time.sleep(1)
    log.info("  Turning backlight ON ...")
    lcd.backlight(True)
    time.sleep(0.5)
    log.info("  ✓ Backlight toggle OK")

    # ── Test 6: all 16 positions on each line ────────────────────────────
    log.info("━━━━ Step 6: character position test ━━━━")
    write_and_log(lcd, 0, "0123456789ABCDEF", "position test line 1")
    write_and_log(lcd, 1, "GHIJKLMNOPQRSTUV", "position test line 2")
    log.info("  >>> CHECK: both lines should show 16 characters with no gaps")
    time.sleep(3)

    # ── Test 7: clear ────────────────────────────────────────────────────
    log.info("━━━━ Step 7: clear display ━━━━")
    lcd.clear()
    log.info("  ✓ Display cleared (both lines blank)")
    time.sleep(1)

    # ── Test 8: scrolling text ───────────────────────────────────────────
    log.info("━━━━ Step 8: scrolling text (simulates TTS response) ━━━━")
    write_and_log(lcd, 0, ">sit down please", "user command")
    log.info("  Scrolling PiDog response on line 2 ...")
    scroll_line(lcd, 1, "Sure! Let me sit down for you.", delay=0.3)
    time.sleep(1)

    # ── Test 9: bridge-style display ─────────────────────────────────────
    log.info("━━━━ Step 9: bridge simulation ━━━━")
    log.info("  Showing startup splash (2s) ...")
    write_and_log(lcd, 0, "  Zenii PiDog  ", "splash line 1")
    write_and_log(lcd, 1, "   I'm ready!  ", "splash line 2")
    time.sleep(2)

    write_and_log(lcd, 0, ">hello pidog    ", "user input")
    write_and_log(lcd, 1, "Woof! Hi there!", "short reply (static)")
    time.sleep(2)

    write_and_log(lcd, 0, ">do a push up   ", "user input")
    log.info("  Scrolling longer reply ...")
    scroll_line(lcd, 1, "Sure! Watch me do a push-up!", delay=0.3)

    # ── Done ─────────────────────────────────────────────────────────────
    log.info("━━━━ All tests passed ━━━━")
    write_and_log(lcd, 0, "  All tests OK  ", "final line 1")
    write_and_log(lcd, 1, "  Bridge ready  ", "final line 2")
    time.sleep(3)

    log.info("Clearing and turning off backlight")
    lcd.clear()
    lcd.backlight(False)
    log.info("✓ Done — LCD is working correctly. Enable it in bridge_config.toml:")
    log.info("    [lcd]")
    log.info("    enabled = true")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="LCD1602 I2C test script")
    parser.add_argument("--address", default="0x27",
                        help="I2C address of LCD (default: 0x27)")
    parser.add_argument("--bus", type=int, default=1,
                        help="I2C bus number (default: 1)")
    args = parser.parse_args()

    address = int(args.address, 16) if args.address.startswith("0x") else int(args.address)

    log.info("═══════════════════════════════════════")
    log.info("  LCD1602 Test  bus=%d  address=0x%02X", args.bus, address)
    log.info("═══════════════════════════════════════")

    smbus = check_smbus()
    bus = open_bus(smbus, args.bus)
    detect_lcd(bus, address)
    lcd = init_lcd(bus, address)
    run_tests(lcd)


if __name__ == "__main__":
    main()
