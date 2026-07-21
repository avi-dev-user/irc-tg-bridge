"""Persistent state for the bridge.

SQLite by default: a real ACID database, embedded, zero setup. The public
surface here is deliberately small and typed so the storage engine can be
swapped (MySQL) without the rest of the code caring how a row is stored.

WeeChat runs all script callbacks on a single thread, so no locking is needed;
the connection is opened once and reused.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE servers (
    name         TEXT PRIMARY KEY,
    anon         INTEGER NOT NULL DEFAULT 0,
    tor          INTEGER NOT NULL DEFAULT 0,
    tls          INTEGER NOT NULL DEFAULT 0,       -- connection is TLS-encrypted

    noise_filter TEXT    NOT NULL DEFAULT 'join,part,quit',
    auth_method  TEXT    NOT NULL DEFAULT 'none',   -- sasl | nickserv | none
    caps         TEXT    NOT NULL DEFAULT '',       -- comma-separated IRCv3 caps seen
    status       TEXT    NOT NULL DEFAULT 'disconnected',  -- connected | connecting | disconnected
    perform      TEXT    NOT NULL DEFAULT '',       -- newline-separated commands to run on connect
    autojoin     INTEGER NOT NULL DEFAULT 1         -- rejoin known channels on connect
);

-- Telegram bots. The primary receives updates and manages; extra rows are
-- send-only workers that share the per-chat rate limit by owning topics.
CREATE TABLE senders (
    bot_id     TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);

-- One WeeChat buffer <-> one Telegram topic. owner_bot is the sender that
-- posts to this topic (and whose token can edit/delete its messages). open is
-- 1 while we are in the channel (or the PM exists) and 0 after we part/close,
-- so a parted channel keeps its topic for reuse but drops out of the joined
-- list and is not auto-rejoined on the next connect.
CREATE TABLE mapping (
    buffer    TEXT PRIMARY KEY,
    topic_id  INTEGER NOT NULL,
    owner_bot TEXT NOT NULL,
    open      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_mapping_topic ON mapping(topic_id);

-- Message id map, both directions, for reply/react/edit/delete routing.
CREATE TABLE messages (
    buffer        TEXT    NOT NULL,
    irc_msgid     TEXT,
    tg_chat_id    INTEGER NOT NULL,
    tg_message_id INTEGER NOT NULL,
    owner_bot     TEXT    NOT NULL,
    ts            INTEGER NOT NULL,
    nick          TEXT,
    PRIMARY KEY (tg_chat_id, tg_message_id)
);
CREATE INDEX idx_messages_msgid ON messages(buffer, irc_msgid);
CREATE INDEX idx_messages_ts ON messages(ts);

-- Per-buffer high-water mark, used to backfill the gap after downtime on
-- networks that support chathistory.
CREATE TABLE buffer_state (
    buffer         TEXT PRIMARY KEY,
    last_seen_msgid TEXT
);

-- Per-server ignore list. Messages and events from an ignored nick are
-- dropped. nick is COLLATE NOCASE so "Bob" and "bob" are one entry and match
-- regardless of the case a network happens to report.
CREATE TABLE ignores (
    server TEXT NOT NULL,
    nick   TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (server, nick)
);
"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._conn.executescript(_SCHEMA)
            version = SCHEMA_VERSION
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        if version < 2:
            # Existing tables predate the servers.status column; add it in place.
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(servers)")]
            if "status" not in cols:
                self._conn.execute(
                    "ALTER TABLE servers ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'disconnected'")
            self._conn.execute("PRAGMA user_version = 2")
            self._conn.commit()
        if version < 3:
            # messages predates the nick column (used for reply-targeting); add
            # it in place so a reply to an old message still resolves.
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")]
            if cols and "nick" not in cols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN nick TEXT")
            self._conn.execute("PRAGMA user_version = 3")
            self._conn.commit()
        if version < 4:
            # On-connect setup: a perform script (raw commands) and an autojoin
            # flag for rejoining known channels. Add both in place on old rows.
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(servers)")]
            if cols and "perform" not in cols:
                self._conn.execute(
                    "ALTER TABLE servers ADD COLUMN perform TEXT NOT NULL DEFAULT ''")
            if cols and "autojoin" not in cols:
                self._conn.execute(
                    "ALTER TABLE servers ADD COLUMN autojoin INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("PRAGMA user_version = 4")
            self._conn.commit()
        if version < 5:
            # Per-server ignore list. New table, so create it in place on an
            # existing database (fresh databases get it from _SCHEMA).
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS ignores ("
                "server TEXT NOT NULL, nick TEXT NOT NULL COLLATE NOCASE, "
                "PRIMARY KEY (server, nick))")
            self._conn.execute("PRAGMA user_version = 5")
            self._conn.commit()
        if version < 6:
            # mapping predates the open flag (joined vs parted). Add it in place;
            # existing rows default to 1 (treated as joined, the safe default).
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(mapping)")]
            if cols and "open" not in cols:
                self._conn.execute(
                    "ALTER TABLE mapping ADD COLUMN open INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("PRAGMA user_version = 6")
            self._conn.commit()
        if version < 7:
            # servers predates the tls flag (shown as an encryption indicator in
            # the server view). Add it in place; existing rows default to 0, which
            # is corrected the next time the server is re-added through the flow.
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(servers)")]
            if cols and "tls" not in cols:
                self._conn.execute(
                    "ALTER TABLE servers ADD COLUMN tls INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("PRAGMA user_version = 7")
            self._conn.commit()
        # Future versions add their steps here, guarded by `version < N`.

    def close(self) -> None:
        self._conn.close()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key)
        return int(value) if value is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key)
        return value == "1" if value is not None else default

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    def upsert_server(
        self,
        name: str,
        *,
        anon: bool = False,
        tor: bool = False,
        tls: bool = False,
        noise_filter: str = "join,part,quit",
        auth_method: str = "none",
        caps: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO servers(name, anon, tor, tls, noise_filter, auth_method, caps) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "anon=excluded.anon, tor=excluded.tor, tls=excluded.tls, "
            "noise_filter=excluded.noise_filter, "
            "auth_method=excluded.auth_method, caps=excluded.caps",
            (name, int(anon), int(tor), int(tls), noise_filter, auth_method, caps),
        )
        self._conn.commit()

    def set_server_status(self, name: str, status: str) -> None:
        self._conn.execute(
            "UPDATE servers SET status = ? WHERE name = ?", (status, name))
        self._conn.commit()

    def set_perform(self, name: str, text: str) -> None:
        # Replace, not append: the console captures one line at a time and the
        # simplest contract is that the latest one wins.
        self._conn.execute(
            "UPDATE servers SET perform = ? WHERE name = ?", (text, name))
        self._conn.commit()

    def get_perform(self, name: str) -> str:
        row = self._conn.execute(
            "SELECT perform FROM servers WHERE name = ?", (name,)
        ).fetchone()
        return row["perform"] if row else ""

    def set_autojoin(self, name: str, on: bool) -> None:
        self._conn.execute(
            "UPDATE servers SET autojoin = ? WHERE name = ?", (int(on), name))
        self._conn.commit()

    def get_server(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM servers WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def list_servers(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def remove_server(self, name: str) -> None:
        # WeeChat buffer names are `irc.server.<name>` for the server buffer and
        # `irc.<name>.<channel|nick>` for its conversations. Purge every table
        # keyed by buffer, so nothing orphaned survives a re-add.
        server_buf = f"irc.server.{name}"
        like = f"irc.{name}.%"
        self._conn.execute("DELETE FROM servers WHERE name = ?", (name,))
        for table in ("mapping", "messages", "buffer_state"):
            self._conn.execute(
                f"DELETE FROM {table} WHERE buffer = ? OR buffer LIKE ?",
                (server_buf, like),
            )
        # ignores are keyed by server name, not buffer; purge them too so a
        # re-added server does not inherit a stale ignore list.
        self._conn.execute("DELETE FROM ignores WHERE server = ?", (name,))
        self._conn.commit()

    def add_sender(self, bot_id: str, token: str, *, primary: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO senders(bot_id, token, is_primary) VALUES(?, ?, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET "
            "token=excluded.token, is_primary=excluded.is_primary",
            (bot_id, token, int(primary)),
        )
        self._conn.commit()

    def list_senders(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM senders").fetchall()
        return [dict(r) for r in rows]

    def primary_sender(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM senders WHERE is_primary = 1 LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def remove_sender(self, bot_id: str) -> None:
        # Only removes the row. Topics this bot already owns keep their owner_bot
        # in mapping and fall back to the primary client at send time, so a
        # removed worker never strands a live topic.
        self._conn.execute("DELETE FROM senders WHERE bot_id = ?", (bot_id,))
        self._conn.commit()

    def owner_topic_counts(self) -> dict[str, int]:
        # How many topics each bot currently owns, for least-loaded assignment.
        rows = self._conn.execute(
            "SELECT owner_bot, COUNT(*) AS n FROM mapping GROUP BY owner_bot"
        ).fetchall()
        return {r["owner_bot"]: r["n"] for r in rows}

    def set_mapping(self, buffer: str, topic_id: int, owner_bot: str) -> None:
        self._conn.execute(
            "INSERT INTO mapping(buffer, topic_id, owner_bot) VALUES(?, ?, ?) "
            "ON CONFLICT(buffer) DO UPDATE SET "
            "topic_id=excluded.topic_id, owner_bot=excluded.owner_bot",
            (buffer, topic_id, owner_bot),
        )
        self._conn.commit()

    def topic_for_buffer(self, buffer: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT topic_id, owner_bot FROM mapping WHERE buffer = ?", (buffer,)
        ).fetchone()
        return dict(row) if row else None

    def set_channel_open(self, buffer: str, is_open: bool) -> None:
        # Mark a channel joined (1) or parted (0). The row survives a part so the
        # topic can be reopened on rejoin; only the flag flips.
        self._conn.execute(
            "UPDATE mapping SET open = ? WHERE buffer = ?", (int(is_open), buffer))
        self._conn.commit()

    def buffer_for_topic(self, topic_id: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT buffer FROM mapping WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return row["buffer"] if row else None

    def all_mappings(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT buffer, topic_id FROM mapping").fetchall()
        return {r["buffer"]: r["topic_id"] for r in rows}

    def list_channels(self, server: str) -> list[dict]:
        # Currently-joined channels only. WeeChat names them irc.<server>.<chan>
        # where the channel starts with one of #&+! (the full prefix set the
        # parser accepts); the server buffer (irc.server.<name>) and PM buffers
        # (irc.<server>.<nick>) lack a channel prefix and are excluded, and a
        # parted channel (open = 0) drops out even though its topic survives.
        rows = self._conn.execute(
            "SELECT buffer, topic_id FROM mapping "
            "WHERE (buffer LIKE ? OR buffer LIKE ? OR buffer LIKE ? OR buffer LIKE ?) "
            "AND open = 1 ORDER BY buffer",
            (f"irc.{server}.#%", f"irc.{server}.&%",
             f"irc.{server}.+%", f"irc.{server}.!%"),
        ).fetchall()
        return [{"buffer": r["buffer"], "topic_id": r["topic_id"]} for r in rows]

    def record_message(
        self,
        *,
        buffer: str,
        tg_chat_id: int,
        tg_message_id: int,
        owner_bot: str,
        irc_msgid: Optional[str] = None,
        ts: Optional[int] = None,
        nick: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO messages"
            "(buffer, irc_msgid, tg_chat_id, tg_message_id, owner_bot, ts, nick) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_chat_id, tg_message_id) DO UPDATE SET "
            "irc_msgid=excluded.irc_msgid, buffer=excluded.buffer, nick=excluded.nick",
            (buffer, irc_msgid, tg_chat_id, tg_message_id, owner_bot,
             ts if ts is not None else int(time.time()), nick),
        )
        self._conn.commit()

    def message_by_tg(self, tg_chat_id: int, tg_message_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE tg_chat_id = ? AND tg_message_id = ?",
            (tg_chat_id, tg_message_id),
        ).fetchone()
        return dict(row) if row else None

    def message_by_msgid(self, buffer: str, irc_msgid: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE buffer = ? AND irc_msgid = ?",
            (buffer, irc_msgid),
        ).fetchone()
        return dict(row) if row else None

    def prune_messages(self, older_than_days: int) -> int:
        cutoff = int(time.time()) - older_than_days * 86400
        cur = self._conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def set_last_seen(self, buffer: str, msgid: str) -> None:
        self._conn.execute(
            "INSERT INTO buffer_state(buffer, last_seen_msgid) VALUES(?, ?) "
            "ON CONFLICT(buffer) DO UPDATE SET last_seen_msgid=excluded.last_seen_msgid",
            (buffer, msgid),
        )
        self._conn.commit()

    def last_seen(self, buffer: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT last_seen_msgid FROM buffer_state WHERE buffer = ?", (buffer,)
        ).fetchone()
        return row["last_seen_msgid"] if row else None

    def add_ignore(self, server: str, nick: str) -> None:
        # NOCASE primary key folds a re-add of the same nick in a different
        # case, so OR IGNORE keeps it a single row without raising.
        self._conn.execute(
            "INSERT OR IGNORE INTO ignores(server, nick) VALUES(?, ?)",
            (server, nick))
        self._conn.commit()

    def remove_ignore(self, server: str, nick: str) -> None:
        self._conn.execute(
            "DELETE FROM ignores WHERE server = ? AND nick = ?", (server, nick))
        self._conn.commit()

    def list_ignores(self, server: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT nick FROM ignores WHERE server = ? ORDER BY nick", (server,)
        ).fetchall()
        return [r["nick"] for r in rows]

    def is_ignored(self, server: str, nick: str) -> bool:
        if not nick:
            return False
        # nick is COLLATE NOCASE, so the match is case-insensitive.
        row = self._conn.execute(
            "SELECT 1 FROM ignores WHERE server = ? AND nick = ? LIMIT 1",
            (server, nick)).fetchone()
        return row is not None
