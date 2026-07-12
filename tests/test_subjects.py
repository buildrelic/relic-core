"""plan_subject_writes: PR/issue body -> the deterministic Subject + dropped-edge plan.

Pure parsing, no Graphiti or FalkorDB. The adapter-level translation (EntityNode/Edge
writes, idempotency, episode attribution) is covered in test_memory.
"""

import json
from datetime import UTC, datetime

from relic.contracts import EpisodeSpec
from relic.graph.subjects import det_uuid, plan_subject_writes


def _spec(body: dict, *, group_id: str = "demo__repo") -> EpisodeSpec:
    return EpisodeSpec(
        name="PR demo/repo#7",
        body=json.dumps(body),
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id=group_id,
    )


def _pr_body(**overrides) -> dict:
    body = {
        "source_type": "pull_request",
        "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
        "pull_request": {
            "number": 7,
            "title": "add auth login",
            "url": "https://github.com/demo/repo/pull/7",
            "state": "merged",
            "created_at": "2025-01-01T00:00:00Z",
            "labels": ["auth"],
            "author": {"login": "alice", "profile_url": "https://github.com/alice"},
        },
        "requested_reviewers": [
            {"name": "bob", "kind": "user", "profile_url": "https://github.com/bob"},
            {"name": "team-sec", "kind": "team"},
        ],
        "linked_issues": [{"identifier": "demo/repo#3", "relation": "closes"}],
    }
    body.update(overrides)
    return body


def test_pull_request_subject_node() -> None:
    plan = plan_subject_writes(_spec(_pr_body()))
    assert plan is not None
    subject = plan.subject
    assert subject.label == "PullRequest"
    assert subject.name == "demo/repo#7"  # repo-scoped identity, matches the episode name
    assert subject.attributes["number"] == 7
    assert subject.attributes["url"] == "https://github.com/demo/repo/pull/7"
    assert subject.attributes["state"] == "merged"
    assert subject.attributes["opened_at"] == "2025-01-01T00:00:00Z"  # the body's created_at


def test_pull_request_dropped_edges() -> None:
    plan = plan_subject_writes(_spec(_pr_body()))
    assert plan is not None
    by_rel = {e.relation: e for e in plan.edges}

    # IN_REPO -> Repo, outbound from the Subject
    assert by_rel["IN_REPO"].direction == "out"
    assert by_rel["IN_REPO"].target.label == "Repo"
    assert by_rel["IN_REPO"].target.name == "demo/repo"

    # CLOSES -> the linked Issue, with the relation carried on the edge
    assert by_rel["CLOSES"].direction == "out"
    assert by_rel["CLOSES"].target.label == "Issue"
    assert by_rel["CLOSES"].target.name == "demo/repo#3"
    assert by_rel["CLOSES"].attributes == {"relation": "closes"}

    # REQUESTED_REVIEW -> Person, inbound (Person -> PR); the team request is skipped
    rr = by_rel["REQUESTED_REVIEW"]
    assert rr.direction == "in"
    assert rr.target.label == "Person"
    assert rr.target.name == "bob"
    assert rr.target.attributes["github_login"] == "bob"
    assert sum(1 for e in plan.edges if e.relation == "REQUESTED_REVIEW") == 1


def test_pull_request_author_and_file_edges() -> None:
    # REL-94 escape hatches: AUTHORED from the body's author login, TOUCHES_PATH from
    # its file paths -- exact body data, measured as fumbled by the extractor.
    plan = plan_subject_writes(
        _spec(_pr_body(files=[{"path": "src/auth/login.py"}, {"path": "docs/auth.md"}]))
    )
    assert plan is not None

    authored = [e for e in plan.edges if e.relation == "AUTHORED"]
    assert len(authored) == 1
    assert authored[0].direction == "in"  # Person -> PR
    assert authored[0].target.label == "Person"
    assert authored[0].target.name == "alice"
    assert authored[0].target.attributes["github_login"] == "alice"

    touches = [e for e in plan.edges if e.relation == "TOUCHES_PATH"]
    assert [e.target.name for e in touches] == ["src/auth/login.py", "docs/auth.md"]
    assert all(e.direction == "out" and e.target.label == "File" for e in touches)


def test_pull_request_ghost_author_plans_no_authored_edge() -> None:
    # A ghost/deleted opener has author: null -- no Person identity, no edge.
    ghost_pr = {**_pr_body()["pull_request"], "author": None}
    plan = plan_subject_writes(_spec(_pr_body(pull_request=ghost_pr)))
    assert plan is not None
    assert all(e.relation != "AUTHORED" for e in plan.edges)


def test_pull_request_without_linked_issues_has_no_closes() -> None:
    plan = plan_subject_writes(_spec(_pr_body(linked_issues=[])))
    assert plan is not None
    assert all(e.relation != "CLOSES" for e in plan.edges)


def test_issue_subject_and_parent_edge() -> None:
    body = {
        "source_type": "issue",
        "issue": {
            "identifier": "REL-99",
            "title": "deterministic subject",
            "url": "https://linear.app/x/REL-99",
            "state": "in_progress",
            "parent": "REL-93",
        },
    }
    plan = plan_subject_writes(_spec(body))
    assert plan is not None
    assert plan.subject.label == "Issue"
    assert plan.subject.name == "REL-99"
    assert plan.subject.attributes["url"] == "https://linear.app/x/REL-99"
    parent = next(e for e in plan.edges if e.relation == "PARENT_OF")
    assert parent.direction == "in"  # parent -> child; the Subject is the child
    assert parent.target.name == "REL-93"


def test_unknown_or_malformed_body_returns_none() -> None:
    assert plan_subject_writes(_spec({"source_type": "commit"})) is None
    bad = EpisodeSpec(
        name="x",
        body="not json",
        source_description="t",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="g",
    )
    assert plan_subject_writes(bad) is None
    # a pull_request body missing the required pull_request section -> None
    incomplete = {"source_type": "pull_request", "repo": {"full_name": "a/b", "url": "u"}}
    assert plan_subject_writes(_spec(incomplete)) is None


def test_det_uuid_is_stable_and_scoped() -> None:
    base = det_uuid("g1", "PullRequest", "demo/repo#7")
    assert base == det_uuid("g1", "PullRequest", "demo/repo#7")  # stable across calls
    assert base != det_uuid("g2", "PullRequest", "demo/repo#7")  # group-scoped
    assert base != det_uuid("g1", "Issue", "demo/repo#7")  # label-scoped
