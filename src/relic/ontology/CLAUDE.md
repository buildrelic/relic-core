# ontology: the typed models and the SkillIR contract

Source-of-truth Pydantic models for everything Relic talks about. Rich types live here; the flat graph-facing mirrors live in `graph/engram.py`.

## Files

- `primitives.py`: shared building blocks. `Id`, `TimePoint`, `TimeRange`, `Money`, `Link`, `ContactInfo`, `AuditInfo`, `TextBlob`, `Taggable`.
- `entities.py`: engineering entities. `Person`, `Repo`, `PullRequest`, `Review`, `Issue`, `Procedure`.
- `org.py`: company-wide entities. `Employee`, `Team`, `Customer`, `Product`, `Project`, `Document`, `Meeting`, `Decision`, `Policy`, `OKRGoal`.
- `skill_ir.py`: the SkillIR contract (`SkillIR`, `FieldSpec`, `Citation`). Ported from the system design doc. Do not redesign it: it is the MCP tool contract, and `templates/skill.md.j2` renders it byte-for-byte.
- `__init__.py`: re-exports everything for `from relic.ontology import ...`.

## Conventions

- Enums are lowercase `Literal` strings, not Enum classes.
- No relationship objects in models. Cross-references are `Id` fields (`reviewer_id`, `pull_request_id`); edges live in the graph.
- Changes to `skill_ir.py` ripple to `compile/render.py`, `registry/store.py`, `serve/`, and `templates/skill.md.j2`. Touch all of them together.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
