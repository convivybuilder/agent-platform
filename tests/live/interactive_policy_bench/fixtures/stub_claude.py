#!/usr/bin/env python3
"""stub_claude — a generic stand-in for the real `claude` binary, invoked ONLY
by `bench.launch_dry_run()`. It is not a simulated LLM: it deterministically
executes the fenced ```bash probes in probe_prompt.md (the SAME file a real
session reads as its prompt — see that file's own docstring), running each
one through the exact PreToolUse hook wire protocol Claude Code uses (a JSON
payload piped to the hook command's stdin, `hookSpecificOutput.
permissionDecision` read back from its stdout). That makes a dry run a
genuine mechanical trial of this bench's settings-file wiring and result
parsing. It is NOT a trial of the real `claude` CLI's own permission-mode
resolution — see interactive_policy_bench/README.md, "What the dry run
proves and doesn't" — so two things here are this stub's OWN documented
convention rather than a claim about real Claude Code:

- `resolved_permission_mode` is simply the LAST `--permission-mode` value on
  the command line. Whether the real CLI actually behaves this way is
  exactly arm B's open question (see bench.permission_mode_note).
- Hook invocation order is: the `BENCH_USER_HOOKS_SETTINGS` stand-in first,
  then each `--settings` file's hooks in the order they appeared on argv.
  The real Claude Code's own ordering across multiple registered hooks isn't
  asserted here; only "does ANY registered hook's deny stop the command" is.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

FENCE_RE = re.compile(r"```bash\n#\s*([A-Za-z0-9_-]+)\n(.*?)```", re.DOTALL)
SUBAGENT_COMMAND = "sqlite3 :memory: \"select 'PROBE-SUBAGENT-SQLITE' || '-RAN';\""


def _clean_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    if extra:
        env.update(extra)
    return env


def parse_flags(argv: list[str]) -> dict:
    prompt = argv[-1] if argv else ""
    body = argv[:-1] if argv else []
    settings_files: list[str] = []
    permission_modes: list[str] = []
    agent = None
    i = 0
    while i < len(body):
        tok = body[i]
        if tok == "--settings" and i + 1 < len(body):
            settings_files.append(body[i + 1])
            i += 2
            continue
        if tok == "--permission-mode" and i + 1 < len(body):
            permission_modes.append(body[i + 1])
            i += 2
            continue
        if tok == "--agent" and i + 1 < len(body):
            agent = body[i + 1]
            i += 2
            continue
        i += 1
    return {
        "settings_files": settings_files, "permission_modes": permission_modes,
        "agent": agent, "prompt": prompt,
    }


def load_hook_commands(settings_path: str) -> list[str]:
    try:
        data = json.loads(Path(settings_path).read_text())
    except Exception:
        return []
    commands = []
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        if entry.get("matcher", "") not in ("", "Bash"):
            continue
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and h.get("command"):
                commands.append(h["command"])
    return commands


def run_hook(hook_command: str, payload: dict, cwd: str) -> tuple[bool, str | None]:
    proc = subprocess.run(
        hook_command, shell=True, input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=_clean_env(),
    )
    stdout = proc.stdout.strip()
    if not stdout:
        return False, None
    try:
        data = json.loads(stdout)
    except Exception:
        return False, None
    out = data.get("hookSpecificOutput", {})
    if out.get("permissionDecision") == "deny":
        return True, out.get("permissionDecisionReason", "denied")
    return False, None


def run_step(label: str, command: str, hook_commands: list[str], cwd: str,
             transcript_path: str, agent_type: str | None,
             agent_id: str | None, lines: list[str]) -> None:
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": command}, "session_id": "stub-session",
        "cwd": cwd, "transcript_path": transcript_path,
    }
    if agent_type:
        payload["agent_type"] = agent_type
    if agent_id:
        payload["agent_id"] = agent_id

    lines.append(f"### STEP {label}")
    lines.append(f"command: {command}")
    denied, reason = False, None
    for hc in hook_commands:
        denied, reason = run_hook(hc, payload, cwd)
        if denied:
            break
    if denied:
        lines.append("decision: DENIED")
        lines.append(f"reason: {reason}")
        return
    lines.append("decision: ALLOWED")
    result = subprocess.run(command, shell=True, capture_output=True, text=True,
                             cwd=cwd, env=_clean_env())
    if result.stdout.strip():
        lines.append(f"stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        lines.append(f"stderr: {result.stderr.strip()}")


def main(argv: list[str]) -> int:
    flags = parse_flags(argv)
    transcript_path = os.environ.get("BENCH_TRANSCRIPT_PATH")
    if not transcript_path:
        print("stub_claude: BENCH_TRANSCRIPT_PATH must be set", file=sys.stderr)
        return 2
    cwd = os.getcwd()
    resolved_mode = flags["permission_modes"][-1] if flags["permission_modes"] else "bypassPermissions"

    hook_commands: list[str] = []
    user_hooks = os.environ.get("BENCH_USER_HOOKS_SETTINGS")
    if user_hooks:
        hook_commands += load_hook_commands(user_hooks)
    for sf in flags["settings_files"]:
        hook_commands += load_hook_commands(sf)

    lines = [
        f"resolved_permission_mode: {resolved_mode}",
        f"agent: {flags['agent']}",
        "",
    ]

    for label, raw_command in FENCE_RE.findall(flags["prompt"]):
        run_step(label, raw_command.strip(), hook_commands, cwd, transcript_path,
                  agent_type=flags["agent"], agent_id=None, lines=lines)

    # Step 12 of probe_prompt.md: a Task-tool subagent dispatch, simulated as
    # a second synthetic call with agent_id/agent_type set the way the hooks
    # reference documents for a dispatched subagent ("present only with
    # --agent or inside a subagent" — tools/rollover-trigger.py already
    # quotes this for its own agent_type check).
    run_step("PROBE-SUBAGENT", SUBAGENT_COMMAND, hook_commands, cwd, transcript_path,
              agent_type="general-purpose", agent_id="stub-subagent-1", lines=lines)

    Path(transcript_path).write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
