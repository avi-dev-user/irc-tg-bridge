"""Tests for the add-server flow, command building, and /list parsing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.commands import (  # noqa: E402
    AddServerFlow, build_addserver_commands, parse_list_reply,
    parse_names_reply, strip_bot_mention,
)


def run_flow(answers):
    flow = AddServerFlow()
    for a in answers:
        assert not flow.is_complete()
        flow.feed(a)
    return flow


def test_full_sasl_flow_collects_all_fields():
    flow = run_flow(["libera", "irc.libera.chat", "6697", "yes", "mynick",
                     "sasl", "secretpw", "off"])
    assert flow.is_complete()
    assert flow.data == {
        "name": "libera", "host": "irc.libera.chat", "port": 6697,
        "tls": True, "nick": "mynick", "auth": "sasl", "password": "secretpw",
        "privacy": "off",
    }


def test_none_auth_skips_password_step():
    flow = AddServerFlow()
    flow.feed("net"); flow.feed("host"); flow.feed("6667"); flow.feed("no")
    flow.feed("nick"); flow.feed("none")   # tls, nick, then auth
    # next prompt must be privacy, not password
    assert flow.prompt_key() == "addserver.privacy"
    flow.feed("off")
    assert flow.is_complete() and "password" not in flow.data


def test_flow_validation_rejects_bad_port_and_name():
    flow = AddServerFlow()
    flow.feed("ok")
    flow.feed("host")
    try:
        flow.feed("notaport")
        assert False, "expected ValueError"
    except ValueError:
        pass
    # step did not advance; still on port
    assert flow.prompt_key() == "addserver.port"


def test_build_commands_sasl_uses_secured_data():
    cmds = build_addserver_commands({
        "name": "libera", "host": "irc.libera.chat", "port": 6697,
        "tls": True, "nick": "me", "auth": "sasl", "password": "pw", "privacy": "off",
    })
    assert "/server add libera irc.libera.chat/6697 -tls" == cmds[0]
    assert "/secure set libera_pass pw" in cmds
    assert '/set irc.server.libera.sasl_password "${sec.data.libera_pass}"' in cmds
    # /connect comes last before the persisting /save, and after the SASL setup
    assert cmds[-2:] == ["/connect libera", "/save"]
    assert cmds.index('/set irc.server.libera.sasl_password "${sec.data.libera_pass}"') \
        < cmds.index("/connect libera")
    # the raw password never appears in a server option, only in secured data
    assert not any("sasl_password pw" in c for c in cmds)


def test_build_commands_nickserv():
    cmds = build_addserver_commands({
        "name": "oftc", "host": "irc.oftc.net", "port": 6697,
        "tls": True, "nick": "me", "auth": "nickserv", "password": "pw", "privacy": "off",
    })
    assert any("NickServ IDENTIFY" in c and "${sec.data.oftc_pass}" in c for c in cmds)


def test_build_commands_plaintext_has_no_tls_flag():
    # TLS is opt-in now; a plaintext choice must not add -tls (the old hardcoded
    # -tls made a plain or local server fail its handshake and never connect).
    cmds = build_addserver_commands({
        "name": "local", "host": "127.0.0.1", "port": 6667,
        "tls": False, "nick": "me", "auth": "none", "privacy": "off",
    })
    assert "/server add local 127.0.0.1/6667" == cmds[0]
    assert not any("-tls" in c for c in cmds)


def test_build_commands_anon_sets_proxy():
    cmds = build_addserver_commands({
        "name": "onionnet", "host": "abc.onion", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "anon",
    })
    assert "/set irc.server.onionnet.proxy tor" in cmds
    # the proxy must be created before the server references it, or the
    # connection fails with "proxy tor not found".
    assert "/proxy add tor socks5 127.0.0.1 9050" in cmds
    assert cmds.index("/proxy add tor socks5 127.0.0.1 9050") \
        < cmds.index("/set irc.server.onionnet.proxy tor")


def test_build_commands_persist_with_save():
    # a runtime-added server is memory-only until saved; without /save it is lost
    # on the next WeeChat restart and cannot be recreated (no host/port stored).
    cmds = build_addserver_commands({
        "name": "libera", "host": "irc.libera.chat", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "off",
    })
    assert cmds[-1] == "/save"


def test_build_commands_onion_disables_tls_verify():
    # a .onion never matches the server's TLS cert name, so the hostname check
    # would fail the handshake; the onion address is the identity, so verify off.
    cmds = build_addserver_commands({
        "name": "onionnet", "host": "abc.onion", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "anon",
    })
    assert "/set irc.server.onionnet.tls_verify off" in cmds


def test_build_commands_no_tls_verify_change_for_clearnet():
    cmds = build_addserver_commands({
        "name": "libera", "host": "irc.libera.chat", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "off",
    })
    assert not any("tls_verify" in c for c in cmds)


def test_build_commands_tor_privacy_sets_proxy():
    cmds = build_addserver_commands({
        "name": "t", "host": "irc.example.org", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "tor",
    })
    assert "/set irc.server.t.proxy tor" in cmds


def test_build_commands_no_proxy_when_privacy_off():
    cmds = build_addserver_commands({
        "name": "plain", "host": "irc.example.org", "port": 6697,
        "tls": True, "nick": "me", "auth": "none", "privacy": "off",
    })
    assert not any("proxy" in c for c in cmds)


def test_back_over_skipped_password_step_for_none_auth():
    # auth "none" skips the password step forward; back() must skip it too,
    # landing on auth rather than a step that was never shown.
    flow = AddServerFlow()
    for a in ["net", "host", "6667", "no", "nick", "none"]:
        flow.feed(a)
    assert flow.current()[0] == "privacy"   # password was skipped
    flow.back()
    assert flow.current()[0] == "auth"


def test_back_from_privacy_lands_on_password_for_sasl():
    flow = AddServerFlow()
    for a in ["net", "host", "6667", "no", "nick", "sasl", "pw"]:
        flow.feed(a)
    assert flow.current()[0] == "privacy"
    flow.back()
    assert flow.current()[0] == "password"


def test_name_length_capped_to_fit_callback_data():
    # the name is packed into callback_data (64-byte cap); an over-long name
    # would overflow it, so the flow must reject it at the name step.
    flow = AddServerFlow()
    try:
        flow.feed("x" * 31)
        assert False, "expected ValueError for an over-long name"
    except ValueError:
        pass
    assert flow.prompt_key() == "addserver.name"   # step did not advance
    flow.feed("x" * 30)   # exactly at the bound is accepted
    assert flow.data["name"] == "x" * 30


def test_nick_must_be_ascii():
    flow = AddServerFlow()
    flow.feed("net"); flow.feed("host"); flow.feed("6667"); flow.feed("no")  # tls
    try:
        flow.feed("טסט")   # Hebrew nick -> rejected (IRC needs ASCII)
        assert False, "expected ValueError for non-ASCII nick"
    except ValueError:
        pass
    flow.feed("avi_test")   # ASCII nick accepted
    assert flow.data["nick"] == "avi_test"


def test_sasl_rejects_skipped_password():
    flow = AddServerFlow()
    for a in ["net", "host", "6667", "no", "nick", "sasl"]:
        flow.feed(a)
    try:
        flow.feed("skip")
        assert False, "expected ValueError for skipped sasl password"
    except ValueError:
        pass


def test_build_omits_creds_when_no_password():
    cmds = build_addserver_commands({
        "name": "n", "host": "h", "port": 6667, "nick": "me",
        "auth": "sasl", "password": None, "tor": False, "anon": False,
    })
    assert not any("secure set" in c for c in cmds)
    assert not any("None" in c for c in cmds)   # never emit the literal None


def test_parse_list_reply():
    assert parse_list_reply("#python 4213 :Python programming") == {
        "channel": "#python", "users": 4213, "topic": "Python programming"}
    assert parse_list_reply("#empty 0 :") == {
        "channel": "#empty", "users": 0, "topic": ""}
    assert parse_list_reply("garbage line") is None
    assert parse_list_reply("#nousercount notanumber :x") is None


def test_parse_names_reply_mixed_prefixes():
    # a 353 with the visibility symbol, channel, then members with @/%/+ prefixes
    parsed = parse_names_reply("= #chan :@alice %helper +carol dave")
    assert parsed["channel"] == "#chan"
    assert parsed["members"] == [
        {"prefix": "@", "nick": "alice"},
        {"prefix": "%", "nick": "helper"},
        {"prefix": "+", "nick": "carol"},
        {"prefix": "", "nick": "dave"},
    ]


def test_parse_names_reply_without_symbol():
    # some relays drop the leading symbol; the channel then leads the head
    parsed = parse_names_reply("#chan :@op plain")
    assert parsed["channel"] == "#chan"
    assert [m["nick"] for m in parsed["members"]] == ["op", "plain"]
    assert parsed["members"][0]["prefix"] == "@"


def test_parse_names_reply_owner_and_admin_prefixes():
    parsed = parse_names_reply("= &local :~founder &admin @op")
    assert [(m["prefix"], m["nick"]) for m in parsed["members"]] == [
        ("~", "founder"), ("&", "admin"), ("@", "op")]


def test_parse_names_reply_empty_membership():
    parsed = parse_names_reply("= #chan :")
    assert parsed == {"channel": "#chan", "members": []}


def test_parse_names_reply_rejects_malformed():
    assert parse_names_reply("no colon here") is None
    assert parse_names_reply("= notachannel :nicks") is None


def test_strip_bot_mention_drops_mention_from_command_word_only():
    # the @botname suffix Telegram adds to a grouped slash command is removed
    assert strip_bot_mention("/part@thebot #chan") == "/part #chan"
    assert strip_bot_mention("/quit@thebot") == "/quit"


def test_strip_bot_mention_preserves_argument_with_at_sign():
    # an argument that legitimately contains @ (a nick@host) must not be touched
    assert strip_bot_mention("/whois nick@host") == "/whois nick@host"
    assert strip_bot_mention("/msg nick@host hi there") == "/msg nick@host hi there"


def test_strip_bot_mention_leaves_non_command_text_unchanged():
    assert strip_bot_mention("hello@world how are you") == "hello@world how are you"
    assert strip_bot_mention("just chatting") == "just chatting"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
