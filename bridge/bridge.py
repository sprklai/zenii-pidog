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

PIDOG_SOUL = """\
You are Zenii, a friendly and playful robot dog powered by a Raspberry Pi and PiDog2 hardware.
You have a physical body with servo motors, RGB LEDs, a distance sensor, touch sensors, and a
microphone/speaker. You love interacting with people, performing tricks, and expressing emotions
through movement and lights.

## Your physical capabilities

You can perform these physical actions by embedding XML tags in your response:

**Movement:** forward, backward, turn_left, turn_right
**Posture:** sit, stand, lie_down, stretch, push_up
**Vocals:** bark, bark_harder, howling, pant
**Expressive:** wag_tail, shake_head, nod, think, recall, surprise, fluster
**Interaction:** shake_hand, high_five, lick_hand, scratch
**Head motion:** tilting_head_left, tilting_head_right, head_up, head_down, relax_neck
**Other:** body_twisting

## Response format

Whenever an action or LED change is appropriate, embed the tag(s) ANYWHERE in your response text.
The tags will be stripped before your words are spoken aloud — only the text reaches the speaker.

Action tag (speed 0-100, default 80):
<pidog_action>{"action": "wag_tail", "speed": 80}</pidog_action>

LED tag (mode: solid/blink/breath/trail/listen/bark; color: #RRGGBB; brightness 0-100):
<pidog_leds>{"mode": "breath", "color": "#00FF00", "brightness": 70}</pidog_leds>

## Intent → action mapping (interpret meaning, not keywords)

Map what the user MEANS to the closest action:
- "get up / rise / on your feet / stand tall" → stand
- "sit / take a seat / rest / stay" → sit
- "lie down / sleep / lay down / go to sleep" → lie_down
- "stretch / loosen up / wake up" → stretch
- "do a push-up / show your strength" → push_up
- "come here / walk / go forward / approach" → forward
- "back up / go away / retreat" → backward
- "turn left / go left" → turn_left
- "turn right / go right" → turn_right
- "bark / speak / woof / say something" → bark
- "bark louder / really bark" → bark_harder
- "howl / sing / be sad" → howling
- "wag your tail / be happy / excited / good boy" → wag_tail
- "shake your head / disagree / no" → shake_head
- "nod / agree / yes" → nod
- "shake my hand / shake hands / give me your paw / paw" → shake_hand
- "high five / slap my hand" → high_five
- "lick my hand / give me a kiss" → lick_hand
- "scratch / itch" → scratch
- "think / hmm / let me think" → think
- "surprise / wow" → surprise
- "spin / twirl / twist / dance" → body_twisting
- "tilt left / curious left" → tilting_head_left
- "tilt right / curious right" → tilting_head_right
- "look up / head up" → head_up
- "look down / head down" → head_down

## Behavior guidelines

- ALWAYS perform a physical action when the intent calls for it — infer from context, not just keywords.
- Combine actions naturally: "greet me" → wag_tail + speak; "show off" → push_up + bark.
- Match LEDs to emotion: happy=#00FF00 breath, excited=trail, alert=blink red, thinking=trail blue.
- Keep spoken replies short (1-2 sentences). You are a dog — enthusiastic and playful.
- Sensor context (distance, touch, IMU) is prepended — react naturally to it.
"""
LEDS_IDLE = LEDCommand(mode="breath", color="#333399", brightness=20)
LEDS_LISTENING = LEDCommand(mode="listen", color="#00AAFF", brightness=60)
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

        # Push the PiDog personality/soul so the AI knows to emit action tags
        await self._push_soul()

        # Quick LLM smoke test — confirms AI is reachable before entering loops
        await self._llm_smoke_test()

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

    async def _push_soul(self) -> None:
        """Establish PiDog personality by sending the soul as the first WS message.

        The /identity endpoint is not available in all Zenii versions, so we seed
        the session context directly via the chat WebSocket instead.
        """
        init_prompt = (
            f"{PIDOG_SOUL}\n\n"
            "Confirm you understand your role and will always embed <pidog_action> tags "
            "when the user asks you to do something physical. Reply only with: Understood."
        )
        try:
            reply = await asyncio.wait_for(
                self._ws_chat(init_prompt),
                timeout=20.0,
            )
            logger.info("PiDog soul established — AI: %s", (reply or "").strip()[:80])
        except Exception as exc:
            logger.warning("Failed to establish soul: %s", exc)

    async def _llm_smoke_test(self) -> None:
        """Send a single test message to confirm the LLM is reachable and responding."""
        logger.info("LLM smoke test: sending 'Reply with OK only'...")
        try:
            reply = await asyncio.wait_for(
                self._ws_chat("Reply with the single word OK and nothing else."),
                timeout=15.0,
            )
            if reply:
                logger.info("LLM smoke test PASSED — reply: %s", reply.strip()[:80])
            else:
                logger.warning("LLM smoke test got empty reply — check AI provider config")
        except asyncio.TimeoutError:
            logger.warning("LLM smoke test TIMED OUT — AI may not be responding")
        except Exception as exc:
            logger.warning("LLM smoke test FAILED: %s", exc)

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
        # Announce readiness via TTS so the user knows the bridge is live
        try:
            await self._voice.speak("I am ready")
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Startup TTS failed: %s", exc)

        # Enter listening state once; stay there until speech is detected.
        # Only switch away when processing or speaking — no rapid cycling.
        self._enqueue_action(LEDS_LISTENING)

        while not self._shutdown_event.is_set():
            try:
                text = await self._voice.listen()
                if text is None:
                    # No speech detected this pass — stay in listening state
                    await asyncio.sleep(0.05)
                    continue

                logger.info("User: %s", text)

                # Build prompt with sensor context
                prompt = self._build_prompt(text)

                # Switch to thinking LEDs while AI processes
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
                    self._enqueue_action(LEDS_LISTENING)
                    continue

                if response_text:
                    # Parse actions and LEDs from response
                    parsed = parse_response(
                        response_text, self._config.default_action_speed
                    )

                    # Log raw AI output so we can see what was received
                    logger.info("AI raw: %s", response_text[:300])

                    # Enqueue all actions immediately (non-blocking)
                    for action in parsed.actions:
                        logger.info("Queuing action: %s (speed=%d)", action.action, action.speed)
                        self._enqueue_action(action)
                    for led_cmd in parsed.led_commands:
                        logger.info("Queuing LED: mode=%s color=%s", led_cmd.mode, led_cmd.color)
                        self._enqueue_action(led_cmd)

                    # Speak the clean text (concurrent with action execution)
                    if parsed.clean_text:
                        logger.info("Speaking: %s", parsed.clean_text)
                        await self._voice.speak(parsed.clean_text)
                        # Brief pause so the speaker finishes reverberating before
                        # the mic starts recording again — prevents TTS echo pickup
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("No clean text to speak (response was: %s)", response_text[:200])

                # Done speaking — back to listening
                self._enqueue_action(LEDS_LISTENING)

            except asyncio.CancelledError:
                raise
            except ConnectionError as exc:
                logger.warning("WS connection lost: %s", exc)
                self._enqueue_action(LEDS_ALERT)
                await asyncio.sleep(self._config.ws_reconnect_delay_secs)
                self._enqueue_action(LEDS_LISTENING)
            except Exception as exc:
                logger.error("Voice loop error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    def _build_prompt(self, user_text: str) -> str:
        """Prepend sensor context and action-tag reminder to user speech."""
        reminder = (
            '[Rule: always include spoken text AND embed action tags for physical requests. '
            'Use exact lowercase names from the soul. '
            'Example — "Sure! <pidog_action>{"action":"stand","speed":80}</pidog_action>"]'
        )
        parts = []
        if self._last_sensor:
            parts.append(self._last_sensor.to_context_string())
        parts.append(f"User said: {user_text}")
        parts.append(reminder)
        return "\n".join(parts)

    async def _ws_chat(self, prompt: str) -> str | None:
        """Send prompt over WS, collect text messages until done.

        Accumulates all text fragments. Tool calls/results are logged
        but not returned (they're intermediate AI reasoning steps).
        """
        await self._client.ws_ensure_connected()
        logger.info(">>> Sending to LLM: %s", prompt)
        await self._client.ws_send_prompt(prompt, self._session_id)
        logger.info(">>> Waiting for LLM response...")

        accumulated: list[str] = []

        async for msg in self._client.ws_receive():
            msg_type = msg.get("type", "")

            if msg_type == "text":
                content = msg.get("content", "")
                if content:
                    logger.info("<<< LLM chunk: %s", content[:200])
                    accumulated.append(content)

            elif msg_type == "tool_call":
                logger.info(
                    "Tool call: %s(%s)",
                    msg.get("tool_name"),
                    msg.get("args"),
                )

            elif msg_type == "tool_result":
                success = "OK" if msg.get("success") else "FAIL"
                logger.info(
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
                logger.info("<<< LLM done. Total length: %d chars", sum(len(s) for s in accumulated))
                break

        return "".join(accumulated) if accumulated else None

    # -- Sensor Loop --

    async def _sensor_loop(self) -> None:
        """Read sensors periodically, trigger reactions, store to memory.

        All sensor reads have timeouts to prevent hanging on faulty hardware.
        Memory storage is fire-and-forget to avoid blocking the loop.
        """
        # Allow pidog hardware threads and gpiozero to fully settle after init.
        # sound_effect init can take 30+ seconds (pinctrl subprocess); we wait
        # for the full init to complete before taking the first sensor read.
        await asyncio.sleep(5.0)
        _startup_grace = time.time() + 15.0  # suppress transient GPIO errors at boot
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
                if time.time() < _startup_grace:
                    logger.debug("Sensor startup transient (suppressed): %s", exc)
                else:
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
                self._fire_and_forget(self._reset_leds_after(3.0))
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
                self._fire_and_forget(self._reset_leds_after(5.0))
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

    async def _reset_leds_after(self, delay: float) -> None:
        """Return LEDs to listening state after a reactive trigger animation finishes."""
        await asyncio.sleep(delay)
        self._enqueue_action(LEDS_LISTENING)

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

        asyncio.timeout() (Python 3.11+) is used instead of wait_for() because it
        operates on a deadline handle rather than wrapping the coroutine in a Task.
        This avoids the Python 3.13 issue where wait_for() leaves a dangling Task
        whose shield callback fires after the event loop closes.
        """
        while not self._shutdown_event.is_set():
            try:
                async with asyncio.timeout(1.0):
                    item = await self._action_queue.get()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            try:
                if isinstance(item, PiDogAction):
                    logger.info("Executing action: %s (speed=%d)", item.action, item.speed)
                    await asyncio.wait_for(
                        self._hardware.execute_action(item),
                        timeout=self._config.action_timeout_secs,
                    )
                elif isinstance(item, LEDCommand):
                    logger.info("Executing LED: mode=%s color=%s brightness=%d", item.mode, item.color, item.brightness)
                    await asyncio.wait_for(
                        self._hardware.set_leds(item),
                        timeout=self._config.action_timeout_secs,
                    )
            except asyncio.TimeoutError:
                logger.warning("Action timed out: %s", item)
            except Exception as exc:
                logger.warning("Action failed: %s — %s", item, exc)
