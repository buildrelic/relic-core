"""Land structured episodes into Graphiti via constrained LLM extraction.

Episodes are added sequentially: each is awaited before the next so its extracted
entities are resolvable when later episodes reference them. FalkorDB is
multi-tenant, so each episode lands under its repo's group_id, partitioning the
graph per source.

The loop is resilient and observable. An episode that fails extraction (a rate
limit, a malformed entity) is caught, counted, and logged, and the run carries
on: one bad PR never aborts the whole ingest. The returned ``LoadStats`` carries
the counts and the failures so the caller can report them. An ``on_loaded``
callback fires per success (with the episode's freshness token) so the caller can
checkpoint progress, and a ``skip`` map (name -> token already landed) lets a re-run
pass over unchanged episodes and *supersede* ones whose content changed.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from relic.contracts import EpisodeSpec
from relic.obs import get_logger

if TYPE_CHECKING:
    from graphiti_core import Graphiti

log = get_logger("load")

# OpenAI rate-limit backoff. Graphiti maps a 429 to RateLimitError and deliberately does
# not retry it (see its OpenAI client), so the backoff lives here. TPM/RPM windows refill
# in seconds, so exponential-with-jitter clears most spikes within a couple of attempts.
# Module-level so tests can swap the wait/attempt count for a fast, deterministic run.
_RETRY_WAIT = wait_random_exponential(multiplier=2, max=60)
_RETRY_ATTEMPTS = 6


@dataclass(slots=True)
class LoadStats:
    """The outcome of a load run: counts, the failures, and how long it took."""

    group_id: str
    attempted: int = 0  # episodes considered this run
    loaded: int = 0  # newly added to the graph
    skipped: int = 0  # already checkpointed with an unchanged token, not re-added
    superseded: int = 0  # re-extracted because their content changed since the last load
    failed: int = 0  # raised during add_episode
    failures: list[tuple[str, str]] = field(default_factory=list)  # (episode name, reason)
    duration_s: float = 0.0

    @property
    def episodes(self) -> int:
        """Episodes newly loaded this run (back-compat alias for ``loaded``)."""
        return self.loaded


def _content_token(spec: EpisodeSpec) -> str:
    """A content fingerprint of an episode body: the supersession signal.

    sha256 of the serialized body -- the exact bytes ``add_episode`` consumes -- so a
    re-presented episode supersedes only when its extracted-from content actually changed,
    not on a no-op source touch (which would re-pay the LLM cost for nothing). A schema or
    budget change shifts every body's bytes and so correctly forces a re-extract.
    """
    return hashlib.sha256(spec.body.encode("utf-8")).hexdigest()


def _as_skip_map(skip: dict[str, str | None] | set[str] | None) -> dict[str, str | None]:
    """Normalize ``skip`` to name -> token. A legacy set means 'skip these, token unknown'."""
    if skip is None:
        return {}
    if isinstance(skip, set):
        return {name: None for name in skip}
    return skip


def _pending(
    episodes: list[EpisodeSpec], skip: dict[str, str | None], stats: LoadStats
) -> tuple[list[EpisodeSpec], list[EpisodeSpec]]:
    """Split episodes into ``(to_add, to_supersede)``, recording the skipped count.

    - name unseen                 -> add (brand new)
    - name seen, token unchanged  -> skip (the steady-state common case)
    - name seen, token ``None``   -> skip (legacy ledger entry; never re-extract on upgrade)
    - name seen, token changed    -> supersede (remove the prior episode, then re-add)
    """
    to_add: list[EpisodeSpec] = []
    supersede: list[EpisodeSpec] = []
    skipped = 0
    for spec in episodes:
        if spec.name not in skip:
            to_add.append(spec)
            continue
        landed = skip[spec.name]
        if landed is not None and landed != _content_token(spec):
            supersede.append(spec)
        else:
            skipped += 1
    stats.skipped = skipped
    if skipped or supersede:
        log.info(
            "resuming: %d of %d already loaded, %d to add, %d to refresh",
            skipped,
            len(episodes),
            len(to_add),
            len(supersede),
        )
    return to_add, supersede


def _chunked(items: list[EpisodeSpec], size: int) -> Iterator[list[EpisodeSpec]]:
    """Yield ``items`` in contiguous slices of at most ``size``."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _call_with_backoff(make_awaitable: Callable[[], Awaitable], *, label: str) -> object:
    """Await a graphiti call, retrying on OpenAI rate limits with exponential backoff.

    Graphiti surfaces a 429 as ``RateLimitError`` and does not retry it, so we wait and
    retry here. ``make_awaitable`` is a factory (not a coroutine) so each attempt gets a
    fresh awaitable. Any non-rate-limit exception propagates on the first raise, so genuine
    failures are not masked.
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


async def _supersede_episode(graphiti: Graphiti, name: str, group_id: str) -> int:
    """Remove the prior episode(s) for ``(name, group_id)`` so a re-add lands in place.

    Re-adding alone would fork: ``add_episode`` mints a fresh Episodic uuid per call, so the
    stale node and its facts would linger. ``graphiti.remove_episode`` is the cascade, but on
    its own it over-deletes: it drops every edge the removed episode *originated*
    (``episodes[0]``), even a fact another episode also supports. So first prune the
    superseded episode out of any edge more than one episode supports -- persisting *only*
    ``e.episodes`` with a targeted update, never ``EntityEdge.save`` (which rewrites the
    unloaded ``fact_embedding`` to NULL and would break vector recall of the rescued fact).
    ``remove_episode`` then deletes only what this episode solely owned: edges it still
    originates and entities only it mentions. The prune must be persisted *before*
    ``remove_episode``, which re-reads ``episodes[0]`` from the graph. Resolving by
    name+group_id also cleans up any pre-existing duplicate. Returns the count removed.
    """
    found, _, _ = await graphiti.driver.execute_query(
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
            edges, _, _ = await graphiti.driver.execute_query(
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
                    await graphiti.driver.execute_query(
                        "MATCH (n:Entity)-[r:RELATES_TO {uuid: $uuid}]->(m:Entity) "
                        "SET r.episodes = $episodes",
                        uuid=edge["uuid"],
                        episodes=[e for e in episodes if e != uuid],
                    )
        await graphiti.remove_episode(uuid)
    return len(found)


async def _add_one(
    graphiti: Graphiti,
    spec: EpisodeSpec,
    stats: LoadStats,
    on_loaded: Callable[[str, str | None], None] | None,
    *,
    remove_first: bool = False,
) -> bool:
    """Add a single episode, updating ``stats``. A failure is caught, not raised.

    Returns True on success (and fires ``on_loaded`` with the episode's freshness token);
    False if extraction failed, in which case the episode is counted and logged so the run
    can carry on. ``remove_first`` supersedes: it removes the prior episode for this name
    before re-adding, so an edited source artifact replaces its stale graph footprint
    instead of forking a second episode. This is the shared per-episode unit used by the
    sequential loader and by the bulk loader's fallback when a whole batch fails.
    """
    from graphiti_core.nodes import EpisodeType

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

    if remove_first:
        try:
            removed = await _supersede_episode(graphiti, spec.name, spec.group_id)
            if removed:
                log.info("superseding %s: removed %d prior episode(s)", spec.name, removed)
        except Exception as exc:  # noqa: BLE001 - a failed removal must not abort; re-add anyway
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
            log.warning("supersede removal failed for %s: %s", spec.name, reason)
            log.debug("supersede removal traceback for %s", spec.name, exc_info=exc)

    try:
        await _call_with_backoff(
            lambda: graphiti.add_episode(
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
            ),
            label=spec.name,
        )
    except Exception as exc:  # noqa: BLE001 - one bad episode must not abort the run
        stats.failed += 1
        reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
        stats.failures.append((spec.name, reason))
        log.warning("episode failed %s: %s", spec.name, reason)
        log.debug("traceback for %s", spec.name, exc_info=exc)
        return False
    if remove_first:
        stats.superseded += 1
    else:
        stats.loaded += 1
    if on_loaded is not None:
        on_loaded(spec.name, _content_token(spec))
    return True


async def load_episodes(
    graphiti: Graphiti,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: dict[str, str | None] | set[str] | None = None,
    on_loaded: Callable[[str, str | None], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
) -> LoadStats:
    """Add each episode to Graphiti with the constrained entity/edge types.

    ``skip`` maps episode names already landed to their freshness token (from a
    checkpoint): an unchanged token is counted as skipped, not re-added; a changed token
    supersedes (the prior episode is removed, then re-added). ``on_loaded`` fires with the
    episode name and its new token after each success, so the caller can checkpoint a
    resumable re-run. ``on_progress`` fires with the live ``LoadStats`` after the skip count
    is known and after every episode is finalized, so a caller can drive a progress bar (the
    count to render is ``loaded + superseded + skipped + failed``). A failed episode is
    logged and counted, and the loop continues.
    """
    from relic.graph.engram import ensure_indexes

    skip_map = _as_skip_map(skip)
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await ensure_indexes(graphiti)

    to_add, supersede = _pending(episodes, skip_map, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before the loop starts
    total = len(to_add) + len(supersede)
    heartbeat = max(1, total // 10) if progress else 0
    start = time.monotonic()
    index = 0
    # Supersede first (remove the prior episode, then re-add) so a refreshed body replaces
    # its stale node in place; then add the brand-new episodes.
    for spec in supersede:
        index += 1
        await _add_one(graphiti, spec, stats, on_loaded, remove_first=True)
        if on_progress is not None:
            on_progress(stats)
        if heartbeat and index % heartbeat == 0:
            log.info("loaded %d/%d episodes", index, total)
    for spec in to_add:
        index += 1
        await _add_one(graphiti, spec, stats, on_loaded)
        if on_progress is not None:
            on_progress(stats)
        if heartbeat and index % heartbeat == 0:
            log.info("loaded %d/%d episodes", index, total)

    stats.duration_s = time.monotonic() - start
    return stats


async def load_episodes_bulk(
    graphiti: Graphiti,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: dict[str, str | None] | set[str] | None = None,
    on_loaded: Callable[[str, str | None], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
    batch_size: int = 10,
) -> LoadStats:
    """Land episodes via Graphiti's batched ``add_episode_bulk`` for fast backfill.

    Bulk extraction runs across a whole batch in parallel and dedupes once, which is far
    faster than the strictly-sequential :func:`load_episodes` on a cold-start ingest. The
    trade-offs versus the sequential path:

    - **Failure isolation.** A batch is all-or-nothing, so one poison episode would fail
      its whole batch. We catch that and fall back to per-episode adds for just that
      batch, preserving the "one bad episode never aborts the run" guarantee.
    - **Rate limits.** A batch fans out many LLM calls at once, so it is the most likely
      to hit OpenAI's TPM limit. Both the bulk call and the per-episode fallback back off
      and retry (see :func:`_call_with_backoff`); smaller ``batch_size`` lowers the burst.
    - **Checkpointing.** ``on_loaded`` fires for every episode in a batch only after the
      batch lands, so the checkpoint is batch-granular on the happy path (a hard crash
      mid-batch replays that batch on the next run, which ``skip`` keeps idempotent).

    ``on_progress`` fires with the live ``LoadStats`` after the skip count is known and
    after each batch lands (and per episode during a fallback), so a caller can drive a
    progress bar; it steps in chunks of ``batch_size`` on the happy path.

    Episodes are grouped by their own ``group_id`` because ``add_episode_bulk`` binds to
    one graph per call (GitHub repo vs Linear land in different partitions). Supersessions
    are inherently per-episode (remove the prior node, then re-add) while the batch add is
    per-group, so they run as a serial pre-pass before any batch -- every batch then adds
    against a clean slate and the bulk dedupe never sees a stale-vs-fresh collision.
    """
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.utils.bulk_utils import RawEpisode

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, ensure_indexes

    skip_map = _as_skip_map(skip)
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await ensure_indexes(graphiti)

    to_add, supersede = _pending(episodes, skip_map, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before loading starts
    total = len(to_add) + len(supersede)
    # Serial supersede pre-pass: remove-then-add each changed episode before batching the
    # fresh ones, so add_episode_bulk only ever adds against a clean slate for that name.
    for spec in supersede:
        await _add_one(graphiti, spec, stats, on_loaded, remove_first=True)
        if on_progress is not None:
            on_progress(stats)
    by_group: dict[str, list[EpisodeSpec]] = {}
    for spec in to_add:
        by_group.setdefault(spec.group_id, []).append(spec)

    start = time.monotonic()
    for grp, specs in by_group.items():
        for batch in _chunked(specs, batch_size):
            raws = [
                RawEpisode(
                    name=spec.name,
                    content=spec.body,
                    source_description=spec.source_description,
                    source=EpisodeType.json,
                    reference_time=spec.reference_time,
                )
                for spec in batch
            ]
            try:
                await _call_with_backoff(
                    lambda raws=raws, grp=grp: graphiti.add_episode_bulk(
                        raws,
                        group_id=grp,
                        entity_types=ENTITY_TYPES,
                        edge_types=EDGE_TYPES,
                        edge_type_map=EDGE_TYPE_MAP,
                    ),
                    label=f"bulk[{grp}]",
                )
            except Exception as exc:  # noqa: BLE001 - a failed batch falls back, never aborts
                reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
                log.warning("bulk batch of %d failed (%s); retrying one by one", len(batch), reason)
                for spec in batch:
                    await _add_one(graphiti, spec, stats, on_loaded)
                    if on_progress is not None:
                        on_progress(stats)
                continue
            stats.loaded += len(batch)
            if on_loaded is not None:
                for spec in batch:
                    on_loaded(spec.name, _content_token(spec))
            if on_progress is not None:
                on_progress(stats)
            if progress:
                # superseded episodes landed in the pre-pass; include them so the heartbeat
                # numerator reaches ``total``.
                log.info("loaded %d/%d episodes", stats.loaded + stats.superseded, total)

    stats.duration_s = time.monotonic() - start
    return stats
