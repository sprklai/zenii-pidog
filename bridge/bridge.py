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
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

import aiohttp

from .action_parser import (
    ACTION_MAP,
    LEDCommand,
    PiDogAction,
    VALID_ACTIONS,
    VALID_LED_MODES,
    _ACTION_ALIASES,
    parse_response,
)
from .config import BridgeConfig
from .hardware import HardwareInterface, SensorReading, create_hardware
from .lcd import LCDDisplay
from .voice import VoiceInterface, create_voice
from .zenii_client import ZeniiClient

logger = logging.getLogger(__name__)

# Matches a complete <pidog_action> or <pidog_leds> tag — used for streaming dispatch.
_STREAM_TAG_RE = re.compile(
    r"<pidog_(action|leds)>(.*?)</pidog_(?:action|leds)>",
    re.DOTALL,
)
# Matches a sentence boundary (period/!/? followed by whitespace) for streaming TTS.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s")

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
        self._lcd: LCDDisplay | None = None
        if config.lcd_enabled:
            try:
                self._lcd = LCDDisplay(config.lcd_i2c_bus, config.lcd_i2c_address)
            except Exception as exc:
                logger.warning("LCD init failed (continuing without display): %s", exc)

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
        self._lcd_dots_task: asyncio.Task | None = None
        self._lcd_listening_active: bool = False

    # -- Lifecycle --

    async def start(self) -> None:
        """Full lifecycle: wait for daemon, parallel setup, run loops."""
        await self._client.start()

        logger.info("Waiting for Zenii daemon at %s ...", self._config.zenii_url)
        await self._wait_for_daemon()
        logger.info("Daemon is healthy — running parallel setup")

        # All four are independent HTTP/WS calls — run in parallel to minimize
        # startup latency (saves 2-4 sequential round-trips vs sequential calls).
        results = await asyncio.gather(
            self._resolve_session(),
            asyncio.wait_for(self._client.ws_connect(), timeout=10.0),
            self._configure_ai_provider(),
            self._ensure_soul(),
            return_exceptions=True,
        )

        session_result = results[0]
        if isinstance(session_result, BaseException):
            raise session_result
        self._session_id = session_result

        for label, result in zip(
            ("ws_connect", "configure_ai", "ensure_soul"), results[1:]
        ):
            if isinstance(result, Exception):
                logger.warning("Startup step %s failed: %s", label, result)

        # Set idle mood
        self._enqueue_action(LEDS_IDLE)

        logger.info("Bridge started — entering main loops")

        if self._lcd:
            try:
                await asyncio.to_thread(self._lcd.show, 1, "  Zenii PiDog  ")
                await asyncio.to_thread(self._lcd.show, 2, "   I'm ready!  ")
                await asyncio.sleep(2)
            except Exception as exc:
                logger.warning("LCD splash failed: %s", exc)

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

    async def _resolve_session(self) -> str:
        """Resume the last session from Zenii memory, or create a new one.

        Session continuity means the AI remembers previous conversations across
        bridge restarts — no need to re-establish context each time.
        """
        try:
            results = await asyncio.wait_for(
                self._client.recall_memory("pidog:session:latest", limit=1),
                timeout=5.0,
            )
            if results:
                data = json.loads(results[0].get("content", "{}"))
                sid = data.get("session_id")
                if sid:
                    sessions = await asyncio.wait_for(
                        self._client.get_sessions(), timeout=5.0
                    )
                    if any(s.get("id") == sid for s in sessions):
                        logger.info("Resuming session: %s", sid)
                        return sid
        except Exception as exc:
            logger.debug("Session resume check failed: %s", exc)

        session_id = await asyncio.wait_for(
            self._client.create_session(self._config.session_title),
            timeout=15.0,
        )
        try:
            await asyncio.wait_for(
                self._client.update_memory(
                    "pidog:session:latest",
                    json.dumps({"session_id": session_id}),
                    category="core",
                ),
                timeout=5.0,
            )
        except Exception:
            pass
        logger.info("New session: %s", session_id)
        return session_id

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

    async def _ensure_soul(self) -> None:
        """Upload PIDOG_SOUL to Zenii's persistent /identity/SOUL.md.

        Zenii auto-injects identity files into every session as a system prompt,
        so the soul persists across restarts without any LLM round-trip.
        Only writes when the content has changed (hash check), so subsequent
        startups are instant.
        """
        soul_hash = hashlib.sha256(PIDOG_SOUL.encode()).hexdigest()[:16]
        try:
            current = await asyncio.wait_for(
                self._client.get_identity("SOUL.md"),
                timeout=5.0,
            )
            # Embed the hash as a comment in the stored content so we can detect
            # stale uploads without a full string compare.
            if current and f"soul_hash:{soul_hash}" in current:
                logger.info("PiDog soul already current (hash=%s) — skipping upload", soul_hash)
                return

            content = f"<!-- soul_hash:{soul_hash} -->\n{PIDOG_SOUL}"
            await asyncio.wait_for(
                self._client.update_identity("SOUL.md", content),
                timeout=5.0,
            )
            await asyncio.wait_for(
                self._client.reload_identity(),
                timeout=5.0,
            )
            logger.info("PiDog soul uploaded to /identity/SOUL.md (hash=%s)", soul_hash)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                # /identity/ endpoint not available on this daemon version — skip silently.
                logger.debug("Soul upload skipped: /identity/ not supported by this daemon")
            else:
                logger.warning("Failed to upload soul to /identity/SOUL.md: %s", exc)
        except Exception as exc:
            logger.warning("Failed to upload soul to /identity/SOUL.md: %s", exc)

    async def _shutdown_watcher(self) -> None:
        """Wait for shutdown signal, then cancel all tasks."""
        await self._shutdown_event.wait()
        raise asyncio.CancelledError()

    async def _shutdown(self) -> None:
        """Graceful cleanup: sit, LEDs off, close connections."""
        logger.info("Shutting down bridge...")

        self._stop_lcd_listening()  # cancels dots + sensor rotation tasks

        # Cancel background tasks — snapshot first: done_callback (discard) fires
        # synchronously inside task.cancel() for already-finished tasks, which
        # would mutate the set mid-iteration and raise RuntimeError.
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Drain the action queue so Python 3.13 doesn't emit "RuntimeError: Event loop
        # is closed" from GC cleanup of any pending Queue.get waiters.
        while not self._action_queue.empty():
            try:
                self._action_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Show shutdown message on LCD
        if self._lcd:
            try:
                await asyncio.to_thread(self._lcd.show, 1, "  Shutting down ")
                await asyncio.to_thread(self._lcd.show, 2, "  Lying down... ")
            except Exception:
                pass

        # Sit before shutdown — direct call bypasses action queue so it always
        # executes even after asyncio loops are cancelled (mirrors official
        # SunFounder pattern: do_action('sit') + wait_all_done() + close()).
        try:
            await asyncio.wait_for(self._hardware.sit_and_stop(), timeout=5.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Shutdown sit timed out or failed: %s", exc)

        try:
            await asyncio.wait_for(
                self._hardware.set_leds(LEDS_OFF),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass

        if self._lcd:
            try:
                await asyncio.wait_for(asyncio.to_thread(self._lcd.close), timeout=2.0)
            except Exception:
                pass

        # Close voice first so the stop flag unblocks any in-flight recording thread,
        # then close hardware (pidog thread join can be slow — cap it).
        try:
            await asyncio.wait_for(self._voice.close(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Voice close timed out or failed: %s", exc)

        try:
            await asyncio.wait_for(self._hardware.close(), timeout=8.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Hardware close timed out or failed: %s", exc)

        try:
            await asyncio.wait_for(self._client.close(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Client close timed out or failed: %s", exc)

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
            logger.warning("Action queue full, dropping: %s", item)

    def _fire_and_forget(self, coro) -> None:
        """Spawn a background task with lifecycle tracking."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _start_lcd_listening(self) -> None:
        if self._lcd is None:
            return
        self._lcd_listening_active = True
        if self._lcd_dots_task and not self._lcd_dots_task.done():
            self._lcd_dots_task.cancel()
        self._lcd_dots_task = asyncio.create_task(self._lcd_listening_animation())

    def _stop_lcd_listening(self) -> None:
        self._lcd_listening_active = False
        if self._lcd_dots_task and not self._lcd_dots_task.done():
            self._lcd_dots_task.cancel()
        self._lcd_dots_task = None

    async def _lcd_listening_animation(self) -> None:
        patterns = ["Listening .   ", "Listening ..  ", "Listening ... "]
        i = 0
        try:
            while True:
                await asyncio.to_thread(self._lcd.show, 2, patterns[i % 3])
                i += 1
                await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            pass


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
        self._start_lcd_listening()

        while not self._shutdown_event.is_set():
            try:
                text = await self._voice.listen()
                if text is None:
                    # No speech detected this pass — stay in listening state
                    await asyncio.sleep(0.05)
                    continue

                logger.info("User: %s", text)
                self._stop_lcd_listening()

                if self._lcd:
                    self._fire_and_forget(
                        asyncio.to_thread(self._lcd.show, 1, (">" + text)[:16].ljust(16))
                    )
                    self._fire_and_forget(
                        asyncio.to_thread(self._lcd.show, 2, "Thinking...     ")
                    )

                # Build prompt with sensor + memory context
                prompt = await self._build_prompt(text)

                # Switch to thinking LEDs while AI processes
                self._enqueue_action(LEDS_THINKING)

                # Stream response: actions dispatched as tags arrive,
                # TTS starts on first complete sentence — not after full response.
                spoke = False
                stop_scroll = threading.Event()
                timed_out = False
                response_sentences: list[str] = []
                try:
                    async with asyncio.timeout(self._config.ws_chat_timeout_secs):
                        async for sentence in self._ws_chat_stream(prompt):
                            if not spoke:
                                # First sentence ready — start LCD scroll for full response
                                # (we don't have the full text yet, so scroll this sentence)
                                if self._lcd:
                                    stop_scroll = threading.Event()
                                    self._fire_and_forget(
                                        asyncio.to_thread(
                                            self._lcd.scroll, 2, sentence,
                                            self._config.lcd_scroll_delay_secs, stop_scroll,
                                        )
                                    )
                                spoke = True
                            response_sentences.append(sentence)
                            logger.info("Speaking: %s", sentence)
                            await self._voice.speak(sentence)
                    if response_sentences:
                        logger.info("<<< LLM: %s", " ".join(response_sentences))
                except TimeoutError:
                    timed_out = True
                    logger.warning(
                        "WS chat timed out after %.0fs", self._config.ws_chat_timeout_secs
                    )

                stop_scroll.set()

                # Scroll the full response on line 2 during the post-TTS linger
                # so the user can read it. For short text it shows statically.
                post_stop = threading.Event()
                if self._lcd and response_sentences:
                    full_text = " ".join(response_sentences)
                    self._fire_and_forget(
                        asyncio.to_thread(
                            self._lcd.scroll, 2, full_text,
                            self._config.lcd_scroll_delay_secs, post_stop,
                        )
                    )

                if timed_out:
                    post_stop.set()
                    if self._lcd:
                        self._fire_and_forget(asyncio.to_thread(self._lcd.show, 2, " " * 16))
                    self._enqueue_action(LEDS_ALERT)
                    await asyncio.sleep(1)
                    self._enqueue_action(LEDS_LISTENING)
                    self._start_lcd_listening()
                    continue

                if spoke:
                    await asyncio.sleep(self._config.echo_prevention_secs)
                    await asyncio.sleep(self._config.lcd_response_linger_secs)
                else:
                    logger.warning("LLM returned no text")

                post_stop.set()
                if self._lcd:
                    self._fire_and_forget(asyncio.to_thread(self._lcd.show, 2, " " * 16))

                # Done speaking — back to listening
                self._enqueue_action(LEDS_LISTENING)
                self._start_lcd_listening()

            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                # RuntimeError from voice.py means the STT circuit breaker tripped
                # (repeated 4xx from the cloud provider).  This is a configuration
                # problem the operator must fix — stop the bridge rather than loop.
                logger.error("Fatal STT error — stopping bridge: %s", exc)
                logger.error("Fix stt_api_key / stt_model in bridge_config.toml and restart")
                self.request_shutdown()
                return
            except ConnectionError as exc:
                logger.warning("WS connection lost: %s", exc)
                self._enqueue_action(LEDS_ALERT)
                await asyncio.sleep(self._config.ws_reconnect_delay_secs)
                self._enqueue_action(LEDS_LISTENING)
                self._start_lcd_listening()
            except Exception as exc:
                logger.error("Voice loop error: %s", exc, exc_info=True)
                await asyncio.sleep(1)
                self._start_lcd_listening()

    async def _build_prompt(self, user_text: str) -> str:
        """Build prompt: sensor snapshot + relevant memories + user speech.

        Memory recall uses Zenii's hybrid FTS5+vector search so the AI has
        relevant past context (events, observations) without manual tracking.
        The soul/action-tag rules live in SOUL.md and are injected by Zenii.
        """
        parts: list[str] = []

        if self._last_sensor:
            parts.append(self._last_sensor.to_context_string())

        # Recall relevant memories — fire concurrently with sensor context assembly.
        try:
            memories = await asyncio.wait_for(
                self._client.recall_memory(user_text, limit=3),
                timeout=2.0,
            )
            if memories:
                snippets = "; ".join(
                    m.get("content", "")[:120] for m in memories if m.get("content")
                )
                if snippets:
                    parts.append(f"[Relevant context: {snippets}]")
        except Exception:
            pass  # memory recall is best-effort — never block a response

        parts.append(f"User said: {user_text}")
        return "\n".join(parts)

    async def _ws_chat_stream(self, prompt: str) -> AsyncIterator[str]:
        """Send prompt, stream response as TTS-ready sentences.

        Dispatches <pidog_action> and <pidog_leds> tags **immediately** when
        their closing tag arrives in the stream — no waiting for the full
        response. Yields clean text as complete sentences so TTS can start
        speaking the first sentence while the LLM is still generating the rest.

        Latency profile vs old _ws_chat:
          Old: wait full LLM → parse → enqueue actions → speak
          New: first tag arrives (~0.3s) → action dispatched immediately
               first sentence arrives (~0.8s) → TTS starts
               remaining sentences yield as LLM streams
        """
        logger.info(">>> LLM prompt:\n%s", prompt)
        # ws_send_prompt handles reconnect internally under _ws_lock — no pre-call needed.
        await self._client.ws_send_prompt(prompt, self._session_id)

        raw_buf = ""        # raw LLM output (may contain partial action tags)
        text_buf = ""       # clean text waiting for a sentence boundary
        total_chars = 0

        def _dispatch_tag(m: re.Match) -> str:
            """Extract, validate, and enqueue one complete action/LED tag. Returns ""."""
            kind = m.group(1)
            body = m.group(2).strip()
            try:
                data = json.loads(body)
                if kind == "action":
                    name = (
                        data.get("action", "").lower().strip()
                        .replace(" ", "_").replace("-", "_")
                    )
                    name = _ACTION_ALIASES.get(name, name)
                    if name in VALID_ACTIONS:
                        pidog_name = ACTION_MAP.get(name, name)
                        speed = int(data.get("speed", self._config.default_action_speed))
                        self._enqueue_action(PiDogAction(action=pidog_name, speed=speed))
                        logger.info("Stream action: %s", pidog_name)
                    else:
                        logger.warning("Unknown stream action: %s", name)
                else:  # leds
                    mode = data.get("mode", "breath")
                    if mode in VALID_LED_MODES:
                        self._enqueue_action(LEDCommand(
                            mode=mode,
                            color=str(data.get("color", "#333399")),
                            brightness=int(data.get("brightness", 80)),
                        ))
                        logger.info("Stream LED: %s", mode)
            except Exception as exc:
                logger.warning("Stream tag parse error: %s — %r", exc, body[:80])
            return ""

        async for msg in self._client.ws_receive():
            msg_type = msg.get("type", "")

            if msg_type == "text":
                chunk = msg.get("content", "")
                if not chunk:
                    continue
                total_chars += len(chunk)
                raw_buf += chunk

                # Dispatch all complete tags in raw_buf, get back clean text.
                cleaned = _STREAM_TAG_RE.sub(_dispatch_tag, raw_buf)

                # If a partial opening tag is at the tail, hold it in raw_buf
                # so we don't accidentally yield it as text.
                p = cleaned.rfind("<pidog_")
                if p >= 0 and not _STREAM_TAG_RE.search(cleaned[p:]):
                    text_buf += cleaned[:p]
                    raw_buf = cleaned[p:]
                else:
                    text_buf += cleaned
                    raw_buf = ""

                # Yield complete sentences from text_buf.
                while True:
                    m = _SENTENCE_SPLIT_RE.search(text_buf)
                    if not m:
                        break
                    sentence = text_buf[: m.start() + 1].strip()
                    text_buf = text_buf[m.end():]
                    if sentence:
                        yield sentence

                # Fallback: if the buffer has grown large with no sentence boundary
                # (e.g., LLM emitting a long unpunctuated phrase), split at the
                # nearest clause boundary so TTS can start instead of waiting for
                # the full response.
                if len(text_buf) > 120:
                    m2 = re.search(r",\s+", text_buf)
                    split_at = m2.end() if m2 else len(text_buf)
                    sentence = text_buf[:split_at].strip()
                    text_buf = text_buf[split_at:]
                    if sentence:
                        yield sentence

            elif msg_type == "tool_call":
                logger.info("Tool call: %s(%s)", msg.get("tool_name"), msg.get("args"))

            elif msg_type == "tool_result":
                logger.info(
                    "Tool result: %s -> %s",
                    msg.get("tool_name"),
                    "OK" if msg.get("success") else "FAIL",
                )

            elif msg_type == "error":
                logger.error("Chat error: %s (hint: %s)", msg.get("error"), msg.get("hint"))
                return

            elif msg_type == "done":
                # Flush: dispatch any remaining tags and yield leftover text.
                remaining = _STREAM_TAG_RE.sub(_dispatch_tag, raw_buf)
                remaining = re.sub(r"</?pidog_\w+[^>]*>", "", remaining)
                final = (text_buf + remaining).strip()
                if final:
                    yield final
                logger.info("<<< LLM done (%d chars)", total_chars)
                return

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
                    # PUT upserts by key — no duplicate entries on repeated calls.
                    self._fire_and_forget(
                        self._client.update_memory(
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
                    # LEDs change near-instantly — use a short timeout so a hung
                    # RGB driver never stalls the queue for the full action window.
                    await asyncio.wait_for(
                        self._hardware.set_leds(item),
                        timeout=self._config.led_action_timeout_secs,
                    )
            except asyncio.TimeoutError:
                logger.warning("Action timed out: %s", item)
            except Exception as exc:
                logger.warning("Action failed: %s — %s", item, exc)
