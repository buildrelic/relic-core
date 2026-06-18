"""The episode contract: the data shape ingest produces and graph consumes.

This lives in contracts, not in either subsystem, so neither ingest nor graph
imports the other. Ingest fills an ``EpisodeSpec``, the composition root hands the
list to ``graph.load``, and graph reads it. A plain dataclass with no behavior and
no subsystem dependency: the seam is the type, not the code that builds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from relic.contracts.episode_body import SCHEMA_VERSION


@dataclass(slots=True)
class EpisodeSpec:
    name: str
    body: str
    source_description: str
    reference_time: datetime
    group_id: str
    # The body's schema version, surfaced on the envelope so the loader/detector can
    # branch without parsing the body. It also lives inside the body. It must never
    # be folded into ``name``: the name is the checkpoint dedup key, and a version in
    # it would re-ingest and fork the graph.
    schema_version: int = SCHEMA_VERSION
