# Issue tracker: Linear

Issues and PRDs for this repo live in Linear, team Relic (issue keys `REL-NNN`).
Drive them through the Linear MCP server. Code and review happen as GitHub PRs in
`buildrelic/relic-core`: a PR references its Linear key, and branches follow
`<author>/rel-<n>-<slug>` (e.g. `zidan/rel-13-expand-scorecard`).

## Conventions (Linear MCP)

- **Create an issue**: `save_issue` with `team: "Relic"`, a title, and a markdown
  body. New issues default to the Backlog state.
- **Read an issue**: `get_issue` by key (e.g. `REL-13`); `list_comments` for its
  thread.
- **List issues**: `list_issues` filtered by `team`, `state`, `label`, or
  `assignee`.
- **Comment**: `save_comment` on the issue.
- **Apply / remove labels and change state**: `save_issue` (it sets labels and the
  workflow state on an existing issue). Valid states: Backlog, Todo, In Progress,
  In Review, Done, Canceled, Duplicate.

## When a skill says "publish to the issue tracker"

Create a Linear issue in team Relic with `save_issue`.

## When a skill says "fetch the relevant ticket"

`get_issue` by its `REL-` key, plus `list_comments`.

## GitHub side

PRs, reviews, and CI live on GitHub (`gh` CLI). The link between the two is the
`REL-` key in the PR title or body. The triage and issue skills act on Linear; the
PR-review and code skills act on GitHub.
