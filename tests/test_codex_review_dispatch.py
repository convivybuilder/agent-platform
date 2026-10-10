"""Codex lane slice 4: a Codex reviewer dispatch (issue CQ·429).

NO TEST HERE RUNS CODEX. A stub `codex` on PATH records its own argv/cwd/env and
either writes a canned `--output-last-message` file, prints a canned `--json`
event stream, or both — the same substitution `test_codex_probe.py` makes for the
conformance probe. `agentctl review-post` is stubbed too (via `run_verdict.post`,
the one function both the MCP verdict server and this dispatch path call): no
test here makes a real GitHub call either.
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

MODEL = "gpt-6-astra"


@pytest.fixture
def CP():
    import codex_probe
    return codex_probe


@pytest.fixture
def CB():
    import codex_bridge
    return codex_bridge


@pytest.fixture(autouse=True)
def _guards_from_this_checkout(CB, monkeypatch):
    monkeypatch.setattr(CB, "default_tools_dir", lambda: TOOLS)


@pytest.fixture(autouse=True)
def _codex_version(CP, monkeypatch):
    monkeypatch.setattr(CP, "codex_version", lambda codex_bin=None: "codex-cli 0.153.2")


def _trust(home):
    key = f"{home / 'hooks.json'}:pre_tool_use:0:0"
    (home / "config.toml").write_text(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:abc"\n')


@pytest.fixture
def home(tmp_path, CB):
    h = tmp_path / "codex-home"
    CB.install(h)
    _trust(h)
    return h


def _probe_passed(CP, home, model=MODEL):
    CP.record(home, model, {"passed": True})


# --- the stub `codex` on PATH ----------------------------------------------------

_TURN_COMPLETED = ('{"type":"turn.completed","usage":{"input_tokens":1000,'
                   '"cached_input_tokens":0,"cache_write_input_tokens":0,'
                   '"output_tokens":100,"reasoning_output_tokens":0}}\n')


def _stub_codex(tmp_path, *, last_message=None, stream=_TURN_COMPLETED, exit_code=0):
    """A `codex` that records {argv, cwd, env} to launch.json, writes `last_message`
    (a string, verbatim) to the path named after `--output-last-message` when one is
    given, and prints `stream` on stdout. `last_message=None` leaves that file
    untouched, as an installed codex-cli that doesn't honour the flag would."""
    d = tmp_path / "stub"
    d.mkdir(exist_ok=True)
    launch_path = d / "launch.json"
    stub = d / "codex"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        f"json.dump({{'argv': argv, 'cwd': os.getcwd(), 'env': dict(os.environ)}}, "
        f"open({str(launch_path)!r}, 'w'))\n"
        f"last_message = {last_message!r}\n"
        "if last_message is not None and '--output-last-message' in argv:\n"
        "    open(argv[argv.index('--output-last-message') + 1], 'w').write(last_message)\n"
        f"sys.stdout.write({stream!r})\n"
        f"sys.exit({exit_code})\n")
    stub.chmod(0o755)
    return stub, launch_path


def _fake_post(calls, *, returncode=0, stdout="posted", stderr=""):
    """A `run_verdict.post` stand-in: records the Target it was called with and
    returns a canned CompletedProcess, so no test here shells out to a real
    `agentctl review-post` (which would try to reach the Library over the network).
    """
    import subprocess

    def post(target, verdict, body, *, agentctl, runner=None):
        calls.append({"target": target, "verdict": verdict, "body": body})
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
    return post


def _dispatch(agentctl, tmp_path, runs_root, monkeypatch, codex_bin, home, *,
             pr=42, repo="agent-platform", review_sha="a" * 40, task="review this"):
    """Drive `_run_session` the way `_spawn_dispatch` does for a `--pr`-pinned
    review-class dispatch: `review_sha`/`pr` already resolved, `run_dir`/`worktree`
    already allocated. Bypasses the worktree/profile machinery `test_spawn_sandbox.
    py`'s Claude helper bypasses too, for the same reason — this is a test of the
    launch and the verdict post, not of worktree allocation."""
    import telemetry as T

    monkeypatch.setattr(T, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(agentctl.CB, "codex_home", lambda: home)
    monkeypatch.setenv("PATH", f"{codex_bin.parent}:{os.environ.get('PATH', '')}")
    run_id = T.new_run_id()
    run_dir = T.create_run_dir(run_id)
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    args = types.SimpleNamespace(agent="reviewer", task=task, parent=None, dry_run=False,
                                 label=None, model=None, issue=None, reason=None)
    agent = {"body": "You are the reviewer.", "model_alias": MODEL, "source": "roster"}
    rc = agentctl._run_session(
        args=args, agent=agent, run_id=run_id, run_dir=run_dir, worktree=worktree,
        profile="p", repo=repo, started_at=T.now_iso(), user_prompt=task,
        review_sha=review_sha, pr=pr)
    return rc, run_id, run_dir


# --- the launch ------------------------------------------------------------------

def test_the_launch_is_read_only_with_the_allowlisted_env_and_the_real_run_id(
        agentctl, CP, CB, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("SOME_RANDOM_SECRET", "s3cret")
    codex_bin, launch_path = _stub_codex(
        tmp_path, last_message=json.dumps({"verdict": "APPROVE", "body": "Looks good."}))
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home)

    launch = json.loads(launch_path.read_text())
    argv = launch["argv"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == MODEL
    assert argv[argv.index("-C") + 1] == str(tmp_path / "wt")
    for flag in ("--add-dir", "--dangerously-bypass-hook-trust",
                 "--dangerously-bypass-approvals-and-sandbox", "--ignore-user-config"):
        assert flag not in argv
    env = launch["env"]
    assert env["AGENTCTL_RUN_ID"] == run_id  # the REAL id, not probe_env's synthetic one
    for leaked in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "SOME_RANDOM_SECRET"):
        assert leaked not in env, leaked
    # PR #821 round 3: the same git/gh credential-helper redirection the Claude
    # dispatch lane gets (dispatch_env.git_credential_guard_env, CQ·418) — a run
    # whose own `git credential fill` or `gh` would otherwise read the operator's
    # real ~/.gitconfig / ~/.config/gh / keychain-stored token finds neither.
    assert os.path.isdir(env["GH_CONFIG_DIR"])
    assert list(Path(env["GH_CONFIG_DIR"]).iterdir()) == []
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # This lane never mints a token: a Codex review has no network and posts
    # nothing itself, unlike the Claude dispatch lane's per-run installation token.
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert rc == 0
    assert len(calls) == 1


def test_the_prompt_carries_the_rendered_agent_body_and_the_brief(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    codex_bin, launch_path = _stub_codex(
        tmp_path, last_message=json.dumps({"verdict": "APPROVE", "body": "ok"}))
    monkeypatch.setattr(agentctl.RV, "post", _fake_post([]))
    _dispatch(agentctl, tmp_path, runs_root, monkeypatch, codex_bin, home,
             task="check the auth refactor")
    argv = json.loads(launch_path.read_text())["argv"]
    prompt = argv[-1]
    assert "You are the reviewer." in prompt
    assert "check the auth refactor" in prompt
    assert str(tmp_path / "wt") in prompt or "worktree" in prompt.lower()


# --- the verdict ------------------------------------------------------------------

def test_the_verdict_posts_dispatcher_side_pinned_to_the_runs_pr_repo_and_commit(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    codex_bin, _ = _stub_codex(
        tmp_path, last_message=json.dumps({"verdict": "APPROVE", "body": "Looks good."}))
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home, pr=42, repo="agent-platform",
                                    review_sha="b" * 40)

    assert rc == 0
    assert len(calls) == 1
    target = calls[0]["target"]
    assert (target.run_id, target.pr, target.repo, target.commit) == (
        run_id, "42", "convivy/agent-platform", "b" * 40)
    assert calls[0]["verdict"] == "APPROVE"
    assert calls[0]["body"] == "Looks good."
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "completed"


def test_a_verdict_naming_another_pr_is_refused_and_nothing_posts(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    codex_bin, _ = _stub_codex(
        tmp_path, last_message=json.dumps(
            {"verdict": "APPROVE", "body": "Looks good.", "pr": "999"}))
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home, pr=42)

    assert rc == 1
    assert calls == []  # target_for refused before `post` was ever called
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "errored"
    assert "the call names PR #999" in outcome["error_reason"]


def test_a_verdict_naming_another_commit_is_refused_and_nothing_posts(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    codex_bin, _ = _stub_codex(
        tmp_path, last_message=json.dumps(
            {"verdict": "APPROVE", "body": "Looks good.", "reviewed_commit": "c" * 40}))
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home, review_sha="b" * 40)

    assert rc == 1
    assert calls == []
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "errored"
    assert "the call names" in outcome["error_reason"]


def test_a_missing_verdict_errors_the_run_and_posts_nothing(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    """The model wrote prose, not a JSON verdict object, to its last message — the
    most likely real failure, standing in for "missing, unparsable or refused"."""
    _probe_passed(CP, home)
    codex_bin, _ = _stub_codex(tmp_path, last_message="I reviewed the diff; it looks fine.")
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home)

    assert rc == 1
    assert calls == []
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["exit_status"] == "errored"
    assert "not valid JSON" in outcome["error_reason"]


def test_no_output_last_message_falls_back_to_the_final_agent_message(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    """`--output-last-message` unhonoured (no file written at all): the verdict
    still comes from the last `agent_message` in the `--json` stream."""
    _probe_passed(CP, home)
    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "body": "Needs a test."})
    stream = (_TURN_COMPLETED
             + json.dumps({"type": "item.completed",
                          "item": {"type": "agent_message", "text": verdict_json}}) + "\n")
    codex_bin, _ = _stub_codex(tmp_path, last_message=None, stream=stream)
    calls = []
    monkeypatch.setattr(agentctl.RV, "post", _fake_post(calls))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home)

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["verdict"] == "REQUEST_CHANGES"
    assert calls[0]["body"] == "Needs a test."


# --- cost and the billing lane ----------------------------------------------------

def test_the_ledger_row_carries_the_codex_lane_and_a_codex_cost(
        agentctl, CP, home, tmp_path, runs_root, monkeypatch):
    _probe_passed(CP, home)
    codex_bin, _ = _stub_codex(
        tmp_path, last_message=json.dumps({"verdict": "APPROVE", "body": "ok"}))
    monkeypatch.setattr(agentctl.RV, "post", _fake_post([]))

    rc, run_id, run_dir = _dispatch(agentctl, tmp_path, runs_root, monkeypatch,
                                    codex_bin, home)

    assert rc == 0
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["billing_source"] == "codex_api"
    cost = json.loads((run_dir / "cost.json").read_text())
    assert cost["cost_source"] == "codex_turn_usage"
    # 1000 input @ $10/Mtok + 100 output @ $50/Mtok = 0.01 + 0.005
    assert cost["usd"] == pytest.approx(0.015)
    assert cost["tokens_in"] == 1000
    assert cost["tokens_out"] == 100


# --- the refusal gate (before any side effect) ------------------------------------

def _spawn_args(**kw):
    base = dict(agent="reviewer", task="t", on="agent-platform", profile=None,
               agents_dir=None, parent=None, pr=None, review_sha=None,
               model=None, reason=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_codex_slug_with_no_probe_pass_is_refused_before_any_side_effect(
        agentctl, CP, home, monkeypatch, capsys):
    """No `CP.record(...)` call in this test: the probe has never passed."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": MODEL})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must be refused before R.can_spawn")))
    assert agentctl.cmd_spawn(_spawn_args()) == 2
    assert "probe gate is closed" in capsys.readouterr().err


def test_a_probe_passed_reviewer_dispatch_is_still_refused_before_any_side_effect(
        agentctl, CP, home, monkeypatch, capsys):
    """PR #821 round 2 (security-reviewer finding): a reviewer dispatch is the one
    case a probe pass used to lift the gate for — see test_codex_probe.py's
    `test_a_probe_pass_no_longer_lifts_the_dispatch_refusal`. No confirmed mechanism
    confines what a Codex review's own `codex exec` reads (CODEX_READ_CONFINEMENT_
    REFUSAL), so it stays refused, pass or no pass, before any side effect
    (`R.can_spawn` never reached)."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    _probe_passed(CP, home)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": MODEL})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must be refused before R.can_spawn")))
    assert agentctl.cmd_spawn(_spawn_args(agent="reviewer")) == 2
    assert agentctl.CODEX_READ_CONFINEMENT_REFUSAL in capsys.readouterr().err


def test_a_codex_coder_dispatch_is_refused(agentctl, CP, home, monkeypatch, capsys):
    """As of PR #821 round 3, a probe-passed coder dispatch is refused at the earlier
    `codex_dispatch_refusal` gate (CODEX_READ_CONFINEMENT_REFUSAL — every Codex dispatch
    stays refused regardless of the probe) rather than at the review-agent-only check
    below it; `R.can_spawn` must never be reached either way."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    _probe_passed(CP, home)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": MODEL})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must be refused before R.can_spawn")))
    assert agentctl.cmd_spawn(_spawn_args(agent="coder")) == 2
    err = capsys.readouterr().err
    assert agentctl.CODEX_READ_CONFINEMENT_REFUSAL in err


# --- a Claude dispatch is unchanged ------------------------------------------------

def test_a_claude_dispatch_never_reaches_the_codex_branch(
        agentctl, tmp_path, runs_root, monkeypatch):
    """`is_codex_model` on an Anthropic slug is False, so `_run_session` falls
    through to the unchanged `claude -p` launch — `tests/test_spawn_sandbox.py`'s
    full suite is the byte-for-byte argv/env pin; this is the direct check that
    the new branch this change adds cannot divert a Claude dispatch into it."""
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", runs_root)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    argv_file = tmp_path / "argv.txt"
    stub = tmp_path / "claude-stub"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {argv_file}\n"
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"model\":\"x\"}'\n"
        "exit 0\n")
    stub.chmod(0o755)
    monkeypatch.setattr(agentctl, "CLAUDE_BIN", str(stub))
    monkeypatch.setattr(agentctl, "_preflight_python_worktree", lambda wt: None)
    monkeypatch.setattr(agentctl, "_finalize_run", lambda **kw: None)
    monkeypatch.setattr(agentctl.R, "reserve_slot", lambda entry: None)
    monkeypatch.setattr(agentctl.R, "attach_pid",
                        lambda run_id, pid, pid_started_at=None, **_kw: None)
    monkeypatch.setattr(agentctl.R, "remove_run", lambda rid: True)
    args = types.SimpleNamespace(agent="reviewer", task="t", parent=None, dry_run=False,
                                 label=None, model=None, review_sha=None)
    agentctl._run_session(
        args=args, agent={"body": "sys", "model_alias": "claude-sonnet-5"},
        run_id="run-not-codex", run_dir=run_dir, worktree=worktree, profile="p",
        repo="constance", started_at="2026-10-06T00:00:00Z", user_prompt="u")
    argv = argv_file.read_text().splitlines()
    assert argv[0] == "-p"
    assert "exec" not in argv  # the codex-exec launch shape never appears
