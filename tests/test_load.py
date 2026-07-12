"""load_episodes: counts, per-episode failure handling, resume skipping, supersession.

Drives a FakeMemory (the in-memory MemoryWriter) so the loop's bookkeeping is tested
without Graphiti, FalkorDB, or OpenAI. The rate-limit backoff and the supersession graph
cascade live behind the seam now, so their tests live in test_memory; the real load ->
query path against live Graphiti lives in test_ingest_integration.py.
"""

from datetime import UTC, datetime

from conftest import FakeMemory
from relic.contracts import EpisodeSpec
from relic.graph.load import _content_token, load_episodes, load_episodes_bulk


def _spec(name: str, group_id: str = "demo__repo", body: str = "{}") -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body=body,
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id=group_id,
    )


async def test_all_episodes_load() -> None:
    memory = FakeMemory()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("Issue REL-10")]

    stats = await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert memory.indices_built
    assert stats.attempted == 3
    assert stats.loaded == 3
    assert stats.skipped == 0
    assert stats.failed == 0
    assert stats.episodes == 3  # back-compat alias
    assert memory.added == ["PR demo/repo#1", "PR demo/repo#2", "Issue REL-10"]
    assert stats.duration_s >= 0


async def test_failure_is_caught_counted_and_does_not_abort() -> None:
    memory = FakeMemory(fail_on={"PR demo/repo#2"})
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]

    stats = await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert stats.loaded == 2
    assert stats.failed == 1
    # the run continued past the failure to the last episode
    assert memory.added == ["PR demo/repo#1", "PR demo/repo#3"]
    assert len(stats.failures) == 1
    name, reason = stats.failures[0]
    assert name == "PR demo/repo#2"
    assert "RuntimeError" in reason


async def test_all_episodes_fail_loads_nothing() -> None:
    names = ["PR demo/repo#1", "PR demo/repo#2"]
    memory = FakeMemory(fail_on=set(names))
    specs = [_spec(n) for n in names]

    stats = await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert stats.loaded == 0
    assert stats.failed == 2
    assert stats.attempted == 2
    assert memory.added == []
    assert [name for name, _ in stats.failures] == names


async def test_skip_set_resumes_without_reloading() -> None:
    memory = FakeMemory()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]

    stats = await load_episodes(
        memory,
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1", "PR demo/repo#2"},
        progress=False,
    )

    assert stats.skipped == 2
    assert stats.loaded == 1
    assert memory.added == ["PR demo/repo#3"]


async def test_on_progress_fires_for_every_finalized_episode_sequential() -> None:
    # on_progress drives the CLI bar, so it must tick for skip/load/fail alike and the
    # completed count (loaded+skipped+failed) must reach the episode total.
    memory = FakeMemory(fail_on={"PR demo/repo#3"})
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]
    # snapshot the live (mutating) stats as a tuple at each call
    seen: list[tuple[int, int, int]] = []

    await load_episodes(
        memory,
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
    memory = FakeMemory()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("PR demo/repo#3")]
    seen: list[tuple[int, int, int]] = []

    await load_episodes_bulk(
        memory,
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
    memory = FakeMemory(fail_on={"PR demo/repo#3"})
    specs = [
        _spec("PR demo/repo#1"),  # skipped
        _spec("PR demo/repo#2"),  # loaded
        _spec("PR demo/repo#3"),  # failed
    ]
    recorded: list[str] = []

    stats = await load_episodes(
        memory,
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1"},
        on_loaded=lambda name, _token: recorded.append(name),
        progress=False,
    )

    assert recorded == ["PR demo/repo#2"]
    assert stats.skipped == 1
    assert stats.loaded == 1
    assert stats.failed == 1


async def test_bulk_groups_by_group_id_and_checkpoints_all() -> None:
    memory = FakeMemory()
    specs = [
        _spec("PR demo/repo#1"),
        _spec("PR demo/repo#2"),
        _spec("Issue LIN-1", group_id="linear"),
    ]
    recorded: list[str] = []

    stats = await load_episodes_bulk(
        memory,
        specs,
        group_id="demo__repo",
        on_loaded=lambda name, _token: recorded.append(name),
        progress=False,
    )

    assert memory.indices_built
    assert stats.loaded == 3
    assert stats.failed == 0
    # add_episode_bulk binds one graph per call, so each group_id is its own batch.
    assert memory.bulk_batches == [
        ("demo__repo", ["PR demo/repo#1", "PR demo/repo#2"]),
        ("linear", ["Issue LIN-1"]),
    ]
    # on_loaded fires for every episode in a landed batch (batch-granular checkpoint).
    assert recorded == ["PR demo/repo#1", "PR demo/repo#2", "Issue LIN-1"]


async def test_bulk_chunks_by_batch_size() -> None:
    memory = FakeMemory()
    specs = [_spec(f"PR demo/repo#{i}") for i in range(5)]

    stats = await load_episodes_bulk(
        memory, specs, group_id="demo__repo", batch_size=2, progress=False
    )

    assert stats.loaded == 5
    assert [len(names) for _, names in memory.bulk_batches] == [2, 2, 1]


async def test_bulk_failed_batch_falls_back_to_per_episode() -> None:
    # The batch's bulk add fails; the fallback adds the good episodes one by one and
    # isolates the single poison episode as failed -- preserving the resilience guarantee.
    memory = FakeMemory(fail_on={"PR demo/repo#2"}, bulk_fail_on={"PR demo/repo#2"})
    specs = [_spec(f"PR demo/repo#{i}") for i in (1, 2, 3)]
    recorded: list[str] = []

    stats = await load_episodes_bulk(
        memory,
        specs,
        group_id="demo__repo",
        batch_size=10,
        on_loaded=lambda name, _token: recorded.append(name),
        progress=False,
    )

    assert stats.loaded == 2
    assert stats.failed == 1
    assert memory.added == ["PR demo/repo#1", "PR demo/repo#3"]
    assert recorded == ["PR demo/repo#1", "PR demo/repo#3"]
    assert [name for name, _ in stats.failures] == ["PR demo/repo#2"]


async def test_bulk_skip_resumes_without_reloading() -> None:
    memory = FakeMemory()
    specs = [_spec(f"PR demo/repo#{i}") for i in (1, 2, 3)]

    stats = await load_episodes_bulk(
        memory,
        specs,
        group_id="demo__repo",
        skip={"PR demo/repo#1", "PR demo/repo#2"},
        progress=False,
    )

    assert stats.skipped == 2
    assert stats.loaded == 1
    assert memory.added == ["PR demo/repo#3"]


# --- supersession (REL-118): a changed body re-extracts in place ---------------
#
# These exercise the loop's supersession *ordering and bookkeeping* through the seam: the
# graph cascade supersede_episode stands in for (rescue corroborated facts, delete
# sole-owned) is tested at the adapter level in test_memory.


async def test_changed_token_supersedes_prior_episode() -> None:
    memory = FakeMemory(supersede_counts={"Meeting x": 1})  # one prior episode to remove
    spec = _spec("Meeting x", body='{"summary": "v2 edited"}')

    stats = await load_episodes(
        memory,
        [spec],
        group_id="demo__repo",
        skip={"Meeting x": "stale-token"},  # differs from the new body's hash -> supersede
        progress=False,
    )

    assert stats.superseded == 1
    assert stats.loaded == 0
    assert stats.skipped == 0
    assert memory.superseded == ["Meeting x"]  # prior episode removed first
    assert memory.added == ["Meeting x"]  # then the fresh body re-added


async def test_unchanged_token_skips_without_removal() -> None:
    memory = FakeMemory()
    spec = _spec("Meeting x", body='{"summary": "v1"}')

    stats = await load_episodes(
        memory,
        [spec],
        group_id="demo__repo",
        skip={"Meeting x": _content_token(spec)},  # token matches -> skip, no work
        progress=False,
    )

    assert stats.skipped == 1
    assert stats.superseded == 0
    assert memory.superseded == []
    assert memory.added == []


async def test_legacy_none_token_never_supersedes() -> None:
    # An upgraded ledger carries name-only (token None) entries; they must skip forever,
    # never re-extract, so upgrading does not force a mass re-ingest.
    memory = FakeMemory()
    spec = _spec("Meeting x", body='{"summary": "v2 edited"}')

    stats = await load_episodes(
        memory,
        [spec],
        group_id="demo__repo",
        skip={"Meeting x": None},
        progress=False,
    )

    assert stats.skipped == 1
    assert stats.superseded == 0
    assert memory.superseded == []


async def test_on_loaded_records_content_token() -> None:
    memory = FakeMemory()
    spec = _spec("PR demo/repo#1", body='{"x": 1}')
    recorded: list[tuple[str, str | None]] = []

    await load_episodes(
        memory,
        [spec],
        group_id="demo__repo",
        on_loaded=lambda name, token: recorded.append((name, token)),
        progress=False,
    )

    assert recorded == [("PR demo/repo#1", _content_token(spec))]


# --- bounded concurrency: overlap the LLM-bound episode adds -------------------
#
# concurrency=1 (the default, covered by every test above) keeps the strictly
# sequential feed; these pin the fan-out mode: the bound is respected, adds genuinely
# overlap, per-episode semantics (checkpoint writes, failure isolation) are unchanged,
# and supersessions complete before the first brand-new add.


class _TrackingMemory(FakeMemory):
    """FakeMemory whose add_episode yields to the loop and records the in-flight high-water.

    The sleep(0) forces a real suspension per add, so overlap is observable: a sequential
    drain never has two adds in flight, a concurrent one does, and the semaphore bound is
    the measured ceiling.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.in_flight = 0
        self.max_in_flight = 0

    async def add_episode(self, spec: EpisodeSpec) -> None:
        import asyncio

        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await super().add_episode(spec)
        finally:
            self.in_flight -= 1


async def test_concurrent_load_overlaps_within_the_bound() -> None:
    memory = _TrackingMemory()
    specs = [_spec(f"PR demo/repo#{i}") for i in range(6)]

    stats = await load_episodes(
        memory, specs, group_id="demo__repo", progress=False, concurrency=3
    )

    assert stats.loaded == 6
    assert stats.failed == 0
    assert sorted(memory.added) == sorted(s.name for s in specs)  # order may interleave
    assert memory.max_in_flight <= 3  # the bound is a hard ceiling
    assert memory.max_in_flight >= 2  # and adds genuinely overlapped


async def test_sequential_default_never_overlaps() -> None:
    memory = _TrackingMemory()
    specs = [_spec(f"PR demo/repo#{i}") for i in range(4)]

    await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert memory.max_in_flight == 1
    assert memory.added == [s.name for s in specs]  # feed order preserved


async def test_concurrent_checkpoint_writes_stay_per_episode() -> None:
    # on_loaded (the checkpoint write) must fire once per landed episode with its own
    # token, not batch-granularly, so a crash mid-drain resumes per episode.
    memory = _TrackingMemory()
    specs = [_spec(f"PR demo/repo#{i}", body=f'{{"i": {i}}}') for i in range(5)]
    recorded: list[tuple[str, str | None]] = []

    await load_episodes(
        memory,
        specs,
        group_id="demo__repo",
        on_loaded=lambda name, token: recorded.append((name, token)),
        progress=False,
        concurrency=4,
    )

    assert sorted(recorded) == sorted((s.name, _content_token(s)) for s in specs)


async def test_concurrent_failure_stays_isolated() -> None:
    memory = _TrackingMemory(fail_on={"PR demo/repo#2"})
    specs = [_spec(f"PR demo/repo#{i}") for i in range(5)]
    seen: list[tuple[int, int, int]] = []

    stats = await load_episodes(
        memory,
        specs,
        group_id="demo__repo",
        on_progress=lambda s: seen.append((s.loaded, s.skipped, s.failed)),
        progress=False,
        concurrency=3,
    )

    assert stats.loaded == 4
    assert stats.failed == 1
    assert [name for name, _ in stats.failures] == ["PR demo/repo#2"]
    assert seen[-1] == (4, 0, 1)  # the bar still reaches the episode total


async def test_concurrent_supersessions_complete_before_new_adds() -> None:
    # A refreshed body must replace its stale node before any brand-new episode extracts,
    # mirroring the sequential path's supersede-first ordering across the fan-out.
    memory = _TrackingMemory(supersede_counts={"Meeting x": 1})
    changed = _spec("Meeting x", body='{"summary": "v2"}')
    fresh = [_spec(f"PR demo/repo#{i}") for i in range(3)]

    stats = await load_episodes(
        memory,
        [changed, *fresh],
        group_id="demo__repo",
        skip={"Meeting x": "stale-token"},
        progress=False,
        concurrency=4,
    )

    assert stats.superseded == 1
    assert stats.loaded == 3
    assert memory.superseded == ["Meeting x"]
    assert memory.added[0] == "Meeting x"  # the supersede wave landed before any new add


async def test_bulk_supersede_runs_serially_before_batches() -> None:
    memory = FakeMemory(supersede_counts={"Meeting x": 1})
    changed = _spec("Meeting x", body='{"summary": "v2"}')
    fresh = _spec("PR demo/repo#1")

    stats = await load_episodes_bulk(
        memory,
        [changed, fresh],
        group_id="demo__repo",
        skip={"Meeting x": "stale-token"},
        batch_size=10,
        progress=False,
    )

    assert stats.superseded == 1
    assert stats.loaded == 1
    assert memory.superseded == ["Meeting x"]
    # the superseded episode lands via the serial pre-pass (add_episode); the fresh one batches
    assert "Meeting x" in memory.added
    assert ("demo__repo", ["PR demo/repo#1"]) in memory.bulk_batches
