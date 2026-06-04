"""Recall: query the memory graph and return facts with their sources.

A generalization of ``graph.queries.reviewers_of``: instead of filtering to
people, recall returns the relevant facts (edges) for a query, each carrying the
source episodes it came from. Built on ``graphiti.search`` (the portable API) so
it rides the Kuzu to FalkorDB migration without changes. Provenance resolution
is best-effort and never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphiti_core import Graphiti


@dataclass(slots=True)
class RecalledFact:
    """A fact recalled from the graph, with the episodes that support it."""

    fact: str
    relation: str
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecallAnswer:
    """The result of a recall query: ordered facts, each with its sources."""

    query: str
    facts: list[RecalledFact] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.facts


async def recall(
    graphiti: Graphiti,
    query: str,
    *,
    group_id: str | None = None,
    num_results: int = 10,
) -> RecallAnswer:
    """Return facts matching ``query`` with their source episodes. Never raises."""
    group_ids = [group_id] if group_id else None
    try:
        edges = await graphiti.search(query, group_ids=group_ids, num_results=num_results)
    except Exception:  # noqa: BLE001 - search may fail if Kuzu FTS is unavailable
        return RecallAnswer(query=query)

    facts: list[RecalledFact] = []
    for edge in edges:
        text = str(getattr(edge, "fact", "") or "").strip()
        if not text:
            continue
        episodes = getattr(edge, "episodes", None) or []
        facts.append(
            RecalledFact(
                fact=text,
                relation=str(getattr(edge, "name", "") or ""),
                sources=await _resolve_sources(graphiti, episodes),
            )
        )
    return RecallAnswer(query=query, facts=facts)


async def _resolve_sources(graphiti: Graphiti, episode_uuids: list[Any]) -> list[str]:
    """Resolve episode uuids to human labels (episode names), best-effort.

    Falls back to the uuid string when a lookup fails, so provenance degrades
    rather than disappears. Order is preserved and duplicates are dropped.
    """
    from graphiti_core.nodes import EpisodicNode

    labels: list[str] = []
    seen: set[str] = set()
    for uuid in episode_uuids:
        key = str(uuid)
        try:
            episode = await EpisodicNode.get_by_uuid(graphiti.driver, key)
            label = episode.name or key
        except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
            label = key
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def format_answer(answer: RecallAnswer) -> str:
    """Render a recall answer as a cited, human-readable block."""
    if answer.is_empty:
        return f'No memory found for: "{answer.query}"'
    lines = [f'Memory for "{answer.query}":', ""]
    for item in answer.facts:
        lines.append(f"- {item.fact}")
        if item.sources:
            lines.append(f"  sources: {', '.join(item.sources)}")
    return "\n".join(lines)
