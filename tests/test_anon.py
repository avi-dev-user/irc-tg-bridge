"""Tests for anonymity command building."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.anon import build_anon_commands, tor_proxy_command  # noqa: E402


def test_anon_forces_tor_proxy():
    cmds = build_anon_commands("secret")
    assert "/set irc.server.secret.proxy tor" in cmds


def test_anon_disables_ctcp_leaks():
    cmds = build_anon_commands("s")
    for t in ("version", "time", "clientinfo"):
        assert f'/set irc.ctcp.{t} ""' in cmds


def test_anon_disables_dcc():
    assert "/plugin unload xfer" in build_anon_commands("s")


def test_anon_scrubs_identity():
    cmds = build_anon_commands("s")
    assert '/set irc.server.s.username ""' in cmds
    assert '/set irc.server.s.realname ""' in cmds
    assert '/set irc.server.s.msg_quit ""' in cmds


def test_tor_proxy_command():
    assert tor_proxy_command() == "/proxy add tor socks5 127.0.0.1 9050"
    assert tor_proxy_command("10.0.0.1", 9150) == "/proxy add tor socks5 10.0.0.1 9150"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
