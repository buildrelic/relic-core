"""GitHub connector: windowed GraphQL PR paging, node parsing, and issue windowing.

Drives fake githubkit clients (dict-in / dict-out, no network) so the fetch logic
is pinned without hitting GitHub. The live path is exercised end to end elsewhere.
"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from relic.ingest.github import _PR_QUERY, _fetch_issues, _fetch_prs_graphql, _pr_node_to_rec
from relic.ingest.mappers import RepoBundle, pr_to_episode

if TYPE_CHECKING:
    from githubkit import GitHub

CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)


def _repo() -> RepoBundle:
    return RepoBundle(full_name="o/r", url="https://github.com/o/r", default_branch="main")


def _node(
    number: int,
    *,
    updated: str,
    merged: str | None = None,
    closed: str | None = None,
    state: str = "MERGED",
    author: str | None = "alice",
    reviews: list[dict[str, Any]] | None = None,
    files: list[dict[str, Any]] | None = None,
    requested: list[dict[str, Any]] | None = None,
    labels: list[dict[str, Any]] | None = None,
    commits: list[dict[str, Any]] | None = None,
    linked: list[dict[str, Any]] | None = None,
    additions: int | None = None,
    deletions: int | None = None,
    changed_files: int | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
) -> dict[str, Any]:
    # Defaults model a merged PR (the common case); pass closed=/state="CLOSED" with no
    # merged= for a close-without-merge. closedAt falls back to mergedAt so a merged node
    # still carries one, matching what GraphQL returns. The Wave-A fields (labels,
    # commits, linked issues, diff stats, branches) default to absent, and the parser
    # tolerates their absence, so the windowing tests are unaffected by them.
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "body": "body",
        "state": state,
        "createdAt": merged or closed or updated,
        "mergedAt": merged,
        "closedAt": closed or merged,
        "updatedAt": updated,
        "additions": additions,
        "deletions": deletions,
        "changedFiles": changed_files,
        "baseRefName": base_ref,
        "headRefName": head_ref,
        "author": {"login": author, "url": f"https://github.com/{author}"} if author else None,
        "labels": {"nodes": labels or []},
        "files": {"nodes": files or []},
        "reviews": {"nodes": reviews or []},
        "reviewRequests": {"nodes": requested or []},
        "commits": {"nodes": commits or []},
        "closingIssuesReferences": {"nodes": linked or []},
    }


class _FakeGraphQLGH:
    """Returns canned pullRequests pages keyed by the ``after`` cursor."""

    def __init__(self, pages: dict[str | None, dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any] | None] = []

    async def async_graphql(self, _query: str, variables: dict[str, Any] | None = None) -> Any:
        self.calls.append(variables)
        after = (variables or {}).get("after")
        return {"repository": {"pullRequests": self.pages[after]}}


def _page(nodes: list[dict[str, Any]], *, next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor},
        "nodes": nodes,
    }


# --- windowing ---------------------------------------------------------------


async def test_paging_stops_once_updated_falls_before_cutoff() -> None:
    gh = _FakeGraphQLGH(
        {
            None: _page(
                [
                    _node(3, updated="2025-07-01T00:00:00Z", merged="2025-07-01T00:00:00Z"),
                    _node(2, updated="2025-05-01T00:00:00Z", merged="2025-05-01T00:00:00Z"),
                ],
                next_cursor="c1",
            )
        }
    )
    prs = await _fetch_prs_graphql(cast("GitHub", gh), "o", "r", cutoff=CUTOFF)
    assert [p.number for p in prs] == [3]
    assert len(gh.calls) == 1  # stopped before following the cursor


async def test_merged_before_cutoff_is_skipped_not_a_stop() -> None:
    # PR #4 was updated in-window (recent comment) but merged long ago: skip, keep paging.
    gh = _FakeGraphQLGH(
        {
            None: _page(
                [
                    _node(5, updated="2025-07-01T00:00:00Z", merged="2025-07-01T00:00:00Z"),
                    _node(4, updated="2025-06-15T00:00:00Z", merged="2025-01-01T00:00:00Z"),
                ]
            )
        }
    )
    prs = await _fetch_prs_graphql(cast("GitHub", gh), "o", "r", cutoff=CUTOFF)
    assert [p.number for p in prs] == [5]


async def test_closed_pr_windows_on_closed_at() -> None:
    # Closed-without-merge PRs have no mergedAt, so the window falls back to closedAt:
    # #6 closed in-window is kept (labeled "closed"); #4 closed long ago but bumped into
    # the window by a recent comment is skipped, same as the merged-before-cutoff case.
    gh = _FakeGraphQLGH(
        {
            None: _page(
                [
                    _node(
                        6,
                        updated="2025-07-01T00:00:00Z",
                        closed="2025-07-01T00:00:00Z",
                        state="CLOSED",
                    ),
                    _node(
                        4,
                        updated="2025-06-15T00:00:00Z",
                        closed="2025-01-01T00:00:00Z",
                        state="CLOSED",
                    ),
                ]
            )
        }
    )
    prs = await _fetch_prs_graphql(cast("GitHub", gh), "o", "r", cutoff=CUTOFF)
    assert [(p.number, p.state) for p in prs] == [(6, "closed")]


async def test_limit_caps_pr_count() -> None:
    gh = _FakeGraphQLGH(
        {
            None: _page(
                [
                    _node(9, updated="2025-08-01T00:00:00Z", merged="2025-08-01T00:00:00Z"),
                    _node(8, updated="2025-07-01T00:00:00Z", merged="2025-07-01T00:00:00Z"),
                ],
                next_cursor="c1",
            )
        }
    )
    prs = await _fetch_prs_graphql(cast("GitHub", gh), "o", "r", cutoff=CUTOFF, limit=1)
    assert [p.number for p in prs] == [9]


async def test_pagination_follows_the_cursor() -> None:
    gh = _FakeGraphQLGH(
        {
            None: _page(
                [_node(3, updated="2025-08-01T00:00:00Z", merged="2025-08-01T00:00:00Z")],
                next_cursor="c1",
            ),
            "c1": _page([_node(2, updated="2025-07-01T00:00:00Z", merged="2025-07-01T00:00:00Z")]),
        }
    )
    prs = await _fetch_prs_graphql(cast("GitHub", gh), "o", "r", cutoff=CUTOFF)
    assert [p.number for p in prs] == [3, 2]
    assert len(gh.calls) == 2


# --- node parsing + episode parity -------------------------------------------


async def test_pr_node_parses_and_maps_to_the_same_episode() -> None:
    node = _node(
        7,
        updated="2025-08-02T00:00:00Z",
        merged="2025-08-02T00:00:00Z",
        author="paris-phan",
        files=[{"path": "infra/gcp.tf", "additions": 100, "deletions": 2}],
        reviews=[
            {
                "state": "COMMENTED",
                "submittedAt": "2025-08-01T00:00:00Z",
                "url": "https://github.com/o/r/pull/7#r1",
                "body": "please add a regression test",
                "author": {"login": "paris-phan", "url": "https://github.com/paris-phan"},
            }
        ],
        requested=[{"requestedReviewer": {"login": "abhinavp5"}}],
    )
    rec = _pr_node_to_rec(node)

    assert rec.number == 7
    assert rec.state == "merged"
    assert rec.author_login == "paris-phan"
    assert rec.author_url == "https://github.com/paris-phan"
    assert rec.files[0].path == "infra/gcp.tf"
    assert rec.reviews[0].login == "paris-phan"
    assert rec.reviews[0].state == "COMMENTED"
    assert [(rr.name, rr.kind) for rr in rec.requested_reviewers] == [("abhinavp5", "user")]
    assert rec.raw is node  # raw payload kept for the raw store

    # Parity: the GraphQL-derived rec maps to the same episode body shape.
    body = json.loads(pr_to_episode(rec, _repo()).body)
    assert body["pull_request"]["number"] == 7
    assert body["reviews"][0]["comment"] == "please add a regression test"
    assert body["requested_reviewers"][0]["name"] == "abhinavp5"
    assert body["requested_reviewers"][0]["kind"] == "user"
    # Per-file stats are now carried (recovered dead payload), feeding TOUCHES_PATH.
    assert body["files"][0]["additions"] == 100
    assert body["files"][0]["deletions"] == 2


def test_pr_node_closed_without_merge_is_labeled_closed() -> None:
    # A close without a merge: GraphQL state CLOSED, no mergedAt. The rec carries "closed"
    # and a null merged_at, so pr_to_episode later anchors it to created_at, not a merge.
    node = _node(3, updated="2025-08-02T00:00:00Z", closed="2025-08-02T00:00:00Z", state="CLOSED")
    rec = _pr_node_to_rec(node)
    assert rec.state == "closed"
    assert rec.merged_at is None
    assert json.loads(pr_to_episode(rec, _repo()).body)["pull_request"]["state"] == "closed"


def test_pr_node_handles_null_author_and_non_user_reviewer() -> None:
    node = _node(
        1,
        updated="2025-08-02T00:00:00Z",
        merged="2025-08-02T00:00:00Z",
        author=None,
        requested=[{"requestedReviewer": {}}],  # e.g. a Team request: no login
    )
    rec = _pr_node_to_rec(node)
    assert rec.author_login is None
    assert rec.author_url is None
    assert rec.requested_reviewers == []


def test_pr_node_sets_is_bot_from_typename_and_drops_it() -> None:
    # GraphQL marks bots via __typename "Bot" with a bare login (no "[bot]" suffix).
    node = _node(
        2,
        updated="2025-08-02T00:00:00Z",
        merged="2025-08-02T00:00:00Z",
        reviews=[
            {
                "state": "COMMENTED",
                "submittedAt": "2025-08-01T00:00:00Z",
                "author": {"login": "alice", "url": "u", "__typename": "User"},
            },
            {
                "state": "APPROVED",
                "submittedAt": "2025-08-01T01:00:00Z",
                "author": {"login": "dependabot", "url": "u", "__typename": "Bot"},
            },
        ],
    )
    rec = _pr_node_to_rec(node)
    assert [(r.login, r.is_bot) for r in rec.reviews] == [("alice", False), ("dependabot", True)]

    # Parity: the bot review is dropped from the episode body, the human one kept.
    # Reviews nest the reviewer under a nullable "reviewer" key (ghost reviewer -> null).
    body = json.loads(pr_to_episode(rec, _repo()).body)
    assert [r["reviewer"]["login"] for r in body["reviews"]] == ["alice"]


def test_pr_query_keeps_newest_reviews_and_files() -> None:
    # reviews/files must page from the END (last:), so the newest reviews -- the ones
    # _select_reviews wants -- survive instead of the oldest. Guards the regression
    # without a live call.
    assert "reviews(last:" in _PR_QUERY
    assert "files(last:" in _PR_QUERY
    assert "reviews(first:" not in _PR_QUERY
    # Wave A: the routing signals must be in the query, paged from the END for commits.
    assert "commits(last:" in _PR_QUERY
    assert "closingIssuesReferences(first:" in _PR_QUERY
    assert "__typename" in _PR_QUERY  # user-vs-team request discrimination


def test_pr_node_parses_team_request_commits_linked_issues_and_stats() -> None:
    # The Wave-A review-routing payload: a team review request, commit authors (the
    # active-maintainer signal), a closed-issue link, diff stats, branches, labels.
    node = _node(
        8,
        updated="2025-08-02T00:00:00Z",
        merged="2025-08-02T00:00:00Z",
        author="alice",
        labels=[{"name": "area:auth"}, {"name": "type:fix"}],
        additions=120,
        deletions=30,
        changed_files=4,
        base_ref="main",
        head_ref="alice/fix-auth",
        requested=[
            {"requestedReviewer": {"__typename": "User", "login": "bob", "url": "https://gh/bob"}},
            {"requestedReviewer": {"__typename": "Team", "name": "auth-team", "slug": "auth"}},
        ],
        commits=[
            {"commit": {"oid": "abc123", "message": "fix", "author": {"user": {"login": "alice"}}}},
            {"commit": {"oid": "def456", "message": "add test", "author": {"user": None}}},
        ],
        linked=[{"number": 100, "repository": {"nameWithOwner": "o/r"}}],
    )
    rec = _pr_node_to_rec(node)

    assert [(rr.name, rr.kind) for rr in rec.requested_reviewers] == [
        ("bob", "user"),
        ("auth-team", "team"),
    ]
    assert [(c.sha, c.author_login) for c in rec.commits] == [("abc123", "alice"), ("def456", None)]
    assert [(li.identifier, li.relation) for li in rec.linked_issues] == [("o/r#100", "closes")]
    assert rec.labels == ["area:auth", "type:fix"]
    assert (rec.additions, rec.deletions, rec.changed_files) == (120, 30, 4)
    assert (rec.base_ref, rec.head_ref) == ("main", "alice/fix-auth")

    # Parity into the episode body.
    body = json.loads(pr_to_episode(rec, _repo()).body)
    assert body["pull_request"]["labels"] == ["area:auth", "type:fix"]
    assert body["pull_request"]["diff_stats"]["changed_files"] == 4
    assert {rr["kind"] for rr in body["requested_reviewers"]} == {"user", "team"}
    assert body["linked_issues"][0] == {"identifier": "o/r#100", "relation": "closes"}
    assert body["commits"][0]["author_login"] == "alice"


# --- issues windowing (REST) -------------------------------------------------


class _FakeIssueModel:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self._data


class _FakeRest:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self._issues = issues
        self.paginate_kwargs: dict[str, Any] | None = None

        class _Issues:
            async def async_list_for_repo(self, **_k: Any) -> None: ...

        self.issues = _Issues()

    def paginate(self, _func: Any, **kwargs: Any) -> Any:
        self.paginate_kwargs = kwargs

        async def _gen():
            for data in self._issues:
                yield _FakeIssueModel(data)

        return _gen()


class _FakeRestGH:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.rest = _FakeRest(issues)


async def test_fetch_issues_passes_since_window_and_skips_prs() -> None:
    gh = _FakeRestGH(
        [
            {
                "number": 1,
                "title": "bug",
                "html_url": "https://github.com/o/r/issues/1",
                "state": "open",
                "body": "boom",
                "created_at": "2025-07-01T00:00:00Z",
            },
            {"number": 2, "pull_request": {"url": "x"}, "title": "actually a PR"},
        ]
    )
    recs = await _fetch_issues(cast("GitHub", gh), "o", "r", cutoff=CUTOFF)

    assert [r.identifier for r in recs] == ["o/r#1"]
    assert gh.rest.paginate_kwargs is not None
    assert gh.rest.paginate_kwargs["since"] == CUTOFF
    assert gh.rest.paginate_kwargs["sort"] == "updated"
    assert gh.rest.paginate_kwargs["direction"] == "desc"


async def test_fetch_issues_respects_limit() -> None:
    issues = [
        {
            "number": n,
            "title": f"i{n}",
            "html_url": f"https://github.com/o/r/issues/{n}",
            "state": "open",
            "created_at": "2025-07-01T00:00:00Z",
        }
        for n in (1, 2, 3)
    ]
    gh = _FakeRestGH(issues)
    recs = await _fetch_issues(cast("GitHub", gh), "o", "r", cutoff=CUTOFF, limit=2)
    assert [r.identifier for r in recs] == ["o/r#1", "o/r#2"]
