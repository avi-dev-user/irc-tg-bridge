"""Async client for the WeeChat "api" relay (WeeChat 4.3+).

REST for request/response (list buffers, send input), one WebSocket for
real-time sync events (new lines, buffer open/close, nick changes). Auth is an
HTTP Basic header carrying the relay password, sent on both the REST calls and
the WebSocket handshake.

Verified against WeeChat 4.9.3 (relay_api 0.4.1).
"""

from __future__ import annotations

import base64
from typing import AsyncIterator, Optional
from urllib.parse import quote

import aiohttp


class RelayError(RuntimeError):
    pass


class WeechatRelay:
    def __init__(self, host: str, port: int, password: str, *, tls: bool = False):
        self._http_base = f"{'https' if tls else 'http'}://{host}:{port}/api"
        self._ws_url = f"{'wss' if tls else 'ws'}://{host}:{port}/api"
        auth = base64.b64encode(f"plain:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {auth}"}
        self._session: Optional[aiohttp.ClientSession] = None
        # The WebSocket needs its own session: a pooled keep-alive connection
        # left over from a REST call breaks the ws upgrade handshake (404).
        self._ws_session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(headers=self._headers)
        # Fail fast with a clear error if auth or the port is wrong.
        async with self._session.get(f"{self._http_base}/version") as r:
            if r.status != 200:
                await self.close()
                raise RelayError(f"relay auth/handshake failed: HTTP {r.status}")
        self._ws_session = aiohttp.ClientSession(headers=self._headers)
        self._ws = await self._ws_session.ws_connect(self._ws_url)

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        for session in (self._ws_session, self._session):
            if session is not None and not session.closed:
                await session.close()
        self._ws = self._ws_session = self._session = None

    async def reconnect(self) -> None:
        """Tear down and reopen. Buffer ids may change (WeeChat may have
        restarted), so callers must re-list buffers after reconnecting."""
        await self.close()
        await self.connect()

    async def version(self) -> dict:
        async with self._session.get(f"{self._http_base}/version") as r:
            return await r.json()

    async def list_buffers(self) -> list[dict]:
        async with self._session.get(f"{self._http_base}/buffers") as r:
            return await r.json()

    async def lines(self, buffer_name: str, count: int = 100) -> list[dict]:
        # Buffer names contain '#', which must be percent-encoded or it is
        # treated as a URL fragment and the request hits the wrong path.
        url = f"{self._http_base}/buffers/{quote(buffer_name, safe='')}/lines"
        async with self._session.get(url, params={"lines": f"-{count}", "colors": "strip"}) as r:
            return await r.json()

    async def input(self, buffer_name: str, data: str) -> None:
        """Send text to a buffer. Text starting with '/' runs as a command,
        anything else is sent as a message, exactly like typing in WeeChat."""
        async with self._session.post(
            f"{self._http_base}/input",
            json={"buffer_name": buffer_name, "command": data},
        ) as r:
            if r.status not in (200, 204):
                raise RelayError(f"input failed: HTTP {r.status} {await r.text()}")

    async def enable_sync(self, *, nicks: bool = False) -> None:
        await self._ws.send_json({
            "request": "POST /api/sync",
            "body": {"sync": True, "input": True, "nicks": nicks, "colors": "strip"},
        })

    async def events(self) -> AsyncIterator[dict]:
        """Yield sync event frames (code 0). Non-event frames (command acks)
        are skipped. Stops when the socket closes."""
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                frame = msg.json()
                if frame.get("code") == 0 and frame.get("message") == "Event":
                    yield frame
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
