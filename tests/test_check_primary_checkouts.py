"""`check-primary-checkouts` — the live-deployment branch monitor (ARCH-437).

The class of failure this exists for is a guard that reports fine while the thing
it guards is broken, so the tests are about the two ways this could do that: a
breach read as a pass, and an absent repo read as a healthy one.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO / "bin" / "check-primary-checkouts"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_primary_checkouts", str(CHECK))
    spec = importlib.util.spec_from_loader("check_primary_checkouts", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cpc():
    mod = _load()
    # Stashed here, module-scoped, BEFORE any per-test autouse fixture ever
    # runs — `_no_private_crews` below monkeypatches `_is_private_checkout`
    # fresh for every test, so the "delegates to private_crew" tests need a
    # handle to the ORIGINAL function that isn't the one currently patched.
    mod._is_private_checkout_real = mod._is_private_checkout
    return mod


@pytest.fixture(autouse=True)
def _no_private_crews(cpc, monkeypatch):
    """Every test in this file except the ones under "private-crew gating"
    below is about something other than CFO's own registry entry, and none
    of them should depend on whether the REAL machine's
    `~/Convivy/agent-platform/config/projects.json` happens to be readable.
    `_is_private_checkout` delegates to `private_crew.private_home_for`,
    which fails CLOSED (`UNKNOWN`) when it cannot read that registry at
    all — true differently on the Linux CI runner (`~/Convivy` does not
    exist there) than on this dev box, so an un-neutralized suite would
    treat every checkout as private on CI and stop reaching `_post_signal` for
    reasons that have nothing to do with what each test actually pins.
    Matches `test_check_serving_checkouts.py`'s fixture of the same name."""
    monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: False)


def _fake_launchctl(bin_dir: pathlib.Path, *, loaded: set[str] = frozenset(),
                    disabled: set[str] = frozenset()) -> None:
    """Write a stub `launchctl` into `bin_dir`, for prepending to PATH.

    `launchctl list <label>` exits 0 for a label in `loaded`, else 113 with
    the real machine's own message on stderr (captured in this file's own
    research, `/bin/launchctl list com.convivy.nonexistent-thing`). Bare
    `launchctl list` (no label — `_legacy_label_loaded`'s CQ-428 fallback
    scan) prints a PID\\tStatus\\tLabel header then one line per loaded
    label, matching the real command's shape. `launchctl print-disabled
    gui/<uid>` prints every `loaded` label as `"<label>" => enabled` or
    `=> disabled`, matching this machine's own `/bin/launchctl print-disabled
    "gui/$(id -u)"` output shape exactly — the stub is shaped off a real
    capture, not a guess at the format.
    """
    lines = ["#!/bin/bash", "set -e", 'case "$1" in', "  list)"]
    lines.append('    if [ -z "$2" ]; then')
    lines.append('      printf "PID\\tStatus\\tLabel\\n"')
    for label in sorted(loaded):
        lines.append(f'      printf "123\\t0\\t{label}\\n"')
    lines.append("      exit 0")
    lines.append("    fi")
    for label in sorted(loaded):
        lines.append(f'    [ "$2" = "{label}" ] && exit 0')
    lines += [
        '    echo "Could not find service \\"$2\\" in domain for port" >&2',
        "    exit 113",
        "    ;;",
        "  print-disabled)",
        '    echo "disabled services = {"',
    ]
    for label in sorted(loaded):
        state = "disabled" if label in disabled else "enabled"
        lines.append(f'    echo \'    "{label}" => {state}\'')
    lines += ["    echo '}'", "    exit 0", "    ;;", "  *) exit 1 ;;", "esac"]
    script = bin_dir / "launchctl"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)


@pytest.fixture(autouse=True)
def _launchctl_all_healthy(cpc, tmp_path_factory, monkeypatch):
    """Every test in this file gets a `launchctl` reporting every job named in
    `_LIVE_CHECKOUTS` as loaded and enabled, unless a test below overrides
    PATH itself. Without this, `check_jobs` shells out to whatever
    `launchctl` is actually on PATH: nothing at all on the Linux pytest
    runner (ARCH-099's CI leg), or this dev box's own REAL, unrelated job
    state — either way making tests like
    `test_main_is_silent_and_exits_zero_when_clean` depend on facts about the
    host rather than the fixture each test builds.
    """
    all_jobs = {label for cfg in cpc._LIVE_CHECKOUTS.values()
               for label in cfg.get("jobs", ())}
    bin_dir = tmp_path_factory.mktemp("fake-bin")
    _fake_launchctl(bin_dir, loaded=all_jobs)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _repo(root: pathlib.Path, name: str, *, branch: str = "main",
          detach: bool = False) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(d), *a],  # noqa: E731
                                    capture_output=True, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    run("commit", "-q", "--allow-empty", "-m", "seed")
    if detach:
        run("checkout", "-q", "--detach", "HEAD")
    elif branch != "main":
        run("checkout", "-q", "-b", branch)
    return d


def test_a_checkout_on_main_passes(cpc, tmp_path):
    _repo(tmp_path, "agent-platform")
    ok, reason = cpc.check("agent-platform", cpc._LIVE_CHECKOUTS["agent-platform"], tmp_path)
    assert ok and reason == "on main"


def test_a_feature_branch_is_a_breach(cpc, tmp_path):
    """The 2026-07-22 and 2026-08-02 incidents. The symlinked bin/ and lib/ serve
    this branch's code to every scheduled job on the box."""
    _repo(tmp_path, "agent-platform", branch="feat/session-view-deck-open-anchors")
    ok, reason = cpc.check("agent-platform", cpc._LIVE_CHECKOUTS["agent-platform"], tmp_path)
    assert not ok
    assert "feat/session-view-deck-open-anchors" in reason


def test_a_detached_head_is_a_breach_and_says_so(cpc, tmp_path):
    """A detached HEAD points the deployment at an arbitrary commit — the same harm
    as a named branch, while reading in `git status` like something transient. It
    is named separately so the alert does not send someone hunting for a branch."""
    _repo(tmp_path, "knowledge", detach=True)
    ok, reason = cpc.check("knowledge", cpc._LIVE_CHECKOUTS["knowledge"], tmp_path)
    assert not ok and reason == "detached HEAD"


def test_a_missing_checkout_is_not_a_breach(cpc, tmp_path):
    """A repo that is not on this machine must read as absent. Policing a checkout
    that does not exist would file a bug every fifteen minutes forever."""
    ok, reason = cpc.check("selector", cpc._LIVE_CHECKOUTS["selector"], tmp_path)
    assert ok and "not found" in reason


def test_a_directory_that_is_not_a_repo_is_not_a_breach(cpc, tmp_path):
    """The presence test is `.git`, not the directory. A stray ~/Convivy/<name>
    folder must not read as a checkout gone wrong."""
    (tmp_path / "knowledge").mkdir()
    ok, reason = cpc.check("knowledge", cpc._LIVE_CHECKOUTS["knowledge"], tmp_path)
    assert ok and "not found" in reason


def test_main_reports_a_breach_and_exits_two(cpc, tmp_path, capsys):
    _repo(tmp_path, "agent-platform", branch="feat/x")
    _repo(tmp_path, "knowledge")
    rc = cpc.main(["--convivy-root", str(tmp_path), "--no-report", "-v"])
    assert rc == 2
    out = capsys.readouterr()
    assert "feat/x" in out.err
    assert "[OK] knowledge: on main" in out.out


def test_main_is_silent_and_exits_zero_when_clean(cpc, tmp_path, capsys):
    """A pass prints nothing without --verbose. A monitor that speaks on every run
    trains its reader to ignore it."""
    for name in cpc._LIVE_CHECKOUTS:
        _repo(tmp_path, name)
    rc = cpc.main(["--convivy-root", str(tmp_path), "--no-report"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- the completeness audit ------------------------------------------------


def _plist(dir_: pathlib.Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text(
        f'<?xml version="1.0"?><plist version="1.0"><dict>{body}</dict></plist>',
        encoding="utf-8")


def test_the_audit_finds_a_referenced_repo_nobody_classified(cpc, tmp_path, capsys):
    """The failure one level up from a branch check: a live-deployed repo that is
    not in the list is not reported healthy, it is not reported at all.

    PR #347's reviewer found `convivy-services` missing; sweeping the plists found
    three more, so the gap was not a one-off. A declared list nobody re-derives
    will have another.
    """
    _repo(tmp_path, "agent-platform")
    _repo(tmp_path, "brand-new-service")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/brand-new-service</string>")
    rc = cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                   "--no-report"])
    assert rc == 2
    assert "brand-new-service" in capsys.readouterr().err


def test_a_repo_declared_not_live_is_not_a_finding(cpc, tmp_path, monkeypatch, capsys):
    """`_NOT_LIVE` is how "its branch does not matter" gets SAID rather than
    defaulted to. Without it the audit would be unsilenceable and would train its
    reader to ignore it."""
    _repo(tmp_path, "agent-platform")
    _repo(tmp_path, "archived-thing")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/archived-thing</string>")
    monkeypatch.setitem(cpc._NOT_LIVE, "archived-thing", "frozen; the job only reads data")
    rc = cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                   "--no-report"])
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_the_audit_ignores_a_path_that_is_not_a_primary_checkout(cpc, tmp_path, capsys):
    """A plist naming a worktree or a plain directory must not become a finding.
    Only a `~/Convivy/<name>` that is itself a repo counts."""
    _repo(tmp_path, "agent-platform")
    (tmp_path / "not-a-repo").mkdir()
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/not-a-repo</string>")
    rc = cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                   "--no-report"])
    assert rc == 0


def test_a_missing_launch_agents_dir_is_not_a_finding(cpc, tmp_path):
    """A machine with no LaunchAgents (a CI runner) must audit nothing rather than
    report everything unclassified."""
    assert cpc.launchd_referenced_repos(tmp_path / "nope", tmp_path) == set()


def test_every_declared_repo_carries_its_deployment_reason(cpc):
    """The alert quotes `reads` to tell whoever is paged WHAT is affected, and
    `adr` to point at why the rule exists. An entry missing either files an alert
    that states a rule and not a consequence.

    Two citation shapes are both accepted: `ARCH-NNN` for anything decided before
    the ARCH log froze at ARCH-441 (2026-08-03, platform/decisions/0018), and
    `platform/decisions/NNNN` for anything decided after — the seam ADR-0018
    describes as the cost of freezing rather than migrating."""
    for repo, cfg in cpc._LIVE_CHECKOUTS.items():
        assert cfg.get("reads"), f"{repo} has no deployment reason"
        adr = cfg.get("adr", "")
        assert adr.startswith("ARCH-") or adr.startswith("platform/decisions/"), \
            f"{repo} has no ADR reference"


def test_library_and_cfo_are_classified(cpc):
    """check-serving-checkouts' own launchd audit (agent-platform#423/#424) found
    these two read by a launchd job and classified in NEITHER `_LIVE_CHECKOUTS`
    nor `_NOT_LIVE` — exactly the gap this file's completeness audit exists to
    surface. Pinned by name and directory casing: CFO's checkout is capital-`CFO`
    on disk while its registry slug is lower-case `cfo` (`config/projects.json`),
    and a regression back to the lower-case form would silently stop the branch
    check from ever finding the real checkout."""
    assert "library" in cpc._LIVE_CHECKOUTS
    assert "CFO" in cpc._LIVE_CHECKOUTS
    assert "cfo" not in cpc._LIVE_CHECKOUTS
    for repo in ("library", "CFO"):
        cfg = cpc._LIVE_CHECKOUTS[repo]
        assert cfg.get("reads"), f"{repo} has no deployment reason"
        assert cfg.get("adr"), f"{repo} has no ADR reference"


_COUNT_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight",
                "nine", "ten", "eleven", "twelve")


def test_no_deployment_reason_states_a_job_count(cpc):
    """A count in a paging alert drifts the moment a job is added or retired.

    The first version of the `selector` entry said "nine launchd jobs" and the
    real number was ten. Being wrong about the number is worse than omitting it,
    because a precise-looking figure invites the reader to trust the rest of the
    sentence (reviewer, #347). Describe the mechanism instead.
    """
    for repo, cfg in cpc._LIVE_CHECKOUTS.items():
        words = re.findall(r"[a-z]+|\d+", cfg["reads"].lower())
        for i, w in enumerate(words[:-1]):
            if w in _COUNT_WORDS or w.isdigit():
                assert not words[i + 1].startswith(("job", "launchd", "service",
                                                    "plist", "process")), (
                    f"{repo}'s reads text counts jobs: '{w} {words[i + 1]}'")


def test_the_dedup_key_does_not_include_the_branch(cpc, tmp_path, monkeypatch):
    """One alert per repo per open incident. Keying on the branch would file a
    fresh bug every time someone switched between feature branches, turning the
    signal into the noise it exists to cut through.

    Asserted through main() with reporting stubbed, so it pins the call rather
    than a constant a refactor could route around.
    """
    seen = []
    monkeypatch.setattr(cpc, "_utc_today", lambda: "2030-01-01")
    _repo(tmp_path, "agent-platform", branch="feat/one")
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: seen.append(k.get("dedup_key")) or {"status": "stub"}
    try:
        cpc.main(["--convivy-root", str(tmp_path), "--no-report"])
    finally:
        cpc._post_signal = original
    assert seen == ["primary-checkout:agent-platform:2030-01-01"]


# --- job liveness: a job that is not loaded, or is disabled, can never show up
# as a branch breach — the checkout can sit cleanly on `main` while the thing
# that reads it never fires at all. `com.convivy.appkit.autoupdate` sat
# disabled for 26 days and nothing above this line would ever have caught it
# (module docstring's "A SECOND, INDEPENDENT BREACH").


def test_job_status_passes_when_loaded_and_enabled(cpc, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.example.ok"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    assert disabled.ok
    ok, reason = cpc.job_status("com.example.ok", disabled)
    assert ok and reason == "loaded and enabled"


def test_job_status_is_a_breach_when_not_loaded(cpc, tmp_path, monkeypatch):
    """The plain "never bootstrapped" case: `launchctl list` has no record of
    the label at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded=set())
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    ok, reason = cpc.job_status("com.example.missing", disabled)
    assert not ok
    assert "not loaded" in reason


def test_job_status_is_a_breach_when_disabled(cpc, tmp_path, monkeypatch):
    """The 26-day incident itself: loaded (or at least once bootstrapped) and
    separately marked disabled — `_launchctl_list_one` alone cannot see this,
    only `launchctl print-disabled` can."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.convivy.appkit.autoupdate"},
                    disabled={"com.convivy.appkit.autoupdate"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    ok, reason = cpc.job_status("com.convivy.appkit.autoupdate", disabled)
    assert not ok
    assert "disabled" in reason


def test_job_status_is_a_breach_when_the_disabled_query_itself_failed(
        cpc, tmp_path, monkeypatch):
    """The finding this round exists for: a `print-disabled` that could not be
    trusted must read as a breach for a loaded job, never as a pass — a
    loaded-but-unknown job reading healthy is the exact blind spot
    `_launchctl_disabled_labels` used to reopen by returning an empty set on
    failure."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.example.ok"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc.DisabledQuery(False, frozenset(), "print-disabled exited 112: Bad request.")
    ok, reason = cpc.job_status("com.example.ok", disabled)
    assert not ok
    assert "unknown" in reason
    assert "Bad request" in reason


def test_check_jobs_reports_only_the_unhealthy_label(cpc, tmp_path, monkeypatch):
    """`convivy-app-kit` declares two jobs; one disabled and one healthy must
    report exactly the disabled one, not both and not neither."""
    _repo(tmp_path, "convivy-app-kit")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir,
                    loaded={"com.convivy.rails.update", "com.convivy.appkit.autoupdate"},
                    disabled={"com.convivy.appkit.autoupdate"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    breaches = cpc.check_jobs("convivy-app-kit", cpc._LIVE_CHECKOUTS["convivy-app-kit"],
                              tmp_path, disabled)
    assert breaches == [("com.convivy.appkit.autoupdate", "disabled (launchctl print-disabled)")]


def test_check_jobs_reports_every_label_as_unknown_when_the_disabled_query_failed(
        cpc, tmp_path, monkeypatch):
    """When this sweep's one `print-disabled` call failed, every declared job
    of a repo whose checkout exists and is loaded must still come back as a
    breach — "unknown" propagated through, not silently dropped into "healthy"
    because `check_jobs` only has a failed query to work with."""
    _repo(tmp_path, "convivy-app-kit")
    disabled = cpc.DisabledQuery(False, frozenset(), "print-disabled timed out after 10s")
    monkeypatch.setattr(cpc, "_launchctl_list_one", lambda label: (True, ""))
    breaches = cpc.check_jobs("convivy-app-kit", cpc._LIVE_CHECKOUTS["convivy-app-kit"],
                              tmp_path, disabled)
    labels = {label for label, _reason in breaches}
    assert labels == {"com.convivy.rails.update", "com.convivy.appkit.autoupdate"}
    assert all("unknown" in reason for _label, reason in breaches)


def test_check_jobs_skips_a_missing_checkout(cpc, tmp_path):
    """Same degrade as the branch check: a repo not cloned on this machine is
    not this machine's problem to page about."""
    disabled = cpc.DisabledQuery(True, frozenset(), "")
    assert cpc.check_jobs("knowledge", cpc._LIVE_CHECKOUTS["knowledge"], tmp_path,
                          disabled) == []


# --- `_launchctl_disabled_labels` itself: the query this whole round is about.
# Each failure shape must produce `DisabledQuery(ok=False, ...)`, never a
# `DisabledQuery(True, frozenset(), ...)` that `job_status` would read as
# "nothing is disabled" — that silent downgrade is exactly how
# `com.convivy.appkit.autoupdate` stayed disabled for 26 days unnoticed even
# after this file started asking.


def test_disabled_labels_parses_a_real_capture(cpc, tmp_path, monkeypatch):
    """Shaped off this machine's own `launchctl print-disabled "gui/$(id -u)"`
    output (this round's research), confirming the header check does not
    reject real output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        "  print-disabled)\n"
        '    printf \'\\tdisabled services = {\\n\'\n'
        '    printf \'\\t\\t"com.docker.helper" => enabled\\n\'\n'
        '    printf \'\\t\\t"com.convivy.appkit.autoupdate" => disabled\\n\'\n'
        '    printf \'\\t}\\n\'\n'
        "    exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    assert disabled.ok
    assert disabled.labels == frozenset({"com.convivy.appkit.autoupdate"})


def test_disabled_labels_parses_a_real_empty_capture(cpc, tmp_path, monkeypatch):
    """Zero disabled services still opens with the header (this round's
    research) — this must parse as `ok=True, labels=frozenset()`, the
    legitimate "nothing disabled" case, not as unparseable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        "  print-disabled)\n"
        '    printf \'\\tdisabled services = {\\n\'\n'
        '    printf \'\\t}\\n\'\n'
        "    exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    assert disabled.ok
    assert disabled.labels == frozenset()


def test_disabled_labels_fails_closed_on_nonzero_exit(cpc, tmp_path, monkeypatch):
    """Shaped off this machine's own `launchctl print-disabled gui/999999`
    (this round's research: "Bad request." on stderr, exit 112) — a bad uid or
    any other rejection must come back `ok=False`, never an empty-but-ok set."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        "  print-disabled)\n"
        '    echo "Bad request." >&2\n'
        '    echo "Could not find domain for user gui: $2" >&2\n'
        "    exit 112 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=999999)
    assert not disabled.ok
    assert disabled.labels == frozenset()
    assert "112" in disabled.reason
    assert "Bad request" in disabled.reason


def test_disabled_labels_fails_closed_on_timeout(cpc, monkeypatch):
    """A hung `print-disabled` must come back `ok=False`, not raise past the
    caller and not silently read as "nothing disabled". Mocked at
    `subprocess.run` rather than an actual `sleep`, matching this repo's own
    convention for a timeout test (`test_changelog_drain.py`)."""
    def _hang(*a, **k):
        raise cpc.subprocess.TimeoutExpired(cmd="launchctl", timeout=10)

    monkeypatch.setattr(cpc.subprocess, "run", _hang)
    disabled = cpc._launchctl_disabled_labels(uid=501)
    assert not disabled.ok
    assert disabled.labels == frozenset()
    assert "timed out" in disabled.reason


def test_disabled_labels_fails_closed_on_unparseable_output(cpc, tmp_path, monkeypatch):
    """Exit 0 with output that does not contain the header every real reply
    has (this round's research) must still fail closed — a format change or a
    truncated capture must not silently parse as an empty, healthy list."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        "  print-disabled)\n"
        '    echo "unrecognized output from some future launchctl"\n'
        "    exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    disabled = cpc._launchctl_disabled_labels(uid=501)
    assert not disabled.ok
    assert disabled.labels == frozenset()
    assert "not recognized" in disabled.reason


def test_main_files_a_disabled_job_breach_on_its_own_dedup_key(cpc, tmp_path, monkeypatch):
    monkeypatch.setattr(cpc, "_utc_today", lambda: "2030-01-01")
    """The checkout is cleanly on `main` — the branch check passes — and the
    job that reads it is disabled anyway. `main()` must still report it, keyed
    separately from the (passing) branch check."""
    _repo(tmp_path, "knowledge")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.convivy.knowledge-watcher"},
                    disabled={"com.convivy.knowledge-watcher"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    seen = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: seen.append((a[0], k.get("dedup_key"))) or {"status": "stub"}
    try:
        rc = cpc.main(["--convivy-root", str(tmp_path), "--uid", "501"])
    finally:
        cpc._post_signal = original

    assert rc == 2
    assert seen == [("knowledge",
                     "primary-checkout:job:knowledge:com.convivy.knowledge-watcher:2030-01-01")]


def test_job_status_falls_back_to_the_pre_migration_label(cpc, tmp_path, monkeypatch):
    """CQ-428 renamed knowledge-watcher's label to com.convivy.knowledge-watcher
    in `launchd/`, but a machine that has not yet run `install.sh
    --migrate-labels` still has it loaded under its OLD, pre-rename label.
    Without the fallback in `job_status`, this sweep would fire a false "not
    loaded" breach for every run between merge and migration. The old label
    named a person, so this test proves the fallback with an arbitrary
    stand-in label rather than the real one (`_legacy_label_loaded` finds it
    by suffix, never by hardcoding it)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.example-legacy.knowledge-watcher"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    disabled = cpc._launchctl_disabled_labels(uid=501)
    ok, reason = cpc.job_status("com.convivy.knowledge-watcher", disabled)
    assert ok, f"expected the old-label fallback to resolve as healthy, got: {reason}"
    assert reason == "loaded and enabled"


def test_legacy_label_loaded_ignores_a_non_owned_prefix(cpc, tmp_path, monkeypatch):
    """Scoped to com.convivy.* labels only — a bare last-dot-segment match
    would false-match an unrelated job for a label this platform never
    owned, such as a different crew's `com.selector-backend.api`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.example-legacy.api"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert cpc._legacy_label_loaded("com.selector-backend.api") is None


def test_legacy_label_loaded_handles_a_multi_dot_bare_name(cpc, tmp_path, monkeypatch):
    """com.convivy.appkit.autoupdate's bare name is `appkit.autoupdate` (two
    segments) — this pins that the match is against the full bare name
    after stripping the owned prefix, not merely the last dot segment."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.example-legacy.appkit.autoupdate",
                                    "com.example-legacy.unrelated.autoupdate"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert cpc._legacy_label_loaded("com.convivy.appkit.autoupdate") == \
        "com.example-legacy.appkit.autoupdate"


def test_job_status_reports_not_loaded_when_neither_label_is_up(cpc, tmp_path, monkeypatch):
    """Once the alias is deleted (migration complete, or the job genuinely
    is down), the new label alone drives the breach — this pins that the
    fallback never masks a REAL not-loaded breach."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded=set())
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    disabled = cpc._launchctl_disabled_labels(uid=501)
    ok, reason = cpc.job_status("com.convivy.knowledge-watcher", disabled)
    assert not ok
    assert "not loaded" in reason


def test_a_private_checkouts_job_breach_is_not_filed(cpc, tmp_path, monkeypatch, capsys):
    """Same gate as a branch breach (ADR-0043): CFO's job being disabled is
    still counted and still printed locally, and must never reach `_post_signal`."""
    _repo(tmp_path, "CFO")
    monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_launchctl(bin_dir, loaded={"com.convivy.cfo-review"},
                    disabled={"com.convivy.cfo-review"})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    bug_calls = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = cpc.main(["--convivy-root", str(tmp_path), "--uid", "501"])
    finally:
        cpc._post_signal = original

    assert bug_calls == []
    assert rc == 2
    err = capsys.readouterr().err
    assert "CFO job com.convivy.cfo-review" in err and "not logged" in err


# --- private-crew gating: a black-box crew's breach is counted, printed
# locally, and never reaches the shared issue queue (platform/decisions/0043) --
#
# CFO is a black-box crew. `bin/check-serving-checkouts` already had this exact
# gate added after a security-reviewer finding (PR #423, run-0d72a1e3): a
# finding's own EXISTENCE in the shared, multi-reader issue queue is a leak
# independent of how little it says — filing "CFO is on branch `x`" is exactly
# what ADR-0043 forbids, even though this check never reads a line of CFO's own
# code. These tests port the sibling file's coverage for the same gate here.


def test_a_private_checkouts_breach_is_not_filed(cpc, tmp_path, monkeypatch, capsys):
    _repo(tmp_path, "CFO", branch="feat/x")
    monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: True)

    bug_calls = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = cpc.main(["--convivy-root", str(tmp_path), "--no-report"])
    finally:
        cpc._post_signal = original

    assert bug_calls == [], "a private crew's breach must never reach _post_signal"
    assert rc == 2, "the breach still counts — a real problem on a private checkout is still real"
    err = capsys.readouterr().err
    assert "CFO" in err and "feat/x" in err and "not logged" in err, \
        "still visible locally, with full detail (branch included), just not shared"


def test_a_non_private_checkouts_breach_is_still_filed(cpc, tmp_path, monkeypatch):
    """The gate must be a gate, not a kill switch — an ordinary checkout's breach
    (here, CFO with privacy forced off) must still reach `_post_signal` exactly as
    before."""
    _repo(tmp_path, "CFO", branch="feat/x")
    monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: False)

    bug_calls = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        cpc.main(["--convivy-root", str(tmp_path), "--no-report"])
    finally:
        cpc._post_signal = original
    assert any(a[0] == "CFO" for a, k in bug_calls), \
        "a non-private checkout's breach must still be logged"


def test_is_private_checkout_delegates_to_the_shared_private_crew_module(
        cpc, tmp_path, monkeypatch):
    """The gate itself, not the plumbing around it — proves `_is_private_checkout`
    genuinely resolves privacy via `private_crew.private_home_for` (the SAME
    registry lookup `check-serving-checkouts`/`session_state.py` already use, not
    a second implementation), using a real fake registry rather than a
    monkeypatched boolean. Also pins the case-sensitivity trap a naive fix would
    hit: the checkout directory here is capital-`CFO`, the registry slug is
    lower-case `cfo` — a bare string/slug comparison would silently never match.
    """
    spec = importlib.util.find_spec("private_crew")
    if spec is None:
        pytest.skip("private_crew not importable in this environment")
    private_crew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private_crew)

    crew_home = tmp_path / "CFO"
    crew_home.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    registry = cfg / "projects.json"
    registry.write_text(f'{{"cfo": {{"home_dir": "{crew_home}", "private": true}}}}')
    monkeypatch.setattr(private_crew, "_REGISTRY", str(registry))
    monkeypatch.setitem(sys.modules, "private_crew", private_crew)

    ordinary = tmp_path / "not-cfo"
    ordinary.mkdir()
    assert cpc._is_private_checkout_real(crew_home) is True
    assert cpc._is_private_checkout_real(ordinary) is False


def test_is_private_checkout_fails_closed_when_the_registry_is_unreadable(
        cpc, tmp_path, monkeypatch):
    """The writer's own fail-direction (ADR-0043: a privacy control must not fail
    open), reused here because this function is about to decide whether to WRITE
    a breach to a shared store — "cannot tell if this is private" must not be
    read as "go ahead and file it"."""
    spec = importlib.util.find_spec("private_crew")
    if spec is None:
        pytest.skip("private_crew not importable in this environment")
    private_crew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private_crew)
    monkeypatch.setattr(private_crew, "_REGISTRY", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setitem(sys.modules, "private_crew", private_crew)

    assert cpc._is_private_checkout_real(tmp_path / "anything") is True


def test_the_unclassified_audit_never_names_a_future_private_crew_in_the_shared_bug(
        cpc, tmp_path, monkeypatch, capsys):
    """The completeness audit is a set of names, so a future private crew showing
    up alongside a future public one must be handled as a MIXED set, not
    all-or-nothing: the public repo still gets the bug it has always gotten, and
    the private one's name must never reach the title, body, or dedup_key of it —
    same rule as a classified private crew's own breach, applied one step
    earlier, before anyone has even decided whether it belongs in
    `_LIVE_CHECKOUTS`."""
    _repo(tmp_path, "newsvc")
    _repo(tmp_path, "futuresecretcrew")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.newsvc.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/newsvc</string>")
    _plist(la, "com.convivy.futuresecretcrew.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/futuresecretcrew</string>")
    monkeypatch.setattr(cpc, "_is_private_checkout",
                        lambda repo_dir: repo_dir.name == "futuresecretcrew")

    bug_calls = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                       "--no-report"])
    finally:
        cpc._post_signal = original

    assert rc == 2
    unclassified_calls = [c for c in bug_calls
                          if "checkout(s) nobody has classified" in c[0][1]]
    assert len(unclassified_calls) == 1, "the public repo must still be logged"
    args, kwargs = unclassified_calls[0]
    title, body, dedup_key = args[1], args[2], kwargs.get("dedup_key", "")
    for field in (title, body, dedup_key):
        assert "futuresecretcrew" not in field
    assert "newsvc" in body

    err = capsys.readouterr().err
    assert "futuresecretcrew" in err and "not logged" in err, \
        "still visible locally, just not shared"


def test_repo_sets_that_joined_alike_get_different_unclassified_keys(cpc, tmp_path, monkeypatch):
    """The old key joined the names with `_`, so the set ["a_b"] and the set ["a", "b"] both became
    `a_b`, and the second distinct set answered `duplicate` on the same day (review of PR #788,
    rounds 1 and 2)."""
    assert "_".join(["a_b"]) == "_".join(["a", "b"]), "the collision the old key produced"
    keys = []
    for run, names in (("one", ["a_b"]), ("two", ["a", "b"])):
        root = tmp_path / run
        root.mkdir()
        for name in names:
            _repo(root, name)
            _plist(root / "LaunchAgents", f"com.convivy.{name}.plist",
                   f"<key>WorkingDirectory</key><string>{root}/{name}</string>")
        monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: False)
        calls = []
        original = cpc._post_signal
        cpc._post_signal = lambda *a, **k: calls.append((a, k)) or {"status": "stub"}
        try:
            cpc.main(["--convivy-root", str(root), "--launch-agents", str(root / "LaunchAgents"),
                      "--no-report"])
        finally:
            cpc._post_signal = original
        keys += [k["dedup_key"] for a, k in calls
                 if "checkout(s) nobody has classified" in a[1]]

    assert len(keys) == 2, keys
    assert keys[0] != keys[1], "distinct unclassified sets must not share a key"


def test_an_all_private_unclassified_set_files_nothing_but_still_counts(
        cpc, tmp_path, monkeypatch, capsys):
    """The other end of the mixed set, and the one branch the rest of this file
    leaves unexercised: when EVERY unclassified checkout is private,
    `unclassified_public` is empty and the shared bug is never built at all.

    The failure this pins is a silent one in both directions. `breaches` is
    incremented before the split, so an all-private set must still make the run
    exit non-zero — a check that goes quiet AND reports success is the
    false-confidence shape this whole file exists to avoid. And nothing may reach
    `_post_signal` on that path, not even an empty-set bug titled "0 checkout(s)
    nobody has classified", which is what a naive `if unclassified:` guard around
    the filing block would produce.
    """
    _repo(tmp_path, "secretcrewone")
    _repo(tmp_path, "secretcrewtwo")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.secretcrewone.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/secretcrewone</string>")
    _plist(la, "com.convivy.secretcrewtwo.plist",
           f"<key>WorkingDirectory</key><string>{tmp_path}/secretcrewtwo</string>")
    monkeypatch.setattr(cpc, "_is_private_checkout", lambda repo_dir: True)

    bug_calls = []
    original = cpc._post_signal
    cpc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                       "--no-report"])
    finally:
        cpc._post_signal = original

    assert rc == 2, "an all-private unclassified set must still fail the run"
    unclassified_calls = [c for c in bug_calls
                          if "checkout(s) nobody has classified" in c[0][1]]
    assert unclassified_calls == [], \
        "nothing may be filed when every unclassified checkout is private"

    err = capsys.readouterr().err
    assert "secretcrewone" in err and "secretcrewtwo" in err, \
        "both names still reach this machine's own log"
    assert "not logged" in err


# --- `--assert`: the same predicate, at the moment of harm (ARCH-441) --------
#
# The monitor bounds how LONG a bad state lasts; an assertion in the actor stops
# it being acted on at all. Both reviewers of PR #347 made that point, from
# different angles. These tests are about the assertion staying the SAME question
# the monitor asks — two implementations would drift, and the one that drifted
# silently would be the assertion, because nothing watches one that passes.


def test_assert_passes_a_checkout_on_main(cpc, tmp_path, capsys):
    _repo(tmp_path, "convivy-app-kit")
    assert cpc.assert_one("convivy-app-kit", tmp_path, actor="the pull") == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # a pass says nothing


@pytest.mark.parametrize("kwargs,expected", [
    ({"branch": "feat/x"}, "on 'feat/x'"),
    ({"detach": True}, "detached HEAD"),
])
def test_assert_refuses_a_checkout_off_main(cpc, tmp_path, capsys, kwargs, expected):
    _repo(tmp_path, "convivy-app-kit", **kwargs)
    rc = cpc.assert_one("convivy-app-kit", tmp_path, actor="the hourly pull")
    assert rc == cpc.EXIT_ASSERT_BREACH
    err = capsys.readouterr().err
    assert "REFUSING" in err and expected in err
    assert "the hourly pull" in err          # names what was about to act
    assert "git switch main" in err          # and what to do about it


def test_assert_passes_a_missing_checkout(cpc, tmp_path):
    """Same as the monitor: a repo that is not there cannot be off main, and
    refusing on absence would stop an actor on a box where the repo was never
    cloned."""
    assert cpc.assert_one("convivy-app-kit", tmp_path, actor="x") == 0


def test_assert_refuses_a_repo_nobody_classified(cpc, tmp_path, capsys):
    """Fails closed on the unknown. An assertion that passed for any name not in
    the table would silently do nothing after a rename — which is precisely how
    a guard comes to report fine while guarding nothing."""
    _repo(tmp_path, "some-new-repo")
    rc = cpc.assert_one("some-new-repo", tmp_path, actor="x")
    assert rc == cpc.EXIT_ASSERT_BREACH
    assert "not in _LIVE_CHECKOUTS" in capsys.readouterr().err


def test_assert_files_no_bug(cpc, tmp_path, monkeypatch):
    """The monitor already files one, on its own dedup_key, within fifteen minutes.
    A second filer would double-report a single incident and make the queue's
    count of it wrong."""
    calls = []
    monkeypatch.setattr(cpc, "_post_signal", lambda *a, **kw: calls.append(a) or {})
    _repo(tmp_path, "convivy-app-kit", branch="feat/x")
    assert cpc.assert_one("convivy-app-kit", tmp_path, actor="x") == cpc.EXIT_ASSERT_BREACH
    assert calls == []


def test_assert_through_main_exits_four_and_skips_the_sweep(cpc, tmp_path, capsys):
    """`--assert` is a pre-flight for one actor: it must not run the completeness
    audit or check the other repos, both of which file bugs."""
    _repo(tmp_path, "convivy-app-kit", branch="feat/x")
    _repo(tmp_path, "agent-platform", branch="feat/also-broken")
    rc = cpc.main(["--convivy-root", str(tmp_path), "--assert", "convivy-app-kit",
                   "--actor", "the hourly pull"])
    assert rc == cpc.EXIT_ASSERT_BREACH
    err = capsys.readouterr().err
    assert "convivy-app-kit" in err
    assert "agent-platform" not in err       # the other breach is not this call's business


def test_assert_exit_code_is_distinct_from_the_sweep_codes(cpc):
    """A caller branches on `$?`. 0/2/3 already mean something to the sweep, so
    the assertion needed its own rather than reusing 'findings'."""
    assert cpc.EXIT_ASSERT_BREACH not in (0, 1, 2, 3)


def test_the_actor_asserts_the_repo_it_acts_on(cpc):
    """The wiring, pinned. The actor names a repo that is actually in the table,
    and the ONE it touches — an assertion against the wrong repo passes forever
    and guards nothing.

    `launch-overnight` was the other actor (asserting `agent-platform`) until it
    was retired with the rest of the autonomous overnight cron fleet
    (issue_01M2GDVPR81Z0X991Z7XD3VR0M, the operator's 2026-09-14 ruling); this guard's
    `agent-platform` half went with it, leaving `appkit-autoupdate` the only
    caller of `--assert`.
    """
    autoupdate = (REPO / "bin" / "appkit-autoupdate").read_text()
    assert "--assert convivy-app-kit" in autoupdate
    assert "convivy-app-kit" in cpc._LIVE_CHECKOUTS


def test_neither_actor_hard_fails_when_the_check_is_missing(cpc):
    """Refusing because the GUARD is absent turns a broken install into a silent
    no-pull — the failure this guard exists to prevent, inverted. The actor
    degrades to its pre-ARCH-441 behaviour and says so."""
    for name in ("appkit-autoupdate",):
        text = (REPO / "bin" / name).read_text()
        assert "not found" in text and "WARNING" in text, name


def test_an_unreadable_head_is_not_reported_as_a_detached_head(cpc, tmp_path):
    """Both are breaches; they differ in what they tell the reader. `symbolic-ref
    -q` exits non-zero with empty stdout on a detached HEAD AND on a corrupted
    repo, and reporting the second as the first sends an operator debugging a
    cancelled night to look at branch state (security-reviewer, #354)."""
    d = _repo(tmp_path, "agent-platform")
    (d / ".git" / "HEAD").write_text("this is not a ref\n")
    branch, err = cpc.current_branch(d)
    assert branch is None
    assert "unreadable" in err and "detached" not in err.split("not a detached")[0]


def test_a_real_detached_head_still_says_detached_head(cpc, tmp_path):
    d = _repo(tmp_path, "agent-platform", detach=True)
    branch, err = cpc.current_branch(d)
    assert branch is None and err == "detached HEAD"


def _root_stub(fake_bin, root):
    """appkit-autoupdate finds the app kit under the machine's root, printed by
    the builder-root beside its own real path (company/decisions/0008), and HOME
    no longer moves it. Each copy of the script below gets a stub sibling that
    prints the fixture root, the same sibling resolution these tests pin for
    check-primary-checkouts."""
    stub = fake_bin / "builder-root"
    stub.write_text(f'#!/bin/bash\n[ "${{1:-}}" = "--root" ] || exit 2\necho "{root}"\n')
    stub.chmod(0o755)


def test_the_autoupdate_script_refuses_end_to_end_against_a_real_checkout(
        cpc, tmp_path, monkeypatch):
    """The wiring test above is a text search, so a script with the right string
    in a COMMENT and the wrong argument in the call would pass it
    (security-reviewer, #354). This runs the real script against a real off-main
    checkout and checks it does not reach the pull."""
    import os
    root = tmp_path / "Convivy"          # the script reads <root>/<repo>; _root_stub prints it
    _repo(root, "convivy-app-kit", branch="feat/x")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Stand in for the installed check, pointed at the fixture root, so the script
    # under test runs its REAL assertion path against a REAL breach.
    (fake_bin / "check-primary-checkouts").write_text(
        f'#!/bin/bash\nexec {CHECK} --convivy-root {root} "$@"\n')
    (fake_bin / "check-primary-checkouts").chmod(0o755)
    # A git that fails loudly if the script ever reaches the pull.
    (fake_bin / "git-should-not-run").write_text("#!/bin/bash\nexit 99\n")

    env = dict(os.environ, HOME=str(tmp_path))
    (tmp_path / ".local").mkdir(exist_ok=True)
    (tmp_path / ".local" / "bin").symlink_to(fake_bin)
    # appkit-autoupdate resolves CHECK relative to its OWN location (platform/
    # decisions/0059 rule 1), not $HOME/.local/bin, so the stub above must sit
    # beside a COPY of the script under test rather than being invoked via
    # REPO/bin directly — otherwise CHECK resolves to REPO/bin/check-primary-
    # checkouts, the real script, and the stub is never executed.
    script = fake_bin / "appkit-autoupdate"
    script.write_text((REPO / "bin" / "appkit-autoupdate").read_text())
    script.chmod(0o755)
    _root_stub(fake_bin, root)
    proc = subprocess.run([str(script)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 4, proc.stderr
    assert "REFUSING" in proc.stderr
    assert "not pulling" in proc.stderr


def test_the_autoupdate_script_pulls_when_the_checkout_is_on_main(
        cpc, tmp_path):
    """The other direction, which matters more: a guard that refused always would
    stop the hourly update forever and look like a working guard."""
    import os
    root = tmp_path / "Convivy"
    d = _repo(root, "convivy-app-kit")
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", str(d)],
                   check=True, capture_output=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "check-primary-checkouts").write_text(
        f'#!/bin/bash\nexec {CHECK} --convivy-root {root} "$@"\n')
    (fake_bin / "check-primary-checkouts").chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path))
    (tmp_path / ".local").mkdir(exist_ok=True)
    (tmp_path / ".local" / "bin").symlink_to(fake_bin)
    # Same reason as the refusal test above: CHECK resolves relative to the
    # script's OWN location, so the stub must sit beside a COPY of the script
    # under test rather than being invoked via REPO/bin directly.
    script = fake_bin / "appkit-autoupdate"
    script.write_text((REPO / "bin" / "appkit-autoupdate").read_text())
    script.chmod(0o755)
    _root_stub(fake_bin, root)
    proc = subprocess.run([str(script)],
                          capture_output=True, text=True, env=env)
    assert "REFUSING" not in proc.stderr
    assert "pulling" in proc.stdout


def test_the_autoupdate_script_pulls_when_the_check_cannot_RUN(cpc, tmp_path):
    """Exit 4 is the only refusal. Any other non-zero means the check could not
    run — a missing interpreter, a broken install — which is the same class as it
    being absent, and freezing the hourly pull on it is this guard's own failure
    mode inverted. It did exactly that on first deploy: launchd's thin PATH could
    not resolve the check's `python3.11`, so the guard exited 127 and the pull
    refused every hour (ARCH-441 follow-up)."""
    import os
    root = tmp_path / "Convivy"
    d = _repo(root, "convivy-app-kit")
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", str(d)],
                   check=True, capture_output=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # exit 127 is what a shell reports for "command not found" — the real symptom.
    (fake_bin / "check-primary-checkouts").write_text("#!/bin/bash\nexit 127\n")
    (fake_bin / "check-primary-checkouts").chmod(0o755)
    # appkit-autoupdate resolves CHECK relative to its OWN location (platform/
    # decisions/0059 rule 1), not $HOME, so the broken-check stub must sit
    # beside a COPY of the script under test rather than at $HOME/.local/bin.
    script = fake_bin / "appkit-autoupdate"
    script.write_text((REPO / "bin" / "appkit-autoupdate").read_text())
    script.chmod(0o755)
    _root_stub(fake_bin, root)
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.run([str(script)],
                          capture_output=True, text=True, env=env)
    assert "REFUSED" not in proc.stderr
    assert "could not run" in proc.stderr and "not a refusal" in proc.stderr
    assert "pulling" in proc.stdout


def test_the_autoupdate_plist_carries_the_path_its_check_needs(cpc):
    """The script's own paths are absolute; the check it calls is
    `#!/usr/bin/env python3.11`, and launchd's thin PATH has no /opt/homebrew
    (ARCH-086). Without this key the guard cannot execute under launchd at all —
    which review did not catch, because the check is a second hop."""
    import plistlib
    d = plistlib.loads((REPO / "launchd" /
                        "com.convivy.appkit.autoupdate.plist").read_bytes())
    path = d["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path, "python3.11 lives here"
    # Templated: install.sh renders __HOME__ to the installing user's $HOME.
    assert "__HOME__/.local/bin" in path, "the check itself lives here"


# --- the dedup_key is scoped to the UTC day ----------------------------------
# The signal store keeps every dedup_key for all time, so an unscoped key logs a
# breach that clears and returns never again. Each run reads the day once, and
# every key it writes carries it. Pinned here: two days give two keys, two runs
# on one day give one key, and present, absent, present on a later day logs again.

def _primary_keys(cpc, tmp_path, monkeypatch, *, day):
    """Run the branch sweep with the agent-platform checkout on a feature branch
    (a breach); return the dedup_keys it logged. The one checkout is switched in
    place by the caller, so a breach can clear and return within one tree."""
    monkeypatch.setattr(cpc, "_utc_today", lambda: day, raising=False)
    keys = []
    monkeypatch.setattr(cpc, "_post_signal",
                        lambda *a, **k: keys.append(k.get("dedup_key")) or {"status": "stub"})
    empty = tmp_path / "no-launch-agents"
    empty.mkdir(exist_ok=True)
    cpc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(empty), "--no-report"])
    return keys


def test_primary_checkout_key_is_scoped_to_the_utc_day(cpc, tmp_path, monkeypatch):
    _repo(tmp_path, "agent-platform", branch="feat/one")
    day_one = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-01")
    day_two = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-02")
    assert day_one != day_two, "the same breach on two UTC days must log twice"
    assert day_one == ["primary-checkout:agent-platform:2030-01-01"]
    assert day_two == ["primary-checkout:agent-platform:2030-01-02"]


def test_primary_checkout_two_runs_on_one_day_give_one_key(cpc, tmp_path, monkeypatch):
    _repo(tmp_path, "agent-platform", branch="feat/one")
    first = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-01")
    second = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-01")
    assert first == second == ["primary-checkout:agent-platform:2030-01-01"]


def test_primary_checkout_present_absent_present_logs_again_on_the_later_day(
        cpc, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "agent-platform", branch="feat/one")
    present = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-01")
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "main"], check=True)
    absent = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-02")
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "feat/one"], check=True)
    back = _primary_keys(cpc, tmp_path, monkeypatch, day="2030-01-03")
    assert absent == []
    assert back == ["primary-checkout:agent-platform:2030-01-03"] and back != present
