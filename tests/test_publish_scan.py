"""publish_scan — Builder's content scan run on the exact commit `agentctl
publish` is about to push.

`scan_commit`'s own subprocess calls (the Shipwright export, and
publish-engine itself) are stubbed: `_export_shipwright_tree` is monkeypatched
to hand back a fixture directory holding a tiny, real `bin/publish-engine`
script (`_write_fake_engine`) that this file controls completely, so every
test exercises `scan_commit`'s real parsing of a real subprocess's exit code,
stdout and stderr — never a canned `subprocess.run` return value standing in
for one.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess as sp
import tempfile
import textwrap
from pathlib import Path

import pytest

import publish_scan


# A fake publish-engine, controlled entirely by env vars this file sets, so each
# test drives a different one of the real tool's documented `--scan-only`
# behaviours: argparse rejecting an unknown repository name or a missing
# `--scan-only` before touching anything, the two names-file checks, a
# findings refusal, a could-not-run refusal, a "pass --ref HEAD" notice on a
# clean exit, and death by signal — each in that tool's own message shape
# (`"publish-engine: ..."`, never a matched value), which is exactly what
# `scan_commit` has to parse.
_FAKE_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, os, signal, sys

    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library", "write-better"])
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--scan-only", action="store_true")
    args = p.parse_args()

    if not args.scan_only:
        print("publish-engine: this fake only knows --scan-only", file=sys.stderr)
        sys.exit(2)

    call_log = os.environ.get("FAKE_ENGINE_CALL_LOG")
    if call_log:
        with open(call_log, "a") as f:
            f.write(args.ref + "\\n")

    dump = os.environ.get("FAKE_ENGINE_ARG_DUMP")
    if dump:
        with open(dump, "w") as f:
            f.write(f"REF={args.ref}\\nSCAN_ONLY={args.scan_only}\\n"
                    f"GIT_DIR={os.environ.get('GIT_DIR', '')}\\n"
                    f"ISOLATED={sys.flags.isolated}\\n"
                    f"SYS_PATH={os.pathsep.join(sys.path)}\\n")

    if os.environ.get("FAKE_ENGINE_SIGTERM"):
        os.kill(os.getpid(), signal.SIGTERM)

    private = os.environ.get("BUILDER_PRIVATE_NAMES", "")
    machine = os.environ.get("BUILDER_MACHINE_NAMES", "")
    if not private or not os.path.isfile(private):
        print(f"publish-engine: BUILDER_PRIVATE_NAMES names {private!r}, which cannot "
              f"be read, so nothing was built", file=sys.stderr)
        sys.exit(2)
    if not machine or not os.path.isfile(machine):
        print(f"publish-engine: BUILDER_MACHINE_NAMES names {machine!r}, which cannot "
              f"be read, so nothing was built", file=sys.stderr)
        sys.exit(2)

    if os.environ.get("FAKE_ENGINE_REFUSED"):
        print(f"publish-engine: the scan of {args.name} at {args.ref[:9]} could not "
              f"finish (ValueError), so it reached no verdict", file=sys.stderr)
        sys.exit(2)

    fail_ref = os.environ.get("FAKE_ENGINE_FAIL_REF")
    findings = os.environ.get("FAKE_ENGINE_FINDINGS") or (fail_ref and fail_ref == args.ref)
    if findings:
        print(f"publish-engine: {args.name} at {args.ref} (deadbeef9) would be refused "
              f"by a release. No value is printed; open each file to judge it.\\n"
              f"  content scan (1):\\n    tests/fixture.py:3: an email address")
        sys.exit(1)

    if os.environ.get("FAKE_ENGINE_REF_NOTICE"):
        print(f"publish-engine: the checkout's HEAD is at deadbeef9, but this run scans "
              f"{args.ref} at deadbeef9; pass --ref HEAD to scan the checkout instead.")

    print(f"publish-engine: {args.name} at {args.ref} (deadbeef9): scanned 3 file(s); "
          f"symlinks, .example siblings, content scan, secret-shaped keys: nothing found")
    sys.exit(0)
''')


def _write_fake_engine(root: Path) -> Path:
    engine = root / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(_FAKE_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return root


@pytest.fixture
def names_files(tmp_path):
    """Both names files present and readable — the passing default; a test that
    wants one missing points `monkeypatch` at a path under `tmp_path` instead."""
    private = tmp_path / "private-names"
    machine = tmp_path / "machine-names"
    private.write_text("Someone\n")
    machine.write_text("somehost\n")
    return private, machine


@pytest.fixture
def stubbed(tmp_path, monkeypatch, names_files):
    """`scan_commit` with every resolver pointed at this test's own fixtures:
    a fake Shipwright checkout (just needs a `.git` dir — `_export_shipwright_tree`
    is replaced outright, so nothing ever reads its history) exporting the fake
    engine above, and both names files present.

    The stub hands back a FRESH copy of the fake-engine tree on every call,
    mirroring the real `_export_shipwright_tree`'s own fresh-tempdir-per-call
    contract: `scan_commit`'s `finally` block deletes whatever directory it
    gets back, and `scan_commits` calls `scan_commit` more than once in a
    single test — a stub returning the same directory every time would have
    the first call's cleanup delete the fixture out from under the second."""
    checkout = tmp_path / "shipwright"
    (checkout / ".git").mkdir(parents=True)
    export_root = _write_fake_engine(tmp_path / "export")
    private, machine = names_files

    def _export(_checkout):
        copy = Path(tempfile.mkdtemp(prefix="publish-engine-src-test-"))
        shutil.copytree(export_root, copy, dirs_exist_ok=True)
        return copy

    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", _export)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    return checkout, export_root, private, machine


def test_a_clean_scan_lets_the_push_proceed(tmp_path, monkeypatch, stubbed):
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    assert publish_scan.scan_commit("agent-platform", tmp_path, "abc123") is None


def test_the_engine_receives_scan_only_from_and_ref_never_version_or_out(
        tmp_path, monkeypatch, stubbed):
    """The fake engine's own parser has no `--version` or `--out` at all, so
    either one being passed would make it exit nonzero (unrecognized
    arguments) and this test would see `scan_commit` raise — the dump just
    pins the three it does expect to the exact values `scan_commit` sent."""
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    sha = "f" * 40
    assert publish_scan.scan_commit("agent-platform", tmp_path, sha) is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["REF"] == sha
    assert lines["SCAN_ONLY"] == "True"


def test_the_engine_runs_isolated_so_a_parent_pythonpath_never_reaches_it(
        tmp_path, monkeypatch, stubbed):
    """`scan_commit` must pass `-I` to the interpreter ahead of the engine
    path, so the scan runs the engine's own code and nothing an inherited
    environment adds — `sys.flags.isolated` and `sys.path` are read from
    INSIDE the fake engine's own process, never asserted against the argv
    this test built, so this catches `-I` being passed in the wrong
    position (after the engine path, where the interpreter never sees it)
    as readily as its being dropped outright."""
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    sentinel_dir = tmp_path / "pythonpath-sentinel"
    sentinel_dir.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(sentinel_dir))
    assert publish_scan.scan_commit("agent-platform", tmp_path, "abc123") is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["ISOLATED"] == "1"
    assert str(sentinel_dir) not in lines["SYS_PATH"].split(os.pathsep)


def test_an_exit_of_1_names_both_a_finding_and_a_pre_scan_error_with_the_engines_own_output(
        tmp_path, monkeypatch, stubbed):
    """Exit 1 covers both a finding AND an error before the scan started —
    publish-engine's `--scan-only` contract does not distinguish the two on
    exit code alone, so the message must not assert a finding happened."""
    monkeypatch.setenv("FAKE_ENGINE_FINDINGS", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")
    message = str(exc.value)
    assert "found something to refuse, or failed before the scan started" in message
    assert "tests/fixture.py:3: an email address" in message
    # The synthetic fixture carries no real matched value anywhere in the
    # message to begin with — the assertion that matters is that the scan's
    # own text (never a value, by its own design) is what's relayed verbatim.
    assert "content scan (1):" in message


def test_an_exit_of_2_refuses_and_says_could_not_run(tmp_path, monkeypatch, stubbed):
    monkeypatch.setenv("FAKE_ENGINE_REFUSED", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")
    message = str(exc.value)
    assert "could not run" in message
    assert "could not finish (ValueError), so it reached no verdict" in message


def test_a_signal_death_refuses_and_says_could_not_run(tmp_path, monkeypatch, stubbed):
    monkeypatch.setenv("FAKE_ENGINE_SIGTERM", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")
    message = str(exc.value)
    assert "could not run" in message
    assert "exit -15" in message


def test_a_ref_head_notice_on_a_clean_exit_still_passes(tmp_path, monkeypatch, stubbed):
    """A `--ref` other than the checkout's HEAD makes the real engine print a
    "pass `--ref HEAD`" notice on stdout before its clean-scan line, on exit 0.
    That notice is not a finding."""
    monkeypatch.setenv("FAKE_ENGINE_REF_NOTICE", "1")
    assert publish_scan.scan_commit("agent-platform", tmp_path, "abc123") is None


def test_write_better_passes_with_a_clean_fake_engine(tmp_path, monkeypatch, stubbed):
    """write-better ships in no release, so a real export of it exits 2
    ("ships in no release, so it can only be scanned: pass --scan-only") —
    `agentctl publish` refuses every write-better push today. `--scan-only`
    is the mode that does accept it."""
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    assert publish_scan.scan_commit("write-better", tmp_path, "abc123") is None


def test_scan_commit_skips_a_repository_publish_engine_does_not_know(
        tmp_path, monkeypatch, stubbed):
    """`agentctl publish` runs `scan_commit` against every repository it
    pushes, including product repositories outside publish-engine's five
    scannable repositories. The fake engine's own argparse rejects a name
    its `choices` has never heard of before touching anything else; that
    rejection is this gate's cue that the repository is not its business,
    not a reason to block the push."""
    assert publish_scan.scan_commit("sous-chef", tmp_path, "abc123") is None


def test_scan_commit_still_blocks_when_the_invalid_choice_names_a_different_argument(
        tmp_path, monkeypatch, stubbed):
    """The skip above is matched precisely against the repository `scan_commit`
    itself passed. An argparse rejection naming some OTHER value — a
    different argument's own `choices=` rejection, not the repository
    name — must still block, even though the message contains the same
    "invalid choice: '...'" shape."""
    bogus = tmp_path / "bogus-engine"
    bogus_engine = bogus / "bin" / "publish-engine"
    bogus_engine.parent.mkdir(parents=True)
    bogus_engine.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import sys
        print("publish-engine: error: argument --mode: invalid choice: "
              "'not-the-repo-you-asked-for' (choose from 'a', 'b')", file=sys.stderr)
        sys.exit(2)
    '''))
    bogus_engine.chmod(bogus_engine.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", lambda _checkout: bogus)
    with pytest.raises(publish_scan.PublishScanRefused, match="could not run"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_an_argparse_exit_naming_scan_only_as_unrecognised_still_blocks(
        tmp_path, monkeypatch, stubbed):
    """An engine too old to know `--scan-only` rejects it with argparse's
    "unrecognized arguments" message — exit 2, but no "invalid choice" text
    at all, so it must still block rather than being read as the one
    precisely-matched skip case."""
    old_engine_root = tmp_path / "old-engine"
    old_engine = old_engine_root / "bin" / "publish-engine"
    old_engine.parent.mkdir(parents=True)
    old_engine.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import argparse
        p = argparse.ArgumentParser(prog="publish-engine")
        p.add_argument("name", choices=["agent-platform", "library", "write-better"])
        p.add_argument("--from", dest="source", required=True)
        p.add_argument("--ref", required=True)
        p.parse_args()
    '''))
    old_engine.chmod(old_engine.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", lambda _checkout: old_engine_root)
    with pytest.raises(publish_scan.PublishScanRefused, match="could not run"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_scan_commits_blocks_rather_than_skips_a_repository_publish_engine_does_not_know(
        tmp_path, monkeypatch, stubbed):
    """`scan_commits` backs the pre-push hook, which is only ever installed in
    a clone of a repository publish-engine scans — unlike `scan_commit`, the
    same argparse rejection here means the hook is misconfigured, and must
    block rather than skip."""
    with pytest.raises(publish_scan.PublishScanRefused, match="misconfigured"):
        publish_scan.scan_commits("sous-chef", tmp_path, ["abc123"])


def test_a_missing_private_names_file_refuses(tmp_path, monkeypatch, stubbed, names_files):
    _, machine = names_files
    monkeypatch.setattr(publish_scan, "private_names_file",
                        lambda: tmp_path / "no-such-private-names")
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    with pytest.raises(publish_scan.PublishScanRefused, match="BUILDER_PRIVATE_NAMES"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_a_missing_machine_names_file_refuses(tmp_path, monkeypatch, stubbed, names_files):
    private, _ = names_files
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file",
                        lambda: tmp_path / "no-such-machine-names")
    with pytest.raises(publish_scan.PublishScanRefused, match="BUILDER_MACHINE_NAMES"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_a_missing_shipwright_checkout_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "nope")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_a_missing_publish_engine_script_refuses(tmp_path, monkeypatch, names_files):
    checkout = tmp_path / "shipwright"
    (checkout / ".git").mkdir(parents=True)
    empty_export = tmp_path / "export-without-engine"
    empty_export.mkdir()
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", lambda _checkout: empty_export)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    with pytest.raises(publish_scan.PublishScanRefused, match="no bin/publish-engine"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


def test_an_export_that_fails_refuses(tmp_path, monkeypatch):
    checkout = tmp_path / "shipwright"
    (checkout / ".git").mkdir(parents=True)

    def _boom(_checkout):
        raise publish_scan.PublishScanRefused(f"could not export {_checkout} at origin/main: boom")

    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", _boom)
    with pytest.raises(publish_scan.PublishScanRefused, match="could not export"):
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")


# --- scan_commits: one `scan_commit` per commit, stopping at the first refusal ----

def test_scan_commits_stops_at_the_first_refusal_and_names_it_k_of_n(
        tmp_path, monkeypatch, stubbed):
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    shas = ["1" * 40, "2" * 40, "3" * 40]
    monkeypatch.setenv("FAKE_ENGINE_FAIL_REF", shas[1])
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", tmp_path, shas)
    message = str(exc.value)
    assert "agent-platform" in message
    assert shas[1][:9] in message
    assert "2 of 3" in message
    # The third commit is never reached once the second is refused.
    assert call_log.read_text().splitlines() == [shas[0], shas[1]]


def test_scan_commits_returns_when_every_commit_passes(tmp_path, monkeypatch, stubbed):
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    shas = ["1" * 40, "2" * 40]
    assert publish_scan.scan_commits("agent-platform", tmp_path, shas) is None
    assert call_log.read_text().splitlines() == shas


# --- the resolvers' own defaults and operator-config overrides ----------------

def test_shipwright_checkout_defaults_to_the_sibling_of_the_primary_checkout(
        tmp_path, monkeypatch):
    monkeypatch.delenv(publish_scan.OC.ENV_VAR, raising=False)
    monkeypatch.setenv(publish_scan.OC.ENV_VAR, str(tmp_path / "no-operator-config.json"))
    import subprocess as sp
    primary = tmp_path / "convivy" / "agent-platform"
    sp.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    assert publish_scan.shipwright_checkout(primary) == tmp_path / "convivy" / "shipwright"


def test_shipwright_checkout_honours_the_operator_config_key(tmp_path, monkeypatch):
    import json
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({"shipwright_checkout": str(tmp_path / "elsewhere")}))
    monkeypatch.setenv(publish_scan.OC.ENV_VAR, str(cfg))
    assert publish_scan.shipwright_checkout(tmp_path) == tmp_path / "elsewhere"


def test_private_and_machine_names_files_default_under_dot_config_convivy(monkeypatch, tmp_path):
    monkeypatch.setenv(publish_scan.OC.ENV_VAR, str(tmp_path / "absent.json"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert publish_scan.private_names_file() == tmp_path / ".config" / "convivy" / "private-names"
    assert publish_scan.machine_names_file() == tmp_path / ".config" / "convivy" / "machine-names"


def test_private_and_machine_names_files_honour_operator_config_keys(tmp_path, monkeypatch):
    import json
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({
        "builder_private_names_file": str(tmp_path / "p"),
        "builder_machine_names_file": str(tmp_path / "m"),
    }))
    monkeypatch.setenv(publish_scan.OC.ENV_VAR, str(cfg))
    assert publish_scan.private_names_file() == tmp_path / "p"
    assert publish_scan.machine_names_file() == tmp_path / "m"


# --- a dispatched run's own git object-store redirect must never reach Shipwright -----

def _real_shipwright_checkout(root: Path) -> Path:
    """A real git repository under `root`, with `origin/main` naming a commit that holds
    a real `bin/publish-engine` file — built with real git commands so the tests below
    exercise `_export_shipwright_tree`'s actual `git archive` subprocess, never a stub."""
    checkout = root / "shipwright"
    sp.run(["git", "init", "-q", "-b", "trunk", str(checkout)], check=True)
    engine = checkout / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True)
    engine.write_text("#!/usr/bin/env python3\n")
    sp.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    sp.run(["git", "-C", str(checkout), "-c", "user.email=t@t.example", "-c", "user.name=t",
            "commit", "-q", "-m", "x"], check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", "HEAD"],
           check=True)
    return checkout


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_OBJECT_DIRECTORY"])
def test_export_shipwright_tree_strips_a_dispatched_runs_object_redirect(
        tmp_path, monkeypatch, var):
    """`tests/conftest.py` strips the fixed three-variable redirect set from
    `os.environ` once, at collection time, so a test that wants to exercise
    `_clean_git_env`'s OWN stripping has to set one back, past that — this
    reproduces, inside the test, the exact redirect a dispatched run's own
    shell carries (`dispatch_env`), pointed at a directory that holds none of
    Shipwright's objects.

    `GIT_DIR` and `GIT_OBJECT_DIRECTORY` each independently make a plain `git archive`
    against this fixture fail with the real git binary (exit 128, confirmed by hand against
    this exact fixture) when left in the subprocess's environment — this is the 2026-10
    bug found by hand while building this module, where a dispatched run's environment
    redirected git against Shipwright into agent-platform's own object store and read back
    no objects. `_clean_git_env` strips both (along with every other var `git rev-parse
    --local-env-vars` names) before `_export_shipwright_tree`'s subprocess call, so this
    must still succeed with either one set."""
    checkout = _real_shipwright_checkout(tmp_path)
    monkeypatch.setenv(var, str(tmp_path / "not-shipwrights-objects"))
    dest = publish_scan._export_shipwright_tree(checkout)
    assert (dest / "bin" / "publish-engine").is_file()


def test_export_shipwright_tree_is_unaffected_by_alternate_object_directories_alone(
        tmp_path, monkeypatch):
    """`GIT_ALTERNATE_OBJECT_DIRECTORIES` is consulted only AFTER git's own default object
    store, never instead of it — unlike `GIT_DIR` and `GIT_OBJECT_DIRECTORY`, which each
    override discovery outright, setting this alone (confirmed by hand against this exact
    fixture, pointed at a directory holding none of Shipwright's objects) still lets `git
    archive` succeed, because Shipwright's checkout's own `.git/objects` already has
    everything the archive needs. `_clean_git_env` strips it anyway, for symmetry with every
    other variable `git rev-parse --local-env-vars` names — not because an occurrence was
    found where this module's own git calls break without that strip: removing this one
    variable from `_clean_git_env`'s strip set does NOT turn this test red. The two tests
    above are the ones that catch a regression; this one records why a third, parallel test
    for this variable would not."""
    checkout = _real_shipwright_checkout(tmp_path)
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "not-shipwrights-objects"))
    dest = publish_scan._export_shipwright_tree(checkout)
    assert (dest / "bin" / "publish-engine").is_file()


def test_the_engine_subprocess_never_sees_a_git_dir_inherited_from_the_parent(
        tmp_path, monkeypatch, stubbed):
    """Unlike the two tests above, which drive `_export_shipwright_tree`'s own `git
    archive` directly, this drives `scan_commit`'s OTHER subprocess call — the engine
    itself — with `GIT_DIR` set in the parent environment the way a push-time hook
    invoked by git would inherit it, pointed at a repository that has nothing to do
    with the one being scanned."""
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated" / ".git"))
    assert publish_scan.scan_commit("agent-platform", tmp_path, "abc123") is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["GIT_DIR"] == ""
