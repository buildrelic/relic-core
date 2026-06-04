from relic.compile.render import render
from relic.ontology.skill_ir import Citation, FieldSpec, SkillIR


def test_render_includes_grounded_fields() -> None:
    skill = SkillIR(
        skill_id="pr-review-routing",
        semver="1.0.0",
        title="Route auth PRs",
        description="Use when opening or reviewing a PR under src/auth/**.",
        scope="repo",
        inputs={"pr_url": FieldSpec(type="string", description="The PR being opened")},
        outputs={"reviewer": FieldSpec(type="string", description="GitHub login to request")},
        preconditions=["PR touches src/auth/**"],
        safety_checks=["Reviewer is an active maintainer"],
        citations=[Citation(label="PR #12", url="https://example.com/pr/12", source_type="pr")],
        owner="person_paris",
    )
    out = render(skill)
    assert "# Route auth PRs" in out
    assert "v1.0.0" in out
    assert "pr_url" in out
    assert "reviewer" in out
    assert "[PR #12](https://example.com/pr/12)" in out
    assert "**Scope:** repo" in out


def test_render_marks_optional_and_default_inputs() -> None:
    skill = SkillIR(
        skill_id="paged-search",
        semver="1.0.0",
        title="Paged search",
        description="Search with paging.",
        scope="repo",
        inputs={
            "query": FieldSpec(type="string", description="search text"),
            "limit": FieldSpec(
                type="integer", description="max results", required=False, default=10
            ),
        },
        owner="person_paris",
    )
    out = render(skill)
    assert "`query` (string): search text" in out
    assert "optional" in out
    assert "default: 10" in out
