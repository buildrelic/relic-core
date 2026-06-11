# Triage labels

The skills speak in five canonical triage roles. Linear is not GitHub: it has a
single-axis workflow (states) plus orthogonal labels. So the roles map onto a mix
of Linear states and labels, not five flat labels.

| Canonical role    | Linear mechanism | Value                          |
| ----------------- | ---------------- | ------------------------------ |
| `needs-triage`    | state            | Backlog                        |
| `needs-info`      | label            | `needs-info`                   |
| `ready-for-agent` | state + label    | Todo + `agent-ready`           |
| `ready-for-human` | state            | Todo (no `agent-ready` label)  |
| `wontfix`         | state            | Canceled                       |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), set the
corresponding state and/or label from this table via `save_issue`.

Notes:
- `needs-info` is a flag, not a position: an issue in Backlog or Todo can still be
  waiting on the reporter. It is a label so it composes with any state.
- `agent-ready` distinguishes the two Todo roles. Todo + `agent-ready` is AFK-ready
  for an agent; Todo alone needs a human.
- Both labels were created on team Relic by the setup skill.
