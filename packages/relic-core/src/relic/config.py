"""Runtime settings, loaded from environment / .env.

Secrets are optional so importing this module (and `relic --help`) never needs a
populated .env. Call `get_settings()` to read them.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    github_token: str | None = None
    linear_api_key: str | None = None
    granola_api_key: str | None = None
    notion_api_key: str | None = None

    # The engram store. Postgres holds the relational memory tables; the schema is
    # owned here and applied by EngramStore.ensure_schema(), so the URL is the only
    # coordinate. The default matches docker-compose.yml.
    database_url: str = "postgresql://relic:relic@localhost:5432/relic"
    # The workspace every locally-ingested memory lands in. The web app writes with
    # the Clerk scope (orgId ?? userId); the CLI and daemon write here.
    relic_workspace: str = "local"

    # Firestore document mirror (optional). Same env names the landing app uses, so
    # one service account serves both sides. When unset, documents stay local-only:
    # memories.body in Postgres remains authoritative and document_id stays NULL.
    firebase_project_id: str | None = None
    firebase_client_email: str | None = None
    firebase_private_key: str | None = None

    registry_db_path: str = "./data/registry.db"
    target_repo: str | None = None

    # Concurrent GitHub hydration requests during fetch (rate-limit safe).
    fetch_concurrency: int = 10

    @field_validator("firebase_private_key", mode="after")
    @classmethod
    def _unescape_private_key(cls, value: str | None) -> str | None:
        """Unescape a PEM pasted with literal ``\\n`` sequences.

        Service-account keys copied out of JSON carry ``\\n`` escapes instead of
        newlines. PEMs legitimately contain whitespace, so this field deliberately
        skips ``_clean_secret``; blank values are still treated as unset.
        """
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned.startswith("#"):
            return None
        return cleaned.replace("\\n", "\n")

    @field_validator(
        "anthropic_api_key",
        "github_token",
        "linear_api_key",
        "granola_api_key",
        "notion_api_key",
        mode="after",
    )
    @classmethod
    def _clean_secret(cls, value: str | None) -> str | None:
        """Treat blank, whitespace-bearing, or inline-comment values as unset.

        A `.env` copied from `.env.example` can leave an inline comment as the
        value (python-dotenv keeps `KEY=  # note`). Real API tokens never contain
        whitespace or `#`, so reject those rather than send a garbage credential.
        """
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned.startswith("#") or any(c.isspace() for c in cleaned):
            return None
        return cleaned


@lru_cache
def get_settings() -> Settings:
    """Return cached settings read from environment / .env."""
    return Settings()
