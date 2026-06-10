# registry: SQLite store for compiled skills

Durable home for SkillIR documents. One table, `skills`, with the full document as JSON plus denormalized scalar columns for cheap filtering.

## Files

- `store.py`: the whole store. `connect()` (auto-creates schema and parent dir, supports `:memory:`), `upsert_skill()`, `get_skill()`, `list_skills()`, `set_status()`, `mark_verified()`, `count_by_status()`. Schema: `skills(skill_id PK, semver, title, scope, status, owner, last_verified_at, document JSON, created_at, updated_at)`.

## Invoked by

Every skill-lifecycle CLI command (register, list, show, verify, deprecate, emit, catalog, serve) plus `doctor.py`. Registry path comes from settings, default `./data/registry.db`.

## Gotchas

- The JSON `document` is the source of truth on read; scalar columns exist only for filtering.
- `upsert_skill()` preserves `created_at` and always rewrites `updated_at`.
- `mark_verified()` stamps `last_verified_at` and refuses to promote a deprecated skill.
- Status is a Literal: `draft`, `verified`, `deprecated`.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
