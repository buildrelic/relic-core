from mcp.types import TextContent

from conftest import FakeEngram, make_hit
from relic.engram.recall import (
    RecallAnswer,
    RecalledFact,
    Source,
    format_answer,
    recall,
)
from relic.registry.store import connect


def test_format_answer_empty() -> None:
    out = format_answer(RecallAnswer(query="who owns auth"))
    assert "No memory found" in out
    assert "who owns auth" in out


def test_format_answer_with_sources() -> None:
    # The load-bearing seam test: this block is what the daemon injects onto prompts
    # and the MCP recall_memory tool returns. Its shape must not drift (ADR-0007).
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
    assert out.startswith('Memory for "auth reviewers":\n\n')
    assert "- paris reviews auth PRs" in out
    assert (
        "source: PR buildrelic/relic-core#12 (https://github.com/buildrelic/relic-core/pull/12)"
        in out
    )


def test_format_answer_source_without_url_has_no_suffix() -> None:
    answer = RecallAnswer(
        query="q",
        facts=[RecalledFact(fact="f", relation="doc", sources=[Source(label="Notion abc")])],
    )
    assert "  source: Notion abc\n" in format_answer(answer) + "\n"


async def test_recall_maps_hits_to_cited_facts() -> None:
    memory = FakeEngram(
        hits=[
            make_hit(
                "PR owner/repo#12",
                title="Route auth PRs",
                snippet="alice requested a review on the auth change",
                url="https://github.com/owner/repo/pull/12",
            )
        ]
    )
    answer = await recall(memory, "auth reviewers")
    assert len(answer.facts) == 1
    fact = answer.facts[0]
    assert fact.fact == "Route auth PRs — alice requested a review on the auth change"
    assert fact.relation == "pull_request"
    assert fact.sources == [
        Source(label="PR owner/repo#12", url="https://github.com/owner/repo/pull/12")
    ]


async def test_recall_snippet_equal_to_title_collapses_to_title() -> None:
    memory = FakeEngram(hits=[make_hit(title="Route auth PRs", snippet="Route auth PRs")])
    answer = await recall(memory, "auth")
    assert answer.facts[0].fact == "Route auth PRs"


async def test_recall_never_raises_on_search_failure() -> None:
    answer = await recall(FakeEngram(search_raises=True), "anything")
    assert answer.is_empty


async def test_recall_passes_scope_to_search() -> None:
    memory = FakeEngram(hits=[make_hit()])
    await recall(memory, "auth", scope="owner__repo")
    assert memory.recorded_scopes == ["owner__repo"]


async def test_recall_unscoped_searches_whole_workspace() -> None:
    memory = FakeEngram(hits=[make_hit()])
    await recall(memory, "auth")
    assert memory.recorded_scopes == [None]


async def test_recall_respects_num_results() -> None:
    memory = FakeEngram(hits=[make_hit(f"PR owner/repo#{i}") for i in range(20)])
    answer = await recall(memory, "auth", num_results=5)
    assert len(answer.facts) == 5


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
