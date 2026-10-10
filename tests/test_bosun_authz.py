"""Tests for lib/agent-platform/bosun_authz.py and `agentctl authz replay`
(E12·3, platform/decisions/0057).

Stage one is a policy and audit layer, not a containment boundary — these
tests assert the DECISION is correct, not that anything is contained; nothing
here claims otherwise.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bosun_authz as AU

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Object-scoped requests: the ceiling binds.
# ---------------------------------------------------------------------------


def test_object_scoped_request_inside_ceiling_is_allowed():
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": "Read", "input": {"file_path": "/work/allowed/file.txt"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is True
    assert isinstance(decision.authorization, AU.ObjectScopedAuthorization)
    assert decision.authorization.object_ref == AU.scope(AU.PATH, "/work/allowed/file.txt")


def test_object_scoped_request_outside_a_reduced_ceiling_is_denied():
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": "Read", "input": {"file_path": "/work/other/secret.txt"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert decision.authorization is None
    assert "ceiling" in decision.reason


def test_ceiling_covers_requires_a_path_boundary_not_a_bare_prefix():
    ceiling = frozenset({AU.scope(AU.PATH, "/work/allowed")})
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/work/allowed")) is True
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/work/allowed/file.txt")) is True
    # "/work/allowed" must not match as a bare string prefix of a sibling
    # directory that merely starts with the same characters.
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/work/allowed-but-not-really/x")) is False


# ---------------------------------------------------------------------------
# The ceiling's TEXT serialization (platform/decisions/0092 Handoff:
# "`ceiling` is stored as an opaque string you give meaning to").
# ---------------------------------------------------------------------------


def test_full_ceiling_text_covers_every_object():
    ceiling = AU.ceiling_entries_from_text("full")
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/anything/at/all")) is True
    assert AU.ceiling_covers(ceiling, AU.scope(AU.DOC, "platform/decisions/0001")) is True


def test_ceiling_text_round_trips_through_entries():
    entries = frozenset({AU.scope(AU.PATH, "/work/allowed"), AU.scope(AU.ISSUE, "issue_1")})
    raw = AU.ceiling_text_from_entries(entries)
    assert AU.ceiling_entries_from_text(raw) == entries


def test_ceiling_text_from_a_json_array_binds_exactly_those_entries():
    raw = AU.ceiling_text_from_entries({AU.scope(AU.PATH, "/work/allowed")})
    ceiling = AU.ceiling_entries_from_text(raw)
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/work/allowed/file.txt")) is True
    assert AU.ceiling_covers(ceiling, AU.scope(AU.PATH, "/etc/passwd")) is False


def test_ceiling_text_from_unparseable_json_raises():
    with pytest.raises(ValueError):
        AU.ceiling_entries_from_text("not json and not 'full'")


def test_ceiling_text_from_a_json_object_rather_than_an_array_raises():
    with pytest.raises(ValueError):
        AU.ceiling_entries_from_text('{"not": "an array"}')


def test_ceiling_text_from_a_json_array_of_non_strings_raises():
    with pytest.raises(ValueError):
        AU.ceiling_entries_from_text("[1, 2, 3]")


def test_ceiling_text_from_a_json_array_containing_the_full_sentinel_raises():
    """FIX (security suggestion, round 2): `"*"` inside a JSON array was a
    second spelling of the `FULL_CEILING` sentinel. This refusal gives the
    sentinel one spelling; it does not make an array narrower than "full" —
    an array can still cover everything a given kind can name, e.g.
    `["path:/"]` for PATH (round 3)."""
    with pytest.raises(ValueError):
        AU.ceiling_entries_from_text(json.dumps([AU.FULL_CEILING]))
    with pytest.raises(ValueError):
        AU.ceiling_entries_from_text(json.dumps([AU.scope(AU.PATH, "/work/allowed"),
                                                  AU.FULL_CEILING]))


# ---------------------------------------------------------------------------
# Path traversal: a `..` segment is denied lexically, never resolved and
# compared — a bypass of the ceiling this module exists to check exactly
# (platform/decisions/0057).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,key", [
    ("Read", "file_path"),
    ("Edit", "file_path"),
    ("Write", "file_path"),
    ("NotebookEdit", "notebook_path"),
])
def test_a_traversal_out_of_the_ceiling_is_denied_for_every_path_tool(tool_name, key):
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": tool_name,
              "input": {key: "/work/allowed/../../../../etc/passwd"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert decision.authorization is None
    assert "'..'" in decision.reason


def test_notebook_edit_file_path_fallback_key_is_also_checked_for_traversal():
    # NotebookEdit tries notebook_path first, then falls back to file_path —
    # the fallback key must get the same traversal check as the primary one.
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": "NotebookEdit",
              "input": {"file_path": "/work/allowed/../../../../etc/passwd"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert "'..'" in decision.reason


def test_a_traversal_that_would_stay_inside_the_ceiling_is_still_denied():
    # /work/allowed/sub/../file.txt lexically resolves to
    # /work/allowed/file.txt — still inside the ceiling if collapsed and
    # compared. It must still be denied: the rule is "deny any '..'
    # segment", not "normalize and check whether it lands inside".
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": "Read", "input": {"file_path": "/work/allowed/sub/../file.txt"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert "'..'" in decision.reason


def test_a_relative_path_is_denied():
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    request = {"tool_name": "Read", "input": {"file_path": "relative/file.txt"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert "not an absolute path" in decision.reason


def test_repeated_separators_and_dot_segments_normalize_to_the_same_path():
    grant = AU.SessionGrant(session_id="s1",
                            ceiling=frozenset({AU.scope(AU.PATH, "/work/allowed")}))
    expected = AU.scope(AU.PATH, "/work/allowed/x")
    for file_path in ("/work/allowed//x", "/work/allowed/./x"):
        request = {"tool_name": "Read", "input": {"file_path": file_path}}
        decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
        assert decision.allow is True, file_path
        assert decision.authorization.object_ref == expected, file_path


def test_scope_refuses_a_relative_or_traversal_path_value():
    with pytest.raises(ValueError):
        AU.scope(AU.PATH, "relative/path")
    with pytest.raises(ValueError):
        AU.scope(AU.PATH, "/work/../etc")


def test_session_grant_refuses_a_traversal_ceiling_entry_however_built():
    # Built via scope() — raises before SessionGrant is even reached.
    with pytest.raises(ValueError):
        AU.SessionGrant(session_id="s1",
                        ceiling=frozenset({AU.scope(AU.PATH, "/work/../etc")}))
    # Built as a raw "kind:value" string, bypassing scope() entirely —
    # SessionGrant itself is the point that refuses to store a widened
    # ceiling, regardless of how the entry arrived.
    with pytest.raises(ValueError):
        AU.SessionGrant(session_id="s1", ceiling=frozenset({"path:/work/../etc"}))
    with pytest.raises(ValueError):
        AU.SessionGrant(session_id="s1", ceiling=frozenset({"path:relative/etc"}))


def test_grant_from_meta_refuses_a_traversal_worktree():
    with pytest.raises(ValueError):
        AU.grant_from_meta({"run_id": "run-x", "worktree": "/work/../etc"})


def test_names_target_tool_with_no_extractable_object_is_denied():
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset({AU.scope(AU.PATH, "/work")}))
    request = {"tool_name": "Read", "input": {}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is False
    assert "no object could be read" in decision.reason


# ---------------------------------------------------------------------------
# Executing requests: a boundary grant, never checked against the ceiling.
# ---------------------------------------------------------------------------


def test_executing_request_gets_a_boundary_grant_and_ignores_the_ceiling():
    # The ceiling is EMPTY — if an executing request were checked against it,
    # it could not possibly be allowed.
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset())
    request = {"tool_name": "Bash", "input": {"command": "rm -rf /work/allowed"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is True
    assert isinstance(decision.authorization, AU.BoundaryGrant)
    assert decision.authorization.tool_name == "Bash"


def test_unknown_tool_is_classified_as_executes():
    assert AU.classify("SomeFutureTool", {"anything": "x"}) == AU.EXECUTES


def test_unknown_tool_is_never_silently_treated_as_object_scoped():
    # Input shaped exactly like a names-its-target call (a file_path key) must
    # not fool the classifier for a tool this module does not recognise.
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset())
    request = {"tool_name": "SomeFutureTool", "input": {"file_path": "/work/file.txt"}}
    decision = AU.decide(request, grant, AU.PermissionSet(), NOW)
    assert decision.allow is True
    assert isinstance(decision.authorization, AU.BoundaryGrant)


# ---------------------------------------------------------------------------
# Revocation: checked on every call, regardless of class or lever.
# ---------------------------------------------------------------------------


def test_a_call_after_revocation_is_denied():
    revoked_at = NOW
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset({AU.scope(AU.PATH, "/work")}),
                            revoked_at=revoked_at)
    object_request = {"tool_name": "Read", "input": {"file_path": "/work/file.txt"}}
    exec_request = {"tool_name": "Bash", "input": {"command": "ls"}}

    before = AU.decide(object_request, grant, AU.PermissionSet(), revoked_at - timedelta(seconds=1))
    assert before.allow is True

    after_object = AU.decide(object_request, grant, AU.PermissionSet(), revoked_at)
    after_exec = AU.decide(exec_request, grant, AU.PermissionSet(), revoked_at)
    assert after_object.allow is False
    assert "revoked" in after_object.reason
    assert after_exec.allow is False, "revocation binds an executing request too"


# ---------------------------------------------------------------------------
# Strict only (platform/decisions/0057, ruled 2026-10-03): every call is
# checked, and there is no other availability position to answer for.
# ---------------------------------------------------------------------------


def test_object_and_executing_requests_get_their_strict_answers():
    ceiling = frozenset({AU.scope(AU.PATH, "/work")})
    grant = AU.SessionGrant(session_id="s1", ceiling=ceiling)
    object_request = {"tool_name": "Read", "input": {"file_path": "/work/file.txt"}}
    exec_request = {"tool_name": "Bash", "input": {"command": "ls"}}

    object_decision = AU.decide(object_request, grant, AU.PermissionSet(), NOW)
    exec_decision = AU.decide(exec_request, grant, AU.PermissionSet(), NOW)
    assert object_decision.allow is True
    assert exec_decision.allow is True
    assert isinstance(exec_decision.authorization, AU.BoundaryGrant)


# ---------------------------------------------------------------------------
# The agent's own permission set — denied regardless of class or ceiling.
# ---------------------------------------------------------------------------


def test_permission_set_denial_overrides_an_in_ceiling_object():
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset({AU.scope(AU.PATH, "/work")}))
    perms = AU.PermissionSet(denied=frozenset({"Read"}))
    request = {"tool_name": "Read", "input": {"file_path": "/work/file.txt"}}
    decision = AU.decide(request, grant, perms, NOW)
    assert decision.allow is False
    assert "permission set" in decision.reason


def test_permission_set_wildcard_denial():
    perms = AU.PermissionSet(denied=frozenset({"mcp__slack__*"}))
    assert perms.denies("mcp__slack__send_message") is True
    assert perms.denies("mcp__kb__kb_get") is False


def test_permissions_from_policy_record_reads_the_denied_list():
    perms = AU.permissions_from_policy_record({"denied": ["Agent", "mcp__ops__issue_create"]})
    assert perms.denies("Agent")
    assert perms.denies("mcp__ops__issue_create")
    assert not perms.denies("Read")


def test_permissions_from_policy_record_handles_a_missing_record():
    assert AU.permissions_from_policy_record(None).denied == frozenset()


# ---------------------------------------------------------------------------
# grant_from_meta — the replay's proxy ceiling.
# ---------------------------------------------------------------------------


def test_grant_from_meta_builds_a_path_ceiling_from_the_worktree():
    grant = AU.grant_from_meta({"run_id": "run-abc", "worktree": "/srv/convivy/w/foo"})
    assert grant.session_id == "run-abc"
    assert grant.ceiling == frozenset({AU.scope(AU.PATH, "/srv/convivy/w/foo")})


def test_grant_from_meta_with_no_worktree_has_an_empty_ceiling():
    grant = AU.grant_from_meta({"run_id": "run-abc"})
    assert grant.ceiling == frozenset()


# ---------------------------------------------------------------------------
# Replay — counts a small synthetic tool_calls.jsonl correctly.
# ---------------------------------------------------------------------------


def test_replay_counts_a_synthetic_tool_calls_log():
    grant = AU.SessionGrant(session_id="s1", ceiling=frozenset({AU.scope(AU.PATH, "/work")}))
    requests = [
        {"tool_name": "Read", "input": {"file_path": "/work/in.txt"}},       # names_target/allow
        {"tool_name": "Read", "input": {"file_path": "/elsewhere/out.txt"}}, # names_target/deny
        {"tool_name": "Bash", "input": {"command": "ls"}},                   # executes/allow
        {"tool_name": "Bash", "input": {"command": "ls"}},                   # executes/allow
        {"tool_name": "Grep", "input": {"pattern": "x"}},                    # executes/allow
    ]
    summary = AU.replay(requests, grant, AU.PermissionSet(), NOW)
    assert summary.total == 5
    assert summary.by_class == {"names_target": 2, "executes": 3}
    assert summary.by_verdict == {"allow": 4, "deny": 1}
    assert summary.by_class_verdict == {
        "names_target/allow": 1,
        "names_target/deny": 1,
        "executes/allow": 3,
    }


# ---------------------------------------------------------------------------
# `agentctl authz replay` — the CLI reads a run dir and reports the same
# counts the pure function computes.
# ---------------------------------------------------------------------------


def _write_run_dir(tmp_path, *, worktree, tool_calls, denied=()):
    run_dir = tmp_path / "run-test1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"run_id": "run-test1", "worktree": str(worktree)}))
    (run_dir / "tool_calls.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in tool_calls))
    (run_dir / "policy.json").write_text(json.dumps({"denied": list(denied)}))
    return run_dir


def test_cmd_authz_replay_reads_a_run_dir_and_reports_the_same_counts(agentctl, tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir()
    run_dir = _write_run_dir(
        tmp_path, worktree=work,
        tool_calls=[
            {"tool_name": "Read", "input": {"file_path": str(work / "f.txt")}},
            {"tool_name": "Read", "input": {"file_path": "/elsewhere/g.txt"}},
            {"tool_name": "Bash", "input": {"command": "ls"}},
        ],
    )
    args = SimpleNamespace(run=[str(run_dir)], all_since=None, json=True)
    rc = agentctl.cmd_authz_replay(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["runs_replayed"] == 1
    assert out["total_calls"] == 3
    assert out["by_class"] == {"names_target": 2, "executes": 1}
    assert out["by_verdict"] == {"allow": 2, "deny": 1}


def test_cmd_authz_replay_honours_the_run_dirs_own_policy_json(agentctl, tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir()
    run_dir = _write_run_dir(
        tmp_path, worktree=work,
        tool_calls=[{"tool_name": "Read", "input": {"file_path": str(work / "f.txt")}}],
        denied=["Read"],
    )
    args = SimpleNamespace(run=[str(run_dir)], all_since=None, json=True)
    rc = agentctl.cmd_authz_replay(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["by_verdict"] == {"deny": 1}


def test_cmd_authz_replay_counts_a_malformed_tool_calls_run_as_unreadable_and_continues(
        agentctl, tmp_path, capsys):
    # A malformed line in one run's tool_calls.jsonl — a file that run's own
    # agent process can write — must not abort the whole sweep; the run is
    # counted as unreadable and the rest still replay.
    work = tmp_path / "work"
    work.mkdir()
    good_run = _write_run_dir(
        tmp_path, worktree=work,
        tool_calls=[{"tool_name": "Read", "input": {"file_path": str(work / "f.txt")}}],
    )
    bad_run = tmp_path / "run-bad"
    bad_run.mkdir()
    (bad_run / "meta.json").write_text(json.dumps({"run_id": "run-bad", "worktree": str(work)}))
    (bad_run / "tool_calls.jsonl").write_text("{not valid json\n")
    (bad_run / "policy.json").write_text(json.dumps({"denied": []}))

    args = SimpleNamespace(run=[str(good_run), str(bad_run)], all_since=None, json=True)
    rc = agentctl.cmd_authz_replay(args)
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["runs_replayed"] == 1
    assert out["total_calls"] == 1
    assert "unreadable tool_calls.jsonl" in captured.err


def test_cmd_authz_replay_counts_a_bad_worktree_run_as_unreadable_and_continues(
        agentctl, tmp_path, capsys):
    # A run's own meta.json can carry a `..`-bearing or relative `worktree` —
    # the same untrusted field `grant_from_meta` now refuses lexically (via
    # `scope()`). That must not abort the whole sweep; each bad run is
    # counted as unreadable and the rest still replay.
    work = tmp_path / "work"
    work.mkdir()
    good_run = _write_run_dir(
        tmp_path, worktree=work,
        tool_calls=[{"tool_name": "Read", "input": {"file_path": str(work / "f.txt")}}],
    )

    traversal_run = tmp_path / "run-traversal"
    traversal_run.mkdir()
    (traversal_run / "meta.json").write_text(
        json.dumps({"run_id": "run-traversal", "worktree": str(work / ".." / "escaped")}))
    (traversal_run / "tool_calls.jsonl").write_text(
        json.dumps({"tool_name": "Bash", "input": {"command": "ls"}}) + "\n")
    (traversal_run / "policy.json").write_text(json.dumps({"denied": []}))

    relative_run = tmp_path / "run-relative"
    relative_run.mkdir()
    (relative_run / "meta.json").write_text(
        json.dumps({"run_id": "run-relative", "worktree": "relative/worktree"}))
    (relative_run / "tool_calls.jsonl").write_text(
        json.dumps({"tool_name": "Bash", "input": {"command": "ls"}}) + "\n")
    (relative_run / "policy.json").write_text(json.dumps({"denied": []}))

    args = SimpleNamespace(
        run=[str(good_run), str(traversal_run), str(relative_run)],
        all_since=None, json=True)
    rc = agentctl.cmd_authz_replay(args)
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["runs_replayed"] == 1
    assert out["total_calls"] == 1
    assert "unreadable worktree in meta.json" in captured.err
    assert str(traversal_run) in captured.err
    assert str(relative_run) in captured.err


def test_cmd_authz_replay_refuses_when_no_run_dirs_resolve(agentctl, tmp_path, capsys):
    args = SimpleNamespace(run=["no-such-run-id"], all_since=None, json=False)
    monkeypatch_runs_root = tmp_path / "agent-runs-empty"
    monkeypatch_runs_root.mkdir()
    orig = agentctl.RUNS_ROOT
    agentctl.RUNS_ROOT = monkeypatch_runs_root
    try:
        rc = agentctl.cmd_authz_replay(args)
    finally:
        agentctl.RUNS_ROOT = orig
    assert rc == 1
