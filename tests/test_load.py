"""load_episodes: counts, per-episode failure handling, resume skipping.

Drives a fake Graphiti so the loop's bookkeeping is tested without OpenAI or
FalkorDB. The real load -> query path against live Graphiti lives in
test_ingest_integration.py.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from graphiti_core.llm_client.errors import RateLimitError
from tenacity import wait_none

from relic.graph.load import load_episodes, load_episodes_bulk
from relic.ingest.mappers import EpisodeSpec

if TYPE_CHECKING:
    from graphiti_core import Graphiti


def _spec(name: str, group_id: str = "demo__repo") -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body="{}",
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id=group_id,
    )


class FakeGraphiti:
    """Minimal stand-in: records adds, can be told to fail on specific names."""

    def __init__(
        self, fail_on: set[str] | None = None, bulk_fail_on: set[str] | None = None
    ) -> None:
        self.fail_on = fail_on or set()
        self.bulk_fail_on = bulk_fail_on or set()
        self.added: list[str] = []
        self.bulk_batches: list[tuple[str, list[str]]] = []  # (group_id, [episode names])
        self.indices_built = False

    async def build_indices_and_constraints(self) -> None:
        self.indices_built = True

    async def add_episode(self, *, name: str, **_kwargs) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"extraction blew up on {name}")
        self.added.append(name)

    async def add_episode_bulk(self, bulk_episodes, *, group_id: str, **_kwargs) -> None:
        names = [e.name for e in bulk_episodes]
        if any(n in self.bulk_fail_on for n in names):
            raise RuntimeError(f"bulk blew up on {group_id}")
        self.bulk_batches.append((group_id, names))
        self.added.extend(names)


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


async def test_on_progress_fires_for_every_finalized_episode_sequential() -> None:
    # on_progress drives the CLI bar, so it must tick for skip/load/fail alike and the
    # completed count (loaded+skipped+failed) must reach the episode total.
    graphiti = FakeGraphiti(fail_on={"PR demo/repo#3"})
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]
    # snapshot the live (mutating) stats as a tuple at each call
    seen: list[tuple[int, int, int]] = []

    await load_episodes(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1"},  # 1 skipped, 1 loaded (#2), 1 failed (#3)
        on_progress=lambda s: seen.append((s.loaded, s.skipped, s.failed)),
        progress=False,
    )

    # one tick for the skip count, then one per finalized episode (2 pending)
    assert seen == [(0, 1, 0), (1, 1, 0), (1, 1, 1)]
    assert seen[-1] == (1, 1, 1)  # completed sum == 3 == len(specs)


async def test_on_progress_fires_per_batch_bulk() -> None:
    graphiti = FakeGraphiti()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]
    seen: list[tuple[int, int, int]] = []

    await load_episodes_bulk(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1"},  # 1 skipped, batch of 2 lands together
        batch_size=10,
        on_progress=lambda s: seen.append((s.loaded, s.skipped, s.failed)),
        progress=False,
    )

    # one tick for the skip count, then one per landed batch (bulk advances in chunks)
    assert seen == [(0, 1, 0), (2, 1, 0)]
    assert seen[-1] == (2, 1, 0)  # completed sum == 3 == len(specs)


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


async def test_bulk_groups_by_group_id_and_checkpoints_all() -> None:
    graphiti = FakeGraphiti()
    specs = [
        _spec("PR demo/repo#1"),
        _spec("PR demo/repo#2"),
        _spec("Issue LIN-1", group_id="linear"),
    ]
    recorded: list[str] = []

    stats = await load_episodes_bulk(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        on_loaded=recorded.append,
        progress=False,
    )

    assert graphiti.indices_built
    assert stats.loaded == 3
    assert stats.failed == 0
    # add_episode_bulk binds one graph per call, so each group_id is its own batch.
    assert graphiti.bulk_batches == [
        ("demo__repo", ["PR demo/repo#1", "PR demo/repo#2"]),
        ("linear", ["Issue LIN-1"]),
    ]
    # on_loaded fires for every episode in a landed batch (batch-granular checkpoint).
    assert recorded == ["PR demo/repo#1", "PR demo/repo#2", "Issue LIN-1"]


async def test_bulk_chunks_by_batch_size() -> None:
    graphiti = FakeGraphiti()
    specs = [_spec(f"PR demo/repo#{i}") for i in range(5)]

    stats = await load_episodes_bulk(
        cast("Graphiti", graphiti), specs, group_id="demo__repo", batch_size=2, progress=False
    )

    assert stats.loaded == 5
    assert [len(names) for _, names in graphiti.bulk_batches] == [2, 2, 1]


async def test_bulk_failed_batch_falls_back_to_per_episode() -> None:
    # The batch's bulk add fails; the fallback adds the good episodes one by one and
    # isolates the single poison episode as failed -- preserving the resilience guarantee.
    graphiti = FakeGraphiti(fail_on={"PR demo/repo#2"}, bulk_fail_on={"PR demo/repo#2"})
    specs = [_spec(f"PR demo/repo#{i}") for i in (1, 2, 3)]
    recorded: list[str] = []

    stats = await load_episodes_bulk(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        batch_size=10,
        on_loaded=recorded.append,
        progress=False,
    )

    assert stats.loaded == 2
    assert stats.failed == 1
    assert graphiti.added == ["PR demo/repo#1", "PR demo/repo#3"]
    assert recorded == ["PR demo/repo#1", "PR demo/repo#3"]
    assert [name for name, _ in stats.failures] == ["PR demo/repo#2"]


async def test_bulk_skip_resumes_without_reloading() -> None:
    graphiti = FakeGraphiti()
    specs = [_spec(f"PR demo/repo#{i}") for i in (1, 2, 3)]

    stats = await load_episodes_bulk(
        cast("Graphiti", graphiti),
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1", "PR demo/repo#2"},
        progress=False,
    )

    assert stats.skipped == 2
    assert stats.loaded == 1
    assert graphiti.added == ["PR demo/repo#3"]


class _FlakyGraphiti(FakeGraphiti):
    """Raises RateLimitError a set number of times per name before succeeding."""

    def __init__(self, rate_limit_times: dict[str, int]) -> None:
        super().__init__()
        self.rate_limit_times = rate_limit_times
        self.attempts: dict[str, int] = {}

    async def add_episode(self, *, name: str, **kwargs) -> None:
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if self.attempts[name] <= self.rate_limit_times.get(name, 0):
            raise RateLimitError
        await super().add_episode(name=name, **kwargs)


async def test_rate_limit_is_retried_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relic.graph.load._RETRY_WAIT", wait_none())  # no real sleeping in tests
    graphiti = _FlakyGraphiti({"PR demo/repo#1": 2})  # 429 twice, then succeeds

    stats = await load_episodes(
        cast("Graphiti", graphiti),
        [_spec("PR demo/repo#1")],
        group_id="demo__repo",
        progress=False,
    )

    assert stats.loaded == 1
    assert stats.failed == 0
    assert graphiti.attempts["PR demo/repo#1"] == 3  # two retries then success
    assert graphiti.added == ["PR demo/repo#1"]


async def test_rate_limit_gives_up_after_max_attempts_and_counts_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relic.graph.load._RETRY_WAIT", wait_none())
    monkeypatch.setattr("relic.graph.load._RETRY_ATTEMPTS", 3)
    graphiti = _FlakyGraphiti({"PR demo/repo#1": 99})  # always rate limited

    stats = await load_episodes(
        cast("Graphiti", graphiti),
        [_spec("PR demo/repo#1")],
        group_id="demo__repo",
        progress=False,
    )

    assert stats.loaded == 0
    assert stats.failed == 1
    assert graphiti.attempts["PR demo/repo#1"] == 3  # capped at _RETRY_ATTEMPTS
    assert "RateLimitError" in stats.failures[0][1]
