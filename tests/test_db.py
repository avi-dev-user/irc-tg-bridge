"""Tests for the persistence layer."""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.db import Database  # noqa: E402


def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "bridge.db")
    return Database(path)


def test_settings_typed_roundtrip():
    db = fresh_db()
    assert db.get("language", "en") == "en"
    db.set("language", "he")
    assert db.get("language") == "he"

    db.set("offset", 42)
    assert db.get_int("offset") == 42
    assert db.get_int("missing", 7) == 7

    db.set("tor_default", True)
    assert db.get_bool("tor_default") is True
    db.set("tor_default", False)
    assert db.get_bool("tor_default") is False


def test_server_crud_and_mapping_cascade():
    db = fresh_db()
    db.upsert_server("libera", auth_method="sasl")
    # real WeeChat buffer names: irc.server.<srv> and irc.<srv>.<conv>
    db.set_mapping("irc.server.libera", 9, "primary")
    db.set_mapping("irc.libera.#weechat", 10, "primary")
    db.set_mapping("irc.libera.alice", 11, "primary")
    db.set_mapping("irc.oftc.#debian", 20, "primary")
    db.record_message(buffer="irc.libera.#weechat", tg_chat_id=1, tg_message_id=1,
                      owner_bot="primary", irc_msgid="x")
    db.set_last_seen("irc.libera.#weechat", "x")

    assert db.get_server("libera")["auth_method"] == "sasl"
    assert len(db.list_servers()) == 1

    # editing an existing server keeps it a single row
    db.upsert_server("libera", anon=True, tor=True)
    srv = db.get_server("libera")
    assert srv["anon"] == 1 and srv["tor"] == 1
    assert len(db.list_servers()) == 1

    # removing a server purges every buffer-keyed row for that server only
    db.remove_server("libera")
    assert db.get_server("libera") is None
    assert db.topic_for_buffer("irc.server.libera") is None
    assert db.topic_for_buffer("irc.libera.#weechat") is None
    assert db.topic_for_buffer("irc.libera.alice") is None
    assert db.message_by_msgid("irc.libera.#weechat", "x") is None
    assert db.last_seen("irc.libera.#weechat") is None
    # a different server is untouched
    assert db.topic_for_buffer("irc.oftc.#debian")["topic_id"] == 20


def test_mapping_both_directions():
    db = fresh_db()
    db.set_mapping("libera.#python", 5, "sender2")
    assert db.topic_for_buffer("libera.#python") == {"topic_id": 5, "owner_bot": "sender2"}
    assert db.buffer_for_topic(5) == "libera.#python"
    assert db.buffer_for_topic(999) is None
    assert db.all_mappings() == {"libera.#python": 5}


def test_senders_pool():
    db = fresh_db()
    db.add_sender("bot_main", "tok1", primary=True)
    db.add_sender("bot_send", "tok2")
    assert db.primary_sender()["bot_id"] == "bot_main"
    assert len(db.list_senders()) == 2


def test_remove_sender():
    db = fresh_db()
    db.add_sender("bot_main", "tok1", primary=True)
    db.add_sender("bot_send", "tok2")
    db.remove_sender("bot_send")
    ids = {s["bot_id"] for s in db.list_senders()}
    assert ids == {"bot_main"}
    # removing a non-existent id is a no-op, not an error
    db.remove_sender("nope")
    assert len(db.list_senders()) == 1


def test_add_sender_upserts_token():
    db = fresh_db()
    db.add_sender("bot", "old")
    db.add_sender("bot", "new")
    senders = db.list_senders()
    assert len(senders) == 1 and senders[0]["token"] == "new"


def test_owner_topic_counts():
    db = fresh_db()
    assert db.owner_topic_counts() == {}
    db.set_mapping("irc.lt.#a", 1, "primary")
    db.set_mapping("irc.lt.#b", 2, "primary")
    db.set_mapping("irc.lt.#c", 3, "worker")
    assert db.owner_topic_counts() == {"primary": 2, "worker": 1}
    # re-owning an existing buffer moves the count, it does not double it
    db.set_mapping("irc.lt.#c", 3, "primary")
    assert db.owner_topic_counts() == {"primary": 3}


def test_message_map_and_prune():
    db = fresh_db()
    db.record_message(
        buffer="libera.#python", tg_chat_id=100, tg_message_id=7,
        owner_bot="primary", irc_msgid="abc", ts=1000,
    )
    by_tg = db.message_by_tg(100, 7)
    assert by_tg["buffer"] == "libera.#python"
    assert by_tg["owner_bot"] == "primary"
    assert db.message_by_msgid("libera.#python", "abc")["tg_message_id"] == 7
    assert db.message_by_tg(100, 999) is None

    # prune drops rows older than the cutoff, keeps recent ones
    db.record_message(
        buffer="libera.#python", tg_chat_id=100, tg_message_id=8,
        owner_bot="primary", irc_msgid="def",
    )
    removed = db.prune_messages(older_than_days=30)
    assert removed == 1
    assert db.message_by_tg(100, 7) is None
    assert db.message_by_tg(100, 8) is not None


def test_fresh_db_has_server_status_column():
    db = fresh_db()
    db.upsert_server("libera")
    assert db.get_server("libera")["status"] == "disconnected"
    assert db.list_servers()[0]["status"] == "disconnected"
    db.set_server_status("libera", "connected")
    assert db.get_server("libera")["status"] == "connected"


def test_migration_adds_status_to_a_v1_database():
    # A pre-status (schema v1) servers table must gain the column on open,
    # with existing rows defaulting to disconnected.
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE servers ("
        "name TEXT PRIMARY KEY, anon INTEGER NOT NULL DEFAULT 0, "
        "tor INTEGER NOT NULL DEFAULT 0, "
        "noise_filter TEXT NOT NULL DEFAULT 'join,part,quit', "
        "auth_method TEXT NOT NULL DEFAULT 'none', "
        "caps TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO servers(name) VALUES('old')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_server("old")["status"] == "disconnected"
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7


def test_record_message_stores_and_returns_nick():
    db = fresh_db()
    db.record_message(
        buffer="libera.#python", tg_chat_id=100, tg_message_id=7,
        owner_bot="primary", irc_msgid="abc", nick="alice",
    )
    assert db.message_by_tg(100, 7)["nick"] == "alice"
    # nick is optional and defaults to NULL
    db.record_message(
        buffer="libera.#python", tg_chat_id=100, tg_message_id=8,
        owner_bot="primary", irc_msgid="def",
    )
    assert db.message_by_tg(100, 8)["nick"] is None


def test_migration_adds_nick_to_a_v2_database():
    # A schema v2 messages table (status already present, nick not) must gain
    # the nick column on open, with existing rows defaulting to NULL.
    path = os.path.join(tempfile.mkdtemp(), "v2.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages ("
        "buffer TEXT NOT NULL, irc_msgid TEXT, tg_chat_id INTEGER NOT NULL, "
        "tg_message_id INTEGER NOT NULL, owner_bot TEXT NOT NULL, "
        "ts INTEGER NOT NULL, PRIMARY KEY (tg_chat_id, tg_message_id))")
    conn.execute(
        "INSERT INTO messages(buffer, tg_chat_id, tg_message_id, owner_bot, ts) "
        "VALUES('libera.#python', 100, 7, 'primary', 1000)")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.message_by_tg(100, 7)["nick"] is None
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    # the new column is writable after migration
    db.record_message(buffer="libera.#python", tg_chat_id=100, tg_message_id=8,
                      owner_bot="primary", nick="bob")
    assert db.message_by_tg(100, 8)["nick"] == "bob"


def test_list_channels_excludes_server_and_pm_buffers():
    db = fresh_db()
    db.upsert_server("libera")
    db.set_mapping("irc.server.libera", 1, "primary")   # server buffer
    db.set_mapping("irc.libera.#weechat", 2, "primary")  # channel
    db.set_mapping("irc.libera.&local", 3, "primary")    # local channel
    db.set_mapping("irc.libera.+modeless", 6, "primary")  # modeless channel
    db.set_mapping("irc.libera.!safechan", 7, "primary")  # safe channel
    db.set_mapping("irc.libera.alice", 4, "primary")     # PM, not a channel
    db.set_mapping("irc.oftc.#debian", 5, "primary")     # different server

    chans = db.list_channels("libera")
    buffers = [c["buffer"] for c in chans]
    # every accepted channel prefix (#, &, +, !) is listed; sorted by buffer
    assert buffers == ["irc.libera.!safechan", "irc.libera.#weechat",
                       "irc.libera.&local", "irc.libera.+modeless"]
    assert all(set(c.keys()) == {"buffer", "topic_id"} for c in chans)
    assert dict((c["buffer"], c["topic_id"]) for c in chans) == {
        "irc.libera.#weechat": 2, "irc.libera.&local": 3,
        "irc.libera.+modeless": 6, "irc.libera.!safechan": 7}
    # the server buffer, the PM buffer, and other servers are excluded
    assert "irc.server.libera" not in buffers
    assert "irc.libera.alice" not in buffers
    assert "irc.oftc.#debian" not in buffers
    assert db.list_channels("oftc") == [{"buffer": "irc.oftc.#debian", "topic_id": 5}]
    assert db.list_channels("nosuch") == []


def test_set_channel_open_toggles_joined_list():
    db = fresh_db()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    db.set_mapping("irc.libera.#python", 3, "primary")
    # a fresh mapping is joined (open defaults to 1)
    assert [c["buffer"] for c in db.list_channels("libera")] == \
        ["irc.libera.#python", "irc.libera.#weechat"]
    # parting a channel drops it from the joined list, but keeps its topic row
    db.set_channel_open("irc.libera.#python", False)
    assert [c["buffer"] for c in db.list_channels("libera")] == ["irc.libera.#weechat"]
    assert db.topic_for_buffer("irc.libera.#python") == {"topic_id": 3, "owner_bot": "primary"}
    # rejoining brings it back
    db.set_channel_open("irc.libera.#python", True)
    assert len(db.list_channels("libera")) == 2


def test_migration_adds_open_to_a_v5_mapping():
    # A schema v5 mapping table (no open column) must gain it on open, with
    # existing rows defaulting to 1 (treated as joined).
    path = os.path.join(tempfile.mkdtemp(), "v5.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE mapping ("
        "buffer TEXT PRIMARY KEY, topic_id INTEGER NOT NULL, owner_bot TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO mapping(buffer, topic_id, owner_bot) "
        "VALUES('irc.libera.#weechat', 2, 'primary')")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    # the pre-existing channel is treated as joined and the flag is writable
    assert db.list_channels("libera") == [{"buffer": "irc.libera.#weechat", "topic_id": 2}]
    db.set_channel_open("irc.libera.#weechat", False)
    assert db.list_channels("libera") == []


def test_last_seen():
    db = fresh_db()
    assert db.last_seen("libera.#python") is None
    db.set_last_seen("libera.#python", "msgid-123")
    assert db.last_seen("libera.#python") == "msgid-123"


def test_fresh_db_has_perform_and_autojoin_defaults():
    db = fresh_db()
    db.upsert_server("libera")
    srv = db.get_server("libera")
    assert srv["perform"] == ""       # empty perform script by default
    assert srv["autojoin"] == 1       # autojoin on by default
    assert db.get_perform("libera") == ""


def test_get_set_perform_roundtrip():
    db = fresh_db()
    db.upsert_server("libera")
    db.set_perform("libera", "/msg InviteBot !invite KEY\n/mode +x")
    assert db.get_perform("libera") == "/msg InviteBot !invite KEY\n/mode +x"
    assert db.get_server("libera")["perform"] == "/msg InviteBot !invite KEY\n/mode +x"
    # set_perform replaces, it does not append
    db.set_perform("libera", "/join #new")
    assert db.get_perform("libera") == "/join #new"
    # unknown server: get returns empty, never raises
    assert db.get_perform("nosuch") == ""


def test_set_autojoin_toggles():
    db = fresh_db()
    db.upsert_server("libera")
    assert db.get_server("libera")["autojoin"] == 1
    db.set_autojoin("libera", False)
    assert db.get_server("libera")["autojoin"] == 0
    db.set_autojoin("libera", True)
    assert db.get_server("libera")["autojoin"] == 1


def test_upsert_server_preserves_perform_and_autojoin():
    # editing a server through upsert (e.g. a tor toggle) must not wipe the
    # perform script or the autojoin flag, which it does not carry.
    db = fresh_db()
    db.upsert_server("libera")
    db.set_perform("libera", "/oper me pw")
    db.set_autojoin("libera", False)
    db.upsert_server("libera", tor=True)
    srv = db.get_server("libera")
    assert srv["tor"] == 1
    assert srv["perform"] == "/oper me pw"
    assert srv["autojoin"] == 0


def test_migration_adds_perform_and_autojoin_to_a_v3_database():
    # A schema v3 servers table (status present, perform/autojoin not) must gain
    # both columns on open, with existing rows defaulting to '' and 1.
    path = os.path.join(tempfile.mkdtemp(), "v3.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE servers ("
        "name TEXT PRIMARY KEY, anon INTEGER NOT NULL DEFAULT 0, "
        "tor INTEGER NOT NULL DEFAULT 0, "
        "noise_filter TEXT NOT NULL DEFAULT 'join,part,quit', "
        "auth_method TEXT NOT NULL DEFAULT 'none', "
        "caps TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'disconnected')")
    conn.execute("INSERT INTO servers(name) VALUES('old')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    db = Database(path)
    srv = db.get_server("old")
    assert srv["perform"] == "" and srv["autojoin"] == 1
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    # the new columns are writable after migration
    db.set_perform("old", "/join #back")
    db.set_autojoin("old", False)
    assert db.get_perform("old") == "/join #back"
    assert db.get_server("old")["autojoin"] == 0


def test_ignore_add_list_remove_roundtrip():
    db = fresh_db()
    assert db.list_ignores("libera") == []
    db.add_ignore("libera", "spammer")
    db.add_ignore("libera", "troll")
    assert db.list_ignores("libera") == ["spammer", "troll"]   # ordered by nick
    assert db.is_ignored("libera", "spammer") is True
    db.remove_ignore("libera", "spammer")
    assert db.list_ignores("libera") == ["troll"]
    assert db.is_ignored("libera", "spammer") is False


def test_is_ignored_is_case_insensitive():
    db = fresh_db()
    db.add_ignore("libera", "SpamBot")
    assert db.is_ignored("libera", "spambot") is True
    assert db.is_ignored("libera", "SPAMBOT") is True
    assert db.is_ignored("libera", "SpamBot") is True
    # a re-add in a different case does not create a second row
    db.add_ignore("libera", "spambot")
    assert db.list_ignores("libera") == ["SpamBot"]
    # a case-varied remove still matches the stored entry
    db.remove_ignore("libera", "SPAMBOT")
    assert db.list_ignores("libera") == []


def test_ignores_are_scoped_per_server():
    db = fresh_db()
    db.add_ignore("libera", "bob")
    assert db.is_ignored("libera", "bob") is True
    assert db.is_ignored("oftc", "bob") is False   # a different server is unaffected
    assert db.list_ignores("oftc") == []


def test_is_ignored_empty_nick_is_false():
    db = fresh_db()
    assert db.is_ignored("libera", "") is False


def test_remove_server_purges_its_ignores():
    db = fresh_db()
    db.upsert_server("libera")
    db.add_ignore("libera", "bob")
    db.add_ignore("oftc", "carol")
    db.remove_server("libera")
    assert db.list_ignores("libera") == []         # purged with the server
    assert db.list_ignores("oftc") == ["carol"]    # another server's list survives


def test_fresh_db_has_ignores_table():
    db = fresh_db()
    # a fresh database gets the table from _SCHEMA, not just via migration
    db.add_ignore("libera", "x")
    assert db.is_ignored("libera", "x") is True


def test_migration_adds_ignores_to_a_v4_database():
    # A schema v4 database (no ignores table) must gain it on open.
    path = os.path.join(tempfile.mkdtemp(), "v4.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE servers ("
        "name TEXT PRIMARY KEY, anon INTEGER NOT NULL DEFAULT 0, "
        "tor INTEGER NOT NULL DEFAULT 0, "
        "noise_filter TEXT NOT NULL DEFAULT 'join,part,quit', "
        "auth_method TEXT NOT NULL DEFAULT 'none', "
        "caps TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'disconnected', "
        "perform TEXT NOT NULL DEFAULT '', "
        "autojoin INTEGER NOT NULL DEFAULT 1)")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    # the table exists and is usable after migration, case-insensitively
    db.add_ignore("old", "Nuisance")
    assert db.is_ignored("old", "nuisance") is True


def test_migration_adds_tls_to_a_v6_database():
    path = os.path.join(tempfile.mkdtemp(), "b.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE servers ("
        "name TEXT PRIMARY KEY, anon INTEGER NOT NULL DEFAULT 0, "
        "tor INTEGER NOT NULL DEFAULT 0, "
        "noise_filter TEXT NOT NULL DEFAULT 'join,part,quit', "
        "auth_method TEXT NOT NULL DEFAULT 'none', "
        "caps TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'disconnected', "
        "perform TEXT NOT NULL DEFAULT '', "
        "autojoin INTEGER NOT NULL DEFAULT 1)")
    conn.execute("INSERT INTO servers(name) VALUES('old')")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    cols = [r["name"] for r in db._conn.execute("PRAGMA table_info(servers)")]
    assert "tls" in cols
    assert db.get_server("old")["tls"] == 0   # existing rows default to off


def test_upsert_server_round_trips_tls():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    db.upsert_server("secure", tls=True)
    assert db.get_server("secure")["tls"] == 1
    db.upsert_server("plain", tls=False)
    assert db.get_server("plain")["tls"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
