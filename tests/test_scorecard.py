"""Tests for the recall scorecard.

Scoring is pure: it takes a RecallAnswer (built here by hand) and checks whether
an expected PR url is among the cited sources. No graph and no key needed, so
the ruler is testable offline.
"""

import json
from pathlib import Path

import pytest

from relic.graph.recall import RecallAnswer, RecalledFact, Source
from relic.scorecard import (
    EvalCase,
    issue_url,
    load_gold,
    pr_url,
    score_case,
    summarize,
    to_payload,
)

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


def test_summarize_lists_missing_sources_on_partial_hit() -> None:
    case = EvalCase(question="q", expect_prs=[1, 4])
    text = summarize([score_case(case, REPO, _answer(pr_url(REPO, 1)))])
    assert "1/2 sources" in text
    assert f"missing: {pr_url(REPO, 4)}" in text
    assert pr_url(REPO, 1) not in text.split("missing:")[1]


def test_score_case_dedups_expected_urls() -> None:
    # a pr listed twice in the gold set must not cap coverage at 0.5
    case = EvalCase(question="q", expect_prs=[4, 4])
    result = score_case(case, REPO, _answer(pr_url(REPO, 4)))
    assert result.expected_urls == [pr_url(REPO, 4)]
    assert result.coverage == 1.0


def test_load_gold_rejects_case_with_no_expected_sources(tmp_path: Path) -> None:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps({"repo": REPO, "cases": [{"question": "q"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="no expected sources"):
        load_gold(path)


@pytest.mark.parametrize(
    "data",
    [
        [],  # top level not an object
        {"cases": [{"question": "q", "expect_prs": [1]}]},  # repo missing
        {"repo": "not a repo", "cases": [{"question": "q", "expect_prs": [1]}]},
        {"repo": REPO, "cases": []},  # no cases
        {"repo": REPO, "cases": ["q"]},  # case not an object
        {"repo": REPO, "cases": [{"question": " ", "expect_prs": [1]}]},  # blank question
        {"repo": REPO, "cases": [{"question": "q", "expect_prs": ["3"]}]},  # str number
        {"repo": REPO, "cases": [{"question": "q", "expect_prs": [3.0]}]},  # float number
        {"repo": REPO, "cases": [{"question": "q", "expect_prs": [0]}]},  # not positive
        {"repo": REPO, "cases": [{"question": "q", "expect_prs": [True]}]},  # bool
    ],
)
def test_load_gold_rejects_malformed_sets(tmp_path: Path, data: object) -> None:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_gold(path)


def test_match_survives_case_and_trailing_slash() -> None:
    # github urls are case-insensitive; a cosmetic variant must still score
    cited = "https://github.com/BuildRelic/Relic-Core/pull/3/"
    result = score_case(EvalCase(question="q", expect_prs=[3]), REPO, _answer(cited))
    assert result.hit is True
    assert result.matched_urls == [pr_url(REPO, 3)]  # canonical form, not the cited variant


def test_summarize_shows_cited_sources_on_miss() -> None:
    urls = [pr_url(REPO, n) for n in (5, 6, 7, 8, 9)]
    text = summarize([score_case(EvalCase(question="q", expect_prs=[3]), REPO, _answer(*urls))])
    assert f"got:    {urls[0]}, {urls[1]}, {urls[2]} (+2 more)" in text


def test_summarize_strips_control_characters() -> None:
    case = EvalCase(question="evil \x1b[2J\x07 question", expect_prs=[3])
    text = summarize([score_case(case, REPO, _answer(pr_url(REPO, 3)))])
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "evil" in text


def test_to_payload_reports_metrics_and_cases() -> None:
    results = [
        score_case(EvalCase("a", [3]), REPO, _answer(pr_url(REPO, 3))),
        score_case(EvalCase("b", [1, 4]), REPO, _answer(pr_url(REPO, 9), pr_url(REPO, 4))),
    ]
    payload = to_payload(results)
    assert payload["total"] == 2
    assert payload["hits"] == 2
    assert payload["hit_rate"] == 1.0
    assert payload["mrr"] == 0.75  # (1/1 + 1/2) / 2
    assert payload["coverage"] == 0.75  # (1.0 + 0.5) / 2
    cases = payload["cases"]
    assert isinstance(cases, list)
    partial = cases[1]
    assert partial["rank"] == 2
    assert partial["missing_urls"] == [pr_url(REPO, 1)]
    json.dumps(payload)  # must be serializable as-is


def test_summarize_handles_empty_results() -> None:
    assert "0/0" in summarize([])
