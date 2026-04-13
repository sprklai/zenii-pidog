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
    # 0.3 → RMS ~9830 — filters ambient noise, requires actual speech.
    silence_threshold: float = 0.3
    # Recording window in seconds. 5s is suitable for short voice commands.
    listen_timeout_secs: float = 5.0

    # Pipecat cloud voice
    pipecat_stt_provider: str = "deepgram"
    pipecat_tts_provider: str = "cartesia"
    pipecat_stt_api_key: str = ""
    pipecat_tts_api_key: str = ""
    pipecat_stt_model: str = ""
    pipecat_tts_model: str = ""
    pipecat_tts_voice: str = ""
    pipecat_sample_rate: int = 16000

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
    ws_chat_timeout_secs: float = 120.0
    action_timeout_secs: float = 10.0
    sensor_read_timeout_secs: float = 5.0

    # WebSocket reconnection
    ws_reconnect_delay_secs: float = 2.0
    ws_max_reconnect_delay_secs: float = 60.0
    health_check_interval_secs: float = 10.0

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
            "silence_threshold", "listen_timeout_secs",
            "sensor_interval_secs", "memory_throttle_secs",
            "obstacle_alert_cm", "touch_cooldown_secs", "obstacle_cooldown_secs",
            "default_action_speed", "thread_pool_size",
            "ws_chat_timeout_secs", "action_timeout_secs", "sensor_read_timeout_secs",
            "ws_reconnect_delay_secs", "ws_max_reconnect_delay_secs",
            "health_check_interval_secs",
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
        }
        for toml_key, attr in pipecat_map.items():
            if toml_key in pipecat:
                setattr(self, attr, pipecat[toml_key])
