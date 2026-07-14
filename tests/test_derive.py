"""derive: the pure body -> row-fields mapping, per artifact type.

Pins what feeds FTS quality (search_text contents), what the web workspace
renders (markdown), and the join rows (people roles, paths). Offline: derive is
pure and never raises, so a malformed body degrades instead of failing.
"""

import json
from datetime import UTC, datetime

from relic.contracts import EpisodeSpec
from relic.engram.derive import derive


def _spec(body: dict | str, name: str = "PR demo/repo#1") -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body=body if isinstance(body, str) else json.dumps(body),
        source_description="test",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )


def _pr_body() -> dict:
    return {
        "source_type": "pull_request",
        "schema_version": 2,
        "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
        "pull_request": {
            "number": 1,
            "title": "Route auth PRs",
            "description": "Adds reviewer routing.",
            "url": "https://github.com/demo/repo/pull/1",
            "state": "merged",
            "labels": ["area:auth"],
            "author": {"login": "alice"},
        },
        "reviews": [{"reviewer": {"login": "bob"}, "state": "APPROVED", "comment": "ship it"}],
        "requested_reviewers": [
            {"name": "carol", "kind": "user"},
            {"name": "platform-team", "kind": "team"},
        ],
        "files": [{"path": "src/auth/token.py"}],
        "commits": [{"sha": "abc", "message": "feat: token rotation", "author_login": "alice"}],
        "linked_issues": [{"identifier": "demo/repo#9", "relation": "closes"}],
    }


def test_pr_derivation() -> None:
    d = derive(_spec(_pr_body()))
    assert d.source == "github"
    assert d.artifact_type == "pull_request"
    assert d.title == "Route auth PRs"
    assert d.url == "https://github.com/demo/repo/pull/1"
    # search_text carries the routing signal: people, reviews, labels, linked issues.
    for token in ("alice", "bob", "APPROVED", "ship it", "carol", "area:auth", "demo/repo#9"):
        assert token in d.search_text
    # Paths AND their segments, so FTS "auth" matches src/auth/token.py.
    assert "src/auth/token.py" in d.search_text
    assert "src auth token.py" in d.search_text
    # People with roles; team review requests carry no person row.
    assert ({"github_login": "alice", "display_name": None, "email": None}, "author") in d.people
    assert ({"github_login": "bob", "display_name": None, "email": None}, "reviewer") in d.people
    roles = {role for _, role in d.people}
    assert "requested_reviewer" in roles
    assert not any(p["github_login"] == "platform-team" for p, _ in d.people)
    assert d.paths == ["src/auth/token.py"]
    # markdown is the web document: title, link, and the review section.
    assert d.markdown.startswith("# Route auth PRs")
    assert "[PR demo/repo#1](https://github.com/demo/repo/pull/1)" in d.markdown
    assert "## Reviews" in d.markdown
    assert "`src/auth/token.py`" in d.markdown


def test_issue_derivation_linear_vs_github() -> None:
    linear = {
        "source_type": "issue",
        "schema_version": 2,
        "issue": {
            "identifier": "REL-42",
            "title": "Fix login",
            "url": "https://linear.app/x/REL-42",
            "state": "Done",
            "assignees": ["paris"],
            "labels": ["bug"],
        },
    }
    d = derive(_spec(linear, name="Issue REL-42"))
    assert d.source == "linear"
    assert d.artifact_type == "issue"
    assert d.url == "https://linear.app/x/REL-42"
    assert ({"github_login": "paris", "display_name": None, "email": None}, "assignee") in d.people
    assert "REL-42" in d.search_text and "bug" in d.search_text

    github = dict(linear)
    github["issue"] = {**linear["issue"], "identifier": "demo/repo#7"}
    assert derive(_spec(github, name="Issue demo/repo#7")).source == "github"


def test_doc_derivation() -> None:
    body = {
        "source_type": "doc",
        "schema_version": 2,
        "url": "https://notion.so/page-abc",
        "title": "Auth runbook",
        "doc_type": "other",
        "authors": [{"login": "Alice Smith"}],
        "body": "Rotate tokens quarterly.",
        "last_edited_at": "2025-06-01T00:00:00Z",
    }
    d = derive(_spec(body, name="Notion abc"))
    assert d.source == "notion"
    assert d.artifact_type == "doc"
    assert d.title == "Auth runbook"
    assert "Rotate tokens quarterly." in d.search_text
    assert "Rotate tokens quarterly." in d.markdown
    alice = {"github_login": "Alice Smith", "display_name": None, "email": None}
    assert (alice, "participant") in d.people


def test_conversation_derivation() -> None:
    body = {
        "source_type": "conversation",
        "schema_version": 2,
        "url": "https://granola.ai/n/1",
        "medium": "meeting",
        "title": "Auth sync",
        "participants": [{"login": "alice"}, {"login": "bob"}],
        "summary": "Decided to rotate tokens.",
        "transcript": "alice: let's rotate quarterly",
        "decisions": ["rotate tokens quarterly"],
    }
    d = derive(_spec(body, name="Meeting 1"))
    assert d.source == "granola"
    assert d.artifact_type == "conversation"
    assert "rotate tokens quarterly" in d.search_text
    assert "## Decisions" in d.markdown
    assert len(d.people) == 2


def test_agent_session_derivation() -> None:
    body = {
        "source_type": "agent_session",
        "schema_version": 2,
        "url": "session://abc",
        "title": "Session abc",
        "agent": "claude-code",
        "cwd": "/home/dev/relic-core",
        "transcript": "User: fix the auth bug\nAssistant: done",
        "files_touched": [{"path": "src/auth/login.py"}],
    }
    d = derive(_spec(body, name="AgentSession abc"))
    assert d.source == "claude-code"
    assert d.artifact_type == "agent_session"
    assert d.url == "session://abc"
    assert "relic-core" in d.search_text  # the cwd leaf is searchable
    assert "fix the auth bug" in d.search_text
    assert d.paths == ["src/auth/login.py"]


def test_unknown_source_type_degrades_with_top_level_url() -> None:
    body = {
        "source_type": "release",
        "url": "https://github.com/o/r/releases/tag/v1",
        "title": "v1",
        "notes": "first release",
    }
    d = derive(_spec(body, name="Release v1"))
    assert d.artifact_type == "release"
    assert d.url == "https://github.com/o/r/releases/tag/v1"
    assert "first release" in d.search_text


def test_malformed_body_degrades_to_raw_text() -> None:
    d = derive(_spec("not json at all", name="Broken x"))
    assert d.title == "Broken x"
    assert d.url is None
    assert "not json at all" in d.search_text
    assert d.people == [] and d.paths == []
