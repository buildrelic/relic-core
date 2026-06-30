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

    async def search_(self, query, config=None, group_ids=None, **_kw):  # noqa: ANN001, ANN202
        # GraphitiMemory.search uses the cross-encoder recipe via graphiti.search_ (REL-11),
        # which returns a SearchResults object whose .edges carries the matched edges.
        from types import SimpleNamespace

        return SimpleNamespace(edges=self._edges)


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


# --- write path: rate-limit backoff (moved here with the backoff, from test_load) ---


def _write_spec(name: str):  # noqa: ANN202
    from datetime import UTC, datetime

    from relic.contracts import EpisodeSpec

    return EpisodeSpec(
        name=name,
        body="{}",
        source_description="t",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )


class _RateLimitedGraphiti:
    """Raises RateLimitError a set number of times on add_episode before succeeding."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.added: list[str] = []
        self.driver = object()

    async def add_episode(self, *, name, **_kwargs):  # noqa: ANN001, ANN202
        from graphiti_core.llm_client.errors import RateLimitError

        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RateLimitError
        self.added.append(name)


async def test_add_episode_retries_rate_limit_then_succeeds(monkeypatch) -> None:
    from tenacity import wait_none

    monkeypatch.setattr("relic.graph.memory._RETRY_WAIT", wait_none())  # no real sleeping
    graphiti = _RateLimitedGraphiti(fail_times=2)  # 429 twice, then succeeds

    await GraphitiMemory(graphiti).add_episode(_write_spec("PR demo/repo#1"))  # type: ignore[arg-type]

    assert graphiti.attempts == 3  # two retries then success
    assert graphiti.added == ["PR demo/repo#1"]


async def test_add_episode_gives_up_after_max_attempts(monkeypatch) -> None:
    import pytest
    from graphiti_core.llm_client.errors import RateLimitError
    from tenacity import wait_none

    monkeypatch.setattr("relic.graph.memory._RETRY_WAIT", wait_none())
    monkeypatch.setattr("relic.graph.memory._RETRY_ATTEMPTS", 3)
    graphiti = _RateLimitedGraphiti(fail_times=99)  # always rate limited

    with pytest.raises(RateLimitError):
        await GraphitiMemory(graphiti).add_episode(_write_spec("PR demo/repo#1"))  # type: ignore[arg-type]

    assert graphiti.attempts == 3  # capped at _RETRY_ATTEMPTS


# --- supersession cascade (REL-118), at the adapter level ----------------------
#
# supersede_episode owns the graph cascade (rescue corroborated facts, delete sole-owned)
# behind the seam, so it is tested here against a fake driver rather than in test_load.


class _FakeSupersedeDriver:
    """Dispatches the three Cypher shapes supersede_episode issues: the (name, group_id)
    episodic lookup, the edge-by-uuids read, and the targeted episodes prune."""

    def __init__(self, gh: "_SupersedeGraphiti") -> None:
        self._gh = gh

    async def execute_query(  # noqa: ANN201
        self,
        query,  # noqa: ANN001
        *,
        name: str = "",
        group_id: str = "",
        uuids=None,  # noqa: ANN001
        uuid: str = "",
        episodes=None,  # noqa: ANN001
        routing_: str = "w",
    ):
        gh = self._gh
        if "e.entity_edges" in query:  # episodic lookup
            ep_uuid = gh.episodes.get(name)
            if ep_uuid is None:
                return [], None, None
            return [{"uuid": ep_uuid, "edge_uuids": gh.entity_edges.get(ep_uuid, [])}], None, None
        if "WHERE r.uuid IN $uuids" in query:  # edge-by-uuids read
            rows = [
                {"uuid": u, "episodes": gh.edges[u]["episodes"]}
                for u in (uuids or [])
                if u in gh.edges
            ]
            return rows, None, None
        if "SET r.episodes" in query:  # targeted prune (keep the corroborated fact)
            gh.edges[uuid]["episodes"] = episodes or []
            gh.pruned.append((uuid, episodes or []))
            return [], None, None
        return [], None, None


class _SupersedeGraphiti:
    """A raw-Graphiti stand-in with just the driver + remove_episode supersede_episode uses.

    Models the episodic store (name -> uuid), each episode's entity_edges, and the edges
    (uuid -> episodes). remove_episode mirrors graphiti: it drops edges it still originates
    (episodes[0] == uuid) and forgets the node.
    """

    def __init__(self, seed, edges=None, entity_edges=None) -> None:  # noqa: ANN001
        self.episodes: dict[str, str] = dict(seed)  # name -> uuid
        self.edges: dict[str, dict[str, list[str]]] = edges or {}  # edge uuid -> {"episodes": ...}
        self.entity_edges: dict[str, list[str]] = entity_edges or {}  # episode uuid -> edge uuids
        self.removed: list[str] = []  # uuids passed to remove_episode, in order
        self.pruned: list[tuple[str, list[str]]] = []  # (edge uuid, new episodes) updates issued
        self.driver = _FakeSupersedeDriver(self)

    async def remove_episode(self, uuid: str) -> None:
        self.removed.append(uuid)
        self.episodes = {n: u for n, u in self.episodes.items() if u != uuid}
        # mirror graphiti: delete only edges this episode still originates (episodes[0] == uuid)
        self.edges = {
            eu: e
            for eu, e in self.edges.items()
            if not (e["episodes"] and e["episodes"][0] == uuid)
        }


async def test_supersede_rescues_corroborated_facts_deletes_sole_owned() -> None:
    # "A" originates two facts: "shared" is also supported by "B"; "solo" is A's alone.
    graphiti = _SupersedeGraphiti(
        seed={"Meeting x": "A"},
        edges={"shared": {"episodes": ["A", "B"]}, "solo": {"episodes": ["A"]}},
        entity_edges={"A": ["shared", "solo"]},
    )

    removed = await GraphitiMemory(graphiti).supersede_episode("Meeting x", "demo__repo")  # type: ignore[arg-type]

    assert removed == 1
    # the corroborated fact survives, with A pruned out of its support list (B now originates)
    assert graphiti.pruned == [("shared", ["B"])]
    assert graphiti.edges["shared"]["episodes"] == ["B"]
    # the fact only A supported is left unpruned, so remove_episode deletes it
    assert "solo" not in graphiti.edges
    assert graphiti.removed == ["A"]


async def test_supersede_absent_episode_is_a_noop() -> None:
    graphiti = _SupersedeGraphiti(seed={})

    removed = await GraphitiMemory(graphiti).supersede_episode("Nothing here", "demo__repo")  # type: ignore[arg-type]

    assert removed == 0
    assert graphiti.removed == []
