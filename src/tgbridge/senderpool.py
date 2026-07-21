"""Assign each Telegram forum topic to one bot from a pool.

Telegram rate-limits sends per (bot, chat). A busy bridge can spread its topics
across several bot tokens so no single bot's per-chat budget is the bottleneck.
This module holds only the decision: given the configured bot ids and how many
topics each already owns, it names the bot for a new topic. The live clients and
their startup live in the gateway; nothing here talks to Telegram, so the whole
assignment policy is unit-tested with plain dicts.
"""

from __future__ import annotations

from typing import Mapping


class SenderPool:
    def __init__(self, sender_ids):
        # Order is meaningful: it is the deterministic tie-break when two bots
        # carry the same number of topics, so a fresh pool fills its bots in the
        # order they were configured (primary first) rather than at random.
        self._ids = list(sender_ids)
        if not self._ids:
            raise ValueError("a sender pool needs at least one bot")

    @property
    def ids(self) -> list[str]:
        return list(self._ids)

    def owner_for_new_topic(self, owner_counts: Mapping[str, int]) -> str:
        """The bot that should own the next new topic: the least-loaded one.
        Counts are per bot; a bot with no topics yet is absent from owner_counts
        and reads as zero. Ties break by configured order, so the result is
        stable for a given state. Counts for bots no longer in the pool are
        ignored (they cannot win)."""
        return min(
            self._ids,
            key=lambda b: (owner_counts.get(b, 0), self._ids.index(b)),
        )
