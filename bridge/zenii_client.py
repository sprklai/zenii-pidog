"""Async HTTP + WebSocket client for the Zenii daemon API."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import AsyncIterator

import aiohttp

from .config import BridgeConfig

logger = logging.getLogger(__name__)


class ZeniiClient:
    """Communicates with Zenii daemon via HTTP REST and WebSocket."""

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_lock = asyncio.Lock()
        self._reconnect_delay = config.ws_reconnect_delay_secs

    # -- Lifecycle --

    async def start(self) -> None:
        """Create the aiohttp session."""
        headers: dict[str, str] = {}
        if self._config.zenii_token:
            headers["Authorization"] = f"Bearer {self._config.zenii_token}"
        self._session = aiohttp.ClientSession(
            base_url=self._config.zenii_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        )

    async def close(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("ZeniiClient not started — call start() first")
        return self._session

    # -- HTTP methods --

    async def health_check(self) -> bool:
        """GET /health -> True if daemon is healthy."""
        session = self._ensure_session()
        try:
            async with session.get("/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "ok"
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def create_session(self, title: str) -> str:
        """POST /sessions -> session_id."""
        session = self._ensure_session()
        async with session.post("/sessions", json={"title": title}) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["id"]

    async def store_memory(
        self, key: str, content: str, category: str = "core"
    ) -> None:
        """POST /memory."""
        session = self._ensure_session()
        payload = {"key": key, "content": content, "category": category}
        async with session.post("/memory", json=payload) as resp:
            resp.raise_for_status()

    async def recall_memory(
        self, query: str, limit: int = 5, offset: int = 0
    ) -> list[dict]:
        """GET /memory?q=...&limit=...&offset=..."""
        session = self._ensure_session()
        params = {"q": query, "limit": str(limit), "offset": str(offset)}
        async with session.get("/memory", params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def chat(
        self, prompt: str, session_id: str | None = None, model: str | None = None
    ) -> dict:
        """POST /chat -> {"response": "...", "session_id": "..."}."""
        session = self._ensure_session()
        payload: dict = {"prompt": prompt}
        if session_id:
            payload["session_id"] = session_id
        if model:
            payload["model"] = model
        async with session.post("/chat", json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def update_identity(self, name: str, content: str) -> None:
        """PUT /identity/{name}."""
        session = self._ensure_session()
        async with session.put(f"/identity/{name}", json={"content": content}) as resp:
            resp.raise_for_status()

    async def reload_identity(self) -> None:
        """POST /identity/reload."""
        session = self._ensure_session()
        async with session.post("/identity/reload") as resp:
            resp.raise_for_status()

    async def set_credential(self, key: str, value: str) -> None:
        """POST /credentials."""
        session = self._ensure_session()
        async with session.post(
            "/credentials", json={"key": key, "value": value}
        ) as resp:
            resp.raise_for_status()

    async def set_default_provider(
        self, provider_id: str, model_id: str
    ) -> None:
        """PUT /providers/default."""
        session = self._ensure_session()
        async with session.put(
            "/providers/default",
            json={"provider_id": provider_id, "model_id": model_id},
        ) as resp:
            resp.raise_for_status()

    async def get_tools(self) -> list[dict]:
        """GET /tools."""
        session = self._ensure_session()
        async with session.get("/tools") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_config(self) -> dict:
        """GET /config."""
        session = self._ensure_session()
        async with session.get("/config") as resp:
            resp.raise_for_status()
            return await resp.json()

    # -- WebSocket methods --

    async def ws_connect(self) -> None:
        """Connect to the chat WebSocket with auth token if configured."""
        session = self._ensure_session()
        url = "/ws/chat"
        if self._config.zenii_token:
            url += f"?token={self._config.zenii_token}"

        if self._ws and not self._ws.closed:
            await self._ws.close()

        self._ws = await session.ws_connect(url)
        self._reconnect_delay = self._config.ws_reconnect_delay_secs
        logger.info("WebSocket connected")

    async def ws_ensure_connected(self) -> None:
        """Reconnect if WebSocket is closed. Exponential backoff with jitter."""
        if self._ws and not self._ws.closed:
            return

        while True:
            # Check daemon health first
            if not await self.health_check():
                logger.warning(
                    "Daemon not healthy, waiting %.1fs before retry",
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._config.ws_max_reconnect_delay_secs,
                )
                continue

            try:
                await self.ws_connect()
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                jitter = random.uniform(0.75, 1.25)
                delay = self._reconnect_delay * jitter
                logger.warning(
                    "WS connect failed (%s), retrying in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._config.ws_max_reconnect_delay_secs,
                )

    async def ws_send_prompt(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Send a chat prompt over WebSocket."""
        async with self._ws_lock:
            await self.ws_ensure_connected()
            assert self._ws is not None
            payload = {
                "prompt": prompt,
                "session_id": session_id,
                "model": model,
            }
            await self._ws.send_json(payload)

    async def ws_receive(self) -> AsyncIterator[dict]:
        """Yield parsed JSON messages until {"type": "done"} or error.

        Raises ConnectionError if the WebSocket closes unexpectedly.
        """
        if self._ws is None or self._ws.closed:
            raise ConnectionError("WebSocket not connected")

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                msg_type = data.get("type", "")

                if msg_type == "done":
                    yield data
                    return
                if msg_type == "error":
                    logger.error(
                        "WS error: %s (code=%s, hint=%s)",
                        data.get("error"),
                        data.get("error_code"),
                        data.get("hint"),
                    )
                    yield data
                    return

                yield data

            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError(f"WS error: {self._ws.exception()}")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                raise ConnectionError("WebSocket closed by server")

        raise ConnectionError("WebSocket message stream ended")
