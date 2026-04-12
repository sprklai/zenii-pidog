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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
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

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.request_shutdown)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        executor.shutdown(wait=False)
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
