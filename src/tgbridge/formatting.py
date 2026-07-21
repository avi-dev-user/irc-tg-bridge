"""Text formatting between Telegram and IRC.

Telegram carries formatting as a list of entities with offsets measured in
UTF-16 code units. IRC carries it as inline mIRC control bytes. This module
converts each way, and splits long text into IRC-safe lines.

The IRC side must read like a normal client, so only the formatting IRC users
actually see is emitted (bold/italic/underline/strikethrough/monospace); a
hidden link becomes the visible URL, the way a person would paste it.
"""

from __future__ import annotations

from typing import Optional

# mIRC control bytes. Each is a toggle: the same byte turns the style on, then
# off. RESET clears everything.
BOLD = "\x02"
ITALIC = "\x1d"
UNDERLINE = "\x1f"
STRIKE = "\x1e"
MONO = "\x11"
COLOR = "\x03"
RESET = "\x0f"

_ENTITY_TO_MIRC = {
    "bold": BOLD,
    "italic": ITALIC,
    "underline": UNDERLINE,
    "strikethrough": STRIKE,
    "code": MONO,
    "pre": MONO,
}

# Conservative per-line byte budget. A raw PRIVMSG line is capped near 512
# bytes including ":nick!user@host PRIVMSG target :" overhead and CRLF; 400
# leaves room without needing to know our exact hostmask.
DEFAULT_LINE_BUDGET = 400


def _utf16_offsets(text: str) -> list[int]:
    """Map each UTF-16 offset to a Python string index (plus a final entry)."""
    index_at = {}
    unit = 0
    for i, ch in enumerate(text):
        index_at[unit] = i
        unit += 1 if ord(ch) <= 0xFFFF else 2
    index_at[unit] = len(text)
    return index_at


def telegram_to_mirc(text: str, entities: Optional[list[dict]] = None) -> str:
    """Render a Telegram message (text + entities) as IRC text with mIRC codes."""
    if not entities:
        return text

    index_at = _utf16_offsets(text)
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}

    for ent in entities:
        start = index_at.get(ent["offset"])
        end = index_at.get(ent["offset"] + ent["length"])
        if start is None or end is None:
            continue
        code = _ENTITY_TO_MIRC.get(ent["type"])
        if code:
            opens.setdefault(start, []).append(code)
            closes.setdefault(end, []).insert(0, code)
        elif ent["type"] == "text_link" and ent.get("url"):
            # Hidden link: reveal the URL so the IRC side sees what was linked.
            closes.setdefault(end, []).insert(0, f" ({ent['url']})")

    out = []
    for i in range(len(text) + 1):
        for code in closes.get(i, []):
            out.append(code)
        for code in opens.get(i, []):
            out.append(code)
        if i < len(text):
            out.append(text[i])
    return "".join(out)


def mirc_to_html(text: str) -> str:
    """Render IRC text (with mIRC codes) as Telegram-safe HTML.

    Open tags are tracked on a stack so nesting stays valid: turning a style
    off closes down to it and reopens the styles that were nested above.
    Colour codes carry no clean Telegram equivalent and are dropped.
    """
    tags = {BOLD: "b", ITALIC: "i", UNDERLINE: "u", STRIKE: "s", MONO: "code"}
    stack: list[str] = []
    out = []

    def toggle(style: str) -> None:
        if style not in stack:
            stack.append(style)
            out.append(f"<{tags[style]}>")
            return
        reopen = []
        while True:
            top = stack.pop()
            out.append(f"</{tags[top]}>")
            if top == style:
                break
            reopen.append(top)
        for t in reversed(reopen):
            stack.append(t)
            out.append(f"<{tags[t]}>")

    def close_all() -> None:
        while stack:
            out.append(f"</{tags[stack.pop()]}>")

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in tags:
            toggle(ch)
            i += 1
        elif ch == RESET:
            close_all()
            i += 1
        elif ch == COLOR:
            # Skip a colour spec: optional fg[,bg] digits.
            i += 1
            digits = 0
            while i < n and text[i].isdigit() and digits < 2:
                i += 1
                digits += 1
            if i < n and text[i] == "," and i + 1 < n and text[i + 1].isdigit():
                i += 1
                digits = 0
                while i < n and text[i].isdigit() and digits < 2:
                    i += 1
                    digits += 1
        else:
            out.append(ch.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            i += 1

    close_all()
    return "".join(out)


def looks_like_art(text: str) -> bool:
    """True when text is dominated by box-drawing, block, or geometric-shape
    characters - IRC ASCII art drawn for a fixed-width terminal, which needs a
    monospace font to keep its shape. Ordinary prose and a stray symbol are not
    caught (they need a run of these characters, not just one)."""
    art = dense = 0
    for ch in text:
        if ch.isspace():
            continue
        dense += 1
        if 0x2500 <= ord(ch) <= 0x25FF:   # box drawing, block elements, geometric
            art += 1
    return art >= 3 and dense >= 4 and art / dense >= 0.30


def _split_word(word: str, budget: int) -> list[str]:
    """Hard-split a single oversized word on UTF-8 byte boundaries."""
    chunks = []
    current = ""
    size = 0
    for ch in word:
        b = len(ch.encode("utf-8"))
        if size + b > budget and current:
            chunks.append(current)
            current, size = "", 0
        current += ch
        size += b
    if current:
        chunks.append(current)
    return chunks


def split_for_irc(text: str, budget: int = DEFAULT_LINE_BUDGET) -> list[str]:
    """Split text into IRC-safe lines: by newline, then by word, UTF-8 safe."""
    lines = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        current = ""
        for word in raw.split(" "):
            if len(word.encode("utf-8")) > budget:
                if current:
                    lines.append(current)
                    current = ""
                pieces = _split_word(word, budget)
                lines.extend(pieces[:-1])
                current = pieces[-1]
                continue
            candidate = word if not current else current + " " + word
            if len(candidate.encode("utf-8")) > budget:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines
