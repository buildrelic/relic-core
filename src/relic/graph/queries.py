"""Graph queries: people connected to work matching a query, with provenance.

`reviewers_of` tries Graphiti hybrid search first, then falls back to a
deterministic Cypher walk over REVIEWED/AUTHORED edges so the query still returns
something if search comes back empty. It never raises.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphiti_core import Graphiti


@dataclass(slots=True)
class ReviewerHit:
    name: str
    relation: str
    fact: str
    episodes: list[str] = field(default_factory=list)
    profile_url: str | None = None


async def reviewers_of(
    graphiti: Graphiti,
    query_text: str,
    *,
    group_id: str | None = None,
    num_results: int = 10,
) -> list[ReviewerHit]:
    """People connected to work matching `query_text` (search first, Cypher fallback)."""
    group_ids = [group_id] if group_id else None
    try:
        edges = await graphiti.search(query_text, group_ids=group_ids, num_results=num_results)
        hits = await _edges_to_hits(graphiti, edges)
        if hits:
            return hits
    except Exception:  # noqa: BLE001 - fall back to the deterministic Cypher walk
        pass
    return await _cypher_fallback(graphiti, query_text, group_id=group_id, limit=num_results)


async def _edges_to_hits(graphiti: Graphiti, edges: list[Any]) -> list[ReviewerHit]:
    from graphiti_core.nodes import EntityNode

    hits: list[ReviewerHit] = []
    for edge in edges:
        for node_uuid in (edge.source_node_uuid, edge.target_node_uuid):
            node = await EntityNode.get_by_uuid(graphiti.driver, node_uuid)
            if "Person" not in (node.labels or []):
                continue
            hits.append(
                ReviewerHit(
                    name=node.name,
                    relation=str(getattr(edge, "name", "") or ""),
                    fact=str(getattr(edge, "fact", "") or ""),
                    episodes=[str(e) for e in (getattr(edge, "episodes", None) or [])],
                    profile_url=_json_attr(getattr(node, "attributes", None)),
                )
            )
    return hits


async def _cypher_fallback(
    graphiti: Graphiti,
    query_text: str,
    *,
    group_id: str | None,
    limit: int,
) -> list[ReviewerHit]:
    cypher = """
        MATCH (person:Entity)-[rel:RELATES_TO]->(work:Entity)
        WHERE rel.name IN ['REVIEWED', 'AUTHORED']
          AND ($group_id IS NULL OR rel.group_id = $group_id)
          AND (
            toLower(rel.fact) CONTAINS toLower($q)
            OR toLower(work.name) CONTAINS toLower($q)
            OR toLower(coalesce(work.summary, '')) CONTAINS toLower($q)
          )
        RETURN person.name AS name, person.attributes AS attrs,
               rel.name AS relation, rel.fact AS fact, rel.episodes AS episodes
        LIMIT $limit
    """
    try:
        records, _, _ = await graphiti.driver.execute_query(
            cypher, q=query_text, group_id=group_id, limit=limit
        )
    except Exception:  # noqa: BLE001 - schema/label mismatch yields no deterministic hits
        return []
    hits: list[ReviewerHit] = []
    for record in records:
        hits.append(
            ReviewerHit(
                name=str(record.get("name", "")),
                relation=str(record.get("relation", "")),
                fact=str(record.get("fact") or ""),
                episodes=[str(e) for e in (record.get("episodes") or [])],
                profile_url=_json_attr(record.get("attrs")),
            )
        )
    return hits


def _json_attr(attrs: Any, key: str = "profile_url") -> str | None:
    """Read `key` from a Graphiti node `attributes` value (a dict or JSON string)."""
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            return None
    if isinstance(attrs, dict):
        value = attrs.get(key)
        return value if isinstance(value, str) else None
    return None
