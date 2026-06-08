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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from relic.ingest.mappers import EpisodeSpec
from relic.obs import get_logger

if TYPE_CHECKING:
    from graphiti_core import Graphiti

log = get_logger("load")


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


async def load_episodes(
    graphiti: Graphiti,
    episodes: list[EpisodeSpec],
    *,
    group_id: str,
    skip: set[str] | None = None,
    on_loaded: Callable[[str], None] | None = None,
    progress: bool = True,
) -> LoadStats:
    """Add each episode to Graphiti with the constrained entity/edge types.

    ``skip`` names episodes already landed (from a checkpoint): they are counted
    as skipped, not re-added. ``on_loaded`` fires with the episode name after each
    success, so the caller can record progress for a resumable re-run. A failed
    episode is logged and counted, and the loop continues.
    """
    from graphiti_core.nodes import EpisodeType

    from relic.graph.engram import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, ensure_indexes

    skip = skip or set()
    stats = LoadStats(group_id=group_id, attempted=len(episodes))

    await ensure_indexes(graphiti)

    pending = [spec for spec in episodes if spec.name not in skip]
    stats.skipped = len(episodes) - len(pending)
    if stats.skipped:
        log.info(
            "resuming: %d of %d already loaded, %d to add",
            stats.skipped,
            len(episodes),
            len(pending),
        )

    total = len(pending)
    heartbeat = max(1, total // 10) if progress else 0
    start = time.monotonic()
    for index, spec in enumerate(pending, start=1):
        try:
            await graphiti.add_episode(
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
            )
        except Exception as exc:  # noqa: BLE001 - one bad episode must not abort the run
            stats.failed += 1
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
            stats.failures.append((spec.name, reason))
            log.warning("episode failed [%d/%d] %s: %s", index, total, spec.name, reason)
            log.debug("traceback for %s", spec.name, exc_info=exc)
            continue
        stats.loaded += 1
        if on_loaded is not None:
            on_loaded(spec.name)
        if heartbeat and index % heartbeat == 0:
            log.info("loaded %d/%d episodes", index, total)

    stats.duration_s = time.monotonic() - start
    return stats
