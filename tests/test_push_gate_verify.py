"""push-gate-verify — the operator's own proof that the push content gate
refuses a forbidden name and passes a clean commit in every installed clone.

Every clone here is a real, from-scratch git repository under a temp root
named with `--root-config` (company/decisions/0008), never a real clone.
Each clone's `pre-push` hook is one of four shapes this file controls
completely: the real stub `install-push-gate` writes (running the real
`bin/push-content-gate`), a stub that allows everything, a stub that denies
everything, and a foreign hook carrying none of this gate's marker.

THE "WORKING" HOOK RUNS AGAINST A CONTENT-AWARE FAKE ENGINE THIS FILE WRITES,
NOT `test_publish_scan.py`'s `_FAKE_ENGINE`. That fixture decides a finding by
an env var or an exact ref match set in advance — it cannot tell a forbidden
probe from a clean one by what either actually contains, and `push-gate-verify`
computes both probes' commit shas itself, so nothing here could set that
env var or ref in advance. This file's engine instead reads the real
`BUILDER_PRIVATE_NAMES` file and the real tree at `--from`/`--ref`, exactly as
the production engine's job (never its implementation) is described in
`lib/agent-platform/publish_scan.py` — the one genuine way to tell the two
probes apart. It reuses `test_push_content_gate.py`'s
`_shipwright_checkout_with`, which already builds the real git checkout and
`origin/main` export either engine script sits behind.

The private name never appears in this file: a synthetic marker string
stands in for it, exactly as the project's content scan requires.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess as sp
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from test_push_content_gate import _shipwright_checkout_with

REPO = Path(__file__).resolve().parent.parent
PGV = REPO / "bin" / "push-gate-verify"
GATE = REPO / "bin" / "push-content-gate"

# bin/install-push-gate's own clone list, duplicated here the same way
# tests/bash/test_install_push_gate.sh already duplicates it.
CLONES = ("agent-platform", "library", "convivy-orient", "convivy-deck", "write-better")
MARKER = "# managed-by: agent-platform push-gate"

# push-gate-verify's own FORBIDDEN_PROBE_PATH and CLEAN_PROBE_PATH,
# duplicated here for the same reason CLONES and MARKER are above: this file
# builds hooks and reads output that must name the same paths push-gate-verify
# itself uses for its two probes.
FORBIDDEN_PROBE_PATH = "push-gate-probe/forbidden.txt"
CLEAN_PROBE_PATH = "push-gate-probe/clean.txt"

_FAKE_HOME = tempfile.mkdtemp(prefix="push-gate-verify-test-home-")

# A content-aware fake publish-engine: reads BUILDER_PRIVATE_NAMES for real and
# greps the real git tree at --from/--ref for each of its lines, so a probe
# commit holding one of them is refused and one that does not is not — see
# this file's own docstring for why `test_publish_scan.py`'s fixture engine
# cannot do this job. It also refuses a `--ref` that does not name a commit,
# the same way the real publish-engine does when it cannot export one: this
# is what a parentless probe's empty-tree parent triggered on 2026-10-26, so
# a probe built that way still gets refused here too, with "could not run"
# rather than a pass.
_CONTENT_ENGINE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import argparse, os, subprocess, sys, time

    p = argparse.ArgumentParser(prog="publish-engine")
    p.add_argument("name", choices=["agent-platform", "library",
                                     "convivy-orient", "convivy-deck", "write-better"])
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--scan-only", action="store_true")
    args = p.parse_args()

    # Tests only: a push-gate-verify probe-timeout test sets this to sleep
    # past a shortened --probe-timeout without waiting out the real 300 s.
    # PUSH_GATE_VERIFY_TEST_SLEEP_REPO, when also set, confines the sleep to
    # one repository, so a multi-clone test can make one clone's probes hang
    # while another's behave normally.
    sleep_seconds = os.environ.get("PUSH_GATE_VERIFY_TEST_SLEEP_SECONDS", "")
    sleep_repo = os.environ.get("PUSH_GATE_VERIFY_TEST_SLEEP_REPO", "")
    if sleep_seconds and (not sleep_repo or sleep_repo == args.name):
        time.sleep(float(sleep_seconds))

    if not args.scan_only:
        print("publish-engine: this fake only knows --scan-only", file=sys.stderr)
        sys.exit(2)

    kind = subprocess.run(
        ["git", "-C", args.source, "cat-file", "-t", args.ref],
        capture_output=True, text=True,
    )
    if kind.returncode != 0 or kind.stdout.strip() != "commit":
        print(f"publish-engine: {args.ref} names no commit in {args.source}, "
              f"so there is nothing to export", file=sys.stderr)
        sys.exit(2)

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

    with open(private) as f:
        markers = [line.strip() for line in f
                   if line.strip() and not line.strip().startswith("#")]

    listing = subprocess.run(
        ["git", "-C", args.source, "ls-tree", "-r", "--name-only", args.ref],
        capture_output=True, text=True,
    )
    if listing.returncode != 0:
        print(f"publish-engine: could not list the tree at {args.ref}: "
              f"{listing.stderr.strip()}", file=sys.stderr)
        sys.exit(2)

    findings = []
    for path in listing.stdout.splitlines():
        if not path:
            continue
        shown = subprocess.run(
            ["git", "-C", args.source, "show", f"{args.ref}:{path}"],
            capture_output=True, text=True,
        )
        if shown.returncode != 0:
            continue
        if any(marker in shown.stdout for marker in markers):
            findings.append(path)

    if findings:
        print(f"publish-engine: {args.name} at {args.ref} would be refused by a "
              f"release. No value is printed; open each file to judge it.")
        for path in findings:
            print(f"  content scan (1):\\n    {path}:1: a private name")
        sys.exit(1)

    print(f"publish-engine: {args.name} at {args.ref}: scanned "
          f"{len(listing.stdout.splitlines())} file(s); nothing found")
    sys.exit(0)
''')


def _write_content_engine(root: Path) -> Path:
    engine = root / "bin" / "publish-engine"
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_text(_CONTENT_ENGINE)
    engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
    return root


@pytest.fixture
def names_files(tmp_path):
    """A synthetic marker standing in for the operator's own private name —
    never a real person's name, per this project's own content scan rule."""
    private = tmp_path / "private-names"
    machine = tmp_path / "machine-names"
    private.write_text("push-gate-verify-test-marker-7f3c\n")
    machine.write_text("push-gate-verify-test-machine\n")
    return private, machine


@pytest.fixture
def operator_config(tmp_path, names_files):
    checkout = _shipwright_checkout_with(tmp_path, _write_content_engine)
    private, machine = names_files
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({
        "shipwright_checkout": str(checkout),
        "builder_private_names_file": str(private),
        "builder_machine_names_file": str(machine),
    }))
    return cfg


@pytest.fixture
def base_env(operator_config):
    """The environment every `push-gate-verify` run in this file runs under."""
    env = dict(os.environ)
    env["HOME"] = _FAKE_HOME
    env["CONVIVY_OPERATOR_CONFIG"] = str(operator_config)
    return env


def _run_git(*args, cwd=None, check=True):
    return sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _seed_clone(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git("init", "-q", "-b", "main", str(path))
    _run_git("config", "user.email", "t@t.example", cwd=str(path))
    _run_git("config", "user.name", "t", cwd=str(path))
    _run_git("config", "commit.gpgsign", "false", cwd=str(path))
    (path / "f.txt").write_text("hello\n")
    _run_git("add", "-A", cwd=str(path))
    _run_git("commit", "-q", "-m", "base", cwd=str(path))


_WORKING_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
exec "{python}" "{gate}" "{repo}" "$@"
'''

# The same real gate as _WORKING_HOOK, but `tee`s git's own four-field
# `pre-push` stdin line to `{capture_file}` first — so a test can read back
# exactly which commit sha push-gate-verify pushed as each probe, something
# push-gate-verify's own output never names. Used by
# test_the_probe_commit_is_built_on_head_with_only_its_own_path.
_WORKING_CAPTURE_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
tee -a "{capture_file}" | exec "{python}" "{gate}" "{repo}" "$@"
'''

_ALLOW_EVERYTHING_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
exit 0
'''

_DENY_EVERYTHING_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
echo "push refused by the content gate: deny-everything stub" >&2
echo "nothing was sent" >&2
exit 1
'''

_FOREIGN_HOOK = '''#!/bin/sh
echo "a foreign pre-push hook, not ours" >&2
exit 0
'''

# Refuses every push (forbidden and clean alike) by printing the forbidden
# probe's own content to stderr, naming the forbidden probe's path, the
# way a real refusal message would — so push-gate-verify's _relevant_lines
# selects the line and _scrub must redact the private name out of it
# before anything is printed. Used by
# test_the_private_name_never_appears_on_the_refusal_path to exercise the
# scrub call, which the success-path no-leak test never reaches.
_LEAKY_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
echo "push refused by the content gate: {probe_path}: {marker}" >&2
echo "nothing was sent" >&2
exit 1
'''

# Refuses every push without ever naming either probe's path — a hook that
# denies for a reason that has nothing to do with content. Used to show
# the forbidden check requires BOTH a non-zero exit AND the forbidden
# path in the output, not either alone.
_DENY_UNRELATED_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
echo "denied: outside business hours" >&2
exit 1
'''

# Tells the forbidden probe from the clean one by what the pushed commit
# itself touches (via `git diff-tree`), the way a real content-aware gate
# would, rather than by the fixed path strings this test file otherwise
# controls. Refuses the forbidden probe (as a real gate should) but for
# an unrelated-sounding reason that never names the forbidden probe's own
# path — only the private name — so push-gate-verify's forbidden check
# reads NOT refused (it requires the path, not the exit code alone, same
# as test_forbidden_check_requires_exit_and_named_path_not_either) and the
# "forbidden probe NOT refused" message is built from a line _scrub must
# redact. The clean probe passes outright, so only this one check's
# `_scrub` call runs. Used by test_the_private_name_never_appears_when_
# the_forbidden_check_itself_fails to exercise the scrub call the other
# no-leak tests never reach.
_FORBIDDEN_CHECK_LEAKY_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
while read local_ref local_sha remote_ref remote_sha; do
    if git diff-tree --no-commit-id --name-only -r --root "$local_sha" \\
            | grep -q '^push-gate-probe/forbidden\\.txt$'; then
        echo "push refused: suspicious content: {marker}" >&2
        exit 1
    fi
done
exit 0
'''

# The same diff-tree trick, used the other way around: refuses the
# forbidden probe correctly (by path, no private name involved) but also
# — the bug this test guards against — refuses the CLEAN probe, logging
# the private name without ever naming the clean probe's own path. Used
# by test_the_private_name_never_appears_when_the_clean_check_itself_fails
# to exercise the clean-check _scrub call.
_CLEAN_REFUSED_LEAKY_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
while read local_ref local_sha remote_ref remote_sha; do
    if git diff-tree --no-commit-id --name-only -r --root "$local_sha" \\
            | grep -q '^push-gate-probe/forbidden\\.txt$'; then
        echo "push refused by the content gate: push-gate-probe/forbidden.txt" >&2
        exit 1
    fi
    if git diff-tree --no-commit-id --name-only -r --root "$local_sha" \\
            | grep -q '^push-gate-probe/clean\\.txt$'; then
        echo "push refused by the content gate: policy violation: {marker}" >&2
        exit 1
    fi
done
exit 0
'''

# Stands in for `push-content-gate` relaying a crashed publish-engine's own
# stdout/stderr unchanged (`lib/agent-platform/publish_scan.py`'s
# `_could_not_run_message`): refuses every push, with the
# gate's own "push refused" line followed by a Python-traceback-shaped
# indented "  File ..." line and an indented source line beneath it, neither
# of which names either probe's path. Used by
# test_a_crashed_engines_traceback_is_filtered_the_refusal_line_is_not to
# show `_relevant_lines` keeps the refusal line and drops the traceback.
_CRASHED_ENGINE_LEAKY_HOOK = '''#!/bin/sh
# managed-by: agent-platform push-gate
echo "push refused by the content gate: publish-engine could not run (exit 1) scanning agent-platform at deadbeef9:" >&2
echo "Traceback (most recent call last):" >&2
echo '  File "bin/publish-engine", line 42, in main' >&2
echo '    raise ValueError("boom")' >&2
echo "ValueError: boom" >&2
echo "nothing was sent" >&2
exit 1
'''


def _install_hook(clone: Path, repo: str, kind: str, *, marker: str | None = None,
                   capture_file: Path | None = None) -> None:
    hooks = clone / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    if kind == "working":
        text = _WORKING_HOOK.format(python=sys.executable, gate=str(GATE), repo=repo)
    elif kind == "working_capture":
        text = _WORKING_CAPTURE_HOOK.format(
            python=sys.executable, gate=str(GATE), repo=repo, capture_file=str(capture_file),
        )
    elif kind == "allow":
        text = _ALLOW_EVERYTHING_HOOK
    elif kind == "deny":
        text = _DENY_EVERYTHING_HOOK
    elif kind == "foreign":
        text = _FOREIGN_HOOK
    elif kind == "leaky":
        text = _LEAKY_HOOK.format(probe_path=FORBIDDEN_PROBE_PATH, marker=marker)
    elif kind == "deny_unrelated":
        text = _DENY_UNRELATED_HOOK
    elif kind == "forbidden_check_leaky":
        text = _FORBIDDEN_CHECK_LEAKY_HOOK.format(marker=marker)
    elif kind == "clean_refused_leaky":
        text = _CLEAN_REFUSED_LEAKY_HOOK.format(marker=marker)
    elif kind == "crashed_leaky":
        text = _CRASHED_ENGINE_LEAKY_HOOK
    else:
        raise ValueError(kind)
    hook.write_text(text)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def _make_root(tmp_path, name: str, hooks: dict) -> Path:
    """`hooks` maps a clone name to "working" / "allow" / "deny" / "foreign";
    a clone this does not name gets no directory at all, so push-gate-verify
    reports it "not installed" the same way a clone missing on this machine
    would be."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for repo, kind in hooks.items():
        clone = root / repo
        _seed_clone(clone)
        _install_hook(clone, repo, kind)
    return root


def _root_config(tmp_path, root: Path, name: str = "session-agent.env") -> Path:
    cfg = tmp_path / name
    cfg.write_text(f"CREWS_ROOT={root}\n")
    return cfg


def _run_pgv(root_config: Path, env: dict, *, bare_repo_dir: Path | None = None,
             probe_timeout: float | None = None) -> sp.CompletedProcess:
    args = [sys.executable, str(PGV), "--root-config", str(root_config)]
    if bare_repo_dir is not None:
        args += ["--bare-repo-dir", str(bare_repo_dir)]
    if probe_timeout is not None:
        args += ["--probe-timeout", str(probe_timeout)]
    return sp.run(args, env=env, capture_output=True, text=True)


# --- every clone passes when the gate works, each probe judged on its own
# --- value rather than on a parent publish-engine could not scan -----------

def test_every_clone_passes_when_the_gate_works(tmp_path, base_env):
    """`_CONTENT_ENGINE` refuses a `--ref` that does not name a commit, the
    same way the real publish-engine refused a parentless probe's empty-tree
    parent on 2026-10-26: this run fails with "could not run" on
    `origin/main`'s push-gate-verify, which built every probe parentless,
    and passes here because each probe's parent is the clone's own HEAD, a
    real commit."""
    root = _make_root(tmp_path, "root-ok", {repo: "working" for repo in CLONES})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 0, result.stdout + result.stderr
    for repo in CLONES:
        assert f"{repo}: refused forbidden probe, passed clean probe" in result.stdout
    assert "could not run" not in result.stdout


# --- an overly permissive hook fails on the forbidden probe ------------------

def test_an_allow_everything_hook_fails_on_the_forbidden_probe(tmp_path, base_env):
    root = _make_root(tmp_path, "root-allow", {"agent-platform": "allow"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 1
    assert "agent-platform: forbidden probe NOT refused" in result.stdout


# --- an overly restrictive hook fails on the clean probe ---------------------

def test_a_deny_everything_hook_fails_on_the_clean_probe(tmp_path, base_env):
    root = _make_root(tmp_path, "root-deny", {"agent-platform": "deny"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 1
    assert result.stdout.startswith("agent-platform: ")
    assert "clean probe failed" in result.stdout


# --- a clone without the marker is reported not installed --------------------

def test_a_clone_without_the_marker_is_not_installed(tmp_path, base_env):
    root = _make_root(tmp_path, "root-foreign", {"agent-platform": "foreign"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 1
    assert "agent-platform: not installed" in result.stdout


# --- HEAD, branches, the index and the working tree are unchanged -----------

def test_head_index_and_working_tree_are_unchanged(tmp_path, base_env):
    root = _make_root(tmp_path, "root-unchanged", {repo: "working" for repo in CLONES})
    cfg = _root_config(tmp_path, root)
    clone = root / "agent-platform"

    head_before = _run_git("rev-parse", "HEAD", cwd=str(clone)).stdout
    branches_before = _run_git(
        "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", cwd=str(clone),
    ).stdout
    status_before = _run_git("status", "--porcelain", cwd=str(clone)).stdout
    index_path = clone / ".git" / "index"
    index_before = index_path.read_bytes()

    result = _run_pgv(cfg, base_env)
    assert result.returncode == 0, result.stdout + result.stderr

    head_after = _run_git("rev-parse", "HEAD", cwd=str(clone)).stdout
    branches_after = _run_git(
        "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", cwd=str(clone),
    ).stdout
    status_after = _run_git("status", "--porcelain", cwd=str(clone)).stdout
    index_after = index_path.read_bytes()

    assert head_after == head_before
    assert branches_after == branches_before
    assert status_before == ""
    assert status_after == status_before
    assert index_after == index_before


# --- the private name never reaches stdout or stderr -------------------------

def test_the_private_name_never_appears_in_output(tmp_path, base_env, names_files):
    private, _machine = names_files
    marker = private.read_text().splitlines()[0].strip()
    root = _make_root(tmp_path, "root-name-check", {"agent-platform": "working"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert marker not in result.stdout
    assert marker not in result.stderr


# --- the private name never reaches output on the refusal path either --------

def test_the_private_name_never_appears_on_the_refusal_path(tmp_path, base_env, names_files):
    """The success path above returns before _relevant_lines / _scrub ever
    run, so it cannot tell a working scrub from a deleted one. This hook
    refuses every push and prints the forbidden probe's own path and
    content to stderr — exactly the shape _relevant_lines selects — so the
    clone-failed message is built from text _scrub must redact."""
    private, _machine = names_files
    marker = private.read_text().splitlines()[0].strip()
    root = tmp_path / "root-leaky-refusal"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    _install_hook(clone, "agent-platform", "leaky", marker=marker)
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert marker not in result.stdout
    assert marker not in result.stderr
    assert "clean probe failed" in result.stdout


# --- the private name never reaches output when the FORBIDDEN check itself
# --- fails (the sibling of the refusal-path test above, for the other site)

def test_the_private_name_never_appears_when_the_forbidden_check_itself_fails(
        tmp_path, base_env, names_files):
    """The refusal-path test above drives a hook that always names the
    forbidden probe's own path, so `forbidden_ok` reads True there and
    `_verify_clone`'s "forbidden probe NOT refused" branch (and its own
    `_scrub` call) never runs. This hook tells the two probes apart by
    what the pushed commit actually touches and refuses the forbidden one
    (the clean probe passes outright), but for an unrelated-sounding
    reason that logs the private name without ever naming the forbidden
    probe's own path — so `forbidden_ok` reads False on the missing path
    alone, same as test_forbidden_check_requires_exit_and_named_path_not_
    either, and the message built from that log line is exactly what
    `_scrub` must redact."""
    private, _machine = names_files
    marker = private.read_text().splitlines()[0].strip()
    root = tmp_path / "root-forbidden-check-leaky"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    _install_hook(clone, "agent-platform", "forbidden_check_leaky", marker=marker)
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert marker not in result.stdout
    assert marker not in result.stderr
    assert "forbidden probe NOT refused" in result.stdout


# --- the private name never reaches output when the CLEAN check itself fails

def test_the_private_name_never_appears_when_the_clean_check_itself_fails(
        tmp_path, base_env, names_files):
    """The refusal-path test above's hook refuses the clean probe too, but
    only as a side effect of denying everything with one fixed message —
    it never isolates the clean-check `_scrub` call from the forbidden
    one. This hook refuses the forbidden probe correctly (by path, no
    private name involved) and, as the bug being guarded against, also
    refuses the CLEAN probe while logging the private name — so only the
    clean check's own `_scrub` call is what keeps the name out of
    push-gate-verify's output here."""
    private, _machine = names_files
    marker = private.read_text().splitlines()[0].strip()
    root = tmp_path / "root-clean-refused"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    _install_hook(clone, "agent-platform", "clean_refused_leaky", marker=marker)
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert marker not in result.stdout
    assert marker not in result.stderr
    assert "clean probe failed" in result.stdout


# --- the forbidden check requires a non-zero exit AND the named path --------

def test_forbidden_check_requires_exit_and_named_path_not_either(tmp_path, base_env):
    """A hook that refuses unconditionally, without ever naming the
    forbidden probe's path, tells push-gate-verify nothing about whether
    the forbidden content was what was refused. The forbidden check must
    read that as NOT refused; an OR of the two conditions would wrongly
    call this refused on the exit code alone."""
    root = _make_root(tmp_path, "root-deny-unrelated", {"agent-platform": "deny_unrelated"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 1
    assert "agent-platform: forbidden probe NOT refused" in result.stdout


# --- a bare-repo init failure is reported per-clone and the run continues ----

def test_bare_repo_init_failure_is_reported_and_the_run_continues(tmp_path, base_env):
    """`agent-platform`'s forbidden bare repo is pointed at a path that
    already exists as a regular file, so `git init --bare` fails for that
    clone alone; the other clones' bare repos sit at ordinary, untaken
    paths under the same directory and succeed normally."""
    root = _make_root(tmp_path, "root-bare-failure", {repo: "working" for repo in CLONES})
    cfg = _root_config(tmp_path, root)
    bare_dir = tmp_path / "bare-repos"
    bare_dir.mkdir()
    (bare_dir / "agent-platform-forbidden").write_text("not a directory\n")

    result = _run_pgv(cfg, base_env, bare_repo_dir=bare_dir)

    assert result.returncode == 1
    assert "agent-platform: git init --bare failed" in result.stdout
    for repo in CLONES:
        if repo != "agent-platform":
            assert f"{repo}: refused forbidden probe, passed clean probe" in result.stdout


# --- regression: an older refused commit in the clone's OWN history must
# --- never be what the probe push is judged on (the production bug) --------

def test_an_older_refused_commit_in_the_clones_history_is_not_scanned(tmp_path, base_env, names_files):
    """Each probe is now built on the clone's own HEAD, so its parent is a
    real commit rather than git's empty-tree sha — but the throwaway bare
    repo is seeded with that same HEAD (`_seed_bare_with_head`, pushed with
    `--no-verify`) before either probe is pushed, so `bin/push-content-
    gate`'s already-known-commits lookup
    (`publish_scan.commits_new_to_remote`) finds HEAD already on the bare
    repo's `refs/heads/probe` and excludes everything reachable from it —
    including an OLDER commit, below HEAD, that already carries a finding.
    Without that seeding, the probe's bare repo would have no ref at all
    when the probe is pushed, and the gate would scan the clone's ENTIRE
    history, oldest first, including that older commit, before ever
    reaching the probe — the bug `bin/push-gate-verify` (39f17c2b1) had
    before probes were made parentless, which this regression test
    originally guarded against; seeding the bare repo with HEAD closes the
    same gap now that probes carry a real parent again."""
    private, _machine = names_files
    marker = private.read_text().splitlines()[0].strip()
    root = tmp_path / "root-old-finding"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    (clone / "secret.txt").write_text(f"{marker}\n")
    _run_git("add", "-A", cwd=str(clone))
    _run_git("commit", "-q", "-m", "an old commit carrying the marker", cwd=str(clone))
    (clone / "f.txt").write_text("a later, clean commit\n")
    _run_git("add", "-A", cwd=str(clone))
    _run_git("commit", "-q", "-m", "clean HEAD", cwd=str(clone))
    _install_hook(clone, "agent-platform", "working")
    cfg = _root_config(tmp_path, root)

    result = _run_pgv(cfg, base_env)

    # Only agent-platform is installed in this root; the other four CLONES
    # report "not installed" and make the overall run fail regardless, so
    # this checks agent-platform's own per-clone line, not the exit code.
    assert "agent-platform: refused forbidden probe, passed clean probe" in result.stdout


# --- each probe commit's parent is a real commit in the clone (its own
# --- HEAD), its tree holds only its own path, and the clone's HEAD,
# --- branches and working tree are left untouched ---------------------------

def test_the_probe_commit_is_built_on_head_with_only_its_own_path(tmp_path, base_env):
    root = tmp_path / "root-parent-is-head"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    capture_file = tmp_path / "pushed-lines.txt"
    capture_file.write_text("")
    _install_hook(clone, "agent-platform", "working_capture", capture_file=capture_file)
    cfg = _root_config(tmp_path, root)

    head_before = _run_git("rev-parse", "HEAD", cwd=str(clone)).stdout.strip()
    refs_before = _run_git("show-ref", cwd=str(clone), check=False).stdout
    branches_before = _run_git(
        "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", cwd=str(clone),
    ).stdout
    index_path = clone / ".git" / "index"
    index_before = index_path.read_bytes()

    result = _run_pgv(cfg, base_env)
    # Only agent-platform is installed in this root (see the sibling
    # regression test above for why the exit code is not checked here).
    assert "agent-platform: refused forbidden probe, passed clean probe" in result.stdout

    head_after = _run_git("rev-parse", "HEAD", cwd=str(clone)).stdout.strip()
    refs_after = _run_git("show-ref", cwd=str(clone), check=False).stdout
    branches_after = _run_git(
        "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", cwd=str(clone),
    ).stdout
    status_after = _run_git("status", "--porcelain", cwd=str(clone)).stdout
    index_after = index_path.read_bytes()
    assert head_after == head_before
    assert refs_after == refs_before
    assert branches_after == branches_before
    assert status_after == ""
    assert index_after == index_before

    # The capture hook ran twice (forbidden probe, then clean probe, in that
    # order — the same order _verify_clone pushes them in), each appending
    # one "<local ref> <local sha> <remote ref> <remote sha>" stdin line.
    pushed_lines = [line for line in capture_file.read_text().splitlines() if line.strip()]
    assert len(pushed_lines) == 2
    probe_paths = (FORBIDDEN_PROBE_PATH, CLEAN_PROBE_PATH)
    for line, expected_path in zip(pushed_lines, probe_paths):
        local_sha = line.split()[1]
        parent = _run_git("rev-parse", f"{local_sha}^", cwd=str(clone), check=False)
        assert parent.returncode == 0, f"probe commit {local_sha} has no parent"
        assert parent.stdout.strip() == head_before, (
            "probe commit's parent is not the clone's own HEAD"
        )
        parent_type = _run_git(
            "cat-file", "-t", parent.stdout.strip(), cwd=str(clone),
        ).stdout.strip()
        assert parent_type == "commit", (
            f"probe commit's parent {parent.stdout.strip()!r} is a {parent_type!r}, not a commit"
        )
        tree_paths = _run_git(
            "ls-tree", "-r", "--name-only", local_sha, cwd=str(clone),
        ).stdout.splitlines()
        assert tree_paths == [expected_path]


# --- a probe push that does not finish is reported, not waited on forever,
# --- and the run moves on to the next clone with no bare repo left behind --

def test_a_probe_push_that_times_out_is_reported_and_the_run_continues(tmp_path, base_env):
    root = tmp_path / "root-timeout"
    root.mkdir()
    slow_clone = root / "agent-platform"
    _seed_clone(slow_clone)
    _install_hook(slow_clone, "agent-platform", "working")
    fast_clone = root / "library"
    _seed_clone(fast_clone)
    _install_hook(fast_clone, "library", "working")
    cfg = _root_config(tmp_path, root)
    bare_dir = tmp_path / "bare-repos"
    bare_dir.mkdir()

    # The budget below is shared by both clones' probe pushes (`--probe-
    # timeout` is one value for the whole run): `library` never sleeps, so
    # it only has to finish a real push inside it, but a loaded runner can
    # make even that slower than 0.5 s, which is what failed this test on
    # unrelated PRs during the 0.7.0 push. 10 s gives `library` a budget a
    # loaded runner meets; `agent-platform`'s sleep is set above that so it
    # still times out.
    env = dict(base_env)
    env["PUSH_GATE_VERIFY_TEST_SLEEP_SECONDS"] = "20"
    env["PUSH_GATE_VERIFY_TEST_SLEEP_REPO"] = "agent-platform"

    result = _run_pgv(cfg, env, bare_repo_dir=bare_dir, probe_timeout=10)

    assert result.returncode == 1
    assert "agent-platform: forbidden probe push did not finish within 10 s" in result.stdout
    assert "library: refused forbidden probe, passed clean probe" in result.stdout
    assert list(bare_dir.iterdir()) == []


# --- a misbehaving probe's output now carries publish-engine's own report
# --- and finding lines, and still never the private name --------------------

def test_a_misbehaving_probes_output_carries_the_engines_finding_line_never_the_name(tmp_path):
    """The clean probe's own fixed content (`CLEAN_PROBE_CONTENT`, "push-
    gate-verify: harmless probe content\\n") happens to contain this test's
    marker literally, so the real content-aware fake engine refuses the
    CLEAN probe for a genuine content reason — not a canned shell stub —
    giving push-gate-verify a real publish-engine report to filter. Before
    `_relevant_lines` kept publish-engine's own "publish-engine: ..." line
    and its indented finding-detail lines, this report's own explanation
    line was dropped from push-gate-verify's output — exactly the truncated
    paste ("...scanning agent-platform at <sha>...:") the operator saw."""
    marker = "harmless probe content"
    private = tmp_path / "private-names"
    machine = tmp_path / "machine-names"
    private.write_text(f"{marker}\n")
    machine.write_text("push-gate-verify-test-machine\n")
    checkout = _shipwright_checkout_with(tmp_path, _write_content_engine)
    cfg = tmp_path / "operator.json"
    cfg.write_text(json.dumps({
        "shipwright_checkout": str(checkout),
        "builder_private_names_file": str(private),
        "builder_machine_names_file": str(machine),
    }))
    env = dict(os.environ)
    env["HOME"] = _FAKE_HOME
    env["CONVIVY_OPERATOR_CONFIG"] = str(cfg)

    root = tmp_path / "root-misbehaving"
    root.mkdir()
    clone = root / "agent-platform"
    _seed_clone(clone)
    _install_hook(clone, "agent-platform", "working")
    root_cfg = _root_config(tmp_path, root)

    result = _run_pgv(root_cfg, env)

    assert "clean probe failed" in result.stdout
    assert "publish-engine: agent-platform at" in result.stdout
    assert f"{CLEAN_PROBE_PATH}:1: a private name" in result.stdout
    assert marker not in result.stdout
    assert marker not in result.stderr


# --- a crashed engine's traceback is filtered out; the refusal line is not --

def test_a_crashed_engines_traceback_is_filtered_the_refusal_line_is_not(tmp_path, base_env):
    """`push-content-gate` relays a crashed publish-engine's own stdout and
    stderr unchanged (`lib/agent-platform/publish_scan.py`'s
    `_could_not_run_message`), so the refusal message `_relevant_lines`
    filters can hold a Python traceback: an indented "  File ..." line and
    an indented source line beneath it, alongside the gate's own "push
    refused" line. Before `_relevant_lines` kept every indented line rather
    than only ones shaped like an engine finding (`<path>:<line>:
    <description>`), the traceback's two indented lines were kept and
    printed too."""
    root = _make_root(tmp_path, "root-crashed", {"agent-platform": "crashed_leaky"})
    cfg = _root_config(tmp_path, root)
    result = _run_pgv(cfg, base_env)
    assert result.returncode == 1
    assert "push refused by the content gate" in result.stdout
    assert "nothing was sent" in result.stdout
    assert 'File "bin/publish-engine"' not in result.stdout
    assert 'raise ValueError("boom")' not in result.stdout
