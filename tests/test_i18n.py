"""Tests for the translation loader."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.i18n import Translator  # noqa: E402

LOCALES = os.path.join(os.path.dirname(__file__), "..", "locales")


def test_loads_en_and_he():
    tr = Translator(LOCALES)
    assert "en" in tr.languages()
    assert "he" in tr.languages()
    assert tr.language_name("he") == "עברית"


def test_translation_and_params():
    tr = Translator(LOCALES)
    assert tr.t("menu.servers", "en") == "Servers"
    assert tr.t("menu.servers", "he") == "שרתים"
    assert tr.t("addserver.connecting", "en", name="libera", nick="me") == \
        "Connecting to libera as me..."


def test_missing_key_falls_back_to_english():
    tr = Translator(LOCALES)
    # An unknown key returns itself, never raises.
    assert tr.t("does.not.exist", "he") == "does.not.exist"


def test_every_key_present_in_hebrew():
    tr = Translator(LOCALES)
    assert tr.missing_keys("he") == set(), \
        f"Hebrew locale is missing keys: {tr.missing_keys('he')}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
