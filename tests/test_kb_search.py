"""kb_search query rewriting (bin/mcp-kb-server._build_match, ARCH-121).

Guards the retrieval fix that took the golden-set eval from recall@10 62% to
100% (NL queries 0% → 100%): free-text → an OR-combined, stopword-stripped,
quoted FTS5 expression. The quoting is a safety property — an unquoted FTS5
keyword (AND/OR/NOT/NEAR) in a user query would raise a syntax error at search
time. Ranking + column weighting live in the SQL and are covered by bin/kb-eval.
"""
from __future__ import annotations


def test_strips_stopwords_and_or_combines(mcp_kb_server):
    # "how"/"do"/"i"/"the"/"to" are stopwords; content terms OR-combined + quoted.
    assert mcp_kb_server._build_match("how do I deploy the gateway to production") == \
        '"deploy" OR "gateway" OR "production"'


def test_drops_punctuation_and_short_tokens(mcp_kb_server):
    assert mcp_kb_server._build_match("what's the gateway?") == '"gateway"'


def test_all_stopwords_returns_none(mcp_kb_server):
    # Nothing meaningful survives → None, so the caller falls back to structured.
    assert mcp_kb_server._build_match("how do the these are") is None
    assert mcp_kb_server._build_match("   ") is None


def test_dedups_preserving_order(mcp_kb_server):
    assert mcp_kb_server._build_match("gateway gateway deploy gateway") == \
        '"gateway" OR "deploy"'


def test_fts5_operators_are_quoted_not_injected(mcp_kb_server):
    # 'AND' is a stopword (dropped); 'near'/'or'-as-content must be quoted so
    # FTS5 reads them as literals, never operators — else search() would raise.
    assert mcp_kb_server._build_match("security AND reviewer") == '"security" OR "reviewer"'
    near = mcp_kb_server._build_match("near miss")
    assert near == '"near" OR "miss"'  # 'near' quoted, not the NEAR operator


def test_lowercases(mcp_kb_server):
    assert mcp_kb_server._build_match("Gateway DEPLOY") == '"gateway" OR "deploy"'
