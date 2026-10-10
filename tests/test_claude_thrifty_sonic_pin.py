"""Guard for the CLAUDE_CODE_THRIFTY_SONIC pin (bin/co, bin/agentctl).

Claude Code 2.1.263 injects a bash-first steer under `--permission-mode
bypassPermissions` (and auto mode) — a system-reminder pushing cat/sed/
heredocs over the Read/Edit/Write tools. CLAUDE_CODE_THRIFTY_SONIC is the
undocumented experiment gate behind it: 1 forces the reminder on, 0/empty
forces it off, unset leaves it to an in-session cohort assignment that flips
per session. Both launchers (`bin/co`, `bin/agentctl`) now export it as 0 on
every bypassPermissions spawn.

Because the gate is undocumented (Anthropic names these `tengu_<two random
words>`; this one is `thrifty_sonic`), any release can rename or drop it with
no changelog entry. A check that only asserts "the launchers set the env var"
would still pass after that — it would be testing our own code, not the
behaviour it's supposed to produce. So this spawns the real binary and reads
the real transcript: the attachment record `{"type": "auto_mode", "bashFirst":
true, ...}` in `~/.claude/projects/<cwd-slug>/<session>.jsonl` is what the
steer actually looks like when it fires.

The positive control (CLAUDE_CODE_THRIFTY_SONIC=1 must show the attachment) is
what makes the main assertion a real check rather than a ritual: without it, a
renamed/dropped gate and a working pin both produce "no attachment" — passing
for the wrong reason forever. Needs the `claude` binary, network, and a valid
subscription auth token; skips cleanly where the binary is absent (CI's
`convivy-linux` runner never installs it, not verified on ci1). Costs two
haiku calls.

This is the proof that the var suppresses the real steer; the CI-visible half
of the guard — that `bin/co` and `bin/agentctl` actually set the var on every
spawn — lives in tests/bash/test_co_thrifty_sonic_pin.sh and
tests/test_agentctl.py's `test_thrifty_sonic_pin_reaches_*` /
`test_every_claude_spawn_site_pins_thrifty_sonic` (reviewer, PR #505): those
run in CI and on every default `pytest` invocation; this file does not.

GATED ON AN EXPLICIT OPT-IN, not just `claude` being on PATH. A developer
machine — including the one these tests were written on — has `claude`
already installed and authenticated for everyday use, so `shutil.which
("claude") is not None` alone would make a bare `pytest` invocation launch two
live, authenticated, bypassPermissions `claude` subprocesses and spend two
haiku calls, with nothing about the command asking for that
(security-reviewer, PR #505). AGENTCTL_RUN_LIVE_CLAUDE_TESTS=1 must be set in
addition to the binary being present.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

CLAUDE_BIN = shutil.which("claude")
_LIVE_OPT_IN = os.environ.get("AGENTCTL_RUN_LIVE_CLAUDE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    CLAUDE_BIN is None or not _LIVE_OPT_IN,
    reason="claude binary not on PATH, or AGENTCTL_RUN_LIVE_CLAUDE_TESTS!=1 — "
           "this guard needs a live spawn (network + a subscription auth "
           "token) and is opt-in so a bare `pytest` on an already-"
           "authenticated dev machine doesn't launch two live sessions by "
           "accident (security-reviewer, PR #505); set "
           "AGENTCTL_RUN_LIVE_CLAUDE_TESTS=1 to run it deliberately",
)

_TRANSCRIPT_WAIT_SECONDS = 30
_PROBE_TIMEOUT_SECONDS = 60


def _sanitize_cwd(path: Path) -> str:
    """The resolved cwd -> its `~/.claude/projects/<slug>` dirname.

    `bin/transcript-capture`'s own `_sanitize` (replace '/' and '.' with '-')
    is NOT what this needs: it exists for SUBSTRING prefix-matching, so it
    only has to be conservative, not exact — and it isn't exact. Confirmed
    empirically against this machine's real `~/.claude/projects` entries: an
    underscore in the cwd (a bare pytest tmp dir is `.../pytest-1220/
    test_something0`) is ALSO sanitized to '-', which the transcript-capture
    version leaves untouched. Every non-alphanumeric byte -> '-' is what
    matched the real directory name in both the `/tmp` (-> `/private/tmp`
    symlink) and pytest-tmp-path cases tried while writing this test.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _newest_transcript(project_dir: Path, after: float) -> Path:
    """The transcript `claude -p` just wrote for this cwd.

    Polled rather than read once: the CLI's own process has exited (subprocess.run
    already waited for it) but the transcript write is not guaranteed to be
    fsynced/visible in the same instant on every filesystem.
    """
    deadline = time.time() + _TRANSCRIPT_WAIT_SECONDS
    while time.time() < deadline:
        if project_dir.is_dir():
            candidates = [
                p for p in project_dir.glob("*.jsonl") if p.stat().st_mtime >= after
            ]
            if candidates:
                return max(candidates, key=lambda p: p.stat().st_mtime)
        time.sleep(0.5)
    raise AssertionError(
        f"no transcript appeared under {project_dir} within "
        f"{_TRANSCRIPT_WAIT_SECONDS}s of the probe spawn"
    )


def _has_auto_mode_attachment(transcript: Path) -> bool:
    """Whether any line in `transcript` carries the auto_mode/bash-first
    attachment. Parsed as JSON rather than a literal substring match: the real
    writer inserts a space after the colon (`"type": "auto_mode"`), so a bare
    '"type":"auto_mode"' substring check never matches actual output."""
    for line in transcript.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        attachment = event.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "auto_mode":
            return True
    return False


def _run_probe(tmp_path: Path, *, thrifty_sonic: str) -> Path:
    # Resolved, not the raw tmp_path: on this machine /tmp -> /private/tmp, and
    # Claude Code sanitizes the REAL cwd into the ~/.claude/projects dirname —
    # confirmed empirically (a raw-path slug never matched the directory the
    # CLI actually wrote to).
    real_cwd = Path(os.path.realpath(tmp_path))
    slug = _sanitize_cwd(real_cwd)
    project_dir = Path.home() / ".claude" / "projects" / slug
    started = time.time()
    env = {**os.environ, "CLAUDE_CODE_THRIFTY_SONIC": thrifty_sonic}
    result = subprocess.run(
        [
            CLAUDE_BIN, "-p", "Reply with the single word OK.",
            "--model", "haiku",
            "--permission-mode", "bypassPermissions",
            "--output-format", "text",
        ],
        cwd=str(real_cwd), env=env, capture_output=True, text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"claude -p exited {result.returncode} (CLAUDE_CODE_THRIFTY_SONIC="
        f"{thrifty_sonic}); stderr:\n{result.stderr}"
    )
    return _newest_transcript(project_dir, started)


def test_thrifty_sonic_pin_suppresses_auto_mode_reminder(tmp_path):
    """CLAUDE_CODE_THRIFTY_SONIC=0 — what bin/co and bin/agentctl now export on
    every bypassPermissions spawn — must keep the auto_mode attachment out of
    the transcript."""
    transcript = _run_probe(tmp_path, thrifty_sonic="0")
    assert not _has_auto_mode_attachment(transcript), (
        f"auto_mode attachment present in {transcript} despite "
        f"CLAUDE_CODE_THRIFTY_SONIC=0 — the pin did not hold"
    )


def test_thrifty_sonic_positive_control_shows_attachment(tmp_path):
    """Positive control: CLAUDE_CODE_THRIFTY_SONIC=1 must force the attachment
    ON. Without this, the test above can't distinguish a working pin from a
    gate that was silently renamed or removed — both look like "no
    attachment"."""
    transcript = _run_probe(tmp_path, thrifty_sonic="1")
    assert _has_auto_mode_attachment(transcript), (
        f"auto_mode attachment absent from {transcript} with "
        f"CLAUDE_CODE_THRIFTY_SONIC=1 — the gate may have been renamed or "
        f"dropped by a Claude Code release; the suppression test above is "
        f"not meaningful until this passes again"
    )
