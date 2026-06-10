# compile: procedures to SkillIR to SKILL.md

Detects procedures in the graph, compiles them into grounded SkillIR, and renders SKILL.md documents. Today only the renderer is real; detection and compilation are Phase 4 stubs.

## Files

- `render.py`: deterministic Jinja2 renderer for SkillIR to SKILL.md. Custom delimiters `<< >>` and `<% %>` so markdown braces do not collide. Template lives at `templates/skill.md.j2`. Pure: same SkillIR in, byte-identical markdown out.
- `detect.py`: stub. Deterministic graph query plus support and confidence thresholds to find procedure candidates. No LLM.
- `compiler.py`: stub. LLM compiler filling SkillIR from grounded evidence via structured output. Every field must cite a source; ungrounded content gets dropped.

## Invoked by

`relic compile --skill <archetype>` in `cli.py` (Phase 4, not wired yet). `render.py` is already used by `serve/emit_files.py`, `serve/catalog.py`, `serve/mcp_server.py`, and `relic show`.

## Gotchas

- Keep render deterministic. Snapshot tests (`tests/test_render.py`, syrupy) will catch drift.
- The SkillIR schema in `ontology/skill_ir.py` is the contract. Do not let the template and the model diverge.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
