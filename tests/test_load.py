"""load_episodes: counts, per-episode failure handling, resume skipping.

Drives a fake Graphiti so the loop's bookkeeping is tested without OpenAI or
FalkorDB. The real load -> query path against live Graphiti lives in
test_ingest_integration.py.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from relic.graph.load import load_episodes
from relic.ingest.mappers import EpisodeSpec

if TYPE_CHECKING:
    from graphiti_core import Graphiti


def _spec(name: str) -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body="{}",
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )


class FakeGraphiti:
    """Minimal stand-in: records adds, can be told to fail on specific names."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.added: list[str] = []
        self.indices_built = False

    async def build_indices_and_constraints(self) -> None:
        self.indices_built = True

    async def add_episode(self, *, name: str, **_kwargs) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"extraction blew up on {name}")
        self.added.append(name)


async def test_all_episodes_load() -> None:
    graphiti = FakeGraphiti()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("Issue REL-10")]

    stats = await load_episodes(
        cast("Graphiti", graphiti), specs, group_id="demo__repo", progress=False
    )

    assert graphiti.indices_built
    assert stats.attempted == 3
    assert stats.loaded == 3
    assert stats.skipped == 0
    assert stats.failed == 0
    assert stats.episodes == 3  # back-compat alias
    assert graphiti.added == ["PR demo/repo#1", "PR demo/repo#2", "Issue REL-10"]
    assert stats.duration_s >= 0


async def test_failure_is_caught_counted_and_does_not_abort() -> None:
    graphiti = FakeGraphiti(fail_on={"PR demo/repo#2"})
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]

    stats = await load_episodes(
        cast("Graphiti", graphiti), specs, group_id="demo__repo", progress=False
    )

    assert stats.loaded == 2
    assert stats.failed == 1
    # the run continued past the failure to the last episode
    assert graphiti.added == ["PR demo/repo#1", "PR demo/repo#3"]
    assert len(stats.failures) == 1
    name, reason = stats.failures[0]
    assert name == "PR demo/repo#2"
    assert "RuntimeError" in reason


async def test_all_episodes_fail_loads_nothing() -> None:
    names = ["PR demo/repo#1", "PR demo/repo#2"]
    graphiti = FakeGraphiti(fail_on=set(names))
    specs = [_spec(n) for n in names]

    stats = await load_episodes(
        cast("Graphiti", graphiti), specs, group_id="demo__repo", progress=False
    )

    assert stats.loaded == 0
    assert stats.failed == 2
    assert stats.attempted == 2
    assert graphiti.added == []
    assert [name for name, _ in stats.failures] == names


async def test_skip_set_resumes_without_reloading() -> None:
    graphiti = FakeGraphiti()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]

    stats = await load_episodes(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1", "PR demo/repo#2"},
        progress=False,
    )

    assert stats.skipped == 2
    assert stats.loaded == 1
    assert graphiti.added == ["PR demo/repo#3"]


async def test_on_loaded_fires_only_for_successes() -> None:
    graphiti = FakeGraphiti(fail_on={"PR demo/repo#3"})
    specs = [
        _spec("PR demo/repo#1"),  # skipped
        _spec("PR demo/repo#2"),  # loaded
        _spec("PR demo/repo#3"),  # failed
    ]
    recorded: list[str] = []

    stats = await load_episodes(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1"},
        on_loaded=recorded.append,
        progress=False,
    )

    assert recorded == ["PR demo/repo#2"]
    assert stats.skipped == 1
    assert stats.loaded == 1
    assert stats.failed == 1
