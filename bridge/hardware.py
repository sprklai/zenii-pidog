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
    async def sit_and_stop(self) -> None:
        """Move to sitting position and stop all motion. Called on shutdown.

        Bypasses the action queue — safe to call after loops are cancelled.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release hardware resources."""


class RealHardware(HardwareInterface):
    """Wraps actual PiDog2 library. Sync calls run via asyncio.to_thread()."""

    def __init__(self) -> None:
        import gc
        import sys
        import time as _time

        from pidog import Pidog  # type: ignore[import-untyped]

        # Suppress the benign gpiozero __del__ "GPIO busy" traceback that fires
        # when old InputDevice objects (from Pidog.__init__ internals) are GC'd
        # after pidog has already re-claimed the pins.  This is not a real error —
        # lgpio's close() tries to claim-input before releasing, which fails when
        # another owner already holds the pin.  We install a targeted hook here so
        # it is only active while RealHardware is alive.
        _orig_hook = sys.unraisablehook

        def _gpio_gc_hook(ur: sys.UnraisableHookArgs) -> None:
            if (
                isinstance(ur.exc_value, Exception)
                and "GPIO busy" in str(ur.exc_value)
                and ur.object is not None
                and "__del__" in str(ur.object)
            ):
                return  # benign gpiozero cleanup race — drop silently
            _orig_hook(ur)

        sys.unraisablehook = _gpio_gc_hook
        self._orig_unraisable_hook = _orig_hook  # restored in close()

        self._dog = Pidog()
        # Brief pause (from official examples: sleep(0.1) after Pidog()) lets
        # servo and sensor threads fully settle before any commands are sent.
        _time.sleep(0.2)
        gc.collect()
        self._rgb = self._dog.rgb_strip
        # Separate locks: servos and LEDs are independent hardware subsystems.
        # Using one lock blocked LED updates for the full duration of slow servo
        # actions (up to action_timeout_secs=10s).
        self._servo_lock = asyncio.Lock()
        self._led_lock = asyncio.Lock()
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

    def _execute_action_sync(self, action: PiDogAction) -> None:
        # do_action() queues internally and returns immediately; wait_all_done()
        # blocks until all body parts finish — prevents actions from overlapping.
        self._dog.do_action(action.action, speed=action.speed)
        self._dog.wait_all_done()

    async def execute_action(self, action: PiDogAction) -> None:
        async with self._servo_lock:
            await asyncio.to_thread(self._execute_action_sync, action)

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
        async with self._led_lock:
            await asyncio.to_thread(
                self._rgb.set_mode, style, [r, g, b], bps, brightness
            )

    def _sit_and_stop_sync(self) -> None:
        # Called directly — bypasses the action queue/lock so it's safe
        # to invoke even after asyncio loops have been cancelled.
        logger.info("Shutdown: moving PiDog to lie down position ...")
        try:
            self._dog.do_action("lie", speed=50)  # pidog key for lie_down
            self._dog.wait_all_done()
            logger.info("Shutdown: PiDog is lying down")
        except Exception as exc:
            logger.warning("Shutdown lie_down failed (continuing): %s", exc)

    async def sit_and_stop(self) -> None:
        await asyncio.to_thread(self._sit_and_stop_sync)

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._rgb.set_mode, "monochromatic", [0, 0, 0], 1, 0)
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._dog.close)
        except Exception:
            pass
        import sys
        sys.unraisablehook = self._orig_unraisable_hook
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

    async def sit_and_stop(self) -> None:
        logger.info("[SIM] Shutdown: PiDog sitting and stopping")

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
