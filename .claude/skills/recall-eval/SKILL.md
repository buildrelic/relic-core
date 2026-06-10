---
name: recall-eval
description: Measure or improve recall quality in relic using the gold-set eval harness. Use whenever the task involves eval/github_recall.json, adding gold cases, running "relic eval" or the scorecard, asking "how good is recall", or judging whether a retrieval/extraction change helped or hurt. Run this before and after any change to search, extraction, or episode mapping.
---

# Evaluating recall

The harness is simple on purpose: a gold set of questions, each listing the PRs a correct answer must cite. `relic eval` runs each question through `recall()` and checks whether the returned facts cite the expected PR URLs. Provenance is the product promise, so the metric is citation hits, not answer prose.

## The pieces

- Gold set: `eval/github_recall.json`. A repo slug plus cases: `{"question": ..., "expect_prs": [<PR numbers>]}`. Numbers are relative to that repo.
- Scorer: `src/relic/scorecard.py`. Loads the gold set, calls recall per question, reports hits and misses.
- Run: `uv run relic eval` (needs FalkorDB up with that repo ingested, plus `OPENAI_API_KEY` for query-time embeddings). `TARGET_REPO` defaults the repo when no flag is passed.

## Adding gold cases

Good cases come from real history you can verify:

1. Find ground truth first: `git log --oneline --merges` or `gh pr list --state merged`, and confirm the PR actually contains what the question asks about.
2. Write the question the way a teammate would ask it ("what was added so recall answers cite their sources?"), not as a keyword query. The harness exists to catch the gap between natural questions and what search finds.
3. List every PR that legitimately answers it in `expect_prs`, not just one.
4. Keep cases diverse: features, fixes, decisions, "who did X". Single-topic gold sets overfit retrieval tuning to one query shape.

A new gold set for another repo is a new JSON file with the same shape; point the eval at it rather than mixing repos in one file.

Gold cases are append-as-you-learn: when a real recall miss, a teammate's unanswered question, or any task exposes a gap worth keeping, add it as a case in the same PR as the work that surfaced it. The gold set should grow with use, not in batches.

## Interpreting a miss

Work the same split as graph-inspect: is the fact missing, or findable-but-not-found?

1. Check the graph has the fact at all (`RELATES_TO` facts for that group_id mentioning the topic). Missing → extraction/mapping problem: the episode body may have been clipped (`_MAX_DESC_CHARS`), bot-filtered, or never ingested (check checkpoints and `LoadStats`).
2. Fact exists but not returned → search problem: try rephrasing toward the fact's wording to confirm, then look at hybrid search behavior in `src/relic/graph/recall.py`.
3. Cited URL differs from expected → check `_extract_url()` in `recall.py` and the episode JSON; the PR number mapping should be exact.

## Discipline

- Run the eval before and after any change to `graph/`, `ingest/mappers.py`, or extraction config, and report both numbers. A retrieval "improvement" that drops citation hits is a regression.
- The eval is not in CI (it needs a live graph and costs API calls), so the numbers only exist if you run them. Paste the before/after into the PR description.
