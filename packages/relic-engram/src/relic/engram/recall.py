"""Recall: query the engram and return facts with their sources.

The answer shapes and the rendered block are carried over verbatim from the graph
era (ADR-0007): ``format_answer``'s output is a seam -- the daemon injects it onto
prompts and the MCP ``recall_memory`` tool returns it -- so its format must not
drift. What changed is the engine: a fact is now a memory row surfaced by Postgres
full-text search, its "fact" text the row's title plus a query-focused headline
snippet, and its single source the row's own name and citation URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relic.engram.store import EngramReader


@dataclass(slots=True)
class Source:
    """Where a recalled fact came from: a memory label and, if known, its URL."""

    label: str
    url: str | None = None


@dataclass(slots=True)
class RecalledFact:
    """A fact recalled from the engram, with the memories that support it."""

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


async def recall(
    memory: EngramReader,
    query: str,
    *,
    scope: str | None = None,
    num_results: int = 10,
) -> RecallAnswer:
    """Return facts matching ``query`` with their sources. Never raises.

    ``scope`` narrows the search to one ingest scope (a repo slug, a Granola owner,
    a Notion workspace); ``None`` searches the whole workspace. The store already
    binds every read to its workspace, so an unscoped read is workspace-wide, never
    tenant-wide.
    """
    try:
        hits = await memory.search(query, scope=scope, num_results=num_results)
    except Exception:  # noqa: BLE001 - search may fail if the store is unavailable
        return RecallAnswer(query=query)

    facts: list[RecalledFact] = []
    for hit in hits:
        snippet = hit.snippet.strip()
        text = f"{hit.title} — {snippet}" if snippet and snippet != hit.title else hit.title
        if not text.strip():
            continue
        facts.append(
            RecalledFact(
                fact=text,
                relation=hit.artifact_type,
                sources=[Source(label=hit.name, url=hit.url)],
            )
        )
    return RecallAnswer(query=query, facts=facts)


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
