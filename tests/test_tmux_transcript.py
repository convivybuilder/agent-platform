"""tmux session name -> transcript path (Deck's session pane).

THE BUG THESE PIN. The join that looks right is cwd -> newest .jsonl, and it
returns the wrong session whenever two claude processes share a directory — a
computer-use fork outranked the real `deck` session by mtime on 2026-07-22.
So the tests that matter are the ones where a decoy exists: a newer transcript,
a sibling claude process, a fork that is not under the pane. Identity by pid must
win every one of them.

Nothing here shells out. The process tree and the run dirs are both injected, so
the logic is tested rather than the machine it happened to run on.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture
def TT():
    spec = importlib.util.spec_from_file_location("tmux_transcript", LIB / "tmux_transcript.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A fake `~/.claude/projects` plus a helper that lands a transcript in it.

    Real files under a real home, rather than a patched `is_file`, because
    containment is now part of what these tests exercise: a recorded path that
    resolves outside this tree must be refused.
    """
    home = tmp_path / "home"
    root = home / ".claude" / "projects"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    def land(project: str, session_id: str) -> Path:
        d = root / project
        d.mkdir(exist_ok=True)
        path = d / f"{session_id}.jsonl"
        path.write_text("")
        return path

    land.root = root
    land.home = home
    return land


@pytest.fixture
def runs(tmp_path, TT, monkeypatch):
    """A run-dirs root with a helper for writing co-session records.

    `private_homes` is neutralized to `[]` by default so every test using this
    fixture searches ONLY the fake `RUNS_ROOT` — never the real machine's
    `~/Convivy/agent-platform/config/projects.json`, which exists and names a
    real private crew on the dev box these tests also run on. A test that
    wants private-crew search coverage overrides it explicitly (see
    `test_a_private_crews_run_dir_is_found_without_knowing_cwd_first`).
    """
    root = tmp_path / "agent-runs"
    monkeypatch.setattr(TT, "RUNS_ROOT", root)
    monkeypatch.setattr(TT.PC, "private_homes", lambda: [])

    def write(day: str, session_id: str, ppid: int, transcript: Path | None = None, **extra):
        d = root / day / f"co-{session_id}"
        d.mkdir(parents=True)
        meta = {"kind": "co-session", "session_id": session_id, "ppid": ppid}
        if transcript is not None:
            meta["transcript_path"] = str(transcript)
        meta.update(extra)
        (d / "meta.json").write_text(json.dumps(meta))
        return d

    return write


def _today(TT):
    from datetime import date
    return date.today().isoformat()


def _tree(TT, monkeypatch, children: dict[int, list[int]], commands: dict[int, str]):
    """Inject a process tree instead of shelling out to pgrep/ps."""
    monkeypatch.setattr(TT, "_children", lambda pid: children.get(pid, []))
    monkeypatch.setattr(TT, "_command_of", lambda pid: commands.get(pid, "/bin/zsh -l"))


# MARK: - Session name validation


def test_rejects_a_session_name_that_could_not_be_a_tmux_argument(TT):
    for name in ["steward; rm -rf ~", "$(whoami)", "a b", "", "x" * 65]:
        with pytest.raises(TT.JoinError):
            TT.pane_pids(name)


# MARK: - Exact-name resolution (`-t` is a pattern, not a name)
#
# `tmux list-panes -t <name>` resolves `-t` as a target PATTERN: tried as a
# session id, then an exact session name, then the START of a session name.
# A target `zzprobe` therefore answers for a real session `zzprobe-second`
# even though no session named `zzprobe` exists — reproduced on this machine,
# tmux 3.6a, twice independently:
#
#     tmux new-session -d -s zzprobe-second
#     tmux list-panes -t zzprobe    -F '#{session_name}'  -> zzprobe-second
#     tmux list-panes -t '=zzprobe' -F '#{session_name}'  -> zzprobe-second
#
# `=` does not fix it, as the second line shows, despite being the documented
# exact-match anchor — do not reach for it.
#
# `fake_run` below implements that same prefix-matching resolution rule
# (verified against the live probe above) rather than hardcoding one target
# string, so it is a faithful stand-in for real tmux, not an invented answer.


def _fake_tmux(sessions: dict[str, str], pane_id: str = "%1", pane_pid: str = "999"):
    """A minimal fake tmux: `sessions` maps name -> id (`$N`). `list-panes -t`
    answers exactly the way real tmux resolves target-session: id-exact, then
    name-exact, then name-prefix. The `-F` format is honoured, matching the
    two distinct shapes `pane_pids` and `panes` each ask for."""
    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            return "\n".join(f"{sid} {name}" for name, sid in sessions.items())
        if argv[:2] == ["tmux", "list-panes"] and "-t" in argv:
            target = argv[argv.index("-t") + 1]
            if target.startswith("$"):
                matched = target in sessions.values()
            elif target in sessions:
                matched = True
            else:
                matched = any(name.startswith(target) for name in sessions)
            if not matched:
                return None
            fmt = argv[argv.index("-F") + 1]
            return pane_pid if fmt == "#{pane_pid}" else f"{pane_id} {pane_pid}"
        return None
    return fake_run


def test_a_prefix_of_a_real_session_is_not_captured(TT, monkeypatch):
    """The unfixed code passed the raw name straight to `-t`, so `pane_pids`
    and `panes` for a nonexistent `zzprobe` returned `zzprobe-second`'s own
    pane. The fix must resolve the exact session id first and find no match."""
    monkeypatch.setattr(TT, "_run", _fake_tmux({"zzprobe-second": "$28"}))

    assert TT.pane_pids("zzprobe") == []
    assert TT.panes("zzprobe") == []
    # The real session still resolves — this isn't a name that broke matching
    # generally, only the ambiguous, nonexistent one.
    assert TT.pane_pids("zzprobe-second") == [999]


def test_a_prefix_of_a_real_session_raises_rather_than_leaking_its_transcript(
        TT, monkeypatch, runs, projects):
    """THE LIVE BUG, end to end. Unpatched, a caller asking about a session
    that does not exist got back whichever real session's name it happened to
    prefix — here, `zzprobe-second`'s transcript, from a claude pane that is
    not the one anybody asked about. The fix must raise instead."""
    day = _today(TT)
    theirs = projects("-Users-operator-other", "cccccccc-3333")
    runs(day, "cccccccc-3333", ppid=999, transcript=theirs)

    monkeypatch.setattr(TT, "_run", _fake_tmux({"zzprobe-second": "$28"}))
    _tree(TT, monkeypatch, children={}, commands={999: "claude"})

    with pytest.raises(TT.JoinError, match="no tmux session"):
        TT.transcript_for_tmux_session("zzprobe")


def test_a_window_name_that_matches_no_session_is_not_captured(TT, monkeypatch):
    """NOT A PROVEN BYPASS — checked live and does not reproduce. A window can
    carry the same name as an unrelated session (`ask--audio` has a window
    literally named `ask` on this machine today), which looks like it could
    be a second, independent way for a bare name to resolve into the wrong
    session. It isn't: a colon-less `-t` on `list-panes` resolves purely by
    target-SESSION rules (id, exact name, then prefix), with no fallback to a
    bare cross-session window-name search — verified live, twice,
    independently (see `_resolve_session_id`'s docstring for the repro).

    So this test pins narrower ground than its name once implied: resolution
    here is keyed on `#{session_id} #{session_name}` from `list-sessions`
    alone, which has no window field to match at all, so a name that exists
    only as a window can never resolve as a session id — and because the fix
    targets the resolved id, `list-panes` is never even called with the bare,
    ambiguous name. It is a cheap guard on the id-targeting mechanism, not
    proof a window-name bypass was ever reachable.
    """
    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            return "$19 ask--audio"  # the window "ask" lives inside this
        if argv[:2] == ["tmux", "list-panes"] and "-t" in argv:
            pytest.fail(f"list-panes called with an unresolved target: {argv}")
        return None

    monkeypatch.setattr(TT, "_run", fake_run)

    assert TT.pane_pids("ask") == []
    assert TT.panes("ask") == []


def test_pane_pids_sees_every_window_not_only_the_active_one(TT, monkeypatch):
    """ADR-0069 Slice 5's finding: `list-panes -t <session>` with no `-s`
    answers for the session's single CURRENT window. A rollover leaves a
    session with two windows (predecessor's, successor's), and the one not
    currently active must not vanish from this reading."""
    def fake_run(argv, stdin=None):
        if argv[:2] == ["tmux", "list-sessions"]:
            return "$5 twowin"
        if argv[:2] == ["tmux", "list-panes"]:
            assert "-s" in argv, f"list-panes must pass -s to see every window: {argv}"
            fmt = argv[argv.index("-F") + 1]
            if fmt == "#{pane_pid}":
                return "111\n222"
            return "%1 111\n%2 222"
        return None

    monkeypatch.setattr(TT, "_run", fake_run)
    assert TT.pane_pids("twowin") == [111, 222]
    assert TT.panes("twowin") == [("%1", 111), ("%2", 222)]


# MARK: - Reading the process tree


def test_children_come_from_ps_rather_than_pgrep(TT, monkeypatch):
    """NOT `pgrep -P`, and this test is the lock on that.

    macOS pgrep excludes its own ancestors by default, so walking a pane's tree
    from INSIDE the session being walked skipped the very claude that was
    asking: the session read `alive=False` while plainly running. `pgrep -a`
    opts back in on macOS but means "show the full command line" on Linux,
    where the digit filter would then drop every child.
    """
    asked = []

    def fake_run(argv, stdin=None):
        asked.append(argv)
        return "  100     1\n  200   100\n  300   100\n  400   200\n"

    monkeypatch.setattr(TT, "_run", fake_run)

    assert TT._children(100) == [200, 300]
    assert TT._children(200) == [400]
    assert TT._children(999) == []
    assert all(argv[0] == "ps" for argv in asked)
    assert not any("pgrep" in argv[0] for argv in asked)


def test_children_survive_a_malformed_ps_line(TT, monkeypatch):
    """One unreadable row must not cost the caller the rest of the table — the
    same per-record degradation every other reader here applies."""
    monkeypatch.setattr(
        TT, "_run",
        lambda argv, stdin=None: "?? ??\n200 100\nheader junk\n300 100\n")
    assert TT._children(100) == [200, 300]


# MARK: - Finding claude in the pane's tree


def test_finds_the_claude_process_under_the_pane_shell(TT, monkeypatch):
    _tree(TT, monkeypatch,
          children={100: [200], 200: [300]},
          commands={200: "/opt/homebrew/bin/claude --resume x", 300: "rg foo"})
    assert TT.claude_pids_under(100) == [200]


def test_prefers_the_shallowest_claude(TT, monkeypatch):
    """The nearest claude to the pane is the session the human is looking at;
    anything deeper is something that session spawned."""
    _tree(TT, monkeypatch,
          children={100: [200], 200: [300]},
          commands={200: "claude", 300: "claude"})
    assert TT.claude_pids_under(100) == [200]


def test_a_process_merely_named_like_claude_does_not_match(TT, monkeypatch):
    _tree(TT, monkeypatch,
          children={100: [200]},
          commands={200: "/usr/local/bin/claude-monitor --watch"})
    assert TT.claude_pids_under(100) == []


def test_a_path_prefixed_claude_still_matches(TT, monkeypatch):
    _tree(TT, monkeypatch,
          children={100: [200]},
          commands={200: "/srv/convivy/.local/share/mise/installs/node/bin/claude --foo"})
    assert TT.claude_pids_under(100) == [200]


def test_a_cyclic_or_repeating_tree_terminates(TT, monkeypatch):
    _tree(TT, monkeypatch, children={100: [100]}, commands={})
    assert TT.claude_pids_under(100) == []


# MARK: - The decoys that defeat a recency heuristic


def test_a_detached_fork_in_the_same_cwd_is_not_chosen(TT, monkeypatch, runs, projects):
    """The live failure this module exists for. A fork shares the cwd and its
    transcript is newer, and it still must lose, because it is not a descendant
    of the pane.

    Both transcripts land in ONE project directory, which is what "same cwd"
    means on disk, and the fork's file is written second so it is also the newer
    one. Neither fact is consulted: the join never compares timestamps and never
    lists the directory. The decoy is here so that a future change to a recency
    heuristic fails this test rather than passing it.
    """
    day = _today(TT)
    real = projects("-Users-operator-deck", "aaaaaaaa-1111")
    fork = projects("-Users-operator-deck", "bbbbbbbb-2222")
    assert fork.stat().st_mtime >= real.stat().st_mtime
    runs(day, "aaaaaaaa-1111", ppid=200, transcript=real)
    runs(day, "bbbbbbbb-2222", ppid=999, transcript=fork)  # elsewhere in the tree

    _tree(TT, monkeypatch, children={100: [200]}, commands={200: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [100])

    assert TT.transcript_for_tmux_session("deck") == real


def test_a_sibling_session_in_another_pane_does_not_leak_in(TT, monkeypatch, runs, projects):
    day = _today(TT)
    mine = projects("-Users-operator-hollis", "aaaaaaaa-1111")
    theirs = projects("-Users-operator-hollis", "cccccccc-3333")
    runs(day, "aaaaaaaa-1111", ppid=201, transcript=mine)
    runs(day, "cccccccc-3333", ppid=301, transcript=theirs)

    _tree(TT, monkeypatch, children={101: [201], 999: [301]},
          commands={201: "claude", 301: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [101])

    assert TT.transcript_for_tmux_session("hollis") == mine


# MARK: - Containment of the recorded path


def test_a_recorded_path_outside_the_projects_tree_is_refused(TT, projects, tmp_path):
    """`meta.json` is written by a hook, which made it tempting to trust. But a
    claude session can write under ~/Convivy/agent-runs and knows its own pid,
    so a prompt-injected one could point transcript_path at any file the operator can
    read. Containment, not trust."""
    secret = tmp_path / "home" / ".config" / "creds.jsonl"
    secret.parent.mkdir(parents=True)
    secret.write_text('{"type":"user"}')
    assert TT._contained_transcript(str(secret)) is None


def test_traversal_out_of_the_projects_tree_is_refused(TT, projects, tmp_path):
    inside = projects("-Users-operator-deck", "aaaaaaaa-1111")
    escape = str(inside.parent / ".." / ".." / ".." / "creds.jsonl")
    assert TT._contained_transcript(escape) is None


def test_a_symlink_out_of_the_projects_tree_is_refused(TT, projects, tmp_path):
    """resolve() before the containment check, so a symlink is caught by the
    same test as a `..` rather than needing its own."""
    secret = tmp_path / "home" / "secret.jsonl"
    secret.write_text("")
    link = projects.root / "-Users-operator-deck"
    link.mkdir()
    (link / "evil.jsonl").symlink_to(secret)
    assert TT._contained_transcript(str(link / "evil.jsonl")) is None


def test_a_non_jsonl_file_inside_the_tree_is_refused(TT, projects):
    other = projects.root / "notes.txt"
    other.write_text("")
    assert TT._contained_transcript(str(other)) is None


def test_a_real_transcript_is_accepted(TT, projects):
    path = projects("-Users-operator-deck", "aaaaaaaa-1111")
    assert TT._contained_transcript(str(path)) == path


def test_a_forged_path_falls_back_to_the_id_glob(TT, monkeypatch, runs, projects):
    """A record that fails containment must not take the session down with it —
    the session_id is still trustworthy, because it is matched inside the tree."""
    day = _today(TT)
    landed = projects("-Users-operator-deck", "aaaaaaaa-1111")
    runs(day, "aaaaaaaa-1111", ppid=200, transcript=Path("/etc/passwd"))

    _tree(TT, monkeypatch, children={100: [200]}, commands={200: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [100])

    assert TT.transcript_for_tmux_session("deck") == landed


# MARK: - Run-dir lookup


def test_matches_the_meta_record_by_ppid(TT, runs):
    day = _today(TT)
    runs(day, "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"
    assert TT.meta_for_pid(9999) is None


def test_a_malformed_meta_does_not_hide_the_others(TT, runs, tmp_path):
    """Per the run-dir contract's degradation rule: one bad record must not cost
    every other session."""
    day = _today(TT)
    bad = TT.RUNS_ROOT / day / "co-broken"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("{not json")
    runs(day, "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_a_session_started_yesterday_is_still_found(TT, runs, monkeypatch):
    """The day-key is local and a long session outlives it. The contract makes
    the same point about Stop finding its record by search, not by prediction."""
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    runs(yesterday, "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def _started(TT, monkeypatch, when: str):
    """Pin what `ps -o lstart=` reports for the pid under test."""
    monkeypatch.setattr(TT, "_run", lambda argv, stdin=None: when if "lstart=" in argv else None)


def test_a_session_older_than_the_fixed_window_is_still_found(TT, runs, monkeypatch):
    """THE BUG THIS CLOSES. A run dir is filed under the day its session STARTED,
    and a co session runs until someone stops it — so a fixed "last N days"
    window loses a session precisely BECAUSE it has been up a long time.

    Observed 2026-07-23: sous-chef and vivy, alive since 2026-07-20, both read
    `state: unknown` in Deck's pane with their records one day outside the
    three-day window.
    """
    from datetime import date, timedelta
    long_ago = date.today() - timedelta(days=TT.SEARCH_DAYS + 4)
    runs(long_ago.isoformat(), "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    _started(TT, monkeypatch, long_ago.strftime("%a %b %d 22:30:05 %Y"))

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_a_session_started_just_before_midnight_is_found_the_next_day(TT, runs, monkeypatch):
    """The process clock and the day-key are both local, but a session starting
    within a second of midnight can land either side of it — so the search opens
    a day earlier than the start date."""
    from datetime import date, timedelta
    started = date.today() - timedelta(days=5)
    filed = started - timedelta(days=1)
    runs(filed.isoformat(), "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    _started(TT, monkeypatch, started.strftime("%a %b %d 00:00:00 %Y"))

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_an_unreadable_start_date_falls_back_to_the_fixed_window(TT, runs, monkeypatch):
    """A pid that has already exited, or a ps that answers in a shape this does
    not parse, degrades to the old bounded search rather than to no search."""
    day = _today(TT)
    runs(day, "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))
    _started(TT, monkeypatch, "not a date at all")

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_the_search_does_not_read_days_before_the_process_existed(TT, runs, monkeypatch):
    """The point of measuring against the process: the record CANNOT predate it,
    so those buckets are not read. A same-named record filed earlier belongs to a
    different, older session and must not be returned."""
    from datetime import date, timedelta
    started = date.today() - timedelta(days=2)
    stale = date.today() - timedelta(days=30)
    runs(stale.isoformat(), "bbbbbbbb-2222", ppid=4242, transcript=Path("/tmp/old.jsonl"))
    _started(TT, monkeypatch, started.strftime("%a %b %d 09:00:00 %Y"))

    assert TT.meta_for_pid(4242) is None


# MARK: - private-crew run dirs (agent-platform#421 / issue_01M0GVMBXCX10F0R3ZAGHGEKFZ)
#
# `session-event-log.py` routes a private crew's SessionStart/Stop identity
# record to `<crew home>/agent-runs/`, not the shared `RUNS_ROOT`. `meta_for_pid`
# has no `cwd` to resolve a single right answer from — discovering `cwd` is
# what the search is FOR — so it has to search every registered private crew's
# own root too, not just the shared one.


def _private_home(tmp_path, TT, monkeypatch, slug="cfo"):
    """A fake private crew home, wired so TT.PC.private_homes() returns it."""
    home = tmp_path / slug
    home.mkdir()
    monkeypatch.setattr(TT.PC, "private_homes", lambda: [str(home)])
    return home


def test_a_private_crews_run_dir_is_found_without_knowing_cwd_first(TT, runs, tmp_path, monkeypatch):
    """The bug itself: a private crew's identity record lives outside RUNS_ROOT,
    and meta_for_pid must find it on the FIRST call — before anything has told
    it which crew this pid belongs to."""
    home = _private_home(tmp_path, TT, monkeypatch)
    day = _today(TT)
    d = home / "agent-runs" / day / "co-aaaaaaaa-1111"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"kind": "co-session", "session_id": "aaaaaaaa-1111", "ppid": 4242}))

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_a_private_crews_run_dir_is_found_by_the_start_date_search_too(TT, tmp_path, monkeypatch):
    """The same coverage, through `_run_dirs_since` rather than the fixed
    window — a long-running private-crew session must not regress the day this
    was fixed for the shared root (2026-07-23)."""
    home = _private_home(tmp_path, TT, monkeypatch)
    from datetime import date, timedelta
    long_ago = date.today() - timedelta(days=TT.SEARCH_DAYS + 4)
    d = home / "agent-runs" / long_ago.isoformat() / "co-aaaaaaaa-1111"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"kind": "co-session", "session_id": "aaaaaaaa-1111", "ppid": 4242}))
    _started(TT, monkeypatch, long_ago.strftime("%a %b %d 22:30:05 %Y"))

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_the_shared_root_is_still_searched_alongside_a_private_crew(TT, runs, tmp_path, monkeypatch):
    """Registering a private crew must not stop ordinary (non-private) sessions
    from resolving — the search adds a root, it does not replace one."""
    _private_home(tmp_path, TT, monkeypatch)
    day = _today(TT)
    runs(day, "bbbbbbbb-2222", ppid=5000, transcript=Path("/tmp/b.jsonl"))

    assert TT.meta_for_pid(5000)["session_id"] == "bbbbbbbb-2222"


def test_an_unreadable_registry_still_finds_the_shared_root(TT, runs, monkeypatch):
    """`private_homes()` raising (no registry on this box, e.g. CI) must degrade
    to the shared root alone — the same answer this file always gave before
    private routing existed — rather than taking every session down with it."""
    day = _today(TT)
    runs(day, "aaaaaaaa-1111", ppid=4242, transcript=Path("/tmp/a.jsonl"))

    def _raise():
        raise FileNotFoundError("no registry on this box")
    monkeypatch.setattr(TT.PC, "private_homes", _raise)

    assert TT.meta_for_pid(4242)["session_id"] == "aaaaaaaa-1111"


def test_two_distinct_records_claiming_one_pid_refuse_rather_than_guess(TT, runs, tmp_path, monkeypatch):
    """THE COLLISION (security-reviewer, PR #421, run-71cd97f7). Searching a
    private crew's root alongside the shared one puts a dead session's stale
    record into the search space for an unrelated, currently-live pid — OS pid
    reuse could land a live claude process on the same numeric pid a stale
    private-crew record still names. Returning either candidate risks handing
    an ordinary caller a black-box crew's own record, `transcript_path`
    included. Two DIFFERENT session_ids both claiming one pid must refuse the
    whole lookup rather than pick one."""
    home = _private_home(tmp_path, TT, monkeypatch)
    day = _today(TT)
    # The shared root's record: what a currently-live, ordinary pid actually is.
    runs(day, "bbbbbbbb-2222", ppid=4242, transcript=Path("/tmp/b.jsonl"))
    # A stale private-crew record that happens to name the SAME pid.
    d = home / "agent-runs" / day / "co-aaaaaaaa-1111"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"kind": "co-session", "session_id": "aaaaaaaa-1111", "ppid": 4242}))

    assert TT.meta_for_pid(4242) is None, \
        "an ambiguous cross-root pid match must refuse, not guess"


def test_the_same_session_found_twice_is_not_treated_as_a_collision(TT, runs, tmp_path, monkeypatch):
    """The refusal is keyed on two DIFFERENT session_ids, not on finding more
    than one file — a legitimate record must still resolve even if it were
    (implausibly) duplicated under two day buckets, so the guard cannot be
    satisfied by merely finding a match twice."""
    _private_home(tmp_path, TT, monkeypatch)
    day = _today(TT)
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    runs(day, "bbbbbbbb-2222", ppid=4242, transcript=Path("/tmp/b.jsonl"))
    runs(yesterday, "bbbbbbbb-2222", ppid=4242, transcript=Path("/tmp/b.jsonl"))

    assert TT.meta_for_pid(4242)["session_id"] == "bbbbbbbb-2222"


# MARK: - Failure reporting


def test_no_such_tmux_session_says_so(TT, monkeypatch):
    monkeypatch.setattr(TT, "pane_pids", lambda name: [])
    with pytest.raises(TT.JoinError, match="no tmux session"):
        TT.transcript_for_tmux_session("nope")


def test_a_pane_with_no_claude_says_so(TT, monkeypatch, runs):
    _tree(TT, monkeypatch, children={100: [200]}, commands={200: "vim"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [100])
    with pytest.raises(TT.JoinError, match="no claude session"):
        TT.transcript_for_tmux_session("deck")


def test_falls_back_to_the_id_glob_when_no_path_was_recorded(TT, monkeypatch, runs, projects):
    """A record written before `transcript_path` existed still resolves by id."""
    day = _today(TT)
    landed = projects("-Users-operator-x", "aaaaaaaa-1111")
    runs(day, "aaaaaaaa-1111", ppid=200, transcript=None)

    _tree(TT, monkeypatch, children={100: [200]}, commands={200: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [100])

    assert TT.transcript_for_tmux_session("deck") == landed


def test_falls_back_to_the_id_glob_when_the_recorded_file_has_moved(TT, monkeypatch, runs, projects):
    """The other fallback branch: a path that passes containment but no longer
    exists on disk. Distinct from the absent-field case above, and it is the one
    that actually happens — a transcript relocates, the record does not."""
    day = _today(TT)
    landed = projects("-Users-operator-x", "aaaaaaaa-1111")
    moved = projects.root / "-Users-operator-x" / "gone.jsonl"  # contained, never created
    assert not moved.exists()
    runs(day, "aaaaaaaa-1111", ppid=200, transcript=moved)

    _tree(TT, monkeypatch, children={100: [200]}, commands={200: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [100])

    assert TT.transcript_for_tmux_session("deck") == landed


def test_the_id_glob_refuses_a_symlink_out_of_the_tree(TT, projects, tmp_path):
    """The fallback is a second path to the same read, so it needs the same
    gate. A glob match is a name, and a name can be a symlink."""
    secret = tmp_path / "home" / "secret.jsonl"
    secret.write_text("")
    d = projects.root / "-Users-operator-deck"
    d.mkdir()
    (d / "aaaaaaaa-1111.jsonl").symlink_to(secret)
    assert TT._transcript_by_id("aaaaaaaa-1111") is None


def test_the_id_glob_returns_a_real_transcript(TT, projects):
    path = projects("-Users-operator-deck", "aaaaaaaa-1111")
    assert TT._transcript_by_id("aaaaaaaa-1111") == path


def test_a_session_id_glob_cannot_be_used_as_a_pattern(TT):
    """`*` as an id previously matched */*.jsonl and returned someone else's
    transcript. Same guard, same reason, at this layer too."""
    assert TT._transcript_by_id("*") is None
    assert TT._transcript_by_id("../../etc/passwd") is None
    assert TT._transcript_by_id(None) is None


def test_transcript_for_session_id_delegates_to_the_id_glob(TT, projects):
    """A thin public wrapper around `_transcript_by_id` (Slice 7, epic
    issue_01M1S2X8ENKYEGSDE3H0REFD3G) — for `co_rollover.
    forward_late_messages`, which holds a predecessor's session id from the
    rollover side channel rather than a live tmux pane to walk."""
    path = projects("-Users-operator-deck", "aaaaaaaa-1111")
    assert TT.transcript_for_session_id("aaaaaaaa-1111") == path


def test_transcript_for_session_id_is_none_for_an_unresolvable_id(TT, projects):
    assert TT.transcript_for_session_id("no-such-session") is None


# MARK: - A pane whose own process is claude


def test_a_pane_that_execd_claude_is_found(TT, monkeypatch):
    """`exec claude` replaces the shell, so claude IS the pane process rather
    than a child of it. Walking only descendants found nothing and the session
    read as having no agent — observed live on a scratch session that was
    plainly running."""
    _tree(TT, monkeypatch, children={}, commands={500: "claude"})
    assert TT.claude_pids_for_pane(500) == [500]


def test_the_shell_shaped_pane_still_descends(TT, monkeypatch):
    """The fleet's own shape: tmux starts a shell, claude runs inside it."""
    _tree(TT, monkeypatch,
          children={100: [200]}, commands={100: "-zsh", 200: "claude"})
    assert TT.claude_pids_for_pane(100) == [200]


def test_the_pane_process_wins_over_a_descendant(TT, monkeypatch):
    """If the pane IS claude, that is the session — anything beneath it is
    something that session spawned."""
    _tree(TT, monkeypatch,
          children={500: [600]}, commands={500: "claude", 600: "claude"})
    assert TT.claude_pids_for_pane(500) == [500]


def test_a_pane_running_something_else_entirely(TT, monkeypatch):
    _tree(TT, monkeypatch, children={100: [200]}, commands={100: "vim", 200: "rg"})
    assert TT.claude_pids_for_pane(100) == []


def test_claude_pane_finds_an_execd_session(TT, monkeypatch):
    monkeypatch.setattr(TT, "panes", lambda name, table=None: [("%171", 500)])
    _tree(TT, monkeypatch, children={}, commands={500: "claude"})
    assert TT.claude_pane("scratch") == ("%171", 500)


def test_the_transcript_join_works_for_an_execd_pane(TT, monkeypatch, runs, projects):
    """`transcript_for_tmux_session` takes the same fix as `claude_pane`, and
    deserves its own witness at the shape that broke: a pane whose own process
    is claude, with no descendant to find."""
    day = _today(TT)
    landed = projects("-Users-operator-scratch", "aaaaaaaa-1111")
    runs(day, "aaaaaaaa-1111", ppid=500, transcript=landed)

    # No children at all — claude IS the pane process.
    _tree(TT, monkeypatch, children={}, commands={500: "claude"})
    monkeypatch.setattr(TT, "pane_pids", lambda name: [500])

    assert TT.transcript_for_tmux_session("scratch") == landed


# MARK: - Reading the process start date


@pytest.mark.parametrize("loc", ["fr_FR.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8", "ja_JP.UTF-8"])
def test_the_start_date_parses_under_a_non_english_locale(TT, loc):
    """`ps` emits English month names whatever LC_TIME says, but `strptime`
    interprets `%a`/`%b` through the PROCESS's locale — so the first version of
    this raised under every locale below, fell back to the old three-day window,
    and quietly restored the bug for anyone not running English. Caught in
    review; the month table removes the dependency rather than juggling it."""
    import locale
    try:
        locale.setlocale(locale.LC_TIME, loc)
    except locale.Error:
        pytest.skip(f"{loc} not installed")
    try:
        assert TT._parse_ps_lstart("Mon Jul 20 22:30:05 2026") == date(2026, 7, 20)
    finally:
        locale.setlocale(locale.LC_TIME, "C")


def test_a_single_digit_day_is_space_padded_by_ps(TT):
    assert TT._parse_ps_lstart("Tue Jul  1 09:05:00 2026") == date(2026, 7, 1)


@pytest.mark.parametrize("junk", [
    "", "not a date at all", "Mon Jul 20 22:30:05",
    "Mon Xxx 20 22:30:05 2026", "Mon Jul 99 22:30:05 2026",
])
def test_unparseable_ps_output_is_none_rather_than_an_exception(TT, junk):
    """The caller's fallback to the bounded window depends on None, and this has
    to survive whatever `ps` on some other platform decides to emit."""
    assert TT._parse_ps_lstart(junk) is None


# MARK: - The full start timestamp, and the stale-pid guard built on it


def test_parse_ps_lstart_full_reads_the_clock_too(TT):
    got = TT._parse_ps_lstart_full("Mon Jul 20 22:30:05 2026")
    assert (got.year, got.month, got.day, got.hour, got.minute, got.second) == (
        2026, 7, 20, 22, 30, 5)
    assert got.tzinfo is not None


@pytest.mark.parametrize("junk", [
    "", "not a date at all", "Mon Jul 20 22:30:05",
    "Mon Xxx 20 22:30:05 2026", "Mon Jul 99 22:30:05 2026",
    "Mon Jul 20 2x:30:05 2026",
])
def test_parse_ps_lstart_full_unparseable_is_none(TT, junk):
    assert TT._parse_ps_lstart_full(junk) is None


def test_process_start_timestamp_is_none_without_ps_output(TT, monkeypatch):
    monkeypatch.setattr(TT, "_run", lambda argv, stdin=None: None)
    assert TT._process_start_timestamp(999999) is None


def test_a_record_with_no_started_at_is_not_treated_as_stale(TT):
    """A record from before this field existed must not be skipped outright —
    that would silently lose a legitimate, still-running session's own
    record. It still has to clear the distinct-session_id collision guard
    this function feeds, which is what stops a genuinely stale record from
    winning."""
    now = datetime.now().astimezone()
    assert TT._record_predates_process({"session_id": "x"}, now) is False


def test_a_record_with_an_unparseable_started_at_is_not_treated_as_stale(TT):
    now = datetime.now().astimezone()
    assert TT._record_predates_process({"started_at": "not a timestamp"}, now) is False


def test_a_record_at_the_process_start_is_not_stale(TT):
    now = datetime.now().astimezone()
    assert TT._record_predates_process({"started_at": now.isoformat()}, now) is False


def test_the_one_second_slack_absorbs_ps_rounding(TT):
    now = datetime.now().astimezone()
    just_before = now - timedelta(milliseconds=500)
    assert TT._record_predates_process({"started_at": just_before.isoformat()}, now) is False


def test_beyond_the_slack_is_stale(TT):
    now = datetime.now().astimezone()
    long_before = now - timedelta(seconds=5)
    assert TT._record_predates_process({"started_at": long_before.isoformat()}, now) is True


@pytest.fixture
def pinned_local_tz():
    """Pin the process's local timezone to MST, UTC-7, no daylight saving —
    the machine the 2026-09-30 collision was found on — so a test can tell a
    comparison that still reasons about real instants from one that has had
    timezone information dropped. `started_at` is always written in UTC
    (`tools/session-event-log.py`, `now.isoformat()` on an aware UTC
    `datetime`); `ps -o lstart=` always reports local wall clock. Restored
    after via `time.tzset()`, never left pinned for a later test."""
    import os
    import time
    old = os.environ.get("TZ")
    os.environ["TZ"] = "MST7"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_record_predates_process_is_not_fooled_by_a_later_utc_clock_reading(
        TT, pinned_local_tz):
    """THE CLASS A TIMEZONE-DROPPING REGRESSION WOULD REINTRODUCE. The process
    started 2026-09-30 14:28:31 local (MST, UTC-7). A record left behind by an
    earlier process holding this pid carries `started_at`
    "2026-09-30T15:00:00+00:00" — its UTC clock digits, 15:00:00, read LATER
    than the process's own local clock digits, 14:28:31, so a comparison that
    strips timezone information from both sides before comparing (e.g.
    `recorded.replace(tzinfo=None) < process_started_at.replace(tzinfo=None)`)
    would call it the newer of the two. Its real instant, 15:00:00 UTC, is
    actually more than six hours BEFORE the process's own real instant,
    21:28:31 UTC, and must still read as stale."""
    process_started_at = datetime(2026, 9, 30, 14, 28, 31).astimezone()
    assert TT._record_predates_process(
        {"started_at": "2026-09-30T15:00:00+00:00"}, process_started_at) is True


def test_a_stale_record_from_an_earlier_process_holding_the_same_pid_is_skipped(
        TT, runs, monkeypatch):
    """THE LIVE CASE (2026-09-30, issue_01M3TBR04B10F04Q9HGJWKS8HG). selector's
    own `co` session, pid 49951, started today; its own record's `started_at`
    landed a few seconds later. A second record for the same pid, filed the
    day before by an EARLIER process that had held it, carried a different
    session_id and an older `started_at`. Unpatched, the two different
    session_ids read as the private-crew collision and `meta_for_pid` refused
    both; the live session's own, newer record must win instead."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    process_start = datetime(today.year, today.month, today.day, 21, 28, 31)
    own_started_at = process_start + timedelta(seconds=6)
    stale_started_at = datetime(yesterday.year, yesterday.month, yesterday.day, 14, 43, 32)

    runs(today.isoformat(), "3c1840e9-live", ppid=49951, transcript=Path("/tmp/live.jsonl"),
         started_at=own_started_at.astimezone().isoformat())
    runs(yesterday.isoformat(), "4585fc67-dead", ppid=49951, transcript=Path("/tmp/dead.jsonl"),
         started_at=stale_started_at.astimezone().isoformat())
    _started(TT, monkeypatch, process_start.strftime("%a %b %d %H:%M:%S %Y"))

    assert TT.meta_for_pid(49951)["session_id"] == "3c1840e9-live"


def test_a_stale_record_from_earlier_the_same_day_is_skipped(TT, runs, monkeypatch):
    """The same shape recurred same-day: pid 11592 held by an earlier process
    at 21:18Z, then by the live one at 22:25Z the same day — proving the fix
    is not merely a same-day window, since both records land in one bucket and
    only their own `started_at` tells them apart."""
    today = date.today()
    process_start = datetime(today.year, today.month, today.day, 22, 25, 0)
    stale_started_at = datetime(today.year, today.month, today.day, 21, 18, 0)
    own_started_at = process_start + timedelta(seconds=5)

    runs(today.isoformat(), "live-session", ppid=11592, transcript=Path("/tmp/live.jsonl"),
         started_at=own_started_at.astimezone().isoformat())
    runs(today.isoformat(), "dead-session", ppid=11592, transcript=Path("/tmp/dead.jsonl"),
         started_at=stale_started_at.astimezone().isoformat())
    _started(TT, monkeypatch, process_start.strftime("%a %b %d %H:%M:%S %Y"))

    assert TT.meta_for_pid(11592)["session_id"] == "live-session"


def test_two_records_both_after_the_start_still_refuse(TT, runs, monkeypatch):
    """The staleness filter narrows the search; it does not remove the guard
    it feeds. Two records that both postdate the process, claiming different
    session_ids for the one pid, is still the broken-input shape that must
    refuse rather than guess — the private-crew protection is unchanged."""
    today = date.today()
    process_start = datetime(today.year, today.month, today.day, 10, 0, 0)
    runs(today.isoformat(), "aaaaaaaa-1111", ppid=7777, transcript=Path("/tmp/a.jsonl"),
         started_at=(process_start + timedelta(seconds=2)).astimezone().isoformat())
    runs(today.isoformat(), "bbbbbbbb-2222", ppid=7777, transcript=Path("/tmp/b.jsonl"),
         started_at=(process_start + timedelta(seconds=3)).astimezone().isoformat())
    _started(TT, monkeypatch, process_start.strftime("%a %b %d %H:%M:%S %Y"))

    assert TT.meta_for_pid(7777) is None


def test_a_stale_record_in_production_utc_format_is_skipped_under_a_non_utc_local_zone(
        TT, runs, monkeypatch, pinned_local_tz):
    """THE REAL CASE (2026-09-30, issue_01M3TBR04B10F04Q9HGJWKS8HG), reproduced
    with each side in its OWN real shape instead of both built from the same
    local offset: `started_at` as production actually writes it, UTC with a
    `+00:00` offset, while the simulated `ps -o lstart=` output is local wall
    clock — MST, UTC-7, this machine's actual zone. Every other test in this
    file builds both sides from the same local offset, which a regression
    that drops timezone information from the comparison would still pass."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    process_start_local = datetime(today.year, today.month, today.day, 14, 28, 31)
    own_started_at = (process_start_local + timedelta(seconds=6)).astimezone(timezone.utc)
    stale_started_at = datetime(
        yesterday.year, yesterday.month, yesterday.day, 14, 43, 32).astimezone(timezone.utc)

    runs(today.isoformat(), "3c1840e9-live", ppid=49951, transcript=Path("/tmp/live.jsonl"),
         started_at=own_started_at.isoformat())
    runs(yesterday.isoformat(), "4585fc67-dead", ppid=49951, transcript=Path("/tmp/dead.jsonl"),
         started_at=stale_started_at.isoformat())
    _started(TT, monkeypatch, process_start_local.strftime("%a %b %d %H:%M:%S %Y"))

    assert TT.meta_for_pid(49951)["session_id"] == "3c1840e9-live"


def test_a_stale_record_whose_utc_clock_reads_later_than_local_process_start_is_still_skipped(
        TT, runs, monkeypatch, pinned_local_tz):
    """THE CASE A NAIVE (TIMEZONE-STRIPPING) COMPARISON GETS BACKWARDS. The
    process started 2026-09-30 14:28:31 local (MST, UTC-7). The record left
    behind by whatever process held this pid before it carries `started_at`
    "...T15:00:00+00:00" — its UTC clock digits, 15:00:00, read LATER than the
    process's own local clock digits, 14:28:31, so a comparison that strips
    timezone information from both sides before comparing would call it the
    newer of the two and let it reach the collision guard, exactly the
    "live session's own record gets refused" failure this PR fixes,
    reintroduced by timezone representation instead of by a missing
    comparison. Its real instant, 15:00:00 UTC, is actually more than six
    hours BEFORE the process's own real instant, 21:28:31 UTC, and must still
    be excluded."""
    today = date.today()
    process_start_local = datetime(today.year, today.month, today.day, 14, 28, 31)
    own_started_at = (process_start_local + timedelta(seconds=6)).astimezone(timezone.utc)
    stale_started_at = datetime(today.year, today.month, today.day, 15, 0, 0, tzinfo=timezone.utc)

    runs(today.isoformat(), "live-session", ppid=55555, transcript=Path("/tmp/live.jsonl"),
         started_at=own_started_at.isoformat())
    runs(today.isoformat(), "dead-session", ppid=55555, transcript=Path("/tmp/dead.jsonl"),
         started_at=stale_started_at.isoformat())
    _started(TT, monkeypatch, process_start_local.strftime("%a %b %d %H:%M:%S %Y"))

    assert TT.meta_for_pid(55555)["session_id"] == "live-session"
