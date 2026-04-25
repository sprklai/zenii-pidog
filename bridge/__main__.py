"""Entry point: python3 -m bridge

Initializes a dedicated ThreadPoolExecutor for all blocking I/O (hardware,
audio recording/playback) so the asyncio event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .bridge import PiDogZeniiBridge
from .config import BridgeConfig

_BRIDGE_VERSION = "0.1.0"


def _file_sha(path: Path) -> str:
    """Return first 8 hex chars of the SHA-256 of a file, or '?' on error."""
    try:
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        return h[:8]
    except OSError:
        return "?"


class _ColorFormatter(logging.Formatter):
    """Color-code log lines by pipeline stage using ANSI escape codes."""

    # Prefixes that mark specific pipeline stages
    _STAGE_COLORS = {
        # STT pipeline
        "User:":            "\033[92m",   # bright green  — STT captured speech
        "STT FINAL":        "\033[92m",   # bright green  — cloud STT final transcript
        "STT interim":      "\033[32m",   # green         — cloud STT interim result
        "MIC listen":       "\033[32m",   # green         — mic active / listening
        "MIC speech":       "\033[92m",   # bright green  — VAD triggered
        "Connecting to":    "\033[32m",   # green         — connecting to cloud STT
        # LLM pipeline
        ">>> LLM":          "\033[93m",   # bright yellow  — prompt sent to LLM
        ">>>":              "\033[93m",   # bright yellow  — sending to LLM (legacy)
        "<<< LLM:":         "\033[96m",   # bright cyan    — LLM response text
        "<<<":              "\033[96m",   # bright cyan    — LLM response (legacy)
        "AI raw:":          "\033[96m",   # bright cyan
        "LLM smoke":        "\033[96m",   # bright cyan
        # TTS / action pipeline
        "Speaking:":        "\033[95m",   # bright magenta — TTS output
        "Queuing action:":  "\033[94m",   # bright blue    — action queued
        "Executing action:": "\033[94m",  # bright blue    — action running
        "Queuing LED:":     "\033[34m",   # blue           — LED queued
        "Executing LED:":   "\033[34m",   # blue           — LED running
        "No clean text":    "\033[91m",   # bright red     — warning
    }

    _LEVEL_COLORS = {
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[41m",   # red background
    }

    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        # Try stage color first (based on message content)
        for prefix, color in self._STAGE_COLORS.items():
            if prefix in record.getMessage():
                return f"{color}{msg}{self._RESET}"
        # Fall back to level color
        color = self._LEVEL_COLORS.get(record.levelname, "")
        return f"{color}{msg}{self._RESET}" if color else msg


def main() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _ColorFormatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logger = logging.getLogger("bridge")

    config = BridgeConfig.load()

    # Print a short content-hash of this file so mismatches between the repo
    # checkout and the deployed copy are immediately visible in logs.
    this_sha = _file_sha(Path(__file__))
    config_src = os.environ.get("PIDOG_CONFIG", "env/defaults")

    logger.info("PiDog-Zenii Bridge v%s (sha=%s)", _BRIDGE_VERSION, this_sha)
    logger.info("  Config:       %s", config_src)
    logger.info("  Zenii URL:    %s", config.zenii_url)
    logger.info("  Voice:        %s", config.voice_provider)
    logger.info("  Hardware:     %s (requested)", "simulated" if config.simulate_hardware else "real")
    logger.info("  Thread pool:  %d threads", config.thread_pool_size)

    executor = ThreadPoolExecutor(
        max_workers=config.thread_pool_size,
        thread_name_prefix="pidog-io",
    )

    bridge = PiDogZeniiBridge(config, executor)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_signal() -> None:
        bridge.request_shutdown()
        # Remove handlers so a second Ctrl+C doesn't interrupt cleanup threads.
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(_sig)
            except Exception:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
