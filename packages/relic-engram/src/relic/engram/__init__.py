"""Engram: the Postgres + Firestore memory store for Relic.

Public API for the engram subsystem (the memory / retrieval store). Other
subsystems import only what is listed in ``__all__`` (``from relic.engram import
...``), never an internal module such as ``store`` or ``recall``. Recall is also
exposed to serve as an injected ``RecallFn`` (see ``relic.contracts``), so the
serve subsystem never imports engram at all.

Production callers go through the store seam (``open_engram`` -> ``EngramStore``,
satisfying ``EngramReader`` / ``EngramWriter``); ``postgres_reachable`` is the
liveness probe. Raw SQL stays private to ``store`` (ADR-0007).
"""

from relic.engram.derive import Derived, derive
from relic.engram.documents import DocumentStore, firestore_configured
from relic.engram.load import LoadStats, load_episodes
from relic.engram.queries import ReviewerHit, reviewers_of
from relic.engram.recall import RecallAnswer, RecalledFact, Source, format_answer, recall
from relic.engram.store import (
    EngramHit,
    EngramReader,
    EngramStore,
    EngramWriter,
    open_engram,
    postgres_reachable,
)

__all__ = [
    "Derived",
    "DocumentStore",
    "EngramHit",
    "EngramReader",
    "EngramStore",
    "EngramWriter",
    "LoadStats",
    "RecallAnswer",
    "RecalledFact",
    "ReviewerHit",
    "Source",
    "derive",
    "firestore_configured",
    "format_answer",
    "load_episodes",
    "open_engram",
    "postgres_reachable",
    "recall",
    "reviewers_of",
]
