"""Shared test fixtures.

``make_skill`` is a factory fixture: call it to build SkillIR instances with
sensible defaults and per-test overrides (id, status, title, ...).
"""

from collections.abc import Callable

import pytest

from relic.ontology.skill_ir import Citation, FieldSpec, SkillIR


@pytest.fixture
def make_skill() -> Callable[..., SkillIR]:
    def _make(
        skill_id: str = "pr-review-routing", *, status: str = "draft", **overrides
    ) -> SkillIR:
        data = {
            "skill_id": skill_id,
            "semver": "1.0.0",
            "title": "Route auth PRs",
            "description": "Use when opening or reviewing a PR under src/auth/**.",
            "scope": "repo",
            "inputs": {"pr_url": FieldSpec(type="string", description="The PR being opened")},
            "outputs": {
                "reviewer": FieldSpec(type="string", description="GitHub login to request")
            },
            "preconditions": ["PR touches src/auth/**"],
            "safety_checks": ["Reviewer is an active maintainer"],
            "citations": [
                Citation(label="PR #12", url="https://example.com/pr/12", source_type="pr")
            ],
            "status": status,
            "owner": "person_paris",
        }
        data.update(overrides)
        return SkillIR(**data)

    return _make
