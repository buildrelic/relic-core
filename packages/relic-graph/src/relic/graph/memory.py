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
    from datetime import datetime

    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient

    from relic.contracts import EpisodeSpec

    # The provider-client triple _build_graphiti hands to Graphiti.
    _ClientTriple = tuple[LLMClient, EmbedderClient, CrossEncoderClient]

log = get_logger("memory")

# OpenAI/Gemini rate-limit backoff for the write path. Graphiti maps a 429 to
# RateLimitError and does not retry it, so the backoff lives in the adapter. Module-level
# so tests can swap the wait/attempt count for a fast, deterministic run.
_RETRY_WAIT = wait_random_exponential(multiplier=2, max=60)
_RETRY_ATTEMPTS = 6


# --- Value types that cross the seam (no Graphiti objects past here) ----------


@dataclass(slots=True, frozen=True)
class MemoryEntity:
    """A node: the resolved endpoint of an edge, or a looked-up entity.

    ``group_id`` is the node's Zone (ADR-0005); ``None`` for a global-tier node
    (Person/Repo/Label/File), which is Zone-exempt. Zone enforcement keys on it.
    """

    uuid: str
    name: str
    labels: list[str]
    attributes: dict[str, Any] = field(default_factory=dict)
    group_id: str | None = None


@dataclass(slots=True, frozen=True)
class MemoryEdge:
    """A fact: a relation between two entities, with the episodes that support it.

    ``source`` and ``target`` are *resolved* — full entities, not bare uuids — so callers
    read ``edge.source.labels`` without a second lookup. ``group_id`` is the fact's Zone.

    Facts are bi-temporal (ADR-0006): ``valid_at``/``invalid_at`` bound when the fact held
    in the world, ``expired_at`` when a later episode contradicted it. A fact is *current*
    when both ``invalid_at`` and ``expired_at`` are ``None``; recall returns current facts
    by default, never silently surfacing a superseded one.
    """

    relation: str
    fact: str
    source: MemoryEntity
    target: MemoryEntity
    episode_uuids: list[str] = field(default_factory=list)
    group_id: str | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    expired_at: datetime | None = None

    @property
    def is_current(self) -> bool:
        """True if this fact has not been invalidated or superseded by a later one."""
        return self.invalid_at is None and self.expired_at is None


@dataclass(slots=True, frozen=True)
class MemoryEpisode:
    """A source episode: its name (a citation label), verbatim content, and Zone.

    ``group_id`` is the episode's Zone, so a scoped read can drop an episode the
    requester may not see rather than leak its content as provenance.
    """

    uuid: str
    name: str
    content: str | None
    group_id: str | None = None


# --- The two interfaces -------------------------------------------------------


class MemoryReader(Protocol):
    """Read side of the memory seam. recall/queries depend on this, not on Graphiti."""

    async def search(
        self, query: str, *, group_ids: list[str] | None = None, num_results: int = 10
    ) -> list[MemoryEdge]: ...

    async def get_episode(self, uuid: str) -> MemoryEpisode | None: ...

    async def get_entity(self, uuid: str) -> MemoryEntity | None: ...

    async def reviewer_walk(
        self, query: str, *, group_id: str | None, limit: int
    ) -> list[MemoryEdge]: ...


class MemoryWriter(Protocol):
    """Write side of the memory seam. load depends on this, not on Graphiti."""

    async def add_episode(self, spec: EpisodeSpec) -> None: ...

    async def add_episode_bulk(self, specs: list[EpisodeSpec]) -> None: ...

    async def build_indices(self) -> None: ...

    async def supersede_episode(self, name: str, group_id: str) -> int: ...


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


# --- The scoped read view -----------------------------------------------------


@dataclass(slots=True, frozen=True)
class ScopedMemory:
    """A principal-scoped read view over a ``MemoryReader`` (ADR-0006 Decision 2).

    Closes over the principal's accessible Zone-set and applies it to *every* read, so a
    serving surface that only ever holds a ``ScopedMemory`` cannot leak across Zones by
    forgetting a filter -- the unsafe path is removed, not documented. An empty Zone-set
    sees nothing (fail closed). Out-of-Zone nodes read as **nonexistent** -- no error, no
    redaction marker (ADR-0005 Decision 5), so the requester never learns they exist.

    Global-tier nodes (Person/Repo/Label/File) are Zone-exempt: a tenant member may see
    *that* a person or repo exists; only their Zoned activity is gated (ADR-0005 Decision
    6). Construct this from the principal's grants at the composition root and hand serve
    nothing else; the unscoped ``GraphitiMemory`` stays for ingest/eval/admin only.
    """

    inner: MemoryReader
    zones: frozenset[str]

    async def search(
        self, query: str, *, group_ids: list[str] | None = None, num_results: int = 10
    ) -> list[MemoryEdge]:
        # The scope is the principal's Zones, always; a caller-supplied ``group_ids`` can
        # only narrow within it, never widen it. Empty scope -> see nothing.
        scoped = self.zones if group_ids is None else self.zones.intersection(group_ids)
        if not scoped:
            return []
        return await self.inner.search(query, group_ids=sorted(scoped), num_results=num_results)

    async def get_episode(self, uuid: str) -> MemoryEpisode | None:
        episode = await self.inner.get_episode(uuid)
        if episode is None or not self._in_scope(episode.group_id):
            return None
        return episode

    async def get_entity(self, uuid: str) -> MemoryEntity | None:
        from relic.graph.schema import is_global_entity

        entity = await self.inner.get_entity(uuid)
        if entity is None:
            return None
        if is_global_entity(entity.labels):
            return entity  # the identity spine is tenant-public
        if not self._in_scope(entity.group_id):
            return None  # out-of-Zone reads as nonexistent
        return entity

    async def reviewer_walk(
        self, query: str, *, group_id: str | None, limit: int
    ) -> list[MemoryEdge]:
        # The deterministic fallback honors the scope too: an out-of-scope ``group_id``
        # yields nothing, and an unscoped walk fans over the principal's Zones.
        if not self.zones or (group_id is not None and group_id not in self.zones):
            return []
        targets = [group_id] if group_id is not None else sorted(self.zones)
        hits: list[MemoryEdge] = []
        for zone in targets:
            hits.extend(await self.inner.reviewer_walk(query, group_id=zone, limit=limit))
        return hits[:limit]

    def _in_scope(self, group_id: str | None) -> bool:
        """A Zoned read is in scope only when its Zone is one the principal holds."""
        return group_id is not None and group_id in self.zones


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
            uuid=str(episode.uuid),
            name=episode.name or "",
            content=episode.content,
            group_id=getattr(episode, "group_id", None),
        )

    async def get_entity(self, uuid: str) -> MemoryEntity | None:
        """Resolve a single node by uuid, or ``None`` if it does not exist.

        The unscoped lookup: it returns the node regardless of Zone. Zone enforcement is
        the job of ``ScopedMemory.get_entity``, which wraps this and hides out-of-Zone
        nodes -- keep the boundary in one place rather than duplicated per call site.
        """
        try:
            return await self._resolve(uuid)
        except Exception:  # noqa: BLE001 - a missing node reads as nonexistent, never fatal
            return None

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
            group_id=getattr(edge, "group_id", None),
            valid_at=getattr(edge, "valid_at", None),
            invalid_at=getattr(edge, "invalid_at", None),
            expired_at=getattr(edge, "expired_at", None),
        )

    async def _resolve(self, uuid: Any) -> MemoryEntity:
        from graphiti_core.nodes import EntityNode

        node = await EntityNode.get_by_uuid(self._graphiti.driver, uuid)
        return MemoryEntity(
            uuid=str(node.uuid),
            name=node.name or "",
            labels=_clean_labels(node.labels),
            attributes=_parse_attrs(getattr(node, "attributes", None)),
            group_id=getattr(node, "group_id", None),
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

        from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, require_episode_zone

        # Fail closed before any write: a Zoned episode with no Zone is never persisted.
        zone = require_episode_zone(spec.group_id)
        await self._with_backoff(
            lambda: self._graphiti.add_episode(
                name=spec.name,
                episode_body=spec.body,
                source_description=spec.source_description,
                reference_time=spec.reference_time,
                source=EpisodeType.json,
                group_id=zone,
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

        from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, require_episode_zone

        if not specs:
            return
        # Fail closed before any write: every episode in the batch must carry a Zone.
        zones = [require_episode_zone(spec.group_id) for spec in specs]
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
        group_id = zones[0]
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

    async def supersede_episode(self, name: str, group_id: str) -> int:
        """Remove the prior episode(s) for ``(name, group_id)`` so a re-add lands in place.

        Re-adding alone would fork: ``add_episode`` mints a fresh Episodic uuid per call, so
        the stale node and its facts would linger. ``remove_episode`` is the cascade, but on
        its own it over-deletes: it drops every edge the removed episode *originated*
        (``episodes[0]``), even a fact another episode also supports. So first prune the
        superseded episode out of any edge more than one episode supports -- persisting *only*
        ``e.episodes`` with a targeted update, never ``EntityEdge.save`` (which rewrites the
        unloaded ``fact_embedding`` to NULL and would break vector recall of the rescued
        fact). ``remove_episode`` then deletes only what this episode solely owned: edges it
        still originates and entities only it mentions. The prune must be persisted *before*
        ``remove_episode``, which re-reads ``episodes[0]`` from the graph. Resolving by
        name+group_id also cleans up any pre-existing duplicate. Returns the count removed.

        Touches ``.driver`` and ``remove_episode`` directly: that is exactly why supersession
        lives behind the seam, so the load loop never sees a raw Graphiti handle.
        """
        found, _, _ = await self._graphiti.driver.execute_query(
            "MATCH (e:Episodic {name: $name, group_id: $group_id}) "
            "RETURN e.uuid AS uuid, e.entity_edges AS edge_uuids",
            name=name,
            group_id=group_id,
            routing_="r",
        )
        for record in found:
            uuid = record["uuid"]
            edge_uuids = record.get("edge_uuids") or []
            if edge_uuids:
                edges, _, _ = await self._graphiti.driver.execute_query(
                    "MATCH (n:Entity)-[r:RELATES_TO]->(m:Entity) WHERE r.uuid IN $uuids "
                    "RETURN r.uuid AS uuid, r.episodes AS episodes",
                    uuids=edge_uuids,
                    routing_="r",
                )
                for edge in edges:
                    episodes = edge.get("episodes") or []
                    # Only rescue a corroborated fact; a sole-supporter edge is left for
                    # remove_episode to delete (its episodes[0] is still this episode).
                    if uuid in episodes and len(episodes) > 1:
                        await self._graphiti.driver.execute_query(
                            "MATCH (n:Entity)-[r:RELATES_TO {uuid: $uuid}]->(m:Entity) "
                            "SET r.episodes = $episodes",
                            uuid=edge["uuid"],
                            episodes=[e for e in episodes if e != uuid],
                        )
            await self._graphiti.remove_episode(uuid)
        return len(found)

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

    The construction -- LLM provider selection + the FalkorDB driver + the 0.29.x
    monkeypatches -- lives in this module (``_build_graphiti``), the only place that imports
    graphiti_core.
    """
    return GraphitiMemory(
        _build_graphiti(
            host=host,
            port=port,
            password=password,
            database=database,
            api_key=api_key,
            max_coroutines=max_coroutines,
        )
    )


# --- Graphiti construction: provider selection, the driver, the 0.29.x workarounds ----
#
# Everything below builds the raw ``Graphiti`` and patches graphiti-core. It used to live
# in ``relic.graph.engram`` (now retired, folded behind ``open_memory``). graphiti_core is
# imported lazily inside the functions so ``relic --help`` stays key-free and import-light.


_falkordb_patched = False


def _patch_falkordb_empty_query() -> None:
    """Work around a graphiti-core <=0.29.1 bug on the FalkorDB fulltext builders.

    When an extracted entity's name sanitizes to nothing (all punctuation or
    stopwords, common in real history: version tags, file paths, single symbols),
    the builders emit `(@group_id:"x") ()` with empty trailing parens. RediSearch
    rejects that with a syntax error, aborting `add_episode`. Graphiti already
    treats an empty string as "skip the fulltext search", so we make the builders
    return '' when the text portion is empty. Remove this once upstream guards it.
    """
    global _falkordb_patched
    if _falkordb_patched:
        return

    import re

    from graphiti_core.driver import falkordb_driver
    from graphiti_core.driver.falkordb.operations import search_ops

    _empty_parens = re.compile(r"\(\s*\)\s*$")
    _group_filter = re.compile(r"\(@group_id:[^)]+\)")

    def _guard(fn):
        def wrapper(*args, **kwargs):
            out = fn(*args, **kwargs)
            if not isinstance(out, str):
                return out
            if _empty_parens.search(out):
                return ""
            # RediSearch treats - as a negation operator even inside quotes, so we must
            # escape hyphens in group_ids with backslashes. We do this on the final query
            # string to avoid failing upstream group_id string character validation.
            return _group_filter.sub(lambda m: m.group(0).replace("-", "\\-"), out)

        return wrapper

    search_ops._build_falkor_fulltext_query = _guard(search_ops._build_falkor_fulltext_query)
    falkordb_driver.FalkorDriver.build_fulltext_query = _guard(
        falkordb_driver.FalkorDriver.build_fulltext_query
    )
    _falkordb_patched = True


_prompt_json_patched = False

# Prompt modules that import `to_prompt_json` by name (so they hold their own reference
# and must be rebound individually). Sourced from graphiti-core 0.29.1.
_PROMPT_JSON_MODULES = (
    "extract_nodes",
    "extract_edges",
    "extract_nodes_and_edges",
    "dedupe_nodes",
    "summarize_nodes",
    "eval",
)


def _patch_prompt_json_datetime() -> None:
    """Make graphiti's prompt JSON serializer tolerate ``datetime`` values.

    graphiti's ``to_prompt_json`` calls ``json.dumps`` without a ``default`` handler. The
    bulk summary/dedup path feeds it node dicts that carry ``datetime`` fields (e.g.
    ``created_at``, ``valid_at``), so an ``add_episode_bulk`` batch raises ``TypeError:
    Object of type datetime is not JSON serializable`` and falls back to slow per-episode
    loading. This shows up with the Gemini provider, which populates those timestamps
    where OpenAI often leaves them null. The crash is at
    ``extract_nodes.extract_summaries_batch`` -> ``to_prompt_json(context['entities'])``.

    ``to_prompt_json`` is prompt-only (never a DB write), so ``default=str`` (ISO-ish text
    for the LLM) is safe. It is imported by name into several prompt modules, so we rebind
    it in each. Remove once upstream adds a default encoder.
    """
    global _prompt_json_patched
    if _prompt_json_patched:
        return

    import importlib

    from graphiti_core.prompts import prompt_helpers

    def _safe_to_prompt_json(data, ensure_ascii=False, indent=None):  # type: ignore[no-untyped-def]
        return json.dumps(data, ensure_ascii=ensure_ascii, indent=indent, default=str)

    setattr(prompt_helpers, "to_prompt_json", _safe_to_prompt_json)  # noqa: B010
    for module_name in _PROMPT_JSON_MODULES:
        try:
            module = importlib.import_module(f"graphiti_core.prompts.{module_name}")
        except Exception:  # noqa: BLE001 - a renamed/removed module just means nothing to patch
            continue
        if getattr(module, "to_prompt_json", None) is not None:
            setattr(module, "to_prompt_json", _safe_to_prompt_json)  # noqa: B010
    _prompt_json_patched = True


# LLM provider selection. Graphiti takes an llm_client, an embedder, and a cross_encoder.
# "openai" (default) uses OpenAI for all three; "gemini" is hybrid -- Gemini Flash for the
# LLM (its TPM ceiling is ~20x OpenAI's low tiers, the throughput win for a big backfill),
# OpenAI for embeddings/reranking -- so it needs both keys. Switching the embedder would
# change vector dimensions and break similarity search, so a provider switch is a
# fresh-graph backfill (`relic ingest --fresh`).


def _openai_embedder(api_key: str):  # noqa: ANN202 - graphiti embedder, kept local
    """OpenAI embedder (text-embedding-3-small). Shared by both providers."""
    from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig

    return OpenAIEmbedder(
        OpenAIEmbedderConfig(api_key=api_key, embedding_model="text-embedding-3-small")
    )


def _openai_reranker(api_key: str):  # noqa: ANN202 - graphiti cross-encoder, kept local
    """OpenAI cross-encoder reranker. Shared by both providers (recall path)."""
    from graphiti_core.cross_encoder import OpenAIRerankerClient
    from graphiti_core.llm_client import LLMConfig

    return OpenAIRerankerClient(config=LLMConfig(api_key=api_key, model="gpt-4o-mini"))


def _openai_clients(api_key: str) -> _ClientTriple:
    """Build the (llm_client, embedder, cross_encoder) triple backed by OpenAI."""
    from graphiti_core.llm_client import LLMConfig, OpenAIClient

    llm_config = LLMConfig(api_key=api_key, model="gpt-4o-mini", small_model="gpt-4o-mini")
    return OpenAIClient(config=llm_config), _openai_embedder(api_key), _openai_reranker(api_key)


def _gemini_clients(gemini_key: str, openai_key: str, model: str) -> _ClientTriple:
    """Build a hybrid triple: Gemini Flash for the LLM, OpenAI for embeddings + rerank.

    Graphiti maps Gemini 429s to the same `RateLimitError` the adapter backoff retries on,
    so the write-path retry works unchanged.
    """
    from graphiti_core.llm_client import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

    llm = GeminiClient(config=LLMConfig(api_key=gemini_key, model=model))
    return llm, _openai_embedder(openai_key), _openai_reranker(openai_key)


def _build_graphiti(
    *,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    database: str | None = None,
    api_key: str | None = None,
    max_coroutines: int | None = None,
) -> Graphiti:
    """Build a raw Graphiti client on FalkorDB, using the configured LLM provider.

    Connection params fall back to settings (FALKORDB_*) when unset. The provider is
    selected by `GRAPHITI_LLM_PROVIDER` (default "openai"); "gemini" is hybrid and needs
    both keys. Clients are built here, not at import, so `relic --help` stays key-free.
    Private: callers go through ``open_memory``, which wraps this raw handle in the seam.
    """
    import os

    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    from relic.config import get_settings

    _patch_falkordb_empty_query()
    _patch_prompt_json_datetime()

    settings = get_settings()
    provider = (settings.graphiti_llm_provider or "openai").strip().lower()
    key = api_key or os.environ.get("OPENAI_API_KEY") or settings.openai_api_key
    host = host if host is not None else settings.falkordb_host
    port = port if port is not None else settings.falkordb_port
    password = password if password is not None else settings.falkordb_password
    database = database if database is not None else settings.falkordb_database

    if provider == "openai":
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is required for `relic ingest` and `relic query`. "
                "Set it in .env or the environment."
            )
        llm_client, embedder, cross_encoder = _openai_clients(key)
    elif provider == "gemini":
        # Hybrid: Gemini for extraction, OpenAI for embeddings/reranking -- needs both.
        if not settings.gemini_api_key or not key:
            raise RuntimeError(
                "GRAPHITI_LLM_PROVIDER=gemini needs GEMINI_API_KEY (extraction) and "
                "OPENAI_API_KEY (embeddings). Set both in .env or the environment."
            )
        llm_client, embedder, cross_encoder = _gemini_clients(
            settings.gemini_api_key, key, settings.gemini_model
        )
    else:
        raise RuntimeError(
            f"Unknown GRAPHITI_LLM_PROVIDER={provider!r}; expected 'openai' (the default) "
            "or 'gemini'."
        )

    return Graphiti(
        graph_driver=FalkorDriver(host=host, port=port, password=password, database=database),
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
        max_coroutines=max_coroutines,
    )


def falkordb_reachable(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True if a TCP connection to ``host:port`` opens within ``timeout`` seconds.

    A cheap liveness probe, no graph query. Callers use it to fail fast with a clear
    message when FalkorDB is down, rather than letting the driver raise deep in a
    constructor-scheduled background task.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def ensure_indexes(graphiti: Graphiti) -> None:
    """Build Graphiti's indices and constraints (idempotent). Prefer ``build_indices``."""
    await graphiti.build_indices_and_constraints()
