"""Land structured episodes into the memory graph through the Memory seam.

Episodes are added sequentially by default: each is awaited before the next so its
extracted entities are resolvable when later episodes reference them. Extraction is
I/O-wait bound on LLM calls (99.5% of a 40-episode run's wall clock), so ``concurrency``
opts a drain into a bounded fan-out: up to N episodes extract at once, trading the
strict feed order (in-flight episodes race entity resolution, the same class of
trade-off the bulk path accepts) for wall clock. Each episode lands under its repo's
group_id, partitioning the graph per source.

The loop is resilient and observable. An episode that fails (a terminal rate limit, a
malformed entity) is caught, counted, and logged, and the run carries on: one bad PR
never aborts the whole ingest. The returned ``LoadStats`` carries the counts and the
failures so the caller can report them. An ``on_loaded`` callback fires per success (with
the episode's freshness token) so the caller can checkpoint progress, and a ``skip`` map
(name -> token already landed) lets a re-run pass over unchanged episodes and *supersede*
ones whose content changed.

This module owns the *loop* — resilience, checkpointing, supersession ordering, stats. The
Graphiti calls, the rate-limit backoff, and the supersession cascade live behind the seam
in ``GraphitiMemory`` (ADR-0003).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from relic.contracts import EpisodeSpec
from relic.obs import get_logger

if TYPE_CHECKING:
    from relic.graph.memory import MemoryWriter

log = get_logger("load")


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


async def _add_one(
    memory: MemoryWriter,
    spec: EpisodeSpec,
    stats: LoadStats,
    on_loaded: Callable[[str, str | None], None] | None,
    *,
    remove_first: bool = False,
) -> bool:
    """Add a single episode, updating ``stats``. A failure is caught, not raised.

    Returns True on success (and fires ``on_loaded`` with the episode's freshness token);
    False if the add failed, in which case the episode is counted and logged so the run can
    carry on. ``remove_first`` supersedes: it removes the prior episode for this name before
    re-adding, so an edited source artifact replaces its stale graph footprint instead of
    forking a second episode. This is the shared per-episode unit used by the sequential
    loader and by the bulk loader's fallback when a whole batch fails. The adapter retries
    transient rate limits internally, so a raise here is terminal.
    """
    if remove_first:
        try:
            removed = await memory.supersede_episode(spec.name, spec.group_id)
            if removed:
                log.info("superseding %s: removed %d prior episode(s)", spec.name, removed)
        except Exception as exc:  # noqa: BLE001 - a failed removal must not abort; re-add anyway
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
            log.warning("supersede removal failed for %s: %s", spec.name, reason)
            log.debug("supersede removal traceback for %s", spec.name, exc_info=exc)

    try:
        await memory.add_episode(spec)
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
    memory: MemoryWriter,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: dict[str, str | None] | set[str] | None = None,
    on_loaded: Callable[[str, str | None], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
    concurrency: int = 1,
) -> LoadStats:
    """Add each episode through the Memory seam, at most ``concurrency`` at a time.

    ``skip`` maps episode names already landed to their freshness token (from a
    checkpoint): an unchanged token is counted as skipped, not re-added; a changed token
    supersedes (the prior episode is removed, then re-added). ``on_loaded`` fires with the
    episode name and its new token after each success, so the caller can checkpoint a
    resumable re-run. ``on_progress`` fires with the live ``LoadStats`` after the skip count
    is known and after every episode is finalized, so a caller can drive a progress bar (the
    count to render is ``loaded + superseded + skipped + failed``). A failed episode is
    logged and counted, and the loop continues.

    ``concurrency=1`` (the default) keeps the strictly sequential, oldest-first feed: an
    entity an early episode introduces is resolved when a later one references it. A
    higher value overlaps the LLM-bound extraction I/O of up to that many episodes.
    Per-episode semantics are unchanged either way — ``on_loaded`` still fires per episode
    as it lands (checkpoint writes stay per-episode), failures stay isolated, and every
    supersession completes before the first brand-new add so a refreshed body never races
    the batch that might reference it. The trade-off is entity resolution across in-flight
    episodes (two episodes introducing the same new entity can miss each other's dedup),
    the same class of trade-off :func:`load_episodes_bulk` accepts per batch.
    """
    skip_map = _as_skip_map(skip)
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await memory.build_indices()

    to_add, supersede = _pending(episodes, skip_map, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before the loop starts
    total = len(to_add) + len(supersede)
    heartbeat = max(1, total // 10) if progress else 0
    start = time.monotonic()
    finalized = 0

    async def _finalize_one(spec: EpisodeSpec, *, remove_first: bool) -> None:
        # The shared per-episode unit for both drain modes: add (or supersede), then tick
        # the bar and the heartbeat. The bookkeeping after the await is synchronous, so
        # concurrent finalizers never interleave inside it.
        nonlocal finalized
        await _add_one(memory, spec, stats, on_loaded, remove_first=remove_first)
        finalized += 1
        if on_progress is not None:
            on_progress(stats)
        if heartbeat and finalized % heartbeat == 0:
            log.info("loaded %d/%d episodes", finalized, total)

    # Supersede first (remove the prior episode, then re-add) so a refreshed body replaces
    # its stale node in place; then add the brand-new episodes.
    if concurrency <= 1:
        for spec in supersede:
            await _finalize_one(spec, remove_first=True)
        for spec in to_add:
            await _finalize_one(spec, remove_first=False)
    else:
        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(spec: EpisodeSpec, *, remove_first: bool) -> None:
            async with semaphore:
                await _finalize_one(spec, remove_first=remove_first)

        # gather (not a TaskGroup) on purpose: _add_one catches per-episode failures, so
        # nothing raises, and one bad episode must never cancel its siblings.
        await asyncio.gather(*(_bounded(s, remove_first=True) for s in supersede))
        await asyncio.gather(*(_bounded(s, remove_first=False) for s in to_add))

    stats.duration_s = time.monotonic() - start
    return stats


async def load_episodes_bulk(
    memory: MemoryWriter,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: dict[str, str | None] | set[str] | None = None,
    on_loaded: Callable[[str, str | None], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
    batch_size: int = 10,
) -> LoadStats:
    """Land episodes via the seam's batched ``add_episode_bulk`` for fast backfill.

    Bulk extraction runs across a whole batch in parallel and dedupes once, which is far
    faster than the strictly-sequential :func:`load_episodes` on a cold-start ingest. The
    trade-offs versus the sequential path:

    - **Failure isolation.** A batch is all-or-nothing, so one poison episode would fail
      its whole batch. We catch that and fall back to per-episode adds for just that
      batch, preserving the "one bad episode never aborts the run" guarantee.
    - **Rate limits.** A batch fans out many LLM calls at once, so it is the most likely
      to hit a provider's TPM limit. The adapter backs off and retries both the bulk call
      and the per-episode fallback; a smaller ``batch_size`` lowers the burst.
    - **Checkpointing.** ``on_loaded`` fires for every episode in a batch only after the
      batch lands, so the checkpoint is batch-granular on the happy path (a hard crash
      mid-batch replays that batch on the next run, which ``skip`` keeps idempotent).

    ``on_progress`` fires with the live ``LoadStats`` after the skip count is known and
    after each batch lands (and per episode during a fallback), so a caller can drive a
    progress bar; it steps in chunks of ``batch_size`` on the happy path.

    Episodes are grouped by their own ``group_id`` because a bulk add binds to one graph
    per call (GitHub repo vs Linear land in different partitions). Supersessions are
    inherently per-episode (remove the prior node, then re-add) while the batch add is
    per-group, so they run as a serial pre-pass before any batch -- every batch then adds
    against a clean slate and the bulk dedupe never sees a stale-vs-fresh collision.
    """
    skip_map = _as_skip_map(skip)
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await memory.build_indices()

    to_add, supersede = _pending(episodes, skip_map, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before loading starts
    total = len(to_add) + len(supersede)
    # Serial supersede pre-pass: remove-then-add each changed episode before batching the
    # fresh ones, so add_episode_bulk only ever adds against a clean slate for that name.
    for spec in supersede:
        await _add_one(memory, spec, stats, on_loaded, remove_first=True)
        if on_progress is not None:
            on_progress(stats)
    by_group: dict[str, list[EpisodeSpec]] = {}
    for spec in to_add:
        by_group.setdefault(spec.group_id, []).append(spec)

    start = time.monotonic()
    for specs in by_group.values():
        for batch in _chunked(specs, batch_size):
            try:
                await memory.add_episode_bulk(batch)
            except Exception as exc:  # noqa: BLE001 - a failed batch falls back, never aborts
                reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
                log.warning("bulk batch of %d failed (%s); retrying one by one", len(batch), reason)
                for spec in batch:
                    await _add_one(memory, spec, stats, on_loaded)
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
