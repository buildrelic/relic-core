"""The Memory seam: a typed port over Graphiti (ADR-0003).

Production graph code (recall, queries, load, cli) depends on the ``MemoryReader`` /
``MemoryWriter`` Protocols and the relic value types below, never on a raw ``Graphiti``
handle. ``GraphitiMemory`` is the one adapter that holds the handle privately and is the
only place that reads Graphiti object shapes (``edge.fact``, ``node.labels``) or touches
``.driver``. Search edges arrive with their endpoints already *resolved* into
``MemoryEntity`` values, so callers never look a node up by uuid.

Eval tools (``eval/graph_stats.py``) stay out of the typed seam: they reach the graph
through the explicit ``execute_read`` escape hatch, which is documented as
introspection-only — production code uses the typed reader methods.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, LiteralString, Protocol

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from relic.obs import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from graphiti_core import Graphiti

    from relic.contracts import EpisodeSpec

log = get_logger("memory")

# OpenAI/Gemini rate-limit backoff for the write path. Graphiti maps a 429 to
# RateLimitError and does not retry it, so the backoff lives in the adapter. Module-level
# so tests can swap the wait/attempt count for a fast, deterministic run.
_RETRY_WAIT = wait_random_exponential(multiplier=2, max=60)
_RETRY_ATTEMPTS = 6


# --- Value types that cross the seam (no Graphiti objects past here) ----------


@dataclass(slots=True, frozen=True)
class MemoryEntity:
    """A node: the resolved endpoint of an edge, or a looked-up entity."""

    uuid: str
    name: str
    labels: list[str]
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class MemoryEdge:
    """A fact: a relation between two entities, with the episodes that support it.

    ``source`` and ``target`` are *resolved* — full entities, not bare uuids — so callers
    read ``edge.source.labels`` without a second lookup.
    """

    relation: str
    fact: str
    source: MemoryEntity
    target: MemoryEntity
    episode_uuids: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class MemoryEpisode:
    """A source episode: its name (a citation label) and verbatim content."""

    uuid: str
    name: str
    content: str | None


# --- The two interfaces -------------------------------------------------------


class MemoryReader(Protocol):
    """Read side of the memory seam. recall/queries depend on this, not on Graphiti."""

    async def search(
        self, query: str, *, group_ids: list[str] | None = None, num_results: int = 10
    ) -> list[MemoryEdge]: ...

    async def get_episode(self, uuid: str) -> MemoryEpisode | None: ...

    async def reviewer_walk(
        self, query: str, *, group_id: str | None, limit: int
    ) -> list[MemoryEdge]: ...


class MemoryWriter(Protocol):
    """Write side of the memory seam. load depends on this, not on Graphiti."""

    async def add_episode(self, spec: EpisodeSpec) -> None: ...

    async def add_episode_bulk(self, specs: list[EpisodeSpec]) -> None: ...

    async def build_indices(self) -> None: ...


# --- Helpers ------------------------------------------------------------------


def _parse_attrs(attrs: Any) -> dict[str, Any]:
    """Normalize a Graphiti node ``attributes`` value (a dict or a JSON string) to a dict."""
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            return {}
    return dict(attrs) if isinstance(attrs, dict) else {}


def _clean_labels(labels: Any) -> list[str]:
    """The entity's ontology labels, dropping Graphiti's structural ``Entity`` marker."""
    return [str(x) for x in (labels or []) if x != "Entity"]


# Deterministic Cypher walk over REVIEWED/AUTHORED edges: the fallback when hybrid search
# comes back empty. Held inside the adapter because it touches the storage schema.
_REVIEWER_WALK_CYPHER: LiteralString = """
    MATCH (person:Entity)-[rel:RELATES_TO]->(work:Entity)
    WHERE rel.name IN ['REVIEWED', 'AUTHORED']
      AND ($group_id IS NULL OR rel.group_id = $group_id)
      AND (
        toLower(rel.fact) CONTAINS toLower($q)
        OR toLower(work.name) CONTAINS toLower($q)
        OR toLower(coalesce(work.summary, '')) CONTAINS toLower($q)
      )
    RETURN person.uuid AS s_uuid, person.name AS s_name, labels(person) AS s_labels,
           person.attributes AS s_attrs,
           work.uuid AS t_uuid, work.name AS t_name, labels(work) AS t_labels,
           work.attributes AS t_attrs,
           rel.name AS relation, rel.fact AS fact, rel.episodes AS episodes
    LIMIT $limit
"""


# --- The Graphiti adapter -----------------------------------------------------


class GraphitiMemory:
    """The one production adapter: satisfies MemoryReader and MemoryWriter over Graphiti.

    Holds the raw ``Graphiti`` privately. Every Graphiti object read and the rate-limit
    backoff live here, so a graphiti-core bump touches this file alone.
    """

    def __init__(self, graphiti: Graphiti) -> None:
        self._graphiti = graphiti

    # -- reader --

    async def search(
        self, query: str, *, group_ids: list[str] | None = None, num_results: int = 10
    ) -> list[MemoryEdge]:
        edges = await self._graphiti.search(query, group_ids=group_ids, num_results=num_results)
        return [await self._to_memory_edge(edge) for edge in edges]

    async def get_episode(self, uuid: str) -> MemoryEpisode | None:
        from graphiti_core.nodes import EpisodicNode

        try:
            episode = await EpisodicNode.get_by_uuid(self._graphiti.driver, uuid)
        except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
            return None
        return MemoryEpisode(
            uuid=str(episode.uuid), name=episode.name or "", content=episode.content
        )

    async def reviewer_walk(
        self, query: str, *, group_id: str | None, limit: int
    ) -> list[MemoryEdge]:
        try:
            records = await self.execute_read(
                _REVIEWER_WALK_CYPHER, q=query, group_id=group_id, limit=limit
            )
        except Exception:  # noqa: BLE001 - a schema/label mismatch yields no deterministic hits
            return []
        return [self._record_to_edge(record) for record in records]

    async def _to_memory_edge(self, edge: Any) -> MemoryEdge:
        """Turn a Graphiti search edge into a MemoryEdge, resolving both endpoints."""
        source = await self._resolve(edge.source_node_uuid)
        target = await self._resolve(edge.target_node_uuid)
        return MemoryEdge(
            relation=str(getattr(edge, "name", "") or ""),
            fact=str(getattr(edge, "fact", "") or ""),
            source=source,
            target=target,
            episode_uuids=[str(e) for e in (getattr(edge, "episodes", None) or [])],
        )

    async def _resolve(self, uuid: Any) -> MemoryEntity:
        from graphiti_core.nodes import EntityNode

        node = await EntityNode.get_by_uuid(self._graphiti.driver, uuid)
        return MemoryEntity(
            uuid=str(node.uuid),
            name=node.name or "",
            labels=_clean_labels(node.labels),
            attributes=_parse_attrs(getattr(node, "attributes", None)),
        )

    @staticmethod
    def _record_to_edge(record: dict[str, Any]) -> MemoryEdge:
        return MemoryEdge(
            relation=str(record.get("relation", "")),
            fact=str(record.get("fact") or ""),
            source=MemoryEntity(
                uuid=str(record.get("s_uuid", "")),
                name=str(record.get("s_name") or ""),
                labels=_clean_labels(record.get("s_labels")),
                attributes=_parse_attrs(record.get("s_attrs")),
            ),
            target=MemoryEntity(
                uuid=str(record.get("t_uuid", "")),
                name=str(record.get("t_name") or ""),
                labels=_clean_labels(record.get("t_labels")),
                attributes=_parse_attrs(record.get("t_attrs")),
            ),
            episode_uuids=[str(e) for e in (record.get("episodes") or [])],
        )

    # -- writer --

    async def add_episode(self, spec: EpisodeSpec) -> None:
        from graphiti_core.nodes import EpisodeType

        from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

        await self._with_backoff(
            lambda: self._graphiti.add_episode(
                name=spec.name,
                episode_body=spec.body,
                source_description=spec.source_description,
                reference_time=spec.reference_time,
                source=EpisodeType.json,
                group_id=spec.group_id,
                entity_types=ENTITY_TYPES,
                edge_types=EDGE_TYPES,
                edge_type_map=EDGE_TYPE_MAP,
            ),
            label=spec.name,
        )

    async def add_episode_bulk(self, specs: list[EpisodeSpec]) -> None:
        """Add one batch of episodes (all sharing a group_id) via Graphiti's bulk path."""
        from graphiti_core.nodes import EpisodeType
        from graphiti_core.utils.bulk_utils import RawEpisode

        from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

        if not specs:
            return
        raws = [
            RawEpisode(
                name=spec.name,
                content=spec.body,
                source_description=spec.source_description,
                source=EpisodeType.json,
                reference_time=spec.reference_time,
            )
            for spec in specs
        ]
        group_id = specs[0].group_id
        await self._with_backoff(
            lambda: self._graphiti.add_episode_bulk(
                raws,
                group_id=group_id,
                entity_types=ENTITY_TYPES,
                edge_types=EDGE_TYPES,
                edge_type_map=EDGE_TYPE_MAP,
            ),
            label=f"bulk[{group_id}]",
        )

    async def build_indices(self) -> None:
        await self._graphiti.build_indices_and_constraints()

    async def _with_backoff(self, make_awaitable: Callable[[], Awaitable], *, label: str) -> object:
        """Await a graphiti write, retrying OpenAI/Gemini rate limits with exponential backoff.

        ``make_awaitable`` is a factory (not a coroutine) so each attempt gets a fresh
        awaitable. Any non-rate-limit exception propagates on the first raise, so genuine
        failures are not masked; the load loop above the seam catches the raise for stats.
        """
        from graphiti_core.llm_client.errors import RateLimitError

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RateLimitError),
            wait=_RETRY_WAIT,
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            reraise=True,
            before_sleep=before_sleep_log(log, logging.WARNING),
        ):
            with attempt:
                return await make_awaitable()
        raise AssertionError(f"unreachable: AsyncRetrying for {label} returned no attempts")

    # -- eval escape hatch (introspection only; production uses the typed reader) --

    async def execute_read(self, cypher: LiteralString, **params: Any) -> list[dict[str, Any]]:
        """Run a read-only Cypher query and return rows as dicts.

        Reserved for measurement tools (eval/graph_stats.py); production code must use the
        typed reader methods so a Graphiti swap stays contained to this adapter.
        """
        records, _, _ = await self._graphiti.driver.execute_query(cypher, **params)
        return [dict(record) for record in records]

    async def close(self) -> None:
        await self._graphiti.close()


def open_memory(
    *,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    database: str | None = None,
    api_key: str | None = None,
    max_coroutines: int | None = None,
) -> GraphitiMemory:
    """Build the configured Graphiti and wrap it in the Memory adapter.

    The construction (LLM provider selection + the FalkorDB driver + the 0.29.x
    monkeypatches) currently lives in ``relic.graph.engram.make_engram``; this is the only
    place outside the adapter that still touches it, and it folds in once engram retires.
    """
    from relic.graph.engram import make_engram

    return GraphitiMemory(
        make_engram(
            host=host,
            port=port,
            password=password,
            database=database,
            api_key=api_key,
            max_coroutines=max_coroutines,
        )
    )
