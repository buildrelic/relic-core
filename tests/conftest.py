"""Shared test fixtures.

``make_skill`` is a factory fixture: call it to build SkillIR instances with
sensible defaults and per-test overrides (id, status, title, ...).

``FakeMemory`` is the one in-memory MemoryReader + MemoryWriter for graph tests: it
replaces the ad-hoc per-module Graphiti fakes so recall/queries/load run without
FalkorDB, OpenAI, or Graphiti (ADR-0003). Import it directly: ``from conftest import
FakeMemory``.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from relic.contracts import EpisodeSpec
from relic.graph.memory import MemoryEdge, MemoryEntity, MemoryEpisode
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
    sets it explicitly and clears the cache itself.
    """
    from relic.config import get_settings

    monkeypatch.setenv("GRANOLA_API_KEY", "")
    monkeypatch.setenv("LINEAR_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class FakeMemory:
    """An in-memory adapter satisfying MemoryReader + MemoryWriter for graph tests.

    The reader returns canned values; the writer records what it was asked to add and can
    be told to fail on specific episode names. No Graphiti, no FalkorDB, no LLM.
    """

    # reader state
    edges: list[MemoryEdge] = field(default_factory=list)
    episodes: dict[str, MemoryEpisode] = field(default_factory=dict)
    entities: dict[str, MemoryEntity] = field(default_factory=dict)  # uuid -> node, for get_entity
    walk_edges: list[MemoryEdge] = field(default_factory=list)
    search_raises: bool = False
    recorded_group_ids: list[list[str] | None] = field(default_factory=list)  # each search call
    # writer state
    fail_on: set[str] = field(default_factory=set)
    bulk_fail_on: set[str] = field(default_factory=set)
    added: list[str] = field(default_factory=list)
    bulk_batches: list[tuple[str, list[str]]] = field(default_factory=list)  # (group_id, names)
    indices_built: bool = False
    superseded: list[str] = field(default_factory=list)  # names passed to supersede_episode
    supersede_counts: dict[str, int] = field(default_factory=dict)  # name -> prior episodes removed

    # -- reader --
    async def search(
        self, query: str, *, group_ids: list[str] | None = None, num_results: int = 10
    ) -> list[MemoryEdge]:
        self.recorded_group_ids.append(group_ids)
        if self.search_raises:
            raise RuntimeError("search unavailable")
        return self.edges[:num_results]

    async def get_episode(self, uuid: str) -> MemoryEpisode | None:
        return self.episodes.get(uuid)

    async def get_entity(self, uuid: str) -> MemoryEntity | None:
        return self.entities.get(uuid)

    async def reviewer_walk(
        self, query: str, *, group_id: str | None, limit: int
    ) -> list[MemoryEdge]:
        return self.walk_edges[:limit]

    # -- writer --
    async def add_episode(self, spec: EpisodeSpec) -> None:
        if spec.name in self.fail_on:
            raise RuntimeError(f"extraction blew up on {spec.name}")
        self.added.append(spec.name)

    async def add_episode_bulk(self, specs: list[EpisodeSpec]) -> None:
        names = [spec.name for spec in specs]
        if any(name in self.bulk_fail_on for name in names):
            raise RuntimeError(f"bulk blew up on {specs[0].group_id if specs else '?'}")
        self.bulk_batches.append((specs[0].group_id, names))
        self.added.extend(names)

    async def build_indices(self) -> None:
        self.indices_built = True

    async def supersede_episode(self, name: str, group_id: str) -> int:
        """Record the supersede request and return a configured prior-episode count.

        The loop only logs the count and increments ``stats.superseded`` on the re-add, so a
        loop test just asserts on ``superseded``/``added``. The graph cascade it stands in
        for is tested at the adapter level (test_memory) against a fake driver.
        """
        self.superseded.append(name)
        return self.supersede_counts.get(name, 0)


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
