"""Tests for the recall scorecard.

Scoring is pure: it takes a RecallAnswer (built here by hand) and checks whether
an expected PR url is among the cited sources. No graph and no key needed, so
the ruler is testable offline.
"""

from pathlib import Path

from relic.graph.recall import RecallAnswer, RecalledFact, Source
from relic.scorecard import EvalCase, issue_url, load_gold, pr_url, score_case, summarize

REPO = "buildrelic/relic-core"


def _answer(*urls: str) -> RecallAnswer:
    facts = [
        RecalledFact(fact="f", relation="", sources=[Source(label="s", url=url)]) for url in urls
    ]
    return RecallAnswer(query="q", facts=facts)


def test_score_case_hit_when_expected_pr_cited() -> None:
    case = EvalCase(question="q", expect_prs=[3])
    result = score_case(case, REPO, _answer(pr_url(REPO, 3), pr_url(REPO, 7)))
    assert result.hit is True
    assert pr_url(REPO, 3) in result.expected_urls


def test_score_case_hit_on_expected_issue() -> None:
    case = EvalCase(question="q", expect_issues=[42])
    result = score_case(case, REPO, _answer(issue_url(REPO, 42)))
    assert result.hit is True
    assert issue_url(REPO, 42) in result.expected_urls


def test_score_case_miss_when_expected_pr_absent() -> None:
    case = EvalCase(question="q", expect_prs=[3])
    result = score_case(case, REPO, _answer(pr_url(REPO, 7), pr_url(REPO, 8)))
    assert result.hit is False


def test_score_case_miss_on_empty_answer() -> None:
    result = score_case(EvalCase(question="q", expect_prs=[3]), REPO, RecallAnswer(query="q"))
    assert result.hit is False
    assert result.found_urls == []


def test_summarize_reports_hit_rate_and_marks() -> None:
    results = [
        score_case(EvalCase("a", [3]), REPO, _answer(pr_url(REPO, 3))),
        score_case(EvalCase("b", [4]), REPO, _answer(pr_url(REPO, 9))),
    ]
    text = summarize(results)
    assert "1/2" in text
    assert "PASS" in text
    assert "MISS" in text


def test_load_gold_parses_shipped_set() -> None:
    gold = load_gold(Path("eval/github_recall.json"))
    assert gold.repo == REPO
    assert len(gold.cases) == 8
    assert all(case.expect_prs for case in gold.cases)
