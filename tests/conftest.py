"""Shared test fixtures.

``make_skill`` is a factory fixture: call it to build SkillIR instances with
sensible defaults and per-test overrides (id, status, title, ...).

``FakeEngram`` is the one in-memory EngramReader + EngramWriter for store tests:
recall/queries/load run against it without Postgres or Firestore (ADR-0007).
Import it directly: ``from conftest import FakeEngram``.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from relic.contracts import EpisodeSpec
from relic.engram.store import EngramHit
from relic.ontology.skill_ir import Citation, FieldSpec, SkillIR


@pytest.fixture(autouse=True)
def _isolate_connector_secrets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test the CI-clean connector baseline, regardless of the developer's .env.

    A combined ``ingest --repo`` run (``_capture``) also fetches Linear and Granola whenever
    their keys are configured. ``get_settings`` is lru_cached and reads the real ``.env``, so a
    developer with ``GRANOLA_API_KEY`` / ``LINEAR_API_KEY`` set would have unit tests pull live
    data over the network — matching neither CI (which has no keys) nor the tests' mocked-
    GitHub-only intent. Null those secrets (an env var overrides the ``.env`` file in
    pydantic-settings) and reset the cache so settings re-read clean. A test that needs a key
    sets it explicitly and clears the cache itself. RELIC_WORKSPACE is pinned so a developer's
    .env can never point unit tests at a real workspace.
    """
    from relic.config import get_settings

    monkeypatch.setenv("GRANOLA_API_KEY", "")
    monkeypatch.setenv("LINEAR_API_KEY", "")
    monkeypatch.setenv("NOTION_API_KEY", "")
    monkeypatch.setenv("RELIC_WORKSPACE", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class FakeEngram:
    """An in-memory adapter satisfying EngramReader + EngramWriter for store tests.

    The reader returns canned hits; the writer records what it was asked to add and can
    be told to fail on specific episode names. No Postgres, no Firestore.
    """

    # reader state
    hits: list[EngramHit] = field(default_factory=list)
    reviewer_hits: list = field(default_factory=list)
    search_raises: bool = False
    recorded_scopes: list[str | None] = field(default_factory=list)  # each search call
    # writer state
    fail_on: set[str] = field(default_factory=set)
    added: list[str] = field(default_factory=list)
    schema_ensured: bool = False

    # -- reader --
    async def search(
        self, query: str, *, scope: str | None = None, num_results: int = 10
    ) -> list[EngramHit]:
        self.recorded_scopes.append(scope)
        if self.search_raises:
            raise RuntimeError("search unavailable")
        return self.hits[:num_results]

    async def reviewers_of(self, text: str, *, scope: str | None = None, limit: int = 10) -> list:
        if self.search_raises:
            raise RuntimeError("store unavailable")
        return self.reviewer_hits[:limit]

    # -- writer --
    async def add_episode(self, spec: EpisodeSpec) -> None:
        if spec.name in self.fail_on:
            raise RuntimeError(f"write blew up on {spec.name}")
        self.added.append(spec.name)

    async def ensure_schema(self) -> None:
        self.schema_ensured = True


def make_hit(
    name: str = "PR owner/repo#12",
    *,
    title: str = "Route auth PRs",
    url: str | None = "https://github.com/owner/repo/pull/12",
    snippet: str = "requested a review from alice on the auth change",
    artifact_type: str = "pull_request",
    scope: str = "owner__repo",
    rank: float = 1.0,
) -> EngramHit:
    """One canned search hit with per-test overrides."""
    return EngramHit(
        name=name,
        title=title,
        url=url,
        snippet=snippet,
        artifact_type=artifact_type,
        scope=scope,
        rank=rank,
    )


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
