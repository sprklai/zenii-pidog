"""Entry point: python3 -m bridge

Initializes a dedicated ThreadPoolExecutor for all blocking I/O (hardware,
audio recording/playback) so the asyncio event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from .bridge import PiDogZeniiBridge
from .config import BridgeConfig


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("bridge")

    config = BridgeConfig.load()

    logger.info("PiDog-Zenii Bridge v0.1.0")
    logger.info("  Zenii URL:    %s", config.zenii_url)
    logger.info("  Voice:        %s", config.voice_provider)
    logger.info("  Hardware:     %s", "simulated" if config.simulate_hardware else "real")
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
