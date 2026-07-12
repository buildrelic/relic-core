"""Recall: query the memory graph and return facts with their sources.

A generalization of ``graph.queries.reviewers_of``: instead of filtering to
people, recall returns the relevant facts (edges) for a query, each carrying the
source episodes it came from. Built on ``graphiti.search`` (the portable,
backend-agnostic API). Provenance resolution is best-effort and never raises.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relic.graph.memory import MemoryReader


@dataclass(slots=True)
class Source:
    """Where a recalled fact came from: an episode label and, if known, its URL."""

    label: str
    url: str | None = None


@dataclass(slots=True)
class RecalledFact:
    """A fact recalled from the graph, with the episodes that support it."""

    fact: str
    relation: str
    sources: list[Source] = field(default_factory=list)


@dataclass(slots=True)
class RecallAnswer:
    """The result of a recall query: ordered facts, each with its sources."""

    query: str
    facts: list[RecalledFact] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.facts


def _scope_filter(zones: Collection[str] | None, group_id: str | None) -> list[str] | None:
    """Resolve the group_id search filter from the caller's Zone scope.

    ``zones`` is the ADR-0005 principal scope and takes precedence: an explicit empty
    collection means "no accessible Zones" and is returned as ``[]`` so recall can fail
    closed. ``group_id`` is the legacy single-scope shim. ``None`` from both means an
    unscoped read (trusted, single-tenant callers only).
    """
    if zones is not None:
        return list(zones)
    if group_id is not None:
        return [group_id]
    return None


async def recall(
    memory: MemoryReader,
    query: str,
    *,
    zones: Collection[str] | None = None,
    group_id: str | None = None,
    num_results: int = 10,
    include_superseded: bool = False,
) -> RecallAnswer:
    """Return facts matching ``query`` with their source episodes. Never raises.

    Recall is scoped to the caller's accessible Zones (``zones``). Per ADR-0005 this
    fails closed: an explicit empty Zone-set sees nothing, and ``search`` is never
    consulted. ``group_id`` is the legacy single-scope alias; passing neither is an
    unscoped read reserved for trusted single-tenant callers.

    Per ADR-0006 recall returns only **current** facts: a fact a later episode
    invalidated or superseded is dropped, so a grounded answer never surfaces a stale
    fact as if it were true. Set ``include_superseded`` to keep them (history views).
    """
    group_ids = _scope_filter(zones, group_id)
    if group_ids is not None and not group_ids:
        # Fail closed: a principal with no accessible Zones sees nothing.
        return RecallAnswer(query=query)
    try:
        edges = await memory.search(query, group_ids=group_ids, num_results=num_results)
    except Exception:  # noqa: BLE001 - search may fail if the memory backend is unavailable
        return RecallAnswer(query=query)

    facts: list[RecalledFact] = []
    for edge in edges:
        if not include_superseded and not edge.is_current:
            continue  # ADR-0006: never surface a superseded fact as current
        text = edge.fact.strip()
        if not text:
            continue
        facts.append(
            RecalledFact(
                fact=text,
                relation=edge.relation,
                sources=await _resolve_sources(memory, edge.episode_uuids),
            )
        )
    return RecallAnswer(query=query, facts=facts)


async def _resolve_sources(memory: MemoryReader, episode_uuids: list[Any]) -> list[Source]:
    """Resolve episode uuids to sources (episode name plus source URL), best-effort.

    Falls back to the uuid string when the episode is unknown, so provenance degrades
    rather than disappears. Order is preserved and duplicate labels are dropped.
    """
    sources: list[Source] = []
    seen: set[str] = set()
    for uuid in episode_uuids:
        key = str(uuid)
        episode = await memory.get_episode(key)
        if episode is not None:
            source = Source(label=episode.name or key, url=_extract_url(episode.content))
        else:
            source = Source(label=key)
        if source.label not in seen:
            seen.add(source.label)
            sources.append(source)
    return sources


def _extract_url(content: Any) -> str | None:
    """Pull the canonical source URL out of an episode body, if present.

    The ingest mappers store the PR or issue under those keys with a ``url``
    field. The repo URL is intentionally ignored: it is not the source of the
    fact.
    """
    if not isinstance(content, str):
        return None
    try:
        body = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("pull_request", "issue"):
        section = body.get(key)
        if isinstance(section, dict) and isinstance(section.get("url"), str):
            return section["url"]
    top = body.get("url")
    return top if isinstance(top, str) else None


def format_answer(answer: RecallAnswer) -> str:
    """Render a recall answer as a cited, human-readable block."""
    if answer.is_empty:
        return f'No memory found for: "{answer.query}"'
    lines = [f'Memory for "{answer.query}":', ""]
    for item in answer.facts:
        lines.append(f"- {item.fact}")
        for source in item.sources:
            suffix = f" ({source.url})" if source.url else ""
            lines.append(f"  source: {source.label}{suffix}")
    return "\n".join(lines)
