"""publish_scan — Builder's content scan run on the exact commits `agentctl
publish` is about to push, refusing only a finding the push ADDS.

`scan_commits`'s own subprocess calls (the Shipwright export, and
publish-engine itself) are stubbed: `_export_shipwright_tree` is monkeypatched
to hand back a fixture directory holding a tiny, real `bin/publish-engine`
script (`_write_fake_engine`) that this file controls completely, so every
test exercises `scan_commits`'s real parsing of a real subprocess's exit code,
stdout and stderr — never a canned `subprocess.run` return value standing in
for one.

A test that cares which of TWO scans (base and head) the fake engine is
asked for drives it with `FAKE_ENGINE_REF_MAP`, a JSON file mapping a ref to
the exact `(exit code, stdout)` the fake prints for it; a ref absent from the
map falls back to the older, single-scan env-var toggles the rest of this
file still uses for everything that returns before a base scan is ever
needed (a clean head, an unscannable repository, a dead or refused engine).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess as sp
import tarfile
import tempfile
import textwrap
from pathlib import Path

import pytest

import publish_scan


# A fake publish-engine, controlled entirely by env vars this file sets, so each
# test drives a different one of the real tool's documented `--scan-only`
# behaviours: argparse rejecting an unknown repository name or a missing
# `--scan-only` before touching anything, the two names-file checks, a
# findings refusal (globally, or per-ref via FAKE_ENGINE_REF_MAP), a
# could-not-run refusal, a "pass --ref HEAD" notice on a clean exit, and death
# by signal — each in that tool's own message shape (`"publish-engine: ..."`,
# never a matched value), which is exactly what `scan_commits` has to parse.
#
# The "pass --ref HEAD" notice can also precede a FINDINGS report, which is
# the real engine's own behaviour `_write_ref_map`'s canned per-ref text below
# reproduces: `_preamble`/`_findings_output`/`_clean_output` take an optional
# `real_head`, and prepend the exact notice line (`_head_notice`, copied from
# Shipwright's `bin/publish-engine` at `origin/main`) whenever the ref being
# built for is not that `real_head` — mirroring the real engine printing it
# whenever `--ref` names a commit other than the checkout's actual HEAD.
_FAKE_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, json, os, signal, sys

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
                    f"HAS_PROJECTS_JSON={'PROJECTS_JSON' in os.environ}\\n"
                    f"HAS_BUILDER_UNCHECKED_WORDS={'BUILDER_UNCHECKED_WORDS' in os.environ}\\n"
                    f"HAS_BUILDER_PRIVATE_NAMES={'BUILDER_PRIVATE_NAMES' in os.environ}\\n"
                    f"HAS_BUILDER_MACHINE_NAMES={'BUILDER_MACHINE_NAMES' in os.environ}\\n"
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

    if os.environ.get("FAKE_ENGINE_PRESCAN_ERROR"):
        # Exit 1 with nothing on stdout at all: `--scan-only`'s contract does
        # not promise stdout holds a findings block on every exit of 1, so
        # this is the "failed before the scan started" half of that ambiguity.
        print(f"publish-engine: {args.name} at {args.ref[:9]}: a pre-scan error, "
              f"not a finding", file=sys.stderr)
        sys.exit(1)

    ref_map_path = os.environ.get("FAKE_ENGINE_REF_MAP")
    if ref_map_path:
        with open(ref_map_path) as f:
            ref_map = json.load(f)
        if args.ref in ref_map:
            exit_code, stdout_text = ref_map[args.ref]
            print(stdout_text)
            sys.exit(exit_code)

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
          f"symlinks, .example siblings, content scan, secret-shaped keys, "
          f"operator registry names: nothing found")
    sys.exit(0)
''')


def _write_fake_engine(root: Path) -> Path:
    engine = root / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(_FAKE_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return root


# --- building the exact text publish-engine's --scan-only prints -------------

def _head_notice(ref: str, real_head: str) -> str:
    """The real engine's own "pass --ref HEAD" notice line, printed ahead of
    its report whenever `ref` names a commit other than the checkout's
    actual HEAD — wording copied from Shipwright's `bin/publish-engine` at
    `origin/main`."""
    return (f"publish-engine: the checkout's HEAD is at {real_head[:9]}, but this run "
            f"scans {ref} at {ref[:9]}; pass --ref HEAD to scan the checkout instead.")


def _preamble(name: str, ref: str, real_head: str | None = None) -> str:
    header = (f"publish-engine: {name} at {ref} (deadbeef9) would be refused by a "
              f"release. No value is printed; open each file to judge it.")
    if real_head is not None and real_head != ref:
        return _head_notice(ref, real_head) + "\n" + header
    return header


def _findings_output(name: str, ref: str, items: list[str], title: str = "operator registry names",
                      real_head: str | None = None) -> str:
    lines = [_preamble(name, ref, real_head), f"  {title} ({len(items)}):"]
    lines += [f"    {item}" for item in items]
    return "\n".join(lines)


def _clean_output(name: str, ref: str, real_head: str | None = None) -> str:
    header = (f"publish-engine: {name} at {ref} (deadbeef9): scanned 3 file(s); "
              f"symlinks, .example siblings, content scan, secret-shaped keys, "
              f"operator registry names: nothing found")
    if real_head is not None and real_head != ref:
        return _head_notice(ref, real_head) + "\n" + header
    return header


def _write_ref_map(tmp_path: Path, mapping: dict[str, tuple[int, str]]) -> Path:
    path = tmp_path / f"ref-map-{len(list(tmp_path.glob('ref-map-*.json')))}.json"
    path.write_text(json.dumps(mapping))
    return path


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
    """`scan_commits` with every resolver pointed at this test's own fixtures:
    a fake Shipwright checkout (just needs a `.git` dir — `_export_shipwright_tree`
    is replaced outright, so nothing ever reads its history) exporting the fake
    engine above, and both names files present.

    The stub hands back a FRESH copy of the fake-engine tree on every call,
    mirroring the real `_export_shipwright_tree`'s own fresh-tempdir-per-call
    contract: `_scan_added_findings`'s `finally` block deletes whatever
    directory it gets back, and it calls the exporter twice per scan (once for
    the head, once for the base) whenever a base scan is needed — a stub
    returning the same directory every time would have the first call's
    cleanup delete the fixture out from under the second."""
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


def _git(wt: Path, *args: str) -> sp.CompletedProcess:
    return sp.run(["git", "-C", str(wt), *args], capture_output=True, text=True, check=True)


def _commit(wt: Path, message: str) -> str:
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t.example", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(wt, "rev-parse", "HEAD").stdout.strip()


def _merge(wt: Path, *args: str) -> sp.CompletedProcess:
    return _git(wt, "-c", "user.email=t@t.example", "-c", "user.name=t", "merge", *args)


def _commit_tree(wt: Path, tree: str, parent: str, message: str) -> str:
    return _git(wt, "-c", "user.email=t@t.example", "-c", "user.name=t",
                "commit-tree", tree, "-p", parent, "-m", message).stdout.strip()


@pytest.fixture
def wt_with_base_and_head(tmp_path) -> tuple[Path, str, str]:
    """A real git repository with two commits: `base`, published as
    `origin/main`, and `head`, its child. `publish_scan._parents(wt, head)`
    resolves to `[base]` here, since `head` has exactly one parent, so this
    fixture is the shape a `scan_commits` test of a single pushed commit
    needs."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("base\n")
    base = _commit(wt, "base")
    _git(wt, "update-ref", "refs/remotes/origin/main", base)
    (wt / "f.txt").write_text("head\n")
    head = _commit(wt, "head")
    return wt, base, head


def test_a_clean_scan_lets_the_push_proceed(tmp_path, monkeypatch, stubbed):
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    assert publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True) is None


def test_the_engine_receives_scan_only_from_and_ref_never_version_or_out(
        tmp_path, monkeypatch, stubbed):
    """The fake engine's own parser has no `--version` or `--out` at all, so
    either one being passed would make it exit nonzero (unrecognized
    arguments) and this test would see `scan_commits` raise — the dump just
    pins the three it does expect to the exact values `scan_commits` sent."""
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    sha = "f" * 40
    assert publish_scan.scan_commits("agent-platform", tmp_path, [sha], skip_unknown_repo=True) is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["REF"] == sha
    assert lines["SCAN_ONLY"] == "True"


def test_the_engine_runs_isolated_so_a_parent_pythonpath_never_reaches_it(
        tmp_path, monkeypatch, stubbed):
    """`scan_commits` must pass `-I` to the interpreter ahead of the engine
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
    assert publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True) is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["ISOLATED"] == "1"
    assert str(sentinel_dir) not in lines["SYS_PATH"].split(os.pathsep)


def test_an_exit_of_1_with_no_parseable_findings_still_refuses(tmp_path, monkeypatch, stubbed):
    """Exit 1 covers both a finding AND an error before the scan started —
    publish-engine's `--scan-only` contract does not distinguish the two on
    exit code alone. With nothing on stdout this gate could parse as a
    finding, it must refuse the HEAD scan outright, without ever needing to
    resolve a base commit to compare against."""
    monkeypatch.setenv("FAKE_ENGINE_PRESCAN_ERROR", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)
    message = str(exc.value)
    assert "found something to refuse, or failed before the scan started" in message
    assert "a pre-scan error, not a finding" in message


def test_an_exit_of_1_with_a_real_finding_absent_from_a_clean_base_refuses(
        monkeypatch, stubbed, wt_with_base_and_head):
    """The ordinary case the old, single-scan test above used to cover: an
    exit of 1 whose stdout DOES parse as a finding. On its own that no longer
    refuses — the finding has to be new relative to the base too — so this
    pins the base as clean and confirms it still refuses, with the engine's
    own finding text relayed verbatim."""
    wt, base, head = wt_with_base_and_head
    ref_map = _write_ref_map(wt, {
        base: (0, _clean_output("agent-platform", base)),
        head: (1, _findings_output("agent-platform", head,
                                    ["tests/fixture.py:3: an email address"],
                                    title="content scan")),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)
    message = str(exc.value)
    assert "tests/fixture.py:3: an email address" in message


def test_an_exit_of_2_refuses_and_says_could_not_run(tmp_path, monkeypatch, stubbed):
    monkeypatch.setenv("FAKE_ENGINE_REFUSED", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)
    message = str(exc.value)
    assert "could not run" in message
    assert "could not finish (ValueError), so it reached no verdict" in message


def test_a_signal_death_refuses_and_says_could_not_run(tmp_path, monkeypatch, stubbed):
    monkeypatch.setenv("FAKE_ENGINE_SIGTERM", "1")
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)
    message = str(exc.value)
    assert "could not run" in message
    assert "exit -15" in message


def test_a_ref_head_notice_on_a_clean_exit_still_passes(tmp_path, monkeypatch, stubbed):
    """A `--ref` other than the checkout's HEAD makes the real engine print a
    "pass `--ref HEAD`" notice on stdout before its clean-scan line, on exit 0.
    That notice is not a finding."""
    monkeypatch.setenv("FAKE_ENGINE_REF_NOTICE", "1")
    assert publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True) is None


def test_write_better_passes_with_a_clean_fake_engine(tmp_path, monkeypatch, stubbed):
    """write-better ships in no release, so a real export of it exits 2
    ("ships in no release, so it can only be scanned: pass --scan-only") —
    `agentctl publish` refuses every write-better push today. `--scan-only`
    is the mode that does accept it."""
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    assert publish_scan.scan_commits("write-better", tmp_path, ["abc123"], skip_unknown_repo=True) is None


def test_scan_commits_with_skip_unknown_repo_skips_a_repository_publish_engine_does_not_know(
        tmp_path, monkeypatch, stubbed):
    """`agentctl publish` runs `scan_commits(..., skip_unknown_repo=True)`
    against every repository it pushes, including product repositories
    outside publish-engine's five scannable repositories. The fake engine's
    own argparse rejects a name its `choices` has never heard of before
    touching anything else; that rejection is this gate's cue that the
    repository is not its business, not a reason to block the push."""
    assert publish_scan.scan_commits("initech", tmp_path, ["abc123"], skip_unknown_repo=True) is None


def test_scan_commits_with_skip_unknown_repo_still_blocks_when_the_invalid_choice_names_a_different_argument(
        tmp_path, monkeypatch, stubbed):
    """The skip above is matched precisely against the repository
    `scan_commits` itself was passed. An argparse rejection naming some
    OTHER value — a different argument's own `choices=` rejection, not the
    repository name — must still block, even though the message contains
    the same "invalid choice: '...'" shape."""
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
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


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
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


def test_scan_commits_blocks_rather_than_skips_a_repository_publish_engine_does_not_know(
        tmp_path, monkeypatch, stubbed):
    """`scan_commits` backs the pre-push hook, which is only ever installed in
    a clone of a repository publish-engine scans — unlike
    `scan_commits(..., skip_unknown_repo=True)`, the same argparse rejection
    here means the hook is misconfigured, and must block rather than skip."""
    with pytest.raises(publish_scan.PublishScanRefused, match="misconfigured"):
        publish_scan.scan_commits("initech", tmp_path, ["abc123"])


def test_a_missing_private_names_file_refuses(tmp_path, monkeypatch, stubbed, names_files):
    _, machine = names_files
    monkeypatch.setattr(publish_scan, "private_names_file",
                        lambda: tmp_path / "no-such-private-names")
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    with pytest.raises(publish_scan.PublishScanRefused, match="BUILDER_PRIVATE_NAMES"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


def test_a_missing_machine_names_file_refuses(tmp_path, monkeypatch, stubbed, names_files):
    private, _ = names_files
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file",
                        lambda: tmp_path / "no-such-machine-names")
    with pytest.raises(publish_scan.PublishScanRefused, match="BUILDER_MACHINE_NAMES"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


def test_a_missing_shipwright_checkout_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "nope")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


@pytest.mark.parametrize("skip_unknown_repo", [True, False])
def test_an_unknown_repository_with_an_adopter_remote_and_no_checkout_passes_on_both_callers(
        tmp_path, monkeypatch, skip_unknown_repo):
    """An adopter instance has no Shipwright checkout at all, and its own
    repository is never one of publish-engine's five scannable ones —
    `agentctl publish` (`skip_unknown_repo=True`) and the pre-push hook
    (`skip_unknown_repo=False`) must both let such a push through without
    ever needing a checkout to find that out, since there is no engine to
    ask without one. The adopter's own `push_remote` names a repository
    this gate still does not recognize, so it is the ONE thing that can
    make this skip well-founded rather than merely unresolved (see the
    unresolvable-remote test below, which refuses on the exact same
    repository name with no remote to vouch for it)."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    assert publish_scan.scan_commits(
        "widgetco-storefront", tmp_path, ["abc123"], skip_unknown_repo=skip_unknown_repo,
        push_remote="https://github.com/widgetco/storefront.git",
    ) is None


@pytest.mark.parametrize("skip_unknown_repo", [True, False])
def test_a_scannable_repository_with_no_checkout_refuses_on_both_callers(
        tmp_path, monkeypatch, skip_unknown_repo):
    """Unlike an adopter's own repository, one of publish-engine's five
    scannable repositories must still refuse for lack of a checkout, on
    both `agentctl publish` and the pre-push hook — this is exactly the
    scan this gate exists to run, so a missing checkout fails closed
    regardless of `skip_unknown_repo`."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "agent-platform", tmp_path, ["abc123"], skip_unknown_repo=skip_unknown_repo,
        )


def test_an_unresolvable_remote_with_no_checkout_refuses_for_an_unknown_name_too(
        tmp_path, monkeypatch):
    """The adopter-passes test above only passes BECAUSE its `push_remote`
    resolves to a repository this gate still does not recognize. With no
    `push_remote` at all (the default), the exact same unfamiliar
    `repo_name` cannot be vouched for by either read, and this gate fails
    closed on "cannot tell" rather than reading the silence as "safe to
    skip"."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "widgetco-storefront", tmp_path, ["abc123"], skip_unknown_repo=True,
        )


def test_a_scannable_repository_checked_out_under_another_directory_name_still_refuses(
        tmp_path, monkeypatch):
    """A rename or relocation of one of publish-engine's five scannable
    repositories' own checkout changes `repo_name` (read from the
    checkout's current directory name) without ever touching its
    `.git/config` — so `push_remote`, naming the real repository under its
    own GitHub slug, must still refuse a push through that renamed
    checkout, even though `repo_name` itself no longer matches any of the
    five."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "my-renamed-checkout", tmp_path, ["abc123"], skip_unknown_repo=True,
            push_remote="https://github.com/convivy/agent-platform.git",
        )


@pytest.mark.parametrize("repo_name", ["Agent-Platform", "AGENT-PLATFORM", "agent-PLATFORM"])
def test_a_scannable_repository_name_case_variant_with_no_checkout_still_refuses(
        tmp_path, monkeypatch, repo_name):
    """`repo_name` is compared against publish-engine's five scannable
    repositories case-insensitively, so a case variant of one of them (a
    directory renamed with different capitalization, say) still refuses
    for lack of a checkout rather than reading as an unfamiliar name."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(repo_name, tmp_path, ["abc123"], skip_unknown_repo=True)


@pytest.mark.parametrize("push_remote", [
    "https://github.com/Convivy/Agent-Platform.git",
    "https://github.com/CONVIVY/AGENT-PLATFORM",
    "HTTPS://GITHUB.COM/convivy/agent-platform.git",
])
def test_a_scannable_repository_remote_case_variant_with_no_checkout_still_refuses(
        tmp_path, monkeypatch, push_remote):
    """`push_remote`'s own owner/repo slug is compared against publish-
    engine's five scannable repositories case-insensitively too, same as
    `repo_name` — a remote spelled with different capitalization than the
    registry's own `convivy/agent-platform` still names the same GitHub
    repository."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "my-renamed-checkout", tmp_path, ["abc123"], skip_unknown_repo=True,
            push_remote=push_remote,
        )


@pytest.mark.parametrize("push_remote", [
    "https://github.com/convivy/agent%2Dplatform.git",
    "https://github.com/convivy/agent%2dplatform.git",
])
def test_a_percent_encoded_scannable_slug_with_no_checkout_still_refuses(
        tmp_path, monkeypatch, push_remote):
    """Percent-encoding an unreserved character in the push target's
    `owner/repo` path is wire-transparent — `agent%2Dplatform` and
    `agent-platform` name the identical GitHub repository — so a remote
    naming one of the five scannable repositories through either encoding
    of its hyphen must still refuse for lack of a checkout, the same as
    the un-encoded form."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "my-renamed-checkout", tmp_path, ["abc123"], skip_unknown_repo=True,
            push_remote=push_remote,
        )


def test_a_percent_encoded_owner_segment_of_a_scannable_slug_still_refuses(
        tmp_path, monkeypatch):
    """The encoding can land in the owner segment rather than the repo
    segment and the decoded slug still must match: `c%6fnvivy` decodes to
    `convivy`, naming the same scannable repository under its real
    owner."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "my-renamed-checkout", tmp_path, ["abc123"], skip_unknown_repo=True,
            push_remote="https://github.com/c%6fnvivy/agent-platform.git",
        )


def test_an_adopters_percent_encoded_remote_that_decodes_to_an_unscannable_slug_passes(
        tmp_path, monkeypatch):
    """An adopter's own remote can also carry percent-encoding without
    naming one of the five — once decoded, `widgetco/storefront%2Dapp`
    is still a repository this gate does not recognize, so it must skip
    for lack of a checkout exactly as an un-encoded adopter remote
    does."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    assert publish_scan.scan_commits(
        "widgetco-storefront", tmp_path, ["abc123"], skip_unknown_repo=True,
        push_remote="https://github.com/widgetco/storefront%2Dapp.git",
    ) is None


def test_a_remote_the_regex_cannot_parse_with_no_checkout_refuses_for_an_unknown_name_too(
        tmp_path, monkeypatch):
    """A remote on some host other than `github.com` cannot be read by
    `_GITHUB_REMOTE_RE` at all — `_names_a_locally_known_scannable_repo`
    reads that as unresolved, not as "not one of the five", so an
    otherwise-unfamiliar `repo_name` still refuses rather than skipping."""
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: tmp_path / "no-such-shipwright")
    with pytest.raises(publish_scan.PublishScanRefused, match="no Shipwright checkout"):
        publish_scan.scan_commits(
            "my-renamed-checkout", tmp_path, ["abc123"], skip_unknown_repo=True,
            push_remote="https://gitlab.com/convivy/agent-platform.git",
        )


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
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


def test_an_export_that_fails_refuses(tmp_path, monkeypatch):
    checkout = tmp_path / "shipwright"
    (checkout / ".git").mkdir(parents=True)

    def _boom(_checkout):
        raise publish_scan.PublishScanRefused(f"could not export {_checkout} at origin/main: boom")

    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "_export_shipwright_tree", _boom)
    with pytest.raises(publish_scan.PublishScanRefused, match="could not export"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


# --- the gate refuses only what the push ADDS --------------------------------

def test_a_head_that_adds_a_name_is_refused_naming_it(tmp_path, monkeypatch, stubbed, wt_with_base_and_head):
    wt, base, head = wt_with_base_and_head
    ref_map = _write_ref_map(wt, {
        base: (0, _clean_output("agent-platform", base)),
        head: (1, _findings_output("agent-platform", head,
                                    ["lib/thing.py:4: 'madeupcrew', from the operator registry"])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)
    message = str(exc.value)
    assert "madeupcrew" in message
    assert "lib/thing.py:4" in message
    # The base MUST actually be consulted, not only the head — a scan that
    # refused on the head finding alone, the way a whole-tree check would,
    # would pass this same assertion for the wrong reason.
    assert call_log.read_text().splitlines() == [head, base]


def test_a_head_with_the_same_name_at_a_shifted_line_passes(monkeypatch, stubbed, wt_with_base_and_head):
    wt, base, head = wt_with_base_and_head
    ref_map = _write_ref_map(wt, {
        base: (1, _findings_output("agent-platform", base,
                                    ["lib/thing.py:4: 'madeupcrew', from the operator registry"])),
        head: (1, _findings_output("agent-platform", head,
                                    ["lib/thing.py:9: 'madeupcrew', from the operator registry"])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    assert publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True) is None


def test_a_head_that_removes_a_name_passes(monkeypatch, stubbed, wt_with_base_and_head):
    """`head` still carries a finding unrelated to the one it removed —
    agent-platform's own `origin/main` carries thousands today, so a push
    that removes one name while thousands of others sit untouched is the
    ordinary case this gate exists for. A whole-tree check (the old
    behaviour) would refuse this push over the untouched findings alone;
    this gate must not, since the push adds none of them."""
    wt, base, head = wt_with_base_and_head
    persistent = "lib/legacy.py:1: 'madeupotherword', from the operator registry"
    ref_map = _write_ref_map(wt, {
        base: (1, _findings_output("agent-platform", base, [
            "lib/thing.py:4: 'madeupcrew', from the operator registry",
            persistent,
        ])),
        head: (1, _findings_output("agent-platform", head, [persistent])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    assert publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True) is None


def test_scan_commits_refuses_a_commit_that_adds_a_name_even_when_a_later_commit_removes_it(
        monkeypatch, stubbed, tmp_path):
    """`scan_commits` compares each pushed commit to its OWN parent, so the
    commit that introduces a name is caught even when a later commit in the
    same push takes it back out — the name reached origin the instant the
    middle commit did."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    _git(wt, "update-ref", "refs/remotes/origin/main", root)
    (wt / "f.txt").write_text("adds a name\n")
    adds = _commit(wt, "adds")
    (wt / "f.txt").write_text("removes it again\n")
    removes = _commit(wt, "removes")

    ref_map = _write_ref_map(wt, {
        root: (0, _clean_output("agent-platform", root)),
        adds: (1, _findings_output("agent-platform", adds,
                                    ["lib/thing.py:1: 'madeupcrew', from the operator registry"])),
        removes: (0, _clean_output("agent-platform", removes)),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [adds, removes])
    assert "madeupcrew" in str(exc.value)
    assert adds[:9] in str(exc.value)


def test_scan_commits_refuses_a_merge_commit_that_itself_adds_a_name_neither_parent_has(
        monkeypatch, stubbed, tmp_path):
    """An "evil merge": the merge commit's own tree carries a name that is in
    NEITHER of its two parents' trees. Comparing a merge only to its first
    parent would still happen to catch this particular shape, since the key
    is new relative to that parent too — this pins the general rule a
    single-parent comparison cannot express on its own: a finding is refused
    only when it is absent from EVERY parent, so this test fails if
    `scan_commits` is ever narrowed back to comparing a merge to just one of
    its parents."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    _git(wt, "update-ref", "refs/remotes/origin/main", root)
    _git(wt, "branch", "other", "main")
    (wt / "f.txt").write_text("main side\n")
    main_tip = _commit(wt, "main side")
    _git(wt, "checkout", "-q", "other")
    (wt / "g.txt").write_text("other side\n")
    other_tip = _commit(wt, "other side")
    _git(wt, "checkout", "-q", "main")
    merge = _merge(wt, "--no-edit", "--no-ff", "other")
    assert merge.returncode == 0, merge.stderr
    merge_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    finding = "lib/thing.py:1: 'madeupcrew', from the operator registry"
    ref_map = _write_ref_map(wt, {
        main_tip: (0, _clean_output("agent-platform", main_tip)),
        other_tip: (0, _clean_output("agent-platform", other_tip)),
        merge_sha: (1, _findings_output("agent-platform", merge_sha, [finding])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [merge_sha], skip_unknown_repo=True)
    assert "madeupcrew" in str(exc.value)
    # Both parents were actually consulted, not just one.
    assert set(call_log.read_text().splitlines()) == {merge_sha, main_tip, other_tip}


# --- the "pass --ref HEAD" notice, printed ahead of a findings report whenever
# --- --ref names a commit other than the checkout's actual HEAD, must not
# --- make `_parse_findings` read that report's own header as a finding -------

def test_scan_commits_passes_when_head_only_repeats_the_base_finding_with_the_notice_on_both_scans(
        monkeypatch, stubbed, wt_with_base_and_head):
    """Both `base` and `head` are scanned with `--ref` naming a commit other
    than the checkout's actual HEAD (neither is `checkout_head` below), so
    both scans carry the real engine's "pass --ref HEAD" notice ahead of
    their own findings header. `head` repeats exactly the one finding
    `base` already has and adds nothing, so this must pass."""
    wt, base, head = wt_with_base_and_head
    checkout_head = "f" * 40
    finding = "lib/thing.py:4: 'madeupcrew', from the operator registry"
    ref_map = _write_ref_map(wt, {
        base: (1, _findings_output("agent-platform", base, [finding], real_head=checkout_head)),
        head: (1, _findings_output("agent-platform", head, [finding], real_head=checkout_head)),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    assert publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True) is None


def test_scan_commits_refuses_only_the_new_finding_with_the_notice_on_both_scans(
        monkeypatch, stubbed, wt_with_base_and_head):
    """Same setup as above, but `head` adds one finding `base` does not
    have. The refusal must name only the new finding, not the inherited
    one, and must still begin with the engine's own report line(s) — here,
    the notice followed by the findings header, carried forward unchanged."""
    wt, base, head = wt_with_base_and_head
    checkout_head = "f" * 40
    inherited = "lib/legacy.py:1: 'madeupotherword', from the operator registry"
    new_finding = "lib/thing.py:4: 'madeupcrew', from the operator registry"
    ref_map = _write_ref_map(wt, {
        base: (1, _findings_output("agent-platform", base, [inherited], real_head=checkout_head)),
        head: (1, _findings_output("agent-platform", head, [inherited, new_finding],
                                    real_head=checkout_head)),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)
    message = str(exc.value)
    assert new_finding in message
    assert inherited not in message
    lines = message.splitlines()
    assert lines[0].endswith("publish-engine: the checkout's HEAD is at "
                              f"{checkout_head[:9]}, but this run scans {head} at {head[:9]}; "
                              "pass --ref HEAD to scan the checkout instead.")
    assert "would be refused by a release" in lines[1]
    assert f"1 at {head[:9]}, 0 at {base[:9]}" in message


def test_the_refusal_names_m_at_head_n_at_base_for_a_key_whose_count_rose(
        monkeypatch, stubbed, wt_with_base_and_head):
    """A key present at BOTH base and head, but more often at head, must
    still refuse — an existing finding, repeated an extra time, is still new
    content the push adds. The refusal must report the two counts against
    their own short shas, not repeat every one of the head's lines as
    though the gate were counting content that was already there."""
    wt, base, head = wt_with_base_and_head
    line = "lib/thing.py:{}: 'madeupcrew', from the operator registry"
    ref_map = _write_ref_map(wt, {
        base: (1, _findings_output("agent-platform", base, [line.format(4)])),
        head: (1, _findings_output("agent-platform", head,
                                    [line.format(4), line.format(9)])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)
    message = str(exc.value)
    assert f"2 at {head[:9]}, 1 at {base[:9]}" in message
    # Both of the head's own lines are kept; the base's line never appears.
    assert line.format(4) in message
    assert line.format(9) in message


def test_scan_commits_passes_when_a_non_tip_commit_inherits_its_findings_with_the_notice(
        monkeypatch, stubbed, tmp_path):
    """`scan_commits` scans every pushed commit, not only the tip, so the
    non-tip commit `c1` here is scanned with `--ref` naming a commit other
    than the checkout's actual HEAD (`c2`, the tip) — both `c1`'s own scan
    and its base (`root`, `c1`'s first parent) carry the notice. `c1` adds
    nothing beyond what `root` already has, and `c2` scans clean, so this
    must pass."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    _git(wt, "update-ref", "refs/remotes/origin/main", root)
    (wt / "f.txt").write_text("c1\n")
    c1 = _commit(wt, "c1")
    (wt / "f.txt").write_text("c2\n")
    c2 = _commit(wt, "c2")

    finding = "lib/thing.py:1: 'madeupcrew', from the operator registry"
    ref_map = _write_ref_map(wt, {
        root: (1, _findings_output("agent-platform", root, [finding], real_head=c2)),
        c1: (1, _findings_output("agent-platform", c1, [finding], real_head=c2)),
        c2: (0, _clean_output("agent-platform", c2, real_head=c2)),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    assert publish_scan.scan_commits("agent-platform", wt, [c1, c2]) is None


def test_a_publish_engine_shaped_line_after_the_findings_body_still_fails_closed(
        monkeypatch, stubbed, wt_with_base_and_head):
    """`_parse_findings` only skips a LEADING run of "publish-engine: "
    lines. A line shaped like one of the engine's own report lines
    appearing AFTER the findings body (something no real engine would ever
    print, but this gate must not assume) must still fail closed, not be
    read as a second header or silently dropped."""
    wt, base, head = wt_with_base_and_head
    malformed = "\n".join([
        _preamble("agent-platform", head),
        "  content scan (1):",
        "    tests/fixture.py:3: an email address",
        "publish-engine: a line that should never follow the findings body",
    ])
    ref_map = _write_ref_map(wt, {head: (1, malformed)})
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused, match="does not recognise"):
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)


def test_an_unparseable_finding_line_fails_closed(monkeypatch, stubbed, wt_with_base_and_head):
    """A line in the findings block that matches neither of the two shapes
    the five detectors print today must refuse rather than be silently
    dropped from the base/head comparison — dropping it could turn a real
    new finding into an empty diff."""
    wt, base, head = wt_with_base_and_head
    malformed = "\n".join([
        _preamble("agent-platform", head),
        "  some detector (1):",
        "    this line has no colon separating a path from anything",
    ])
    ref_map = _write_ref_map(wt, {head: (1, malformed)})
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused, match="cannot parse"):
        publish_scan.scan_commits("agent-platform", wt, [head], skip_unknown_repo=True)


# --- scan_commits: one scan per commit, stopping at the first refusal -------

def test_scan_commits_stops_at_the_first_refusal_and_names_it_k_of_n(
        tmp_path, monkeypatch, stubbed):
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    _git(wt, "update-ref", "refs/remotes/origin/main", root)
    (wt / "f.txt").write_text("c1\n")
    c1 = _commit(wt, "c1")
    (wt / "f.txt").write_text("c2\n")
    c2 = _commit(wt, "c2")
    (wt / "f.txt").write_text("c3\n")
    c3 = _commit(wt, "c3")
    shas = [c1, c2, c3]

    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    ref_map = _write_ref_map(wt, {
        c1: (0, _clean_output("agent-platform", c1)),
        c2: (1, _findings_output("agent-platform", c2,
                                  ["lib/thing.py:1: 'madeupcrew', from the operator registry"])),
    })
    monkeypatch.setenv("FAKE_ENGINE_REF_MAP", str(ref_map))
    with pytest.raises(publish_scan.PublishScanRefused) as exc:
        publish_scan.scan_commits("agent-platform", wt, shas)
    message = str(exc.value)
    assert "agent-platform" in message
    assert c2[:9] in message
    assert "2 of 3" in message
    # c1 scans clean against its own parent (root) and is never re-scanned as
    # a base. c2 has findings, so its own first parent — c1 — is scanned a
    # second time, as c2's base. c3 is never reached once c2 is refused.
    assert call_log.read_text().splitlines() == [c1, c2, c1]


def test_scan_commits_returns_when_every_commit_passes(tmp_path, monkeypatch, stubbed):
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    shas = ["1" * 40, "2" * 40]
    assert publish_scan.scan_commits("agent-platform", tmp_path, shas) is None
    assert call_log.read_text().splitlines() == shas


# --- `commits_new_to_remote` + `scan_commits`: the exact two calls
# --- `agentctl publish`'s own `_content_scan_gate` makes, so a push that
# --- merges in a name `origin/main` already carries passes the same way the
# --- pre-push hook's own version of this does (bin/push-content-gate,
# --- `tests/test_push_content_gate.py`'s equivalent tests) -------------------

def test_publishs_own_scan_passes_a_branch_merging_in_a_name_already_on_main(
        monkeypatch, stubbed, tmp_path):
    """The `agentctl publish` shape of the fix: a branch already published,
    merging in a commit `origin/main` already carries a name in, must pass.
    Before the fix, `scan_commit`'s single comparison against the
    merge-base of `head` and `origin/main` would have read every commit
    `main` gained since the branch forked as newly added by the merge."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("base\n")
    base = _commit(wt, "base")
    _git(wt, "update-ref", "refs/remotes/origin/main", base)
    _git(wt, "checkout", "-q", "-b", "feature")
    (wt / "g.txt").write_text("feature\n")
    feature_tip = _commit(wt, "feature work")
    _git(wt, "update-ref", "refs/remotes/origin/feature", feature_tip)

    _git(wt, "checkout", "-q", "main")
    (wt / "f.txt").write_text("adds a marker on main\n")
    main_marker = _commit(wt, "adds a marker on main")
    _git(wt, "update-ref", "refs/remotes/origin/main", main_marker)

    _git(wt, "checkout", "-q", "feature")
    merge = _merge(wt, "--no-edit", "main")
    assert merge.returncode == 0, merge.stderr
    merge_sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_ENGINE_FAIL_REF", main_marker)

    shas = publish_scan.commits_new_to_remote(wt, merge_sha, feature_tip, "origin", "feature")
    assert shas == [merge_sha]
    assert publish_scan.scan_commits("agent-platform", wt, shas, skip_unknown_repo=True) is None
    # main_marker, already on origin/main, was never asked about at all.
    assert call_log.read_text().splitlines() == [merge_sha]


def test_publishs_own_scan_passes_one_new_clean_commit_on_an_already_published_branch(
        monkeypatch, stubbed, tmp_path):
    """The same control as the pre-push hook's own version of this case: a
    branch already published, carrying a name from before the gate existed
    (agent-platform's own `origin/main` does, by the thousand), gets one
    new, clean commit on top — that commit adds nothing, so the push must
    pass, and the already-published commit must never be re-scanned."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("base\n")
    base = _commit(wt, "base")
    _git(wt, "update-ref", "refs/remotes/origin/main", base)
    (wt / "f.txt").write_text("clean follow-up\n")
    new_sha = _commit(wt, "clean follow-up")

    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_ENGINE_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_ENGINE_FAIL_REF", base)

    shas = publish_scan.commits_new_to_remote(wt, new_sha, base, "origin", "main")
    assert shas == [new_sha]
    assert publish_scan.scan_commits("agent-platform", wt, shas, skip_unknown_repo=True) is None
    assert call_log.read_text().splitlines() == [new_sha]


# --- a replace ref or a graft, not just a planted tracking ref, can hide a
# --- commit from this module's own git plumbing (security review, PR #1002
# --- round 1, non-blocking): `refs/replace/<oid>` and `.git/info/grafts` are
# --- both local-only rewrites `git push`'s own transport never honours, so a
# --- dirty commit genuinely reaching the remote could still be read, by this
# --- module's OWN git calls, as a clean stand-in or as having a different
# --- parent than it really does. `no_replace_env` (`GIT_NO_REPLACE_OBJECTS`
# --- and `GIT_GRAFT_FILE`) is what closes this, layered into `_clean_git_env`
# --- so every git call in this module carries it. ---------------------------

def _orphan_commit(wt: Path, branch: str, filename: str, content: str, message: str) -> str:
    """A new root commit (no parent) on a fresh orphan branch, leaving `main`
    checked out afterward — a real, independent object to replace or graft a
    boundary commit onto, standing in for an attacker's forged stand-in."""
    _git(wt, "checkout", "-q", "--orphan", branch)
    _git(wt, "rm", "-rf", "-q", ".")
    (wt / filename).write_text(content)
    sha = _commit(wt, message)
    _git(wt, "checkout", "-q", "main")
    return sha


def test_commits_new_to_remote_is_not_fooled_by_a_replaced_boundary_commit(tmp_path):
    """`commits_new_to_remote`'s own `rev-list ... --not <remote_sha> ...` is
    git plumbing, which substitutes a `refs/replace/<oid>` entry's content
    for the real object's — including a replaced commit's reported PARENTS,
    during the very walk that decides what is "reachable from the boundary"
    and so already excluded — unless `GIT_NO_REPLACE_OBJECTS` is set.
    `remote_sha` here (`boundary`) is genuinely unrelated to `head` (`x`);
    replacing `boundary` with a forged commit whose own parent IS `x` makes
    that walk read `x` as reachable from the boundary too, dropping it (and
    its real parent) from the result even though `x` never descends from
    `boundary` at all. `run_sandbox.py`'s write protection for a dispatched
    run's own linked worktree is built by enumerating paths that exist when
    the sandbox is set up, so a NEW `refs/replace/<oid>` (created after that
    enumeration) is not covered by it either. Confirmed by hand, outside
    this harness, against this exact setup before `no_replace_env` existed:
    the plain `git rev-list` command this function runs returned empty."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    boundary = _orphan_commit(wt, "boundary-branch", "g.txt", "boundary\n", "boundary")

    forged = _commit_tree(wt, f"{boundary}^{{tree}}", x, "forged")
    _git(wt, "replace", boundary, forged)

    shas = publish_scan.commits_new_to_remote(wt, x, boundary, "origin", None)
    assert shas == [root, x], (
        "the replaced boundary must not hide x, or its own real parent, from "
        "the commits-new-to-remote scan"
    )


def test_commits_new_to_remote_is_not_fooled_by_a_grafted_boundary_commit(tmp_path):
    """The same attack as above, via `.git/info/grafts` instead of `git
    replace` — a separate, legacy mechanism `GIT_NO_REPLACE_OBJECTS` does
    NOT disable (confirmed by hand: with only `GIT_NO_REPLACE_OBJECTS` set,
    a graft still fooled a plain `git rev-list` against this exact setup);
    `GIT_GRAFT_FILE` pointed at an empty file is what closes this one."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    boundary = _orphan_commit(wt, "boundary-branch", "g.txt", "boundary\n", "boundary")

    (wt / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (wt / ".git" / "info" / "grafts").write_text(f"{boundary} {x}\n")

    shas = publish_scan.commits_new_to_remote(wt, x, boundary, "origin", None)
    assert shas == [root, x], (
        "the grafted boundary must not hide x, or its own real parent, from "
        "the commits-new-to-remote scan"
    )


def test_parents_resists_a_replaced_commit_reporting_a_different_parent(tmp_path):
    """The OTHER vector `_parents` itself is exposed to: a replace ref on the
    commit being scanned, not on a boundary — `scan_commits` would then
    compare `x`'s real findings against the REPLACEMENT'S parent rather than
    `x`'s own real one, letting an operator-chosen "clean" stand-in's parent
    serve as the baseline for content that was never actually checked
    against it. Replacing `x` with a parentless (orphan) commit makes a
    naive `rev-parse x^@` report no parents at all; `_parents` must still
    report `x`'s REAL parent, `root`."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    clean_standin = _orphan_commit(wt, "clean-branch", "g.txt", "clean\n", "clean stand-in")

    _git(wt, "replace", x, clean_standin)
    assert publish_scan._parents(wt, x) == [root]


def test_parents_resists_a_grafted_commit_reporting_a_different_parent(tmp_path):
    """The graft version of the test above."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    clean_standin = _orphan_commit(wt, "clean-branch", "g.txt", "clean\n", "clean stand-in")

    (wt / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (wt / ".git" / "info" / "grafts").write_text(f"{x} {clean_standin}\n")
    assert publish_scan._parents(wt, x) == [root]


# --- the resolvers' own defaults and operator-config overrides ----------------

def test_commits_new_to_remote_does_not_trust_a_plain_head_ref(tmp_path):
    """THE ROUND-2 GAP (security review, PR #1002 round 2), at the function
    level: every ref under `refs/remotes/origin/*` is a candidate for
    `_trusted_tracking_shas`'s sweep, `refs/remotes/origin/HEAD` included
    like any other name under that namespace, so `commits_new_to_remote`
    itself must not trust whatever is sitting there, independent of whether
    a refresh ran first — the ordinary callers always refresh immediately
    before calling this, but a plain ref decoy here happens to also be
    pruned away by that refresh's own `--prune` (unlike the symbolic form
    the next test covers, confirmed by hand to survive it), so this test
    calls the function directly, with no refresh in between, to pin the
    function's own contract rather than relying on that refresh behavior.
    A plain ref planted at `origin/HEAD`, pointing at `x` with no relation
    to `remote_sha` at all, would otherwise hide `x` (and its real parent)
    from this scan exactly like the replaced/grafted boundary attacks
    above."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    boundary = _orphan_commit(wt, "boundary-branch", "g.txt", "boundary\n", "boundary")
    _git(wt, "update-ref", "refs/remotes/origin/HEAD", x)

    shas = publish_scan.commits_new_to_remote(wt, x, boundary, "origin", None)
    assert shas == [root, x], (
        "a plain refs/remotes/origin/HEAD decoy must not hide x, or its own "
        "real parent, from the commits-new-to-remote scan"
    )


def test_commits_new_to_remote_does_not_trust_a_symbolic_head_ref(tmp_path):
    """The symref form of the gap above: `refs/remotes/origin/HEAD` as a
    SYMBOLIC ref pointing at the push's own destination tracking ref
    (`<remote_name>/<dest>`, already excluded on its own account) rather
    than a plain ref. Excluding `origin/<dest>` by name does not also
    exclude `origin/HEAD` pointing at it — `origin/HEAD` is a distinct ref
    that independently matches `_trusted_tracking_shas`'s sweep and
    dereferences to the same commit, re-trusting it through the symref
    unless `origin/HEAD` is excluded by name in its own right (which it is,
    on top of the general symbolic-ref exclusion the next test pins)."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    _git(wt, "update-ref", "refs/remotes/origin/main", x)
    _git(wt, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    shas = publish_scan.commits_new_to_remote(wt, x, None, "origin", "main")
    assert shas == [root, x], (
        "a symbolic origin/HEAD pointing at the push's own excluded "
        "destination ref must not re-trust it through that back door"
    )


def test_commits_new_to_remote_does_not_trust_a_symbolic_ref_under_another_name(tmp_path):
    """THE ROUND-3 GAP (reviewer, PR #1002 round 4): round 4's own fix
    excluded `refs/remotes/origin/HEAD` by name, but ANY symbolic ref under
    `refs/remotes/origin/*` survives `refresh_remote_tracking_refs`'s own
    `--prune` untouched (confirmed by hand against a real fetch —
    `_trusted_tracking_shas`'s own docstring), not only one planted at
    `HEAD`. A dispatched run that plants a symbolic ref at any OTHER name,
    `refs/remotes/origin/zzz-decoy` here, pointing at a local tag on `x`,
    must not have that commit (or its real parent) read as already known.
    Fails on 98c89eee1: `--exclude=origin/HEAD` lets `--remotes=origin`
    sweep `origin/zzz-decoy` in regardless, dereferencing to `x` and
    dropping it from the result."""
    wt = tmp_path / "wt"
    sp.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    (wt / "f.txt").write_text("root\n")
    root = _commit(wt, "root")
    (wt / "f.txt").write_text("x\n")
    x = _commit(wt, "x")
    boundary = _orphan_commit(wt, "boundary-branch", "g.txt", "boundary\n", "boundary")
    _git(wt, "tag", "decoy-target", x)
    _git(wt, "symbolic-ref", "refs/remotes/origin/zzz-decoy", "refs/tags/decoy-target")

    shas = publish_scan.commits_new_to_remote(wt, x, boundary, "origin", None)
    assert shas == [root, x], (
        "a symbolic decoy under a name other than HEAD must not hide x, or "
        "its own real parent, from the commits-new-to-remote scan"
    )


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
    exercise `_export_shipwright_tree`'s actual `git archive` subprocess, never a stub.

    `origin` is a real remote, added pointing at the checkout itself, with `refs/heads/main`
    set to the same commit as `refs/remotes/origin/main` below — `_export_shipwright_tree`
    now fetches `origin`'s `main` before archiving (this work item), so a caller exercising
    its `git archive` alone, with no interest in the fetch, needs that fetch to find a real
    `origin` remote with a real `main` branch to succeed against, even though fetching a
    checkout from itself changes nothing it already has."""
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
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/heads/main", "HEAD"], check=True)
    sp.run(["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)], check=True)
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


# --- extraction on a Python without `tarfile`'s `filter` keyword (CQ: Debian 12
# ships 3.11.2; the keyword arrived in 3.11.4) -------------------------------

def test_export_shipwright_tree_extracts_a_real_archive_when_the_data_filter_is_unsupported(
        tmp_path, monkeypatch):
    """Python 3.11.2 — Debian 12's own packaged Python, and the minimum this gate
    supports — has no `tarfile.data_filter`: the `filter` keyword `extractall` needs to
    request it arrived only in 3.11.4, and passing it anyway raises `TypeError` before
    extraction starts. `_export_shipwright_tree` must still extract a real, ordinary
    archive end to end on such a Python."""
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    checkout = _real_shipwright_checkout(tmp_path)
    dest = publish_scan._export_shipwright_tree(checkout)
    assert (dest / "bin" / "publish-engine").is_file()


def test_export_shipwright_tree_never_passes_the_filter_keyword_when_unsupported(
        tmp_path, monkeypatch):
    """A stricter check than the test above: rather than merely observing that
    extraction succeeds (which it would on THIS Python even if `filter` were passed,
    since this Python actually has `data_filter`), this makes `TarFile.extractall`
    itself raise exactly the `TypeError` Python 3.11.2 raises when handed a `filter` it
    does not understand, so the test only passes if `_export_shipwright_tree`'s fallback
    branch truly never passes the keyword at all."""
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    real_extractall = tarfile.TarFile.extractall

    def rejects_filter_kwarg(self, path=".", members=None, *, numeric_owner=False, filter=None):
        if filter is not None:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real_extractall(self, path, members, numeric_owner=numeric_owner)

    monkeypatch.setattr(tarfile.TarFile, "extractall", rejects_filter_kwarg)
    checkout = _real_shipwright_checkout(tmp_path)
    dest = publish_scan._export_shipwright_tree(checkout)
    assert (dest / "bin" / "publish-engine").is_file()


def test_export_shipwright_tree_refuses_a_member_that_escapes_the_destination_when_unsupported(
        tmp_path, monkeypatch):
    """With no `tarfile.data_filter` to lean on, `_export_shipwright_tree` must check
    every member by hand (`_refuse_unsafe_tar_member`) before extracting any of them —
    this builds a synthetic archive (never a real Shipwright export) holding one member
    whose name is a `../` escape, and confirms the gate refuses it and removes its own
    temp directory, rather than extracting it outside the destination."""
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    monkeypatch.setattr(publish_scan, "_fetch_shipwright_main", lambda checkout: None)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"synthetic escape payload\n"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    tar_bytes = buf.getvalue()

    checkout = tmp_path / "shipwright"
    checkout.mkdir()

    def fake_git_archive(args, **kwargs):
        assert args[:4] == ["git", "-C", str(checkout), "archive"]
        return sp.CompletedProcess(args, 0, stdout=tar_bytes, stderr=b"")

    monkeypatch.setattr(publish_scan.subprocess, "run", fake_git_archive)

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)

    with pytest.raises(publish_scan.PublishScanRefused,
                        match="resolves outside the extraction directory"):
        publish_scan._export_shipwright_tree(checkout)
    assert created and not Path(created[0]).exists()


# --- `_refuse_unsafe_tar_member` itself, every branch (the stand-in the test above
# drives only one branch of, via a full `_export_shipwright_tree` call) -------------

def _dest(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    dest.mkdir()
    return dest


def test_refuse_unsafe_tar_member_refuses_an_absolute_name(tmp_path):
    member = tarfile.TarInfo(name="/etc/passwd")
    with pytest.raises(publish_scan.PublishScanRefused, match="absolute member name"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_character_device(tmp_path):
    member = tarfile.TarInfo(name="dev/null")
    member.type = tarfile.CHRTYPE
    with pytest.raises(publish_scan.PublishScanRefused, match="device or FIFO"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_block_device(tmp_path):
    member = tarfile.TarInfo(name="dev/sda")
    member.type = tarfile.BLKTYPE
    with pytest.raises(publish_scan.PublishScanRefused, match="device or FIFO"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_fifo(tmp_path):
    member = tarfile.TarInfo(name="a-fifo")
    member.type = tarfile.FIFOTYPE
    with pytest.raises(publish_scan.PublishScanRefused, match="device or FIFO"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_symlink_escaping_the_destination(tmp_path):
    member = tarfile.TarInfo(name="inside/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../outside.txt"
    with pytest.raises(publish_scan.PublishScanRefused,
                        match="link leaving the extraction directory"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_hardlink_escaping_the_destination(tmp_path):
    member = tarfile.TarInfo(name="inside/link")
    member.type = tarfile.LNKTYPE
    member.linkname = "../outside.txt"
    with pytest.raises(publish_scan.PublishScanRefused,
                        match="link leaving the extraction directory"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_symlink_with_an_absolute_target(tmp_path):
    member = tarfile.TarInfo(name="link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    with pytest.raises(publish_scan.PublishScanRefused, match="absolute target"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_hardlink_with_an_absolute_target(tmp_path):
    member = tarfile.TarInfo(name="link")
    member.type = tarfile.LNKTYPE
    member.linkname = "/etc/passwd"
    with pytest.raises(publish_scan.PublishScanRefused, match="absolute target"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_refuse_unsafe_tar_member_refuses_a_member_type_outside_the_allowlist(tmp_path):
    """`data_filter` itself (`_get_filtered_attrs`'s own `SpecialFileError`) refuses
    any member that is not a regular file, a directory, a symlink or a hardlink —
    `GNUTYPE_LONGNAME` stands in here for that whole remaining category: it is a real
    tar type byte, and is none of the four this gate allows through."""
    member = tarfile.TarInfo(name="an-exotic-member")
    member.type = tarfile.GNUTYPE_LONGNAME
    with pytest.raises(publish_scan.PublishScanRefused, match="a type data_filter refuses"):
        publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


@pytest.mark.parametrize("member_type", [
    tarfile.REGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE,
])
def test_refuse_unsafe_tar_member_allows_every_type_data_filter_allows(tmp_path, member_type):
    """The allowlist's complement: a regular file, a directory, and a symlink or
    hardlink whose target stays inside `dest` are exactly what `data_filter` lets
    through, and none of them raises here."""
    member = tarfile.TarInfo(name="inside/member")
    member.type = member_type
    if member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        member.linkname = "sibling.txt"
    publish_scan._refuse_unsafe_tar_member(tmp_path / "shipwright", _dest(tmp_path), member)


def test_the_engine_subprocess_never_sees_a_git_dir_inherited_from_the_parent(
        tmp_path, monkeypatch, stubbed):
    """Unlike the two tests above, which drive `_export_shipwright_tree`'s own `git
    archive` directly, this drives `scan_commits`'s OTHER subprocess call — the engine
    itself — with `GIT_DIR` set in the parent environment the way a push-time hook
    invoked by git would inherit it, pointed at a repository that has nothing to do
    with the one being scanned."""
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated" / ".git"))
    assert publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True) is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["GIT_DIR"] == ""


# --- Shipwright's #254 deleted the release name scan (CQ·609) ---------------

def test_a_clean_scan_runs_with_no_unchecked_words_and_no_registry_forwarded(
        tmp_path, monkeypatch, stubbed):
    """Shipwright's #254 deleted the release name scan, and with it every
    read of BUILDER_UNCHECKED_WORDS and PROJECTS_JSON in publish-engine and
    tools/release; the stub export `stubbed` builds has no tools/release at
    all, and this patches `projects.raw_document` to blow up if ever called,
    standing in for the registry being unreachable. The scan still runs the
    stub engine and returns clean, forwarding neither of the two removed
    inputs while still forwarding the two that remain."""
    import projects

    def _unreachable():
        raise RuntimeError("the registry must not be read by this scan")

    monkeypatch.setattr(projects, "raw_document", _unreachable)
    _checkout, export_root, _private, _machine = stubbed
    assert not (export_root / "tools" / "release").exists()
    dump = tmp_path / "args-dump.txt"
    monkeypatch.setenv("FAKE_ENGINE_ARG_DUMP", str(dump))
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.delenv("BUILDER_UNCHECKED_WORDS", raising=False)
    assert publish_scan.scan_commits(
        "agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True,
    ) is None
    lines = dict(line.split("=", 1) for line in dump.read_text().splitlines())
    assert lines["HAS_PROJECTS_JSON"] == "False"
    assert lines["HAS_BUILDER_UNCHECKED_WORDS"] == "False"
    assert lines["HAS_BUILDER_PRIVATE_NAMES"] == "True"
    assert lines["HAS_BUILDER_MACHINE_NAMES"] == "True"


# --- `_export_shipwright_tree` fetches Shipwright's own origin/main before --
# --- archiving it (2026-10-31: after a Shipwright merge at 15:43:07Z, every
# --- `agentctl publish` refused for hours on a finding that merge had already
# --- fixed, because nothing here ever fetched this checkout). A fake `git`
# --- binary, prepended onto PATH, forwards every command to the real `git`
# --- except `fetch`, which it records and can fail or hang on cue — the same
# --- env-var-controlled-fake pattern `_FAKE_ENGINE` above uses for
# --- publish-engine, applied to git itself so these tests see the exact argv
# --- `_fetch_shipwright_main` sends a real subprocess, never a canned
# --- `subprocess.run` return value standing in for one. ---------------------

_FAKE_GIT = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os
    import subprocess
    import sys
    import time

    args = sys.argv[1:]


    def _subcommand(argv):
        i = 0
        while i < len(argv):
            if argv[i] == "-C":
                i += 2
                continue
            if argv[i].startswith("-"):
                i += 1
                continue
            return argv[i]
        return None

    sub = _subcommand(args)

    if sub == "fetch":
        call_log = os.environ.get("FAKE_GIT_FETCH_CALL_LOG")
        if call_log:
            with open(call_log, "a") as f:
                f.write(" ".join(args) + "\\n")
                f.write(f"GIT_TERMINAL_PROMPT={os.environ.get('GIT_TERMINAL_PROMPT', '')}\\n")
                f.write(f"GIT_CONFIG_NOSYSTEM={os.environ.get('GIT_CONFIG_NOSYSTEM', '')}\\n")
                f.write(f"GIT_CONFIG_GLOBAL={os.environ.get('GIT_CONFIG_GLOBAL', '')}\\n")
                f.write(f"GIT_SSH_COMMAND={os.environ.get('GIT_SSH_COMMAND', '')}\\n")
                f.write(f"GIT_SSH={os.environ.get('GIT_SSH', '')}\\n")
                f.write(f"GIT_PROXY_COMMAND={os.environ.get('GIT_PROXY_COMMAND', '')}\\n")
        if os.environ.get("FAKE_GIT_FETCH_FAIL"):
            sys.stderr.write("fake git fetch: refused by the fake remote\\n")
            sys.exit(128)
        if os.environ.get("FAKE_GIT_FETCH_HANG"):
            time.sleep(float(os.environ["FAKE_GIT_FETCH_HANG_SECONDS"]))
            sys.exit(0)

    order_log = os.environ.get("FAKE_GIT_ORDER_LOG")
    if order_log and sub in ("fetch", "archive"):
        with open(order_log, "a") as f:
            f.write(sub + "\\n")

    real_git = os.environ["FAKE_GIT_REAL_GIT"]
    result = subprocess.run([real_git] + args)
    sys.exit(result.returncode)
''')


def _install_fake_git(tmp_path: Path, monkeypatch) -> None:
    """Prepend a directory holding `_FAKE_GIT`, named `git`, onto `PATH` — every
    bare `["git", ...]` subprocess call in `publish_scan` (there is no other
    kind) resolves to it instead of the real binary, which it finds itself via
    `FAKE_GIT_REAL_GIT` (resolved BEFORE `PATH` is overridden) and defers to
    for everything this file does not drive directly."""
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "fake-git-bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(_FAKE_GIT)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_GIT_REAL_GIT", real_git)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _real_shipwright_checkout_with_scan_engine(root: Path) -> Path:
    """Like `_real_shipwright_checkout`, but `bin/publish-engine` is the real,
    env-var-driven `_FAKE_ENGINE` script rather than an empty stand-in, so a
    test can run `scan_commits` itself end to end against this checkout
    instead of stubbing `_export_shipwright_tree` outright."""
    checkout = root / "shipwright"
    sp.run(["git", "init", "-q", "-b", "trunk", str(checkout)], check=True)
    engine = checkout / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True)
    engine.write_text(_FAKE_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    sp.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    sp.run(["git", "-C", str(checkout), "-c", "user.email=t@t.example", "-c", "user.name=t",
            "commit", "-q", "-m", "x"], check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", "HEAD"],
           check=True)
    sp.run(["git", "-C", str(checkout), "update-ref", "refs/heads/main", "HEAD"], check=True)
    sp.run(["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)], check=True)
    return checkout


def test_agentctl_run_id_skips_the_fetch_and_the_engine_still_runs(tmp_path, monkeypatch, names_files):
    """A dispatched run's own sandbox denies a write into the Shipwright checkout
    (`run_sandbox.py`), so `_fetch_shipwright_main` must not even try the fetch
    when `AGENTCTL_RUN_ID` names one — but the scan itself must still run,
    against `origin/main` exactly as this checkout already has it."""
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-test-skip-fetch")
    checkout = _real_shipwright_checkout_with_scan_engine(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    order_log = tmp_path / "order.log"
    monkeypatch.setenv("FAKE_GIT_ORDER_LOG", str(order_log))
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)

    assert publish_scan.scan_commits(
        "agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True,
    ) is None

    assert order_log.read_text().splitlines() == ["archive"]


def test_without_agentctl_run_id_the_fetch_runs(tmp_path, monkeypatch, names_files):
    """The counterpart of the test above: with `AGENTCTL_RUN_ID` unset, the fetch
    still runs before the archive, same as before this work item."""
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout = _real_shipwright_checkout_with_scan_engine(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    order_log = tmp_path / "order.log"
    monkeypatch.setenv("FAKE_GIT_ORDER_LOG", str(order_log))
    monkeypatch.delenv("FAKE_ENGINE_FINDINGS", raising=False)
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)

    assert publish_scan.scan_commits(
        "agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True,
    ) is None

    assert order_log.read_text().splitlines() == ["fetch", "archive"]


def _real_shipwright_checkout_with_remote(root: Path) -> tuple[Path, Path]:
    """A real Shipwright checkout (`_real_shipwright_checkout`'s own shape, on branch
    `trunk`) plus a SEPARATE bare repository standing in for its `origin`, with a
    `main` branch the checkout's own `origin/main` has not seen yet — the shape that
    makes `_fetch_shipwright_main`'s own fetch the only thing that can bring the
    checkout's `origin/main` up to date."""
    checkout = _real_shipwright_checkout(root)
    remote = root / "shipwright-origin.git"
    sp.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    push_from = root / "shipwright-push-from"
    sp.run(["git", "clone", "-q", str(checkout), str(push_from)], check=True)
    sp.run(["git", "-C", str(push_from), "checkout", "-q", "-b", "main"], check=True)
    (push_from / "bin" / "publish-engine").write_text("#!/usr/bin/env python3\n# updated\n")
    sp.run(["git", "-C", str(push_from), "add", "-A"], check=True)
    sp.run(["git", "-C", str(push_from), "-c", "user.email=t@t.example", "-c", "user.name=t",
            "commit", "-q", "-m", "updated main"], check=True)
    sp.run(["git", "-C", str(push_from), "push", "-q", str(remote), "main:main"], check=True)
    sp.run(["git", "-C", str(checkout), "remote", "set-url", "origin", str(remote)], check=True)
    return checkout, remote


def test_fetch_shipwright_main_runs_the_exact_refspec_before_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    call_log = tmp_path / "fetch-calls.log"
    order_log = tmp_path / "order.log"
    monkeypatch.setenv("FAKE_GIT_FETCH_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_GIT_ORDER_LOG", str(order_log))

    dest = publish_scan._export_shipwright_tree(checkout)

    lines = call_log.read_text().splitlines()
    assert lines[0] == (
        f"-C {checkout} fetch --no-tags origin "
        f"refs/heads/main:refs/remotes/origin/main"
    )
    assert lines[1] == "GIT_TERMINAL_PROMPT=0"
    assert order_log.read_text().splitlines() == ["fetch", "archive"]
    # The fetch actually ran for real (via the fake's own deferral to the real git),
    # so the archive below reads the UPDATED main the fetch just brought in.
    assert "# updated" in (dest / "bin" / "publish-engine").read_text()


def test_fetch_shipwright_main_pins_config_regardless_of_the_callers_own_env(
        tmp_path, monkeypatch):
    """A caller's own `GIT_CONFIG_GLOBAL`, pointed at a config file that could
    redirect `origin`, must never reach the fetch: `_fetch_shipwright_main` forces
    its own `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=os.devnull` on top of
    whatever the caller's environment already set, same as `primary_guard.py`'s
    `_SAFE_GIT_ENV` and `dispatch_env.py`'s `git_credential_guard_env` already do
    for this exact reason."""
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    call_log = tmp_path / "fetch-calls.log"
    monkeypatch.setenv("FAKE_GIT_FETCH_CALL_LOG", str(call_log))
    attacker_config = tmp_path / "attacker-gitconfig"
    attacker_config.write_text("[nothing]\n\tplaceholder = 1\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(attacker_config))

    publish_scan._export_shipwright_tree(checkout)

    lines = call_log.read_text().splitlines()
    recorded = dict(line.split("=", 1) for line in lines[2:])
    assert recorded["GIT_CONFIG_NOSYSTEM"] == "1"
    assert recorded["GIT_CONFIG_GLOBAL"] == os.devnull
    assert recorded["GIT_CONFIG_GLOBAL"] != str(attacker_config)


def test_fetch_shipwright_main_drops_the_callers_ssh_and_proxy_overrides(
        tmp_path, monkeypatch):
    """A caller's own `GIT_SSH_COMMAND`/`GIT_SSH` or `GIT_PROXY_COMMAND` could run
    an arbitrary command in place of ssh, or route the fetch through a proxy
    that rewrites what `origin` answers; none of the three must reach the
    fetch."""
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    call_log = tmp_path / "fetch-calls.log"
    monkeypatch.setenv("FAKE_GIT_FETCH_CALL_LOG", str(call_log))
    monkeypatch.setenv("GIT_SSH_COMMAND", "/bin/sh -c 'echo pwned' --")
    monkeypatch.setenv("GIT_SSH", "/bin/sh")
    monkeypatch.setenv("GIT_PROXY_COMMAND", "/bin/sh -c 'echo pwned' --")

    publish_scan._export_shipwright_tree(checkout)

    lines = call_log.read_text().splitlines()
    recorded = dict(line.split("=", 1) for line in lines[2:])
    assert recorded["GIT_SSH_COMMAND"] == ""
    assert recorded["GIT_SSH"] == ""
    assert recorded["GIT_PROXY_COMMAND"] == ""


def test_fetch_shipwright_main_drops_an_inherited_protocol_limit(
        tmp_path, monkeypatch):
    """`agentctl publish`'s own push sets `GIT_ALLOW_PROTOCOL=https` on itself
    (`github_app.py`'s `push_env`, scoped to that push's own HTTPS remote); when
    `bin/push-content-gate` runs this module from inside that push's pre-push
    hook, this process inherits that `https`-only value as plain environment.
    This test's own remote is a local path, which git reaches over its own
    `file` transport — refused by the inherited `https`-only value just as it
    would be by Shipwright's real SSH origin, and allowed only once the fetch
    drops the variable and runs under git's own default protocol policy, the
    same policy a plain `git push`'s own hook fetch would run under."""
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
    real_run = sp.run
    fetch_envs = []

    def _spy(args, **kwargs):
        if "fetch" in args:
            fetch_envs.append(kwargs.get("env") or {})
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish_scan.subprocess, "run", _spy)

    dest = publish_scan._export_shipwright_tree(checkout)

    assert "GIT_ALLOW_PROTOCOL" not in fetch_envs[0]
    # The fetch actually ran, for real, against the local origin above, so the
    # archive below reads the UPDATED main the fetch just brought in.
    assert "# updated" in (dest / "bin" / "publish-engine").read_text()


def _real_git(real_git: str, checkout: Path, *args: str) -> sp.CompletedProcess:
    """A verification-only git call against `checkout`, always the REAL binary
    (its path captured before `_install_fake_git` touches `PATH`) — so a test
    confirming the checkout's own state is never itself routed through the
    fetch stub."""
    return sp.run([real_git, "-C", str(checkout), *args], capture_output=True, text=True, check=True)


def test_a_failing_fetch_refuses_and_runs_no_engine(tmp_path, monkeypatch, names_files):
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    real_git = shutil.which("git")
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_GIT_FETCH_FAIL", "1")
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    head_before = _real_git(real_git, checkout, "rev-parse", "HEAD").stdout
    with pytest.raises(publish_scan.PublishScanRefused, match="could not fetch"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)
    head_after = _real_git(real_git, checkout, "rev-parse", "HEAD").stdout
    assert head_before == head_after


def test_a_timed_out_fetch_refuses_and_runs_no_engine(tmp_path, monkeypatch, names_files):
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setattr(publish_scan, "_SHIPWRIGHT_FETCH_TIMEOUT_S", 0.2)
    monkeypatch.setenv("FAKE_GIT_FETCH_HANG", "1")
    monkeypatch.setenv("FAKE_GIT_FETCH_HANG_SECONDS", "5")
    private, machine = names_files
    monkeypatch.setattr(publish_scan, "shipwright_checkout", lambda wt: checkout)
    monkeypatch.setattr(publish_scan, "private_names_file", lambda: private)
    monkeypatch.setattr(publish_scan, "machine_names_file", lambda: machine)
    with pytest.raises(publish_scan.PublishScanRefused, match="timed out"):
        publish_scan.scan_commits("agent-platform", tmp_path, ["abc123"], skip_unknown_repo=True)


def test_a_failing_fetch_leaves_the_checkouts_working_tree_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    real_git = shutil.which("git")
    checkout, _remote = _real_shipwright_checkout_with_remote(tmp_path)
    _install_fake_git(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_GIT_FETCH_FAIL", "1")
    status_before = _real_git(real_git, checkout, "status", "--porcelain").stdout
    branch_before = _real_git(real_git, checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout
    with pytest.raises(publish_scan.PublishScanRefused, match="could not fetch"):
        publish_scan._export_shipwright_tree(checkout)
    status_after = _real_git(real_git, checkout, "status", "--porcelain").stdout
    branch_after = _real_git(real_git, checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout
    assert status_before == status_after == ""
    assert branch_before == branch_after == "trunk\n"
