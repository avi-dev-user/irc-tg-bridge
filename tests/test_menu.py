"""Tests for menu structure and callback encoding."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge import menu  # noqa: E402
from tgbridge.i18n import Translator  # noqa: E402

LOCALES = os.path.join(os.path.dirname(__file__), "..", "locales")


def bound(lang="en"):
    tr = Translator(LOCALES)
    return lambda key: tr.t(key, lang)


def test_callback_roundtrip():
    assert menu.parse_cb(menu.cb("srv", "view", "libera")) == ("srv", "view", "libera")
    assert menu.parse_cb(menu.cb("nav", "main")) == ("nav", "main", "")


def test_callback_handles_colons_in_arg():
    # server names are simple, but an arg with a colon must survive (split max 2)
    assert menu.parse_cb("set:lang:pt:BR") == ("set", "lang", "pt:BR")


def test_callback_too_long_rejected():
    try:
        menu.cb("srv", "view", "x" * 80)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_main_menu_structure_and_labels():
    m = menu.main_menu(bound("en"))
    labels = [b[0] for row in m for b in row]
    assert "Servers" in labels and "Settings" in labels
    # every button carries a parseable callback
    for row in m:
        for _, data in row:
            ns, action, _ = menu.parse_cb(data)
            assert ns and action


def test_main_menu_translated():
    labels = [b[0] for row in menu.main_menu(bound("he")) for b in row]
    assert "שרתים" in labels


def test_servers_menu_shows_badges_and_back():
    m = menu.servers_menu(bound("en"),
                          [{"name": "libera", "tor": False, "anon": False},
                           {"name": "secret", "tor": True, "anon": True}])
    flat = [b for row in m for b in row]
    # no status recorded -> the red (disconnected) dot, then the name
    assert any(label == "🔴libera" for label, _ in flat)
    # tor badge follows the status dot, before the name
    assert any(label == "🔴🧅secret" for label, _ in flat)
    assert any(menu.parse_cb(d)[:2] == ("nav", "main") for _, d in flat)


def test_servers_menu_status_dot_per_state():
    m = menu.servers_menu(bound("en"),
                          [{"name": "up", "status": "connected"},
                           {"name": "mid", "status": "connecting"},
                           {"name": "down", "status": "disconnected"},
                           {"name": "unset"}])
    labels = [label for row in m for label, _ in row]
    assert "🟢up" in labels
    assert "🟡mid" in labels
    assert "🔴down" in labels
    assert "🔴unset" in labels   # missing status defaults to disconnected


def test_server_title_shows_status_and_badge():
    t = menu.server_title(bound("en"),
                          {"name": "libera", "status": "connected", "tor": True})
    assert "libera" in t and "🟢" in t and "🧅" in t and "Connected" in t


def test_server_title_shows_encryption():
    on = menu.server_title(bound("en"),
                           {"name": "x", "status": "connected", "tls": 1})
    off = menu.server_title(bound("en"),
                            {"name": "x", "status": "connected", "tls": 0})
    assert "🔒" in on and "🔓" not in on
    assert "🔓" in off and "🔒" not in off


def test_server_title_shows_auth_method():
    t = menu.server_title(bound("en"),
                          {"name": "x", "status": "connected", "auth_method": "sasl"})
    assert "SASL" in t


def test_server_view_menu_common_actions_and_back():
    flat = [menu.parse_cb(d) for row in menu.server_view_menu(
        bound("en"), {"name": "libera"}) for _, d in row]
    for c in [("srv", "join", "libera"), ("srv", "channels", "libera"),
              ("srv", "discover", "libera"), ("srv", "nick", "libera"),
              ("srv", "away", "libera"), ("srv", "reconnect", "libera"),
              ("srv", "disconnect", "libera"), ("srv", "settings", "libera"),
              ("srv", "remove", "libera")]:
        assert c in flat, c
    assert ("nav", "servers", "") in flat            # back to the servers list
    # the rare config is NOT on the main view (it moved under Settings)
    assert ("srv", "tor", "libera") not in flat
    assert ("srv", "identify", "libera") not in flat


def test_server_view_menu_away_button_reflects_state():
    off = {menu.parse_cb(d): label for row in menu.server_view_menu(
        bound("en"), {"name": "libera"}) for label, d in row}
    assert off[("srv", "away", "libera")] == "Set away"
    on = {menu.parse_cb(d): label for row in menu.server_view_menu(
        bound("en"), {"name": "libera", "away": True}) for label, d in row}
    assert on[("srv", "away", "libera")] == "Clear away"


def test_server_settings_menu_holds_moved_config():
    flat = [b for row in menu.server_settings_menu(
        bound("en"), {"name": "libera"}) for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    for c in [("srv", "identify", "libera"), ("srv", "register", "libera"),
              ("srv", "perform", "libera"),
              ("srv", "ignores", "libera"), ("srv", "autojoin", "libera"),
              ("srv", "noisejoin", "libera"), ("srv", "noisepart", "libera"),
              ("srv", "noisequit", "libera"), ("srv", "tor", "libera"),
              ("srv", "motd", "libera"), ("srv", "info", "libera")]:
        assert c in by_cb, c
    assert by_cb[("srv", "identify", "libera")] == "Identify (NickServ)"
    assert by_cb[("srv", "register", "libera")] == "Register (NickServ)"
    assert ("srv", "view", "libera") in by_cb            # back to the server view
    assert all(len(d.encode("utf-8")) <= 64 for _, d in flat)


def test_server_settings_autojoin_button_reflects_state():
    def by_cb(srv):
        return {menu.parse_cb(d): label for row in menu.server_settings_menu(
            bound("en"), srv) for label, d in row}
    assert by_cb({"name": "libera", "autojoin": 1})[("srv", "autojoin", "libera")].endswith("✓")
    assert by_cb({"name": "libera"})[("srv", "autojoin", "libera")].endswith("✓")
    assert by_cb({"name": "libera", "autojoin": 0})[("srv", "autojoin", "libera")].endswith("✗")


def test_server_settings_noise_toggles_reflect_state():
    by_cb = {menu.parse_cb(d): label for row in menu.server_settings_menu(
        bound("en"), {"name": "libera", "noise_filter": "part,quit"}) for label, d in row}
    assert by_cb[("srv", "noisejoin", "libera")].endswith("✓")
    assert by_cb[("srv", "noisepart", "libera")].endswith("✗")
    assert by_cb[("srv", "noisequit", "libera")].endswith("✗")


def test_server_settings_noise_default_all_hidden():
    by_cb = {menu.parse_cb(d): label for row in menu.server_settings_menu(
        bound("en"), {"name": "libera"}) for label, d in row}
    assert by_cb[("srv", "noisejoin", "libera")].endswith("✗")
    assert by_cb[("srv", "noisepart", "libera")].endswith("✗")
    assert by_cb[("srv", "noisequit", "libera")].endswith("✗")


def test_channels_menu_actions_by_index_and_has_back():
    channels = [{"buffer": "irc.libera.#weechat", "topic_id": 2},
                {"buffer": "irc.libera.&local", "topic_id": 3}]
    m = menu.channels_menu(bound("en"), "libera", channels, 7)
    flat = [b for row in m for b in row]
    # each channel is one button opening its actions panel, referenced by
    # generation.index, label shows the name (leave lives inside that panel)
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    assert ("srv", "actions", "7.0") in by_cb
    assert ("srv", "actions", "7.1") in by_cb
    assert "#weechat" in by_cb[("srv", "actions", "7.0")]
    assert "&local" in by_cb[("srv", "actions", "7.1")]
    # a stray tap never parts a channel: no leave path at this level
    assert not any(menu.parse_cb(d)[:2] == ("srv", "leavech") for _, d in flat)
    # names never leak into callback_data (index only)
    assert all("#" not in d and "&" not in d for _, d in flat)
    # back returns to the server view
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)


def test_channels_menu_empty_still_has_back():
    m = menu.channels_menu(bound("en"), "libera", [], 1)
    flat = [b for row in m for b in row]
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)
    assert not any(menu.parse_cb(d)[:2] == ("srv", "leavech") for _, d in flat)


def test_channels_menu_has_actions_button_per_channel():
    channels = [{"buffer": "irc.libera.#weechat", "topic_id": 2}]
    m = menu.channels_menu(bound("en"), "libera", channels, 7)
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    # each channel gets one Actions button referenced by generation.index
    assert ("srv", "actions", "7.0") in by_cb
    assert "#weechat" in by_cb[("srv", "actions", "7.0")]
    # names never leak into callback_data (index only)
    assert all("#" not in d and "&" not in d for _, d in flat)


def test_channel_panel_menu_structure():
    m = menu.channel_panel_menu(bound("en"), "libera", "#weechat", 7, 0)
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    assert ("srv", "names", "7.0") in by_cb
    assert ("srv", "who", "7.0") in by_cb
    assert ("srv", "topic", "7.0") in by_cb
    assert ("srv", "leaveconfirm", "7.0") in by_cb   # leave asks first, in-panel
    assert ("srv", "channels", "libera") in by_cb    # back to the channels view
    # no channel name in callback_data, all within the 64-byte cap
    assert all("#" not in d for _, d in flat)
    assert all(len(d.encode("utf-8")) <= 64 for _, d in flat)


def test_names_menu_buttons_by_index_no_nick_leak():
    users = [{"prefix": "@", "nick": "alice"}, {"prefix": "", "nick": "bob"}]
    m = menu.names_menu(bound("en"), "libera", users, 5)
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    assert ("usr", "pick", "5.0") in by_cb
    assert ("usr", "pick", "5.1") in by_cb
    # the label carries the prefix + nick; the callback carries only the index
    assert by_cb[("usr", "pick", "5.0")] == "@alice"
    assert by_cb[("usr", "pick", "5.1")] == "bob"
    assert all("alice" not in d and "bob" not in d for _, d in flat)


def test_names_menu_has_back_to_channels():
    users = [{"prefix": "@", "nick": "alice"}]
    m = menu.names_menu(bound("en"), "libera", users, 5)
    flat = [b for row in m for b in row]
    # back returns to the server's channels list (the level above the panel)
    assert any(menu.parse_cb(d) == ("srv", "channels", "libera") for _, d in flat)


def test_names_menu_empty_still_has_back():
    m = menu.names_menu(bound("en"), "libera", [], 1)
    flat = [b for row in m for b in row]
    assert not any(menu.parse_cb(d)[:2] == ("usr", "pick") for _, d in flat)
    assert any(menu.parse_cb(d) == ("srv", "channels", "libera") for _, d in flat)


def test_user_actions_menu_has_all_actions_and_back():
    m = menu.user_actions_menu(bound("en"), 5, 2)
    flat = [b for row in m for b in row]
    cbs = {menu.parse_cb(d) for _, d in flat}
    for act in ("whois", "op", "deop", "voice", "devoice", "kick", "ban"):
        assert ("usr", act, "5.2") in cbs, act
    assert ("usr", "pickback", "5.2") in cbs    # back to the names picker
    assert all(len(d.encode("utf-8")) <= 64 for _, d in flat)


def test_discovered_menu_joins_by_generation_index():
    channels = [{"channel": "#python", "users": 4213, "topic": "Py"},
                {"channel": "#weechat", "users": 120, "topic": "chat"}]
    m = menu.discovered_menu(bound("en"), "libera", channels, 3)
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    assert ("srv", "discinfo", "3.0") in by_cb
    assert ("srv", "discinfo", "3.1") in by_cb
    assert "#python" in by_cb[("srv", "discinfo", "3.0")]
    assert "(4213)" in by_cb[("srv", "discinfo", "3.0")]
    assert "Py" in by_cb[("srv", "discinfo", "3.0")]     # topic shown for context
    # names never leak into callback_data
    assert all("#" not in d for _, d in flat)


def test_discovered_menu_topic_trimmed_and_optional():
    long_topic = "welcome to the channel, please read the rules and be nice to everyone"
    channels = [{"channel": "#chan", "users": 5, "topic": long_topic},
                {"channel": "#bare", "users": 2, "topic": ""}]
    m = menu.discovered_menu(bound("en"), "libera", channels, 3)
    by_cb = {menu.parse_cb(d): label for row in m for label, d in row}
    labeled = by_cb[("srv", "discinfo", "3.0")]
    assert labeled.startswith("#chan (5) · welcome to the channel")
    assert labeled.endswith("...")                       # a long topic is trimmed
    assert len(labeled) < len(long_topic)
    # a channel with no topic is just "name (users)", no trailing separator
    assert by_cb[("srv", "discinfo", "3.1")] == "#bare (2)"
    # topic text never leaks into callback_data
    assert all("welcome" not in d for _, d in [b for row in m for b in row])


def test_discovered_menu_has_back_to_server_view():
    channels = [{"channel": "#python", "users": 4213, "topic": "Py"}]
    m = menu.discovered_menu(bound("en"), "libera", channels, 3)
    flat = [b for row in m for b in row]
    # back returns to the server view the discovery was launched from
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)


def test_discovered_menu_empty_still_has_back():
    m = menu.discovered_menu(bound("en"), "libera", [], 1)
    flat = [b for row in m for b in row]
    assert not any(menu.parse_cb(d)[:2] == ("srv", "discinfo") for _, d in flat)
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)


def test_discovered_channel_menu_join_and_back():
    m = menu.discovered_channel_menu(bound("en"), 3, 0)
    by_cb = {menu.parse_cb(d): label for row in m for label, d in row}
    assert ("srv", "joinidx", "3.0") in by_cb        # Join actually joins
    assert by_cb[("srv", "joinidx", "3.0")] == "Join"
    assert ("srv", "discback", "") in by_cb          # Back to the discovered list
    assert all(len(d.encode("utf-8")) <= 64 for _, d in [b for row in m for b in row])


def test_discovered_channel_title_shows_name_users_topic():
    t = menu.discovered_channel_title(
        bound("en"), {"channel": "#python", "users": 4213, "topic": "Python <chat>"})
    assert "#python" in t and "4213" in t
    assert "Python &lt;chat&gt;" in t                 # topic shown, HTML-escaped
    # a channel with no topic falls back to a readable line, not an empty block
    bare = menu.discovered_channel_title(
        bound("en"), {"channel": "#bare", "users": 1, "topic": ""})
    assert "No topic set." in bare


def test_server_settings_menu_has_ignores_button():
    m = menu.server_settings_menu(bound("en"), {"name": "libera"})
    flat = [b for row in m for b in row]
    assert any(menu.parse_cb(d) == ("srv", "ignores", "libera") for _, d in flat)
    assert any(label == "Ignore list" for label, _ in flat)


def test_ignores_menu_unignores_by_index_and_has_add_and_back():
    m = menu.ignores_menu(bound("en"), "libera", ["spammer", "troll"], 4)
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    # each nick is an Unignore button referenced by generation.index
    assert ("srv", "unignore", "4.0") in by_cb
    assert ("srv", "unignore", "4.1") in by_cb
    assert "spammer" in by_cb[("srv", "unignore", "4.0")]
    assert "troll" in by_cb[("srv", "unignore", "4.1")]
    # the Add action and the back button target this server by name
    assert any(menu.parse_cb(d) == ("srv", "ignoreadd", "libera") for _, d in flat)
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)


def test_ignores_menu_empty_still_has_add_and_back():
    m = menu.ignores_menu(bound("en"), "libera", [], 1)
    flat = [b for row in m for b in row]
    assert not any(menu.parse_cb(d)[:2] == ("srv", "unignore") for _, d in flat)
    assert any(menu.parse_cb(d) == ("srv", "ignoreadd", "libera") for _, d in flat)
    assert any(menu.parse_cb(d) == ("srv", "view", "libera") for _, d in flat)


def test_help_menu_has_category_buttons_and_back():
    m = menu.help_menu(bound("en"))
    flat = [b for row in m for b in row]
    by_cb = {menu.parse_cb(d): label for label, d in flat}
    for slug in menu.HELP_CATEGORIES:
        assert ("help", "cat", slug) in by_cb
    # labels are translated names, not the raw slug or key
    assert by_cb[("help", "cat", "channels")] == "Channels"
    # every callback stays within the 64-byte cap (cb() would raise otherwise)
    assert all(len(d.encode("utf-8")) <= 64 for _, d in flat)
    # back returns to the main console
    assert any(menu.parse_cb(d) == ("nav", "main", "") for _, d in flat)


def test_help_menu_translated():
    labels = [b[0] for row in menu.help_menu(bound("he")) for b in row]
    assert "ערוצים" in labels


def test_language_menu_lists_codes_and_has_back():
    m = menu.language_menu(bound("en"), ["en", "he"], {"en": "English", "he": "עברית"})
    flat = [b for row in m for b in row]
    assert any(menu.parse_cb(d) == ("set", "lang", "he") for _, d in flat)
    # back returns to the settings menu
    assert any(menu.parse_cb(d) == ("nav", "settings", "") for _, d in flat)


def test_settings_menu_has_back_to_main():
    m = menu.settings_menu(bound("en"), {"language": "en", "tor_default": False})
    flat = [b for row in m for b in row]
    assert any(menu.parse_cb(d) == ("nav", "main", "") for _, d in flat)


def test_server_view_menu_has_back_to_servers():
    m = menu.server_view_menu(bound("en"), {"name": "libera"})
    flat = [b for row in m for b in row]
    assert any(menu.parse_cb(d) == ("nav", "servers", "") for _, d in flat)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
