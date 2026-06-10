# eval: recall gold sets

Test cases for measuring recall quality. The scorecard (`src/relic/scorecard.py`, `relic eval`) checks that recall answers cite the expected PRs.

## Files

- `github_recall.json`: repo slug plus an array of cases, each a `question` and `expect_prs` (PR numbers that must be cited).

## Gotchas

- Cases are keyed to `buildrelic/relic-core`; PR numbers are repo-relative. A gold set for another repo needs its own file.
- Not wired into CI. Runs are manual, against a populated graph.

## Maintaining this file

Update it in the same commit as a change that invalidates it. Current state only: history lives in the git log.
