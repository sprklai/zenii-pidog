"""Voice I/O providers: Local (Vosk+Piper), Cloud (direct REST), Text (fallback).

Provider selection via PIDOG_VOICE_PROVIDER env var:
  "local"   - Vosk STT + piper-tts (offline, RPi-optimized)
  "pipecat" - Cloud STT + TTS via direct provider REST APIs
  "text"    - stdin/stdout fallback (development, SSH)

STT providers (set via pipecat_stt_provider / PIPECAT_STT_PROVIDER):
  "groq"     - Groq Whisper (whisper-large-v3-turbo) — fastest, best accent support
  "deepgram" - Deepgram Nova-2 WebSocket streaming — low latency, built-in VAD
  "sarvam"   - Sarvam AI Saaras — optimised for Indian English (en-IN) + 10 Indian languages
  "azure"    - Azure Speech batch REST
  "google"   - Google Cloud Speech batch REST

Groq Whisper uses VAD-based recording (energy threshold) then a single POST to
api.groq.com/openai/v1/audio/transcriptions — no streaming required.
Sarvam AI uses VAD-based recording then the sarvamai SDK WebSocket (pip install sarvamai).
"""

from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import queue as _queue
import re
import shutil
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

import threading

import aiohttp

from .config import BridgeConfig

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<pidog_(?:action|leds)>.*?</pidog_(?:action|leds)>", re.DOTALL)


class _SttConfigError(Exception):
    """Raised when the STT circuit breaker trips on repeated 4xx responses."""


def strip_action_tags(text: str) -> str:
    """Remove all pidog XML tags from text before speaking."""
    return _TAG_RE.sub("", text).strip()


class VoiceInterface(ABC):
    """Abstract voice I/O provider."""

    @abstractmethod
    async def listen(self) -> str | None:
        """Block until speech detected, return transcribed text. None = no input."""

    @abstractmethod
    async def speak(self, text: str) -> None:
        """Convert text to speech and play through speaker."""

    @abstractmethod
    async def close(self) -> None:
        """Release audio resources."""


# ---------------------------------------------------------------------------
# Provider: Local (Vosk STT + piper-tts)
# ---------------------------------------------------------------------------


class LocalVoice(VoiceInterface):
    """Offline voice: Vosk STT from microphone + piper-tts subprocess to speaker.

    Requires: vosk, sounddevice, piper binary in PATH.
    Best for RPi4 where latency matters and no cloud dependency is wanted.
    """

    def __init__(self, config: BridgeConfig, executor: ThreadPoolExecutor) -> None:
        import sounddevice as sd  # type: ignore[import-untyped]
        from vosk import KaldiRecognizer, Model  # type: ignore[import-untyped]

        self._config = config
        self._executor = executor
        self._sample_rate = 16000

        logger.info("Loading Vosk STT model: %s", config.stt_model_path)
        self._model = Model(config.stt_model_path)
        self._recognizer = KaldiRecognizer(self._model, self._sample_rate)
        self._sd = sd
        self._speak_lock = asyncio.Lock()
        self._stop = threading.Event()

        logger.info("LocalVoice ready (Vosk STT + piper TTS)")

    def _listen_sync(self) -> str | None:
        """Record from microphone using energy VAD, then run Vosk recognition.

        Stops recording ~1.2s after speech ends instead of waiting out the full
        listen_timeout_secs window — saves 3-4s latency for short commands.
        """
        import json as _json
        import numpy as np

        if self._stop.is_set():
            return None

        self._recognizer.Reset()
        chunk_ms = 80
        chunk_frames = int(self._sample_rate * chunk_ms / 1000)
        rms_threshold = max(300, int(self._config.silence_threshold * 32767 * 0.5))
        wait_limit = int(self._config.listen_timeout_secs * 1000 / chunk_ms)
        silence_end_chunks = int(1200 / chunk_ms)  # 1.2s of silence ends utterance

        frames: list[bytes] = []
        speech_started = False
        silence_count = 0
        wait_count = 0

        logger.info("MIC listening (Vosk VAD), speak now...")

        try:
            with self._sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_frames,
            ) as stream:
                while not self._stop.is_set():
                    chunk, _ = stream.read(chunk_frames)
                    rms = int(np.sqrt(np.mean(chunk.astype("float32") ** 2)))

                    if not speech_started:
                        wait_count += 1
                        if rms > rms_threshold:
                            logger.info("MIC speech detected (RMS=%d)", rms)
                            speech_started = True
                            frames.append(chunk.tobytes())
                            silence_count = 0
                        elif wait_count >= wait_limit:
                            return None  # timeout — no speech
                    else:
                        frames.append(chunk.tobytes())
                        if rms < rms_threshold:
                            silence_count += 1
                            if silence_count >= silence_end_chunks:
                                break  # speech ended
                        else:
                            silence_count = 0
        except Exception as exc:
            logger.warning("Microphone read failed: %s", exc)
            return None

        if not frames:
            return None

        self._recognizer.AcceptWaveform(b"".join(frames))
        result = _json.loads(self._recognizer.FinalResult())
        text = result.get("text", "").strip()
        return text if text else None

    async def listen(self) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._listen_sync)

    async def speak(self, text: str) -> None:
        clean = strip_action_tags(text)
        if not clean:
            return

        async with self._speak_lock:
            piper = None
            aplay = None
            try:
                piper = await asyncio.create_subprocess_exec(
                    self._config.tts_binary,
                    "--model", self._config.tts_model,
                    "--output_raw",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                aplay = await asyncio.create_subprocess_exec(
                    "aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-q",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                # Feed text to piper
                piper.stdin.write(clean.encode("utf-8"))
                await piper.stdin.drain()
                piper.stdin.close()

                # Stream audio chunks piper → aplay as they are generated.
                # aplay starts playing after the first ~93ms chunk arrives,
                # instead of waiting for the full synthesis to complete.
                while True:
                    chunk = await asyncio.wait_for(
                        piper.stdout.read(4096), timeout=30.0
                    )
                    if not chunk:
                        break
                    aplay.stdin.write(chunk)
                    await aplay.stdin.drain()

                aplay.stdin.close()
                await asyncio.gather(
                    asyncio.wait_for(piper.wait(), timeout=5.0),
                    asyncio.wait_for(aplay.wait(), timeout=30.0),
                    return_exceptions=True,
                )
            except asyncio.TimeoutError:
                logger.warning("TTS timed out")
            except FileNotFoundError as exc:
                logger.warning("TTS binary not found: %s", exc)
            except Exception as exc:
                logger.warning("TTS failed: %s", exc)
            finally:
                for proc in (piper, aplay):
                    if proc and proc.returncode is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    async def close(self) -> None:
        self._stop.set()
        try:
            self._sd.stop()  # unblocks any in-progress sd.rec() immediately
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Provider: Cloud (direct REST to Deepgram/Cartesia/ElevenLabs/Azure/Google)
# ---------------------------------------------------------------------------

# TTS output sample rate — all supported providers return 22050Hz PCM
_TTS_SAMPLE_RATE = 22050


class CloudVoice(VoiceInterface):
    """Cloud STT/TTS via direct provider REST APIs using aiohttp.

    No extra packages required beyond the core aiohttp dependency.
    Supported STT providers: deepgram, azure, google
    Supported TTS providers: cartesia, elevenlabs, azure, google

    Provider and API keys are read from BridgeConfig (env vars or TOML).
    Audio capture/playback uses sounddevice + numpy.
    """

    # After this many consecutive STT 4xx responses the bridge raises a fatal
    # ConfigurationError so the operator sees a clear message instead of an
    # infinite retry loop.
    _STT_FAULT_LIMIT = 5

    def __init__(self, config: BridgeConfig, executor: ThreadPoolExecutor) -> None:
        self._config = config
        self._executor = executor
        self._speak_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None
        self._stt_fault_count = 0  # consecutive 4xx counter
        self._stop = threading.Event()

        if not config.pipecat_stt_api_key:
            raise RuntimeError(
                f"STT API key is empty (provider={config.pipecat_stt_provider}).\n"
                "  Set stt_api_key in bridge_config.toml  or  PIPECAT_STT_API_KEY env var."
            )
        if not config.pipecat_tts_api_key:
            raise RuntimeError(
                f"TTS API key is empty (provider={config.pipecat_tts_provider}).\n"
                "  Set tts_api_key in bridge_config.toml  or  PIPECAT_TTS_API_KEY env var."
            )

        logger.info(
            "CloudVoice ready (STT=%s, TTS=%s)",
            config.pipecat_stt_provider,
            config.pipecat_tts_provider,
        )

    def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # No total timeout — WebSocket streaming connections can be long-lived
            self._http = aiohttp.ClientSession()
        return self._http

    # -- STT --

    async def listen(self) -> str | None:
        """Listen for speech and return transcript. None = no speech detected."""
        provider = self._config.pipecat_stt_provider.lower()
        try:
            if provider == "groq":
                result = await self._stt_groq_batch()
            elif provider == "deepgram":
                result = await self._stt_deepgram_streaming()
            elif provider == "sarvam":
                result = await self._stt_sarvam_streaming()
            elif provider == "azure":
                result = await self._stt_azure_batch()
            elif provider == "google":
                result = await self._stt_google_batch()
            else:
                logger.warning("Unknown STT provider: %s", provider)
                return None
            self._stt_fault_count = 0
            return result
        except _SttConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        except asyncio.TimeoutError:
            logger.warning("STT timed out")
            return None
        except Exception as exc:
            logger.warning("STT failed: %s", exc)
            return None

    def _record_with_vad_sync(self) -> bytes | None:
        """Record audio using energy-based VAD: wait for speech, stop on silence.

        Returns raw int16 mono PCM or None if no speech heard within timeout.
        """
        try:
            import numpy as np
            import sounddevice as sd  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("sounddevice/numpy not installed — cannot capture audio")
            return None

        sample_rate = self._config.pipecat_sample_rate
        device = self._config.mic_device if self._config.mic_device >= 0 else None
        chunk_ms = 80
        chunk_frames = int(sample_rate * chunk_ms / 1000)
        # Use a lower threshold than the old batch approach — Indian accent speech
        # can have lower energy; 300 RMS is a safe floor for real speech.
        rms_threshold = max(300, int(self._config.silence_threshold * 32767 * 0.5))
        wait_limit = int(self._config.listen_timeout_secs * 1000 / chunk_ms)
        # Stop after 1.2s of silence following speech
        silence_end_chunks = int(1200 / chunk_ms)

        frames: list[bytes] = []
        speech_started = False
        silence_count = 0
        wait_count = 0

        logger.info("MIC listening (VAD threshold RMS=%d, device=%s), speak now...",
                    rms_threshold, device if device is not None else "default")

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=chunk_frames,
                device=device,
            ) as stream:
                while not self._stop.is_set():
                    chunk, _ = stream.read(chunk_frames)
                    rms = int(np.sqrt(np.mean(chunk.astype("float32") ** 2)))

                    if not speech_started:
                        wait_count += 1
                        if rms > rms_threshold:
                            logger.info("MIC speech detected (RMS=%d)", rms)
                            speech_started = True
                            frames.append(chunk.tobytes())
                            silence_count = 0
                        elif wait_count >= wait_limit:
                            return None  # timeout — no speech
                    else:
                        frames.append(chunk.tobytes())
                        if rms < rms_threshold:
                            silence_count += 1
                            if silence_count >= silence_end_chunks:
                                break  # speech ended
                        else:
                            silence_count = 0
        except Exception as exc:
            logger.warning("VAD recording failed: %s", exc)
            return None

        return b"".join(frames) if frames else None

    async def _stt_groq_batch(self) -> str | None:
        """Record with VAD then POST to Groq Whisper API.

        Model: whisper-large-v3-turbo (fast, strong accent support).
        Falls back to distil-whisper-large-v3-en if model config says so.
        """
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(self._executor, self._record_with_vad_sync)
        if not pcm:
            return None

        wav = self._pcm_to_wav(pcm, self._config.pipecat_sample_rate)
        model = self._config.pipecat_stt_model or "whisper-large-v3-turbo"

        data = aiohttp.FormData()
        data.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        data.add_field("model", model)
        data.add_field("language", "en")
        data.add_field("response_format", "json")

        headers = {"Authorization": f"Bearer {self._config.pipecat_stt_api_key}"}

        try:
            async with self._get_http().post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    self._stt_fault_count += 1
                    if self._stt_fault_count >= self._STT_FAULT_LIMIT:
                        raise _SttConfigError("Groq API key invalid — check stt_api_key in bridge_config.toml")
                    logger.warning("Groq STT auth error (check API key)")
                    return None
                resp.raise_for_status()
                result = await resp.json()
                text = result.get("text", "").strip()
                if text:
                    logger.info("STT FINAL (Groq): %s", text)
                return text or None
        except _SttConfigError:
            raise
        except Exception as exc:
            logger.warning("Groq STT failed: %s", exc)
            return None

    async def _stt_deepgram_streaming(self) -> str | None:
        """Stream microphone audio to Deepgram via WebSocket.

        Uses Deepgram's live streaming API (same approach as Pipecat):
        - Sends 20ms audio chunks over persistent WebSocket
        - Deepgram's VAD detects end of utterance (speech_final=true)
        - Returns transcript when user finishes speaking
        - No fixed recording window — responds immediately when speech ends
        """
        try:
            import sounddevice as sd  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("sounddevice not installed — cannot capture audio")
            return None

        sample_rate = self._config.pipecat_sample_rate
        device = self._config.mic_device if self._config.mic_device >= 0 else None
        chunk_ms = 20
        chunk_frames = int(sample_rate * chunk_ms / 1000)  # 320 frames @ 16kHz

        # Thread-safe queue: sounddevice callback (C thread) → asyncio coroutine
        audio_q: _queue.SimpleQueue[bytes | None] = _queue.SimpleQueue()

        def _on_audio(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                logger.debug("Audio callback: %s", status)
            audio_q.put(indata.tobytes())

        _FALLBACK_MODEL = "nova-2"
        effective_model = self._config.pipecat_stt_model or _FALLBACK_MODEL

        def _build_ws_url(model: str) -> str:
            qs = "&".join([
                f"model={model}",
                "language=en",
                "encoding=linear16",
                f"sample_rate={sample_rate}",
                "channels=1",
                f"endpointing={self._config.deepgram_endpointing_ms}",
                f"utterance_end_ms={self._config.deepgram_utterance_end_ms}",
                "vad_events=true",
                "smart_format=true",
                "interim_results=true",
            ])
            return f"wss://api.deepgram.com/v1/listen?{qs}"

        ws_url = _build_ws_url(effective_model)
        headers = {"Authorization": f"Token {self._config.pipecat_stt_api_key.strip()}"}

        logger.info("Connecting to Deepgram (model=%s, device=%s)",
                    effective_model, device if device is not None else "default")

        transcript = ""

        try:
            async with self._get_http().ws_connect(
                ws_url, headers=headers, heartbeat=20, receive_timeout=60.0
            ) as ws:
                logger.info("MIC streaming to Deepgram, speak now...")

                with sd.InputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=chunk_frames,
                    callback=_on_audio,
                    device=device,
                ):
                    async def _send_audio() -> None:
                        loop = asyncio.get_running_loop()
                        while True:
                            # Run blocking queue.get in thread pool to avoid busy-loop
                            chunk = await loop.run_in_executor(
                                self._executor, audio_q.get
                            )
                            if chunk is None:
                                break
                            try:
                                await ws.send_bytes(chunk)
                            except Exception:
                                break

                    async def _recv_transcript() -> str:
                        nonlocal transcript
                        # Timeout is measured from speech START (SpeechStarted event),
                        # not connection open — prevents the race where the user speaks
                        # near the end of the window and speech_final arrives after cancel.
                        async with asyncio.timeout(self._config.listen_timeout_secs) as deadline:
                            async for msg in ws:
                                if msg.type != aiohttp.WSMsgType.TEXT:
                                    break
                                data = _json.loads(msg.data)
                                t = data.get("type", "")

                                if t == "SpeechStarted":
                                    logger.info("MIC speech started")
                                    deadline.reschedule(
                                        asyncio.get_running_loop().time()
                                        + self._config.listen_timeout_secs
                                    )

                                elif t == "Results":
                                    alts = (data.get("channel", {})
                                            .get("alternatives", [{}]))
                                    text = alts[0].get("transcript", "").strip() if alts else ""
                                    is_final = data.get("is_final", False)
                                    speech_final = data.get("speech_final", False)

                                    if text:
                                        logger.info(
                                            "STT %s: %s",
                                            "FINAL" if is_final else "interim",
                                            text,
                                        )
                                    if is_final and text:
                                        # Accumulate: multi-sentence commands produce multiple
                                        # is_final results before speech_final arrives.
                                        transcript = (transcript + " " + text).strip() if transcript else text
                                    if speech_final and text:
                                        return transcript

                                elif t == "UtteranceEnd":
                                    if transcript:
                                        return transcript

                                elif t == "Error":
                                    msg_text = data.get("message", "")
                                    logger.error("Deepgram error: %s", msg_text)
                                    if data.get("variant") in ("TOKEN_LIMIT_REACHED", "INVALID_AUTH"):
                                        self._stt_fault_count += 1
                                        if self._stt_fault_count >= self._STT_FAULT_LIMIT:
                                            raise _SttConfigError(
                                                f"Deepgram auth/quota error: {msg_text}"
                                            )
                                    break
                        return transcript

                    send_task = asyncio.create_task(_send_audio())
                    try:
                        await _recv_transcript()  # timeout managed internally
                    except asyncio.TimeoutError:
                        pass  # no speech in timeout window — normal, return None
                    finally:
                        audio_q.put(None)   # stop _send_audio
                        send_task.cancel()
                        try:
                            await send_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        try:
                            await ws.send_str('{"type":"CloseStream"}')
                        except Exception:
                            pass

        except _SttConfigError:
            raise
        except aiohttp.WSServerHandshakeError as exc:
            status = getattr(exc, "status", None) or 0
            if 400 <= status < 500:
                dg_reason = getattr(exc, "message", "") or str(exc)
                # If the failure is from an explicitly-configured non-default model,
                # retry immediately with nova-2 before counting as a fault.
                # Covers the common case of a plan-restricted model (e.g. nova-3).
                if effective_model != _FALLBACK_MODEL:
                    logger.warning(
                        "Deepgram rejected model '%s' (HTTP %d) — permanently switching to %s "
                        "for this session. Fix: remove stt_model from bridge_config.toml "
                        "[voice.pipecat]. Deepgram says: %s",
                        effective_model, status, _FALLBACK_MODEL, dg_reason,
                    )
                    # Permanently update — no restore on success, so every subsequent
                    # listen() call goes directly to nova-2 without retrying the rejected model.
                    self._config.pipecat_stt_model = _FALLBACK_MODEL
                    try:
                        return await self._stt_deepgram_streaming()
                    except _SttConfigError:
                        raise
                    except Exception as fb_exc:
                        logger.warning("Deepgram fallback (%s) also failed: %s", _FALLBACK_MODEL, fb_exc)
                    return None

                self._stt_fault_count += 1
                if self._stt_fault_count >= self._STT_FAULT_LIMIT:
                    raise _SttConfigError(
                        f"Deepgram WebSocket rejected (HTTP {status}) after "
                        f"{self._stt_fault_count} attempts — "
                        f"check stt_api_key and model '{effective_model}' in bridge_config.toml. "
                        f"Deepgram says: {dg_reason}"
                    )
                logger.warning(
                    "Deepgram WS rejected (HTTP %d, attempt %d/%d, model=%s). "
                    "Check stt_api_key in bridge_config.toml. Deepgram says: %s",
                    status, self._stt_fault_count, self._STT_FAULT_LIMIT, effective_model, dg_reason,
                )
                await asyncio.sleep(min(self._stt_fault_count * 2.0, 10.0))
            else:
                logger.warning("Deepgram streaming failed: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Deepgram streaming failed: %s", exc)
            return None

        return transcript if transcript else None

    @staticmethod
    def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
        """Wrap raw int16 mono PCM in a minimal WAV container (used by Azure/Google)."""
        import struct
        bits_per_sample = 16
        num_channels = 1
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm_bytes)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE", b"fmt ",
            16, 1, num_channels, sample_rate,
            byte_rate, block_align, bits_per_sample,
            b"data", data_size,
        )
        return header + pcm_bytes

    def _record_audio_sync(self) -> bytes | None:
        """Batch-record fixed-duration PCM (used by Azure/Google batch STT)."""
        try:
            import numpy as np
            import sounddevice as sd  # type: ignore[import-untyped]
            device = self._config.mic_device if self._config.mic_device >= 0 else None
            frames = int(self._config.pipecat_sample_rate * self._config.listen_timeout_secs)
            audio = sd.rec(frames, samplerate=self._config.pipecat_sample_rate,
                           channels=1, dtype="int16", device=device, blocking=True)
            rms = int(np.sqrt(np.mean(audio.astype("float32") ** 2)))
            threshold = int(self._config.silence_threshold * 32767)
            if rms < threshold:
                return None
            return audio.tobytes()
        except Exception as exc:
            logger.warning("Microphone read failed: %s", exc)
            return None

    async def _stt_sarvam_streaming(self) -> str | None:
        """Live-stream mic audio to Sarvam AI Saaras via WebSocket, return transcript.

        Architecture mirrors Deepgram streaming:
          sounddevice callback → audio_q → _send_audio (chunks every 500ms or on silence)
          Sarvam WS responses → _recv_transcript (collects until is_final or timeout)

        Uses sarvamai SDK (pip install sarvamai).
        Config:
          pipecat_stt_api_key  — Sarvam AI subscription key
          pipecat_stt_model    — required: "saaras:v3" or "saarika:v2.5"
          sarvam_language_code — default "en-IN"  (also accepts hi-IN, ta-IN, etc.)
        """
        try:
            from sarvamai import AsyncSarvamAI  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("sarvamai not installed — run: pip install sarvamai")
            return None

        try:
            import numpy as np
            import sounddevice as sd  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("sounddevice/numpy not installed — cannot capture audio")
            return None

        sample_rate = self._config.pipecat_sample_rate
        device = self._config.mic_device if self._config.mic_device >= 0 else None

        # Sounddevice sends 20ms sub-chunks via callback; we accumulate them into
        # 500ms WAV chunks before sending to Sarvam (gives the model enough context
        # per call while keeping end-of-speech latency low).
        sub_ms = 20
        sub_frames = int(sample_rate * sub_ms / 1000)
        chunk_ms = 500
        subs_per_chunk = chunk_ms // sub_ms   # 25 sub-chunks = 500ms

        audio_q: _queue.SimpleQueue[bytes | None] = _queue.SimpleQueue()

        def _on_audio(indata, frames, time_info, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                logger.debug("Sarvam audio callback: %s", status)
            audio_q.put(indata.tobytes())

        model = self._config.pipecat_stt_model
        if not model:
            logger.error(
                "Sarvam STT: stt_model not set in bridge_config.toml. "
                "Add: stt_model = \"saaras:v3\"  (or saarika:v2.5) under [voice.pipecat]"
            )
            return None
        language = self._config.sarvam_language_code

        logger.info("Connecting to Sarvam AI (model=%s, lang=%s, device=%s)",
                    model, language, device if device is not None else "default")

        transcript = ""

        try:
            client = AsyncSarvamAI(
                api_subscription_key=self._config.pipecat_stt_api_key.strip()
            )
            async with client.speech_to_text_streaming.connect(
                model=model,
                mode="transcribe",
                language_code=language,
                high_vad_sensitivity=True,
            ) as ws:
                logger.info("MIC streaming to Sarvam AI, speak now...")

                with sd.InputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=sub_frames,
                    callback=_on_audio,
                    device=device,
                ):
                    async def _send_audio() -> None:
                        """Read mic chunks, run energy VAD, send 500ms WAV slices to Sarvam."""
                        loop = asyncio.get_running_loop()
                        rms_threshold = max(
                            300, int(self._config.silence_threshold * 32767 * 0.5)
                        )
                        wait_limit = int(
                            self._config.listen_timeout_secs * 1000 / sub_ms
                        )
                        # Stop sending after 1.2s of post-speech silence
                        silence_end_subs = int(1200 / sub_ms)
                        logger.info("MIC listening (VAD threshold RMS=%d, timeout=%.1fs)",
                                    rms_threshold, self._config.listen_timeout_secs)

                        pending: list[bytes] = []   # sub-chunks not yet sent
                        speech_started = False
                        silence_count = 0
                        wait_count = 0

                        while True:
                            raw = await loop.run_in_executor(self._executor, audio_q.get)
                            if raw is None:
                                break

                            arr = np.frombuffer(raw, dtype="int16").astype("float32")
                            rms = int(np.sqrt(np.mean(arr ** 2)))

                            if not speech_started:
                                wait_count += 1
                                if rms > rms_threshold:
                                    logger.info("MIC speech detected (RMS=%d)", rms)
                                    speech_started = True
                                    pending.append(raw)
                                    silence_count = 0
                                elif wait_count >= wait_limit:
                                    break  # no speech within timeout
                            else:
                                pending.append(raw)
                                if rms < rms_threshold:
                                    silence_count += 1
                                else:
                                    silence_count = 0

                                # Flush every 500ms OR immediately when speech ends
                                speech_ended = silence_count >= silence_end_subs
                                if len(pending) >= subs_per_chunk or speech_ended:
                                    pcm = b"".join(pending)
                                    pending = []
                                    wav = self._pcm_to_wav(pcm, sample_rate)
                                    audio_b64 = base64.b64encode(wav).decode("utf-8")
                                    try:
                                        await ws.transcribe(audio=audio_b64)
                                    except Exception as exc:
                                        logger.debug("Sarvam send error: %s", exc)
                                        break
                                    if speech_ended:
                                        break

                        # Flush any leftover sub-chunks after loop exits
                        if pending:
                            pcm = b"".join(pending)
                            wav = self._pcm_to_wav(pcm, sample_rate)
                            audio_b64 = base64.b64encode(wav).decode("utf-8")
                            try:
                                await ws.transcribe(audio=audio_b64)
                            except Exception:
                                pass

                    async def _recv_transcript() -> str:
                        """Collect Sarvam responses until is_final or timeout."""
                        nonlocal transcript
                        async with asyncio.timeout(
                            self._config.listen_timeout_secs
                        ) as deadline:
                            while True:
                                response = await ws.recv()

                                # Extract text — try every field name Sarvam SDK may use
                                if hasattr(response, "transcript"):
                                    text = response.transcript or ""
                                elif hasattr(response, "text"):
                                    text = response.text or ""
                                elif isinstance(response, dict):
                                    text = (
                                        response.get("transcript")
                                        or response.get("text", "")
                                    )
                                else:
                                    text = ""
                                text = text.strip()

                                # Detect final vs interim result
                                is_final = (
                                    getattr(response, "is_final", False)
                                    or (isinstance(response, dict)
                                        and response.get("is_final", False))
                                )

                                if text:
                                    logger.info(
                                        "STT %s (Sarvam): %s",
                                        "FINAL" if is_final else "interim",
                                        text,
                                    )
                                    # Reschedule timeout from when we first hear text
                                    if not transcript:
                                        deadline.reschedule(
                                            asyncio.get_running_loop().time()
                                            + self._config.listen_timeout_secs
                                        )
                                    transcript = text  # keep latest (covers interim updates)

                                if is_final:
                                    return transcript

                        return transcript  # timeout fallback — return best interim

                    send_task = asyncio.create_task(_send_audio())
                    try:
                        await _recv_transcript()
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        audio_q.put(None)   # unblock _send_audio queue.get
                        send_task.cancel()
                        try:
                            await send_task
                        except (asyncio.CancelledError, Exception):
                            pass

        except Exception as exc:
            logger.warning("Sarvam STT failed: %s", exc)
            return None

        return transcript if transcript else None

    async def _stt_azure_batch(self) -> str | None:
        """Batch POST to Azure Speech-to-Text (fallback for Azure provider)."""
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(self._executor, self._record_audio_sync)
        if not audio_bytes:
            return None
        region = self._config.pipecat_stt_model or "eastus"
        url = (f"https://{region}.stt.speech.microsoft.com"
               "/speech/recognition/conversation/cognitiveservices/v1?language=en-US")
        headers = {
            "Ocp-Apim-Subscription-Key": self._config.pipecat_stt_api_key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        }
        wav = self._pcm_to_wav(audio_bytes, self._config.pipecat_sample_rate)
        async with self._get_http().post(url, headers=headers, data=wav) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("RecognitionStatus") == "Success":
                return data.get("DisplayText", "").strip() or None
            return None

    async def _stt_google_batch(self) -> str | None:
        """Batch POST to Google Cloud Speech-to-Text (fallback for Google provider)."""
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(self._executor, self._record_audio_sync)
        if not audio_bytes:
            return None
        body = {
            "config": {
                "encoding": "LINEAR16",
                "sampleRateHertz": self._config.pipecat_sample_rate,
                "languageCode": "en-US",
            },
            "audio": {"content": base64.b64encode(audio_bytes).decode()},
        }
        async with self._get_http().post(
            f"https://speech.googleapis.com/v1/speech:recognize"
            f"?key={self._config.pipecat_stt_api_key}",
            json=body,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            results = data.get("results", [])
            if results:
                return results[0]["alternatives"][0].get("transcript", "").strip() or None
            return None

    # -- TTS --

    async def speak(self, text: str) -> None:
        clean = strip_action_tags(text)
        if not clean:
            return

        async with self._speak_lock:
            provider = self._config.pipecat_tts_provider.lower()
            try:
                if provider == "cartesia":
                    # Streaming path: first audio chunk starts playing ~50-100ms
                    # after the request is sent instead of waiting for full download.
                    await self._tts_cartesia_streaming(clean)
                    return

                if provider == "elevenlabs":
                    audio_bytes = await self._tts_elevenlabs(clean)
                elif provider == "azure":
                    audio_bytes = await self._tts_azure(clean)
                elif provider == "google":
                    audio_bytes = await self._tts_google(clean)
                else:
                    logger.warning("Unknown TTS provider: %s", provider)
                    return

                if audio_bytes:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._executor, self._play_audio_sync, audio_bytes
                    )
            except asyncio.TimeoutError:
                logger.warning("Cloud TTS timed out")
            except Exception as exc:
                logger.warning("Cloud TTS failed: %s", exc)

    async def _tts_cartesia_streaming(self, text: str) -> None:
        """Stream Cartesia TTS via SSE, piping audio chunks to sd.OutputStream.

        First audio starts playing ~50-100ms after the request is sent.
        Falls back to the batch bytes endpoint on any stream error.
        """
        import base64
        import queue as _q
        import numpy as np
        import sounddevice as sd

        body = {
            "transcript": text,
            "model_id": self._config.pipecat_tts_model or "sonic-english",
            "voice": {
                "mode": "id",
                "id": self._config.pipecat_tts_voice or "a0e99841-438c-4a64-b679-ae501e7d6091",
            },
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": _TTS_SAMPLE_RATE,
            },
        }
        headers = {
            "X-API-Key": self._config.pipecat_tts_api_key,
            "Cartesia-Version": "2024-06-10",
            "Content-Type": "application/json",
        }

        # Thread-safe queue bridges async SSE reader and sync sd.OutputStream writer.
        audio_q: _q.SimpleQueue[bytes | None] = _q.SimpleQueue()

        def _play_stream() -> None:
            with sd.OutputStream(
                samplerate=_TTS_SAMPLE_RATE, channels=1, dtype="int16"
            ) as stream:
                while True:
                    chunk = audio_q.get()
                    if chunk is None:
                        break
                    stream.write(np.frombuffer(chunk, dtype="int16"))

        loop = asyncio.get_running_loop()
        play_future = loop.run_in_executor(self._executor, _play_stream)

        try:
            async with self._get_http().post(
                "https://api.cartesia.ai/tts/sse",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode(errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if not payload:
                        continue
                    try:
                        data = _json.loads(payload)
                    except Exception:
                        continue
                    if data.get("done"):
                        break
                    audio_b64 = data.get("data")
                    if audio_b64:
                        audio_q.put(base64.b64decode(audio_b64))
        except Exception as exc:
            logger.warning("Cartesia SSE stream error: %s — falling back to bytes endpoint", exc)
            # Drain the play worker before retrying with batch
            audio_q.put(None)
            await play_future
            audio_bytes = await self._tts_cartesia(text)
            if audio_bytes:
                await loop.run_in_executor(self._executor, self._play_audio_sync, audio_bytes)
            return
        finally:
            audio_q.put(None)   # signal end of stream to play worker
            await play_future   # wait for all queued audio to finish playing

    async def _tts_cartesia(self, text: str) -> bytes | None:
        """POST to Cartesia TTS bytes endpoint → raw PCM s16le at 22050Hz."""
        body = {
            "transcript": text,
            "model_id": self._config.pipecat_tts_model or "sonic-english",
            "voice": {
                "mode": "id",
                "id": self._config.pipecat_tts_voice or "a0e99841-438c-4a64-b679-ae501e7d6091",
            },
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": _TTS_SAMPLE_RATE,
            },
        }
        headers = {
            "X-API-Key": self._config.pipecat_tts_api_key,
            "Cartesia-Version": "2024-06-10",
            "Content-Type": "application/json",
        }
        async with self._get_http().post(
            "https://api.cartesia.ai/tts/bytes",
            json=body,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _tts_elevenlabs(self, text: str) -> bytes | None:
        """POST to ElevenLabs TTS → raw PCM at 22050Hz."""
        voice_id = self._config.pipecat_tts_voice or "21m00Tcm4TlvDq8ikWAM"
        model_id = self._config.pipecat_tts_model or "eleven_monolingual_v1"
        headers = {
            "xi-api-key": self._config.pipecat_tts_api_key,
            "Content-Type": "application/json",
        }
        async with self._get_http().post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            "?output_format=pcm_22050",
            json={"text": text, "model_id": model_id},
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _tts_azure(self, text: str) -> bytes | None:
        """POST SSML to Azure TTS → raw PCM 22050Hz mono.

        pipecat_tts_model is used as the Azure region (e.g. 'eastus').
        pipecat_tts_voice is the neural voice name (e.g. 'en-US-JennyNeural').
        """
        region = self._config.pipecat_tts_model or "eastus"
        voice = self._config.pipecat_tts_voice or "en-US-JennyNeural"
        ssml = (
            f'<speak version="1.0" xml:lang="en-US">'
            f'<voice name="{voice}">{text}</voice>'
            f"</speak>"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self._config.pipecat_tts_api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "raw-22050hz-16bit-mono-pcm",
        }
        async with self._get_http().post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers=headers,
            data=ssml.encode(),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _tts_google(self, text: str) -> bytes | None:
        """POST to Google Cloud TTS → base64-decoded LINEAR16 PCM at 22050Hz."""
        voice_name = self._config.pipecat_tts_voice or "en-US-Standard-C"
        body = {
            "input": {"text": text},
            "voice": {"languageCode": "en-US", "name": voice_name},
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": _TTS_SAMPLE_RATE,
            },
        }
        async with self._get_http().post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize"
            f"?key={self._config.pipecat_tts_api_key}",
            json=body,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            b64 = data.get("audioContent", "")
            return base64.b64decode(b64) if b64 else None

    # -- Playback --

    def _play_audio_sync(self, audio_bytes: bytes) -> None:
        """Play raw s16le PCM through speaker (blocking, runs in thread pool)."""
        try:
            import numpy as np
            import sounddevice as sd  # type: ignore[import-untyped]

            audio_array = np.frombuffer(audio_bytes, dtype="int16")
            sd.play(audio_array, samplerate=_TTS_SAMPLE_RATE, blocking=True)
        except Exception as exc:
            logger.warning("Audio playback failed: %s", exc)

    async def close(self) -> None:
        self._stop.set()
        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None


# ---------------------------------------------------------------------------
# Provider: Text (stdin/stdout fallback)
# ---------------------------------------------------------------------------


class TextVoice(VoiceInterface):
    """Text mode: reads from stdin, prints to stdout.

    Used for development, SSH sessions, or when no audio hardware is available.
    """

    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor
        logger.info("TextVoice ready (stdin/stdout)")

    async def listen(self) -> str | None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(self._executor, self._read_input)
            if text is None:
                return None
            text = text.strip()
            return text if text else None
        except (EOFError, KeyboardInterrupt):
            return None

    @staticmethod
    def _read_input() -> str | None:
        try:
            return input("You> ")
        except EOFError:
            return None

    async def speak(self, text: str) -> None:
        clean = strip_action_tags(text)
        if clean:
            print(f"Buddy> {clean}")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_voice(
    config: BridgeConfig, executor: ThreadPoolExecutor
) -> VoiceInterface:
    """Create voice provider based on config.voice_provider.

    Falls back to TextVoice if the requested provider can't initialize.
    """
    provider = config.voice_provider.lower()

    if provider == "text":
        return TextVoice(executor)

    if provider == "pipecat":
        if not config.pipecat_stt_api_key or not config.pipecat_tts_api_key:
            logger.warning(
                "Cloud voice API keys not configured — falling back to text mode. "
                "Set PIPECAT_STT_API_KEY and PIPECAT_TTS_API_KEY."
            )
            return TextVoice(executor)
        try:
            return CloudVoice(config, executor)
        except Exception as exc:
            logger.warning("CloudVoice init failed (%s) — falling back to text", exc)
            return TextVoice(executor)

    if provider == "local":
        if config.simulate_hardware:
            logger.info("Simulation mode — using text voice")
            return TextVoice(executor)

        if not config.stt_model_path:
            logger.info("No STT model path configured — using text voice")
            return TextVoice(executor)

        if not shutil.which(config.tts_binary):
            logger.warning(
                "TTS binary '%s' not found — using text voice", config.tts_binary
            )
            return TextVoice(executor)

        try:
            return LocalVoice(config, executor)
        except ImportError as exc:
            logger.warning(
                "Voice deps not installed (%s) — using text voice", exc
            )
            return TextVoice(executor)
        except Exception as exc:
            logger.warning("LocalVoice init failed (%s) — using text voice", exc)
            return TextVoice(executor)

    logger.warning("Unknown voice provider '%s' — using text voice", provider)
    return TextVoice(executor)
