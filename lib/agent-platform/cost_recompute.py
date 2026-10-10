"""Recompute stored sub-run cost.json records that were priced with the wrong rates.

`tools/subagent-telemetry-emit.py` prices a Task subagent's transcript usage through
`telemetry.cost_from_usage` and freezes the figure into `<runs>/<day>/sub-<agent_id>/
cost.json` (`cost_source: estimate_fallback`). cost.json keeps only totals, so a
record priced at the wrong rate (Opus 5.5 unpriced at $0, Sonnet 5 at $3/$15 until
agent-platform#668) cannot be corrected from itself. The usage lives in the
subagent's own transcript under `~/.claude/projects/*/<session>/subagents/`, which
is what this module re-reads and re-prices through the CURRENT table.

Only `sub-*` records with `cost_source: estimate_fallback` are candidates: an
`anthropic_cli` figure is the CLI's billed total, and a gateway or Codex figure is
metered spend, none of which the price table produced. A record that cannot be
recomputed (transcript gone, no usage, a model with tokens and no price row) is
reported and left alone; nothing here writes $0 or a guess.

`scan_sonnet_5_scale`/`run_sonnet_5_scale` cover the records `scan` cannot: a
Sonnet 5 record priced under the old $3/$15 row whose transcript is gone. Every
rate in that row is exactly 3/2 of the corrected $2/$10 one, so it scales the
stored usd by 2/3 instead of re-deriving it from usage.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import telemetry as T

BACKUP_NAME = "cost.json.pre-recompute"
_USAGE_KEYS = ("input_tokens", "output_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens")
_EPS = 1e-6

# claude-sonnet-5 priced at $3/$15 (cache $3.75/$0.30) from 66bba90b (2026-06-30)
# until 8e333eca (2026-09-24T23:17:35-07:00, i.e. 2026-09-25T06:17:35Z), which
# corrected it to $2/$10 (cache $2.50/$0.20) — the pricing page's introductory
# rate that became standard (telemetry.CLAUDE_PRICING_PER_MTOK). Every one of
# the four rates in the old row is exactly 3/2 of the corrected one, so a
# record priced under the old row is corrected by scaling its usd by 2/3,
# with no transcript needed.
SONNET_5_MODEL = "claude-sonnet-5"
SONNET_5_SCALE = 2 / 3
SONNET_5_PRICE_FIX_UTC = datetime(2026, 9, 25, 6, 17, 35, tzinfo=timezone.utc)
SONNET_5_SCALE_FROM = (
    "scaled claude-sonnet-5 usd by 2/3: priced under the 2026-06-30..2026-09-25 "
    "row ($3/$15) that should have read $2/$10; no transcript to recompute from"
)


def transcript_index(transcripts_root: Path) -> dict[str, Path]:
    """{agent_id: transcript path} for every subagent transcript under the root."""
    idx: dict[str, Path] = {}
    if transcripts_root.is_dir():
        for p in transcripts_root.glob("*/*/subagents/agent-*.jsonl"):
            idx[p.name[len("agent-"):-len(".jsonl")]] = p
    return idx


def usage_from_transcript(path: Path) -> dict[str, dict[str, int]]:
    """Sum per-model usage the way the SubagentStop hook did when it wrote the record.

    Mirrors the loop in tools/subagent-telemetry-emit.py: assistant records only,
    deduplicated on (message.id, requestId), via the shared T.UsageDedup, because
    Claude Code writes the same response into a transcript more than once."""
    usage: dict[str, dict[str, int]] = {}
    dedup = T.UsageDedup()
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        model, u = msg.get("model"), msg.get("usage")
        if not (model and isinstance(u, dict)):
            continue
        if dedup.is_duplicate(msg.get("id"), rec.get("requestId")):
            continue
        acc = usage.setdefault(model, {})
        for k in _USAGE_KEYS:
            acc[k] = acc.get(k, 0) + int(u.get(k, 0) or 0)
        # TTL breakdown (1h vs 5m cache writes), when the turn's usage carries
        # one — needed so T.cost_from_usage prices a 1-hour write at the
        # 1-hour rate instead of the 5-minute rate.
        cc = u.get("cache_creation")
        if isinstance(cc, dict):
            acc_cc = acc.setdefault("cache_creation", {})
            for k in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                acc_cc[k] = acc_cc.get(k, 0) + int(cc.get(k, 0) or 0)
    return usage


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def price_usage(usage: dict[str, dict[str, int]]) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """(by_model, "") through the current table, or (None, reason) when it cannot be priced.

    A model with tokens and no price row is refused: `cost_from_usage` would record
    it unmeasured, which is not a recomputed figure. A model with no tokens is kept
    in `cost_from_usage`'s unpriced shape without calling it, since that call
    posts an operating-plane alert."""
    if not usage:
        return None, "no assistant usage in the transcript"
    priced: dict[str, dict[str, int]] = {}
    zero_unpriced: list[str] = []
    for model, u in usage.items():
        if T.resolve_pricing(model):
            priced[model] = u
        elif any(u.get(k, 0) for k in _USAGE_KEYS):
            return None, f"no price row for {model}"
        else:
            zero_unpriced.append(model)
    by_model = T.cost_from_usage(priced) if priced else {}
    for model in zero_unpriced:
        by_model[model] = {"tokens_in": 0, "tokens_out": 0, "usd": None, "unpriced": True}
    return by_model, ""


def _differs(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when the current table prices a record differently from what is stored."""
    if abs(float(old.get("usd") or 0.0) - float(new["usd"])) > _EPS:
        return True
    old_models = old.get("by_model") if isinstance(old.get("by_model"), dict) else {}
    for model, m in new["by_model"].items():
        if m.get("usd") is None:
            continue
        o = old_models.get(model)
        if not isinstance(o, dict) or abs(float(o.get("usd") or 0.0) - m["usd"]) > _EPS:
            return True
    return False


def _iter_sub_records(runs_root: Path):
    """Yield (day, run_dir, name, old) for every sub-run cost.json under
    `runs_root`, in day/run order. `old` is `_read_json`'s result: the parsed
    dict, or None when cost.json is missing/not a dict/invalid JSON."""
    if not runs_root.is_dir():
        return
    for day_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run_dir in sorted(day_dir.glob("sub-*")):
            cost_path = run_dir / "cost.json"
            if not cost_path.is_file():
                continue
            yield day_dir.name, run_dir, f"{day_dir.name}/{run_dir.name}", _read_json(cost_path)


def scan(runs_root: Path, transcripts_root: Path) -> dict[str, Any]:
    """Classify every sub-run cost.json under `runs_root`. Read-only.

    Returns {"days": {day: {"scanned", "changed", "old_usd", "new_usd"}},
    "changes": [Change dict], "cannot": [(day/run, reason)], "already": n}."""
    index = transcript_index(transcripts_root)
    days: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    cannot: list[tuple[str, str]] = []
    already = 0
    for day, run_dir, name, old in _iter_sub_records(runs_root):
        if not isinstance(old, dict):
            cannot.append((name, "cost.json unreadable"))
            continue
        if old.get("cost_source") != "estimate_fallback":
            continue
        if "recomputed" in old:
            already += 1
            continue
        d = days.setdefault(day, {"scanned": 0, "changed": 0,
                                  "old_usd": 0.0, "new_usd": 0.0, "cannot": 0})
        d["scanned"] += 1
        meta = _read_json(run_dir / "meta.json")
        sub = _read_json(run_dir / "subagent.json")
        agent_id = sub.get("agent_id") if isinstance(sub, dict) else None
        path = index.get(agent_id) if isinstance(agent_id, str) else None
        if path is None:
            d["cannot"] += 1
            cannot.append((name, "transcript not found" if agent_id else "no agent_id in subagent.json"))
            continue
        try:
            by_model, why = price_usage(usage_from_transcript(path))
        except (OSError, ValueError) as e:
            by_model, why = None, f"transcript unreadable ({type(e).__name__})"
        if by_model is None:
            d["cannot"] += 1
            cannot.append((name, why))
            continue
        new = T.cost_payload(by_model, "estimate_fallback")
        # The transcript must still hold the usage this record priced. On the live
        # tree 189 records had a transcript that had grown past the stored token
        # counts (a subagent resumed after its SubagentStop record was written);
        # re-pricing those would change the usage as well as the rate.
        if (old.get("tokens_in"), old.get("tokens_out")) != (new["tokens_in"], new["tokens_out"]):
            d["cannot"] += 1
            cannot.append((name, "transcript usage no longer matches the stored token counts"))
            continue
        if not _differs(old, new):
            continue
        old_usd = float(old.get("usd") or 0.0)
        d["changed"] += 1
        d["old_usd"] += old_usd
        d["new_usd"] += new["usd"]
        changes.append({"name": name, "run_dir": run_dir, "old": old, "new": new,
                        "transcript": path, "parent_run": (meta or {}).get("parent_run")
                        if isinstance(meta, dict) else None})
    return {"days": days, "changes": changes, "cannot": cannot, "already": already}


def _backup_original(run_dir: Path) -> None:
    """Save cost.json as `BACKUP_NAME` beside it, unless one is already there.

    An existing backup is left as it is: it can only be the original from an
    interrupted earlier apply, and overwriting it would lose that."""
    backup = run_dir / BACKUP_NAME
    if not backup.exists():
        T.write_owner_only(backup, (run_dir / "cost.json").read_bytes())


def _write_cost_json(run_dir: Path, payload: dict[str, Any]) -> None:
    """Replace `run_dir`/cost.json atomically with `payload`, at mode 0600."""
    fd, tmp = tempfile.mkstemp(dir=run_dir, prefix=".cost.json.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, run_dir / "cost.json")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_change(change: dict[str, Any]) -> None:
    """Save the original beside it, then replace cost.json atomically."""
    run_dir: Path = change["run_dir"]
    _backup_original(run_dir)
    payload = dict(change["new"])
    payload["recomputed"] = {
        "at": T.now_iso(),
        "from": "subagent transcript usage priced by telemetry.CLAUDE_PRICING_PER_MTOK",
        "transcript": str(change["transcript"]),
        "previous_usd": change["old"].get("usd"),
        "previous_copy": BACKUP_NAME,
    }
    _write_cost_json(run_dir, payload)


def format_report(result: dict[str, Any], *, applied: bool) -> str:
    verb = "changed" if applied else "would change"
    lines = []
    tot = {"scanned": 0, "changed": 0, "old_usd": 0.0, "new_usd": 0.0, "cannot": 0}
    for day, d in sorted(result["days"].items()):
        lines.append(f"{day}  scanned {d['scanned']}  {verb} {d['changed']}  "
                     f"old ${d['old_usd']:.4f}  new ${d['new_usd']:.4f}  "
                     f"cannot recompute {d['cannot']}")
        for k in tot:
            tot[k] += d[k]
    lines.append(f"TOTAL  scanned {tot['scanned']}  {verb} {tot['changed']}  "
                 f"old ${tot['old_usd']:.4f}  new ${tot['new_usd']:.4f}  "
                 f"cannot recompute {tot['cannot']}  already recomputed {result['already']}")
    if result["cannot"]:
        lines.append("Cannot recompute (left untouched):")
        lines.extend(f"  {name}: {why}" for name, why in result["cannot"])
    return "\n".join(lines)


def run(runs_root: Path, transcripts_root: Path, *, apply: bool) -> tuple[dict[str, Any], str]:
    result = scan(runs_root, transcripts_root)
    if apply:
        for change in result["changes"]:
            apply_change(change)
    return result, format_report(result, applied=apply)


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (`Z` or explicit offset), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sonnet_5_priced_before_fix(meta: Any) -> bool | None:
    """Whether a sub-run's cost.json was priced before the Sonnet 5 rate fix.

    `ended_at` is the closer signal: `subagent-telemetry-emit.py` calls
    `write_cost` synchronously right after setting it, against whichever
    price table is live at that instant. `started_at` is the fallback when
    `ended_at` is missing. Both are timestamps taken from the subagent's own
    transcript, not file mtime, which a machine migration can reset to the
    day of the migration regardless of when the record was actually priced.
    Returns None when meta.json holds neither, so the caller can leave a
    record it cannot date alone rather than guess."""
    if not isinstance(meta, dict):
        return None
    for key in ("ended_at", "started_at"):
        dt = _parse_ts(meta.get(key))
        if dt is not None:
            return dt < SONNET_5_PRICE_FIX_UTC
    return None


def scan_sonnet_5_scale(runs_root: Path) -> dict[str, Any]:
    """Classify every sub-run cost.json priced with the old claude-sonnet-5 row.

    Unlike `scan`, this never reads a transcript: it scales the stored
    claude-sonnet-5 usd by 2/3 in place, for the records `scan` cannot touch
    because their transcript is gone. Read-only.

    Returns {"days": {day: {"scanned", "changed", "old_usd", "new_usd", "skipped"}},
    "changes": [Change dict], "skipped": [(day/run, reason)], "already": n}."""
    days: dict[str, dict[str, Any]] = {}
    changes: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    already = 0
    for day, run_dir, name, old in _iter_sub_records(runs_root):
        if not isinstance(old, dict):
            skipped.append((name, "cost.json unreadable"))
            continue
        by_model = old.get("by_model")
        if not isinstance(by_model, dict) or SONNET_5_MODEL not in by_model:
            continue
        if "recomputed" in old:
            already += 1
            continue
        d = days.setdefault(day, {"scanned": 0, "changed": 0,
                                  "old_usd": 0.0, "new_usd": 0.0, "skipped": 0})
        d["scanned"] += 1
        before_fix = sonnet_5_priced_before_fix(_read_json(run_dir / "meta.json"))
        if before_fix is False:
            continue  # already priced at the corrected rate — nothing to do
        if before_fix is None:
            d["skipped"] += 1
            skipped.append((name, "no started_at/ended_at in meta.json to date the pricing"))
            continue
        # A leftover backup does not mean this record is done: the "recomputed"
        # check above already returns for a completed record, so reaching here
        # with a backup on disk means a prior run wrote it and was interrupted
        # before the atomic replace (os.replace never landed, cost.json is
        # still the original). Scale it same as scan/apply_change would;
        # _backup_original leaves an existing backup alone, so the true
        # original is never overwritten.
        son_usd = by_model[SONNET_5_MODEL].get("usd")
        if son_usd is None:
            d["skipped"] += 1
            skipped.append((name, "claude-sonnet-5 entry has no usd to scale"))
            continue
        new_by_model = dict(by_model)
        new_by_model[SONNET_5_MODEL] = {**by_model[SONNET_5_MODEL],
                                        "usd": round(son_usd * SONNET_5_SCALE, 6)}
        new_usd = round(sum(m.get("usd") or 0.0 for m in new_by_model.values()), 4)
        old_usd = float(old.get("usd") or 0.0)
        d["changed"] += 1
        d["old_usd"] += old_usd
        d["new_usd"] += new_usd
        changes.append({"name": name, "run_dir": run_dir, "old": old,
                        "new_by_model": new_by_model, "new_usd": new_usd})
    return {"days": days, "changes": changes, "skipped": skipped, "already": already}


def apply_sonnet_5_scale(change: dict[str, Any]) -> None:
    """Save the original beside it, then replace cost.json atomically."""
    run_dir: Path = change["run_dir"]
    _backup_original(run_dir)
    payload = dict(change["old"])
    payload["by_model"] = change["new_by_model"]
    payload["usd"] = change["new_usd"]
    payload["recomputed"] = {
        "at": T.now_iso(),
        "from": SONNET_5_SCALE_FROM,
        "previous_usd": change["old"].get("usd"),
        "previous_copy": BACKUP_NAME,
    }
    _write_cost_json(run_dir, payload)


def format_sonnet_5_scale_report(result: dict[str, Any], *, applied: bool) -> str:
    verb = "scaled" if applied else "would scale"
    lines = []
    tot = {"scanned": 0, "changed": 0, "old_usd": 0.0, "new_usd": 0.0, "skipped": 0}
    for day, d in sorted(result["days"].items()):
        lines.append(f"{day}  scanned {d['scanned']}  {verb} {d['changed']}  "
                     f"old ${d['old_usd']:.4f}  new ${d['new_usd']:.4f}  "
                     f"skipped {d['skipped']}")
        for k in tot:
            tot[k] += d[k]
    lines.append(f"TOTAL  scanned {tot['scanned']}  {verb} {tot['changed']}  "
                 f"old ${tot['old_usd']:.4f}  new ${tot['new_usd']:.4f}  "
                 f"skipped {tot['skipped']}  already recomputed {result['already']}")
    if result["skipped"]:
        lines.append("Skipped (left untouched):")
        lines.extend(f"  {name}: {why}" for name, why in result["skipped"])
    return "\n".join(lines)


def run_sonnet_5_scale(runs_root: Path, *, apply: bool) -> tuple[dict[str, Any], str]:
    result = scan_sonnet_5_scale(runs_root)
    if apply:
        for change in result["changes"]:
            apply_sonnet_5_scale(change)
    return result, format_sonnet_5_scale_report(result, applied=apply)
