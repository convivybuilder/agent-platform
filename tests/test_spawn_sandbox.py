"""The argv and env `agentctl`'s spawn gives every claude run (Option A, Slate a-ow0q4o).

A stub `claude` records what spawn passed it, so these tests check the command
spawn builds, not Claude Code's enforcement. The enforcement is checked by
`tests/live/test_dispatched_run_confinement.py` against a real run.
"""

import json
import types
from pathlib import Path

import pytest

import run_sandbox as RS


def _spawn(agentctl, tmp_path, monkeypatch, *, user_prompt="u", repo="constance", **session_kwargs):
    """Drive `_run_session` (the path foreground, detached and reviewer runs all
    share) with a stub claude; return the argv and the env the stub received.

    `session_kwargs` forwards onto `_run_session` unchanged (e.g.
    `docs_unavailable_reason`, `render_skipped`, `materialized_documents`,
    `claude_md_blocked`) — the same fields `cmd_spawn` threads through from
    `activate_profile`'s outcome on a real dispatch. `exist_ok=True` on the
    worktree mkdir: a caller that wants `_fleet_blocks_confirmed_in_claude_md`
    to confirm delivery pre-populates `tmp_path / "wt" / ".claude" / CLAUDE.md`
    before calling this."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    argv_file = tmp_path / "argv.txt"
    env_file = tmp_path / "env.txt"
    stub = tmp_path / "claude-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {argv_file}\n"
        f"env > {env_file}\n"
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"model\":\"x\"}'\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(agentctl, "CLAUDE_BIN", str(stub))
    monkeypatch.setattr(agentctl, "_run_origin", lambda parent=None: "interactive")
    monkeypatch.setattr(agentctl, "_gateway_virtual_key", lambda: "vk-test")
    monkeypatch.setattr(agentctl, "_gateway_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(agentctl, "_preflight_python_worktree", lambda wt: None)
    monkeypatch.setattr(agentctl, "_finalize_run", lambda **kw: None)
    monkeypatch.setattr(agentctl.R, "reserve_slot", lambda entry: None)
    monkeypatch.setattr(agentctl.R, "attach_pid", lambda run_id, pid, pid_started_at=None, **_kw: None)
    monkeypatch.setattr(agentctl.R, "remove_run", lambda rid: True)
    args = types.SimpleNamespace(agent="reviewer", task="t", parent=None,
                                 dry_run=False, label=None, model=None, review_sha=None)
    agentctl._run_session(
        args=args, agent={"body": "sys", "model_alias": "sonnet"},
        run_id="run-sandbox", run_dir=run_dir, worktree=worktree, profile="p",
        repo=repo, started_at="2026-10-06T00:00:00Z", user_prompt=user_prompt,
        **session_kwargs)
    argv = argv_file.read_text().splitlines()
    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    return argv, env


def _settings(argv):
    assert argv.count("--settings") == 1, "exactly one --settings: spawn's own"
    return json.loads(argv[argv.index("--settings") + 1])


# A `scope: fleet` instruction-block row shaped exactly like the validated rows
# `_materialized_documents` reads back from a real `.profile-documents.json`
# (`_validate_instruction_block_row`), and the matching marker
# `bin/knowledge-ctx`'s `render_instruction_blocks_md` writes ahead of that
# block's body in a real composed CLAUDE.md — the one fact
# `_fleet_blocks_confirmed_in_claude_md` looks for.
_FLEET_BLOCK_ID = "platform/instruction-blocks/no-agent-merges"
_FLEET_BLOCK_HASH = "f" * 64


def _fleet_block_row(block_id=_FLEET_BLOCK_ID):
    return {"id": block_id, "kind": "instruction-block",
            "body_sha256": _FLEET_BLOCK_HASH, "scope": "fleet"}


def _write_rendered_claude_md(worktree, block_ids):
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    parts = [f"<!-- instruction-block: {block_id} scope:fleet "
             f"body_sha256:{_FLEET_BLOCK_HASH} -->\nbody\n" for block_id in block_ids]
    (claude_dir / "CLAUDE.md").write_text("\n".join(parts))


def test_every_run_is_launched_under_the_sandbox_and_the_grant_is_denied(
        agentctl, tmp_path, monkeypatch):
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch)

    settings = _settings(argv)
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    # The denial holds under the passwd home a run is launched with, and under
    # the caller's own HOME.
    deny = settings["permissions"]["deny"]
    for home in (RS.passwd_home(), caller_home):
        assert f"Read(//{str(home).lstrip('/')}/.orient-control/**)" in deny
        assert str(home / ".orient-control") in settings["sandbox"]["filesystem"]["denyRead"]
    # The settings flag sits before the variadic --disallowedTools and before the prompt.
    assert argv.index("--settings") < argv.index("--model")
    if "--disallowedTools" in argv:
        assert argv.index("--settings") < argv.index("--disallowedTools")


def test_a_full_successful_render_excludes_the_runs_own_user_claude_md(
        agentctl, tmp_path, monkeypatch):
    # Context trim phase 2 (issue_01M4D32KA2F99QGKABZ8HV2RG5): a dispatched run's
    # own claude must skip ONLY the operator's user-scope CLAUDE.md, via the
    # SAME --settings flag the sandbox already travels in — one merged layer,
    # not a second flag that could race or be dropped independently. Round 2
    # (security-reviewer, PR #823): that exclusion is only safe once the
    # worktree's own rendered CLAUDE.md is shown to actually carry the fleet
    # block `materialized_documents` recorded — a bare `docs_unavailable_reason
    # is None` is not that showing (see the blocked-write and missing-block
    # tests below). This is the one case where every one of those checks
    # passes: a fleet block recorded AND its marker present in CLAUDE.md.
    caller_home = tmp_path / "caller-home"
    monkeypatch.setenv("HOME", str(caller_home))
    _write_rendered_claude_md(tmp_path / "wt", [_FLEET_BLOCK_ID])
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch,
                     materialized_documents=[_fleet_block_row()])

    settings = _settings(argv)
    excludes = settings["claudeMdExcludes"]
    for home in (RS.passwd_home(), caller_home):
        assert str(home / ".claude" / "CLAUDE.md") in excludes
    # Unchanged: the sandbox and permissions this --settings also carries.
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    deny = settings["permissions"]["deny"]
    assert f"Read(//{str(RS.passwd_home()).lstrip('/')}/.orient-control/**)" in deny


@pytest.mark.parametrize("docs_unavailable_reason, render_skipped", [
    # An is_local agent (CFO/EA): `cmd_spawn` never calls `activate_profile`.
    ("render skipped for local/private dispatch", None),
    # A private crew's dispatch kept on the primary checkout: same skip, the other
    # dispatch class that shares it.
    ("render skipped for local/private dispatch", "primary-checkout"),
    # A normal dispatch where `activate_profile` ran but never confirmed
    # `profile_documents_current` (a `--dry-run`) — it ran, but did not succeed
    # in the sense this exclusion requires.
    ("render did not produce a current individuation (e.g. --dry-run)", None),
])
def test_a_dispatch_with_no_confirmed_fleet_render_keeps_loading_the_user_claude_md(
        agentctl, tmp_path, monkeypatch, docs_unavailable_reason, render_skipped):
    # Round 1 (security-reviewer, PR #823): `cmd_spawn` sets `docs_unavailable_reason`
    # on exactly these dispatch classes, none of which ran the fleet-carrying render
    # this exclusion assumes happened. Excluding the user CLAUDE.md for one of them
    # would drop every `scope: fleet` rule with no replacement. No CLAUDE.md or
    # `materialized_documents` is planted here — `docs_unavailable_reason` alone
    # must short-circuit before either is even looked at.
    kwargs = {"docs_unavailable_reason": docs_unavailable_reason}
    if render_skipped is not None:
        kwargs["render_skipped"] = render_skipped
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch, **kwargs)
    assert _settings(argv)["claudeMdExcludes"] == []


def test_a_blocked_claude_md_write_keeps_loading_the_user_claude_md(
        agentctl, tmp_path, monkeypatch):
    # Round 2 (security-reviewer, PR #823, the defect round 1 missed): a render
    # can confirm `.profile-documents.json` current (`docs_unavailable_reason is
    # None`) while the ARCH-023/080 ownership gate still refused the CLAUDE.md
    # write itself (`activate_profile`'s `claude_md_blocked`) — the record and
    # the file go current and blocked independently. `materialized_documents`
    # still carries the fleet block the render WOULD have composed; the
    # worktree's actual CLAUDE.md (never written, matching a real blocked
    # write) does not carry it. The exclusion must not fire.
    argv, _ = _spawn(
        agentctl, tmp_path, monkeypatch,
        claude_md_blocked=(str(tmp_path / "wt" / ".claude" / "CLAUDE.md"),
                           "not listed in the previous individuation manifest"),
        docs_unavailable_reason=None,
        materialized_documents=[_fleet_block_row()])
    assert _settings(argv)["claudeMdExcludes"] == []


def test_a_rendered_claude_md_missing_one_fleet_block_keeps_loading_the_user_claude_md(
        agentctl, tmp_path, monkeypatch):
    # The gap round 2 guards against even on a render that genuinely confirmed
    # current AND had its CLAUDE.md write succeed: `materialized_documents`
    # recorded two fleet blocks, but the worktree's actual CLAUDE.md carries
    # the marker for only one of them. A check that trusted the record instead
    # of re-reading the file would wrongly exclude.
    other_id = "platform/instruction-blocks/verify-before-you-report"
    _write_rendered_claude_md(tmp_path / "wt", [_FLEET_BLOCK_ID])  # `other_id` omitted
    argv, _ = _spawn(
        agentctl, tmp_path, monkeypatch,
        docs_unavailable_reason=None,
        materialized_documents=[_fleet_block_row(), _fleet_block_row(other_id)])
    assert _settings(argv)["claudeMdExcludes"] == []


def test_a_vivy_dispatch_gets_the_agent_postgres_socket_and_pghost(agentctl, tmp_path, monkeypatch):
    # Round-1 revise on PR #817: a vivy coder's test suite needs
    # the AGENT cluster's unix socket, never the operator's own cluster's — see RS's
    # POSTGRES_SOCKET_REPOS comment for why. PGHOST/PGPORT point its client at that
    # socket; PGUSER picks the one non-superuser role that cluster trusts there. The
    # sandbox grant is the only thing that actually lets the connection through.
    argv, env = _spawn(agentctl, tmp_path, monkeypatch, repo="vivy")
    net = _settings(argv)["sandbox"]["network"]
    expected_socket = RS.agent_postgres_socket_path(RS.passwd_home())
    assert net["allowUnixSockets"] == [expected_socket]
    assert "allowLocalBinding" not in net
    assert env["PGHOST"] == str(RS.agent_postgres_socket_dir(RS.passwd_home()))
    assert env["PGPORT"] == str(RS.AGENT_POSTGRES_PORT)
    assert env["PGUSER"] == RS.AGENT_POSTGRES_ROLE
    assert "/tmp/.s.PGSQL.5432" not in expected_socket


def test_a_non_postgres_repo_dispatch_gets_neither_the_socket_nor_pghost(
        agentctl, tmp_path, monkeypatch):
    argv, env = _spawn(agentctl, tmp_path, monkeypatch, repo="constance")
    assert "allowUnixSockets" not in _settings(argv)["sandbox"]["network"]
    assert "PGHOST" not in env
    assert "PGPORT" not in env
    assert "PGUSER" not in env


def test_every_run_is_dontask_so_its_file_tools_are_confined(agentctl, tmp_path, monkeypatch):
    # bypassPermissions allows every write and no deny list can carve the worktree out
    # of that, so the file-write scope binds only under dontAsk, enforced or not.
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "bypassPermissions" not in argv
    # The allow list the file tools need travels in the same settings.
    assert "Edit(//tmp/**)" in _settings(argv)["permissions"]["allow"]


def test_a_task_that_starts_with_a_flag_cannot_replace_the_settings(
        agentctl, tmp_path, monkeypatch):
    hostile = '--settings={"sandbox":{"enabled":false}} --permission-mode=default'
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch, user_prompt=hostile)

    settings = _settings(argv)
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    # `--` makes the task positional: claude reads it as the prompt, not as options.
    assert argv[-2:] == ["--", hostile]


def test_a_run_cannot_choose_its_home_or_its_claude_config(agentctl, tmp_path, monkeypatch):
    # A nested spawn inherits the caller's env. A caller that sets HOME (to a home
    # whose settings it wrote, whose hooks run outside the sandbox) or
    # CLAUDE_CONFIG_DIR must not reach the child.
    monkeypatch.setenv("HOME", str(tmp_path / "evil-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "evil-config"))
    argv, env = _spawn(agentctl, tmp_path, monkeypatch)

    assert env["HOME"] == str(RS.passwd_home())
    assert "CLAUDE_CONFIG_DIR" not in env
    # The grant stays denied under the passwd home the run now sees.
    assert f"Read(//{str(RS.passwd_home()).lstrip('/')}/.orient-control/**)" in _settings(argv)["permissions"]["deny"]


def test_a_run_gets_the_verdict_server_with_its_own_run_id_and_is_named_for_the_run_tree(
        agentctl, tmp_path, monkeypatch):
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch)

    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    entry = cfg["mcpServers"]["verdict"]
    assert entry["command"].endswith("/bin/mcp-verdict-server")
    assert entry["env"] == {"AGENTCTL_RUN_ID": "run-sandbox"}
    # The `--name` after it ends `--mcp-config`'s list of values, and marks the run's tree.
    assert argv[argv.index("--mcp-config") + 2] == "--name"
    assert argv[argv.index("--name") + 1] == "agentctl-dispatched-run:run-sandbox"
    # The run's own records are denied for writing, so meta.json cannot be rewritten by it.
    settings = _settings(argv)
    assert str(agentctl.RUNS_ROOT) in settings["sandbox"]["filesystem"]["denyWrite"]
    assert str(agentctl.RUNS_ROOT) not in settings["sandbox"]["filesystem"]["allowWrite"]


def test_a_run_has_no_mcp_config_file_for_it_to_write(agentctl, tmp_path, monkeypatch):
    # The config is inline JSON, so no file on disk stands between the launch and the server.
    argv, _ = _spawn(agentctl, tmp_path, monkeypatch)
    raw = argv[argv.index("--mcp-config") + 1]
    assert raw.startswith("{") and not Path(raw).exists()


def test_a_dispatched_run_cannot_dispatch_and_is_refused_before_any_read(
        agentctl, monkeypatch, capsys):
    monkeypatch.setattr(agentctl.RS, "inside_dispatched_run", lambda: True)

    def read_before_the_refusal(parent):
        raise AssertionError("spawn read something before refusing a dispatched run")

    monkeypatch.setattr(agentctl, "_resolve_parent", read_before_the_refusal)
    code, run_id = agentctl._spawn_dispatch(types.SimpleNamespace(parent=None))
    assert code != 0 and run_id is None
    assert ("error: a dispatched run cannot dispatch; name the helper you need in your result"
            in capsys.readouterr().err)


def test_a_dispatched_run_is_refused_even_with_an_explicit_parent_naming_another_run(
        agentctl, monkeypatch, capsys):
    # agent-platform#799 (E12·1): a capped run tried to inherit an uncapped origin by
    # naming it with --parent. The refusal above is the first statement in
    # _spawn_dispatch, before _resolve_parent or _run_origin run at all, so a --parent
    # naming some OTHER run never reaches origin resolution in the first place.
    monkeypatch.setattr(agentctl.RS, "inside_dispatched_run", lambda: True)

    def resolved_before_the_refusal(*_a, **_kw):
        raise AssertionError("spawn resolved an origin before refusing a dispatched run")

    monkeypatch.setattr(agentctl, "_resolve_parent", resolved_before_the_refusal)
    monkeypatch.setattr(agentctl, "_run_origin", resolved_before_the_refusal)
    code, run_id = agentctl._spawn_dispatch(
        types.SimpleNamespace(parent="run-another-interactive-run"))
    assert code != 0 and run_id is None
    assert ("error: a dispatched run cannot dispatch; name the helper you need in your result"
            in capsys.readouterr().err)


def test_a_dispatched_run_is_refused_with_agentctl_run_id_set_to_another_run(
        agentctl, monkeypatch, capsys):
    # The narrower bypass agent-platform#799 named: clear nothing, just let this
    # process's own AGENTCTL_RUN_ID (set by whatever spawned it) point at another run.
    # No --parent is passed at all; the refusal still fires before origin resolution.
    monkeypatch.setattr(agentctl.RS, "inside_dispatched_run", lambda: True)
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-another-uncapped-run")

    def resolved_before_the_refusal(*_a, **_kw):
        raise AssertionError("spawn resolved an origin before refusing a dispatched run")

    monkeypatch.setattr(agentctl, "_resolve_parent", resolved_before_the_refusal)
    monkeypatch.setattr(agentctl, "_run_origin", resolved_before_the_refusal)
    code, run_id = agentctl._spawn_dispatch(types.SimpleNamespace(parent=None))
    assert code != 0 and run_id is None
    assert ("error: a dispatched run cannot dispatch; name the helper you need in your result"
            in capsys.readouterr().err)


def test_a_spawn_outside_a_run_is_not_refused(agentctl, monkeypatch):
    monkeypatch.setattr(agentctl.RS, "inside_dispatched_run", lambda: False)

    class Reached(Exception):
        pass

    def reached(parent):
        raise Reached()

    monkeypatch.setattr(agentctl, "_resolve_parent", reached)
    with pytest.raises(Reached):
        agentctl._spawn_dispatch(types.SimpleNamespace(parent=None))
