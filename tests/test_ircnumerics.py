"""Tests for turning IRC error numerics into friendly text."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.ircnumerics import friendly_numeric  # noqa: E402


def test_477_mentions_registered_and_identify():
    msg = friendly_numeric(477, "#tldev :You need a registered nick to join")
    assert msg is not None
    low = msg.lower()
    assert "registered" in low and "identify" in low
    assert "#tldev" in msg   # keeps the specific channel from the tail


def test_473_mentions_invite_only():
    msg = friendly_numeric(473, "#secret :Cannot join channel (+i)")
    assert msg is not None and "invite-only" in msg.lower()
    assert "#secret" in msg


def test_474_mentions_banned():
    msg = friendly_numeric(474, "#chan :Cannot join channel (+b)")
    assert msg is not None and "banned" in msg.lower()
    assert "#chan" in msg


def test_475_mentions_key_or_password():
    msg = friendly_numeric(475, "#locked :Cannot join channel (+k)")
    assert msg is not None
    low = msg.lower()
    assert "key" in low or "password" in low
    assert "#locked" in msg


def test_471_full_channel():
    msg = friendly_numeric(471, "#packed :Cannot join channel (+l)")
    assert msg is not None and "full" in msg.lower() and "#packed" in msg


def test_401_no_such_nick():
    msg = friendly_numeric(401, "ghost :No such nick/channel")
    assert msg is not None and "ghost" in msg


def test_433_nick_in_use():
    msg = friendly_numeric(433, "tgb :Nickname is already in use")
    assert msg is not None and "use" in msg.lower() and "tgb" in msg


def test_461_keeps_command_and_reason():
    msg = friendly_numeric(461, "MODE :Not enough parameters")
    assert msg is not None and "MODE" in msg
    assert "parameter" in msg.lower()


def test_461_target_or_reason_with_braces_does_not_raise():
    # the target/reason are raw server text; a stray brace must not blow up the
    # builder (it must never be fed through .format()).
    msg = friendly_numeric(461, "PR{X}Y :bad")
    assert msg is not None and "PR{X}Y" in msg
    msg = friendly_numeric(461, "MODE :need {n} args")
    assert msg is not None and "MODE" in msg and "{n}" in msg


def test_482_not_operator():
    msg = friendly_numeric(482, "#room :You're not channel operator")
    assert msg is not None and "operator" in msg.lower() and "#room" in msg


def test_weechat_display_form_is_parsed():
    # WeeChat can render the numeric as "target: reason" without the raw colon.
    msg = friendly_numeric(477, "#tldev: You need a registered nick")
    assert msg is not None and "#tldev" in msg and "registered" in msg.lower()


def test_generic_message_when_no_target():
    msg = friendly_numeric(473, "Cannot join channel (+i)")
    assert msg is not None and "invite-only" in msg.lower()


def test_402_no_such_server():
    msg = friendly_numeric(402, "irc.dead.net :No such server")
    assert msg is not None and "server" in msg.lower() and "irc.dead.net" in msg


def test_405_too_many_channels():
    msg = friendly_numeric(405, "#extra :You have joined too many channels")
    assert msg is not None and "too many channels" in msg.lower() and "#extra" in msg


def test_406_was_no_such_nick():
    msg = friendly_numeric(406, "ghost :There was no such nickname")
    assert msg is not None and "no record" in msg.lower() and "ghost" in msg


def test_412_no_text_to_send():
    msg = friendly_numeric(412, ":No text to send")
    assert msg is not None and "empty" in msg.lower()


def test_436_nick_collision():
    msg = friendly_numeric(436, "tgb :Nickname collision KILL")
    assert msg is not None and "collision" in msg.lower() and "tgb" in msg


def test_437_temporarily_unavailable():
    msg = friendly_numeric(437, "#chan :Nick/channel is temporarily unavailable")
    assert msg is not None and "temporarily unavailable" in msg.lower() and "#chan" in msg


def test_451_not_registered():
    msg = friendly_numeric(451, ":You have not registered")
    assert msg is not None and "register" in msg.lower()


def test_464_password_mismatch():
    msg = friendly_numeric(464, ":Password incorrect")
    assert msg is not None and "password" in msg.lower()


def test_465_banned_from_server():
    msg = friendly_numeric(465, ":You are banned from this server")
    assert msg is not None and "banned" in msg.lower()


def test_478_ban_list_full():
    msg = friendly_numeric(478, "#chan b :Channel ban list is full")
    assert msg is not None and "ban list" in msg.lower() and "#chan" in msg


def test_485_only_creator():
    msg = friendly_numeric(485, "#uniq :You're not the original channel operator")
    assert msg is not None and "creator" in msg.lower() and "#uniq" in msg


def test_491_no_oper_host():
    msg = friendly_numeric(491, ":No O-lines for your host")
    assert msg is not None and "operator" in msg.lower()


def test_new_numerics_with_braces_do_not_raise():
    # 402/478 template on the target; a brace in the raw target must not blow up.
    assert friendly_numeric(402, "srv{x} :No such server") is not None
    assert friendly_numeric(478, "#c{h}an b :list full") is not None
    assert friendly_numeric(464, ":bad {pw}") is not None


def test_reply_numerics_left_unmapped():
    # whois/names/list replies are rendered by their own features; this module
    # must not double-handle them.
    for numeric in (311, 312, 313, 317, 318, 319, 322, 323, 330, 353, 366, 671):
        assert friendly_numeric(numeric, "#x :some reply payload") is None


def test_unknown_numerics_return_none():
    assert friendly_numeric(1, "Welcome to the network") is None
    assert friendly_numeric(372, "- some MOTD line") is None
    assert friendly_numeric(322, "#python 42 :topic") is None
    assert friendly_numeric(None, "no numeric here") is None


def test_no_leading_marker():
    # the router adds the warning marker; the explanation must not carry one.
    for numeric in (401, 403, 473, 474, 477, 482):
        msg = friendly_numeric(numeric, "#x :reason")
        assert msg and not msg.startswith(("!", "*", "-", "["))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
