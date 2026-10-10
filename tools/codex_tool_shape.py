"""Turn a Codex PreToolUse payload into the shape our two guards already read.

Imported by tools/codex-hook-shim.py and, lazily, by tools/outbound-guard.py.
Standard library only: outbound-guard runs under /usr/bin/python3.

WHY THIS EXISTS. Our guards were written for Claude Code, where a shell command
arrives as tool_name "Bash" with tool_input.command. Codex's shell tool is not
called Bash, and its name and argument keys vary with the model's catalog entry
(every model on this machine declares shell_type "unified_exec"). A guard that
branches on tool == "Bash" and otherwise exits 0 reads a Codex command as
"not a shell call" and lets it through, which looks exactly like coverage.

So classify() answers one question per payload, and a wrong answer must land on
"unnormalizable", never on "other":

  shell           a command we can read; `command` holds the text to judge
  connector       an mcp__ tool; the guards judge it by name and arguments as-is
  other           a tool NAMED in INERT_TOOLS, known not to execute a command
  unnormalizable  every other tool with no command we can read

`other` is an allowlist. Codex's tool catalog is open (custom tools take a
string input, and a catalog release can add `js_repl` or `python`), so a name
blocklist fails open on every name it did not anticipate. An unknown tool is
denied until someone reads what it does and adds it here.

The last is denied by the shim and blocked by outbound-guard. Fail closed,
against the guards' fail-open default, because here an unread payload means an
unrecognised harness rather than a missing jq.

RECORDED SHAPES. Two are observed, in a Codex rollout from 2026-09-04
(~/.codex/sessions): a custom tool named `exec` whose input is a JavaScript
string that calls `tools.exec_command({cmd: "...", workdir: "..."})`. Codex's
"code mode" issues shell commands from inside that runtime, so a hook on `exec`
sees one coarse call of source code and never the command inside it. classify()
cannot read a command out of JavaScript, so it lands on "unnormalizable" and the
run is denied until slice 2's conformance probe settles what a hook really sees.
The other names (shell_command, shell, local_shell, write_stdin) are from Codex's
public tool definitions and are NOT observed on this machine; a shape none of
them fits is denied, not guessed.
"""
import re
import shlex

# Tool names that carry a shell command, and the input keys that hold it.
SHELL_TOOLS = {"exec_command", "shell_command", "shell", "local_shell", "container.exec",
               "unified_exec", "run_shell_command", "bash", "Bash"}
COMMAND_KEYS = ("command", "cmd", "script")

# Tools known not to run a command, named exactly as Codex sends them. Only these
# are allowed without a readable command. read_file, list_dir and grep_files are
# from Codex's public tool definitions and are not observed on this machine. Read,
# Glob and Grep are Claude's read-only tools: outbound-guard's ported suite feeds
# them to the guard, and they cannot run a command.
INERT_TOOLS = {"apply_patch", "update_plan", "view_image", "web_search", "request_user_input",
               "read_file", "list_dir", "grep_files", "Read", "Glob", "Grep"}


class Unreadable(Exception):
    """An argv whose executed script the guards cannot be shown; the caller denies."""


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_C_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_ARG_TAKING = re.compile(r"-[A-Za-z]*[oO][A-Za-z]*")  # -o/-O consume the next argument
_PREFIXES = {"sudo", "nohup", "time", "command"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "tcsh", "csh", "ash", "rbash", "busybox"}
# A versioned shell (bash-5.2, sh4) is still that shell.
_SHELL_NAME = re.compile("(?:" + "|".join(sorted(SHELLS)) + r")(?:[-.0-9].*)?")


def _is_shell(arg):
    return bool(_SHELL_NAME.fullmatch(arg.rsplit("/", 1)[-1]))


def _is_c_flag(arg):
    return arg == "--command" or bool(_C_FLAG.fullmatch(arg))


def _strip_wrappers(argv):
    """(rest, exact): argv without the leading wrappers we can parse exactly.

    env takes only NAME=VALUE assignments; sudo, nohup, time and command take no
    options. Anything else (`env -i`, `sudo -u root`) stops the strip with exact False,
    because we can no longer say which program runs.
    """
    rest = list(argv)
    while rest:
        name = rest[0].rsplit("/", 1)[-1]
        if name == "env":
            i = 1
            while i < len(rest) and _ASSIGNMENT.match(rest[i]):
                i += 1
            if i < len(rest) and rest[i].startswith("-"):
                return rest, False
            rest = rest[i:]
        elif name in _PREFIXES:
            if len(rest) > 1 and rest[1].startswith("-"):
                return rest, False
            rest = rest[1:]
        else:
            break
    return rest, True


def _argv_to_command(argv):
    """The text the guards should judge for one argv, or raise Unreadable.

    THE RULE: a guard's reading must never be narrower than what the executor runs.
    Joining `["bash", "-lc", "gh pr merge 1"]` would quote the script, and
    block-pr-merge anchors on a command-segment start, so `gh pr merge` behind the
    quote is invisible. So a shell run with -c is unwrapped only in the one form we can
    read exactly, [shell, option flags..., -c flag, script] with nothing after the
    script, and every other shell-with-c argv is refused rather than enumerated:
    trailing arguments (`bash -c '$1' x 'gh pr merge 1'` runs the merge but reads as
    `$1`), option arguments, and wrappers we could not parse. A shell with no -c runs a
    script file or stdin, and its joined form is judged as before.
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        return None
    rest, exact = _strip_wrappers(argv)
    if not (exact and rest and _is_shell(rest[0])):
        # Any wrapper we do not parse (timeout, xargs, exec, nice, doas, busybox sh...)
        # can sit before a shell, so a shell -c anywhere in the argv is refused rather
        # than judged as the quoted join, which hides the script from a segment-start anchor.
        for i, arg in enumerate(argv):
            if _is_shell(arg) and any(_is_c_flag(a) for a in argv[i + 1:]):
                raise Unreadable("a shell -c command sits behind a wrapper the guards cannot parse")
        return shlex.join(argv if not exact or not rest else rest)
    args = rest[1:]
    cflags = [i for i, a in enumerate(args) if _is_c_flag(a)]
    if cflags:
        flags = args[:-1]
        if (len(cflags) == 1 and len(args) >= 2 and cflags[0] == len(args) - 2
                and all(f.startswith("-") and not _ARG_TAKING.fullmatch(f) for f in flags)):
            return args[-1]
        raise Unreadable("a shell -c command with arguments the guards cannot read exactly")
    return shlex.join(rest)


def _commands_in(node):
    """Every command text readable anywhere in one tool_input."""
    if isinstance(node, str):
        return [node]
    if not isinstance(node, dict):
        return []
    found = []
    for key in COMMAND_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            got = _argv_to_command(value)
            if got is not None:
                found.append(got)
    action = node.get("action")  # local_shell nests it: {"action": {"command": [...]}}
    if isinstance(action, dict):
        found.extend(_commands_in(action))
    return found


def _command_from(node):
    """(command, conflict) for one tool_input; command is None if it holds none we can read.

    Codex runs whichever key its own schema reads, and we cannot tell which that is, so
    `{"cmd": "gh pr merge 1", "command": "true"}` must not be judged by `true`. Every
    readable command is collected across all keys and nestings; two that differ are a
    conflict, and the caller denies. One, or several identical, judge as one.
    """
    try:
        found = _commands_in(node)
    except Unreadable as exc:
        return None, str(exc)
    if len(set(found)) > 1:
        return None, ("the input names different commands under different keys "
                      f"({len(set(found))} distinct), and the guards cannot know which one runs")
    return (found[0] if found else None), None


def classify(call):
    """Return (kind, command, cwd, reason) for a Codex PreToolUse payload."""
    if not isinstance(call, dict) or not isinstance(call.get("tool_name"), str) or not call["tool_name"]:
        return "unnormalizable", None, None, "the payload has no readable tool_name"
    tool = call["tool_name"]
    args = call.get("tool_input")
    cwd = call.get("cwd")
    if isinstance(args, dict):
        cwd = args.get("workdir") or args.get("cwd") or cwd

    if tool.startswith("mcp__"):
        return "connector", None, cwd, None

    if tool == "write_stdin":
        # Polling a running command sends no characters; typing into a shell that
        # an earlier, harmless command started IS running a command.
        chars = args.get("chars") if isinstance(args, dict) else None
        if chars is None or chars == "":
            return "other", None, cwd, None
        if isinstance(chars, str):
            return "shell", chars, cwd, None
        return "unnormalizable", None, cwd, "write_stdin carries chars that are not a string"

    if tool in SHELL_TOOLS:
        command, conflict = _command_from(args)
        if conflict:
            return "unnormalizable", None, cwd, f"shell tool '{tool}': {conflict}"
        if command is not None:
            return "shell", command, cwd, None
        return ("unnormalizable", None, cwd,
                f"shell tool '{tool}' has an input shape with no readable command "
                f"({_describe(args)})")

    if tool in INERT_TOOLS:
        return "other", None, cwd, None

    command, conflict = _command_from(args) if isinstance(args, dict) else (None, None)
    if conflict:
        return "unnormalizable", None, cwd, f"tool '{tool}': {conflict}"
    if command is not None:
        return "shell", command, cwd, None
    return ("unnormalizable", None, cwd,
            f"tool '{tool}' is not known to be inert and its input is an "
            f"unrecognised shape ({_describe(args)}); a hook on it sees the call, "
            f"not the commands inside it")


def _describe(args):
    if isinstance(args, str):
        return f"a string of {len(args)} characters"
    if isinstance(args, dict):
        return "an object with keys " + ", ".join(sorted(str(k) for k in args)[:8])
    return f"a {type(args).__name__}"
