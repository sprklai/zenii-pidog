"""PiDog2 hardware abstraction: real hardware + simulated fallback."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from .action_parser import LEDCommand, PiDogAction
from .config import BridgeConfig

logger = logging.getLogger(__name__)


@dataclass
class SensorReading:
    """Immutable snapshot of all sensor data."""

    distance_cm: int
    touch: str  # "left" | "right" | "both" | "none"
    sound_direction_deg: int  # 0-360
    pitch: float
    roll: float
    yaw: float
    timestamp: float

    def to_context_string(self) -> str:
        """Format for prompt injection before user speech."""
        t = datetime.fromtimestamp(self.timestamp).strftime("%H:%M")

        if abs(self.pitch) < 5 and abs(self.roll) < 5:
            imu_str = "stable"
        else:
            imu_str = f"pitch={self.pitch:.1f} roll={self.roll:.1f}"

        return (
            f"[Sensors] Distance: {self.distance_cm}cm"
            f" | Touch: {self.touch}"
            f" | Sound: {self.sound_direction_deg}deg"
            f" | IMU: {imu_str}"
            f" | Time: {t}"
        )

    def differs_significantly(self, other: SensorReading | None) -> bool:
        """Return True if readings changed enough to warrant a /memory POST."""
        if other is None:
            return True
        if self.touch != other.touch:
            return True
        if abs(self.distance_cm - other.distance_cm) > 10:
            return True
        if abs(self.sound_direction_deg - other.sound_direction_deg) > 30:
            return True
        if abs(self.pitch - other.pitch) > 5 or abs(self.roll - other.roll) > 5:
            return True
        return False


class HardwareInterface(ABC):
    """Abstract interface for PiDog2 hardware."""

    @abstractmethod
    async def read_sensors(self) -> SensorReading:
        """Read all sensors and return a snapshot."""

    @abstractmethod
    async def execute_action(self, action: PiDogAction) -> None:
        """Execute a physical action (servo movement, sound)."""

    @abstractmethod
    async def set_leds(self, cmd: LEDCommand) -> None:
        """Set RGB LED strip mode/color/brightness."""

    @abstractmethod
    async def close(self) -> None:
        """Release hardware resources."""


class RealHardware(HardwareInterface):
    """Wraps actual PiDog2 library. Sync calls run via asyncio.to_thread()."""

    def __init__(self) -> None:
        import gc

        from pidog import Pidog  # type: ignore[import-untyped]

        self._dog = Pidog()
        # Force GC so intermediate gpiozero objects created during Pidog.__init__
        # are finalized NOW (in this thread) rather than later in the asyncio loop,
        # which would print "Exception ignored in GPIOBase.__del__: GPIO busy".
        gc.collect()
        self._rgb = self._dog.rgb_strip
        self._action_lock = asyncio.Lock()
        logger.info("PiDog2 hardware initialized")

    def _read_imu_sync(self) -> tuple[float, float, float]:
        # pidog updates dog.pitch and dog.roll via its internal imu_thread
        pitch = float(getattr(self._dog, "pitch", 0.0) or 0.0)
        roll = float(getattr(self._dog, "roll", 0.0) or 0.0)
        return pitch, roll, 0.0

    def _read_sensors_sync(self) -> SensorReading:
        distance = self._dog.read_distance()
        try:
            touch = self._dog.dual_touch.read()
        except AttributeError:
            # robot_hat Pin.value() calls InputDevice.on() missing in newer gpiozero
            touch = getattr(self._dog, "touch", "N")
        try:
            sound_dir = self._dog.ears.read()
        except AttributeError:
            sound_dir = -1
        pitch, roll, yaw = self._read_imu_sync()

        return SensorReading(
            distance_cm=int(distance) if distance is not None else 999,
            touch=str(touch) if touch and touch != "N" else "none",
            sound_direction_deg=int(sound_dir) if sound_dir and sound_dir >= 0 else 0,
            pitch=pitch,
            roll=roll,
            yaw=yaw,
            timestamp=time.time(),
        )

    async def read_sensors(self) -> SensorReading:
        return await asyncio.to_thread(self._read_sensors_sync)

    async def execute_action(self, action: PiDogAction) -> None:
        async with self._action_lock:
            await asyncio.to_thread(
                self._dog.do_action, action.action, speed=action.speed
            )

    # Maps user-facing LED mode names to pidog RGBStrip style names.
    # Full pidog style list: monochromatic, breath, boom, bark, speak, listen
    _LED_MODE_MAP: dict[str, str] = {
        "solid":   "monochromatic",
        "blink":   "boom",
        "trail":   "speak",
        "breath":  "breath",
        "listen":  "listen",
        "bark":    "bark",
    }

    async def set_leds(self, cmd: LEDCommand) -> None:
        r, g, b = _hex_to_rgb(cmd.color)
        style = self._LED_MODE_MAP.get(cmd.mode, "monochromatic")
        brightness = max(0.0, min(1.0, cmd.brightness / 100.0))
        bps = 1.0
        async with self._action_lock:
            await asyncio.to_thread(
                self._rgb.set_mode, style, (r, g, b), bps, brightness
            )

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._rgb.set_mode, "monochromatic", (0, 0, 0), 1, 0)
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._dog.close)
        except Exception:
            pass
        logger.info("PiDog2 hardware closed")


class SimulatedHardware(HardwareInterface):
    """Mock for development/testing without physical PiDog.

    Logs all actions and returns slowly-varying synthetic sensor data.
    """

    def __init__(self) -> None:
        self._base_time = time.time()
        logger.info("Simulated hardware initialized (no PiDog connected)")

    async def read_sensors(self) -> SensorReading:
        elapsed = time.time() - self._base_time
        # Slowly varying distance (30-80cm range)
        distance = int(55 + 25 * math.sin(elapsed / 10))
        # Occasional touch events (~5% chance)
        touch_options = ["none"] * 19 + ["left"]
        touch = random.choice(touch_options)
        # Slowly rotating sound direction
        sound_dir = int((elapsed * 10) % 360)
        # Stable IMU with minor noise
        pitch = random.gauss(0, 0.5)
        roll = random.gauss(0, 0.5)

        return SensorReading(
            distance_cm=distance,
            touch=touch,
            sound_direction_deg=sound_dir,
            pitch=round(pitch, 2),
            roll=round(roll, 2),
            yaw=0.0,
            timestamp=time.time(),
        )

    async def execute_action(self, action: PiDogAction) -> None:
        logger.info("[SIM] Action: %s (speed=%d)", action.action, action.speed)

    async def set_leds(self, cmd: LEDCommand) -> None:
        logger.info(
            "[SIM] LEDs: mode=%s color=%s brightness=%d",
            cmd.mode,
            cmd.color,
            cmd.brightness,
        )

    async def close(self) -> None:
        logger.info("Simulated hardware closed")


def create_hardware(config: BridgeConfig) -> HardwareInterface:
    """Factory: return real or simulated hardware based on config.

    If simulate_hardware=False (real hardware requested) and the pidog library
    is missing or fails to initialise, startup is aborted with a clear error
    rather than silently running in simulation.  Set simulate_hardware=true in
    bridge_config.toml (or PIDOG_SIMULATE=true) to allow simulation fallback.
    """
    if config.simulate_hardware:
        return SimulatedHardware()
    try:
        hw = RealHardware()
        logger.info("PiDog2 real hardware ready")
        return hw
    except ImportError as exc:
        raise RuntimeError(
            "pidog library not found — cannot start with simulate_hardware=false.\n"
            "  Fix A: install the library:  pip install pidog\n"
            "  Fix B: enable simulation:    PIDOG_SIMULATE=true  or  simulate_hardware = true"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"PiDog hardware init failed ({exc}) — cannot start with simulate_hardware=false.\n"
            "  Fix A: check hardware connections and power.\n"
            "  Fix B: enable simulation:    PIDOG_SIMULATE=true  or  simulate_hardware = true"
        ) from exc


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return (0, 0, 0)
    try:
        return (
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )
    except ValueError:
        return (0, 0, 0)
