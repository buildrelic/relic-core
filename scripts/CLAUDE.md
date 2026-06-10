# scripts: repo automation

Small stdlib-only scripts run by hooks and pre-commit, not part of the `relic` package.

## Files

- `ensure_claude_md.py`: creates a stub CLAUDE.md in any code directory missing one. A code directory is one that directly holds a tracked or untracked-but-not-ignored file; hidden dirs and the repo root are exempt. Runs from pre-commit (exit 1 means stage the new stubs) and from the Claude Code PostToolUse hook in `.claude/settings.json` (`--hook`, exit 2 feeds the stub list back to Claude to fill in).

## Gotchas

- Keep these scripts stdlib-only so they run before any environment exists.
- They run from pre-commit and hooks, so they must stay fast and silent on the happy path.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
