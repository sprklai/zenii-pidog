"""Voice I/O providers: Local (Vosk+Piper), Cloud (direct REST), Text (fallback).

Provider selection via PIDOG_VOICE_PROVIDER env var:
  "local"   - Vosk STT + piper-tts (offline, RPi-optimized)
  "pipecat" - Cloud STT/TTS via direct provider REST APIs (no extra packages)
  "text"    - stdin/stdout fallback (development, SSH)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

import aiohttp

from .config import BridgeConfig

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<pidog_(?:action|leds)>.*?</pidog_(?:action|leds)>", re.DOTALL)


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

        logger.info("LocalVoice ready (Vosk STT + piper TTS)")

    def _listen_sync(self) -> str | None:
        """Record from microphone and run Vosk recognition (blocking)."""
        import json as _json

        self._recognizer.Reset()
        frames = int(self._sample_rate * self._config.listen_timeout_secs)

        try:
            audio = self._sd.rec(
                frames,
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocking=True,
            )
        except Exception as exc:
            logger.warning("Microphone read failed: %s", exc)
            return None

        self._recognizer.AcceptWaveform(audio.tobytes())
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
            try:
                piper = await asyncio.create_subprocess_exec(
                    self._config.tts_binary,
                    "--model", self._config.tts_model,
                    "--output_raw",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                piper_out, _ = await asyncio.wait_for(
                    piper.communicate(clean.encode("utf-8")),
                    timeout=30.0,
                )

                if piper_out:
                    aplay = await asyncio.create_subprocess_exec(
                        "aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-q",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(
                        aplay.communicate(piper_out),
                        timeout=30.0,
                    )
            except asyncio.TimeoutError:
                logger.warning("TTS timed out")
            except FileNotFoundError as exc:
                logger.warning("TTS binary not found: %s", exc)
            except Exception as exc:
                logger.warning("TTS failed: %s", exc)

    async def close(self) -> None:
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

    def __init__(self, config: BridgeConfig, executor: ThreadPoolExecutor) -> None:
        self._config = config
        self._executor = executor
        self._speak_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None
        logger.info(
            "CloudVoice ready (STT=%s, TTS=%s)",
            config.pipecat_stt_provider,
            config.pipecat_tts_provider,
        )

    def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._http

    # -- Recording --

    def _record_audio_sync(self) -> bytes | None:
        """Record raw PCM (int16, mono) from microphone (blocking, runs in thread pool)."""
        try:
            import numpy as np
            import sounddevice as sd  # type: ignore[import-untyped]

            frames = int(
                self._config.pipecat_sample_rate * self._config.listen_timeout_secs
            )
            audio = sd.rec(
                frames,
                samplerate=self._config.pipecat_sample_rate,
                channels=1,
                dtype="int16",
                blocking=True,
            )
            # Discard silent recordings (RMS < 100 out of 32768) to avoid wasting API quota
            rms = int(np.sqrt(np.mean(audio.astype("float32") ** 2)))
            if rms < 100:
                return None
            return audio.tobytes()
        except Exception as exc:
            logger.warning("Microphone read failed: %s", exc)
            return None

    @staticmethod
    def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
        """Wrap raw int16 mono PCM in a minimal WAV container."""
        import struct
        num_samples = len(pcm_bytes) // 2
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm_bytes)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,          # PCM chunk size
            1,           # PCM format
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b"data",
            data_size,
        )
        return header + pcm_bytes

    # -- STT --

    async def listen(self) -> str | None:
        loop = asyncio.get_running_loop()
        audio_bytes = await loop.run_in_executor(
            self._executor, self._record_audio_sync
        )
        if not audio_bytes:
            return None

        provider = self._config.pipecat_stt_provider.lower()
        try:
            if provider == "deepgram":
                return await self._stt_deepgram(audio_bytes)
            elif provider == "azure":
                return await self._stt_azure(audio_bytes)
            elif provider == "google":
                return await self._stt_google(audio_bytes)
            else:
                logger.warning("Unknown STT provider: %s", provider)
                return None
        except asyncio.TimeoutError:
            logger.warning("Cloud STT timed out")
            return None
        except Exception as exc:
            logger.warning("Cloud STT failed: %s", exc)
            return None

    async def _stt_deepgram(self, audio_bytes: bytes) -> str | None:
        """POST raw audio to Deepgram prerecorded API."""
        params = {
            "model": self._config.pipecat_stt_model or "nova-2",
            "smart_format": "true",
            # Tell Deepgram the exact encoding so no WAV header is needed
            "encoding": "linear16",
            "sample_rate": str(self._config.pipecat_sample_rate),
            "channels": "1",
        }
        headers = {
            "Authorization": f"Token {self._config.pipecat_stt_api_key}",
            "Content-Type": "audio/raw",
        }
        async with self._get_http().post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers=headers,
            data=audio_bytes,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            alts = (
                data.get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])
            )
            text = alts[0].get("transcript", "").strip() if alts else ""
            return text or None

    async def _stt_azure(self, audio_bytes: bytes) -> str | None:
        """POST WAV audio to Azure Cognitive Services Speech-to-Text.

        pipecat_stt_model is used as the Azure region (e.g. 'eastus').
        """
        region = self._config.pipecat_stt_model or "eastus"
        url = (
            f"https://{region}.stt.speech.microsoft.com"
            "/speech/recognition/conversation/cognitiveservices/v1"
            "?language=en-US"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self._config.pipecat_stt_api_key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        }
        wav_bytes = self._pcm_to_wav(audio_bytes, self._config.pipecat_sample_rate)
        async with self._get_http().post(url, headers=headers, data=wav_bytes) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("RecognitionStatus") == "Success":
                return data.get("DisplayText", "").strip() or None
            return None

    async def _stt_google(self, audio_bytes: bytes) -> str | None:
        """POST base64-encoded audio to Google Cloud Speech-to-Text REST API."""
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
                    audio_bytes = await self._tts_cartesia(clean)
                elif provider == "elevenlabs":
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
