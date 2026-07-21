"""Admin management: the guided add-server flow, the WeeChat commands it
produces, and parsing for channel discovery.

The flow and command building are pure so they are tested without Telegram or a
live relay. Executing the commands and updating the database is a thin wrapper
on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

AUTH_METHODS = ("sasl", "nickserv", "none")


def is_valid_nick(value: str) -> bool:
    """An IRC nick must be ASCII with no spaces; a Hebrew nick is rejected by the
    server ("Erroneous Nickname") and never registers. Shared by the add-server
    flow and the server-view nick change so both accept exactly the same set."""
    v = value.strip()
    return bool(v) and v.isascii() and not any(c.isspace() for c in v)


def strip_bot_mention(text: str) -> str:
    """Telegram appends @botname to a slash command typed in a group. IRC must
    not see it (/part@thebot is not a command), so drop it from the command word
    while leaving the arguments untouched (a nick like nick@host is preserved)."""
    if not text.startswith("/"):
        return text
    head, sep, rest = text.partition(" ")
    return head.split("@", 1)[0] + sep + rest


@dataclass
class AddServerFlow:
    """Collects a server definition step by step. Each `feed` returns the i18n
    key to prompt next, or None when complete. Raises ValueError on bad input
    so the caller can re-prompt the same step."""
    step: int = 0
    data: dict = field(default_factory=dict)

    # (field, prompt key, kind, options). Choice steps are answered by buttons;
    # text steps by a typed message. The password step is skipped for auth none.
    STEPS = [
        ("name", "addserver.name", "text", None),
        ("host", "addserver.host", "text", None),
        ("port", "addserver.port", "text", None),
        ("tls", "addserver.tls", "choice", ["yes", "no"]),
        ("nick", "addserver.nick", "text", None),
        ("auth", "addserver.auth", "choice", ["sasl", "nickserv", "none"]),
        ("password", "addserver.sasl", "text", None),
        # one privacy choice replaces the old separate tor/anon questions, which
        # overlapped (anonymity implies Tor). off = plain, tor = hide IP via Tor,
        # anon = full anonymity (Tor + scrubbed identity, see build_anon_commands).
        ("privacy", "addserver.privacy", "choice", ["off", "tor", "anon"]),
    ]

    def current(self):
        return self.STEPS[self.step] if self.step < len(self.STEPS) else None

    def prompt_key(self) -> Optional[str]:
        step = self.current()
        return step[1] if step else None

    def is_choice(self) -> bool:
        step = self.current()
        return bool(step) and step[2] == "choice"

    def back(self) -> None:
        if self.step > 0:
            self.step -= 1
            # step back over the password step when auth is none (it is skipped)
            if self.STEPS[self.step][0] == "password" and self.data.get("auth") == "none":
                self.step -= 1

    def options(self):
        step = self.current()
        return step[3] if step else None

    def feed(self, value: str) -> Optional[str]:
        field_name = self.STEPS[self.step][0]
        validated = self._validate(field_name, value)
        if field_name == "password" and validated is None \
                and self.data.get("auth") in ("sasl", "nickserv"):
            raise ValueError("a password is required for sasl/nickserv")
        self.data[field_name] = validated
        self.step += 1
        # auth "none" has no password to ask for
        if self.step < len(self.STEPS) and self.STEPS[self.step][0] == "password" \
                and self.data.get("auth") == "none":
            self.step += 1
        return self.prompt_key()

    def is_complete(self) -> bool:
        return self.step >= len(self.STEPS)

    @staticmethod
    def _validate(field_name: str, value: str):
        v = value.strip()
        if field_name == "name":
            if not v or any(c.isspace() for c in v):
                raise ValueError("name must be a single word")
            # The name is packed into callback_data (menu.cb("srv", ..., name)),
            # which Telegram caps at 64 bytes; bound it so any name fits.
            if len(v) > 30:
                raise ValueError("name must be at most 30 characters")
            return v
        if field_name == "host":
            if not v or any(c.isspace() for c in v):
                raise ValueError("host must be a single word (name or .onion)")
            return v
        if field_name == "port":
            if not v.isdigit():
                raise ValueError("port must be a number")
            return int(v)
        if field_name == "nick":
            if not is_valid_nick(v):
                raise ValueError("nick must be ASCII letters/digits, no spaces")
            return v
        if field_name == "auth":
            if v.lower() not in AUTH_METHODS:
                raise ValueError(f"auth must be one of {AUTH_METHODS}")
            return v.lower()
        if field_name == "password":
            return None if v.lower() == "skip" else v
        if field_name == "tls":
            return v.lower() in ("yes", "y", "1", "true", "כן")
        if field_name == "privacy":
            if v.lower() not in ("off", "tor", "anon"):
                raise ValueError("privacy must be off, tor, or anon")
            return v.lower()
        return v


def build_addserver_commands(d: dict) -> list[str]:
    """Translate a completed flow into WeeChat commands (run on core.weechat).
    Secrets go through secured data, never inline in the server options."""
    name = d["name"]
    host, port, nick = d["host"], d["port"], d["nick"]
    auth = d.get("auth", "none")
    privacy = d.get("privacy", "off")
    use_tor = privacy in ("tor", "anon")
    # TLS is opt-in: a plaintext or local test server would fail a TLS handshake.
    tls_flag = " -tls" if d.get("tls") else ""
    cmds = [
        f"/server add {name} {host}/{port}{tls_flag}",
        f"/set irc.server.{name}.nicks {nick}",
    ]
    if use_tor:
        # The server references a proxy named "tor"; create it first or the
        # connection fails with "proxy tor not found".
        cmds.insert(0, "/proxy add tor socks5 127.0.0.1 9050")
    password = d.get("password")
    if auth == "sasl" and password:
        cmds.append(f"/secure set {name}_pass {password}")
        cmds.append(f"/set irc.server.{name}.sasl_mechanism plain")
        cmds.append(f"/set irc.server.{name}.sasl_username {nick}")
        cmds.append(f'/set irc.server.{name}.sasl_password "${{sec.data.{name}_pass}}"')
    elif auth == "nickserv" and password:
        cmds.append(f"/secure set {name}_pass {password}")
        cmds.append(
            f'/set irc.server.{name}.command "/msg NickServ IDENTIFY ${{sec.data.{name}_pass}}"'
        )
    if use_tor:
        cmds.append(f"/set irc.server.{name}.proxy tor")
    cmds.append(f"/connect {name}")
    return cmds


def parse_list_reply(message: str) -> Optional[dict]:
    """Parse a RPL_LIST (322) line body: '#channel <users> :<topic>'."""
    parts = message.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(("#", "&")):
        return None
    channel, users = parts[0], parts[1]
    if not users.isdigit():
        return None
    topic = parts[2].lstrip(":") if len(parts) == 3 else ""
    return {"channel": channel, "users": int(users), "topic": topic}


# Membership prefixes a nick can carry in a NAMES reply, most powerful first:
# ~ owner, & admin, @ op, % halfop, + voice. We surface the leading one.
_NAME_PREFIXES = "~&@%+"


def parse_names_reply(message: str) -> Optional[dict]:
    """Parse a RPL_NAMREPLY (353) line body: '<symbol> #channel :nick1 nick2 ...'.

    The client target is already stripped (as with RPL_LIST/WHOIS), leaving the
    channel-visibility symbol (=/*/@), the channel, then the space-separated
    members after the colon. Each member may carry one status prefix. Returns
    {channel, members: [{prefix, nick}, ...]} or None when malformed."""
    head, sep, trailing = message.partition(":")
    if not sep:
        return None
    parts = head.split()
    # the channel is the token after the visibility symbol (=/*/@), or leads the
    # head when a relay dropped the symbol; either way it must be a real channel.
    if len(parts) >= 2 and parts[0] in ("=", "*", "@"):
        channel = parts[1]
    elif parts:
        channel = parts[0]
    else:
        return None
    if not channel.startswith(("#", "&", "+", "!")):
        return None
    members = []
    for token in trailing.split():
        prefix = ""
        if token[0] in _NAME_PREFIXES:
            prefix, token = token[0], token[1:]
        if token:
            members.append({"prefix": prefix, "nick": token})
    return {"channel": channel, "members": members}
