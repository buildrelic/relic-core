from collections.abc import Callable

from relic.ontology.skill_ir import SkillIR
from relic.serve.catalog import render_catalog


def test_render_catalog_lists_skills_with_links(make_skill: Callable[..., SkillIR]) -> None:
    skills = [
        make_skill("pr-review-routing"),
        make_skill(
            "deploy-flow", title="Deploy flow", description="Use when deploying.", scope="team"
        ),
    ]
    out = render_catalog(skills)
    assert "# Skills" in out
    assert "2 skills." in out
    assert "[Route auth PRs](pr-review-routing/SKILL.md)" in out
    assert "[Deploy flow](deploy-flow/SKILL.md)" in out
    assert "`deploy-flow` · team · v1.0.0" in out


def test_render_catalog_empty() -> None:
    assert "0 skills." in render_catalog([])


def test_render_catalog_singular(make_skill: Callable[..., SkillIR]) -> None:
    assert "1 skill." in render_catalog([make_skill("only-one")])
