"""Engram: the Graphiti-backed knowledge graph for Relic.

Public API for the graph subsystem (the memory / retrieval store). Other subsystems
import only what is listed in ``__all__`` (``from relic.graph import ...``), never an
internal module such as ``memory`` or ``recall``. Recall is also exposed to serve as an
injected ``RecallFn`` (see ``relic.contracts``), so the serve subsystem never imports
graph at all.

Production callers go through the Memory seam (``open_memory`` -> ``GraphitiMemory``,
satisfying ``MemoryReader`` / ``MemoryWriter``); ``falkordb_reachable`` remains for the
liveness probe (ADR-0003). Raw Graphiti construction stays private to ``memory`` (the
retired ``make_engram`` is now ``_build_graphiti``, reachable only via ``open_memory``).
"""

from relic.graph.audit import ZoneAuditReport, ZoneViolation, audit_zone_integrity
from relic.graph.field_map import ARTIFACT_FIELD_MAPS, body_path_exists
from relic.graph.load import LoadStats, load_episodes, load_episodes_bulk
from relic.graph.memory import (
    GraphitiMemory,
    MemoryEdge,
    MemoryEntity,
    MemoryEpisode,
    MemoryReader,
    MemoryWriter,
    ScopedMemory,
    ensure_indexes,
    falkordb_reachable,
    open_memory,
)
from relic.graph.queries import ReviewerHit, reviewers_of
from relic.graph.recall import RecallAnswer, RecalledFact, Source, format_answer, recall
from relic.graph.schema import (
    GLOBAL_ENTITY_TYPES,
    ZONED_ENTITY_TYPES,
    ZoneIntegrityError,
    is_global_entity,
    is_global_type,
    require_episode_zone,
)

__all__ = [
    "ARTIFACT_FIELD_MAPS",
    "GLOBAL_ENTITY_TYPES",
    "ZONED_ENTITY_TYPES",
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
    "ScopedMemory",
    "Source",
    "ZoneAuditReport",
    "ZoneIntegrityError",
    "ZoneViolation",
    "audit_zone_integrity",
    "body_path_exists",
    "ensure_indexes",
    "falkordb_reachable",
    "format_answer",
    "is_global_entity",
    "is_global_type",
    "load_episodes",
    "load_episodes_bulk",
    "open_memory",
    "recall",
    "require_episode_zone",
    "reviewers_of",
]
