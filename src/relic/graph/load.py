"""Land structured episodes into Graphiti via constrained LLM extraction.

Episodes are added sequentially: each is awaited before the next so its extracted
entities are resolvable when later episodes reference them. FalkorDB is
multi-tenant, so each episode lands under its repo's group_id, partitioning the
graph per source.

The loop is resilient and observable. An episode that fails extraction (a rate
limit, a malformed entity) is caught, counted, and logged, and the run carries
on: one bad PR never aborts the whole ingest. The returned ``LoadStats`` carries
the counts and the failures so the caller can report them. An ``on_loaded``
callback fires per success so the caller can checkpoint progress, and a ``skip``
set lets a re-run pass over episodes that already landed.
"""

from __future__ import annotations

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

from relic.ingest.mappers import EpisodeSpec
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
    skipped: int = 0  # already checkpointed, not re-added
    failed: int = 0  # raised during add_episode
    failures: list[tuple[str, str]] = field(default_factory=list)  # (episode name, reason)
    duration_s: float = 0.0

    @property
    def episodes(self) -> int:
        """Episodes newly loaded this run (back-compat alias for ``loaded``)."""
        return self.loaded


def _pending(episodes: list[EpisodeSpec], skip: set[str], stats: LoadStats) -> list[EpisodeSpec]:
    """Filter out already-landed episodes, recording the skipped count on ``stats``."""
    pending = [spec for spec in episodes if spec.name not in skip]
    stats.skipped = len(episodes) - len(pending)
    if stats.skipped:
        log.info(
            "resuming: %d of %d already loaded, %d to add",
            stats.skipped,
            len(episodes),
            len(pending),
        )
    return pending


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


async def _add_one(
    graphiti: Graphiti,
    spec: EpisodeSpec,
    stats: LoadStats,
    on_loaded: Callable[[str], None] | None,
) -> bool:
    """Add a single episode, updating ``stats``. A failure is caught, not raised.

    Returns True on success (and fires ``on_loaded``); False if extraction failed, in
    which case the episode is counted and logged so the run can carry on. This is the
    shared per-episode unit used by the sequential loader and by the bulk loader's
    fallback when a whole batch fails.
    """
    from graphiti_core.nodes import EpisodeType

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

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
    stats.loaded += 1
    if on_loaded is not None:
        on_loaded(spec.name)
    return True


async def load_episodes(
    graphiti: Graphiti,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: set[str] | None = None,
    on_loaded: Callable[[str], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
) -> LoadStats:
    """Add each episode to Graphiti with the constrained entity/edge types.

    ``skip`` names episodes already landed (from a checkpoint): they are counted
    as skipped, not re-added. ``on_loaded`` fires with the episode name after each
    success, so the caller can record progress for a resumable re-run. ``on_progress``
    fires with the live ``LoadStats`` after the skip count is known and after every
    episode is finalized (loaded *or* failed), so a caller can drive a progress bar
    (the count to render is ``loaded + skipped + failed``). A failed episode is logged
    and counted, and the loop continues.
    """
    from relic.graph.engram import ensure_indexes

    skip = skip or set()
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await ensure_indexes(graphiti)

    pending = _pending(episodes, skip, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before the loop starts
    total = len(pending)
    heartbeat = max(1, total // 10) if progress else 0
    start = time.monotonic()
    for index, spec in enumerate(pending, start=1):
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
    skip: set[str] | None = None,
    on_loaded: Callable[[str], None] | None = None,
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
    one graph per call (GitHub repo vs Linear land in different partitions).
    """
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.utils.bulk_utils import RawEpisode

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, ensure_indexes

    skip = skip or set()
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await ensure_indexes(graphiti)

    pending = _pending(episodes, skip, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before loading starts
    by_group: dict[str, list[EpisodeSpec]] = {}
    for spec in pending:
        by_group.setdefault(spec.group_id, []).append(spec)

    total = len(pending)
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
                    on_loaded(spec.name)
            if on_progress is not None:
                on_progress(stats)
            if progress:
                log.info("loaded %d/%d episodes", stats.loaded, total)

    stats.duration_s = time.monotonic() - start
    return stats
