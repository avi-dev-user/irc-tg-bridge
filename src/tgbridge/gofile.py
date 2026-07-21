"""Upload a file to gofile.io and return its download link.

gofile takes large files for free with a guest account (no token needed), which
sidesteps Telegram's own upload caps: the bridge re-hosts a received file there
and posts a link, instead of trying to push the bytes through Telegram. The
response parsing is pure so it is tested directly; the HTTP call is thin glue
(aiohttp is imported lazily so this module loads without it).
"""

from __future__ import annotations

import os
from typing import Optional

_API = "https://api.gofile.io"


def parse_server(payload: dict) -> Optional[str]:
    """The upload server name from a getServer/servers response, tolerating the
    shapes gofile has used (data.server, or data.servers[0].name)."""
    data = (payload or {}).get("data") or {}
    if isinstance(data.get("server"), str):
        return data["server"]
    servers = data.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            return first.get("name")
        if isinstance(first, str):
            return first
    return None


def parse_link(payload: dict) -> Optional[str]:
    """The download page URL from an uploadFile response, or None on failure."""
    if (payload or {}).get("status") != "ok":
        return None
    return ((payload.get("data") or {}).get("downloadPage")) or None


async def upload_file(path: str, *, token: Optional[str] = None,
                      api: str = _API) -> str:
    """Upload a local file to gofile and return its download page URL. Raises on
    failure. Uses a guest account when no token is given."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{api}/getServer") as resp:
            server = parse_server(await resp.json())
        if not server:
            raise RuntimeError("gofile: no upload server available")
        with open(path, "rb") as fh:
            form = aiohttp.FormData()
            if token:
                form.add_field("token", token)
            form.add_field("file", fh, filename=os.path.basename(path))
            async with session.post(
                    f"https://{server}.gofile.io/uploadFile", data=form) as resp:
                link = parse_link(await resp.json())
    if not link:
        raise RuntimeError("gofile: upload did not return a link")
    return link
