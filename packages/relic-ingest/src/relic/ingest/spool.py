"""Episode spool: a durable, per-repo queue of mapped episodes awaiting extraction.

Raw ingestion (fetch + map) is fast and deterministic. Graph extraction is the
slow half: it runs an LLM per episode and dominates an ingest's wall clock (a
40-episode run on astral-sh/uv spent 99.5% of its time there). The spool decouples
the two. ``relic ingest`` writes each mapped ``EpisodeSpec`` here before extraction;
``relic load`` reads them back later and runs extraction, so the expensive step can
run after capture, on another machine, or one batch at a time.

Layout is one JSON file per episode under ``<base>/<group_id>/<slug>-<hash>.json``.
Per-file (not a single JSONL) so a re-capture upserts by episode name instead of
appending duplicates, a single episode can be inspected or loaded on its own, and
the directory reads as the list of what was captured. The ``<hash>`` (first 8 hex of
the name's sha256) keeps the filename unique even when two names slugify alike; the
``<slug>`` keeps it legible. Draining is by the checkpoint, not deletion: ``load``
skips episodes already landed, so the spool is re-runnable and idempotent.

The spool directory groups by a capture key (the repo's ``group_id``), which can
differ from an episode's own ``group_id``: a GitHub ingest that also pulls Linear
spools both under the GitHub repo's key, while each episode keeps its own group in
the record so the loader still partitions it correctly. This mirrors the checkpoint,
which is one ledger per capture key.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from relic.contracts import SCHEMA_VERSION, EpisodeSpec

_BASE = "data/spool"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def spool_dir(group_id: str, base: Path | str = _BASE) -> Path:
    """Directory holding the spooled episodes for one capture: ``<base>/<group_id>``."""
    return Path(base) / group_id


def _filename(name: str) -> str:
    """A unique, legible filename for an episode name: ``<slug>-<8 hex>.json``."""
    slug = _SLUG_RE.sub("_", name).strip("_") or "episode"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.json"


def _to_record(spec: EpisodeSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "body": spec.body,
        "source_description": spec.source_description,
        "reference_time": spec.reference_time.isoformat(),
        "group_id": spec.group_id,
        "schema_version": spec.schema_version,
    }


def _from_record(record: dict) -> EpisodeSpec:
    return EpisodeSpec(
        name=record["name"],
        body=record["body"],
        source_description=record["source_description"],
        reference_time=datetime.fromisoformat(record["reference_time"]),
        group_id=record["group_id"],
        schema_version=int(record.get("schema_version", SCHEMA_VERSION)),
    )


def sort_episodes(specs: list[EpisodeSpec]) -> list[EpisodeSpec]:
    """Return episodes in canonical load order: oldest first, name as a tiebreak.

    The sequential loader resolves an entity introduced by an earlier episode when a
    later one references it, so feed order changes the graph. Both the in-memory
    capture path and the spool read order by this one key, so a combined ``relic
    ingest`` and a split ``ingest --no-load`` + ``relic load`` hand the loader the
    same sequence and build the same graph. ``reference_time`` is the episode's
    temporal anchor, so oldest-first also matches Graphiti's own time axis.
    """
    return sorted(specs, key=lambda spec: (spec.reference_time, spec.name))


def spool_episode(spec: EpisodeSpec, group_id: str, *, base: Path | str = _BASE) -> Path:
    """Write one episode under ``group_id``'s spool, overwriting any prior copy by name."""
    path = spool_dir(group_id, base) / _filename(spec.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_to_record(spec), indent=2), encoding="utf-8")
    return path


def spool_episodes(
    specs: list[EpisodeSpec], group_id: str, *, base: Path | str = _BASE
) -> list[Path]:
    """Write every episode under ``group_id``'s spool. Returns the paths written."""
    return [spool_episode(spec, group_id, base=base) for spec in specs]


def read_spool(group_id: str, *, base: Path | str = _BASE) -> list[EpisodeSpec]:
    """Read every spooled episode for a capture, oldest first. Empty if the spool is absent.

    Ordered by ``(reference_time, name)`` so the sequential loader sees episodes in a
    stable, roughly chronological order: an entity an early episode introduces is
    resolvable when a later one references it.
    """
    directory = spool_dir(group_id, base)
    if not directory.is_dir():
        return []
    specs = [
        _from_record(json.loads(path.read_text(encoding="utf-8")))
        for path in directory.glob("*.json")
    ]
    return sort_episodes(specs)


def spool_count(group_id: str, *, base: Path | str = _BASE) -> int:
    """Count the episodes currently spooled for a capture, without parsing them."""
    directory = spool_dir(group_id, base)
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*.json"))


def clear_spool(group_id: str, *, base: Path | str = _BASE) -> int:
    """Delete a capture's spool. Returns how many episode files were removed."""
    directory = spool_dir(group_id, base)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        path.unlink()
        removed += 1
    if not any(directory.iterdir()):
        directory.rmdir()
    return removed
