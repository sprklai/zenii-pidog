#!/usr/bin/env python3
"""Standalone LCD1602 test script — with contrast and I2C diagnostics.

Run this directly on the Pi (inside the venv):

  python3 test_lcd.py              # default address 0x27
  python3 test_lcd.py --address 0x3F

Step-by-step tests with clear pass/fail messages.
If Step 4 shows backlight blinking but no characters → turn the contrast pot.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lcd_test")

# ---------------------------------------------------------------------------
# PCF8574 → HD44780 bit mapping
# P0=RS  P1=RW  P2=E  P3=BL  P4=D4  P5=D5  P6=D6  P7=D7
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


# ---------------------------------------------------------------------------
# Step 0 — smbus
# ---------------------------------------------------------------------------
def check_smbus():
    log.info("━━━━ Step 0: checking smbus library ━━━━")
    try:
        import smbus2 as smbus
        log.info("✓ smbus2 %s imported", getattr(smbus, "__version__", ""))
        return smbus
    except ImportError:
        pass
    try:
        import smbus  # type: ignore[import]
        log.info("✓ system smbus imported")
        return smbus
    except ImportError:
        log.error("✗ smbus not found — run: pip install smbus2")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 1 — open bus
# ---------------------------------------------------------------------------
def open_bus(smbus, bus_num: int):
    log.info("━━━━ Step 1: opening I2C bus %d ━━━━", bus_num)
    try:
        bus = smbus.SMBus(bus_num)
        log.info("✓ I2C bus %d opened", bus_num)
        return bus
    except FileNotFoundError:
        log.error("✗ /dev/i2c-%d not found — enable I2C via raspi-config", bus_num)
        sys.exit(1)
    except PermissionError:
        log.error("✗ Permission denied — sudo usermod -aG i2c $USER then re-login")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — scan bus
# ---------------------------------------------------------------------------
def detect_lcd(bus, address: int) -> None:
    log.info("━━━━ Step 2: scanning I2C bus for devices ━━━━")
    found = []
    for addr in range(0x03, 0x78):
        try:
            bus.read_byte(addr)
            found.append(addr)
            log.debug("  device at 0x%02X", addr)
        except Exception:
            pass
    log.info("✓ devices found: %s", [f"0x{a:02X}" for a in found])
    if address not in found:
        alt = 0x3F if address == 0x27 else 0x27
        log.error("✗ LCD not at 0x%02X — try: python3 test_lcd.py --address 0x%02X", address, alt)
        sys.exit(1)
    log.info("✓ LCD detected at 0x%02X", address)


# ---------------------------------------------------------------------------
# Step 3 — raw I2C write test (backlight toggle — visible with eyes)
# ---------------------------------------------------------------------------
def test_backlight(bus, address: int) -> None:
    log.info("━━━━ Step 3: backlight toggle test (I2C write check) ━━━━")
    log.info("  Watch the LCD backlight — it should blink OFF for 1 second then ON")
    try:
        bus.write_byte(address, _BL)          # backlight ON
        time.sleep(0.5)
        bus.write_byte(address, 0x00)         # backlight OFF
        log.info("  >>> backlight should be OFF now ...")
        time.sleep(1.0)
        bus.write_byte(address, _BL)          # backlight ON
        log.info("  >>> backlight should be ON again")
        time.sleep(0.5)
        log.info("✓ I2C writes working (if backlight blinked)")
        log.info("  If backlight did NOT blink → wiring problem on SDA/SCL/GND/VCC")
    except Exception as exc:
        log.error("✗ I2C write failed: %s", exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Low-level driver (self-contained)
# ---------------------------------------------------------------------------
class _RawLCD:
    def __init__(self, bus, address: int) -> None:
        self._b = bus
        self._a = address
        self._bl = _BL

    def _w(self, data: int) -> None:
        self._b.write_byte(self._a, data)

    def _pulse(self, data: int) -> None:
        self._w(data | _EN)
        time.sleep(0.001)        # 1ms — generous for slow displays
        self._w(data & ~_EN)
        time.sleep(0.001)

    def _nibble(self, n: int, mode: int) -> None:
        d = (n & 0xF0) | mode | self._bl
        self._w(d)
        self._pulse(d)

    def _byte(self, b: int, mode: int) -> None:
        self._nibble(b & 0xF0, mode)
        self._nibble((b << 4) & 0xF0, mode)

    def cmd(self, c: int) -> None:
        self._byte(c, 0)

    def char_byte(self, b: int) -> None:
        """Write a raw byte as character data (for filled blocks etc.)."""
        self._nibble(b & 0xF0, _RS)
        self._nibble((b << 4) & 0xF0, _RS)

    def cursor(self, row: int, col: int) -> None:
        self.cmd(_CMD_SET_DDRAM | (_ROW[row] + col))

    def clear(self) -> None:
        self.cmd(_CMD_CLEAR)
        time.sleep(0.003)

    def backlight(self, on: bool) -> None:
        self._bl = _BL if on else 0
        self._w(self._bl)

    def write_line(self, row: int, text: str) -> None:
        padded = text[:16].ljust(16)
        self.cursor(row, 0)
        for ch in padded:
            try:
                self._byte(ord(ch), _RS)
            except Exception:
                self._byte(ord("?"), _RS)

    def fill_blocks(self, row: int) -> None:
        """Write 16 filled-block characters (chr(0xFF)) to a row.
        These appear as solid dark rectangles regardless of contrast level.
        """
        self.cursor(row, 0)
        for _ in range(16):
            self.char_byte(0xFF)

    def init(self) -> None:
        time.sleep(0.1)           # extra wait after power-on
        # HD44780 4-bit init sequence (with generous delays)
        for delay in (0.005, 0.005, 0.002):
            self._nibble(0x30, 0)
            time.sleep(delay)
        self._nibble(0x20, 0)     # switch to 4-bit mode
        time.sleep(0.002)
        self.cmd(_CMD_FUNC_SET)   # 0x28: 4-bit, 2 lines, 5x8
        time.sleep(0.001)
        self.cmd(_CMD_DISPLAY_ON) # 0x0C: display on, cursor off
        time.sleep(0.001)
        self.cmd(_CMD_CLEAR)      # 0x01: clear
        time.sleep(0.003)
        self.cmd(_CMD_ENTRY_MODE) # 0x06: L→R, no shift
        time.sleep(0.001)


# ---------------------------------------------------------------------------
# Step 4 — init LCD
# ---------------------------------------------------------------------------
def init_lcd(bus, address: int) -> _RawLCD:
    log.info("━━━━ Step 4: initialising LCD HD44780 controller ━━━━")
    lcd = _RawLCD(bus, address)
    try:
        lcd.init()
        log.info("✓ HD44780 init complete (4-bit, 2 lines)")
        return lcd
    except Exception as exc:
        log.error("✗ Init failed: %s", exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Step 5 — filled block test
# ---------------------------------------------------------------------------
def test_filled_blocks(lcd: _RawLCD) -> None:
    log.info("━━━━ Step 5: filled-block test ━━━━")
    log.info("  Writing 16 solid blocks to each line (visible at any contrast level)")
    lcd.fill_blocks(0)
    lcd.fill_blocks(1)
    log.info("  >>> CHECK: do you see two rows of solid dark rectangles?")
    log.info("  If YES → LCD is working, contrast pot just needs adjustment")
    log.info("  If NO  → check VCC (try 5V from RPi Pin 2 instead of HAT 3.3V)")
    time.sleep(5)


# ---------------------------------------------------------------------------
# Step 6 — contrast guidance
# ---------------------------------------------------------------------------
def guide_contrast(lcd: _RawLCD) -> None:
    log.info("━━━━ Step 6: contrast adjustment guide ━━━━")
    log.info("  Writing text to both lines ...")
    lcd.write_line(0, "ADJUST CONTRAST ")
    lcd.write_line(1, "TURN BLUE POT-> ")
    log.info("  >>> TURN THE BLUE SCREW POT on the back of the LCD backpack")
    log.info("      (small blue rectangular component with a cross screw)")
    log.info("      Turn slowly while looking at the LCD — text will appear")
    log.info("      Clockwise = more contrast on most modules")
    log.info("  Holding for 15 seconds — adjust the pot now ...")
    time.sleep(15)


# ---------------------------------------------------------------------------
# Step 7 — static text test
# ---------------------------------------------------------------------------
def test_static(lcd: _RawLCD) -> None:
    log.info("━━━━ Step 7: static text test ━━━━")
    lcd.write_line(0, "  LCD Test OK   ")
    lcd.write_line(1, " Zenii  PiDog   ")
    log.info("  >>> Both lines should show readable text now")
    time.sleep(3)


# ---------------------------------------------------------------------------
# Step 8 — scroll test
# ---------------------------------------------------------------------------
def test_scroll(lcd: _RawLCD) -> None:
    log.info("━━━━ Step 8: scroll test (simulates TTS response) ━━━━")
    lcd.write_line(0, ">sit down please")
    text = "Sure! Let me sit down for you."
    padded = " " * 16 + text + " " * 16
    log.info("  Scrolling %d chars on line 2 ...", len(text))
    for i in range(len(padded) - 15):
        lcd.write_line(1, padded[i : i + 16])
        time.sleep(0.3)
    log.info("✓ Scroll complete")
    time.sleep(1)


# ---------------------------------------------------------------------------
# Step 9 — bridge sim
# ---------------------------------------------------------------------------
def test_bridge_sim(lcd: _RawLCD) -> None:
    log.info("━━━━ Step 9: bridge startup simulation ━━━━")
    lcd.write_line(0, "  Zenii PiDog  ")
    lcd.write_line(1, "   I'm ready!  ")
    log.info("  >>> This is what the LCD will show when the bridge starts")
    time.sleep(3)
    lcd.clear()
    lcd.backlight(False)
    log.info("✓ Done — LCD working correctly")
    log.info("")
    log.info("  Enable in bridge_config.toml:")
    log.info("    [lcd]")
    log.info("    enabled = true")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="LCD1602 I2C test")
    parser.add_argument("--address", default="0x27")
    parser.add_argument("--bus", type=int, default=1)
    args = parser.parse_args()

    address = int(args.address, 16) if args.address.startswith("0x") else int(args.address)

    log.info("═══════════════════════════════════════════")
    log.info("  LCD1602 Test   bus=%d   address=0x%02X", args.bus, address)
    log.info("═══════════════════════════════════════════")

    smbus   = check_smbus()
    bus     = open_bus(smbus, args.bus)
    detect_lcd(bus, address)
    test_backlight(bus, address)
    lcd     = init_lcd(bus, address)
    test_filled_blocks(lcd)
    guide_contrast(lcd)
    test_static(lcd)
    test_scroll(lcd)
    test_bridge_sim(lcd)


if __name__ == "__main__":
    main()
