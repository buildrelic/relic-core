# Commit message instructions

Generate commit messages that follow this project's conventions. A message has a
**title** (subject line) and an optional **description** (body).

## Title

- Short and skimmable. Aim for 50 characters or fewer; hard cap at 72.
- Imperative mood, lowercase first word, no trailing period.
  Example: `tighten retry backoff`, not `Tightened the retry backoff.`
- Read like a person dashed it off, not like an AI wrote it. Plain and specific.
- No Conventional Commit prefixes (`feat:`, `fix:`, `chore:`, etc.). No emoji.
- No em dashes.
- State *what* changed at a glance. The body explains *why* and *how*.
- When the change relates to a Linear issue, append `[part of <ISSUE-ID>]` to the
  title. The issue ID is the Linear team prefix plus a number, for example
  `[part of REL-42]`. Infer the ID from the branch name when present: Linear
  branches look like `<username>/<prefix>-<number>-<slug>`, e.g.
  `paris/rel-10-ingest-run-observability` -> `[part of REL-10]` (uppercase the
  prefix). If no issue ID is derivable, omit the suffix entirely. Do not invent one.

Title examples:
- `tighten ingest retry backoff [part of REL-10]`
- `narrow payload type before indexing in test [part of REL-13]`
- `drop unused scorecard fixture`

## Description

- Optional. Include one when the change is non-trivial or the reasoning is not
  obvious from the diff.
- Separate it from the title with one blank line.
- Wrap body lines at about 72 characters.
- Cover what changed and why, context, and tradeoffs. Paragraphs and bullet lists
  are both fine.
- Write the reasoning the diff cannot show. Do not just restate the title.
- Do not add `Co-Authored-By` or tool-attribution footers unless asked.

## What not to do

- No `feat:`/`fix:`/`chore:` prefixes, no emoji, no em dashes.
- Do not fabricate a Linear issue ID. Only add `[part of <ID>]` when the branch or
  context makes the ID clear.
- Do not pad with filler ("This commit ...", "In this change ...").
