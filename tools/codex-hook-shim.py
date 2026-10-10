#!/usr/bin/python3
"""Codex PreToolUse bridge: run our two Claude guards against a Codex tool call.

The operator ruled on 2026-09-23 that a Codex run is held to the same restrictions as a
Claude run. The guards that carry those restrictions are block-pr-merge.sh (no PR
merges, no push to main from a dispatched run) and outbound-guard.py (nothing
sends to anyone but the operator). Both read Claude's payload: tool_name "Bash" and
tool_input.command. Codex's payload has the same envelope and a different tool.

Registered in Codex's hooks.json with NO matcher (see lib/agent-platform/
codex_bridge.py), so it sees every tool call. A matcher is a guess about a tool
name that varies per model and per catalog release, and a wrong guess is
invisible. This script decides inside instead:

  shell command      rewritten to tool_name "Bash" + tool_input.command, then
                     both guards run on it unchanged
  connector (mcp__)  handed to outbound-guard as it arrived
  can run a command  and cannot be read: DENIED, naming the shape
  anything else      allowed only if named as inert (codex_tool_shape.INERT_TOOLS)

A guard's verdict is read the way Claude reads it: exit 2 blocks (outbound-guard),
and a hookSpecificOutput deny on stdout blocks (block-pr-merge). A guard that
times out or crashes with any other exit code is a denial too, since a guard that
did not answer is not a guard that said yes. The shim's own crash is a denial too. That is the reverse of
block-pr-merge's own fail-open on a missing jq, so the shim also refuses to run
when jq is absent.

The deny goes back as Codex's PreToolUse output, which the binary's wire schema
gives as hookSpecificOutput.permissionDecision "deny" with a reason, and also as
the reason on stderr with exit 2. Whether `codex exec` honours the JSON is
unmeasured; the conformance probe (slice 2) is what settles that.

When AGENTCTL_CODEX_DENIAL_LOG names a file, every denial also appends one JSON line to
it: the command from the tool payload as a JSON string (so a newline in it is escaped),
the reason, and a UTC timestamp. Only the conformance probe sets the variable, and reads
the file as its evidence that a hook denied (lib/agent-platform/codex_probe.py). With the
variable unset the shim writes nothing. The write is best-effort and never changes the
decision: a failed write leaves no record, which the probe reads as not denied.
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lib", "agent-platform"))

import safe_path as SP  # noqa: E402

GUARDS = (
    ("block-pr-merge.sh", ["/bin/bash"]),
    ("block-pkill-option-order.sh", ["/bin/bash"]),
    ("outbound-guard.py", ["/usr/bin/python3"]),
)
TIMEOUT_S = 10
DENIAL_LOG_ENV = "AGENTCTL_CODEX_DENIAL_LOG"
_COMMAND = None  # the shell command of the call being judged, for the denial record


def record_denial(reason):
    """Append this denial to the probe's log, if one is named. Never raises."""
    try:
        path = os.environ.get(DENIAL_LOG_ENV)
        if not path:
            return
        line = json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "command": _COMMAND, "reason": reason}) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception:
        pass


def deny(reason):
    """Block the call both ways: the JSON deny on stdout, and the reason on stderr with
    exit 2, the status Claude's hook contract blocks on. Whether `codex exec` honours the
    JSON is unmeasured, so the exit status must not depend on it. Neither write may raise,
    or main()'s catch-all would retry into the same failure and the process would exit 1."""
    message = f"Blocked by the Codex hook bridge: {reason}"
    record_denial(reason)
    try:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }}))
        sys.stdout.flush()
    except Exception:
        pass
    try:
        print(message, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    # os._exit, not sys.exit: a closed pipe makes the interpreter's shutdown flush fail,
    # and that changes the exit status from 2.
    os._exit(2)


def verdict(script, argv, payload):
    """A guard's refusal reason for `payload`, or None if it let the call through."""
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        return f"guard {script} is missing at {path}"
    try:
        run = subprocess.run(argv + [path], input=json.dumps(payload), capture_output=True,
                             text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"guard {script} did not answer ({type(exc).__name__})"
    if run.returncode == 2:
        return run.stderr.strip() or f"guard {script} blocked the call"
    if run.returncode != 0:
        return f"guard {script} failed with exit {run.returncode}"
    try:
        out = json.loads(run.stdout) if run.stdout.strip() else {}
        decision = out.get("hookSpecificOutput", {})
        if decision.get("permissionDecision") == "deny":
            return decision.get("permissionDecisionReason") or f"guard {script} denied the call"
    except (ValueError, AttributeError):
        return f"guard {script} answered with output the bridge cannot read"
    return None


def _judge():
    import codex_tool_shape  # inside main's catch-all: a failed import must deny
    try:
        call = json.load(sys.stdin)
    except Exception as exc:  # fail closed: an unread payload is an unknown harness
        deny(f"could not read the tool call ({exc})")
    kind, command, cwd, why = codex_tool_shape.classify(call)
    if kind == "unnormalizable":
        deny(why)
    global _COMMAND
    _COMMAND = command if kind == "shell" and isinstance(command, str) else None
    if kind == "other":
        sys.exit(0)
    if not SP.safe_which("jq"):
        deny("jq is not installed, and block-pr-merge.sh lets everything through without it")
    payload = call
    if kind == "shell":
        payload = dict(call)
        payload["tool_name"] = "Bash"
        payload["tool_input"] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
    for script, argv in GUARDS:
        if kind == "connector" and script == "block-pr-merge.sh":
            continue  # it judges a shell command and there is none
        reason = verdict(script, argv, payload)
        if reason:
            deny(reason)
    sys.exit(0)


def main():
    """Fail closed on the shim's own crash. Under Claude's hook contract only exit 2
    blocks, so an uncaught exception (exit 1) would let the call through."""
    try:
        _judge()
    except Exception as exc:  # SystemExit from deny() and exit(0) is not an Exception
        deny(f"the bridge crashed ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()
