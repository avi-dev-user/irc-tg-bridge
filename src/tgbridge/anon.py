"""Anonymity enforcement for a server.

When a server is anonymous the bridge closes the technical leak vectors, it does
not merely advise. Setting the server proxy to Tor is fail-closed by
construction: if Tor is down WeeChat cannot connect and never falls back to a
direct link. Some controls (CTCP replies, DCC/xfer) are global in WeeChat, so
they are applied whenever any anonymous server exists; that is a safe default
for the others too.

Exact option names are verified against the live WeeChat at deploy.
"""

from __future__ import annotations

# CTCP replies that would leak client/timezone identity. Emptied to disable.
_CTCP_LEAKS = ("version", "time", "source", "userinfo", "clientinfo", "finger", "ping")


def build_anon_commands(name: str) -> list[str]:
    """WeeChat commands to enforce anonymity for server `name` (run on core)."""
    cmds = [
        f"/set irc.server.{name}.proxy tor",   # force Tor, fail-closed
        f"/set irc.server.{name}.ipv6 off",
        f'/set irc.server.{name}.username ""',   # neutral ident
        f'/set irc.server.{name}.realname ""',   # neutral realname
        f'/set irc.server.{name}.msg_part ""',   # no client-identifying part msg
        f'/set irc.server.{name}.msg_quit ""',   # no client-identifying quit msg
    ]
    cmds += [f'/set irc.ctcp.{t} ""' for t in _CTCP_LEAKS]
    cmds.append("/plugin unload xfer")           # no DCC (reveals IP)
    return cmds


def tor_proxy_command(socks_host: str = "127.0.0.1", socks_port: int = 9050) -> str:
    """Ensure a WeeChat proxy named 'tor' exists before a server references it."""
    return f"/proxy add tor socks5 {socks_host} {socks_port}"
