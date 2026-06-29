from datetime import datetime

from mcp.types import TextContent

from conftest import FakeMemory
from relic.graph.memory import MemoryEdge, MemoryEntity, MemoryEpisode
from relic.graph.recall import (
    RecallAnswer,
    RecalledFact,
    Source,
    _extract_url,
    _resolve_sources,
    format_answer,
    recall,
)
from relic.registry.store import connect


def _edge(
    fact: str, relation: str, episodes: list[str], *, invalid_at: datetime | None = None
) -> MemoryEdge:
    person = MemoryEntity(uuid="p", name="paris", labels=["Person"], attributes={})
    work = MemoryEntity(uuid="w", name="PR#1", labels=["PullRequest"], attributes={})
    return MemoryEdge(
        relation=relation,
        fact=fact,
        source=person,
        target=work,
        episode_uuids=episodes,
        invalid_at=invalid_at,  # set => the fact is superseded, not current
    )


def test_format_answer_empty() -> None:
    out = format_answer(RecallAnswer(query="who owns auth"))
    assert "No memory found" in out
    assert "who owns auth" in out


def test_format_answer_with_sources() -> None:
    answer = RecallAnswer(
        query="auth reviewers",
        facts=[
            RecalledFact(
                fact="paris reviews auth PRs",
                relation="REVIEWED",
                sources=[
                    Source(
                        label="PR buildrelic/relic-core#12",
                        url="https://github.com/buildrelic/relic-core/pull/12",
                    )
                ],
            )
        ],
    )
    out = format_answer(answer)
    assert "- paris reviews auth PRs" in out
    assert (
        "source: PR buildrelic/relic-core#12 (https://github.com/buildrelic/relic-core/pull/12)"
        in out
    )


async def test_recall_maps_edges_and_drops_blank_facts(monkeypatch) -> None:
    # Fetch the submodule object via sys.modules: `relic.graph` re-exports the `recall`
    # function, which shadows the same-named submodule on attribute access, so a string
    # target cannot traverse it. import_module returns the real module to patch its internal.
    import importlib

    recall_module = importlib.import_module("relic.graph.recall")

    async def fake_resolve(_memory, episodes):
        return [Source(label=f"episode:{e}") for e in episodes]

    monkeypatch.setattr(recall_module, "_resolve_sources", fake_resolve)
    memory = FakeMemory(
        edges=[
            _edge("paris reviews auth PRs", "REVIEWED", ["ep1"]),
            _edge("   ", "BLANK", ["ep2"]),
        ]
    )
    answer = await recall(memory, "auth reviewers")
    assert len(answer.facts) == 1
    assert answer.facts[0].fact == "paris reviews auth PRs"
    assert answer.facts[0].sources == [Source(label="episode:ep1")]


async def test_recall_never_raises_on_search_failure() -> None:
    answer = await recall(FakeMemory(search_raises=True), "anything")
    assert answer.is_empty


async def test_recall_fail_closed_on_empty_zones() -> None:
    # ADR-0005: a principal with no accessible Zones sees nothing, and search is never
    # consulted -- so even a fake brimming with edges yields an empty answer.
    memory = FakeMemory(edges=[_edge("classified fact", "REVIEWED", ["ep1"])])
    answer = await recall(memory, "anything", zones=[])
    assert answer.is_empty
    assert memory.recorded_group_ids == []  # search not called at all


async def test_recall_passes_zones_to_search_as_filter() -> None:
    memory = FakeMemory(edges=[_edge("paris reviews auth PRs", "REVIEWED", ["ep1"])])
    await recall(memory, "auth", zones=["team-a", "kb"])
    assert memory.recorded_group_ids == [["team-a", "kb"]]


async def test_recall_legacy_group_id_still_scopes() -> None:
    memory = FakeMemory(edges=[_edge("paris reviews auth PRs", "REVIEWED", ["ep1"])])
    await recall(memory, "auth", group_id="team-a")
    assert memory.recorded_group_ids == [["team-a"]]


async def test_recall_drops_superseded_facts_by_default() -> None:
    # ADR-0006: a fact a later episode invalidated is never surfaced as current.
    from datetime import UTC, datetime

    superseded = datetime(2020, 1, 1, tzinfo=UTC)
    memory = FakeMemory(
        edges=[
            _edge("paris owns auth", "OWNS", ["ep1"]),
            _edge("robin owned auth", "OWNS", ["ep2"], invalid_at=superseded),
        ]
    )
    answer = await recall(memory, "auth owner")
    assert [f.fact for f in answer.facts] == ["paris owns auth"]


async def test_recall_include_superseded_keeps_history() -> None:
    from datetime import UTC, datetime

    superseded = datetime(2020, 1, 1, tzinfo=UTC)
    memory = FakeMemory(edges=[_edge("robin owned auth", "OWNS", ["ep2"], invalid_at=superseded)])
    answer = await recall(memory, "auth owner", include_superseded=True)
    assert len(answer.facts) == 1


def test_memory_edge_is_current_tracks_both_invalidation_and_expiry() -> None:
    # A fact is current only while neither bound is set; expiry (a later episode
    # contradicted it) supersedes it just as invalidation does.
    from datetime import UTC, datetime

    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    assert _edge("x", "OWNS", []).is_current
    assert not _edge("x", "OWNS", [], invalid_at=stamp).is_current
    person = MemoryEntity(uuid="p", name="paris", labels=["Person"])
    expired = MemoryEdge(relation="OWNS", fact="x", source=person, target=person, expired_at=stamp)
    assert not expired.is_current


def test_extract_url_prefers_pr_then_issue() -> None:
    assert _extract_url('{"pull_request": {"url": "https://x/pr/12"}}') == "https://x/pr/12"
    assert _extract_url('{"issue": {"url": "https://x/issue/42"}}') == "https://x/issue/42"
    assert _extract_url('{"repo": {"url": "https://x/repo"}}') is None  # repo is not the source
    assert _extract_url("not json") is None
    assert _extract_url(None) is None


async def test_resolve_sources_adds_name_and_url() -> None:
    memory = FakeMemory(
        episodes={
            "ep1": MemoryEpisode(
                uuid="ep1",
                name="PR buildrelic/relic-core#12",
                content='{"pull_request": {"url": "https://github.com/buildrelic/relic-core/pull/12"}}',
            )
        }
    )
    sources = await _resolve_sources(memory, ["ep1"])
    assert sources == [
        Source(
            label="PR buildrelic/relic-core#12",
            url="https://github.com/buildrelic/relic-core/pull/12",
        )
    ]


async def test_mcp_recall_tool_exposed_when_recall_fn_given(tmp_path) -> None:
    from relic.serve.mcp_server import build_server

    conn = connect(tmp_path / "registry.db")

    async def recall_fn(query: str, num_results: int = 10) -> str:
        return f"recalled: {query} (n={num_results})"

    server = build_server(conn, recall_fn=recall_fn)
    tools = await server.list_tools()
    assert "recall_memory" in {t.name for t in tools}

    result = await server.call_tool("recall_memory", {"query": "auth"})
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert "recalled: auth" in block.text


async def test_mcp_recall_tool_absent_without_recall_fn(tmp_path) -> None:
    from relic.serve.mcp_server import build_server

    conn = connect(tmp_path / "registry.db")
    server = build_server(conn)
    tools = await server.list_tools()
    assert "recall_memory" not in {t.name for t in tools}
