"""Inline menu structure and callback encoding.

Kept pure and separate from Kurigram: a menu is rows of (label, callback_data)
tuples, and callback_data is a compact `ns:action:arg` string (Telegram caps it
at 64 bytes). The Telegram layer turns these into InlineKeyboardButtons, and
routes a tapped callback back through `parse_cb`. Because it is pure, the whole
navigation is tested without a live bot.
"""

from __future__ import annotations

from typing import Optional

Button = tuple[str, str]        # (label, callback_data)
Menu = list[list[Button]]       # rows of buttons


_STATUS_BADGE = {"connected": "🟢", "connecting": "🟡", "disconnected": "🔴"}


def _server_badges(server: dict) -> str:
    """Status dot followed by the tor/anon marker, prepended to a server name."""
    status = _STATUS_BADGE.get(server.get("status", "disconnected"),
                               _STATUS_BADGE["disconnected"])
    tor = "🧅" if server.get("tor") else ("🔒" if server.get("anon") else "")
    return f"{status}{tor}"


def cb(ns: str, action: str, arg: str = "") -> str:
    data = f"{ns}:{action}:{arg}" if arg else f"{ns}:{action}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def parse_cb(data: str) -> tuple[str, str, str]:
    parts = data.split(":", 2)
    ns = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    return ns, action, arg


def main_menu(t) -> Menu:
    """t: a callable key -> translated label (already bound to the language).

    Only actions with a live handler are shown. Channels/discover/nick land
    here once wired against a real network (they need live /list output)."""
    return [
        [(t("menu.servers"), cb("nav", "servers")),
         (t("menu.add_server"), cb("srv", "add"))],
        [(t("menu.reconnect"), cb("srv", "reconnect_all"))],
        [(t("menu.settings"), cb("nav", "settings")),
         (t("menu.help"), cb("sys", "help"))],
    ]


# Help categories, in display order. Each maps to a help.b.<slug> button label
# and a help.cat.<slug> page. The manager validates a tapped slug against this
# set before rendering, so an unknown arg cannot select a missing key.
HELP_CATEGORIES = ("channels", "users", "modes", "messaging", "server", "info")


def help_menu(t) -> Menu:
    """The help hub keyboard: one button per command category, then Back.

    Category buttons carry cb("help", "cat", <slug>); the slug is a fixed short
    identifier (never a translated name), so callback_data stays well under the
    64-byte cap and is language-independent."""
    rows: Menu = []
    cats = list(HELP_CATEGORIES)
    for i in range(0, len(cats), 2):
        row = [(t(f"help.b.{slug}"), cb("help", "cat", slug))
               for slug in cats[i:i + 2]]
        rows.append(row)
    rows.append([(t("menu.back"), cb("nav", "main"))])
    return rows


def servers_menu(t, servers: list[dict]) -> Menu:
    rows: Menu = []
    for s in servers:
        name = s["name"]
        rows.append([(f"{_server_badges(s)}{name}", cb("srv", "view", name))])
    rows.append([(t("menu.add_server"), cb("srv", "add")),
                 (t("menu.back"), cb("nav", "main"))])
    return rows


def server_title(t, server: dict) -> str:
    """The server-view message text: the status dot and name on top, then a
    detail line the user can read at a glance, the state (connected / etc.),
    whether the link is encrypted, and the login method if any."""
    name = server["name"]
    safe = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    status = server.get("status", "disconnected")
    if status not in ("connected", "connecting", "disconnected"):
        status = "disconnected"
    detail = [t("status." + status)]
    detail.append("🔒 " + t("server.encrypted") if server.get("tls")
                  else "🔓 " + t("server.plaintext"))
    auth_label = {"sasl": "SASL", "nickserv": "NickServ"}.get(
        server.get("auth_method", "none"))
    if auth_label:
        detail.append(auth_label)
    return f"{_server_badges(server)} <b>{safe}</b>\n{' · '.join(detail)}"


def server_view_menu(t, server: dict) -> Menu:
    """The common per-server actions. The server's name and status go in the
    message text (not a button), and the rarely-touched configuration lives one
    tap away under Settings, so this stays short."""
    name = server["name"]
    away_label = t("menu.back_from_away") if server.get("away") else t("menu.away")
    return [
        [(t("menu.join"), cb("srv", "join", name)),
         (t("menu.channels"), cb("srv", "channels", name))],
        [(t("menu.discover"), cb("srv", "discover", name))],
        [(t("menu.change_nick"), cb("srv", "nick", name)),
         (away_label, cb("srv", "away", name))],
        [(t("menu.reconnect"), cb("srv", "reconnect", name)),
         (t("menu.disconnect"), cb("srv", "disconnect", name))],
        [(t("menu.settings_server"), cb("srv", "settings", name))],
        [(t("menu.remove"), cb("srv", "remove", name)),
         (t("menu.back"), cb("nav", "servers"))],
    ]


def server_settings_menu(t, server: dict) -> Menu:
    """The rarely-touched per-server configuration, reached from the server view.
    A checked toggle reads "this is on / this event is visible"."""
    name = server["name"]
    tor = "✓" if server.get("tor") else "✗"
    autojoin = "✓" if server.get("autojoin", 1) else "✗"
    muted = {x.strip() for x in server.get("noise_filter", "join,part,quit").split(",")
             if x.strip()}

    def shown(kind: str) -> str:
        return "✓" if kind not in muted else "✗"

    return [
        [(t("menu.identify"), cb("srv", "identify", name))],
        [(t("menu.register"), cb("srv", "register", name))],
        [(t("menu.perform"), cb("srv", "perform", name))],
        [(t("menu.ignores"), cb("srv", "ignores", name))],
        [(f"{t('menu.autojoin')}: {autojoin}", cb("srv", "autojoin", name))],
        [(f"{t('menu.noise_joins')}: {shown('join')}", cb("srv", "noisejoin", name))],
        [(f"{t('menu.noise_parts')}: {shown('part')}", cb("srv", "noisepart", name))],
        [(f"{t('menu.noise_quits')}: {shown('quit')}", cb("srv", "noisequit", name))],
        [(f"Tor: {tor}", cb("srv", "tor", name))],
        [(t("menu.motd"), cb("srv", "motd", name)),
         (t("menu.info"), cb("srv", "info", name))],
        [(t("menu.back"), cb("srv", "view", name))],
    ]


def channels_menu(t, server: str, channels: list[dict], gen: int) -> Menu:
    """One button per joined channel, opening its actions panel (names, topic,
    leave, ...).

    Channel names contain '#' and can be long, so callback_data references a
    channel by its index into the ordered list the manager stored, not by name.
    The list's generation id is packed with the index so a tap on a stale menu
    is rejected instead of resolving against a different list.
    """
    rows: Menu = []
    for i, ch in enumerate(channels):
        channel = ch["buffer"].split(".", 2)[-1]
        rows.append([(channel, cb("srv", "actions", f"{gen}.{i}"))])
    rows.append([(t("menu.back"), cb("srv", "view", server))])
    return rows


def channel_panel_menu(t, server: str, channel: str, gen: int, index: int) -> Menu:
    """Quick actions for one joined channel. Names/Topic/Who each fire an IRC
    command on the server buffer; their replies flow to the channel/server topic.
    The channel is referenced by the same generation.index as its Leave button,
    so no channel name is packed into callback_data."""
    ref = f"{gen}.{index}"
    return [
        [(t("menu.names"), cb("srv", "names", ref)),
         (t("menu.who"), cb("srv", "who", ref))],
        [(t("menu.topic"), cb("srv", "topic", ref))],
        [(f"{t('menu.leave')} {channel}", cb("srv", "leaveconfirm", ref))],
        [(t("menu.back"), cb("srv", "channels", server))],
    ]


def names_menu(t, server: str, users: list[dict], gen: int) -> Menu:
    """One button per channel member, labelled "<prefix><nick>", referenced by
    generation.index (never the nick), the same stale-guarded way as the other
    dynamic pickers. Back returns to the server's channels list, the level above
    the panel the /names was fired from."""
    rows: Menu = [[(f"{u['prefix']}{u['nick']}", cb("usr", "pick", f"{gen}.{i}"))]
                  for i, u in enumerate(users)]
    rows.append([(t("menu.back"), cb("srv", "channels", server))])
    return rows


def user_actions_menu(t, gen: int, index: int) -> Menu:
    """Power-user actions for the picked member. Each button carries the same
    generation.index reference, so the manager resolves the target nick and
    channel from the stored names list without packing them into callback_data."""
    ref = f"{gen}.{index}"
    return [
        [(t("menu.whois"), cb("usr", "whois", ref))],
        [(t("menu.op"), cb("usr", "op", ref)),
         (t("menu.deop"), cb("usr", "deop", ref))],
        [(t("menu.voice"), cb("usr", "voice", ref)),
         (t("menu.devoice"), cb("usr", "devoice", ref))],
        [(t("menu.kick"), cb("usr", "kick", ref)),
         (t("menu.ban"), cb("usr", "ban", ref))],
        [(t("menu.back"), cb("usr", "pickback", ref))],
    ]


# Longest channel topic shown on a discovery button before it is trimmed. The
# label is only cosmetic (the callback carries the index, not the text), so a
# long topic cannot overflow callback_data; this just keeps the button tidy.
_DISCOVER_TOPIC_MAX = 45


def _discovered_label(ch: dict, joined: bool = False) -> str:
    """A tidy one-line button: a check when we are already in the channel, then
    "#channel (users)" plus a trimmed topic when the /LIST reply carried one, so
    the channel's purpose (and whether you are in it) reads before joining."""
    label = f"{'✓ ' if joined else ''}{ch['channel']} ({ch['users']})"
    topic = " ".join((ch.get("topic") or "").split())   # collapse newlines/runs
    if topic:
        if len(topic) > _DISCOVER_TOPIC_MAX:
            topic = topic[:_DISCOVER_TOPIC_MAX].rstrip() + "..."
        label = f"{label} · {topic}"
    return label


def discovered_menu(t, server: str, channels: list[dict], gen: int,
                    joined: Optional[set] = None) -> Menu:
    """One tappable button per discovered channel (a check if already joined,
    name, user count, and a trimmed topic), referenced by index the same
    generation-tagged way as channels_menu (never by name). Tapping opens the
    channel's detail view (not an immediate join); Back returns to the server
    view. `joined` is the set of lower-cased channel names we are already in."""
    joined = joined or set()
    rows: Menu = [[(_discovered_label(ch, ch["channel"].lower() in joined),
                    cb("srv", "discinfo", f"{gen}.{i}"))]
                  for i, ch in enumerate(channels)]
    rows.append([(t("menu.back"), cb("srv", "view", server))])
    return rows


def discovered_channel_title(t, ch: dict, joined: bool = False) -> str:
    """The detail message for one discovered channel: name, user count, whether
    you are already in it, and the full topic (untruncated here, since it is
    message text not a button)."""
    name = ch["channel"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    topic = " ".join((ch.get("topic") or "").split())
    topic = topic.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = topic if topic else t("discover.no_topic")
    head = f"<b>{name}</b>\n👥 {ch['users']}"
    if joined:
        head = f"{head}\n{t('discover.already_joined')}"
    return f"{head}\n\n{body}"


def discovered_channel_menu(t, gen: int, index: int) -> Menu:
    """Detail-view actions for one discovered channel: join it, go back to the
    list to browse others, or jump to the main menu. The channel is referenced
    by the same generation.index as its list button, so no channel name is
    packed into callback_data."""
    ref = f"{gen}.{index}"
    return [
        [(t("discover.join"), cb("srv", "joinidx", ref))],
        [(t("menu.back"), cb("srv", "discback")),
         (t("menu.main"), cb("nav", "main"))],
    ]


def channel_left_menu(t, topic_id: int) -> Menu:
    """Offered in a channel's topic when we leave it (part, kick, or the buffer
    closing): close the topic, keep it open, or delete it outright. The topic id
    is the callback arg, so no channel name is packed into callback_data."""
    tid = str(topic_id)
    return [
        [(t("channel.close"), cb("chan", "close", tid))],
        [(t("channel.keep"), cb("chan", "keep", tid))],
        [(t("channel.delete"), cb("chan", "delete", tid))],
    ]


def ignores_menu(t, server: str, nicks: list[str], gen: int) -> Menu:
    """The ignore list for a server: one Unignore button per ignored nick
    (referenced by generation.index, never by name, like channels_menu), an Add
    action, and a back button."""
    rows: Menu = []
    for i, nick in enumerate(nicks):
        rows.append([(f"{t('menu.unignore')} {nick}",
                      cb("srv", "unignore", f"{gen}.{i}"))])
    rows.append([(t("menu.ignore_add"), cb("srv", "ignoreadd", server))])
    rows.append([(t("menu.back"), cb("srv", "view", server))])
    return rows


def settings_menu(t, settings: dict) -> Menu:
    lang = settings.get("language", "en")
    tor_default = settings.get("tor_default", False)
    # Only settings wired to real behavior are shown. topic_mode (private vs
    # group) returns when the private-chat-topics backend lands.
    return [
        [(f"{t('settings.language')}: {lang}", cb("set", "language"))],
        [(f"{t('settings.tor_default')}: {'✓' if tor_default else '✗'}",
          cb("set", "tor_default"))],
        [(t("menu.senders"), cb("nav", "senders"))],
        [(t("menu.back"), cb("nav", "main"))],
    ]


def senders_menu(t, senders: list[dict], gen: int) -> Menu:
    """The extra sender bots: one Remove button per worker (referenced by
    generation.index, never by id, like the other dynamic pickers), an Add
    action, and Back. The primary bot is not listed: it cannot be removed."""
    rows: Menu = []
    for i, s in enumerate(senders):
        rows.append([(f"{t('senders.remove')} {s['bot_id']}",
                      cb("set", "senderdel", f"{gen}.{i}"))])
    rows.append([(t("senders.add"), cb("set", "senderadd"))])
    rows.append([(t("menu.back"), cb("nav", "settings"))])
    return rows


def language_menu(t, languages: list[str], names: dict) -> Menu:
    rows = [[(names.get(code, code), cb("set", "lang", code))] for code in languages]
    rows.append([(t("menu.back"), cb("nav", "settings"))])
    return rows


def confirm_menu(t, yes_cb: str, no_cb: str) -> Menu:
    return [[(t("yes"), yes_cb), (t("no"), no_cb)]]
