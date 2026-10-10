"""session_lifecycle — start and stop the tmux sessions Deck attaches to.

WHY THIS EXISTS. Deck could attach to a co session and drive it, and could do
nothing else: a session had to be created by hand in another terminal first, and
ended by hand afterwards. So the window that is meant to be where the fleet is
worked could not begin or finish any of it. The operator, 2026-07-23: "we need some way
to start tmux sessions ... right now I have to start them in a separate iTerm
window, and then Deck can attach to them."

THE SHAPE. Starting is two steps that must not be conflated: create a detached
tmux session with a working directory, then run a command inside it. Deck's
catalog supplies both. Stopping is the reverse and is destructive, so it is
explicit about which half it is doing.

WHAT IS VALIDATED AND WHY EACH. Three different things reach a shell here and
each needs its own gate:

  * the session NAME lands in tmux argv — the existing charset rule, shared with
    every other verb.
  * the WORKING DIRECTORY lands in `tmux new-session -c`, and a path that does
    not exist makes tmux fail in a way that reads like a broken session rather
    than a bad config, so it is resolved and checked here.
  * the COMMAND is typed into the session as keystrokes. It is the one input
    that is arbitrary by design — the point of the catalog is that a row can say
    `co -r` — so it cannot be an allowlist of commands. It is bounded instead:
    one line, no control characters, and length-capped, so a config row cannot
    smuggle a newline and become two commands.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".local" / "lib" / "agent-platform"))
import claude_trust as CTR  # noqa: E402
import session_input as SI  # noqa: E402
import tmux_transcript as TT  # noqa: E402
import builder_root as BR  # noqa: E402

JoinError = TT.JoinError
InputError = SI.InputError


def _real_account_home() -> Path:
    """The account's home from the passwd database, not `$HOME`. Now read from
    `builder_root`, the one module that resolves the machine's root
    (company/decisions/0008); see its docstring for why passwd and not `$HOME`."""
    return BR.real_account_home()


# The default root `--create-dir` may `mkdir(parents=True)` under, when nothing
# in CREWS_ROOT_CONFIG overrides it. Nothing server-side stops a caller of
# `session-agent start --dir <path> --create-dir` from naming any absolute path
# this account can write — "only Deck's New Project flow passes this flag" is a
# client-side convention, not an enforced boundary — so this is the
# defense-in-depth floor under that convention. See `_creatable_root` and
# `_validated_directory` for how it is applied.
CONVIVY_ROOT = BR.fallback_for(_real_account_home())

# The per-machine config naming the actual creatable root, read fresh on every
# call rather than cached or sourced. `session-agent` runs over ssh
# non-interactively, which does not load a login shell's rc files — an
# environment variable set in `.zshrc` would silently never reach this
# process, and the root would fall back to the default with nothing to say so.
# A file read at call time has no such gap, which is why this is a file rather
# than `os.environ`.
CREWS_ROOT_CONFIG = BR.config_for(_real_account_home())

# The only key `CREWS_ROOT_CONFIG` may set. Parsed as DATA, never sourced or
# executed — the same doctrine `overnight-flags.sh` applies to the overnight
# launchers' own per-machine config, for the same reason: this file is
# operator-editable, and a bad or malicious line in it must never do more than
# leave the setting unset.
_CREWS_ROOT_KEY = BR.KEY

# What a catalog row may ask to be run. Long enough for a real command with
# flags, short enough that a runaway value is refused rather than typed.
MAX_COMMAND = 200

# The default, and the reason the catalog usually carries no command at all.
DEFAULT_COMMAND = "co"

# How long `stop --kill` waits for the agent to actually go before killing the
# session out from under it. Bounded because an agent that will not exit must
# not be able to block the kill forever — the user asked for the session gone.
EXIT_WAIT_SECONDS = 8.0
EXIT_POLL_SECONDS = 0.25


class LifecycleError(Exception):
    """A session could not be started or stopped, with a reason to act on."""


def _validated_command(command: str | None) -> str:
    """The command to run in a new session.

    NOT AN ALLOWLIST, and that is deliberate: the catalog exists so a row can say
    `co -r` or a specialist agent, and enumerating those here would mean editing
    this file every time the operator adds one. The gates are on SHAPE instead — a single
    line with no control characters — which is what stops a config row from
    becoming two commands or from carrying an escape sequence into the terminal.
    """
    # `if command is None`, NOT `command or DEFAULT` — the `or` form turned an
    # explicitly empty command into `co`, so a catalog row with `"command": ""`
    # silently started an agent the row did not ask for. Absent means default;
    # present-but-empty is a config mistake and says so.
    if command is None:
        command = DEFAULT_COMMAND
    command = command.strip()
    if not command:
        raise LifecycleError("refusing to run an empty command")
    if len(command) > MAX_COMMAND:
        raise LifecycleError(f"command is longer than {MAX_COMMAND} characters")
    # `\n` would submit and start a second command; `\r`, `\x1b` and friends are
    # terminal control rather than text. The same rule `send_message` applies to
    # a message, for the same reason.
    # U+2028/U+2029 are above the ASCII range and Unicode calls them line
    # terminators. They are not exploitable at a shell, which reads only ASCII
    # control bytes as line breaks — but Deck is the caller, JavaScript treats
    # them as real line breaks in a string literal, and a future step there that
    # re-serialises the command would turn one into a `\n` on the far side of
    # this gate. Cheaper to close now than to track that path. Raised by
    # security-reviewer.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "\u2028\u2029"
           for ch in command):
        raise LifecycleError("a command must be one line with no control characters")
    return command


def _configured_crews_root_raw() -> str | None:
    """`builder_root.configured_raw`, reading this module's CREWS_ROOT_CONFIG at
    call time so a test that points it elsewhere is honoured."""
    return BR.configured_raw(CREWS_ROOT_CONFIG)


def _expanded_against_real_home(raw: str) -> Path:
    """`builder_root.expanded`, against this module's `_real_account_home`."""
    return BR.expanded(raw, _real_account_home())


def _creatable_root() -> Path:
    """`builder_root.root`, the machine's one root resolver, fed this module's
    CREWS_ROOT_CONFIG, CONVIVY_ROOT and `_real_account_home` at call time. The
    logic lives there; these names stay because tests point them at a temporary
    directory."""
    return BR.root(CREWS_ROOT_CONFIG, CONVIVY_ROOT, _real_account_home())


def creatable_root() -> str:
    """The public read of `_creatable_root`, for a caller that only wants to
    know the value rather than act on it.

    Deck's `crewsRoot` setting ships hardcoded to a fixed path under the
    account's home directory, named after Convivy itself — which does not
    belong compiled into a client meant for an operator who has never heard
    of Convivy — but it cannot simply drop that
    default, because this host refuses to create a crew directory outside
    THIS value. Dropping Deck's default without a way to ask the host what it
    actually is would just move the mismatch: every create would be refused
    instead of every create assuming the wrong root.

    Stringified exactly as `_validated_directory` embeds it in a refusal
    message — unresolved, not `str(Path(...).resolve())` — so a client
    reporting this value and a refusal naming it always agree.
    """
    return str(_creatable_root())


def _validated_directory(directory: str | None,
                         create: bool = False) -> tuple[str | None, bool]:
    """An existing absolute directory (or None to inherit tmux's default), and
    whether THIS CALL is what made it exist.

    Expanded here rather than passed through, because `~` reaches tmux as a
    literal when it arrives in argv rather than through a shell — a session
    would start in a directory named `~` beside the repo. Checked for existence
    for a different reason: `tmux new-session -c` on a missing path fails with a
    message about the session, and the user would go looking at the wrong thing.

    `create` OPTS A CALLER IN to `mkdir -p`-ing a missing directory instead of
    refusing it, and it is off by default on purpose: a catalog row's
    `workingDirectory` is usually someone's config, and a typo in it should keep
    failing loudly with "no such directory" rather than quietly manufacturing a
    directory nobody meant to create. Deck's New Project flow is the one caller
    that MEANS the directory not to exist yet — it is creating a project, not
    addressing an existing one — so it is the only caller that passes `create`.

    A CREATED directory must resolve under the creatable root — `CREWS_ROOT`
    from `CREWS_ROOT_CONFIG` when the box has set one, `CONVIVY_ROOT`
    (the compiled-in fallback, `~/builder`) otherwise (see
    `_creatable_root`) — because "only Deck passes
    `create`" is a caller convention with nothing in this repo enforcing it,
    so this is the server-side floor under that convention: without it, any
    caller reaching this flag could `mkdir(parents=True)` at an arbitrary
    absolute path. Checked AFTER resolving symlinks and `..` components
    (`Path.resolve()`), so a path that only *looks* contained — an existing
    symlinked parent, or a `../../` climb — cannot walk out past the check
    that was meant to catch it.

    The second element of the return is `True` only for the `mkdir` branch —
    `create=True` on a directory that already existed returns `False`, same as
    `create=False`. `start` uses this, not the `create` flag alone, to decide
    whether to hand the path to `claude_trust`: a directory `--create-dir`
    merely tolerated because it was already there is not one the caller just
    chose to create.

    `exist_ok=False` ON THE MKDIR ITSELF, not `True` — because `True` would
    mean "the mkdir branch ran" and "this call's own mkdir made the
    directory" are not the same fact. Another local process can win the race
    between the `is_dir()` check above and this call's own `mkdir`, creating
    the target first with content of its own choosing; `exist_ok=True` would
    swallow that as `FileExistsError` and return `True` for a directory this
    call merely adopted. Caught here and treated the same as the ordinary
    adopt branch — `dir_created=False` — so `start` still succeeds (the
    directory exists, which is all the caller asked for) but never offers a
    racing creator's directory to `claude_trust`.

    `FileExistsError` OUT OF `mkdir` IS NOT PROOF OF A DIRECTORY. `Path.mkdir`
    raises it just the same when a regular file, or a symlink resolving to
    one, already sits at the target — `exist_ok` only changes whether an
    *existing directory* is tolerated, not what happens when the existing
    thing is not a directory. Adopting on the strength of the exception alone
    would hand `start` a path that is not a directory at all, and tmux would
    be asked to use it as one. So the catch checks `is_dir()` itself: a real
    directory is the racing-creator case this exists for and is adopted the
    same as before; anything else re-raises as the loud failure a plain file
    at this path always deserved.
    """
    if directory is None:
        return None, False
    expanded = Path(directory).expanduser()
    if not expanded.is_absolute():
        raise LifecycleError(f"working directory must be absolute: {directory}")
    if not expanded.is_dir():
        if not create:
            raise LifecycleError(f"no such directory: {expanded}")
        root = _creatable_root()
        resolved = expanded.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            raise LifecycleError(
                f"--create-dir path must be under {root}: {resolved}")
        try:
            resolved.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if resolved.is_dir():
                return str(resolved), False
            raise LifecycleError(
                f"--create-dir path exists and is not a directory: {resolved}")
        return str(resolved), True
    return str(expanded), False


def session_exists(name: str) -> bool:
    """Whether tmux already has a session by this name."""
    TT.validate_session_name(name)
    return TT.run(["tmux", "has-session", "-t", f"={name}"]) is not None


# THE DEFAULT TMUX SERVER SHOULD BE FORKED FROM THE GUI SESSION. Every tmux session
# inherits the security context of the SERVER that forked it, not of the client
# that attached. A server forked from an SSH login cannot read the login
# Keychain, so every crew under it got exit 36 from `security
# find-generic-password` and HTTP 401 from gh, which crews read as a revoked
# token. That lasted from a restart over SSH at 08:42 to a restart from the GUI
# session at 08:50 on 2026-10-05. The keeper job runs in the Aqua session and
# forks the server there. ensure_default_server() starts that job only when no
# server answers; a server that is already up passes, whatever forked it. So the
# GUI origin is guaranteed only for a server this helper started.
KEEPER_LABEL = "com.convivy.tmux-keeper"
KEEPER_SESSION = "keeper"
# Bounds the wait for the keeper session. Short polls, so a keeper that is up is
# seen within a fraction of a second of appearing.
KEEPER_WAIT_SECONDS = 10.0
KEEPER_POLL_SECONDS = 0.2


class TmuxServerError(LifecycleError):
    """The default tmux server is not running and could not be started from the GUI."""


def _probe_default_server() -> tuple[list[str] | None, str]:
    """Session names on the default tmux server, or None when no server answers.

    The second item is the fault from this check, empty on success: tmux's
    stderr when it exits non-zero (`no server running` and a socket error both
    arrive that way), or the exception text when tmux could not be run.

    TMUX and TMUX_PANE are stripped: when TMUX is set, tmux resolves its socket
    from it, so an inherited value would ask the caller's server rather than the
    default one this checks.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        done = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if done.returncode != 0:
        return None, done.stderr.strip()
    return done.stdout.splitlines(), ""


def _keeper_refusal(detail: str) -> TmuxServerError:
    return TmuxServerError(
        f"{detail}. The tmux server must be started from the GUI session: run "
        f"`launchctl kickstart gui/{os.getuid()}/{KEEPER_LABEL}` there.")


def ensure_default_server(wait: float = KEEPER_WAIT_SECONDS,
                          poll: float = KEEPER_POLL_SECONDS) -> None:
    """Make sure the default tmux server is running.

    Returns at once when any session exists on the default server, whichever
    process forked it, so a server forked over SSH passes unchecked. Otherwise it
    kickstarts the keeper job without `-k`, because a keeper that is already
    running must not be killed, and waits up to `wait` seconds for the keeper
    session to appear. The keeper job forks that server from the GUI session. It
    raises TmuxServerError when the kickstart fails or the keeper session does
    not appear in time; the timeout message carries the last tmux fault.

    Checks the default socket only. A caller with TMUX_TMPDIR set checks the
    socket that variable names instead. No current caller sets it, so no guard
    is added for it.

    Never runs `tmux new-session` or `start-server` itself. A caller's shell is
    the wrong place to fork the server, because the server inherits that shell's
    security context.
    """
    names, _ = _probe_default_server()
    if names:
        return
    try:
        kick = subprocess.run(
            ["launchctl", "kickstart", f"gui/{os.getuid()}/{KEEPER_LABEL}"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _keeper_refusal(f"launchctl could not run ({exc})") from exc
    if kick.returncode != 0:
        raise _keeper_refusal(
            f"launchctl kickstart exited {kick.returncode}: {kick.stderr.strip()}")
    deadline = time.monotonic() + wait
    # Exact match on the list. `has-session -t keeper` prefix-matches, so a
    # session named `keepers` would otherwise pass for the keeper.
    names, fault = _probe_default_server()
    while names is None or KEEPER_SESSION not in names:
        if time.monotonic() >= deadline:
            if names is None:
                last = fault or "tmux exited without a message"
            else:
                last = f"the server lists {', '.join(names) or 'no sessions'}"
            raise _keeper_refusal(
                f"the keeper session did not appear within {wait:g} seconds; "
                f"last tmux check: {last}")
        time.sleep(poll)
        names, fault = _probe_default_server()


# What a pane's foreground process may be for the command to be typed into it.
# The pane's CURRENT command being the shell is what says nothing is running in
# it: a pane sitting in vim or a REPL reports that program instead, and typing
# `co` there would go into its buffer rather than to a prompt.
ADOPTABLE_SHELLS = frozenset({"zsh", "bash", "sh", "fish", "dash", "ksh",
                              "tcsh", "csh"})

# The `%N` form tmux gives a pane. Matched in full rather than by its leading
# `%`, so the value that becomes a `send-keys` target is one this module has
# checked rather than whatever tmux's format output happened to contain. The
# value is tmux's own and there is no injection path through it today; the point
# is to hold that invariant here instead of depending on tmux's behaviour
# remaining what it is. Raised by security-reviewer.
PANE_ID_RE = re.compile(r"%\d+")


def _idle_shell_pane(name: str) -> str | None:
    """The id of a pane sitting at a bare shell prompt, or None if none is.

    Used to decide whether an agentless session can be adopted. Returns a pane
    id rather than a bool because the command is then typed at THAT pane: a
    session with more than one pane would otherwise have its keystrokes go to
    whichever pane tmux considers active, which is not the one that was checked.
    """
    out = TT.run(["tmux", "list-panes", "-t", f"={name}", "-F",
                  "#{pane_id} #{pane_current_command}"])
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        # A foreground process whose argv[0] carries a space splits into more
        # than two parts and the pane is skipped, which is the conservative
        # answer: it is not a bare shell.
        if len(parts) == 2 and PANE_ID_RE.fullmatch(parts[0]) \
                and parts[1] in ADOPTABLE_SHELLS:
            return parts[0]
    return None


def start(name: str, directory: str | None = None,
          command: str | None = None, create_dir: bool = False) -> dict:
    """Create a detached tmux session and run a command in it.

    `create_dir` passes straight through to `_validated_directory`'s `create`:
    off by default, so a missing `directory` still raises `LifecycleError`
    exactly as before. See that function's docstring for who should pass it.

    A DIRECTORY THIS CALL CREATES IS PRE-TRUSTED FOR CLAUDE CODE. Claude Code's
    interactive startup added a workspace-trust prompt with nothing to
    pre-answer it — a brand-new directory has never been trusted, so `co`
    typed into a session that was just created here stops on that prompt with
    nobody there to answer it, and the crew is born unusable. `directory` was
    named by the caller and just created on their behalf, which is the
    decision the trust prompt exists to ask about — so `_validated_directory`
    having actually made it (not merely `create_dir` being set; see its own
    docstring) is what triggers `claude_trust.pre_trust_created_directory`.
    Never for an adopted or pre-existing directory: that one was not created by
    this call and the user may never have looked inside it, so it keeps
    prompting. See `claude_trust` for the coupling this leans on and how it
    fails soft; the outcome is reported in `directory_trusted` /
    `directory_trust_reason` below rather than raised, since a session that
    starts with a trust prompt is a UX regression, not a reason to fail
    `start`.

    DETACHED, always. Deck attaches over its own ssh connection; a session
    created attached would hold this process open and tie the session's life to
    an ssh call that is meant to return.

    ADOPTS AN AGENTLESS SESSION RATHER THAN REFUSING IT. What must never happen
    is a SECOND agent in a pane that already has one — two claudes in one pane is
    not a state anything downstream can read, and `claude_pane` would return
    whichever it found first. Session-already-exists is a different thing, and
    conflating the two produced a dead end: a session whose agent had exited
    without the session being killed (a plain `/exit`, a crash, a `stop` without
    `--kill`) greys on Deck's rail, which is correct — it has no agent — and its
    Start button then failed forever with "already exists". Deck swallows that
    message as an expected race, so the click reported nothing at all. Observed
    on the `ask` session, 2026-07-24.

    So the refusal is now on the agent, not on the session: an existing session
    with a live agent is refused, and one sitting at a bare shell has the command
    typed into it, which is the same two steps a fresh session gets minus the
    creation. A pane running something else is refused too, because the command
    is keystrokes and they would land in that program.
    """
    TT.validate_session_name(name)
    # The command is checked BEFORE tmux is asked anything. A bad command should
    # cost nothing and touch nothing — and the existence check is a subprocess,
    # so validating first is also the cheaper order.
    run_command = _validated_command(command)
    # The server check comes before the directory is validated and created, so a
    # refused server leaves no directory behind. A bad directory is still refused,
    # but only after this check: when the server is up the check is one read, and
    # when it is down the only side effect a refused row can leave is the keeper
    # having been kickstarted.
    ensure_default_server()
    workdir, dir_created = _validated_directory(directory, create=create_dir)

    directory_trusted = None
    directory_trust_reason = None
    if dir_created:
        trust = CTR.pre_trust_created_directory(_real_account_home(), workdir)
        directory_trusted = trust["trusted"]
        directory_trust_reason = trust["reason"]

    existed = session_exists(name)
    # The session name, which `send-keys` resolves to its active pane — see the
    # note below on why this carries no `=` anchor. Adoption replaces it with the
    # id of the pane that was actually checked.
    target = name
    if existed:
        try:
            TT.claude_pane(name)
        except TT.JoinError:
            pass  # No agent — adoptable, if a pane is at a prompt.
        else:
            raise LifecycleError(f"session {name!r} already has an agent")
        pane = _idle_shell_pane(name)
        if pane is None:
            raise LifecycleError(
                f"session {name!r} exists and no pane of it is at a shell prompt")
        # The pane that was checked, not the session's active pane. `workdir` is
        # deliberately not applied: the session already has a directory, and a
        # `cd` typed ahead of the command would be a second command on a line
        # that is gated as one.
        target = pane
    else:
        argv = ["tmux", "new-session", "-d", "-s", name]
        if workdir:
            argv += ["-c", workdir]
        if TT.run(argv) is None:
            raise LifecycleError(f"tmux could not create session {name!r}")

    # Typed rather than passed as new-session's own command argument. A command
    # given to `new-session` becomes the pane's process, so when it exits the
    # session dies with it — an agent that finished would take its own terminal
    # away, and there would be no shell left to start another one in.
    #
    # NOT shell-quoted, and that is not an oversight: this is keystrokes into an
    # interactive shell, so the text has to arrive exactly as a person would
    # type it — quoting it would run `'co -r'` as a program name.
    # `_validated_command` is what makes that safe.
    # `-l`, AND IT IS THE WHOLE SAFETY OF THIS CALL. `tmux send-keys` matches
    # each argument against its KEY TABLE first: an argument that is a key name
    # is dispatched as that key rather than typed. So a catalog row of `C-d`
    # sent Ctrl-D and exited the shell, leaving a session with no agent — the
    # exact state `_validated_command` was meant to prevent, reached while
    # looking like nothing had gone wrong. `Enter`, `Escape`, `C-c`, `Tab` and
    # the arrows are all reachable the same way, and all pass a gate that only
    # looks for control CHARACTERS.
    #
    # Measured: without `-l` the session was dead a second later; with it the
    # shell printed `C-d: command not found` and stayed up.
    #
    # `session_input.KEYS` exists for exactly this reason one module over — it
    # is an allowlist "because this string reaches `tmux send-keys` as a key
    # specification". This is that hazard arriving by another door. Caught by
    # security-reviewer.
    #
    # The text and the Enter are therefore two calls: the first literal, the
    # second a real key press.
    #
    # No `=` anchor: that is a SESSION-target modifier and these take a PANE
    # target, where `send-keys -t =name` fails outright. tmux also rewrites a
    # dot in a session name to an underscore at creation, so the prefix-match
    # misparse the anchor guards against is not reachable here. An adopted
    # session passes a `%N` pane id instead, which is unambiguous either way.
    if TT.run(["tmux", "send-keys", "-l", "-t", target, run_command]) is None:
        raise LifecycleError(f"session {name!r} started but the command did not")
    if TT.run(["tmux", "send-keys", "-t", target, "Enter"]) is None:
        raise LifecycleError(f"session {name!r} has the command but it was not submitted")

    # `directory` is the one the session was CREATED with, so an adopted session
    # reports None rather than the workdir it was asked for — the caller should
    # not read back a directory that was never applied.
    #
    # `directory_trusted` is `None` when pre-trust was never attempted (no
    # `directory`, an adopted session, or `_validated_directory` did not itself
    # create it) — distinct from `False`, which means an attempt was made and
    # failed, with `directory_trust_reason` saying why.
    return {"ok": True, "session": name,
            "directory": None if existed else workdir,
            "command": run_command, "created": not existed,
            "directory_trusted": directory_trusted,
            "directory_trust_reason": directory_trust_reason}


# The prefix Deck gives a scratch shell's tmux session, mirrored from
# `TerminalTarget.remoteShell` on the Swift side. Nothing else may be reaped:
# the whole safety of this pass is that it can only ever address sessions Deck
# created for scratch work, never a fleet session with an agent in it.
SCRATCH_SHELL_PREFIX = "deck-shell-"

# A whole scratch-shell name, matched in full before the reaper acts on it.
#
# THE PARSER READS A FORMAT STRING THE NAMES THEMSELVES FEED. `list-sessions`
# output is split on whitespace, and tmux permits whitespace in a session name
# even though `validate_session_name` does not — so a session created outside
# `session-agent` and named with an embedded newline would produce a synthetic
# second line whose first token is whatever followed it. The kill target is
# always `={parts[0]}`, so nothing outside the `deck-shell-` namespace is
# addressable either way; what this closes is one scratch shell forging a line
# that names ANOTHER one, killing it early against a timestamp that was never
# its own. Trusting the creation paths to have validated is not the same as
# checking. Raised by security-reviewer.
SCRATCH_SHELL_NAME_RE = re.compile(r"deck-shell-[A-Za-z0-9._-]{1,53}")

# How long a scratch shell must sit untouched before it is reapable. A day,
# because a shell you used this morning and will use again after lunch is not
# stale, and because the cost of keeping one is a few kilobytes against the cost
# of reaping one wrongly, which is somebody's work.
SCRATCH_SHELL_IDLE_SECONDS = 24 * 60 * 60


def sessions() -> dict:
    """Every non-scratch tmux session on this host, for Deck to group under a
    project by name or by `#{session_path}` (multiple-sessions-per-project
    design, section B).

    TWO TMUX CALLS, not one per session. The original one-call design leaned on
    `#{session_path}` being the sole field capable of carrying a delimiter or a
    record boundary — true, but that is exactly what makes it capable of
    FORGING one too: a path holding both a raw newline and three raw tabs
    (`mkdir -p $'/tmp/x\\nfake\\t9\\t1\\t/fake'`, legal POSIX — only `/` and NUL
    are forbidden) produces a line that is indistinguishable, from inside that
    one call, from a genuine second record. No amount of re-framing the single
    call closes this, because the untrusted field can contain any delimiter the
    framing might use to describe itself. So a second, cheap call anchors the
    result instead: `tmux list-sessions -F '#{session_id}\t#{session_name}'`
    costs one more round trip, still nothing per session, and Deck polls this on the rail's
    ~4s cadence over the same ControlMaster the first call already shares.

    THE ANCHOR IS THE TRUSTED NAME SET. `#{session_name}` is escaped by tmux at
    storage — a session named with an embedded tab or newline reads back with
    those bytes replaced by the literal two-character sequence `\\t`/`\\n` — so
    call one always yields exactly one line per real session, and every one of
    those lines carries a genuine name (after the session's `$<digits>` id and
    one tab; see the comment at the call). (Measured against a live tmux 3.6a:
    `#{session_path}`, unlike the name, is NOT escaped, in either plain or
    `#{q:...}` form — a `q:`-wrapped path reads back with the same raw bytes,
    since escaping happens at name-storage time, before `q:` ever sees the
    value. There is no framing of the path call that would make it
    self-describing.)

    CALL TWO IS THE EXISTING RECORD CALL, parsed exactly as before — TAB
    DELIMITED, path last, each candidate record `maxsplit=3` so a raw tab in
    the path lands inside it rather than shifting the field count, and a line
    without at least three tabs treated as a continuation of the record still
    being built rather than a new one (recombining a raw newline in a path,
    which still splits `list-sessions`' output into two lines; `splitlines()`
    is not used for this, since it also breaks on `\\r`, `\\v`, `\\f`,
    `\\x1c`-`\\x1e`, `\\x85`, U+2028 and U+2029, widening the set a hostile path
    could exploit). THIS PARSE ALONE DOES NOT GUARANTEE ANYTHING — a crafted
    path can still make it emit a record under a name the attacker chose, or
    truncate a real record's path where its own embedded tabs+newline read as
    a record boundary. What it guarantees, paired with the anchor below, is
    that every record it emits is CHEAP TO CHECK against a name tmux itself
    vouches for.

    RECONCILIATION IS WHAT ACTUALLY CLOSES THE FORGERY. A parsed record is
    kept only if its name is a member of the trusted set from call one; every
    other parsed record — including a fully-formed, plausible-looking one — is
    dropped, because a name not in tmux's own name listing cannot be a real
    session. A name can also appear on MORE THAN ONE parsed record (a
    crafted path forging a record under an existing session's own name is
    exactly this case): when that happens, which record is genuine is not
    decidable from here, so none of its fields are trusted — the session is
    emitted once, by name, with `path`, `windows` and `attached` all `null`,
    rather than guessing. A trusted name with no parsed record at all (the
    record call dropped it — a bad window-count field, or it went missing
    between the two calls) gets the same treatment: emitted by name with
    everything else `null`, the same degrade-rather-than-drop instinct the
    original design had for `path` alone, now extended to every field this
    parse cannot vouch for.

    THIS IS WHAT THE PARSE ACTUALLY GUARANTEES: no name appears in the output
    that is not a name from a tmux-escaped, delimiter-free listing of real
    session names (call one). It does NOT guarantee a genuine record's `path`
    survives a self-inflicted collision with the >=3-tab heuristic — a path
    containing both a raw newline and three raw tabs still truncates that
    session's own `path` at the newline, because the fragment after it reads
    as a well-formed record of its own and gets split off. THAT TRUNCATION IS
    A DATA-QUALITY COST, NOT A FORGERY, ONLY WHEN THE INJECTED FRAGMENT'S
    PARSED NAME MATCHES NO TRUSTED NAME: the session's `name`, and the ROW it
    appears in, are still real and the only one for that name. When the
    injected fragment's name instead matches an existing trusted name —
    including the session's own name, a self-collision — the RECONCILIATION
    branch above fires instead: `parsed[name]` now holds two entries, the row
    is judged undecidable, and every field but `name` degrades to `null`, the
    same as any other multiply-claimed name.

    `deck-shell-*` IS EXCLUDED from the trusted set, using the same
    `SCRATCH_SHELL_PREFIX` the reaper uses to find them: they are Deck's own
    scratch shells, one per fleet session, and are noise in a list meant for
    grouping conversations under projects.

    DEGRADES, NEVER FAILS. No tmux server and no sessions both read as `[]` —
    neither is exceptional enough to raise from, and this runs on a poll that
    must not error a whole rail over an absent tmux.
    """
    # THE ANCHOR CALL ALSO CARRIES `#{session_id}`, because the id is the field
    # that survives a rename and Deck matches pinned children by it (the same
    # `$<digits>` `session-agent crews` already serves). It rides on this call
    # rather than the record call so a row that degrades below still has it.
    # Splitting on the FIRST tab is safe here: tmux escapes a tab in a name at
    # storage, and an id is `$` plus digits, so neither half can hold one. An id
    # that is not that shape reads as `null` rather than passing through.
    trusted_listed = TT.run(["tmux", "list-sessions", "-F",
                             "#{session_id}\t#{session_name}"])
    if not trusted_listed:
        return {"sessions": []}
    session_ids: dict[str, str | None] = {}
    trusted = []
    for line in trusted_listed.split("\n"):
        raw_id, _, name = line.partition("\t")
        if not name or name.startswith(SCRATCH_SHELL_PREFIX):
            continue
        trusted.append(name)
        # Same shape as session_crews.TMUX_SESSION_ID_RE; keep the two in step.
        session_ids[name] = raw_id if re.fullmatch(r"\$\d{1,9}", raw_id) else None
    if not trusted:
        return {"sessions": []}

    listed = TT.run(["tmux", "list-sessions", "-F",
                     "#{session_name}\t#{session_windows}\t#{session_attached}\t"
                     "#{session_path}"])

    records = []
    if listed:
        current = None
        for line in listed.split("\n"):
            if line.count("\t") >= 3:
                if current is not None:
                    records.append(current)
                current = line
            elif current is not None:
                current += "\n" + line
            # else: nothing three-tabbed has started a record yet — dropped.
        if current is not None:
            records.append(current)

    # name -> every (windows, attached, path) triple parsed under that name.
    # More than one entry means the name cannot be trusted to pick between
    # them; see the reconciliation note above.
    parsed: dict[str, list[tuple[str, str, str]]] = {}
    for record in records:
        # maxsplit=3 always yields exactly four parts here: `record` was only
        # ever added above because it carries at least three tabs. No
        # field-count guard, and no unpack that can raise.
        name, windows, attached, path = record.split("\t", 3)
        if not name:
            continue
        parsed.setdefault(name, []).append((windows, attached, path))

    out = []
    for name in trusted:
        entries = parsed.get(name, [])
        windows_int = None
        if len(entries) == 1:
            raw_windows, attached, path = entries[0]
            # isdigit() alone is not conclusive: it accepts non-ASCII
            # decimal-like characters (e.g. superscript '²') that int()
            # then rejects, which would raise ValueError straight out of a
            # function documented to degrade rather than fail. isascii()
            # closes that gap; the try/except is a second, independent gate
            # in case some other character class slips past both checks —
            # belt and braces, since nothing is meant to escape this parse.
            if raw_windows.isascii() and raw_windows.isdigit():
                try:
                    windows_int = int(raw_windows)
                except ValueError:
                    windows_int = None
        if windows_int is not None:
            out.append({
                "name": name,
                "sessionId": session_ids.get(name),
                "path": path or None,
                "windows": windows_int,
                "attached": attached != "0",
            })
        else:
            out.append({"name": name, "sessionId": session_ids.get(name),
                        "path": None, "windows": None, "attached": None})
    return {"sessions": out}


def _every_pane_is_idle(name: str) -> bool:
    """Whether EVERY pane of a session sits at a bare shell.

    NOT `_idle_shell_pane`, AND THE DIFFERENCE IS THE WHOLE SAFETY OF THE REAP.
    That helper answers a different question — "is there a pane I can type into"
    — so it returns the FIRST bare shell it finds. A session with `sleep 600` in
    one pane and a prompt in the other answers yes to it, and would have been
    killed with the work still running. The reaper needs the opposite
    quantifier: spare unless ALL panes are idle.

    Caught by reviewer. The suite passed with the bug because no test had two
    panes, which is why there is one now.

    Anything unreadable — no output, a malformed row, an id that is not tmux's
    `%N` — returns False and spares the session. A reaper that cannot see what
    is in a pane must not close it.
    """
    out = TT.run(["tmux", "list-panes", "-t", f"={name}", "-F",
                  "#{pane_id} #{pane_current_command}"])
    if not out:
        return False
    seen = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not PANE_ID_RE.fullmatch(parts[0]):
            return False
        if parts[1] not in ADOPTABLE_SHELLS:
            return False
        seen += 1
    return seen > 0


def reap_scratch_shells(idle_seconds: float = SCRATCH_SHELL_IDLE_SECONDS,
                        now: float | None = None) -> dict:
    """Close idle Deck scratch shells. Returns what went and what stayed, with why.

    WHY THIS IS ALL GATES. A reaper that kills a shell with work in it is the
    same defect Deck spent 2026-07-24 removing — a session switch tearing down a
    pane and taking a running command with it. So a shell is closed only when
    every one of these holds, and the report names which gate spared the rest:

      * its name carries `SCRATCH_SHELL_PREFIX`, so a fleet session is not
        merely unlikely to be reaped, it is unaddressable here;
      * NO CLIENT IS ATTACHED, so a pane open in front of the user is never
        closed under them, however long the shell has been quiet;
      * the pane's foreground process is a bare shell — the same reading
        `_idle_shell_pane` uses to decide a session is idle — so a shell running
        anything at all is spared regardless of its activity timestamp;
      * tmux's own `session_activity` is older than `idle_seconds`.

    The activity clock alone would not be enough, which is the point of the
    third gate: `session_activity` tracks OUTPUT, so a long silent command — a
    build, a sleep, a `tail -f` waiting on a quiet log — looks exactly like an
    abandoned prompt to it.
    """
    # A THRESHOLD THAT CANNOT BE COMPARED IS NOT A LENIENT THRESHOLD, IT IS NO
    # THRESHOLD. `idle < nan` is False, so a NaN falls THROUGH the age gate to
    # the kill rather than sparing — the opposite of what a caller passing a
    # nonsense value would expect, and the opposite of how every other unreadable
    # input here is treated. A negative value reaps everything for the same
    # reason. Both are refused rather than clamped, so the caller learns.
    # Raised by security-reviewer.
    if not math.isfinite(idle_seconds) or idle_seconds < 0:
        raise LifecycleError(
            f"idle_seconds must be a non-negative number, got {idle_seconds!r}")

    listed = TT.run(["tmux", "list-sessions", "-F",
                     "#{session_name} #{session_attached} #{session_activity}"])
    if not listed:
        # No tmux, or no sessions. Both mean nothing to reap, and neither is
        # exceptional enough to raise from — this runs unattended.
        return {"ok": True, "reaped": [], "spared": []}

    clock = time.time() if now is None else now
    reaped, spared = [], []
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) != 3 or not SCRATCH_SHELL_NAME_RE.fullmatch(parts[0]):
            continue
        name, attached, activity = parts[0], parts[1], parts[2]
        if attached != "0":
            spared.append({"session": name, "why": "a client is attached"})
            continue
        # THE CLOCK GATES COME BEFORE THE PANE READ, which is the order the
        # docstring claims and the first draft did not have: comparing two
        # integers already parsed from `list-sessions` is free, while reading the
        # panes is a subprocess per session. A shell used five minutes ago is
        # spared by age alone, so inspecting it costs a process for nothing —
        # every run, for every active shell. Raised by reviewer.
        if not activity.isdigit():
            # An unreadable timestamp is a reason NOT to act. The pass is
            # advisory; a session it cannot judge keeps its shell.
            spared.append({"session": name, "why": "activity time unreadable"})
            continue
        idle = clock - int(activity)
        if idle < idle_seconds:
            spared.append({"session": name, "why": f"idle only {int(idle)}s"})
            continue
        if not _every_pane_is_idle(name):
            spared.append({"session": name, "why": "something is running in it"})
            continue
        if TT.run(["tmux", "kill-session", "-t", f"={name}"]) is None:
            spared.append({"session": name, "why": "tmux would not close it"})
            continue
        reaped.append({"session": name, "idle_seconds": int(idle)})

    return {"ok": True, "reaped": reaped, "spared": spared}


def stop(name: str, kill: bool = False) -> dict:
    """End the agent in a session, and optionally the session itself.

    TWO HALVES, NAMED SEPARATELY. Ending the agent is recoverable — the tmux
    session stays and another agent can be started in it. Killing the session
    destroys its scrollback and anything else running in it, so it happens only
    when asked for explicitly.

    An absent session is not an error: the caller wanted it gone and it is gone.

    ASSUMES CLAUDE CODE IS IN THE PANE. `/exit` is sent blind. To a bare shell
    it is a path that does not exist and the shell says so; to vim or a REPL it
    is harmless noise. Nothing common is damaged by it, but a caller that
    started something else in the session will see `exit_sent` true and
    `agent_gone` false and should know why rather than deduce it.
    """
    TT.validate_session_name(name)
    if not session_exists(name):
        # The same keys as every other path. An asymmetric shape means a caller
        # reading `result["exit_sent"]` gets a KeyError on exactly the branch it
        # was least likely to test. Caught in review.
        return {"ok": True, "session": name, "existed": False,
                "exit_sent": False, "agent_gone": True, "killed": False}

    exit_sent = False
    try:
        # `/exit` through the same paste path a message takes, so the agent
        # shuts down the way it does for a human rather than being signalled.
        SI.send_message(name, "/exit")
        exit_sent = True
    except (SI.InputError, TT.JoinError):
        # No agent in the pane, or it cannot be addressed. Not a failure: a
        # session with nothing running in it is already in the state `stop`
        # is trying to reach.
        pass

    # WAITING IS THE POINT OF THE GENTLE STOP, and the first version did not.
    # `send_message` settles the paste for 0.4s and returns; it does not wait for
    # Claude Code to process `/exit` and shut down. So `--kill` destroyed the
    # session while the agent was still exiting — "stop the agent, then kill the
    # session" was really "signal it and pull the floor away". Caught in review.
    agent_gone = _await_agent_exit(name) if exit_sent else True

    if not kill:
        return {"ok": True, "session": name, "existed": True,
                "exit_sent": exit_sent, "agent_gone": agent_gone, "killed": False}

    if TT.run(["tmux", "kill-session", "-t", f"={name}"]) is None:
        raise LifecycleError(f"could not kill session {name!r}")
    # `agent_gone` is reported rather than enforced: an agent that would not
    # leave inside the window is a thing the caller should be able to see, and
    # the session is gone either way because that is what was asked for.
    return {"ok": True, "session": name, "existed": True,
            "exit_sent": exit_sent, "agent_gone": agent_gone, "killed": True}


def _await_agent_exit(name: str, timeout: float = EXIT_WAIT_SECONDS) -> bool:
    """Whether the agent left before the timeout.

    Polls for the claude process rather than trusting the paste: `claude_pane`
    raising JoinError is the same signal `state` uses to say a session has no
    agent, so this agrees with what Deck will read a moment later.

    Deliberately resolves its own session table fresh on every poll rather
    than fetching one once for the whole wait: this loop exists to watch a
    session DISAPPEAR, so a table cached across iterations would keep
    reporting a gone session as alive for the rest of the timeout window.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            TT.claude_pane(name)
        except TT.JoinError:
            return True
        time.sleep(EXIT_POLL_SECONDS)
    return False
