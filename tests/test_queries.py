"""reviewers_of: search-first with the reviewer_walk Cypher fallback, through the seam.

Drives FakeMemory so both paths -- hybrid search and the deterministic reviewer_walk
fallback (which used to be untested because faking graphiti.driver was awkward) -- run
without Graphiti or FalkorDB.
"""

from conftest import FakeMemory
from relic.graph.memory import MemoryEdge, MemoryEntity
from relic.graph.queries import reviewers_of


def _edge(
    person_name: str = "paris",
    relation: str = "REVIEWED",
    fact: str = "paris reviewed auth",
    episodes: tuple[str, ...] = ("ep1",),
) -> MemoryEdge:
    person = MemoryEntity(
        uuid="p",
        name=person_name,
        labels=["Person"],
        attributes={"profile_url": "https://github.com/paris"},
    )
    work = MemoryEntity(uuid="w", name="relic-core#12", labels=["PullRequest"], attributes={})
    return MemoryEdge(
        relation=relation, fact=fact, source=person, target=work, episode_uuids=list(episodes)
    )


async def test_reviewers_of_returns_person_endpoints_from_search() -> None:
    hits = await reviewers_of(FakeMemory(edges=[_edge()]), "auth")
    assert len(hits) == 1
    assert hits[0].name == "paris"
    assert hits[0].relation == "REVIEWED"
    assert hits[0].profile_url == "https://github.com/paris"
    assert hits[0].episodes == ["ep1"]


async def test_reviewers_of_falls_back_to_reviewer_walk_when_search_empty() -> None:
    # search returns nothing; the deterministic walk supplies the hit
    memory = FakeMemory(edges=[], walk_edges=[_edge(person_name="zidan")])
    hits = await reviewers_of(memory, "auth")
    assert [h.name for h in hits] == ["zidan"]


async def test_reviewers_of_falls_back_when_search_raises() -> None:
    memory = FakeMemory(search_raises=True, walk_edges=[_edge(person_name="abhinav")])
    hits = await reviewers_of(memory, "auth")
    assert [h.name for h in hits] == ["abhinav"]


async def test_reviewers_of_skips_non_person_endpoints() -> None:
    work = MemoryEntity(uuid="w", name="relic-core#12", labels=["PullRequest"], attributes={})
    repo = MemoryEntity(uuid="r", name="demo/repo", labels=["Repo"], attributes={})
    edge = MemoryEdge(
        relation="IN_REPO", fact="pr in repo", source=work, target=repo, episode_uuids=[]
    )
    hits = await reviewers_of(FakeMemory(edges=[edge], walk_edges=[]), "auth")
    assert hits == []
