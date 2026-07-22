"""Orchestration for the console: menus, the add-server flow, and deciding
what a piece of text means.

This ties together the pieces that are each tested on their own (menu, commands,
router). The decision logic here - is an add-server flow in progress, is this a
raw IRC command, or is it a conversation message - is what these tests pin.
Rendering keyboards and sending them is the Telegram layer's job; here we only
decide and act through injected collaborators.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from . import menu
from .commands import AddServerFlow, build_addserver_commands, is_valid_nick
from .anon import build_anon_commands

CORE_BUFFER = "core.weechat"

# How long the "Fetching channel list..." message waits for a /list reply before
# giving up. Private trackers often restrict LIST, or the reply never completes,
# so without this the message would hang forever.
DISCOVER_TIMEOUT = 12.0


def _split_index(arg: str) -> tuple[int, int]:
    """Decode a "gen.index" picker argument. Returns (-1, -1) when malformed,
    a generation that never matches a stored list (they start at 1)."""
    gen, _, index = arg.partition(".")
    if gen.isdigit() and index.isdigit():
        return int(gen), int(index)
    return -1, -1


class Manager:
    def __init__(self, db, backend, gateway, translator, router, *, admin_id: int,
                 on_group_set=None):
        self._db = db
        self._backend = backend
        self._gw = gateway
        self._tr_obj = translator     # Translator: has .t, .languages(), .language_name()
        self._t = translator.t
        self._router = router
        self._admin_id = admin_id
        # Called once when /usegroup saves a group during onboarding, so the
        # entry point can bring up the full bridge without a restart.
        self._on_group_set = on_group_set
        self._addflow: Optional[AddServerFlow] = None
        self._flow_msg_id: Optional[int] = None  # the one flow message we edit in place
        self._pending: Optional[tuple] = None   # (action, server) awaiting one text answer
        # The message a text prompt is shown in, and the callback to return to
        # when it is answered, so the prompt is edited in place (Cancel -> Back +
        # result) instead of leaving a stale prompt with a dangling Cancel.
        self._prompt_msg_id: int = 0
        self._prompt_back: str = ""
        # After a server is added we keep editing its flow message: first to
        # "connecting", then to "connected"/"failed" once the router reports back.
        self._connect_msg: dict[str, int] = {}
        self._connect_nick: dict[str, str] = {}
        # The channel list currently shown, so a "leavech" tap can resolve its
        # index arg back to a channel without packing the name into callback_data.
        self._chan_list: Optional[dict] = None
        # The channels a /list discovery returned, so a "joinidx" tap resolves
        # its index arg back to a channel the same index-only way.
        self._discovered: Optional[dict] = None
        # Per-server discovery state so a /list that never replies does not leave
        # the "fetching" message hanging: the message id we can edit to a
        # "no channels" notice, and the timeout task that does so.
        self._discover_msg: dict[str, int] = {}
        self._discover_tasks: dict[str, object] = {}
        self._discover_timeout = DISCOVER_TIMEOUT
        # The ignore list currently shown, so an "unignore" tap resolves its
        # index arg back to a nick without packing the nick into callback_data.
        self._ignore_list: Optional[dict] = None
        # The channel member list currently shown, so a "usr" tap resolves its
        # index arg back to a nick (and the channel it belongs to) the same
        # index-only, generation-guarded way.
        self._names_list: Optional[dict] = None
        # Every stored list gets a fresh generation id, encoded into its
        # buttons. A tap on an older, still-visible picker carries a stale id
        # and is rejected, so it cannot act on whatever list is stored now.
        self._list_gen = 0
        # Per-server away flag. IRC away state is session-only (it clears on
        # reconnect), so it lives here in memory rather than in the database.
        self._away: dict[str, bool] = {}
        # The sender-bot list currently shown, so a "senderdel" tap resolves its
        # index arg back to a bot_id without packing it into callback_data, the
        # same generation-guarded way as the other pickers.
        self._sender_list: Optional[dict] = None

    def _lang(self) -> str:
        return self._db.get("language", "en")

    def _tr(self, key: str, **params) -> str:
        return self._t(key, self._lang(), **params)

    async def _finish_prompt(self, text: str) -> None:
        """A text prompt was answered: edit its message into the result plus a
        single Back button, so its Cancel is gone and there is a way back. Falls
        back to a plain console message when there is no prompt message to edit."""
        back = self._prompt_back or menu.cb("nav", "main")
        if self._prompt_msg_id:
            await self._gw.edit_menu(self._prompt_msg_id, text,
                                     [[(self._tr("menu.back"), back)]])
        else:
            await self._gw.send_console(text)
        self._prompt_msg_id = 0
        self._prompt_back = ""

    async def on_console_text(self, from_id: int, message_id: int, text: str) -> None:
        if from_id != self._admin_id:
            return  # management is admin-only (default-deny)

        if self._pending is not None:
            action, server = self._pending
            self._pending = None
            if action == "join":
                channel = text.strip()
                if channel and not channel.startswith(("#", "&")):
                    channel = "#" + channel
                # /join must run on the server buffer, not core
                await self._backend.send_command(f"irc.server.{server}", f"/join {channel}")
                await self._finish_prompt(self._tr("channels.joined_ok", channel=channel))
            elif action == "identify":
                # Identify to NickServ on the server buffer, then scrub the
                # password message so it does not linger in the chat history.
                await self._backend.send_command(
                    f"irc.server.{server}", f"/msg NickServ IDENTIFY {text.strip()}")
                await self._gw.delete_message(message_id)
                await self._finish_prompt(self._tr("nickserv.identified"))
            elif action == "register":
                # Register the current nick with NickServ. The first token is the
                # password, an optional second token is a contact email. Scrub the
                # message afterwards since it carries the password.
                parts = text.split()
                password = parts[0] if parts else ""
                email = parts[1] if len(parts) > 1 else ""
                register = f"/msg NickServ REGISTER {password}"
                if email:
                    register += f" {email}"
                await self._backend.send_command(f"irc.server.{server}", register)
                await self._gw.delete_message(message_id)
                await self._finish_prompt(self._tr("nickserv.registered"))
            elif action == "perform":
                # Store the on-connect commands, one per line; this replaces the
                # whole set (see Database.set_perform).
                self._db.set_perform(server, text.strip())
                await self._finish_prompt(self._tr("perform.saved"))
            elif action == "ignore":
                nick = text.strip()
                if nick:
                    self._db.add_ignore(server, nick)
                    await self._finish_prompt(
                        self._tr("ignores.added", nick=nick))
            elif action == "nick":
                nick = text.strip()
                if not is_valid_nick(nick):
                    # keep the flow open so the next message retries the change
                    self._pending = ("nick", server)
                    await self._gw.send_console(self._tr("nick.invalid"))
                else:
                    await self._backend.send_command(
                        f"irc.server.{server}", f"/nick {nick}")
                    await self._finish_prompt(
                        self._tr("nick.changed", nick=nick))
            elif action == "addsender":
                # A Telegram bot token is "<bot_id>:<secret>"; the id prefix is a
                # stable, non-secret key for the row. Reject anything without it,
                # then scrub the message since the token is a credential.
                token = text.strip()
                bot_id = token.split(":", 1)[0]
                # Scrub first either way: even a mistyped token is credential-like
                # and must not linger in the chat history.
                await self._gw.delete_message(message_id)
                if ":" not in token or not bot_id.isdigit():
                    self._pending = ("addsender", server)
                    await self._gw.send_console(self._tr("senders.invalid"))
                else:
                    self._db.add_sender(bot_id, token)
                    # Start the worker now so it carries sends immediately; no
                    # restart needed (start_senders skips one already running).
                    await self._gw.start_senders(
                        [{"bot_id": bot_id, "token": token}])
                    await self._finish_prompt(
                        self._tr("senders.added", bot_id=bot_id))
            return

        if self._addflow is not None:
            if self._addflow.is_choice():
                # this step needs a button; refresh the prompt, ignore the text
                await self._edit_flow()
                return
            secret = self._addflow.current()[0] == "password"
            await self._feed_flow(text)
            if secret:  # a typed password must not linger in the chat history
                await self._gw.delete_message(message_id)
            return

        if text.startswith("/"):
            # Raw IRC command passthrough. IRC commands (/join, /mode, ...) must
            # run on a server buffer, not core; route to the one server if there
            # is exactly one, otherwise fall back to core.
            servers = self._db.list_servers()
            buf = f"irc.server.{servers[0]['name']}" if len(servers) == 1 else CORE_BUFFER
            await self._backend.send_command(buf, text)

    def _flow_menu(self):
        rows = []
        if self._addflow.is_choice():
            def label(opt):
                return {"yes": self._tr("yes"), "no": self._tr("no"),
                        "off": self._tr("privacy.off"), "tor": self._tr("privacy.tor"),
                        "anon": self._tr("privacy.anon")}.get(opt, opt)
            rows.append([(label(o), menu.cb("flow", "set", o))
                         for o in self._addflow.options()])
        nav = []
        if self._addflow.step > 0:
            nav.append((self._tr("menu.back"), menu.cb("flow", "back")))
        nav.append((self._tr("menu.cancel"), menu.cb("flow", "cancel")))
        rows.append(nav)
        return rows

    def _flow_view(self):
        return (self._tr(self._addflow.prompt_key()), self._flow_menu())

    async def _edit_flow(self) -> None:
        if self._flow_msg_id is not None:
            await self._gw.edit_menu(self._flow_msg_id, *self._flow_view())

    async def _feed_flow(self, value: str) -> None:
        try:
            self._addflow.feed(value)
        except ValueError as exc:
            await self._gw.edit_menu(
                self._flow_msg_id,
                f"{exc}\n{self._tr(self._addflow.prompt_key())}", self._flow_menu())
            return
        if not self._addflow.is_complete():
            await self._edit_flow()
            return
        data = self._addflow.data
        self._addflow = None
        privacy = data.get("privacy", "off")
        is_anon = privacy == "anon"
        for cmd in build_addserver_commands(data):
            await self._backend.send_command(CORE_BUFFER, cmd)
        if is_anon:
            for cmd in build_anon_commands(data["name"]):
                await self._backend.send_command(CORE_BUFFER, cmd)
        self._db.upsert_server(
            data["name"], anon=is_anon, tor=privacy in ("tor", "anon"),
            tls=bool(data.get("tls")), auth_method=data.get("auth", "none"),
        )
        self._db.set_server_status(data["name"], "connecting")
        self._arm_connect(data["name"])
        self._connect_msg[data["name"]] = self._flow_msg_id
        self._connect_nick[data["name"]] = data["nick"]
        await self._gw.edit_menu(
            self._flow_msg_id,
            self._tr("addserver.connecting", name=data["name"], nick=data["nick"]),
            None)
        self._flow_msg_id = None

    def _arm_connect(self, name: str) -> None:
        if self._router is not None:
            self._router.arm_connect_timeout(name)

    async def on_server_status(self, name: str, status: str) -> None:
        msg_id = self._connect_msg.pop(name, None)
        if msg_id is None:
            return
        nick = self._connect_nick.pop(name, "")
        if status == "connected":
            text = self._tr("addserver.connected", nick=nick)
        else:
            text = self._tr("addserver.failed", name=name)
        await self._gw.edit_menu(msg_id, text, None)

    async def on_onboard(self, from_id: int, kind: str, chat_id: int,
                         chat_type: str):
        """Returns a view (text, menu-or-None) to show the admin, or None."""
        if from_id != self._admin_id:
            return None
        if kind in ("start", "menu"):
            # /start and /menu both open the console. During onboarding there is
            # no console yet (no backend), so show the start view; once the bridge
            # is fully up, show the main console menu (so /start works in the group
            # too, where the user naturally reaches for it).
            if self._backend is None:
                return self._start_view()
            return self._nav_view("main")
        if kind == "usegroup":
            # A forum-enabled supergroup reports its type as "forum"; that is
            # exactly what we need (it has Topics). Plain groups/private do not.
            if chat_type not in ("forum", "supergroup"):
                return (self._tr("onboard.not_a_group"), None)
            self._db.set("group_chat_id", chat_id)
            if self._on_group_set:
                self._on_group_set(chat_id)
            return (self._tr("onboard.group_saved"), None)
        return None

    async def on_callback(self, from_id: int, data: str, message_id: int = 0):
        """Handle a button tap; return the view to render in place, or None."""
        if from_id != self._admin_id:
            return None
        # The message this tap is on is the one a prompt gets rendered into, so
        # remember it: when the prompt is answered we edit it in place (drop its
        # Cancel, show the result and a Back button) instead of leaving it behind.
        self._prompt_msg_id = message_id
        ns, action, arg = menu.parse_cb(data)
        # A pending text prompt (join/identify/nick/perform/ignore) offers only a
        # Cancel button; tapping anything else means the user walked away from it,
        # so drop it now or the next message they type would be misread as its
        # answer. flow callbacks (cancel/back/set) are the prompt's own controls.
        if self._pending is not None and ns != "flow":
            self._pending = None
        if ns == "dm" and action == "lang" and arg:
            # Language is settable from the private chat before any group exists.
            self._db.set("language", arg)
            return self._start_view()
        if ns == "flow":
            if action == "cancel":
                if self._flow_msg_id is not None:
                    await self._gw.edit_menu(self._flow_msg_id, self._tr("flow.cancelled"), None)
                self._addflow = self._pending = self._flow_msg_id = None
                return None
            if action == "back" and self._addflow is not None:
                self._addflow.back()
                await self._edit_flow()
                return None
            if action == "set" and self._addflow is not None and self._addflow.is_choice():
                await self._feed_flow(arg)
                return None
            return None
        if ns == "sys" and action == "help":
            return (self._tr("help.intro"), menu.help_menu(self._bound()))
        if ns == "help" and action == "cat":
            if arg not in menu.HELP_CATEGORIES:
                return None
            back = [[(self._tr("menu.back"), menu.cb("sys", "help"))]]
            return (self._tr(f"help.cat.{arg}"), back)
        if ns == "nav":
            return self._nav_view(action)
        if ns == "srv":
            return await self._server_action(action, arg)
        if ns == "usr":
            return await self._user_action(action, arg)
        if ns == "set":
            return self._settings_action(action, arg)
        return None

    async def show_main(self) -> None:
        """Send the main console menu as a new message (startup)."""
        title, m = self._nav_view("main")
        await self._gw.send_menu(title, m)

    def _start_view(self):
        text = self._tr("onboard.ready") if self._db.get_int("group_chat_id") \
            else self._tr("onboard.needs_group")
        langs = self._tr_obj.languages()
        row = [(self._tr_obj.language_name(c), menu.cb("dm", "lang", c)) for c in langs]
        return (text, [row])

    def _nav_view(self, action: str):
        if action == "servers":
            return (self._tr("menu.servers"),
                    menu.servers_menu(self._bound(), self._db.list_servers()))
        if action == "settings":
            return (self._tr("settings.title"),
                    menu.settings_menu(self._bound(), self._settings_dict()))
        if action == "senders":
            return self._senders_view()
        return (self._tr("menu.title"), menu.main_menu(self._bound()))

    def _senders_view(self):
        # Only the extra (non-primary) bots are listed and removable; the main
        # bot is not a row here. A fresh generation guards a stale Remove tap.
        senders = [s for s in self._db.list_senders() if not s.get("is_primary")]
        self._list_gen += 1
        gen = self._list_gen
        self._sender_list = {"gen": gen, "senders": senders}
        title = self._tr("senders.title") if senders else self._tr("senders.none")
        return (title, menu.senders_menu(self._bound(), senders, gen))

    async def _server_action(self, action: str, name: str):
        if action == "add":
            self._addflow = AddServerFlow()
            # one flow message we edit in place as steps are answered
            self._flow_msg_id = await self._gw.send_menu(*self._flow_view())
            return None
        if action == "join":
            self._pending = ("join", name)
            self._prompt_back = menu.cb("srv", "view", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("channels.join_prompt"), cancel)
        if action == "identify":
            self._pending = ("identify", name)
            self._prompt_back = menu.cb("srv", "settings", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("nickserv.prompt"), cancel)
        if action == "register":
            self._pending = ("register", name)
            self._prompt_back = menu.cb("srv", "settings", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("nickserv.register_prompt"), cancel)
        if action == "perform":
            self._pending = ("perform", name)
            self._prompt_back = menu.cb("srv", "settings", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            current = self._db.get_perform(name)
            prompt = self._tr("perform.prompt")
            if current:
                # show what is set now (monospace) so the user is not editing
                # blind; it may hold a password, but this is the admin's own console.
                safe = (current.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;"))
                prompt = (f"{self._tr('perform.current', cmd=f'<code>{safe}</code>')}"
                          f"\n\n{prompt}")
            return (prompt, cancel)
        if action == "ignoreadd":
            self._pending = ("ignore", name)
            self._prompt_back = menu.cb("srv", "ignores", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("ignores.add_prompt"), cancel)
        if action == "nick":
            self._pending = ("nick", name)
            self._prompt_back = menu.cb("srv", "view", name)
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("nick.prompt"), cancel)
        if action == "reconnect":
            self._db.set_server_status(name, "connecting")
            self._arm_connect(name)
            await self._backend.send_command(CORE_BUFFER, f"/connect {name}")
        elif action == "reconnect_all":
            # Leave an already-connected server alone: it is up, so it should not
            # flip to "connecting" and sit under a failure timeout that could
            # mislabel it. Only the servers that actually need to come up are
            # marked and armed.
            for s in self._db.list_servers():
                if s.get("status") != "connected":
                    self._db.set_server_status(s["name"], "connecting")
                    self._arm_connect(s["name"])
            await self._backend.send_command(CORE_BUFFER, "/reconnect -all")
        elif action == "disconnect":
            self._db.set_server_status(name, "disconnected")
            await self._backend.send_command(CORE_BUFFER, f"/disconnect {name}")
        elif action == "settings":
            return self._server_settings_view(name)
        elif action == "remove":
            # confirm first: Remove sits next to Back, and it is destructive.
            return (self._tr("confirm.remove", name=name),
                    menu.confirm_menu(self._bound(),
                                      menu.cb("srv", "remove2", name),
                                      menu.cb("srv", "view", name)))
        elif action == "remove2":
            await self._backend.send_command(CORE_BUFFER, f"/disconnect {name}")
            await self._backend.send_command(CORE_BUFFER, f"/server del {name}")
            self._db.remove_server(name)
        elif action == "away":
            return await self._toggle_away(name)
        elif action == "motd":
            await self._backend.send_command(f"irc.server.{name}", "/motd")
            return None   # the reply flows to the server topic; view stays put
        elif action == "info":
            await self._backend.send_command(f"irc.server.{name}", "/version")
            return None
        elif action == "tor":
            await self._toggle_tor(name)
            return self._server_settings_view(name)
        elif action == "autojoin":
            srv = self._db.get_server(name) or {"name": name}
            self._db.set_autojoin(name, not srv.get("autojoin", 1))
            return self._server_settings_view(name)
        elif action in ("noisejoin", "noisepart", "noisequit"):
            return self._toggle_noise(name, action[len("noise"):])
        elif action == "view":
            return self._server_view(name)
        elif action == "channels":
            return self._channels_view(name)
        elif action == "leaveconfirm":
            return self._leave_confirm(name)
        elif action == "leavech":
            return await self._leave_channel(name)
        elif action == "actions":
            return self._channel_panel(name)
        elif action in ("names", "topic", "who"):
            return await self._channel_command(action, name)
        elif action == "discover":
            return await self._discover_channels(name)
        elif action == "discinfo":
            return self._discovered_info(name)
        elif action == "discback":
            return self._discovered_list_view()
        elif action == "joinidx":
            return await self._join_discovered(name)
        elif action == "ignores":
            return self._ignores_view(name)
        elif action == "unignore":
            return self._unignore(name)
        return self._nav_view("servers")

    def _server_srv(self, name: str) -> dict:
        # get_server returns a fresh dict each call, so injecting the in-memory
        # away flag here never touches stored state.
        srv = self._db.get_server(name) or {"name": name}
        srv["away"] = self._away.get(name, False)
        return srv

    def _server_view(self, name: str):
        """The server view: status and name in the message text, common actions
        below, the rest behind Settings."""
        srv = self._server_srv(name)
        return (menu.server_title(self._bound(), srv),
                menu.server_view_menu(self._bound(), srv))

    def _server_settings_view(self, name: str):
        srv = self._server_srv(name)
        # Lead with the same status header as the server view so this screen
        # always says which server it is configuring (a bare "Server settings"
        # left that ambiguous when reopened later).
        title = (f"{menu.server_title(self._bound(), srv)}\n"
                 f"⚙️ {self._tr('menu.settings_server')}")
        return (title, menu.server_settings_menu(self._bound(), srv))

    async def _toggle_away(self, name: str):
        now_away = not self._away.get(name, False)
        self._away[name] = now_away
        # /away <msg> sets it; a bare /away clears it. Runs on the server buffer.
        command = f"/away {self._tr('away.default_message')}" if now_away else "/away"
        await self._backend.send_command(f"irc.server.{name}", command)
        return self._server_view(name)

    def _channels_view(self, server: str):
        channels = self._db.list_channels(server)
        self._list_gen += 1
        gen = self._list_gen
        self._chan_list = {"gen": gen, "server": server, "channels": channels}
        return (self._tr("channels.joined"),
                menu.channels_menu(self._bound(), server, channels, gen))

    async def _leave_channel(self, arg: str):
        chan = self._chan_list
        gen, index = _split_index(arg)
        if chan is None or gen != chan["gen"]:
            return self._nav_view("servers")   # stale picker: ignore the tap
        server, channels = chan["server"], chan["channels"]
        if 0 <= index < len(channels):
            buffer = channels[index]["buffer"]
            channel = buffer.split(".", 2)[-1]
            # /part must run on the server buffer, matching the /join path.
            await self._backend.send_command(
                f"irc.server.{server}", f"/part {channel}")
            # Mark it parted now: the buffer_closed event that also does this
            # arrives asynchronously, so the immediate re-render below would
            # still list the channel without this.
            self._db.set_channel_open(buffer, False)
        # Re-render from the database so the channel we just left is gone.
        return self._channels_view(server)

    def _resolve_channel(self, arg: str):
        """Resolve a "gen.index" reference against the stored channel list into
        (server, channel, gen, index), or None for a stale/out-of-range tap."""
        chan = self._chan_list
        gen, index = _split_index(arg)
        if chan is None or gen != chan["gen"]:
            return None
        channels = chan["channels"]
        if 0 <= index < len(channels):
            channel = channels[index]["buffer"].split(".", 2)[-1]
            return chan["server"], channel, gen, index
        return None

    def _channel_panel(self, arg: str):
        resolved = self._resolve_channel(arg)
        if resolved is None:
            return self._nav_view("servers")   # stale/out-of-range: ignore the tap
        server, channel, gen, index = resolved
        return (channel,
                menu.channel_panel_menu(self._bound(), server, channel, gen, index))

    def _leave_confirm(self, arg: str):
        """Confirm before parting: Leave is a destructive action, so it asks
        first (Yes parts, No returns to the channel's panel). Leaving lives only
        inside the panel now, so a channel is never parted by a single stray tap."""
        resolved = self._resolve_channel(arg)
        if resolved is None:
            return self._nav_view("servers")   # stale/out-of-range: ignore the tap
        _server, channel, gen, index = resolved
        ref = f"{gen}.{index}"
        return (self._tr("confirm.leave", channel=channel),
                menu.confirm_menu(self._bound(),
                                  menu.cb("srv", "leavech", ref),
                                  menu.cb("srv", "actions", ref)))

    async def _channel_command(self, action: str, arg: str):
        """A panel button (Names/Topic/Who): fire the matching IRC command on the
        server buffer. Its reply flows to the channel/server topic as usual; the
        panel message stays put (None = no re-render)."""
        resolved = self._resolve_channel(arg)
        if resolved is None:
            return None
        server, channel, _, index = resolved
        if action == "names" and self._router is not None:
            # Arm the router to collect this names burst and post a user picker.
            self._router.mark_names(server)
        elif action in ("topic", "who") and self._router is not None:
            # These replies (33x/35x) have no friendly mapping and never pass
            # through handle_telegram, so point the router at the channel's own
            # topic; otherwise they fall to the server status topic.
            topic_id = self._chan_list["channels"][index]["topic_id"]
            self._router.expect_reply_in(server, topic_id)
        command = {"names": f"/names {channel}",
                   "topic": f"/topic {channel}",
                   "who": f"/who {channel}"}[action]
        await self._backend.send_command(f"irc.server.{server}", command)
        return None

    async def on_names(self, server: str, channel: str, users: list[dict]) -> None:
        """Router callback: a /names finished. Store the membership and post a
        picker, one button per user, referenced by index (never the nick)."""
        self._list_gen += 1
        gen = self._list_gen
        self._names_list = {"gen": gen, "server": server,
                            "channel": channel, "users": users}
        if not users:
            await self._gw.send_console(self._tr("names.none", channel=channel))
            return
        await self._gw.send_menu(self._tr("names.title", channel=channel),
                                 menu.names_menu(self._bound(), server, users, gen))

    async def _user_action(self, action: str, arg: str):
        nl = self._names_list
        gen, index = _split_index(arg)
        if nl is None or gen != nl["gen"]:
            return None   # stale picker: ignore the tap
        users = nl["users"]
        if not (0 <= index < len(users)):
            return None
        server, channel = nl["server"], nl["channel"]
        nick = users[index]["nick"]
        if action == "pick":
            label = f"{users[index]['prefix']}{nick}"
            return (label, menu.user_actions_menu(self._bound(), gen, index))
        if action == "pickback":
            return (self._tr("names.title", channel=channel),
                    menu.names_menu(self._bound(), server, users, gen))
        command = {
            "whois": f"/whois {nick}",
            "op": f"/mode {channel} +o {nick}",
            "deop": f"/mode {channel} -o {nick}",
            "voice": f"/mode {channel} +v {nick}",
            "devoice": f"/mode {channel} -v {nick}",
            "kick": f"/kick {channel} {nick}",
            "ban": f"/mode {channel} +b {nick}",
        }.get(action)
        if command is not None:
            await self._backend.send_command(f"irc.server.{server}", command)
        return None

    async def _discover_channels(self, server: str):
        # Post the fetching notice first and hold its id, then flag the router and
        # ask for the list. Doing the send before mark_discover matters: a fast
        # /list reply is consumed on a separate task, so on_channel_list could run
        # during an await here; by the time collection is enabled (and any reply
        # can arrive) the message id and the timeout are already in place, so
        # completion always finds the message to edit and can cancel the timer.
        self._discover_msg[server] = await self._gw.send_menu(
            self._tr("channels.fetching"), None)
        if self._router is not None:
            self._router.mark_discover(server)
        self._arm_discover_timeout(server)
        # /list must run on the server buffer, matching the /join path.
        await self._backend.send_command(f"irc.server.{server}", "/list")
        return None

    def _arm_discover_timeout(self, server: str) -> None:
        self._cancel_discover_timeout(server)   # never leave two timers per server
        self._discover_tasks[server] = asyncio.create_task(
            self._discover_timeout_later(server))

    def _cancel_discover_timeout(self, server: str) -> None:
        task = self._discover_tasks.pop(server, None)
        if task is not None:
            task.cancel()

    async def _discover_timeout_later(self, server: str) -> None:
        try:
            await asyncio.sleep(self._discover_timeout)
        except asyncio.CancelledError:
            return   # the list arrived (or teardown): nothing to report
        self._discover_tasks.pop(server, None)
        msg_id = self._discover_msg.pop(server, None)
        if msg_id is None:
            return   # already resolved by on_channel_list
        # Stop collecting so a late 322/323 line does not fire the picker after
        # we have already told the admin the list is empty.
        if self._router is not None:
            self._router.clear_discover(server)
        await self._gw.edit_menu(msg_id, self._tr("channels.none"), None)

    def close(self) -> None:
        """Cancel outstanding timers so they cannot fire after teardown."""
        for server in list(self._discover_tasks):
            self._cancel_discover_timeout(server)

    async def on_channel_list(self, server: str, channels: list[dict]) -> None:
        """Router callback: a /list finished. Store the result and post a picker,
        one button per channel, referenced by index (never the name)."""
        # The list arrived: stop the timeout and reuse the fetching message so the
        # placeholder is never left dangling - edit it into the picker, or into
        # the "no channels" notice when empty. Only a /list not started by the
        # Discover button (no held id) falls back to a fresh message.
        self._cancel_discover_timeout(server)
        msg_id = self._discover_msg.pop(server, None)
        self._list_gen += 1
        gen = self._list_gen
        self._discovered = {"gen": gen, "server": server, "channels": channels}
        if not channels:
            if msg_id is not None:
                await self._gw.edit_menu(msg_id, self._tr("discover.none"), None)
            else:
                await self._gw.send_console(self._tr("discover.none"))
            return
        picker = menu.discovered_menu(self._bound(), server, channels, gen,
                                      self._joined_channels(server))
        if msg_id is not None:
            await self._gw.edit_menu(msg_id, self._tr("menu.discover"), picker)
        else:
            await self._gw.send_menu(self._tr("menu.discover"), picker)

    def _discovered_info(self, arg: str):
        """Detail view for a tapped discovered channel: name, users, full topic,
        with Join / Back buttons (so a tap browses, it does not join by itself)."""
        disc = self._discovered
        gen, index = _split_index(arg)
        if disc is None or gen != disc["gen"]:
            return self._nav_view("servers")   # stale picker: ignore the tap
        channels = disc["channels"]
        if not (0 <= index < len(channels)):
            return self._nav_view("servers")
        ch = channels[index]
        joined = ch["channel"].lower() in self._joined_channels(disc["server"])
        return (menu.discovered_channel_title(self._bound(), ch, joined),
                menu.discovered_channel_menu(self._bound(), gen, index))

    def _discovered_list_view(self):
        """Back from a channel detail to the discovered-channels list, re-rendered
        from the stored result (same generation, so the join refs still match)."""
        disc = self._discovered
        if disc is None:
            return self._nav_view("servers")
        return (self._tr("menu.discover"),
                menu.discovered_menu(self._bound(), disc["server"],
                                     disc["channels"], disc["gen"],
                                     self._joined_channels(disc["server"])))

    def _joined_channels(self, server: str) -> set:
        """Lower-cased names of the channels we are currently in on this server,
        so the discovery views can mark which ones are already joined."""
        return {row["buffer"].split(".", 2)[-1].lower()
                for row in self._db.list_channels(server)}

    async def _join_discovered(self, arg: str):
        disc = self._discovered
        gen, index = _split_index(arg)
        if disc is None or gen != disc["gen"]:
            return None   # stale picker: ignore the tap
        server, channels = disc["server"], disc["channels"]
        if 0 <= index < len(channels):
            channel = channels[index]["channel"]
            await self._backend.send_command(
                f"irc.server.{server}", f"/join {channel}")
            await self._gw.send_console(self._tr("channels.joined_ok", channel=channel))
        return None

    def _ignores_view(self, server: str):
        nicks = self._db.list_ignores(server)
        self._list_gen += 1
        gen = self._list_gen
        self._ignore_list = {"gen": gen, "server": server, "nicks": nicks}
        return (self._tr("ignores.title"),
                menu.ignores_menu(self._bound(), server, nicks, gen))

    def _unignore(self, arg: str):
        ig = self._ignore_list
        gen, index = _split_index(arg)
        if ig is None or gen != ig["gen"]:
            return self._nav_view("servers")   # stale picker: ignore the tap
        server, nicks = ig["server"], ig["nicks"]
        if 0 <= index < len(nicks):
            self._db.remove_ignore(server, nicks[index])
        # Re-render from the database so the row we just removed is gone.
        return self._ignores_view(server)

    def _toggle_noise(self, name: str, kind: str):
        # noise_filter is the set of muted event kinds. Flip this kind's
        # membership, then write the whole row back (preserving the other
        # server fields) so the router picks it up on the next event.
        srv = self._db.get_server(name) or {"name": name}
        tokens = [x.strip() for x in
                  (srv.get("noise_filter") or "").split(",") if x.strip()]
        if kind in tokens:
            tokens.remove(kind)
        else:
            tokens.append(kind)
        self._db.upsert_server(
            name, noise_filter=",".join(tokens),
            anon=bool(srv.get("anon")), tor=bool(srv.get("tor")),
            tls=bool(srv.get("tls")),
            auth_method=srv.get("auth_method", "none"), caps=srv.get("caps", ""))
        return self._server_settings_view(name)

    async def _toggle_tor(self, name: str) -> None:
        srv = self._db.get_server(name) or {"name": name}
        new_tor = not srv.get("tor")
        if new_tor:
            await self._backend.send_command(CORE_BUFFER, "/proxy add tor socks5 127.0.0.1 9050")
            await self._backend.send_command(CORE_BUFFER, f"/set irc.server.{name}.proxy tor")
        else:
            await self._backend.send_command(CORE_BUFFER, f"/unset irc.server.{name}.proxy")
        self._db.upsert_server(
            name, tor=new_tor, anon=bool(srv.get("anon")),
            tls=bool(srv.get("tls")),
            noise_filter=srv.get("noise_filter", "join,part,quit"),
            auth_method=srv.get("auth_method", "none"), caps=srv.get("caps", ""))
        # The reconnect may never reach RPL_WELCOME (Tor down, SOCKS refused), so
        # mark it connecting and arm the timeout, matching the reconnect action;
        # without this the badge would stay green while the server is really down.
        self._db.set_server_status(name, "connecting")
        self._arm_connect(name)
        await self._backend.send_command(CORE_BUFFER, f"/reconnect {name}")

    def _settings_action(self, action: str, arg: str):
        if action == "language":
            langs = self._tr_obj.languages()
            names = {code: self._tr_obj.language_name(code) for code in langs}
            return (self._tr("settings.language"),
                    menu.language_menu(self._bound(), langs, names))
        if action == "lang" and arg:
            self._db.set("language", arg)
        elif action == "tor_default":
            self._db.set("tor_default", not self._db.get_bool("tor_default"))
        elif action == "senderadd":
            self._pending = ("addsender", "")
            self._prompt_back = menu.cb("nav", "senders")
            cancel = [[(self._tr("menu.cancel"), menu.cb("flow", "cancel"))]]
            return (self._tr("senders.add_prompt"), cancel)
        elif action == "senderdel":
            return self._delete_sender(arg)
        return self._nav_view("settings")

    def _delete_sender(self, arg: str):
        sl = self._sender_list
        gen, index = _split_index(arg)
        if sl is None or gen != sl["gen"]:
            return self._nav_view("settings")   # stale picker: ignore the tap
        senders = sl["senders"]
        if 0 <= index < len(senders):
            self._db.remove_sender(senders[index]["bot_id"])
        # Re-render from the database so the row we just removed is gone.
        return self._senders_view()

    def _bound(self):
        lang = self._lang()
        return lambda key: self._t(key, lang)

    def _settings_dict(self) -> dict:
        return {
            "language": self._lang(),
            "topic_backend": self._db.get("topic_backend", "private"),
            "tor_default": self._db.get_bool("tor_default"),
        }
