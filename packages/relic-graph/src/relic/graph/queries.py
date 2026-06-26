"""Graph queries: people connected to work matching a query, with provenance.

`reviewers_of` tries the memory seam's hybrid search first, then falls back to its
deterministic ``reviewer_walk`` (a Cypher walk over REVIEWED/AUTHORED, held inside the
adapter) so the query still returns something if search comes back empty. It never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relic.graph.memory import MemoryEdge, MemoryEntity, MemoryReader


@dataclass(slots=True)
class ReviewerHit:
    name: str
    relation: str
    fact: str
    episodes: list[str] = field(default_factory=list)
    profile_url: str | None = None


async def reviewers_of(
    memory: MemoryReader,
    query_text: str,
    *,
    group_id: str | None = None,
    num_results: int = 10,
) -> list[ReviewerHit]:
    """People connected to work matching `query_text` (search first, Cypher fallback)."""
    group_ids = [group_id] if group_id else None
    try:
        edges = await memory.search(query_text, group_ids=group_ids, num_results=num_results)
        hits = _edges_to_hits(edges)
        if hits:
            return hits
    except Exception:  # noqa: BLE001 - fall back to the deterministic Cypher walk
        pass
    edges = await memory.reviewer_walk(query_text, group_id=group_id, limit=num_results)
    return _edges_to_hits(edges)


def _edges_to_hits(edges: list[MemoryEdge]) -> list[ReviewerHit]:
    """One hit per Person endpoint of each edge (the resolved endpoints carry the labels)."""
    hits: list[ReviewerHit] = []
    for edge in edges:
        for endpoint in (edge.source, edge.target):
            if "Person" not in endpoint.labels:
                continue
            hits.append(
                ReviewerHit(
                    name=endpoint.name,
                    relation=edge.relation,
                    fact=edge.fact,
                    episodes=list(edge.episode_uuids),
                    profile_url=_profile_url(endpoint),
                )
            )
    return hits


def _profile_url(entity: MemoryEntity) -> str | None:
    value = entity.attributes.get("profile_url")
    return value if isinstance(value, str) else None
