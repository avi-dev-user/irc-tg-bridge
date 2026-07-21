"""IRC seen as structured messages and events, on top of the WeeChat relay.

WeeChat delivers every line with a tag list that says what it is
(irc_privmsg, irc_notice, irc_join, ...), who sent it (nick_<nick>), and the
IRCv3 metadata (irc_tag_msgid=..., irc_tag_time=...). `parse_line` turns that
plus the buffer's local variables into either a conversation message or a
non-message event, or nothing for pure noise. It is pure, so it is tested
directly against captured real WeeChat output.

The backend interface (start/stream/send) is what the router talks to, so a
phase-2 built-in IRC engine can replace this without touching the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Iterable, Optional

if TYPE_CHECKING:
    from .weechat_relay import WeechatRelay

# tags that mark a line as a real conversation message
_MSG_TAGS = ("irc_privmsg", "irc_action")
# non-message IRC events we surface (rendered for the Telegram side only)
_EVENT_TAGS = {
    "irc_join": "join", "irc_part": "part", "irc_quit": "quit",
    "irc_kick": "kick", "irc_mode": "mode", "irc_nick": "nick",
    "irc_topic": "topic", "irc_invite": "invite",
}


@dataclass
class IrcMessage:
    server: str
    buffer: str          # full WeeChat buffer name, e.g. irc.libera.#weechat
    conversation: str    # channel (#x) or the other nick for a PM
    nick: str            # sender
    text: str
    is_private: bool     # PM vs channel
    is_action: bool = False
    is_notice: bool = False
    is_self: bool = False
    highlight: bool = False
    msgid: Optional[str] = None
    time: Optional[str] = None
    reply_to_msgid: Optional[str] = None  # IRCv3 reply tag: the msgid replied to


@dataclass
class IrcEvent:
    server: str
    buffer: str
    kind: str            # join | part | quit | kick | mode | nick | topic | invite | server
    text: str
    affects_me: bool = False
    numeric: Optional[int] = None   # IRC numeric (e.g. 372 MOTD) for server lines
    lifecycle: Optional[str] = None  # "opened" | "closed": our own channel join/leave
    nick: Optional[str] = None      # the acting nick, when the line names one


@dataclass
class IrcReaction:
    """An IRCv3 draft reaction (a TAGMSG carrying +draft/react= and a
    +draft/reply= target) placing an emoji on an earlier message."""
    server: str
    buffer: str
    target_msgid: str    # the msgid the reaction is attached to
    emoji: str
    nick: str
    is_self: bool = False   # our own reaction echoed back, do not mirror it again


@dataclass
class IrcRedact:
    """An IRCv3 draft message redaction (a REDACT command or a +draft/delete=
    tag) removing an earlier message."""
    server: str
    buffer: str
    target_msgid: str    # the msgid being redacted


@dataclass
class IrcTyping:
    """An IRCv3 draft/typing notification (a TAGMSG carrying +typing=active|
    paused|done) from another user in a channel or PM."""
    server: str
    buffer: str
    nick: str
    state: str            # active | paused | done
    is_self: bool = False


def _tag_value(tags: list[str], prefix: str) -> Optional[str]:
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def _reply_target(tags: list[str]) -> Optional[str]:
    """The msgid an IRCv3 reply points at, tolerating the tag forms WeeChat may
    expose (+draft/reply / +reply, with or without the irc_tag_ prefix)."""
    for t in tags:
        for marker in ("+draft/reply=", "draft/reply=", "+reply="):
            if marker in t:
                return t.split(marker, 1)[1]
    return None


def _typing_state(tags: list[str]) -> Optional[str]:
    """The state of an IRCv3 draft/typing tag (active|paused|done), tolerating
    the tag forms WeeChat may expose. None when the line carries no typing tag."""
    for t in tags:
        for marker in ("+typing=", "draft/typing="):
            if marker in t:
                return t.split(marker, 1)[1]
    return None


def _react_emoji(tags: list[str]) -> Optional[str]:
    """The emoji carried by an IRCv3 draft reaction tag, tolerating the tag
    forms WeeChat may expose (+draft/react / draft/react, with or without the
    irc_tag_ prefix). None when the line carries no reaction tag."""
    for t in tags:
        for marker in ("+draft/react=", "draft/react="):
            if marker in t:
                return t.split(marker, 1)[1]
    return None


def _redact_target(tags: list[str], text: str) -> Optional[str]:
    """The msgid a redaction removes: from a +draft/delete= tag when present,
    else the msgid argument of a REDACT command line (irc_redact). Returns None
    when the line is not a redaction."""
    for t in tags:
        for marker in ("+draft/delete=", "draft/delete="):
            if marker in t:
                return t.split(marker, 1)[1]
    if "irc_redact" in tags:
        # REDACT <target> <msgid> [:reason]. WeeChat may or may not keep the
        # verb on the relayed line, so drop a leading REDACT token if present and
        # read the msgid as the argument after the target either way.
        parts = text.split()
        if parts and parts[0].upper() == "REDACT":
            parts = parts[1:]
        if len(parts) >= 2:
            return parts[1]
    return None


# WeeChat routes /LIST output to a dedicated buffer named "irc.list_<server>"
# (local_variables type "list"), not to the server buffer.
_LIST_BUFFER_PREFIX = "irc.list_"


def _list_server_from_name(name: str) -> str:
    """The server behind WeeChat's dedicated /LIST buffer, whose name is
    "irc.list_<server>" (e.g. "irc.list_libera" -> "libera"), or ""
    when the name is not a list buffer."""
    if name.startswith(_LIST_BUFFER_PREFIX):
        return name[len(_LIST_BUFFER_PREFIX):]
    return ""


# How many missed lines to pull per backfill request. One screenful is plenty
# to catch up a short outage without flooding the topic on a long one.
_CHATHISTORY_LIMIT = 100


def build_chathistory_request(target: str, last_seen: Optional[str]) -> str:
    """The raw WeeChat /quote line that asks the server for messages missed on
    <target> (a channel or nick) while we were away, using the IRCv3
    draft/chathistory extension. With a high-water msgid we fetch everything
    AFTER it; with none (first time seen on this buffer) we fetch the LATEST
    batch. WeeChat's /quote passes the line to the server verbatim, so a network
    without the extension simply ignores it. The replayed lines arrive through
    the normal buffer_line_added stream and are de-duplicated by their msgid."""
    if last_seen:
        return f"/quote CHATHISTORY AFTER {target} msgid={last_seen} {_CHATHISTORY_LIMIT}"
    return f"/quote CHATHISTORY LATEST {target} * {_CHATHISTORY_LIMIT}"


def _notice_body(text: str) -> str:
    """WeeChat renders an IRC notice line as "Notice(nick) -> target: body" (and
    "Notice(nick): body" for a private one). The bridge shows the sender with its
    own "-nick-" prefix, so keep only the body to avoid doubling that wrapper. A
    line that does not carry the wrapper is returned unchanged."""
    if text.startswith("Notice(") and ": " in text:
        return text.split(": ", 1)[1]
    return text


def parse_line(buffer: dict, body: dict, our_nick: Optional[str]):
    """buffer: {name, type, server, conversation}. body: a relay line body.
    Returns IrcMessage, IrcEvent, or None (noise not worth forwarding)."""
    tags = body.get("tags", [])
    text = body.get("message", "")
    btype = buffer.get("type")
    server = buffer.get("server", "")
    nick = _tag_value(tags, "nick_")

    # IRCv3 draft reactions and redactions arrive as TAGMSG / REDACT, not as
    # ordinary conversation lines, so recognise them before the message/event
    # handling. Both are draft extensions; a network without them simply never
    # sends these, so the checks fall through harmlessly.
    emoji = _react_emoji(tags)
    if emoji is not None:
        target = _reply_target(tags)
        if target is None:
            return None   # a reaction with no target message is unusable
        is_self = "self_msg" in tags or (our_nick is not None and nick == our_nick)
        return IrcReaction(server=server, buffer=buffer["name"],
                           target_msgid=target, emoji=emoji, nick=nick or "",
                           is_self=is_self)
    redact_target = _redact_target(tags, text)
    if redact_target is not None:
        return IrcRedact(server=server, buffer=buffer["name"],
                         target_msgid=redact_target)
    typing_state = _typing_state(tags)
    if typing_state is not None:
        is_self = "self_msg" in tags or (our_nick is not None and nick == our_nick)
        return IrcTyping(server=server, buffer=buffer["name"], nick=nick or "",
                         state=typing_state, is_self=is_self)

    is_notice = "irc_notice" in tags
    is_action = "irc_action" in tags
    is_msg = any(t in tags for t in _MSG_TAGS) or (is_notice and btype in ("channel", "private"))

    if is_msg:
        is_self = "self_msg" in tags or (our_nick is not None and nick == our_nick)
        return IrcMessage(
            server=server,
            buffer=buffer["name"],
            conversation=buffer.get("conversation", ""),
            nick=nick or "",
            text=_notice_body(text) if is_notice else text,
            is_private=(btype == "private"),
            is_action=is_action,
            is_notice=is_notice,
            is_self=is_self,
            highlight=bool(body.get("highlight")),
            msgid=_tag_value(tags, "irc_tag_msgid="),
            time=_tag_value(tags, "irc_tag_time="),
            reply_to_msgid=_reply_target(tags),
        )

    numeric = None
    for t in tags:
        if t.startswith("irc_") and t[4:].isdigit():
            numeric = int(t[4:])
            break

    kind = next((k for tag, k in _EVENT_TAGS.items() if tag in tags), None)
    if kind:
        if kind in ("join", "part") and nick and nick == our_nick:
            return None  # our own join/leave; the buffer opened/closed signal reports it
        affects = bool(our_nick and (nick == our_nick or our_nick in text))
        return IrcEvent(server=server, buffer=buffer["name"], kind=kind,
                        text=text, affects_me=affects, numeric=numeric, nick=nick)

    # server notices/numerics: surface on the server buffer only, as context.
    if btype == "server" and (is_notice or "irc_numeric" in tags):
        return IrcEvent(server=server, buffer=buffer["name"], kind="server",
                        text=text, numeric=numeric)

    # RPL_LIST rows land on WeeChat's dedicated "list" buffer, not the server
    # buffer. WeeChat prints them pre-formatted with empty tags ("#chan  12 topic")
    # and sends no RPL_LISTEND line, so a tagged numeric is surfaced as-is while a
    # bare channel row (recognised by its prefix) gets a synthetic 322 for the
    # collector; the header and blank lines carry no prefix and drop out. The
    # server comes from the buffer's local variable, falling back to the
    # "irc.list_<server>" name so it matches the plain name discovery expects.
    if btype == "list":
        list_server = server or _list_server_from_name(buffer["name"])
        if numeric is not None or "irc_numeric" in tags:
            return IrcEvent(server=list_server, buffer=buffer["name"],
                            kind="server", text=text, numeric=numeric)
        if text.lstrip()[:1] in ("#", "&"):
            return IrcEvent(server=list_server, buffer=buffer["name"],
                            kind="server", text=text, numeric=322)
        return None

    return None


def channel_join_event(info: dict):
    """A channel buffer opening means we just joined it. This is a reliable
    self-join signal: it does not depend on knowing our nick, which may not be
    registered yet when the IRC join line arrives (e.g. a forced join right
    after connect)."""
    if info.get("type") != "channel":
        return None
    channel = info.get("conversation") or info.get("name", "")
    return IrcEvent(server=info.get("server", ""), buffer=info["name"],
                    kind="join", text=f"✓ Joined {channel}".rstrip(),
                    affects_me=True, lifecycle="opened")


def channel_close_event(info: dict):
    """A channel buffer closing means we left it (via /part, a kick, or the
    server dropping). The mirror of channel_join_event, and just as reliable."""
    if info is None or info.get("type") != "channel":
        return None
    channel = info.get("conversation") or info.get("name", "")
    return IrcEvent(server=info.get("server", ""), buffer=info["name"],
                    kind="part", text=f"🔒 No longer in {channel}".rstrip(),
                    affects_me=True, lifecycle="closed")


def private_open_event(info: dict):
    """A private buffer opening (e.g. /query <nick>, or the other side messaging
    first) means a PM conversation now exists. Unlike a channel join it is not
    worth announcing; it only needs its Telegram topic to exist so the PM has a
    home before the first line arrives. Carries no text so the router emits no
    body, just ensures the topic."""
    if info is None or info.get("type") != "private":
        return None
    return IrcEvent(server=info.get("server", ""), buffer=info["name"],
                    kind="private", text="", lifecycle="opened")


def connected_servers(buffer_infos: Iterable[dict]) -> set[str]:
    """The servers we are currently registered on, read straight from their
    WeeChat server buffers. A server buffer whose local variables carry a
    non-empty nick means registration completed (RPL_WELCOME was received), so
    we are connected to that network. Pure: buffer infos in, server names out."""
    return {
        info["server"]
        for info in buffer_infos
        if info.get("type") == "server" and info.get("nick") and info.get("server")
    }


def reconcile_server_status(db, connected: set[str]) -> None:
    """Align each known server's stored status with reality after a (re)connect.

    A fresh RPL_WELCOME is what normally flips a server to "connected", but on a
    bridge restart those already-registered servers send no new welcome, so their
    badge stays stale. Here we set every server the db knows to "connected" if it
    reports a nick, "disconnected" otherwise. Servers present in WeeChat but not
    in the db are left untouched; the real 001 handling is unaffected.

    A server mid-connect ("connecting") is skipped: a relay reconnect can land
    inside a user-initiated connect before the server buffer has a nick, and
    forcing it to "disconnected" here would clobber that pending state (and make
    the connect-timeout callback suppress its "failed" notice). The 001/timeout
    machinery owns the transient state, so we leave it alone."""
    for row in db.list_servers():
        if row.get("status") == "connecting":
            continue
        name = row["name"]
        db.set_server_status(name, "connected" if name in connected else "disconnected")


class WeechatIrcBackend:
    """Streams IrcMessage/IrcEvent from WeeChat and sends into IRC."""

    def __init__(self, relay: WeechatRelay):
        self._relay = relay
        self._buffers: dict[int, dict] = {}   # buffer_id -> parsed buffer info
        self._nicks: dict[str, str] = {}       # server -> our nick

    @staticmethod
    def _buffer_info(raw: dict) -> dict:
        lv = raw.get("local_variables", {})
        name = raw["name"]
        btype = lv.get("type", "core" if lv.get("plugin") == "core" else "")
        server = lv.get("server", "")
        # WeeChat's dedicated /LIST buffer ("irc.list_<server>"). Its type is
        # "list" and its local variables carry the server, but fall back to the
        # name prefix for both if those variables are ever absent, so RPL_LIST
        # numerics still carry the plain server name channel discovery expects.
        if name.startswith(_LIST_BUFFER_PREFIX):
            if not btype:
                btype = "list"
            if not server:
                server = _list_server_from_name(name)
        return {
            "id": raw["id"],
            "name": name,
            "type": btype,
            "server": server,
            "conversation": lv.get("channel", raw.get("short_name", "")),
            "nick": lv.get("nick"),
        }

    async def start(self) -> None:
        # Clear any stale state so this is safe to call again after a reconnect
        # (buffer ids are only valid within one WeeChat session).
        self._buffers.clear()
        self._nicks.clear()
        for raw in await self._relay.list_buffers():
            info = self._buffer_info(raw)
            self._buffers[info["id"]] = info
            if info["type"] == "server" and info["nick"]:
                self._nicks[info["server"]] = info["nick"]
        await self._relay.enable_sync()

    def connected_servers(self) -> set[str]:
        """Servers whose server buffer currently reports a nick (we are
        registered on them). Valid once start() has listed the buffers."""
        return connected_servers(self._buffers.values())

    def nick_for(self, server: str) -> str:
        """Our current nick on a server, so a mention of it can be highlighted in
        the message text. Empty when we are not (yet) registered there."""
        return self._nicks.get(server, "")

    async def stream(self) -> AsyncIterator[object]:
        async for ev in self._relay.events():
            name = ev.get("event_name")
            if name in ("buffer_opened",):
                info = self._buffer_info(ev["body"])
                self._buffers[info["id"]] = info
                if info["type"] == "server" and info["nick"]:
                    self._nicks[info["server"]] = info["nick"]
                # A channel buffer opening = we joined it; a private buffer
                # opening = a PM now exists and needs its topic. At most one
                # of these matches (a buffer is one type).
                signal = channel_join_event(info) or private_open_event(info)
                if signal is not None:
                    yield signal
            elif name == "buffer_closed":
                info = self._buffers.pop(ev.get("buffer_id"), None)
                close = channel_close_event(info)
                if close is not None:
                    yield close   # closing a channel buffer = we left it
            elif name == "buffer_line_added":
                buf = self._buffers.get(ev.get("buffer_id"))
                if buf is None:
                    continue
                parsed = parse_line(buf, ev["body"], self._nicks.get(buf["server"]))
                if parsed is not None:
                    yield parsed

    async def send_message(self, buffer: str, text: str) -> None:
        await self._relay.input(buffer, text)

    async def send_command(self, buffer: str, command: str) -> None:
        if not command.startswith("/"):
            command = "/" + command
        await self._relay.input(buffer, command)
