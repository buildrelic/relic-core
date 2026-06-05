"""Recall scorecard: measure whether recall cites the PR that holds the answer.

A deterministic eval, no LLM judge: each case names a question and the PR
number(s) whose content actually answers it. We run recall, then check the
recalled facts' sources for an expected PR url. The score is the hit rate. This
turns "is recall any good?" into a number you can watch as ingestion changes,
and the ruler itself costs nothing per run beyond the recall calls it measures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from relic.graph.recall import RecallAnswer


@dataclass(slots=True)
class EvalCase:
    """One question and the PR number(s) whose content answers it."""

    question: str
    expect_prs: list[int]


@dataclass(slots=True)
class GoldSet:
    """A scorecard: the repo under test and its cases."""

    repo: str
    cases: list[EvalCase]


@dataclass(slots=True)
class CaseResult:
    """The outcome of scoring one case against a recall answer."""

    question: str
    expected_urls: list[str]
    found_urls: list[str]
    hit: bool


def pr_url(repo: str, number: int) -> str:
    """Canonical GitHub PR url: the unambiguous source identifier to match on."""
    return f"https://github.com/{repo}/pull/{number}"


def load_gold(path: str | Path) -> GoldSet:
    """Load a scorecard gold set from JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        EvalCase(question=case["question"], expect_prs=list(case["expect_prs"]))
        for case in data["cases"]
    ]
    return GoldSet(repo=data["repo"], cases=cases)


def score_case(case: EvalCase, repo: str, answer: RecallAnswer) -> CaseResult:
    """Score one case: a hit if any expected PR url appears among the answer's sources."""
    expected = [pr_url(repo, number) for number in case.expect_prs]
    found = [source.url for fact in answer.facts for source in fact.sources if source.url]
    hit = any(url in found for url in expected)
    return CaseResult(question=case.question, expected_urls=expected, found_urls=found, hit=hit)


def summarize(results: list[CaseResult]) -> str:
    """Render the scorecard: hit rate plus a per-case pass/fail line."""
    total = len(results)
    hits = sum(1 for result in results if result.hit)
    pct = round(100 * hits / total) if total else 0
    lines = [f"recall scorecard: {hits}/{total} cited the right PR ({pct}%)", ""]
    for result in results:
        mark = "PASS" if result.hit else "MISS"
        lines.append(f"[{mark}] {result.question}")
        if not result.hit:
            lines.append(f"       wanted: {', '.join(result.expected_urls) or '(none)'}")
    return "\n".join(lines)
