"""The Codex hook bridge: tools/codex-hook-shim.py, tools/codex_tool_shape.py,
lib/agent-platform/codex_bridge.py, and the dispatcher's refusal without them.

The operator ruled on 2026-09-23 that a Codex run is held to a Claude run's restrictions.
NO TEST HERE RUNS CODEX, and none reads or writes ~/.codex: every CODEX_HOME is a
tmp_path. The payloads are Codex-shaped by the wire schema the 0.153.2 binary
embeds (the envelope fields below) and by one recorded tool call (CODE_MODE_EXEC).
Whether a real `codex exec` sends these and honours the deny is unmeasured, and the
slice 2 conformance probe is what measures it.
"""
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SHIM = TOOLS / "codex-hook-shim.py"

# The tool call recorded in ~/.codex/sessions/2026/09/04 (rollout-...-01a06d2c...):
# in code mode the model calls a custom tool `exec` whose input is JavaScript that
# reaches the shell through tools.exec_command. The command is inside the string.
CODE_MODE_EXEC = ('const r = await tools.exec_command({cmd:"gh pr merge 12",'
                  '"workdir":"/tmp","yield_time_ms":10000}); text(r.output);\n')

needs_jq = pytest.mark.skipif(shutil.which("jq") is None,
                              reason="block-pr-merge.sh fails open without jq")


def payload(tool_name, tool_input, **extra):
    """A PreToolUse payload with the envelope the binary's wire schema requires."""
    return {"cwd": "/tmp", "hook_event_name": "PreToolUse", "model": "gpt-6-astra",
            "permission_mode": "default", "session_id": "s1", "tool_input": tool_input,
            "tool_name": tool_name, "tool_use_id": "call_1", "transcript_path": "/tmp/t",
            "turn_id": "turn_1", **extra}


def run_shim(call, env_extra=None):
    """(exit code, parsed stdout) of the real shim on `call`."""
    env = {k: v for k, v in os.environ.items() if k != "AGENTCTL_RUN_ID"}
    env.update(env_extra or {})
    raw = call if isinstance(call, str) else json.dumps(call)
    run = subprocess.run([sys.executable, str(SHIM)], input=raw, capture_output=True,
                         text=True, env=env, timeout=60)
    return run.returncode, (json.loads(run.stdout) if run.stdout.strip() else {})


def denial(out):
    spec = out.get("hookSpecificOutput", {})
    return spec.get("permissionDecision"), spec.get("permissionDecisionReason", "")


# --- the shim -----------------------------------------------------------------

@needs_jq
def test_shim_denies_a_pr_merge_in_codexs_shell_tool():
    code, out = run_shim(payload("exec_command", {"cmd": "gh pr merge 12 --squash",
                                                  "workdir": "/tmp"}))
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny")
    assert "ARCH-020" in reason, "the denial must come from block-pr-merge, not a jq fallback"


@needs_jq
def test_shim_unwraps_a_shell_argv_so_the_merge_stays_at_a_segment_start():
    """`bash -lc 'gh pr merge 1'` as an argv must reach the guard as the script:
    quoted into one string it starts with `bash`, and the guard anchors on the
    segment start."""
    _, out = run_shim(payload("shell", {"command": ["bash", "-lc", "gh pr merge 1"]}))
    assert denial(out)[0] == "deny"


@needs_jq
def test_shim_denies_a_push_to_main_only_from_a_dispatched_run():
    call = payload("exec_command", {"cmd": "git push origin main"})
    assert denial(run_shim(call, {"AGENTCTL_RUN_ID": "run-1"})[1])[0] == "deny"
    assert denial(run_shim(call)[1])[0] is None


@needs_jq
def test_shim_lets_an_ordinary_command_and_a_non_command_tool_through():
    assert run_shim(payload("exec_command", {"cmd": "ls -la"})) == (0, {})
    assert run_shim(payload("apply_patch", {"input": "*** Begin Patch"})) == (0, {})
    assert run_shim(payload("write_stdin", {"session_id": 3, "chars": ""})) == (0, {})


@needs_jq
def test_shim_denies_a_send_capable_command():
    code, out = run_shim(payload("exec_command", {"cmd": "sendmail someone@example.com"}))
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny")
    assert "outbound-guard" in reason


@needs_jq
def test_shim_denies_a_command_typed_into_a_running_session():
    """A shell an earlier harmless command started can be sent anything."""
    _, out = run_shim(payload("write_stdin", {"session_id": 3, "chars": "gh pr merge 5\n"}))
    assert denial(out)[0] == "deny"


def test_shim_denies_code_mode_exec_naming_the_shape():
    """The recorded code-mode call: the command is inside a JavaScript string, and a
    hook sees the call rather than what it runs. Denied, and the reason says why."""
    code, out = run_shim(payload("exec", CODE_MODE_EXEC))
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny")
    assert "'exec'" in reason and "unrecognised shape" in reason
    assert f"a string of {len(CODE_MODE_EXEC)} characters" in reason


def test_shim_denies_a_command_capable_tool_whose_input_it_cannot_read():
    for call in (payload("exec", {"input": CODE_MODE_EXEC}),
                 payload("shell_command", {"unexpected": 1}),
                 payload("some_future_tool", {"argv": ["rm", "-rf", "x"], "extra": None}),
                 payload("run_terminal", ["not", "a", "dict"])):
        decision, reason = denial(run_shim(call)[1])
        assert decision == "deny", call["tool_name"]
        assert call["tool_name"] in reason


def test_shim_denies_a_payload_it_cannot_parse():
    for raw in ("not json", "[]", json.dumps({"tool_input": {"cmd": "ls"}})):
        assert denial(run_shim(raw)[1])[0] == "deny", raw


INERT_TOOLS = ["apply_patch", "update_plan", "view_image", "web_search", "request_user_input",
               "read_file", "list_dir", "grep_files", "Read", "Glob", "Grep"]


def test_shim_denies_an_unknown_tool_it_cannot_read_a_command_from():
    """The tool catalog is open. `js_repl` takes a string and matches no name pattern a
    blocklist could have anticipated; it must land on a denial rather than on `other`."""
    for name, given in (("js_repl", "require('child_process').execSync('gh pr merge 1')"),
                        ("python", "import os; os.system('x')"),
                        ("node", {"source": "x"}),
                        ("made_up_tool_7", "anything"),
                        ("made_up_tool_7", {}),
                        ("made_up_tool_7", None)):
        decision, reason = denial(run_shim(payload(name, given))[1])
        assert decision == "deny", (name, given)
        assert f"'{name}'" in reason and "not known to be inert" in reason


@pytest.mark.parametrize("name", INERT_TOOLS)
def test_shim_allows_each_inert_tool(name):
    assert run_shim(payload(name, {"input": "x"})) == (0, {})


def test_an_unknown_tool_that_carries_a_readable_command_is_judged_as_one():
    _, out = run_shim(payload("brand_new_shell", {"command": "sendmail someone@example.com"}))
    assert denial(out)[0] == "deny"


# --- outbound-guard on a Codex shape, directly ---------------------------------

def _load_guard():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("outbound_guard_codex", str(TOOLS / "outbound-guard.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _guard_exit(monkeypatch, tmp_path, call):
    import io
    mod = _load_guard()
    monkeypatch.setattr(mod, "LOG", str(tmp_path / "outbound-guard.log"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call)))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    return exc.value.code


def test_outbound_guard_blocks_a_send_in_codexs_shell_tool(monkeypatch, tmp_path):
    send = payload("exec_command", {"cmd": "sendmail someone@example.com < body.txt"})
    assert _guard_exit(monkeypatch, tmp_path, send) == 2


def test_outbound_guard_reads_the_argv_and_the_workdir_of_a_shell_call(monkeypatch, tmp_path):
    argv = payload("shell", {"command": ["bash", "-lc", "msmtp someone@example.com"]})
    assert _guard_exit(monkeypatch, tmp_path, argv) == 2


def test_outbound_guard_blocks_a_command_capable_shape_it_cannot_read(monkeypatch, tmp_path):
    assert _guard_exit(monkeypatch, tmp_path, payload("exec", CODE_MODE_EXEC)) == 2


def test_outbound_guard_still_passes_a_harmless_codex_command(monkeypatch, tmp_path):
    assert _guard_exit(monkeypatch, tmp_path, payload("exec_command", {"cmd": "ls"})) == 0
    assert _guard_exit(monkeypatch, tmp_path, payload("apply_patch", {"input": "x"})) == 0


# --- the bridge: install and verify --------------------------------------------

def _trust(home, hooks_json=None, **entry):
    key = f"{hooks_json or home / 'hooks.json'}:pre_tool_use:0:0"
    fields = {"trusted_hash": "sha256:abc", **entry}
    body = "\n".join(f"{k} = {json.dumps(v)}" for k, v in fields.items())
    (home / "config.toml").write_text(f'[hooks.state."{key}"]\n{body}\n')


@pytest.fixture
def bridge():
    import codex_bridge
    return codex_bridge


@pytest.fixture(autouse=True)
def _guards_from_this_checkout(bridge, monkeypatch):
    """The default tools dir is the operator's checkout, which a test must not read."""
    monkeypatch.setattr(bridge, "default_tools_dir", lambda: TOOLS)


@pytest.fixture
def tools_copy(tmp_path, bridge):
    """The guards and shim copied out of the repo, so a test can edit them.

    The shim also imports `safe_path` from a sibling `lib/agent-platform/`
    (relative to its own location, the same layout the real repo and the
    deployed farm both have), so that directory is copied alongside `tools/`
    here too — a subprocess run of the copied shim (`_run_copied_shim`) gets
    no help from this test session's own `sys.path`, unlike `_load_shim`'s
    in-process load."""
    dest = tmp_path / "tools"
    dest.mkdir()
    for name in (bridge.SHIM, bridge.SHAPE, *bridge.GUARDS):
        shutil.copy(TOOLS / name, dest / name)
    lib_dest = tmp_path / "lib" / "agent-platform"
    lib_dest.mkdir(parents=True)
    shutil.copy(ROOT / "lib" / "agent-platform" / "safe_path.py", lib_dest / "safe_path.py")
    return dest


def test_install_registers_the_shim_with_no_matcher(bridge, tmp_path):
    home = tmp_path / "codex"
    bridge.install(home, TOOLS)
    doc = json.loads((home / "hooks.json").read_text())
    (entry,) = doc["hooks"]["PreToolUse"]
    assert "matcher" not in entry
    assert entry["hooks"][0]["command"].endswith(str(SHIM))


def test_install_refuses_to_replace_a_hooks_json_that_is_not_its_own(bridge, tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "hooks.json").write_text('{"hooks": {"Stop": []}}')
    with pytest.raises(FileExistsError):
        bridge.install(home, TOOLS)
    assert json.loads((home / "hooks.json").read_text()) == {"hooks": {"Stop": []}}


def test_a_trusted_untouched_install_verifies_clean(bridge, tmp_path):
    home = tmp_path / "codex"
    bridge.install(home, TOOLS)
    _trust(home)
    assert bridge.verify(home, TOOLS) == []


def test_verify_reports_an_empty_codex_home_as_absent(bridge, tmp_path):
    (problem,) = bridge.verify(tmp_path / "codex", TOOLS)
    assert "absent" in problem


def test_verify_reports_a_fresh_install_as_untrusted(bridge, tmp_path):
    home = tmp_path / "codex"
    bridge.install(home, TOOLS)
    (problem,) = bridge.verify(home, TOOLS)
    assert "untrusted" in problem


def test_verify_does_not_read_an_unrelated_or_disabled_hook_as_trust(bridge, tmp_path):
    home = tmp_path / "codex"
    bridge.install(home, TOOLS)
    _trust(home, hooks_json=tmp_path / "other" / "hooks.json")
    assert any("untrusted" in p for p in bridge.verify(home, TOOLS))
    _trust(home, enabled=False)
    assert any("untrusted" in p for p in bridge.verify(home, TOOLS))
    _trust(home, trusted_hash="")
    assert any("untrusted" in p for p in bridge.verify(home, TOOLS))


def test_verify_reports_a_guard_edited_after_install_as_modified(bridge, tmp_path, tools_copy):
    home = tmp_path / "codex"
    bridge.install(home, tools_copy)
    _trust(home)
    assert bridge.verify(home, tools_copy) == []
    guard = tools_copy / "block-pr-merge.sh"
    guard.write_text(guard.read_text() + "\n# edited\n")
    (problem,) = bridge.verify(home, tools_copy)
    assert "modified" in problem and "block-pr-merge.sh" in problem


def test_verify_reports_an_edited_hooks_json_as_modified_or_absent(bridge, tmp_path):
    home = tmp_path / "codex"
    bridge.install(home, TOOLS)
    _trust(home)
    doc = json.loads((home / "hooks.json").read_text())
    doc["hooks"]["PreToolUse"][0]["matcher"] = "Bash"  # the guess the bridge exists to avoid
    (home / "hooks.json").write_text(json.dumps(doc))
    problems = bridge.verify(home, TOOLS)
    assert any("absent" in p for p in problems) and any("modified" in p for p in problems)


def test_verify_reports_a_missing_guard_as_absent(bridge, tmp_path, tools_copy):
    home = tmp_path / "codex"
    bridge.install(home, tools_copy)
    _trust(home)
    (tools_copy / "outbound-guard.py").unlink()
    (problem,) = bridge.verify(home, tools_copy)
    assert "absent" in problem and "outbound-guard.py" in problem


# --- the dispatcher ------------------------------------------------------------

CODEX_SLUGS = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
               "gpt-reserve", "codex-auto-review"]


def test_fallback_model_for_a_codex_model_is_none(agentctl):
    """It was the deepseek alias: a Codex id is not gateway-prefixed, so it fell
    into the Anthropic-direct branch and a dead Codex run respawned on a metered
    gateway model in the same worktree."""
    for slug in CODEX_SLUGS:
        assert agentctl.fallback_model_for(slug) is None, slug


def test_is_codex_model_does_not_claim_gateway_or_claude_ids(agentctl):
    assert all(agentctl.is_codex_model(s) for s in CODEX_SLUGS)
    for other in ("openai/gpt-5", "claude-opus-5", "deepseek/deepseek-v4-flash", "", "gpt5",
                  "gpt-5\nCo-Authored-By: x"):
        assert not agentctl.is_codex_model(other), other


def test_a_dispatch_is_refused_when_the_guards_are_absent(agentctl, tmp_path):
    refusal = agentctl.codex_dispatch_refusal("gpt-6-astra", tmp_path / "codex")
    assert "absent" in refusal and "do not bind" in refusal


def test_a_dispatch_is_refused_when_the_guards_are_untrusted(agentctl, tmp_path):
    home = tmp_path / "codex"
    agentctl.CB.install(home)
    assert "untrusted" in agentctl.codex_dispatch_refusal("gpt-6-astra", home)


def test_a_dispatch_is_refused_when_the_guards_are_modified(agentctl, tmp_path):
    home = tmp_path / "codex"
    agentctl.CB.install(home)
    _trust(home)
    assert agentctl.codex_precondition_refusal("gpt-6-astra", home) is None
    (home / "hooks.json").write_text((home / "hooks.json").read_text() + " ")
    assert "modified" in agentctl.codex_dispatch_refusal("gpt-6-astra", home)


def test_a_dispatch_with_no_model_pinned_is_refused(agentctl, tmp_path):
    home = tmp_path / "codex"
    agentctl.CB.install(home)
    _trust(home)
    for unpinned in (None, "", "   "):
        assert "-m" in agentctl.codex_dispatch_refusal(unpinned, home)
    assert "not a model the Codex CLI serves" in agentctl.codex_dispatch_refusal("claude-opus-5", home)


def test_cmd_spawn_refuses_a_codex_roster_model_before_any_side_effect(agentctl, monkeypatch,
                                                                       tmp_path):
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": "gpt-6-astra"})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("a Codex dispatch without guards must be refused before R.can_spawn")))
    args = types.SimpleNamespace(agent="reviewer", task="t", on="agent-platform", profile=None,
                                 agents_dir=None, parent=None, pr=None, review_sha=None,
                                 model=None, reason=None)
    assert agentctl.cmd_spawn(args) == 2


# --- the shim fails closed ------------------------------------------------------

def _load_shim(monkeypatch, tools):
    """The shim copied into `tools`, loaded in-process so a test can patch its module
    state. HERE resolves from the file, so its guards are the ones beside the copy."""
    import importlib.machinery
    import importlib.util
    monkeypatch.setattr(sys, "path", list(sys.path))
    loader = importlib.machinery.SourceFileLoader(f"shim_{abs(hash(str(tools)))}",
                                                  str(tools / "codex-hook-shim.py"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    monkeypatch.setattr(mod.SP, "safe_which", lambda name: "/usr/bin/jq")  # jq is not under test
    mod.exit_codes = []

    def fake_exit(code):  # deny() ends in os._exit, which would end pytest itself
        mod.exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(mod.os, "_exit", fake_exit)
    return mod


def _shim_answer(mod, monkeypatch, capsys, call):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call)))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    out = capsys.readouterr().out
    return exc.value.code, (json.loads(out) if out.strip() else {})


LS = payload("exec_command", {"cmd": "ls"})


def _fake_guard(tools, name, body):
    path = tools / name
    path.write_text(body)
    path.chmod(0o755)


def _fake_guards(tools, block_pr="exit 0", outbound="sys.exit(0)"):
    """Replace both guards with stubs, so a test controls exactly how each one answers."""
    _fake_guard(tools, "block-pr-merge.sh", f"#!/bin/bash\n{block_pr}\n")
    _fake_guard(tools, "outbound-guard.py", f"#!/usr/bin/python3\nimport sys\n{outbound}\n")


def test_the_fake_guards_let_a_command_through(tools_copy, monkeypatch, capsys):
    """The control for the denials below: with both stubs answering yes, the same call
    is allowed, so a denial there comes from the stub's behaviour and nothing else."""
    _fake_guards(tools_copy)
    mod = _load_shim(monkeypatch, tools_copy)
    assert _shim_answer(mod, monkeypatch, capsys, LS) == (0, {})


@pytest.mark.parametrize("guard", ["block-pr-merge.sh", "outbound-guard.py"])
def test_a_guard_that_exits_1_is_a_denial(tools_copy, monkeypatch, capsys, guard):
    _fake_guards(tools_copy, block_pr="exit 1" if guard.endswith(".sh") else "exit 0",
                 outbound="sys.exit(1)" if guard.endswith(".py") else "sys.exit(0)")
    mod = _load_shim(monkeypatch, tools_copy)
    code, out = _shim_answer(mod, monkeypatch, capsys, LS)
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny")
    assert f"guard {guard} failed with exit 1" in reason


def test_a_guard_that_times_out_is_a_denial(tools_copy, monkeypatch, capsys):
    _fake_guards(tools_copy, block_pr="exec sleep 30")
    mod = _load_shim(monkeypatch, tools_copy)
    monkeypatch.setattr(mod, "TIMEOUT_S", 0.5)
    decision, reason = denial(_shim_answer(mod, monkeypatch, capsys, LS)[1])
    assert decision == "deny" and "did not answer (TimeoutExpired)" in reason


@pytest.mark.parametrize("guard", ["block-pr-merge.sh", "outbound-guard.py"])
def test_a_missing_guard_file_is_a_denial(tools_copy, monkeypatch, capsys, guard):
    _fake_guards(tools_copy)
    (tools_copy / guard).unlink()
    mod = _load_shim(monkeypatch, tools_copy)
    decision, reason = denial(_shim_answer(mod, monkeypatch, capsys, LS)[1])
    assert decision == "deny" and f"guard {guard} is missing" in reason


def test_unreadable_guard_stdout_is_a_denial(tools_copy, monkeypatch, capsys):
    _fake_guards(tools_copy, block_pr="echo 'this is not json'")
    mod = _load_shim(monkeypatch, tools_copy)
    decision, reason = denial(_shim_answer(mod, monkeypatch, capsys, LS)[1])
    assert decision == "deny" and "output the bridge cannot read" in reason


def test_a_guard_stdout_that_is_not_an_object_is_a_denial(tools_copy, monkeypatch, capsys):
    """Pins verdict()'s own branch: the catch-all's reason reads "the bridge crashed"."""
    _fake_guards(tools_copy, block_pr="echo '[1]'")
    mod = _load_shim(monkeypatch, tools_copy)
    decision, reason = denial(_shim_answer(mod, monkeypatch, capsys, LS)[1])
    assert decision == "deny" and "output the bridge cannot read" in reason
    assert "crashed" not in reason


def test_a_guard_deny_on_stdout_is_a_denial(tools_copy, monkeypatch, capsys):
    """block-pr-merge answers with a hookSpecificOutput deny and exit 0."""
    reply = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
                                               "permissionDecisionReason": "no merges"}})
    _fake_guards(tools_copy, block_pr=f"echo '{reply}'")
    mod = _load_shim(monkeypatch, tools_copy)
    assert denial(_shim_answer(mod, monkeypatch, capsys, LS)[1]) == ("deny", "Blocked by the "
                                                                   "Codex hook bridge: no merges")


def test_jq_absent_is_a_denial(tools_copy, monkeypatch, capsys):
    _fake_guards(tools_copy)
    mod = _load_shim(monkeypatch, tools_copy)
    monkeypatch.setattr(mod.SP, "safe_which", lambda name: None)
    decision, reason = denial(_shim_answer(mod, monkeypatch, capsys, LS)[1])
    assert decision == "deny" and "jq is not installed" in reason


def _run_copied_shim(tools, call):
    run = subprocess.run([sys.executable, str(tools / "codex-hook-shim.py")], input=json.dumps(call),
                         capture_output=True, text=True, timeout=60)
    return run.returncode, (json.loads(run.stdout) if run.stdout.strip() else {}), run.stderr


def test_a_failed_import_of_the_shape_module_is_a_denial(tools_copy):
    """Uncaught, this is a traceback and exit 1, which Claude's hook contract reads as
    non-blocking."""
    (tools_copy / "codex_tool_shape.py").write_text("raise ImportError('boom')\n")
    code, out, _ = _run_copied_shim(tools_copy, LS)
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny") and "the bridge crashed" in reason


def test_a_classify_that_raises_is_a_denial(tools_copy):
    (tools_copy / "codex_tool_shape.py").write_text(
        "def classify(call):\n    raise RuntimeError('boom')\n")
    code, out, _ = _run_copied_shim(tools_copy, LS)
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny") and "RuntimeError" in reason


def test_a_guard_whose_stdout_is_not_utf8_is_a_denial(tools_copy):
    """subprocess.run(text=True) raises UnicodeDecodeError, a ValueError that verdict()
    does not catch; only the shim's own catch-all stands between it and exit 1."""
    _fake_guards(tools_copy, block_pr=r"printf '\377\376'")
    code, out, _ = _run_copied_shim(tools_copy, LS)
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny") and "UnicodeDecodeError" in reason


def test_the_refusal_names_the_command_that_installs_the_guards(bridge, tmp_path):
    (absent,) = bridge.verify(tmp_path / "codex", TOOLS)
    assert "codex_bridge" in absent and "b.install(b.codex_home())" in absent
    home = tmp_path / "codex2"
    bridge.install(home, TOOLS)
    (home / bridge.MANIFEST).unlink()
    _trust(home)
    (unverified,) = bridge.verify(home, TOOLS)
    assert "b.install(b.codex_home())" in unverified


def test_the_install_command_in_the_refusal_runs(bridge, tmp_path):
    """Run it, into a tmp CODEX_HOME, rather than trust that it reads right."""
    (absent,) = bridge.verify(tmp_path / "codex", TOOLS)
    cmd = absent.split("install them with: ", 1)[1].split(", then review", 1)[0]
    home = tmp_path / "fresh"
    run = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60,
                         env={**os.environ, "CODEX_HOME": str(home)})
    assert run.returncode == 0, run.stderr
    assert (home / "hooks.json").is_file() and (home / bridge.MANIFEST).is_file()


def _override_args(types_mod=types):
    return types_mod.SimpleNamespace(agent="reviewer", task="t", on="agent-platform", profile=None,
                                     agents_dir=None, parent=None, pr=None, review_sha=None,
                                     model="gpt-6-astra", reason="test")


def _claude_roster_agent(agentctl, monkeypatch, tmp_path):
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda name, repo=None, agents_dir=None: {"model_alias": "sonnet"})
    monkeypatch.setattr(agentctl.R, "can_spawn", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("a Codex override must be refused before R.can_spawn")))


def test_cmd_spawn_refuses_a_codex_model_override_on_a_claude_roster_agent(agentctl, monkeypatch,
                                                                          tmp_path, capsys):
    """Today the override preflight refuses it first (the platform serves no Codex model
    through it); the next test isolates this slice's gate for when that preflight passes."""
    _claude_roster_agent(agentctl, monkeypatch, tmp_path)
    assert agentctl.cmd_spawn(_override_args()) == 2
    assert "gpt-6-astra" in capsys.readouterr().err


def test_the_codex_gate_refuses_a_model_override_the_preflight_let_through(agentctl, monkeypatch,
                                                                         tmp_path, capsys):
    """The gate keys on `args.model or the roster alias`. Keyed on the alias alone, a
    Claude roster agent with `--model gpt-...` would start a Codex run with no guards."""
    _claude_roster_agent(agentctl, monkeypatch, tmp_path)
    monkeypatch.setattr(agentctl, "_spawn_override_preflight", lambda **kw: None)
    assert agentctl.cmd_spawn(_override_args()) == 2
    err = capsys.readouterr().err
    assert "refusing to dispatch on Codex model gpt-6-astra" in err and "do not bind" in err


# --- a denial also exits 2 ------------------------------------------------------

def test_a_denial_exits_2_with_the_reason_on_stderr(tools_copy):
    """Claude's hook contract blocks on exit 2 and reads stderr; whether `codex exec`
    honours the stdout JSON is unmeasured, so the status must carry the block."""
    _fake_guards(tools_copy, block_pr="exit 1")
    code, out, err = _run_copied_shim(tools_copy, LS)
    assert code == 2 and denial(out)[0] == "deny"
    assert "guard block-pr-merge.sh failed with exit 1" in err


def test_a_denial_still_exits_2_when_the_json_cannot_be_written(tools_copy, monkeypatch, capsys):
    """If the stdout write raised, main()'s catch-all would retry into the same failure
    and the process would exit 1, which the contract reads as non-blocking."""
    import io
    _fake_guards(tools_copy, block_pr="exit 1")
    mod = _load_shim(monkeypatch, tools_copy)
    real_print = print

    def print_or_fail(*args, file=None, **kw):
        if file is None:
            raise BrokenPipeError("stdout is closed")
        real_print(*args, file=file, **kw)

    monkeypatch.setattr("builtins.print", print_or_fail)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(LS)))
    with pytest.raises(SystemExit):
        mod.main()
    assert mod.exit_codes == [2], "deny() must end in os._exit(2): a closed pipe's shutdown flush cannot change it"
    assert "failed with exit 1" in capsys.readouterr().err


# --- every command key is judged ------------------------------------------------

def test_two_command_keys_that_disagree_are_a_denial():
    """Codex runs whichever key its schema reads. Judging the first one (`true`) while
    the other (`gh pr merge`) runs is a bypass."""
    for tool_input in ({"cmd": "gh pr merge 1", "command": "true"},
                       {"command": "true", "cmd": "gh pr merge 1"},
                       {"script": "true", "command": ["bash", "-lc", "gh pr merge 1"]}):
        code, out = run_shim(payload("exec_command", tool_input))
        decision, reason = denial(out)
        assert (code, decision) == (2, "deny"), tool_input
        assert "different commands" in reason


def test_a_nested_command_that_disagrees_with_the_top_level_one_is_a_denial():
    code, out = run_shim(payload("local_shell", {"command": "true",
                                                 "action": {"command": ["gh", "pr", "merge", "1"]}}))
    decision, reason = denial(out)
    assert (code, decision) == (2, "deny") and "different commands" in reason


def test_a_disagreement_on_an_unknown_tool_is_a_denial():
    code, out = run_shim(payload("brand_new_shell", {"cmd": "gh pr merge 1", "command": "true"}))
    assert (code, denial(out)[0]) == (2, "deny")


def test_identical_command_keys_are_judged_as_one(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    import codex_tool_shape
    one = codex_tool_shape.classify(payload("exec_command", {"cmd": "ls"}))
    two = codex_tool_shape.classify(payload("exec_command", {"cmd": "ls", "command": "ls",
                                                             "action": {"script": "ls"}}))
    assert one == two == ("shell", "ls", "/tmp", None)
    # and the merge is still caught when both keys agree on it
    code, out = run_shim(payload("exec_command", {"cmd": "gh pr merge 1", "command": "gh pr merge 1"}))
    assert (code, denial(out)[0]) == (2, "deny")


def test_outbound_guard_blocks_disagreeing_command_keys(monkeypatch, tmp_path):
    call = payload("exec_command", {"cmd": "sendmail someone@example.com", "command": "true"})
    assert _guard_exit(monkeypatch, tmp_path, call) == 2


# --- outbound-guard's own non-Bash branch fails closed --------------------------

@pytest.mark.parametrize("module", [
    "raise ImportError('boom')\n",
    "def classify(call):\n    raise RuntimeError('boom')\n",
])
def test_outbound_guard_blocks_a_non_bash_call_when_the_shape_module_fails(tools_copy, module):
    (tools_copy / "codex_tool_shape.py").write_text(module)
    run = subprocess.run([sys.executable, str(tools_copy / "outbound-guard.py")],
                         input=json.dumps(LS), capture_output=True, text=True, timeout=60)
    assert run.returncode == 2 and "could not classify the tool call" in run.stderr


# --- the guard never reads narrower than the executor runs -----------------------

MERGE = "gh pr merge 1"


def _shape(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    import codex_tool_shape
    return codex_tool_shape


HIDING_ARGVS = [
    ["bash", "-l", "-c", MERGE],
    ["bash", "--login", "-c", MERGE],
    ["env", "bash", "-c", MERGE],
    ["sudo", "bash", "-c", MERGE],
    ["env", "-i", "bash", "-c", MERGE],
    ["sudo", "-u", "root", "bash", "-c", MERGE],
    ["bash", "-c", "$1", "x", MERGE],
    ["bash", "-c", MERGE, "x"],
    ["bash", "-o", "pipefail", "-c", MERGE],
    ["bash", "-c", "-c", MERGE],
    ["bash", "--command", MERGE],
    ["/bin/bash", "-c", "$1", "x", MERGE],
    ["nohup", "zsh", "-lc", "$0", MERGE],
    ["fish", "-c", "$argv", MERGE],
    ["timeout", "5", "bash", "-c", MERGE],
    ["xargs", "bash", "-c", MERGE],
    ["exec", "bash", "-c", MERGE],
    ["nice", "-n", "5", "bash", "-c", MERGE],
    ["doas", "bash", "-c", MERGE],
    ["busybox", "sh", "-c", MERGE],
    ["tcsh", "-c", MERGE],
    ["bash-5.2", "-c", "$1", "x", MERGE],
    ["timeout", "5", "bash-5.2", "-c", MERGE],
    ["timeout", "5", "rbash", "-c", MERGE],
]


@needs_jq
@pytest.mark.parametrize("argv", HIDING_ARGVS, ids=lambda a: " ".join(a))
def test_a_shell_argv_that_hides_a_merge_is_denied_end_to_end(argv):
    code, out = run_shim(payload("shell", {"command": argv}))
    assert (code, denial(out)[0]) == (2, "deny"), argv


@pytest.mark.parametrize("argv", HIDING_ARGVS, ids=lambda a: " ".join(a))
def test_a_shell_argv_is_unwrapped_and_judged_or_refused(monkeypatch, argv):
    """Never the quoted join, under which `gh pr merge` is not at a segment start."""
    shape = _shape(monkeypatch)
    kind, command, _, why = shape.classify(payload("shell", {"command": argv}))
    if kind == "shell":
        assert command == MERGE, argv
    else:
        assert kind == "unnormalizable" and why, argv


@pytest.mark.parametrize("argv", [
    ["bash", "-l", "-c", MERGE],
    ["bash", "--login", "-c", MERGE],
    ["env", "bash", "-c", MERGE],
    ["env", "A=1", "bash", "-c", MERGE],
    ["sudo", "bash", "-c", MERGE],
    ["/bin/bash", "-c", MERGE],
    ["nohup", "time", "command", "sh", "-ic", MERGE],
    ["bash", "--command", MERGE],
])
def test_an_exactly_readable_shell_argv_is_judged_as_its_script(monkeypatch, argv):
    shape = _shape(monkeypatch)
    assert shape.classify(payload("shell", {"command": argv}))[:2] == ("shell", MERGE)


@pytest.mark.parametrize("argv,expected", [
    (["tcsh", "-c", MERGE], MERGE),
    (["bash-5.2", "-c", MERGE], MERGE),
    (["timeout", "5", "ls"], "timeout 5 ls"),
    (["xargs", "grep", "-r", "foo"], "xargs grep -r foo"),
])
def test_new_shell_names_are_unwrapped_and_non_shell_wrappers_are_not_over_denied(
        monkeypatch, argv, expected):
    shape = _shape(monkeypatch)
    assert shape.classify(payload("shell", {"command": argv}))[:2] == ("shell", expected)


def test_a_shell_with_no_c_is_judged_by_its_joined_form(monkeypatch):
    shape = _shape(monkeypatch)
    assert shape.classify(payload("shell", {"command": ["bash", "script.sh"]}))[:2] == (
        "shell", "bash script.sh")
    assert shape.classify(payload("shell", {"command": ["bash"]}))[:2] == ("shell", "bash")


def test_an_exact_bash_c_ls_is_still_allowed(monkeypatch):
    shape = _shape(monkeypatch)
    assert shape.classify(payload("shell", {"command": ["bash", "-c", "ls"]}))[:2] == ("shell", "ls")
    code, out = run_shim(payload("shell", {"command": ["bash", "-c", "ls"]}))
    assert (code, denial(out)[0]) == (0, None)


def test_a_wrapped_program_that_is_not_a_shell_is_judged_without_its_wrapper(monkeypatch):
    shape = _shape(monkeypatch)
    assert shape.classify(payload("shell", {"command": ["env", "A=1", "gh", "pr", "merge", "1"]}))[:2] == (
        "shell", "gh pr merge 1")


# --- the shim and the bridge name the same guards -------------------------------

def test_the_shims_guards_and_the_bridges_guards_are_the_same_set(bridge):
    """The shim runs the guards named in its GUARDS tuple, and the bridge's GUARDS feeds
    the install manifest. Nothing else ties the two lists together: round 1 of the pkill
    guard left the bridge entry out. The shim is read with ast, so no shim code runs."""
    import ast
    tree = ast.parse((TOOLS / "codex-hook-shim.py").read_text())
    shim_tuple = next(node.value for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(getattr(t, "id", None) == "GUARDS" for t in node.targets))
    shim_names = {name for name, _interpreter in ast.literal_eval(shim_tuple)}
    assert shim_names == set(bridge.GUARDS)
