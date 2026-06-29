"""Guard the ontology triplication seam (ADR-0001): body→node correspondence stays true.

The flat graph node and the wire body are deliberately separate representations; the
flatten/rename between them used to live only in a docstring. These tests make that
correspondence enforced: a node field with no body source, or a mapped body path that no
longer resolves, fails here -- not silently as a null fact at graph-read time.
"""

import pytest

from relic.graph.field_map import (
    ARTIFACT_FIELD_MAPS,
    PULL_REQUEST_FIELD_MAP,
    body_path_exists,
)


@pytest.mark.parametrize("artifact", sorted(ARTIFACT_FIELD_MAPS))
def test_every_node_field_has_a_body_source(artifact: str) -> None:
    node_model, _body_model, field_map = ARTIFACT_FIELD_MAPS[artifact]
    # A new flat-node attribute must be classified deliberately; an unmapped one fails here
    # rather than extracting to null because the extractor was never told where it comes from.
    assert set(node_model.model_fields) == set(field_map)


@pytest.mark.parametrize("artifact", sorted(ARTIFACT_FIELD_MAPS))
def test_every_mapped_path_resolves_in_the_body(artifact: str) -> None:
    _node_model, body_model, field_map = ARTIFACT_FIELD_MAPS[artifact]
    for node_attr, path in field_map.items():
        assert body_path_exists(body_model, path), f"{artifact}.{node_attr} <- {path} is dangling"


def test_documented_rename_and_flattens_are_pinned() -> None:
    # The canonical rename off Graphiti's reserved created_at, and the flattens through
    # the nested timestamps/diff_stats sections.
    assert PULL_REQUEST_FIELD_MAP["opened_at"] == "pull_request.created_at"
    for flattened in ("first_review_at", "total_additions", "changed_files"):
        assert PULL_REQUEST_FIELD_MAP[flattened].count(".") == 2


def test_body_path_resolver_rejects_unknown_and_non_descending_paths() -> None:
    from relic.contracts.episode_body import PrEpisodeBody

    assert not body_path_exists(PrEpisodeBody, "pull_request.no_such_field")
    assert not body_path_exists(PrEpisodeBody, "pull_request.number.deeper")  # leaf, can't descend
    assert body_path_exists(PrEpisodeBody, "pull_request.timestamps.approved_at")
