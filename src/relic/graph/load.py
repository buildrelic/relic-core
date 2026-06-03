"""Land structured episodes into Graphiti via constrained LLM extraction.

Episodes are added sequentially: Kuzu serializes writes and Graphiti expects each
episode awaited before the next. A deterministic per-episode uuid makes re-ingest
update in place instead of accumulating duplicate episodic nodes.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from relic.ingest.mappers import EpisodeSpec

if TYPE_CHECKING:
    from graphiti_core import Graphiti


@dataclass(slots=True)
class LoadStats:
    episodes: int
    group_id: str


def _episode_uuid(group_id: str, name: str) -> str:
    digest = hashlib.sha1(f"{group_id}:{name}".encode(), usedforsecurity=False).hexdigest()
    return str(uuid.UUID(digest[:32]))


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
            group_id=group_id,
            uuid=_episode_uuid(group_id, spec.name),
            entity_types=ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=EDGE_TYPE_MAP,
        )
    return LoadStats(episodes=len(episodes), group_id=group_id)
