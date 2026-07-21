"""Human-friendly text for cryptic IRC error numerics.

IRC servers report failures as bare numeric codes: a join rejected with 477, a
message bounced with 404. The raw line already carries the channel/target and
the server's own reason, but the numeric itself is opaque ("#tldev: You need a
registered nick" leaves a user guessing). friendly_numeric turns a known error
into one clear English sentence, keeping the server's target/reason so the
message stays specific. Pure and import-free, so it is tested directly.
"""

from __future__ import annotations

from typing import Optional

_CHANNEL_PREFIXES = "#&+!"


def _split(text: str) -> tuple[str, str]:
    """Split an IRC error text into a leading target (channel/nick/command) and
    the server's human reason. Handles the raw ' :' form and WeeChat's
    'target: reason' display form; otherwise treats it all as the reason unless
    it opens with a channel name."""
    text = (text or "").strip()
    if not text:
        return "", ""
    if " :" in text:
        head, reason = text.split(" :", 1)
    elif ": " in text:
        head, reason = text.split(": ", 1)
    else:
        parts = text.split(None, 1)
        if parts and parts[0][:1] in _CHANNEL_PREFIXES:
            head = parts[0]
            reason = parts[1] if len(parts) > 1 else ""
        else:
            head, reason = "", text
    return head.strip().rstrip(":"), reason.strip()


def _t(target: str, templated: str, generic: str) -> str:
    return templated.format(t=target) if target else generic


def _tail(reason: str) -> str:
    return f" ({reason})" if reason else ""


# numeric -> builder(target, reason) -> message. Only errors worth explaining;
# an unmapped numeric (welcome, MOTD, whois, ...) returns None and is untouched.
_BUILDERS = {
    401: lambda t, r: _t(
        t, "No such nick or channel: {t}. It may be offline, or the name is misspelled.",
        "No such nick or channel; it may be offline, or the name is misspelled."),
    402: lambda t, r: _t(
        t, "No such server: {t} could not be found.",
        "No such server: that server name could not be found."),
    403: lambda t, r: _t(
        t, "No such channel: {t} does not exist on this network.",
        "No such channel; it does not exist on this network."),
    404: lambda t, r: _t(
        t, "Cannot send to {t}: the channel is not accepting your message (it may be "
           "moderated, you may be banned, or it blocks messages from non-members).",
        "Cannot send to that channel: it may be moderated, you may be banned, or it "
        "blocks messages from non-members."),
    405: lambda t, r: _t(
        t, "Too many channels: you have joined the maximum allowed, so you cannot join {t}.",
        "Too many channels: you have joined the maximum allowed and cannot join another."),
    406: lambda t, r: _t(
        t, "No record of anyone ever using the nick {t}.",
        "No record of anyone ever using that nick."),
    411: lambda t, r: "No recipient given: name who the message is for" + _tail(r) + ".",
    412: lambda t, r: "No text to send: the message was empty" + _tail(r) + ".",
    421: lambda t, r: _t(
        t, "Unknown command: the server does not understand {t}.",
        "Unknown command: the server did not recognise that command."),
    432: lambda t, r: _t(
        t, "Invalid nickname: {t} is not allowed on this network.",
        "Invalid nickname: that name is not allowed on this network."),
    433: lambda t, r: _t(
        t, "Nickname already in use: {t} is taken. Choose a different nick.",
        "Nickname already in use. Choose a different nick."),
    436: lambda t, r: _t(
        t, "Nick collision: {t} clashed with another user on the network and was dropped.",
        "Nick collision: your nick clashed with another user on the network and was dropped."),
    437: lambda t, r: _t(
        t, "{t} is temporarily unavailable; try again in a moment.",
        "That nick or channel is temporarily unavailable; try again in a moment."),
    441: lambda t, r: "That user is not on that channel, so the action does not apply.",
    442: lambda t, r: _t(
        t, "You are not on {t}, so that action does not apply.",
        "You are not on that channel, so that action does not apply."),
    443: lambda t, r: _t(
        t, "You are already on {t}.",
        "You are already on that channel."),
    451: lambda t, r: (
        "You have not registered yet: finish connecting before using that command"
        + _tail(r) + "."),
    # Built with an f-string, not _t: this message needs both the target and the
    # reason, while _t only fills in the target.
    461: lambda t, r: (
        f"Not enough parameters for {t}: the command is missing a "
        f"required argument{_tail(r)}." if t else
        f"Not enough parameters: the command is missing a required argument{_tail(r)}."),
    462: lambda t, r: "You are already registered and cannot register again" + _tail(r) + ".",
    464: lambda t, r: "Wrong server password" + _tail(r) + ".",
    465: lambda t, r: "You are banned from this server" + _tail(r) + ".",
    471: lambda t, r: _t(
        t, "{t} is full (+l); it has reached its user limit, so you cannot join right now.",
        "The channel is full (+l); it has reached its user limit, so you cannot join right now."),
    472: lambda t, r: _t(
        t, "Unknown mode character: {t} is not a mode this server supports.",
        "Unknown mode character" + _tail(r) + "."),
    473: lambda t, r: _t(
        t, "{t} is invite-only (+i); you need an invite to join.",
        "That channel is invite-only (+i); you need an invite to join."),
    474: lambda t, r: _t(
        t, "You are banned from {t} (+b) and cannot join.",
        "You are banned from that channel (+b) and cannot join."),
    475: lambda t, r: _t(
        t, "{t} needs a key (+k); include the correct channel password when you join.",
        "That channel needs a key (+k); include the correct channel password when you join."),
    476: lambda t, r: _t(
        t, "Bad channel name: {t} is not formatted correctly.",
        "Bad channel name: that channel is not formatted correctly."),
    477: lambda t, r: _t(
        t, "{t} needs a registered, identified nick to join. Tap the Identify button to "
           "register/identify with NickServ, then try again.",
        "That channel needs a registered, identified nick to join. Tap the Identify button "
        "to register/identify with NickServ, then try again."),
    478: lambda t, r: _t(
        t, "The ban list for {t} is full; no more bans can be added.",
        "The ban list for that channel is full; no more bans can be added."),
    481: lambda t, r: "Permission denied: you lack the privileges for that" + _tail(r) + ".",
    482: lambda t, r: _t(
        t, "You are not a channel operator on {t}, so you cannot do that.",
        "You are not a channel operator, so you cannot do that."),
    483: lambda t, r: "You cannot KILL a server.",
    484: lambda t, r: "Your connection is restricted, so that action is not allowed" + _tail(r) + ".",
    485: lambda t, r: _t(
        t, "Only the original creator of {t} can do that.",
        "Only the channel's original creator can do that."),
    491: lambda t, r: "You are not allowed to become an operator from your host.",
}


def friendly_numeric(numeric: Optional[int], text: str) -> Optional[str]:
    """A clear one-line English explanation for a known IRC error numeric, or
    None when the numeric is not one we explain. No leading marker: the caller
    adds its own."""
    builder = _BUILDERS.get(numeric)
    if builder is None:
        return None
    target, reason = _split(text)
    return builder(target, reason)
