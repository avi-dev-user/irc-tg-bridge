"""Tests for the IRC<->Telegram router, with fakes for Telegram and IRC."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.db import Database  # noqa: E402
from tgbridge.i18n import Translator  # noqa: E402
from tgbridge.ircbackend import (  # noqa: E402
    IrcEvent, IrcMessage, IrcReaction, IrcRedact, IrcTyping)
from tgbridge.router import Router, build_react_quote  # noqa: E402

_LOCALES = os.path.join(os.path.dirname(__file__), "..", "locales")


def _tr():
    return Translator(_LOCALES)


async def _ok_upload(path):
    return "https://gofile.io/d/abc123"


async def _boom_upload(path):
    raise RuntimeError("gofile is down")


class FakeGateway:
    chat_id = 777
    owner_bot = "primary"

    def __init__(self):
        self.sent = []
        self.created = []
        self.created_owners = []
        self.send_owners = []
        self.edited = []
        self.reactions = []
        self.deleted = []
        self.closed = []
        self.reopened = []
        self.typing = []
        self._topic = 100
        self._msg = 500

    async def create_topic(self, title, owner_bot=None):
        self._topic += 1
        self.created.append((self._topic, title))
        self.created_owners.append(owner_bot)
        return self._topic

    async def send(self, topic_id, html, reply_to_message_id=None, owner_bot=None):
        self._msg += 1
        self.sent.append((topic_id, html, reply_to_message_id))
        self.send_owners.append((topic_id, owner_bot))
        return self._msg

    async def edit_message(self, message_id, html, owner_bot=None):
        self.edited.append((message_id, html, owner_bot))

    async def react(self, message_id, emoji):
        self.reactions.append((message_id, emoji))

    async def delete_message(self, message_id):
        self.deleted.append(message_id)

    async def close_topic(self, topic_id):
        self.closed.append(topic_id)

    async def reopen_topic(self, topic_id):
        self.reopened.append(topic_id)

    async def send_typing(self, topic_id):
        self.typing.append(topic_id)


class FakeIrc:
    def __init__(self):
        self.messages = []
        self.commands = []
        self.connected = set()
        self.nick = ""

    async def send_message(self, buffer, text):
        self.messages.append((buffer, text))

    async def send_command(self, buffer, command):
        self.commands.append((buffer, command))

    def connected_servers(self):
        return self.connected

    def nick_for(self, server):
        return self.nick


def _db():
    return Database(os.path.join(tempfile.mkdtemp(), "b.db"))


def chan_msg(**kw):
    d = dict(server="lt", buffer="irc.lt.#weechat", conversation="#weechat",
             nick="alice", text="hi", is_private=False)
    d.update(kw)
    return IrcMessage(**d)


def run(coro):
    # asyncio.run cancels any leftover debounce tasks and closes the loop, so no
    # unclosed loop is left for the GC to finalize (which prints a spurious
    # "Invalid file descriptor" traceback at interpreter shutdown).
    return asyncio.run(coro)


def test_loop_prevention_self_message_not_forwarded():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(is_self=True)))
    assert gw.sent == [] and gw.created == []


def test_remote_message_creates_topic_and_records():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hello", msgid="m1")))
    assert len(gw.created) == 1
    topic_id, title = gw.created[0]
    assert "#weechat" in title
    assert len(gw.sent) == 1
    assert "<b>alice</b>: hello" == gw.sent[0][1]
    # mapping + message recorded
    assert db.topic_for_buffer("irc.lt.#weechat")["topic_id"] == topic_id
    rec = db.message_by_msgid("irc.lt.#weechat", "m1")
    assert rec and rec["tg_chat_id"] == 777 and rec["owner_bot"] == "primary"
    assert rec["nick"] == "alice"


def test_existing_topic_reused_no_duplicate():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(text="one", msgid="a")))
    run(r.handle_irc(chan_msg(text="two", msgid="b")))
    assert len(gw.created) == 1  # topic created once
    assert len(gw.sent) == 2


def test_private_message_has_no_nick_prefix():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(buffer="irc.lt.bob", conversation="bob",
                              is_private=True, nick="bob", text="psst")))
    assert gw.sent[0][1] == "psst"


def test_action_rendered():
    # weechat's action line already includes the actor's nick in the text
    # ("bob waves"), so it is rendered as-is under the "*", not doubled.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="bob", text="bob waves", is_action=True)))
    assert gw.sent[0][1] == "<i>* bob waves</i>"


def test_highlight_channel_message_gets_bell_marker_and_bold_nick():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hey you", highlight=True)))
    assert gw.sent[0][1] == "🔔 <b>alice</b>: hey you"


def test_non_highlight_channel_message_has_no_marker():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hi", highlight=False)))
    assert gw.sent[0][1] == "<b>alice</b>: hi"
    assert "🔔" not in gw.sent[0][1]


def test_highlight_private_message_has_no_marker():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(buffer="irc.lt.bob", conversation="bob",
                              is_private=True, nick="bob", text="psst",
                              highlight=True)))
    assert gw.sent[0][1] == "psst"
    assert "🔔" not in gw.sent[0][1]


def test_highlight_uses_plain_username_mention_when_available():
    # a public @username is placed as plain text so Telegram notifies server-side;
    # a tg://user link would need the bot to have "met" the user first, which
    # fails for a member it has only seen in the group.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, mention_user_id=555, mention_label="@avi")
    run(r.handle_irc(chan_msg(nick="bob", text="hey you", highlight=True)))
    html = gw.sent[0][1]
    assert "🔔 @avi" in html                 # plain @username -> reliable ping
    assert "tg://user" not in html           # no fragile id-based link needed
    assert "<b>bob</b>: hey you" in html


def test_highlight_marker_is_plain_bell_without_mention_id():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)   # no mention_user_id configured
    run(r.handle_irc(chan_msg(nick="bob", text="hey you", highlight=True)))
    assert gw.sent[0][1] == "🔔 <b>bob</b>: hey you"
    assert "tg://user" not in gw.sent[0][1]


def test_highlight_nick_bolded_and_marker_links_when_only_id():
    # with only an id (no public @username), the nick is bolded in the text and
    # the bell marker falls back to a best-effort tg://user link.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    irc.nick = "mynick"
    r = Router(db, gw, irc, mention_user_id=555)   # no @username label
    run(r.handle_irc(chan_msg(nick="bob", text="hey mynick, ping", highlight=True)))
    html = gw.sent[0][1]
    assert "<b>mynick</b>" in html                  # nick highlighted in the text
    assert 'href="tg://user?id=555"' in html         # bell is the id fallback link


def test_highlight_nick_in_text_bolds_without_mention_id():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    irc.nick = "mynick"
    r = Router(db, gw, irc)   # no mention user configured
    run(r.handle_irc(chan_msg(nick="bob", text="yo mynick", highlight=True)))
    html = gw.sent[0][1]
    assert "<b>mynick</b>" in html and "tg://user" not in html


def test_highlight_nick_is_whole_word_case_insensitive():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    irc.nick = "cat"
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="bob", text="CAT and category", highlight=True)))
    html = gw.sent[0][1]
    assert "<b>CAT</b>" in html                         # matched, original case kept
    assert "category" in html and "<b>category</b>" not in html   # not a substring


def emit_flush(r, item):
    # events are coalesced with a debounce, so drive + flush in one loop
    async def go():
        await r.handle_irc(item)
        await r.flush()
    run(go())


def test_join_event_muted_by_default():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")  # default noise_filter join,part,quit
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                           text="bob joined"))
    assert gw.sent == []


def test_kick_affecting_me_always_shown_and_styled():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="kick",
                           text="tgb was kicked", affects_me=True))
    assert len(gw.sent) == 1
    assert gw.sent[0][1] == "<b><i>tgb was kicked</i></b>"


def test_channel_opened_lifecycle_reopens_topic_and_emits_join():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                           text="Joined #weechat", affects_me=True,
                           lifecycle="opened"))
    topic_id = gw.created[0][0]
    assert gw.reopened == [topic_id]
    assert len(gw.sent) == 1
    assert gw.sent[0][1] == "<b><i>Joined #weechat</i></b>"
    assert gw.closed == []


def test_private_open_ensures_topic_without_joined_line():
    # /query <nick> opens a PM buffer; the router creates its topic proactively
    # (title "server . nick") but posts no channel-style "Joined" line.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.alice", kind="private",
                           text="", lifecycle="opened"))
    assert len(gw.created) == 1
    topic_id, title = gw.created[0]
    assert title == "lt · alice"
    assert db.topic_for_buffer("irc.lt.alice")["topic_id"] == topic_id
    assert gw.sent == []                 # no announcement body for a PM open
    assert gw.reopened == [topic_id]     # opened lifecycle still un-closes it


def test_private_open_reuses_existing_topic():
    # a re-query of the same nick must not spawn a second PM topic.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    ev = IrcEvent(server="lt", buffer="irc.lt.alice", kind="private",
                  text="", lifecycle="opened")
    emit_flush(r, ev)
    emit_flush(r, ev)
    assert len(gw.created) == 1
    assert gw.sent == []


def test_channel_closed_lifecycle_sends_notice_then_closes_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    run(r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="part",
                              text="No longer in #weechat", affects_me=True,
                              lifecycle="closed")))
    topic_id = gw.created[0][0]
    assert gw.sent == [(topic_id, "<i>No longer in #weechat</i>", None)]
    assert gw.closed == [topic_id]
    assert gw.reopened == []


def test_channel_closed_lifecycle_flushes_pending_before_notice():
    # a kick reason queued just before the close must land ahead of the notice.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat",
                                    kind="kick", text="kicked (spam)",
                                    affects_me=True))
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat",
                                    kind="part", text="No longer in #weechat",
                                    affects_me=True, lifecycle="closed"))
    run(go())
    topic_id = gw.created[0][0]
    bodies = [html for tid, html, _ in gw.sent if tid == topic_id]
    assert bodies == ["<b><i>kicked (spam)</i></b>", "<i>No longer in #weechat</i>"]
    assert gw.closed == [topic_id]


def test_server_event_goes_to_server_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="SASL ok"))
    tid = db.topic_for_buffer("irc.server.lt")
    assert tid is not None and tid["topic_id"] != 9   # its own topic, not console
    assert gw.sent[0][0] == tid["topic_id"]


def test_server_buffer_mode_event_uses_server_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    # a "User mode +x" line is kind=mode but sits on the server buffer
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="mode",
                           text="User mode [+x]"))
    assert db.topic_for_buffer("irc.server.lt") is not None
    assert gw.sent[0][0] != 9      # server topic, not the console


def test_events_coalesced_into_one_message():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)

    async def go():
        for line in ["line one", "line two", "line three"]:
            await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                        kind="server", text=line))
        await r.flush()
    run(go())
    assert len(gw.sent) == 1                       # three lines -> one message
    assert gw.sent[0][1].count("\n") == 2


def test_telegram_to_irc_message_and_command():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 7, "hello channel"))
    run(r.handle_telegram(42, 8, "/topic new topic"))
    assert irc.messages == [("irc.lt.#weechat", "hello channel")]
    assert irc.commands == [("irc.lt.#weechat", "/topic new topic")]
    # a plain message is marked delivered; a command shows "working" until its
    # reply (none is driven here, so it stays on the working mark)
    assert (7, "👍") in gw.reactions and (8, "👀") in gw.reactions


def test_telegram_multiline_becomes_two_irc_sends_in_order():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 7, "first line\nsecond line"))
    assert irc.messages == [
        ("irc.lt.#weechat", "first line"),
        ("irc.lt.#weechat", "second line"),
    ]
    assert (7, "👍") in gw.reactions


def test_telegram_to_irc_failure_marked():
    class Boom(FakeIrc):
        async def send_message(self, buffer, text):
            raise RuntimeError("irc down")
    gw, irc, db = FakeGateway(), Boom(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "will fail"))
    assert (9, "👎") in gw.reactions


def test_multiline_command_runs_each_line():
    # a pasted block of commands (one per line) must all run, not just the first.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/nick newnick\n/msg NickServ REGISTER pw mail"))
    assert ("irc.lt.#weechat", "/nick newnick") in irc.commands
    assert ("irc.lt.#weechat", "/msg NickServ REGISTER pw mail") in irc.commands


def test_chathistory_unknown_command_is_suppressed():
    # our internal CHATHISTORY probe errors on a server without it; do not show it.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                                    text="CHATHISTORY :Unknown command", numeric=421))
        await r.flush()
    run(go())
    assert gw.sent == []


def test_real_unknown_command_is_still_shown():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                                    text="FROBNICATE :Unknown command", numeric=421))
        await r.flush()
    run(go())
    assert gw.sent != []   # a genuine unknown command is surfaced to the user


def test_notice_burst_is_coalesced_into_one_message():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.NickServ", 30, "primary")
    r = Router(db, gw, irc)

    async def go():
        for line in ("line one", "line two", "line three"):
            await r.handle_irc(chan_msg(buffer="irc.lt.NickServ", conversation="NickServ",
                                        is_private=True, is_notice=True,
                                        nick="NickServ", text=line))
        await r.flush()
    run(go())
    assert len(gw.sent) == 1   # a service burst is one message, not three
    body = gw.sent[0][1]
    assert "line one" in body and "line two" in body and "line three" in body


def test_command_reply_routed_to_origin_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 1, "/whois bob"))   # command typed in topic 42
    gw.sent.clear()
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="bob is a real user"))
    # the whois reply lands in the origin topic (42), not the console (9)
    assert gw.sent[0][0] == 42


def test_expect_reply_in_routes_reply_to_named_topic():
    # a panel command (Topic/Who) never passes through handle_telegram, so the
    # manager registers the target topic directly; the next server reply for the
    # server must land there rather than in the server status topic.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 55, "primary")
    r = Router(db, gw, irc)
    r.expect_reply_in("lt", 55)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="No topic is set for #weechat"))
    assert gw.sent[0][0] == 55   # the channel topic, not a fresh server topic


def test_welcome_numeric_marks_connected_and_fires_callback():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    assert db.get_server("lt")["status"] == "disconnected"
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="Welcome to the network", numeric=1))
    assert db.get_server("lt")["status"] == "connected"
    assert seen == [("lt", "connected")]


def test_non_welcome_server_line_does_not_mark_connected():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="MOTD line", numeric=372))
    assert db.get_server("lt")["status"] == "disconnected"
    assert seen == []


def test_connect_timeout_marks_failed_when_no_welcome():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_server_status("lt", "connecting")
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    r._connect_timeout = 0.0

    async def go():
        r.arm_connect_timeout("lt")
        await asyncio.sleep(0.02)   # let the armed timer run
    run(go())
    assert db.get_server("lt")["status"] == "disconnected"
    assert seen == [("lt", "failed")]


def test_connect_timeout_reconciles_when_weechat_already_connected():
    # a reconnect on a server weechat already holds open gets "already connected"
    # and no fresh welcome; the timer must trust weechat's state and resolve to
    # connected instead of mislabelling a live server as disconnected.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_server_status("lt", "connecting")
    irc.connected = {"lt"}
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    r._connect_timeout = 0.0

    async def go():
        r.arm_connect_timeout("lt")
        await asyncio.sleep(0.02)
    run(go())
    assert db.get_server("lt")["status"] == "connected"
    assert seen == [("lt", "connected")]


def test_typed_list_off_list_buffer_resolves_to_picker():
    # WeeChat's /LIST buffer streams untagged rows and sends no RPL_LISTEND, so a
    # /list typed in a topic (no explicit discovery armed) must still collect the
    # rows off the list buffer and fire the picker once they settle (debounce).
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    got = []

    async def on_list(server, channels):
        got.append((server, channels))

    r = Router(db, gw, irc)
    r.set_channel_list_callback(on_list)
    r._list_debounce = 0.0
    r._connect_timeout = 60.0

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.list_lt",
                                    kind="server", text="#weechat 42 :chat", numeric=322))
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.list_lt",
                                    kind="server", text="#test 1 :", numeric=322))
        await asyncio.sleep(0.03)   # let the debounce fire
    run(go())
    assert len(got) == 1
    server, channels = got[0]
    assert server == "lt"
    assert [c["channel"] for c in channels] == ["#weechat", "#test"]  # by users desc


def test_welcome_cancels_connect_timeout():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_server_status("lt", "connecting")
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    r._connect_timeout = 0.05

    async def go():
        r.arm_connect_timeout("lt")
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="Welcome", numeric=1))
        await asyncio.sleep(0.1)   # past the timeout window: it must not fire
        await r.flush()
    run(go())
    assert db.get_server("lt")["status"] == "connected"
    assert seen == [("lt", "connected")]   # never a spurious "failed"
    assert "lt" not in r._connect_tasks


def test_connect_timeout_leaves_a_server_no_longer_connecting():
    # if the server left the connecting state before the timer fires (a manual
    # disconnect, or a success), the timer must not overwrite that state.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_server_status("lt", "connected")
    seen = []

    async def on_status(server, status):
        seen.append((server, status))

    r = Router(db, gw, irc)
    r.set_server_status_callback(on_status)
    r._connect_timeout = 0.0

    async def go():
        r.arm_connect_timeout("lt")
        await asyncio.sleep(0.02)
    run(go())
    assert db.get_server("lt")["status"] == "connected"   # untouched
    assert seen == []


def test_server_buffer_message_routed_to_server_topic():
    # a NickServ/services reply arrives as a message on the server buffer; it
    # goes to the server's own status topic, not a channel/PM topic.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcMessage(server="lt", buffer="irc.server.lt", conversation="lt",
                             nick="NickServ", text="You are now identified",
                             is_private=False, is_notice=True))
    tid = db.topic_for_buffer("irc.server.lt")
    assert tid is not None and tid["topic_id"] != 9      # its own topic, not console
    assert gw.created[0][1] == "⚙ lt"                    # the server-status title
    assert gw.sent and gw.sent[0][0] == tid["topic_id"]
    assert "identified" in gw.sent[0][1]


def test_bare_nick_command_reply_fills_replied_to_nick():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    db.record_message(buffer="irc.lt.#weechat", tg_chat_id=gw.chat_id,
                      tg_message_id=500, owner_bot="primary", nick="alice")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/whois", reply_to=500))
    assert irc.commands == [("irc.lt.#weechat", "/whois alice")]
    assert (9, "👀") in gw.reactions   # a command shows "working" until its reply


def test_command_working_reaction_flips_to_done_on_reply():
    # a command shows "working" (eyes) and flips to done once its server reply
    # comes back, so the user sees the command was actually answered.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    r.set_channel_list_callback(lambda *a: asyncio.sleep(0))
    r._cmd_ack_timeout = 60.0   # keep the fallback from firing during the test
    r._list_debounce = 0.0

    async def go():
        await r.handle_telegram(42, 9, "/list")
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.list_lt", kind="server",
                                    text="#weechat 3 :x", numeric=322))
        await asyncio.sleep(0.03)   # debounce -> list reply -> ack flip
    run(go())
    assert (9, "👀") in gw.reactions   # working shown first
    assert (9, "👌") in gw.reactions   # flipped to done when the reply arrived


def test_command_ack_armed_before_the_command_is_sent():
    # a fast server can answer before the slow react() returns, so the ack must
    # already be armed by the time the command goes out or the reply races past
    # the arming and only the timeout would flip it.
    gw, db = FakeGateway(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    seen = {}

    class FastIrc(FakeIrc):
        async def send_command(self, buffer, command):
            seen["armed"] = "lt" in r._cmd_ack and 9 in r._cmd_ack["lt"]
            await super().send_command(buffer, command)

    irc = FastIrc()
    r = Router(db, gw, irc)
    r._cmd_ack_timeout = 60.0
    run(r.handle_telegram(42, 9, "/whois someone"))
    assert seen.get("armed") is True   # armed before the command was sent


def test_command_ack_flips_on_channel_mode_echo():
    # /mode's reply is a MODE echo rendered in the channel branch, not a server
    # numeric; it must still flip the command's "working" reaction to done.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    r._cmd_ack_timeout = 60.0   # prove the flip is the echo, not the timeout

    async def go():
        await r.handle_telegram(42, 9, "/mode #weechat -m")
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat",
                                    kind="mode", text="#weechat [-m] by mynick"))
        await r.flush()
    run(go())
    assert (9, "👀") in gw.reactions   # working shown first
    assert (9, "👌") in gw.reactions   # flipped by the mode echo


def test_command_ack_not_flipped_by_ambient_join():
    # an unrelated join while a command is pending must not flip it early.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    r._cmd_ack_timeout = 60.0

    async def go():
        await r.handle_telegram(42, 9, "/whois nobody")
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat",
                                    kind="join", text="bob joined", nick="bob"))
        await r.flush()
    run(go())
    assert (9, "👀") in gw.reactions
    assert (9, "👌") not in gw.reactions   # still working: a join is not its reply


def test_two_commands_same_server_both_flip_to_done():
    # two commands sent close together on one server must both resolve, not leave
    # the first stuck on "working" (the ack is queued per server, not replaced).
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    r._cmd_ack_timeout = 60.0

    async def go():
        await r.handle_telegram(42, 9, "/mode #weechat +m")
        await r.handle_telegram(42, 10, "/mode #weechat -m")
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat",
                                    kind="mode", text="#weechat [-m] by op"))
        await r.flush()
    run(go())
    assert (9, "👌") in gw.reactions and (10, "👌") in gw.reactions


def test_bare_nick_command_round_trips_from_recorded_message():
    # end to end: a message arrives from IRC (recorded with its nick), and a
    # bare reply command resolves the sender via the tg message id.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="carol", text="hi", msgid="m1")))
    topic_id, _ = gw.created[0]
    recorded = db.message_by_msgid("irc.lt.#weechat", "m1")
    run(r.handle_telegram(topic_id, 9, "/msg", reply_to=recorded["tg_message_id"]))
    assert irc.commands == [("irc.lt.#weechat", "/msg carol")]


def test_nick_command_with_explicit_arg_is_unchanged():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    db.record_message(buffer="irc.lt.#weechat", tg_chat_id=gw.chat_id,
                      tg_message_id=500, owner_bot="primary", nick="alice")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/whois bob", reply_to=500))
    assert irc.commands == [("irc.lt.#weechat", "/whois bob")]


def test_bare_nick_command_reply_without_matching_record_is_unchanged():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/whois", reply_to=12345))
    assert irc.commands == [("irc.lt.#weechat", "/whois")]


def test_bare_nick_command_without_reply_is_unchanged():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/whois"))
    assert irc.commands == [("irc.lt.#weechat", "/whois")]


def test_non_nick_command_reply_is_not_filled():
    # /topic is not a nick-command, so a reply must not append a nick to it.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    db.record_message(buffer="irc.lt.#weechat", tg_chat_id=gw.chat_id,
                      tg_message_id=500, owner_bot="primary", nick="alice")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/topic", reply_to=500))
    assert irc.commands == [("irc.lt.#weechat", "/topic")]


def test_bare_nick_command_reply_to_record_without_nick_is_unchanged():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    db.record_message(buffer="irc.lt.#weechat", tg_chat_id=gw.chat_id,
                      tg_message_id=500, owner_bot="primary")  # no nick
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 9, "/kick", reply_to=500))
    assert irc.commands == [("irc.lt.#weechat", "/kick")]


def test_telegram_unmapped_topic_ignored():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_telegram(999, 1, "into the void"))
    assert irc.messages == [] and irc.commands == [] and gw.reactions == []


def test_channel_discovery_collects_sorts_and_caps():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_list(server, channels):
        got.append((server, channels))

    r.set_channel_list_callback(on_list)
    r.mark_discover("lt")

    async def go():
        for i in range(25):   # more than the top-20 cap
            await r.handle_irc(IrcEvent(
                server="lt", buffer="irc.server.lt", kind="server",
                text=f"#chan{i} {i} :topic {i}", numeric=322))
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="End of /LIST", numeric=323))
    run(go())
    assert len(got) == 1
    server, channels = got[0]
    assert server == "lt"
    assert len(channels) == 20                       # capped to the busiest 20
    assert channels[0] == {"channel": "#chan24", "users": 24, "topic": "topic 24"}
    users = [c["users"] for c in channels]
    assert users == sorted(users, reverse=True)      # sorted by user count desc
    assert gw.sent == []                             # collected, not dumped to a topic


def test_channel_discovery_only_collects_when_pending():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_list(server, channels):
        got.append((server, channels))

    r.set_channel_list_callback(on_list)

    # No mark_discover: a 322 line is an ordinary server line and reaches its topic.
    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="#python 10 :Py", numeric=322))
        await r.flush()
    run(go())
    assert got == []
    assert len(gw.sent) == 1


def test_channel_discovery_marker_cleared_after_listend():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_list(server, channels):
        got.append((server, channels))

    r.set_channel_list_callback(on_list)
    r.mark_discover("lt")

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="#a 3 :x", numeric=322))
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="End of /LIST", numeric=323))
        # a later stray 322 (no fresh discover) is no longer captured
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.server.lt",
                                    kind="server", text="#b 9 :y", numeric=322))
        await r.flush()
    run(go())
    assert len(got) == 1                       # only the finished list fired once
    assert [c["channel"] for c in got[0][1]] == ["#a"]
    assert len(gw.sent) == 1                    # the stray 322 went to the topic


def test_list_reply_off_list_buffer_collects_without_explicit_discovery():
    # A /list reply on WeeChat's dedicated "irc.list_<server>" buffer is collected
    # into the channel picker even with no discovery armed (a /list typed in a
    # topic), instead of dumping raw rows or spawning a duplicate list topic.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    got = []

    async def on_list(server, channels):
        got.append((server, channels))

    r = Router(db, gw, irc)
    r.set_channel_list_callback(on_list)
    r._list_debounce = 0.0

    async def go():
        await r.handle_irc(IrcEvent(server="lt", buffer="irc.list_lt", kind="server",
                                    text="#python 10 :Py", numeric=322))
        await asyncio.sleep(0.03)
    run(go())
    assert db.topic_for_buffer("irc.list_lt") is None       # no duplicate topic
    assert len(got) == 1 and [c["channel"] for c in got[0][1]] == ["#python"]
    assert gw.sent == []                                     # collected, not dumped raw


def test_friendly_numeric_for_failed_join_does_not_spawn_channel_topic():
    # a 477 join rejection names a channel we are not in; the warning must not
    # spawn an empty topic for it - it goes to the server topic instead.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="#tldev :You need a registered nick to join",
                           numeric=477))
    assert db.topic_for_buffer("irc.lt.#tldev") is None
    srv = db.topic_for_buffer("irc.server.lt")
    assert srv is not None and len(gw.sent) == 1
    topic_id, html, _ = gw.sent[0]
    assert topic_id == srv["topic_id"]
    assert "⚠" in html and "registered" in html.lower() and "#tldev" in html


def test_friendly_numeric_without_channel_goes_to_server_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="Nickname is already in use", numeric=433))
    tid = db.topic_for_buffer("irc.server.lt")
    assert tid is not None
    assert gw.sent and gw.sent[0][0] == tid["topic_id"]
    assert "in use" in gw.sent[0][1].lower()


def test_friendly_numeric_on_channel_buffer_without_token_stays_in_that_channel():
    # a friendly numeric that rode in on a channel buffer but whose text names no
    # channel must land in that channel's topic, not the server topic.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#tldev", kind="mode",
                           text="You cannot send to that channel", numeric=404))
    tid = db.topic_for_buffer("irc.lt.#tldev")
    assert tid is not None and tid["topic_id"] != 9
    assert len(gw.sent) == 1
    topic_id, html, _ = gw.sent[0]
    assert topic_id == tid["topic_id"]
    assert "⚠" in html


def test_unmapped_numeric_still_uses_raw_handling():
    # a numeric with no friendly mapping (MOTD) is shown as before, unchanged.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="- MOTD line", numeric=372))
    assert len(gw.sent) == 1
    assert gw.sent[0][1] == "<i>- MOTD line</i>"   # plain raw render, no warning
    assert "⚠" not in gw.sent[0][1]


def _whois(server, numeric, text):
    return IrcEvent(server=server, buffer=f"irc.server.{server}", kind="server",
                    text=text, numeric=numeric)


def feed_flush(r, items):
    async def go():
        for it in items:
            await r.handle_irc(it)
        await r.flush()
    run(go())


def test_whois_numerics_folded_into_one_card():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    feed_flush(r, [
        _whois("lt", 311, "bob ~buser host.example.com * :Bob The Realname"),
        _whois("lt", 312, "bob irc.example.net :Example Network"),
        _whois("lt", 313, "bob :is an IRC operator"),
        _whois("lt", 317, "bob 42 1700000000 :seconds idle, signon time"),
        _whois("lt", 319, "bob :@#chan1 +#chan2 #chan3"),
        _whois("lt", 330, "bob bobaccount :is logged in as"),
        _whois("lt", 671, "bob :is using a secure connection"),
        _whois("lt", 318, "bob :End of /WHOIS list"),
    ])
    assert len(gw.sent) == 1                       # every numeric -> one card
    topic_id, html, _ = gw.sent[0]
    tid = db.topic_for_buffer("irc.server.lt")
    assert tid is not None and topic_id == tid["topic_id"]   # server topic, not console
    assert "<b>bob</b>" in html
    assert "host.example.com" in html
    assert "#chan1" in html
    assert "Bob The Realname" in html
    assert "42s" in html                           # idle humanised
    assert "irc.example.net" in html               # 312 server line
    assert "bobaccount" in html                    # 330 account
    assert "operator" in html.lower()              # 313 operator flag
    assert "secure" in html.lower()                # 671 secure connection


def test_fmt_idle_scales_across_units():
    from tgbridge.router import _fmt_idle
    assert _fmt_idle(42) == "42s"
    assert _fmt_idle(90) == "1m 30s"
    assert _fmt_idle(3700) == "1h 1m"
    assert _fmt_idle(90000) == "1d 1h"


def test_lone_whois_end_emits_nothing():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    feed_flush(r, [_whois("lt", 318, "bob :End of /WHOIS list")])
    assert gw.sent == []
    assert gw.created == []


def test_whois_card_routed_to_command_origin_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc)
    run(r.handle_telegram(42, 1, "/whois bob"))    # command typed in topic 42
    gw.sent.clear()
    feed_flush(r, [
        _whois("lt", 311, "bob ~buser host.example.com * :Bob"),
        _whois("lt", 319, "bob :#chan1"),
        _whois("lt", 318, "bob :End of /WHOIS list"),
    ])
    assert len(gw.sent) == 1
    assert gw.sent[0][0] == 42                      # card lands in the origin topic


def test_interleaved_whois_kept_separate_by_nick():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    feed_flush(r, [
        _whois("lt", 311, "alice ~a ahost.net * :Alice"),
        _whois("lt", 311, "bob ~b bhost.net * :Bob"),
        _whois("lt", 319, "alice :#alicechan"),
        _whois("lt", 319, "bob :#bobchan"),
        _whois("lt", 318, "alice :End of /WHOIS list"),
        _whois("lt", 318, "bob :End of /WHOIS list"),
    ])
    # both cards land in the one server topic and coalesce into a single send;
    # what matters is that fields never cross between the two nicks.
    assert len(gw.sent) == 1
    blob = gw.sent[0][1]
    cards = [c for c in blob.split("<b>") if c]
    assert len(cards) == 2
    alice = next(c for c in cards if c.startswith("alice</b>"))
    bob = next(c for c in cards if c.startswith("bob</b>"))
    assert "ahost.net" in alice and "#alicechan" in alice
    assert "bhost.net" not in alice and "#bobchan" not in alice
    assert "bhost.net" in bob and "#bobchan" in bob
    assert "ahost.net" not in bob and "#alicechan" not in bob


def test_whois_numerics_do_not_reach_raw_handling():
    # a whois numeric with no 318 yet must not emit any raw server line.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    feed_flush(r, [_whois("lt", 311, "bob ~b bhost.net * :Bob")])
    assert gw.sent == []                            # buffered, nothing dumped raw


def _names_ev(server, numeric, text):
    return IrcEvent(server=server, buffer=f"irc.server.{server}", kind="server",
                    text=text, numeric=numeric)


def test_names_collection_emits_one_user_list_on_endofnames():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_names(server, channel, users):
        got.append((server, channel, users))

    r.set_names_callback(on_names)
    r.mark_names("lt")
    feed_flush(r, [
        _names_ev("lt", 353, "= #weechat :@alice %helper +carol dave"),
        _names_ev("lt", 353, "= #weechat :+erin frank"),   # a second 353 accrues
        _names_ev("lt", 366, "#weechat :End of /NAMES list"),
    ])
    assert len(got) == 1                       # one list, on the terminator only
    server, channel, users = got[0]
    assert server == "lt" and channel == "#weechat"
    assert [u["nick"] for u in users] == \
        ["alice", "helper", "carol", "dave", "erin", "frank"]
    assert users[0] == {"prefix": "@", "nick": "alice"}
    assert users[3] == {"prefix": "", "nick": "dave"}
    assert gw.sent == []                       # collected, not dumped to a topic


def test_names_not_collected_without_mark():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_names(server, channel, users):
        got.append((server, channel, users))

    r.set_names_callback(on_names)
    # no mark_names: a 353 is an ordinary server line and reaches its topic
    feed_flush(r, [_names_ev("lt", 353, "= #weechat :@alice")])
    assert got == []
    assert len(gw.sent) == 1


def test_names_marker_cleared_after_endofnames():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_names(server, channel, users):
        got.append((server, channel, users))

    r.set_names_callback(on_names)
    r.mark_names("lt")
    feed_flush(r, [
        _names_ev("lt", 353, "= #a :alice"),
        _names_ev("lt", 366, "#a :End of /NAMES list"),
        # a later stray 353 (no fresh mark) is no longer captured
        _names_ev("lt", 353, "= #b :bob"),
    ])
    assert len(got) == 1
    assert got[0][1] == "#a"
    assert len(gw.sent) == 1                    # the stray 353 went to the topic


def test_names_collection_caps_large_membership():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    got = []

    async def on_names(server, channel, users):
        got.append((server, channel, users))

    r.set_names_callback(on_names)
    r.mark_names("lt")
    big = " ".join(f"user{i}" for i in range(150))
    feed_flush(r, [
        _names_ev("lt", 353, f"= #big :{big}"),
        _names_ev("lt", 366, "#big :End of /NAMES list"),
    ])
    assert len(got) == 1
    _server, channel, users = got[0]
    assert channel == "#big"
    assert len(users) == 100                    # capped to the first 100 members


def test_arm_connect_clears_stale_names_buffers():
    # a NAMES burst whose 366 never arrived must not leak into the next connect.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    r._connect_timeout = 60.0
    r._names_pending.add("lt")
    r._names_buffer["lt"] = {"#x": [{"prefix": "", "nick": "a"}]}

    async def go():
        r.arm_connect_timeout("lt")
        await r.flush()
    run(go())
    assert "lt" not in r._names_pending
    assert "lt" not in r._names_buffer


def _welcome(server="lt"):
    return IrcEvent(server=server, buffer=f"irc.server.{server}", kind="server",
                    text="Welcome to the network", numeric=1)


def test_welcome_runs_perform_then_rejoins_known_channels():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")   # autojoin on by default
    db.set_perform("lt", "/msg InviteBot !invite KEY")
    db.set_mapping("irc.server.lt", 1, "primary")     # server buffer, not a channel
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    db.set_mapping("irc.lt.&local", 3, "primary")
    db.set_mapping("irc.lt.alice", 4, "primary")      # PM, not a channel
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    # perform runs first (so an invite lands before we try the channel), then
    # one /join per known channel, all on the server buffer.
    assert irc.commands == [
        ("irc.server.lt", "/msg InviteBot !invite KEY"),
        ("irc.server.lt", "/join #weechat"),
        ("irc.server.lt", "/join &local"),
    ]
    assert db.get_server("lt")["status"] == "connected"


def test_welcome_autojoin_off_still_runs_perform_but_skips_joins():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_autojoin("lt", False)
    db.set_perform("lt", "/oper me pw")
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    assert irc.commands == [("irc.server.lt", "/oper me pw")]   # no /join


def test_welcome_blank_perform_lines_are_skipped():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_autojoin("lt", False)
    db.set_perform("lt", "\n  \n/mode +x\n\n")
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    assert irc.commands == [("irc.server.lt", "/mode +x")]


def test_welcome_no_perform_no_channels_issues_nothing():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")   # empty perform, no channels mapped
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    assert irc.commands == []


def test_second_welcome_while_connected_does_not_rerun_setup():
    # a network can send more than one 001; the joins/perform run once per
    # connect, not on every welcome line.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_perform("lt", "/mode +x")
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    first = list(irc.commands)
    assert first == [("irc.server.lt", "/mode +x"), ("irc.server.lt", "/join #weechat")]
    emit_flush(r, _welcome())       # already connected now
    assert irc.commands == first    # nothing new issued


def test_reconnect_reruns_setup():
    # a fresh connect (status reset to connecting by the manager) must re-run
    # the perform and the rejoins.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_perform("lt", "/mode +x")
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    assert irc.commands == [("irc.server.lt", "/mode +x")]
    db.set_server_status("lt", "connecting")   # what a manual reconnect does
    emit_flush(r, _welcome())
    assert irc.commands == [("irc.server.lt", "/mode +x"),
                            ("irc.server.lt", "/mode +x")]


def test_channel_closed_lifecycle_marks_channel_parted():
    # parting closes the topic but keeps its mapping (for reuse on rejoin); the
    # channel must drop out of the joined list so it is not auto-rejoined.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    assert [c["buffer"] for c in db.list_channels("lt")] == ["irc.lt.#weechat"]
    r = Router(db, gw, irc)
    run(r.handle_irc(IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="part",
                              text="No longer in #weechat", affects_me=True,
                              lifecycle="closed")))
    assert db.topic_for_buffer("irc.lt.#weechat") is not None   # topic kept
    assert db.list_channels("lt") == []                         # but not joined


def test_channel_reopened_lifecycle_marks_channel_joined_again():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    db.set_channel_open("irc.lt.#weechat", False)   # previously parted
    assert db.list_channels("lt") == []
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                           text="Joined #weechat", affects_me=True,
                           lifecycle="opened"))
    assert [c["buffer"] for c in db.list_channels("lt")] == ["irc.lt.#weechat"]


def test_welcome_autojoin_skips_a_parted_channel():
    # a channel the user deliberately left (open = 0) must not be auto-rejoined
    # on the next connect, even with autojoin on.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.set_mapping("irc.lt.#weechat", 2, "primary")
    db.set_mapping("irc.lt.#python", 3, "primary")
    db.set_channel_open("irc.lt.#python", False)
    r = Router(db, gw, irc)
    emit_flush(r, _welcome())
    assert irc.commands == [("irc.server.lt", "/join #weechat")]


def test_arm_connect_clears_stale_reply_buffers():
    # a WHOIS/list whose terminator never arrived (socket dropped mid-burst)
    # must not leak into the next connection.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    r._connect_timeout = 60.0   # keep the armed timer from firing during the test
    r._whois["lt"] = {"bob": {"nick": "bob"}}
    r._list_buffer["lt"] = [{"channel": "#x", "users": 1, "topic": ""}]
    r._discover_pending.add("lt")

    async def go():
        r.arm_connect_timeout("lt")
        await r.flush()   # cancels the armed timer, leaves no pending task
    run(go())
    assert "lt" not in r._whois
    assert "lt" not in r._list_buffer
    assert "lt" not in r._discover_pending


def test_ignored_nick_channel_message_is_dropped():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.add_ignore("lt", "spammer")
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="spammer", text="buy now", msgid="m1")))
    assert gw.sent == [] and gw.created == []
    # nothing recorded for a dropped message
    assert db.message_by_msgid("irc.lt.#weechat", "m1") is None


def test_ignored_nick_match_is_case_insensitive_in_router():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.add_ignore("lt", "SpamBot")
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="spambot", text="hi", msgid="m1")))
    assert gw.sent == []


def test_non_ignored_nick_message_still_forwarded():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.add_ignore("lt", "spammer")
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hello", msgid="m1")))
    assert len(gw.sent) == 1 and "alice" in gw.sent[0][1]


def test_ignore_is_scoped_per_server_in_router():
    # ignoring "bob" on lt must not silence "bob" on another server.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.add_ignore("other", "bob")
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(server="lt", nick="bob", text="hi", msgid="m1")))
    assert len(gw.sent) == 1


def test_ignored_nick_event_is_dropped():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.add_ignore("lt", "troll")
    r = Router(db, gw, irc)
    # a mode change by an ignored nick (not muted noise, not about me) is dropped
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="mode",
                           text="troll sets +o troll", nick="troll"))
    assert gw.sent == []


def test_ignored_event_that_affects_me_is_still_shown():
    # you were kicked by an ignored op: the ignore must not hide it from you.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.add_ignore("lt", "op")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="kick",
                           text="tgb was kicked by op", affects_me=True, nick="op"))
    assert len(gw.sent) == 1
    assert "tgb was kicked" in gw.sent[0][1]


def test_event_without_nick_is_never_dropped_by_ignore():
    # a server numeric carries no acting nick, so the ignore path leaves it alone.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    db.add_ignore("lt", "someone")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="- MOTD line", numeric=372))
    assert len(gw.sent) == 1


def test_show_event_reflects_noise_filter_toggle():
    # _show_event reads noise_filter from the database on each call, so flipping
    # the setting changes whether a join is shown without recreating the router.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")   # default noise_filter mutes join,part,quit
    r = Router(db, gw, irc)
    join = IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                    text="bob joined", nick="bob")
    assert r._show_event(join) is False              # join muted by default
    # unmute joins by dropping the token, keeping the rest of the filter
    db.upsert_server("lt", noise_filter="part,quit")
    assert r._show_event(join) is True               # now shown
    # muting it again hides it once more
    db.upsert_server("lt", noise_filter="join,part,quit")
    assert r._show_event(join) is False


def test_friendly_error_uses_existing_channel_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(buffer="irc.lt.#chan", conversation="#chan",
                              nick="x", text="hi")))   # creates the #chan topic
    tid = db.topic_for_buffer("irc.lt.#chan")["topic_id"]
    gw.sent.clear()
    emit_flush(r, IrcEvent(server="lt", buffer="irc.server.lt", kind="server",
                           text="#chan :Cannot send to channel", numeric=404))
    assert gw.sent[0][0] == tid


def test_incoming_reply_threads_onto_mirrored_message():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="original", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    run(r.handle_irc(chan_msg(nick="bob", text="reply!", msgid="m2",
                              reply_to_msgid="m1")))
    assert gw.sent[1][2] == tg_id          # second message threaded onto m1


def test_incoming_reply_to_unknown_msgid_not_threaded():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="bob", text="hi", msgid="m2",
                              reply_to_msgid="ghost")))
    assert gw.sent[0][2] is None


def test_outgoing_reply_prefixes_nick_on_channel():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hey", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    topic = db.topic_for_buffer("irc.lt.#weechat")["topic_id"]
    run(r.handle_telegram(topic, 9, "sure thing", reply_to=tg_id))
    assert ("irc.lt.#weechat", "alice: sure thing") in irc.messages


def test_outgoing_reply_no_prefix_in_pm():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(buffer="irc.lt.bob", conversation="bob",
                              is_private=True, nick="bob", text="yo", msgid="p1")))
    tg_id = db.message_by_msgid("irc.lt.bob", "p1")["tg_message_id"]
    topic = db.topic_for_buffer("irc.lt.bob")["topic_id"]
    run(r.handle_telegram(topic, 9, "hello", reply_to=tg_id))
    assert ("irc.lt.bob", "hello") in irc.messages     # no nick prefix in a PM


def test_outgoing_reply_prefixes_only_first_line():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hey", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    topic = db.topic_for_buffer("irc.lt.#weechat")["topic_id"]
    run(r.handle_telegram(topic, 9, "line one\nline two", reply_to=tg_id))
    sent = [t for b, t in irc.messages if b == "irc.lt.#weechat"]
    assert sent == ["alice: line one", "line two"]


def test_art_message_rendered_in_monospace():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="haard", text="████▄▄▐▌█▀▀█")))
    html = gw.sent[0][1]
    assert "<pre>" in html and "haard" in html


def test_normal_message_not_monospace():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="just a normal line")))
    assert "<pre>" not in gw.sent[0][1]


def test_incoming_reaction_placed_on_mapped_message():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="original", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    run(r.handle_irc(IrcReaction(server="lt", buffer="irc.lt.#weechat",
                                 target_msgid="m1", emoji="👍", nick="bob")))
    assert gw.reactions == [(tg_id, "👍")]


def test_incoming_reaction_to_unknown_msgid_does_nothing():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(IrcReaction(server="lt", buffer="irc.lt.#weechat",
                                 target_msgid="ghost", emoji="👍", nick="bob")))
    assert gw.reactions == []


def test_self_reaction_is_dropped():
    # our own reaction, echoed back from IRC, must not be mirrored again.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="original", msgid="m1")))
    run(r.handle_irc(IrcReaction(server="lt", buffer="irc.lt.#weechat",
                                 target_msgid="m1", emoji="👍", nick="tgb",
                                 is_self=True)))
    assert gw.reactions == []


def test_incoming_redact_deletes_mapped_message():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="oops", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    run(r.handle_irc(IrcRedact(server="lt", buffer="irc.lt.#weechat",
                               target_msgid="m1")))
    assert gw.deleted == [tg_id]


def test_incoming_redact_to_unknown_msgid_does_nothing():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(IrcRedact(server="lt", buffer="irc.lt.#weechat",
                               target_msgid="ghost")))
    assert gw.deleted == []


def test_build_react_quote_line():
    assert build_react_quote("#weechat", "abc", "👍") == \
        "/quote @+draft/react=👍;+draft/reply=abc TAGMSG #weechat"


def test_outgoing_reaction_sends_quote_on_channel_buffer():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hey", msgid="m1")))
    tg_id = db.message_by_msgid("irc.lt.#weechat", "m1")["tg_message_id"]
    run(r.handle_telegram_reaction(tg_id, "🔥"))
    assert irc.commands == [
        ("irc.lt.#weechat",
         "/quote @+draft/react=🔥;+draft/reply=m1 TAGMSG #weechat")]


def test_outgoing_reaction_targets_nick_in_pm():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(buffer="irc.lt.bob", conversation="bob",
                              is_private=True, nick="bob", text="yo", msgid="p1")))
    tg_id = db.message_by_msgid("irc.lt.bob", "p1")["tg_message_id"]
    run(r.handle_telegram_reaction(tg_id, "👍"))
    assert irc.commands == [
        ("irc.lt.bob", "/quote @+draft/react=👍;+draft/reply=p1 TAGMSG bob")]


def test_outgoing_reaction_unknown_message_does_nothing():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_telegram_reaction(999999, "👍"))
    assert irc.commands == []


def test_outgoing_reaction_without_irc_msgid_does_nothing():
    # a mirrored message with no IRC msgid (e.g. a server-status line) cannot be
    # referenced by a draft/reply target, so no TAGMSG is sent.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    db.record_message(buffer="irc.lt.#weechat", tg_chat_id=gw.chat_id,
                      tg_message_id=700, owner_bot="primary", nick="alice")
    r = Router(db, gw, irc)
    run(r.handle_telegram_reaction(700, "👍"))
    assert irc.commands == []


def test_outgoing_reaction_on_server_buffer_message_does_nothing():
    # a reaction on a NickServ/server-status message has no conversation target.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.server.lt", 9, "primary")
    db.record_message(buffer="irc.server.lt", tg_chat_id=gw.chat_id,
                      tg_message_id=800, owner_bot="primary", irc_msgid="s1",
                      nick="NickServ")
    r = Router(db, gw, irc)
    run(r.handle_telegram_reaction(800, "👍"))
    assert irc.commands == []


def test_single_bot_routes_everything_to_primary():
    # No pool: the default. Every topic is owned by the gateway's primary and
    # every send routes through it, unchanged from before the pool existed.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(text="hi", msgid="m1")))
    run(r.flush())
    assert gw.created_owners == ["primary"]
    assert all(owner == "primary" for _, owner in gw.send_owners)
    assert db.topic_for_buffer("irc.lt.#weechat")["owner_bot"] == "primary"


def test_pool_assigns_new_topic_to_least_loaded_bot():
    from tgbridge.senderpool import SenderPool
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    # primary already owns two topics; worker "b" owns none, so the new topic
    # must go to b.
    db.set_mapping("irc.lt.#one", 11, "primary")
    db.set_mapping("irc.lt.#two", 12, "primary")
    pool = SenderPool(["primary", "b"])
    r = Router(db, gw, irc, sender_pool=pool)
    run(r.handle_irc(chan_msg(buffer="irc.lt.#new", conversation="#new",
                              text="hi", msgid="m1")))
    run(r.flush())
    assert gw.created_owners == ["b"]
    assert db.topic_for_buffer("irc.lt.#new")["owner_bot"] == "b"
    # the message sent to the new topic went through its owner, b
    new_topic = db.topic_for_buffer("irc.lt.#new")["topic_id"]
    assert (new_topic, "b") in gw.send_owners
    # and the message was recorded under b, so a later edit/delete uses b
    rec = db.message_by_msgid("irc.lt.#new", "m1")
    assert rec["owner_bot"] == "b"


def test_pool_returns_stored_owner_for_existing_topic():
    from tgbridge.senderpool import SenderPool
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    # #weechat already exists and is owned by worker b; a fresh message to it
    # must route through b, not be reassigned by the pool.
    db.set_mapping("irc.lt.#weechat", 50, "b")
    pool = SenderPool(["primary", "b"])
    r = Router(db, gw, irc, sender_pool=pool)
    run(r.handle_irc(chan_msg(text="hello", msgid="m1")))
    run(r.flush())
    assert gw.created == []   # no new topic
    assert (50, "b") in gw.send_owners
    rec = db.message_by_msgid("irc.lt.#weechat", "m1")
    assert rec["owner_bot"] == "b"


def test_pool_event_batch_routes_through_topic_owner():
    from tgbridge.senderpool import SenderPool
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 60, "b")
    pool = SenderPool(["primary", "b"])
    r = Router(db, gw, irc, sender_pool=pool)
    # a shown event (a topic change) is coalesced and flushed through the owner
    ev = IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="topic",
                  nick="alice", text="new topic", affects_me=False)
    run(r.handle_irc(ev))
    run(r.flush())
    assert any(owner == "b" for _, owner in gw.send_owners)
    assert all(owner == "b" for _, owner in gw.send_owners)


def test_incoming_typing_shows_indicator_in_the_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hi", msgid="m1")))   # topic exists
    topic = db.topic_for_buffer("irc.lt.#weechat")["topic_id"]
    run(r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#weechat",
                               nick="bob", state="active")))
    assert gw.typing == [topic]        # the typing action was shown in that topic
    r._stop_typing(topic)              # clean up the refresh task


def test_incoming_typing_ignored_without_a_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#nowhere",
                               nick="bob", state="active")))
    assert gw.typing == []             # no topic yet -> nothing shown


def test_own_typing_is_ignored():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hi", msgid="m1")))
    run(r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#weechat",
                               nick="me", state="active", is_self=True)))
    assert gw.typing == []


def test_typing_done_stops_the_refresh():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hi", msgid="m1")))
    topic = db.topic_for_buffer("irc.lt.#weechat")["topic_id"]

    # active then done must run in ONE loop so the refresh task persists between
    # them (each run()/asyncio.run is a fresh loop that would tear it down).
    async def go():
        await r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#weechat",
                                     nick="bob", state="active"))
        assert topic in r._typing_tasks       # refresh armed
        await r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#weechat",
                                     nick="bob", state="done"))
        assert topic not in r._typing_tasks   # done cancelled it
    run(go())


def test_typing_stopped_when_the_message_arrives():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hi", msgid="m1")))
    topic = db.topic_for_buffer("irc.lt.#weechat")["topic_id"]

    async def go():
        await r.handle_irc(IrcTyping(server="lt", buffer="irc.lt.#weechat",
                                     nick="bob", state="active"))
        assert topic in r._typing_tasks
        await r.handle_irc(chan_msg(nick="bob", text="here it is", msgid="m2"))
        assert topic not in r._typing_tasks   # the message superseded the hint
    run(go())


def _msg_row_count(db, buffer, msgid):
    return db._conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE buffer = ? AND irc_msgid = ?",
        (buffer, msgid)).fetchone()["n"]


def test_backfill_duplicate_msgid_not_mirrored_again():
    # A chathistory replay resends a line we already mirrored. It must not be
    # sent to Telegram a second time, nor create a second messages row.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="hello", msgid="dup1")))
    assert len(gw.sent) == 1
    first_tg_id = gw.sent[0]
    run(r.handle_irc(chan_msg(nick="alice", text="hello", msgid="dup1")))
    assert len(gw.sent) == 1                       # not mirrored again
    assert gw.sent[0] == first_tg_id               # the same single line
    assert _msg_row_count(db, "irc.lt.#weechat", "dup1") == 1   # no duplicate row


def test_new_msgid_mirrors_and_advances_last_seen():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="one", msgid="m1")))
    assert db.last_seen("irc.lt.#weechat") == "m1"
    run(r.handle_irc(chan_msg(nick="bob", text="two", msgid="m2")))
    assert len(gw.sent) == 2                        # both mirrored
    assert db.last_seen("irc.lt.#weechat") == "m2"  # high-water advanced


def test_message_without_msgid_always_mirrors():
    # No msgid means we cannot dedup, so even an identical repeat mirrors and
    # never touches the high-water mark (which is a msgid).
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.handle_irc(chan_msg(nick="alice", text="ping", msgid=None)))
    run(r.handle_irc(chan_msg(nick="alice", text="ping", msgid=None)))
    assert len(gw.sent) == 2
    assert db.last_seen("irc.lt.#weechat") is None


def test_request_backfill_after_form_on_server_buffer():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.request_backfill("irc.lt.#weechat", "m9"))
    assert irc.commands == [
        ("irc.server.lt", "/quote CHATHISTORY AFTER #weechat msgid=m9 100")]


def test_request_backfill_latest_form_without_last_seen():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.request_backfill("irc.lt.#weechat", None))
    assert irc.commands == [
        ("irc.server.lt", "/quote CHATHISTORY LATEST #weechat * 100")]


def test_request_backfill_noop_for_server_buffer():
    # The server buffer has no conversation target to backfill.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc)
    run(r.request_backfill("irc.server.lt", "m1"))
    assert irc.commands == []


def test_backfill_fires_on_reopen_from_high_water():
    # Rejoining a channel must request the gap since the last seen msgid, so
    # messages missed during downtime are replayed. Without the wiring the loop
    # advances last_seen but never issues the chathistory request.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    db.set_last_seen("irc.lt.#weechat", "m42")
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                           text="Joined #weechat", affects_me=True,
                           lifecycle="opened"))
    assert ("irc.server.lt",
            "/quote CHATHISTORY AFTER #weechat msgid=m42 100") in irc.commands


def test_backfill_on_first_join_asks_for_latest():
    # A first join has no high-water mark, so the request is the LATEST form.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.upsert_server("lt")
    r = Router(db, gw, irc)
    emit_flush(r, IrcEvent(server="lt", buffer="irc.lt.#weechat", kind="join",
                           text="Joined", affects_me=True, lifecycle="opened"))
    assert ("irc.server.lt",
            "/quote CHATHISTORY LATEST #weechat * 100") in irc.commands


def test_file_notice_language_follows_runtime_db_change():
    # The router's own file-transfer notices must honour a /language switch made
    # after construction, like the manager, not a language cached at startup.
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, translator=_tr(), upload=_ok_upload)
    db.set("language", "he")
    run(r.handle_incoming_file("alice", "lt", "/dl/alice.report.pdf"))
    assert any("מקבל" in html for _tid, html, _reply in gw.sent)


def test_incoming_file_notice_then_edit_to_link_in_sender_pm_topic():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, translator=_tr(), upload=_ok_upload)
    run(r.handle_incoming_file("alice", "lt", "/dl/alice.report.pdf"))
    # the sender's PM topic was created (irc.<server>.<nick>, title "lt . nick")
    assert len(gw.created) == 1
    topic_id, title = gw.created[0]
    assert title == "lt · alice"
    assert db.topic_for_buffer("irc.lt.alice")["topic_id"] == topic_id
    # one notice posted, to that topic, naming the file (nick prefix stripped)
    assert len(gw.sent) == 1
    sent_topic, notice_html, _ = gw.sent[0]
    assert sent_topic == topic_id
    assert "report.pdf" in notice_html and "Receiving" in notice_html
    assert "alice.report.pdf" not in notice_html    # display name has no prefix
    # then that same message is edited in place to the gofile link
    assert len(gw.edited) == 1
    edited_id, edited_html, _owner = gw.edited[0]
    assert edited_id == 501                          # the id the notice send returned
    assert "https://gofile.io/d/abc123" in edited_html
    assert "report.pdf" in edited_html


def test_incoming_file_display_name_keeps_dots_in_original():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, translator=_tr(), upload=_ok_upload)
    run(r.handle_incoming_file("bob", "lt", "/dl/bob.holiday.2024.mkv"))
    assert "holiday.2024.mkv" in gw.sent[0][1]
    assert "bob.holiday" not in gw.sent[0][1]


def test_incoming_file_upload_failure_edits_to_failure_text():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, translator=_tr(), upload=_boom_upload)
    run(r.handle_incoming_file("alice", "lt", "/dl/alice.secret.zip"))
    # the notice still went up, then it is edited to a clear failure line
    assert len(gw.sent) == 1
    assert len(gw.edited) == 1
    _id, edited_html, _owner = gw.edited[0]
    assert "secret.zip" in edited_html
    assert "Could not" in edited_html
    assert "gofile.io" not in edited_html            # no link on failure


def test_incoming_file_edit_routes_through_topic_owner():
    from tgbridge.senderpool import SenderPool
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    # the sender's PM topic is already owned by worker "b"; both the notice send
    # and its edit must go through b (a bot can only edit its own message).
    db.set_mapping("irc.lt.alice", 70, "b")
    pool = SenderPool(["primary", "b"])
    r = Router(db, gw, irc, sender_pool=pool, translator=_tr(), upload=_ok_upload)
    run(r.handle_incoming_file("alice", "lt", "/dl/alice.report.pdf"))
    assert gw.send_owners == [(70, "b")]
    assert gw.edited[0][2] == "b"


def test_outgoing_file_sends_link_to_mapped_buffer():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc, translator=_tr(), upload=_ok_upload)
    run(r.handle_outgoing_file(42, "/tmp/pic.jpg"))
    assert irc.messages == [
        ("irc.lt.#weechat", "Shared a file (pic.jpg): https://gofile.io/d/abc123")]
    assert gw.sent == []                              # nothing posted back to Telegram


def test_outgoing_file_to_pm_buffer():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.bob", 43, "primary")
    r = Router(db, gw, irc, translator=_tr(), upload=_ok_upload)
    run(r.handle_outgoing_file(43, "/tmp/clip.mp4"))
    assert irc.messages == [
        ("irc.lt.bob", "Shared a file (clip.mp4): https://gofile.io/d/abc123")]


def test_outgoing_file_unmapped_topic_is_noop():
    calls = []

    async def track_upload(path):
        calls.append(path)
        return "https://gofile.io/d/x"

    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    r = Router(db, gw, irc, translator=_tr(), upload=track_upload)
    run(r.handle_outgoing_file(999, "/tmp/pic.jpg"))
    assert irc.messages == []
    assert calls == []                                # never even uploaded


def test_outgoing_file_upload_failure_sends_nothing_to_irc():
    gw, irc, db = FakeGateway(), FakeIrc(), _db()
    db.set_mapping("irc.lt.#weechat", 42, "primary")
    r = Router(db, gw, irc, translator=_tr(), upload=_boom_upload)
    run(r.handle_outgoing_file(42, "/tmp/pic.jpg"))
    assert irc.messages == []                         # no failure line leaked to IRC


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
