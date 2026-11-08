"""The environment for any subprocess that starts a claude process or an agent
run (issue_01M2EG40BSYN7MXM2EWBT9VKBN).

`tmux` has two independent ways to find the dispatcher's live server, and a
dispatched run must lose both:

1. **The inherited pointer.** `tmux` 3.6a resolves `$TMUX`'s socket ahead of
   `$TMUX_TMPDIR`, so a spawned process that inherits the dispatcher's `TMUX`
   can still reach the dispatcher's live tmux server even when something
   points `TMUX_TMPDIR` elsewhere. Stripping `TMUX`/`TMUX_PANE` closes this.
2. **The same-UID default socket.** With `TMUX` and `TMUX_TMPDIR` both absent,
   `tmux` falls back to `$TMPDIR/tmux-$(id -u)/default` (`/tmp/tmux-<uid>/
   default` in practice) — the socket every real co session runs on
   (`session_lifecycle.py`'s `tmux new-session -d -s <name>` never passes
   `-L`/`-S`). Stripping `TMUX` alone still leaves this route open: a
   dispatched run's unqualified `tmux kill-server` resolves there with nothing
   inherited at all. Pointing `TMUX_TMPDIR` at a private directory this
   module owns closes it, because that directory can never be the operator's
   default socket dir.

A detached/dispatched run has no pane and never belongs to either of the
dispatcher's tmux identities — on 2026-09-13 a dispatched coder's bare `tmux
kill-server` experiments reached the operator's live server and killed every
co session on the machine, because `agentctl`'s spawn env carried
`TMUX`/`TMUX_PANE` straight through from `{**os.environ}` (closed by route 1
above) and, on the follow-up round, because nothing closed route 2 either.

Every dispatch to a repo also carries the object-store redirect (knowledge#793 round 6):
`GIT_OBJECT_DIRECTORY` pointed at the run's own private object store and
`GIT_ALTERNATE_OBJECT_DIRECTORIES` at the shared one. `run_sandbox`'s `objects` under a
shared git directory is now denied wholesale — rounds 3-5 narrowed that grant by name
(`objects/info`, an enumerated pack/index file, then `objects/pack` wholesale) and each
round's reviewer found the next name still open, down to the structural gap rounds 3-5
never closed: an EXISTING loose object could be deleted (unlinking only needs write
permission on its fanout directory, not the 0444 file itself) and recreated with
different content under the same hash-named path, which no ordinary git read ever
catches. Rather than find a fifth name to narrow, the run's own git now writes new
objects somewhere else entirely — `run_sandbox.private_objects_dir` — and reads the
shared store only as an alternate, so nothing it does ever needs write access to
`objects` at all. `run_sandbox.import_private_objects` is how those writes reach the
shared store once the run has ended: through git's own verified transfer (`pack-objects`
+ `index-pack`), never a file copy. The round-5 settings that kept a run's own fetch to
loose objects (`transfer.unpackLimit`/`fetch.unpackLimit` raised, `gc.auto`/
`maintenance.auto` off) are dropped — moot now that the run's writes land in a private
directory nothing else ever reads automatically: a pack landing there is exactly as safe
as loose objects would be, since `import_private_objects` enumerates and transfers
whichever shape the run produced.

BOTH VARS ARE PROCESS-WIDE (issue_01M4QXDGVE43R2WYT01VDQQBPY), so without
`_private_git_wrapper_dir`'s wrapper below, they redirect every git invocation the run
makes, not only the ones against its own repository: `git -C <another checkout> ...`
reads only from this run's own private store and its own repository's shared `objects`,
and gets "bad object"/"missing object" for content that is right there in the other
repository's real store. Reproduced on 2026-10-12 by two live runs reading a sibling
checkout and by a reviewer who fell back to a stale working tree and raised a false
blocking finding over it. `dispatch_env` closes this by putting a `git` wrapper first on
the run's PATH (`AGENTCTL_REAL_GIT`/`AGENTCTL_OWN_GIT_COMMON_DIR`, set alongside the two
redirect vars): it resolves the invocation's OWN target repository the same way git
itself would (honoring any leading `-C`/`--git-dir`/`--work-tree`/`-c`/etc. before the
subcommand) and drops both vars before exec'ing the real git whenever that target isn't
the run's own repository. A per-repository config equivalent doesn't exist for this: git
has no config key for where a repository's objects live (only `GIT_OBJECT_DIRECTORY`, or
the implicit `$GIT_DIR/objects`), and mutating the target repository's own directory
structure to redirect it per-run would persist past the run and race a sibling run or
worktree sharing the same common git directory — the wrapper changes nothing on disk.

`git init` and `git clone` are a second case the same resolve-and-compare can't answer,
because the repository being probed doesn't exist yet (issue_01M4R8KTY7THR8HYGJA849N3AH):
`git init -q --bare <scratch>/remote.git` under the redirect wrote the new repository's
objects into the run's own private store, not `<scratch>/remote.git/objects` — the
leftover directory held only `config description HEAD hooks info refs` — and a push to
it then failed with "does not appear to be a git repository". The wrapper treats `init`
and `clone` as always creating a repository foreign to the run's own, and drops both
redirect vars unconditionally for them, without running the rev-parse probe at all: a
plain `git init`/`git clone` that happens to recreate the run's own repository in place
is not a case this supports, and gets the same foreign-repo treatment as any other target.

`dispatch_env(deny_push=True)` closes a second, unrelated gap
(issue_01M3MMJFKSE5ZCA5YNXCJ1YCAK): a reviewer or security-reviewer run
inherits the operator's git credential, which can push, and the roster's
read-only tool list is a policy record, not an enforcement — on 2026-09-28 a
`cd` into a not-yet-created scratch directory failed, the shell stayed in the
reviewer's own worktree, and the `git push` meant for a throwaway repo pushed
a stray branch to the real convivy/agent-platform instead, twice, on PR #698.
`deny_push` rewrites a push to a network URL prefix (https, http, git, ssh,
or the `git@host:` scp shorthand) to a URL scheme with no registered git
remote helper, so git refuses before opening a connection — no DNS, no TCP,
nothing for a test to mock or an offline run to hang on — except to a remote
configured with an explicit `remote.<name>.pushurl`, which git does not
rewrite with `pushInsteadOf`. Only `pushInsteadOf` is set,
never `insteadOf`, so `git fetch`/`git log`/`git diff` against the real
remote are untouched, and a push to a local path (a scratch bare repo in a
test, say) matches none of the rewritten prefixes and still works.

Every dispatch also gets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and
`BASH_DEFAULT_TIMEOUT_MS=600000`, closing a third gap (issue_01M3N2CFM6D17GD7B5TWM323F3): a
dispatched run whose Bash test command outlasts its timeout gets that
command moved to the background, ends its turn to wait on it, and never
resumes, leaving its work uncommitted — measured seven times (2026-09-12,
-13, -25, and four times on 2026-09-28), twice despite a brief forbidding
it. Measured on Claude Code 2.1.284: a `sleep 130` Bash call given timeout
125000 was refused and the model pointed at run_in_background/Monitor with
neither var set; with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` set, the same
call ran in the foreground and was killed at the timeout (exit 143).
`BASH_DEFAULT_TIMEOUT_MS` sets the timeout a Bash call gets when it passes
none (the 2026-09-12 and -13 runs passed none, so they ran under the
120000 default). Both are overwritten even when *base* already carries a
value for either key, since a dispatched run must not inherit an
interactive session's own choice.

A fourth gap (CQ·418, 2026-10-07): a run's own `git credential fill` reached the
operator's live `gh` token — twice, in two security-reviewer transcripts — through
`~/.gitconfig`'s `credential.https://github.com.helper = !gh auth git-credential` and
the Xcode system gitconfig's `osxkeychain` helper. Every dispatch now unconditionally
points `GH_CONFIG_DIR` at an empty per-run directory and `GIT_CONFIG_GLOBAL`/
`GIT_CONFIG_SYSTEM` at `/dev/null` (with `GIT_CONFIG_NOSYSTEM=1` and
`GIT_TERMINAL_PROMPT=0`), so neither config file is ever read, and carries this run's
own read-only `github_token` (minted by the dispatcher, never a credential this run's
own process ever holds the signing key for) as `GH_TOKEN`/`GITHUB_TOKEN` — see
`dispatch_env`'s own docstring for the full mechanism, including the one case env
redirection alone does not close (`-c credential.helper=...` on the command line) and
why the minted token closes it anyway.

A fifth gap (2026-10-21, deck-coder run-ccc745b2): SwiftPM wraps `Package.swift`
manifest and plugin compilation in its own `sandbox-exec`, and macOS refuses a sandbox
applied inside a sandbox — the run is already under the run sandbox (agent-platform PR
821 measured the same refusal). A plain `swift build`/`swift test` inside a dispatched
run fails with `sandbox-exec: sandbox_apply: Operation not permitted` while compiling
the manifest; `--disable-sandbox` on the `swift` invocation avoids it, and disabling
SwiftPM's OWN sandbox denies this run's manifest/plugin code nothing it didn't already
have: the nested sandbox could never apply in the first place, and the run sandbox still
confines everything SwiftPM runs — EXCEPT the network, which the run sandbox allows to
a 4-host allowlist (`run_sandbox.NETWORK_ALLOWED`: github.com, api.github.com, pypi.org,
files.pythonhosted.org) rather than denying outright the way SwiftPM's own sandbox did
(agent-platform PR 965 round-1 security finding). Manifest/plugin code — the run's own
package, or any dependency's — now runs with that allowance in place, in the same
process environment that carries this run's live `GH_TOKEN`/`GITHUB_TOKEN` (the fourth
gap's own read-only installation token, scoped to this run's one repository) and, for a
gateway-model dispatch, `ANTHROPIC_API_KEY` (`bin/agentctl`'s `gateway_env` — a
spend-capable LiteLLM virtual key): reading its own environment and exfiltrating what
it reads over that allowance (the token itself, or anything it can read from that one
repository) is not something either sandbox stops. SwiftPM ships no environment variable or
config setting for `--disable-sandbox` itself — `SWIFTC_DISABLE_SANDBOX` exists but
governs `swiftc`'s own sandboxing, not the manifest/plugin sandbox, and measured to have
no effect on the failure above; `--disable-sandbox` is the only knob that does (verified
by running both against the same minimal package manifest on 2026-10-21). `dispatch_env`
puts a `swift` wrapper first on every run's PATH, next to the git wrapper: it adds
`--disable-sandbox` to `build`, `test`, `run`, and `package` (every `package`
subcommand, since all of them load the manifest) when the caller hasn't already passed
it, and passes every other invocation through unchanged. Before any exec, on EVERY
invocation through it (not only the ones `--disable-sandbox` applies to), it also
unsets `GH_TOKEN`, `GITHUB_TOKEN`, and `ANTHROPIC_API_KEY` from the real `swift`
process's own environment (`_SWIFT_WRAPPER_STRIPPED_CREDENTIALS`, below) — the three
credentials dispatch_env()/bin/agentctl set for a run that manifest/plugin code has no
legitimate reason to read. Neither is needed for a dependency fetch: SwiftPM resolves a
git dependency by shelling out to `git`, which this same dispatch already points at
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM=/dev/null` with `GIT_CONFIG_NOSYSTEM=1` (the
fourth gap, above) — no file-based `credential.helper` is ever consulted in this
environment regardless, and SwiftPM's own git invocation passes no
`-c credential.helper=...` of its own, so there is no route by which a swift dependency
fetch would have used either var even before this strip. The wrapper otherwise follows
the git wrapper's exact discipline for the same reason the OUTAGE comments above
`_GIT_WRAPPER_SCRIPT` give: `AGENTCTL_REAL_SWIFT`, never a bare-name PATH lookup, with
an absolute path baked in at wrapper-write time as the fallback, and a marker line so a
copy of this wrapper is never mistaken for the real `swift` binary.
"""
from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

# The two vars that name the dispatcher's own tmux server + pane. Nothing else
# is stripped — every other ambient var still reaches the child as before.
_STRIP = ("TMUX", "TMUX_PANE")

# The crew ops credential's path (bin/co names it only in the --mcp-config of a crew
# co claude, and bin/mcp-ops-server reads it — platform/contracts/library-protocol
# §2.5). `agentctl spawn` builds its env from the calling shell, so a co's own path
# exported there would reach every dispatched run and let it hold the co's
# `item.create` grant; this strip keeps it out. `_dispatch_mcp_config_json` names a
# dispatched run its OWN crew's non-co file instead, which never reaches this strip
# since it is set directly in the run's `--mcp-config`, never inherited from the
# calling shell's environment. The path is not a secret. The dispatch sandbox
# (agent-platform#793) denies read on the whole `.orient-control` directory the file
# lives in, so a dispatched run cannot read it either. Kept apart from _STRIP because
# that tuple's reason is the tmux identity.
_CO_ONLY = ("ORIENT_OPS_CREW_TOKEN_FILE",)

# A URL scheme with no registered `git-remote-<scheme>` helper, so a push
# rewritten to it fails immediately with "unable to find remote helper" —
# before any network I/O. Must be a valid URI scheme (letters/digits/+/-/.,
# starting with a letter): RFC 3986's scheme grammar, which git's helper
# lookup follows.
_PUSH_REFUSAL_BASE = "agentctl-reviewer-no-push://"

# Every prefix a network git remote can start with: https/http/git URLs,
# ssh:// URLs, and the `git@host:path` scp-like shorthand (the form GitHub's
# own ssh clone URLs use). A local path (plain, `file://`, or a bare repo
# under /tmp) starts with none of these, so `git push` to one is unaffected.
_PUSH_REFUSAL_PREFIXES = ("https://", "http://", "git://", "ssh://", "git@")

# Lazily created, at most once per process, and removed at that process's
# exit. macOS caps a unix socket path at ~104 bytes and `tmux` appends
# "tmux-<uid>/default" to whatever TMUX_TMPDIR names, so this must be short.
# Measured: a run's own record directory under the platform's data root
# (`builder_root.runs_root()`'s `agent-runs/<date>/<run-id>/tmux-<uid>/
# default`) comes to 84 bytes on this machine alone, with no headroom for a
# longer $HOME or run-id; `/tmp/tmuxd-<8 chars>/tmux-<uid>/default` comes to 36.
_tmux_tmpdir: str | None = None


def _private_tmux_tmpdir() -> str:
    """A short-lived directory, private to this process, for TMUX_TMPDIR.

    One per process rather than one per call: every `dispatch_env()` call in
    a given process is dispatching on behalf of the same run (a respawn/retry
    loop, or a long-lived driver launching several children in turn), so they
    share one identity and one cleanup.

    `atexit` cleans this up ONLY for a process that exits normally (returns
    from `main()`, calls `sys.exit()`, or raises) — `model-scorecard` exits
    this way. The daemonized grandchild
    (`_detach_and_run`'s) instead calls `os._exit()` as
    its normal, expected termination — not just under `SIGKILL` — and
    `os._exit()` skips `atexit` entirely, so this registration never fires
    for it. That call site must invoke `cleanup_private_tmux_tmpdir()`
    explicitly right before `os._exit()`. A `SIGKILL`ed process (on any path)
    still skips both and leaks an empty directory under `/tmp`, which is
    inert (no tmux server is ever meant to start there) and harmless clutter.
    """
    global _tmux_tmpdir
    if _tmux_tmpdir is None:
        # dir="/tmp" explicitly: `tempfile.gettempdir()` resolves to
        # `$TMPDIR` first, which on macOS is a long per-user path
        # (`/var/folders/<hash>/<hash>/T`, 48 bytes measured on this
        # machine) — 65 bytes once `tmux` appends "tmux-<uid>/default",
        # already most of the ~104-byte budget before the mkdtemp suffix.
        _tmux_tmpdir = tempfile.mkdtemp(prefix="tmuxd-", dir="/tmp")
        atexit.register(shutil.rmtree, _tmux_tmpdir, ignore_errors=True)
    return _tmux_tmpdir


def cleanup_private_tmux_tmpdir() -> None:
    """Remove this process's private TMUX_TMPDIR directory, if `dispatch_env()`
    ever created one; a no-op otherwise. Idempotent, and never raises.

    Call this explicitly right before any `os._exit()` on a path that may
    have called `dispatch_env()` — `os._exit()` skips the `atexit` cleanup
    `_private_tmux_tmpdir()` registers, so those paths have no other cleanup.
    """
    global _tmux_tmpdir
    if _tmux_tmpdir is not None:
        shutil.rmtree(_tmux_tmpdir, ignore_errors=True)
        _tmux_tmpdir = None


# Same one-per-process, atexit-cleaned shape as `_tmux_tmpdir` above, for `GH_CONFIG_DIR`
# (CQ·418): a dispatched run's `gh` must read no `hosts.yml` of its own — an empty
# directory with no config in it reads as "no stored host", never the operator's.
_gh_config_dir: str | None = None


def _private_gh_config_dir() -> str:
    """A short-lived, empty directory, private to this process, for GH_CONFIG_DIR.

    One per process, for the same reason `_private_tmux_tmpdir` is: a respawn/retry
    inside one run shares one identity. `cleanup_private_gh_config_dir` is the
    `os._exit()`-path counterpart, exactly as `cleanup_private_tmux_tmpdir` is for
    the tmux directory — see that function's docstring for why `atexit` alone is not
    enough."""
    global _gh_config_dir
    if _gh_config_dir is None:
        _gh_config_dir = tempfile.mkdtemp(prefix="ghcfg-", dir="/tmp")
        atexit.register(shutil.rmtree, _gh_config_dir, ignore_errors=True)
    return _gh_config_dir


def cleanup_private_gh_config_dir() -> None:
    """Remove this process's private GH_CONFIG_DIR directory, if `dispatch_env()`
    ever created one; a no-op otherwise. Idempotent, and never raises. Call this
    explicitly right before any `os._exit()`, alongside `cleanup_private_tmux_tmpdir`
    — see that function's docstring for why."""
    global _gh_config_dir
    if _gh_config_dir is not None:
        shutil.rmtree(_gh_config_dir, ignore_errors=True)
        _gh_config_dir = None


# A fixed line every copy of the wrapper script carries, so one copy can tell another
# copy apart from the real git binary without comparing inodes (the OUTAGE paragraph
# below needs this: a SECOND run's wrapper, a different file from this process's own
# $0, still has to be recognized as "a wrapper", not "the real git").
_GIT_WRAPPER_MARKER = "AGENTCTL_GIT_WRAPPER_MARKER_V1"

# The wrapper that scopes the object-store redirect to the run's own repository
# (issue_01M4QXDGVE43R2WYT01VDQQBPY) — see the module docstring's "BOTH VARS ARE
# PROCESS-WIDE" paragraph for the incident and why this is the fix. bash 3.2
# (launchd-critical) compatible: no declare -A, mapfile, ${var,,}. `AGENTCTL_REAL_GIT`
# and `AGENTCTL_OWN_GIT_COMMON_DIR` are set alongside it in `dispatch_env`, below.
#
# OUTAGE, 2026-10-12: `AGENTCTL_REAL_GIT` is meant to always name the real git binary,
# but `tests/bash/test_git_object_redirect_wrapper_scopes_to_own_repo.sh` resolved "real"
# git with `command -v git` — which, run from INSIDE a dispatched run (this wrapper is
# first on that run's own PATH), resolved to the run's own wrapper instead — and handed
# that back in as AGENTCTL_REAL_GIT. The probe below, `$("$real_git" ... rev-parse ...)`,
# is a command substitution: every level forks a NEW process that runs this same script
# again, with the same self-referential AGENTCTL_REAL_GIT still in its environment, which
# forks the next level the same way. Nothing bounded the chain: two runs reached 9,256
# bash processes between them before the host's per-user process limit stopped it. The
# check below runs BEFORE any exec or probe, so a self-referential AGENTCTL_REAL_GIT is
# refused on the very first invocation — zero forks, not a bounded few — whether it
# names this exact file ($0, the "-ef" branch below) or a different file carrying this
# same marker line (a different run's wrapper copy; the marker is how that's told apart
# from the real git binary, which carries no such line). This is NOT an exported
# "already inside the wrapper" flag: git legitimately re-invokes `git` through PATH on
# its own (hooks, submodules, `!`-aliases), and those nested invocations reach this
# wrapper again with a CORRECT AGENTCTL_REAL_GIT and must keep working — a flag that
# refused every nested call would break them. The refusal is about what AGENTCTL_REAL_GIT
# resolves to, never about whether this script has already run once in this process tree.
#
# OUTAGE, 2026-10-17 08:11-08:25 MST: a test in another repository took
# `shutil.which("git")` as "the real git", which inside a dispatched run resolved to
# THIS wrapper, then ran a subprocess with an env of only `PATH=<dir holding a git
# stub>`. With AGENTCTL_REAL_GIT unset, the script below fell back to the bare name
# `"git"` and resolved IT through that PATH too, landing on the stub; the stub called
# `git` again, which was this wrapper again, each generation forking the next, until
# the operator's account hit its 10,122-of-10,666 process ceiling. A self-referential
# AGENTCTL_REAL_GIT (the 2026-10-12 shape above) is only one way a bare-name PATH
# lookup can land back on a wrapper or a stub that calls one; the actual fix is to
# never do that lookup here at all. `AGENTCTL_REAL_GIT`, when unset, now falls back to
# an absolute path baked into this exact script at write time (`_private_git_wrapper_
# dir`, resolved the same wrapper-skipping way `AGENTCTL_REAL_GIT` itself is), and the
# script refuses outright — before any exec or probe — whenever neither value is an
# absolute path to an executable file, rather than ever asking `$PATH` what `git` means.
_GIT_WRAPPER_SCRIPT = """#!/bin/bash
# @@GIT_WRAPPER_MARKER@@
# Placed first on a dispatched run's PATH: the run's object-store redirect
# (GIT_OBJECT_DIRECTORY / GIT_ALTERNATE_OBJECT_DIRECTORIES) is a process-wide env var,
# so without this wrapper every git invocation the run makes -- `git -C <another
# checkout> ...` included -- reads objects only from this run's own private store and
# its own repository's shared objects directory. This wrapper drops both vars before
# exec'ing the real git whenever the invocation's own target repository (resolved
# honoring the same leading global options git itself would apply before its
# subcommand: -C, --git-dir, --work-tree, -c, --namespace, --super-prefix,
# --config-env, --exec-path, --attr-source, plus the boolean globals that can affect
# discovery) is not AGENTCTL_OWN_GIT_COMMON_DIR. `init` and `clone` always create a
# repository rather than resolve an existing one, so they get the same foreign-repo
# treatment unconditionally, with no rev-parse probe: a plain `init`/`clone` that
# happens to recreate the run's own repository in place is not a case this supports.
# See dispatch_env.py's module docstring for the mechanism and the incident.

real_git="$AGENTCTL_REAL_GIT"
own_common_dir="$AGENTCTL_OWN_GIT_COMMON_DIR"

if [ -z "$real_git" ]; then
    # Baked in at wrapper-write time by dispatch_env.py's _private_git_wrapper_dir, from
    # THAT call's own ambient PATH (the wrapper file is written once per process, unlike
    # AGENTCTL_REAL_GIT, which dispatch_env() re-resolves from the per-call PATH on every
    # call in that process) -- see the second OUTAGE comment above _GIT_WRAPPER_SCRIPT for
    # why this is a baked absolute path rather than another PATH lookup here. shlex.quote()
    # escaped by the Python side, so a path holding a space or a shell metacharacter
    # round-trips as one literal value, never interpreted. Empty (`''`) when that
    # resolution itself found nothing usable.
    real_git=@@BAKED_REAL_GIT@@
fi

# Never exec, probe, or resolve a bare command name through $PATH here -- see the second
# OUTAGE comment above _GIT_WRAPPER_SCRIPT in dispatch_env.py: that is exactly how a stub
# calling back into "git" turned into an unbounded fork chain. $real_git must already be
# an absolute path to an executable file; anything else is refused before any exec or
# probe below, whether AGENTCTL_REAL_GIT was unset and the baked fallback above is empty
# or non-absolute, or AGENTCTL_REAL_GIT itself was.
case "$real_git" in
    /*) ;;
    *)
        echo "agentctl git wrapper: no usable real git binary (AGENTCTL_REAL_GIT is unset or not an absolute path, and the baked fallback is empty or not an absolute path) -- refusing instead of resolving a bare 'git' through PATH (see dispatch_env.py's _GIT_WRAPPER_SCRIPT docstring)" >&2
        exit 127
        ;;
esac

if [ ! -f "$real_git" ] || [ ! -x "$real_git" ]; then
    echo "agentctl git wrapper: real git path '$real_git' is not an executable file -- refusing instead of resolving a bare 'git' through PATH (see dispatch_env.py's _GIT_WRAPPER_SCRIPT docstring)" >&2
    exit 127
fi

# Refuse before any exec or probe below if $real_git resolves to a copy of THIS wrapper
# rather than the real git binary -- see the first OUTAGE comment above _GIT_WRAPPER_SCRIPT
# in dispatch_env.py for the incident and why this has to run first, unconditionally, and
# cannot be an exported "already inside the wrapper" flag.
_agentctl_is_wrapper_copy() {
    # True if $1 IS a copy of this wrapper: the same file as this script ($0 -- the
    # direct self-reference the 2026-10-12 incident hit), or any OTHER file carrying
    # this script's marker line (a different run's /tmp/gitwrap-*/git). Never true for
    # the real git binary, which carries no such line.
    [ -f "$1" ] || return 1
    if [ "$1" -ef "$0" ]; then
        return 0
    fi
    grep -q '@@GIT_WRAPPER_MARKER@@' "$1" 2>/dev/null
}

if _agentctl_is_wrapper_copy "$real_git"; then
    echo "agentctl git wrapper: AGENTCTL_REAL_GIT ('$real_git') resolves to a copy of this wrapper, not the real git binary -- refusing instead of recursing into it (see dispatch_env.py's _GIT_WRAPPER_SCRIPT docstring)" >&2
    exit 127
fi

if [ -z "$GIT_OBJECT_DIRECTORY" ] && [ -z "$GIT_ALTERNATE_OBJECT_DIRECTORIES" ]; then
    exec "$real_git" "$@"
fi

if [ -z "$own_common_dir" ]; then
    # The redirect is set but nothing told us what it's relative to -- leave it as the
    # dispatcher set it rather than guess (fail closed to the pre-fix behaviour, never
    # widen access).
    exec "$real_git" "$@"
fi

# Collect the leading global options -- git's own grammar always puts these before the
# subcommand -- so the probe below asks about the SAME repository this invocation
# itself would use. `-C` is the form the reported failures actually used.
probe_args=()
argv=("$@")
n=${#argv[@]}
i=0
while [ "$i" -lt "$n" ]; do
    arg="${argv[$i]}"
    case "$arg" in
        -C|--git-dir|--work-tree|--namespace|--super-prefix|--exec-path|--attr-source|-c|--config-env)
            probe_args+=("$arg")
            i=$((i + 1))
            if [ "$i" -lt "$n" ]; then
                probe_args+=("${argv[$i]}")
                i=$((i + 1))
            fi
            ;;
        --git-dir=*|--work-tree=*|--namespace=*|--super-prefix=*|--exec-path=*|--attr-source=*|--config-env=*)
            probe_args+=("$arg")
            i=$((i + 1))
            ;;
        --bare|--no-replace-objects|--no-lazy-fetch|--no-optional-locks|--no-advice|--literal-pathspecs|--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs|-p|--paginate|-P|--no-pager)
            probe_args+=("$arg")
            i=$((i + 1))
            ;;
        *)
            break
            ;;
    esac
done

# `init`/`clone` always create a repository rather than resolve an existing one -- a
# `git init <path>`/`git clone <src> <dst>` issued from the run's own worktree carries no
# -C/--git-dir of its own, so without this check the probe below would resolve the run's
# own repository and leave the redirect in place, and the new repository's objects would
# land in the run's private store instead of its own `<path>/objects`. Treat the target as
# foreign unconditionally, with no rev-parse probe: a plain init/clone that recreates the
# run's own repository in place is not a case this supports.
subcommand=""
if [ "$i" -lt "$n" ]; then
    subcommand="${argv[$i]}"
fi
if [ "$subcommand" = "init" ] || [ "$subcommand" = "clone" ]; then
    exec env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES "$real_git" "$@"
fi

target_common_dir=$("$real_git" "${probe_args[@]}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
probe_status=$?

if [ "$probe_status" -ne 0 ] || [ -z "$target_common_dir" ]; then
    # No resolvable repository at all (not inside one, or the real invocation below
    # will fail for the same reason) -- nothing to compare, so leave the redirect in
    # place.
    exec "$real_git" "$@"
fi

if [ "$target_common_dir" = "$own_common_dir" ]; then
    exec "$real_git" "$@"
fi

exec env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES "$real_git" "$@"
""".replace("@@GIT_WRAPPER_MARKER@@", _GIT_WRAPPER_MARKER)

# Same one-per-process, atexit-cleaned shape as `_tmux_tmpdir` above, for the directory
# holding the wrapper script.
_git_wrapper_dir: str | None = None


def _private_git_wrapper_dir() -> str:
    """A short-lived directory, private to this process, holding the `git` wrapper
    script (`_GIT_WRAPPER_SCRIPT`) that scopes the object-store redirect to the run's
    own repository. One per process, for the same reason `_private_tmux_tmpdir` is.
    `cleanup_private_git_wrapper_dir` is the `os._exit()`-path counterpart — see
    `_private_tmux_tmpdir`'s docstring for why `atexit` alone is not enough.

    The script's `@@BAKED_REAL_GIT@@` fallback (the second OUTAGE comment above
    `_GIT_WRAPPER_SCRIPT`) is resolved HERE, once, from THIS call's own ambient
    `os.environ["PATH"]` — not the per-call PATH `dispatch_env()` resolves
    `AGENTCTL_REAL_GIT` from for `env["PATH"]` below, because this function only ever
    runs once per process (the `_git_wrapper_dir is None` guard) while a later
    `dispatch_env()` call in the same process can pass a different `base`/PATH each
    time; the written file has to carry one fixed fallback regardless. `AGENTCTL_REAL_GIT`
    itself, set fresh on every `dispatch_env()` call, is what actually answers each
    call's own PATH — the baked value only matters for a child that scrubs its
    environment down to bare `PATH` and loses `AGENTCTL_REAL_GIT` entirely (the
    2026-10-17 incident this closes). `shlex.quote()`-escaped so a path holding a
    space or a shell metacharacter can't break the script or inject into it; empty
    (`_resolve_real_git` found nothing) is quoted to `''`, which the script's own
    absolute-path check then refuses."""
    global _git_wrapper_dir
    if _git_wrapper_dir is None:
        _git_wrapper_dir = tempfile.mkdtemp(prefix="gitwrap-", dir="/tmp")
        baked_real_git = _resolve_real_git(os.environ.get("PATH", os.defpath), _git_wrapper_dir)
        script = _GIT_WRAPPER_SCRIPT.replace("@@BAKED_REAL_GIT@@", shlex.quote(baked_real_git))
        wrapper_path = Path(_git_wrapper_dir) / "git"
        wrapper_path.write_text(script)
        wrapper_path.chmod(0o755)
        atexit.register(shutil.rmtree, _git_wrapper_dir, ignore_errors=True)
    return _git_wrapper_dir


def cleanup_private_git_wrapper_dir() -> None:
    """Remove this process's private git-wrapper directory, if `dispatch_env()` ever
    created one; a no-op otherwise. Idempotent, and never raises. Call this explicitly
    right before any `os._exit()`, alongside `cleanup_private_tmux_tmpdir` — see that
    function's docstring for why."""
    global _git_wrapper_dir
    if _git_wrapper_dir is not None:
        shutil.rmtree(_git_wrapper_dir, ignore_errors=True)
        _git_wrapper_dir = None


# How many bytes of a candidate `git` to read when checking it for the wrapper marker
# below — the marker sits on the wrapper script's second line, so a multi-megabyte real
# git binary never needs reading past this to answer "no".
_MARKER_PROBE_BYTES = 4096


def _is_git_wrapper_copy(path: str) -> bool:
    """True if the file at `path` carries `_GIT_WRAPPER_MARKER` — a copy of
    `_GIT_WRAPPER_SCRIPT` (this process's own wrapper, or a DIFFERENT run's
    `/tmp/gitwrap-*/git` already sitting on an inherited PATH), never the real git
    binary, which carries no such line."""
    try:
        with open(path, "rb") as f:
            head = f.read(_MARKER_PROBE_BYTES)
    except OSError:
        return False
    return _GIT_WRAPPER_MARKER.encode() in head


def _resolve_real_git(path: str, wrapper_dir: str) -> str:
    """The real `git` binary reachable from `path`, skipping `wrapper_dir` itself AND
    any other PATH entry whose `git` is itself a copy of this module's wrapper
    (`_is_git_wrapper_copy`) — not only the one directory this call's own caller just
    created. `wrapper_dir` names that one directly (cheaper than reading its freshly
    written file back), but it alone isn't enough: a `dispatch_env()` call made from
    INSIDE an already-dispatched run has an ANCESTOR process's wrapper directory already
    first on the inherited PATH, under a different tmp path than this call's own
    `wrapper_dir`, so the plain `p != wrapper_dir` filter let it through and
    `shutil.which` resolved AGENTCTL_REAL_GIT straight back to that ancestor's wrapper —
    the second outage, 2026-10-12 20:52 MST, from `tests/test_dispatch_env.py`'s own
    `dispatch_env()` call running inside a dispatched coder run. Returns the empty
    string when nothing else on `path` has one — NEVER the bare name `"git"`: that name
    would have to be resolved through `$PATH` by SOMETHING to be of any use, and letting
    either caller (`dispatch_env`'s `AGENTCTL_REAL_GIT`, or `_private_git_wrapper_dir`'s
    baked fallback) carry a bare name back into the wrapper script is the exact PATH
    lookup the 2026-10-17 outage used to turn a scrubbed-env stub into an unbounded fork
    chain (see the second OUTAGE comment above `_GIT_WRAPPER_SCRIPT`). An empty result
    can't happen in a real dispatch (git is how the worktree got there), only in a test
    with a stripped PATH that also never runs git; both callers treat it as "no usable
    real git" and the wrapper refuses rather than guessing."""
    for entry in path.split(os.pathsep):
        if not entry or entry == wrapper_dir:
            continue
        candidate = os.path.join(entry, "git")
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if _is_git_wrapper_copy(candidate):
            continue
        return candidate
    return ""


# A fixed line every copy of the swift wrapper carries, mirroring
# `_GIT_WRAPPER_MARKER`'s reasoning: one copy has to tell another copy apart from the
# real `swift` binary without comparing inodes.
_SWIFT_WRAPPER_MARKER = "AGENTCTL_SWIFT_WRAPPER_MARKER_V1"

# Credential-bearing vars dispatch_env()/bin/agentctl set for a run that manifest or
# plugin code — compiled with SwiftPM's own network denial removed by
# --disable-sandbox, below — must never see (agent-platform PR 965 round-1 security
# finding; the module docstring's fifth-gap paragraph has the mechanism). GH_TOKEN and
# GITHUB_TOKEN: the per-run read-only GitHub App installation token
# `git_credential_guard_env` sets, below. ANTHROPIC_API_KEY: the gateway-model
# dispatch's spend-capable LiteLLM virtual key `bin/agentctl`'s `gateway_env` sets.
# Stripped from EVERY invocation through the wrapper, not only the ones
# --disable-sandbox applies to.
_SWIFT_WRAPPER_STRIPPED_CREDENTIALS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY")

# Disables SwiftPM's own `sandbox-exec` around manifest/plugin compilation (the
# module docstring's fifth-gap paragraph has the measurement and the incident).
# `AGENTCTL_REAL_SWIFT` and the baked `@@BAKED_REAL_SWIFT@@` fallback follow the git
# wrapper's exact discipline (see the OUTAGE comments above `_GIT_WRAPPER_SCRIPT`):
# never resolve a bare `swift` through PATH, and never recurse into a copy of this
# same wrapper. bash 3.2 (launchd-critical) compatible: no declare -A, mapfile,
# ${var,,}.
_SWIFT_WRAPPER_SCRIPT = """#!/bin/bash
# @@SWIFT_WRAPPER_MARKER@@
# Placed first on a dispatched run's PATH: SwiftPM wraps Package.swift manifest and
# plugin compilation in its own sandbox-exec, which macOS refuses from inside this
# run's own sandbox. This wrapper adds --disable-sandbox to the subcommands that
# compile a manifest -- build, test, run, and package (every package subcommand) --
# when the caller hasn't already passed it, and passes every other invocation
# through unchanged. It also unsets this run's credential-bearing vars
# (@@STRIPPED_CREDENTIALS@@) from the real swift process's own environment before any
# exec below, on every invocation -- --disable-sandbox removes SwiftPM's own network
# denial during manifest/plugin compilation, and that code must never be able to read
# and exfiltrate this run's own read-only GitHub token or gateway spend key over the
# network allowance the outer run sandbox still grants (agent-platform PR 965 round-1
# security finding). See dispatch_env.py's module docstring (the fifth-gap paragraph)
# for the incident, the measurement, and the finding, and the OUTAGE comments above
# _GIT_WRAPPER_SCRIPT for why this never resolves a bare 'swift' through PATH.

real_swift="$AGENTCTL_REAL_SWIFT"

if [ -z "$real_swift" ]; then
    # Baked in at wrapper-write time, from that call's own ambient PATH -- see
    # _GIT_WRAPPER_SCRIPT's matching comment for why this is a baked absolute path
    # rather than another PATH lookup here. shlex.quote()-escaped by the Python side.
    real_swift=@@BAKED_REAL_SWIFT@@
fi

# Never exec, probe, or resolve a bare command name through $PATH here -- the exact
# shape of the git wrapper's 2026-10-17 outage. $real_swift must already be an
# absolute path to an executable file; anything else is refused before any exec.
case "$real_swift" in
    /*) ;;
    *)
        echo "agentctl swift wrapper: no usable real swift binary (AGENTCTL_REAL_SWIFT is unset or not an absolute path, and the baked fallback is empty or not an absolute path) -- refusing instead of resolving a bare 'swift' through PATH (see dispatch_env.py's _SWIFT_WRAPPER_SCRIPT docstring)" >&2
        exit 127
        ;;
esac

if [ ! -f "$real_swift" ] || [ ! -x "$real_swift" ]; then
    echo "agentctl swift wrapper: real swift path '$real_swift' is not an executable file -- refusing instead of resolving a bare 'swift' through PATH (see dispatch_env.py's _SWIFT_WRAPPER_SCRIPT docstring)" >&2
    exit 127
fi

# Refuse before any exec below if $real_swift resolves to a copy of THIS wrapper
# rather than the real swift binary -- same reasoning as the git wrapper's
# _agentctl_is_wrapper_copy.
_agentctl_is_swift_wrapper_copy() {
    [ -f "$1" ] || return 1
    if [ "$1" -ef "$0" ]; then
        return 0
    fi
    grep -q '@@SWIFT_WRAPPER_MARKER@@' "$1" 2>/dev/null
}

if _agentctl_is_swift_wrapper_copy "$real_swift"; then
    echo "agentctl swift wrapper: AGENTCTL_REAL_SWIFT ('$real_swift') resolves to a copy of this wrapper, not the real swift binary -- refusing instead of recursing into it (see dispatch_env.py's _SWIFT_WRAPPER_SCRIPT docstring)" >&2
    exit 127
fi

# Unset every credential this run's dispatch environment carries before any exec below
# -- see dispatch_env.py's _SWIFT_WRAPPER_STRIPPED_CREDENTIALS for what each is and
# where it's set. Applies to every invocation through this wrapper, not only the
# subcommands --disable-sandbox is added to.
for _agentctl_swift_cred in @@STRIPPED_CREDENTIALS@@; do
    unset "$_agentctl_swift_cred"
done
unset _agentctl_swift_cred

subcommand="$1"
case "$subcommand" in
    build|test|run|package)
        already_disabled=0
        for arg in "$@"; do
            if [ "$arg" = "--disable-sandbox" ]; then
                already_disabled=1
                break
            fi
        done
        if [ "$already_disabled" -eq 1 ]; then
            exec "$real_swift" "$@"
        fi
        shift
        exec "$real_swift" "$subcommand" --disable-sandbox "$@"
        ;;
    *)
        exec "$real_swift" "$@"
        ;;
esac
""".replace("@@SWIFT_WRAPPER_MARKER@@", _SWIFT_WRAPPER_MARKER) \
   .replace("@@STRIPPED_CREDENTIALS@@", " ".join(_SWIFT_WRAPPER_STRIPPED_CREDENTIALS))

# Same one-per-process, atexit-cleaned shape as `_git_wrapper_dir`, for the directory
# holding the swift wrapper script.
_swift_wrapper_dir: str | None = None


def _private_swift_wrapper_dir() -> str:
    """A short-lived directory, private to this process, holding the `swift` wrapper
    script (`_SWIFT_WRAPPER_SCRIPT`). One per process, for the same reason
    `_private_git_wrapper_dir` is. `cleanup_private_swift_wrapper_dir` is the
    `os._exit()`-path counterpart — see `_private_tmux_tmpdir`'s docstring for why
    `atexit` alone is not enough.

    The baked `@@BAKED_REAL_SWIFT@@` fallback is resolved HERE, once, from THIS call's
    own ambient `os.environ["PATH"]` — not the per-call PATH `dispatch_env()` resolves
    `AGENTCTL_REAL_SWIFT` from, for the same reason `_private_git_wrapper_dir` bakes its
    own fallback once: this function only ever runs once per process, while a later
    `dispatch_env()` call in the same process can pass a different `base`/PATH each
    time."""
    global _swift_wrapper_dir
    if _swift_wrapper_dir is None:
        _swift_wrapper_dir = tempfile.mkdtemp(prefix="swiftwrap-", dir="/tmp")
        baked_real_swift = _resolve_real_swift(os.environ.get("PATH", os.defpath), _swift_wrapper_dir)
        script = _SWIFT_WRAPPER_SCRIPT.replace("@@BAKED_REAL_SWIFT@@", shlex.quote(baked_real_swift))
        wrapper_path = Path(_swift_wrapper_dir) / "swift"
        wrapper_path.write_text(script)
        wrapper_path.chmod(0o755)
        atexit.register(shutil.rmtree, _swift_wrapper_dir, ignore_errors=True)
    return _swift_wrapper_dir


def cleanup_private_swift_wrapper_dir() -> None:
    """Remove this process's private swift-wrapper directory, if `dispatch_env()` ever
    created one; a no-op otherwise. Idempotent, and never raises. Call this explicitly
    right before any `os._exit()`, alongside `cleanup_private_tmux_tmpdir` — see that
    function's docstring for why."""
    global _swift_wrapper_dir
    if _swift_wrapper_dir is not None:
        shutil.rmtree(_swift_wrapper_dir, ignore_errors=True)
        _swift_wrapper_dir = None


def _is_swift_wrapper_copy(path: str) -> bool:
    """True if the file at `path` carries `_SWIFT_WRAPPER_MARKER` — a copy of
    `_SWIFT_WRAPPER_SCRIPT` (this process's own wrapper, or a DIFFERENT run's
    `/tmp/swiftwrap-*/swift` already sitting on an inherited PATH), never the real
    `swift` binary, which carries no such line. Same contract as `_is_git_wrapper_copy`."""
    try:
        with open(path, "rb") as f:
            head = f.read(_MARKER_PROBE_BYTES)
    except OSError:
        return False
    return _SWIFT_WRAPPER_MARKER.encode() in head


def _resolve_real_swift(path: str, wrapper_dir: str) -> str:
    """The real `swift` binary reachable from `path`, skipping `wrapper_dir` itself AND
    any other PATH entry whose `swift` is itself a copy of this module's wrapper
    (`_is_swift_wrapper_copy`) — same contract as `_resolve_real_git`, for the same
    reason (an ANCESTOR process's wrapper directory already on an inherited PATH).
    Returns the empty string when nothing else on `path` has one — never the bare name
    `"swift"`."""
    for entry in path.split(os.pathsep):
        if not entry or entry == wrapper_dir:
            continue
        candidate = os.path.join(entry, "swift")
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if _is_swift_wrapper_copy(candidate):
            continue
        return candidate
    return ""


# The identity `_kb_commit` (bin/agentctl) already falls back to, per-commit, when
# no identity resolves for a kb write (company/decisions/0009 — the same
# no-GitHub placeholder a fresh Builder install's first commit gets). Reused here
# rather than inventing a second placeholder for the identical "no identity
# anywhere on this account" case, now hit at `agentctl spawn` time instead of
# `kb apply` time: a dispatched coder's own `git commit` resolves identity the
# same way `_kb_commit`'s probe already checks for, under this same
# `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM=/dev/null` redirection.
_FALLBACK_COMMIT_NAME = "Builder"
_FALLBACK_COMMIT_EMAIL = "builder@localhost"


def _git_identity_resolves(env: dict, cwd: str | None) -> bool:
    """Whether `git var GIT_AUTHOR_IDENT` succeeds under `env` in `cwd` — the exact
    probe `_kb_commit` (bin/agentctl) already runs before falling back to its own
    placeholder identity, reused here so a dispatched run's own commit and a kb
    write agree on what counts as "no identity". Must run AFTER
    `git_credential_guard_env` has already pointed `GIT_CONFIG_GLOBAL`/
    `GIT_CONFIG_SYSTEM` at `/dev/null` in this same `env` (CQ·418) — so this
    probes the SAME resolution the run's own `git commit` is about to make:
    `cwd`'s own local `.git/config` (when `cwd` sits inside a worktree) and the
    passwd-GECOS/hostname auto-identity git falls back to on its own, never the
    account's `~/.gitconfig`, which this same `env` already closes regardless of
    what this probe finds. On a measured adopter Linux account with neither a
    local identity nor a usable GECOS/hostname fallback, `git var` exits non-zero
    with "fatal: empty ident name ... not allowed" — on the operator's own Mac,
    the same command exits 0, resolving to the passwd GECOS name and the
    machine's own hostname, with no `~/.gitconfig` involved either."""
    try:
        return subprocess.run(["git", "var", "GIT_AUTHOR_IDENT"], env=env, cwd=cwd,
                              capture_output=True, text=True).returncode == 0
    except OSError:
        return False


def git_credential_guard_env(env: dict, *, github_token: str | None = None) -> dict:
    """Mutate and return `env` with the CQ·418 git/gh credential-helper redirection:
    `GH_CONFIG_DIR` pointed at a private empty directory, `GIT_CONFIG_GLOBAL`/
    `GIT_CONFIG_SYSTEM` at `/dev/null` with `GIT_CONFIG_NOSYSTEM=1`, and
    `GIT_TERMINAL_PROMPT=0` — see `dispatch_env`'s own docstring for the mechanism
    and the incident. `github_token`, when given, is set as both `GH_TOKEN` and
    `GITHUB_TOKEN`; omitted (the default), any inherited value of either is
    stripped rather than left in place, same as `dispatch_env`'s own posture.

    The one function both the Claude dispatch lane (`dispatch_env`, below) and the
    Codex review lane (`bin/agentctl`'s `_run_codex_review_session`) call for this,
    so a variable added to the list reaches both lanes from one edit."""
    env["GH_CONFIG_DIR"] = _private_gh_config_dir()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    if github_token is not None:
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
    return env


def deny_push_env(env: dict) -> dict:
    """Mutate and return `env` with the `GIT_CONFIG_*` pairs that rewrite a `git push`
    to a network remote into a scheme with no registered helper (issue_01M3MMJFKSE5ZCA5YNXCJ1YCAK)
    — see `dispatch_env`'s own docstring for the mechanism and the incident. Layers onto
    whatever `GIT_CONFIG_*` pairs `env` already carries rather than assuming none exist:
    `GIT_CONFIG_COUNT` is read first and used as the starting index, so an existing pair's
    index is never reused, and the returned count covers every pair, old and new.

    The one function both `dispatch_env(deny_push=True)` (the direct dispatch path) and
    `review_launch.session_env` (the two-account launcher; every session that launcher
    runs is review-class, so this is unconditional there rather than gated on a
    parameter) call for this, so the two launch paths cannot diverge on what a reviewer's
    or security-reviewer's `git` refuses to push to."""
    start = int(env.get("GIT_CONFIG_COUNT") or 0)
    pairs = [(f"url.{_PUSH_REFUSAL_BASE}.pushInsteadOf", prefix)
            for prefix in _PUSH_REFUSAL_PREFIXES]
    for offset, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(pairs))
    return env


def dispatch_env(base: dict | None = None, *, deny_push: bool = False,
                 private_object_dir: Path | None = None,
                 shared_object_dir: Path | None = None,
                 github_token: str | None = None,
                 worktree: Path | None = None) -> dict:
    """A copy of *base* (default the current `os.environ`) with the
    dispatcher's tmux identity removed: `TMUX`/`TMUX_PANE` stripped, and
    `TMUX_TMPDIR` pointed at a private directory so an unqualified `tmux` in
    the child can't fall back to the operator's default socket either. Also
    carries `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and
    `BASH_DEFAULT_TIMEOUT_MS=600000`, overwriting any value *base* already
    has for either key — see the module docstring for the incident and the
    measurement behind both.

    `private_object_dir` (a dispatch to a repo always passes this —
    `run_sandbox.run_private_objects_dir`) sets `GIT_OBJECT_DIRECTORY`, so the run's own
    git writes new objects there instead of the shared `objects` directory `run_sandbox`
    denies wholesale. `shared_object_dir` (the shared git directory's own `objects`) sets
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`, so the run's git still reads everything already
    in the shared store — see the module docstring for why this replaced round 5's
    `_OBJECT_STORE_CONFIG`. Neither is set when `private_object_dir` is None (a dispatch
    with no git_dir at all — a non-repo agent, or a test).

    When both `private_object_dir` and `shared_object_dir` are given, a `git` wrapper
    (issue_01M4QXDGVE43R2WYT01VDQQBPY, `_private_git_wrapper_dir`) is also put first on
    `PATH`, with `AGENTCTL_REAL_GIT` (the real git binary) and
    `AGENTCTL_OWN_GIT_COMMON_DIR` (`shared_object_dir`'s own parent, the run's own common
    git directory) set so it can tell the run's own repository from any other one the
    run's git might be pointed at — see the module docstring's "BOTH VARS ARE
    PROCESS-WIDE" paragraph for why this is needed at all.

    EVERY CALL, UNCONDITIONALLY, ALSO CLOSES THE GIT-CREDENTIAL-HELPER ROUTE TO THE
    OPERATOR'S GITHUB TOKEN (CQ·418: two security-reviewer runs printed that token into
    their transcripts by running `git credential fill`, which consulted `~/.gitconfig`'s
    `credential.https://github.com.helper` and, through it, `gh`'s keyring-stored token).
    `GH_CONFIG_DIR` is pointed at an empty per-run directory (`_private_gh_config_dir`), so
    a run's own `gh` finds no `hosts.yml` of its own. `GIT_CONFIG_GLOBAL` and
    `GIT_CONFIG_SYSTEM` are pointed at `/dev/null` (always present, always empty — no
    per-run file needed for either) with `GIT_CONFIG_NOSYSTEM=1` as a second lock on the
    system config, so neither `~/.gitconfig` nor the Xcode system gitconfig's
    `credential.helper = osxkeychain` is ever read — but these three only close a
    FILE-based `credential.helper`. A `-c credential.helper=...` passed on the git
    command line itself is not a file, so it still resolves to whatever helper the
    command line names. `github_token`, when given (a per-run convivy-bosun
    installation token, minted by the dispatcher — see `bin/agentctl`'s
    `_mint_run_github_token` — and read-only), is what answers THAT case: set as both
    `GH_TOKEN` and `GITHUB_TOKEN`, which `gh` resolves before its own config or
    keyring, so even a `-c credential.helper='!gh auth git-credential'` on the command
    line answers with this run's own token, never the operator's, whichever helper
    invoked it. `GIT_TERMINAL_PROMPT=0` is the separate, general backstop: if whatever
    helper DOES run returns nothing (`github_token=None` — a dry run, or a non-GitHub
    test, never mints one), git would otherwise fall back to an interactive terminal
    prompt, which has nothing to talk to in a dispatched run and would hang; set to 0,
    git fails the credential fill outright instead.

    EVERY CALL, UNCONDITIONALLY, ALSO PUTS A `swift` WRAPPER FIRST ON `PATH`
    (`_private_swift_wrapper_dir`, the module docstring's fifth-gap paragraph): it adds
    `--disable-sandbox` to `build`/`test`/`run`/`package` when the caller hasn't
    already passed it, so a dispatched run's `swift build` doesn't hit SwiftPM's own
    sandbox-exec failing inside this run's own sandbox. `AGENTCTL_REAL_SWIFT` is set to
    the real `swift` binary resolved from this call's own PATH, mirroring
    `AGENTCTL_REAL_GIT`'s resolution. Because `--disable-sandbox` also removes SwiftPM's
    own network denial around manifest/plugin compilation, the wrapper unsets
    `GH_TOKEN`/`GITHUB_TOKEN`/`ANTHROPIC_API_KEY` (`_SWIFT_WRAPPER_STRIPPED_CREDENTIALS`)
    from the real `swift` process's own environment before every exec, so that code
    can't read and exfiltrate this run's read-only GitHub token or gateway spend key
    over the network the outer run sandbox still allows (agent-platform PR 965 round-1
    security finding).

    Use this for the env of any subprocess that starts a `claude` process or
    an agent run, spreading it first so per-call overrides still win:
    `{**dispatch_env(), "AGENTCTL_RUN_ID": run_id}`.

    `deny_push=True` (reviewer and security-reviewer roles only) additionally
    refuses a `git push` to a network URL, except to a remote configured with
    an explicit `remote.<name>.pushurl` — see the module docstring for the
    mechanism and the incident it closes. Its `GIT_CONFIG_*` pairs are layered onto
    any already in *base* rather than assuming none exist, so a caller that already
    sets some (there are none on this path today) doesn't get them silently
    overwritten.

    A SIXTH gap (measured on an adopter's fresh Linux account, 2026-10-31): that
    account has no git identity anywhere (no `~/.gitconfig`, which this call's own
    `GIT_CONFIG_GLOBAL=/dev/null` closes for every dispatch regardless; no local
    `.git/config`; and no GECOS/hostname git could fall back to on its own, unlike
    the operator's own Mac), so the first `git commit` a dispatched coder makes
    fails with "Author identity unknown" — after the sandbox fix above, this was
    the next thing blocking the same adopter's first dispatched commit. `worktree`
    (the run's own checkout, when this dispatch has one) is probed with
    `_git_identity_resolves` — the identical `git var GIT_AUTHOR_IDENT` check
    `_kb_commit` (bin/agentctl) already runs before its own fallback — AFTER
    `git_credential_guard_env` has set `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`, so
    this probes the exact resolution the run's own commit is about to make. Only
    when that probe fails does this set `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/
    `GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` to `_FALLBACK_COMMIT_NAME`/
    `_FALLBACK_COMMIT_EMAIL` — the same placeholder `_kb_commit` already uses for
    the identical "no identity anywhere" case. An account with an identity
    (`_git_identity_resolves` true, the operator's own Mac included) gets no
    override at all: this block changes nothing about `env` for it.
    """
    env = dict(os.environ if base is None else base)
    for key in _STRIP + _CO_ONLY:
        env.pop(key, None)
    env["TMUX_TMPDIR"] = _private_tmux_tmpdir()
    # Overwritten unconditionally: a dispatched run must never inherit an
    # interactive session's own background-task/timeout choice.
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env["BASH_DEFAULT_TIMEOUT_MS"] = "600000"
    # Unconditional, for every dispatch — see the docstring section above and
    # `git_credential_guard_env`'s own docstring (the helper this and the Codex
    # review lane both call).
    env = git_credential_guard_env(env, github_token=github_token)
    # Sixth gap, above: probed AFTER the redirection just above, so this sees the
    # same `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM=/dev/null` the run's own commit
    # will. `None` when this dispatch has no worktree of its own (a non-repo
    # agent, or a test) — the probe then runs in whatever directory this process
    # itself is in, which is never a repo this dispatch's commit will use either.
    if not _git_identity_resolves(env, str(worktree) if worktree is not None else None):
        env["GIT_AUTHOR_NAME"] = _FALLBACK_COMMIT_NAME
        env["GIT_AUTHOR_EMAIL"] = _FALLBACK_COMMIT_EMAIL
        env["GIT_COMMITTER_NAME"] = _FALLBACK_COMMIT_NAME
        env["GIT_COMMITTER_EMAIL"] = _FALLBACK_COMMIT_EMAIL
    # Unconditional, for every dispatch, regardless of whether this is a git
    # dispatch at all — see the module docstring's fifth-gap paragraph. The swift
    # wrapper goes first on PATH so a dispatched run's `swift build`/`test`/`run`/
    # `package` doesn't hit SwiftPM's own sandbox-exec failing inside this run's own
    # sandbox.
    swift_wrapper_dir = _private_swift_wrapper_dir()
    resolved_real_swift = _resolve_real_swift(env.get("PATH", os.defpath), swift_wrapper_dir)
    if resolved_real_swift:
        env["AGENTCTL_REAL_SWIFT"] = resolved_real_swift
    else:
        # Nothing usable on this call's own PATH (e.g. a test with a stripped PATH that
        # never runs swift) -- leave AGENTCTL_REAL_SWIFT unset rather than put an empty
        # value in its place; the wrapper's own baked fallback decides from here.
        env.pop("AGENTCTL_REAL_SWIFT", None)
    env["PATH"] = swift_wrapper_dir + os.pathsep + env.get("PATH", os.defpath)
    if private_object_dir is not None:
        env["GIT_OBJECT_DIRECTORY"] = str(private_object_dir)
    if shared_object_dir is not None:
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(shared_object_dir)
    if private_object_dir is not None and shared_object_dir is not None:
        # Scope the redirect above to the run's own repository
        # (issue_01M4QXDGVE43R2WYT01VDQQBPY) — see the module docstring's "BOTH VARS
        # ARE PROCESS-WIDE" paragraph. `shared_object_dir`'s own parent is the run's
        # common git directory (`shared_object_dir` is always `git_dir / "objects"` —
        # `run_sandbox.run_private_objects_dir`/`bin/agentctl`'s only real caller never
        # passes one without the other), `realpath`d so a later string compare inside
        # the wrapper isn't tripped by a symlink neither side resolved the same way
        # (e.g. macOS's /tmp -> /private/tmp).
        wrapper_dir = _private_git_wrapper_dir()
        resolved_real_git = _resolve_real_git(env.get("PATH", os.defpath), wrapper_dir)
        if resolved_real_git:
            env["AGENTCTL_REAL_GIT"] = resolved_real_git
        else:
            # Nothing usable on this call's own PATH -- leave AGENTCTL_REAL_GIT unset
            # rather than put an empty value in its place; the wrapper's own baked
            # fallback (also from `_resolve_real_git`, possibly also empty) is what
            # decides from here, and either refuses or doesn't on its own terms.
            env.pop("AGENTCTL_REAL_GIT", None)
        env["AGENTCTL_OWN_GIT_COMMON_DIR"] = os.path.dirname(
            os.path.realpath(str(shared_object_dir)))
        env["PATH"] = wrapper_dir + os.pathsep + env.get("PATH", os.defpath)
    if deny_push:
        env = deny_push_env(env)
    return env
