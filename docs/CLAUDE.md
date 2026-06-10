# docs: hand-written platform documentation

Architecture, processes, data model, and runbooks. Written by hand, no generator. `README.md` is the entry point and links everything.

## Map

- `architecture.md`: the two halves (capture/memory/recall and skills/serve), the one contract (SkillIR), the two stores.
- `processes.md`: every process the platform runs, with triggers, reads, writes, dependencies.
- `data-model.md`: the three type layers (rich ontology, flat graph nodes, SkillIR) and episode JSON.
- `ingestion.md`, `memory-and-recall.md`, `skills.md`: the pipeline halves in depth.
- `cli-reference.md`, `configuration.md`, `development.md`: commands, settings, local setup.
- `roadmap.md`: milestones M0 to M4, phases 0 to 8, built versus pending.

## Conventions

- House voice from `AGENTS.md` applies: short declarative sentences, no em dashes, specifics over adjectives, lowercase product terms.
- A behavior change in code means updating the matching doc in the same PR. `roadmap.md` is the status truth; other docs may describe phases that are still stubs.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
