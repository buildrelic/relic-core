"""Linear connector: GraphQL pulls of issues (guarded behind LINEAR_API_KEY)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from relic.ingest.mappers import IssueRec

if TYPE_CHECKING:
    from relic.config import Settings

_ISSUES_QUERY = """
query Issues($after: String) {
  issues(first: 50, after: $after) {
    nodes {
      identifier
      title
      url
      state { name }
      assignee { name }
      labels { nodes { name } }
      createdAt
      completedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def linear_enabled(settings: Settings) -> bool:
    """True when a Linear API key is configured."""
    return bool(settings.linear_api_key)


async def fetch_issues(api_key: str) -> list[IssueRec]:
    """Fetch all Linear issues, paginating, mapped into IssueRec."""
    from gql import Client, gql
    from gql.transport.httpx import HTTPXAsyncTransport

    transport = HTTPXAsyncTransport(
        url="https://api.linear.app/graphql", headers={"Authorization": api_key}
    )
    query = gql(_ISSUES_QUERY)
    out: list[IssueRec] = []
    async with Client(transport=transport, fetch_schema_from_transport=False) as session:
        cursor: str | None = None
        while True:
            result = await session.execute(query, variable_values={"after": cursor})
            block = result["issues"]
            for node in block["nodes"]:
                out.append(_node_to_issue(node))
            page = block["pageInfo"]
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
    return out


def _node_to_issue(node: dict[str, Any]) -> IssueRec:
    state = (node.get("state") or {}).get("name", "")
    assignee = node.get("assignee") or {}
    assignees = [assignee["name"]] if assignee.get("name") else []
    labels = [lab["name"] for lab in (node.get("labels") or {}).get("nodes", []) if lab.get("name")]
    return IssueRec(
        source="linear",
        identifier=node.get("identifier", ""),
        title=node.get("title", ""),
        url=node.get("url", ""),
        state=state,
        assignees=assignees,
        labels=labels,
        created_at=node.get("createdAt"),
        closed_at=node.get("completedAt"),
        raw=node,
    )
