# examples/skills: canonical SkillIR documents

Reference JSON showing what a complete, well-grounded skill looks like.

## Files

- `pr-review-routing.json`: routes auth PRs to active maintainers. Exercises every SkillIR field: inputs, outputs, preconditions, safety checks, citations.

## Gotchas

- Examples must validate against `src/relic/ontology/skill_ir.py`. A schema change there means updating these files in the same commit.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
