"""publish_scan — Builder's content scan run on the exact commit `agentctl
publish` is about to push.

`scan_commit`'s own subprocess calls (the Shipwright export, and
publish-engine itself) are stubbed: `_export_shipwright_tree` is monkeypatched
to hand back a fixture directory holding a tiny, real `bin/publish-engine`
script (`_write_fake_engine`) that this file controls completely, so every
test exercises `scan_commit`'s real parsing of a real subprocess's exit code
and stderr — never a canned `subprocess.run` return value standing in for
one.
"""
from __future__ import annotations

import stat
import subprocess as sp
import textwrap
from pathlib import Path

import pytest

import publish_scan


# A fake publish-engine, controlled entirely by env vars this file sets, so each
# test drives a different one of the real tool's documented behaviours: argparse
# rejecting an unknown repository name before touching anything, and the two
# names-file checks and the content-scan refusal publish-engine's own `Refused`
# path would print — in that tool's own message shape (`"publish-engine: ..."`,
# never a matched value), which is exactly what `scan_commit` has to parse.
_FAKE_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, os, sys

    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library"])
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    args = p.parse_args()

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
    if os.environ.get("FAKE_ENGINE_FINDINGS"):
        print(f"publish-engine: {args.name} at deadbeef cannot be exported as it "
              f"stands. The tree was DISCARDED. No value is printed; open each file "
              f"to judge it.\\n  content scan (1):\\n"
              f"    tests/fixture.py:3: an email address", file=sys.stderr)
        sys.exit(2)
    print(f"publish-engine: built 3 file(s) of {args.name} at deadbeef")
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
    engine above, and both names files present."""
    checkout = tmp_path / "shipwright"
    (checkout / ".git").mkdir(parents=True)
    export_root = _write_fake_engine(tmp_path / "export")
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", lambda _checkout: export_root)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    return checkout, export_root, private, machine


def test_a_clean_scan_lets_the_push_proceed(tmp_path, monkeypatch, stubbed):
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    assert publish_scan.scan_commit("agent-platform", tmp_path, "abc123") is None


def test_findings_refuse_and_name_file_line_and_class_never_a_value(
        tmp_path, monkeypatch, stubbed):
    monkeypatch.setenv("FAKE_ENGINE_FINDINGS", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commit("agent-platform", tmp_path, "abc123")
    message = str(exc.value)
    assert "tests/fixture.py:3: an email address" in message
    assert "DISCARDED" in message
    # The synthetic fixture carries no real matched value anywhere in the
    # message to begin with — the assertion that matters is that the scan's
    # own text (never a value, by its own design) is what's relayed verbatim.
    assert "content scan (1):" in message


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


def test_a_repository_publish_engine_does_not_export_is_pushed_without_a_scan(
        tmp_path, monkeypatch, stubbed):
    """`sous-chef` is not in the fake engine's `choices` — the real engine's own
    argparse rejects it before touching the names files at all, and that
    rejection must read as "not an engine export", never as a refusal."""
    assert publish_scan.scan_commit("sous-chef", tmp_path, "abc123") is None


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
    """`tests/conftest.py` strips `run_sandbox.GIT_OBJECT_REDIRECT_VARS` from `os.environ`
    once, at collection time, so a test that wants to exercise `_clean_git_env`'s OWN
    stripping has to set one back, past that — this reproduces, inside the test, the exact
    redirect a dispatched run's own shell carries (`dispatch_env`), pointed at a directory
    that holds none of Shipwright's objects.

    `GIT_DIR` and `GIT_OBJECT_DIRECTORY` each independently make a plain `git archive`
    against this fixture fail with the real git binary (exit 128, confirmed by hand against
    this exact fixture) when left in the subprocess's environment — this is the 2026-10
    bug found by hand while building this module, where a dispatched run's environment
    redirected git against Shipwright into agent-platform's own object store and read back
    no objects. `_clean_git_env` strips both before `_export_shipwright_tree`'s subprocess
    call, so this must still succeed with either one set."""
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
    everything the archive needs. `_clean_git_env` strips it anyway, for symmetry with
    `run_sandbox.GIT_OBJECT_REDIRECT_VARS` — the one list conftest and
    `import_private_objects` are also keyed on — not because an occurrence was found where
    this module's own git calls break without that strip: removing this one variable from
    `_clean_git_env`'s strip set does NOT turn this test red. The two tests above are the
    ones that catch a regression; this one records why a third, parallel test for this
    variable would not."""
    checkout = _real_shipwright_checkout(tmp_path)
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "not-shipwrights-objects"))
    dest = publish_scan._export_shipwright_tree(checkout)
    assert (dest / "bin" / "publish-engine").is_file()
