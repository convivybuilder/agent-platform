"""No launchd plist may run a job out of the ~/.local/bin symlink farm (ADR-0059).

THE HAZARD. `~/.local/bin/<name>` is a symlink into the agent-platform working
checkout (ARCH-229), so a plist naming one runs whatever branch that checkout
happens to sit on, on a timer, unattended. ADR-0059 rule 1 calls that production
USE and requires the job read the copy `install-agent-platform-shared` builds from
main into ~/.local/deploy/agent-platform-shared instead.

WHY A STATIC TEST AND NOT ONLY THE RUNTIME CHECKER. `bin/check-deploy-pinned-jobs`
watches what is LOADED on this machine, once a day. It cannot see a committed
template regress back to `~/.local/bin/<name>` — a bad rebase, a stale branch
merged — until someone re-installs and the checker next happens to run. The two
guards catch different things: this one fails in CI on the commit that introduces
the regression, that one catches a live machine drifting from what is committed.

WHY AN EXPLICIT LIST RATHER THAN A PATTERN. Every plist that still names the
symlink farm is here by name, so the remaining migration debt is enumerated in
code rather than in a handoff document nobody reads. The list can only shrink: a
NEW plist reaching the farm fails, a migrated one whose entry is left behind fails
as stale, and migrating one means deleting its line. #469 counted this population
wrong by reading only ProgramArguments[0], so the walk below reads every entry.

WHY THE CHECKOUT ROOT TOO, NOT ONLY THE FARM STRING. `com.convivy.transcript-capture`
named `__HOME__/Convivy/agent-platform/bin/transcript-capture` directly —
the same hazard ADR-0059 rule 1 exists to close, reached by a different string.
It contains no `__HOME__/.local/bin/` substring, so it passed this
file's farm check from 2026-08-19 until this checkout-root check existed
alongside it. The two hazards are checked together below because a plist that
reaches either one is reading the same live working tree.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHD = REPO / "launchd"
SYMLINK_FARM = "__HOME__/.local/bin/"
CHECKOUT_ROOT = "__HOME__/Convivy/agent-platform/"

# The overnight family used to be listed here as inert-and-not-being-migrated.
# Its plists are GONE as of 2026-09-10 (the operator): the jobs are retired, not paused,
# and autonomous work returns event-triggered rather than cron-fired. Deleting
# the templates is what makes that stick — `install.sh` copies every plist in
# `launchd/` into ~/Library/LaunchAgents, so a disabled job came back after a
# reboot and had to be disabled again. No template, nothing to re-enable.
# `test_the_retired_overnight_fleet_stays_retired` below is the guard.
#
# Every plist that still runs out of the symlink farm, with the reason it does.
# Deleting a line here is how a migration is recorded; adding one needs a reason
# that is not "the test went red".
NOT_YET_DEPLOY_PINNED = {
    # Correct forever, not debt. The CD watcher reads the checkout to learn what
    # to deploy, and must never run FROM the area it writes — pinning it there
    # would mean a change to its own logic could never take effect.
    "com.convivy.agent-platform-autopull.plist": "the CD watcher itself; exempt by design",
}

DEPLOY_AREA = "__HOME__/.local/deploy/agent-platform-shared"


def _argv(path: Path) -> list[str]:
    """Every argv entry of a plist, however the job names its program.

    A plist may use `ProgramArguments` (an array) or the bare `Program` key (a
    single string). Reading only the first would miss the second entirely, which
    is the same shape of partial read that made #469's measurement wrong.
    """
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    argv = list(data.get("ProgramArguments") or [])
    program = data.get("Program")
    if isinstance(program, str):
        argv.append(program)
    return [a for a in argv if isinstance(a, str)]


def _farm_entries(path: Path) -> list[str]:
    return [a for a in _argv(path) if SYMLINK_FARM in a]


def _checkout_entries(path: Path) -> list[str]:
    return [a for a in _argv(path) if CHECKOUT_ROOT in a]


def _plists() -> list[Path]:
    return sorted(LAUNCHD.glob("*.plist"))


def test_there_are_plists_to_check():
    """A glob that silently matched nothing would make every test below vacuous."""
    assert len(_plists()) > 20


def test_no_unlisted_plist_runs_out_of_the_symlink_farm_or_checkout():
    """Two distinct strings reach the same working tree (see module docstring's
    WHY THE CHECKOUT ROOT TOO): the farm symlink, and the checkout path itself.
    """
    offenders = {
        p.name: _farm_entries(p) + _checkout_entries(p)
        for p in _plists()
        if (_farm_entries(p) or _checkout_entries(p)) and p.name not in NOT_YET_DEPLOY_PINNED
    }
    assert not offenders, (
        "These plists run a job out of ~/.local/bin or the agent-platform "
        "checkout directly, either of which reads the working checkout "
        "(ADR-0059 rule 1). Repoint them at "
        f"{DEPLOY_AREA}, or, if one genuinely cannot move yet, add it to "
        f"NOT_YET_DEPLOY_PINNED with the reason: {offenders}"
    )


def test_the_allowlist_holds_no_stale_entry():
    """A migrated plist must lose its line, or the list stops meaning anything."""
    stale = [
        name for name in NOT_YET_DEPLOY_PINNED
        if not (LAUNCHD / name).exists() or not _farm_entries(LAUNCHD / name)
    ]
    assert not stale, (
        "These are listed as not-yet-deploy-pinned but no longer reach the symlink "
        f"farm (or no longer exist). Delete their lines: {stale}"
    )


def test_the_jobs_this_pr_migrated_read_the_deploy_area():
    """The plists whose migration is the point of this change.

    Pinned by name rather than left to the allowlist's absence, so that reverting
    one fails with the reason rather than merely not being caught. Originally
    four: com.convivy.tech-debt-harvest.plist was deleted 2026-09-15 along with its
    producer, whose only consumer had been removed (issue_01M2K1XYDY23673TSZCRV1R9EC).
    """
    for name, script in (
        ("com.convivy.tailscale-watchdog.plist", "tailscale-watchdog"),
        ("com.convivy.check-deploy-pinned-jobs.plist", "check-deploy-pinned-jobs"),
    ):
        argv = _argv(LAUNCHD / name)
        assert f"{DEPLOY_AREA}/bin/{script}" in argv, f"{name} no longer runs the deploy copy"
        assert not _farm_entries(LAUNCHD / name), f"{name} reaches the symlink farm again"


def test_the_knowledge_watcher_reads_the_deploy_area():
    """knowledge-watcher's migration is this change's own point, not #471's four.

    Pinned by name for the same reason as test_the_jobs_this_pr_migrated_read_the_deploy_area:
    reverting this plist should fail with the reason, not merely stop being caught
    by the allowlist's absence. It was the last remaining live breach in
    NOT_YET_DEPLOY_PINNED because it needed lib/knowledge/ and watchdog/PyYAML
    shipped into the deploy venv first — that is what install-agent-platform-shared
    now does.
    """
    argv = _argv(LAUNCHD / "com.convivy.knowledge-watcher.plist")
    assert argv == [
        f"{DEPLOY_AREA}/venv/bin/python3",
        f"{DEPLOY_AREA}/bin/knowledge-watcher",
    ], "com.convivy.knowledge-watcher.plist no longer runs the deploy copy's own interpreter and script"
    assert not _farm_entries(LAUNCHD / "com.convivy.knowledge-watcher.plist"), \
        "com.convivy.knowledge-watcher.plist reaches the symlink farm again"


def test_the_last_six_jobs_read_the_deploy_area():
    """The six plists that were still loaded and firing from the farm or the
    checkout when this change was made (check-deploy-pinned-jobs has exited 2
    on this every day since 2026-09-04): run-observer, run-retention,
    check-serving-checkouts, transcript-capture, vivy-main-suite and
    launch-weekly-consulting. transcript-capture named the checkout directly
    rather than the farm — see the module docstring's WHY THE CHECKOUT ROOT TOO.

    Pinned by name for the same reason as test_the_jobs_this_pr_migrated_read_the_deploy_area
    and test_the_knowledge_watcher_reads_the_deploy_area: reverting one of these
    plists should fail with the reason, not merely stop being caught by the
    allowlist's absence.
    """
    python_jobs = {
        "com.convivy.run-observer.plist": "run-observer",
        "com.convivy.run-retention.plist": "run-retention",
        "com.convivy.serving-checkout-check.plist": "check-serving-checkouts",
        "com.convivy.transcript-capture.plist": "transcript-capture",
        "com.convivy.vivy-main-suite.plist": "vivy-main-suite",
    }
    for name, script in python_jobs.items():
        argv = _argv(LAUNCHD / name)
        assert argv == [
            f"{DEPLOY_AREA}/venv/bin/python3",
            f"{DEPLOY_AREA}/bin/{script}",
        ], f"{name} no longer runs the deploy copy's own interpreter and script"
        assert not _farm_entries(LAUNCHD / name), f"{name} reaches the symlink farm again"
        assert not _checkout_entries(LAUNCHD / name), f"{name} reaches the agent-platform checkout again"

    argv = _argv(LAUNCHD / "com.convivy.weekly-consulting.plist")
    assert argv == [f"{DEPLOY_AREA}/bin/launch-weekly-consulting"], \
        "com.convivy.weekly-consulting.plist no longer runs the deploy copy by its own path"
    assert not _farm_entries(LAUNCHD / "com.convivy.weekly-consulting.plist"), \
        "com.convivy.weekly-consulting.plist reaches the symlink farm again"
    assert not _checkout_entries(LAUNCHD / "com.convivy.weekly-consulting.plist"), \
        "com.convivy.weekly-consulting.plist reaches the agent-platform checkout again"


def test_the_tailscale_watchdog_keeps_the_system_shell_as_its_interpreter():
    """Its entry point is a shell script, so argv[0] is /bin/sh and stays there.

    The deploy area builds a python venv and nothing else; pointing this at
    `<deploy>/venv/bin/python3` while copying the pattern from a python job would
    make it fail to exec on its next firing.
    """
    argv = _argv(LAUNCHD / "com.convivy.tailscale-watchdog.plist")
    assert argv[0] == "/bin/sh"
    assert argv[1] == f"{DEPLOY_AREA}/bin/tailscale-watchdog"


def test_argv_reads_a_job_that_uses_the_bare_program_key(tmp_path):
    """`Program` is launchd's other way to name the executable, and it is a string.

    No plist in launchd/ uses it today, so this branch of `_argv` would otherwise
    be dead from the suite's point of view — the reviewer's round-2 finding. A
    plist added later with `Program` pointing into the symlink farm must fail the
    guard above rather than slip past it for naming the key differently, which is
    the same partial-read shape that made #469 miscount.
    """
    farm_job = tmp_path / "com.convivy.hypothetical.plist"
    with farm_job.open("wb") as fh:
        plistlib.dump({"Label": "com.convivy.hypothetical",
                       "Program": f"{SYMLINK_FARM}hypothetical"}, fh)
    assert _argv(farm_job) == [f"{SYMLINK_FARM}hypothetical"]
    assert _farm_entries(farm_job) == [f"{SYMLINK_FARM}hypothetical"]

    pinned_job = tmp_path / "com.convivy.pinned.plist"
    with pinned_job.open("wb") as fh:
        plistlib.dump({"Label": "com.convivy.pinned",
                       "Program": f"{DEPLOY_AREA}/bin/pinned"}, fh)
    assert _farm_entries(pinned_job) == []


def test_argv_reads_program_and_program_arguments_together(tmp_path):
    """A plist may carry both, and only `Program` may be the farm-reaching one.

    The farm path is deliberately NOT in `ProgramArguments` here. Put it in both
    and the assertion passes whether or not `_argv` reads `Program` at all — a
    test that cannot fail for the reason it names, which is the shape this whole
    change exists to correct.
    """
    job = tmp_path / "com.convivy.both.plist"
    with job.open("wb") as fh:
        plistlib.dump({"Label": "com.convivy.both",
                       "Program": f"{SYMLINK_FARM}both",
                       "ProgramArguments": [f"{DEPLOY_AREA}/bin/both", "--flag"]}, fh)
    assert _farm_entries(job) == [f"{SYMLINK_FARM}both"]


# --- the retired overnight cron fleet (the operator, 2026-09-10) ----------------------


# The seven jobs that fired the autonomous overnight on a timer. Retired, not
# paused: autonomous work returns event-triggered rather than cron-fired.
RETIRED_OVERNIGHT_PLISTS = {
    "com.convivy.overnight-orchestrator.plist",
    "com.convivy.overnight-orchestrator-hollis.plist",
    "com.convivy.overnight-preflight.plist",
    "com.convivy.overnight-healthcheck.plist",
    "com.convivy.overnight-wrapup.plist",
    "com.convivy.issue-queue-drain.plist",
    "com.convivy.weekly-curation.plist",
}

# The machinery those jobs invoked that STAYS, now that the overnight drain
# itself is gone too (the operator, 2026-09-14: "we're doing things differently now,
# events-based rather than on cron" — issue_01M2GDVPR81Z0X991Z7XD3VR0M's removal
# of overnight-orchestrator, knowledge-scribe, kb-sync-verifier and full-monty).
# `launch-overnight`, `launch-overnight-hollis`, `overnight-wrapup`,
# `overnight-preflight`, `overnight-healthcheck`, `overnight-checkout-guard`,
# `overnight-resume-watchdog`, `overnight-digest-assemble`, `overnight-ledger`,
# `overnight-manifest` and `overnight-claim` are deleted outright, not merely
# untimered — there is no more interactive `/start-overnight` path left for them
# to serve. `launch-weekly-curation` is unrelated to the overnight drain (it is
# its own scheduled dispatcher) and stays. `issue-queue-drain` was also kept
# here for the same reason at the time, but by 2026-10-09 it had lost its own
# plist and every other caller too, so it was deleted outright (work item
# issue_01M4J3CQS16N6GX513M56ZSWPH) rather than kept in this set.
KEPT_OVERNIGHT_PROGRAMS = {
    "launch-weekly-curation",
}


def test_the_retired_overnight_fleet_stays_retired():
    """Disabling was tried and did not hold, which is why the templates are gone.

    `launchctl disable` writes a flag, and a flag is something a reboot, a
    `bootstrap`, or a re-install can flip back. The operator turned this fleet off, it
    returned after a reboot, and turned it off again — so the durable fix is
    that there is no plist to enable.

    THE HALF THAT ACTUALLY BIT: `install.sh` phase 4 copies every plist in
    `launchd/` into ~/Library/LaunchAgents. Re-adding a template here puts the
    job back on the machine at the next install, whatever its disabled state was.
    That is the regression this test exists to catch, and it catches it at PR
    time rather than at the next reboot.
    """
    returned = sorted(p.name for p in _plists() if p.name in RETIRED_OVERNIGHT_PLISTS)
    assert not returned, (
        f"retired overnight cron plists are back in launchd/: {returned}. These "
        "jobs were retired on 2026-09-10 (the operator) — autonomous work returns "
        "event-triggered, not cron-fired. install.sh copies every plist here into "
        "~/Library/LaunchAgents, so re-adding one re-arms it on the machine. If a "
        "scheduled trigger is genuinely wanted again, that is the operator's call to make "
        "explicitly, not something to restore by reinstating a template."
    )


def test_the_overnight_machinery_itself_is_not_deleted():
    """`launch-weekly-curation` is a scheduled dispatcher in its own right,
    unrelated to the overnight drain that was removed alongside its cron
    timers — this pins that a future cleanup pass does not mistake it for
    overnight-drain residue and sweep it away too.
    """
    missing = sorted(n for n in KEPT_OVERNIGHT_PROGRAMS
                     if not (REPO / "bin" / n).exists())
    assert not missing, (
        f"programs deleted that were never part of the overnight drain: {missing}. "
        "These are their own scheduled dispatchers, not overnight-drain machinery."
    )
