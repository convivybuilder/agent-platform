"""session-follow-watchdog — duplicate-follower detection (the 2026-08-12 leak).

The whole point of this watchdog is threshold-free duplicate detection: one
`session-agent state <name> --follow` per tmux session is healthy, and MORE than
one is the leak, regardless of how many sessions or panes happen to exist. These
tests pin that against constructed process listings — text shaped like real `ps
-ax -o pid=,ppid=,command=` output — never a live `ps` call.
"""
from __future__ import annotations


def _ps_line(pid: int, ppid: int, command: str) -> str:
    return f"{pid:>5} {ppid:>5} {command}"


def _follower_cmd(name: str, *, interpreter: str | None = None,
                   path: str = "/srv/convivy/.local/bin/session-agent") -> str:
    prefix = f"{interpreter} " if interpreter else ""
    return f"{prefix}{path} state {name} --follow"


# --- follower_session: command parsing ---------------------------------------


def test_follower_session_matches_direct_invocation(session_follow_watchdog):
    w = session_follow_watchdog
    assert w.follower_session(_follower_cmd("svc")) == "svc"


def test_follower_session_matches_with_interpreter_prefix(session_follow_watchdog):
    w = session_follow_watchdog
    cmd = _follower_cmd("constance", interpreter="python3.11")
    assert w.follower_session(cmd) == "constance"


def test_follower_session_matches_bare_path(session_follow_watchdog):
    w = session_follow_watchdog
    assert w.follower_session(_follower_cmd("ask", path="session-agent")) == "ask"


def test_follower_session_ignores_unrelated_commands(session_follow_watchdog):
    w = session_follow_watchdog
    assert w.follower_session("/bin/zsh -c 'echo hi'") is None
    assert w.follower_session("/usr/libexec/logd") is None


def test_follower_session_reads_the_name_with_the_flag_first(session_follow_watchdog):
    """`--follow` is a store_true, so argparse takes it on either side of the
    positional. Reading the token straight after `state` returns "--follow" as
    the session name for this form — collapsing every session invoked this way
    into one bogus duplicate bucket, and hiding a real duplicate under its own
    name."""
    w = session_follow_watchdog
    cmd = "/srv/convivy/.local/bin/session-agent state --follow sous-chef"
    assert w.follower_session(cmd) == "sous-chef"


def test_follower_session_still_needs_the_follow_flag(session_follow_watchdog):
    w = session_follow_watchdog
    assert w.follower_session("/path/session-agent state sous-chef") is None


def test_follower_session_steps_over_a_value_taking_flag(session_follow_watchdog):
    """`--interval` takes a float, so skipping only dash-prefixed tokens leaves
    its VALUE looking like the session name. Every session invoked this way
    would then collide under one bogus bucket."""
    w = session_follow_watchdog
    cmd = "/path/session-agent state --interval 5 sous-chef --follow"
    assert w.follower_session(cmd) == "sous-chef"


def test_follower_session_handles_the_joined_flag_form(session_follow_watchdog):
    w = session_follow_watchdog
    cmd = "/path/session-agent state --interval=5 sous-chef --follow"
    assert w.follower_session(cmd) == "sous-chef"


def test_follower_session_reads_a_name_that_begins_with_a_dash(session_follow_watchdog):
    """`--` is how argparse takes such a name, and treating the separator as
    one more flag loses the process entirely — worse than misfiling it, because
    a duplicate under that name would never be seen at all."""
    w = session_follow_watchdog
    cmd = "/path/session-agent state --follow -- -foo"
    assert w.follower_session(cmd) == "-foo"


def test_the_value_taking_flag_list_matches_session_agent(session_follow_watchdog):
    """The watchdog restates part of `session-agent state`'s argument spec, so
    it can rot. This reads the real subparser and fails here when a
    value-taking flag is added there, rather than letting the watchdog
    silently misread a session name."""
    import re
    from pathlib import Path

    w = session_follow_watchdog
    src = (Path(__file__).resolve().parent.parent / "bin" / "session-agent").read_text()
    block = src.split('sub.add_parser("state"', 1)[1].split("sub.add_parser(", 1)[0]
    # `[\w-]`, not `[a-z-]`: a flag named with a digit or a capital would match
    # neither side of the comparison below and the test would pass vacuously on
    # the one case it exists for — a value-taking flag added there and forgotten
    # here. A pin that can agree by both sides missing something reads as
    # coverage while providing none.
    value_flags = {
        m.group(1)
        for m in re.finditer(r'add_argument\(\s*"(--[\w-]+)"([^)]*)\)', block, re.S)
        if 'action="store_true"' not in m.group(2)
    }
    assert value_flags, "found no flags at all — the source split or regex stopped matching"
    assert value_flags == set(w._STATE_VALUE_FLAGS), (
        f"`session-agent state` takes values for {sorted(value_flags)}; the "
        f"watchdog knows {sorted(w._STATE_VALUE_FLAGS)}")


def test_follower_session_sanitises_the_name_it_read_from_ps(session_follow_watchdog):
    """The name is argv out of `ps` — subprocess output the platform did not
    author — and it reaches an alert body and meta, so it must be sanitised
    before it leaves this process. Escape sequences are the ones that render."""
    w = session_follow_watchdog
    assert w.follower_session(_follower_cmd("\x1b[31msvc\x1b[0m")) == "svc"


def test_follower_session_bounds_a_pathological_name(session_follow_watchdog):
    w = session_follow_watchdog
    name = w.follower_session(_follower_cmd("s" * 500))
    assert len(name) <= 67  # 64 plus sanitise's literal "..."


def test_follower_session_ignores_non_follow_session_agent_calls(session_follow_watchdog):
    w = session_follow_watchdog
    # `state` without `--follow` (a one-shot poll) must not count as a follower.
    assert w.follower_session(
        "/srv/convivy/.local/bin/session-agent state svc") is None
    # a different verb entirely
    assert w.follower_session(
        "/srv/convivy/.local/bin/session-agent send svc --text -") is None


# --- parse_ps_output: raw ps text -> structured procs -------------------------


def test_parse_ps_output_reads_pid_ppid_command(session_follow_watchdog):
    w = session_follow_watchdog
    text = "\n".join([
        _ps_line(1, 0, "/sbin/launchd"),
        _ps_line(501, 1, _follower_cmd("svc")),
    ])
    procs = w.parse_ps_output(text)
    assert procs == [
        {"pid": 1, "ppid": 0, "command": "/sbin/launchd"},
        {"pid": 501, "ppid": 1, "command": _follower_cmd("svc")},
    ]


def test_parse_ps_output_skips_unparseable_lines(session_follow_watchdog):
    w = session_follow_watchdog
    text = "\n".join(["", "garbage line with no pid", _ps_line(9, 1, "sleep 5")])
    procs = w.parse_ps_output(text)
    assert procs == [{"pid": 9, "ppid": 1, "command": "sleep 5"}]


# --- duplicate_sessions + orphan_count: the detection itself -----------------


def test_one_follower_per_session_is_clean(session_follow_watchdog):
    w = session_follow_watchdog
    procs = [
        {"pid": 100, "ppid": 500, "command": _follower_cmd("svc")},
        {"pid": 101, "ppid": 500, "command": _follower_cmd("constance")},
        {"pid": 102, "ppid": 500, "command": _follower_cmd("ask")},
    ]
    followed = w.followers(procs)
    assert len(followed) == 3
    assert w.duplicate_sessions(followed) == {}


def test_two_followers_for_one_session_is_flagged(session_follow_watchdog):
    w = session_follow_watchdog
    procs = [
        {"pid": 100, "ppid": 500, "command": _follower_cmd("svc")},
        {"pid": 101, "ppid": 1, "command": _follower_cmd("svc")},
        {"pid": 102, "ppid": 500, "command": _follower_cmd("constance")},
    ]
    followed = w.followers(procs)
    dups = w.duplicate_sessions(followed)
    assert set(dups) == {"svc"}
    assert len(dups["svc"]) == 2
    assert {p["pid"] for p in dups["svc"]} == {100, 101}


def test_the_20260812_shape_several_sessions_each_over_20(session_follow_watchdog):
    """The measured incident: svc=28, constance=25, sous-chef=24, ask=21, every
    duplicate follower orphaned (ppid=1), plus a handful of clean sessions."""
    w = session_follow_watchdog
    procs = []
    pid = 1000
    for name, n in (("svc", 28), ("constance", 25), ("sous-chef", 24), ("ask", 21)):
        for _ in range(n):
            procs.append({"pid": pid, "ppid": 1, "command": _follower_cmd(name)})
            pid += 1
    for name in ("clean-one", "clean-two"):
        procs.append({"pid": pid, "ppid": 500, "command": _follower_cmd(name)})
        pid += 1

    followed = w.followers(procs)
    dups = w.duplicate_sessions(followed)

    assert set(dups) == {"svc", "constance", "sous-chef", "ask"}
    assert len(dups["svc"]) == 28
    assert len(dups["constance"]) == 25
    assert len(dups["sous-chef"]) == 24
    assert len(dups["ask"]) == 21
    assert w.orphan_count(followed) == 28 + 25 + 24 + 21


def test_ppid_one_counts_as_orphan(session_follow_watchdog):
    w = session_follow_watchdog
    followed = [
        {"pid": 1, "ppid": 1, "session": "svc"},
        {"pid": 2, "ppid": 500, "session": "svc"},
    ]
    assert w.orphan_count(followed) == 1


def test_no_followers_at_all_is_clean_not_an_error(session_follow_watchdog):
    w = session_follow_watchdog
    procs = [
        {"pid": 1, "ppid": 0, "command": "/sbin/launchd"},
        {"pid": 2, "ppid": 1, "command": "/usr/libexec/logd"},
    ]
    followed = w.followers(procs)
    assert followed == []
    assert w.duplicate_sessions(followed) == {}
    assert w.orphan_count(followed) == 0


# --- main(): dry-run exit codes + no crash on a clean/leaked run -------------


def test_main_dry_run_clean_returns_zero(session_follow_watchdog, monkeypatch):
    w = session_follow_watchdog
    monkeypatch.setattr(w, "run_ps", lambda: _ps_line(1, 0, "/sbin/launchd"))
    assert w.main(["--dry-run"]) == 0


def test_main_dry_run_leak_returns_one_and_posts_nothing(session_follow_watchdog,
                                                          monkeypatch):
    w = session_follow_watchdog
    text = "\n".join([
        _ps_line(100, 500, _follower_cmd("svc")),
        _ps_line(101, 1, _follower_cmd("svc")),
    ])
    monkeypatch.setattr(w, "run_ps", lambda: text)
    posted = []
    monkeypatch.setattr(
        w.signal_client, "post_alert",
        lambda *a, **kw: posted.append((a, kw)))
    assert w.main(["--dry-run"]) == 1
    assert posted == []


def test_main_posts_alert_on_a_real_leak(session_follow_watchdog, monkeypatch):
    w = session_follow_watchdog
    text = "\n".join([
        _ps_line(100, 500, _follower_cmd("svc")),
        _ps_line(101, 1, _follower_cmd("svc")),
    ])
    monkeypatch.setattr(w, "run_ps", lambda: text)
    posted = []
    monkeypatch.setattr(
        w.signal_client, "post_alert",
        lambda stream, body, **kw: posted.append((stream, body, kw)))
    assert w.main([]) == 1
    assert len(posted) == 1
    stream, body, kw = posted[0]
    assert stream == w.ALERT_STREAM
    assert "svc" in body
    assert kw["meta"]["duplicate_sessions"] == {"svc": 2}
    assert kw["meta"]["orphan_count"] == 1
    assert kw["meta"]["follower_count"] == 2


# --- the check could not run is not the same answer as nothing found ---------


def test_a_failed_ps_is_none_not_empty(session_follow_watchdog, monkeypatch):
    """`ps` on a working host always lists at least launchd, so "" would be
    evidence the look failed — which is a different answer from a clean scan
    and must not be collapsed into one."""
    w = session_follow_watchdog

    def boom(*a, **kw):
        raise OSError("no ps")

    monkeypatch.setattr(w.subprocess, "run", boom)
    assert w.run_ps() is None


def test_a_nonzero_ps_is_none(session_follow_watchdog, monkeypatch):
    w = session_follow_watchdog

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(w.subprocess, "run", lambda *a, **kw: Result())
    assert w.run_ps() is None


def test_an_empty_but_successful_ps_is_none(session_follow_watchdog, monkeypatch):
    """`ps -ax` on a live host cannot exit 0 with no output, so this enforces
    in code what the docstring asserts rather than trusting the platform."""
    w = session_follow_watchdog

    class Result:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(w.subprocess, "run", lambda *a, **kw: Result())
    assert w.run_ps() is None


def test_main_degrades_loudly_when_it_cannot_look(session_follow_watchdog,
                                                   monkeypatch):
    """The failure that matters: reporting all-clear on a check that never ran.
    The measured incident stalled a health check for 9.5s, inside run_ps's own
    10s timeout, so this is a plausible shape for a recurrence to take."""
    w = session_follow_watchdog
    monkeypatch.setattr(w, "run_ps", lambda: None)
    posted = []
    monkeypatch.setattr(
        w.signal_client, "post_alert",
        lambda stream, body, **kw: posted.append((stream, body, kw)))
    assert w.main([]) == 2
    assert len(posted) == 1
    _, body, kw = posted[0]
    assert "not an all-clear" in body
    assert kw["meta"]["alert"] == "session-follow-watchdog-blind"


def test_main_posts_nothing_when_blind_under_dry_run(session_follow_watchdog,
                                                      monkeypatch):
    w = session_follow_watchdog
    monkeypatch.setattr(w, "run_ps", lambda: None)
    posted = []
    monkeypatch.setattr(
        w.signal_client, "post_alert",
        lambda stream, body, **kw: posted.append((stream, body, kw)))
    assert w.main(["--dry-run"]) == 2
    assert posted == []
