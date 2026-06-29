"""ScopedMemory (ADR-0006): the principal-scoped read view is the only safe handle.

These pin the guarantees a serving surface relies on: every read is forced into the
principal's Zone-set, an empty set sees nothing without ever touching the inner reader,
a caller-supplied filter can only narrow, out-of-Zone nodes read as nonexistent, and the
global identity spine stays tenant-public.
"""

from conftest import FakeMemory
from relic.graph.memory import MemoryEdge, MemoryEntity, MemoryEpisode, ScopedMemory


def _edge(fact: str = "paris reviews auth", group_id: str = "team-a") -> MemoryEdge:
    person = MemoryEntity(uuid="p", name="paris", labels=["Person"])
    work = MemoryEntity(uuid="w", name="PR#1", labels=["PullRequest"], group_id=group_id)
    return MemoryEdge(relation="REVIEWED", fact=fact, source=person, target=work, group_id=group_id)


async def test_search_forces_the_principal_zone_filter() -> None:
    memory = ScopedMemory(FakeMemory(edges=[_edge()]), frozenset({"team-a"}))
    edges = await memory.search("auth")
    assert len(edges) == 1
    assert memory.inner.recorded_group_ids == [["team-a"]]  # scope pushed into the inner search


async def test_empty_zone_set_sees_nothing_and_never_calls_inner() -> None:
    memory = ScopedMemory(FakeMemory(edges=[_edge()]), frozenset())
    assert await memory.search("auth") == []
    assert memory.inner.recorded_group_ids == []  # fail closed: inner search not consulted


async def test_caller_group_ids_can_only_narrow_within_scope() -> None:
    memory = ScopedMemory(FakeMemory(edges=[_edge()]), frozenset({"team-a", "kb"}))
    await memory.search("auth", group_ids=["kb", "team-b"])  # team-b not held
    assert memory.inner.recorded_group_ids == [["kb"]]  # intersection only


async def test_caller_group_ids_disjoint_from_scope_sees_nothing() -> None:
    memory = ScopedMemory(FakeMemory(edges=[_edge()]), frozenset({"team-a"}))
    assert await memory.search("auth", group_ids=["team-b"]) == []
    assert memory.inner.recorded_group_ids == []


async def test_global_entity_is_visible_regardless_of_zone() -> None:
    # A Person is global (Zone-exempt): existence is tenant-public even with a Zone tag the
    # principal does not hold.
    person = MemoryEntity(uuid="p", name="paris", labels=["Person"], group_id="classified")
    memory = ScopedMemory(FakeMemory(entities={"p": person}), frozenset({"team-a"}))
    assert await memory.get_entity("p") == person


async def test_zoned_entity_in_scope_is_visible_out_of_scope_is_nonexistent() -> None:
    in_zone = MemoryEntity(uuid="a", name="PR#1", labels=["PullRequest"], group_id="team-a")
    out_zone = MemoryEntity(uuid="b", name="PR#2", labels=["PullRequest"], group_id="classified")
    memory = ScopedMemory(FakeMemory(entities={"a": in_zone, "b": out_zone}), frozenset({"team-a"}))
    assert await memory.get_entity("a") == in_zone
    assert await memory.get_entity("b") is None  # no error, no marker -- just absent


async def test_get_episode_out_of_zone_is_hidden() -> None:
    ep = MemoryEpisode(uuid="e", name="PR#9", content="{}", group_id="classified")
    memory = ScopedMemory(FakeMemory(episodes={"e": ep}), frozenset({"team-a"}))
    assert await memory.get_episode("e") is None


async def test_reviewer_walk_out_of_scope_group_id_yields_nothing() -> None:
    memory = ScopedMemory(FakeMemory(walk_edges=[_edge()]), frozenset({"team-a"}))
    assert await memory.reviewer_walk("auth", group_id="team-b", limit=5) == []
