# CLI reference

Every command is a subcommand of `relic` ([`cli.py`](../src/relic/cli.py)). Run
`uv run relic --help` for the live list, or `uv run relic <command> --help` for one
command. The CLI prints `--help` when given no arguments.

Commands group into the capture and memory side, the skills side, diagnostics, and
two stubs that land in later phases.

## Capture and memory

### relic ingest

Pull merged PRs, reviews, and issues into the graph.

```
relic ingest --repo OWNER/NAME [--limit N]
```

- `--repo` (required): the `owner/name` to ingest. Anything without a `/` exits 2.
- `--limit` (optional): cap merged PRs and issues pulled, most recent first.

Needs a GitHub token, `OPENAI_API_KEY`, and a running FalkorDB. Pulls Linear too
when `LINEAR_API_KEY` is set. See [ingestion.md](ingestion.md).

```bash
uv run relic ingest --repo astral-sh/uv --limit 20
```

### relic recall

Recall facts from the memory graph, with their sources.

```
relic recall "QUERY" [--repo OWNER/NAME] [--num-results N]
```

- `query` (argument, required): what to recall.
- `--repo` (optional): scope to a repo's graph. Defaults to `TARGET_REPO`.
- `--num-results` (optional, default 10): max facts to return.

```bash
uv run relic recall "who usually reviews auth changes?"
```

### relic query

Find people connected to work matching the text (search first, Cypher fallback).

```
relic query "TEXT" [--repo OWNER/NAME]
```

- `text` (argument, required): the query string.
- `--repo` (optional): scope to a repo's graph. Defaults to `TARGET_REPO`.

Prints each person, relation, fact, profile URL, and source episodes,
de-duplicated by name.

### relic eval

Score recall against a gold set: does it cite the PR or issue that holds each
answer?

```
relic eval [--path PATH] [--num-results N]
```

- `--path` (optional, default `eval/github_recall.json`): the gold set JSON.
- `--num-results` (optional, default 10): facts per question.

```bash
uv run relic eval
uv run relic eval --path eval/my-set.json
```

## Skills

### relic register

Load a `SkillIR` JSON file into the registry as a skill (the manual authoring
path). Invalid JSON exits 1.

```
relic register PATH
```

```bash
uv run relic register examples/skills/pr-review-routing.json
```

### relic list

List registered skills, optionally filtered by status.

```
relic list [--status draft|verified|deprecated]
```

### relic show

Show a skill's rendered `SKILL.md`, or its raw `SkillIR` JSON. Unknown id exits 1.

```
relic show SKILL_ID [--json]
```

### relic verify

Promote a draft skill to verified and stamp `last_verified_at`. Unknown id or a
deprecated skill exits 1.

```
relic verify SKILL_ID
```

### relic deprecate

Mark a skill deprecated so it stops being emitted or served. Unknown id exits 1.

```
relic deprecate SKILL_ID
```

### relic emit

Write verified skills and a catalog index to a target repo's `.claude/skills/`,
and remove the files of any skill since deprecated.

```
relic emit --repo /path/to/target-repo
```

### relic catalog

Print a browsable markdown index of verified skills to stdout.

```
relic catalog
```

### relic serve

Serve verified skills and memory recall as MCP tools over stdio. The one
long-running command. See [skills.md](skills.md).

```
relic serve
```

## Diagnostics

### relic doctor

Report registry, graph, and key status for this setup. Reads only, changes
nothing, never raises. See [configuration.md](configuration.md).

```
relic doctor
```

## Stubs (later phases)

### relic resolve

Unify people across GitHub and Linear into single `Person` nodes. Phase 3. Prints
"not implemented yet" and exits 1.

### relic compile

Detect a procedure and compile it into a grounded `SkillIR`. Phase 4. Prints "not
implemented yet" and exits 1.

```
relic compile --skill ARCHETYPE
```

## Quick reference

| Command | Required args | Key flags | Needs graph |
|---|---|---|---|
| `ingest` | `--repo` | `--limit` | yes |
| `recall` | `query` | `--repo`, `--num-results` | yes |
| `query` | `text` | `--repo` | yes |
| `eval` | none | `--path`, `--num-results` | yes |
| `register` | `path` | none | no |
| `list` | none | `--status` | no |
| `show` | `skill_id` | `--json` | no |
| `verify` | `skill_id` | none | no |
| `deprecate` | `skill_id` | none | no |
| `emit` | `--repo` | none | no |
| `catalog` | none | none | no |
| `serve` | none | none | optional |
| `doctor` | none | none | no |
| `resolve` | none | none | stub |
| `compile` | `--skill` | none | stub |
