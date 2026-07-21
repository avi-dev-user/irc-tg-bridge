"""Tests for the DCC download filename parser and the completion-detection scan."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.filewatch import (  # noqa: E402
    _scan, parse_xfer_filename, watch_dir)


def test_splits_nick_from_original():
    assert parse_xfer_filename("alice.report.pdf") == ("alice", "report.pdf")


def test_original_may_contain_many_dots():
    # only the first dot separates the nick; the rest stays in the original name
    assert parse_xfer_filename("bob.holiday.2024.final.mkv") == \
        ("bob", "holiday.2024.final.mkv")


def test_no_separator_is_not_a_nick_prefix():
    # a name with no dot carries no nick prefix: return it unchanged, nick None
    assert parse_xfer_filename("plainname") == (None, "plainname")


def test_empty_nick_is_rejected():
    # a leading dot (dotfile) has no nick before it, so it is not a prefix
    assert parse_xfer_filename(".hidden") == (None, ".hidden")


def test_empty_original_is_rejected():
    # a trailing dot leaves no original name, so it is not a valid prefix split
    assert parse_xfer_filename("alice.") == (None, "alice.")


# --- completion detection (_scan) -----------------------------------------

def _write(path, name, data):
    with open(os.path.join(path, name), "wb") as fh:
        fh.write(data)


def _poll(path, sizes, counts, done, *, stable_polls=2):
    return _scan(path, os.listdir(path), sizes, counts, done,
                 stable_polls=stable_polls)


def test_scan_growing_file_is_not_complete():
    # A file whose size keeps changing is still transferring; never completes.
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, set()
    _write(d, "a.f", b"12")
    assert _poll(d, sizes, counts, done) == []      # first sighting
    _write(d, "a.f", b"123456")                      # grew between polls
    assert _poll(d, sizes, counts, done) == []       # size changed, resets
    _write(d, "a.f", b"1234567890")
    assert _poll(d, sizes, counts, done) == []


def test_scan_stable_nonzero_file_completes_after_stable_polls():
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, set()
    _write(d, "a.f", b"hello")
    assert _poll(d, sizes, counts, done, stable_polls=2) == []       # 1st obs
    full = os.path.join(d, "a.f")
    assert _poll(d, sizes, counts, done, stable_polls=2) == [full]   # 2nd: done


def test_scan_three_stable_polls_required_when_configured():
    # With stable_polls=3 a single unchanged interval must NOT complete it; a
    # transfer that stalls for one poll at a partial size is not mistaken for done.
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, set()
    _write(d, "a.f", b"partial")
    assert _poll(d, sizes, counts, done, stable_polls=3) == []   # obs 1
    assert _poll(d, sizes, counts, done, stable_polls=3) == []   # obs 2 (stalled)
    full = os.path.join(d, "a.f")
    assert _poll(d, sizes, counts, done, stable_polls=3) == [full]   # obs 3


def test_scan_zero_byte_file_never_completes():
    # An empty file (transfer not started, or a placeholder) must never fire.
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, set()
    _write(d, "a.f", b"")
    for _ in range(5):
        assert _poll(d, sizes, counts, done, stable_polls=2) == []


def test_scan_removed_then_recreated_name_retriggers():
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, set()
    full = os.path.join(d, "a.f")
    _write(d, "a.f", b"one")
    assert _poll(d, sizes, counts, done, stable_polls=2) == []
    assert _poll(d, sizes, counts, done, stable_polls=2) == [full]
    done.add("a.f")                     # caller marks it handed off
    os.remove(full)
    assert _poll(d, sizes, counts, done, stable_polls=2) == []   # gone, forgotten
    _write(d, "a.f", b"two")            # a new transfer reuses the name
    assert _poll(d, sizes, counts, done, stable_polls=2) == []
    assert _poll(d, sizes, counts, done, stable_polls=2) == [full]   # fires again


def test_scan_already_done_name_is_skipped():
    d = tempfile.mkdtemp()
    sizes, counts, done = {}, {}, {"a.f"}
    _write(d, "a.f", b"hello")
    assert _poll(d, sizes, counts, done, stable_polls=2) == []
    assert _poll(d, sizes, counts, done, stable_polls=2) == []


# --- watch_dir handoff guard (async glue) ----------------------------------

def test_watch_dir_survives_a_raising_handoff_and_retries():
    # A handoff that raises must not terminate the watcher; the file is retried
    # on a later poll (not marked done), then handed off successfully.
    d = tempfile.mkdtemp()
    _write(d, "alice.doc", b"payload")
    calls = []
    done_evt = asyncio.Event()

    async def on_file(full):
        calls.append(full)
        if len(calls) == 1:
            raise RuntimeError("transient telegram error")
        done_evt.set()

    async def go():
        task = asyncio.create_task(
            watch_dir(d, on_file, interval=0.01, stable_polls=2))
        try:
            await asyncio.wait_for(done_evt.wait(), timeout=5)
        finally:
            task.cancel()
        assert not task.done() or task.cancelled()   # loop was still alive

    asyncio.run(go())
    assert len(calls) >= 2                            # first raised, then retried
    assert calls[0] == calls[1] == os.path.join(d, "alice.doc")


def test_watch_dir_hands_off_each_file_once_on_success():
    d = tempfile.mkdtemp()
    _write(d, "bob.mkv", b"movie-bytes")
    calls = []
    seen = asyncio.Event()

    async def on_file(full):
        calls.append(full)
        seen.set()

    async def go():
        task = asyncio.create_task(
            watch_dir(d, on_file, interval=0.01, stable_polls=2))
        await asyncio.wait_for(seen.wait(), timeout=5)
        await asyncio.sleep(0.1)          # several more polls elapse
        task.cancel()

    asyncio.run(go())
    assert calls == [os.path.join(d, "bob.mkv")]      # handed off exactly once


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
