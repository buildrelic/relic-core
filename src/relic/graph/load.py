"""Land structured episodes into Graphiti via constrained LLM extraction.

Episodes are added sequentially: Kuzu serializes writes and Graphiti expects each
episode awaited before the next. A deterministic per-episode uuid makes re-ingest
update in place instead of accumulating duplicate episodic nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from relic.ingest.mappers import EpisodeSpec

if TYPE_CHECKING:
    from graphiti_core import Graphiti


@dataclass(slots=True)
class LoadStats:
    episodes: int
    group_id: str


async def load_episodes(
    graphiti: Graphiti,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    progress: bool = True,
) -> LoadStats:
    """Add each episode to Graphiti with the constrained entity/edge types."""
    from graphiti_core.nodes import EpisodeType
    from rich.progress import track

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, ensure_fts_indexes

    await ensure_fts_indexes(graphiti)
    items = track(episodes, description="loading episodes") if progress else episodes
    for spec in items:
        await graphiti.add_episode(
            name=spec.name,
            episode_body=spec.body,
            source_description=spec.source_description,
            reference_time=spec.reference_time,
            source=EpisodeType.json,
            # group_id left default and uuid auto-generated: Kuzu is single-database
            # (Graphiti's custom-group_id path needs a multi-database driver), and a
            # provided uuid must already exist. Re-ingest is additive; delete the Kuzu
            # store to reload clean. Per-repo partitioning returns with FalkorDB.
            entity_types=ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=EDGE_TYPE_MAP,
        )
    return LoadStats(episodes=len(episodes), group_id=group_id)
