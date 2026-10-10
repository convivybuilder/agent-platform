#!/usr/bin/env python3
"""A dry-run stand-in for `tools/block-pr-merge.sh` — NOT the real script, and
never wired into ~/.claude/settings.json. `launch_dry_run()` in bench.py wires
this into `fixtures/stub_claude` as `BENCH_USER_HOOKS_SETTINGS`, standing in
for the ALWAYS-registered user-level hooks so the "does the bench's own
`--settings` hook run ALONGSIDE the user-level ones, not instead of them"
measurement has something to exercise without a live session.

Deliberately narrow: only the `gh pr merge` shape, segment-anchored the same
way the real hook is (`(^|[;&(])\\s*gh\\s+pr\\s+merge`), because that's the one
probe (PROBE-GHMERGE) this bench sends. The reason string below contains
"ARCH-020" on purpose — the real hook's own denial text starts with "BLOCKED
by ARCH-020 (hard hook)", so the SAME substring check
(`bench.GH_MERGE_DENIAL_MARKER`) is meaningful in both a live transcript and
this stub's simulated one, rather than the bench needing two different
markers for the same measurement in its two modes.
"""
from __future__ import annotations

import json
import re
import sys

SEG = re.compile(r"(^|[;&(])\s*gh\s+pr\s+merge", re.IGNORECASE)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    normalized = command.replace("\t", " ").replace("\n", ";")
    if SEG.search(normalized):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "STUB-USER-LEVEL: BLOCKED by ARCH-020 (hard hook): agents never merge "
                    "PRs. [dry-run stand-in for tools/block-pr-merge.sh — not the real hook]"
                ),
            },
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
