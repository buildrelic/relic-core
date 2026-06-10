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
    """One fact per url, in order: the url's position is its fact rank."""
    facts = [
        RecalledFact(fact="f", relation="", sources=[Source(label="s", url=url)]) for url in urls
    ]
    return RecallAnswer(query="q", facts=facts)


def _fact(*urls: str) -> RecalledFact:
    """A single fact citing several sources -- to test fact-rank vs source order."""
    return RecalledFact(fact="f", relation="", sources=[Source(label="s", url=url) for url in urls])


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


def test_rank_is_position_of_first_fact_with_expected_source() -> None:
    case = EvalCase(question="q", expect_prs=[3])
    result = score_case(case, REPO, _answer(pr_url(REPO, 7), pr_url(REPO, 3)))
    assert result.rank == 2
    assert result.reciprocal_rank == 0.5


def test_rank_counts_facts_not_sources() -> None:
    # pr 3 is the second *source* but sits in the first (only) fact -> rank 1.
    answer = RecallAnswer(query="q", facts=[_fact(pr_url(REPO, 7), pr_url(REPO, 3))])
    result = score_case(EvalCase(question="q", expect_prs=[3]), REPO, answer)
    assert result.rank == 1
    assert result.reciprocal_rank == 1.0


def test_reciprocal_rank_is_zero_on_miss() -> None:
    result = score_case(EvalCase(question="q", expect_prs=[3]), REPO, _answer(pr_url(REPO, 7)))
    assert result.rank is None
    assert result.reciprocal_rank == 0.0


def test_coverage_partial_when_some_expected_found() -> None:
    case = EvalCase(question="q", expect_prs=[1, 4])
    result = score_case(case, REPO, _answer(pr_url(REPO, 1)))
    assert result.hit is True
    assert result.coverage == 0.5
    assert result.matched_urls == [pr_url(REPO, 1)]


def test_coverage_full_when_all_expected_found() -> None:
    case = EvalCase(question="q", expect_prs=[1, 4])
    result = score_case(case, REPO, _answer(pr_url(REPO, 4), pr_url(REPO, 1)))
    assert result.coverage == 1.0
    assert set(result.matched_urls) == {pr_url(REPO, 1), pr_url(REPO, 4)}
    assert result.rank == 1


def test_summarize_reports_mrr_and_coverage() -> None:
    results = [
        score_case(EvalCase("a", [3]), REPO, _answer(pr_url(REPO, 3))),
        score_case(EvalCase("b", [4]), REPO, _answer(pr_url(REPO, 9), pr_url(REPO, 4))),
    ]
    text = summarize(results)
    assert "MRR" in text
    assert "0.75" in text  # (1/1 + 1/2) / 2
    assert "coverage" in text
    assert "rank 1" in text
