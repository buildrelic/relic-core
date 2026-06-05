"""Land structured episodes into Graphiti via constrained LLM extraction.

Episodes are added sequentially: each episode is awaited before the next so its
extracted entities are resolvable when later episodes reference them. FalkorDB is
multi-tenant, so each episode lands under its repo's group_id, partitioning the
graph per source.
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

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, ensure_indexes

    await ensure_indexes(graphiti)
    items = track(episodes, description="loading episodes") if progress else episodes
    for spec in items:
        await graphiti.add_episode(
            name=spec.name,
            episode_body=spec.body,
            source_description=spec.source_description,
            reference_time=spec.reference_time,
            source=EpisodeType.json,
            # Partition per repo: FalkorDB is multi-tenant, so each episode lands under
            # its source's group_id. Search and the Cypher fallback both filter on it.
            group_id=spec.group_id,
            entity_types=ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=EDGE_TYPE_MAP,
        )
    return LoadStats(episodes=len(episodes), group_id=group_id)
