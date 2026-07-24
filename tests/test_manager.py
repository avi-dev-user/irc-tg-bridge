"""Tests for the console orchestration (manager): add-server flow, callbacks,
admin-only enforcement. Telegram and IRC are faked; the database is real."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.db import Database  # noqa: E402
from tgbridge.i18n import Translator  # noqa: E402
from tgbridge.manager import Manager, CORE_BUFFER  # noqa: E402

LOCALES = os.path.join(os.path.dirname(__file__), "..", "locales")
ADMIN = 1


class FakeGW:
    def __init__(self):
        self.console = []
        self.menus = []
        self.edits = []
        self.deleted = []
        self._mid = 1000

    async def send_console(self, text):
        self.console.append(text)

    async def send_menu(self, title, m):
        self._mid += 1
        self.menus.append((title, m))
        return self._mid

    async def edit_menu(self, message_id, title, m):
        self.edits.append((message_id, title, m))

    async def delete_message(self, message_id):
        self.deleted.append(message_id)

    async def start_senders(self, senders):
        self.started_senders = getattr(self, "started_senders", [])
        self.started_senders.extend(senders)

    async def close_topic(self, topic_id):
        self.closed = getattr(self, "closed", [])
        self.closed.append(topic_id)

    async def delete_topic(self, topic_id):
        self.deleted_topics = getattr(self, "deleted_topics", [])
        self.deleted_topics.append(topic_id)


class FakeBackend:
    def __init__(self):
        self.commands = []

    async def send_command(self, buffer, command):
        self.commands.append((buffer, command))

    async def send_message(self, buffer, text):
        pass


def make():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    gw, be = FakeGW(), FakeBackend()
    mgr = Manager(db, be, gw, tr, router=None, admin_id=ADMIN)
    return mgr, db, gw, be


def run(coro):
    # asyncio.run cancels any leftover tasks (e.g. a discovery timeout armed by
    # the call under test) and closes the loop, so none leak between tests.
    return asyncio.run(coro)


def test_management_is_admin_only():
    mgr, db, gw, be = make()
    run(mgr.on_console_text(999, 1, "/quit"))    # not the admin
    run(mgr.on_callback(999, "srv:add"))
    assert be.commands == [] and gw.console == [] and gw.menus == []


def test_raw_command_passthrough():
    mgr, db, gw, be = make()
    run(mgr.on_console_text(ADMIN, 1, "/whois someone"))
    assert be.commands == [(CORE_BUFFER, "/whois someone")]


def test_console_channel_command_routes_to_hosting_server():
    # with several servers, a command naming a channel must run on the server
    # that channel is joined on, not core (where it is a silent no-op).
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.upsert_server("hebits")
    db.set_mapping("irc.libera.#electronics", 10, "primary")
    run(mgr.on_console_text(ADMIN, 1, "/part #electronics"))
    assert be.commands == [("irc.server.libera", "/part #electronics")]


def test_console_ambiguous_channel_asks_to_use_the_topic():
    # the same channel joined on two servers cannot be resolved from the console.
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.upsert_server("oftc")
    db.set_mapping("irc.libera.#chat", 10, "primary")
    db.set_mapping("irc.oftc.#chat", 11, "primary")
    run(mgr.on_console_text(ADMIN, 1, "/part #chat"))
    assert be.commands == []                       # not sent to a wrong server
    assert any("#chat" in c for c in gw.console)   # told to use the topic


def test_leave_prompt_close_closes_the_topic():
    mgr, db, gw, be = make()
    db.set_mapping("irc.libera.#electronics", 55, "primary")
    view = run(mgr.on_callback(ADMIN, "chan:close:55"))
    assert getattr(gw, "closed", []) == [55]
    assert view and view[0] == mgr._tr("channel.closed")


def test_leave_prompt_delete_removes_topic_and_mapping():
    mgr, db, gw, be = make()
    db.set_mapping("irc.libera.#electronics", 55, "primary")
    view = run(mgr.on_callback(ADMIN, "chan:delete:55"))
    assert getattr(gw, "deleted_topics", []) == [55]
    assert db.buffer_for_topic(55) is None      # mapping forgotten
    assert view is None                         # message goes with the topic


def test_leave_prompt_keep_leaves_topic_intact():
    mgr, db, gw, be = make()
    db.set_mapping("irc.libera.#electronics", 55, "primary")
    view = run(mgr.on_callback(ADMIN, "chan:keep:55"))
    assert getattr(gw, "closed", []) == [] and getattr(gw, "deleted_topics", []) == []
    assert db.buffer_for_topic(55) == "irc.libera.#electronics"
    assert view and view[0] == mgr._tr("channel.kept")


def test_addserver_flow_end_to_end_with_anon():
    # text steps come via console, choice steps (tls/auth/privacy) via buttons;
    # the flow lives in one message that is edited in place.
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))           # sends the one flow message
    assert mgr._flow_msg_id is not None
    run(mgr.on_console_text(ADMIN, 1, "libera"))
    run(mgr.on_console_text(ADMIN, 1, "irc.libera.chat"))
    run(mgr.on_console_text(ADMIN, 1, "6697"))
    run(mgr.on_callback(ADMIN, "flow:set:yes"))      # tls (button)
    run(mgr.on_console_text(ADMIN, 1, "mynick"))
    run(mgr.on_callback(ADMIN, "flow:set:none"))     # auth (button), skips password
    run(mgr.on_callback(ADMIN, "flow:set:anon"))     # privacy (button) -> completes
    cmds = [c for _, c in be.commands]
    assert "/server add libera irc.libera.chat/6697 -tls" in cmds
    assert "/connect libera" in cmds
    assert "/set irc.server.libera.proxy tor" in cmds       # anon enforcement ran
    assert '/set irc.ctcp.version ""' in cmds
    srv = db.get_server("libera")
    assert srv["anon"] == 1 and srv["tor"] == 1
    assert mgr._addflow is None and mgr._flow_msg_id is None  # flow finished
    assert any("libera" in title for _, title, _ in gw.edits)  # done shown


def test_addserver_flow_reprompts_on_bad_input():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))
    run(mgr.on_console_text(ADMIN, 1, "libera"))
    run(mgr.on_console_text(ADMIN, 1, "host"))
    run(mgr.on_console_text(ADMIN, 1, "notaport"))   # invalid port
    # did not advance or crash: still on the port step, server not created
    assert mgr._addflow is not None and mgr._addflow.current()[0] == "port"
    assert db.get_server("libera") is None


def test_choice_step_ignores_typed_text():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))
    for v in ["libera", "irc.libera.chat", "6697"]:
        run(mgr.on_console_text(ADMIN, 1, v))
    # now on the tls choice step; typing text must not advance it
    run(mgr.on_console_text(ADMIN, 1, "yes"))
    assert mgr._addflow.current()[0] == "tls"


def test_cancel_button_aborts_flow():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))
    run(mgr.on_callback(ADMIN, "flow:cancel"))
    assert mgr._addflow is None and mgr._flow_msg_id is None
    assert gw.edits  # the flow message was edited to a cancelled state


def test_back_button_returns_a_step():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))
    run(mgr.on_console_text(ADMIN, 1, "libera"))   # now on host step
    assert mgr._addflow.current()[0] == "host"
    run(mgr.on_callback(ADMIN, "flow:back"))       # back to name step
    assert mgr._addflow.current()[0] == "name"


def test_password_message_is_deleted():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "srv:add"))
    run(mgr.on_console_text(ADMIN, 1, "libera"))
    run(mgr.on_console_text(ADMIN, 2, "irc.libera.chat"))
    run(mgr.on_console_text(ADMIN, 3, "6697"))
    run(mgr.on_callback(ADMIN, "flow:set:no"))       # tls (button)
    run(mgr.on_console_text(ADMIN, 4, "me"))         # nick
    run(mgr.on_callback(ADMIN, "flow:set:sasl"))     # auth -> sasl, then password (text)
    run(mgr.on_console_text(ADMIN, 55, "s3cret"))
    assert 55 in gw.deleted            # the password message was scrubbed
    assert 4 not in gw.deleted         # non-secret answers are not deleted


def test_join_action_prompts_with_cancel_and_sets_pending():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:join:libera"))
    assert mgr._pending == ("join", "libera")
    # a real prompt is shown (not the raw key), with a cancel button
    assert text and text != "channels.join_prompt"
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)  # Back


def test_answering_a_prompt_edits_it_in_place_with_a_back_button():
    # A prompt is rendered into the tapped message; answering it should edit
    # that same message (Cancel gone, result + a single Back button), not leave
    # the prompt behind and drop the result as a fresh console message.
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:join:libera", 555))   # tapped message id 555
    run(mgr.on_console_text(ADMIN, 1, "#test"))
    assert gw.edits and gw.edits[-1][0] == 555            # same message edited
    _mid, title, m = gw.edits[-1]
    assert "#test" in title
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)
    assert not any(M.parse_cb(d)[0] == "flow" for _, d in flat)  # Cancel gone
    assert gw.console == []                                # not echoed as console


def test_prompt_without_a_message_id_falls_back_to_console():
    # /cancel-style entry points call on_callback with no message id; the result
    # then has nowhere to be edited and must fall back to a console message.
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:join:libera"))        # no message id
    run(mgr.on_console_text(ADMIN, 1, "#test"))
    assert gw.edits == []
    assert any("#test" in c for c in gw.console)


def test_identify_action_prompts_and_sets_pending():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:identify:libera"))
    assert mgr._pending == ("identify", "libera")
    # a real prompt is shown, not the raw key, with a cancel button
    assert text and text != "nickserv.prompt"
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "settings", "libera") for _, d in flat)  # Back


def test_identify_sends_command_on_server_buffer_and_scrubs_password():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:identify:libera"))
    run(mgr.on_console_text(ADMIN, 77, "hunter2"))
    # IDENTIFY goes to NickServ on the server buffer (not core)
    assert ("irc.server.libera", "/msg NickServ IDENTIFY hunter2") in be.commands
    assert 77 in gw.deleted        # the password message was scrubbed
    assert mgr._pending is None    # pending consumed after one answer


def test_identify_cancel_clears_pending():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:identify:libera"))
    assert mgr._pending == ("identify", "libera")
    run(mgr.on_callback(ADMIN, "flow:cancel"))
    assert mgr._pending is None
    # a later message is not misread as a password to identify with
    run(mgr.on_console_text(ADMIN, 88, "not a password"))
    assert not any("IDENTIFY" in c for _, c in be.commands)
    assert 88 not in gw.deleted


def test_register_action_prompts_and_sets_pending():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:register:libera"))
    assert mgr._pending == ("register", "libera")
    # a real prompt is shown, not the raw key, with a cancel button
    assert text and text != "nickserv.register_prompt"
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "settings", "libera") for _, d in flat)  # Back


def test_register_sends_command_without_email_and_scrubs_password():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:register:libera"))
    run(mgr.on_console_text(ADMIN, 77, "hunter2"))
    # REGISTER goes to NickServ on the server buffer (not core), no email token
    assert ("irc.server.libera", "/msg NickServ REGISTER hunter2") in be.commands
    assert 77 in gw.deleted        # the password message was scrubbed
    assert mgr._pending is None    # pending consumed after one answer


def test_register_sends_command_with_email():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:register:libera"))
    run(mgr.on_console_text(ADMIN, 78, "hunter2 alice@example.org"))
    assert ("irc.server.libera",
            "/msg NickServ REGISTER hunter2 alice@example.org") in be.commands
    assert 78 in gw.deleted
    assert mgr._pending is None


def test_register_cancel_clears_pending():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:register:libera"))
    assert mgr._pending == ("register", "libera")
    run(mgr.on_callback(ADMIN, "flow:cancel"))
    assert mgr._pending is None
    # a later message is not misread as a password to register with
    run(mgr.on_console_text(ADMIN, 88, "not a password"))
    assert not any("REGISTER" in c for _, c in be.commands)
    assert 88 not in gw.deleted


def test_perform_action_opens_management_view():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_perform("libera", "/msg NickServ IDENTIFY pw\n/msg HeBoT !invite KEY")
    text, m = run(mgr.on_callback(ADMIN, "srv:perform:libera"))
    assert mgr._pending is None                 # a view, not a prompt
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "permadd", "libera") for _, d in flat)
    assert any(M.parse_cb(d) == ("srv", "permdel", "libera.0") for _, d in flat)
    assert any(M.parse_cb(d) == ("srv", "permdel", "libera.1") for _, d in flat)
    assert any(M.parse_cb(d) == ("srv", "settings", "libera") for _, d in flat)  # Back


def test_perform_view_lists_commands_in_full():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_perform("libera", "/msg NickServ IDENTIFY secret")
    text, _m = run(mgr.on_callback(ADMIN, "srv:perform:libera"))
    # shown in full (admin-only console): the whole command, secret included
    assert "/msg NickServ IDENTIFY secret" in text
    # an empty server lists no commands
    db.upsert_server("oftc")
    text2, _m2 = run(mgr.on_callback(ADMIN, "srv:perform:oftc"))
    assert "1." not in text2


def test_permadd_appends_a_command():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_perform("libera", "/msg NickServ IDENTIFY pw")
    run(mgr.on_callback(ADMIN, "srv:permadd:libera"))
    assert mgr._pending == ("permadd", "libera")
    run(mgr.on_console_text(ADMIN, 33, "/msg InviteBot !invite KEY"))
    assert db.get_perform("libera") == "/msg NickServ IDENTIFY pw\n/msg InviteBot !invite KEY"
    assert mgr._pending is None
    assert 33 not in gw.deleted                 # a perform command is not scrubbed


def test_permdel_removes_one_command_by_index():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_perform("libera", "/msg a\n/msg b\n/msg c")
    run(mgr.on_callback(ADMIN, "srv:permdel:libera.1"))   # remove the middle
    assert db.get_perform("libera") == "/msg a\n/msg c"


def test_permadd_back_clears_pending():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:permadd:libera"))
    assert mgr._pending == ("permadd", "libera")
    # tapping the Back button (any non-flow callback) drops the pending prompt
    run(mgr.on_callback(ADMIN, "srv:perform:libera"))
    assert mgr._pending is None
    run(mgr.on_console_text(ADMIN, 44, "just chatting"))
    assert db.get_perform("libera") == ""


def test_autojoin_toggle_flips_db_and_returns_view():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    assert db.get_server("libera")["autojoin"] == 1
    title, m = run(mgr.on_callback(ADMIN, "srv:autojoin:libera"))
    assert db.get_server("libera")["autojoin"] == 0     # flipped off
    assert "Server settings" in title and m is not None   # settings submenu re-rendered
    run(mgr.on_callback(ADMIN, "srv:autojoin:libera"))
    assert db.get_server("libera")["autojoin"] == 1     # flipped back on


def test_noise_toggle_flips_token_without_clobbering_other_fields():
    mgr, db, gw, be = make()
    db.upsert_server("libera", anon=True, tor=True,
                     auth_method="sasl", caps="sasl", noise_filter="join,part,quit")
    db.set_autojoin("libera", False)
    db.set_perform("libera", "/msg X hi")
    db.set_server_status("libera", "connected")
    # showing joins removes "join" from the muted set
    title, m = run(mgr.on_callback(ADMIN, "srv:noisejoin:libera"))
    srv = db.get_server("libera")
    assert set(srv["noise_filter"].split(",")) == {"part", "quit"}
    assert "Server settings" in title and m is not None   # settings submenu re-rendered
    # the unrelated columns survived the write-back
    assert srv["anon"] == 1 and srv["tor"] == 1
    assert srv["auth_method"] == "sasl" and srv["caps"] == "sasl"
    assert srv["autojoin"] == 0
    assert srv["perform"] == "/msg X hi"
    assert srv["status"] == "connected"
    # toggling the same kind again mutes it back (order-independent set)
    run(mgr.on_callback(ADMIN, "srv:noisejoin:libera"))
    assert set(db.get_server("libera")["noise_filter"].split(",")) == \
        {"join", "part", "quit"}


def test_away_toggle_sets_then_clears_and_flips_state():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    # first tap: set away with the default message, on the server buffer
    title, m = run(mgr.on_callback(ADMIN, "srv:away:libera"))
    assert mgr._away["libera"] is True
    last = be.commands[-1]
    assert last[0] == "irc.server.libera"
    assert last[1].startswith("/away ") and len(last[1]) > len("/away ")
    # the re-rendered view now offers to clear away
    away_label = {M.parse_cb(d): label for row in m for label, d in row}[
        ("srv", "away", "libera")]
    assert away_label == "Clear away"
    # second tap: clear away with a bare /away
    be.commands.clear()
    title, m = run(mgr.on_callback(ADMIN, "srv:away:libera"))
    assert mgr._away["libera"] is False
    assert be.commands[-1] == ("irc.server.libera", "/away")
    label = {M.parse_cb(d): label for row in m for label, d in row}[
        ("srv", "away", "libera")]
    assert label == "Set away"


def test_nick_action_prompts_and_sets_pending():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:nick:libera"))
    assert mgr._pending == ("nick", "libera")
    assert text and text != "nick.prompt"
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)  # Back


def test_nick_flow_sends_nick_for_valid_value():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:nick:libera"))
    run(mgr.on_console_text(ADMIN, 9, "newnick"))
    assert ("irc.server.libera", "/nick newnick") in be.commands
    assert mgr._pending is None       # consumed after one valid answer
    assert gw.console                 # a confirmation was shown


def test_nick_flow_reprompts_and_does_not_send_for_invalid_value():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:nick:libera"))
    run(mgr.on_console_text(ADMIN, 9, "bad nick"))   # has a space -> invalid
    assert not any(c.startswith("/nick") for _, c in be.commands)
    assert mgr._pending == ("nick", "libera")   # still armed for a retry
    # a retry with a valid nick now goes through on the server buffer
    run(mgr.on_console_text(ADMIN, 10, "goodnick"))
    assert ("irc.server.libera", "/nick goodnick") in be.commands
    assert mgr._pending is None


def test_nick_flow_rejects_non_ascii():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:nick:libera"))
    run(mgr.on_console_text(ADMIN, 9, "שלום"))  # Hebrew nick
    assert not any(c.startswith("/nick") for _, c in be.commands)
    assert mgr._pending == ("nick", "libera")


def test_motd_and_info_send_on_server_buffer():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    assert run(mgr.on_callback(ADMIN, "srv:motd:libera")) is None
    assert run(mgr.on_callback(ADMIN, "srv:info:libera")) is None
    assert ("irc.server.libera", "/motd") in be.commands
    assert ("irc.server.libera", "/version") in be.commands


def test_reconnect_all_leaves_connected_server_alone():
    mgr, db, gw, be = make()
    db.upsert_server("up"); db.set_server_status("up", "connected")
    db.upsert_server("down"); db.set_server_status("down", "disconnected")
    run(mgr.on_callback(ADMIN, "srv:reconnect_all"))
    assert db.get_server("up")["status"] == "connected"       # untouched
    assert db.get_server("down")["status"] == "connecting"    # brought up
    assert (CORE_BUFFER, "/reconnect -all") in be.commands


def test_pending_cleared_when_navigating_away():
    # opening a prompt then tapping another button (not Cancel) must drop the
    # pending, so the next typed message is not hijacked as the prompt's answer.
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:identify:libera"))
    assert mgr._pending == ("identify", "libera")
    run(mgr.on_callback(ADMIN, "nav:servers"))     # walked away via a button
    assert mgr._pending is None
    run(mgr.on_console_text(ADMIN, 5, "irc.libera.chat"))
    assert not any("IDENTIFY" in c for _, c in be.commands)
    assert 5 not in gw.deleted


def test_reconnect_and_remove_callbacks():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:reconnect:libera"))
    # Remove is confirmed first: the tap shows a confirmation, deletes nothing.
    run(mgr.on_callback(ADMIN, "srv:remove:libera"))
    assert db.get_server("libera") is not None
    assert "/server del libera" not in [c for _, c in be.commands]
    # Confirming (remove2) actually deletes.
    run(mgr.on_callback(ADMIN, "srv:remove2:libera"))
    cmds = [c for _, c in be.commands]
    assert "/connect libera" in cmds
    assert "/server del libera" in cmds
    assert db.get_server("libera") is None


def test_remove_confirmation_no_keeps_server():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:remove:libera"))
    # the confirm view offers a No that returns to the server view, not deletion
    flat = [d for row in m for _, d in row]
    assert "srv:view:libera" in flat
    assert db.get_server("libera") is not None       # nothing deleted


def test_toggle_tor_enable_then_disable():
    mgr, db, gw, be = make()
    db.upsert_server("libera")   # tor off by default
    assert not db.get_server("libera")["tor"]
    run(mgr.on_callback(ADMIN, "srv:tor:libera"))   # enable
    cmds = [c for buf, c in be.commands if buf == CORE_BUFFER]
    assert "/proxy add tor socks5 127.0.0.1 9050" in cmds
    assert "/set irc.server.libera.proxy tor" in cmds
    assert "/reconnect libera" in cmds
    assert db.get_server("libera")["tor"] == 1        # flag persisted
    be.commands.clear()
    run(mgr.on_callback(ADMIN, "srv:tor:libera"))   # disable
    cmds = [c for buf, c in be.commands if buf == CORE_BUFFER]
    assert "/unset irc.server.libera.proxy" in cmds   # proxy removed, not added
    assert "/proxy add tor socks5 127.0.0.1 9050" not in cmds
    assert "/reconnect libera" in cmds
    assert db.get_server("libera")["tor"] == 0        # flag flipped back


class FakeConnectRouter:
    def __init__(self):
        self.armed = []

    def mark_discover(self, server):
        pass

    def arm_connect_timeout(self, server):
        self.armed.append(server)


def test_toggle_tor_sets_connecting_and_arms_timeout():
    # a tor reconnect can silently fail (Tor down / SOCKS refused); it must mark
    # the server connecting and arm the timeout so the badge resolves, matching
    # the explicit reconnect action.
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeConnectRouter()
    mgr = Manager(db, FakeBackend(), FakeGW(), tr, router=fr, admin_id=ADMIN)
    db.upsert_server("libera")
    db.set_server_status("libera", "connected")
    run(mgr.on_callback(ADMIN, "srv:tor:libera"))
    assert db.get_server("libera")["status"] == "connecting"
    assert fr.armed == ["libera"]


def test_completed_flow_and_reconnect_arm_connect_timeout():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeConnectRouter()
    mgr = Manager(db, FakeBackend(), FakeGW(), tr, router=fr, admin_id=ADMIN)
    run(mgr.on_callback(ADMIN, "srv:add"))
    run(mgr.on_console_text(ADMIN, 1, "libera"))
    run(mgr.on_console_text(ADMIN, 1, "irc.example.org"))
    run(mgr.on_console_text(ADMIN, 1, "6697"))
    run(mgr.on_callback(ADMIN, "flow:set:yes"))    # tls
    run(mgr.on_console_text(ADMIN, 1, "mynick"))
    run(mgr.on_callback(ADMIN, "flow:set:none"))   # auth none
    run(mgr.on_callback(ADMIN, "flow:set:off"))    # privacy off -> completes
    assert fr.armed == ["libera"]                  # add-server arms the timeout
    run(mgr.on_callback(ADMIN, "srv:reconnect:libera"))
    assert fr.armed == ["libera", "libera"]        # a reconnect arms it again


def _complete_addflow(mgr, name="libera", nick="mynick"):
    run(mgr.on_callback(ADMIN, "srv:add"))
    flow_id = mgr._flow_msg_id
    run(mgr.on_console_text(ADMIN, 1, name))
    run(mgr.on_console_text(ADMIN, 1, "irc.example.org"))
    run(mgr.on_console_text(ADMIN, 1, "6697"))
    run(mgr.on_callback(ADMIN, "flow:set:yes"))    # tls
    run(mgr.on_console_text(ADMIN, 1, nick))
    run(mgr.on_callback(ADMIN, "flow:set:none"))   # auth none (skips password)
    run(mgr.on_callback(ADMIN, "flow:set:off"))    # privacy off -> completes
    return flow_id


def test_completed_flow_sets_connecting_and_records_message():
    mgr, db, gw, be = make()
    flow_id = _complete_addflow(mgr, name="libera", nick="mynick")
    assert db.get_server("libera")["status"] == "connecting"
    assert mgr._connect_msg["libera"] == flow_id
    # the flow message was edited to the connecting text, keeping its id
    assert any(mid == flow_id and "libera" in title and "mynick" in title
               for mid, title, _ in gw.edits)


def test_on_server_status_connected_edits_recorded_message():
    mgr, db, gw, be = make()
    flow_id = _complete_addflow(mgr, name="libera", nick="mynick")
    gw.edits.clear()
    run(mgr.on_server_status("libera", "connected"))
    assert any(mid == flow_id and "mynick" in title for mid, title, _ in gw.edits)
    assert "libera" not in mgr._connect_msg   # consumed after reporting


def test_on_server_status_failed_edits_recorded_message():
    mgr, db, gw, be = make()
    flow_id = _complete_addflow(mgr, name="libera", nick="mynick")
    gw.edits.clear()
    run(mgr.on_server_status("libera", "failed"))
    assert any(mid == flow_id and "libera" in title for mid, title, _ in gw.edits)
    assert "libera" not in mgr._connect_msg


def test_on_server_status_unknown_server_is_a_noop():
    mgr, db, gw, be = make()
    run(mgr.on_server_status("nope", "connected"))
    assert gw.edits == []


def test_server_action_status_transitions():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.upsert_server("oftc")
    run(mgr.on_callback(ADMIN, "srv:reconnect:libera"))
    assert db.get_server("libera")["status"] == "connecting"
    run(mgr.on_callback(ADMIN, "srv:disconnect:libera"))
    assert db.get_server("libera")["status"] == "disconnected"
    run(mgr.on_callback(ADMIN, "srv:reconnect_all"))
    assert db.get_server("libera")["status"] == "connecting"
    assert db.get_server("oftc")["status"] == "connecting"


def test_channels_action_builds_list_from_db():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.server.libera", 1, "primary")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    db.set_mapping("irc.libera.alice", 3, "primary")   # PM, excluded
    title, m = run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    # the ordered list is stored for index-based leaving, tagged by generation
    assert mgr._chan_list == {
        "gen": gen,
        "server": "libera",
        "channels": [{"buffer": "irc.libera.#weechat", "topic_id": 2}]}
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "actions", f"{gen}.0") for _, d in flat)
    # PM buffer is not offered as a channel
    assert not any("alice" in label for label, _ in flat)


def test_leavech_sends_part_on_server_buffer_and_rerenders():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    db.set_mapping("irc.libera.#python", 3, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))   # populates _chan_list
    gen = mgr._chan_list["gen"]
    title, m = run(mgr.on_callback(ADMIN, f"srv:leavech:{gen}.0"))
    # /part on the correct channel, sent on the server buffer (not core)
    assert ("irc.server.libera", "/part #python") in be.commands
    # index 0 is the first ordered channel (#python sorts before #weechat)
    assert be.commands[-1] == ("irc.server.libera", "/part #python")
    # a view is returned so the console can re-render the remaining channels
    assert m is not None


def test_leavech_drops_left_channel_from_rerendered_list():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    db.set_mapping("irc.libera.#python", 3, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, f"srv:leavech:{gen}.0"))   # leave #python (index 0)
    # the parted channel is gone from both the joined list and the re-render,
    # so its Leave button does not reappear and autojoin will not rejoin it
    assert db.list_channels("libera") == [{"buffer": "irc.libera.#weechat", "topic_id": 2}]
    assert [c["buffer"] for c in mgr._chan_list["channels"]] == ["irc.libera.#weechat"]


def test_leavech_out_of_range_index_is_safe():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    before = list(be.commands)
    view = run(mgr.on_callback(ADMIN, f"srv:leavech:{gen}.9"))
    assert be.commands == before   # no /part for a nonexistent index
    assert view is not None


def test_discover_action_requests_list_and_sends_fetching():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    result = run(mgr.on_callback(ADMIN, "srv:discover:libera"))
    # /list is asked for on the server buffer, not core
    assert ("irc.server.libera", "/list") in be.commands
    # the fetching notice is sent as its own message (so its id can be tracked
    # for the timeout), not returned as a view that edits the tapped message
    assert result is None
    assert gw.menus
    title, _m = gw.menus[-1]
    assert title and title != "channels.fetching"       # real translation
    # its id is remembered so a timeout can edit that same message
    assert mgr._discover_msg.get("libera") is not None


def test_discover_flags_router_when_present():
    class FakeRouter:
        def __init__(self):
            self.discovered = []

        def mark_discover(self, server):
            self.discovered.append(server)

    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeRouter()
    mgr = Manager(db, FakeBackend(), FakeGW(), tr, router=fr, admin_id=ADMIN)
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:discover:libera"))
    assert fr.discovered == ["libera"]


def test_discinfo_shows_detail_then_discback_returns_to_list():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    chans = [{"channel": "#python", "users": 40, "topic": "Py chat"},
             {"channel": "#weechat", "users": 5, "topic": ""}]
    run(mgr.on_channel_list("libera", chans))   # populates _discovered + posts picker
    gen = mgr._discovered["gen"]
    # tapping a channel opens its detail, it does NOT join
    title, m = run(mgr.on_callback(ADMIN, f"srv:discinfo:{gen}.0"))
    assert "#python" in title and "40" in title and "Py chat" in title
    assert be.commands == []
    cbs = {M.parse_cb(d) for row in m for _, d in row}
    assert ("srv", "joinidx", f"{gen}.0") in cbs   # Join actually joins
    assert ("srv", "discback", "") in cbs          # Back to the list
    # Back re-renders the discovered list
    _t2, m2 = run(mgr.on_callback(ADMIN, "srv:discback"))
    cbs2 = {M.parse_cb(d) for row in m2 for _, d in row}
    assert ("srv", "discinfo", f"{gen}.0") in cbs2
    assert ("srv", "discinfo", f"{gen}.1") in cbs2


def test_discovery_marks_channels_already_joined():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#in", 5, "primary")   # we are in #in, not #out
    run(mgr.on_channel_list("libera", [{"channel": "#in", "users": 3, "topic": ""},
                                       {"channel": "#out", "users": 8, "topic": ""}]))
    gen = mgr._discovered["gen"]
    _title, m = gw.menus[-1]
    by_cb = {M.parse_cb(d): label for row in m for label, d in row}
    assert by_cb[("srv", "discinfo", f"{gen}.0")].startswith("✓ #in")
    assert "✓" not in by_cb[("srv", "discinfo", f"{gen}.1")]


class FakeDiscoverRouter:
    def __init__(self):
        self.discovered = []
        self.cleared = []

    def mark_discover(self, server):
        self.discovered.append(server)

    def clear_discover(self, server):
        self.cleared.append(server)


def _discover_mgr():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeDiscoverRouter()
    gw = FakeGW()
    mgr = Manager(db, FakeBackend(), gw, tr, router=fr, admin_id=ADMIN)
    db.upsert_server("libera")
    return mgr, db, gw, fr, tr


def test_discovery_timeout_reports_none_and_clears_pending():
    # A /list that never replies must not leave the fetching message hanging:
    # after the (tiny) timeout it becomes the "no channels" notice and the
    # router's discovery pending is cleared so a late line is ignored.
    mgr, db, gw, fr, tr = _discover_mgr()
    mgr._discover_timeout = 0.0
    tracked = {}

    async def scenario():
        await mgr.on_callback(ADMIN, "srv:discover:libera")
        tracked["id"] = mgr._discover_msg["libera"]   # capture before the timer pops it
        await asyncio.sleep(0.02)                      # let the armed timer fire

    run(scenario())
    none_text = tr.t("channels.none", "en")
    # the very message that showed "fetching" was edited to the "no channels" text
    assert (tracked["id"], none_text, None) in gw.edits
    assert fr.cleared == ["libera"]                # discovery pending cleared
    assert "libera" not in mgr._discover_msg       # forgotten after reporting
    assert "libera" not in mgr._discover_tasks


def test_discovery_completion_cancels_timeout_and_shows_picker():
    # If the list arrives before the timeout, the timer is cancelled, no
    # "no channels" notice is shown, and the picker is posted as today.
    mgr, db, gw, fr, tr = _discover_mgr()
    mgr._discover_timeout = 5.0   # long enough that only cancellation stops it

    async def scenario():
        await mgr.on_callback(ADMIN, "srv:discover:libera")
        assert "libera" in mgr._discover_tasks     # timer is armed
        await mgr.on_channel_list(
            "libera", [{"channel": "#py", "users": 3, "topic": ""}])

    run(scenario())
    none_text = tr.t("channels.none", "en")
    assert not any(title == none_text for _, title, _ in gw.edits)  # never reported
    assert fr.cleared == []                        # completion path, not timeout
    assert "libera" not in mgr._discover_tasks     # timer cancelled and forgotten
    assert "libera" not in mgr._discover_msg
    assert mgr._discovered["channels"][0]["channel"] == "#py"       # picker built


def test_discovery_completion_reuses_fetching_message():
    # The list arriving must edit the very "fetching" message into the picker,
    # never post a second message and orphan the placeholder.
    mgr, db, gw, fr, tr = _discover_mgr()
    mgr._discover_timeout = 5.0
    captured = {}

    async def scenario():
        await mgr.on_callback(ADMIN, "srv:discover:libera")
        captured["id"] = mgr._discover_msg["libera"]
        await mgr.on_channel_list(
            "libera", [{"channel": "#py", "users": 3, "topic": ""}])

    run(scenario())
    fetch_id = captured["id"]
    assert len(gw.menus) == 1                       # only the fetching notice
    discover_title = tr.t("menu.discover", "en")
    picker_edits = [(mid, title, m) for mid, title, m in gw.edits
                    if mid == fetch_id and title == discover_title]
    assert len(picker_edits) == 1                   # reused via edit, not resent
    assert picker_edits[0][2] is not None           # the picker keyboard
    assert "libera" not in mgr._discover_msg        # id consumed
    assert "libera" not in mgr._discover_tasks       # timer cancelled


def test_discovery_completion_empty_edits_fetching_message():
    # An empty list from the Discover button edits the fetching notice into the
    # "no channels" message rather than leaving the placeholder dangling.
    mgr, db, gw, fr, tr = _discover_mgr()
    mgr._discover_timeout = 5.0
    captured = {}

    async def scenario():
        await mgr.on_callback(ADMIN, "srv:discover:libera")
        captured["id"] = mgr._discover_msg["libera"]
        await mgr.on_channel_list("libera", [])

    run(scenario())
    none_text = tr.t("discover.none", "en")
    assert (captured["id"], none_text, None) in gw.edits
    assert gw.console == []                          # not orphaned via a fresh notice
    assert len(gw.menus) == 1                        # only the fetching notice


def test_close_cancels_pending_discovery_timer():
    # Teardown must cancel outstanding timers so none fires afterwards.
    mgr, db, gw, fr, tr = _discover_mgr()
    mgr._discover_timeout = 5.0

    async def scenario():
        await mgr.on_callback(ADMIN, "srv:discover:libera")
        assert "libera" in mgr._discover_tasks
        mgr.close()

    run(scenario())
    assert "libera" not in mgr._discover_tasks
    # the timer never fired, so no "no channels" notice was written
    none_text = tr.t("channels.none", "en")
    assert not any(title == none_text for _, title, _ in gw.edits)


def test_on_channel_list_stores_and_builds_picker():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    channels = [{"channel": "#python", "users": 4213, "topic": "Py"},
                {"channel": "#weechat", "users": 120, "topic": "chat"}]
    run(mgr.on_channel_list("libera", channels))
    gen = mgr._discovered["gen"]
    assert mgr._discovered == {"gen": gen, "server": "libera", "channels": channels}
    # one picker message, one button per channel, addressed by generation.index
    assert len(gw.menus) == 1
    _title, m = gw.menus[0]
    flat = [b for row in m for b in row]
    by_cb = {M.parse_cb(d): label for label, d in flat}
    assert ("srv", "discinfo", f"{gen}.0") in by_cb   # tap opens detail, not join
    assert ("srv", "discinfo", f"{gen}.1") in by_cb
    assert "#python" in by_cb[("srv", "discinfo", f"{gen}.0")]
    assert "(4213)" in by_cb[("srv", "discinfo", f"{gen}.0")]
    # channel names never leak into callback_data (index only)
    assert all("#" not in d for _, d in flat)
    # the picker returns to the server view the discovery was launched from
    assert ("srv", "view", "libera") in by_cb


def test_on_channel_list_empty_reports_none_without_picker():
    mgr, db, gw, be = make()
    run(mgr.on_channel_list("libera", []))
    assert gw.menus == []       # no picker for an empty result
    assert gw.console           # a "no channels" notice instead


def test_joinidx_sends_join_on_server_buffer():
    mgr, db, gw, be = make()
    channels = [{"channel": "#python", "users": 4213, "topic": ""},
                {"channel": "#weechat", "users": 120, "topic": ""}]
    run(mgr.on_channel_list("libera", channels))
    gen = mgr._discovered["gen"]
    run(mgr.on_callback(ADMIN, f"srv:joinidx:{gen}.1"))
    # /join on the channel at that index, on the server buffer (not core)
    assert be.commands[-1] == ("irc.server.libera", "/join #weechat")
    assert any("#weechat" in c for c in gw.console)   # confirmation shown


def test_joinidx_out_of_range_is_safe():
    mgr, db, gw, be = make()
    run(mgr.on_channel_list("libera", [{"channel": "#a", "users": 1, "topic": ""}]))
    gen = mgr._discovered["gen"]
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"srv:joinidx:{gen}.9"))
    assert be.commands == before   # no /join for a nonexistent index


def test_stale_picker_tap_is_rejected():
    # discover on one list, then another; a tap carrying the older generation
    # must not resolve against the newest stored list.
    mgr, db, gw, be = make()
    run(mgr.on_channel_list("liba", [{"channel": "#old", "users": 5, "topic": ""}]))
    stale_gen = mgr._discovered["gen"]
    run(mgr.on_channel_list("libb", [{"channel": "#new", "users": 9, "topic": ""}]))
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"srv:joinidx:{stale_gen}.0"))
    assert be.commands == before   # stale generation: no /join fired
    # a tap with the current generation still works
    fresh_gen = mgr._discovered["gen"]
    run(mgr.on_callback(ADMIN, f"srv:joinidx:{fresh_gen}.0"))
    assert be.commands[-1] == ("irc.server.libb", "/join #new")


def test_stale_leave_tap_is_rejected():
    mgr, db, gw, be = make()
    db.upsert_server("liba")
    db.set_mapping("irc.liba.#one", 2, "primary")
    db.upsert_server("libb")
    db.set_mapping("irc.libb.#two", 3, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:liba"))
    stale_gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, "srv:channels:libb"))   # overwrites _chan_list
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"srv:leavech:{stale_gen}.0"))
    assert be.commands == before   # stale generation: no /part fired


def test_channel_actions_opens_panel():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))   # populates _chan_list
    gen = mgr._chan_list["gen"]
    title, m = run(mgr.on_callback(ADMIN, f"srv:actions:{gen}.0"))
    assert title == "#weechat"
    flat = [b for row in m for b in row]
    cbs = {M.parse_cb(d) for _, d in flat}
    assert ("srv", "names", f"{gen}.0") in cbs
    assert ("srv", "who", f"{gen}.0") in cbs
    assert ("srv", "topic", f"{gen}.0") in cbs
    assert ("srv", "leaveconfirm", f"{gen}.0") in cbs   # leave asks first
    assert ("srv", "channels", "libera") in cbs   # back to channels view


def test_leaveconfirm_asks_before_parting():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    title, m = run(mgr.on_callback(ADMIN, f"srv:leaveconfirm:{gen}.0"))
    # a confirmation, not an immediate part
    assert "#weechat" in title
    assert be.commands == []                     # nothing parted yet
    cbs = {M.parse_cb(d) for row in m for _, d in row}
    assert ("srv", "leavech", f"{gen}.0") in cbs   # Yes parts
    assert ("srv", "actions", f"{gen}.0") in cbs   # No returns to the panel


def test_server_settings_title_names_the_server():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_server_status("libera", "connected")
    title, m = run(mgr.on_callback(ADMIN, "srv:settings:libera"))
    assert "libera" in title and "Server settings" in title   # says which server
    assert m is not None                                       # settings menu shown


def test_channel_panel_topic_and_who_send_on_server_buffer():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, f"srv:topic:{gen}.0"))
    run(mgr.on_callback(ADMIN, f"srv:who:{gen}.0"))
    assert ("irc.server.libera", "/topic #weechat") in be.commands
    assert ("irc.server.libera", "/who #weechat") in be.commands


def test_channel_panel_names_arms_router_and_sends_names():
    class FakeNamesRouter:
        def __init__(self):
            self.named = []

        def mark_names(self, server):
            self.named.append(server)

    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeNamesRouter()
    be = FakeBackend()
    mgr = Manager(db, be, FakeGW(), tr, router=fr, admin_id=ADMIN)
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 2, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, f"srv:names:{gen}.0"))
    # arms the router to collect the reply, and asks for the channel's names
    assert fr.named == ["libera"]
    assert ("irc.server.libera", "/names #weechat") in be.commands


def test_channel_panel_topic_and_who_route_reply_to_channel_topic():
    # Topic/Who fire from a button and never pass through handle_telegram, so the
    # manager must point the router at the channel's own topic; otherwise the
    # 33x/35x reply numerics fall to the server status topic. Names is exempt:
    # it is collected and posted as a picker, not routed to a topic.
    class FakeReplyRouter:
        def __init__(self):
            self.expected = []
            self.named = []

        def mark_names(self, server):
            self.named.append(server)

        def expect_reply_in(self, server, topic_id):
            self.expected.append((server, topic_id))

    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    fr = FakeReplyRouter()
    be = FakeBackend()
    mgr = Manager(db, be, FakeGW(), tr, router=fr, admin_id=ADMIN)
    db.upsert_server("libera")
    db.set_mapping("irc.libera.#weechat", 42, "primary")   # channel topic id 42
    run(mgr.on_callback(ADMIN, "srv:channels:libera"))
    gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, f"srv:topic:{gen}.0"))
    run(mgr.on_callback(ADMIN, f"srv:who:{gen}.0"))
    # both replies are pointed at the channel's topic, not the server topic
    assert fr.expected == [("libera", 42), ("libera", 42)]
    # names does not route a reply (it posts a picker instead)
    run(mgr.on_callback(ADMIN, f"srv:names:{gen}.0"))
    assert fr.expected == [("libera", 42), ("libera", 42)]
    assert fr.named == ["libera"]


def test_channel_panel_stale_generation_rejected():
    mgr, db, gw, be = make()
    db.upsert_server("liba")
    db.set_mapping("irc.liba.#one", 2, "primary")
    db.upsert_server("libb")
    db.set_mapping("irc.libb.#two", 3, "primary")
    run(mgr.on_callback(ADMIN, "srv:channels:liba"))
    stale_gen = mgr._chan_list["gen"]
    run(mgr.on_callback(ADMIN, "srv:channels:libb"))   # overwrites _chan_list
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"srv:who:{stale_gen}.0"))
    assert be.commands == before   # stale generation: no command fired


def test_on_names_stores_and_builds_user_picker():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    users = [{"prefix": "@", "nick": "alice"}, {"prefix": "", "nick": "bob"}]
    run(mgr.on_names("libera", "#weechat", users))
    gen = mgr._names_list["gen"]
    assert mgr._names_list == {
        "gen": gen, "server": "libera", "channel": "#weechat", "users": users}
    assert len(gw.menus) == 1
    _title, m = gw.menus[0]
    flat = [b for row in m for b in row]
    by_cb = {M.parse_cb(d): label for label, d in flat}
    assert ("usr", "pick", f"{gen}.0") in by_cb
    assert ("usr", "pick", f"{gen}.1") in by_cb
    assert by_cb[("usr", "pick", f"{gen}.0")] == "@alice"
    # nicks never leak into callback_data (index only)
    assert all("alice" not in d and "bob" not in d for _, d in flat)
    # the picker returns to the server's channels list
    assert ("srv", "channels", "libera") in by_cb


def test_on_names_empty_reports_none_without_picker():
    mgr, db, gw, be = make()
    run(mgr.on_names("libera", "#empty", []))
    assert gw.menus == []       # no picker for an empty membership
    assert gw.console           # a "no users" notice instead


def test_user_pick_opens_actions_menu():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    run(mgr.on_names("libera", "#weechat", [{"prefix": "@", "nick": "alice"}]))
    gen = mgr._names_list["gen"]
    title, m = run(mgr.on_callback(ADMIN, f"usr:pick:{gen}.0"))
    assert title == "@alice"
    flat = [b for row in m for b in row]
    cbs = {M.parse_cb(d)[:2] for _, d in flat}
    for act in ("whois", "op", "deop", "voice", "devoice", "kick", "ban"):
        assert ("usr", act) in cbs, act


def test_user_actions_send_correct_commands_on_server_buffer():
    mgr, db, gw, be = make()
    run(mgr.on_names("libera", "#weechat", [{"prefix": "", "nick": "bob"}]))
    gen = mgr._names_list["gen"]
    cases = {
        "whois": "/whois bob",
        "op": "/mode #weechat +o bob",
        "deop": "/mode #weechat -o bob",
        "voice": "/mode #weechat +v bob",
        "devoice": "/mode #weechat -v bob",
        "kick": "/kick #weechat bob",
        "ban": "/mode #weechat +b bob",
    }
    for action, cmd in cases.items():
        be.commands.clear()
        run(mgr.on_callback(ADMIN, f"usr:{action}:{gen}.0"))
        assert be.commands == [("irc.server.libera", cmd)], action


def test_user_pickback_returns_names_picker():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    users = [{"prefix": "@", "nick": "alice"}, {"prefix": "", "nick": "bob"}]
    run(mgr.on_names("libera", "#weechat", users))
    gen = mgr._names_list["gen"]
    _title, m = run(mgr.on_callback(ADMIN, f"usr:pickback:{gen}.0"))
    flat = [b for row in m for b in row]
    by_cb = {M.parse_cb(d): label for label, d in flat}
    assert ("usr", "pick", f"{gen}.0") in by_cb   # back to the picker
    # the restored picker still carries its own Back to the channels list
    assert ("srv", "channels", "libera") in by_cb


def test_user_action_stale_generation_rejected():
    mgr, db, gw, be = make()
    run(mgr.on_names("liba", "#a", [{"prefix": "", "nick": "one"}]))
    stale_gen = mgr._names_list["gen"]
    run(mgr.on_names("libb", "#b", [{"prefix": "", "nick": "two"}]))
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"usr:kick:{stale_gen}.0"))
    assert be.commands == before   # stale generation: no command fired
    fresh_gen = mgr._names_list["gen"]
    run(mgr.on_callback(ADMIN, f"usr:kick:{fresh_gen}.0"))
    assert be.commands[-1] == ("irc.server.libb", "/kick #b two")


def test_user_action_out_of_range_index_is_safe():
    mgr, db, gw, be = make()
    run(mgr.on_names("libera", "#c", [{"prefix": "", "nick": "a"}]))
    gen = mgr._names_list["gen"]
    before = list(be.commands)
    run(mgr.on_callback(ADMIN, f"usr:kick:{gen}.9"))
    assert be.commands == before   # out-of-range index: no command fired


def test_ignores_action_builds_list_from_db():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.add_ignore("libera", "spammer")
    title, m = run(mgr.on_callback(ADMIN, "srv:ignores:libera"))
    gen = mgr._ignore_list["gen"]
    assert mgr._ignore_list == {
        "gen": gen, "server": "libera", "nicks": ["spammer"]}
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "unignore", f"{gen}.0") for _, d in flat)
    # the Add action is offered
    assert any(M.parse_cb(d) == ("srv", "ignoreadd", "libera") for _, d in flat)


def test_ignoreadd_prompts_and_sets_pending():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    text, m = run(mgr.on_callback(ADMIN, "srv:ignoreadd:libera"))
    assert mgr._pending == ("ignore", "libera")
    # a real prompt is shown (not the raw key), with a cancel button
    assert text and text != "ignores.add_prompt"
    flat = [b for row in m for b in row]
    assert any(M.parse_cb(d) == ("srv", "ignores", "libera") for _, d in flat)  # Back


def test_ignore_flow_adds_to_db():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:ignoreadd:libera"))
    run(mgr.on_console_text(ADMIN, 5, "spammer"))
    assert db.is_ignored("libera", "spammer") is True
    assert mgr._pending is None       # pending consumed after one answer
    assert gw.console                 # an added-confirmation was shown


def test_ignore_add_cancel_clears_pending():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    run(mgr.on_callback(ADMIN, "srv:ignoreadd:libera"))
    run(mgr.on_callback(ADMIN, "flow:cancel"))
    assert mgr._pending is None
    # a later message is not misread as a nick to ignore
    run(mgr.on_console_text(ADMIN, 6, "just chatting"))
    assert db.list_ignores("libera") == []


def test_unignore_removes_from_db_and_rerenders():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.add_ignore("libera", "spammer")
    db.add_ignore("libera", "troll")
    run(mgr.on_callback(ADMIN, "srv:ignores:libera"))   # populates _ignore_list
    gen = mgr._ignore_list["gen"]
    title, m = run(mgr.on_callback(ADMIN, f"srv:unignore:{gen}.0"))
    # index 0 is the first ordered nick (spammer sorts before troll)
    assert db.is_ignored("libera", "spammer") is False
    assert db.is_ignored("libera", "troll") is True
    assert m is not None              # the remaining list is re-rendered


def test_unignore_out_of_range_index_is_safe():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    db.add_ignore("libera", "spammer")
    run(mgr.on_callback(ADMIN, "srv:ignores:libera"))
    gen = mgr._ignore_list["gen"]
    view = run(mgr.on_callback(ADMIN, f"srv:unignore:{gen}.9"))
    assert db.is_ignored("libera", "spammer") is True   # nothing removed
    assert view is not None


def test_stale_unignore_tap_is_rejected():
    mgr, db, gw, be = make()
    db.upsert_server("liba")
    db.add_ignore("liba", "one")
    db.upsert_server("libb")
    db.add_ignore("libb", "two")
    run(mgr.on_callback(ADMIN, "srv:ignores:liba"))
    stale_gen = mgr._ignore_list["gen"]
    run(mgr.on_callback(ADMIN, "srv:ignores:libb"))   # overwrites _ignore_list
    run(mgr.on_callback(ADMIN, f"srv:unignore:{stale_gen}.0"))
    assert db.is_ignored("liba", "one") is True        # stale tap removed nothing


def test_help_hub_has_category_buttons_and_back():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    text, m = run(mgr.on_callback(ADMIN, "sys:help"))
    assert m is not None
    # the intro is a real translation, not the raw key
    assert text and text != "help.intro" and "<b>" in text
    assert len(text) < 4096
    flat = [b for row in m for b in row]
    # a button per researched category, each carrying help:cat:<slug>
    cats = {M.parse_cb(d)[2] for _, d in flat if M.parse_cb(d)[:2] == ("help", "cat")}
    assert cats == set(M.HELP_CATEGORIES)
    # the hub still returns to the main console
    assert any(M.parse_cb(d) == ("nav", "main", "") for _, d in flat)
    # labels are translated, never raw keys
    assert not any(label.startswith("help.") or label == "menu.back"
                   for label, _ in flat)


def test_help_category_pages_render_real_translations():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    # a couple of commands we expect to see in each category page
    expected = {
        "channels": ["/join", "/topic"],
        "users": ["/whois", "/who"],
        "modes": ["/mode", "/kick"],
        "messaging": ["/msg", "/notice"],
        "server": ["/quit", "/quote"],
        "info": ["/motd", "/version"],
    }
    for slug in M.HELP_CATEGORIES:
        text, m = run(mgr.on_callback(ADMIN, f"help:cat:{slug}"))
        assert text, f"{slug} page empty"
        # a real translation, not the raw key echoed back
        assert text != f"help.cat.{slug}", f"{slug} returned raw key"
        assert "<code>" in text and len(text) < 4096
        for cmd in expected[slug]:
            assert cmd in text, f"{slug} page missing {cmd}"
        # the category page offers a Back button to the help hub
        flat = [b for row in m for b in row]
        assert any(M.parse_cb(d) == ("sys", "help", "") for _, d in flat)


def test_help_category_pages_translate_in_hebrew():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.set("language", "he")
    for slug in M.HELP_CATEGORIES:
        text, _ = run(mgr.on_callback(ADMIN, f"help:cat:{slug}"))
        assert text != f"help.cat.{slug}"
        # Hebrew text present (at least one Hebrew letter)
        assert any("א" <= ch <= "ת" for ch in text), f"{slug} not Hebrew"


def test_help_unknown_category_ignored():
    mgr, db, gw, be = make()
    assert run(mgr.on_callback(ADMIN, "help:cat:bogus")) is None


def test_nav_servers_returns_menu_view():
    mgr, db, gw, be = make()
    db.upsert_server("libera")
    title, m = run(mgr.on_callback(ADMIN, "nav:servers"))
    assert any("libera" in label for row in m for label, _ in row)


def test_language_change_persists():
    mgr, db, gw, be = make()
    run(mgr.on_callback(ADMIN, "set:lang:he"))
    assert db.get("language") == "he"


def test_language_menu_lists_real_languages():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    title, m = run(mgr.on_callback(ADMIN, "set:language"))
    flat = [b for row in m for b in row]
    # real language options are offered (not characters of a string)
    assert any(M.parse_cb(d) == ("set", "lang", "he") for _, d in flat)
    assert any(M.parse_cb(d) == ("set", "lang", "en") for _, d in flat)


def test_usegroup_saves_group_and_signals():
    # /usegroup in a forum saves the id and fires on_group_set so the entry
    # point can bring up the full bridge live, without a restart.
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    signalled = []
    mgr = Manager(db, FakeBackend(), FakeGW(), tr, router=None, admin_id=ADMIN,
                  on_group_set=signalled.append)
    run(mgr.on_onboard(ADMIN, "usegroup", -100500, "forum"))
    assert db.get_int("group_chat_id") == -100500
    assert signalled == [-100500]


def test_usegroup_rejects_plain_chat_without_signal():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    signalled = []
    mgr = Manager(db, FakeBackend(), FakeGW(), tr, router=None, admin_id=ADMIN,
                  on_group_set=signalled.append)
    run(mgr.on_onboard(ADMIN, "usegroup", 4242, "private"))
    assert db.get_int("group_chat_id") == 0 and signalled == []


def test_menu_command_full_mode_returns_main_menu():
    # /menu (or /console) in full mode (a backend is wired) reopens the main
    # console menu, so the admin can resummon it inside the group.
    from tgbridge import menu as M
    mgr, db, gw, be = make()   # make() wires a real backend -> full mode
    text, m = run(mgr.on_onboard(ADMIN, "menu", -100500, "forum"))
    assert m is not None
    flat = [b for row in m for b in row]
    cbs = {M.parse_cb(d) for _, d in flat}
    assert ("nav", "servers", "") in cbs
    assert ("srv", "add", "") in cbs
    assert ("srv", "reconnect_all", "") in cbs
    assert ("nav", "settings", "") in cbs
    assert ("sys", "help", "") in cbs
    # labels are real translations, never raw keys
    assert not any(label.startswith("menu.") for label, _ in flat)


def test_menu_command_onboarding_returns_start_view():
    # Before the bridge is fully up (no backend), /menu shows the start view,
    # not the console menu, mirroring the onboarding Manager wired in main.py.
    from tgbridge import menu as M
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    tr = Translator(LOCALES)
    mgr = Manager(db, None, FakeGW(), tr, router=None, admin_id=ADMIN)
    text, m = run(mgr.on_onboard(ADMIN, "menu", 4242, "private"))
    assert m is not None
    flat = [b for row in m for b in row]
    # the start view offers direct language buttons (dm:lang), not console nav
    assert any(M.parse_cb(d)[:2] == ("dm", "lang") for _, d in flat)
    assert not any(M.parse_cb(d) == ("nav", "servers", "") for _, d in flat)


def test_menu_command_is_admin_only():
    mgr, db, gw, be = make()
    assert run(mgr.on_onboard(999, "menu", -100500, "forum")) is None


def test_dm_language_sets_and_returns_start_view():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    text, m = run(mgr.on_callback(ADMIN, "dm:lang:he"))
    assert db.get("language") == "he"
    flat = [b for row in m for b in row]
    # the start view offers direct language buttons (dm:lang)
    assert any(M.parse_cb(d) == ("dm", "lang", "en") for _, d in flat)


def test_add_sender_stores_and_scrubs_token():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    # open Sender bots, tap Add: the prompt appears and a pending flow is armed
    title, m = run(mgr.on_callback(ADMIN, M.cb("set", "senderadd")))
    assert mgr._pending == ("addsender", "")
    # the admin sends a token; it is stored under its id prefix and scrubbed
    run(mgr.on_console_text(ADMIN, 4242, "123456:AAgoodtoken"))
    senders = db.list_senders()
    assert len(senders) == 1
    assert senders[0]["bot_id"] == "123456"
    assert senders[0]["token"] == "123456:AAgoodtoken"
    assert 4242 in gw.deleted           # the token message was deleted
    assert mgr._pending is None
    # started right away, so it carries sends without a restart
    assert getattr(gw, "started_senders", []) == [
        {"bot_id": "123456", "token": "123456:AAgoodtoken"}]


def test_add_sender_rejects_non_token_and_still_scrubs():
    mgr, db, gw, be = make()
    mgr._pending = ("addsender", "")
    run(mgr.on_console_text(ADMIN, 99, "not-a-token"))
    assert db.list_senders() == []          # nothing stored
    assert 99 in gw.deleted                 # still scrubbed (credential-like)
    assert mgr._pending == ("addsender", "")  # flow stays open to retry


def test_add_sender_rejects_colon_token_with_non_numeric_id():
    # A token with a colon but a non-numeric id prefix ("abc:secret") must be
    # rejected too: the id prefix keys the row and has to be numeric. This pins
    # the isdigit() half of the validation, which the no-colon case never hits.
    mgr, db, gw, be = make()
    mgr._pending = ("addsender", "")
    run(mgr.on_console_text(ADMIN, 77, "abc:secret"))
    assert db.list_senders() == []            # nothing stored
    assert 77 in gw.deleted                   # still scrubbed (credential-like)
    assert mgr._pending == ("addsender", "")  # flow stays open to retry


def test_remove_sender_updates_db():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.add_sender("111", "111:tok1")
    db.add_sender("222", "222:tok2")
    # render the list so the picker generation is current, then tap Remove
    title, m = mgr._senders_view()
    gen = mgr._sender_list["gen"]
    run(mgr.on_callback(ADMIN, M.cb("set", "senderdel", f"{gen}.0")))
    ids = {s["bot_id"] for s in db.list_senders()}
    assert ids == {"222"}


def test_remove_sender_stale_picker_ignored():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    db.add_sender("111", "111:tok1")
    mgr._senders_view()
    stale = mgr._sender_list["gen"] - 1   # a generation from a superseded list
    run(mgr.on_callback(ADMIN, M.cb("set", "senderdel", f"{stale}.0")))
    assert len(db.list_senders()) == 1    # nothing removed


def test_settings_menu_offers_sender_bots():
    from tgbridge import menu as M
    mgr, db, gw, be = make()
    _title, m = mgr._nav_view("settings")
    flat = [d for row in m for _, d in row]
    assert M.cb("nav", "senders") in flat


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
