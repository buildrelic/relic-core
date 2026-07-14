"""Engram queries: people connected to work matching a query, with provenance.

``reviewers_of`` is the review-routing question ("who reviews auth changes?") as
one SQL query over the ``memory_people`` join, the ``memory_paths`` table, and
full-text search -- deterministic where the old Cypher walk was best-effort. It
never raises: a store failure returns an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from relic.engram.store import ReviewerHit

if TYPE_CHECKING:
    from relic.engram.store import EngramStore

__all__ = ["ReviewerHit", "reviewers_of"]


async def reviewers_of(
    store: EngramStore,
    query_text: str,
    *,
    scope: str | None = None,
    num_results: int = 10,
) -> list[ReviewerHit]:
    """People connected to work matching ``query_text`` (path match or full-text)."""
    try:
        return await store.reviewers_of(query_text, scope=scope, limit=num_results)
    except Exception:  # noqa: BLE001 - a people query must degrade, not raise
        return []
