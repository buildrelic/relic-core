"""Settings: engram defaults, the secret sanitizer, and the Firebase PEM unescape.

The Firebase private key deliberately bypasses ``_clean_secret`` (PEMs contain
whitespace) and gets its own normalizer instead; these pin both behaviors so a
half-filled .env fails clean rather than sending a garbage credential, while a
pasted service-account key still parses.
"""

import pytest
from pydantic_settings import SettingsConfigDict

from relic.config import Settings


class _IsolatedSettings(Settings):
    # Ignore any developer .env so behavior is tested against inputs only.
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


@pytest.fixture(autouse=True)
def _clear_engram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DATABASE_URL",
        "RELIC_WORKSPACE",
        "FETCH_CONCURRENCY",
        "FIREBASE_PROJECT_ID",
        "FIREBASE_CLIENT_EMAIL",
        "FIREBASE_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_engram_defaults() -> None:
    s = _IsolatedSettings()
    assert s.database_url == "postgresql://relic:relic@localhost:5432/relic"
    assert s.relic_workspace == "local"
    assert s.fetch_concurrency == 10


def test_workspace_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELIC_WORKSPACE", "org_abc123")
    s = _IsolatedSettings()
    assert s.relic_workspace == "org_abc123"


def test_clean_secret_rejects_comment_and_whitespace_values() -> None:
    # A `.env` copied from `.env.example` can leave an inline comment as the value.
    assert _IsolatedSettings(github_token="# fill me in").github_token is None
    assert _IsolatedSettings(github_token="ghp abc").github_token is None
    assert _IsolatedSettings(github_token="  ").github_token is None
    assert _IsolatedSettings(github_token="ghp_real").github_token == "ghp_real"


def test_firebase_private_key_unescapes_newlines() -> None:
    # Service-account keys copied out of JSON carry literal \n escapes.
    pasted = "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"
    s = _IsolatedSettings(firebase_private_key=pasted)
    assert s.firebase_private_key == "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"


def test_firebase_private_key_keeps_real_newlines() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    s = _IsolatedSettings(firebase_private_key=pem)
    assert s.firebase_private_key == pem


def test_firebase_private_key_blank_is_unset() -> None:
    assert _IsolatedSettings(firebase_private_key="  ").firebase_private_key is None
    assert _IsolatedSettings(firebase_private_key="# todo").firebase_private_key is None
