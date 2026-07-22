"""The Telegram transport, built on Kurigram (a maintained Pyrogram fork).

This is the thin glue between Telegram and the tested logic: it renders menus
as inline keyboards, registers the message/callback/command handlers, and routes
each incoming message either to the console orchestrator (management) or to the
router (a conversation). The decision logic lives in the tested modules; this
layer only carries bytes.

Kurigram installs as the `pyrogram` module (drop-in). Verified live at deploy,
once the bot token and api credentials exist.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable, Optional

from pyrogram import Client, enums, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .commands import strip_bot_mention

# A view is (text, menu-or-None); the layer renders it as a new message or an
# in-place edit. menu is rows of (label, callback_data).
View = Optional[tuple]
ConsoleText = Callable[[int, int, str], Awaitable[None]]   # (from_id, message_id, text)
Callback = Callable[[int, str, int], Awaitable[View]]      # (from_id, callback_data, message_id) -> view
# (topic_id, message_id, text, reply_to)
Conversation = Callable[[int, int, str, Optional[int]], Awaitable[None]]
# (from_id, kind, chat_id, chat_type) -> view
Onboard = Callable[[int, str, int, str], Awaitable[View]]
# (message_id, emoji): a group member reacted to a mirrored message
ReactionAdded = Callable[[int, str], Awaitable[None]]
# (topic_id, local_path): a document/photo was sent in a conversation topic
OutgoingFile = Callable[[int, str], Awaitable[None]]

# Commands the bot owns. They are never forwarded to IRC as raw commands.
_BOT_COMMANDS = {"start", "usegroup", "cancel", "help", "settings", "language",
                 "console", "menu"}


class TelegramGateway:
    def __init__(self, *, api_id: int, api_hash: str, bot_token: str,
                 session_dir: str, group_chat_id: int, console_topic_id: int,
                 owner_bot_id: str = "primary"):
        self._client = Client("tgbridge_bot", api_id=api_id, api_hash=api_hash,
                              bot_token=bot_token, workdir=session_dir)
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_dir = session_dir
        self._group_chat_id = group_chat_id
        self._console_topic_id = console_topic_id
        self._owner_bot = owner_bot_id
        # Extra send-only bots, keyed by bot_id (the numeric prefix of a token).
        # Empty for a single-bot setup; started from the senders table by
        # start_senders(). A topic owned by one of these posts through it, so a
        # busy chat spreads its sends over several per-chat flood budgets.
        self._workers: dict[str, "Client"] = {}
        # Our own Telegram user id, filled in at start(). Used to drop reaction
        # updates the bot itself authored (mirroring an IRC reaction calls
        # send_reaction), so a reaction we placed cannot bounce back into IRC.
        self._bot_user_id: Optional[int] = None
        self._on_console: Optional[ConsoleText] = None
        self._on_callback: Optional[Callback] = None
        self._on_conversation: Optional[Conversation] = None
        self._on_onboard: Optional[Onboard] = None
        self._on_reaction_added: Optional[ReactionAdded] = None
        self._on_file: Optional[OutgoingFile] = None
        self._group_handler = False   # group-text handler registered once
        # Serialise and space out sends so a burst (channel join, backlog) does
        # not trip Telegram's flood limit. Kurigram still auto-waits on 420. The
        # flood budget is per bot, so each sender is paced on its own lock and
        # clock: that is exactly why spreading topics over several bots lifts the
        # ceiling (one shared pacer would cap total throughput regardless).
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._last_send: dict[str, float] = {}
        self._min_interval = 1.1

    def _effective_owner(self, owner_bot: Optional[str]) -> str:
        # The bot that will actually post: the requested worker only when it is
        # running, otherwise the primary (which any topic falls back to). Both
        # the pacer and the client picker resolve through this, so a topic owned
        # by a removed or failed-to-start worker is paced on the primary's clock,
        # not on a separate idle one, and cannot slip past its flood budget.
        if owner_bot is not None and owner_bot in self._workers:
            return owner_bot
        return self._owner_bot

    async def _pace(self, owner_bot: Optional[str] = None) -> None:
        key = self._effective_owner(owner_bot)
        lock = self._pace_locks.setdefault(key, asyncio.Lock())
        async with lock:
            gap = self._min_interval - (time.monotonic() - self._last_send.get(key, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_send[key] = time.monotonic()

    @property
    def chat_id(self) -> int:
        return self._group_chat_id

    @property
    def owner_bot(self) -> str:
        return self._owner_bot

    def set_console(self, topic_id: int) -> None:
        self._console_topic_id = topic_id

    def bind_group(self, chat_id: int) -> None:
        # Attach the group-text handler once. Called from start() when the group
        # is known up front, and again after /usegroup picks one live; the flag
        # keeps it from double-registering (which would dispatch every message
        # twice).
        self._group_chat_id = chat_id
        if self._group_handler:
            return
        self._client.add_handler(MessageHandler(
            self._on_text, filters.chat(chat_id) & filters.text))
        # A document or photo posted in a conversation topic is re-hosted and its
        # link sent to IRC; registered alongside the text handler so it is bound
        # exactly once for the group.
        self._client.add_handler(MessageHandler(
            self._on_media,
            filters.chat(chat_id) & (filters.document | filters.photo)))
        self._group_handler = True

    async def can_resolve_group(self) -> bool:
        # A freshly-swapped bot has never seen the group, so its peer is not in
        # the session cache and any send would fail; the caller then waits for a
        # /usegroup update to populate it instead of crashing on first send.
        if not self._group_chat_id:
            return False
        try:
            await self._client.resolve_peer(self._group_chat_id)
            return True
        except Exception:
            return False

    def handlers(self, *, console: ConsoleText, callback: Callback,
                 conversation: Conversation, onboard: Onboard,
                 reaction: Optional[ReactionAdded] = None,
                 file: Optional[OutgoingFile] = None) -> None:
        self._on_console = console
        self._on_callback = callback
        self._on_conversation = conversation
        self._on_onboard = onboard
        self._on_reaction_added = reaction
        self._on_file = file

    async def start(self) -> None:
        c = self._client
        c.add_handler(MessageHandler(self._start_cmd, filters.command("start") & filters.private))
        c.add_handler(MessageHandler(self._usegroup_cmd, filters.command("usegroup")))
        c.add_handler(MessageHandler(self._cancel_cmd, filters.command("cancel")))
        c.add_handler(MessageHandler(self._help_cmd, filters.command("help")))
        # /menu (and /console) reopen the console; not private-restricted, so the
        # menu can be resummoned inside the group, where /start does nothing.
        c.add_handler(MessageHandler(self._menu_cmd, filters.command(["menu", "console"])))
        c.add_handler(CallbackQueryHandler(self._on_cb))
        # Reaction mirroring is a draft extension on both sides; register its
        # handler lazily so a Kurigram build without the update type just skips
        # it (the feature degrades silently rather than crashing startup).
        try:
            from pyrogram.handlers import MessageReactionUpdatedHandler
            c.add_handler(MessageReactionUpdatedHandler(self._on_reaction_update))
        except Exception as exc:
            print(f"[tg] reaction updates unavailable: {exc}")
        await c.start()
        try:
            me = await c.get_me()
            self._bot_user_id = getattr(me, "id", None)
        except Exception:
            pass   # best-effort; the reaction actor guard degrades to chat-only
        if self._group_chat_id:
            self.bind_group(self._group_chat_id)

    async def start_senders(self, senders) -> None:
        """Live glue: start a send-only Kurigram client per extra bot token so
        the pool can post through it. Untested (it needs a real Telegram login);
        the decision of which bot owns a topic is the tested router/pool logic.
        A worker that fails to start is skipped, so one bad token cannot stop the
        bridge, and its topics fall back to the primary at send time."""
        for s in senders:
            bot_id = s["bot_id"]
            if bot_id in self._workers or bot_id == self._owner_bot:
                continue
            try:
                worker = Client(
                    f"tgbridge_bot_{bot_id}", api_id=self._api_id,
                    api_hash=self._api_hash, bot_token=s["token"],
                    workdir=self._session_dir)
                await worker.start()
                self._workers[bot_id] = worker
            except Exception as exc:
                print(f"[tg] sender {bot_id} failed to start: {exc}")

    def _client_for(self, owner_bot: Optional[str]) -> "Client":
        # A worker if this bot owns the topic and is running; otherwise the
        # primary, which any topic can post through (an unknown or removed owner
        # falls back here instead of failing the send). Resolved through the same
        # helper the pacer uses so the two never disagree on which bot sends.
        key = self._effective_owner(owner_bot)
        worker = self._workers.get(key)
        return worker if worker is not None else self._client

    async def stop(self) -> None:
        for worker in self._workers.values():
            try:
                await worker.stop()
            except Exception:
                pass
        await self._client.stop()

    async def create_topic(self, title: str,
                           owner_bot: Optional[str] = None) -> int:
        topic = await self._client_for(owner_bot).create_forum_topic(
            self._group_chat_id, title)
        return topic.id

    async def send(self, topic_id: int, html: str,
                   reply_to_message_id: Optional[int] = None,
                   owner_bot: Optional[str] = None) -> int:
        await self._pace(owner_bot)
        msg = await self._client_for(owner_bot).send_message(
            self._group_chat_id, html, message_thread_id=topic_id,
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=reply_to_message_id)
        return msg.id

    async def edit_message(self, message_id: int, html: str,
                           owner_bot: Optional[str] = None) -> None:
        # Edit a message in place (used to turn a "receiving..." notice into the
        # finished file link). A bot can only edit its own messages, so this must
        # go through the same worker that sent it. Best-effort.
        await self._pace(owner_bot)
        try:
            await self._client_for(owner_bot).edit_message_text(
                self._group_chat_id, message_id, html,
                parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

    async def user_label(self, user_id: int) -> str:
        # Resolve a member's @username (or first name) for a highlight mention. A
        # bot cannot resolve a bare user id it has never "met" (get_users and
        # get_chat_member fail with PEER_ID_INVALID until then), but iterating the
        # group's members returns each user's access hash, which resolves them
        # even on a cold start. A public @username, placed later as plain text, is
        # what makes a mention actually ping the operator.
        try:
            await self._client.get_chat(self._group_chat_id)   # warm the group peer
            async for m in self._client.get_chat_members(self._group_chat_id):
                if m.user and m.user.id == user_id:
                    if getattr(m.user, "username", None):
                        return f"@{m.user.username}"
                    return getattr(m.user, "first_name", "") or ""
        except Exception:
            pass
        return ""

    async def send_console(self, text: str) -> int:
        return await self.send(self._console_topic_id, text)

    async def edit_menu(self, message_id: int, title: str, menu) -> None:
        # Edit a message in place so a flow shows one evolving prompt whose
        # buttons update as steps are answered, instead of a trail of messages.
        await self._pace()
        try:
            await self._client.edit_message_text(
                self._group_chat_id, message_id, title,
                reply_markup=self._kb(menu), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

    @staticmethod
    def _kb(menu) -> Optional[InlineKeyboardMarkup]:
        if not menu:
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in menu
        ])

    async def send_menu(self, title: str, menu) -> int:
        return await self.send_menu_in(self._console_topic_id, title, menu)

    async def send_menu_in(self, topic_id: int, title: str, menu) -> int:
        await self._pace()
        msg = await self._client.send_message(
            self._group_chat_id, title, message_thread_id=topic_id,
            reply_markup=self._kb(menu), parse_mode=enums.ParseMode.HTML)
        return msg.id

    async def delete_topic(self, topic_id: int) -> None:
        # Best-effort: drop the whole topic when the user asks to delete it.
        try:
            await self._client.delete_forum_topic(self._group_chat_id, topic_id)
        except Exception:
            pass

    async def _on_text(self, _c, message) -> None:
        if message.from_user is None:
            return  # anonymous admin / channel-linked post: no identity to act on
        text = message.text or ""
        if text.startswith("/"):
            cmd = text[1:].split()[0].split("@")[0].lower()
            if cmd in _BOT_COMMANDS:
                return  # our own command, handled elsewhere; never forward to IRC
        text = strip_bot_mention(text)   # /part@thebot -> /part before IRC sees it
        topic_id = message.message_thread_id or self._console_topic_id
        if topic_id == self._console_topic_id:
            if self._on_console:
                await self._on_console(message.from_user.id, message.id, text)
        elif self._on_conversation:
            await self._on_conversation(topic_id, message.id, text,
                                        message.reply_to_message_id)

    async def _on_media(self, _c, message) -> None:
        # A document or photo in a conversation topic: download it to a temp path
        # and hand it to the router, which re-hosts it and posts the link to IRC.
        # Media in the console topic is ignored (nothing to send it to). The temp
        # file is removed after, even if the handoff raises.
        if message.from_user is None or self._on_file is None:
            return
        topic_id = message.message_thread_id or self._console_topic_id
        if topic_id == self._console_topic_id:
            return
        try:
            path = await message.download()
        except Exception as exc:
            print(f"[tg] media download failed: {exc}")
            return
        try:
            await self._on_file(topic_id, path)
        except Exception as exc:
            print(f"[tg] outgoing file handoff failed: {exc}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    async def react(self, message_id: int, emoji: str) -> None:
        # Delivery feedback: mark a message sent to IRC (or failed). Best-effort.
        try:
            await self._client.send_reaction(self._group_chat_id, message_id, emoji=emoji)
        except Exception:
            pass

    async def _on_reaction_update(self, _c, update) -> None:
        # Thin glue: a member reacted to a message in the group. Hand the newest
        # emoji to the tested router logic, which mirrors it onto IRC. Reaction
        # removals (empty new list) and custom/premium emoji (no unicode form)
        # are skipped. Best-effort; a failure here must not disturb the client.
        if self._on_reaction_added is None:
            return
        # Only the bridge group. Message ids are per-chat, so a reaction in any
        # other chat the bot shares could otherwise collide with a mirrored id
        # and fire a stray reaction into IRC.
        chat = getattr(update, "chat", None)
        if chat is not None and getattr(chat, "id", None) != self._group_chat_id:
            return
        # Skip a reaction the bot itself placed (mirroring an IRC reaction), so
        # it cannot echo back and re-mirror onto IRC.
        actor = getattr(update, "user", None)
        if actor is not None and getattr(actor, "id", None) == self._bot_user_id \
                and self._bot_user_id is not None:
            return
        try:
            reactions = getattr(update, "new_reaction", None) or []
            emoji = getattr(reactions[-1], "emoji", None) if reactions else None
            if emoji:
                await self._on_reaction_added(update.message_id, emoji)
        except Exception as exc:
            print(f"[tg] reaction update failed: {exc}")

    async def delete_message(self, message_id: int) -> None:
        # Best-effort: used to scrub a password the admin typed in the console.
        try:
            await self._client.delete_messages(self._group_chat_id, message_id)
        except Exception:
            pass

    async def send_typing(self, topic_id: int) -> None:
        # Show the "typing" action in a topic while someone on IRC is typing.
        # It expires after about 5s, so the router re-sends it periodically.
        try:
            await self._client.send_chat_action(
                self._group_chat_id, enums.ChatAction.TYPING, message_thread_id=topic_id)
        except Exception:
            pass

    async def close_topic(self, topic_id: int) -> None:
        # Best-effort: mark the topic closed when we leave the IRC channel.
        try:
            await self._client.close_forum_topic(self._group_chat_id, topic_id)
        except Exception:
            pass

    async def reopen_topic(self, topic_id: int) -> None:
        # Best-effort: reopen the topic when we rejoin the IRC channel.
        try:
            await self._client.reopen_forum_topic(self._group_chat_id, topic_id)
        except Exception:
            pass

    async def _on_cb(self, _c, query) -> None:
        try:
            if self._on_callback:
                view = await self._on_callback(query.from_user.id, query.data,
                                               query.message.id)
                if view:
                    text, menu = view
                    try:
                        await query.message.edit_text(
                            text, reply_markup=self._kb(menu),
                            parse_mode=enums.ParseMode.HTML)
                    except Exception:
                        pass  # edit fails if content is unchanged; harmless
        except Exception as exc:
            # A view build can raise (e.g. a legacy server name too long for
            # callback_data). Do not leave the tap spinning: log and answer.
            print(f"[tg] callback {query.data!r} failed: {exc}")
        await query.answer()

    async def _start_cmd(self, _c, message) -> None:
        print(f"[tg] /start from {message.from_user.id if message.from_user else '?'}")
        await self._onboard_reply(message, "start")

    async def _usegroup_cmd(self, _c, message) -> None:
        ct = getattr(message.chat.type, "value", str(message.chat.type))
        print(f"[tg] /usegroup in chat {message.chat.id} type={ct}")
        await self._onboard_reply(message, "usegroup")

    async def _cancel_cmd(self, _c, message) -> None:
        await self._reply_callback(message, "flow:cancel")

    async def _help_cmd(self, _c, message) -> None:
        await self._reply_callback(message, "sys:help")

    async def _menu_cmd(self, _c, message) -> None:
        await self._onboard_reply(message, "menu")

    async def _reply_callback(self, message, data: str) -> None:
        if not self._on_callback or message.from_user is None:
            return
        view = await self._on_callback(message.from_user.id, data)
        if view:
            text, menu = view
            await message.reply_text(text, reply_markup=self._kb(menu),
                                     parse_mode=enums.ParseMode.HTML)

    async def _onboard_reply(self, message, kind: str) -> None:
        if not self._on_onboard:
            return
        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        view = await self._on_onboard(
            message.from_user.id, kind, message.chat.id, chat_type)
        if view:
            text, menu = view
            await message.reply_text(text, reply_markup=self._kb(menu),
                                     parse_mode=enums.ParseMode.HTML)
