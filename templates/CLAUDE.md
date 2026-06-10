# templates: Jinja2 templates for rendered documents

## Files

- `skill.md.j2`: renders a SkillIR to SKILL.md. Title and semver, description, metadata, inputs, outputs, preconditions, safety checks, citations with links.

## Gotchas

- Delimiters are `<< >>` and `<% %>`, not `{{ }}`, so markdown braces do not collide. The matching Environment config lives in `src/relic/compile/render.py`.
- Rendering must stay deterministic. `tests/test_render.py` snapshots the output (syrupy); template changes mean regenerating snapshots on purpose.
- The template and `src/relic/ontology/skill_ir.py` are one contract. Change them together.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
