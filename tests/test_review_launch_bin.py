"""`bin/review-launch`'s `ClaudeSession` — the real-I/O adapter around
`lib/agent-platform/review_launch.py`'s testable core (platform/decisions/0090,
platform/specs/builder-runs-as-two-accounts).

Round 2 of #732, findings 1 and 2: the session must drop from root to the
`reviewers` account before `claude -p` starts, build `HOME` from THAT
account's own passwd entry (never root's), and never let the subprocess
inherit the sudo caller's stdin. `getpwnam` and `run` are injected so none
of this needs a real `reviewers` account, real root privileges, or a real
subprocess — see `ClaudeSession.__init__`'s docstring.

Round 3 of #732 adds `getgrouplist` to that same injection list (finding 1 —
the real `os.getgrouplist("reviewers", ...)` default would be exercised
against whatever (if anything) a given test machine happens to have at that
account name, which is exactly the determinism these tests exist to avoid;
see `fake_pwent`/`_FAKE_GROUPS` below).

ROUND 4 OF #732, FINDING 1 REMOVES `chown` FROM THIS LIST ENTIRELY:
`ClaudeSession.run` no longer chowns anything — that moved into
`review_launch.prepare_run_dir`, tested in `tests/test_review_launch.py` —
so `ClaudeSession.__init__` no longer accepts a `chown=` parameter at all,
and the three tests that used to exercise the old chown walk here
(`test_run_gives_the_reviewers_account_write_access_to_the_run_directory`,
`..._read_access_to_files_prepare_run_dir_wrote`, `..._skips_chowning_a_
symlink_planted_in_the_run_directory`) are gone with it, replaced below by
`test_run_leaves_the_run_directorys_ownership_and_mode_untouched`.

ROUND 4 OF #732, FINDING 2 changes what `run` returns, from a bare exit code
to `(exit_code, verdict)`: the verdict is now read from the session's own
captured stdout via `review_launch.extract_verdict`, never a file.

ROUND 5 OF #732, FINDING 2 replaces the `run=subprocess.run` injection with
`popen=subprocess.Popen` plus two more injection points, `select_fn` and
`monotonic`: at round 4's head (309d2e38), `ClaudeSession.run` handed the
whole call to `subprocess.run(stdout=subprocess.PIPE, timeout=...)`, which
buffers the ENTIRE child stdout in memory before this method ever sees any
of it — so `RL.MAX_SESSION_STDOUT_BYTES` only bounded what `extract_verdict`
later refused to parse, never what root actually read. `run` now reads the
child's stdout itself, a chunk at a time, via a fake `popen` whose `.stdout`
is a plain `io.BytesIO` (never a real pipe or process) and a fake `select_fn`
that decides per call whether the stream is "ready" — which is what makes a
multi-chunk read, an overflow past the byte cap, or a session stuck past the
deadline all exercisable without any real time passing or any real I/O.
`_RecordingRun`/`subprocess.TimeoutExpired` are gone with the `subprocess.run`
call they stood in for; `_RecordingPopen`/`_FakeProc` below replace them.

ROUND 7 OF #732 adds three real-subprocess tests alongside `test_run_kills_a_
real_session_that_writes_once_then_stalls`/`..._whose_slow_trickle_exceeds_
the_deadline` above, for the security reviewer's round-6 follow-up finding:
`test_run_kills_a_child_that_outlives_its_already_exited_leader` and
`test_run_kills_a_grandchild_still_alive_after_a_clean_eof` (the group-kill
family — `_kill_process_group` calling `os.killpg(os.getpgid(pid), ...)`
instead of `os.killpg(pid, ...)` directly, and `run`'s own `finally` block
skipping the kill entirely on a clean EOF), and `test_run_returns_within_
the_deadline_when_the_pipe_closes_late` (the closing `wait()`'s budget
reusing a stale `remaining` instead of a fresh clock read). A fourth,
`test_sigterm_to_the_launcher_leaves_no_group_member_alive`, drives
`ClaudeSession.run` from a standalone driver script (never through `main`,
which needs a real `reviewers` account) to exercise the new `_handle_
terminate`/`_Terminated` signal handling end to end.

ROUND 8 OF #732 adds two more real-subprocess tests for the security
reviewer's round-7 follow-up finding, each pinned to its window by a
deterministic hook (`ClaudeSession`'s new `after_popen=`/`before_group_kill=`
test seams) rather than a race against a timer:
`test_a_signal_pending_during_the_startup_window_still_kills_the_group`
(a signal deferred by the round-8 `pthread_sigmask` block, delivered the
instant it is unblocked) and
`test_a_second_signal_during_cleanup_does_not_abort_the_group_kill` (a
second signal fired from the top of `run`'s own `finally`, which the
round-8 `SIG_IGN` fix in `_handle_terminate` must silently drop)."""
from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import types

import pytest


@pytest.fixture
def fake_pwent():
    return types.SimpleNamespace(pw_dir="/var/empty/reviewers", pw_uid=501, pw_gid=501)


# A fixed, deterministic stand-in for the `reviewers` account's real
# supplementary groups (round 3 of #732, finding 1) — never the real
# `os.getgrouplist("reviewers", ...)`, which on a machine with no such
# account returns whatever the platform happens to do with an unknown name
# (observed on this machine: a garbage sentinel GID), not an empty or
# single-group list a real `reviewers` account would actually carry.
_FAKE_GROUPS = [501]


def _fake_getgrouplist(seen=None):
    def _get(user, group):
        if seen is not None:
            seen.append((user, group))
        return _FAKE_GROUPS
    return _get


def _result_stdout(text: str) -> bytes:
    return (json.dumps({"type": "result", "result": text}) + "\n").encode("utf-8")


def _always_ready(stream, timeout):
    """A `select_fn` that reports every stream ready on every call — stands
    in for `ClaudeSession`'s real `_readable`, which `select.select`s a real
    file descriptor `io.BytesIO` does not have one of."""
    return True


def _never_ready(stream, timeout):
    """A `select_fn` that reports every stream NOT ready — used to prove the
    deadline, not a data arrival, is what ends the read loop."""
    return False


class _FakeProc:
    """Stands in for the object `subprocess.Popen(...)` returns. `.stdout` is
    whatever file-like object the test hands it (normally `io.BytesIO`);
    `.kill()`/`.wait()` just record that they were called rather than
    signalling or reaping a real process."""

    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class _RecordingPopen:
    """Stands in for `subprocess.Popen` itself: records the call and hands
    back a fixed, pre-built `_FakeProc` regardless of what it was called
    with."""

    def __init__(self, proc):
        self.proc = proc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.proc


def test_run_closes_the_sessions_stdin(review_launch_bin, tmp_path, fake_pwent):
    """Finding 1: whatever is sitting on the sudo caller's own stdin must
    never reach `claude -p` — at head a3cd844c, `ClaudeSession.run` called
    `subprocess.run(cmd, env=env, cwd=str(cwd))` with no `stdin=` at all,
    which inherits the parent's stdin unchanged."""
    proc = _FakeProc(io.BytesIO(b""))
    recorder = _RecordingPopen(proc)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(), select_fn=_always_ready)

    session.run(cmd=["claude", "-p", "task"], env={}, cwd=tmp_path / "run" / "worktree")

    assert len(recorder.calls) == 1
    _cmd, kwargs = recorder.calls[0]
    assert "stdin" in kwargs, "ClaudeSession.run must pass stdin= explicitly"
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_run_drops_to_the_reviewers_account(review_launch_bin, tmp_path, fake_pwent):
    """Finding 2: the session must run as the `reviewers` account, not root
    (this process's own identity, since it is reached only through `sudo`)
    — at head a3cd844c, `ClaudeSession.run` called `subprocess.run(cmd, env=env,
    cwd=str(cwd))` with no `user=`/`group=` at all, so the child stayed root."""
    proc = _FakeProc(io.BytesIO(b""))
    recorder = _RecordingPopen(proc)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(), select_fn=_always_ready)

    session.run(cmd=["claude", "-p", "task"], env={}, cwd=tmp_path / "run" / "worktree")

    _cmd, kwargs = recorder.calls[0]
    assert kwargs.get("user") == review_launch_bin.REVIEWERS_ACCOUNT
    assert kwargs.get("group") == review_launch_bin.REVIEWERS_ACCOUNT


def test_run_passes_the_reviewers_accounts_own_groups_as_extra_groups(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 3 of #732, finding 1: at head e9c10f2f `ClaudeSession.run`
    called `subprocess.run(..., user=..., group=...)` with no
    `extra_groups=` at all. CPython only calls `setgroups()` in the child
    when `extra_groups` is given — without it, the session keeps every
    supplementary group THIS (root) process belongs to, `admin` included
    (platform/decisions/0090's catch: `admin` can write `/opt/homebrew/bin`,
    this very interpreter's own directory), even though `user=`/`group=`
    changed its uid/gid to `reviewers`. `run` must look the real account's
    groups up BY NAME (`getgrouplist(REVIEWERS_ACCOUNT, pw_gid)`), not pass
    a fixed or empty list, so a future `reviewers` group membership change
    is picked up without a code change here."""
    seen = []
    proc = _FakeProc(io.BytesIO(b""))
    recorder = _RecordingPopen(proc)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(seen), select_fn=_always_ready)

    session.run(cmd=["claude", "-p", "task"], env={}, cwd=tmp_path / "run" / "worktree")

    assert seen == [(review_launch_bin.REVIEWERS_ACCOUNT, fake_pwent.pw_gid)]
    _cmd, kwargs = recorder.calls[0]
    assert kwargs.get("extra_groups") == _FAKE_GROUPS, (
        "ClaudeSession.run must pass extra_groups=, or the child keeps "
        "root's own supplementary groups (admin included)")


def test_home_reads_the_reviewers_accounts_own_passwd_entry(
        review_launch_bin, tmp_path, fake_pwent):
    """Finding 2: `HOME` (and so `CLAUDE_CONFIG_DIR`'s own base path) must
    come from the `reviewers` account's own passwd entry, never from this
    (root) process's `$HOME` or `os.path.expanduser('~')` — at head
    a3cd844c, `ClaudeSession.__init__` took `home=Path(os.path.expanduser('~'))`
    from the caller, which under `sudo` resolves to root's own home."""
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(_FakeProc(io.BytesIO(b""))), getgrouplist=_fake_getgrouplist())

    assert session.home() == review_launch_bin.Path(fake_pwent.pw_dir)


def test_home_is_looked_up_by_the_reviewers_account_name_not_a_fixed_path(
        review_launch_bin, tmp_path):
    """The lookup must actually key on `REVIEWERS_ACCOUNT` — a fake
    `getpwnam` that ignores its argument and a real one that only answers
    for the right name would look identical from `home()` alone; this pins
    the argument `getpwnam` is called with."""
    seen = []

    def _getpwnam(name):
        seen.append(name)
        return types.SimpleNamespace(pw_dir="/var/empty/reviewers", pw_uid=501, pw_gid=501)

    review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=_getpwnam,
        popen=_RecordingPopen(_FakeProc(io.BytesIO(b""))), getgrouplist=_fake_getgrouplist())

    assert seen == [review_launch_bin.REVIEWERS_ACCOUNT]


def test_owner_returns_the_accounts_uid_and_gid(review_launch_bin, tmp_path, fake_pwent):
    """Round 4 of #732, finding 1: `review_launch.main` calls
    `session.owner()` to know who `prepare_run_dir` should chown the run's
    fixed children to — it must read the SAME passwd entry `home()` does,
    not a separate or fixed value."""
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(_FakeProc(io.BytesIO(b""))), getgrouplist=_fake_getgrouplist())

    assert session.owner() == (fake_pwent.pw_uid, fake_pwent.pw_gid)


def test_claude_session_no_longer_accepts_a_chown_parameter_at_all(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 4 of #732, finding 1: at head 09642bed `ClaudeSession.__init__`
    accepted `chown=os.chown` (defaulted, so even a caller that never passed
    one got the real syscall) and `run` used it to chown the run directory
    itself to `reviewers` before populating it — the one precondition every
    attack the round-4 security review found needed. Removing the parameter
    altogether, rather than just not calling it, is what makes that
    capability unreachable from this class rather than merely unused."""
    with pytest.raises(TypeError):
        review_launch_bin.ClaudeSession(
            oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
            popen=_RecordingPopen(_FakeProc(io.BytesIO(b""))), getgrouplist=_fake_getgrouplist(),
            chown=lambda *a, **kw: None)


def test_run_leaves_the_run_directorys_ownership_and_mode_untouched(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 4 of #732, finding 1: at head 09642bed `ClaudeSession.run`
    itself chowned the run directory (`cwd.parent`) to `reviewers` before
    populating it — ownership now moves entirely into
    `review_launch.prepare_run_dir` (tested in `tests/test_review_launch.py`),
    which never re-owns the run directory at all. `run` must leave whatever
    it is handed exactly as it found it — no chown, no mkdir, no mode
    change — since by the time it runs, `prepare_run_dir` has already
    created and owned everything the session needs."""
    run_dir = tmp_path / "runs" / "run-xyz"
    (run_dir / "worktree").mkdir(parents=True)
    os.chmod(run_dir, 0o710)
    before = run_dir.stat()

    proc = _FakeProc(io.BytesIO(b""))
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(proc), getgrouplist=_fake_getgrouplist(), select_fn=_always_ready)
    session.run(cmd=["claude"], env={}, cwd=run_dir / "worktree")

    after = run_dir.stat()
    assert (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) == \
           (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode))


def test_run_captures_stdout_and_extracts_the_verdict_from_it(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 4 of #732, finding 2: the verdict comes back on the session's
    OWN stdout, read by THIS call, parsed by `review_launch.extract_verdict`
    — never a file under the run directory."""
    proc = _FakeProc(io.BytesIO(_result_stdout("VERDICT: APPROVE")), returncode=0)
    recorder = _RecordingPopen(proc)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(), select_fn=_always_ready)

    exit_code, verdict = session.run(cmd=["claude"], env={}, cwd=tmp_path / "run" / "worktree")

    assert exit_code == 0
    assert verdict == "APPROVE"
    _cmd, kwargs = recorder.calls[0]
    assert kwargs.get("stdout") is subprocess.PIPE, "run must capture stdout to read the verdict from"


def test_run_reassembles_a_result_delivered_across_several_chunks(
        review_launch_bin, tmp_path, fake_pwent):
    """The read loop must not assume one `read()` call returns the whole
    message — `io.BytesIO.read(n)` here only ever returns `n` bytes at a
    time, forcing `run` to loop and concatenate exactly as a real pipe
    delivering the session's output in pieces would."""
    payload = _result_stdout("VERDICT: REQUEST_CHANGES")

    class _OneByteAtATime(io.BytesIO):
        def read(self, n=-1):  # noqa: ARG002 — deliberately ignores n
            return super().read(1)

    proc = _FakeProc(_OneByteAtATime(payload), returncode=0)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(proc), getgrouplist=_fake_getgrouplist(), select_fn=_always_ready)

    exit_code, verdict = session.run(cmd=["claude"], env={}, cwd=tmp_path / "run" / "worktree")

    assert exit_code == 0
    assert verdict == "REQUEST_CHANGES"


def test_run_times_out_only_once_the_deadline_actually_passes(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 5 of #732: `run`'s deadline is `self._monotonic() +
    RL.SESSION_TIMEOUT_S`, read once up front — not a `timeout=` kwarg
    handed to `Popen`, which accepts no such argument at all now that `run`
    uses `subprocess.Popen` instead of `subprocess.run`. A clock that has
    not yet reached the deadline must not report a timeout exit code; one
    that has must.

    Round 7 of #732 drops this test's own former `and not proc.killed` half
    of the first assertion — `_kill_process_group` now runs on EVERY exit
    from `run`'s read loop, clean EOF included (see `test_run_kills_a_
    grandchild_still_alive_after_a_clean_eof`), so `proc.killed` no longer
    distinguishes a clean return from a timed-out one; `exit_code` still
    does, which is what this test is actually about."""
    proc = _FakeProc(io.BytesIO(b""))
    session_timeout = review_launch_bin.RL.SESSION_TIMEOUT_S

    # Three values: the deadline computation, the loop's one `remaining`
    # check, and (round 7 of #732) `finally`'s own fresh read for
    # `wait_budget` — never just the first two, now that `wait_budget` no
    # longer reuses the loop's last `remaining` snapshot.
    clock = iter([0.0, session_timeout - 1, session_timeout - 1])
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(proc), getgrouplist=_fake_getgrouplist(),
        select_fn=_always_ready, monotonic=lambda: next(clock))
    # Before the deadline: the stream has no data, so the loop reads EOF
    # (an empty `io.BytesIO`) and returns normally rather than timing out.
    exit_code, verdict = session.run(cmd=["claude"], env={}, cwd=tmp_path / "run" / "worktree")
    assert exit_code == 0

    proc2 = _FakeProc(io.BytesIO(b""))
    clock2 = iter([0.0, session_timeout + 1, session_timeout + 1])
    session2 = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(proc2), getgrouplist=_fake_getgrouplist(),
        select_fn=_never_ready, monotonic=lambda: next(clock2))
    exit_code2, verdict2 = session2.run(cmd=["claude"], env={}, cwd=tmp_path / "run" / "worktree")
    assert exit_code2 == 124
    assert verdict2 is None
    assert proc2.killed is True


def test_run_refuses_output_past_the_cap_without_holding_more_than_it(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 5 of #732, finding 2: at head 309d2e38 `subprocess.run(stdout=
    subprocess.PIPE)` buffered the ENTIRE child stdout before `extract_verdict`
    ever bounded anything — root held an unbounded amount of a runaway
    session's output for the whole run. `run` must now enforce
    `RL.MAX_SESSION_STDOUT_BYTES` WHILE reading: once the running total would
    exceed it, the session is killed and nothing further is read. A counting
    stream pins the actual property: the TOTAL bytes ever handed to `run` by
    `.read()` never exceeds the cap by more than one byte (the single extra
    byte that proves the cap was crossed at all)."""
    monkeypatch.setattr(review_launch_bin.RL, "MAX_SESSION_STDOUT_BYTES", 10)

    class _CountingStream(io.BytesIO):
        served = 0

        def read(self, n=-1):
            data = super().read(n)
            self.served += len(data)
            return data

    stream = _CountingStream(b"x" * 1000)
    proc = _FakeProc(stream, returncode=0)
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=_RecordingPopen(proc), getgrouplist=_fake_getgrouplist(),
        select_fn=_always_ready, monotonic=lambda: 0.0)

    exit_code, verdict = session.run(cmd=["claude"], env={}, cwd=tmp_path / "run" / "worktree")

    assert exit_code == 125
    assert verdict is None
    assert proc.killed is True
    assert stream.served <= 11, "run must never hold more than the cap (plus one byte) at once"


class _RealPopenDroppingPrivilegeKwargs:
    """Round 6 of #732: every test above fakes `.stdout` as `io.BytesIO`,
    whose `.read(n)` never blocks — so none of them can see a real pipe's
    default `io.BufferedReader.read(n)` blocking past `select`'s readiness
    report (both round-5 reviews). This wraps the REAL `subprocess.Popen`
    instead, dropping `user=`/`group=`/`extra_groups=` before the call (the
    test process is not root, so passing them through would raise
    `PermissionError`) — everything else `ClaudeSession.run` passes,
    including `bufsize=` and `start_new_session=`, reaches the real
    subprocess unchanged, so these tests exercise the actual I/O semantics
    the fakes cannot. Records every `Popen` it creates so a test can check
    the child is no longer alive once `run` returns."""

    def __init__(self):
        self.procs: "list[subprocess.Popen]" = []

    def __call__(self, cmd, **kwargs):
        kwargs.pop("user", None)
        kwargs.pop("group", None)
        kwargs.pop("extra_groups", None)
        proc = subprocess.Popen(cmd, **kwargs)
        self.procs.append(proc)
        return proc


def _kill_all(pids=(), pidfiles=()):
    """Round 9 of #732, the round-8 code review's test-hygiene finding:
    SIGKILLs every pid in `pids`, plus every pid recorded in a file in
    `pidfiles` (a missing or empty file contributes nothing) — shared by
    every real-subprocess test's own `finally` below so a failing assertion
    (the exact run a plant produces) leaves no leader or forked grandchild
    sleeping behind it on the runner. Never kills a pid the test itself did
    not hand it, directly or via one of its own pidfiles."""
    all_pids = list(pids)
    for pidfile in pidfiles:
        try:
            text = pidfile.read_text().strip()
        except (FileNotFoundError, OSError):
            continue
        if text:
            all_pids.append(int(text))
    for pid in all_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_run_kills_a_real_session_that_writes_once_then_stalls(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 6 of #732, both reviews: a session that writes a little output
    and then goes quiet (a hung API connection, a steered child) must not
    wedge `run` past `RL.SESSION_TIMEOUT_S` — at round 5's head (5d13bf54)
    it did, because `Popen`'s default buffered `proc.stdout.read(n)` keeps
    reading until it has `n` bytes or hits EOF, not until it has ANY bytes,
    so the one `.read()` call issued right after `select` reported the pipe
    ready blocked for as long as the child kept it open. Plant: remove
    `bufsize=0` from the `self._popen(...)` call in `bin/review-launch`'s
    `ClaudeSession.run` (restoring the default buffered stdout) and this
    test's `elapsed < session_timeout + margin` assertion goes red — the
    real child used here sleeps 10s after its one write, so a real pipe's
    buffered `.read()` blocks for (most of) that whole 10s rather than the
    ~2s deadline patched below."""
    session_timeout = 2.0
    monkeypatch.setattr(review_launch_bin.RL, "SESSION_TIMEOUT_S", session_timeout)
    recorder = _RealPopenDroppingPrivilegeKwargs()
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist())

    cmd = [sys.executable, "-c",
           "import sys, time\n"
           "sys.stdout.write('x\\n')\n"
           "sys.stdout.flush()\n"
           "time.sleep(10)\n"]
    start = time.monotonic()
    try:
        exit_code, verdict = session.run(cmd=cmd, env={}, cwd=tmp_path)
        elapsed = time.monotonic() - start

        assert elapsed < session_timeout + 3.0, (
            f"run took {elapsed:.2f}s against a {session_timeout}s deadline — "
            "it blocked in a read instead of enforcing the deadline")
        assert exit_code == 124
        assert verdict is None
        assert recorder.procs[-1].poll() is not None, "run must not leave the child running"
    finally:
        _kill_all(pids=[p.pid for p in recorder.procs])


def test_run_kills_a_real_session_whose_slow_trickle_exceeds_the_deadline(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 6 of #732: the same buffered-read bug also bites a session that
    never fully stalls, just trickles output slowly — `self._select` reports
    the pipe ready as soon as the first byte lands, but a buffered
    `.read(min(_READ_CHUNK, ...))` asks for up to 64 KiB and keeps reading
    until it gets that much or hits EOF, so it keeps blocking through
    several more trickled bytes rather than returning after the first one.
    Plant: remove `bufsize=0` from `ClaudeSession.run`'s `self._popen(...)`
    call and this test's `elapsed < session_timeout + margin` assertion goes
    red — the child below writes one byte every 0.5s for 20 writes (10s
    total), so a buffered read blocks for most of that 10s instead of the
    ~2s deadline patched below."""
    session_timeout = 2.0
    monkeypatch.setattr(review_launch_bin.RL, "SESSION_TIMEOUT_S", session_timeout)
    recorder = _RealPopenDroppingPrivilegeKwargs()
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist())

    cmd = [sys.executable, "-c",
           "import sys, time\n"
           "for _ in range(20):\n"
           "    sys.stdout.write('x')\n"
           "    sys.stdout.flush()\n"
           "    time.sleep(0.5)\n"]
    start = time.monotonic()
    try:
        exit_code, verdict = session.run(cmd=cmd, env={}, cwd=tmp_path)
        elapsed = time.monotonic() - start

        assert elapsed < session_timeout + 3.0, (
            f"run took {elapsed:.2f}s against a {session_timeout}s deadline — "
            "a slow trickle blocked a read instead of the deadline ending it")
        assert exit_code == 124
        assert verdict is None
        assert recorder.procs[-1].poll() is not None, "run must not leave the child running"
    finally:
        _kill_all(pids=[p.pid for p in recorder.procs])


def _pid_alive(pid: int) -> bool:
    """`True` iff `pid` still names a process — used below instead of
    `recorder.procs[-1].poll()`, which only ever reaps/reports the named
    leader, never a grandchild it forked that stays in the same group."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_dead(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_run_kills_a_child_that_outlives_its_already_exited_leader(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 7 of #732, the security reviewer's round-6 follow-up finding:
    at round 6's head (e41fcdca) `_kill_process_group` called
    `os.killpg(os.getpgid(pid), SIGKILL)` — looking up `pid`'s own pgid
    before signalling it. On macOS, `os.getpgid(pid)` raises
    `ProcessLookupError` once `pid` (the leader) has exited, EVEN WHILE a
    grandchild it forked is still alive in the same group — the lookup
    fails on the specific, now-gone process asked about, not on the group
    its still-living member keeps alive. The leader below forks a child
    that sleeps far longer than the patched deadline WITHOUT closing its
    own (inherited) copy of the stdout pipe, then exits — reproducing the
    exact state (`ps -g`: leader `<defunct>`, grandchild alive, same pgid)
    the reviewer's standalone script showed on this machine, confirmed
    separately before this fix (see the PR body). Plant: in `_kill_process_
    group`, restore `os.killpg(os.getpgid(pid), SIGKILL)` and this test's
    final `_wait_until_dead(child_pid)` assertion goes red — the exception
    is swallowed, `proc.kill()` only reaps the already-dead leader, and the
    grandchild outlives `run`'s return."""
    session_timeout = 1.5
    monkeypatch.setattr(review_launch_bin.RL, "SESSION_TIMEOUT_S", session_timeout)
    recorder = _RealPopenDroppingPrivilegeKwargs()
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist())

    child_pidfile = tmp_path / "child.pid"
    cmd = [sys.executable, "-c",
           "import os, sys, time\n"
           "pid = os.fork()\n"
           "if pid == 0:\n"
           "    time.sleep(30)\n"
           "    os._exit(0)\n"
           "else:\n"
           f"    open({str(child_pidfile)!r}, 'w').write(str(pid))\n"
           "    sys.stdout.write('x\\n'); sys.stdout.flush()\n"
           "    os._exit(0)\n"]

    try:
        exit_code, verdict = session.run(cmd=cmd, env={}, cwd=tmp_path)

        assert exit_code == 124, "the leader's exit must not read as a clean, verdict-bearing run"
        assert verdict is None
        child_pid = int(child_pidfile.read_text())
        assert _wait_until_dead(child_pid), (
            "the grandchild outlived its already-exited leader past run()'s return")
    finally:
        _kill_all(pids=[p.pid for p in recorder.procs], pidfiles=[child_pidfile])


def test_run_kills_a_grandchild_still_alive_after_a_clean_eof(
        review_launch_bin, tmp_path, fake_pwent):
    """Round 7 of #732, the other half of the same finding (the round-6
    code reviewer's comment on the same PR): at round 6's head (e41fcdca)
    the group kill in `run`'s `finally` block ran only `if not clean_eof` —
    a leader that exits CLEANLY, after its own grandchild has independently
    closed its OWN inherited copy of the stdout pipe (so `run` sees
    ordinary EOF, not a stall or a timeout), got no kill at all, leaving
    the grandchild to keep running behind a finished-looking review. Plant:
    restore the `if not clean_eof:` guard around `_kill_process_group(proc)`
    in `run`'s `finally` block and this test's final `_wait_until_dead`
    assertion goes red."""
    recorder = _RealPopenDroppingPrivilegeKwargs()
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist())

    child_pidfile = tmp_path / "child.pid"
    cmd = [sys.executable, "-c",
           "import os, sys, time\n"
           "pid = os.fork()\n"
           "if pid == 0:\n"
           "    os.close(1)\n"
           "    time.sleep(5)\n"
           "    os._exit(0)\n"
           "else:\n"
           f"    open({str(child_pidfile)!r}, 'w').write(str(pid))\n"
           "    sys.stdout.write('x\\n'); sys.stdout.flush()\n"
           "    sys.stdout.close()\n"
           "    os._exit(0)\n"]

    try:
        exit_code, verdict = session.run(cmd=cmd, env={}, cwd=tmp_path)

        assert exit_code == 0, "the leader's own clean exit must still be reported as such"
        child_pid = int(child_pidfile.read_text())
        assert _wait_until_dead(child_pid), (
            "the grandchild survived a clean EOF from its leader past run()'s return")
    finally:
        _kill_all(pids=[p.pid for p in recorder.procs], pidfiles=[child_pidfile])


def test_run_returns_within_the_deadline_when_the_pipe_closes_late(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 7 of #732, finding 2: at round 6's head (e41fcdca) the closing
    `proc.wait(timeout=wait_budget)` computed `wait_budget` from
    `remaining` — the value the read loop computed at the TOP of its LAST
    iteration, before the `self._select` call that iteration then blocked
    in. A session whose pipe closes late inside that blocked call reaches
    `finally` with `remaining` still holding nearly its full pre-block
    value, even though almost none of that time is actually left before
    `RL.SESSION_TIMEOUT_S` — so the wait below could then run for nearly
    ANOTHER full deadline if the process (as here) keeps running after
    closing its own stdout.

    The child closes stdout on a SENTINEL FILE this test's own timer thread
    creates at `session_timeout - 0.3`s, measured from this test's own
    clock, rather than a fixed `time.sleep` in the child — a child-side
    sleep would add the child interpreter's own, unpredictable startup
    latency on top of whatever delay it slept for, which on this machine
    was enough by itself to push the close past the deadline and mask the
    bug this test exists to catch. Plant: in `run`'s `finally` block,
    revert `wait_budget = max(0.0, deadline - self._monotonic())` to
    `max(0.0, remaining)` (the round-6 code) and this test's `elapsed <
    session_timeout + margin` assertion goes red — the reviewer's own
    standalone script, with a 3s deadline, returned at 5.62s."""
    session_timeout = 3.0
    monkeypatch.setattr(review_launch_bin.RL, "SESSION_TIMEOUT_S", session_timeout)
    recorder = _RealPopenDroppingPrivilegeKwargs()
    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist())

    sentinel = tmp_path / "close_now"
    cmd = [sys.executable, "-c",
           "import os, sys, time\n"
           "sys.stdout.write('x\\n'); sys.stdout.flush()\n"
           f"while not os.path.exists({str(sentinel)!r}):\n"
           "    time.sleep(0.01)\n"
           "os.close(1)\n"  # closes the real fd; sys.stdout.close() alone does not
           "time.sleep(10)\n"]

    timer = threading.Timer(session_timeout - 0.3, sentinel.touch)
    timer.start()
    start = time.monotonic()
    try:
        exit_code, verdict = session.run(cmd=cmd, env={}, cwd=tmp_path)
        elapsed = time.monotonic() - start

        margin = 1.5
        assert elapsed < session_timeout + margin, (
            f"run took {elapsed:.2f}s against a {session_timeout}s deadline — "
            "the final wait reused a stale 'remaining' instead of the time "
            "actually left")
        assert recorder.procs[-1].poll() is not None, "run must not leave the child running"
    finally:
        timer.cancel()
        _kill_all(pids=[p.pid for p in recorder.procs])


_CHILD_SCRIPT = """\
import os, sys, time

with open({leader_pidfile!r}, "w") as f:
    f.write(str(os.getpid()))

pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)

with open({child_pidfile!r}, "w") as f:
    f.write(str(pid))

sys.stdout.write("x\\n")
sys.stdout.flush()
time.sleep(30)
"""

_DRIVER_SCRIPT = """\
import importlib.machinery
import importlib.util
import os
import signal
import subprocess
import sys
import types

loader = importlib.machinery.SourceFileLoader("review_launch_driver", {review_launch_path!r})
spec = importlib.util.spec_from_loader("review_launch_driver", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, mod._handle_terminate)


class _Recorder:
    def __call__(self, cmd, **kwargs):
        kwargs.pop("user", None)
        kwargs.pop("group", None)
        kwargs.pop("extra_groups", None)
        return subprocess.Popen(cmd, **kwargs)


fake_pwent = types.SimpleNamespace(pw_dir="/tmp", pw_uid=os.getuid(), pw_gid=os.getgid())
session = mod.ClaudeSession(
    oauth_token_path=mod.Path("/nonexistent"),
    getpwnam=lambda name: fake_pwent,
    popen=_Recorder(),
    getgrouplist=lambda user, group: [],
)

cmd = [sys.executable, {child_script_path!r}]

print("READY")
sys.stdout.flush()
try:
    exit_code, verdict = session.run(cmd=cmd, env={{}}, cwd={cwd!r})
    print("RETURNED", exit_code)
    sys.stdout.flush()
except mod._Terminated as e:
    print("TERMINATED", e.args[0])
    sys.stdout.flush()
    sys.exit(128 + int(e.args[0]))
"""


def test_sigterm_to_the_launcher_leaves_no_group_member_alive(review_launch_bin, tmp_path):
    """Round 7 of #732, finding 3: `agentctl kill` reaches `bin/review-launch`
    as a plain SIGTERM (what `sudo` forwards unchanged); at round 6's head
    (e41fcdca) the file installed no handler for it at all, so the default
    disposition ended the process at once, running neither `ClaudeSession.
    run`'s process-group kill nor any other cleanup — the session it
    started (here, a leader plus a forked grandchild, both in one process
    group) was left running, orphaned, with no deadline left to end it.

    This drives `ClaudeSession.run` from a standalone driver script that
    installs `_handle_terminate` the same way `main` does — never through
    `main` itself, which needs a real `reviewers` account this test must
    not depend on. Plant: delete the `for sig in (...): signal.signal(sig,
    mod._handle_terminate)` loop from `_DRIVER_SCRIPT` above (mirroring
    deleting the equivalent loop from `bin/review-launch`'s own `main`) and
    this test's final `_wait_until_dead` assertions go red — both the
    leader and the grandchild are still alive long after the SIGTERM below,
    and the driver process itself never prints `TERMINATED` or exits with
    `128 + SIGTERM`, since the default disposition kills it with no Python
    code — this script included — ever running."""
    child_pidfile = tmp_path / "child.pid"
    leader_pidfile = tmp_path / "leader.pid"
    child_script_path = tmp_path / "child.py"
    driver_script_path = tmp_path / "driver.py"

    child_script_path.write_text(_CHILD_SCRIPT.format(
        leader_pidfile=str(leader_pidfile), child_pidfile=str(child_pidfile)))
    driver_script_path.write_text(_DRIVER_SCRIPT.format(
        review_launch_path=review_launch_bin.__file__,
        child_script_path=str(child_script_path), cwd=str(tmp_path)))

    proc = subprocess.Popen([sys.executable, str(driver_script_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        ready = proc.stdout.readline()
        assert ready.strip() == "READY", f"driver did not start cleanly: {ready!r}"

        deadline = time.monotonic() + 5.0
        while not child_pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child_pidfile.exists(), "driver's session never reported its grandchild's pid"
        time.sleep(0.2)  # let the driver reach its second, blocking select() call

        os.kill(proc.pid, signal.SIGTERM)
        outcome = proc.stdout.readline()
        returncode = proc.wait(timeout=10)

        assert outcome.strip() == "TERMINATED 15", outcome
        assert returncode == 128 + signal.SIGTERM

        leader_pid = int(leader_pidfile.read_text())
        child_pid = int(child_pidfile.read_text())
        assert _wait_until_dead(leader_pid), "the session leader survived SIGTERM to the launcher"
        assert _wait_until_dead(child_pid), "the session's grandchild survived SIGTERM to the launcher"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        _kill_all(pidfiles=[leader_pidfile, child_pidfile])


def _install_terminate_handlers(review_launch_bin):
    """Installs `review_launch_bin._handle_terminate` for SIGTERM/SIGHUP/
    SIGINT in THIS (pytest worker) process, the same way `main` installs it
    in a real launch — the two tests below send real signals to
    `os.getpid()` and need THIS process, not a driver subprocess, to react
    to them the way `ClaudeSession.run` assumes. Returns the previous
    handlers so a test can restore them in its own `finally`."""
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        old[sig] = signal.signal(sig, review_launch_bin._handle_terminate)
    return old


def _restore_handlers(old):
    for sig, handler in old.items():
        signal.signal(sig, handler)


_FORK_SLEEP_30_CHILD = """\
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)
else:
    open({child_pidfile!r}, "w").write(str(pid))
    sys.stdout.write("x\\n"); sys.stdout.flush()
    time.sleep(30)
"""


def test_a_signal_pending_during_the_startup_window_still_kills_the_group(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 8 of #732, the security reviewer's round-7 finding 1, the
    startup window: at round 7's head (b9828a78) `run` called `self._popen`
    with no signal masking at all — a SIGTERM/SIGHUP/SIGINT landing inside
    `Popen` itself, or in the gap between it returning and `run`'s own
    `try`/`finally` taking ownership of `proc`, raised `_Terminated` with
    no `finally` yet able to see `proc`, leaving the whole session (leader
    and any grandchild) running with nothing left to end it.

    `after_popen=` fires exactly in that gap — `proc` exists, signals are
    still blocked by round 8's `pthread_sigmask` call, nothing protects
    `proc` yet — so this sends the signal from precisely the point the
    finding names, rather than racing a timer against a ~1.2ms window. The
    leader is a plain `time.sleep(30)` (no fork): the deferral itself is
    what this test is about, not `_kill_process_group`'s own group-kill
    mechanics, which the round-7 fork-based tests above already cover.
    `_handle_terminate` must be installed for real in THIS process, since
    the signal is delivered to `os.getpid()` directly.

    Plant: in `bin/review-launch`'s `ClaudeSession.run`, remove the
    `signal.pthread_sigmask(signal.SIG_BLOCK, block_signals)` call and the
    `try`/`except BaseException` wrapping `self._popen` (restoring the bare
    `proc = self._popen(...)` call, `preexec_fn=` dropped), while leaving
    `self._after_popen(proc)` in place — `after_popen`'s SIGTERM is no
    longer deferred, so `_handle_terminate` fires immediately inside the
    hook call, `_Terminated` propagates out of `run` with NEITHER the
    group-kill `finally` below (not yet entered) NOR a chance to kill
    `proc` ever running, and this test's `_wait_until_dead` assertion goes
    red."""
    old_handlers = _install_terminate_handlers(review_launch_bin)
    recorder = _RealPopenDroppingPrivilegeKwargs()

    def after_popen(proc):
        os.kill(os.getpid(), signal.SIGTERM)

    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(), after_popen=after_popen)

    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

    try:
        try:
            session.run(cmd=cmd, env={}, cwd=tmp_path)
            raise AssertionError("run() must propagate _Terminated for the deferred signal")
        except review_launch_bin._Terminated as e:
            assert e.args[0] == signal.SIGTERM

        leader_pid = recorder.procs[-1].pid
        assert _wait_until_dead(leader_pid), (
            "the session leader survived a signal pending during the startup window")
    finally:
        _restore_handlers(old_handlers)
        _kill_all(pids=[p.pid for p in recorder.procs])


def test_a_second_signal_during_cleanup_does_not_abort_the_group_kill(
        review_launch_bin, tmp_path, fake_pwent, monkeypatch):
    """Round 8 of #732, the security reviewer's round-7 finding 1, the
    second window: at round 7's head (b9828a78) `_handle_terminate` stayed
    installed for the whole of `run`'s `finally`, so a SECOND SIGTERM/
    SIGHUP/SIGINT landing after the first one had already started
    unwinding — before `os.killpg` in `_kill_process_group` got to run —
    fired the handler again from inside the still-executing `finally`,
    raising a fresh `_Terminated` that replaced the one already propagating
    and abandoned the group kill mid-cleanup, with no retry.

    `before_group_kill=` fires as the first statement of `finally`, after
    the FIRST signal (sent by a `threading.Timer` while `run` is blocked in
    a real `select.select` on the child's pipe) has already run
    `_handle_terminate` once — which, with round 8's fix, has already set
    all three signals to `SIG_IGN`. The second signal sent from the hook
    must therefore do nothing, and the group kill that follows it must
    still run.

    Plant: in `bin/review-launch`'s `_handle_terminate`, remove the `for
    sig in (...): signal.signal(sig, signal.SIG_IGN)` loop (restoring the
    bare `raise _Terminated(signum)`) — the hook's SIGHUP then re-enters
    the still-installed handler, raising a NEW `_Terminated(SIGHUP)` from
    inside the hook call, before `_kill_process_group(proc)` on the next
    line ever runs, and this test's `_wait_until_dead` assertions go red."""
    session_timeout = 5.0
    monkeypatch.setattr(review_launch_bin.RL, "SESSION_TIMEOUT_S", session_timeout)
    old_handlers = _install_terminate_handlers(review_launch_bin)
    recorder = _RealPopenDroppingPrivilegeKwargs()
    second_signal_sent = []

    def before_group_kill():
        second_signal_sent.append(True)
        os.kill(os.getpid(), signal.SIGHUP)

    session = review_launch_bin.ClaudeSession(
        oauth_token_path=tmp_path / "token", getpwnam=lambda name: fake_pwent,
        popen=recorder, getgrouplist=_fake_getgrouplist(),
        before_group_kill=before_group_kill)

    child_pidfile = tmp_path / "child.pid"
    cmd = [sys.executable, "-c", _FORK_SLEEP_30_CHILD.format(child_pidfile=str(child_pidfile))]

    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        try:
            session.run(cmd=cmd, env={}, cwd=tmp_path)
            raise AssertionError("run() must propagate the first signal as _Terminated")
        except review_launch_bin._Terminated as e:
            assert e.args[0] == signal.SIGTERM, (
                "a second signal during cleanup must not replace the first one's _Terminated")

        assert second_signal_sent, "before_group_kill never ran — the plant check cannot apply"
        leader_pid = recorder.procs[-1].pid
        assert _wait_until_dead(leader_pid), (
            "a second signal during cleanup left the session leader running")
        child_pid = int(child_pidfile.read_text())
        assert _wait_until_dead(child_pid), (
            "a second signal during cleanup left the grandchild running")
    finally:
        timer.cancel()
        _restore_handlers(old_handlers)
        _kill_all(pids=[p.pid for p in recorder.procs], pidfiles=[child_pidfile])



