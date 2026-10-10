"""The registry's concurrency limits come from the agent policy file.

The policy is `projects/platform/policy/agent-policy.yaml` at origin/main of the
knowledge repository, parsed by `agent_policy`. Every test builds its own
knowledge repository under tmp_path, with origin/main made by `update-ref`, and
points `registry.KNOWLEDGE_REPO` at it. Nothing reads HOME, ~/Convivy, the live
run registry or an installed venv. `runs_root` supplies the isolated registry and
clears the once-per-process limits cache.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import agent_policy as ap
import registry

SHIPPED = (Path(__file__).parent / "fixtures" / "agent-policy-6fe9537.yaml").read_text(encoding="utf-8")
SOURCE_LINE = "registry: concurrency limits from"


def policy_text(**limits: int | str) -> str:
    """The shipped policy file with the named limits replaced."""
    text = SHIPPED
    for key, value in limits.items():
        text, n = re.subn(rf"(?m)^{key}: .*$", f"{key}: {value}", text)
        assert n == 1
    return text


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def knowledge(tmp_path, monkeypatch, runs_root):
    """Build a knowledge repo whose origin/main holds `committed` (None: no policy
    file there), point the registry at it, and return its path."""

    def make(committed: str | None, working: str | None = None) -> Path:
        repo = tmp_path / "knowledge"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        target = repo / ap.POLICY_PATH
        target.parent.mkdir(parents=True)
        if committed is None:
            (repo / "README.md").write_text("no policy\n")
        else:
            target.write_text(committed, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "knowledge")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        if working is not None:
            target.write_text(working, encoding="utf-8")
        monkeypatch.setattr(registry, "KNOWLEDGE_REPO", repo)
        monkeypatch.setattr(registry, "_LIMITS", None)
        return repo

    return make


def _entry(run_id, *, parent=None, agent="coder"):
    return {
        "run_id": run_id,
        "agent": agent,
        "task": "t",
        "started_at": "2026-05-31T00:00:00Z",
        "repo": "constance",
        "worktree": "/tmp/wt",
        "pid": __import__("os").getpid(),
        "parent_run": parent,
    }


def test_the_constants_equal_the_parsers_fallback():
    assert (registry.MAX_CONCURRENT_DEFAULT, registry.MAX_WORKERS_PER_TEAM_DEFAULT,
            registry.MAX_LIVE_SUPERVISORS_DEFAULT) == (
        ap.FALLBACK_MAX_CONCURRENT, ap.FALLBACK_MAX_WORKERS_PER_TEAM, ap.FALLBACK_MAX_LIVE_SUPERVISORS)


# ---- each limit is read from the file -----------------------------------------


def test_max_concurrent_is_read_from_the_policy(knowledge):
    knowledge(policy_text(max_concurrent=3))
    for i in range(3):
        registry.add_run(_entry(f"run-{i}"))
    ok, _current, reason = registry.can_spawn()
    assert ok is False
    assert reason == "global concurrency ceiling (3/3)"


def test_a_max_concurrent_above_the_fallback_is_honoured(knowledge):
    knowledge(policy_text(max_concurrent=20))
    for i in range(registry.MAX_CONCURRENT_DEFAULT + 1):
        registry.add_run(_entry(f"run-{i}"))
    assert registry.can_spawn()[0] is True


def test_max_workers_per_team_is_read_from_the_policy(knowledge):
    knowledge(policy_text(max_workers_per_team=2))
    registry.add_run(_entry("orch-1", agent="planning-orchestrator"))
    for i in range(2):
        registry.add_run(_entry(f"w-{i}", parent="orch-1"))
    ok, _current, reason = registry.can_spawn("orch-1", "coder")
    assert ok is False
    assert reason == "team orch-1 at worker cap (2/2)"


def test_max_live_supervisors_is_read_from_the_policy(knowledge):
    knowledge(policy_text(max_live_supervisors=2))
    for i in range(2):
        registry.add_run(_entry(f"orch-{i}", agent="planning-orchestrator"))
    ok, _current, reason = registry.can_spawn(None, "planning-orchestrator")
    assert ok is False
    assert reason.startswith("live supervisor cap (2/2)")


# ---- the fallback: 16, 4 and 4, and the stderr line says why ------------------

def bad(**broken: int | str) -> str:
    """A policy file with one broken rule and low limits everywhere else, so a
    partly-read file shows as 2 and not as the fallback's 4."""
    return policy_text(**{"max_workers_per_team": 2, "max_live_supervisors": 2, "max_concurrent": 2, **broken})


MALFORMED = [
    ("a limit of zero", bad(max_concurrent=0), "max_concurrent"),
    ("a limit above 1000", bad(max_workers_per_team=1001), "max_workers_per_team"),
    ("a negative limit", bad(max_live_supervisors=-1), "max_live_supervisors"),
    ("a limit that is not a whole number", bad(max_concurrent="2.5"), "max_concurrent"),
    ("a limit written as null", bad(max_live_supervisors="null"), "max_live_supervisors"),
    ("a limit with a leading zero", bad(max_concurrent="07"), "max_concurrent"),
    ("a limit in other digits", bad(max_concurrent="\u0663"), "max_concurrent"),
    ("a limit with a trailing comment", bad(max_concurrent="3 # low"), "max_concurrent"),
    ("a duplicated key", bad() + "max_concurrent: 1000\n", "appears more than once"),
]


@pytest.mark.parametrize("label,text,why", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_malformed_policy_uses_the_fallback_and_says_why(knowledge, capsys, label, text, why):
    knowledge(text)
    registry.add_run(_entry("orch-1", agent="planning-orchestrator"))
    for i in range(4):
        registry.add_run(_entry(f"w-{i}", parent="orch-1"))
    ok, _current, reason = registry.can_spawn("orch-1", "coder")
    assert (ok, reason) == (False, "team orch-1 at worker cap (4/4)")
    err = capsys.readouterr().err
    assert f"{SOURCE_LINE} the fallback, because" in err
    assert why in err
    assert "max_concurrent=16 max_workers_per_team=4 max_live_supervisors=4" in err
    lim = registry.limits()
    assert isinstance(lim, ap.Fallback)
    assert (lim.max_concurrent, lim.max_workers_per_team, lim.max_live_supervisors) == (16, 4, 4)


def test_a_missing_policy_file_uses_the_fallback_and_says_why(knowledge, capsys):
    knowledge(None)
    registry.can_spawn()
    assert "the fallback, because the policy file is missing at origin/main" in capsys.readouterr().err
    assert registry.limits().max_concurrent == 16


def test_a_repository_with_no_origin_main_uses_the_fallback(knowledge, capsys):
    repo = knowledge(policy_text(max_concurrent=3))
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    registry.can_spawn()
    assert "the fallback, because the policy file is missing at origin/main" in capsys.readouterr().err
    lim = registry.limits()
    assert (lim.max_concurrent, lim.max_workers_per_team, lim.max_live_supervisors) == (16, 4, 4)


def test_a_knowledge_path_that_is_not_a_repository_uses_the_fallback(tmp_path, monkeypatch, runs_root, capsys):
    monkeypatch.setattr(registry, "KNOWLEDGE_REPO", tmp_path / "does-not-exist")
    monkeypatch.setattr(registry, "_LIMITS", None)
    registry.can_spawn()
    assert "the fallback, because the policy file is missing at origin/main" in capsys.readouterr().err
    assert registry.limits().max_concurrent == 16


def test_a_working_tree_policy_with_higher_limits_is_ignored(knowledge, capsys):
    knowledge(policy_text(max_concurrent=3), working=policy_text(max_concurrent=900))
    for i in range(3):
        registry.add_run(_entry(f"run-{i}"))
    ok, _current, reason = registry.can_spawn()
    assert ok is False
    assert reason == "global concurrency ceiling (3/3)"


# ---- once per process ---------------------------------------------------------


def test_the_source_line_prints_once_for_many_checks(knowledge, capsys):
    knowledge(policy_text(max_concurrent=5))
    for i in range(3):
        registry.can_spawn()
    registry.add_run(_entry("run-0"))
    registry.reserve_slot({k: v for k, v in _entry("run-1").items() if k != "pid"})
    err = capsys.readouterr().err
    assert err.count(SOURCE_LINE) == 1
    assert f"{SOURCE_LINE} the policy file at origin/main of" in err
    assert "max_concurrent=5 max_workers_per_team=4 max_live_supervisors=4" in err


def test_the_policy_is_read_once_and_a_change_reaches_the_next_process(knowledge, monkeypatch):
    repo = knowledge(policy_text(max_concurrent=3))
    reads = []
    real = ap.policy_text_at_origin_main
    monkeypatch.setattr(ap, "policy_text_at_origin_main", lambda path: reads.append(path) or real(path))
    for _ in range(5):
        registry.can_spawn()
    assert len(reads) == 1
    # origin/main moves to a new limit: this process keeps the one it read...
    (repo / ap.POLICY_PATH).write_text(policy_text(max_concurrent=9), encoding="utf-8")
    git(repo, "commit", "-am", "raise")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert registry.limits().max_concurrent == 3
    # ...and the next process reads it.
    monkeypatch.setattr(registry, "_LIMITS", None)
    assert registry.limits().max_concurrent == 9
    assert len(reads) == 2


# ---- the policy read happens before the lock is taken (PR #694 round 1) -------


def _spy_lock_and_read(monkeypatch):
    """Record, in order, when the policy read is attempted and when the registry
    lock is actually taken (LOCK_EX, not the LOCK_UN release). Returns the list."""
    order: list[str] = []
    real_flock = registry.fcntl.flock

    def spy_flock(fd, op):
        if op == registry.fcntl.LOCK_EX:
            order.append("lock")
        return real_flock(fd, op)

    monkeypatch.setattr(registry.fcntl, "flock", spy_flock)
    return order


def test_the_policy_is_read_before_the_lock_is_taken(knowledge, monkeypatch):
    """can_spawn's git read (behind limits()) must run before the registry lock
    is taken: every `agentctl spawn` is a fresh process serializing on that lock,
    so a read done under it would stall every concurrent spawn on the machine."""
    knowledge(policy_text(max_concurrent=5))
    order = _spy_lock_and_read(monkeypatch)
    real_read = ap.policy_text_at_origin_main

    def spy_read(path):
        order.append("read")
        return real_read(path)

    monkeypatch.setattr(ap, "policy_text_at_origin_main", spy_read)
    registry.can_spawn()
    assert order == ["read", "lock"]


def test_add_run_and_reserve_slot_also_read_the_policy_before_the_lock(knowledge, monkeypatch):
    knowledge(policy_text(max_concurrent=5))
    real_read = ap.policy_text_at_origin_main
    calls = {
        "run-0": lambda: registry.add_run(_entry("run-0")),
        "run-1": lambda: registry.reserve_slot(
            {k: v for k, v in _entry("run-1").items() if k != "pid"}),
    }

    for run_id, call in calls.items():
        monkeypatch.setattr(registry, "_LIMITS", None)
        order = _spy_lock_and_read(monkeypatch)

        def spy_read(path):
            order.append("read")
            return real_read(path)

        monkeypatch.setattr(ap, "policy_text_at_origin_main", spy_read)
        call()
        assert order == ["read", "lock"]
        assert registry.remove_run(run_id) is True


def test_a_failing_policy_read_before_the_lock_still_gives_the_fallback(knowledge, monkeypatch, capsys):
    """A read that fails in a way `AP.load`/`policy_text_at_origin_main` do not
    already catch (a hang past their own timeout would surface the same way, as
    a raised exception) must not stop the spawn and must not run under the lock."""
    knowledge(policy_text(max_concurrent=5))
    order = _spy_lock_and_read(monkeypatch)

    def boom(path):
        order.append("read")
        raise RuntimeError("boom")

    monkeypatch.setattr(ap, "policy_text_at_origin_main", boom)
    ok, _current, reason = registry.can_spawn()
    assert order == ["read", "lock"]
    assert (ok, reason) == (True, None)
    lim = registry.limits()
    assert isinstance(lim, ap.Fallback)
    assert (lim.max_concurrent, lim.max_workers_per_team, lim.max_live_supervisors) == (16, 4, 4)
    assert "the fallback, because reading the policy raised" in capsys.readouterr().err


def test_reading_the_run_list_does_not_read_the_policy(knowledge, monkeypatch):
    """bin/run-register-api runs a deploy-pinned copy of registry.py that lists
    runs and never reads a limit; it must not need `agent_policy` or a git read."""
    knowledge(policy_text(max_concurrent=3))
    monkeypatch.setattr(ap, "policy_text_at_origin_main", lambda path: pytest.fail("read the policy"))
    registry.list_runs()
    registry.get_run("nope")
    registry.reap_stale()


# ---- the migration ------------------------------------------------------------


def test_the_migration_does_not_overwrite_a_policy_value_with_a_constant(knowledge, runs_root):
    """Before, a legacy registry (no max_workers_per_team, max_concurrent below 16)
    had max_concurrent raised to the 16 constant, and the check read the stored 16."""
    knowledge(policy_text(max_concurrent=8))
    (runs_root / "active.json").write_text(json.dumps({"version": 1, "max_concurrent": 4, "runs": []}))
    for i in range(8):
        registry.add_run(_entry(f"run-{i}"))
    ok, _current, reason = registry.can_spawn()
    assert ok is False
    assert reason == "global concurrency ceiling (8/8)"
    assert "max_concurrent" not in json.loads((runs_root / "active.json").read_text())


def test_stored_limits_are_neither_read_nor_kept(knowledge, runs_root):
    knowledge(policy_text(max_concurrent=3, max_workers_per_team=2, max_live_supervisors=2))
    (runs_root / "active.json").write_text(json.dumps(
        {"version": 1, "max_concurrent": 999, "max_workers_per_team": 999, "max_live_supervisors": 999, "runs": []}))
    registry.add_run(_entry("orch-0", agent="planning-orchestrator"))
    registry.add_run(_entry("orch-1", agent="planning-orchestrator"))
    assert registry.can_spawn(None, "planning-orchestrator")[2].startswith("live supervisor cap (2/2)")
    registry.add_run(_entry("run-2"))
    assert registry.can_spawn()[2] == "global concurrency ceiling (3/3)"
    stored = json.loads((runs_root / "active.json").read_text())
    assert not {"max_concurrent", "max_workers_per_team", "max_live_supervisors"} & stored.keys()


def test_a_stored_null_supervisor_limit_does_not_stop_a_supervisor_spawn(knowledge, runs_root):
    """The live active.json can carry `max_live_supervisors: null`; the old check
    compared a count against it and raised TypeError."""
    knowledge(policy_text(max_live_supervisors=2))
    (runs_root / "active.json").write_text(json.dumps({"version": 1, "max_live_supervisors": None, "runs": []}))
    registry.add_run(_entry("orch-0", agent="planning-orchestrator"))
    assert registry.can_spawn(None, "planning-orchestrator")[0] is True


def test_bootstrap_stores_no_limits(runs_root):
    registry.bootstrap()
    assert json.loads((runs_root / "active.json").read_text()) == {"version": 1, "runs": []}
