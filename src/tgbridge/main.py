"""Entry point: wire the pieces into one running program.

    WeeChat  <-relay->  WeechatIrcBackend  ->  Router  <->  TelegramGateway  <->  Telegram

Two flows run concurrently: IRC lines stream in and are routed to Telegram;
Telegram messages are dispatched into IRC. Configuration comes from the
environment (secrets) and the database (everything set from the bot).
"""

from __future__ import annotations

import asyncio
import os

from .db import Database
from .filewatch import parse_xfer_filename, watch_dir
from .i18n import Translator
from .ircbackend import WeechatIrcBackend, reconcile_server_status
from .manager import Manager
from .router import Router
from .senderpool import SenderPool
from .telegram import TelegramGateway
from .weechat_relay import WeechatRelay

LOCALES_DIR = os.environ.get("TGBRIDGE_LOCALES", "/opt/irc-tg-bridge/locales")

DB_PATH = os.environ.get("TGBRIDGE_DB", "/var/lib/irc-tg-bridge/state/bridge.db")
SESSION_DIR = os.environ.get("TGBRIDGE_SESSIONS", "/var/lib/irc-tg-bridge/state")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


async def run() -> None:
    db = Database(DB_PATH)
    tr = Translator(LOCALES_DIR)
    admin_id = int(_require("ADMIN_TELEGRAM_ID"))
    group_chat_id = db.get_int("group_chat_id", 0)
    console_topic_id = db.get_int("console_topic_id", 0)

    gateway = TelegramGateway(
        api_id=int(_require("API_ID")),
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        session_dir=SESSION_DIR,
        group_chat_id=group_chat_id,
        console_topic_id=console_topic_id,
    )

    # Onboarding handlers, wired before the client starts so /start and
    # /usegroup work immediately. Language can be set from the private chat here;
    # console/conversation need IRC, so they stay no-ops until full mode.
    group_ready = asyncio.Event()
    onboard = Manager(db, None, gateway, tr, None, admin_id=admin_id,
                      on_group_set=lambda _cid: group_ready.set())
    gateway.handlers(console=_noop, callback=onboard.on_callback,
                     conversation=_noop, onboard=onboard.on_onboard)
    await gateway.start()

    # Decide whether we can go straight to full mode. A returning bridge already
    # has its console topic, so it never sends blind at startup - go straight in,
    # no network check (a slow/overloaded box must not force re-onboarding). Only
    # first-time setup must confirm the bot can resolve the group before it tries
    # to create the console topic; a freshly-swapped bot cannot, so it waits for a
    # /usegroup update to teach it the group, then comes up in place, no restart.
    ready = bool(group_chat_id) and (
        bool(console_topic_id) or await gateway.can_resolve_group())
    if not ready:
        why = "no group set" if not group_chat_id else "bot not in the group yet"
        print(f"[main] ONBOARDING ({why}); waiting for /usegroup")
        await group_ready.wait()
        group_chat_id = db.get_int("group_chat_id", 0)
        gateway.bind_group(group_chat_id)
        print(f"[main] group ready ({group_chat_id}); bringing up full mode")

    relay = WeechatRelay(
        os.environ.get("RELAY_HOST", "127.0.0.1"),
        int(os.environ.get("RELAY_PORT", "9799")),
        _require("RELAY_SECRET"),
    )
    await relay.connect()
    backend = WeechatIrcBackend(relay)

    # Resolve the admin's @username (through the group) once so a highlight can
    # mention and ping them; falls back to a stored label, then to nothing.
    mention_label = await gateway.user_label(admin_id) or db.get("mention_label", "")

    # Sender pool: only when extra bots are configured. The primary (the main
    # bot) is always the first id, then each worker. With no workers the pool is
    # None and every topic is owned by (and posted through) the primary, exactly
    # as a single-bot bridge. Starting the worker clients is live glue.
    workers = [s for s in db.list_senders() if not s.get("is_primary")]
    sender_pool = None
    if workers:
        sender_pool = SenderPool([gateway.owner_bot] + [w["bot_id"] for w in workers])
        await gateway.start_senders(workers)

    router = Router(db, gateway, backend, sender_pool=sender_pool,
                    mention_user_id=admin_id, mention_label=mention_label,
                    translator=tr)
    manager = Manager(db, backend, gateway, tr, router, admin_id=admin_id)
    router.set_server_status_callback(manager.on_server_status)
    router.set_channel_list_callback(manager.on_channel_list)
    router.set_names_callback(manager.on_names)
    gateway.handlers(console=manager.on_console_text, callback=manager.on_callback,
                     conversation=router.handle_telegram, onboard=manager.on_onboard,
                     reaction=router.handle_telegram_reaction,
                     file=router.handle_outgoing_file)

    # Incoming DCC re-hosting: when a download directory is configured, watch it
    # for completed transfers and re-host each one to the sender's PM topic. The
    # filename's nick prefix identifies the sender; the server it belongs to is a
    # single configured value (DCC does not encode it in the filename). Both are
    # live config, so the watcher only runs when the directory is set.
    xfer_dir = os.environ.get("TGBRIDGE_XFER_DIR")
    xfer_server = os.environ.get("TGBRIDGE_XFER_SERVER", "")

    async def _on_incoming_file(full_path: str) -> None:
        nick, _original = parse_xfer_filename(os.path.basename(full_path))
        if not nick or not xfer_server:
            print(f"[filewatch] cannot route {full_path}: "
                  f"nick={nick!r} server={xfer_server!r}")
            return
        await router.handle_incoming_file(nick, xfer_server, full_path)

    if xfer_dir:
        asyncio.create_task(watch_dir(xfer_dir, _on_incoming_file))

    # First run after the group is set: create the console topic and open the
    # menu, otherwise the bot would have no console to drive it from. On a later
    # restart the console already exists, so we do NOT re-post the menu (that
    # would drop a fresh "IRC Control" message on every restart); the user opens
    # it with /menu when they want it.
    if not console_topic_id:
        console_topic_id = await gateway.create_topic(
            tr.t("menu.title", db.get("language", "en")))
        db.set("console_topic_id", console_topic_id)
        gateway.set_console(console_topic_id)
        try:
            await manager.show_main()
        except Exception as exc:
            # Best-effort: if the box is momentarily overloaded the menu can wait;
            # do not take the whole bridge down over one failed send.
            print(f"[main] could not post the console menu yet: {exc}")

    async def consume() -> None:
        async for item in backend.stream():
            try:
                await router.handle_irc(item)
            except Exception as exc:  # one bad line must not kill the bridge
                print(f"[router] error handling {type(item).__name__}: {exc}")

    try:
        # Reconnect loop: WeeChat restarts or a dropped socket must not end the
        # bridge. backend.start() re-lists buffers (ids reset per session).
        while True:
            try:
                await backend.start()
                # After a restart the still-connected servers send no new
                # RPL_WELCOME, so their badges would read stale. Reconcile from
                # the freshly listed buffers before streaming, so status matches
                # reality at once; a later real 001 still flips status as usual.
                reconcile_server_status(db, backend.connected_servers())
                await consume()
            except Exception as exc:
                print(f"[relay] stream error: {exc}")
            print("[relay] disconnected; reconnecting in 5s")
            await asyncio.sleep(5)
            try:
                await relay.reconnect()
            except Exception as exc:
                print(f"[relay] reconnect failed: {exc}")
    finally:
        manager.close()
        await router.flush()
        await gateway.stop()
        await relay.close()


async def _noop(*_args) -> None:
    return None


def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
