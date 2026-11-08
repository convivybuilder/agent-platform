"""lib/agent-platform/ledger.py — the shared lane classifier and at-finish
spend-ledger poster `bin/agentctl`'s `_finalize_run` and `bin/model-scorecard`'s
sweep both call (test-as-touched, ARCH-098/099)."""
from __future__ import annotations

import json

import pytest

import cost_join as cj
import ledger as LG


def _is_gateway(model):
    return bool(model) and model.startswith("openai/")


def _is_codex(model):
    return bool(model) and model.startswith("gpt-")


@pytest.fixture(autouse=True)
def _ledger_refusal_marker_home(tmp_path, monkeypatch):
    """Every refusal-marker read/write in this file (`ledger.write_refusal_
    marker`/`_read_refused_marker_usd`) resolves `home` through `run_sandbox.
    passwd_home()` when no explicit `home` is given — redirected here to
    this test's own `tmp_path` so no test in this file touches this
    machine's own `~/.local/state/agent-platform/ledger-refusals` (round 2,
    PR #1021 round 1 finding)."""
    monkeypatch.setattr(LG.RS, "passwd_home", lambda: tmp_path)


# --- ledger_lane --------------------------------------------------------------


def test_ledger_lane_gateway_for_a_gateway_model():
    assert LG.ledger_lane("openai/gpt-5", is_gateway_model=_is_gateway,
                          is_codex_model=_is_codex) == "gateway"


def test_ledger_lane_subscription_for_an_anthropic_model():
    assert LG.ledger_lane("claude-sonnet-5", is_gateway_model=_is_gateway,
                          is_codex_model=_is_codex) == "subscription"


def test_ledger_lane_openai_subscription_for_a_codex_model():
    assert LG.ledger_lane("gpt-5.5", is_gateway_model=_is_gateway,
                          is_codex_model=_is_codex) == "openai-subscription"


def test_ledger_lane_none_for_no_model():
    assert LG.ledger_lane(None, is_gateway_model=_is_gateway,
                          is_codex_model=_is_codex) is None
    assert LG.ledger_lane("", is_gateway_model=_is_gateway,
                          is_codex_model=_is_codex) is None


# --- post_run_ledger_entry -----------------------------------------------------


def _mk_run_dir(tmp_path, **meta_fields):
    rd = tmp_path / "run-ledger-test"
    rd.mkdir()
    meta = {"run_id": "run-ledger-test", "agent": "reviewer", "model": "claude-sonnet-5"}
    meta.update(meta_fields)
    (rd / "meta.json").write_text(json.dumps(meta))
    return rd


def _stub_list_ledger_entries(monkeypatch, entries):
    """Patches `LG.issue_client.list_ledger_entries` to return `entries`
    (a list of plain dicts) regardless of its filters — every
    `post_run_ledger_entry` test below that resolves a lane now makes this
    read (`ledger.read_live_ledger_state`) before it ever reaches
    `post_ledger_entry`, so a test that doesn't stub it would otherwise hit
    the real network client and raise `IssueQueueUnavailable`."""
    monkeypatch.setattr(LG.issue_client, "list_ledger_entries",
                        lambda *a, **kw: entries)


def test_post_run_ledger_entry_posts_once_with_run_id_as_dedup_key(tmp_path, monkeypatch):
    """A finished reviewer-family run posts once, under dedup_key=run_id — the
    same key model-scorecard's own sweep posts under, so the two writers can
    never double-count."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(monkeypatch, [])
    captured = {}

    def fake_post(product, usd, *, run_id, dedup_key, lane, **kw):
        captured["calls"] = captured.get("calls", 0) + 1
        captured["run_id"] = run_id
        captured["dedup_key"] = dedup_key
        captured["lane"] = lane
        captured["product"] = product
        captured["usd"] = usd
        captured.update(kw)
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(LG.issue_client, "post_ledger_entry", fake_post)
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.42, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex, day="2026-10-21")

    assert status == "recorded"
    assert captured["calls"] == 1
    assert captured["run_id"] == "run-ledger-test"
    assert captured["dedup_key"] == "run-ledger-test"
    assert captured["lane"] == "subscription"
    assert captured["day"] == "2026-10-21"
    assert captured["agent"] == "reviewer"
    assert captured["model"] == "claude-sonnet-5"


def test_post_run_ledger_entry_records_the_status_on_meta(tmp_path, monkeypatch):
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(monkeypatch, [])
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry",
                        lambda *a, **kw: {"status": "recorded", "entry": {}})
    LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_post_status"] == "recorded"


def test_post_run_ledger_entry_skips_no_lane_without_posting(tmp_path, monkeypatch):
    """No model at all resolves no lane and returns before any ledger read —
    `list_ledger_entries` is left unstubbed here on purpose, so a regression
    that moved the ledger read ahead of the lane check would error rather
    than silently pass."""
    rd = _mk_run_dir(tmp_path, model=None)
    called = []
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry",
                        lambda *a, **kw: called.append(1))
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model=None,
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    assert status == "skip:no-lane"
    assert called == []
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_post_status"] == "skip:no-lane"


def test_post_run_ledger_entry_never_raises_when_the_post_blows_up(tmp_path, monkeypatch):
    """A failing or refused post must never change the run's exit status or
    outcome — it must not even raise, regardless of what the HTTP layer does."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(monkeypatch, [])

    def _boom(*a, **kw):
        raise ConnectionRefusedError("library is down")
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry", _boom)

    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)  # must not raise

    assert status == "error:ConnectionRefusedError"
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_post_status"] == "error:ConnectionRefusedError"


def test_post_run_ledger_entry_never_raises_on_an_unwritable_meta(tmp_path, monkeypatch):
    """Even a failure to record the status on meta.json (disk full, a
    concurrently-deleted run dir) must not escape — the ledger post is
    strictly best-effort."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(monkeypatch, [])
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry",
                        lambda *a, **kw: {"status": "recorded", "entry": {}})
    (rd / "meta.json").unlink()  # T.update_meta's read-modify-write now fails

    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)  # must not raise

    assert status == "recorded"


def test_post_run_ledger_entry_rejects_a_non_finite_cost(tmp_path, monkeypatch):
    """`cost_usd` is validated finite and non-negative before any arithmetic
    or network call — the same boundary `budget._partial_usd` applies to the
    same run-influenced figure. Against code with no such check, `float('nan')`
    would flow straight into `remaining_after_live`'s subtraction and post a
    NaN `usd` to the Library."""
    rd = _mk_run_dir(tmp_path)
    called = []
    monkeypatch.setattr(LG.issue_client, "list_ledger_entries",
                        lambda *a, **kw: called.append("read"))
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry",
                        lambda *a, **kw: called.append("post"))

    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=float("nan"), is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)

    assert status == "error:invalid-cost"
    assert called == []
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_post_status"] == "error:invalid-cost"


def test_post_run_ledger_entry_posts_nothing_when_the_ledger_read_fails(
        tmp_path, monkeypatch):
    """Against code with no ledger read at all (3adf80d9b, which trusted a
    cached meta.json figure instead), this scenario doesn't exist — there is
    nothing to fail. Against the fixed code, an unreachable Library at
    finish must post NOTHING (never falling back to the raw, un-reduced
    cost_usd), and a LATER call — standing in for model-scorecard's next
    sweep pass — that reaches the Library posts exactly the remainder, with
    no double-count from the failed first attempt."""
    rd = _mk_run_dir(tmp_path)

    def _boom(*a, **kw):
        raise LG.issue_client.IssueQueueUnavailable("library is down")
    monkeypatch.setattr(LG.issue_client, "list_ledger_entries", _boom)
    posted = []
    monkeypatch.setattr(
        LG.issue_client, "post_ledger_entry",
        lambda product, usd, *, run_id, dedup_key, lane, **kw:
            posted.append(usd) or {"status": "recorded", "entry": {}})

    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.50, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)

    assert status == "error:ledger-read-IssueQueueUnavailable"
    assert posted == []

    # The Library comes back with nothing recorded yet for this run — the
    # later pass posts the full, un-reduced $0.50, exactly the remainder.
    monkeypatch.setattr(LG.issue_client, "list_ledger_entries", lambda *a, **kw: [])
    status2 = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.50, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    assert status2 == "recorded"
    assert posted == [pytest.approx(0.50)]


# --- record_disproportionate_refusal / refused_figure_unchanged (E1·9,
# budget-enforcement epic Clause 6: a refused report is surfaced against the
# run that produced it and never resubmitted unchanged) -----------------------


def test_record_disproportionate_refusal_writes_all_four_fields(tmp_path):
    rd = _mk_run_dir(tmp_path)
    LG.record_disproportionate_refusal(
        rd, usd=0.1234, reason="this report exceeds 10x the scope's soft cap",
        poster="finish", dedup_key="run-ledger-test")
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_report_refused_usd"] == 0.1234
    assert meta["ledger_report_refused_reason"] == "this report exceeds 10x the scope's soft cap"
    assert meta["ledger_report_refused_poster"] == "finish"
    assert meta["ledger_report_refused_dedup_key"] == "run-ledger-test"


def test_record_disproportionate_refusal_never_raises_on_an_unwritable_meta(tmp_path):
    rd = _mk_run_dir(tmp_path)
    (rd / "meta.json").unlink()
    LG.record_disproportionate_refusal(
        rd, usd=0.1, reason="x", poster="live", dedup_key="run-ledger-test:live:1")  # must not raise


def test_refused_figure_unchanged_true_on_an_exact_match():
    LG.write_refusal_marker("run-abc", usd=0.1234, reason="x", poster="finish",
                            dedup_key="run-abc")
    assert LG.refused_figure_unchanged(0.1234, run_id="run-abc") is True


def test_refused_figure_unchanged_false_on_a_different_figure():
    LG.write_refusal_marker("run-abc", usd=999.0, reason="x", poster="finish",
                            dedup_key="run-abc")
    assert LG.refused_figure_unchanged(0.1234, run_id="run-abc") is False


def test_refused_figure_unchanged_false_with_no_prior_refusal():
    assert LG.refused_figure_unchanged(0.1234, run_id="run-abc") is False


def test_refused_figure_unchanged_false_with_a_meta_json_only_refusal(tmp_path):
    """Round 2 (security review of PR #1021 round 1, blocking finding):
    against the round-1 code, a run's own meta.json carrying
    `ledger_report_refused_usd` was enough to skip — a field the DISPATCHED
    run itself can write for its whole execution window. Against the fixed
    code, meta.json alone decides nothing; only the marker file does, and
    none exists here."""
    rd = _mk_run_dir(tmp_path, ledger_report_refused_usd=0.1234,
                     ledger_report_refused_reason="this report exceeds 10x the scope's soft cap")
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_report_refused_usd"] == 0.1234  # the run's own forged field
    assert LG.refused_figure_unchanged(0.1234, run_id="run-ledger-test") is False


def test_refused_figure_unchanged_false_on_a_malformed_marker_file(tmp_path):
    """A marker file that is present but not valid JSON — the safe
    direction is to treat it as no prior refusal, never to raise or to
    trust a truncated/corrupt record."""
    path = LG.refusal_marker_path(tmp_path, "run-abc")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert LG.refused_figure_unchanged(0.1234, run_id="run-abc") is False


def test_refused_figure_unchanged_false_on_a_non_finite_marker_figure(tmp_path):
    """A marker carrying a non-finite `usd` (the exact shape a forged
    record could contain) must not compare equal to anything — `nan` or
    `inf` never equals any real figure in Python, but this is also the
    `_read_refused_marker_usd` guard rejecting it outright before any
    comparison is made."""
    path = LG.refusal_marker_path(tmp_path, "run-abc")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": "run-abc", "usd": float("nan"),
                                "reason": "x", "poster": "finish", "dedup_key": "run-abc"}))
    assert LG.refused_figure_unchanged(float("nan"), run_id="run-abc") is False
    assert LG.refused_figure_unchanged(0.1234, run_id="run-abc") is False


def test_write_refusal_marker_rejects_an_unsafe_run_id(tmp_path):
    """`refusal_marker_path` is the one place an untrusted run_id becomes a
    path component; a run_id carrying `/` or `..` must not escape
    `REFUSAL_MARKER_RELDIR`, and `write_refusal_marker` must swallow the
    resulting `ValueError` rather than raise it into its caller."""
    with pytest.raises(ValueError):
        LG.refusal_marker_path(tmp_path, "../escape")
    LG.write_refusal_marker("../escape", usd=0.1, reason="x", poster="finish",
                            dedup_key="x")  # must not raise
    assert not (tmp_path.parent / "escape.json").exists()


# --- post_run_ledger_entry and a disproportionate refusal (E1·9) -------------


def test_post_run_ledger_entry_records_a_disproportionate_refusal_with_poster_and_key(
        tmp_path, monkeypatch):
    """Against the unfixed code (no write back to the run at all), the run
    dir's meta.json never gains any of the four refusal fields."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(monkeypatch, [])
    monkeypatch.setattr(
        LG.issue_client, "post_ledger_entry",
        lambda *a, **kw: {"status": "disproportionate",
                          "error": "this report exceeds 10x the scope's soft cap",
                          "reported_usd": 0.1, "basis": "soft_cap", "entry": {}})
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    assert status == "disproportionate"
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_report_refused_usd"] == pytest.approx(0.1)
    assert meta["ledger_report_refused_reason"] == "this report exceeds 10x the scope's soft cap"
    assert meta["ledger_report_refused_poster"] == "finish"
    assert meta["ledger_report_refused_dedup_key"] == "run-ledger-test"


def test_post_run_ledger_entry_skips_resending_an_unchanged_refused_figure(
        tmp_path, monkeypatch):
    """Clause 6: against the unfixed code (no pre-post check against the
    run's own refusal mark), the stub below would receive a second call for
    the same $0.1 figure — `calls` would be `[1]`, not `[]`. The marker
    comes from the state-tree file (round 2, PR #1021: never meta.json,
    which a dispatched run can write for its whole execution window)."""
    rd = _mk_run_dir(tmp_path)
    LG.write_refusal_marker("run-ledger-test", usd=0.1, reason="x", poster="finish",
                            dedup_key="run-ledger-test")
    _stub_list_ledger_entries(monkeypatch, [])
    calls = []
    monkeypatch.setattr(
        LG.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "disproportionate",
                                             "error": "x", "entry": {}})
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    assert status == "skip:refused-unchanged"
    assert calls == []


def test_post_run_ledger_entry_posts_despite_a_meta_json_only_refused_figure(
        tmp_path, monkeypatch):
    """Round 2 (security review of PR #1021 round 1, BLOCKING finding): a
    run's own meta.json carrying `ledger_report_refused_usd` equal to the
    figure about to be sent, with NO marker file, must not be skipped — a
    dispatched run holds `AGENTCTL_RUN_DIR` and unrestricted Bash for its
    whole execution window, so it can set this field to its own true
    remaining spend and make a real refusal disappear from the Library.

    AGAINST THE ROUND-1 CODE (a4e1bc1e3's ledger.py, which read
    `REFUSED_USD_FIELD` straight off `run_dir`'s meta.json), this test
    fails: `calls` comes back `[]` and `status` reads
    `"skip:refused-unchanged"`, because that code treated the run's own
    forged field as a genuine prior refusal."""
    rd = _mk_run_dir(tmp_path, ledger_report_refused_usd=0.1,
                     ledger_report_refused_reason="forged by the run itself")
    _stub_list_ledger_entries(monkeypatch, [])
    calls = []
    monkeypatch.setattr(
        LG.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)
    assert status == "recorded"
    assert calls == [1]


def test_post_run_ledger_entry_reposts_a_changed_figure_after_a_refusal(
        tmp_path, monkeypatch):
    """A later call with a DIFFERENT figure for the same run (its cost was
    corrected) posts again — against an implementation that keys the skip on
    run_id alone rather than the figure, the stub below would never be
    called."""
    rd = _mk_run_dir(tmp_path)
    LG.write_refusal_marker("run-ledger-test", usd=999.0, reason="x", poster="finish",
                            dedup_key="run-ledger-test")
    _stub_list_ledger_entries(monkeypatch, [])
    calls = []
    monkeypatch.setattr(
        LG.issue_client, "post_ledger_entry",
        lambda *a, **kw: calls.append(1) or {"status": "recorded", "entry": {}})
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.1, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)  # 0.1, not the refused 999.0
    assert status == "recorded"
    assert calls == [1]


# --- read_live_ledger_state (CQ·446 round 2) ------------------------------------


def test_read_live_ledger_state_sums_non_voided_entries_per_lane(monkeypatch):
    monkeypatch.setattr(
        LG.issue_client, "list_ledger_entries",
        lambda *, run_id, timeout: [
            {"usd": 0.10, "lane": "subscription", "voided_at": None},
            {"usd": 0.05, "lane": "subscription", "voided_at": None},
            {"usd": 0.20, "lane": "gateway", "voided_at": None},
        ])
    state = LG.read_live_ledger_state("run-abc", timeout=3.0)
    assert state["lane_totals"] == {"subscription": pytest.approx(0.15),
                                    "gateway": pytest.approx(0.20)}
    assert state["entry_count"] == 3


def test_read_live_ledger_state_excludes_voided_entries_from_the_sum_but_counts_them(
        monkeypatch):
    """A voided entry still occupies a sequence slot at the Library (it was
    recorded once, under a dedup_key that must never be reused), so it counts
    toward `entry_count`, but its `usd` must not count toward what's already
    been reported — a run whose live post was voided must be able to have
    that amount re-reported."""
    monkeypatch.setattr(
        LG.issue_client, "list_ledger_entries",
        lambda *, run_id, timeout: [
            {"usd": 0.30, "lane": "subscription", "voided_at": "2026-10-24T00:00:00Z"},
            {"usd": 0.10, "lane": "subscription", "voided_at": None},
        ])
    state = LG.read_live_ledger_state("run-abc", timeout=3.0)
    assert state["lane_totals"] == {"subscription": pytest.approx(0.10)}
    assert state["entry_count"] == 2


def test_read_live_ledger_state_raises_on_a_non_finite_usd_entry(monkeypatch):
    monkeypatch.setattr(
        LG.issue_client, "list_ledger_entries",
        lambda *, run_id, timeout: [{"usd": float("inf"), "lane": "subscription",
                                     "voided_at": None, "dedup_key": "run-abc:live:1"}])
    with pytest.raises(ValueError):
        LG.read_live_ledger_state("run-abc", timeout=3.0)


def test_read_live_ledger_state_raises_on_a_negative_usd_entry(monkeypatch):
    monkeypatch.setattr(
        LG.issue_client, "list_ledger_entries",
        lambda *, run_id, timeout: [{"usd": -0.01, "lane": "subscription",
                                     "voided_at": None, "dedup_key": "run-abc:live:1"}])
    with pytest.raises(ValueError):
        LG.read_live_ledger_state("run-abc", timeout=3.0)


def test_read_live_ledger_state_raises_on_a_non_numeric_usd_entry(monkeypatch):
    monkeypatch.setattr(
        LG.issue_client, "list_ledger_entries",
        lambda *, run_id, timeout: [{"usd": "not-a-number", "lane": "subscription",
                                     "voided_at": None, "dedup_key": "run-abc:live:1"}])
    with pytest.raises(ValueError):
        LG.read_live_ledger_state("run-abc", timeout=3.0)


# --- remaining_after_live (CQ·446) ---------------------------------------------


def test_remaining_after_live_subtracts_the_lanes_own_figure():
    assert LG.remaining_after_live(1.50, {"subscription": 0.40}, "subscription") == 1.10


def test_remaining_after_live_ignores_a_different_lanes_figure():
    """A fail-over run that live-reported under the gateway lane must not
    have that amount subtracted from the subscription lane's own final cost
    — the two lanes are tracked, and subtracted, independently."""
    assert LG.remaining_after_live(1.50, {"gateway": 0.40}, "subscription") == 1.50


def test_remaining_after_live_defaults_to_zero_with_no_live_reported_record():
    assert LG.remaining_after_live(1.50, None, "subscription") == 1.50
    assert LG.remaining_after_live(1.50, {}, "subscription") == 1.50


def test_remaining_after_live_can_go_negative_when_live_posting_overshot():
    """The caller (post_run_ledger_entry / report_to_ledger), not this
    function, decides a non-positive remainder posts nothing."""
    assert LG.remaining_after_live(1.00, {"subscription": 1.20}, "subscription") == -0.20


# --- live_dedup_key (CQ·446) ----------------------------------------------------


def test_live_dedup_key_is_distinct_from_the_bare_run_id():
    key = LG.live_dedup_key("run-abc", 1)
    assert key != "run-abc"
    assert key == "run-abc:live:1"


def test_live_dedup_key_changes_with_the_sequence_number():
    assert LG.live_dedup_key("run-abc", 1) != LG.live_dedup_key("run-abc", 2)


# --- post_run_ledger_entry subtracts the live-reported figure (CQ·446) --------


def test_post_run_ledger_entry_posts_only_the_remainder_after_live_reporting(
        tmp_path, monkeypatch):
    """Against the unfixed code (no subtraction at all), this would post the
    full $0.42 again, not the $0.12 actually left over."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(
        monkeypatch, [{"usd": 0.30, "lane": "subscription", "voided_at": None}])
    captured = {}

    def fake_post(product, usd, *, run_id, dedup_key, lane, **kw):
        captured["usd"] = usd
        return {"status": "recorded", "entry": {}}

    monkeypatch.setattr(LG.issue_client, "post_ledger_entry", fake_post)
    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.42, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)

    assert status == "recorded"
    assert captured["usd"] == pytest.approx(0.12)


def test_post_run_ledger_entry_skips_without_posting_when_live_reporting_covered_it(
        tmp_path, monkeypatch):
    """Against the unfixed code, a run whose live reporter already posted its
    whole cost would be double-counted here — this must post nothing at all."""
    rd = _mk_run_dir(tmp_path)
    _stub_list_ledger_entries(
        monkeypatch, [{"usd": 0.42, "lane": "subscription", "voided_at": None}])
    called = []
    monkeypatch.setattr(LG.issue_client, "post_ledger_entry",
                        lambda *a, **kw: called.append(1))

    status = LG.post_run_ledger_entry(
        rd, run_id="run-ledger-test", repo="agent-platform", model="claude-sonnet-5",
        agent="reviewer", cost_usd=0.42, is_gateway_model=_is_gateway,
        is_codex_model=_is_codex)

    assert status == "skip:already-reported-live"
    assert called == []
    meta = json.loads((rd / "meta.json").read_text())
    assert meta["ledger_post_status"] == "skip:already-reported-live"


# --- resolve_item_id: the one PR-match rule the run-cost and ledger sides of
# E16's attribution split both go through, so a run with no --issue can't be
# attributed differently by the two. ------------------------------------------


_A_PR_LINK = {"pr_url": "https://github.com/convivy/widget/pull/55", "pr_number": 55}
_AN_ITEM_LINKING_IT = {"id": "issue_XYZ", "pr_url": _A_PR_LINK["pr_url"],
                       "pr_repo": None, "pr": None}


def test_resolve_item_id_returns_the_named_issue_directly(tmp_path):
    rd = _mk_run_dir(tmp_path)
    assert LG.resolve_item_id(rd, issue="issue_ABC", repo="widget") == "issue_ABC"


def test_resolve_item_id_never_reads_pr_link_when_issue_is_given(tmp_path):
    """Against code that always tries the PR match, a malformed pr-link.json
    would raise here; the real `issue` short-circuit never reads the file."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text("{not json")
    assert LG.resolve_item_id(rd, issue="issue_ABC", repo="widget") == "issue_ABC"


def test_resolve_item_id_none_with_no_issue_and_no_run_dir():
    assert LG.resolve_item_id(None, issue=None, repo="widget") is None


def test_resolve_item_id_none_with_no_issue_and_no_pr_link(tmp_path, monkeypatch):
    """No pr-link.json at all: None, with no item-corpus fetch attempted —
    `list_all_items` is left unstubbed so a regression that fetches anyway
    fails this test on the unstubbed network call."""
    rd = _mk_run_dir(tmp_path)
    assert LG.resolve_item_id(rd, issue=None, repo="widget") is None


def test_resolve_item_id_resolves_through_the_runs_pr(tmp_path, monkeypatch):
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    monkeypatch.setattr(LG.issue_client, "list_all_items",
                        lambda *a, **kw: [_AN_ITEM_LINKING_IT])
    assert LG.resolve_item_id(rd, issue=None, repo="widget") == "issue_XYZ"


def test_resolve_item_id_uses_the_given_items_without_fetching(tmp_path, monkeypatch):
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))

    def _boom(*a, **kw):
        raise AssertionError("must not fetch when items is already given")
    monkeypatch.setattr(LG.issue_client, "list_all_items", _boom)
    assert LG.resolve_item_id(
        rd, issue=None, repo="widget", items=[_AN_ITEM_LINKING_IT]) == "issue_XYZ"


def test_resolve_item_id_none_when_the_pr_matches_no_item(tmp_path, monkeypatch):
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    monkeypatch.setattr(LG.issue_client, "list_all_items", lambda *a, **kw: [])
    assert LG.resolve_item_id(rd, issue=None, repo="widget") is None


def test_resolve_item_id_posts_with_no_item_and_records_why_on_a_failed_lookup(
        tmp_path, monkeypatch):
    """A failed item lookup (the Library unreachable) must never block or fail
    the caller's own ledger post: this returns None rather than raising, and
    records the reason on the run's own meta.json beside `ledger_post_status`."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))

    def _boom(*a, **kw):
        raise LG.issue_client.IssueQueueUnavailable("library is down")
    monkeypatch.setattr(LG.issue_client, "list_all_items", _boom)

    assert LG.resolve_item_id(rd, issue=None, repo="widget") is None
    meta = json.loads((rd / "meta.json").read_text())
    assert "IssueQueueUnavailable" in meta[LG.ITEM_LOOKUP_ERROR_FIELD]


def test_resolve_item_id_agrees_with_cost_joins_own_pr_match(tmp_path, monkeypatch):
    """The shared-rule guarantee itself: `resolve_item_id` (the ledger side)
    and `cost_join.item_for_pr` (the run-cost side, called directly) must give
    the exact same answer for the same PR and item corpus — they are, in fact,
    the same function underneath."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    items = [_AN_ITEM_LINKING_IT]
    monkeypatch.setattr(LG.issue_client, "list_all_items", lambda *a, **kw: items)

    via_ledger = LG.resolve_item_id(rd, issue=None, repo="widget")
    via_cost_join = cj.item_for_pr(
        _A_PR_LINK["pr_url"], _A_PR_LINK["pr_number"], "widget", items)

    assert via_ledger == via_cost_join == "issue_XYZ"


def test_resolve_item_id_a_same_numbered_pr_in_another_repo_does_not_attribute(
        tmp_path, monkeypatch):
    """A bare PR number names nothing without its repo (`cost_join._linked_
    items`/`_index_prs`): an item linking PR #55 in a DIFFERENT repo must not
    attribute this run's own #55 in `widget` just because the number matches."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps({"pr_url": None, "pr_number": 55}))
    other_repo_item = {"id": "issue_OTHER", "pr_url": None,
                       "pr_repo": "convivy/other-widget", "pr": 55}
    monkeypatch.setattr(LG.issue_client, "list_all_items",
                        lambda *a, **kw: [other_repo_item])
    assert LG.resolve_item_id(rd, issue=None, repo="widget") is None


# --- resolve_item_id's deadline_s: the TOTAL-time bound on the item-corpus
# fetch (`issue_client.list_all_items`'s own `deadline`), threaded through so
# a slow-but-reachable Library can't stall a bounded caller for up to
# 7 states x 100 pages x issue_client._TIMEOUT (round 1, both reviewers). ----


def test_resolve_item_id_converts_deadline_s_to_an_absolute_deadline(tmp_path, monkeypatch):
    """`deadline_s` is a seconds BUDGET; `list_all_items` wants an absolute
    `_monotonic()` deadline (`issue_client.deadline_in`). Against code that
    forwards `deadline_s` to `list_all_items` unconverted (it has no
    `deadline_s` parameter), this fails with a TypeError at the call below."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    captured = {}
    monkeypatch.setattr(LG.issue_client, "deadline_in",
                        lambda seconds: ("absolute-deadline", seconds))

    def fake_list_all_items(*, deadline=None, **kw):
        captured["deadline"] = deadline
        return [_AN_ITEM_LINKING_IT]
    monkeypatch.setattr(LG.issue_client, "list_all_items", fake_list_all_items)

    assert LG.resolve_item_id(rd, issue=None, repo="widget", deadline_s=3.0) == "issue_XYZ"
    assert captured["deadline"] == ("absolute-deadline", 3.0)


def test_resolve_item_id_with_no_deadline_s_fetches_unbounded(tmp_path, monkeypatch):
    """`deadline_s` omitted (no caller before this parameter existed) must
    reach `list_all_items` as `deadline=None` — its own unbounded default —
    rather than some other falsy stand-in a careless conversion could produce."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    captured = {}

    def fake_list_all_items(*, deadline=None, **kw):
        captured["deadline"] = deadline
        return [_AN_ITEM_LINKING_IT]
    monkeypatch.setattr(LG.issue_client, "list_all_items", fake_list_all_items)

    assert LG.resolve_item_id(rd, issue=None, repo="widget") == "issue_XYZ"
    assert captured["deadline"] is None


def test_resolve_item_id_deadline_s_ignored_when_items_is_already_given(tmp_path):
    """`items` already in hand means no fetch happens at all, so a `deadline_s`
    alongside it must be inert rather than raise or change the answer."""
    rd = _mk_run_dir(tmp_path)
    (rd / "pr-link.json").write_text(json.dumps(_A_PR_LINK))
    assert LG.resolve_item_id(
        rd, issue=None, repo="widget", items=[_AN_ITEM_LINKING_IT],
        deadline_s=0.0) == "issue_XYZ"
