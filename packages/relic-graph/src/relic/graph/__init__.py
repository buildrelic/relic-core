"""Engram: the Graphiti-backed knowledge graph for Relic.

Public API for the graph subsystem (the memory / retrieval store). Other
subsystems import only what is listed in ``__all__`` (``from relic.graph import
...``), never an internal module such as ``engram`` or ``recall``. Recall is also
exposed to serve as an injected ``RecallFn`` (see ``relic.contracts``), so the
serve subsystem never imports graph at all.
"""

from relic.graph.engram import ensure_indexes, falkordb_reachable, make_engram
from relic.graph.load import LoadStats, load_episodes, load_episodes_bulk
from relic.graph.queries import ReviewerHit, reviewers_of
from relic.graph.recall import RecallAnswer, RecalledFact, Source, format_answer, recall

__all__ = [
    "LoadStats",
    "RecallAnswer",
    "RecalledFact",
    "ReviewerHit",
    "Source",
    "ensure_indexes",
    "falkordb_reachable",
    "format_answer",
    "load_episodes",
    "load_episodes_bulk",
    "make_engram",
    "recall",
    "reviewers_of",
]
