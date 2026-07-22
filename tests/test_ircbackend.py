"""Tests for IRC line parsing, using tag shapes captured from real WeeChat/Libera."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.ircbackend import (  # noqa: E402
    parse_line, channel_join_event, channel_close_event, private_open_event,
    connected_servers, reconcile_server_status,
    IrcMessage, IrcEvent, IrcReaction, IrcRedact)

SERVER_BUF = {"name": "irc.lt", "type": "server", "server": "lt", "conversation": "lt"}
CHAN_BUF = {"name": "irc.lt.#weechat", "type": "channel", "server": "lt", "conversation": "#weechat"}
PM_BUF = {"name": "irc.lt.alice", "type": "private", "server": "lt", "conversation": "alice"}
LIST_BUF = {"name": "irc.list_libera", "type": "list", "server": "libera",
            "conversation": ""}


def test_server_numeric_becomes_server_event():
    # real capture: the 001 welcome line
    body = {"message": "Welcome to the Libera.Chat ... tgb_probe_9931",
            "tags": ["irc_001", "irc_numeric", "irc_tag_time=2026-07-19T03:14:31.292Z",
                     "nick_silver.libera.chat", "log3"], "highlight": False}
    ev = parse_line(SERVER_BUF, body, "tgb_probe_9931")
    assert isinstance(ev, IrcEvent) and ev.kind == "server"
    assert ev.numeric == 1   # derived from the irc_001 tag, drives connect status


def test_server_numeric_derived_for_motd():
    body = {"message": "- some MOTD line",
            "tags": ["irc_372", "irc_numeric", "log3"], "highlight": False}
    ev = parse_line(SERVER_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "server"
    assert ev.numeric == 372


def test_server_notice_is_server_event_not_message():
    body = {"message": "*** Checking Ident",
            "tags": ["irc_notice", "nick_silver.libera.chat", "log1"], "highlight": False}
    ev = parse_line(SERVER_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "server"
    assert ev.numeric is None   # a non-numeric server notice carries no numeric


def test_channel_notice_strips_weechat_wrapper():
    # weechat renders a channel notice as "Notice(nick) -> target: body"; the
    # bridge keeps only the body so its own "-nick-" prefix is not doubled.
    body = {"message": "Notice(tester2) -> #test: hello there",
            "tags": ["irc_notice", "nick_tester2"], "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(m, IrcMessage) and m.is_notice is True
    assert m.text == "hello there" and m.nick == "tester2"


def test_channel_privmsg_from_other():
    body = {"message": "hey, anyone around?",
            "tags": ["irc_privmsg", "nick_alice", "irc_tag_msgid=abc123",
                     "irc_tag_time=2026-07-19T03:15:00.000Z", "log1"], "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(m, IrcMessage)
    assert m.nick == "alice" and m.text == "hey, anyone around?"
    assert m.is_self is False and m.is_private is False
    assert m.msgid == "abc123"
    assert m.conversation == "#weechat" and m.server == "lt"


def test_self_message_by_tag():
    body = {"message": "my own line", "tags": ["irc_privmsg", "nick_tgb", "self_msg"],
            "highlight": False}
    m = parse_line(CHAN_BUF, body, "someoneelse")
    assert isinstance(m, IrcMessage) and m.is_self is True


def test_self_message_by_nick_fallback():
    # no self_msg tag, but sender nick equals our nick on this server
    body = {"message": "echo", "tags": ["irc_privmsg", "nick_tgb"], "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert m.is_self is True


def test_action_flag():
    body = {"message": "waves", "tags": ["irc_action", "nick_bob"], "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(m, IrcMessage) and m.is_action is True and m.nick == "bob"


def test_private_message():
    body = {"message": "psst", "tags": ["irc_privmsg", "nick_alice"], "highlight": True}
    m = parse_line(PM_BUF, body, "tgb")
    assert m.is_private is True and m.highlight is True


def test_channel_notice_is_message():
    body = {"message": "[notice] heads up", "tags": ["irc_notice", "nick_chanserv"],
            "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(m, IrcMessage) and m.is_notice is True


def test_join_event():
    body = {"message": "bob (bob@host) has joined #weechat",
            "tags": ["irc_join", "nick_bob"], "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "join"


def test_own_join_line_suppressed():
    # our own join arrives as an IRC line too; it is dropped so it does not
    # duplicate the buffer-opened join signal.
    body = {"message": "tgb (tgb@host) has joined #weechat",
            "tags": ["irc_join", "nick_tgb"], "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb") is None


def test_channel_open_is_a_self_join_signal():
    join = channel_join_event({"name": "irc.lt.#weechat", "type": "channel",
                               "server": "lt", "conversation": "#weechat"})
    assert isinstance(join, IrcEvent) and join.kind == "join"
    assert join.affects_me is True and "#weechat" in join.text
    # server/private buffers opening are not channel joins
    assert channel_join_event({"name": "irc.lt", "type": "server"}) is None
    assert channel_join_event({"name": "irc.lt.bob", "type": "private"}) is None


def test_channel_close_is_a_self_part_signal():
    close = channel_close_event({"name": "irc.lt.#weechat", "type": "channel",
                                 "server": "lt", "conversation": "#weechat"})
    assert isinstance(close, IrcEvent) and close.kind == "part"
    assert close.affects_me is True and close.lifecycle == "closed"
    assert "#weechat" in close.text
    # a missing buffer, or a server/private buffer closing, is not a channel part
    assert channel_close_event(None) is None
    assert channel_close_event({"name": "irc.lt", "type": "server"}) is None
    assert channel_close_event({"name": "irc.lt.bob", "type": "private"}) is None


def test_private_open_is_a_pm_topic_signal():
    ev = private_open_event({"name": "irc.lt.alice", "type": "private",
                             "server": "lt", "conversation": "alice"})
    assert isinstance(ev, IrcEvent) and ev.kind == "private"
    assert ev.lifecycle == "opened" and ev.buffer == "irc.lt.alice"
    assert ev.text == ""   # no body: opening a PM is not announced like a join
    # channel/server buffers opening are not PM signals
    assert private_open_event({"name": "irc.lt.#weechat", "type": "channel"}) is None
    assert private_open_event({"name": "irc.lt", "type": "server"}) is None
    assert private_open_event(None) is None


def test_channel_and_private_open_are_mutually_exclusive():
    # the two helpers never both fire for one buffer: exactly one type matches.
    chan = {"name": "irc.lt.#x", "type": "channel", "server": "lt", "conversation": "#x"}
    pm = {"name": "irc.lt.bob", "type": "private", "server": "lt", "conversation": "bob"}
    assert channel_join_event(chan) is not None and private_open_event(chan) is None
    assert private_open_event(pm) is not None and channel_join_event(pm) is None


def test_own_part_line_is_a_closed_lifecycle():
    # /part leaves the WeeChat buffer open, so no buffer-close signal fires; the
    # part line itself must surface leaving as a "closed" lifecycle.
    body = {"message": "tgb (tgb@host) has left #weechat", "tags": ["irc_part",
            "nick_tgb"], "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "part" and ev.lifecycle == "closed"


def test_other_part_line_is_not_a_leave():
    # someone else parting is ambient noise, never our own "closed" lifecycle.
    body = {"message": "bob (bob@host) has left #weechat", "tags": ["irc_part",
            "nick_bob"], "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "part" and ev.lifecycle is None


def test_kick_affecting_me():
    body = {"message": "tgb was kicked by op (spam)", "tags": ["irc_kick", "nick_op"],
            "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "kick" and ev.affects_me is True
    # being kicked is a leave: the buffer stays, so mark it closed
    assert ev.lifecycle == "closed"


def test_kicking_someone_else_is_not_my_leave():
    # we do the kicking (we are the acting nick): we stay in the channel.
    body = {"message": "bob was kicked by tgb (spam)", "tags": ["irc_kick",
            "nick_tgb"], "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "kick" and ev.lifecycle is None


def test_noise_is_dropped():
    # a TLS certificate info line on the server buffer: no useful tag
    body = {"message": "gnutls: receiving 2 certificates", "tags": ["tls"], "highlight": False}
    assert parse_line(SERVER_BUF, body, "tgb") is None


def test_list_reply_on_list_buffer_is_server_numeric():
    # RPL_LIST arrives on the dedicated list buffer, tagged irc_322, and must be
    # surfaced as a server-kind numeric so channel discovery can collect it.
    body = {"message": "#tldev 42 :dev chatter",
            "tags": ["irc_322", "irc_numeric", "log3"], "highlight": False}
    ev = parse_line(LIST_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "server"
    assert ev.numeric == 322 and ev.server == "libera"
    assert ev.text == "#tldev 42 :dev chatter"


def test_list_end_on_list_buffer_is_server_numeric():
    body = {"message": ":End of /LIST",
            "tags": ["irc_323", "irc_numeric", "log3"], "highlight": False}
    ev = parse_line(LIST_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "server" and ev.numeric == 323
    assert ev.server == "libera"


def test_non_numeric_list_line_is_dropped():
    # a plain informational line on the list buffer with no numeric tag is noise.
    body = {"message": "gathering channels", "tags": ["log3"], "highlight": False}
    assert parse_line(LIST_BUF, body, "tgb") is None


def test_untagged_list_row_becomes_synthetic_322():
    # WeeChat's own /LIST buffer prints pre-formatted rows with EMPTY tags and no
    # numeric; a channel row (recognised by its prefix) must still be surfaced as a
    # synthetic 322 so discovery collects it, since the tagged form never arrives.
    body = {"message": "#test       1  ", "tags": [], "highlight": False}
    ev = parse_line(LIST_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.kind == "server" and ev.numeric == 322
    assert ev.server == "libera" and ev.text.strip().startswith("#test")


def test_untagged_list_header_is_dropped():
    # the "Receiving list of channels..." header carries no channel prefix.
    body = {"message": "Receiving list of channels, please wait...", "tags": [],
            "highlight": False}
    assert parse_line(LIST_BUF, body, "tgb") is None


def test_list_server_derived_from_buffer_name_when_var_absent():
    # local_variables usually carry server, but if absent it is derived from the
    # "irc.list_<server>" buffer name, matching the plain name mark_discover uses.
    buf = {"name": "irc.list_libera", "type": "list", "server": "",
           "conversation": ""}
    body = {"message": "#tldev 42 :dev", "tags": ["irc_322", "irc_numeric"],
            "highlight": False}
    ev = parse_line(buf, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.server == "libera"


def test_buffer_info_derives_list_type_and_server_from_name():
    from tgbridge.ircbackend import WeechatIrcBackend
    # a list buffer whose local_variables omit type and server: both are filled
    # in from the "irc.list_<server>" name prefix.
    raw = {"id": 7, "name": "irc.list_libera", "short_name": "list_libera",
           "local_variables": {}}
    info = WeechatIrcBackend._buffer_info(raw)
    assert info["type"] == "list" and info["server"] == "libera"


def test_event_carries_acting_nick():
    # the acting nick is exposed on the event so the ignore list can match it.
    body = {"message": "bob (bob@host) has joined #weechat",
            "tags": ["irc_join", "nick_bob"], "highlight": False}
    ev = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(ev, IrcEvent) and ev.nick == "bob"


def test_reply_tag_parsed_in_any_form():
    for tag in ("irc_tag_+draft/reply=abc", "+draft/reply=abc",
                "irc_tag_+reply=abc", "+reply=abc"):
        body = {"message": "hi", "tags": ["irc_privmsg", "nick_bob", tag],
                "highlight": False}
        m = parse_line(CHAN_BUF, body, "tgb")
        assert m.reply_to_msgid == "abc", tag


def test_no_reply_tag_leaves_none():
    body = {"message": "hi", "tags": ["irc_privmsg", "nick_bob"], "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb").reply_to_msgid is None


def test_reaction_extracted_in_any_tag_form():
    # a TAGMSG reaction: the emoji comes from +draft/react=, the target from the
    # reply tag; tolerate the plain and irc_tag_ prefixed forms of each.
    for react in ("irc_tag_+draft/react=👍", "+draft/react=👍", "draft/react=👍"):
        body = {"message": "", "tags": ["irc_tagmsg", "nick_bob", react,
                                        "+draft/reply=abc"], "highlight": False}
        r = parse_line(CHAN_BUF, body, "tgb")
        assert isinstance(r, IrcReaction), react
        assert r.emoji == "👍" and r.target_msgid == "abc"
        assert r.nick == "bob" and r.server == "lt"
        assert r.buffer == "irc.lt.#weechat" and r.is_self is False


def test_reaction_without_target_returns_none():
    # a reaction tag with no reply target has nothing to attach to: dropped.
    body = {"message": "", "tags": ["irc_tagmsg", "nick_bob", "+draft/react=🔥"],
            "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb") is None


def test_self_reaction_flagged():
    # our own reaction, echoed back by the server, is marked is_self.
    body = {"message": "", "tags": ["irc_tagmsg", "nick_tgb", "self_msg",
                                    "+draft/react=👍", "+draft/reply=abc"],
            "highlight": False}
    r = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(r, IrcReaction) and r.is_self is True
    # nick fallback works too, without the self_msg tag
    body2 = {"message": "", "tags": ["irc_tagmsg", "nick_tgb",
                                     "+draft/react=👍", "+draft/reply=abc"],
             "highlight": False}
    assert parse_line(CHAN_BUF, body2, "tgb").is_self is True


def test_plain_message_is_not_a_reaction_or_redact():
    body = {"message": "hi", "tags": ["irc_privmsg", "nick_bob"], "highlight": False}
    m = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(m, IrcMessage)   # neither a reaction nor a redact


def test_redact_extracted_from_delete_tag():
    for delete in ("irc_tag_+draft/delete=xyz", "+draft/delete=xyz",
                   "draft/delete=xyz"):
        body = {"message": "", "tags": ["irc_tagmsg", "nick_op", delete],
                "highlight": False}
        red = parse_line(CHAN_BUF, body, "tgb")
        assert isinstance(red, IrcRedact), delete
        assert red.target_msgid == "xyz" and red.buffer == "irc.lt.#weechat"
        assert red.server == "lt"


def test_redact_extracted_from_redact_command():
    # REDACT <target> <msgid> [:reason]: the msgid is the second token.
    body = {"message": "#weechat delme :spam", "tags": ["irc_redact", "nick_op"],
            "highlight": False}
    red = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(red, IrcRedact) and red.target_msgid == "delme"


def test_redact_command_with_verb_kept_on_the_line():
    # If WeeChat leaves the REDACT verb on the relayed line, the target is still
    # skipped and the msgid (the argument after the target) is read correctly.
    body = {"message": "REDACT #weechat delme :spam",
            "tags": ["irc_redact", "nick_op"], "highlight": False}
    red = parse_line(CHAN_BUF, body, "tgb")
    assert isinstance(red, IrcRedact) and red.target_msgid == "delme"


def test_no_reaction_or_redact_tag_leaves_normal_parsing():
    # a bare TAGMSG-like line with none of the draft tags is dropped as noise,
    # not turned into a reaction or a redact.
    body = {"message": "", "tags": ["irc_tagmsg", "nick_bob"], "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb") is None


def test_connected_servers_reads_nick_on_server_buffers():
    # one registered server buffer (has a nick), one still connecting (no nick),
    # plus channel/list buffers that never signal connectedness.
    infos = [
        {"type": "server", "server": "lt", "nick": "tgb"},
        {"type": "server", "server": "other", "nick": None},
        {"type": "channel", "server": "lt", "nick": "tgb"},
        {"type": "list", "server": "lt", "nick": None},
    ]
    assert connected_servers(infos) == {"lt"}


def test_connected_servers_ignores_empty_nick_and_blank_server():
    infos = [
        {"type": "server", "server": "a", "nick": ""},      # empty nick: not registered
        {"type": "server", "server": "", "nick": "tgb"},    # no server name: skip
    ]
    assert connected_servers(infos) == set()


class _FakeDb:
    def __init__(self, names, statuses=None):
        self._names = list(names)
        # Stored status per server, so a server mid-connect can be represented.
        self._statuses = dict(statuses or {})
        self.status: dict[str, str] = {}

    def list_servers(self):
        return [{"name": n, "status": self._statuses.get(n)} for n in self._names]

    def set_server_status(self, name, status):
        self.status[name] = status


def test_reconcile_sets_connected_and_disconnected_for_known_servers():
    db = _FakeDb(["lt", "other"])
    reconcile_server_status(db, {"lt"})
    assert db.status == {"lt": "connected", "other": "disconnected"}


def test_reconcile_ignores_servers_not_in_db():
    # "ghost" is connected in WeeChat but unknown to the db: it is never touched.
    db = _FakeDb(["lt"])
    reconcile_server_status(db, {"lt", "ghost"})
    assert db.status == {"lt": "connected"}
    assert "ghost" not in db.status


def test_reconcile_leaves_a_connecting_server_untouched():
    # "pending" is mid-connect (buffer nick not populated yet). A relay reconnect
    # firing reconcile must not clobber it to "disconnected"; the 001/timeout
    # machinery owns that transient state. "lt" is still reconciled as usual.
    db = _FakeDb(["lt", "pending"], statuses={"pending": "connecting"})
    reconcile_server_status(db, {"lt"})
    assert db.status == {"lt": "connected"}
    assert "pending" not in db.status


def test_backend_connected_servers_uses_listed_buffers():
    from tgbridge.ircbackend import WeechatIrcBackend
    backend = WeechatIrcBackend(relay=None)
    backend._buffers = {
        1: {"type": "server", "server": "lt", "nick": "tgb"},
        2: {"type": "server", "server": "other", "nick": None},
        3: {"type": "channel", "server": "lt", "nick": "tgb"},
    }
    assert backend.connected_servers() == {"lt"}


def test_typing_tagmsg_parsed():
    from tgbridge.ircbackend import IrcTyping
    for tag in ("irc_tag_+typing=active", "+typing=active", "draft/typing=active"):
        body = {"message": "", "tags": ["irc_tagmsg", "nick_bob", tag], "highlight": False}
        tp = parse_line(CHAN_BUF, body, "tgb")
        assert isinstance(tp, IrcTyping) and tp.state == "active" and tp.nick == "bob", tag
        assert tp.is_self is False
    # our own typing is flagged so the router can drop it
    body = {"message": "", "tags": ["irc_tagmsg", "nick_tgb", "+typing=active"],
            "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb").is_self is True
    # done state parses too
    body = {"message": "", "tags": ["irc_tagmsg", "nick_bob", "+typing=done"],
            "highlight": False}
    assert parse_line(CHAN_BUF, body, "tgb").state == "done"


def test_build_chathistory_request_after_with_last_seen():
    from tgbridge.ircbackend import build_chathistory_request
    line = build_chathistory_request("#weechat", "abc123")
    assert line == "/quote CHATHISTORY AFTER #weechat msgid=abc123 100"


def test_build_chathistory_request_latest_without_last_seen():
    from tgbridge.ircbackend import build_chathistory_request
    # both None and empty string mean "no high-water mark yet" -> LATEST
    for empty in (None, ""):
        line = build_chathistory_request("#weechat", empty)
        assert line == "/quote CHATHISTORY LATEST #weechat * 100", empty


def test_build_chathistory_request_works_for_a_pm_target():
    from tgbridge.ircbackend import build_chathistory_request
    assert build_chathistory_request("bob", "m1") == \
        "/quote CHATHISTORY AFTER bob msgid=m1 100"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
