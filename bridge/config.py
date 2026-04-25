"""Bridge configuration: env vars -> optional TOML -> defaults."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default


@dataclass
class BridgeConfig:
    """All bridge settings. Env vars take precedence over TOML."""

    # Zenii daemon connection
    zenii_url: str = "http://127.0.0.1:18981"
    zenii_token: str | None = None

    # AI provider — configured in Zenii at startup if set
    # provider_id must match a Zenii provider (e.g. "anthropic", "openai", "ollama")
    ai_provider: str = ""
    ai_model: str = ""
    ai_api_key: str = ""

    # Session
    session_title: str = "pidog-session"

    # Hardware
    simulate_hardware: bool = False

    # Voice provider: "local" (Vosk+Piper), "pipecat" (cloud), "text" (stdin/stdout)
    voice_provider: str = "local"

    # Local voice (Vosk STT + Piper TTS)
    stt_model_path: str = ""
    tts_model: str = "en_US-ryan-low"
    tts_binary: str = "piper"
    # Silence threshold as fraction of int16 max (0.0-1.0).
    # Used as: rms_threshold = max(100, int(silence_threshold * 32767 * 0.5))
    # 0.005 → RMS ~100  — very sensitive; use if mic level is low (check logs)
    # 0.02  → RMS ~327  — default; picks up normal speech on RPi HAT mic
    # 0.10  → RMS ~1638 — louder environment, need to speak up
    # 0.30  → RMS ~4915 — very loud environment only (was the broken default)
    # Tip: watch "MIC waiting for speech: peak RMS=N" in logs while speaking.
    # If N is below the threshold, lower silence_threshold or fix mic_device.
    silence_threshold: float = 0.02
    # Recording window in seconds. 5s is suitable for short voice commands.
    listen_timeout_secs: float = 5.0
    # sounddevice device index or name for mic input / speaker output.
    # -1 = system default. Use `python3 -c "import sounddevice; print(sounddevice.query_devices())"` to list.
    # PiDog Robot HAT I2S mic is typically "seeed-2mic-voicecard" or device index 1 or 2.
    mic_device: int = -1
    speaker_device: int = -1

    # Pipecat cloud voice
    pipecat_stt_provider: str = "deepgram"
    pipecat_tts_provider: str = "cartesia"
    pipecat_stt_api_key: str = ""
    pipecat_tts_api_key: str = ""
    pipecat_stt_model: str = ""
    pipecat_tts_model: str = ""
    pipecat_tts_voice: str = ""
    pipecat_sample_rate: int = 16000
    # Deepgram endpointing: ms of silence before utterance-end is declared.
    # utterance_end_ms minimum enforced by Deepgram API is 1000ms — values below
    # this cause HTTP 400 Bad Request on the WebSocket handshake.
    deepgram_endpointing_ms: int = 400
    deepgram_utterance_end_ms: int = 1000

    # Sarvam AI STT — language for Saaras model
    # Options: en-IN, hi-IN, ta-IN, te-IN, kn-IN, ml-IN, mr-IN, gu-IN, pa-IN, bn-IN, od-IN
    sarvam_language_code: str = "en-IN"

    # Sensor loop
    sensor_interval_secs: float = 2.0
    memory_throttle_secs: float = 30.0
    obstacle_alert_cm: int = 15
    touch_cooldown_secs: float = 60.0
    obstacle_cooldown_secs: float = 30.0

    # Action queue
    default_action_speed: int = 80

    # Concurrency
    thread_pool_size: int = 6
    ws_chat_timeout_secs: float = 30.0
    action_timeout_secs: float = 10.0
    led_action_timeout_secs: float = 2.0
    sensor_read_timeout_secs: float = 5.0

    # WebSocket reconnection
    ws_reconnect_delay_secs: float = 2.0
    ws_max_reconnect_delay_secs: float = 60.0
    health_check_interval_secs: float = 10.0

    # Post-speech pause before mic reopens (prevents TTS echo pickup).
    # Reduce toward 0.2 in a well-isolated acoustic environment.
    echo_prevention_secs: float = 0.5

    # LCD1602 display (optional — requires python3-smbus + I2C wiring)
    lcd_enabled: bool = False
    lcd_i2c_address: int = 0x27   # 0x27 (PCF8574T) or 0x3F (PCF8574AT)
    lcd_i2c_bus: int = 1
    lcd_scroll_delay_secs: float = 0.35

    @classmethod
    def load(cls, toml_path: str | None = None) -> BridgeConfig:
        """Load config: env vars first, then optional TOML, then defaults."""
        cfg = cls()

        # Load TOML file if specified
        path = toml_path or os.environ.get("PIDOG_CONFIG")
        if path and tomllib is not None:
            try:
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                cfg._apply_toml(data)
            except FileNotFoundError:
                pass

        # Env vars override everything
        cfg.zenii_url = os.environ.get("ZENII_URL", cfg.zenii_url)
        cfg.zenii_token = os.environ.get("ZENII_TOKEN", cfg.zenii_token)
        cfg.ai_provider = os.environ.get("ZENII_AI_PROVIDER", cfg.ai_provider)
        cfg.ai_model = os.environ.get("ZENII_AI_MODEL", cfg.ai_model)
        cfg.ai_api_key = os.environ.get("ZENII_AI_API_KEY", cfg.ai_api_key)
        cfg.session_title = os.environ.get("PIDOG_SESSION_TITLE", cfg.session_title)
        cfg.simulate_hardware = _env_bool("PIDOG_SIMULATE", cfg.simulate_hardware)

        # Voice provider
        cfg.voice_provider = os.environ.get(
            "PIDOG_VOICE_PROVIDER", cfg.voice_provider
        ).lower()

        # Local voice settings
        cfg.stt_model_path = os.environ.get("PIDOG_STT_MODEL", cfg.stt_model_path)
        cfg.tts_model = os.environ.get("PIDOG_TTS_MODEL", cfg.tts_model)
        cfg.tts_binary = os.environ.get("PIDOG_TTS_BINARY", cfg.tts_binary)
        cfg.silence_threshold = _env_float(
            "PIDOG_SILENCE_THRESHOLD", cfg.silence_threshold
        )
        cfg.listen_timeout_secs = _env_float(
            "PIDOG_LISTEN_TIMEOUT", cfg.listen_timeout_secs
        )
        cfg.mic_device = _env_int("PIDOG_MIC_DEVICE", cfg.mic_device)
        cfg.speaker_device = _env_int("PIDOG_SPEAKER_DEVICE", cfg.speaker_device)

        # Pipecat cloud voice settings
        cfg.pipecat_stt_provider = os.environ.get(
            "PIPECAT_STT_PROVIDER", cfg.pipecat_stt_provider
        )
        cfg.pipecat_tts_provider = os.environ.get(
            "PIPECAT_TTS_PROVIDER", cfg.pipecat_tts_provider
        )
        cfg.pipecat_stt_api_key = os.environ.get(
            "PIPECAT_STT_API_KEY", cfg.pipecat_stt_api_key
        )
        cfg.pipecat_tts_api_key = os.environ.get(
            "PIPECAT_TTS_API_KEY", cfg.pipecat_tts_api_key
        )
        cfg.pipecat_stt_model = os.environ.get(
            "PIPECAT_STT_MODEL", cfg.pipecat_stt_model
        )
        cfg.pipecat_tts_model = os.environ.get(
            "PIPECAT_TTS_MODEL", cfg.pipecat_tts_model
        )
        cfg.pipecat_tts_voice = os.environ.get(
            "PIPECAT_TTS_VOICE", cfg.pipecat_tts_voice
        )
        cfg.pipecat_sample_rate = _env_int(
            "PIPECAT_SAMPLE_RATE", cfg.pipecat_sample_rate
        )
        cfg.deepgram_endpointing_ms = _env_int(
            "DEEPGRAM_ENDPOINTING_MS", cfg.deepgram_endpointing_ms
        )
        cfg.deepgram_utterance_end_ms = _env_int(
            "DEEPGRAM_UTTERANCE_END_MS", cfg.deepgram_utterance_end_ms
        )
        cfg.sarvam_language_code = os.environ.get(
            "SARVAM_LANGUAGE_CODE", cfg.sarvam_language_code
        )

        # Sensor settings
        cfg.sensor_interval_secs = _env_float(
            "PIDOG_SENSOR_INTERVAL", cfg.sensor_interval_secs
        )
        cfg.memory_throttle_secs = _env_float(
            "PIDOG_MEMORY_THROTTLE", cfg.memory_throttle_secs
        )
        cfg.obstacle_alert_cm = _env_int("PIDOG_OBSTACLE_ALERT_CM", cfg.obstacle_alert_cm)
        cfg.touch_cooldown_secs = _env_float(
            "PIDOG_TOUCH_COOLDOWN", cfg.touch_cooldown_secs
        )
        cfg.obstacle_cooldown_secs = _env_float(
            "PIDOG_OBSTACLE_COOLDOWN", cfg.obstacle_cooldown_secs
        )

        # Action / concurrency settings
        cfg.default_action_speed = _env_int(
            "PIDOG_DEFAULT_SPEED", cfg.default_action_speed
        )
        cfg.thread_pool_size = _env_int(
            "PIDOG_THREAD_POOL_SIZE", cfg.thread_pool_size
        )
        cfg.ws_chat_timeout_secs = _env_float(
            "PIDOG_WS_CHAT_TIMEOUT", cfg.ws_chat_timeout_secs
        )
        cfg.action_timeout_secs = _env_float(
            "PIDOG_ACTION_TIMEOUT", cfg.action_timeout_secs
        )
        cfg.led_action_timeout_secs = _env_float(
            "PIDOG_LED_ACTION_TIMEOUT", cfg.led_action_timeout_secs
        )
        cfg.sensor_read_timeout_secs = _env_float(
            "PIDOG_SENSOR_READ_TIMEOUT", cfg.sensor_read_timeout_secs
        )

        # WS reconnection
        cfg.ws_reconnect_delay_secs = _env_float(
            "PIDOG_WS_RECONNECT_DELAY", cfg.ws_reconnect_delay_secs
        )
        cfg.ws_max_reconnect_delay_secs = _env_float(
            "PIDOG_WS_MAX_RECONNECT_DELAY", cfg.ws_max_reconnect_delay_secs
        )
        cfg.health_check_interval_secs = _env_float(
            "PIDOG_HEALTH_CHECK_INTERVAL", cfg.health_check_interval_secs
        )

        cfg.echo_prevention_secs = _env_float(
            "PIDOG_ECHO_PREVENTION_SECS", cfg.echo_prevention_secs
        )

        # LCD display
        cfg.lcd_enabled = _env_bool("PIDOG_LCD_ENABLED", cfg.lcd_enabled)
        cfg.lcd_i2c_address = _env_int("PIDOG_LCD_ADDRESS", cfg.lcd_i2c_address)
        cfg.lcd_i2c_bus = _env_int("PIDOG_LCD_BUS", cfg.lcd_i2c_bus)
        cfg.lcd_scroll_delay_secs = _env_float(
            "PIDOG_LCD_SCROLL_DELAY", cfg.lcd_scroll_delay_secs
        )

        return cfg

    def _apply_toml(self, data: dict) -> None:
        """Apply TOML values to config fields.

        Supports both flat keys and [sections]:
          [voice]
          provider = "pipecat"
          [voice.pipecat]
          stt_provider = "deepgram"
        """
        # [zenii] section — AI provider config applied to Zenii daemon at startup
        zenii_sec = data.get("zenii", {})
        zenii_map = {
            "url":      "zenii_url",
            "token":    "zenii_token",
            "ai_provider": "ai_provider",
            "ai_model":    "ai_model",
            "ai_api_key":  "ai_api_key",
        }
        for toml_key, attr in zenii_map.items():
            if toml_key in zenii_sec:
                setattr(self, attr, zenii_sec[toml_key])

        # Flat keys at top level
        simple_keys = [
            "zenii_url", "zenii_token", "session_title", "simulate_hardware",
            "voice_provider", "stt_model_path", "tts_model", "tts_binary",
            "silence_threshold", "listen_timeout_secs", "mic_device", "speaker_device",
            "sensor_interval_secs", "memory_throttle_secs",
            "obstacle_alert_cm", "touch_cooldown_secs", "obstacle_cooldown_secs",
            "default_action_speed", "thread_pool_size",
            "ws_chat_timeout_secs", "action_timeout_secs", "led_action_timeout_secs",
            "sensor_read_timeout_secs",
            "ws_reconnect_delay_secs", "ws_max_reconnect_delay_secs",
            "health_check_interval_secs",
            "lcd_enabled", "lcd_i2c_address", "lcd_i2c_bus", "lcd_scroll_delay_secs",
            "echo_prevention_secs",
        ]
        for key in simple_keys:
            if key in data:
                setattr(self, key, data[key])

        # [voice] section
        voice = data.get("voice", {})
        if "provider" in voice:
            self.voice_provider = voice["provider"]

        # [voice.local] section (toml key "stt_model" maps to field stt_model_path)
        local = voice.get("local", {})
        local_map = {
            "stt_model": "stt_model_path",
            "tts_model": "tts_model",
            "tts_binary": "tts_binary",
        }
        for toml_key, attr in local_map.items():
            if toml_key in local:
                setattr(self, attr, local[toml_key])

        # [voice.pipecat] section
        pipecat = voice.get("pipecat", data.get("pipecat", {}))
        pipecat_map = {
            "stt_provider": "pipecat_stt_provider",
            "tts_provider": "pipecat_tts_provider",
            "stt_api_key": "pipecat_stt_api_key",
            "tts_api_key": "pipecat_tts_api_key",
            "stt_model": "pipecat_stt_model",
            "tts_model": "pipecat_tts_model",
            "tts_voice": "pipecat_tts_voice",
            "sample_rate": "pipecat_sample_rate",
            "deepgram_endpointing_ms": "deepgram_endpointing_ms",
            "deepgram_utterance_end_ms": "deepgram_utterance_end_ms",
            "sarvam_language_code": "sarvam_language_code",
        }
        for toml_key, attr in pipecat_map.items():
            if toml_key in pipecat:
                setattr(self, attr, pipecat[toml_key])

        # [lcd] section
        lcd = data.get("lcd", {})
        lcd_map = {
            "enabled": "lcd_enabled",
            "address": "lcd_i2c_address",
            "bus": "lcd_i2c_bus",
            "scroll_delay": "lcd_scroll_delay_secs",
        }
        for toml_key, attr in lcd_map.items():
            if toml_key in lcd:
                setattr(self, attr, lcd[toml_key])
