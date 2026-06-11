# Domain docs

How the engineering skills consume this repo's domain documentation. This repo is
single-context.

## Before exploring, read these

- `CONTEXT.md` at the repo root (the domain glossary).
- `docs/adr/` for architectural decisions that touch the area you are about to
  work in.

If either is absent, proceed silently. Do not flag the absence or suggest creating
them upfront. `/grill-with-docs` creates them lazily when terms or decisions get
resolved.

## Layout (single-context)

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-graph-port-seam.md
│   └── ...
└── src/relic/
```

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a refactor proposal, a
hypothesis, a test name), use the term as defined in `CONTEXT.md`. Do not drift to
synonyms the glossary avoids. If a concept you need is not in the glossary, that is
a signal: either you are inventing language the project does not use (reconsider),
or there is a real gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it rather than silently
overriding it. Example: "Contradicts ADR-0001 (graph-port seam), but worth
reopening because ..."

## When to revisit

This repo is single-context while one domain language covers it. If you add an area
with its own glossary (a marketing site: leads, waitlist, pricing; or a desktop
shell: workspaces, panes, sync), add a `CONTEXT-MAP.md` at the root, scope a
`CONTEXT.md` per area, and switch this doc to multi-context. A new app or a new
language (TypeScript, Electron) alone is not the trigger; a second glossary is.
