"""The body→node field map: the seam between the wire body and the flat graph node.

ADR-0001 keeps three representations of each artifact on purpose -- the rich
``relic.ontology`` models (typed-API source of truth), the versioned wire body
(``relic.contracts.episode_body``), and the flat Graphiti-facing node
(``relic.graph.schema``). This module does **not** collapse them. It closes the one
unguarded gap between them: the flatten-and-rename the extractor must perform from a
body to a flat node used to live only in a prose docstring ("a known fumble surface"),
validated nowhere. Here that correspondence is **executable data**, so a consistency
test (``tests/test_field_map.py``) fails CI the moment a node field gains no source, or a
mapped body path stops resolving -- instead of surfacing as a silent null at graph-read
time.

The map is the spec of what the extractor is asked to produce; it is intentionally not a
runtime transform (Graphiti's LLM does the extraction). Its job is to be the single,
checked record of the correspondence.
"""

from __future__ import annotations

import typing

from pydantic import BaseModel

from relic.contracts.episode_body import PrEpisodeBody
from relic.graph.schema import PullRequestNode

# Each entry maps a flat node attribute to the dotted path of its source inside the
# episode body. A path with one dot reads a top-level section field; deeper paths are the
# *flattens* (e.g. through ``timestamps`` / ``diff_stats``); a differing leaf name is a
# *rename* (the canonical example: ``opened_at`` <- ``pull_request.created_at``, renamed
# off Graphiti's reserved ``created_at``).
PULL_REQUEST_FIELD_MAP: dict[str, str] = {
    "number": "pull_request.number",
    "title": "pull_request.title",
    "state": "pull_request.state",
    "url": "pull_request.url",
    "opened_at": "pull_request.created_at",  # rename
    "merged_at": "pull_request.merged_at",
    "base_ref": "pull_request.base_ref",
    "head_ref": "pull_request.head_ref",
    "first_review_at": "pull_request.timestamps.first_review_at",  # flatten
    "approved_at": "pull_request.timestamps.approved_at",  # flatten
    "time_to_merge_hours": "pull_request.timestamps.time_to_merge_hours",  # flatten
    "total_additions": "pull_request.diff_stats.total_additions",  # flatten
    "total_deletions": "pull_request.diff_stats.total_deletions",  # flatten
    "changed_files": "pull_request.diff_stats.changed_files",  # flatten
}

# artifact label -> (flat node model, wire body model, node-attr -> body-path map).
# A new Zoned artifact whose node fields need a checked source is registered here.
ARTIFACT_FIELD_MAPS: dict[str, tuple[type[BaseModel], type[BaseModel], dict[str, str]]] = {
    "PullRequest": (PullRequestNode, PrEpisodeBody, PULL_REQUEST_FIELD_MAP),
}


def _model_within(annotation: object) -> type[BaseModel] | None:
    """The pydantic model an annotation carries, unwrapping ``X | None`` / ``Optional[X]``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def body_path_exists(body_model: type[BaseModel], dotted_path: str) -> bool:
    """True if ``dotted_path`` resolves through ``body_model``'s nested pydantic fields.

    Walks each segment: intermediate segments must descend into a nested model; the final
    segment must be a declared field. A rename or a moved/removed body field makes this
    return ``False`` -- which is exactly what the consistency test keys on.
    """
    current: type[BaseModel] = body_model
    segments = dotted_path.split(".")
    for index, segment in enumerate(segments):
        field = current.model_fields.get(segment)
        if field is None:
            return False
        if index == len(segments) - 1:
            return True
        nested = _model_within(field.annotation)
        if nested is None:
            return False
        current = nested
    return True
