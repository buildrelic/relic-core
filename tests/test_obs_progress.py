"""relic.obs.load_progress: the ingest progress bar wiring, drained out of the CLI.

Disabled yields a no-op (caller falls back to heartbeat logs); enabled yields an
``update(completed, counts)`` callback that does not raise.
"""

from relic.obs import load_progress


def test_disabled_yields_none():
    with load_progress("loading", 10, enabled=False) as update:
        assert update is None


def test_enabled_yields_a_working_update_callback():
    with load_progress("loading", 3, enabled=True) as update:
        assert update is not None
        update(1, "1✓")  # must not raise; renders to the shared (non-terminal) console
        update(3, "3✓")
