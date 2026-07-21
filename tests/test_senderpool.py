"""Tests for the pure sender-pool assignment policy (no live Telegram)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgbridge.senderpool import SenderPool  # noqa: E402


def test_empty_pool_is_rejected():
    try:
        SenderPool([])
    except ValueError:
        return
    raise AssertionError("an empty pool should raise")


def test_single_bot_always_wins():
    pool = SenderPool(["primary"])
    assert pool.owner_for_new_topic({}) == "primary"
    # even with a stale count from some other, no-longer-configured bot
    assert pool.owner_for_new_topic({"gone": 99, "primary": 5}) == "primary"


def test_least_loaded_wins():
    pool = SenderPool(["a", "b", "c"])
    # b carries the fewest topics, so it takes the next one.
    assert pool.owner_for_new_topic({"a": 3, "b": 1, "c": 2}) == "b"


def test_absent_bot_reads_as_zero():
    pool = SenderPool(["a", "b"])
    # b has no topics yet (absent from the counts) so it is the least loaded.
    assert pool.owner_for_new_topic({"a": 2}) == "b"


def test_tie_breaks_by_configured_order():
    pool = SenderPool(["a", "b", "c"])
    # all equal: the first configured bot wins, deterministically.
    assert pool.owner_for_new_topic({}) == "a"
    assert pool.owner_for_new_topic({"a": 1, "b": 1, "c": 1}) == "a"


def test_counts_for_bots_outside_the_pool_are_ignored():
    pool = SenderPool(["a", "b"])
    # A huge count for a removed bot must not affect a and b's contest.
    assert pool.owner_for_new_topic({"removed": 100, "a": 1}) == "b"


def test_fills_round_robin_as_topics_accrue():
    pool = SenderPool(["a", "b"])
    counts: dict[str, int] = {}
    picks = []
    for _ in range(4):
        owner = pool.owner_for_new_topic(counts)
        picks.append(owner)
        counts[owner] = counts.get(owner, 0) + 1
    # least-loaded with an order tie-break alternates the two bots evenly.
    assert picks == ["a", "b", "a", "b"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
