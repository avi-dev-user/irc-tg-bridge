"""Route between IRC and Telegram.

The one correctness rule that matters most (invariant 1): a message crosses
once, in the right direction. Loops are prevented by never forwarding our own
IRC lines back to Telegram - a message injected from Telegram echoes back from
WeeChat tagged self, and is dropped here.

The router depends on small injected collaborators (an IRC backend, a Telegram
gateway, the database), so its logic is tested with fakes, no live services.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Optional, Protocol

from . import gofile
from .commands import parse_list_reply, parse_names_reply
from .filewatch import parse_xfer_filename
from .formatting import looks_like_art, mirc_to_html, split_for_irc
from .ircbackend import (
    IrcEvent, IrcMessage, IrcReaction, IrcRedact, IrcTyping,
    build_chathistory_request)
from .ircnumerics import friendly_numeric

# Prefix for a friendly error line surfaced from an IRC numeric.
_WARNING = "⚠️"

# Prepended to a channel message that mentions you (IrcMessage.highlight), so a
# mention stands out from ordinary chatter.
_HIGHLIGHT = "🔔"

# After a command is sent from a topic, its server replies (whois, list, ...)
# are shown in that same topic for this many seconds, instead of the console.
_CMD_REPLY_WINDOW = 20.0

# Reaction feedback on a message sent to IRC, distinct per outcome so a command
# does not look like a plain delivered message: a command shows "working" until
# its reply arrives (or the ack timeout below), then "handled"; a plain message
# just shows delivered; a failed send shows the down mark. These are limited to
# Telegram's default reaction set (a bot cannot react with an arbitrary emoji
# even where the group allows all of them, so a check/cross would be rejected).
_ACK_WORKING = "👀"       # command sent, waiting for its reply
_ACK_CMD_DONE = "👌"      # command's reply came back
_ACK_DELIVERED = "👍"     # a plain message reached IRC
_ACK_FAILED = "👎"        # the send failed

# A /list can return thousands of channels; only the busiest are worth offering
# as tappable join buttons.
_CHANNEL_LIST_CAP = 20

# WeeChat routes /LIST output to a dedicated "irc.list_<server>" buffer. A reply
# not captured by discovery rides in on it and must be routed to the server's
# status topic, never used to spawn a topic of its own.
_LIST_BUFFER_PREFIX = "irc.list_"

# Commands whose first argument is a nick. Replying to a message and typing one
# of these bare (no nick) fills in the replied-to sender's nick.
_NICK_COMMANDS = {"/whois", "/msg", "/query", "/op", "/voice", "/kick", "/invite"}

# WHOIS reply numerics we fold into one card, and the terminator that flushes it.
# These carry no error mapping, so they are disjoint from the friendly-error set.
_WHOIS_NUMERICS = frozenset({311, 312, 313, 317, 319, 330, 671})
_WHOIS_END = 318

# NAMES reply numerics: RPL_NAMREPLY lines accumulate the membership, terminated
# by RPL_ENDOFNAMES. Disjoint from the whois and error numerics above.
_NAMES_REPLY = 353
_NAMES_END = 366

# A channel can have thousands of members; keep only the first this-many as
# tappable user buttons (a truncation note is logged).
_NAMES_CAP = 100


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_channel_buffer(buffer: str) -> bool:
    """True for a channel buffer (irc.<server>.#chan), False for the server
    buffer or a PM (irc.<server>.<nick>) where a nick prefix is pointless."""
    parts = buffer.split(".", 2)
    return len(parts) == 3 and parts[2][:1] in "#&+!"


def _channel_token(text: str) -> Optional[str]:
    """The leading channel name in an IRC error text (e.g. "#tldev :reason" ->
    "#tldev"), or None when the text does not begin with a channel. Only the
    first token is considered: the target is always leading in these numerics."""
    tok = next(iter(text.split()), "").strip(":,")
    return tok if tok and tok[0] in "#&+!" else None


def _parse_whois(numeric: int, text: str) -> tuple[Optional[str], dict]:
    """Pull the target nick and the fields a single WHOIS numeric carries. The
    first token is the nick for every whois numeric (the client target the
    relay strips leaves the queried nick leading, as with RPL_LIST). Returns
    (nick, fields); nick is None only when the text is empty."""
    head, sep, trailing = text.partition(" :")
    parts = head.split()
    nick = parts[0] if parts else None
    fields: dict = {}
    if numeric == 311 and len(parts) >= 3:
        fields["user"], fields["host"] = parts[1], parts[2]
        if trailing:
            fields["realname"] = trailing
    elif numeric == 312 and len(parts) >= 2:
        fields["server"] = parts[1]
    elif numeric == 313:
        fields["operator"] = True
    elif numeric == 317 and len(parts) >= 2 and parts[1].isdigit():
        fields["idle"] = int(parts[1])
    elif numeric == 319:
        channels = trailing.strip() or " ".join(parts[1:])
        if channels:
            fields["channels"] = channels
    elif numeric == 330 and len(parts) >= 2:
        fields["account"] = parts[1]
    elif numeric == 671:
        fields["secure"] = True
    return nick, fields


def _fmt_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _format_whois_card(nick: str, f: dict) -> str:
    """One tidy card from the collected whois fields, using allowed HTML only.
    Every line is optional; absent fields are simply skipped."""
    lines = [f"<b>{_escape(nick)}</b>"]
    if f.get("user") and f.get("host"):
        lines.append(f"<code>{_escape(f['user'])}@{_escape(f['host'])}</code>")
    elif f.get("host"):
        lines.append(f"<code>{_escape(f['host'])}</code>")
    if f.get("realname"):
        lines.append(f"<i>{_escape(f['realname'])}</i>")
    if f.get("account"):
        lines.append(f"Account: <b>{_escape(f['account'])}</b>")
    if f.get("channels"):
        lines.append(f"Channels: {_escape(f['channels'])}")
    if "idle" in f:
        lines.append(f"Idle: {_fmt_idle(f['idle'])}")
    if f.get("server"):
        lines.append(f"Server: {_escape(f['server'])}")
    if f.get("operator"):
        lines.append("<i>is an IRC operator</i>")
    if f.get("secure"):
        lines.append("<i>secure connection</i>")
    return "\n".join(lines)


class Gateway(Protocol):
    """The Telegram side, as the router needs it. Implemented by the Kurigram
    layer; faked in tests. owner_bot names which bot in the sender pool posts;
    it defaults to the primary, so a single-bot setup ignores it entirely."""
    async def create_topic(self, title: str,
                           owner_bot: Optional[str] = None) -> int: ...
    async def send(self, topic_id: int, html: str,
                   reply_to_message_id: Optional[int] = None,
                   owner_bot: Optional[str] = None) -> int: ...
    async def edit_message(self, message_id: int, html: str,
                           owner_bot: Optional[str] = None) -> None: ...
    async def react(self, message_id: int, emoji: str) -> None: ...
    async def delete_message(self, message_id: int) -> None: ...
    async def send_typing(self, topic_id: int) -> None: ...
    async def close_topic(self, topic_id: int) -> None: ...
    async def reopen_topic(self, topic_id: int) -> None: ...
    @property
    def chat_id(self) -> int: ...
    @property
    def owner_bot(self) -> str: ...


class IrcSink(Protocol):
    async def send_message(self, buffer: str, text: str) -> None: ...
    async def send_command(self, buffer: str, command: str) -> None: ...
    def connected_servers(self) -> set[str]: ...
    def nick_for(self, server: str) -> str: ...


def build_react_quote(target: str, msgid: str, emoji: str) -> str:
    """The raw WeeChat /quote line that mirrors a Telegram reaction onto IRC as
    an IRCv3 draft reaction: a TAGMSG to the conversation carrying the emoji and
    the replied-to msgid. WeeChat's /quote passes the client tags to the server
    verbatim; a network without the extension silently ignores them."""
    return f"/quote @+draft/react={emoji};+draft/reply={msgid} TAGMSG {target}"


def _topic_title(msg) -> str:
    server = getattr(msg, "server", "") or "?"
    conv = getattr(msg, "conversation", "") or getattr(msg, "buffer", "")
    return f"{server} · {conv}"


class Router:
    def __init__(self, db, gateway: Gateway, irc: IrcSink, *,
                 on_server_status=None, sender_pool=None,
                 mention_user_id: Optional[int] = None, mention_label: str = "",
                 translator=None, upload=None):
        self._db = db
        self._gw = gateway
        self._irc = irc
        # Translator for the file-transfer notices the router posts itself (most
        # user-facing text is produced by the manager, but a re-hosted file's
        # notice originates here). None falls back to the raw key. The language is
        # read from the database on each call (like the manager), so a runtime
        # /language change takes effect without a restart.
        self._tr = translator
        # The re-host uploader, injected so tests never hit the network. Defaults
        # to gofile (which imports aiohttp lazily, so importing it here is cheap).
        self._upload = upload if upload is not None else gofile.upload_file
        # When set, a new topic is assigned to the least-loaded bot in the pool
        # and its sends route through that bot. None means single-bot: every
        # topic is owned by (and posted through) the gateway's primary bot,
        # exactly as before the pool existed.
        self._pool = sender_pool
        # topic_id -> owner_bot, so a send (including a coalesced event batch,
        # which only has the topic id) routes through the topic's owning bot
        # without re-reading the database each time. Populated by _ensure_topic.
        self._topic_owner: dict[int, str] = {}
        # When set, a mention (IrcMessage.highlight) tags this Telegram user by
        # id, so they get a personal ping even in a muted topic. The label, if
        # any, is the visible text on the link (e.g. their @username).
        self._mention_user_id = mention_user_id
        self._mention_label = mention_label
        # Called when a server's connection state changes, so the console can
        # update the message that reported "connecting".
        self._on_server_status = on_server_status
        # Called with (server, channels) once a /list finishes, so the console
        # can offer the discovered channels as join buttons.
        self._on_channel_list = None
        # Channel discovery is opt-in per server: we only buffer RPL_LIST lines
        # for a server the manager has flagged via mark_discover, so ordinary
        # /list output typed in a topic still flows to that topic unchanged.
        self._discover_pending: set[str] = set()
        self._list_buffer: dict[str, list] = {}
        # NAMES collection, opt-in per server the same way as discovery: only a
        # server the manager flagged via mark_names buffers its 353 lines, so an
        # auto-NAMES on join still flows to its topic. Keyed by channel because
        # the reply names it, flushed to on_names on the 366 terminator.
        self._on_names = None
        self._names_pending: set[str] = set()
        self._names_buffer: dict[str, dict[str, list]] = {}
        # WHOIS replies arrive as a burst of numerics terminated by 318; buffer
        # them per server, keyed by the queried nick, and render one card on end.
        self._whois: dict[str, dict[str, dict]] = {}
        self._cmd_origin: dict[str, tuple] = {}   # server -> (topic_id, expiry)
        # Coalesce a burst of event lines (whois, names, join/part) per topic
        # into one message. Chat messages stay 1:1 (needed for reply/react).
        self._pending: dict[int, list] = {}
        self._flush_tasks: dict[int, object] = {}
        self._batch_delay = 0.7
        # Per-topic typing indicator: a deadline (extended by each "active") and
        # the refresh task that re-sends the chat action until the deadline.
        self._typing_until: dict[int, float] = {}
        self._typing_tasks: dict[int, object] = {}
        # A server that never reaches RPL_WELCOME (unreachable host, wrong port,
        # refused TLS) would sit on "connecting" forever. Arm a timer when a
        # connect starts; if welcome does not arrive in time, report it failed.
        self._connect_tasks: dict[str, object] = {}
        self._connect_timeout = 45.0
        # WeeChat's /LIST buffer streams rows with no RPL_LISTEND, so completion
        # is a debounce: each row restarts a short timer, and the collected list is
        # flushed to the picker once the rows stop arriving.
        self._list_flush_tasks: dict[str, object] = {}
        self._list_debounce = 2.0
        # A command's "working" reaction waits for its server reply to flip to
        # done; server -> the message id awaiting that flip, plus a timeout so a
        # command with no textual reply (a /join, a /nick) is not stuck on eyes.
        self._cmd_ack: dict[str, list[int]] = {}
        self._cmd_ack_tasks: dict[str, object] = {}
        self._cmd_ack_timeout = 15.0

    def expect_reply_in(self, server: str, topic_id: int) -> None:
        """Route the next command reply burst for this server to a specific
        topic. Used for panel buttons (Topic/Who) that fire an IRC command
        without passing through handle_telegram, so their 33x/35x reply
        numerics land in the channel's own topic instead of the server topic."""
        self._cmd_origin[server] = (topic_id, time.monotonic() + _CMD_REPLY_WINDOW)

    def set_server_status_callback(self, cb) -> None:
        self._on_server_status = cb

    def set_channel_list_callback(self, cb) -> None:
        self._on_channel_list = cb

    def set_names_callback(self, cb) -> None:
        self._on_names = cb

    def mark_discover(self, server: str) -> None:
        """Start collecting the next /list reply for this server. Any partial
        buffer from an earlier, unfinished discovery is dropped."""
        self._discover_pending.add(server)
        self._list_buffer.pop(server, None)

    def clear_discover(self, server: str) -> None:
        """Stop collecting this server's /list reply and drop any partial buffer.
        Used when a discovery times out with no reply, so a late 322/323 line is
        ignored instead of firing the picker after the console has given up."""
        self._discover_pending.discard(server)
        self._list_buffer.pop(server, None)

    def mark_names(self, server: str) -> None:
        """Start collecting the next /names reply for this server. Any partial
        buffer from an earlier, unfinished names request is dropped."""
        self._names_pending.add(server)
        self._names_buffer.pop(server, None)

    def arm_connect_timeout(self, server: str) -> None:
        """Called when a connect/reconnect starts. If RPL_WELCOME does not
        arrive within the timeout, the server is reported failed so the console
        stops waiting and the status dot resolves."""
        self._cancel_connect_timeout(server)
        # A fresh connection invalidates any half-collected reply state from the
        # previous session: a WHOIS or /list whose terminator numeric never
        # arrived (the socket dropped mid-burst) would otherwise leak here.
        self._whois.pop(server, None)
        self._list_buffer.pop(server, None)
        self._discover_pending.discard(server)
        self._names_buffer.pop(server, None)
        self._names_pending.discard(server)
        self._connect_tasks[server] = asyncio.create_task(
            self._connect_timeout_later(server))

    def _cancel_connect_timeout(self, server: str) -> None:
        task = self._connect_tasks.pop(server, None)
        if task is not None:
            task.cancel()

    async def _connect_timeout_later(self, server: str) -> None:
        try:
            await asyncio.sleep(self._connect_timeout)
        except asyncio.CancelledError:
            return
        self._connect_tasks.pop(server, None)
        # Only fail a server still waiting to connect. A success (welcome
        # cancels this task) or a manual disconnect will have moved it on.
        srv = self._db.get_server(server)
        if srv is None or srv.get("status") != "connecting":
            return
        # No welcome arrived in time, but that is expected when a reconnect hits a
        # server weechat already holds open: it answers "already connected" and
        # sends no fresh welcome. Trust weechat's own state over the missed welcome
        # before declaring a failure, so a live server is not mislabelled 🔴.
        if server in self._irc.connected_servers():
            self._db.set_server_status(server, "connected")
            if self._on_server_status is not None:
                await self._on_server_status(server, "connected")
            return
        self._db.set_server_status(server, "disconnected")
        if self._on_server_status is not None:
            await self._on_server_status(server, "failed")

    async def _perform_on_connect(self, server: str) -> None:
        """Run the server's on-connect setup: its perform script first (so an
        invite it requests lands before we try the channel), then rejoin the
        channels we had topics for when autojoin is on. Everything runs on the
        server buffer, the same buffer /join and /msg use elsewhere."""
        srv = self._db.get_server(server) or {}
        server_buf = f"irc.server.{server}"
        for line in (srv.get("perform") or "").splitlines():
            line = line.strip()
            if line:
                await self._irc.send_command(server_buf, line)
        if srv.get("autojoin", 1):
            for ch in self._db.list_channels(server):
                channel = ch["buffer"].split(".", 2)[-1]
                await self._irc.send_command(server_buf, f"/join {channel}")

    @staticmethod
    def _server_title(server: str) -> str:
        return f"⚙ {server}"

    async def handle_irc(self, item) -> None:
        if isinstance(item, IrcMessage):
            await self._on_message(item)
        elif isinstance(item, IrcReaction):
            await self._on_reaction(item)
        elif isinstance(item, IrcRedact):
            await self._on_redact(item)
        elif isinstance(item, IrcTyping):
            await self._on_typing(item)
        elif isinstance(item, IrcEvent):
            await self._on_event(item)

    async def _on_typing(self, tp: IrcTyping) -> None:
        # Show Telegram's typing indicator in the topic while someone on IRC is
        # typing. Only for a topic that already exists (do not spawn one just for
        # this), and never for our own typing.
        if tp.is_self:
            return
        row = self._db.topic_for_buffer(tp.buffer)
        if row is None:
            return
        topic_id = row["topic_id"]
        if tp.state == "active":
            # each "active" extends the window; the refresh loop re-sends the
            # action (it expires after about 5s) until the window lapses.
            self._typing_until[topic_id] = time.monotonic() + 8
            await self._gw.send_typing(topic_id)
            if topic_id not in self._typing_tasks:
                self._typing_tasks[topic_id] = asyncio.create_task(
                    self._typing_refresh(topic_id))
        else:
            self._stop_typing(topic_id)   # paused or done

    async def _typing_refresh(self, topic_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(4)
                if time.monotonic() >= self._typing_until.get(topic_id, 0):
                    break
                await self._gw.send_typing(topic_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._typing_tasks.pop(topic_id, None)

    def _stop_typing(self, topic_id: int) -> None:
        self._typing_until.pop(topic_id, None)
        task = self._typing_tasks.pop(topic_id, None)
        if task is not None:
            task.cancel()

    async def _on_reaction(self, r: IrcReaction) -> None:
        # Our own reaction, echoed back by the server: the Telegram side already
        # has it, so mirroring it again would double it. Drop it (invariant 1).
        if r.is_self:
            return
        row = self._db.message_by_msgid(r.buffer, r.target_msgid)
        if row is not None:
            await self._gw.react(row["tg_message_id"], r.emoji)

    async def _on_redact(self, r: IrcRedact) -> None:
        # Delete the mirrored Telegram message when we can map the redacted
        # msgid; an unknown target (never mirrored) leaves Telegram untouched.
        row = self._db.message_by_msgid(r.buffer, r.target_msgid)
        if row is not None:
            await self._gw.delete_message(row["tg_message_id"])

    async def _on_message(self, m: IrcMessage) -> None:
        if m.is_self:
            return  # loop prevention: our own line, do not echo back
        if self._db.is_ignored(m.server, m.nick):
            return  # ignored nick: drop the message
        # Backfill dedup: a line carrying a msgid we already recorded is one
        # chathistory replayed (e.g. on rejoin after downtime). It is already on
        # the Telegram side, so mirroring it again would double it (invariant 1).
        # Skip silently; a line with no msgid cannot be deduped, so it mirrors.
        if m.msgid and self._db.message_by_msgid(m.buffer, m.msgid) is not None:
            return
        if m.buffer.startswith("irc.server."):
            # services/server messages (NickServ, server notices) go to the
            # server's own status topic.
            topic_id = await self._ensure_topic(m.buffer, self._server_title(m.server))
            self._emit(topic_id, self._render_message(m))
            return
        if m.is_notice:
            # a notice is an informational burst (a multi-line NickServ reply, a
            # bot line), not conversation; coalesce it into its topic instead of
            # one Telegram message per line, the way events are batched.
            topic_id = await self._ensure_topic(m.buffer, _topic_title(m))
            self._emit(topic_id, self._render_message(m))
            return
        topic_id = await self._ensure_topic(m.buffer, _topic_title(m))
        self._stop_typing(topic_id)   # the message arrived; drop the typing hint
        html = self._render_message(m)
        # IRCv3 reply: if this line replies to one we mirrored, thread the Telegram
        # message onto it. Unknown target (never mirrored) just sends normally.
        reply_to = None
        if m.reply_to_msgid:
            row = self._db.message_by_msgid(m.buffer, m.reply_to_msgid)
            if row:
                reply_to = row["tg_message_id"]
        owner = self._owner_for(topic_id)
        msg_id = await self._gw.send(topic_id, html, reply_to_message_id=reply_to,
                                     owner_bot=owner)
        self._db.record_message(
            buffer=m.buffer, tg_chat_id=self._gw.chat_id, tg_message_id=msg_id,
            owner_bot=owner, irc_msgid=m.msgid, nick=m.nick,
        )
        # Advance the per-buffer high-water mark so a later backfill knows where
        # the gap starts. Only a line with a msgid moves it (the mark is a msgid).
        if m.msgid:
            self._db.set_last_seen(m.buffer, m.msgid)

    async def _on_event(self, e: IrcEvent) -> None:
        # Ignored nick: drop the event when it names an acting nick, but never
        # an event about you (a kick/mode that affects you is worth seeing even
        # if the actor is ignored). Server numerics carry no nick, so they pass.
        if e.nick and not e.affects_me and self._db.is_ignored(e.server, e.nick):
            return
        # RPL_LIST (322) / RPL_LISTEND (323): capture the list into a picker
        # instead of dumping rows to a topic. Collect it for an explicit discovery,
        # and also whenever it rode in on the dedicated /LIST buffer (a manually
        # typed /list), so the button and a typed /list resolve the same way.
        if e.numeric in (322, 323) and (e.server in self._discover_pending
                                        or e.buffer.startswith(_LIST_BUFFER_PREFIX)):
            await self._collect_list(e)
            return
        # RPL_NAMREPLY (353) / RPL_ENDOFNAMES (366): when a names request is in
        # progress for this server, capture the membership instead of dumping it.
        if e.numeric in (_NAMES_REPLY, _NAMES_END) and e.server in self._names_pending:
            await self._collect_names(e)
            return
        # WHOIS numerics are collected into one card instead of dumped raw; the
        # terminator (318) flushes it. Disjoint from the error numerics below.
        if e.numeric in _WHOIS_NUMERICS or e.numeric == _WHOIS_END:
            await self._collect_whois(e)
            return
        # Our own CHATHISTORY backfill probe draws a 421 "unknown command" on a
        # server without the extension. It is internal, not something the user
        # typed, so stay quiet instead of surfacing that error every rejoin.
        if e.numeric == 421 and "CHATHISTORY" in e.text.upper():
            return
        # A known error numeric becomes a clear warning line instead of the raw
        # code; numerics with no friendly mapping fall through to raw handling.
        if e.numeric is not None:
            friendly = friendly_numeric(e.numeric, e.text)
            if friendly is not None:
                await self._emit_numeric_warning(e, friendly)
                return
        # Server-scoped output (numerics, MOTD, user-mode, notices) goes to the
        # server's own status topic, or to the topic a command came from.
        if e.kind == "server" or e.buffer.startswith("irc.server."):
            if e.numeric == 1:   # RPL_WELCOME: the server accepted us
                self._cancel_connect_timeout(e.server)
                # Only run the on-connect setup on a fresh connect. A network
                # can send more than one 001 in a session; re-running the joins
                # and perform each time would spam the server.
                was_connected = (self._db.get_server(e.server) or {}
                                 ).get("status") == "connected"
                self._db.set_server_status(e.server, "connected")
                if self._on_server_status is not None:
                    await self._on_server_status(e.server, "connected")
                if not was_connected:
                    await self._perform_on_connect(e.server)
            if not self._show_event(e):
                return
            origin = self._cmd_origin.get(e.server)
            if origin and origin[1] > time.monotonic():
                topic_id = origin[0]   # recent command from a topic: reply there
            else:
                # A /list reply not being collected (manual /list, or one that
                # arrived after the discovery window) rides in on the dedicated
                # list buffer; route it to the server's status topic instead of
                # spawning a duplicate keyed on the list buffer name.
                buffer = e.buffer
                if buffer.startswith(_LIST_BUFFER_PREFIX):
                    buffer = f"irc.server.{e.server}"
                topic_id = await self._ensure_topic(buffer, self._server_title(e.server))
        else:
            # Ensure the channel's topic exists even when this event is filtered
            # noise, so joining a channel makes its topic appear right away.
            channel = e.buffer.split(".", 2)[-1]
            topic_id = await self._ensure_topic(e.buffer, f"{e.server} · {channel}")
            if e.lifecycle == "opened":
                self._db.set_channel_open(e.buffer, True)   # (re)joined
                await self._gw.reopen_topic(topic_id)   # rejoined: undo the close
                # Catch up on anything said while we were away. last_seen is the
                # high-water msgid _on_message advanced; None (a first join) asks
                # for recent history. Replays return through the normal stream and
                # are de-duplicated by msgid, so this never double-posts.
                await self.request_backfill(e.buffer, self._db.last_seen(e.buffer))
            elif e.lifecycle == "closed":
                # parted/kicked: drop it from the joined list so it is not
                # auto-rejoined, while keeping the mapping so the topic reopens.
                self._db.set_channel_open(e.buffer, False)
                # push any pending lines (e.g. a kick reason) before the topic
                # closes, so they are not stranded behind the closure notice.
                await self._flush(topic_id)
                await self._gw.send(topic_id, "<i>" + _escape(e.text) + "</i>",
                                    owner_bot=self._owner_for(topic_id))
                await self._gw.close_topic(topic_id)
                return
            if e.kind == "private":
                return   # PM open: the topic now exists, nothing to announce
            if not self._show_event(e):
                return
        body = _escape(e.text)
        html = f"<b><i>{body}</i></b>" if e.affects_me else f"<i>{body}</i>"
        self._emit(topic_id, html)
        # This event is the visible result of whatever command is waiting: a
        # server line (motd, version, a numeric) or a channel echo of a /mode,
        # /topic, /kick or /nick. Flip its "working" reaction to done. Ambient
        # join/part/quit is excluded so unrelated traffic does not flip it early;
        # _ack_reply is a no-op anyway when no command is pending.
        if e.kind in ("server", "mode", "topic", "kick", "nick"):
            await self._ack_reply(e.server)

    async def _emit_numeric_warning(self, e: IrcEvent, message: str) -> None:
        """Surface a friendly error line in the topic it concerns. Route to a
        channel's topic only when it already exists (a failed /join to a channel
        we are not in must not spawn an empty topic); otherwise show it where the
        command came from, else the server's own topic."""
        topic_id = None
        channel = _channel_token(e.text)
        if channel:
            # a channel named in the error text (a failed /join) is one we are
            # not in: use its topic only if it already exists, never spawn one.
            topic_id = self._existing_topic(f"irc.{e.server}.{channel}")
        if topic_id is None and e.buffer and e.kind != "server" \
                and not e.buffer.startswith("irc.server."):
            # rode in on a channel buffer we do have: ensure that topic.
            chan = e.buffer.split(".", 2)[-1]
            topic_id = await self._ensure_topic(e.buffer, f"{e.server} · {chan}")
        if topic_id is None:
            origin = self._cmd_origin.get(e.server)
            if origin and origin[1] > time.monotonic():
                topic_id = origin[0]
            else:
                topic_id = await self._ensure_topic(
                    f"irc.server.{e.server}", self._server_title(e.server))
        self._emit(topic_id, f"<b>{_WARNING}</b> <i>{_escape(message)}</i>")
        await self._ack_reply(e.server)   # a command that erred still got a reply

    def _existing_topic(self, buffer: str) -> Optional[int]:
        row = self._db.topic_for_buffer(buffer)
        return row["topic_id"] if row else None

    async def _collect_list(self, e: IrcEvent) -> None:
        if e.numeric == 322:
            parsed = parse_list_reply(e.text)
            if parsed is not None:
                self._list_buffer.setdefault(e.server, []).append(parsed)
                self._arm_list_flush(e.server)   # no 323 arrives; debounce instead
            return
        # An explicit 323 RPL_LISTEND (relays that still send it): flush at once.
        await self._flush_list(e.server)

    def _arm_list_flush(self, server: str) -> None:
        task = self._list_flush_tasks.pop(server, None)
        if task is not None:
            task.cancel()
        self._list_flush_tasks[server] = asyncio.create_task(
            self._list_flush_later(server))

    async def _list_flush_later(self, server: str) -> None:
        try:
            await asyncio.sleep(self._list_debounce)
        except asyncio.CancelledError:
            return
        self._list_flush_tasks.pop(server, None)
        await self._flush_list(server)

    async def _flush_list(self, server: str) -> None:
        task = self._list_flush_tasks.pop(server, None)
        if task is not None:
            task.cancel()
        if server not in self._list_buffer:
            return   # already flushed, or an end with no rows to show
        self._discover_pending.discard(server)
        channels = self._list_buffer.pop(server, [])
        channels.sort(key=lambda c: c["users"], reverse=True)
        if len(channels) > _CHANNEL_LIST_CAP:
            print(f"[router] /list for {server}: {len(channels)} channels, "
                  f"showing the top {_CHANNEL_LIST_CAP}")
            channels = channels[:_CHANNEL_LIST_CAP]
        if self._on_channel_list is not None:
            await self._on_channel_list(server, channels)
        await self._ack_reply(server)   # the /list reply arrived: flip working->done

    async def _collect_names(self, e: IrcEvent) -> None:
        if e.numeric == _NAMES_REPLY:
            parsed = parse_names_reply(e.text)
            if parsed is not None:
                by_channel = self._names_buffer.setdefault(e.server, {})
                by_channel.setdefault(parsed["channel"], []).extend(parsed["members"])
            return
        # 366 RPL_ENDOFNAMES: the membership for the named channel is complete.
        self._names_pending.discard(e.server)
        channel = next(iter(e.text.split()), "").strip(":,")
        by_channel = self._names_buffer.pop(e.server, {})
        # Prefer the channel the terminator names; fall back to the lone channel
        # collected when its name did not parse cleanly.
        users = by_channel.get(channel)
        if users is None and len(by_channel) == 1:
            channel, users = next(iter(by_channel.items()))
        if users is None:
            return
        if len(users) > _NAMES_CAP:
            print(f"[router] /names for {e.server} {channel}: {len(users)} members, "
                  f"showing the first {_NAMES_CAP}")
            users = users[:_NAMES_CAP]
        if self._on_names is not None:
            await self._on_names(e.server, channel, users)
        await self._ack_reply(e.server)   # the /names reply arrived

    async def _collect_whois(self, e: IrcEvent) -> None:
        nick, fields = _parse_whois(e.numeric, e.text)
        server_buf = self._whois.setdefault(e.server, {})
        key = nick or ""
        if e.numeric != _WHOIS_END:
            entry = server_buf.setdefault(key, {})
            if nick:
                entry.setdefault("nick", nick)
            entry.update(fields)
            return
        # 318 RPL_ENDOFWHOIS: flush the matching buffer as one card. Fall back to
        # the lone pending entry when the terminator's nick key does not match
        # (numerics that carried no parseable nick landed in the "" slot).
        data = server_buf.pop(key, None)
        if data is None and key and len(server_buf) == 1:
            data = server_buf.popitem()[1]
        if data is None and key:
            data = server_buf.pop("", None)
        if not server_buf:
            self._whois.pop(e.server, None)
        if not data:
            return   # a lone 318, nothing was collected
        card = _format_whois_card(data.get("nick") or nick or "?", data)
        topic_id = await self._whois_topic(e.server)
        self._emit(topic_id, card)
        await self._ack_reply(e.server)   # the /whois reply arrived

    async def _whois_topic(self, server: str) -> int:
        """Where a whois card lands: the command-origin topic while its reply
        window is open, else the server's own status topic."""
        origin = self._cmd_origin.get(server)
        if origin and origin[1] > time.monotonic():
            return origin[0]
        return await self._ensure_topic(
            f"irc.server.{server}", self._server_title(server))

    def _emit(self, topic_id: int, html: str) -> None:
        """Queue an event line; a short debounce coalesces a burst into one msg."""
        self._pending.setdefault(topic_id, []).append(html)
        if topic_id not in self._flush_tasks:
            self._flush_tasks[topic_id] = asyncio.create_task(self._flush_later(topic_id))

    async def _flush_later(self, topic_id: int) -> None:
        try:
            await asyncio.sleep(self._batch_delay)
        except asyncio.CancelledError:
            return
        await self._flush(topic_id)

    async def _flush(self, topic_id: int) -> None:
        task = self._flush_tasks.pop(topic_id, None)
        # A direct _flush (close path) can pre-empt a still-sleeping debounce
        # task; cancel it so it is not left orphaned. Skip when we are that task.
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        lines = self._pending.pop(topic_id, [])
        if lines:
            await self._gw.send(topic_id, "\n".join(lines),
                                owner_bot=self._owner_for(topic_id))

    async def flush(self) -> None:
        """Flush all pending batches now (shutdown, or synchronously in tests)."""
        for server in list(self._connect_tasks):
            self._cancel_connect_timeout(server)
        for server in list(self._list_flush_tasks):
            task = self._list_flush_tasks.pop(server, None)
            if task:
                task.cancel()
        for server in list(self._cmd_ack_tasks):
            task = self._cmd_ack_tasks.pop(server, None)
            if task:
                task.cancel()
        for topic_id in list(self._typing_tasks):
            self._stop_typing(topic_id)
        for topic_id in list(self._pending):
            task = self._flush_tasks.get(topic_id)
            if task:
                task.cancel()
            await self._flush(topic_id)

    def _show_event(self, e: IrcEvent) -> bool:
        if e.affects_me:
            return True   # events about you are always shown
        server = self._db.get_server(e.server)
        noise = (server or {}).get("noise_filter", "join,part,quit")
        muted = {x.strip() for x in noise.split(",") if x.strip()}
        return e.kind not in muted

    def _render_message(self, m: IrcMessage) -> str:
        if m.is_action:
            # weechat's action line already begins with the actor's nick
            # ("alice waves"), so render it as-is under the "*" rather than
            # prepending the nick a second time.
            return f"<i>* {mirc_to_html(m.text)}</i>"
        # Block/box ASCII art needs a monospace font to keep its shape; render it
        # in <pre> instead of the normal inline formatting. Missing glyphs still
        # show as boxes (the viewer's font, out of our control).
        if looks_like_art(m.text):
            body = f"<pre>{_escape(m.text)}</pre>"
        else:
            body = mirc_to_html(m.text)
            if m.highlight:
                # our nick was mentioned: make it stand out in the text and, when
                # a Telegram user is configured, turn it into a mention that pings.
                body = self._highlight_nick(body, self._irc.nick_for(m.server))
        if m.is_private:
            return body
        # A colon after the sender reads as "<who>: <what>", the way chat clients
        # attribute a line. A notice keeps its -nick- form (no colon) so it stays
        # visibly distinct from an ordinary message.
        prefix = f"<b>{_escape(m.nick)}</b>:"
        if m.is_notice:
            prefix = f"<b>-{_escape(m.nick)}-</b>"
        if m.highlight:
            prefix = f"{self._highlight_marker()} {prefix}"
        return f"{prefix} {body}"

    def _highlight_nick(self, body: str, nick: str) -> str:
        """Bold our nick where it appears so a mention stands out in the text.
        Whole-word and case-insensitive; nicks under two chars are skipped so they
        cannot match inside the HTML the formatter produced. The ping itself is
        carried by the mention marker, not here."""
        if not nick or len(nick) < 2:
            return body
        return re.sub(rf"(?<!\w){re.escape(nick)}(?!\w)",
                      lambda mo: f"<b>{mo.group(0)}</b>", body, flags=re.IGNORECASE)

    def _highlight_marker(self) -> str:
        # A public @username placed as plain text is resolved and notified by
        # Telegram server-side, so it pings reliably without the bot having to
        # resolve the user's id (which fails until the bot has "met" them). Fall
        # back to a tg://user link when only an id/name is known, else a bell.
        if self._mention_label.startswith("@"):
            return f"{_HIGHLIGHT} {self._mention_label}"
        if self._mention_user_id:
            inner = _HIGHLIGHT
            if self._mention_label:
                inner = f"{_HIGHLIGHT} {_escape(self._mention_label)}"
            return f'<a href="tg://user?id={self._mention_user_id}">{inner}</a>'
        return _HIGHLIGHT

    def _pick_owner(self) -> str:
        """The bot to own a new topic: the pool's least-loaded pick, or the
        gateway's primary bot when there is no pool (single-bot setup)."""
        if self._pool is None:
            return self._gw.owner_bot
        return self._pool.owner_for_new_topic(self._db.owner_topic_counts())

    def _owner_for(self, topic_id: int) -> str:
        """The bot that posts to this topic. Cached by _ensure_topic; a topic
        not seen this session (e.g. a command-reply target) falls back to the
        primary, which any bot in the group can post through."""
        return self._topic_owner.get(topic_id, self._gw.owner_bot)

    def _t(self, key: str, **params: object) -> str:
        if self._tr is not None:
            return self._tr.t(key, self._db.get("language", "en"), **params)
        return key

    async def _ensure_topic(self, buffer: str, title: str) -> int:
        existing = self._db.topic_for_buffer(buffer)
        if existing:
            self._topic_owner[existing["topic_id"]] = existing["owner_bot"]
            return existing["topic_id"]
        owner = self._pick_owner()
        topic_id = await self._gw.create_topic(title, owner_bot=owner)
        # save before the first send so a race cannot create a duplicate topic
        self._db.set_mapping(buffer, topic_id, owner)
        self._topic_owner[topic_id] = owner
        return topic_id

    def _fill_reply_nick(self, text: str, reply_to: Optional[int]) -> str:
        """When the user replies to a message and types a bare nick-command
        (e.g. "/whois" with no argument), append the replied-to sender's nick."""
        if reply_to is None:
            return text
        parts = text.split()
        if len(parts) != 1 or parts[0] not in _NICK_COMMANDS:
            return text
        rec = self._db.message_by_tg(self._gw.chat_id, reply_to)
        if rec and rec.get("nick"):
            return f"{parts[0]} {rec['nick']}"
        return text

    def _reply_nick(self, reply_to: Optional[int]) -> Optional[str]:
        """The sender nick of the message a Telegram reply points at, if known."""
        if reply_to is None:
            return None
        rec = self._db.message_by_tg(self._gw.chat_id, reply_to)
        return rec.get("nick") if rec else None

    @staticmethod
    def _conversation_target(buffer: str) -> Optional[str]:
        """The IRC target (channel or nick) a TAGMSG would address for this
        buffer, or None for the server buffer (its messages, e.g. NickServ, have
        no conversation to react into)."""
        parts = buffer.split(".", 2)
        if len(parts) != 3 or parts[1] == "server":
            return None
        return parts[2]

    async def request_backfill(self, buffer: str,
                               last_seen: Optional[str]) -> None:
        """Ask the server for the messages missed on this channel/PM while we
        were away, via the IRCv3 draft/chathistory extension. Wired to fire on
        (re)open; best-effort - the request goes out on the server buffer and a
        network without the extension ignores it. The replayed lines return
        through the normal stream and are de-duplicated in _on_message by msgid,
        so this never double-posts. No-op for the server buffer (no target)."""
        target = self._conversation_target(buffer)
        if target is None:
            return
        server = buffer.split(".", 2)[1]
        line = build_chathistory_request(target, last_seen)
        await self._irc.send_command(f"irc.server.{server}", line)

    async def handle_telegram_reaction(self, message_id: int, emoji: str) -> None:
        """A Telegram reaction was added to a mirrored message: place the same
        emoji on the IRC message as an IRCv3 draft reaction. Best-effort - a
        message we never mirrored, or one with no IRC msgid, is left alone, and a
        network lacking the extension silently drops the tags."""
        rec = self._db.message_by_tg(self._gw.chat_id, message_id)
        if not rec or not rec.get("irc_msgid"):
            return
        target = self._conversation_target(rec["buffer"])
        if target is None:
            return
        line = build_react_quote(target, rec["irc_msgid"], emoji)
        await self._irc.send_command(rec["buffer"], line)

    async def handle_incoming_file(self, nick: str, server: str,
                                   path: str) -> None:
        """Re-host a file received over DCC and post its link to the sender's PM
        topic. DCC is one-to-one, so the sender nick maps to the private topic
        irc.<server>.<nick>. A "receiving" notice goes up first, then it is
        edited in place to the gofile link (or to a failure line if the upload
        raised), so the topic shows one evolving message instead of two."""
        buffer = f"irc.{server}.{nick}"
        topic_id = await self._ensure_topic(buffer, f"{server} · {nick}")
        # The on-disk name carries the nick prefix; strip it for a clean display
        # name (the sender is already identified by the topic).
        _prefix, name = parse_xfer_filename(os.path.basename(path))
        owner = self._owner_for(topic_id)
        notice = await self._gw.send(
            topic_id, self._t("files.receiving", name=_escape(name)),
            owner_bot=owner)
        try:
            link = await self._upload(path)
        except Exception as exc:
            print(f"[router] incoming file re-host failed for {name}: {exc}")
            await self._gw.edit_message(
                notice, self._t("files.failed", name=_escape(name)),
                owner_bot=owner)
            return
        await self._gw.edit_message(
            notice, self._t("files.uploaded", name=_escape(name), link=link),
            owner_bot=owner)

    async def handle_outgoing_file(self, topic_id: int, path: str) -> None:
        """Re-host a file a user sent from Telegram and post its gofile link into
        the mapped IRC conversation (channel or PM). An unmapped topic is a no-op.
        On upload failure nothing is sent to IRC (sending a failure line into a
        channel would only be noise); the error is logged for the operator."""
        buffer = self._db.buffer_for_topic(topic_id)
        if buffer is None:
            return
        name = os.path.basename(path)
        try:
            link = await self._upload(path)
        except Exception as exc:
            print(f"[router] outgoing file re-host failed for {name}: {exc}")
            return
        for line in split_for_irc(self._t("files.sent_link", name=name, link=link)):
            await self._irc.send_message(buffer, line)

    async def handle_telegram(self, topic_id: int, message_id: int, text: str,
                              reply_to: Optional[int] = None) -> None:
        buffer = self._db.buffer_for_topic(topic_id)
        if buffer is None:
            return
        text = self._fill_reply_nick(text, reply_to)
        is_command = text.startswith("/")
        parts = buffer.split(".")
        server = parts[1] if len(parts) >= 2 else ""
        # Arm the ack and show "working" BEFORE sending a command: react() is a
        # slow API call, and a fast server (a local one especially) can answer
        # before it returns. The reply must find the ack already armed and land
        # after the 👀, so it flips to 👌 instead of the 👀 overwriting the flip.
        if is_command and server:
            # Remember where this command came from so its server replies (whois,
            # list, ...) land back in this topic, not the console.
            self._cmd_origin[server] = (topic_id, time.monotonic() + _CMD_REPLY_WINDOW)
            self._arm_cmd_ack(server, message_id)
            await self._gw.react(message_id, _ACK_WORKING)
        try:
            if is_command:
                # A pasted block can hold several commands, one per line; run each
                # so "/nick x" + "/msg NickServ ..." both fire, not just the first.
                for cmd_line in text.split("\n"):
                    cmd_line = cmd_line.strip()
                    if cmd_line:
                        await self._irc.send_command(buffer, cmd_line)
            else:
                # A Telegram message can carry newlines and exceed the IRC line
                # limit; send each wrapped line as its own PRIVMSG, in order.
                lines = split_for_irc(text)
                # Replying to a channel message addresses the sender the universal
                # IRC way ("nick: text"), on the first line only. Pointless in a PM.
                if lines and reply_to is not None and _is_channel_buffer(buffer):
                    nick = self._reply_nick(reply_to)
                    if nick:
                        lines[0] = f"{nick}: {lines[0]}"
                for line in lines:
                    await self._irc.send_message(buffer, line)
            ok = True
        except Exception as exc:
            print(f"[router] telegram->irc failed: {exc}")
            ok = False
        # Reaction feedback so the user sees what happened. A command shows
        # "working" until its reply comes back (or the ack timeout); a plain
        # message just shows delivered; a failed send shows the down mark.
        if not ok:
            self._cancel_cmd_ack(server, message_id)   # undo the armed 👀 ack
            await self._gw.react(message_id, _ACK_FAILED)
        elif not is_command:
            await self._gw.react(message_id, _ACK_DELIVERED)
        # a command's 👀 is already shown and its ack armed; the reply (or the
        # timeout) flips it to 👌.

    def _arm_cmd_ack(self, server: str, message_id: int) -> None:
        # Queue this command's message, do not replace: two commands sent close
        # together on one server (a /mode then a /kick) must both flip, not leave
        # the first stuck on "working" when the second overwrote it. The reply is
        # not correlated to a specific command (IRC gives no such link), so the
        # next reply (or the timeout) resolves every command still waiting.
        self._cmd_ack.setdefault(server, []).append(message_id)
        task = self._cmd_ack_tasks.pop(server, None)
        if task is not None:
            task.cancel()
        self._cmd_ack_tasks[server] = asyncio.create_task(
            self._cmd_ack_later(server))

    def _cancel_cmd_ack(self, server: str, message_id: int) -> None:
        """Drop one command's pending ack (its send failed, so it will get 👎 and
        must not later be flipped to 👌 by a reply or the timeout)."""
        pending = self._cmd_ack.get(server)
        if not pending or message_id not in pending:
            return
        pending.remove(message_id)
        if not pending:
            self._cmd_ack.pop(server, None)
            task = self._cmd_ack_tasks.pop(server, None)
            if task is not None:
                task.cancel()

    async def _cmd_ack_later(self, server: str) -> None:
        try:
            await asyncio.sleep(self._cmd_ack_timeout)
        except asyncio.CancelledError:
            return
        self._cmd_ack_tasks.pop(server, None)
        await self._ack_reply(server)   # no textual reply came: mark done anyway

    async def _ack_reply(self, server: str) -> None:
        """Flip every command still "working" on this server to done once a reply
        arrives (or the timeout fires). A no-op when nothing is waiting."""
        pending = self._cmd_ack.pop(server, None)
        if not pending:
            return
        task = self._cmd_ack_tasks.pop(server, None)
        if task is not None:
            task.cancel()
        for message_id in pending:
            await self._gw.react(message_id, _ACK_CMD_DONE)
