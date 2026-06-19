from relic.ingest import format_ingest_timing


def test_format_ingest_timing_block() -> None:
    block = format_ingest_timing(
        "astral-sh/uv",
        [
            ("fetch", 3.2),
            ("map + raw store", 0.5),
            ("load (extraction)", 756.1),
            ("setup + index", 0.2),
        ],
        760.0,
        loaded=39,
        skipped=0,
        failed=1,
        per_episode_seconds=18.9,
        bulk=False,
    )
    assert "ingest timing: astral-sh/uv (sequential)" in block
    assert "load (extraction)" in block
    assert "99.5%" in block  # 756.1 / 760.0
    assert "0.4%" in block  # 3.2 / 760.0
    assert "total" in block and "760.0s" in block
    assert "40 episodes: 39 loaded · 0 skipped · 1 failed" in block
    assert "~18.9s/episode" in block


def test_format_ingest_timing_handles_zero_total() -> None:
    # A stubbed or instant run must not divide by zero.
    block = format_ingest_timing(
        "o/r",
        [("fetch", 0.0), ("load (extraction)", 0.0)],
        0.0,
        loaded=0,
        skipped=0,
        failed=0,
        per_episode_seconds=0.0,
        bulk=True,
    )
    assert "ingest timing: o/r (bulk)" in block
    assert "0 episodes" in block
