"""Tests for Telegram <-> IRC formatting and line splitting."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge import formatting as f  # noqa: E402


def test_plain_text_passthrough():
    assert f.telegram_to_mirc("hello", None) == "hello"
    assert f.telegram_to_mirc("hello", []) == "hello"


def test_bold_entity_wraps():
    out = f.telegram_to_mirc("hi there", [{"type": "bold", "offset": 0, "length": 2}])
    assert out == f.BOLD + "hi" + f.BOLD + " there"


def test_text_link_reveals_url():
    out = f.telegram_to_mirc(
        "click", [{"type": "text_link", "offset": 0, "length": 5,
                   "url": "https://x.example"}]
    )
    assert out == "click (https://x.example)"


def test_utf16_offsets_with_astral_char():
    # The emoji is 2 UTF-16 units, so the bold entity on "ab" starts at offset 2.
    out = f.telegram_to_mirc(
        "\U0001F600ab", [{"type": "bold", "offset": 2, "length": 2}]
    )
    assert out == "\U0001F600" + f.BOLD + "ab" + f.BOLD


def test_mirc_to_html_bold_and_escape():
    assert f.mirc_to_html(f.BOLD + "a<b>" + f.BOLD) == "<b>a&lt;b&gt;</b>"


def test_mirc_to_html_reset_closes():
    assert f.mirc_to_html(f.BOLD + f.ITALIC + "x" + f.RESET + "y") == "<b><i>x</i></b>y"


def test_mirc_to_html_drops_color():
    assert f.mirc_to_html(f.COLOR + "04red") == "red"
    assert f.mirc_to_html(f.COLOR + "04,01text") == "text"


def test_split_short_stays_one_line():
    assert f.split_for_irc("just a short line") == ["just a short line"]


def test_split_newlines_become_lines():
    assert f.split_for_irc("one\ntwo\n\nthree") == ["one", "two", "three"]


def test_split_long_line_on_word_boundary():
    text = "word " * 200  # ~1000 bytes
    lines = f.split_for_irc(text, budget=100)
    assert len(lines) > 1
    assert all(len(line.encode("utf-8")) <= 100 for line in lines)
    # No word is broken: every token is intact.
    assert set(" ".join(lines).split()) == {"word"}


def test_split_hard_breaks_oversized_word():
    lines = f.split_for_irc("x" * 250, budget=100)
    assert all(len(line.encode("utf-8")) <= 100 for line in lines)
    assert "".join(lines) == "x" * 250


def test_split_outbound_one_liner_returns_itself():
    assert f.split_for_irc("hello there") == ["hello there"]


def test_split_multiline_paste_one_entry_per_line():
    assert f.split_for_irc("one\ntwo\nthree") == ["one", "two", "three"]
    # Consecutive newlines produce a blank that is dropped, never sent as a line.
    assert f.split_for_irc("one\n\ntwo") == ["one", "two"]


def test_split_multibyte_never_breaks_a_char():
    # Each shekel sign is 3 UTF-8 bytes; a budget of 10 must land on char edges.
    lines = f.split_for_irc("₪" * 20, budget=10)
    for line in lines:
        assert len(line.encode("utf-8")) <= 10
        line.encode("utf-8").decode("utf-8")   # valid: no truncated multibyte
    assert "".join(lines) == "₪" * 20


def test_looks_like_art_true_for_block_and_box():
    assert f.looks_like_art("████ ▄▄ ▐▌ █▀▀█")     # block elements
    assert f.looks_like_art("┌─┬─┐ │ │ └─┴─┘")      # box drawing


def test_looks_like_art_false_for_normal_text():
    assert not f.looks_like_art("hello everyone, how are you?")
    assert not f.looks_like_art("nice 👍")          # a single non-art symbol
    assert not f.looks_like_art("2 + 2 = 4")
    assert not f.looks_like_art("")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
