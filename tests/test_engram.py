"""make_engram provider dispatch + the Gemini hybrid client builder.

make_engram resolves GRAPHITI_LLM_PROVIDER and the API keys, then dispatches to a
client-builder, all *before* it constructs the FalkorDriver or any network client. So
the dispatch's failure modes -- a missing key, an unknown provider -- are exercisable
here without OpenAI, Gemini, or FalkorDB. The Gemini *success* build is covered by
calling `_gemini_clients` directly (it only constructs client objects, no network); the
full happy path against live services stays in the gated test_ingest_integration.
"""

import pytest

from relic.config import Settings
from relic.graph.engram import _gemini_clients, make_engram


def _use_settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Pin get_settings() to a constructed Settings and neutralize any ambient OpenAI key.

    We set OPENAI_API_KEY to blank rather than deleting it: graphiti_core calls
    load_dotenv() at import (which make_engram triggers), and that would otherwise
    re-inject a developer's real .env key into os.environ. load_dotenv uses
    override=False, so a value already present wins -- and blank sanitizes to None in
    Settings and reads falsy in make_engram's os.environ lookup.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")
    settings = Settings(**overrides)
    monkeypatch.setattr("relic.config.get_settings", lambda: settings)


def test_openai_provider_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default provider needs a key, with a clear message.
    _use_settings(monkeypatch, graphiti_llm_provider="openai", openai_api_key=None)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        make_engram()


def test_gemini_without_gemini_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hybrid gemini needs the Gemini key for extraction; OpenAI key alone is not enough.
    _use_settings(
        monkeypatch, graphiti_llm_provider="gemini", gemini_api_key=None, openai_api_key="sk-test"
    )

    with pytest.raises(RuntimeError, match="needs GEMINI_API_KEY"):
        make_engram()


def test_gemini_without_openai_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hybrid gemini still needs the OpenAI key for embeddings/reranking.
    _use_settings(
        monkeypatch, graphiti_llm_provider="gemini", gemini_api_key="g-test", openai_api_key=None
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        make_engram()


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, graphiti_llm_provider="llama", openai_api_key="sk-test")

    with pytest.raises(RuntimeError, match="Unknown GRAPHITI_LLM_PROVIDER"):
        make_engram()


def test_provider_value_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    # "  GEMINI  " must route to the gemini branch, not the unknown-provider error.
    # We prove routing by withholding the OpenAI key: reaching the dual-key check (rather
    # than the "Unknown provider" error) means normalization worked.
    _use_settings(
        monkeypatch,
        graphiti_llm_provider="  GEMINI  ",
        gemini_api_key="g-test",
        openai_api_key=None,
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        make_engram()


def test_make_engram_passes_configured_gemini_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # make_engram should forward settings.gemini_model to the client builder. We capture
    # the builder's args and stop before FalkorDriver construction with a sentinel.
    _use_settings(
        monkeypatch,
        graphiti_llm_provider="gemini",
        gemini_api_key="g-test",
        openai_api_key="sk-test",
        gemini_model="gemini-custom-xyz",
    )
    captured: dict[str, str] = {}

    def _spy(gemini_key: str, openai_key: str, model: str):  # noqa: ANN202
        captured["gemini_key"] = gemini_key
        captured["openai_key"] = openai_key
        captured["model"] = model
        raise RuntimeError("sentinel: stop before driver")

    monkeypatch.setattr("relic.graph.engram._gemini_clients", _spy)

    with pytest.raises(RuntimeError, match="sentinel"):
        make_engram()
    assert captured == {
        "gemini_key": "g-test",
        "openai_key": "sk-test",
        "model": "gemini-custom-xyz",
    }


def test_prompt_json_patch_serializes_datetime() -> None:
    # graphiti's to_prompt_json crashes on datetime, which fails add_episode_bulk under
    # Gemini. The patch must make it (and the by-name re-imports) datetime-safe.
    import importlib
    from datetime import UTC, datetime

    from relic.graph.engram import _patch_prompt_json_datetime

    _patch_prompt_json_datetime()

    # Read the rebound reference from a consumer module (it imports to_prompt_json by
    # name, so this checks the patch reached the modules that actually call it).
    extract_nodes = importlib.import_module("graphiti_core.prompts.extract_nodes")
    out = extract_nodes.to_prompt_json({"created_at": datetime(2025, 1, 1, tzinfo=UTC)})
    assert "2025-01-01" in out


def test_gemini_clients_builds_hybrid_triple() -> None:
    # Direct build (no dispatch, no network): Gemini LLM + OpenAI embedder/reranker.
    # Removing the hybrid wiring forces this test to be updated alongside it.
    llm, embedder, cross_encoder = _gemini_clients("g-test", "sk-test", "gemini-2.5-flash-lite")

    assert type(llm).__name__ == "GeminiClient"
    assert llm.model == "gemini-2.5-flash-lite"
    assert type(embedder).__name__ == "OpenAIEmbedder"
    assert type(cross_encoder).__name__ == "OpenAIRerankerClient"


# --- The REL-93 ontology registration (docs/adr/0001-engram-graph-ontology.mdx) ---
#
# These lock the type dicts the LLM extractor is handed per add_episode, so a drift
# from the accepted ontology -- a re-added Review node, a reserved attribute name, an
# unfed Project type -- fails here rather than silently polluting a real graph.


def test_ontology_registers_the_rel93_in_scope_types() -> None:
    # engram registers exactly the REL-93 types the captured PR/issue bodies feed.
    from relic.graph.schema import EDGE_TYPES, ENTITY_TYPES

    assert set(ENTITY_TYPES) == {"Person", "Repo", "PullRequest", "Issue", "Label", "File"}
    assert set(EDGE_TYPES) == {
        "AUTHORED",
        "REVIEWED",
        "REQUESTED_REVIEW",
        "TOUCHES_PATH",
        "HAS_LABEL",
        "IN_REPO",
        "ASSIGNED_TO",
        "PARENT_OF",
        "CLOSES",
    }


def test_ontology_omits_removed_and_unfed_types() -> None:
    # Guard the ADR's "register only what a captured body feeds" calls against regression.
    from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

    assert "Review" not in ENTITY_TYPES  # Review is the REVIEWED edge, not a node
    assert "Procedure" not in ENTITY_TYPES  # detector/compiler is a separate track
    assert "Project" not in ENTITY_TYPES  # deferred: issue body carries no project field
    assert "REPORTS_TO" not in EDGE_TYPES  # deferred: no connector feeds org structure
    assert "IN_PROJECT" not in EDGE_TYPES
    assert ("Issue", "Repo") not in EDGE_TYPE_MAP  # issue body carries no repo (group_id does)


def test_edge_type_map_is_closed_over_registered_types() -> None:
    # Every (source, target) label and every relation in the map must be registered.
    from relic.graph.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES

    for (src, tgt), relations in EDGE_TYPE_MAP.items():
        assert src in ENTITY_TYPES, f"unregistered source label: {src}"
        assert tgt in ENTITY_TYPES, f"unregistered target label: {tgt}"
        for rel in relations:
            assert rel in EDGE_TYPES, f"unregistered relation: {rel}"


def test_entity_types_avoid_graphiti_reserved_names() -> None:
    # The guard behind the opened_at rename: a created_at attribute would collide with
    # Graphiti's EntityNode fields and be rejected by validate_entity_types.
    from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types

    from relic.graph.schema import ENTITY_TYPES

    assert validate_entity_types(ENTITY_TYPES) is True
