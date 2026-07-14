"""load_episodes: counts, per-episode failure handling, resume skipping, refresh.

Drives a FakeEngram (the in-memory EngramWriter) so the loop's bookkeeping is
tested without Postgres or Firestore. Supersession is structural now — the store's
upsert replaces a changed row in place — so the loop only *counts* refreshes; the
real upsert semantics are tested against a live Postgres in test_engram_store.py.
"""

from datetime import UTC, datetime

from conftest import FakeEngram
from relic.contracts import EpisodeSpec
from relic.engram.load import _content_token, load_episodes


def _spec(name: str, group_id: str = "demo__repo", body: str = "{}") -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body=body,
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id=group_id,
    )


async def test_all_episodes_load() -> None:
    memory = FakeEngram()
    specs = [_spec("PR demo/repo#1"), _spec("PR demo/repo#2"), _spec("Issue REL-10")]

    stats = await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert memory.schema_ensured
    assert stats.attempted == 3
    assert stats.loaded == 3
    assert stats.skipped == 0
    assert stats.failed == 0
    assert stats.episodes == 3  # back-compat alias
    assert memory.added == ["PR demo/repo#1", "PR demo/repo#2", "Issue REL-10"]
    assert stats.duration_s >= 0


async def test_failure_is_caught_counted_and_does_not_abort() -> None:
    memory = FakeEngram(fail_on={"PR demo/repo#2"})
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
    memory = FakeEngram(fail_on=set(names))
    specs = [_spec(n) for n in names]

    stats = await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert stats.loaded == 0
    assert stats.failed == 2
    assert stats.attempted == 2
    assert memory.added == []
    assert [name for name, _ in stats.failures] == names


async def test_skip_set_resumes_without_reloading() -> None:
    memory = FakeEngram()
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


async def test_on_progress_fires_for_every_finalized_episode() -> None:
    # on_progress drives the CLI bar, so it must tick for skip/load/fail alike and the
    # completed count (loaded+skipped+failed) must reach the episode total.
    memory = FakeEngram(fail_on={"PR demo/repo#3"})
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


async def test_on_loaded_fires_only_for_successes() -> None:
    memory = FakeEngram(fail_on={"PR demo/repo#3"})
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


# --- refresh (formerly supersession): a changed body re-lands in place ---------
#
# The store's upsert makes the replacement structural; the loop's job is only the
# token diff and the counter. The in-place row semantics live in test_engram_store.


async def test_changed_token_counts_as_superseded() -> None:
    memory = FakeEngram()
    spec = _spec("Meeting x", body='{"summary": "v2 edited"}')

    stats = await load_episodes(
        memory,
        [spec],
        group_id="demo__repo",
        skip={"Meeting x": "stale-token"},  # differs from the new body's hash -> refresh
        progress=False,
    )

    assert stats.superseded == 1
    assert stats.loaded == 0
    assert stats.skipped == 0
    assert memory.added == ["Meeting x"]  # the fresh body landed (upsert replaces in place)


async def test_unchanged_token_skips_without_rewrite() -> None:
    memory = FakeEngram()
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
    assert memory.added == []


async def test_legacy_none_token_never_refreshes() -> None:
    # An upgraded ledger carries name-only (token None) entries; they must skip forever,
    # never re-land, so upgrading does not force a mass re-ingest.
    memory = FakeEngram()
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
    assert memory.added == []


async def test_refreshes_land_before_new_adds() -> None:
    # A refreshed body replaces its stale row before any brand-new episode lands,
    # mirroring the old supersede-first ordering.
    memory = FakeEngram()
    changed = _spec("Meeting x", body='{"summary": "v2"}')
    fresh = [_spec(f"PR demo/repo#{i}") for i in range(3)]

    stats = await load_episodes(
        memory,
        [*fresh, changed],
        group_id="demo__repo",
        skip={"Meeting x": "stale-token"},
        progress=False,
    )

    assert stats.superseded == 1
    assert stats.loaded == 3
    assert memory.added[0] == "Meeting x"  # the refresh wave landed before any new add


async def test_on_loaded_records_content_token() -> None:
    memory = FakeEngram()
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


async def test_sequential_feed_order_preserved() -> None:
    memory = FakeEngram()
    specs = [_spec(f"PR demo/repo#{i}") for i in range(4)]

    await load_episodes(memory, specs, group_id="demo__repo", progress=False)

    assert memory.added == [s.name for s in specs]  # feed order preserved
