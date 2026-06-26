"""GraphitiMemory adapter: attribute parsing, label cleaning, endpoint resolution.

The adapter's job is to hand callers relic value types with resolved endpoints, so the
Graphiti object reads (get_by_uuid, node.labels, the attributes dict-or-JSON) never leak
above the seam. These exercise that without a live FalkorDB.
"""

from relic.graph.memory import (
    GraphitiMemory,
    MemoryEntity,
    _clean_labels,
    _parse_attrs,
)


def test_parse_attrs_handles_dict_json_and_garbage() -> None:
    assert _parse_attrs({"profile_url": "x"}) == {"profile_url": "x"}
    assert _parse_attrs('{"profile_url": "x"}') == {"profile_url": "x"}  # JSON string
    assert _parse_attrs("not json") == {}
    assert _parse_attrs(None) == {}
    assert _parse_attrs(42) == {}


def test_clean_labels_drops_the_entity_marker() -> None:
    assert _clean_labels(["Entity", "Person"]) == ["Person"]
    assert _clean_labels(["PullRequest"]) == ["PullRequest"]
    assert _clean_labels(None) == []


class _FakeNode:
    def __init__(self, uuid: str, name: str, labels: list[str], attributes: object) -> None:
        self.uuid = uuid
        self.name = name
        self.labels = labels
        self.attributes = attributes


class _FakeEdge:
    def __init__(self, name: str, fact: str, source: str, target: str, episodes: list[str]) -> None:
        self.name = name
        self.fact = fact
        self.source_node_uuid = source
        self.target_node_uuid = target
        self.episodes = episodes


class _FakeGraphiti:
    def __init__(self, edges: list[_FakeEdge]) -> None:
        self._edges = edges
        self.driver = object()

    async def search(self, query, *, group_ids=None, num_results=10):  # noqa: ANN001, ANN202
        return self._edges


async def test_search_returns_edges_with_resolved_endpoints(monkeypatch) -> None:
    from graphiti_core.nodes import EntityNode

    nodes = {
        "p1": _FakeNode("p1", "alice", ["Entity", "Person"], {"profile_url": "https://gh/alice"}),
        "pr1": _FakeNode("pr1", "relic-core#42", ["Entity", "PullRequest"], None),
    }

    async def fake_get(_driver, uuid):  # noqa: ANN001, ANN202
        return nodes[uuid]

    monkeypatch.setattr(EntityNode, "get_by_uuid", fake_get)
    graphiti = _FakeGraphiti([_FakeEdge("AUTHORED", "alice authored #42", "p1", "pr1", ["ep1"])])

    edges = await GraphitiMemory(graphiti).search("auth")  # type: ignore[arg-type]

    assert len(edges) == 1
    edge = edges[0]
    assert edge.relation == "AUTHORED"
    assert edge.fact == "alice authored #42"
    assert edge.episode_uuids == ["ep1"]
    # endpoints arrive fully resolved, with the Entity marker stripped and attrs parsed
    assert edge.source == MemoryEntity(
        "p1", "alice", ["Person"], {"profile_url": "https://gh/alice"}
    )
    assert edge.target == MemoryEntity("pr1", "relic-core#42", ["PullRequest"], {})
