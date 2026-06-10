"""Settings: the deprecated SEMAPHORE_LIMIT fallback to the split concurrency knobs.

SEMAPHORE_LIMIT used to bound both phases; it is now FETCH_CONCURRENCY (fetch) and
GRAPHITI_MAX_COROUTINES (load). To avoid silently dropping an old `SEMAPHORE_LIMIT=3`,
the model validator applies it to whichever new knob the user did not set. These tests
pin that behavior and that it keys off what was actually provided (model_fields_set),
not off the values.
"""

import pytest
from pydantic_settings import SettingsConfigDict

from relic.config import Settings


class _IsolatedSettings(Settings):
    # Ignore any developer .env so the fallback is tested against inputs only.
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


@pytest.fixture(autouse=True)
def _clear_concurrency_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SEMAPHORE_LIMIT", "FETCH_CONCURRENCY", "GRAPHITI_MAX_COROUTINES"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_is_set() -> None:
    s = _IsolatedSettings()
    assert (s.fetch_concurrency, s.graphiti_max_coroutines) == (10, 20)


def test_semaphore_only_falls_back_to_both_knobs() -> None:
    # The reviewer's scenario: a legacy .env with only SEMAPHORE_LIMIT=3 must still throttle.
    s = _IsolatedSettings(semaphore_limit=3)
    assert (s.fetch_concurrency, s.graphiti_max_coroutines) == (3, 3)


def test_explicit_new_knob_wins_and_only_unset_falls_back() -> None:
    s = _IsolatedSettings(semaphore_limit=3, fetch_concurrency=15)
    assert (s.fetch_concurrency, s.graphiti_max_coroutines) == (15, 3)


def test_semaphore_from_env_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proves model_fields_set captures env-sourced fields, not just init kwargs.
    monkeypatch.setenv("SEMAPHORE_LIMIT", "4")
    s = _IsolatedSettings()
    assert (s.fetch_concurrency, s.graphiti_max_coroutines) == (4, 4)
