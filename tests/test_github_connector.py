"""Unit tests for the pure helpers in the GitHub connector (no network)."""

from relic.ingest.github import _parent_identifier, _pr_state


def test_pr_state_merged() -> None:
    # GraphQL's state enum already folds a merge in: a merged PR is MERGED, never CLOSED.
    assert _pr_state({"state": "MERGED"}) == "merged"


def test_pr_state_closed_without_merge() -> None:
    assert _pr_state({"state": "CLOSED"}) == "closed"


def test_pr_state_missing_is_empty() -> None:
    # Defensive: the query always selects state, but a missing field shouldn't blow up.
    assert _pr_state({}) == ""


def test_parent_identifier_parses_subissue_url() -> None:
    # GitHub returns the parent's REST URL inline on a sub-issue.
    url = "https://api.github.com/repos/astral-sh/uv/issues/18506"
    assert _parent_identifier(url) == "astral-sh/uv#18506"


def test_parent_identifier_none_for_top_level_issue() -> None:
    assert _parent_identifier(None) is None


def test_parent_identifier_none_on_unparseable_url() -> None:
    assert _parent_identifier("https://example.com/not-an-issue") is None
