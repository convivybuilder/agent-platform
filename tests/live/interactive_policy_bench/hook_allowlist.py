#!/usr/bin/env python3
"""hook_allowlist.py — the bench's throwaway, Bosun-owned PreToolUse hook.

Registered ONLY through the bench's own `--settings` file (never
~/.claude/settings.json), for arms A and B of the slice-1 bench
(issue_01M2F52S46Y31S1QPMN4EFPD2C, comment 2698). It is arm A/B's candidate
for "a PreToolUse hook the Bosun owns" — one of the epic's two mechanisms
under trial, tried here in miniature against exactly the product-manager's
two granted `Bash(...)` commands. It is not the slice-3 mechanism and carries
no ambition to become it; it exists to give the bench something real to
launch a session against.

Wired as a `matcher: "Bash"` PreToolUse hook, so every payload on stdin is a
Bash tool call: `tool_input.command` is what ran, `session_id`/`cwd` identify
the session, and `agent_id`/`agent_type` are present "only with --agent or
inside a subagent" per the hooks reference tools/rollover-trigger.py already
quotes. This hook does not special-case any of those identity fields for its
decision — the SAME allow-list binds a subagent's call as the main thread's,
which is what lets the bench's "subagent denied" measurement come from this
one hook rather than a second one.

Two modes, both real (not simulated only for the bench's own dry run):

- `enforce` (arm A, arm B): a denied command is actually denied.
- `observe` (the rollout-mode question the epic's DoD asks slice 1 to leave
  answered: "observe mode records what it would have denied and blocks
  nothing"): the decision is computed exactly the same way and logged the
  same way, but a deny never reaches Claude Code — the command runs. Only the
  log line says `would_deny` instead of `deny`. `bench.py check-hook
  --mode observe` is what proves this mode exists and behaves this way,
  without needing a live session at all.

The log is the ground truth the bench reads back (`--log`, JSONL, one line
per invocation): timestamp, every identity field the payload carried, the
command, the decision, the reason, and how long this hook took to decide
(`duration_ms`) — the bench's "per-call latency the arm adds" measurement is
exactly this field, aggregated across a run's log lines.

Fails open on anything unparseable (missing jq-equivalent, bad JSON, missing
`--allow-file`), matching the convention `tools/block-pr-merge.sh` already
documents for the same reason: a hook that can't parse its input blocking
ALL bash is a worse failure than the one call it might have denied.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy_matcher as PM  # noqa: E402


def _read_stdin_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_allow_regexes(allow_file: str):
    try:
        prefixes = json.loads(Path(allow_file).read_text())
    except Exception:
        return None
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        return None
    return PM.build_allow_regexes(prefixes)


def _log(log_path: str, record: dict) -> None:
    try:
        with open(log_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # the log is diagnostics, never the enforcement path


def _deny_output(reason: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED by the policy bench's throwaway allow-list hook: {reason}"
            ),
        },
    })


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-file", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--mode", choices=["enforce", "observe"], default="enforce")
    args = parser.parse_args(argv)

    started = time.monotonic()
    payload = _read_stdin_payload()
    command = payload.get("tool_input", {}).get("command") if isinstance(
        payload.get("tool_input"), dict
    ) else None

    allow_regexes = _load_allow_regexes(args.allow_file)

    record = {
        "at": time.time(),
        "mode": args.mode,
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "hook_event_name": payload.get("hook_event_name"),
        "tool_name": payload.get("tool_name"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "command": command,
    }

    if allow_regexes is None:
        record["decision"] = "allow"
        record["reason"] = "fail-open: --allow-file unreadable or malformed"
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        _log(args.log, record)
        return 0

    if not isinstance(command, str) or not command:
        record["decision"] = "allow"
        record["reason"] = "fail-open: no tool_input.command in payload"
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        _log(args.log, record)
        return 0

    allowed, reason = PM.decide(command, allow_regexes)
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    record["duration_ms"] = duration_ms
    record["reason"] = reason

    if allowed:
        record["decision"] = "allow"
        _log(args.log, record)
        return 0

    if args.mode == "observe":
        record["decision"] = "would_deny"
        _log(args.log, record)
        return 0

    record["decision"] = "deny"
    _log(args.log, record)
    print(_deny_output(reason))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
