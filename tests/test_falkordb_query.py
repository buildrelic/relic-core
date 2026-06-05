"""Regression test for the FalkorDB empty-fulltext-query shim in engram.

graphiti-core <=0.29.1 builds `(@group_id:"x") ()` when a search term sanitizes to
nothing (punctuation or stopwords), and RediSearch rejects the empty parens with a
syntax error that aborts `add_episode`. `_patch_falkordb_empty_query` rewrites that
to '' (graphiti's "skip fulltext search" sentinel). No live server needed.
"""

from relic.graph.engram import _patch_falkordb_empty_query


def test_empty_text_yields_no_query_normal_text_survives() -> None:
    _patch_falkordb_empty_query()
    from graphiti_core.driver.falkordb.operations import search_ops

    build = search_ops._build_falkor_fulltext_query
    group = ["buildrelic__relic"]

    # All-punctuation and stopword-only terms must collapse to the skip sentinel,
    # not emit invalid empty parens.
    assert build(".", group) == ""
    assert build("the", group) == ""
    assert build("", group) == ""

    # Real terms still produce a group-scoped RediSearch query.
    assert build("auth login", group) == '(@group_id:"buildrelic__relic") (auth | login)'
