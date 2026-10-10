"""`check-serving-checkouts` — the launchd-served-checkout monitor.

The Library incident this exists for: `com.convivy.library`'s plist points its
`WorkingDirectory` straight at the crew's own checkout, and that checkout sat
on a feature branch for 19 hours after the PR merged through `gh`. Restarting
the service would have restarted the same stale branch, so the tests below are
about the two distinctions that make this check something other than
`check-primary-checkouts` with a wider net: branch state and dirty-tree state
are independently tracked findings, and a dirty tree is only a finding when it
touches a file outside `.claude/` that is actually TRACKED (not merely
untracked noise).

CFO is a black-box crew, so a separate group of tests pins that no test here
ever needs a file's CONTENT to pass — only its path and a one/two-letter
status code, which is the whole design constraint stated in the script's own
docstring.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO / "bin" / "check-serving-checkouts"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_serving_checkouts", str(CHECK))
    spec = importlib.util.spec_from_loader("check_serving_checkouts", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def csc():
    mod = _load()
    # Stashed here, module-scoped, BEFORE any per-test autouse fixture ever
    # runs — `_no_private_crews` below monkeypatches `_is_private_checkout`
    # fresh for every test, so the "delegates to private_crew" tests need a
    # handle to the ORIGINAL function that isn't the one currently patched.
    mod._is_private_checkout_real = mod._is_private_checkout
    return mod


@pytest.fixture(autouse=True)
def _no_private_crews(csc, monkeypatch):
    """Every test in this file except the ones under "private-crew gating"
    below is about something other than CFO's own registry entry, and none
    of them should depend on whether the REAL machine's
    `~/Convivy/agent-platform/config/projects.json` happens to be readable.
    `_is_private_checkout` delegates to `private_crew.private_home_for`,
    which fails CLOSED (`UNKNOWN`) when it cannot read that registry at
    all — true on this dev box (readable, real CFO entry) and true
    differently on the Linux CI runner (`~/Convivy` does not exist there),
    so an un-neutralized test suite would file bugs locally and silently
    stop filing them on CI, for a reason with nothing to do with what each
    test is actually pinning. Neutralized to `False` (not private) by
    default, matching this file's existing hermeticity convention
    (`test_session_verbs.py`'s `_no_private_crews`, same reasoning)."""
    monkeypatch.setattr(csc, "_is_private_checkout", lambda repo_dir: False)


def _run(repo_dir: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, check=True)


def _repo(root: pathlib.Path, name: str, *, branch: str = "main",
          detach: bool = False) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True)
    _run(d, "init", "-q", "-b", "main")
    _run(d, "config", "user.email", "t@example.com")
    _run(d, "config", "user.name", "T")
    (d / "seed.md").write_text("seed\n")
    _run(d, "add", "-A")
    _run(d, "commit", "-q", "-m", "seed")
    if detach:
        _run(d, "checkout", "-q", "--detach", "HEAD")
    elif branch != "main":
        _run(d, "checkout", "-q", "-b", branch)
    return d


def _plist(dir_: pathlib.Path, name: str, *, label: str, working_directory: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{label}</string>\n"
        f"  <key>WorkingDirectory</key><string>{working_directory}</string>\n"
        "</dict></plist>\n",
        encoding="utf-8")


# --- discovery ----------------------------------------------------------------


_HAS_PLUTIL = shutil.which("plutil") is not None


def test_a_serving_plist_is_discovered(csc, tmp_path):
    repo = _repo(tmp_path, "somesvc")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist", label="com.convivy.thing", working_directory=str(repo))
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {repo.resolve(): ["com.convivy.thing"]}
    assert unparseable == []


def test_multiple_services_sharing_one_checkout_are_grouped(csc, tmp_path):
    """`convivy-orient` backs 8 plists from one checkout — the finding must name
    all of them, not just whichever plist happened to be read last."""
    repo = _repo(tmp_path, "orient")
    la = tmp_path / "LaunchAgents"
    for i, label in enumerate(["com.convivy.orient", "com.convivy.orient-dashboard",
                               "com.convivy.orient-deploy-watch"]):
        _plist(la, f"svc{i}.plist", label=label, working_directory=str(repo))
    found, _, _ = csc.serving_checkouts(la, tmp_path)
    assert found[repo.resolve()] == sorted(
        ["com.convivy.orient", "com.convivy.orient-dashboard", "com.convivy.orient-deploy-watch"])


def test_a_working_directory_outside_convivy_root_is_excluded_not_dropped(csc, tmp_path):
    """Homebrew's own installation is git-managed; this check has no opinion
    about its branch and must not treat it as a serving checkout — but the
    exclusion must be NAMED, not silently dropped (steward, 2026-08-21): a
    real git checkout outside `--convivy-root` is a different fact from "not
    a checkout at all," and reporting only the former as a bare `{}` is the
    same shape as the unparseable-plist bug this file exists to avoid a
    second time. Real example: `homebrew.mxcl.postgresql@15`'s
    WorkingDirectory is `/opt/homebrew`, which sits on a branch called
    `stable` — a true positive by the branch rule and a false positive by
    intent, correctly excluded because it is outside `~/Convivy`."""
    outside = tmp_path / "elsewhere" / "brew"
    _repo(outside.parent, "brew")
    la = tmp_path / "LaunchAgents"
    convivy_root = tmp_path / "Convivy"
    convivy_root.mkdir()
    _plist(la, "homebrew.mxcl.thing.plist", label="homebrew.mxcl.thing",
          working_directory=str(outside))
    found, excluded, unparseable = csc.serving_checkouts(la, convivy_root)
    assert found == {} and unparseable == []
    assert excluded == {outside.resolve(): ["homebrew.mxcl.thing"]}


def test_a_working_directory_that_is_not_a_repo_is_ignored_not_excluded(csc, tmp_path):
    """Absent from ALL THREE — a plain directory is not a checkout at all, a
    different fact from a real checkout this scope deliberately excludes."""
    (tmp_path / "not-a-repo").mkdir()
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist", label="com.convivy.thing",
          working_directory=str(tmp_path / "not-a-repo"))
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {} and excluded == {} and unparseable == []


def test_a_missing_launch_agents_dir_finds_nothing(csc, tmp_path):
    """A machine with no LaunchAgents (a CI runner) must not crash."""
    found, excluded, unparseable = csc.serving_checkouts(tmp_path / "nope", tmp_path)
    assert found == {} and excluded == {} and unparseable == []


def test_a_plist_that_parses_fine_with_no_working_directory_key_is_not_a_finding(
        csc, tmp_path):
    """THE MOST COMMON REAL SHAPE (reviewer, PR #423, run-9dcd3d1b) — most
    launchd plists don't serve from a checkout at all. A plist that parses
    cleanly but simply never declares `WorkingDirectory` must be absent from
    ALL THREE results: not a finding (nothing to check), not `unparseable`
    (the file itself was perfectly readable), not `excluded` (there is no
    checkout to exclude)."""
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.ordinary.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.convivy.ordinary</string>\n"
        "  <key>ProgramArguments</key><array><string>/bin/true</string></array>\n"
        "</dict></plist>\n",
        encoding="utf-8")
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {} and excluded == {} and unparseable == []


# --- the survey-invalidating bug: a plist that cannot be parsed must be
# REPORTED, not silently skipped ------------------------------------------
#
# The steward ran the manual survey this issue was scoped from with
# `plistlib` alone, hit the identical `--`-in-comment failure, and the
# exception was swallowed rather than surfaced: those services were absent
# from the count rather than flagged unreadable. "13 services, so the scope
# is small" was filed on that undercount. A re-run with `plutil` found 29.
# Fail-open on an unparseable input is worse than no check at all, because it
# reports a clean result over a partial read — this is the fix.


@pytest.mark.skipif(not _HAS_PLUTIL, reason="the leniency under test is plutil's own")
def test_a_plist_with_an_xml_comment_containing_a_double_dash_is_still_read(csc, tmp_path):
    """THE BUG THIS PINS. `plistlib`'s strict XML parser raises `ExpatError` on a
    `--` sequence inside a `<!-- comment -->` — several real plists on this
    machine have exactly that in their own header comments, documentation, not
    the directive. `com.selector-backend.api` was the ONLY plist naming that
    checkout, so a stricter reader than `plutil` (what this prefers) made an
    entire real, currently-running service invisible to this check, not just
    undercounted its label list.
    """
    repo = _repo(tmp_path, "somesvc")
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.thing.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<!--\n"
        "  Retention -- one month -- then rotate.\n"
        "-->\n"
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.convivy.thing</string>\n"
        f"  <key>WorkingDirectory</key><string>{repo}</string>\n"
        "</dict></plist>\n",
        encoding="utf-8")
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {repo.resolve(): ["com.convivy.thing"]}
    assert unparseable == []


def test_a_genuinely_unparseable_plist_is_reported_not_skipped(csc, tmp_path):
    """THE FIX ITSELF. A plist that is not valid property-list data at all —
    not merely one with a comment a strict parser dislikes — must show up in
    `unparseable`, not vanish from the count the way the manual survey's did."""
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.broken.plist").write_text("this is not a plist at all\x00\x01", encoding="utf-8")
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {}
    assert unparseable == ["com.convivy.broken.plist"]


def test_a_readable_plist_alongside_an_unparseable_one_still_finds_the_readable_one(
        csc, tmp_path):
    """One bad plist must not hide every other — the same degradation rule
    every other reader in this file's neighbourhood follows."""
    repo = _repo(tmp_path, "somesvc")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.good.plist", label="com.convivy.good", working_directory=str(repo))
    (la / "com.convivy.broken.plist").write_text("not a plist\x00\x01", encoding="utf-8")
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {repo.resolve(): ["com.convivy.good"]}
    assert unparseable == ["com.convivy.broken.plist"]


def test_the_plistlib_fallback_reads_an_ordinary_plist_when_plutil_is_absent(
        csc, tmp_path, monkeypatch):
    """This script only ever RUNS in production on macOS — a launchd job,
    reading ~/Library/LaunchAgents, which does not exist elsewhere. The
    fallback exists purely to keep this test suite exercising real parsing
    logic on this repo's Linux `python` CI job, where `plutil` is not on
    PATH at all. An ordinary, well-formed plist (what every test fixture in
    this file is) must still be readable through it."""
    monkeypatch.setattr(csc, "_PLUTIL", None)
    repo = _repo(tmp_path, "somesvc")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist", label="com.convivy.thing", working_directory=str(repo))
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {repo.resolve(): ["com.convivy.thing"]}
    assert unparseable == []


def test_the_plistlib_fallback_also_reports_unparseable_rather_than_skipping(
        csc, tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "_PLUTIL", None)
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.broken.plist").write_text("not a plist\x00\x01", encoding="utf-8")
    found, excluded, unparseable = csc.serving_checkouts(la, tmp_path)
    assert found == {}
    assert unparseable == ["com.convivy.broken.plist"]


def test_main_files_one_finding_for_all_unparseable_plists(csc, tmp_path, capsys):
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.broken-a.plist").write_text("not a plist\x00", encoding="utf-8")
    (la / "com.convivy.broken-b.plist").write_text("also not a plist\x00", encoding="utf-8")
    seen = []
    original = csc._post_signal
    csc._post_signal = lambda *a, dedup_key, **k: seen.append(dedup_key) or {"status": "stub"}
    try:
        rc = csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original
    assert rc == 2
    assert len(seen) == 1 and seen[0].startswith("serving-checkout:unparseable:")
    # The local log still names names — this is the LOCAL, single-machine
    # channel, not the shared store the dedup_key/body avoid naming them in.
    err = capsys.readouterr().err
    assert "com.convivy.broken-a.plist" in err and "com.convivy.broken-b.plist" in err


def test_the_unparseable_bug_never_names_a_filename(csc, tmp_path, monkeypatch):
    """THE FOLLOW-UP FIX (security-reviewer, PR #423 round 2, non-blocking,
    picked up immediately per the steward's direction rather than deferred).
    A plist's own filename can name a crew — `com.convivy.cfo-review.plist`
    genuinely contains "cfo" — and this check cannot resolve privacy for a
    file it cannot parse at all (confirmed directly: `plutil -extract` fails
    identically to `plutil -lint` on a genuinely corrupt file, so there is no
    partial WorkingDirectory to hand `_is_private_checkout`). So neither the
    dedup_key nor the body sent to the SHARED store may ever contain a
    filename — only a count. The filenames still reach this process's own
    local stderr log (pinned by the test above)."""
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.convivy.cfo-review-broken.plist").write_text("not a plist\x00", encoding="utf-8")

    calls = []
    original = csc._post_signal
    csc._post_signal = lambda product, title, body, dedup_key, **k: (
        calls.append((product, title, body, dedup_key)) or {"status": "stub"})
    try:
        csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original

    assert len(calls) == 1
    _product, title, body, dedup_key = calls[0]
    for field in (title, body, dedup_key):
        assert "com.convivy.cfo-review-broken.plist" not in field
        assert "cfo" not in field.lower()


def test_the_dedup_key_cannot_be_used_to_confirm_which_plist_broke(csc, tmp_path):
    """THE ACTUAL FIX (security-reviewer, PR #424 round 1, blocking). A first
    version of this fix hashed the sorted filenames into the dedup_key —
    plausible-looking, but wrong: launchd plist filenames in this fleet are
    drawn from a small, largely-public namespace
    (`com.convivy.<crew>[-suffix].plist`), so a reader of the shared queue
    could enumerate a few dozen candidate names, hash each, and match
    against the published digest in microseconds — confirming exactly which
    plist (and which crew) broke, with no special access. SHA-256 is
    one-way against a large/unknown input space; it is not private against
    a small, guessable one.

    The fix is coarser dedup instead: the dedup_key carries only the COUNT of
    unparseable plists, a value with no connection to which ones they are.
    Two completely different plist sets of the same size MUST produce the
    identical dedup_key — proven directly here, not just "no hash function
    is called" — since that identity is what makes the key un-reversible:
    there is nothing set-specific left to reverse.
    """
    def _run_once(la, filenames):
        la.mkdir()
        for name in filenames:
            (la / name).write_text("not a plist\x00", encoding="utf-8")
        seen = []
        original = csc._post_signal
        csc._post_signal = lambda *a, dedup_key, **k: seen.append(dedup_key) or {"status": "stub"}
        try:
            csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
        finally:
            csc._post_signal = original
        return seen[0]

    key_a = _run_once(tmp_path / "LA1", ["com.convivy.cfo-review.plist"])
    key_b = _run_once(tmp_path / "LA2", ["com.convivy.unrelated-thing.plist"])
    assert key_a == key_b, "same count must produce the identical key regardless of which plist"
    assert "cfo" not in key_a.lower()


def test_the_dedup_key_varies_by_date_so_a_resolved_incident_cannot_silence_a_later_one(
        csc, tmp_path, monkeypatch):
    """THE SECOND ROUND-2 FIX (steward, 2026-08-21, verified against the actual
    Library server code, not assumed from a docstring). `library/store.py`'s
    `insert_item` has no state filter on its dedup_key lookup
    (`SELECT * FROM issue_items WHERE dedup_key = ?`), and
    `library/app.py`'s `_can_file_into` — the gate that decides whether a
    dedup_key collision is answered idempotently — checks only authorization
    and product scope, never `existing["state"]`. So a bare count-based key
    would, the FIRST time any same-count incident is ever resolved, silently
    stop filing forever for that count: a completely different, later
    incident collides with the old resolved row and vanishes into a `200
    idempotent: true`.

    Scoping the key to today's date closes this: at most one bug per day
    while a count persists (bounded, not spam at the 15-minute interval),
    and the date always differs from whatever day a prior incident with the
    same count was resolved on."""
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "broken.plist").write_text("not a plist\x00", encoding="utf-8")

    def _run_on(day: str) -> str:
        monkeypatch.setattr(csc, "_today", lambda: day)
        seen = []
        original = csc._post_signal
        csc._post_signal = lambda *a, dedup_key, **k: seen.append(dedup_key) or {"status": "stub"}
        try:
            csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
        finally:
            csc._post_signal = original
        return seen[0]

    key_day_one = _run_on("2030-01-01")
    key_day_two = _run_on("2030-01-02")
    assert key_day_one != key_day_two, \
        "the same count on two different days must not collide with a resolved prior incident"
    assert "2030-01-01" in key_day_one and "2030-01-02" in key_day_two


def test_main_names_an_excluded_checkout_without_filing_anything(csc, tmp_path, capsys):
    """A line in the output, not a finding and not a bug (steward, 2026-08-21)
    — the whole point is that the scope of a run is legible from its log
    without reading this file's source, printed even on an otherwise clean
    run (unconditional, not gated on --verbose)."""
    outside = tmp_path / "elsewhere" / "cookbook-app"
    _repo(outside.parent, "cookbook-app")
    la = tmp_path / "LaunchAgents"
    convivy_root = tmp_path / "Convivy"
    convivy_root.mkdir()
    _plist(la, "com.convivy.outside-backend.plist", label="com.convivy.outside-backend",
          working_directory=str(outside))

    bug_calls = []
    original = csc._post_signal
    csc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = csc.main(["--convivy-root", str(convivy_root), "--launch-agents", str(la),
                      "--no-report"])
    finally:
        csc._post_signal = original
    assert rc == 0, "an exclusion alone is not a finding"
    assert bug_calls == [], "an exclusion must not file anything"
    err = capsys.readouterr().err
    assert "[EXCLUDED]" in err
    assert "cookbook-app" in err and "com.convivy.outside-backend" in err


# --- branch state ---------------------------------------------------------


def test_a_checkout_on_main_clean_has_no_findings(csc, tmp_path):
    repo = _repo(tmp_path, "somesvc")
    assert csc.check(repo) == []


def test_a_feature_branch_is_a_branch_finding(csc, tmp_path):
    repo = _repo(tmp_path, "somesvc", branch="feat/x")
    findings = csc.check(repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "branch"
    assert "feat/x" in findings[0]["reason"]
    assert findings[0]["paths"] == []


def test_a_detached_head_is_a_branch_finding_and_says_so(csc, tmp_path):
    """Named separately from an ordinary off-main branch — it points the
    service at an arbitrary commit while `git status` shows nothing that
    reads as a problem at a glance."""
    repo = _repo(tmp_path, "somesvc", detach=True)
    findings = csc.check(repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "branch"
    assert findings[0]["reason"] == "detached HEAD"


# --- the noise problem: .claude/ and untracked files -----------------------


def test_dirty_claude_individuation_is_not_a_finding(csc, tmp_path):
    """THE ORIENT CASE. 7 uncommitted files, all `.claude/` individuation
    (some modified-tracked, some untracked) plus a stray untracked file
    outside `.claude/` — none of it is a defect."""
    repo = _repo(tmp_path, "orient")
    (repo / ".claude").mkdir()
    (repo / ".claude" / ".materialized").write_text("x")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "individuate")
    (repo / ".claude" / ".materialized").write_text("y")          # modified, tracked
    (repo / ".claude" / "new-agent.md").write_text("z")           # untracked
    (repo / "stray-icon.png").write_text("binary-ish")            # untracked, outside .claude
    assert csc.check(repo) == []


def test_a_modified_tracked_file_outside_claude_is_a_dirty_finding(csc, tmp_path):
    """THE CFO CASE. A modified, TRACKED file outside `.claude/` — this is the
    finding the noise-filtering exists to still catch."""
    repo = _repo(tmp_path, "cfo")
    (repo / "schema.sql").write_text("create table x (id int);\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "schema")
    (repo / "schema.sql").write_text("create table x (id int, y int);\n")
    findings = csc.check(repo)
    assert len(findings) == 1
    assert findings[0]["kind"] == "dirty"
    assert findings[0]["paths"] == ["schema.sql"]


def test_an_untracked_file_outside_claude_is_not_a_finding(csc, tmp_path):
    """Untracked is a different situation from modified-and-committed-once: it
    is something nobody has committed yet, not something that drifted from a
    reviewed state. CFO's real incident had untracked `agent-runs/`,
    `hook-log/`, `webapp/` directories alongside its real (modified-tracked)
    finding, and only the modified files were the point."""
    repo = _repo(tmp_path, "cfo")
    (repo / "generated-output").mkdir()
    (repo / "generated-output" / "run.log").write_text("x")
    assert csc.check(repo) == []


def test_branch_and_dirty_are_two_independent_findings_when_both_occur(csc, tmp_path):
    """THE BUG CAUGHT BEFORE SHIPPING. A checkout can be off `main` AND carry a
    dirty served file at once — collapsing that into one reason would let
    fixing either condition silently drop the other from the alert."""
    repo = _repo(tmp_path, "somesvc", branch="feat/x")
    (repo / "app.py").write_text("x = 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "seed2")
    (repo / "app.py").write_text("x = 2\n")
    findings = csc.check(repo)
    kinds = sorted(f["kind"] for f in findings)
    assert kinds == ["branch", "dirty"]


# --- a private crew's findings never reach the shared issue queue ----------
#
# THE BUG (security-reviewer, PR #423, run-0d72a1e3). A finding's own
# EXISTENCE in the shared, multi-reader issue queue is a leak for a
# black-box crew independent of how little it says — filing "CFO is on
# branch `x` with these changed files" is exactly what
# `platform/decisions/0043` forbids, even though this check never reads a
# line of CFO's own code. These tests override this file's
# `_no_private_crews` default, since that default exists for every OTHER
# test in this file.


def test_a_private_checkouts_finding_is_not_filed(csc, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, "cfo", branch="feat/x")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.cfo-review.plist", label="com.convivy.cfo-review",
          working_directory=str(repo))
    monkeypatch.setattr(csc, "_is_private_checkout", lambda repo_dir: True)

    bug_calls = []
    original = csc._post_signal
    csc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        rc = csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original

    assert bug_calls == [], "a private crew's finding must never reach _post_signal"
    assert rc == 2, "the finding still counts — a real problem on a private checkout is still real"
    err = capsys.readouterr().err
    assert "cfo" in err and "not logged" in err, "still visible locally, just not shared"


def test_a_non_private_checkouts_finding_is_still_filed(csc, tmp_path, monkeypatch, capsys):
    """The gate must be a gate, not a kill switch — an ordinary checkout's
    finding must still reach `_post_signal` exactly as before."""
    repo = _repo(tmp_path, "ordinary", branch="feat/x")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.thing.plist", label="com.convivy.thing", working_directory=str(repo))
    monkeypatch.setattr(csc, "_is_private_checkout", lambda repo_dir: False)

    bug_calls = []
    original = csc._post_signal
    csc._post_signal = lambda *a, **k: bug_calls.append((a, k)) or {"status": "stub"}
    try:
        csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original
    assert len(bug_calls) == 1


# --- resolving `private_crew` itself (issue_01M33KK04A9B1A2MC710CNF583) -----


def test_ensure_lib_on_path_prefers_the_checkout_over_the_installed_farm(csc, monkeypatch):
    """`_ensure_lib_on_path` must find THIS checkout's own lib/agent-platform
    even when the installed ~/.local/lib farm doesn't exist at all — the
    state of a Builder deploy area, which has bin/ and lib/agent-platform/
    together but never runs install.sh. The checkout candidate is real
    (`REPO / "lib" / "agent-platform"`); the farm candidate is patched to a
    directory that cannot exist, so a pass here cannot be the farm quietly
    covering for the checkout candidate.
    """
    checkout_lib = REPO / "lib" / "agent-platform"
    missing_farm = pathlib.Path("/nonexistent-farm-dir-for-test")
    monkeypatch.setattr(csc, "_LIB_CANDIDATES", (checkout_lib, missing_farm))
    # conftest.py's own bootstrap already put this checkout's lib on the real
    # sys.path for the whole suite — strip it so the assertion below proves
    # THIS CALL inserted it, not that the entry was already there.
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(checkout_lib)])
    csc._ensure_lib_on_path()
    assert sys.path[0] == str(checkout_lib)


def test_ensure_lib_on_path_falls_through_to_pythonpath_when_absent(csc, monkeypatch, tmp_path):
    """Neither candidate existing is not an error: the import then falls
    through to whatever PYTHONPATH already put on sys.path, which is how a
    test reaches this checkout's own lib rather than whatever is installed
    on the machine. This is the one DoD line issue_01M33KK04A9B1A2MC710CNF583
    calls out by name for this file — converting `_ensure_lib_on_path` to try
    the checkout first must not break the fall-through.
    """
    absent_a = tmp_path / "absent-a"
    absent_b = tmp_path / "absent-b"
    monkeypatch.setattr(csc, "_LIB_CANDIDATES", (absent_a, absent_b))
    before = list(sys.path)
    csc._ensure_lib_on_path()
    assert sys.path == before


def test_is_private_checkout_delegates_to_the_shared_private_crew_module(
        csc, tmp_path, monkeypatch):
    """The gate itself, not the plumbing around it — proves `_is_private_checkout`
    genuinely resolves privacy via `private_crew.private_home_for` (the SAME
    registry lookup `session_state.py`/`tmux_transcript.py` already use, not
    a second implementation), using a real fake registry rather than a
    monkeypatched boolean. Also pins the case-sensitivity trap a naive fix
    would hit: the checkout directory here is capital-`CFO`, the registry
    slug is lower-case `cfo` — a bare string/slug comparison would silently
    never match.
    """
    import importlib.util as iu
    spec = iu.find_spec("private_crew")
    if spec is None:
        pytest.skip("private_crew not importable in this environment")
    private_crew = iu.module_from_spec(spec)
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
    assert csc._is_private_checkout_real(crew_home) is True
    assert csc._is_private_checkout_real(ordinary) is False


def test_is_private_checkout_fails_closed_when_the_registry_is_unreadable(
        csc, tmp_path, monkeypatch):
    """The writer's own fail-direction (ADR-0043: a privacy control must not
    fail open), reused here because this function is about to decide
    whether to WRITE a finding to a shared store — "cannot tell if this is
    private" must not be read as "go ahead and file it"."""
    import importlib.util as iu
    spec = iu.find_spec("private_crew")
    if spec is None:
        pytest.skip("private_crew not importable in this environment")
    private_crew = iu.module_from_spec(spec)
    spec.loader.exec_module(private_crew)
    monkeypatch.setattr(private_crew, "_REGISTRY", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setitem(sys.modules, "private_crew", private_crew)

    assert csc._is_private_checkout_real(tmp_path / "anything") is True


# --- content safety (CFO is a black box) -----------------------------------


def test_the_check_never_reads_file_content(csc, tmp_path, monkeypatch):
    """CFO is a black-box crew (platform/decisions/0043) — this check must work
    from git metadata alone. Pins it behaviourally: `Path.read_text`/`read_bytes`
    and `git show`/`git diff`/`git log -p` must never be invoked by `check()`,
    even though a file with genuinely sensitive-looking content sits in the
    dirty set.
    """
    repo = _repo(tmp_path, "cfo")
    (repo / "financials.csv").write_text("acct,amount\n1234,500\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "seed2")
    (repo / "financials.csv").write_text("acct,amount\n1234,999999-SECRET-BALANCE\n")

    real_run = subprocess.run

    def _guarded_run(argv, *a, **kw):
        assert "diff" not in argv and "show" not in argv and "log" not in argv, \
            f"content-reading git command invoked: {argv}"
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", _guarded_run)

    real_read_text = pathlib.Path.read_text

    def _guarded_read_text(self, *a, **kw):
        assert self.name != "financials.csv", "check() read a served file's content"
        return real_read_text(self, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", _guarded_read_text)

    findings = csc.check(repo)
    assert findings[0]["paths"] == ["financials.csv"]
    assert "999999-SECRET-BALANCE" not in str(findings)


def test_the_alert_body_never_contains_file_content(csc, tmp_path):
    """The same property, checked at the `main()` boundary that actually
    builds the bug body a human reads."""
    repo = _repo(tmp_path, "cfo")
    (repo / "financials.csv").write_text("acct,amount\n1234,500\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "seed2")
    (repo / "financials.csv").write_text("acct,amount\n1234,999999-SECRET-BALANCE\n")
    la = tmp_path / "LaunchAgents"
    _plist(la, "com.convivy.cfo-review.plist", label="com.convivy.cfo-review",
          working_directory=str(repo))

    bodies = []
    original = csc._post_signal
    csc._post_signal = lambda product, title, body, dedup_key, **kw: (
        bodies.append(body) or {"status": "stub"})
    try:
        csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original
    assert bodies, "expected a finding to be filed"
    assert "999999-SECRET-BALANCE" not in "\n".join(bodies)
    assert "financials.csv" in "\n".join(bodies)


# --- main(): dedup_key separation, exit codes, grouping ----------------------


def test_the_dedup_key_separates_branch_from_dirty(csc, tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "_utc_today", lambda: "2030-01-01")
    repo1 = _repo(tmp_path, "branchy", branch="feat/x")
    repo2 = _repo(tmp_path, "dirty")
    (repo2 / "app.py").write_text("x = 1\n")
    _run(repo2, "add", "-A")
    _run(repo2, "commit", "-q", "-m", "seed2")
    (repo2 / "app.py").write_text("x = 2\n")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.branchy", working_directory=str(repo1))
    _plist(la, "b.plist", label="svc.dirty", working_directory=str(repo2))

    seen = []
    original = csc._post_signal
    csc._post_signal = lambda *a, dedup_key, **k: seen.append(dedup_key) or {"status": "stub"}
    try:
        csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original
    assert sorted(seen) == ["serving-checkout:branchy:branch:2030-01-01",
                            "serving-checkout:dirty:dirty:2030-01-01"]


def test_the_dedup_key_does_not_include_the_branch_name(csc, tmp_path, monkeypatch):
    monkeypatch.setattr(csc, "_utc_today", lambda: "2030-01-01")
    """One alert per repo per open FINDING KIND. Keying on the branch name would
    file a fresh bug every time someone switched between feature branches."""
    repo = _repo(tmp_path, "somesvc", branch="feat/one")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.one", working_directory=str(repo))
    seen = []
    original = csc._post_signal
    csc._post_signal = lambda *a, dedup_key, **k: seen.append(dedup_key) or {"status": "stub"}
    try:
        csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    finally:
        csc._post_signal = original
    assert seen == ["serving-checkout:somesvc:branch:2030-01-01"]


def test_main_exits_zero_and_is_silent_when_everything_is_clean(csc, tmp_path, capsys):
    """A pass prints nothing without --verbose. A monitor that speaks on every
    run trains its reader to ignore it."""
    repo = _repo(tmp_path, "somesvc")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.one", working_directory=str(repo))
    rc = csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_reports_findings_and_exits_two(csc, tmp_path, capsys):
    repo = _repo(tmp_path, "somesvc", branch="feat/x")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.one", working_directory=str(repo))
    rc = csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la),
                  "--no-report", "-v"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "feat/x" in err
    assert "svc.one" in err


def test_a_service_with_no_findings_is_silent_even_when_others_have_them(csc, tmp_path, capsys):
    clean = _repo(tmp_path, "clean-one")
    dirty = _repo(tmp_path, "somesvc", branch="feat/x")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.clean", working_directory=str(clean))
    _plist(la, "b.plist", label="svc.dirty", working_directory=str(dirty))
    rc = csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "svc.dirty" in err
    assert "svc.clean" not in err


# --- the dedup_key is scoped to the UTC day ----------------------------------
# The signal store keeps every dedup_key for all time, so an unscoped key logs a
# breach that clears and returns never again. Each run reads the day once, and
# every key it writes carries it. Pinned here: two days give two keys, two runs
# on one day give one key, and present, absent, present on a later day logs again.

def _serving_keys(csc, tmp_path, monkeypatch, *, day, breach):
    """Run main() on one service whose checkout is on a feature branch (breach)
    or on main (clean); return the dedup_keys it logged. check() is stubbed so
    the git state is the only thing that varies, and the day is pinned."""
    repo = tmp_path / "branchy"
    if not repo.exists():
        _repo(tmp_path, "branchy")
    la = tmp_path / "LaunchAgents"
    _plist(la, "a.plist", label="svc.branchy", working_directory=str(repo))
    findings = [{"kind": "branch", "reason": "is on feat/x, not main", "paths": []}]
    monkeypatch.setattr(csc, "_utc_today", lambda: day, raising=False)
    monkeypatch.setattr(csc, "check", lambda repo_dir: findings if breach else [])
    keys = []
    monkeypatch.setattr(csc, "_post_signal",
                        lambda *a, dedup_key, **k: keys.append(dedup_key) or {"status": "stub"})
    csc.main(["--convivy-root", str(tmp_path), "--launch-agents", str(la), "--no-report"])
    return keys


def test_serving_checkout_key_is_scoped_to_the_utc_day(csc, tmp_path, monkeypatch):
    day_one = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    day_two = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-02", breach=True)
    assert day_one != day_two, "the same breach on two UTC days must log twice"
    assert day_one == ["serving-checkout:branchy:branch:2030-01-01"]
    assert day_two == ["serving-checkout:branchy:branch:2030-01-02"]


def test_serving_checkout_two_runs_on_one_day_give_one_key(csc, tmp_path, monkeypatch):
    first = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    second = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    assert first == second == ["serving-checkout:branchy:branch:2030-01-01"]


def test_serving_checkout_present_absent_present_logs_again_on_the_later_day(
        csc, tmp_path, monkeypatch):
    present = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    absent = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-02", breach=False)
    back = _serving_keys(csc, tmp_path, monkeypatch, day="2030-01-03", breach=True)
    assert absent == []
    assert back == ["serving-checkout:branchy:branch:2030-01-03"] and back != present
