"""Translations for the bot interface.

English is the source language and the set of keys. Other locales are JSON
files with the same keys; any key a locale is missing falls back to English, so
a half-translated locale still works. Adding a language is one file.
"""

from __future__ import annotations

import json
import os
from typing import Optional

DEFAULT_LANG = "en"


class Translator:
    def __init__(self, locales_dir: str):
        self._locales_dir = locales_dir
        self._catalogs: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        for entry in os.listdir(self._locales_dir):
            if not entry.endswith(".json"):
                continue
            lang = entry[: -len(".json")]
            with open(os.path.join(self._locales_dir, entry), encoding="utf-8") as fh:
                self._catalogs[lang] = json.load(fh)
        if DEFAULT_LANG not in self._catalogs:
            raise RuntimeError(f"missing base locale {DEFAULT_LANG}.json")

    def languages(self) -> list[str]:
        return sorted(self._catalogs)

    def language_name(self, lang: str) -> str:
        catalog = self._catalogs.get(lang, {})
        return catalog.get("language_name", lang)

    def t(self, key: str, lang: Optional[str] = None, **params: object) -> str:
        catalog = self._catalogs.get(lang or DEFAULT_LANG, {})
        template = catalog.get(key)
        if template is None:
            template = self._catalogs[DEFAULT_LANG].get(key, key)
        if params:
            try:
                return template.format(**params)
            except (KeyError, IndexError):
                # A malformed placeholder should never crash a reply; show the
                # raw template rather than raising in a message handler.
                return template
        return template

    def missing_keys(self, lang: str) -> set[str]:
        """Keys present in the base locale but absent from `lang`."""
        base = set(self._catalogs[DEFAULT_LANG])
        return base - set(self._catalogs.get(lang, {}))
