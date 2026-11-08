"""Checks that a real `codex sandbox` run enforces the profile
`run_sandbox.codex_permission_overrides` renders (epic E33 slice 4,
issue_01M5VN2R0RX3SE0AVK77Q75YB1) — the Codex counterpart to
`tests/live/test_dispatch_read_allowlist_live.py`'s Claude check.

Builds two canaries directly under the real account home (`run_sandbox.
passwd_home()`) and removes both afterward: one at a path nothing in
`read_allowlist`'s own output names, and one inside a scratch worktree
`read_allowlist` always reopens (its own unconditional `worktree` entry — no `git
init` needed for that). Renders the real profile and runs `codex sandbox -c ... -P
<profile> -- /bin/cat <canary>` against each, the same subcommand the orchestrator's
own hand probe used (2026-10-26, codex-cli 0.161.0, work-item comment
5948) to find that a more specific "read" entry wins over a broader home "deny".

Not part of the CI gate: CI has no `codex`, and this spends a real sandboxed
process. Run it on demand from the worktree, with the output shown:

    AGENTCTL_LIVE_CODEX_READ_ALLOWLIST=1 .venv/bin/python -m pytest \\
        tests/live/test_codex_dispatch_read_allowlist_live.py -s
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import run_sandbox as RS

REAL_HOME = RS.passwd_home()

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTCTL_LIVE_CODEX_READ_ALLOWLIST") != "1" or shutil.which("codex") is None,
    reason="live Codex read-allowlist check: set AGENTCTL_LIVE_CODEX_READ_ALLOWLIST=1 "
          "and have `codex` on PATH",
)

# A fresh name per run: `codex sandbox -P <name>` selects a config profile, and this
# must never collide with a profile name a real dispatch or another live run uses.
PROFILE = f"agentctl_live_read_allowlist_probe_{uuid.uuid4().hex[:8]}"


def _run_cat(overrides: list, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["codex", "sandbox", *overrides, "-P", PROFILE, "--", "/bin/cat", str(path)],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def test_codex_sandbox_denies_the_home_and_reopens_the_read_allowlist():
    marker = uuid.uuid4().hex
    probe_dir = REAL_HOME / f".agentctl-codex-probe-{uuid.uuid4().hex}"
    probe_dir.mkdir()
    try:
        # Directly under the real account home, at a path `read_allowlist` does not
        # name — must be refused.
        unnamed_canary = probe_dir / "unnamed-canary.txt"
        unnamed_canary.write_text(f"unnamed-canary-{marker}\n")

        # `read_allowlist` always reopens `worktree` itself, git checkout or not —
        # no `git init` needed for this canary to be expected readable.
        worktree = probe_dir / "wt"
        worktree.mkdir()
        reopened_canary = worktree / "reopened-canary.txt"
        reopened_canary.write_text(f"reopened-canary-{marker}\n")

        overrides = RS.codex_permission_overrides(PROFILE, REAL_HOME, worktree=worktree)

        denied = _run_cat(overrides, unnamed_canary)
        assert denied.returncode != 0, (
            f"codex sandbox read the unnamed canary {unnamed_canary}: "
            f"rc={denied.returncode} stdout={denied.stdout!r} stderr={denied.stderr!r}")
        assert marker not in denied.stdout

        allowed = _run_cat(overrides, reopened_canary)
        assert allowed.returncode == 0 and f"reopened-canary-{marker}" in allowed.stdout, (
            f"codex sandbox did not read the reopened canary {reopened_canary}: "
            f"rc={allowed.returncode} stdout={allowed.stdout!r} stderr={allowed.stderr!r}")
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
