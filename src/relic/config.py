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
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    github_token: str | None = None
    linear_api_key: str | None = None

    graphiti_llm_provider: str = "openai"
    engram_db_path: str = "./data/engram.kuzu"
    registry_db_path: str = "./data/registry.db"
    target_repo: str | None = None
    semaphore_limit: int = 10

    @field_validator(
        "anthropic_api_key",
        "gemini_api_key",
        "openai_api_key",
        "github_token",
        "linear_api_key",
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
