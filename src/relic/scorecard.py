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
- **coverage** -- mean share of each case's expected sources that were cited,
  averaged across all cases. One of two expected PRs found is half coverage, not
  a clean pass. For a single-source case coverage equals the hit, so this only
  separates from hit rate as multi-source cases land in the gold set.

This turns "is recall any good?" into numbers you can watch as ingestion and
retrieval change, and the ruler costs nothing per run beyond the recall calls it
measures. ``summarize`` renders a run for the terminal; ``to_payload`` renders
the same run as JSON-ready data (``relic eval --json``) so the numbers can be
tracked across runs instead of eyeballed from scrollback.
"""

from __future__ import annotations

import json
import re
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

    @property
    def missing_urls(self) -> list[str]:
        """Expected sources recall never cited -- what to chase on a miss or partial hit."""
        return [url for url in self.expected_urls if url not in self.matched_urls]


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _canon(url: str) -> str:
    """Comparison key for a source url. GitHub treats owner and repo names
    case-insensitively and a trailing slash is noise, so neither may turn an
    otherwise exact match into a silent zero."""
    return url.strip().rstrip("/").lower()


def _printable(text: str) -> str:
    """Drop control characters so text from a gold set or the graph cannot drive
    the terminal it is printed to (ANSI escapes, bell, cursor moves)."""
    return "".join(ch for ch in text if ch.isprintable())


def pr_url(repo: str, number: int) -> str:
    """Canonical GitHub PR url: the unambiguous source identifier to match on."""
    return f"https://github.com/{repo}/pull/{number}"


def issue_url(repo: str, number: int) -> str:
    """Canonical GitHub issue url, the source identifier for issue-backed cases."""
    return f"https://github.com/{repo}/issues/{number}"


def _expect_numbers(raw: dict, key: str) -> list[int]:
    """Read a case's expected PR or issue numbers, accepting only positive ints.

    A stray string, float, or bool would otherwise build a url like ``pull/3.0``
    that can never match, silently zeroing the case. Deduped because a number
    listed twice must not deflate coverage.
    """
    numbers = raw.get(key, [])
    if not isinstance(numbers, list) or any(
        isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in numbers
    ):
        msg = f"{key} must be a list of positive ints: {raw.get('question', '?')!r}"
        raise ValueError(msg)
    return list(dict.fromkeys(numbers))


def load_gold(path: str | Path) -> GoldSet:
    """Load a scorecard gold set from JSON, rejecting shapes that cannot score.

    Every rejection here is a case that would otherwise run and report a
    misleading number: a malformed repo or number builds urls that never match,
    a case with no expected sources can never hit, an empty set scores 0/0.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"gold set must be a JSON object: {path}"
        raise ValueError(msg)
    repo = data.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        msg = f"gold set repo must be owner/name: {repo!r}"
        raise ValueError(msg)
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        msg = f"gold set must list at least one case: {path}"
        raise ValueError(msg)
    cases: list[EvalCase] = []
    for raw in raw_cases:
        question = raw.get("question") if isinstance(raw, dict) else None
        if not isinstance(question, str) or not question.strip():
            msg = f"gold case needs a non-empty question: {raw!r}"
            raise ValueError(msg)
        case = EvalCase(
            question=question,
            expect_prs=_expect_numbers(raw, "expect_prs"),
            expect_issues=_expect_numbers(raw, "expect_issues"),
        )
        if not case.expect_prs and not case.expect_issues:
            msg = f"gold case has no expected sources: {case.question!r}"
            raise ValueError(msg)
        cases.append(case)
    return GoldSet(repo=repo, cases=cases)


def score_case(case: EvalCase, repo: str, answer: RecallAnswer) -> CaseResult:
    """Score one case: which expected sources were cited, and how highly ranked.

    Walks the answer's facts in ranked order. ``rank`` is the position of the first
    fact citing any expected source; ``matched_urls`` collects every expected source
    found across all facts. Rank is counted in facts, not flattened sources, because
    the fact is the unit recall ranks and reranking (REL-11) reorders. A match is an
    exact url comparison after case and trailing-slash normalization, since GitHub
    urls are case-insensitive and a cosmetic near-miss must not score zero.
    """
    expected = [pr_url(repo, number) for number in case.expect_prs]
    expected += [issue_url(repo, number) for number in case.expect_issues]
    # dedup, order preserved: a number listed twice must not deflate coverage
    expected = list(dict.fromkeys(expected))
    by_key = {_canon(url): url for url in expected}

    found_urls: list[str] = []
    matched: list[str] = []
    rank: int | None = None
    for position, fact in enumerate(answer.facts, start=1):
        for source in fact.sources:
            if not source.url:
                continue
            found_urls.append(source.url)
            url = by_key.get(_canon(source.url))
            if url is not None and url not in matched:
                matched.append(url)
                if rank is None:
                    rank = position
    return CaseResult(
        question=case.question,
        expected_urls=expected,
        found_urls=found_urls,
        matched_urls=matched,
        rank=rank,
    )


@dataclass(slots=True)
class Aggregate:
    """The scorecard's headline numbers over one eval run."""

    total: int
    hits: int
    mrr: float
    coverage: float

    @property
    def hit_rate(self) -> float:
        """Share of cases citing at least one expected source, in [0.0, 1.0]."""
        return self.hits / self.total if self.total else 0.0


def aggregate(results: list[CaseResult]) -> Aggregate:
    """Fold per-case results into the three headline metrics."""
    total = len(results)
    return Aggregate(
        total=total,
        hits=sum(1 for result in results if result.hit),
        mrr=sum(result.reciprocal_rank for result in results) / total if total else 0.0,
        coverage=sum(result.coverage for result in results) / total if total else 0.0,
    )


def summarize(results: list[CaseResult]) -> str:
    """Render the scorecard: the three aggregate metrics, then a per-case line.

    Misses also show a sample of what recall *did* cite, so investigating one
    starts from the output instead of a manual recall re-run.
    """
    agg = aggregate(results)
    pct = round(100 * agg.hit_rate)
    metrics = [
        ("hit rate", f"{agg.hits}/{agg.total} ({pct}%)", "at least one expected source cited"),
        ("MRR", f"{agg.mrr:.2f}", "mean 1/rank of the first correct source (1.0 = always first)"),
        ("coverage", f"{agg.coverage:.2f}", "mean share of expected sources found"),
    ]
    value_width = max(len(value) for _, value, _ in metrics) + 4
    lines = [f"recall scorecard: {agg.hits}/{agg.total} cited the right source ({pct}%)", ""]
    lines += [f"  {name:<11}{value:<{value_width}}{caption}" for name, value, caption in metrics]
    lines.append("")
    for result in results:
        mark = "PASS" if result.hit else "MISS"
        detail = ""
        if result.hit:
            bits = [f"rank {result.rank}"]
            if len(result.expected_urls) > 1:
                bits.append(f"{len(result.matched_urls)}/{len(result.expected_urls)} sources")
            detail = f"  ({', '.join(bits)})"
        lines.append(f"[{mark}] {_printable(result.question)}{detail}")
        if result.missing_urls:
            label = "missing" if result.hit else "wanted"
            lines.append(f"       {label}: {', '.join(result.missing_urls)}")
        if not result.hit and result.found_urls:
            cited = list(dict.fromkeys(result.found_urls))
            shown = ", ".join(_printable(url) for url in cited[:3])
            extra = f" (+{len(cited) - 3} more)" if len(cited) > 3 else ""
            lines.append(f"       got:    {shown}{extra}")
    return "\n".join(lines)


def to_payload(results: list[CaseResult]) -> dict[str, object]:
    """The same run as JSON-ready data, so metrics can be tracked across runs."""
    agg = aggregate(results)
    return {
        "total": agg.total,
        "hits": agg.hits,
        "hit_rate": round(agg.hit_rate, 4),
        "mrr": round(agg.mrr, 4),
        "coverage": round(agg.coverage, 4),
        "cases": [
            {
                "question": result.question,
                "hit": result.hit,
                "rank": result.rank,
                "reciprocal_rank": round(result.reciprocal_rank, 4),
                "coverage": round(result.coverage, 4),
                "expected_urls": result.expected_urls,
                "matched_urls": result.matched_urls,
                "missing_urls": result.missing_urls,
            }
            for result in results
        ],
    }
