---
name: new-entity-type
description: Add or change an entity type, edge type, or ontology model in relic-core (a new node like Comment/Meeting/Deploy, a new relationship, new fields on existing types). Use whenever the task touches src/relic/ontology/ or the graph schema in engram.py, including "add X to the ontology", "track Y in the graph", or "add a relationship between A and B". The type system is mirrored in two layers that must change together.
---

# Adding an entity or edge type

Relic keeps two type layers on purpose, and they must move together:

1. **Rich ontology** (`src/relic/ontology/`): the canonical Pydantic models with nested objects, `AuditInfo`, contact blocks. This is what the rest of the codebase imports.
2. **Flat graph mirror** (`src/relic/graph/engram.py`): flattened Pydantic models Graphiti uses to type extraction. Flat is a hard requirement: no nested objects, no reserved attribute names: Graphiti turns these into LLM-facing schemas and graph properties.

Skipping the mirror means extraction never produces your type; skipping the ontology means the codebase has no canonical model. Do both.

## Procedure

1. **Rich model** in `src/relic/ontology/entities.py` (engineering) or `org.py` (company-wide). House conventions:
   - Enums are lowercase `Literal` strings (`state: Literal["open", "closed", "merged"]`), never Enum classes.
   - Relationships are `Id` reference fields (`reviewer_id: Id`), never nested model references. Edges live in the graph; models are nodes with foreign keys.
   - Reuse `primitives.py` (`Id`, `TimePoint`, `AuditInfo`, `Taggable`) instead of redeclaring.
2. Export it from `ontology/__init__.py`.
3. **Flat mirror** in `engram.py`: `<Name>Node(BaseModel)` with scalar fields only, plus a docstring (Graphiti feeds field descriptions to the extraction LLM, so write them as instructions about what to extract). Register it in `ENTITY_TYPES`.
4. **Edges**: a flat edge model (see `Reviewed`, `TouchesPath`), registered in `EDGE_TYPES` and in `EDGE_TYPE_MAP` for each (source, target) label pair it can connect. An edge type missing from `EDGE_TYPE_MAP` is silently never extracted: this is the classic miss.
5. **Tests**: round-trip/validation cases in `tests/test_ontology.py`; if the type affects episode mapping, extend `tests/test_mappers.py`.
6. **Docs**: `docs/data-model.md` describes all three type layers; update it and `src/relic/ontology/CLAUDE.md` (or `graph/CLAUDE.md` for edge details) in the same commit.
7. `just check` to run lint, types, and tests.

## SkillIR is different

`ontology/skill_ir.py` is not a graph type: it is the MCP tool contract. Do not redesign it as part of ontology work. A SkillIR field change ripples to `compile/render.py`, `templates/skill.md.j2` (snapshot tests in `test_render.py` will catch drift; regenerate with `--snapshot-update` only for deliberate changes), `registry/store.py` columns, and `serve/`.

## Existing types take effect lazily

New entity/edge types only apply to episodes ingested after the change. The existing graph is not migrated; if the type matters for already-ingested history, the repo needs a fresh backfill (`--fresh`) and that is an expensive, user-confirmed call.
