"""PR-3 telemetry.py coverage backfill (Phase C, ARCH-102).

write_cost and VALID_BILLING_SOURCES were previously uncovered (tests/README.md
"NOT yet covered" list). Touching them in PR-3 obliges covering them now
(test-as-touched, ARCH-098/099).
"""
from __future__ import annotations

import json
import os

import pytest
import telemetry as T


def test_write_cost_default_cost_source_anthropic_cli(tmp_path):
    """Calling write_cost without cost_source writes 'anthropic_cli'."""
    T.write_cost(tmp_path, by_model={"claude-opus-4-7": {"tokens_in": 100, "tokens_out": 20, "usd": 0.5}})
    cost = json.loads((tmp_path / "cost.json").read_text())
    assert cost["cost_source"] == "anthropic_cli"
    assert cost["usd"] == 0.5


def test_write_cost_gateway_spend_log(tmp_path):
    """cost_source='gateway_spend_log' is written through to cost.json."""
    T.write_cost(
        tmp_path,
        by_model={"deepseek/deepseek-v4-flash": {"tokens_in": 500, "tokens_out": 200, "usd": 0.0023}},
        cost_source="gateway_spend_log",
    )
    cost = json.loads((tmp_path / "cost.json").read_text())
    assert cost["cost_source"] == "gateway_spend_log"
    assert cost["usd"] == pytest.approx(0.0023)


def test_write_cost_estimate_fallback(tmp_path):
    """cost_source='estimate_fallback' is written through to cost.json."""
    T.write_cost(
        tmp_path,
        by_model={"openai/gpt-5": {"tokens_in": 200, "tokens_out": 50, "usd": 0.75}},
        cost_source="estimate_fallback",
    )
    cost = json.loads((tmp_path / "cost.json").read_text())
    assert cost["cost_source"] == "estimate_fallback"


def test_write_cost_rejects_invalid_cost_source(tmp_path):
    """An unrecognised cost_source raises ValueError."""
    with pytest.raises(ValueError, match="invalid cost_source"):
        T.write_cost(
            tmp_path,
            by_model={},
            cost_source="made_up_source",
        )


def test_write_cost_top_level_totals_derived(tmp_path):
    """tokens_in, tokens_out, usd at the top level are sums over by_model."""
    T.write_cost(
        tmp_path,
        by_model={
            "model-a": {"tokens_in": 100, "tokens_out": 30, "usd": 0.1},
            "model-b": {"tokens_in": 200, "tokens_out": 50, "usd": 0.2},
        },
    )
    cost = json.loads((tmp_path / "cost.json").read_text())
    assert cost["tokens_in"] == 300
    assert cost["tokens_out"] == 80
    assert cost["usd"] == pytest.approx(0.3)


def test_write_cost_partial_writes_and_reads_back(tmp_path):
    """Round trip: write_cost_partial's payload is exactly what read_cost_partial
    returns, plus a written_at staleness timestamp write_cost doesn't carry."""
    T.write_cost_partial(
        tmp_path,
        by_model={"claude-opus-4-7": {"tokens_in": 100, "tokens_out": 20, "usd": 0.5}},
        cost_source="estimate_fallback",
    )
    partial = T.read_cost_partial(tmp_path)
    assert partial["usd"] == pytest.approx(0.5)
    assert partial["tokens_in"] == 100
    assert partial["tokens_out"] == 20
    assert partial["cost_source"] == "estimate_fallback"
    assert "written_at" in partial and partial["written_at"]


def test_write_cost_partial_rejects_invalid_cost_source(tmp_path):
    with pytest.raises(ValueError, match="invalid cost_source"):
        T.write_cost_partial(tmp_path, by_model={}, cost_source="made_up_source")


def test_write_cost_partial_requires_cost_source(tmp_path):
    """cost_source is keyword-only with no default (the operator, 2026-09-15): Deck's
    approximate/recorded marking only survives to every consumer if nothing
    can write this file without stating which kind of number it wrote."""
    with pytest.raises(TypeError):
        T.write_cost_partial(tmp_path, by_model={})


def test_write_cost_partial_is_atomic_no_tmp_file_left(tmp_path):
    """Temp-file-plus-rename: no stray .tmp file survives a write, and a
    reader never sees anything but a complete cost-partial.json or none."""
    T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 1, "tokens_out": 1, "usd": 0.01}},
                        cost_source="estimate_fallback")
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"cost-partial.json"}


def test_write_cost_partial_overwrites_in_place(tmp_path):
    """A second checkpoint replaces the first rather than merging with it —
    each write reflects the run's CURRENT accumulated total, not a delta."""
    T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 1, "tokens_out": 1, "usd": 0.01}},
                        cost_source="estimate_fallback")
    T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 5, "tokens_out": 5, "usd": 0.05}},
                        cost_source="estimate_fallback")
    partial = T.read_cost_partial(tmp_path)
    assert partial["usd"] == pytest.approx(0.05)
    assert partial["tokens_in"] == 5


def test_read_cost_partial_returns_none_when_absent(tmp_path):
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_returns_none_on_corrupt_file(tmp_path):
    (tmp_path / "cost-partial.json").write_text("{not json")
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_returns_none_when_cost_source_missing(tmp_path):
    """A figure with no cost_source can never reach a consumer marked as
    anything, so it must never reach one at all (the operator, 2026-09-15) — this is
    what makes cost_source required at write time actually load-bearing: a
    partial that predates the field, or that was hand-planted, is treated as
    unreadable rather than returned unmarked."""
    (tmp_path / "cost-partial.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "by_model": {}, "written_at": "x"}))
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_returns_none_when_cost_source_unknown(tmp_path):
    (tmp_path / "cost-partial.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "cost_source": "made_up_source",
         "by_model": {}, "written_at": "x"}))
    assert T.read_cost_partial(tmp_path) is None


@pytest.mark.parametrize("bad_cost_source", [["a", "b"], {"x": 1}, 5, None],
                         ids=["list", "dict", "int", "none"])
def test_read_cost_partial_returns_none_for_a_non_valid_cost_source_shape(tmp_path, bad_cost_source):
    """Round 6 (security-reviewer finding on agent-platform#592): the
    membership test `cost_source not in VALID_COST_SOURCES` calls `hash()` on
    the left-hand value as part of `in`, and a run-written `cost_source` of a
    list or dict is unhashable — raising `TypeError` out of this function
    instead of returning None like every other malformed shape in this
    run-controlled file. An int or explicit None already returned None before
    this fix (hashable, just not a member); tested alongside the two that
    raised so the property — any non-valid-string cost_source reads as None,
    never raises — is checked as one shape rather than pinned to the two
    inputs that happened to crash."""
    (tmp_path / "cost-partial.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "cost_source": bad_cost_source,
         "by_model": {}, "written_at": "x"}))
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_tolerates_by_model_as_a_string(tmp_path):
    """`by_model` is returned as-is, never type-checked or operated on inside
    `read_cost_partial` itself (only `cost_source` gates the read) — a run
    that wrote a malformed `by_model` alongside an otherwise-valid
    `cost_source` must not raise here either."""
    (tmp_path / "cost-partial.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "cost_source": "estimate_fallback",
         "by_model": "not-a-dict", "written_at": "x"}))
    partial = T.read_cost_partial(tmp_path)
    assert partial["usd"] == pytest.approx(1.0)
    assert partial["by_model"] == "not-a-dict"


def test_write_cost_partial_never_touches_cost_json(tmp_path):
    """A partial checkpoint must not write, or be confused with, cost.json —
    the two are independent siblings (write_cost_partial's docstring)."""
    T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 1, "tokens_out": 1, "usd": 9.99}},
                        cost_source="estimate_fallback")
    assert not (tmp_path / "cost.json").exists()
    T.write_cost(tmp_path, by_model={"m": {"tokens_in": 2, "tokens_out": 2, "usd": 0.02}})
    # cost.json's own figure is unaffected by whatever the partial recorded.
    assert T.read_cost(tmp_path)["usd"] == pytest.approx(0.02)
    # and the (now stale) partial is untouched by write_cost — a caller must
    # check cost.json's existence itself; write_cost never cleans it up.
    assert T.read_cost_partial(tmp_path)["usd"] == pytest.approx(9.99)


def test_write_cost_partial_temp_name_is_unpredictable(tmp_path):
    """Round 2 (security-reviewer finding on agent-platform#592): the pre-fix
    temp name (`.cost-partial.json.<agentctl-pid>.tmp`) was predictable —
    the writing process's own PID, recoverable from inside the run well
    within the checkpoint interval — so a symlink planted there before the
    first checkpoint got opened instead of the real temp file. Pre-planting
    a symlink at that exact legacy name must have no effect on a checkpoint:
    the real write lands at a fresh, `tempfile.mkstemp`-random name the
    planted symlink can't have guessed."""
    legacy_tmp = tmp_path / f".{T.COST_PARTIAL_FILENAME}.{os.getpid()}.tmp"
    legacy_tmp.symlink_to("/dev/null")
    T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 1, "tokens_out": 1, "usd": 0.01}},
                        cost_source="estimate_fallback")
    # the planted symlink is untouched — the write never targeted it
    assert legacy_tmp.is_symlink()
    assert os.readlink(legacy_tmp) == "/dev/null"
    partial = T.read_cost_partial(tmp_path)
    assert partial["usd"] == pytest.approx(0.01)


def test_write_cost_partial_at_0600(tmp_path):
    """write_cost_partial creates cost-partial.json at mode 0600, like every
    other owner-only run-dir file."""
    old = os.umask(0o000)
    try:
        T.write_cost_partial(tmp_path, by_model={}, cost_source="estimate_fallback")
        assert (tmp_path / "cost-partial.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_write_cost_partial_unlinks_tmp_file_on_write_failure(tmp_path, monkeypatch):
    """Reviewer suggestion, round 2 (agent-platform#592): a write failure (e.g.
    disk full) must not strand the mkstemp temp file in the run's own
    owner-only directory — unlink it before re-raising."""
    def _raise(fd, data):
        raise OSError("ENOSPC: no space left on device")
    monkeypatch.setattr(os, "write", _raise)
    with pytest.raises(OSError):
        T.write_cost_partial(tmp_path, by_model={"m": {"tokens_in": 1, "tokens_out": 1, "usd": 0.01}},
                             cost_source="estimate_fallback")
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_read_cost_partial_returns_none_promptly_on_a_fifo_with_no_writer(tmp_path):
    """Round 3 (security-reviewer finding on agent-platform#592): a FIFO
    planted at cost-partial.json's own name (not the temp path — the temp
    path was already closed in round 2), with no writer, hung a plain
    `read_text()` forever — every caller of this function sits on the
    synchronous budget-gate path checked before each spawn. `os.lstat`
    refuses it before ever opening, so this must return None immediately,
    not merely eventually; the SIGALRM is a hang detector, not the pass
    condition."""
    import signal
    (tmp_path / T.COST_PARTIAL_FILENAME).unlink(missing_ok=True)
    os.mkfifo(tmp_path / T.COST_PARTIAL_FILENAME)

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("read_cost_partial hung on a writerless FIFO")))
    signal.alarm(10)
    try:
        result = T.read_cost_partial(tmp_path)
    finally:
        signal.alarm(0)
    assert result is None


def test_read_cost_partial_returns_none_on_a_symlink(tmp_path):
    """A symlink at cost-partial.json's own name (distinct from the round-2
    temp-path symlink finding) must not be followed — os.lstat sees the
    symlink itself, not its target, so this is refused before any open. The
    target holds a genuinely valid partial (not just an unparseable file) so
    this actually distinguishes the fix from the pre-fix `read_text()`, which
    would have followed the link and returned the target's real content —
    a symlink to something unparseable would have "passed" either way."""
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"usd": 42.0, "tokens_in": 1, "tokens_out": 1,
                                  "cost_source": "estimate_fallback", "by_model": {},
                                  "written_at": "x"}))
    (tmp_path / T.COST_PARTIAL_FILENAME).symlink_to(target)
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_returns_none_on_a_directory(tmp_path):
    """A directory planted at cost-partial.json's own name is refused the
    same way — os.lstat's S_ISREG check rejects it before any open."""
    (tmp_path / T.COST_PARTIAL_FILENAME).mkdir()
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_partial_rejects_a_file_over_the_size_cap(tmp_path):
    """A genuine partial is a few hundred bytes; a run could otherwise grow
    this file without bound (it fully controls the JSON), so a read this
    function performs unconditionally before every spawn must not read an
    unbounded amount of attacker-controlled data — oversized is treated the
    same as corrupt."""
    huge = json.dumps({"usd": 1.0, "padding": "x" * (T._COST_PARTIAL_MAX_BYTES + 1)})
    (tmp_path / T.COST_PARTIAL_FILENAME).write_text(huge)
    assert T.read_cost_partial(tmp_path) is None


def test_read_cost_guarded_returns_none_when_absent(tmp_path):
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_returns_the_written_payload(tmp_path):
    """`read_cost_guarded` reads back exactly what `write_cost` wrote — the
    guarded reader added for `_agent_run_detail`'s finished-run half, alongside
    `read_cost_partial`'s existing in-flight half."""
    T.write_cost(tmp_path, by_model={"claude-opus-4-7": {"tokens_in": 100, "tokens_out": 20, "usd": 0.5}})
    cost = T.read_cost_guarded(tmp_path)
    assert cost["usd"] == pytest.approx(0.5)
    assert cost["cost_source"] == "anthropic_cli"


def test_read_cost_guarded_returns_none_on_corrupt_file(tmp_path):
    (tmp_path / "cost.json").write_text("{not json")
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_returns_none_when_cost_source_missing(tmp_path):
    """Same posture as `read_cost_partial`: a figure with no `cost_source` can
    never reach a consumer marked as anything, so it is treated as unreadable
    rather than returned unmarked — covers a `cost.json` predating the field."""
    (tmp_path / "cost.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "by_model": {}}))
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_returns_none_when_cost_source_unknown(tmp_path):
    (tmp_path / "cost.json").write_text(json.dumps(
        {"usd": 1.0, "tokens_in": 0, "tokens_out": 0, "cost_source": "made_up_source",
         "by_model": {}}))
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_returns_none_promptly_on_a_fifo_with_no_writer(tmp_path):
    """Same hardening as `read_cost_partial`'s identical test, for `cost.json`:
    a FIFO planted at this name with no writer must return None immediately,
    not merely eventually — this reader also sits on a path polled for every
    pinned session on every poll."""
    import signal
    (tmp_path / "cost.json").unlink(missing_ok=True)
    os.mkfifo(tmp_path / "cost.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("read_cost_guarded hung on a writerless FIFO")))
    signal.alarm(10)
    try:
        result = T.read_cost_guarded(tmp_path)
    finally:
        signal.alarm(0)
    assert result is None


def test_read_cost_guarded_returns_none_on_a_symlink(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"usd": 42.0, "tokens_in": 1, "tokens_out": 1,
                                  "cost_source": "anthropic_cli", "by_model": {}}))
    (tmp_path / "cost.json").symlink_to(target)
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_returns_none_on_a_directory(tmp_path):
    (tmp_path / "cost.json").mkdir()
    assert T.read_cost_guarded(tmp_path) is None


def test_read_cost_guarded_rejects_a_file_over_the_size_cap(tmp_path):
    huge = json.dumps({"usd": 1.0, "padding": "x" * (T._COST_MAX_BYTES + 1)})
    (tmp_path / "cost.json").write_text(huge)
    assert T.read_cost_guarded(tmp_path) is None


def test_write_meta_accepts_provider_api_billing_sources(tmp_path):
    """New provider_api:* billing sources pass write_meta validation."""
    required = {
        "run_id": "run-test",
        "agent": "bug-fixer",
        "task": "fix bug",
        "started_at": "2026-05-31T10:00:00Z",
        "repo": "agent-platform",
        "worktree": "/tmp/wt",
        "profile": "agent-platform/daytime",
        "model": "deepseek/deepseek-v4-flash",
    }
    for source in ("provider_api:openai", "provider_api:deepseek",
                   "provider_api:moonshot", "provider_api:openrouter",
                   "provider_api:zai"):
        T.write_meta(tmp_path, **required, billing_source=source)
        meta = json.loads((tmp_path / "meta.json").read_text())
        assert meta["billing_source"] == source, f"expected {source!r}, got {meta['billing_source']!r}"


def test_write_meta_rejects_unknown_billing_source(tmp_path):
    """An unrecognised billing_source still raises ValueError."""
    with pytest.raises(ValueError, match="invalid billing_source"):
        T.write_meta(
            tmp_path,
            run_id="run-x", agent="coder", task="task",
            started_at="2026-05-31T10:00:00Z",
            repo="constance", worktree="/tmp", profile="constance/daytime",
            model="claude-sonnet-4-6",
            billing_source="not_a_real_source",
        )


def test_valid_billing_sources_contains_gateway_providers():
    """VALID_BILLING_SOURCES includes all Phase C provider_api entries."""
    for source in ("provider_api:openai", "provider_api:deepseek",
                   "provider_api:moonshot", "provider_api:openrouter",
                   "provider_api:zai"):
        assert source in T.VALID_BILLING_SOURCES, f"{source!r} missing from VALID_BILLING_SOURCES"


_META_REQUIRED = {
    "run_id": "run-o", "agent": "coder", "task": "t",
    "started_at": "2026-05-31T10:00:00Z", "repo": "agent-platform",
    "worktree": "/tmp/wt", "profile": "agent-platform/daytime",
    "model": "claude-sonnet-4-6",
}


def test_write_meta_stamps_and_validates_origin(tmp_path):
    """origin defaults to interactive, accepts valid values, rejects junk (ARCH-156)."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["origin"] == "interactive"
    T.write_meta(tmp_path, **_META_REQUIRED, origin="scheduled")
    assert json.loads((tmp_path / "meta.json").read_text())["origin"] == "scheduled"
    with pytest.raises(ValueError, match="invalid origin"):
        T.write_meta(tmp_path, **_META_REQUIRED, origin="sneaky")


def test_write_meta_persists_review_sha(tmp_path):
    """review_sha is recorded for a pinned review run and None otherwise, so the
    authenticity gate can bind a stamp to the commit its run reviewed (ARCH-193)."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["review_sha"] is None
    T.write_meta(tmp_path, **_META_REQUIRED, review_sha="abc123")
    assert json.loads((tmp_path / "meta.json").read_text())["review_sha"] == "abc123"


def test_write_meta_persists_overnight_wave(tmp_path):
    """The ARCH-268 phase-split night-wave key is recorded when given and None
    otherwise, so cost_join can group a night's stage runs into one row."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["overnight_wave"] is None
    T.write_meta(tmp_path, **_META_REQUIRED, overnight_wave="sous-chef/2026-06-27")
    assert json.loads((tmp_path / "meta.json").read_text())["overnight_wave"] == "sous-chef/2026-06-27"


def test_write_meta_persists_label(tmp_path):
    """The work-unit label is recorded when given and None otherwise (ARCH-239),
    so the cost/session dashboards have a clean Task column and fall back to
    truncating `task` for runs dispatched before --label adoption."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["label"] is None
    T.write_meta(tmp_path, **_META_REQUIRED, label="recursion-guard")
    assert json.loads((tmp_path / "meta.json").read_text())["label"] == "recursion-guard"


def test_write_meta_stamps_and_validates_model_choice(tmp_path):
    """model_choice defaults to None (every meta.json predating the model-lane
    epic's Archives record, issue_01M174CH86JBCP7291PS0TM097), accepts each of
    the four enum values, and rejects junk the same way origin does."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["model_choice"] is None
    for choice in T.VALID_MODEL_CHOICES:
        T.write_meta(tmp_path, **_META_REQUIRED, model_choice=choice)
        assert json.loads((tmp_path / "meta.json").read_text())["model_choice"] == choice
    with pytest.raises(ValueError, match="invalid model_choice"):
        T.write_meta(tmp_path, **_META_REQUIRED, model_choice="sneaky")


def test_write_meta_persists_issue(tmp_path):
    """The work-item id (`agentctl spawn --issue`) is recorded when given and
    None otherwise — left null rather than guessed out of free-text `task`
    (model-lane epic issue_01M174CH86JBCP7291PS0TM097)."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["issue"] is None
    T.write_meta(tmp_path, **_META_REQUIRED, issue="issue_01M174CH86JBCP7291PS0TM097")
    assert (json.loads((tmp_path / "meta.json").read_text())["issue"]
            == "issue_01M174CH86JBCP7291PS0TM097")


def test_write_meta_stamps_and_validates_cost_cap_source(tmp_path):
    """cost_cap_source (issue_01M174CABDWSEP2EEVY6JXZ5VB, the operator's 2026-10-07
    ruling) defaults to None (every meta.json predating this field), accepts
    each of VALID_COST_CAP_SOURCES, and rejects junk the same way origin does."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["cost_cap_source"] is None
    for source in T.VALID_COST_CAP_SOURCES:
        T.write_meta(tmp_path, **_META_REQUIRED, cost_cap_source=source)
        assert json.loads((tmp_path / "meta.json").read_text())["cost_cap_source"] == source
    with pytest.raises(ValueError, match="invalid cost_cap_source"):
        T.write_meta(tmp_path, **_META_REQUIRED, cost_cap_source="sneaky")


def test_write_meta_stamps_and_validates_persona_source(tmp_path):
    """persona_source (agentctl `_agent_from_grant`/`resolve_agent_from_grant`)
    defaults to None (every meta.json predating this field, and a refusal
    recorded with no agent ever materialized), accepts each of
    VALID_PERSONA_SOURCES, and rejects junk the same way cost_cap_source does."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    assert json.loads((tmp_path / "meta.json").read_text())["persona_source"] is None
    for source in T.VALID_PERSONA_SOURCES:
        T.write_meta(tmp_path, **_META_REQUIRED, persona_source=source)
        assert json.loads((tmp_path / "meta.json").read_text())["persona_source"] == source
    with pytest.raises(ValueError, match="invalid persona_source"):
        T.write_meta(tmp_path, **_META_REQUIRED, persona_source="sneaky")


def test_update_meta_validates_cost_cap_source(tmp_path):
    """An ARCH-131 fail-over respawn writes cost_cap_source through update_meta
    (via `_respawn_in_place`), not write_meta — same field discipline as
    model_choice above closes the same gap here."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    T.update_meta(tmp_path, cost_cap_source="env")   # valid: passes
    assert json.loads((tmp_path / "meta.json").read_text())["cost_cap_source"] == "env"
    with pytest.raises(ValueError, match="invalid cost_cap_source"):
        T.update_meta(tmp_path, cost_cap_source="sneaky")


def test_resolve_pricing_normalizes_model_variants():
    """resolve_pricing maps the harness's id variants (1M-context suffix, dated
    snapshot) to the bare family row, and returns None for an unknown model (ARCH-239)."""
    assert T.resolve_pricing("claude-opus-4-8")["input"] == 5.0
    assert T.resolve_pricing("claude-opus-4-8[1m]")["output"] == 25.0          # context suffix
    assert T.resolve_pricing("claude-haiku-4-5-20251001")["input"] == 1.0      # dated snapshot
    assert T.resolve_pricing("claude-sonnet-4-6")["cache_read"] == 0.30
    assert T.resolve_pricing("claude-sonnet-5")["input"] == 2.0                 # ARCH-282 product-co tier; API table: $2/$10
    assert T.resolve_pricing("claude-sonnet-5[1m]")["output"] == 10.0          # the actual co launch string
    assert T.resolve_pricing("claude-sonnet-5")["cache_write"] == 2.50
    assert T.resolve_pricing("claude-sonnet-5")["cache_write_1h"] == 4.0
    assert T.resolve_pricing("claude-sonnet-5")["cache_read"] == 0.20
    assert T.resolve_pricing("claude-sonnet-5-5") == {
        "input": 2.0, "output": 10.0, "cache_write": 2.50, "cache_write_1h": 4.0, "cache_read": 0.20}
    assert T.resolve_pricing("claude-sonnet-5-5[1m]")["output"] == 10.0        # the actual co launch string
    assert T.resolve_pricing("claude-opus-5-5") == {
        "input": 4.0, "output": 20.0, "cache_write": 5.00, "cache_write_1h": 8.0, "cache_read": 0.20}
    assert T.resolve_pricing("claude-opus-5-5[1m]")["output"] == 20.0          # the co launch string
    assert T.resolve_pricing("claude-fable-5")["input"] == 10.0                # top tier, 2x Opus
    assert T.resolve_pricing("claude-fable-5")["output"] == 50.0
    fable_5_1 = {"input": 10.0, "output": 50.0, "cache_write": 12.50, "cache_write_1h": 20.0, "cache_read": 0.25}
    assert T.resolve_pricing("claude-fable-5-1") == fable_5_1                  # cache read is 0.025x input
    assert T.resolve_pricing("claude-mythos-5-1") == fable_5_1
    assert T.resolve_pricing("deepseek/deepseek-v4-flash") is None                 # not an Anthropic model


def test_every_dispatchable_anthropic_model_is_priced():
    """The gap this guards: an unpriced model records usd=0.0, and a cost guard
    reads zero as free rather than as unmeasured. Fable was unpriced for months
    for exactly that reason — nothing failed, the number was just always zero.
    Add a row here whenever a model becomes dispatchable."""
    # claude-fable-5-1 is what the `fable` alias resolves to since the
    # 2026-09-28 repoint; claude-fable-5 stays dispatchable as a retired,
    # no-longer-aliased literal (`agentctl spawn --model claude-fable-5`).
    for model in ("claude-fable-5", "claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5",
                  "claude-sonnet-5-5", "claude-sonnet-4-6", "claude-haiku-4-5"):
        row = T.resolve_pricing(model)
        assert row is not None, f"{model} is dispatchable but unpriced — it would record usd=0.0"
        assert row["input"] > 0 and row["output"] > 0, f"{model} has a zero price"


def test_every_model_alias_resolves_to_a_price_row(agentctl):
    """CQ·370's definition of done: derive the dispatchable set from
    `agentctl.MODEL_ALIASES` itself rather than a hand-maintained tuple here, so
    the next alias added without a price row fails THIS test rather than
    shipping silently (the exact gap `test_every_dispatchable_anthropic_model_is_priced`'s
    literal tuple leaves open — it tracks today's aliases, not the live dict).

    Gateway-routed values (`openai/...`, `deepseek/...`, `moonshot/...`,
    `zai/...`) price via the gateway spend log, not this table —
    `agentctl.is_gateway_model` is the real definition of that split, so use
    it rather than a hand-rolled prefix check."""
    anthropic_aliases = {
        alias: model for alias, model in agentctl.MODEL_ALIASES.items()
        if not agentctl.is_gateway_model(model)
    }
    assert anthropic_aliases, "no Anthropic-direct alias found in MODEL_ALIASES"
    for alias, model in anthropic_aliases.items():
        row = T.resolve_pricing(model)
        assert row is not None, (
            f"MODEL_ALIASES[{alias!r}] = {model!r} has no price row — "
            f"it would record usd=0.0")
        assert row["input"] > 0 and row["output"] > 0, f"{model} has a zero price"


def test_sonnet_5_5_usage_prices_to_a_number_not_none():
    """The seen bug (issue_01M42Q03QVZJW3KH8ZCFY5JHFC): claude-sonnet-5-5 had no
    price row at all, so every cost total that included a run on it read None."""
    priced = T.cost_from_usage({"claude-sonnet-5-5": {
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
    row = priced["claude-sonnet-5-5"]
    assert row["usd"] == 2.0 + 10.0
    assert "unpriced" not in row


def test_cache_creation_breakdown_prices_1h_and_5m_at_their_own_rates():
    """A usage record carrying the TTL breakdown (`usage.cache_creation`) prices
    its 1-hour tokens at the 1-hour rate and its 5-minute tokens at the 5-minute
    rate, rather than both at the single 5-minute `cache_write` rate."""
    priced = T.cost_from_usage({"claude-sonnet-5": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                            "ephemeral_5m_input_tokens": 0}}})
    row = priced["claude-sonnet-5"]
    assert row["usd"] == 4.0          # 1M @ the 1h rate ($4), not the 5m rate ($2.50)
    assert row["tokens_in"] == 1_000_000

    priced_5m = T.cost_from_usage({"claude-sonnet-5": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 0,
                            "ephemeral_5m_input_tokens": 1_000_000}}})
    assert priced_5m["claude-sonnet-5"]["usd"] == 2.50

    priced_mixed = T.cost_from_usage({"claude-sonnet-5": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 2_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                            "ephemeral_5m_input_tokens": 1_000_000}}})
    row_mixed = priced_mixed["claude-sonnet-5"]
    assert row_mixed["usd"] == 4.0 + 2.50
    assert row_mixed["tokens_in"] == 2_000_000


def test_flat_cache_creation_field_alone_still_prices_at_the_5_minute_rate():
    """No `cache_creation` breakdown present → unchanged behavior: the flat
    `cache_creation_input_tokens` figure prices at the 5-minute `cache_write` rate."""
    priced = T.cost_from_usage({"claude-sonnet-5": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000}})
    row = priced["claude-sonnet-5"]
    assert row["usd"] == 2.50
    assert row["tokens_in"] == 1_000_000


def test_mixed_model_total_including_sonnet_5_5_is_a_number():
    """A by_model total spanning several models, one of them claude-sonnet-5-5,
    sums to a real dollar figure rather than failing or carrying a None term."""
    priced = T.cost_from_usage({
        "claude-opus-4-8": {"input_tokens": 1_000_000, "output_tokens": 0,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "claude-sonnet-5-5": {"input_tokens": 1_000_000, "output_tokens": 0,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })
    assert all(v["usd"] is not None for v in priced.values())
    total = sum(v["usd"] for v in priced.values())
    assert total == 5.0 + 2.0


def test_fable_costs_more_than_opus_at_identical_usage():
    """Pins the ordering rather than the literals, so a future price edit that
    inverts the tiers fails here even if both rows still parse."""
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    fable = T.cost_from_usage({"claude-fable-5": usage})["claude-fable-5"]["usd"]
    opus = T.cost_from_usage({"claude-opus-4-8": usage})["claude-opus-4-8"]["usd"]
    assert fable == 60.0          # 1M @ $10 + 1M @ $50
    assert fable > opus


def test_usage_dedup_collapses_a_byte_identical_repeat():
    """Claude Code writes the same assistant message's usage up to 4x in a long
    session, byte-identical. UsageDedup must flag every occurrence after the
    first as a duplicate on the same (message.id, requestId) key."""
    d = T.UsageDedup()
    assert d.is_duplicate("msg_1", "req_1") is False   # first occurrence: keep
    assert d.is_duplicate("msg_1", "req_1") is True    # 2nd
    assert d.is_duplicate("msg_1", "req_1") is True    # 3rd
    assert d.is_duplicate("msg_1", "req_1") is True    # 4th


def test_usage_dedup_counts_two_distinct_messages_separately():
    d = T.UsageDedup()
    assert d.is_duplicate("msg_1", "req_1") is False
    assert d.is_duplicate("msg_2", "req_2") is False
    assert d.is_duplicate("msg_1", "req_1") is True
    assert d.is_duplicate("msg_2", "req_2") is True


def test_usage_dedup_never_collapses_keyless_records():
    """A record with neither id is counted every time, not dropped and not
    collapsed with another keyless record."""
    d = T.UsageDedup()
    assert d.is_duplicate(None, None) is False
    assert d.is_duplicate(None, None) is False
    assert d.is_duplicate(None, None) is False
    assert d.keyless == 3


def test_cost_from_usage_prices_and_preserves_tokens():
    """cost_from_usage prices usage from the frozen table; tokens_in counts new +
    cache-write + cache-read; an unknown model keeps its tokens at usd=0 (ARCH-239)."""
    priced = T.cost_from_usage({
        "claude-opus-4-8": {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                            "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0},
    })["claude-opus-4-8"]
    # 1M input @ $5 + 1M output @ $25 + 1M cache-read @ $0.50 = $30.50
    assert priced["usd"] == 30.50
    assert priced["tokens_in"] == 2_000_000   # new input + cache-read
    assert priced["tokens_out"] == 1_000_000
    # unknown model → tokens preserved (count is never lost), no dollar figure
    unknown = T.cost_from_usage({"deepseek/deepseek-v4-flash": {"input_tokens": 500, "output_tokens": 200}})
    assert unknown["deepseek/deepseek-v4-flash"]["usd"] is None
    assert unknown["deepseek/deepseek-v4-flash"]["unpriced"] is True
    assert unknown["deepseek/deepseek-v4-flash"]["tokens_in"] == 500


def test_opus_5_5_usage_is_priced_not_free():
    """The seen bug: 1M in + 100k out on claude-opus-5-5[1m] returned usd 0.0."""
    priced = T.cost_from_usage({"claude-opus-5-5[1m]": {
        "input_tokens": 1_000_000, "output_tokens": 100_000,
        "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}})
    row = priced["claude-opus-5-5[1m]"]
    assert row["usd"] == 4.0 + 2.0 + 0.20 + 5.00     # in, 100k out, cache read, cache write
    assert "unpriced" not in row


def test_an_unpriced_model_is_never_summed_as_zero(tmp_path):
    """write_cost and write_cost_partial total dollars from by_model. An unpriced
    entry (usd None) must not raise, must not add a figure, and must be named in
    `unpriced_models` so a reader can tell 'measured $0' from 'not measured'."""
    by_model = T.cost_from_usage({
        "claude-opus-4-8": {"input_tokens": 1_000_000},
        "mystery/model-x": {"input_tokens": 1_000_000}})
    for writer in (T.write_cost, lambda d, by_model: T.write_cost_partial(
            d, by_model=by_model, cost_source="estimate_fallback")):
        d = tmp_path / str(id(writer))
        d.mkdir()
        payload = writer(d, by_model=by_model) if writer is T.write_cost else writer(d, by_model)
        assert payload["usd"] == 5.0                        # the priced floor only
        assert payload["unpriced_models"] == ["mystery/model-x"]
        assert payload["by_model"]["mystery/model-x"]["usd"] is None
    # a fully priced run carries no marker
    clean = T.write_cost(tmp_path, by_model=T.cost_from_usage({"claude-opus-4-8": {"input_tokens": 1}}))
    assert "unpriced_models" not in clean


def test_update_meta_freezes_review_sha(tmp_path):
    """review_sha is write-once (ARCH-193): once a review run is pinned, update_meta
    must not repoint it at a newer head (that would re-open stale-replay). An
    unrelated update and an idempotent re-set are fine; a change raises."""
    T.write_meta(tmp_path, **_META_REQUIRED, review_sha="a" * 40)
    T.update_meta(tmp_path, ended_at="2026-06-18T11:00:00Z")  # unrelated update is fine
    T.update_meta(tmp_path, review_sha="a" * 40)              # idempotent re-set is fine
    assert json.loads((tmp_path / "meta.json").read_text())["review_sha"] == "a" * 40
    with pytest.raises(ValueError, match="review_sha is immutable"):
        T.update_meta(tmp_path, review_sha="f" * 40)


def test_update_meta_freezes_origin(tmp_path):
    """update_meta must not CHANGE a run's origin — not to junk, and (the real
    threat the security review flagged) not to the other VALID origin. A
    scheduled→interactive flip would slip the run's spend out of the capped
    pool, so it must raise even though "interactive" is itself valid (PR #15)."""
    T.write_meta(tmp_path, **_META_REQUIRED, origin="scheduled")
    T.update_meta(tmp_path, ended_at="2026-05-31T11:00:00Z")  # unrelated update is fine
    T.update_meta(tmp_path, origin="scheduled")               # idempotent re-set is fine
    assert json.loads((tmp_path / "meta.json").read_text())["origin"] == "scheduled"
    with pytest.raises(ValueError, match="origin is immutable"):
        T.update_meta(tmp_path, origin="interactive")          # valid-but-wrong → blocked
    with pytest.raises(ValueError, match="origin is immutable"):
        T.update_meta(tmp_path, origin="not_an_origin")        # junk → also blocked
    # origin on disk is unchanged after the rejected calls
    assert json.loads((tmp_path / "meta.json").read_text())["origin"] == "scheduled"


def test_update_meta_validates_model_choice(tmp_path):
    """write_meta validates model_choice against VALID_MODEL_CHOICES, but the
    ARCH-131 fail-over writes model_choice="harness_fallback" through
    update_meta (via _respawn_in_place), not write_meta — extending the same
    field discipline here closes that gap now that the field is load-bearing
    for the model-lane epic's Archives record (reviewer suggestion, PR #477
    round 2)."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    T.update_meta(tmp_path, model_choice="harness_fallback")   # valid: passes
    assert json.loads((tmp_path / "meta.json").read_text())["model_choice"] == "harness_fallback"
    T.update_meta(tmp_path, model_choice=None)                 # None: passes
    assert json.loads((tmp_path / "meta.json").read_text())["model_choice"] is None
    with pytest.raises(ValueError, match="invalid model_choice"):
        T.update_meta(tmp_path, model_choice="sneaky")
    # rejected call left the on-disk value unchanged
    assert json.loads((tmp_path / "meta.json").read_text())["model_choice"] is None


def test_create_run_dir_append_files_at_0600(tmp_path, monkeypatch):
    """create_run_dir initializes transcript.jsonl and tool_calls.jsonl at mode 0600."""
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("test-run-0001", date="2026-07-28")
    for name in ("transcript.jsonl", "tool_calls.jsonl"):
        mode = (run_dir / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name}: expected 0600, got {oct(mode)}"


def test_create_run_dir_append_files_are_empty_and_writable(tmp_path, monkeypatch):
    """Append files start empty and remain writable after 0600 init."""
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("test-run-0002", date="2026-07-28")
    assert (run_dir / "transcript.jsonl").stat().st_size == 0
    assert (run_dir / "tool_calls.jsonl").stat().st_size == 0
    # append_transcript and append_tool_call must still succeed on 0600 files
    T.append_transcript(run_dir, {"type": "test", "value": 1})
    T.append_tool_call(run_dir, tool_name="Read", input={"path": "/x"}, result_summary="ok", duration_ms=1)
    assert (run_dir / "transcript.jsonl").stat().st_size > 0
    assert (run_dir / "tool_calls.jsonl").stat().st_size > 0


def test_write_outcome_file_mode_new(tmp_path):
    """write_outcome creates outcome.json at mode 0600."""
    T.write_outcome(tmp_path, exit_status="completed")
    mode = (tmp_path / "outcome.json").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_write_outcome_file_mode_rewrite_from_644(tmp_path):
    """write_outcome corrects an existing outcome.json that was at 644."""
    path = tmp_path / "outcome.json"
    path.write_text("{}")
    path.chmod(0o644)
    T.write_outcome(tmp_path, exit_status="completed")
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600 after rewrite, got {oct(mode)}"


def test_write_outcome_api_error_fields_recorded(tmp_path):
    """api_error_message, api_error_status, terminal_reason appear in outcome.json
    when passed, and the file is still written at mode 0600 (both PRs #278 and
    the 0600 fix must survive the rebase)."""
    T.write_outcome(
        tmp_path,
        exit_status="errored",
        terminal_reason="api_error",
        api_error_status=403,
        api_error_message="Authentication method not allowed",
    )
    outcome = json.loads((tmp_path / "outcome.json").read_text())
    assert outcome["terminal_reason"] == "api_error"
    assert outcome["api_error_status"] == 403
    assert outcome["api_error_message"] == "Authentication method not allowed"
    # mode check: both changes must coexist
    mode = (tmp_path / "outcome.json").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_write_outcome_api_error_fields_absent_for_normal_run(tmp_path):
    """Fields absent from the call must not appear in outcome.json — optional keys
    are conditional insertions, not nulled-out defaults."""
    T.write_outcome(tmp_path, exit_status="completed")
    outcome = json.loads((tmp_path / "outcome.json").read_text())
    assert "terminal_reason" not in outcome
    assert "api_error_status" not in outcome
    assert "api_error_message" not in outcome
    assert "error_reason" not in outcome


def test_ensure_runs_root_pins_lax_existing_tree(tmp_path, monkeypatch):
    """_ensure_runs_root sets 0700 on a pre-existing tree that was created wider."""
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    tmp_path.chmod(0o755)
    T._ensure_runs_root()
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_create_run_dir_modes_under_permissive_umask(tmp_path, monkeypatch):
    """run dir and prompts/ are 0700; RUNS_ROOT is pinned to 0700 — all independent of umask.

    Regression for the umask-masked mkdir trap: Path.mkdir() with no explicit
    mode defaults to 0o777; under a permissive umask (no bits stripped) that
    yields 0o777. os.chmod sets the final mode unconditionally, bypassing umask.
    """
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    tmp_path.chmod(0o755)  # start wider than owner-only
    old_umask = os.umask(0o000)  # permissive: no bits stripped
    try:
        d = T.create_run_dir("run-abc", date="2026-07-28")
    finally:
        os.umask(old_umask)
    assert tmp_path.stat().st_mode & 0o777 == 0o700, "RUNS_ROOT not pinned to 0700"
    assert d.stat().st_mode & 0o777 == 0o700, "run dir not 0700"
    assert (d / "prompts").stat().st_mode & 0o777 == 0o700, "prompts/ not 0700"


def test_validate_run_dir_flags_invalid_origin(tmp_path):
    """validate_run_dir surfaces a bad origin at audit time, mirroring the
    billing_source check (PR #15). Asserted on the errors list so the test
    needs no full run-dir scaffold."""
    T.write_meta(tmp_path, **_META_REQUIRED, origin="scheduled")
    # corrupt the origin on disk (bypassing update_meta's guard) to a bad value
    meta = json.loads((tmp_path / "meta.json").read_text())
    meta["origin"] = "bogus"
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    errs = T.validate_run_dir(tmp_path)
    assert any("invalid origin: bogus" in e for e in errs)


# --- 0600 file-mode tests (write_owner_only / open_owner_only) ----------------
# All run under os.umask(0o000) so a naive write_text/open would produce 0o666,
# proving that fchmod — not umask — is what sets the final mode.


def test_write_owner_only_truncate_creates_at_0600(tmp_path):
    """write_owner_only creates a new file at 0600 regardless of umask."""
    old = os.umask(0o000)
    try:
        path = tmp_path / "test.txt"
        T.write_owner_only(path, "hello")
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text() == "hello"
    finally:
        os.umask(old)


def test_write_owner_only_truncate_corrects_existing_mode(tmp_path):
    """write_owner_only fixes an existing 0644 file to 0600 on rewrite."""
    path = tmp_path / "test.txt"
    path.write_text("old")
    path.chmod(0o644)
    old = os.umask(0o000)
    try:
        T.write_owner_only(path, "new")
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text() == "new"
    finally:
        os.umask(old)


def test_write_owner_only_append_extends_without_truncating(tmp_path):
    """append=True does not truncate and preserves 0600."""
    old = os.umask(0o000)
    try:
        path = tmp_path / "log.txt"
        T.write_owner_only(path, "line1\n")
        T.write_owner_only(path, "line2\n", append=True)
        assert path.read_text() == "line1\nline2\n"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_open_owner_only_truncate_creates_at_0600(tmp_path):
    """open_owner_only returns a writable file object; the file lands at 0600."""
    old = os.umask(0o000)
    try:
        path = tmp_path / "stderr.log"
        with T.open_owner_only(path) as f:
            f.write(b"err output")
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_bytes() == b"err output"
    finally:
        os.umask(old)


def test_open_owner_only_append_extends_without_truncating(tmp_path):
    """open_owner_only with append=True extends the file at 0600."""
    old = os.umask(0o000)
    try:
        path = tmp_path / "stderr.log"
        with T.open_owner_only(path) as f:
            f.write(b"first\n")
        with T.open_owner_only(path, append=True) as f:
            f.write(b"second\n")
        assert path.read_bytes() == b"first\nsecond\n"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_write_meta_at_0600(tmp_path):
    """write_meta creates meta.json at mode 0600."""
    old = os.umask(0o000)
    try:
        T.write_meta(tmp_path, **_META_REQUIRED)
        assert (tmp_path / "meta.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_update_meta_rewrites_at_0600(tmp_path):
    """update_meta rewrites meta.json at 0600 even when the prior mode was 0644."""
    T.write_meta(tmp_path, **_META_REQUIRED)
    (tmp_path / "meta.json").chmod(0o644)
    old = os.umask(0o000)
    try:
        T.update_meta(tmp_path, ended_at="2026-07-29T12:00:00Z")
        assert (tmp_path / "meta.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_write_prompt_at_0600(tmp_path):
    """write_prompt creates system.md and user.md at mode 0600."""
    (tmp_path / "prompts").mkdir()
    old = os.umask(0o000)
    try:
        T.write_prompt(tmp_path, system="sys prompt", user="user prompt")
        for name in ("prompts/system.md", "prompts/user.md"):
            mode = (tmp_path / name).stat().st_mode & 0o777
            assert mode == 0o600, f"{name}: expected 0600, got {oct(mode)}"
    finally:
        os.umask(old)


def test_write_docs_consulted_writes_both_activation_keys(tmp_path):
    """Decision 0088: `individuated_at_activation` is written beside the
    pre-rename `materialized_at_activation` (Deck reads the old one), same value."""
    T.write_docs_consulted(
        tmp_path, profile="agent-platform/daytime",
        individuated_agents=["b", "a"], individuated_skills=["s"],
        queried_at_runtime=[],
    )
    payload = json.loads((tmp_path / "docs_consulted.json").read_text())
    want = {"agents": ["a", "b"], "skills": ["s"]}
    assert payload["materialized_at_activation"] == want
    assert payload["individuated_at_activation"] == want


def test_write_docs_consulted_writes_the_materialized_document_set(tmp_path):
    """`materialized_documents` (work item issue_01M3BFGE9QE8339CK5RNVP5XQC) is
    written verbatim under `docs`, the full set a run was given — distinct
    from `individuated_at_activation`'s agent/skill NAMES only, which this
    does not replace."""
    docs = [
        {"id": "agent-platform/agents/coder", "kind": "agent", "body_sha256": "a" * 64},
        {"id": "agent-platform/skills/run", "kind": "skill", "body_sha256": "b" * 64},
        {"id": "platform/decisions/fleet-rule", "kind": "instruction-block",
         "body_sha256": "c" * 64, "scope": "fleet"},
    ]
    T.write_docs_consulted(
        tmp_path, profile="agent-platform/daytime",
        individuated_agents=["coder"], individuated_skills=["run"],
        queried_at_runtime=[], materialized_documents=docs,
    )
    payload = json.loads((tmp_path / "docs_consulted.json").read_text())
    assert payload["docs"] == docs


def test_write_docs_consulted_defaults_the_document_set_to_empty(tmp_path):
    """A caller that passes no `materialized_documents` (the budget-refused
    call site, which has no worktree to read one from) gets `docs: []`
    rather than a missing key — a reader must not need to distinguish
    "no documents" from "this run predates the field"."""
    T.write_docs_consulted(
        tmp_path, profile="agent-platform/daytime",
        individuated_agents=[], individuated_skills=[], queried_at_runtime=[],
    )
    payload = json.loads((tmp_path / "docs_consulted.json").read_text())
    assert payload["docs"] == []


def test_write_docs_consulted_at_0600(tmp_path):
    """write_docs_consulted creates docs_consulted.json at mode 0600."""
    old = os.umask(0o000)
    try:
        T.write_docs_consulted(
            tmp_path, profile="agent-platform/daytime",
            individuated_agents=[], individuated_skills=[], queried_at_runtime=[],
        )
        assert (tmp_path / "docs_consulted.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_write_cost_at_0600(tmp_path):
    """write_cost creates cost.json at mode 0600."""
    old = os.umask(0o000)
    try:
        T.write_cost(tmp_path, by_model={})
        assert (tmp_path / "cost.json").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_write_owner_only_rejects_symlink(tmp_path):
    """O_NOFOLLOW: write_owner_only raises OSError when the path is a symlink."""
    target = tmp_path / "real.txt"
    target.write_text("original")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError):
        T.write_owner_only(link, "attacker-content")
    # The real file behind the symlink must be untouched.
    assert target.read_text() == "original"


def test_open_owner_only_rejects_symlink(tmp_path):
    """O_NOFOLLOW: open_owner_only raises OSError when the path is a symlink."""
    target = tmp_path / "real.log"
    target.write_bytes(b"original")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        T.open_owner_only(link)


# --- the unpriced-model warning ------------------------------------------------

_UNPRICED = {"claude-nova-9": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture
def alerts(tmp_path, monkeypatch):
    """Run the alert inline, with post_alert stubbed and the claim dir in tmp."""
    import signal_client as SC
    posts: list[tuple] = []
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(T, "_ALERT_RUNNER", lambda fn: fn())
    monkeypatch.setattr(SC, "post_alert",
                        lambda stream, body, **kw: posts.append((stream, body, kw)) or "created")
    return posts


def test_an_unpriced_model_posts_one_alert_naming_the_slug_and_the_table(alerts):
    out = T.cost_from_usage(_UNPRICED, where="run-abc")
    assert out["claude-nova-9"]["unpriced"] is True     # the cost is still written
    assert len(alerts) == 1
    body = alerts[0][1]
    assert "claude-nova-9" in body
    assert "CLAUDE_PRICING_PER_MTOK in lib/agent-platform/telemetry.py" in body
    assert "run-abc" in body


def test_the_alert_post_uses_a_short_timeout(alerts):
    # The SubagentStop hook waits on this thread at exit; signal_client's 30s
    # default across 3 attempts would outlive the hook's own timeout.
    T.cost_from_usage(_UNPRICED)
    assert alerts[0][2].get("timeout", 30) <= 5


def test_a_priced_model_posts_nothing(alerts):
    T.cost_from_usage({"claude-opus-5-5": {"input_tokens": 10}})
    assert alerts == []


def test_the_same_model_twice_in_a_day_posts_once(alerts, capsys):
    T.cost_from_usage(_UNPRICED)
    T.cost_from_usage(_UNPRICED)
    assert len(alerts) == 1
    # the stderr line is printed at every detection, deduplicated or not
    assert capsys.readouterr().err.count("claude-nova-9 has no price row") == 2


def test_another_model_or_the_next_day_posts_again(alerts, monkeypatch):
    T.cost_from_usage(_UNPRICED)
    T.cost_from_usage({"claude-nova-10": {"input_tokens": 1}})
    assert len(alerts) == 2
    import datetime as dt

    class _Tomorrow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2099, 1, 1, tzinfo=tz)
    monkeypatch.setattr(T, "datetime", _Tomorrow)
    T.cost_from_usage(_UNPRICED)
    assert len(alerts) == 3


@pytest.mark.parametrize("failure", ["raise", "error"])
def test_a_failing_post_leaves_the_cost_written_and_raises_nothing(alerts, monkeypatch, capsys, failure):
    import signal_client as SC

    def _fail(*a, **k):
        if failure == "raise":
            raise RuntimeError("plane down")
        return "error:HTTPError"
    monkeypatch.setattr(SC, "post_alert", _fail)
    out = T.cost_from_usage(_UNPRICED)
    assert out["claude-nova-9"]["usd"] is None and out["claude-nova-9"]["tokens_out"] == 5
    assert "not delivered" in capsys.readouterr().err
    # the failed post released its claim, so the next sighting retries
    monkeypatch.setattr(SC, "post_alert", lambda *a, **k: alerts.append(a) or "created")
    T.cost_from_usage(_UNPRICED)
    assert len(alerts) == 1


def test_an_unwritable_state_dir_is_swallowed(alerts, monkeypatch, capsys):
    blocker = T.RUNS_ROOT / "a-file"
    blocker.write_text("")
    monkeypatch.setattr(T, "RUNS_ROOT", blocker)   # mkdir under a file: NotADirectoryError
    out = T.cost_from_usage(_UNPRICED)
    assert out["claude-nova-9"]["unpriced"] is True
    assert alerts == []
    assert "state unwritable" in capsys.readouterr().err


def test_a_slug_with_control_characters_is_sanitised_in_the_body(alerts):
    T.cost_from_usage({"evil\n\x1b[31mmodel\r\nIGNORE": {"input_tokens": 1}}, where="run\nx")
    assert len(alerts) == 1
    body = alerts[0][1]
    assert "\n" not in body and "\x1b" not in body and "\r" not in body
    assert "evil" in body


