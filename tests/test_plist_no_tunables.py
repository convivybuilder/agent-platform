"""No launchd plist may carry a tunable runtime flag (ARCH-342).

THE BUG THIS PINS. The overnight feature flags (phase-split, digest-assembler) lived
in the live plists, set by hand with PlistBuddy and never committed. `install.sh`
copies repo templates OVER the live plists — so running the installer would have
silently reverted the sous-chef phase-split canary and the digest assembler, turning
fragment mode off and re-breaking the consolidated digest, with an exit code of 0.
Caught on 2026-07-13 by diffing live against template, one command before running it.

THE INVARIANT. A plist carries only what never changes on a running machine: the
program, its schedule, PATH, and the budget caps (which are safety limits, versioned
on purpose). Everything an operator might tune belongs in ~/.config/convivy/
overnight.env, which the launchers source. Template == live, so there is nothing left
to drift.

This test is the enforcement. Add a flag to a plist and it goes red, naming the file.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHD = REPO / "launchd"

# Tunables — an operator flips these to change behavior. They must never sit in a
# plist, because a plist is copied over the live one by install.sh.
FORBIDDEN_KEYS = {
    "OVERNIGHT_PHASE_SPLIT",
    "OVERNIGHT_PHASE_SPLIT_PRODUCTS",
    "OVERNIGHT_DIGEST_ASSEMBLER",
    "OVERNIGHT_DIGEST_ASSEMBLER_PRODUCTS",
    "OVERNIGHT_BASE_BRANCH",
}

# Deliberately allowed in a plist: PATH is launchd plumbing (ARCH-086), and the budget
# caps are safety limits that must exist by default rather than depend on a config file
# being present — an absent config must never mean "no spend cap".
ALLOWED_ENV_KEYS = {
    "PATH", "HOME", "DAILY_BUDGET_USD", "BUDGET_HARD_LIMIT_USD",
    "AGENT_RUN_ORIGIN", "OSLogRateLimit", "PYTHONPATH", "PYTHONUNBUFFERED",
    "KNOWLEDGE_ROOT", "TZ", "LANG", "LC_ALL",
    # A fixed Slack channel id — committed, identical live, and not something an
    # operator flips to change behavior. It is a destination, not a canary.
    "SSHD_WATCHDOG_CHANNEL",
}


def _plists() -> list[Path]:
    return sorted(LAUNCHD.glob("*.plist"))


def test_there_are_plists_to_check():
    assert _plists(), "no plists found — the glob is wrong and this suite is vacuous"


@pytest.mark.parametrize("path", _plists(), ids=lambda p: p.name)
def test_no_plist_carries_a_tunable_flag(path: Path):
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    env = data.get("EnvironmentVariables") or {}
    offending = FORBIDDEN_KEYS & set(env)
    assert not offending, (
        f"{path.name} carries tunable flag(s) {sorted(offending)} in "
        f"EnvironmentVariables. install.sh copies this template OVER the live plist, "
        f"so a flag here is a flag that silently reverts an operator's live setting "
        f"(ARCH-342). Put it in ~/.config/convivy/overnight.env instead — see "
        f"launchd/overnight.env.example."
    )


@pytest.mark.parametrize("path", _plists(), ids=lambda p: p.name)
def test_plist_env_keys_are_on_the_allowlist(path: Path):
    """Catch the NEXT tunable, not just the four we know about. A new env key in a
    plist is either plumbing (add it to ALLOWED_ENV_KEYS, deliberately) or a tunable
    that belongs in the config file."""
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    env = data.get("EnvironmentVariables") or {}
    unknown = set(env) - ALLOWED_ENV_KEYS
    assert not unknown, (
        f"{path.name} has env key(s) {sorted(unknown)} that are on neither the "
        f"allowlist nor the config file. If it is launchd plumbing, add it to "
        f"ALLOWED_ENV_KEYS here. If an operator would ever tune it, it belongs in "
        f"~/.config/convivy/overnight.env (ARCH-342)."
    )


def _plists_and_examples() -> list[Path]:
    """Templates too. The gateway plist is committed only as a .example — the live
    one is gitignored — so a *.plist glob does not see it at all, and the secrets
    this pins were in exactly that file."""
    return sorted(LAUNCHD.glob("*.plist")) + sorted(LAUNCHD.glob("*.plist.example"))


# Substrings that mark an env key as a credential. Matched case-insensitively against
# the key name, which is enough: the leak this pins was six keys all named *_API_KEY.
SECRET_KEY_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "MASTER_KEY")


@pytest.mark.parametrize("path", _plists_and_examples(), ids=lambda p: p.name)
def test_no_plist_carries_a_credential(path: Path):
    """No launchd plist may carry a secret, empty placeholder or not (ARCH-342).

    THE BUG THIS PINS. Six live provider API keys sat in the gateway's live plist at
    mode 0644 — world-readable, and rendered in full by `launchctl print` to anyone
    who can run it. Secrets belong in a 0600 file the launcher sources.

    The empty-placeholder case is deliberately caught too. The template shipped the
    six keys with empty <string></string> values, and that is precisely the
    invitation: the operator's job became 'fill these in', in the plist.
    """
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    env = data.get("EnvironmentVariables") or {}
    offending = sorted(
        k for k in env
        if any(marker in k.upper() for marker in SECRET_KEY_MARKERS)
    )
    assert not offending, (
        f"{path.name} carries credential-shaped env key(s) {offending}. A plist is "
        f"world-readable by default, install.sh copies it over the live one, and "
        f"`launchctl print` renders every value. Put them in a 0600 file the launcher "
        f"sources — see gateway/llm-gateway-run.sh and gateway/README.md (ARCH-342)."
    )


def test_an_agent_spawning_job_puts_local_bin_on_path():
    """`agentctl spawn` execs `claude` BY NAME (agentctl._spawn_claude), and
    claude exists only at ~/.local/bin/claude. A launchd job that spawns agents
    without ~/.local/bin on PATH therefore cannot start one — and fails in the
    shape hardest to notice: changelog-drain reported "wrote 0, failed N" per
    pass and exited non-zero, indistinguishable from agents that ran and produced
    nothing. Caught by reading the job's log, not by any alarm (ARCH-086).

    Every other agent-spawning job already carries the path; changelog-drain was
    written new and reasoned its way out of the convention. This pins the ones
    that spawn, rather than every job whose program merely lives in ~/.local/bin
    — a report job that shells nothing needs no PATH entry, and asserting it
    fleet-wide would fail 16 plists that are working correctly.
    """
    import plistlib

    spawners = ("changelog-drain", "launch-overnight",
                "launch-weekly", "overnight-")
    checked = 0
    for path in sorted(LAUNCHD.glob("*.plist")):
        data = plistlib.loads(path.read_bytes())
        prog = " ".join(str(a) for a in data.get("ProgramArguments", []))
        if not any(s in prog for s in spawners):
            continue
        env_path = (data.get("EnvironmentVariables") or {}).get("PATH", "")
        if not env_path:
            continue          # inherits launchd's default; a separate question
        checked += 1
        assert ".local/bin" in env_path, (
            f"{path.name} spawns agents but omits ~/.local/bin from PATH, so "
            f"`agentctl spawn` cannot exec claude and every run fails to start one")
    assert checked, "no agent-spawning plists matched — the spawner list has rotted"


def test_ci_result_capture_runs_every_three_minutes():
    """CI results should land within 3 minutes of landing on GitHub (operator
    ruling 2026-10-15, relayed by the steward), so `StartInterval` is 180, not
    the 900 (15 min) cadence this job started with."""
    path = LAUNCHD / "com.convivy.ci-result-capture.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["StartInterval"] == 180, (
        f"{path.name}'s StartInterval is {data['StartInterval']}, not 180 — "
        f"CI results would lag the crews' 3-minute pace")
