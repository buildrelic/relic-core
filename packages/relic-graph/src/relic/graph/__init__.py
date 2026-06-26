"""Engram: the Graphiti-backed knowledge graph for Relic.

Public API for the graph subsystem (the memory / retrieval store). Other subsystems
import only what is listed in ``__all__`` (``from relic.graph import ...``), never an
internal module such as ``memory`` or ``recall``. Recall is also exposed to serve as an
injected ``RecallFn`` (see ``relic.contracts``), so the serve subsystem never imports
graph at all.

Production callers go through the Memory seam (``open_memory`` -> ``GraphitiMemory``,
satisfying ``MemoryReader`` / ``MemoryWriter``); ``make_engram`` and ``falkordb_reachable``
remain for construction and the liveness probe (ADR-0003).
"""

from relic.graph.engram import ensure_indexes, falkordb_reachable, make_engram
from relic.graph.load import LoadStats, load_episodes, load_episodes_bulk
from relic.graph.memory import (
    GraphitiMemory,
    MemoryEdge,
    MemoryEntity,
    MemoryEpisode,
    MemoryReader,
    MemoryWriter,
    open_memory,
)
from relic.graph.queries import ReviewerHit, reviewers_of
from relic.graph.recall import RecallAnswer, RecalledFact, Source, format_answer, recall

__all__ = [
    "GraphitiMemory",
    "LoadStats",
    "MemoryEdge",
    "MemoryEntity",
    "MemoryEpisode",
    "MemoryReader",
    "MemoryWriter",
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
    "open_memory",
    "recall",
    "reviewers_of",
]
