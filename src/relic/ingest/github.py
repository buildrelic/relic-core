"""GitHub connector: windowed pulls of merged + closed PRs (with files + reviews) and issues.

PRs are fetched with a single batched GraphQL query per page (PR plus its files,
reviews, and requested reviewers in one round trip) instead of a REST fan-out of
several calls per PR -- the difference is large on a 12-month backfill of a busy
repo. Both PRs and issues are windowed to the last ``months`` so the first run
does not page the repo's entire history. Issues stay on the REST list endpoint
(already one call per page) using its native ``since`` filter.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from githubkit import GitHub

from relic.ingest.mappers import FileChange, IssueRec, PullRequestRec, RepoBundle, ReviewRec

if TYPE_CHECKING:
    from relic.config import Settings

_NO_TOKEN = "No GitHub token. Set GITHUB_TOKEN in .env or run `gh auth login`."

# Default backfill window. ~12 months; kept as days to avoid a relativedelta dependency.
_DEFAULT_MONTHS = 12
_DAYS_PER_MONTH = 30.44

# Conservative GraphQL page sizes. Smaller pages keep us under GitHub's per-query node
# limit (a query that asks for too many total nodes fails outright rather than truncating).
_PR_PAGE_SIZE = 25
_FILES_PER_PR = 100
_REVIEWS_PER_PR = 50

_PR_QUERY = """
query PRs($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: [MERGED, CLOSED]
      orderBy: {field: UPDATED_AT, direction: DESC}
      first: $first
      after: $after
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        url
        body
        state
        createdAt
        mergedAt
        closedAt
        updatedAt
        author { login url }
        files(last: __FILES__) { nodes { path additions deletions } }
        reviews(last: __REVIEWS__) {
          nodes { state submittedAt url body author { login url __typename } }
        }
        reviewRequests(first: __REVIEWS__) {
          nodes { requestedReviewer { ... on User { login } } }
        }
      }
    }
  }
}
""".replace("__FILES__", str(_FILES_PER_PR)).replace("__REVIEWS__", str(_REVIEWS_PER_PR))


def resolve_github_token(settings: Settings) -> str:
    """Return a GitHub token from settings, else from `gh auth token`."""
    if settings.github_token:
        return settings.github_token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(_NO_TOKEN) from exc
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(_NO_TOKEN)
    return token


def make_github(token: str) -> GitHub:
    """Build an authenticated githubkit client."""
    return GitHub(token)


def _dump(model: Any) -> dict[str, Any]:
    """Normalize a githubkit model to a JSON-ish dict (Missing fields omitted)."""
    return model.model_dump(mode="json", exclude_unset=True)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp to a tz-aware datetime (None passes through)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Pull the ``nodes`` list out of a GraphQL connection, tolerating nulls."""
    if not connection:
        return []
    return connection.get("nodes") or []


async def fetch_repo(
    gh: GitHub,
    owner: str,
    name: str,
    *,
    concurrency: int = 10,
    limit: int | None = None,
    months: int = _DEFAULT_MONTHS,
) -> RepoBundle:
    """Fetch terminal-state PRs (with files + reviews) and non-PR issues for `owner/name`.

    Both merged and closed-without-merge PRs are pulled -- a close without a merge still
    carries signal ("we tried this and dropped it"). Open and draft PRs are skipped on
    purpose: they are mutable, and the name-keyed checkpoint can't update an episode once
    it lands, so an ingested draft would freeze at its in-progress state and never reflect
    the eventual merge. ``_pr_state`` labels each as merged vs closed.

    Only items from the last ``months`` are pulled: PRs whose terminal event (merge or
    close) falls in the window, and issues updated within it. ``limit`` further caps how
    many PRs and issues are kept, to bound the first run on a large repo. Both queries page
    by *update* time (descending), so ``limit`` keeps the most-recently-**updated** items,
    not strictly the most-recently-resolved (an old PR with a fresh comment sorts ahead).

    ``concurrency`` is retained for back-compat (and a possible REST fallback); the
    GraphQL PR path batches many PRs per request, so it does not fan out per PR.
    """
    full_name = f"{owner}/{name}"
    cutoff = datetime.now(UTC) - timedelta(days=round(months * _DAYS_PER_MONTH))
    bundle = RepoBundle(
        full_name=full_name, url=f"https://github.com/{full_name}", default_branch="main"
    )
    bundle.pull_requests = await _fetch_prs_graphql(gh, owner, name, cutoff=cutoff, limit=limit)
    bundle.issues = await _fetch_issues(gh, owner, name, cutoff=cutoff, limit=limit)
    return bundle


def _pr_state(node: dict[str, Any]) -> str:
    """Collapse a GraphQL PR node's status into one label (the lowercased ``state`` enum).

    GraphQL's ``state`` already folds a merge in -- a merged PR is ``MERGED``, never
    ``CLOSED`` -- so the enum alone separates merged from closed-without-merge, with no
    cross-check of ``mergedAt`` the way the REST shape needed. The query fetches only
    MERGED and CLOSED, so in practice this returns "merged" or "closed".
    """
    return (node.get("state") or "").lower()


def _pr_node_to_rec(node: dict[str, Any]) -> PullRequestRec:
    """Map a GraphQL pullRequest node to the same PullRequestRec the REST path built."""
    author = node.get("author") or {}

    files = [
        FileChange(
            path=f.get("path", ""),
            additions=int(f.get("additions", 0) or 0),
            deletions=int(f.get("deletions", 0) or 0),
            status="",  # GraphQL changed-file nodes carry no status; mappers read only path
        )
        for f in _nodes(node.get("files"))
    ]

    reviews: list[ReviewRec] = []
    for r in _nodes(node.get("reviews")):
        reviewer = r.get("author") or {}
        reviews.append(
            ReviewRec(
                login=reviewer.get("login"),
                profile_url=reviewer.get("url"),
                state=r.get("state", ""),
                submitted_at=r.get("submittedAt"),
                url=r.get("url"),
                body=r.get("body"),
                # GraphQL marks app/bot actors with __typename "Bot" and returns a bare
                # login (no REST-style "[bot]" suffix), so the typename is the reliable
                # signal for dropping CI/dependabot reviews in _select_reviews.
                is_bot=reviewer.get("__typename") == "Bot",
            )
        )

    requested_reviewers = [
        login
        for rr in _nodes(node.get("reviewRequests"))
        if (login := (rr.get("requestedReviewer") or {}).get("login"))
    ]

    return PullRequestRec(
        number=int(node["number"]),
        title=node.get("title", ""),
        url=node.get("url", ""),
        state=_pr_state(node),
        author_login=author.get("login"),
        author_url=author.get("url"),
        created_at=node.get("createdAt", ""),
        merged_at=node.get("mergedAt"),
        body=node.get("body"),
        reviews=reviews,
        requested_reviewers=requested_reviewers,
        files=files,
        raw=node,
    )


async def _fetch_prs_graphql(
    gh: GitHub, owner: str, name: str, *, cutoff: datetime, limit: int | None = None
) -> list[PullRequestRec]:
    """Page terminal-state PRs (merged + closed) via GraphQL, stopping once past the window.

    Because nodes come back ordered by ``updatedAt`` descending and a PR's terminal event
    (merge or close) is never after its last update, we can stop paging the moment a node's
    ``updatedAt`` falls before the cutoff. A node updated in-window but resolved before it
    (e.g. an old PR that got a recent comment) is skipped, not a stop signal.

    ``limit`` cuts off the walk early, so it keeps the most-recently-updated terminal PRs in
    the window -- not strictly the most-recently-resolved, since the ordering is by update.
    """
    prs: list[PullRequestRec] = []
    after: str | None = None
    while True:
        data = await gh.async_graphql(
            _PR_QUERY,
            variables={"owner": owner, "name": name, "first": _PR_PAGE_SIZE, "after": after},
        )
        connection = ((data or {}).get("repository") or {}).get("pullRequests") or {}

        stop = False
        for node in _nodes(connection):
            updated = _parse_dt(node.get("updatedAt"))
            if updated is not None and updated < cutoff:
                stop = True
                break
            # Terminal time = when the PR left the open set: its merge, else its close.
            # Skip a PR whose terminal event predates the window even though a later
            # comment bumped updatedAt into it -- mirrors the rule for merged PRs.
            terminal = _parse_dt(node.get("mergedAt")) or _parse_dt(node.get("closedAt"))
            if terminal is not None and terminal < cutoff:
                continue
            prs.append(_pr_node_to_rec(node))
            if limit is not None and len(prs) >= limit:
                return prs

        page_info = connection.get("pageInfo") or {}
        if stop or not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return prs


_PARENT_URL_RE = re.compile(r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")


def _parent_identifier(parent_issue_url: str | None) -> str | None:
    """Turn a GitHub ``parent_issue_url`` into our ``owner/repo#number`` identifier.

    Sub-issues carry their parent's REST URL inline in the issues list response, so
    no extra call is needed. Parsing it into the same identifier shape
    ``_fetch_issues`` builds lets the child and parent line up in the graph. A
    cross-repo parent keeps its own owner/repo.
    """
    if not parent_issue_url:
        return None
    match = _PARENT_URL_RE.search(parent_issue_url)
    if not match:
        return None
    return f"{match['owner']}/{match['repo']}#{match['number']}"


async def _fetch_issues(
    gh: GitHub, owner: str, name: str, *, cutoff: datetime, limit: int | None = None
) -> list[IssueRec]:
    full_name = f"{owner}/{name}"
    issues: list[IssueRec] = []
    async for raw_issue in gh.rest.paginate(
        gh.rest.issues.async_list_for_repo,
        owner=owner,
        repo=name,
        state="all",
        # Native server-side filter: issues updated at/after the cutoff. githubkit types
        # ``since`` as a datetime and serializes it to ISO-8601 itself, so pass the aware
        # datetime directly (not .isoformat(), which would be a type error).
        since=cutoff,
        sort="updated",
        direction="desc",
        per_page=100,
    ):
        data = _dump(raw_issue)
        if data.get("pull_request"):
            continue  # the issues endpoint also returns PRs
        assignees = [a.get("login") for a in (data.get("assignees") or []) if a.get("login")]
        label_names: list[str] = []
        for label in data.get("labels") or []:
            value = label.get("name") if isinstance(label, dict) else label
            if isinstance(value, str) and value:
                label_names.append(value)
        issues.append(
            IssueRec(
                source="github",
                identifier=f"{full_name}#{data.get('number')}",
                title=data.get("title", ""),
                url=data.get("html_url", ""),
                state=data.get("state", ""),
                body=data.get("body"),
                assignees=[a for a in assignees if a],
                labels=label_names,
                created_at=data.get("created_at"),
                closed_at=data.get("closed_at"),
                parent=_parent_identifier(data.get("parent_issue_url")),
                raw=data,
            )
        )
        if limit is not None and len(issues) >= limit:
            break
    return issues
