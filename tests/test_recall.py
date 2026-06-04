from mcp.types import TextContent

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


class _FakeEdge:
    def __init__(self, fact: str, name: str, episodes: list[str]) -> None:
        self.fact = fact
        self.name = name
        self.episodes = episodes


class _FakeGraphiti:
    def __init__(self, edges: list[_FakeEdge], *, raises: bool = False) -> None:
        self._edges = edges
        self._raises = raises
        self.driver = None

    async def search(self, query: str, *, group_ids=None, num_results: int = 10):
        if self._raises:
            raise RuntimeError("FTS unavailable")
        return self._edges


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
    async def fake_resolve(_graphiti, episodes):
        return [Source(label=f"episode:{e}") for e in episodes]

    monkeypatch.setattr("relic.graph.recall._resolve_sources", fake_resolve)
    graphiti = _FakeGraphiti(
        [
            _FakeEdge("paris reviews auth PRs", "REVIEWED", ["ep1"]),
            _FakeEdge("   ", "BLANK", ["ep2"]),
        ]
    )
    answer = await recall(graphiti, "auth reviewers")  # type: ignore[arg-type]
    assert len(answer.facts) == 1
    assert answer.facts[0].fact == "paris reviews auth PRs"
    assert answer.facts[0].sources == [Source(label="episode:ep1")]


async def test_recall_never_raises_on_search_failure() -> None:
    answer = await recall(_FakeGraphiti([], raises=True), "anything")  # type: ignore[arg-type]
    assert answer.is_empty


def test_extract_url_prefers_pr_then_issue() -> None:
    assert _extract_url('{"pull_request": {"url": "https://x/pr/12"}}') == "https://x/pr/12"
    assert _extract_url('{"issue": {"url": "https://x/issue/42"}}') == "https://x/issue/42"
    assert _extract_url('{"repo": {"url": "https://x/repo"}}') is None  # repo is not the source
    assert _extract_url("not json") is None
    assert _extract_url(None) is None


async def test_resolve_sources_adds_name_and_url(monkeypatch) -> None:
    from graphiti_core.nodes import EpisodicNode

    class _Episode:
        name = "PR buildrelic/relic-core#12"
        content = '{"pull_request": {"url": "https://github.com/buildrelic/relic-core/pull/12"}}'

    async def fake_get(_driver, _uuid):
        return _Episode()

    monkeypatch.setattr(EpisodicNode, "get_by_uuid", fake_get)
    sources = await _resolve_sources(_FakeGraphiti([]), ["ep1"])  # type: ignore[arg-type]
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
