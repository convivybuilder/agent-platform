"""cost-recompute: re-price stored sub-run cost.json records from transcript usage.

Fixtures are run dirs and subagent transcripts under tmp_path; nothing reads the
host's ~/Convivy/agent-runs or ~/.claude.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import cost_recompute as CR
import pytest
import telemetry as T

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib" / "agent-platform"
BIN = REPO / "bin" / "cost-recompute"

DAY = "2026-09-22"
# One assistant turn: 1M new input, 100k output, 2M cache read, 500k cache write.
USAGE = {"input_tokens": 1_000_000, "output_tokens": 100_000,
         "cache_read_input_tokens": 2_000_000, "cache_creation_input_tokens": 500_000}
# Current table: Opus 5.5 = 4 / 20 / 5.00 write / 0.20 read; Sonnet 5 = 2 / 10 / 2.50 / 0.20.
OPUS_55_USD = round(1 * 4 + 0.1 * 20 + 0.5 * 5.00 + 2 * 0.20, 6)     # 8.9
SONNET_5_NEW_USD = round(1 * 2 + 0.1 * 10 + 0.5 * 2.50 + 2 * 0.20, 6)  # 4.65
SONNET_5_OLD_USD = round(1 * 3 + 0.1 * 15 + 0.5 * 3.75 + 2 * 0.30, 6)  # 6.975


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj))


def _turn(model: str, usage: dict, mid: str = "m1") -> str:
    return json.dumps({"type": "assistant", "requestId": f"r-{mid}",
                       "message": {"id": mid, "model": model, "usage": usage}})


def _transcript(root: Path, agent_id: str, lines: list[str]) -> Path:
    d = root / "proj" / "session-1" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def _record(runs: Path, agent_id: str, model: str, usd: float, *,
            source: str = "estimate_fallback", day: str = DAY, sub: bool = True) -> Path:
    rd = runs / day / (f"sub-{agent_id}" if sub else f"run-{agent_id}")
    rd.mkdir(parents=True)
    tin = USAGE["input_tokens"] + USAGE["cache_read_input_tokens"] + USAGE["cache_creation_input_tokens"]
    _write(rd / "cost.json", {
        "tokens_in": tin, "tokens_out": USAGE["output_tokens"], "usd": usd,
        "cost_source": source,
        "by_model": {model: {"tokens_in": tin, "tokens_out": USAGE["output_tokens"], "usd": usd}}})
    _write(rd / "subagent.json", {"turns": 1, "agent_id": agent_id, "source": "SubagentStop"})
    _write(rd / "meta.json", {"run_id": rd.name, "parent_run": "session-1"})
    return rd


def _record_multi(runs: Path, agent_id: str, by_model: dict, *, day: str = DAY) -> Path:
    rd = runs / day / f"sub-{agent_id}"
    rd.mkdir(parents=True)
    tokens_in = sum(m.get("tokens_in", 0) for m in by_model.values())
    tokens_out = sum(m.get("tokens_out", 0) for m in by_model.values())
    usd = round(sum(m["usd"] for m in by_model.values()), 4)
    _write(rd / "cost.json", {
        "tokens_in": tokens_in, "tokens_out": tokens_out, "usd": usd,
        "cost_source": "estimate_fallback", "by_model": by_model})
    _write(rd / "subagent.json", {"turns": 1, "agent_id": agent_id, "source": "SubagentStop"})
    _write(rd / "meta.json", {"run_id": rd.name, "parent_run": "session-1"})
    return rd


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _tree(tmp_path: Path):
    runs, tx = tmp_path / "runs", tmp_path / "claude-projects"
    runs.mkdir()
    tx.mkdir()
    return runs, tx


def _set_priced_at(run_dir: Path, ts: str | None) -> None:
    """Overwrite meta.json's started_at/ended_at, or drop both when ts is None."""
    meta = json.loads((run_dir / "meta.json").read_text())
    meta.pop("started_at", None)
    meta.pop("ended_at", None)
    if ts is not None:
        meta["started_at"] = ts
        meta["ended_at"] = ts
    _write(run_dir / "meta.json", meta)


PRE_FIX_TS = "2026-08-01T00:00:00Z"
POST_FIX_TS = "2026-09-26T00:00:00Z"


def test_opus_55_at_zero_and_sonnet_5_at_old_rates_are_recomputed(tmp_path):
    runs, tx = _tree(tmp_path)
    opus = _record(runs, "aopus", "claude-opus-5-5", 0.0)
    son = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(tx, "aopus", [_turn("claude-opus-5-5", USAGE)])
    _transcript(tx, "ason", [_turn("claude-sonnet-5", USAGE)])

    result, report = CR.run(runs, tx, apply=True)

    assert json.loads((opus / "cost.json").read_text())["usd"] == OPUS_55_USD
    assert json.loads((son / "cost.json").read_text())["usd"] == SONNET_5_NEW_USD
    assert json.loads((son / "cost.json").read_text())["by_model"]["claude-sonnet-5"]["usd"] == SONNET_5_NEW_USD
    assert result["days"][DAY]["changed"] == 2
    assert f"old ${SONNET_5_OLD_USD:.4f}" in report or "old $6.9750" in report
    assert "TOTAL" in report


def test_an_anthropic_cli_record_is_untouched(tmp_path):
    runs, tx = _tree(tmp_path)
    rd = _record(runs, "acli", "claude-opus-5-5", 0.0, source="anthropic_cli")
    _transcript(tx, "acli", [_turn("claude-opus-5-5", USAGE)])
    before = _snapshot(runs)

    result, _ = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before
    assert result["changes"] == []
    assert not (rd / CR.BACKUP_NAME).exists()


def test_a_run_dir_that_is_not_a_sub_run_is_untouched(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "agw", "claude-opus-5-5", 0.0, sub=False)
    _transcript(tx, "agw", [_turn("claude-opus-5-5", USAGE)])
    before = _snapshot(runs)

    CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before


def test_a_record_with_no_transcript_is_reported_and_untouched(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "agone", "claude-sonnet-5", SONNET_5_OLD_USD)
    before = _snapshot(runs)

    result, report = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before
    assert result["cannot"] == [(f"{DAY}/sub-agone", "transcript not found")]
    assert f"{DAY}/sub-agone: transcript not found" in report


def test_a_transcript_without_usage_is_reported_and_never_written_as_zero(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "anous", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(tx, "anous", [json.dumps({"type": "user", "message": {"content": "hi"}})])
    before = _snapshot(runs)

    result, _ = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before
    assert result["cannot"][0][1] == "no assistant usage in the transcript"


def test_a_model_with_tokens_and_no_price_row_is_reported_not_zeroed(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "aunp", "claude-mystery-9", 0.0)
    _transcript(tx, "aunp", [_turn("claude-mystery-9", USAGE)])
    before = _snapshot(runs)

    result, _ = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before
    assert result["cannot"][0][1] == "no price row for claude-mystery-9"


def test_a_transcript_that_outgrew_the_stored_tokens_is_reported_not_repriced(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "agrew", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(tx, "agrew", [_turn("claude-sonnet-5", USAGE, "m1"), _turn("claude-sonnet-5", USAGE, "m2")])
    before = _snapshot(runs)

    result, _ = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == before
    assert result["cannot"][0][1] == "transcript usage no longer matches the stored token counts"


def test_a_record_already_at_current_prices_is_not_a_change(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "aok", "claude-sonnet-5", SONNET_5_NEW_USD)
    _transcript(tx, "aok", [_turn("claude-sonnet-5", USAGE)])
    before = _snapshot(runs)

    result, _ = CR.run(runs, tx, apply=True)

    assert result["changes"] == [] and _snapshot(runs) == before


def test_duplicated_transcript_records_are_counted_once(tmp_path):
    runs, tx = _tree(tmp_path)
    rd = _record(runs, "adup", "claude-opus-5-5", 0.0)
    line = _turn("claude-opus-5-5", USAGE)
    _transcript(tx, "adup", [line, line])

    CR.run(runs, tx, apply=True)

    assert json.loads((rd / "cost.json").read_text())["usd"] == OPUS_55_USD


def test_usage_from_transcript_dedupes_a_message_written_four_times(tmp_path):
    """Claude Code writes the same assistant message's usage up to 4x in a long
    session, byte-identical. usage_from_transcript must sum it once, not 4x."""
    line = _turn("claude-opus-5-5", USAGE, mid="m1")
    p = _transcript(tmp_path, "aquad", [line, line, line, line])
    usage = CR.usage_from_transcript(p)
    assert usage == {"claude-opus-5-5": USAGE}


def test_usage_from_transcript_counts_two_distinct_messages_separately(tmp_path):
    lines = [_turn("claude-opus-5-5", USAGE, mid="m1"),
             _turn("claude-opus-5-5", USAGE, mid="m2")]
    p = _transcript(tmp_path, "atwo", lines)
    usage = CR.usage_from_transcript(p)
    assert usage["claude-opus-5-5"] == {k: v * 2 for k, v in USAGE.items()}


def test_usage_from_transcript_counts_a_keyless_record_once_not_dropped(tmp_path):
    """A record with neither message.id nor requestId is counted, never dropped
    and never collapsed with another keyless record."""
    keyless = json.dumps({"type": "assistant",
                          "message": {"model": "claude-opus-5-5", "usage": USAGE}})
    p = _transcript(tmp_path, "akeyless", [keyless])
    usage = CR.usage_from_transcript(p)
    assert usage == {"claude-opus-5-5": USAGE}


def test_usage_from_transcript_prices_a_1h_cache_write_at_the_1h_rate(tmp_path):
    """A turn's `usage.cache_creation` TTL breakdown must survive
    `usage_from_transcript`'s per-turn accumulation and reach
    `T.cost_from_usage` through it. Sonnet 5: input $2/Mtok, 1h cache write
    $4/Mtok — a turn with no input/output tokens and 1M 1h-cache-write
    tokens prices to exactly $4 only if the breakdown reached
    cost_from_usage; dropping it (falling back to the flat
    `cache_creation_input_tokens` field, which this turn leaves at 0) would
    price it at $0."""
    usage_with_cc = {"input_tokens": 0, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                     "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000,
                                        "ephemeral_5m_input_tokens": 0}}
    line = _turn("claude-sonnet-5", usage_with_cc, mid="m1")
    p = _transcript(tmp_path, "acache1h", [line])
    usage = CR.usage_from_transcript(p)
    assert usage["claude-sonnet-5"]["cache_creation"] == {
        "ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 0}
    priced = T.cost_from_usage(usage)
    assert priced["claude-sonnet-5"]["usd"] == pytest.approx(4.0)


def test_dry_run_changes_no_byte_on_disk(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "aopus", "claude-opus-5-5", 0.0)
    _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(tx, "aopus", [_turn("claude-opus-5-5", USAGE)])
    _transcript(tx, "ason", [_turn("claude-sonnet-5", USAGE)])
    before = _snapshot(tmp_path)

    result, report = CR.run(runs, tx, apply=False)

    assert _snapshot(tmp_path) == before
    assert result["days"][DAY]["changed"] == 2
    assert "would change 2" in report


def test_apply_saves_the_original_and_records_the_recompute(tmp_path):
    runs, tx = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    original = (rd / "cost.json").read_bytes()
    _transcript(tx, "ason", [_turn("claude-sonnet-5", USAGE)])

    CR.run(runs, tx, apply=True)

    assert (rd / CR.BACKUP_NAME).read_bytes() == original
    new = json.loads((rd / "cost.json").read_text())
    assert new["recomputed"]["previous_usd"] == SONNET_5_OLD_USD
    assert new["recomputed"]["at"] and new["recomputed"]["from"]
    assert oct((rd / "cost.json").stat().st_mode & 0o777) == "0o600"
    assert not [p for p in rd.iterdir() if p.name.endswith(".tmp")]


def test_apply_is_idempotent(tmp_path):
    runs, tx = _tree(tmp_path)
    _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(tx, "ason", [_turn("claude-sonnet-5", USAGE)])
    CR.run(runs, tx, apply=True)
    after_first = _snapshot(runs)

    result, report = CR.run(runs, tx, apply=True)

    assert _snapshot(runs) == after_first
    assert result["changes"] == [] and result["already"] == 1
    assert "already recomputed 1" in report


def test_a_leftover_backup_is_not_overwritten(tmp_path):
    runs, tx = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    (rd / CR.BACKUP_NAME).write_text("original from an interrupted apply")
    _transcript(tx, "ason", [_turn("claude-sonnet-5", USAGE)])

    CR.run(runs, tx, apply=True)

    assert (rd / CR.BACKUP_NAME).read_text() == "original from an interrupted apply"


def test_recompute_matches_what_the_subagent_hook_writes(tmp_path):
    """Parity: a record the hook writes from a transcript reads as unchanged, so the
    tool's parse and the hook's cannot drift apart unseen."""
    home = tmp_path / "home"
    (home / "agent-runs").mkdir(parents=True)
    tp = _transcript(home / ".claude" / "projects", "aparity",
                     [_turn("claude-sonnet-5", USAGE, "m1"), _turn("claude-sonnet-5", USAGE, "m2")])
    env = dict(os.environ, HOME=str(home), BUILDER_ROOT=str(home), PYTHONPATH=str(LIB))
    hook_in = json.dumps({"agent_id": "aparity", "agent_type": "general-purpose",
                          "agent_transcript_path": str(tp), "session_id": "session-1",
                          "cwd": str(tmp_path)})
    subprocess.run([sys.executable, str(REPO / "tools" / "subagent-telemetry-emit.py")],
                   input=hook_in, text=True, env=env, check=True, capture_output=True)
    runs = home / "agent-runs"
    written = list(runs.glob("*/sub-aparity/cost.json"))
    assert len(written) == 1, "the hook wrote no record; the parity check has nothing to compare"

    result, _ = CR.run(runs, home / ".claude" / "projects", apply=False)

    assert result["changes"] == [] and result["cannot"] == []
    assert sum(d["scanned"] for d in result["days"].values()) == 1


def test_cli_dry_run_against_a_fake_home_changes_nothing(tmp_path):
    home = tmp_path / "home"
    runs = home / "agent-runs"
    runs.mkdir(parents=True)
    _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _transcript(home / ".claude" / "projects", "ason", [_turn("claude-sonnet-5", USAGE)])
    before = _snapshot(home)
    env = dict(os.environ, HOME=str(home), BUILDER_ROOT=str(home), PYTHONPATH=str(LIB))

    proc = subprocess.run([sys.executable, str(BIN)], capture_output=True, text=True, env=env)

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "dry run, nothing changed" in proc.stdout
    assert "would change 1" in proc.stdout
    assert _snapshot(home) == before


# --- --scale-sonnet-5: no transcript, scale the old-rate usd by 2/3 -----------


def test_a_pre_fix_sonnet_5_record_scales_by_two_thirds(tmp_path):
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _set_priced_at(rd, PRE_FIX_TS)
    original = (rd / "cost.json").read_bytes()

    result, report = CR.run_sonnet_5_scale(runs, apply=True)

    new = json.loads((rd / "cost.json").read_text())
    assert new["by_model"]["claude-sonnet-5"]["usd"] == SONNET_5_NEW_USD
    assert new["usd"] == SONNET_5_NEW_USD
    assert new["recomputed"]["previous_usd"] == SONNET_5_OLD_USD
    assert new["recomputed"]["previous_copy"] == CR.BACKUP_NAME
    assert new["recomputed"]["at"] and "2/3" in new["recomputed"]["from"]
    assert (rd / CR.BACKUP_NAME).read_bytes() == original
    assert result["days"][DAY]["changed"] == 1
    assert "scaled 1" in report
    assert oct((rd / "cost.json").stat().st_mode & 0o777) == "0o600"


def test_a_post_fix_sonnet_5_record_is_untouched(tmp_path):
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_NEW_USD)
    _set_priced_at(rd, POST_FIX_TS)
    before = _snapshot(runs)

    result, _ = CR.run_sonnet_5_scale(runs, apply=True)

    assert _snapshot(runs) == before
    assert result["changes"] == []


def test_a_mixed_model_record_scales_only_its_sonnet_5_part(tmp_path):
    runs, _ = _tree(tmp_path)
    by_model = {
        "claude-sonnet-5": {"tokens_in": 1_000, "tokens_out": 100, "usd": SONNET_5_OLD_USD},
        "claude-opus-5-5": {"tokens_in": 2_000, "tokens_out": 200, "usd": OPUS_55_USD},
    }
    rd = _record_multi(runs, "amix", by_model)
    _set_priced_at(rd, PRE_FIX_TS)

    result, _ = CR.run_sonnet_5_scale(runs, apply=True)

    new = json.loads((rd / "cost.json").read_text())
    assert new["by_model"]["claude-sonnet-5"]["usd"] == SONNET_5_NEW_USD
    assert new["by_model"]["claude-opus-5-5"] == by_model["claude-opus-5-5"]
    assert new["usd"] == round(SONNET_5_NEW_USD + OPUS_55_USD, 4)
    assert result["days"][DAY]["changed"] == 1


def test_an_already_recomputed_record_is_skipped_by_scale(tmp_path):
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _set_priced_at(rd, PRE_FIX_TS)
    cost = json.loads((rd / "cost.json").read_text())
    cost["recomputed"] = {"at": "2026-07-01T00:00:00Z",
                          "from": "subagent transcript usage priced by telemetry.CLAUDE_PRICING_PER_MTOK",
                          "transcript": "/some/transcript", "previous_usd": 99.0,
                          "previous_copy": CR.BACKUP_NAME}
    _write(rd / "cost.json", cost)
    before = _snapshot(runs)

    result, _ = CR.run_sonnet_5_scale(runs, apply=True)

    assert _snapshot(runs) == before
    assert result["changes"] == [] and result["already"] == 1


def test_a_second_scale_run_changes_nothing(tmp_path):
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _set_priced_at(rd, PRE_FIX_TS)
    CR.run_sonnet_5_scale(runs, apply=True)
    after_first = _snapshot(runs)

    result, report = CR.run_sonnet_5_scale(runs, apply=True)

    assert _snapshot(runs) == after_first
    assert result["changes"] == [] and result["already"] == 1
    assert "already recomputed 1" in report


def test_a_record_with_no_started_at_or_ended_at_is_skipped_and_reported(tmp_path):
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "anodate", "claude-sonnet-5", SONNET_5_OLD_USD)
    _set_priced_at(rd, None)
    before = _snapshot(runs)

    result, report = CR.run_sonnet_5_scale(runs, apply=True)

    assert _snapshot(runs) == before
    assert result["changes"] == []
    assert result["days"][DAY]["skipped"] == 1
    assert (f"{DAY}/sub-anodate", "no started_at/ended_at in meta.json to date the pricing") \
        in result["skipped"]
    assert "no started_at/ended_at in meta.json to date the pricing" in report


def test_a_record_interrupted_between_backup_and_rewrite_is_scaled_next_run(tmp_path):
    """cost.json still equals the backup: the prior apply wrote the backup and
    was interrupted before the atomic replace, so the record was never scaled."""
    runs, _ = _tree(tmp_path)
    rd = _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    _set_priced_at(rd, PRE_FIX_TS)
    original = (rd / "cost.json").read_bytes()
    (rd / CR.BACKUP_NAME).write_bytes(original)

    result, report = CR.run_sonnet_5_scale(runs, apply=True)

    new = json.loads((rd / "cost.json").read_text())
    assert new["by_model"]["claude-sonnet-5"]["usd"] == SONNET_5_NEW_USD
    assert new["recomputed"]["previous_usd"] == SONNET_5_OLD_USD
    assert (rd / CR.BACKUP_NAME).read_bytes() == original
    assert result["days"][DAY]["changed"] == 1
    assert "scaled 1" in report


def test_scale_dry_run_writes_nothing(tmp_path):
    runs, _ = _tree(tmp_path)
    _record(runs, "ason", "claude-sonnet-5", SONNET_5_OLD_USD)
    rd = runs / DAY / "sub-ason"
    _set_priced_at(rd, PRE_FIX_TS)
    before = _snapshot(tmp_path)

    result, report = CR.run_sonnet_5_scale(runs, apply=False)

    assert _snapshot(tmp_path) == before
    assert result["days"][DAY]["changed"] == 1
    assert "would scale 1" in report
