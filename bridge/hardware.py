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
        from pidog import Pidog  # type: ignore[import-untyped]

        self._dog = Pidog()
        self._rgb = self._dog.rgb_strip
        self._action_lock = asyncio.Lock()
        logger.info("PiDog2 hardware initialized")

    def _read_sensors_sync(self) -> SensorReading:
        distance = self._dog.ultrasonic.read_distance()
        touch = self._dog.dual_touch.read()
        sound_dir = self._dog.sound_direction.read_direction()
        accel = self._dog.imu.read_accel()

        return SensorReading(
            distance_cm=int(distance) if distance is not None else 999,
            touch=str(touch) if touch else "none",
            sound_direction_deg=int(sound_dir) if sound_dir is not None else 0,
            pitch=float(accel[0]) if accel else 0.0,
            roll=float(accel[1]) if accel else 0.0,
            yaw=float(accel[2]) if len(accel) > 2 else 0.0,
            timestamp=time.time(),
        )

    async def read_sensors(self) -> SensorReading:
        return await asyncio.to_thread(self._read_sensors_sync)

    async def execute_action(self, action: PiDogAction) -> None:
        async with self._action_lock:
            speed = action.speed
            # Movement actions take speed param; posture/expression actions don't
            movement_actions = {"forward", "backward", "turn_left", "turn_right"}
            if action.action in movement_actions:
                await asyncio.to_thread(
                    self._dog.do_action, action.action, speed=speed
                )
            else:
                await asyncio.to_thread(self._dog.do_action, action.action)

    async def set_leds(self, cmd: LEDCommand) -> None:
        r, g, b = _hex_to_rgb(cmd.color)
        await asyncio.to_thread(
            self._rgb.set_mode, cmd.mode, (r, g, b), cmd.brightness
        )

    async def close(self) -> None:
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
    """Factory: return real or simulated hardware based on config."""
    if config.simulate_hardware:
        return SimulatedHardware()
    try:
        return RealHardware()
    except ImportError:
        logger.warning(
            "pidog library not found — falling back to simulation mode"
        )
        return SimulatedHardware()
    except Exception as exc:
        logger.warning(
            "Failed to initialize PiDog hardware (%s) — falling back to simulation",
            exc,
        )
        return SimulatedHardware()


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
