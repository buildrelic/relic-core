import json
from datetime import UTC, datetime

from relic.ontology.entities import Procedure, ProcedureEvidence
from relic.ontology.primitives import AuditInfo, TextBlob
from relic.ontology.skill_ir import SkillIR


def test_skill_ir_json_schema_is_valid() -> None:
    schema = SkillIR.model_json_schema()
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    props = schema["properties"]
    for key in ("skill_id", "semver", "title", "scope", "owner", "status"):
        assert key in props
    assert "$defs" in schema
    assert "Citation" in schema["$defs"]
    assert "FieldSpec" in schema["$defs"]
    json.dumps(schema)  # must be JSON-serializable


def test_procedure_round_trips() -> None:
    original = Procedure(
        id="proc_pr_review_routing_001",
        title="Route auth PRs to the right reviewer",
        archetype="pr-review-routing",
        description=TextBlob(markdown="Tag the auth owner on PRs touching src/auth/**."),
        confidence=0.86,
        support=7,
        evidence=[
            ProcedureEvidence(
                source_url="https://github.com/buildrelic/relic-core/pull/12",
                label="PR #12",
                kind="pr",
            )
        ],
        recommended_reviewer_ids=["person_paris"],
        scope="repo",
        status="draft",
        tags=["review", "auth"],
        audit=AuditInfo(
            created_by="person_paris",
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        ),
    )
    restored = Procedure.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.model_dump() == original.model_dump()
