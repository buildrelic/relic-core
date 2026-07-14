"""Land structured episodes into the engram store.

The loop keeps the shape it had in the graph era -- ``LoadStats``, the ``skip``
map, ``on_loaded`` checkpointing, per-episode failure isolation -- because the
callers (the CLI, the daemon capture path, the checkpoint ledger) are built
against it. What changed underneath (ADR-0007) is that an "add" is now a Postgres
upsert keyed on ``(workspace_id, name)``: a changed body replaces its prior row in
place, so supersession needs no removal pass. The token diff still runs, but only
to *count* refreshes; the write path is identical either way.

``concurrency`` is kept in the signature for seam compatibility and ignored: a row
upsert is milliseconds, so there is nothing worth fanning out (the parameter used
to overlap LLM extraction I/O, which no longer exists).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from relic.contracts import EpisodeSpec
from relic.obs import get_logger

if TYPE_CHECKING:
    from relic.engram.store import EngramWriter

log = get_logger("load")


@dataclass(slots=True)
class LoadStats:
    """The outcome of a load run: counts, the failures, and how long it took."""

    group_id: str
    attempted: int = 0  # episodes considered this run
    loaded: int = 0  # newly added to the engram
    skipped: int = 0  # already checkpointed with an unchanged token, not re-added
    superseded: int = 0  # refreshed in place because their content changed
    failed: int = 0  # raised during add_episode
    failures: list[tuple[str, str]] = field(default_factory=list)  # (episode name, reason)
    duration_s: float = 0.0

    @property
    def episodes(self) -> int:
        """Episodes newly loaded this run (back-compat alias for ``loaded``)."""
        return self.loaded


def _content_token(spec: EpisodeSpec) -> str:
    """A content fingerprint of an episode body: the refresh signal.

    sha256 of the serialized body -- the exact bytes the store hashes into
    ``content_hash`` -- so a re-presented episode counts as superseded only when its
    content actually changed, not on a no-op source touch. A schema or budget change
    shifts every body's bytes and so correctly forces a refresh.
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
    """Split episodes into ``(to_add, to_refresh)``, recording the skipped count.

    - name unseen                 -> add (brand new)
    - name seen, token unchanged  -> skip (the steady-state common case)
    - name seen, token ``None``   -> skip (legacy ledger entry; never re-land on upgrade)
    - name seen, token changed    -> refresh (the upsert replaces the row in place)
    """
    to_add: list[EpisodeSpec] = []
    refresh: list[EpisodeSpec] = []
    skipped = 0
    for spec in episodes:
        if spec.name not in skip:
            to_add.append(spec)
            continue
        landed = skip[spec.name]
        if landed is not None and landed != _content_token(spec):
            refresh.append(spec)
        else:
            skipped += 1
    stats.skipped = skipped
    if skipped or refresh:
        log.info(
            "resuming: %d of %d already loaded, %d to add, %d to refresh",
            skipped,
            len(episodes),
            len(to_add),
            len(refresh),
        )
    return to_add, refresh


async def _add_one(
    memory: EngramWriter,
    spec: EpisodeSpec,
    stats: LoadStats,
    on_loaded: Callable[[str, str | None], None] | None,
    *,
    refresh: bool = False,
) -> bool:
    """Add a single episode, updating ``stats``. A failure is caught, not raised.

    Returns True on success (and fires ``on_loaded`` with the episode's freshness
    token); False if the add failed, in which case the episode is counted and logged
    so the run can carry on. ``refresh`` only affects which counter ticks: the store's
    upsert replaces a changed row in place either way.
    """
    try:
        await memory.add_episode(spec)
    except Exception as exc:  # noqa: BLE001 - one bad episode must not abort the run
        stats.failed += 1
        reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
        stats.failures.append((spec.name, reason))
        log.warning("episode failed %s: %s", spec.name, reason)
        log.debug("traceback for %s", spec.name, exc_info=exc)
        return False
    if refresh:
        stats.superseded += 1
    else:
        stats.loaded += 1
    if on_loaded is not None:
        on_loaded(spec.name, _content_token(spec))
    return True


async def load_episodes(
    memory: EngramWriter,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: dict[str, str | None] | set[str] | None = None,
    on_loaded: Callable[[str, str | None], None] | None = None,
    on_progress: Callable[[LoadStats], None] | None = None,
    progress: bool = True,
    concurrency: int = 1,
) -> LoadStats:
    """Land each episode in the engram store, sequentially.

    ``skip`` maps episode names already landed to their freshness token (from a
    checkpoint): an unchanged token is counted as skipped, not re-added; a changed
    token counts as superseded (the upsert refreshes the row in place). ``on_loaded``
    fires with the episode name and its new token after each success, so the caller
    can checkpoint a resumable re-run. ``on_progress`` fires with the live
    ``LoadStats`` after the skip count is known and after every episode is finalized
    (the count to render is ``loaded + superseded + skipped + failed``). A failed
    episode is logged and counted, and the loop continues.

    ``concurrency`` is accepted for seam compatibility and ignored: a row upsert has
    no extraction I/O to overlap.
    """
    del concurrency  # kept in the signature for seam compatibility (see module docstring)
    skip_map = _as_skip_map(skip)
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await memory.ensure_schema()

    to_add, refresh = _pending(episodes, skip_map, stats)
    if on_progress is not None:
        on_progress(stats)  # reflect already-skipped episodes before the loop starts
    total = len(to_add) + len(refresh)
    heartbeat = max(1, total // 10) if progress else 0
    start = time.monotonic()
    finalized = 0

    ordered = [(s, True) for s in refresh] + [(s, False) for s in to_add]
    for finalized, (spec, is_refresh) in enumerate(ordered, start=1):
        await _add_one(memory, spec, stats, on_loaded, refresh=is_refresh)
        if on_progress is not None:
            on_progress(stats)
        if heartbeat and finalized % heartbeat == 0:
            log.info("loaded %d/%d episodes", finalized, total)

    stats.duration_s = time.monotonic() - start
    return stats
