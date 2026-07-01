"""Runtime settings, loaded from environment / .env.

Secrets are optional so importing this module (and `relic --help`) never needs a
populated .env. Call `get_settings()` to read them.
"""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    github_token: str | None = None
    linear_api_key: str | None = None
    granola_api_key: str | None = None
    notion_api_key: str | None = None

    graphiti_llm_provider: str = "openai"
    # Model used when graphiti_llm_provider == "gemini". Configurable so a wrong or
    # region-specific model id is an env change, not a code change. Flash-Lite is the
    # fastest/cheapest Flash variant, suited to the high-volume extraction in a backfill.
    gemini_model: str = "gemini-2.5-flash-lite"
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_password: str | None = None
    falkordb_database: str = "relic"
    registry_db_path: str = "./data/registry.db"
    target_repo: str | None = None

    # Two distinct concurrency budgets, deliberately separate. The GitHub fetch wants a
    # modest fan-out to stay under API rate limits; the Graphiti load wants a higher
    # internal LLM concurrency (graphiti's own default is 20). A single shared knob would
    # force one to compromise the other. ``semaphore_limit`` is the deprecated shared knob:
    # when it is the only one a user set, ``_semaphore_limit_fallback`` applies it to the
    # new knobs so an old ``SEMAPHORE_LIMIT=3`` still throttles rather than being ignored.
    semaphore_limit: int = 10  # deprecated: use fetch_concurrency / graphiti_max_coroutines
    fetch_concurrency: int = 10  # concurrent GitHub hydration requests (rate-limit safe)
    graphiti_max_coroutines: int = 20  # graphiti internal LLM concurrency during load
    bulk_load: bool = False  # route the load through add_episode_bulk (see `relic ingest --bulk`)
    bulk_batch_size: int = 10  # episodes per add_episode_bulk call; smaller = less TPM burst

    @model_validator(mode="after")
    def _semaphore_limit_fallback(self) -> "Settings":
        """Honor a deprecated, explicitly-set SEMAPHORE_LIMIT as the fallback for the split knobs.

        SEMAPHORE_LIMIT used to bound both phases; it is now FETCH_CONCURRENCY (fetch) and
        GRAPHITI_MAX_COROUTINES (load). If someone still sets only SEMAPHORE_LIMIT (e.g. =3
        to dodge rate limits), apply it to whichever new knob they did not set so their
        intent isn't silently dropped, and tell them to migrate. ``model_fields_set`` carries
        the fields an env/.env/init actually provided, so untouched defaults don't trigger it.
        """
        if "semaphore_limit" not in self.model_fields_set:
            return self
        fell_back = [
            name
            for name in ("fetch_concurrency", "graphiti_max_coroutines")
            if name not in self.model_fields_set
        ]
        for name in fell_back:
            setattr(self, name, self.semaphore_limit)
        from relic.obs import get_logger

        log = get_logger("config")
        if fell_back:
            log.warning(
                "SEMAPHORE_LIMIT is deprecated; applied it (%d) to %s. Set FETCH_CONCURRENCY "
                "and GRAPHITI_MAX_COROUTINES instead.",
                self.semaphore_limit,
                ", ".join(fell_back),
            )
        else:
            log.warning(
                "SEMAPHORE_LIMIT is deprecated and ignored; FETCH_CONCURRENCY and "
                "GRAPHITI_MAX_COROUTINES override it."
            )
        return self

    @field_validator(
        "anthropic_api_key",
        "gemini_api_key",
        "openai_api_key",
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
