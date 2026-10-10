"""session-event-log hook — the exit-code contract above all else.

This hook is registered on `Stop`. A nonzero exit from a Stop hook blocks the
stop and forces the model to keep going, so a crash here would wedge every
session on the machine. The first class of tests pins exit 0 against every
malformed input we could think of; treat a failure there as release-blocking
regardless of what the rest of the suite says.

The remaining tests pin the properties that keep the log usable: one file per
session (so seven concurrent sessions never interleave a partial write), an
untrusted `session_id` that cannot escape the log directory, and a size ceiling
so one pathological message cannot write an unbounded line.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import sys
import time
import uuid
from pathlib import Path

import pytest

import tmux_transcript as TT

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "session-event-log.py"


def _run(payload, tmp_home, event="Stop", extra_env=None):
    """Invoke the hook exactly as the harness does: JSON on stdin, event as argv[1].

    Runs under `sys.executable` rather than the script's own shebang (now
    `#!/usr/bin/python3`, the fixed system interpreter the hooks config
    actually execs this under — `platform/decisions/0090`'s Handoff) so the
    rest of this suite exercises the module under whatever interpreter is
    running it; `test_imports_under_the_system_python3` below covers the
    shebang's own interpreter specifically.

    BUILDER_ROOT is set so the hook's run-records root, builder_root.runs_root(),
    resolves under tmp_path instead of the real run corpus; HOME alone no longer
    does, because the resolver reads the passwd home. This literal environment
    drops conftest's BUILDER_ROOT, so it must carry its own: without it the hook
    wrote these tests' records into the machine's real agent-runs (seen while
    moving run records onto the resolver, 2026-09-11).
    """
    # The black-box routing resolves the registry and crew homes from the
    # machine's root, which ignores HOME, so the hook is handed an explicit
    # --root-config naming tmp_home/Convivy. The directory must exist, or
    # root() falls back to the real machine root and the real registry.
    try:
        (tmp_home / "Convivy").mkdir(parents=True, exist_ok=True)
        cfg = tmp_home / "session-agent.env"
        cfg.write_text(f"CREWS_ROOT={tmp_home / 'Convivy'}\n")
    except OSError:
        # tmp_home is read-only in the unwritable-log test. An empty scratch
        # root elsewhere has no registry, so routing is UNKNOWN and nothing
        # real is read.
        alt = Path(tempfile.mkdtemp())
        (alt / "Convivy").mkdir()
        cfg = alt / "session-agent.env"
        cfg.write_text(f"CREWS_ROOT={alt / 'Convivy'}\n")
    env = {"HOME": str(tmp_home), "BUILDER_ROOT": str(tmp_home / "Convivy"),
           "PATH": "/usr/bin:/bin"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(HOOK), event, "--root-config", str(cfg)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _log_dir(tmp_home):
    """The single day directory. Filtered to directories because the log root
    also holds the `.metadata_never_index` marker."""
    root = tmp_home / "Convivy" / "agent-runs" / "hook-log"
    days = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    assert len(days) == 1, f"expected exactly one day dir, got {days}"
    return days[0]


def test_spotlight_opt_out_marker_is_created(tmp_path):
    """Spotlight indexes as root, so 0600 does not keep prompt text out of the
    index. The marker opts the subtree out."""
    _run('{"session_id":"s1","hook_event_name":"Stop"}', tmp_path)
    root = tmp_path / "Convivy" / "agent-runs" / "hook-log"
    assert (root / ".metadata_never_index").is_file()


MALFORMED = [
    pytest.param('{"session_id": broken', id="malformed-json"),
    pytest.param("", id="empty-stdin"),
    pytest.param("[1,2,3]", id="json-array-not-object"),
    pytest.param("null", id="json-null"),
    pytest.param('"a bare string"', id="json-string"),
    pytest.param('{"hook_event_name":"Stop"}', id="missing-session-id"),
    pytest.param('{"session_id":12345}', id="non-string-session-id"),
    pytest.param('{"session_id":null}', id="null-session-id"),
    pytest.param('{"session_id":{"nested":"object"}}', id="object-session-id"),
    pytest.param("\x00\x01\x02", id="control-bytes"),
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_exit_zero_on_malformed_input(payload, tmp_path):
    """The contract that keeps a bad payload from wedging a session."""
    assert _run(payload, tmp_path).returncode == 0


def test_exit_zero_when_log_dir_is_unwritable(tmp_path):
    """A read-only HOME must not turn into a nonzero exit."""
    tmp_path.chmod(0o500)
    try:
        result = _run('{"session_id":"s1","hook_event_name":"Stop"}', tmp_path)
        assert result.returncode == 0
    finally:
        tmp_path.chmod(0o700)


def test_writes_one_line_of_valid_json(tmp_path):
    _registry(tmp_path, {})  # readable registry, no private crews: cwd is not redacted
    _run('{"session_id":"s1","hook_event_name":"Stop","cwd":"/tmp"}', tmp_path)
    lines = (_log_dir(tmp_path) / "s1.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "Stop"
    assert record["payload"]["cwd"] == "/tmp"
    assert "ts" in record and "ppid" in record


def test_sessions_write_to_separate_files(tmp_path):
    """Seven concurrent sessions must never share an append target."""
    for sid in ("alpha", "beta", "gamma"):
        _run(f'{{"session_id":"{sid}","hook_event_name":"Stop"}}', tmp_path)
    written = sorted(p.name for p in _log_dir(tmp_path).glob("*.jsonl"))
    assert written == ["alpha.jsonl", "beta.jsonl", "gamma.jsonl"]


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("../../../etc/passwd", id="parent-traversal"),
        pytest.param("/etc/passwd", id="absolute-path"),
        pytest.param("a/b/c", id="subdirectory"),
        pytest.param("..", id="dotdot"),
    ],
)
def test_session_id_cannot_escape_the_log_dir(hostile, tmp_path):
    """`session_id` arrives from the payload; treat it as untrusted filename input."""
    _run(json.dumps({"session_id": hostile, "hook_event_name": "Stop"}), tmp_path)
    for path in _log_dir(tmp_path).iterdir():
        assert path.parent == _log_dir(tmp_path)
        assert path.is_file()


def test_oversized_payload_is_truncated_with_a_marker(tmp_path):
    payload = json.dumps(
        {"session_id": "big", "hook_event_name": "Stop", "last_assistant_message": "x" * 200_000}
    )
    _run(payload, tmp_path)
    record = json.loads((_log_dir(tmp_path) / "big.jsonl").read_text())
    assert record["payload"]["_truncated"] is True
    assert record["payload"]["_original_bytes"] > 200_000
    assert record["payload"]["session_id"] == "big"


def test_truncation_ceiling_holds_when_the_copied_fields_are_also_huge(tmp_path):
    """The replacement record carries fields through from the payload. If those
    are copied unbounded, the ceiling is nominal: a 200KB `cwd` produces a 200KB
    'truncated' line."""
    payload = json.dumps(
        {
            "session_id": "big",
            "hook_event_name": "Stop",
            "cwd": "/" + "d" * 200_000,
            "tool_name": "T" * 200_000,
            "last_assistant_message": "x" * 200_000,
        }
    )
    _run(payload, tmp_path)
    written = (_log_dir(tmp_path) / "big.jsonl").read_text()
    assert len(written) < 4_000, f"truncated line was {len(written)} bytes"
    record = json.loads(written)
    assert record["payload"]["_truncated"] is True
    assert len(record["payload"]["cwd"]) <= 256
    assert len(record["payload"]["tool_name"]) <= 256


@pytest.mark.parametrize(
    "field,source",
    [
        pytest.param("hook_event_name", "payload", id="event-from-payload"),
        pytest.param("TMUX_PANE", "env", id="env-value"),
    ],
)
def test_ceiling_holds_for_fields_that_live_outside_payload(field, source, tmp_path):
    """The truncation branch only rebuilds `payload`. Anything stored outside it
    — the event name, the captured env — escapes the ceiling unless it is capped
    at construction, so a single oversized value there defeats the invariant."""
    # 65KB, not 200KB. Linux caps a single execve string at MAX_ARG_STRLEN
    # (PAGE_SIZE * 32 = 128KB), so a 200KB env value kills the child with E2BIG
    # before the hook runs and the assertion never fires. macOS's limit is
    # higher, which is why this passed locally and failed on CI. 65KB still
    # exceeds MAX_LINE, so the invariant under test is unchanged.
    huge = "z" * 65_000
    env = {"HOME": str(tmp_path), "BUILDER_ROOT": str(tmp_path / "Convivy"),
           "PATH": "/usr/bin:/bin"}
    body = {"session_id": "s1", "last_assistant_message": "x" * 100_000}
    if source == "payload":
        body[field] = huge
    else:
        env[field] = huge

    subprocess.run(
        [sys.executable, str(HOOK), "Stop"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
    )
    written = (_log_dir(tmp_path) / "s1.jsonl").read_text()
    assert len(written) < 4_000, f"line was {len(written)} bytes; ceiling defeated via {field}"


def test_log_files_are_not_readable_by_other_users(tmp_path):
    """`UserPromptSubmit` payloads carry the user's full typed prompt, so these
    files must not rest on the parent directory's mode for confidentiality."""
    _run('{"session_id":"s1","hook_event_name":"UserPromptSubmit"}', tmp_path)
    log_file = _log_dir(tmp_path) / "s1.jsonl"
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert _log_dir(tmp_path).stat().st_mode & 0o777 == 0o700


def test_day_directory_and_timestamp_use_the_same_clock(tmp_path):
    """A local-time directory against a UTC timestamp files a record logged just
    after local midnight under tomorrow's date while its `ts` reads yesterday."""
    _run('{"session_id":"s1","hook_event_name":"Stop"}', tmp_path)
    day_dir = _log_dir(tmp_path)
    record = json.loads((day_dir / "s1.jsonl").read_text())
    assert record["ts"].startswith(day_dir.name)


def test_event_name_falls_back_to_argv_when_payload_lacks_it(tmp_path):
    """The registration passes the event as argv[1] so a payload schema change
    that drops `hook_event_name` still lands labeled rather than as unknown."""
    _run('{"session_id":"s1"}', tmp_path, event="UserPromptSubmit")
    record = json.loads((_log_dir(tmp_path) / "s1.jsonl").read_text())
    assert record["event"] == "UserPromptSubmit"


def test_appends_rather_than_overwrites(tmp_path):
    for _ in range(3):
        _run('{"session_id":"s1","hook_event_name":"Stop"}', tmp_path)
    assert len((_log_dir(tmp_path) / "s1.jsonl").read_text().splitlines()) == 3


# --- the co-session identity record (Deck WS1.2) ------------------------------

def _co_dirs(tmp_home):
    runs = Path(tmp_home) / "Convivy" / "agent-runs"
    return sorted(p for p in runs.rglob("co-*") if p.is_dir()) if runs.exists() else []


def test_session_start_writes_an_identity_record(tmp_path):
    _registry(tmp_path, {})  # readable registry, no private crews: private_home is None
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    r = _run(json.dumps({"session_id": sid, "cwd": "/srv/convivy/substrate",
                         "model": "claude-opus-4-8",
                         "transcript_path": "/x/y.jsonl"}), tmp_path, event="SessionStart")
    assert r.returncode == 0
    dirs = _co_dirs(tmp_path)
    assert [d.name for d in dirs] == [f"co-{sid}"]
    meta = json.loads((dirs[0] / "meta.json").read_text())
    assert meta["kind"] == "co-session"
    assert meta["cwd"] == "/srv/convivy/substrate"
    assert meta["product"] == "substrate"          # derived from cwd
    assert meta["model"] == "claude-opus-4-8"
    assert meta["transcript_path"] == "/x/y.jsonl"
    assert meta["started_at"] and meta["heartbeat_at"]


def test_stop_refreshes_the_heartbeat_without_rewriting_identity(tmp_path):
    _registry(tmp_path, {})  # readable registry, no private crews: private_home is None
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    _run(json.dumps({"session_id": sid, "cwd": "/a/substrate", "model": "m"}),
         tmp_path, event="SessionStart")
    d = _co_dirs(tmp_path)[0]
    before = json.loads((d / "meta.json").read_text())
    r = _run(json.dumps({"session_id": sid, "cwd": "/somewhere/else", "model": "OTHER"}),
             tmp_path, event="Stop")
    assert r.returncode == 0
    after = json.loads((d / "meta.json").read_text())
    assert after["started_at"] == before["started_at"]
    assert after["model"] == "m"                   # identity is not rewritten
    assert after["cwd"] == "/a/substrate"
    assert after["heartbeat_at"] >= before["heartbeat_at"]


def test_a_stop_for_a_session_that_predates_the_emitter_writes_nothing(tmp_path):
    """Creating a record from a Stop would invent a start time it does not know.

    Every session running when this ships is in exactly that state.
    """
    r = _run(json.dumps({"session_id": "never-started", "cwd": "/a/b"}),
             tmp_path, event="Stop")
    assert r.returncode == 0
    assert _co_dirs(tmp_path) == []


def test_other_events_write_no_record(tmp_path):
    r = _run(json.dumps({"session_id": "s1", "cwd": "/a/b"}),
             tmp_path, event="UserPromptSubmit")
    assert r.returncode == 0
    assert _co_dirs(tmp_path) == []


@pytest.mark.parametrize("payload", ["", "not json", "[]", "null", '{"session_id": ""}'])
def test_a_malformed_sessionstart_still_exits_zero_and_writes_no_record(tmp_path, payload):
    """The exit-code contract outranks the record: a SessionStart hook that fails
    would wedge the session it is trying to describe."""
    r = _run(payload, tmp_path, event="SessionStart")
    assert r.returncode == 0
    assert _co_dirs(tmp_path) == []


def test_a_hostile_session_id_cannot_escape_the_runs_tree(tmp_path):
    r = _run(json.dumps({"session_id": "../../../../etc/pwned", "cwd": "/a/b"}),
             tmp_path, event="SessionStart")
    assert r.returncode == 0
    assert not Path("/etc/pwned").exists()
    for d in _co_dirs(tmp_path):
        assert ".." not in str(d)


def test_stop_finds_a_record_filed_under_a_previous_day(tmp_path):
    """THE MIDNIGHT BUG THIS PINS.

    Computing the day at Stop time points at TODAY's tree, but a session that
    began yesterday is filed under yesterday — the lookup missed and the heartbeat
    froze at the SessionStart timestamp, so a long-running session read as one
    that never checked in. Sessions here routinely span midnight.
    """
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    runs = tmp_path / "Convivy" / "agent-runs"
    old = runs / "2001-01-01" / f"co-{sid}"          # a day that is certainly not today
    old.mkdir(parents=True)
    (old / "meta.json").write_text(json.dumps({
        "kind": "co-session", "session_id": sid,
        "started_at": "2001-01-01T23:59:00+00:00",
        "heartbeat_at": "2001-01-01T23:59:00+00:00",
        "cwd": "/a/substrate", "model": "m"}), encoding="utf-8")

    r = _run(json.dumps({"session_id": sid, "cwd": "/a/substrate"}), tmp_path, event="Stop")

    assert r.returncode == 0
    after = json.loads((old / "meta.json").read_text())
    assert after["heartbeat_at"] > "2001-01-01T23:59:00+00:00", (
        "the heartbeat did not refresh — Stop looked only in today's tree")
    assert after["started_at"] == "2001-01-01T23:59:00+00:00"   # identity intact


def test_sessionstart_suppresses_spotlight_over_the_runs_tree(tmp_path):
    """The runs tree holds prompts/user.md and transcript.jsonl.

    The hook-log tree suppresses Spotlight because the indexer runs as root and
    copies content into an index the 0600 mode does not reach. The same threat
    applies to the runs tree, which had no marker.
    """
    _run(json.dumps({"session_id": "s1", "cwd": "/a/b"}), tmp_path, event="SessionStart")
    runs = tmp_path / "Convivy" / "agent-runs"
    assert (runs / ".metadata_never_index").exists()


def test_sessionstart_pins_the_runs_tree_to_owner_only(tmp_path):
    """0700 by intent, not by luck of which tool created the directory first."""
    runs = tmp_path / "Convivy" / "agent-runs"
    runs.mkdir(parents=True)
    runs.chmod(0o755)                                   # a laxer pre-existing tree
    _run(json.dumps({"session_id": "s1", "cwd": "/a/b"}), tmp_path, event="SessionStart")
    assert (runs.stat().st_mode & 0o777) == 0o700


# --- black box crews (ADR-0043): private content must not reach the shared root ---


def _registry(tmp_home, entries):
    """Write a projects.json where the hook expects it, under the redirected HOME."""
    cfg = tmp_home / "Convivy" / "agent-platform" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "projects.json").write_text(json.dumps(entries), encoding="utf-8")


def _private_registry(tmp_home):
    crew = tmp_home / "Convivy" / "CFO"
    crew.mkdir(parents=True, exist_ok=True)
    _registry(tmp_home, {
        "_meta": {"note": "underscore keys are metadata, not projects"},
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "vivy": {"home_dir": "~/Convivy/vivy"},
    })
    return crew


def _crew_log_lines(crew):
    days = [p for p in (crew / "hook-log").iterdir() if p.is_dir()]
    assert len(days) == 1, f"expected one day dir under the crew, got {days}"
    files = list(days[0].glob("*.jsonl"))
    assert len(files) == 1, f"expected one session file, got {files}"
    return files[0].read_text(encoding="utf-8")


def _shared_root(tmp_home):
    return tmp_home / "Convivy" / "agent-runs" / "hook-log"


def test_a_private_crews_prompt_never_reaches_the_shared_root(tmp_path):
    """The measured leak: 14 CFO sessions holding 147,005 characters of verbatim
    prompt and assistant text in the shared run root, outside every ADR-0043 layer.

    The record is REDIRECTED rather than dropped — the operator's ledger-split rule puts a
    black box crew's detailed record inside the crew's own directory.
    """
    crew = _private_registry(tmp_path)
    secret = "ACCOUNT 12345678 ROUTING 87654321"
    _run(json.dumps({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(crew), "prompt": secret}), tmp_path,
         event="UserPromptSubmit")

    assert secret in _crew_log_lines(crew), "the crew must keep its own record"
    root = _shared_root(tmp_path)
    spilled = "".join(p.read_text(encoding="utf-8")
                      for p in root.rglob("*.jsonl")) if root.exists() else ""
    assert secret not in spilled, "private content reached the shared root"


def test_a_symlinked_path_into_a_private_crew_is_still_private(tmp_path):
    """Comparison is by inode, not by string.

    `bin/co`'s transport wall shipped with a string comparison and a symlink walked
    straight through it (security-reviewer, PR #409). The same mistake here would
    put the prompt in the shared root while the session sat in the real directory.
    """
    crew = _private_registry(tmp_path)
    link = tmp_path / "shortcut"
    link.symlink_to(crew)
    secret = "SORT CODE 00-11-22"
    _run(json.dumps({"session_id": "s2", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(link), "prompt": secret}), tmp_path,
         event="UserPromptSubmit")

    assert secret in _crew_log_lines(crew)
    root = _shared_root(tmp_path)
    spilled = "".join(p.read_text(encoding="utf-8")
                      for p in root.rglob("*.jsonl")) if root.exists() else ""
    assert secret not in spilled


def test_a_differently_cased_path_into_a_private_crew_is_still_private(tmp_path):
    """The case this suite was missing, and the reason the comparison is by inode.

    `os.path.realpath` resolves symlinks but does NOT canonicalise case on macOS,
    whose boot volume is case-insensitive by default. So `cd ~/convivy/cfo` lands in
    the real directory with the typed case surviving into the resolved path, and a
    string comparison against the registry's `~/Convivy/CFO` fails while the session
    sits in the crew.

    Found by mutation-testing: replacing `os.path.samefile` with `==` left every
    other test in this file passing, which meant the inode comparison was untested
    and could have been "simplified" away by anyone who tried.
    """
    crew = _private_registry(tmp_path)
    miscased = tmp_path / "Convivy" / "cfo"
    if not miscased.is_dir():
        pytest.skip("case-sensitive filesystem: the lowercase path is a different dir")
    secret = "BSB 062-000 ACCT 12345678"
    _run(json.dumps({"session_id": "s7", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(miscased), "prompt": secret}), tmp_path,
         event="UserPromptSubmit")

    assert secret in _crew_log_lines(crew)
    root = _shared_root(tmp_path)
    spilled = "".join(p.read_text(encoding="utf-8")
                      for p in root.rglob("*.jsonl")) if root.exists() else ""
    assert secret not in spilled


def test_a_subdirectory_of_a_private_crew_is_private(tmp_path):
    """A session in a subdirectory belongs to the same crew, so the walk goes up."""
    crew = _private_registry(tmp_path)
    sub = crew / "statements" / "2026"
    sub.mkdir(parents=True)
    secret = "CARD 4111111111111111"
    _run(json.dumps({"session_id": "s3", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(sub), "prompt": secret}), tmp_path,
         event="UserPromptSubmit")

    assert secret in _crew_log_lines(crew)


def test_a_root_config_that_names_no_root_makes_every_answer_unknown(tmp_path):
    """`--root-config` must not quietly fall back to the machine's real root.
    `builder_root.root()` never raises: every config below comes back as its
    fallback, the real root, so the hook used to route against the real
    registry, and a prompt from a directory no real crew owns was written in full
    (#545: round 1 named the missing file and key, round 2 the empty value and
    the too-wide ones). With no root it cannot tell, so every answer is UNKNOWN
    and content is stripped. Each case carries its own secret, so a failure
    names the case that leaked.

    On a machine whose real root holds no registry, as on the CI runner, the
    fallback is UNKNOWN anyway, so this discriminates only where the real
    registry exists."""
    ordinary = tmp_path / "Convivy" / "vivy"
    ordinary.mkdir(parents=True)
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    cases = {"no-root": "SOMETHING_ELSE=1\n", "empty": "CREWS_ROOT=\n",
             "relative": "CREWS_ROOT=relative/crews\n", "slash": "CREWS_ROOT=/\n",
             "home": "CREWS_ROOT=~\n", "not-a-dir": f"CREWS_ROOT={a_file}\n"}
    configs = {"no-such": tmp_path / "no-such.env"}
    for name, text in cases.items():
        configs[name] = tmp_path / f"{name}.env"
        configs[name].write_text(text)

    for name, cfg in configs.items():
        r = subprocess.run(
            [sys.executable, str(HOOK), "UserPromptSubmit", "--root-config", str(cfg)],
            input=json.dumps({"session_id": f"s-{name}",
                              "hook_event_name": "UserPromptSubmit",
                              "cwd": str(ordinary), "prompt": f"STRIP-ME-{name}-424242"}),
            capture_output=True, text=True,
            env={"HOME": str(tmp_path), "BUILDER_ROOT": str(tmp_path / "Convivy"),
                 "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr

    written = "".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in tmp_path.rglob("*.jsonl") if p.is_file())
    assert written, "the hook must still write its records, stripped"
    leaked = [name for name in configs if f"STRIP-ME-{name}-424242" in written]
    assert leaked == [], f"these configs routed against the real root: {leaked}"


def test_an_ordinary_crew_still_writes_to_the_shared_root(tmp_path):
    """The redirect must not swallow everyone else's instrumentation."""
    _private_registry(tmp_path)
    ordinary = tmp_path / "Convivy" / "vivy"
    ordinary.mkdir(parents=True)
    _run(json.dumps({"session_id": "s4", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(ordinary), "prompt": "ordinary work"}), tmp_path,
         event="UserPromptSubmit")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert "ordinary work" in written


def test_an_unreadable_registry_strips_content_rather_than_guessing(tmp_path):
    """ADR-0043: a privacy control must not be the thing that fails open.

    With no registry the hook cannot tell which crew a session belongs to, so it
    must not assume public. Writing nothing would trade a privacy failure for an
    availability one across the whole fleet, so the event lands and its content
    does not.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)
    secret = "IBAN GB00BARC00000000000000"
    _run(json.dumps({"session_id": "s5", "hook_event_name": "UserPromptSubmit",
                     "cwd": str(crew), "prompt": secret}), tmp_path,
         event="UserPromptSubmit")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert secret not in written, "content survived an undeterminable crew"
    assert "registry-unreadable" in written, "the event itself should still land"
    assert "s5" in written


@pytest.mark.parametrize(
    "cwd_field",
    [
        pytest.param({}, id="absent"),
        pytest.param({"cwd": None}, id="none"),
        pytest.param({"cwd": 12345}, id="non-string"),
        pytest.param({"cwd": ""}, id="empty-string"),
    ],
)
def test_a_missing_cwd_strips_content_rather_than_guessing_not_private(cwd_field, tmp_path):
    """The leak reviewer found on PR #413 (run-bb8a514a): with a valid, readable
    registry but a `cwd` that can't be checked against it, `_private_home_for`
    used to return `None` — which `main()` reads as "not private" — instead of
    `_UNKNOWN`, so a private crew's prompt with an unusable `cwd` wrote straight
    through to the shared root unredacted.

    `test_the_exit_code_contract_holds_for_every_new_path` covers this same input
    shape but only asserts exit code 0, which the pre-fix leak also satisfied —
    exit code and content-safety are different properties.
    """
    _private_registry(tmp_path)  # readable registry, at least one private crew
    secret = "SECRET ACCOUNT 999888"
    payload = {"session_id": "s8", "hook_event_name": "UserPromptSubmit", "prompt": secret}
    payload.update(cwd_field)
    _run(json.dumps(payload), tmp_path, event="UserPromptSubmit")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert secret not in written, "content survived a cwd that could not be checked"
    assert "s8" in written


def test_tool_input_is_redacted_when_the_registry_is_unreadable(tmp_path):
    """The second, non-blocking finding on the same review: the registry-unreadable
    fail-closed path only stripped `prompt`/`user_prompt`/`last_assistant_message`/
    `message`, but this hook is also registered on `PreToolUse` for
    `AskUserQuestion`/`ExitPlanMode` (~/.claude/settings.json), whose content lives
    in `tool_input` instead. An unreadable registry at exactly one of those events
    used to leave `tool_input` unredacted in the shared root.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # crew home exists, but no projects.json — unreadable registry
    secret = "SHOULD WE WIRE $50,000 TO ACCOUNT 999888?"
    payload = {
        "session_id": "s9",
        "hook_event_name": "PreToolUse",
        "cwd": str(crew),
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": secret},
    }
    _run(json.dumps(payload), tmp_path, event="PreToolUse")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert secret not in written, "tool_input survived an undeterminable crew"
    assert "registry-unreadable" in written, "the event itself should still land"
    assert "s9" in written


def test_cwd_is_redacted_in_the_primary_write_when_the_registry_is_unreadable(tmp_path):
    """The blocking finding on PR #413 (run-72daaa55): `_strip_content` stripped
    `prompt`/`user_prompt`/`last_assistant_message`/`message`/`tool_input` but
    never touched `cwd` — the one field `_private_home_for` itself reads to
    identify a crew (the module's own comment: "the only signal about which
    crew a session belongs to is `payload.cwd`"). A `registry-unreadable` event
    used to write the crew's real `cwd` straight into the shared root even
    though its content fields read `[redacted: ...]` right next to it.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # crew home exists, but no projects.json: unreadable registry
    payload = {
        "session_id": "s10",
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(crew),
        "prompt": "irrelevant",
    }
    _run(json.dumps(payload), tmp_path, event="UserPromptSubmit")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert str(crew) not in written, "the real crew cwd reached the shared root"
    record = json.loads(
        "".join(p.read_text(encoding="utf-8") for p in _shared_root(tmp_path).rglob("*.jsonl"))
    )
    assert record["payload"]["cwd"] == "[redacted: crew privacy could not be determined]"


def test_transcript_path_is_redacted_in_the_primary_write_when_the_registry_is_unreadable(tmp_path):
    """`transcript_path` is a live pointer at the crew's full verbatim transcript —
    `_co_record`'s own docstring already treats it as identifying, content-pointing
    information alongside `cwd`. It was never in `_CONTENT_FIELDS` either, so it
    also survived unredacted in the primary write on the identical `_UNKNOWN` path.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # crew home exists, but no projects.json: unreadable registry
    payload = {
        "session_id": "s11",
        "hook_event_name": "Stop",
        "cwd": str(crew),
        "transcript_path": "/srv/convivy/CFO/transcript.jsonl",
        "last_assistant_message": "irrelevant",
    }
    _run(json.dumps(payload), tmp_path, event="Stop")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert "/srv/convivy/CFO/transcript.jsonl" not in written
    record = json.loads(written)
    assert record["payload"]["transcript_path"] == "[redacted: crew privacy could not be determined]"


def test_a_private_crews_identity_record_never_reaches_the_shared_runs_root(tmp_path):
    """The blocking finding on PR #413 (run-73e19889): `_co_record` wrote straight
    to `RUNS_ROOT` for every session regardless of crew, so a CFO or Ask session's
    cwd, product, model, and transcript_path — a live pointer at that session's
    full verbatim transcript — landed on the shared dashboard surface ADR-0043
    says a black box crew must not appear on. Same redirect as the primary write:
    the record belongs inside the crew's own directory, not the shared root.
    """
    crew = _private_registry(tmp_path)
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    r = _run(json.dumps({"session_id": sid, "cwd": str(crew), "model": "claude-opus-4-8",
                         "transcript_path": "/x/y.jsonl"}), tmp_path, event="SessionStart")
    assert r.returncode == 0

    assert _co_dirs(tmp_path) == [], "the identity record must not land under the shared runs root"

    crew_co_dirs = sorted(p for p in (crew / "agent-runs").rglob("co-*") if p.is_dir())
    assert [d.name for d in crew_co_dirs] == [f"co-{sid}"]
    meta = json.loads((crew_co_dirs[0] / "meta.json").read_text())
    assert meta["cwd"] == str(crew)
    assert meta["transcript_path"] == "/x/y.jsonl"

    r = _run(json.dumps({"session_id": sid, "cwd": str(crew), "model": "claude-opus-4-8"}),
             tmp_path, event="Stop")
    assert r.returncode == 0
    assert _co_dirs(tmp_path) == [], "the Stop heartbeat must not land under the shared runs root either"
    after = json.loads((crew_co_dirs[0] / "meta.json").read_text())
    assert after["heartbeat_at"] >= meta["heartbeat_at"]


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param("unreadable_registry", id="registry-unreadable"),
        pytest.param("unresolvable_cwd", id="cwd-unresolvable"),
    ],
)
def test_sessionstart_identity_record_is_redacted_when_private_home_is_unknown(setup, tmp_path):
    """The gap on PR #413 (run-c7ea4a4b): the primary spool write strips content on
    `_UNKNOWN` via `_strip_content`, but `_co_record` wrote `cwd`/`product`/
    `transcript_path` verbatim into the shared runs root regardless of WHY the
    directory redirect happened — "confirmed not private" (`None`) and "cannot
    tell" (`_UNKNOWN`) landed in the same directory with the same content. A
    SessionStart with `cwd` pointed at a crew directory with no readable
    `projects.json` used to write the real cwd, product, and full transcript path
    into the shared root; reviewer reproduced this directly.
    """
    if setup == "unreadable_registry":
        crew = tmp_path / "Convivy" / "CFO"
        crew.mkdir(parents=True)  # crew home exists, but no projects.json: unreadable registry
        cwd = str(crew)
    else:
        _private_registry(tmp_path)  # readable registry, at least one private crew
        cwd = ""  # unresolvable: _private_home_for treats an empty cwd as _UNKNOWN

    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    r = _run(json.dumps({
        "session_id": sid,
        "cwd": cwd,
        "model": "claude-opus-4-8",
        "transcript_path": "/x/y/transcript.jsonl",
    }), tmp_path, event="SessionStart")
    assert r.returncode == 0

    dirs = _co_dirs(tmp_path)
    assert [d.name for d in dirs] == [f"co-{sid}"], "the record must still land, just redacted"
    meta = json.loads((dirs[0] / "meta.json").read_text())
    assert meta["cwd"] == "[redacted: crew privacy could not be determined]"
    assert meta["product"] == "[redacted: crew privacy could not be determined]"
    assert meta["transcript_path"] == "[redacted: crew privacy could not be determined]"
    assert meta["model"] == "claude-opus-4-8", "model does not identify a crew, so it survives"


def test_stop_does_not_reveal_a_cwd_redacted_at_sessionstart(tmp_path):
    """Stop only refreshes the heartbeat and never rewrites identity (pinned by
    `test_stop_refreshes_the_heartbeat_without_rewriting_identity`), so a record
    redacted at SessionStart because `private_home` was `_UNKNOWN` must still read
    redacted after a Stop with the same undeterminable crew.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # no projects.json: unreadable registry for both events
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    _run(json.dumps({"session_id": sid, "cwd": str(crew), "model": "m"}),
         tmp_path, event="SessionStart")
    d = _co_dirs(tmp_path)[0]
    redacted = json.loads((d / "meta.json").read_text())
    assert redacted["cwd"] == "[redacted: crew privacy could not be determined]"

    r = _run(json.dumps({"session_id": sid, "cwd": str(crew), "model": "m"}),
             tmp_path, event="Stop")
    assert r.returncode == 0
    after = json.loads((d / "meta.json").read_text())
    assert after["cwd"] == "[redacted: crew privacy could not be determined]"
    assert after["heartbeat_at"] >= redacted["heartbeat_at"]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"session_id": "s12", "cwd": "CREW_HOME", "prompt": "SECRET", broken', id="malformed-json"),
        pytest.param('["s12", "CREW_HOME", "SECRET"]', id="json-array-not-object"),
    ],
)
def test_unparsed_payload_is_redacted_when_the_registry_is_unreadable(raw, tmp_path):
    """The gap on PR #413 (run-a4882834): when stdin is not valid JSON, or is
    valid JSON that isn't an object, `main()` stores the entire raw body under
    `payload["_unparsed"]` — which `_strip_content` never touched, since it only
    knows the fixed key names in `_CONTENT_FIELDS`/`_IDENTITY_FIELDS`. On the
    exact `_UNKNOWN` (registry-unreadable) path this whole file exists to guard,
    the raw prompt text and the real cwd landed verbatim in the shared root
    under a `"registry-unreadable: content stripped"` marker that was false for
    this case. `test_exit_zero_on_malformed_input` covers these same input
    shapes but only asserts exit code, which the pre-fix leak also satisfied.
    """
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # crew home exists, but no projects.json: unreadable registry
    secret = "SECRET ACCOUNT 999888"
    payload = raw.replace("CREW_HOME", str(crew)).replace("SECRET", secret)

    _run(payload, tmp_path, event="UserPromptSubmit")

    written = "".join(p.read_text(encoding="utf-8")
                      for p in _shared_root(tmp_path).rglob("*.jsonl"))
    assert secret not in written, "raw stdin body survived under _unparsed"
    assert str(crew) not in written, "the real crew cwd survived under _unparsed"
    assert "registry-unreadable" in written, "the event itself should still land"


def test_the_exit_code_contract_holds_for_every_new_path(tmp_path):
    """A Stop hook that exits non-zero wedges the session. That contract outranks
    everything else this file does, including this feature."""
    crew = _private_registry(tmp_path)
    for cwd in (str(crew), "/nonexistent/path", ""):
        r = _run(json.dumps({"session_id": "s6", "hook_event_name": "Stop",
                             "cwd": cwd, "last_assistant_message": "x"}), tmp_path)
        assert r.returncode == 0, f"non-zero exit for cwd={cwd!r}"


# --- the tmux session of the pane a co session runs in (CQ·392) ---------------

TMUX = shutil.which("tmux")
TMUX_SID = "0199c7de-1234-4abc-8def-0123456789ab"


@pytest.fixture
def pane():
    """Start a session on a private tmux server and return the env a hook gets inside it.

    The server lives on its own socket, and TMUX is left out of every call, so the
    operator's default server is never reached. The socket directory comes from
    `mkdtemp` rather than pytest's tmp_path: macOS caps a Unix socket path near 104
    bytes, and the pytest path alone exceeds that. `start(name)` returns the TMUX and
    TMUX_PANE a process in that session would inherit.
    """
    if TMUX is None:
        pytest.skip("tmux is not installed")
    tmux_dir = Path(tempfile.mkdtemp(prefix="hk-"))
    sock = f"hook-test-{uuid.uuid4().hex[:8]}"
    base = {"PATH": "/usr/bin:/bin", "TMUX_TMPDIR": str(tmux_dir)}

    def tmux(*args):
        return subprocess.run([TMUX, "-L", sock, *args], capture_output=True, text=True, env=base)

    def start(name):
        made = tmux("new-session", "-d", "-s", name, "sleep 3600")
        assert made.returncode == 0, made.stderr
        def field(fmt):
            return tmux("display-message", "-p", "-t", name, fmt).stdout.strip()
        return {"PATH": f"{os.path.dirname(TMUX)}:/usr/bin:/bin",
                "TMUX": f"{field('#{socket_path}')},{field('#{pid}')},0",
                "TMUX_PANE": field("#{pane_id}")}

    yield start
    tmux("kill-server")
    shutil.rmtree(tmux_dir, ignore_errors=True)


def test_sessionstart_inside_a_pane_records_the_pane_session_name(pane, tmp_path):
    _registry(tmp_path, {})
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": "/a/substrate"}), tmp_path,
             event="SessionStart", extra_env=pane("crew_ok"))
    assert r.returncode == 0
    meta = json.loads((_co_dirs(tmp_path)[0] / "meta.json").read_text())
    assert meta["tmux_session"] == "crew_ok"


def test_sessionstart_outside_a_pane_omits_the_tmux_session_field(tmp_path):
    _registry(tmp_path, {})
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": "/a/substrate"}), tmp_path,
             event="SessionStart")
    assert r.returncode == 0
    meta = json.loads((_co_dirs(tmp_path)[0] / "meta.json").read_text())
    assert "tmux_session" not in meta


def test_a_pane_name_outside_the_charset_is_omitted_and_the_record_still_lands(pane, tmp_path):
    _registry(tmp_path, {})
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": "/a/substrate"}), tmp_path,
             event="SessionStart", extra_env=pane("bad name"))
    assert r.returncode == 0
    meta = json.loads((_co_dirs(tmp_path)[0] / "meta.json").read_text())
    assert meta["session_id"] == TMUX_SID
    assert "tmux_session" not in meta


def test_a_private_crews_tmux_session_is_written_to_its_own_record(pane, tmp_path):
    crew = _private_registry(tmp_path)
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": str(crew)}), tmp_path,
             event="SessionStart", extra_env=pane("crew_ok"))
    assert r.returncode == 0
    assert _co_dirs(tmp_path) == [], "the record must not reach the shared runs root"
    crew_dirs = sorted(p for p in (crew / "agent-runs").rglob("co-*") if p.is_dir())
    meta = json.loads((crew_dirs[0] / "meta.json").read_text())
    assert meta["tmux_session"] == "crew_ok"


def test_the_tmux_session_is_redacted_when_the_crew_cannot_be_determined(pane, tmp_path):
    """On the unknown-crew path the name is blanked like cwd, so the crew's pane is not shown."""
    crew = tmp_path / "Convivy" / "CFO"
    crew.mkdir(parents=True)  # no projects.json: unreadable registry
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": str(crew)}), tmp_path,
             event="SessionStart", extra_env=pane("crew_ok"))
    assert r.returncode == 0
    text = (_co_dirs(tmp_path)[0] / "meta.json").read_text()
    assert json.loads(text)["tmux_session"] == "[redacted: crew privacy could not be determined]"
    assert "crew_ok" not in text


def _hook_module():
    spec = importlib.util.spec_from_file_location("session_event_log", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    return hook


def test_the_hook_name_charset_is_the_one_tmux_transcript_uses():
    assert _hook_module()._TMUX_NAME_RE.pattern == TT.SESSION_NAME_RE.pattern


def test_the_hook_name_charset_refuses_a_trailing_newline():
    assert _hook_module()._TMUX_NAME_RE.fullmatch("crew_ok\n") is None


def _stub_tmux(tmp_path, body):
    """A `tmux` the hook finds first on PATH. Its body runs for the display-message call.

    It logs each call, so a test can tell the failure path it means to cover was the
    one reached and not a skipped call.
    """
    bin_dir = tmp_path / "tmux-stub"
    bin_dir.mkdir()
    calls = tmp_path / "tmux-calls.log"
    stub = bin_dir / "tmux"
    stub.write_text(f"#!/bin/sh\necho called >> '{calls}'\n{body}\n")
    stub.chmod(0o755)
    return bin_dir, calls


def _record_without_a_tmux_name(tmp_path, env):
    """Run SessionStart in a pane whose tmux call fails; the record must land without the name."""
    _registry(tmp_path, {})
    r = _run(json.dumps({"session_id": TMUX_SID, "cwd": "/a/substrate"}), tmp_path,
             event="SessionStart", extra_env={"TMUX_PANE": "%0", **env})
    assert r.returncode == 0
    meta = json.loads((_co_dirs(tmp_path)[0] / "meta.json").read_text())
    assert meta["session_id"] == TMUX_SID
    assert "tmux_session" not in meta


def test_a_missing_tmux_binary_omits_the_field_and_the_record_still_lands(tmp_path):
    empty = tmp_path / "no-tmux"
    empty.mkdir()
    _record_without_a_tmux_name(tmp_path, {"PATH": str(empty)})


def test_a_display_message_that_exits_non_zero_omits_the_field_and_the_record_still_lands(tmp_path):
    bin_dir, calls = _stub_tmux(tmp_path, "echo crew_ok\nexit 1")  # a name on stdout, refused by exit status
    _record_without_a_tmux_name(tmp_path, {"PATH": str(bin_dir)})
    assert calls.read_text().split() == ["called"]


def test_a_display_message_past_two_seconds_omits_the_field_and_the_record_still_lands(tmp_path):
    bin_dir, calls = _stub_tmux(tmp_path, "exec /bin/sleep 30")  # absolute: PATH is the stub dir alone
    started = time.monotonic()
    _record_without_a_tmux_name(tmp_path, {"PATH": str(bin_dir)})
    assert calls.read_text().split() == ["called"]
    # The hook's own two-second bound ends the call. The 30-second sleep would hold
    # the hook far longer, so a bound this loose still tells the two apart.
    assert time.monotonic() - started < 15


# --- runs under the system interpreter the hooks config actually invokes ----

def test_imports_under_the_system_python3(tmp_path):
    """This hook is registered in the hooks config with no interpreter named
    (`platform/decisions/0090`'s Handoff), so Claude Code execs the file by
    its own shebang, `#!/usr/bin/python3` — macOS and Linux's fixed,
    root-owned system interpreter, 3.9 on the mini that actually runs these
    hooks. Run directly under that binary rather than `sys.executable` (this
    repo's newer venv interpreter, which `_run` above uses): a 3.10+-only
    construct in this file would pass every other test here and still crash
    at import under the interpreter actually used.

    This alone does not prove the property everywhere this suite runs:
    `/usr/bin/python3` is whatever version the HOST ships, which on CI's
    `convivy-linux` runner is 3.10+ (not verified on ci1), not 3.9 — see
    `test_the_pinned_file_parses_under_python_3_9_grammar` below for the
    check that holds regardless of which interpreter actually runs it."""
    (tmp_path / "Convivy").mkdir()
    cfg = tmp_path / "session-agent.env"
    cfg.write_text(f"CREWS_ROOT={tmp_path / 'Convivy'}\n")
    proc = subprocess.run(
        ["/usr/bin/python3", str(HOOK), "Stop", "--root-config", str(cfg)],
        input=json.dumps({"session_id": "s1", "hook_event_name": "Stop"}),
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "BUILDER_ROOT": str(tmp_path / "Convivy"),
             "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr


def test_the_pinned_file_parses_under_python_3_9_grammar():
    """The check above runs under whatever `/usr/bin/python3` is on the HOST,
    which is 3.9 only on the mini — CI's `convivy-linux` runner ships 3.10+ (not verified on ci1), so a
    reintroduced 3.10-only construct (a `match` statement, `except*`, ...)
    would pass every test in this file there and still crash on the mini.
    `ast.parse`'s `feature_version` gates the grammar the parser accepts, not
    which interpreter is running it, so this holds the same on every host —
    confirmed by planting a `match` statement here and watching it fail under
    both this repo's venv interpreter and a real 3.9 one.

    This does not catch every possible 3.10-only construct — a runtime-only
    difference such as a bare `int | None` evaluated outside a deferred
    annotation is semantics, not grammar, and `ast.parse` cannot see it — but
    it is the mechanism that actually fails in CI, which a fixed-interpreter
    subprocess call does not."""
    ast.parse(HOOK.read_text(), filename=str(HOOK), feature_version=(3, 9))
