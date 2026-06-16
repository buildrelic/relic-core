"""The episode contract: the data shape ingest produces and graph consumes.

This lives in contracts, not in either subsystem, so neither ingest nor graph
imports the other. Ingest fills an ``EpisodeSpec``, the composition root hands the
list to ``graph.load``, and graph reads it. A plain dataclass with no behavior and
no subsystem dependency: the seam is the type, not the code that builds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class EpisodeSpec:
    name: str
    body: str
    source_description: str
    reference_time: datetime
    group_id: str
