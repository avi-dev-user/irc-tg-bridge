"""Watch WeeChat's DCC download directory for completed incoming transfers.

WeeChat's xfer plugin auto-accepts a DCC send into a download directory (a live
config, not code here) and names each file after a configured pattern. We assume
that pattern encodes the remote nick as a prefix: "<nick>.<original>" (WeeChat's
xfer.file.download_path is set so the filename begins with the sender nick and a
dot, e.g. "alice.report.pdf"). DCC is one-to-one, so the nick prefix identifies
the sender, and the sender maps to their private-message topic on the bridge.

The filename parsing is pure and tested. The per-poll scan is a pure helper
(_scan): a completed transfer is a file whose size stays non-zero and unchanged
across several consecutive polls, after which the file is handed to a callback
(which re-hosts it). The poll loop itself is thin live glue over that helper.
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional


def parse_xfer_filename(name: str) -> tuple[Optional[str], str]:
    """Split a download filename "<nick>.<original>" into (nick, original).

    The original name may itself contain dots, so only the first dot separates
    the nick prefix from the rest ("alice.holiday.2024.mkv" -> ("alice",
    "holiday.2024.mkv")). A name with no separator, an empty nick, or an empty
    original is treated as not carrying a nick prefix: (None, name is returned
    unchanged), so the caller can skip it rather than route a file to nobody."""
    nick, sep, original = name.partition(".")
    if not sep or not nick or not original:
        return None, name
    return nick, original


OnFile = Callable[[str], Awaitable[None]]

# How many consecutive polls a file's size must stay unchanged before the
# transfer is treated as complete. One unchanged interval is too eager: a DCC
# transfer that stalls briefly at a partial size would look done and a truncated
# file would be uploaded. Requiring several settling polls avoids that.
_DEFAULT_STABLE_POLLS = 3


def _scan(path: str, names: list[str], sizes: dict[str, int],
          counts: dict[str, int], done: set[str], *,
          stable_polls: int) -> list[str]:
    """One poll's worth of completion detection, pure and synchronous.

    Given the current directory listing, update `sizes`/`counts`/`done` in place
    and return the full paths of files that just became complete. A file is
    complete once its size is non-zero and has held the same value across
    `stable_polls` consecutive observations. A name already in `done` is skipped
    (handed off already); a name that has disappeared is forgotten so a later
    transfer reusing it is picked up again. `done` is not advanced here: the
    caller adds a name only after a successful handoff, so a failed transfer is
    reported again on the next poll and retried."""
    present: set[str] = set()
    completed: list[str] = []
    for name in names:
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        present.add(name)
        if name in done:
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > 0 and sizes.get(name) == size:
            counts[name] = counts.get(name, 1) + 1
            if counts[name] >= stable_polls:
                completed.append(full)
        else:
            sizes[name] = size
            counts[name] = 1
    for gone in [n for n in sizes if n not in present]:
        sizes.pop(gone, None)
        counts.pop(gone, None)
    done &= present
    return completed


async def watch_dir(path: str, on_file: OnFile, *, interval: float = 2.0,
                    stable_polls: int = _DEFAULT_STABLE_POLLS) -> None:
    """Poll a directory and hand each completed file to `on_file`.

    Completion detection lives in _scan; this loop only sleeps, lists, and does
    the handoff. A handoff that raises is logged and the loop continues, so one
    transient Telegram error cannot terminate the watcher and silently drop every
    later incoming file. A file is marked done only after a successful handoff,
    so a failure is retried on the next poll. Errors reading the directory are
    ignored for that poll (it is retried on the next)."""
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {}
    done: set[str] = set()
    while True:
        await asyncio.sleep(interval)
        try:
            names = os.listdir(path)
        except OSError:
            continue
        for full in _scan(path, names, sizes, counts, done,
                           stable_polls=stable_polls):
            try:
                await on_file(full)
            except Exception as exc:
                print(f"[filewatch] handoff failed for {full}: {exc}")
                continue
            done.add(os.path.basename(full))
