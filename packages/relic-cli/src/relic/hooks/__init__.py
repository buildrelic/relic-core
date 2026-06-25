"""Claude Code hook shims for the Relic daemon.

These run as standalone scripts wired into Claude Code by `relic install-hooks`, not
as imported library code. They stay stdlib-only so they start fast and never drag the
relic package onto a per-keystroke hot path.
"""
