"""The flat Graphiti-facing ontology (``relic.graph.schema``): registration invariants.

These pin the ontology dicts the extractor is handed per ``add_episode`` -- no live
FalkorDB, no LLM. They guard the structural rules graphiti enforces (no reserved field
names, flat scalars) and the internal consistency of the three dicts, so a typo in an
edge name or a node label fails here, not silently at extraction time. The Session
artifact type (docs/adr/0001, the Session amendment) is checked explicitly.
"""

from pydantic import BaseModel

from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, AgentSessionNode

# Graphiti reserves these EntityNode field names; a custom attribute may not collide.
_RESERVED = {
    "uuid",
    "name",
    "group_id",
    "labels",
    "created_at",
    "summary",
    "attributes",
    "name_embedding",
}


def test_session_artifact_type_is_registered() -> None:
    assert ENTITY_TYPES["AgentSession"] is AgentSessionNode
    assert "TOUCHED" in EDGE_TYPES
    assert "REFERENCES" in EDGE_TYPES


def test_session_edge_map_links_work_to_artifacts() -> None:
    # AUTHORED is reused for Person -> Session; TOUCHED/REFERENCES are the deterministic
    # links to what the session worked on.
    assert EDGE_TYPE_MAP[("Person", "AgentSession")] == ["AUTHORED"]
    assert EDGE_TYPE_MAP[("AgentSession", "File")] == ["TOUCHED"]
    assert EDGE_TYPE_MAP[("AgentSession", "PullRequest")] == ["REFERENCES"]
    assert EDGE_TYPE_MAP[("AgentSession", "Issue")] == ["REFERENCES"]


def test_edge_map_only_names_registered_nodes_and_edges() -> None:
    # Every (source, target) uses ENTITY_TYPES labels; every edge name is in EDGE_TYPES.
    for (source, target), edges in EDGE_TYPE_MAP.items():
        assert source in ENTITY_TYPES, source
        assert target in ENTITY_TYPES, target
        for edge in edges:
            assert edge in EDGE_TYPES, edge


def test_entity_types_avoid_reserved_names_and_stay_flat() -> None:
    # Graphiti requires flat scalar models with no reserved field names. This is what
    # graphiti's validate_entity_types enforces; assert it directly for the whole set.
    from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types

    assert validate_entity_types(ENTITY_TYPES) is True

    for name, model in ENTITY_TYPES.items():
        for field_name, field in model.model_fields.items():
            assert field_name not in _RESERVED, f"{name}.{field_name} is reserved"
            # Flat: no nested BaseModel attributes (FalkorDB rejects non-scalar props).
            annotation = field.annotation
            assert not (isinstance(annotation, type) and issubclass(annotation, BaseModel)), (
                f"{name}.{field_name} is a nested model; must be a flat scalar"
            )
