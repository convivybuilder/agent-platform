"""kb-eval scoring logic (bin/kb-eval, ARCH-121).

Exercises the pure scorers with a stub search callable — no index, no FastMCP.
Guards: rank/RR/hit flags, recall@k semantics (any expected id in top k),
filter pass-through, and the by-style/by-kind aggregation the /kb dashboard reads.
"""
from __future__ import annotations


def test_kind_of(kb_eval):
    assert kb_eval.kind_of("shared/runbooks/foo") == "runbooks"
    assert kb_eval.kind_of("globex/decisions/0001-x") == "decisions"
    assert kb_eval.kind_of("nodelim") == "?"


def test_score_case_hit_rank(kb_eval):
    def search(query, limit, **f):
        return [{"id": "a/x/1"}, {"id": "shared/runbooks/target"}, {"id": "a/x/3"}]

    case = {"id": "c1", "query": "q", "style": "keyword", "expect": ["shared/runbooks/target"]}
    r = kb_eval.score_case(case, search, 10)
    assert r["rank"] == 2 and r["rr"] == 0.5
    assert r["hit@1"] is False and r["hit@5"] is True and r["hit@k"] is True
    assert r["expect_kind"] == "runbooks"


def test_score_case_miss(kb_eval):
    def search(query, limit, **f):
        return [{"id": "a/x/1"}]

    case = {"id": "c2", "query": "q", "expect": ["shared/runbooks/target"]}
    r = kb_eval.score_case(case, search, 10)
    assert r["rank"] is None and r["rr"] == 0.0 and r["hit@k"] is False


def test_score_case_any_expected_counts(kb_eval):
    # recall@k is satisfied if ANY expected id appears in the top k.
    def search(query, limit, **f):
        return [{"id": "agent-platform/agents/security-auditor"}]

    case = {"id": "c3", "query": "security reviewer",
            "expect": ["agent-platform/agents/security-reviewer", "agent-platform/agents/security-auditor"]}
    r = kb_eval.score_case(case, search, 10)
    assert r["rank"] == 1 and r["hit@1"] is True


def test_score_case_passes_filters_and_limit(kb_eval):
    seen = {}

    def search(query, limit, **f):
        seen.update(f)
        seen["limit"] = limit
        return []

    case = {"id": "c4", "query": "deploy", "expect": ["globex/decisions/0012-x"],
            "filters": {"project": "globex"}}
    kb_eval.score_case(case, search, 7)
    assert seen["project"] == "globex" and seen["limit"] == 7


def test_example_golden_set_is_well_formed(kb_eval):
    # Guards the repository's synthetic example set: every case must carry the
    # keys score_case reads, a non-empty expect list, a known style, and a unique
    # id, and both query styles must be represented. Comment lines (#) are
    # skipped by load_golden.
    cases = kb_eval.load_golden(kb_eval.GOLDEN_EXAMPLE)
    assert len(cases) >= 6
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate golden case id"
    styles = {c.get("style", "keyword") for c in cases}
    assert styles == {"keyword", "nl"}, "example set must cover both query styles"
    for c in cases:
        assert c["query"].strip(), f"empty query in {c['id']}"
        assert isinstance(c["expect"], list) and c["expect"], f"bad expect in {c['id']}"
        assert all("/" in e for e in c["expect"]), f"expect id not a doc path in {c['id']}"
        assert c.get("style", "keyword") in ("keyword", "nl"), f"bad style in {c['id']}"


def test_resolve_golden_prefers_an_explicit_override(kb_eval, tmp_path):
    explicit = tmp_path / "custom-golden.jsonl"
    assert kb_eval.resolve_golden(explicit) == explicit


def test_default_golden_scores_the_install_set_when_present(kb_eval, tmp_path, monkeypatch, capsys):
    install_golden = tmp_path / "install" / "kb-golden.jsonl"
    install_golden.parent.mkdir(parents=True)
    install_golden.write_text('{"id": "x", "query": "q", "expect": ["a/b"]}\n')
    monkeypatch.setattr(kb_eval.BR, "root", lambda: tmp_path)

    assert kb_eval.default_golden() == install_golden
    assert kb_eval.resolve_golden(None) == install_golden
    assert capsys.readouterr().err == ""


def test_default_golden_falls_back_to_the_example_set_when_absent(kb_eval, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kb_eval.BR, "root", lambda: tmp_path)  # no install/ at all

    assert kb_eval.default_golden() == kb_eval.GOLDEN_EXAMPLE
    assert kb_eval.resolve_golden(None) == kb_eval.GOLDEN_EXAMPLE
    err = capsys.readouterr().err
    assert "synthetic example set" in err
    assert str(tmp_path / "install" / "kb-golden.jsonl") in err


def test_aggregate_by_style_and_overall(kb_eval):
    scored = [
        {"style": "keyword", "expect_kind": "runbooks", "hit@1": True, "hit@5": True, "hit@k": True, "rr": 1.0},
        {"style": "nl", "expect_kind": "decisions", "hit@1": False, "hit@5": False, "hit@k": False, "rr": 0.0},
    ]
    agg = kb_eval.aggregate(scored, 10)
    assert agg["overall"]["n"] == 2 and agg["overall"]["recall@10"] == 0.5 and agg["overall"]["mrr"] == 0.5
    assert agg["by_style"]["keyword"]["recall@1"] == 1.0
    assert agg["by_style"]["nl"]["mrr"] == 0.0 and agg["by_style"]["nl"]["recall@10"] == 0.0
