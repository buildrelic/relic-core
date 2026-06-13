"""Engram: Graphiti entity/edge type declarations and the graph factory.

Graphiti's custom types must be flat scalar models (no nested objects) and must
avoid the reserved attribute names: uuid, name, group_id, labels, created_at,
summary, attributes, name_embedding. So these *Node / edge models mirror the
rich ontology in `relic.ontology` rather than reusing it. The rich models stay
the source of truth for the typed API; these are the graph-facing view.

`entity_types` / `edge_types` are passed per `add_episode()` call in Phase 2,
not to the Graphiti constructor.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient

    # The provider-client triple make_engram hands to Graphiti.
    _ClientTriple = tuple[LLMClient, EmbedderClient, CrossEncoderClient]


# --- Flat entity types (Graphiti-facing) ------------------------------------


class PersonNode(BaseModel):
    """A person: teammate or external collaborator."""

    full_name: str | None = Field(None, description="Person's full name")
    preferred_name: str | None = Field(None, description="Preferred or short name")
    email: str | None = Field(None, description="Primary email")
    slack_handle: str | None = Field(None, description="Slack handle")
    external_org: str | None = Field(None, description="External org if not internal")
    profile_url: str | None = Field(None, description="Canonical profile URL")


class RepoNode(BaseModel):
    """A source code repository."""

    full_name: str | None = Field(None, description="owner/name slug, e.g. buildrelic/relic")
    url: str | None = Field(None, description="Canonical repository URL")
    default_branch: str | None = Field(None, description="Default branch name")


class PullRequestNode(BaseModel):
    """A pull request."""

    number: int | None = Field(None, description="PR number within its repo")
    title: str | None = Field(None, description="PR title")
    state: str | None = Field(None, description="open, closed, or merged")
    url: str | None = Field(None, description="Canonical PR URL")
    merged_at: datetime | None = Field(None, description="Merge timestamp if merged")


class ReviewNode(BaseModel):
    """A review left on a pull request."""

    state: str | None = Field(None, description="approved, changes_requested, commented")
    url: str | None = Field(None, description="Canonical review URL")
    submitted_at: datetime | None = Field(None, description="When the review was submitted")


class IssueNode(BaseModel):
    """A tracked issue or ticket (GitHub or Linear)."""

    identifier: str | None = Field(None, description="External id, e.g. REL-42 or repo#123")
    title: str | None = Field(None, description="Issue title")
    state: str | None = Field(None, description="Workflow state")
    url: str | None = Field(None, description="Canonical issue URL")


class ProcedureNode(BaseModel):
    """A standard operating procedure inferred from team activity.

    Note: `Procedure` is also a Graphiti built-in entity type. In Phase 2 decide
    whether to register this under a distinct key (e.g. "TeamProcedure").
    """

    archetype: str | None = Field(None, description="Procedure archetype/category")
    confidence: float | None = Field(None, description="Confidence 0..1")
    support: int | None = Field(None, description="Count of supporting episodes")


# --- Flat edge types (attributes only; Graphiti owns the endpoints) ---------


class Authored(BaseModel):
    """Person authored a pull request."""


class Reviewed(BaseModel):
    """Person reviewed a pull request."""

    state: str | None = Field(None, description="approved, changes_requested, commented")


class TouchesPath(BaseModel):
    """A pull request touched a file path."""

    path: str | None = Field(None, description="Repository-relative file path")
    additions: int | None = Field(None, description="Lines added")
    deletions: int | None = Field(None, description="Lines deleted")


class ReportsTo(BaseModel):
    """Person reports to another person."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": PersonNode,
    "Repo": RepoNode,
    "PullRequest": PullRequestNode,
    "Review": ReviewNode,
    "Issue": IssueNode,
    "Procedure": ProcedureNode,
}

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "AUTHORED": Authored,
    "REVIEWED": Reviewed,
    "TOUCHES_PATH": TouchesPath,
    "REPORTS_TO": ReportsTo,
}

# Keys are (source_label, target_label) using ENTITY_TYPES keys.
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "PullRequest"): ["AUTHORED", "REVIEWED"],
    ("Person", "Person"): ["REPORTS_TO"],
}


_falkordb_patched = False


def _patch_falkordb_empty_query() -> None:
    """Work around a graphiti-core <=0.29.1 bug on the FalkorDB fulltext builders.

    When an extracted entity's name sanitizes to nothing (all punctuation or
    stopwords, common in real history: version tags, file paths, single symbols),
    the builders emit `(@group_id:"x") ()` with empty trailing parens. RediSearch
    rejects that with a syntax error, aborting `add_episode`. Graphiti already
    treats an empty string as "skip the fulltext search", so we make the builders
    return '' when the text portion is empty. Remove this once upstream guards it.
    """
    global _falkordb_patched
    if _falkordb_patched:
        return

    import re

    from graphiti_core.driver import falkordb_driver
    from graphiti_core.driver.falkordb.operations import search_ops

    _empty_parens = re.compile(r"\(\s*\)\s*$")
    _group_filter = re.compile(r"\(@group_id:[^)]+\)")

    def _guard(fn):
        def wrapper(*args, **kwargs):
            out = fn(*args, **kwargs)
            if not isinstance(out, str):
                return out
            if _empty_parens.search(out):
                return ""
            # RediSearch treats - as a negation operator even inside quotes, so we must
            # escape hyphens in group_ids with backslashes. We do this on the final query
            # string to avoid failing upstream group_id string character validation.
            return _group_filter.sub(lambda m: m.group(0).replace("-", "\\-"), out)

        return wrapper

    search_ops._build_falkor_fulltext_query = _guard(search_ops._build_falkor_fulltext_query)
    falkordb_driver.FalkorDriver.build_fulltext_query = _guard(
        falkordb_driver.FalkorDriver.build_fulltext_query
    )
    _falkordb_patched = True


_prompt_json_patched = False

# Prompt modules that import `to_prompt_json` by name (so they hold their own reference
# and must be rebound individually). Sourced from graphiti-core 0.29.1.
_PROMPT_JSON_MODULES = (
    "extract_nodes",
    "extract_edges",
    "extract_nodes_and_edges",
    "dedupe_nodes",
    "summarize_nodes",
    "eval",
)


def _patch_prompt_json_datetime() -> None:
    """Make graphiti's prompt JSON serializer tolerate ``datetime`` values.

    graphiti's ``to_prompt_json`` calls ``json.dumps`` without a ``default`` handler. The
    bulk summary/dedup path feeds it node dicts that carry ``datetime`` fields (e.g.
    ``created_at``, ``valid_at``), so an ``add_episode_bulk`` batch raises ``TypeError:
    Object of type datetime is not JSON serializable`` and falls back to slow per-episode
    loading. This shows up with the Gemini provider, which populates those timestamps
    where OpenAI often leaves them null. The crash is at
    ``extract_nodes.extract_summaries_batch`` -> ``to_prompt_json(context['entities'])``.

    ``to_prompt_json`` is prompt-only (never a DB write), so ``default=str`` (ISO-ish text
    for the LLM) is safe. It is imported by name into several prompt modules, so we rebind
    it in each. Remove once upstream adds a default encoder.
    """
    global _prompt_json_patched
    if _prompt_json_patched:
        return

    import importlib
    import json

    from graphiti_core.prompts import prompt_helpers

    def _safe_to_prompt_json(data, ensure_ascii=False, indent=None):  # type: ignore[no-untyped-def]
        return json.dumps(data, ensure_ascii=ensure_ascii, indent=indent, default=str)

    setattr(prompt_helpers, "to_prompt_json", _safe_to_prompt_json)  # noqa: B010
    for module_name in _PROMPT_JSON_MODULES:
        try:
            module = importlib.import_module(f"graphiti_core.prompts.{module_name}")
        except Exception:  # noqa: BLE001 - a renamed/removed module just means nothing to patch
            continue
        if getattr(module, "to_prompt_json", None) is not None:
            setattr(module, "to_prompt_json", _safe_to_prompt_json)  # noqa: B010
    _prompt_json_patched = True


# --- LLM provider selection -------------------------------------------------
#
# Graphiti is provider-agnostic: it takes an llm_client, an embedder, and a
# cross_encoder. The single biggest operational constraint on a cold-start 12-month
# backfill is the LLM provider's tokens-per-minute (TPM) quota -- the load phase fans
# out ~6-8 extraction calls per episode, and a busy repo saturates a low usage tier,
# so the ingest spends time backing off (see `relic.graph.load._call_with_backoff`).
# Backoff makes the run *resilient* to TPM spikes but cannot raise the ceiling.
#
# Two providers are wired:
#   - "openai" (default): OpenAI for LLM + embeddings + reranker.
#   - "gemini" (hybrid): Gemini Flash for the LLM (its TPM limits are ~20x OpenAI's
#     low tiers, the throughput win for a large backfill), but OpenAI is kept for
#     embeddings and reranking. Embeddings stay on OpenAI deliberately: graphiti's
#     GeminiEmbedder forces batch_size=1 for gemini-embedding-001 (serial, slow), and
#     embeddings use a separate TPM pool that was never the bottleneck. So "gemini"
#     needs BOTH keys. Switching the embedder would change vector dimensions and break
#     similarity search against an OpenAI-built graph, so a provider switch is a
#     fresh-graph backfill (see `relic ingest --fresh`).


def _openai_embedder(api_key: str):  # noqa: ANN202 - graphiti embedder, kept local
    """OpenAI embedder (text-embedding-3-small). Shared by both providers."""
    from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig

    return OpenAIEmbedder(
        OpenAIEmbedderConfig(api_key=api_key, embedding_model="text-embedding-3-small")
    )


def _openai_reranker(api_key: str):  # noqa: ANN202 - graphiti cross-encoder, kept local
    """OpenAI cross-encoder reranker. Shared by both providers (recall path)."""
    from graphiti_core.cross_encoder import OpenAIRerankerClient
    from graphiti_core.llm_client import LLMConfig

    return OpenAIRerankerClient(config=LLMConfig(api_key=api_key, model="gpt-4o-mini"))


def _openai_clients(api_key: str) -> _ClientTriple:
    """Build the (llm_client, embedder, cross_encoder) triple backed by OpenAI.

    The default provider. Built here, not at import, so `relic --help` stays key-free.
    """
    from graphiti_core.llm_client import LLMConfig, OpenAIClient

    llm_config = LLMConfig(api_key=api_key, model="gpt-4o-mini", small_model="gpt-4o-mini")
    return OpenAIClient(config=llm_config), _openai_embedder(api_key), _openai_reranker(api_key)


def _gemini_clients(gemini_key: str, openai_key: str, model: str) -> _ClientTriple:
    """Build a hybrid triple: Gemini Flash for the LLM, OpenAI for embeddings + rerank.

    Gemini is the throughput escape hatch when OpenAI's TPM caps a backfill. Only the
    LLM (extraction) moves to Gemini; embeddings/reranking stay on OpenAI for the
    reasons in the module note above -- hence both keys are required. Graphiti maps
    Gemini 429s to the same `RateLimitError` the load-phase backoff retries on
    (gemini_client.py), so `_call_with_backoff` works unchanged.
    """
    from graphiti_core.llm_client import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

    llm = GeminiClient(config=LLMConfig(api_key=gemini_key, model=model))
    return llm, _openai_embedder(openai_key), _openai_reranker(openai_key)


def make_engram(
    *,
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    database: str | None = None,
    api_key: str | None = None,
    max_coroutines: int | None = None,
) -> Graphiti:
    """Build a Graphiti client on FalkorDB, using the configured LLM provider.

    Connection params fall back to settings (FALKORDB_*) when unset, so callers can
    pass nothing for the default local instance. FalkorDB is a networked, multi-tenant
    graph server: run one locally (e.g. `docker compose up -d falkordb`) before
    `relic ingest` or `relic query`.

    The provider is selected by `GRAPHITI_LLM_PROVIDER` (default "openai"). "gemini" is
    hybrid -- Gemini Flash for the LLM, OpenAI for embeddings/reranking -- so it needs
    both keys (see the module note above). Keys come from `api_key`/OPENAI_API_KEY and
    settings; a clear error is raised if a required one is missing. Clients are built
    here, not at import, so `relic --help` stays key-free.
    """
    import os

    from graphiti_core import Graphiti
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    from relic.config import get_settings

    _patch_falkordb_empty_query()
    _patch_prompt_json_datetime()

    settings = get_settings()
    provider = (settings.graphiti_llm_provider or "openai").strip().lower()
    key = api_key or os.environ.get("OPENAI_API_KEY") or settings.openai_api_key
    host = host if host is not None else settings.falkordb_host
    port = port if port is not None else settings.falkordb_port
    password = password if password is not None else settings.falkordb_password
    database = database if database is not None else settings.falkordb_database

    if provider == "openai":
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is required for `relic ingest` and `relic query`. "
                "Set it in .env or the environment."
            )
        llm_client, embedder, cross_encoder = _openai_clients(key)
    elif provider == "gemini":
        # Hybrid: Gemini for extraction, OpenAI for embeddings/reranking -- needs both.
        if not settings.gemini_api_key or not key:
            raise RuntimeError(
                "GRAPHITI_LLM_PROVIDER=gemini needs GEMINI_API_KEY (extraction) and "
                "OPENAI_API_KEY (embeddings). Set both in .env or the environment."
            )
        llm_client, embedder, cross_encoder = _gemini_clients(
            settings.gemini_api_key, key, settings.gemini_model
        )
    else:
        raise RuntimeError(
            f"Unknown GRAPHITI_LLM_PROVIDER={provider!r}; expected 'openai' (the default) "
            "or 'gemini'."
        )

    return Graphiti(
        graph_driver=FalkorDriver(host=host, port=port, password=password, database=database),
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
        max_coroutines=max_coroutines,
    )


def falkordb_reachable(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True if a TCP connection to ``host:port`` opens within ``timeout`` seconds.

    A cheap liveness probe, no graph query. Callers use it to fail fast with a
    clear message when FalkorDB is down, rather than letting the driver raise deep
    in a constructor-scheduled background task.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def ensure_indexes(graphiti: Graphiti) -> None:
    """Build Graphiti's indices and constraints (idempotent).

    FalkorDB honours `build_indices_and_constraints()`, so search works once it has
    run. The FalkorDriver also schedules this in its constructor, but we await it
    explicitly here so indexes exist before the first episode lands.
    """
    await graphiti.build_indices_and_constraints()
