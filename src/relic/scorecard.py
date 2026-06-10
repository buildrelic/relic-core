"""Recall scorecard: measure whether recall surfaces the PR that holds the answer.

A deterministic eval, no LLM judge: each case names a question and the PR
number(s) whose content actually answers it. We run recall, then inspect the
recalled facts' sources for the expected urls. The scorecard reports three
numbers, all read off the same run:

- **hit rate** -- share of cases where at least one expected source was cited.
  The original, coarsest signal: did the right source show up at all?
- **MRR** -- mean reciprocal rank of the first fact citing an expected source.
  Rank-sensitive, so it moves when reranking pushes the right fact up or down;
  this is the number recall reranking is tuned against.
- **coverage** -- for cases that expect several sources, the mean share found.
  A hit on one of two expected PRs is half coverage, not a clean pass.

This turns "is recall any good?" into numbers you can watch as ingestion and
retrieval change, and the ruler costs nothing per run beyond the recall calls it
measures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relic.graph.recall import RecallAnswer


@dataclass(slots=True)
class EvalCase:
    """One question and the PR or issue number(s) whose content answers it."""

    question: str
    expect_prs: list[int] = field(default_factory=list)
    expect_issues: list[int] = field(default_factory=list)


@dataclass(slots=True)
class GoldSet:
    """A scorecard: the repo under test and its cases."""

    repo: str
    cases: list[EvalCase]


@dataclass(slots=True)
class CaseResult:
    """The outcome of scoring one case against a recall answer.

    ``matched_urls`` is the expected urls actually cited, in the order first seen.
    ``rank`` is the 1-indexed position of the first *fact* that cites an expected
    source (facts come back in recall's ranked order), or ``None`` on a miss. The
    three metrics are derived from these: ``hit`` (any match), ``reciprocal_rank``
    (how highly the first match ranked), and ``coverage`` (share of expected found).
    """

    question: str
    expected_urls: list[str]
    found_urls: list[str]
    matched_urls: list[str]
    rank: int | None

    @property
    def hit(self) -> bool:
        """Whether any expected source was cited at all -- the coarsest signal."""
        return bool(self.matched_urls)

    @property
    def reciprocal_rank(self) -> float:
        """1/rank of the first cited expected source, or 0.0 if none was cited."""
        return 1.0 / self.rank if self.rank else 0.0

    @property
    def coverage(self) -> float:
        """Share of this case's expected sources that were cited, in [0.0, 1.0]."""
        return len(self.matched_urls) / len(self.expected_urls) if self.expected_urls else 0.0


def pr_url(repo: str, number: int) -> str:
    """Canonical GitHub PR url: the unambiguous source identifier to match on."""
    return f"https://github.com/{repo}/pull/{number}"


def issue_url(repo: str, number: int) -> str:
    """Canonical GitHub issue url, the source identifier for issue-backed cases."""
    return f"https://github.com/{repo}/issues/{number}"


def load_gold(path: str | Path) -> GoldSet:
    """Load a scorecard gold set from JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        EvalCase(
            question=case["question"],
            expect_prs=list(case.get("expect_prs", [])),
            expect_issues=list(case.get("expect_issues", [])),
        )
        for case in data["cases"]
    ]
    return GoldSet(repo=data["repo"], cases=cases)


def score_case(case: EvalCase, repo: str, answer: RecallAnswer) -> CaseResult:
    """Score one case: which expected sources were cited, and how highly ranked.

    Walks the answer's facts in ranked order. ``rank`` is the position of the first
    fact citing any expected source; ``matched_urls`` collects every expected source
    found across all facts. Rank is counted in facts, not flattened sources, because
    the fact is the unit recall ranks and reranking (REL-11) reorders.
    """
    expected = [pr_url(repo, number) for number in case.expect_prs]
    expected += [issue_url(repo, number) for number in case.expect_issues]
    expected_set = set(expected)

    found_urls: list[str] = []
    matched: list[str] = []
    rank: int | None = None
    for position, fact in enumerate(answer.facts, start=1):
        for source in fact.sources:
            if not source.url:
                continue
            found_urls.append(source.url)
            if source.url in expected_set and source.url not in matched:
                matched.append(source.url)
                if rank is None:
                    rank = position
    return CaseResult(
        question=case.question,
        expected_urls=expected,
        found_urls=found_urls,
        matched_urls=matched,
        rank=rank,
    )


def summarize(results: list[CaseResult]) -> str:
    """Render the scorecard: the three aggregate metrics, then a per-case line."""
    total = len(results)
    hits = sum(1 for result in results if result.hit)
    pct = round(100 * hits / total) if total else 0
    mrr = sum(result.reciprocal_rank for result in results) / total if total else 0.0
    coverage = sum(result.coverage for result in results) / total if total else 0.0
    lines = [
        f"recall scorecard: {hits}/{total} cited the right source ({pct}%)",
        "",
        f"  hit rate   {hits}/{total} ({pct}%)".ljust(26) + "at least one expected source cited",
        f"  MRR        {mrr:.2f}".ljust(26) + "rank of the first correct source, averaged",
        f"  coverage   {coverage:.2f}".ljust(26) + "expected sources found per case, averaged",
        "",
    ]
    for result in results:
        mark = "PASS" if result.hit else "MISS"
        detail = ""
        if result.hit:
            bits = [f"rank {result.rank}"]
            if len(result.expected_urls) > 1:
                bits.append(f"{len(result.matched_urls)}/{len(result.expected_urls)} sources")
            detail = f"  ({', '.join(bits)})"
        lines.append(f"[{mark}] {result.question}{detail}")
        if not result.hit:
            lines.append(f"       wanted: {', '.join(result.expected_urls) or '(none)'}")
    return "\n".join(lines)
