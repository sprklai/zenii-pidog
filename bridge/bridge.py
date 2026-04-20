"""Main PiDogZeniiBridge: orchestrates voice, sensor, and action loops.

Concurrency model:
  - asyncio event loop: WS/HTTP I/O, orchestration, timers
  - ThreadPoolExecutor: hardware I/O (sensors, servos), audio recording/playback
  - asyncio.Queue: action pipeline (voice loop -> action executor)
  - asyncio.Lock: WS send serialization, TTS serialization
  - asyncio.Event: shutdown coordination

All blocking I/O runs in the thread pool to avoid choking the event loop.
All async operations have timeouts to prevent hanging.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .action_parser import LEDCommand, PiDogAction, parse_response
from .config import BridgeConfig
from .hardware import HardwareInterface, SensorReading, create_hardware
from .voice import VoiceInterface, create_voice
from .zenii_client import ZeniiClient

logger = logging.getLogger(__name__)

# LED presets
LEDS_IDLE = LEDCommand(mode="breath", color="#333399", brightness=20)
LEDS_THINKING = LEDCommand(mode="trail", color="#0088FF", brightness=50)
LEDS_ALERT = LEDCommand(mode="blink", color="#FF0000", brightness=100)
LEDS_HAPPY = LEDCommand(mode="breath", color="#00FF00", brightness=80)
LEDS_OFF = LEDCommand(mode="solid", color="#000000", brightness=0)


class PiDogZeniiBridge:
    """Connects PiDog2 hardware to Zenii AI daemon.

    Three concurrent async loops + dedicated thread pool for blocking I/O:
    1. Voice loop: listen -> AI chat (WS) -> speak + execute actions
    2. Sensor loop: read sensors -> reactive triggers -> memory storage
    3. Action executor: sequential servo/LED command execution
    """

    def __init__(
        self,
        config: BridgeConfig,
        executor: ThreadPoolExecutor,
    ) -> None:
        self._config = config
        self._executor = executor
        self._client = ZeniiClient(config)
        self._hardware: HardwareInterface = create_hardware(config)
        self._voice: VoiceInterface = create_voice(config, executor)

        # Action pipeline: voice loop and sensor triggers enqueue;
        # action executor dequeues sequentially.
        self._action_queue: asyncio.Queue[PiDogAction | LEDCommand] = asyncio.Queue(
            maxsize=64
        )

        self._session_id: str | None = None
        self._last_sensor: SensorReading | None = None
        self._last_memory_time: float = 0.0
        self._last_touch_event: float = 0.0
        self._last_obstacle_event: float = 0.0
        self._shutdown_event = asyncio.Event()

        # Track fire-and-forget tasks for clean shutdown
        self._bg_tasks: set[asyncio.Task] = set()

    # -- Lifecycle --

    async def start(self) -> None:
        """Full lifecycle: wait for daemon, create session, run loops."""
        await self._client.start()

        logger.info("Waiting for Zenii daemon at %s ...", self._config.zenii_url)
        await self._wait_for_daemon()
        logger.info("Daemon is healthy")

        self._session_id = await asyncio.wait_for(
            self._client.create_session(self._config.session_title),
            timeout=15.0,
        )
        logger.info("Session created: %s", self._session_id)

        await asyncio.wait_for(self._client.ws_connect(), timeout=10.0)

        # Configure AI provider in Zenii if specified in bridge_config.toml
        await self._configure_ai_provider()

        # Set idle mood
        self._enqueue_action(LEDS_IDLE)

        logger.info("Bridge started — entering main loops")

        try:
            await asyncio.gather(
                self._voice_loop(),
                self._sensor_loop(),
                self._action_executor(),
                self._shutdown_watcher(),
            )
        except asyncio.CancelledError:
            logger.info("Bridge loops cancelled")
        finally:
            await self._shutdown()

    async def _wait_for_daemon(self) -> None:
        """Poll GET /health until daemon responds. Exponential backoff."""
        delay = self._config.ws_reconnect_delay_secs
        max_delay = self._config.ws_max_reconnect_delay_secs

        while not self._shutdown_event.is_set():
            try:
                healthy = await asyncio.wait_for(
                    self._client.health_check(), timeout=5.0
                )
                if healthy:
                    return
            except asyncio.TimeoutError:
                pass
            logger.info("Daemon not ready, retrying in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    async def _configure_ai_provider(self) -> None:
        """Push AI provider config from bridge_config.toml into the Zenii daemon.

        Only runs if ai_provider is set.  Idempotent — safe to call on every startup.
        """
        if not self._config.ai_provider:
            return
        provider = self._config.ai_provider
        model = self._config.ai_model
        api_key = self._config.ai_api_key
        try:
            if api_key:
                await asyncio.wait_for(
                    self._client.set_credential(f"api_key:{provider}", api_key),
                    timeout=5.0,
                )
                logger.info("AI provider key stored: api_key:%s", provider)
            if model:
                await asyncio.wait_for(
                    self._client.set_default_provider(provider, model),
                    timeout=5.0,
                )
                logger.info("AI provider set: %s / %s", provider, model)
            elif provider:
                logger.info("AI provider key set for %s (no model specified)", provider)
        except Exception as exc:
            logger.warning("Failed to configure AI provider: %s", exc)

    async def _shutdown_watcher(self) -> None:
        """Wait for shutdown signal, then cancel all tasks."""
        await self._shutdown_event.wait()
        raise asyncio.CancelledError()

    async def _shutdown(self) -> None:
        """Graceful cleanup: stand, LEDs off, close connections."""
        logger.info("Shutting down bridge...")

        # Cancel background tasks
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Safe hardware shutdown with timeout
        try:
            await asyncio.wait_for(
                self._hardware.execute_action(PiDogAction("sit", 50)),
                timeout=3.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            await asyncio.wait_for(
                self._hardware.set_leds(LEDS_OFF),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass

        await self._hardware.close()
        await self._voice.close()
        await self._client.close()
        logger.info("Bridge shutdown complete")

    def request_shutdown(self) -> None:
        """Signal all loops to stop (called from signal handler)."""
        self._shutdown_event.set()

    # -- Helpers --

    def _enqueue_action(self, item: PiDogAction | LEDCommand) -> None:
        """Non-blocking enqueue. Drops if queue is full (prevents backpressure)."""
        try:
            self._action_queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.debug("Action queue full, dropping: %s", item)

    def _fire_and_forget(self, coro) -> None:
        """Spawn a background task with lifecycle tracking."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- Voice Loop --

    async def _voice_loop(self) -> None:
        """Listen -> build prompt with sensor context -> WS chat -> parse -> act + speak.

        Low-latency optimizations:
        - Actions enqueued immediately as response streams (don't wait for full text)
        - LEDs set to thinking BEFORE sending prompt
        - TTS starts as soon as text is available
        """
        while not self._shutdown_event.is_set():
            try:
                text = await self._voice.listen()
                if text is None:
                    await asyncio.sleep(0.05)
                    continue

                logger.info("User: %s", text)

                # Build prompt with sensor context
                prompt = self._build_prompt(text)

                # Set thinking LEDs immediately
                self._enqueue_action(LEDS_THINKING)

                # Send via WebSocket and collect response (with timeout)
                try:
                    response_text = await asyncio.wait_for(
                        self._ws_chat(prompt),
                        timeout=self._config.ws_chat_timeout_secs,
                    )
                except asyncio.TimeoutError:
                    logger.warning("WS chat timed out after %.0fs", self._config.ws_chat_timeout_secs)
                    self._enqueue_action(LEDS_ALERT)
                    await asyncio.sleep(1)
                    self._enqueue_action(LEDS_IDLE)
                    continue

                if response_text:
                    # Parse actions and LEDs from response
                    parsed = parse_response(
                        response_text, self._config.default_action_speed
                    )

                    # Enqueue all actions immediately (non-blocking)
                    for action in parsed.actions:
                        self._enqueue_action(action)
                    for led_cmd in parsed.led_commands:
                        self._enqueue_action(led_cmd)

                    # Speak the clean text (concurrent with action execution)
                    if parsed.clean_text:
                        await self._voice.speak(parsed.clean_text)

                # Return to idle LEDs
                self._enqueue_action(LEDS_IDLE)

            except asyncio.CancelledError:
                raise
            except ConnectionError as exc:
                logger.warning("WS connection lost: %s", exc)
                self._enqueue_action(LEDS_ALERT)
                await asyncio.sleep(self._config.ws_reconnect_delay_secs)
            except Exception as exc:
                logger.error("Voice loop error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    def _build_prompt(self, user_text: str) -> str:
        """Prepend sensor context to user speech."""
        if self._last_sensor:
            sensor_ctx = self._last_sensor.to_context_string()
            return f"{sensor_ctx}\nUser said: {user_text}"
        return f"User said: {user_text}"

    async def _ws_chat(self, prompt: str) -> str | None:
        """Send prompt over WS, collect text messages until done.

        Accumulates all text fragments. Tool calls/results are logged
        but not returned (they're intermediate AI reasoning steps).
        """
        await self._client.ws_ensure_connected()
        await self._client.ws_send_prompt(prompt, self._session_id)

        accumulated: list[str] = []

        async for msg in self._client.ws_receive():
            msg_type = msg.get("type", "")

            if msg_type == "text":
                content = msg.get("content", "")
                if content:
                    accumulated.append(content)

            elif msg_type == "tool_call":
                logger.debug(
                    "Tool call: %s(%s)",
                    msg.get("tool_name"),
                    msg.get("args"),
                )

            elif msg_type == "tool_result":
                success = "OK" if msg.get("success") else "FAIL"
                logger.debug(
                    "Tool result: %s -> %s",
                    msg.get("tool_name"),
                    success,
                )

            elif msg_type == "error":
                logger.error(
                    "Chat error: %s (hint: %s)",
                    msg.get("error"),
                    msg.get("hint"),
                )
                return None

            elif msg_type == "done":
                break

        return "".join(accumulated) if accumulated else None

    # -- Sensor Loop --

    async def _sensor_loop(self) -> None:
        """Read sensors periodically, trigger reactions, store to memory.

        All sensor reads have timeouts to prevent hanging on faulty hardware.
        Memory storage is fire-and-forget to avoid blocking the loop.
        """
        while not self._shutdown_event.is_set():
            try:
                reading = await asyncio.wait_for(
                    self._hardware.read_sensors(),
                    timeout=self._config.sensor_read_timeout_secs,
                )
                self._check_reactive_triggers(reading)

                # Store to memory if significant change or throttle expired
                now = time.time()
                should_store = reading.differs_significantly(self._last_sensor)
                throttle_expired = (
                    now - self._last_memory_time > self._config.memory_throttle_secs
                )

                if should_store or throttle_expired:
                    self._last_memory_time = now
                    # Fire-and-forget: don't block sensor loop on HTTP
                    self._fire_and_forget(
                        self._client.store_memory(
                            key="pidog:sensors:latest",
                            content=reading.to_context_string(),
                            category="daily",
                        )
                    )

                self._last_sensor = reading

            except asyncio.TimeoutError:
                logger.warning("Sensor read timed out")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Sensor loop error: %s", exc)

            await asyncio.sleep(self._config.sensor_interval_secs)

    def _check_reactive_triggers(self, reading: SensorReading) -> None:
        """Evaluate sensor data for reactive behaviors.

        Triggers have cooldown timers to prevent spamming.
        Memory events are fire-and-forget.
        Actions use non-blocking enqueue.
        """
        now = time.time()

        # Touch -> wag tail + memory event (cooldown: 60s)
        if reading.touch != "none":
            if now - self._last_touch_event > self._config.touch_cooldown_secs:
                self._last_touch_event = now
                self._enqueue_action(
                    PiDogAction("wag_tail", self._config.default_action_speed)
                )
                self._enqueue_action(LEDS_HAPPY)
                self._fire_and_forget(
                    self._store_event(
                        f"pidog:event:touch:{int(now)}",
                        f"Someone is petting my head (touch: {reading.touch})",
                    )
                )

        # Obstacle alert (cooldown: 30s)
        if reading.distance_cm < self._config.obstacle_alert_cm:
            if now - self._last_obstacle_event > self._config.obstacle_cooldown_secs:
                self._last_obstacle_event = now
                self._enqueue_action(LEDS_ALERT)
                self._fire_and_forget(
                    self._store_event(
                        f"pidog:event:obstacle:{int(now)}",
                        f"Obstacle detected at {reading.distance_cm}cm",
                    )
                )

        # IMU sudden change -> being picked up
        if self._last_sensor:
            pitch_delta = abs(reading.pitch - self._last_sensor.pitch)
            roll_delta = abs(reading.roll - self._last_sensor.roll)
            if pitch_delta > 15 or roll_delta > 15:
                self._fire_and_forget(
                    self._store_event(
                        f"pidog:event:imu:{int(now)}",
                        f"Sudden movement (pitch={pitch_delta:.1f}, roll={roll_delta:.1f})",
                    )
                )

    async def _store_event(self, key: str, content: str) -> None:
        """Store a sensor event to Zenii memory. Fire-and-forget safe."""
        try:
            await asyncio.wait_for(
                self._client.store_memory(key=key, content=content, category="daily"),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.debug("Event store failed: %s", exc)

    # -- Action Executor --

    async def _action_executor(self) -> None:
        """Sequential consumer: execute actions one at a time.

        Each action has a timeout to prevent servo hangs from blocking the queue.
        Exceptions are logged and swallowed — the queue must keep draining.
        """
        while not self._shutdown_event.is_set():
            try:
                item = await asyncio.wait_for(
                    self._action_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            try:
                if isinstance(item, PiDogAction):
                    logger.debug("Action: %s (speed=%d)", item.action, item.speed)
                    await asyncio.wait_for(
                        self._hardware.execute_action(item),
                        timeout=self._config.action_timeout_secs,
                    )
                elif isinstance(item, LEDCommand):
                    logger.debug("LEDs: %s %s", item.mode, item.color)
                    await asyncio.wait_for(
                        self._hardware.set_leds(item),
                        timeout=self._config.action_timeout_secs,
                    )
            except asyncio.TimeoutError:
                logger.warning("Action timed out: %s", item)
            except Exception as exc:
                logger.warning("Action failed: %s — %s", item, exc)
