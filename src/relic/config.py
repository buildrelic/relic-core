"""Runtime settings, loaded from environment / .env.

Secrets are optional so importing this module (and `relic --help`) never needs a
populated .env. Call `get_settings()` to read them.
"""

from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    """Return cached settings read from environment / .env."""
    return Settings()
