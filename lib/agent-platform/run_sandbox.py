"""The Claude Code sandbox every dispatched run is launched under.

The operator chose Option A on 2026-10-06 (Slate a-ow0q4o): the `ops-co` grant in
`~/.orient-control/ops-co-token` must not be readable by a dispatched run. A run
executes as the same Unix user as the co, so file modes cannot keep it out. The
sandbox does: `settings_json` builds the `--settings` value that `bin/agentctl`'s
`_spawn_claude` and `review_launch.session_command` pass to `claude -p`.

Each key below is checked against the installed Claude Code (2.1.292) docs:
code.claude.com/docs/en/sandboxing, /settings-reference, /permissions and
/settings. The command-line `--settings` layer outranks the project and user
layers, so a worktree's own `.claude/settings*.json` cannot override a scalar
here, and array keys such as `denyRead` only add entries.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

import builder_root as BR

# The directory the grant lives in, under the account home a run's claude uses.
ORIENT_CONTROL = ".orient-control"

# Whole directories under the account home that a run may not read. Each one holds
# credentials that a run has no reason to read:
# - `.orient-control`: the `ops-co` grant (`ops-co-token`), `agent-platform-token` (the
#   review-record and signal credential; see `mcp-verdict-server`), Deck's approval
#   credential (`deck-library-token`, which the Library answers as actor `deck`) and
#   `library-client-token`.
# - `.library`: `client-tokens.json` (the `deck` and person-login secrets), a saved copy
#   of a person's login (`person_*.token.new`), `ci-fetch-token`, the Library's
#   databases and `tls/server.key`.
# - `.config/gh`: `gh`'s own config, including `hosts.yml`, where it stores the operator's
#   token when it isn't in the keychain (CQ·418). `dispatch_env` already points a run's
#   `GH_CONFIG_DIR` at an empty per-run directory, so this is the second layer: even a run
#   that reset that variable itself cannot read the real one back.
# - `Library/Keychains`: the macOS keychain files `gh auth status` reports the operator's
#   token is stored in (CQ·418). A file-read deny does not by itself stop a Keychain
#   Services call through `securityd` — see `MACH_LOOKUP`'s comment below for why that
#   path is denied by a different mechanism — but it does stop a run that tried to read
#   the keychain database directly.
DENIED_DIRS = (".orient-control", ".library", ".config/gh", "Library/Keychains")

# The operator's user-scope CLAUDE.md, under each home in `homes` below. A dispatched
# run's claude excludes it via `claudeMdExcludes` (settings() key confirmed against the
# installed Claude Code binary's own schema, 2.1.294: "Glob patterns or absolute paths
# of CLAUDE.md files to exclude from loading ... Only applies to User, Project, and
# Local memory types"), so most of the file — Slate, handoff, talk-better, escalation
# to the operator, dispatch rules a run cannot act on — never reaches a run's context on every
# turn (context trim phase 2, issue_01M4D32KA2F99QGKABZ8HV2RG5). The ten `scope: fleet`
# rules that DO bind a dispatched run reach it instead through its own rendered
# `.claude/CLAUDE.md` (`knowledge-ctx use`'s `include_fleet`), which composes them
# unconditionally for a dispatched session — but only for the dispatch classes whose
# `agentctl spawn` actually ran that render AND confirmed every fleet block landed in
# the worktree's own CLAUDE.md (see `settings()`'s `exclude_user_claude_md` parameter):
# an `is_local` agent, a private crew's dispatch kept on the primary checkout, a render
# that never confirmed `profile_documents_current` (e.g. a `--dry-run`), and a render
# that DID confirm it but whose own CLAUDE.md write the ARCH-023/080 ownership gate
# refused (`claude_md_blocked` — round 1 missed this: `.profile-documents.json` goes
# current independently of whether that write landed) all skip or lose the render, so
# excluding this file for one of those would drop every fleet rule with nothing to
# replace it.
USER_CLAUDE_MD = Path(".claude") / "CLAUDE.md"

# The one file under the Convivy root that a run may not read. `api-tokens.json` holds the
# `ops-co` secret as its entry's key. The checkout is untracked, so no worktree carries a
# copy, but its absolute path is readable. Only the file is denied: its directory is ordinary.
ORIENT_TOKEN_FILE = ("convivy-orient", "api-tokens.json")

# Three more files under the account home that a run may not read (CQ·418): `.gitconfig`,
# where the operator's `credential.https://github.com.helper` points at `gh`; the
# convivy-bosun App's own private key, which signs the JWT that mints every run's OWN
# installation token — that minting happens in the dispatcher (`agentctl`), before this
# run starts, and the key must never be reachable from inside one; and the Library's
# bosun-ledger-reporter credential, which `agentctl` will use to POST spend entries
# (including subscription-lane ones) to the ledger — a run that could read it could
# forge spend records. Denied ahead of the Library minting it, so there is never a
# window where the file exists and is readable from inside a run.
DENIED_HOME_FILES = (".gitconfig", ".config/convivy/bosun-app.pem",
                      ".config/convivy/bosun-ledger-reporter.token")

# Tools a dispatched run may call without a prompt. Under `dontAsk` anything not listed
# here, or in a rule from this module, is refused. The list is what the run records show
# coders and reviewers calling (their `tool_calls.jsonl`, 2026-08 to 2026-10). `Bash`
# is unrestricted by this list; the sandbox confines it. `Read` covers reads anywhere;
# the deny rules above still refuse the credential paths. The `mcp__` entries are whole
# servers, which the run has today under `bypassPermissions`.
TOOL_ALLOW = (
    "Bash", "Read", "Grep", "Glob", "WebFetch", "WebSearch", "Agent", "Skill",
    "ToolSearch", "TaskOutput", "TaskStop", "Monitor", "ScheduleWakeup",
    "EnterWorktree", "ExitWorktree", "ReportFindings",
    "mcp__ops", "mcp__kb", "mcp__verdict", "mcp__claude_ai_Claude_Docs",
)

# The MCP server a dispatched run's verdict goes through (`mcp-verdict-server`). Spawn
# names it in the run's `--mcp-config` as this key, so its tools are `mcp__verdict__*`.
VERDICT_SERVER = "verdict"
# A dispatched run's claude is launched with `--name` set to this prefix and the run id.
# The name is in the claude process's argv, which the run cannot rewrite, so a process
# under the run finds it by walking its ancestors (`inside_dispatched_run`).
RUN_NAME_PREFIX = "agentctl-dispatched-run:"

# Where the Edit, Write and NotebookEdit tools may write, besides the worktree: the
# system temp directory, which reviewers compose their findings in. `Edit(path)` is the
# rule that governs `Write` and `NotebookEdit` (Claude Code 2.1.292 permissions docs,
# "Read and Edit"); a `Write(path)` rule is never consulted. A run's commits and its
# `pr-body.md` go through Bash, not these tools: the gitdir is a protected path, and
# `dontAsk` denies protected-path writes even when an allow rule matches.
#
# An `--eval-mode` replay does NOT get this grant (round 8, agent-platform#813 round 7
# finding): `replay run` dispatches several model arms against the same corpus entry at
# once, by design, against possibly adversarial historical PRs, and every one of those
# arms held `Write` and the sandbox's `allowWrite` over this whole shared root under the
# round-6 design — so any sibling arm could plant or overwrite another arm's verdict
# file before the dispatcher ever read it back. `settings`'s `eval_scratch_dir`
# parameter is the fix: an eval dispatch gets only its own directory
# (`create_eval_scratch_dir`), never this shared root.
SCRATCH_ROOTS = ("/tmp", "/private/tmp")

# The filename an `--eval-mode` replay's verdict lives under, inside its own
# `create_eval_scratch_dir` directory. Fixed, not keyed by `run_id` the way the old
# shared-root path was: the directory itself is now the unguessable part that keeps two
# replays apart, so the filename inside it no longer needs to carry that job too.
EVAL_VERDICT_FILENAME = "verdict.json"

# The `mkdtemp` prefix every `create_eval_scratch_dir` directory is named with. Shared
# with `settings()`'s own `eval_deny_read` (round 9, agent-platform#813 round 8 finding):
# every eval scratch directory on a temp root carries this same prefix, so one glob per
# root denies READ to all of them at once, and `allowRead` re-opens only the calling
# dispatch's own.
EVAL_SCRATCH_PREFIX = "agentctl-eval-"


def create_eval_scratch_dir() -> Path:
    """Creates and returns a fresh, unguessable directory for one `--eval-mode`
    replay's verdict write (round 8, agent-platform#813 round 7 finding).

    Round 6's design put every replay's verdict at `os.path.realpath("/tmp") /
    f"{run_id}-eval-review.json"` — a path readable by any other run (nothing denies
    `runs_root` for READING, only writing, and a run's own `meta.json` under
    `runs_root` records its `run_id`) and discoverable with `Glob` over the shared
    root regardless of whether the id leaked. `replay run` dispatches several model
    arms against the same corpus entry at once, and every dispatched run — eval arms
    included — held `Write` and the sandbox's `allowWrite` over that same shared
    root, so any sibling arm could plant or overwrite another arm's verdict file
    before the dispatcher ever collected it.

    This directory is the fix: `tempfile.mkdtemp` under the resolved system temp
    root, mode 0700 (`mkdtemp`'s own default — owner-only) and named with
    `mkdtemp`'s own random suffix, neither predictable nor derived from `run_id` the
    way the old filename was, so a sibling arm sharing the same temp root cannot
    guess it, and a bare directory listing of that root gives up only a name, never
    which run it belongs to or what it holds.

    Created once per dispatch, by the dispatcher itself, before that run's claude
    process ever starts — never by the sandboxed run. Its path is handed to exactly
    that one run through that run's own launch (the eval system-prompt directive's
    write target, and `TMPDIR`), and it is never written to `meta.json`, `run_dir`,
    or anywhere else under `runs_root` a sibling run's `Read` or `Glob` could
    discover it — those stay the only way a sibling could still learn it, and
    neither path is taken."""
    root = os.path.realpath(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix=EVAL_SCRATCH_PREFIX, dir=root))


def eval_verdict_path(scratch_dir: Path) -> Path:
    """Where one `--eval-mode` replay's verdict lives, inside its own
    `create_eval_scratch_dir` directory."""
    return Path(scratch_dir) / EVAL_VERDICT_FILENAME


# The `mkdtemp` prefix every `create_tool_session_scratch_dir` directory is
# named with (CQ·138). Distinct from `EVAL_SCRATCH_PREFIX`: the two serve
# unrelated purposes and nothing here needs them to share a glob.
TOOL_SESSION_SCRATCH_PREFIX = "agentctl-toolsession-"

# Where every dispatch's tool-session scratch directory is created, relative
# to the account home (CQ·138). Never the resolved system temp root
# (`tempfile.gettempdir()`): that root sits inside `allowWrite` for every
# ORDINARY (non-eval) dispatch (`settings()`'s own `allow_write = [Path(t) for
# t in tmp_roots]` branch), and `allowWrite` on a directory grants write on
# the directory ENTRY, not merely on files already inside it — so a dispatch
# whose scratch directory sat there could list that shared root, find a
# SIBLING dispatch's `TOOL_SESSION_SCRATCH_PREFIX` directory, and remove or
# rename it, including a name it did not create. Removing a sibling's scratch
# directory makes that sibling's own finalize find no marker to verify
# (`_verified_tool_session_marker`'s own device/inode check fails closed) and
# skip its own sweep, leaving that sibling's leftover tool-shell processes
# alive.
#
# `tool_session_scratch_root`, below, moves the parent outside every root any
# dispatch's `allowWrite` holds: the temp roots, `runs_root`, the worktree,
# the git directory, and the account-home subdirectories `DENIED_DIRS` names.
# Chosen under the account's own per-user state directory, the same
# `.local/state/agent-platform` tree `agent_postgres_socket_dir` already uses
# for this platform's own state outside any dispatch's reach — neither
# function's directory is ever granted to a run, by design. Named as ONE
# constant, read by both `create_tool_session_scratch_dir` (the dispatcher,
# before a run's claude ever starts) and by this module's own tests, so the
# two can never name different locations.
TOOL_SESSION_SCRATCH_RELDIR = Path(".local/state/agent-platform/tool-sessions")


def tool_session_scratch_root(home: Path) -> Path:
    """Where `create_tool_session_scratch_dir` creates one dispatch's scratch
    directory, under the given account home — never `Path.home()` or `$HOME`,
    which a caller's environment can point elsewhere (the same reasoning as
    `passwd_home()`, below). This is a FIXED, PREDICTABLE path, shared by every
    dispatch on the machine: what keeps one dispatch's scratch directory apart
    from another's is the unguessable `mkdtemp` suffix `create_tool_session_
    scratch_dir` creates inside it. What matters is that THIS parent is
    outside every `allowWrite` root any dispatch holds (see
    `TOOL_SESSION_SCRATCH_RELDIR`'s own comment), so a run that can list or
    write under this path at all is a run that was granted something it
    should never have — see this module's own `settings()` docstring on
    `tool_session_marker` for why nothing here adds that grant. This whole
    guarantee rests on that parent staying outside `allowWrite`: a future
    change that adds this path, or an ancestor of it, to any dispatch's
    `allowWrite` would let that dispatch reach every OTHER dispatch's scratch
    directory and marker, not only its own."""
    return Path(home) / TOOL_SESSION_SCRATCH_RELDIR


def create_tool_session_scratch_dir(home: Path) -> Path:
    """Creates and returns a fresh directory, outside `runs_root` AND outside
    every root any dispatch's `allowWrite` holds, to hold one dispatch's
    tool-session marker (the marker itself is `create_tool_session_marker`,
    below).

    Outside `runs_root`: `run_dir` sits under `runs_root`, which `settings()`
    denies for writing by every dispatched run's Bash, on purpose
    (`test_the_agent_runs_root_is_denied_for_writing_by_a_run`) — a run that
    could write under `runs_root` could rewrite the `meta.json` a verdict is
    checked against, so a marker mechanism that wrote there would always be
    refused in a real dispatch.

    Outside the shared system temp root: that root sits inside every
    ORDINARY dispatch's own `allowWrite`, so a dispatch whose scratch
    directory lived there could reach and remove or rename a SIBLING
    dispatch's scratch directory (see `TOOL_SESSION_SCRATCH_RELDIR`'s own
    comment for the mechanics). This directory lives under
    `tool_session_scratch_root(home)` instead — a per-account state path no
    dispatch's `allowWrite` ever names — created 0700 if missing (never
    trusting whatever mode a pre-existing directory there happens to have),
    with `tempfile.mkdtemp` inside it (mode 0700, `mkdtemp`'s own random
    suffix). It holds the one file (`create_tool_session_marker`'s return
    value) `settings()`'s `tool_session_marker` parameter actually grants
    this dispatch write access to. The directory itself is never granted —
    see `settings()`'s own docstring for what that file-only grant does and
    does not let the run do. Created once per dispatch, by the dispatcher,
    before that run's claude process ever starts — never by the sandboxed
    run itself."""
    root = tool_session_scratch_root(home)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return Path(tempfile.mkdtemp(prefix=TOOL_SESSION_SCRATCH_PREFIX, dir=str(root)))


# The marker's fixed filename inside its `create_tool_session_scratch_dir`
# directory (CQ·138). Fixed, not keyed by run id: the directory itself is
# already this dispatch's own unguessable, owner-only scratch space, so the
# filename inside it carries no isolation job of its own.
TOOL_SESSION_MARKER_NAME = "marker"


def create_tool_session_marker(scratch_dir: Path) -> Path:
    """Creates and returns an empty, owner-only file inside `scratch_dir` (this
    dispatch's own `create_tool_session_scratch_dir` directory) — the ONE path
    `settings()`'s `tool_session_marker` parameter grants this dispatch write
    access to (CQ·138).

    An earlier design recorded each Bash-tool shell's OWN REPORTED pid — a
    value the run writes about itself, with no OS-verified link to any
    process at all — and the finalize-time sweep trusted that a live process
    whose pgid and sid both happened to equal a recorded number WAS the tool
    shell. Every other run's own Bash-tool shell is under the same uid and is
    exactly such a process (a brand-new zsh session, pgid == sid == pid) — so
    a run could write ANOTHER run's tool-shell pid into its own record and
    have this run's trusted supervisor kill that shell at finalize. The
    content was the attack surface, and the run could always write it, an
    `allowWrite` grant on the record file notwithstanding.

    This file replaces that record with a liveness mechanism the run cannot
    forge: nothing is ever written into it or read back from it. A tool
    shell's `.zshenv` (`_write_tool_session_zdotdir` in `bin/agentctl`) opens
    it for append on a file descriptor that stays open for the lifetime of
    that shell and everything it backgrounds or disowns, including a
    descendant reparented to pid 1 — so "who currently holds this file open"
    (`lsof -t <path>`, queried by the supervisor against the device and inode
    recorded for THIS exact file, never content read from it) IS this
    dispatch's own tool-session tree, with no run-reported identifier
    anywhere in the path. The content a run writes into the file (it can:
    `settings()` grants the file itself) is never consulted by anything."""
    path = scratch_dir / TOOL_SESSION_MARKER_NAME
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path


# Bash may reach these hosts. `strictAllowlist` makes any other host a refusal
# rather than a prompt, which a bypassPermissions run would otherwise pass.
# A loopback Library (127.0.0.1) is unreachable from Bash without
# `network.allowLocalBinding`, which opens every localhost port, so it is not set.
NETWORK_ALLOWED = ("github.com", "api.github.com", "pypi.org", "files.pythonhosted.org")

# Lets `gh` verify TLS under Seatbelt. Without it `gh` fails with
# "x509: OSStatus -26276". Measured on this machine.
#
# THIS TUPLE IS AN ALLOWLIST, NOT A DENYLIST (CQ·418): mach-lookup is default-denied under
# Seatbelt, and a service reaches a run only by being named here. The macOS keychain is
# read through `securityd`'s own mach service, which this tuple does not name, so a run's
# `security find-*-password` or `git credential-osxkeychain` already cannot reach it by
# this same omission — nothing needs to be ADDED to deny it; only `DENIED_DIRS`' file-read
# layer, above, is an addition. Unconfirmed by a live run under the real sandbox as of this
# change — see the PR body for what the co's own live check (outside this repo's test
# suite) found.
MACH_LOOKUP = ("com.apple.trustd.agent",)

# A dedicated Postgres cluster for the repos named in `POSTGRES_SOCKET_REPOS`
# (round-1 revise on PR #817): a vivy coder's test suite
# needs a database, and `run-0b59399a` (2026-10-08 03:57Z) failed on both
# /tmp/.s.PGSQL.5432 and 127.0.0.1:5432 with the sandbox's TCP localhost left
# closed (see `NETWORK_ALLOWED`'s comment — opening `allowLocalBinding` would
# open every local port, the loopback Library included).
#
# ROUND 1 GRANTED THE OPERATOR'S OWN CLUSTER'S SOCKET. The co then checked that
# cluster directly (PR #817 comment, 2026-10-08): its pg_hba trusts every
# role on the local socket, and the role a dispatch connects as by default
# (the operator's own unix user) is a superuser there. A superuser can run `COPY ... TO
# PROGRAM`, which runs a shell command in the server process — so that grant
# was command execution on the host, not a database-scope question, whatever
# role or database the dispatch's own query named. `bin/agent-postgres-
# provision` builds a SEPARATE cluster instead: its own data and socket
# directories, no TCP listener, and one role (`AGENT_POSTGRES_ROLE`) with
# LOGIN and CREATEDB and nothing else — pg_hba trusts that role on this
# cluster's socket and rejects every other role on it, the bootstrap
# superuser included.
#
# THE SOCKET DIRECTORY HAS NO SYMLINK IN ITS PATH, unlike `/tmp` (see
# `SCRATCH_ROOTS` above). Round 1's reviewers both flagged that nothing shows
# whether Seatbelt's `(remote unix-socket (subpath ...))` predicate resolves
# a symlink the same way the filesystem predicates do — confirmed only for
# filesystem rules. `home / AGENT_POSTGRES_RELDIR` resolves to itself on this
# machine at every component (`.local`, `state`, `agent-platform`,
# `postgres`, `sockets`), so the question has nothing left to apply to here.
#
# `sandbox.network.allowUnixSockets` is the grant itself: an array of socket
# paths, separate from `allowLocalBinding` and from
# `allowedDomains`/`NETWORK_ALLOWED` above (confirmed against the installed
# Claude Code 2.1.293 binary — no settings schema ships alongside it, so this
# was read from the binary's own strings: `icr`/`r5`'s seatbelt profile
# builder takes `allowUnixSockets` as a list and emits one `(allow
# network-outbound (remote unix-socket (subpath "<path>")))` rule per entry,
# distinct from `allowAllUnixSockets` (bool, path-regex "^/") and from
# `allowLocalBinding` (bool, local IP ports)). Keyed on the bare repo name
# `_spawn_claude` already passes as `repo` (`args.on`, e.g. "vivy"), the same
# name `LOCAL_AGENT_REPOS` above matches against. Add a repo by adding its
# name to `POSTGRES_SOCKET_REPOS`.
AGENT_POSTGRES_RELDIR = Path(".local/state/agent-platform/postgres")
AGENT_POSTGRES_PORT = 5433
AGENT_POSTGRES_ROLE = "agent"
POSTGRES_SOCKET_REPOS = ("vivy",)


def agent_postgres_socket_dir(home: Path) -> Path:
    """Where `bin/agent-postgres-provision` puts the agent cluster's unix socket,
    under the given account home. Both this module's `settings` (the sandbox
    grant) and `bin/agentctl` (`PGHOST`) call this, so the two can never name
    different directories."""
    return Path(home) / AGENT_POSTGRES_RELDIR / "sockets"


def agent_postgres_socket_path(home: Path) -> str:
    return str(agent_postgres_socket_dir(home) / f".s.PGSQL.{AGENT_POSTGRES_PORT}")

# The Edit and Write tools run outside the sandbox, so these deny rules are the
# only guard on them. Each path names a place a run could plant a setting,
# hook or MCP server that runs outside the sandbox.
HOME_EDIT_DENIED = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude.json",
    "Library/LaunchAgents",
)
# `.git` is the worktree's pointer file. A run that rewrote it could point git at a
# directory of its own (agent-platform#793, condition 1).
WORKTREE_EDIT_DENIED = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".git")

# Round 6 (reviewer + security-reviewer blocking finding, both independently): rounds 3-5
# each narrowed the shared `objects` directory's write grant — denying `objects/info`,
# then enumerated pack/index files, then `objects/pack` wholesale — but every round left
# the two-hex-digit loose-object fanout directories themselves writable, because a run's
# OWN commit/fetch legitimately creates new loose objects there and nothing had found a
# way to allow that write without also allowing the one git never needs: deleting an
# EXISTING loose object's file and recreating it, under the same content-addressed name,
# with different content. `rm` only needs write permission on the directory, not the
# file (loose objects are mode 0444), so the wholesale grant let a run silently corrupt
# any object the primary checkout or a sibling worktree reads — `cat-file`, `log -p`,
# `diff`, `checkout` all return the substituted content with no error; only `git fsck`
# (which nothing here runs) would ever catch it. `objects` is now denied wholesale, the
# same way `hooks` always was: the run no longer writes there at all. `private_objects_dir`
# and `import_private_objects`, below, are the other side of that — where the run's own
# git writes instead, and how those writes rejoin the shared store once the run has ended.
PRIVATE_OBJECTS_DIRNAME = "agentctl-private-objects"

# The env vars `dispatch_env` sets to redirect a run's git at the private object store
# (see `private_objects_dir` below). `import_private_objects` strips them before pointing
# git back at the shared store; `tests/conftest.py` and `tests/bash/run.sh` strip the same
# three so a git fixture under a dispatched run writes into the repo it creates instead of
# the run's private store.
GIT_OBJECT_REDIRECT_VARS = ("GIT_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def private_objects_dir(git_dir: Path, linked: Path | None) -> Path:
    """Where a dispatched run's own git writes new objects, now that `objects` under
    `git_dir` is denied wholesale (see the comment above).

    Lives inside the run's own git-administrative area: `linked` (the run's linked
    worktree's own `<common>/worktrees/<name>` directory) when this run's worktree is a
    linked one, or directly under `git_dir` for the one case where it isn't (a worktree
    that IS `git_dir`'s own main checkout). Never inside the worktree's own working
    directory, so `git status` in the worktree never shows it as untracked — a run's Bash
    could otherwise commit or otherwise disturb it. `dispatch_env`'s `GIT_OBJECT_DIRECTORY`
    points the run's own git here; `GIT_ALTERNATE_OBJECT_DIRECTORIES` points it at the
    shared store (`git_dir / "objects"`) so the run still reads every object already
    there. `settings()` exempts this exact path from `_protect_rest_of_git_dir`'s walk, so
    it is never swept into the wholesale deny the way a brand-new name elsewhere under
    `git_dir` would be."""
    return (linked or git_dir) / PRIVATE_OBJECTS_DIRNAME


def run_private_objects_dir(worktree: Path, git_dir: Path) -> Path:
    """`private_objects_dir`, resolving `linked` from `worktree` itself — the one call a
    caller outside this module needs, before spawn (to create the directory and wire
    `dispatch_env`) and after the run ends (to import what it wrote)."""
    return private_objects_dir(git_dir, _linked_gitdir(worktree))


def import_private_objects(git_dir: Path, private_dir: Path, *, remove: bool = True) -> None:
    """Move a finished run's own objects from `private_dir` into the shared store under
    `git_dir`, through git's own verified transfer — never a file copy — so every caller
    that later reads this worktree outside the sandbox (`agentctl publish`, `pr-open`, a
    revise coder re-spawned on the same worktree, the worktree pin check, a reviewer
    worktree that fetches from the pushed branch) finds the run's commits without needing
    the private store's environment itself. A no-op when `private_dir` doesn't exist — a
    run that never wrote an object, or a dry run, which never creates it.

    Enumerates the objects physically present in `private_dir` alone: `GIT_OBJECT_DIRECTORY`
    pointed at it with NO alternate configured, so `git cat-file --batch-all-objects` sees
    exactly what this run wrote and nothing the shared store could otherwise supply through
    an alternate. Of those, only the ones the shared store does not already have are packed
    (`git pack-objects`) and indexed into it (`git index-pack`, with `GIT_OBJECT_DIRECTORY`
    back to its default — the shared store `git_dir` itself names) — a plain existence check
    against the shared store first, never a reason on its own to trust a name (see below).
    `index-pack` is the verification: it recomputes every object's hash while unpacking the
    incoming pack and writes the result under a name derived from the PACK's own content, so
    nothing here trusts a name it was handed — and it writes a brand-new pack file alongside
    whatever the shared store already has, never touching an existing loose object or pack by
    name (content-addressed storage never needs to: an object git already has under a given
    hash is, by construction, the same content a second write under that hash would produce).

    `remove` defaults to True: once the transfer above is done, `private_dir` is safe to
    delete, because the ONLY caller of this default is dispatcher code that runs after the
    run's own claude process has already exited (every existing call site) — nothing can
    write a new loose object into it after this function's own enumeration was taken, so
    deleting the whole directory loses nothing.

    `remove=False` is for the one caller that is NOT like that: `bin/agentctl`'s
    `_stream_transcript`, catching a LIVE run's objects up into the shared store
    periodically WHILE the run can still write more (knowledge#793's own window — the
    worktree's checked-out branch, in the shared ref store, can point at a commit whose
    content exists only in the private store for as long as this run holds it, and ANY
    other git process reading the shared store — `git fetch`, `git fsck`, `git rev-list
    --all` — trips on it, not just this run's own worktree). Deleting `private_dir` on
    that path would race the run's own still-running git: an object written AFTER this
    call's own enumeration but BEFORE an unconditional `rmtree` would be destroyed with no
    recovery path. `remove=False` leaves every file in `private_dir` in place — re-imported
    (cheaply: already-present objects are skipped) on the next periodic tick, and physically
    removed only once by the one `remove=True` call this run's own end makes."""
    if not private_dir.is_dir():
        return
    base_env = {k: v for k, v in os.environ.items()
               if k not in GIT_OBJECT_REDIRECT_VARS}
    source_env = {**base_env, "GIT_DIR": str(git_dir), "GIT_OBJECT_DIRECTORY": str(private_dir)}
    names = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"],
        env=source_env, capture_output=True, text=True, check=True,
    ).stdout
    if names.strip():
        # A plain existence check against the shared store (its own GIT_DIR, no private
        # override) — not a trust decision: an object this reports present was ALREADY
        # verified by a prior index-pack (or was in the shared store to begin with), and
        # one reported missing is still only packed, never copied, below. This is what
        # keeps a long-running run's repeated `remove=False` ticks cheap: tick N+1 does
        # not re-pack what tick N already moved.
        shared_env = {**base_env, "GIT_DIR": str(git_dir)}
        checked = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input=names, env=shared_env, capture_output=True, text=True, check=True,
        ).stdout
        missing = [line.split(" ", 1)[0] for line in checked.splitlines()
                  if line.endswith(" missing")]
        if missing:
            pack = subprocess.run(
                ["git", "pack-objects", "--stdout"],
                input=("\n".join(missing) + "\n").encode(), env=source_env,
                capture_output=True, check=True,
            ).stdout
            subprocess.run(
                ["git", "index-pack", "--stdin"],
                input=pack, env={**base_env, "GIT_DIR": str(git_dir)}, capture_output=True, check=True,
            )
    if remove:
        shutil.rmtree(private_dir, ignore_errors=True)
# Files in a linked worktree's own git directory that git reads as pointers or config. A run
# that wrote one could point git at a config it wrote, with `core.fsmonitor` or
# `core.hooksPath`, and git would run that command on the next git command in the worktree.
# The rest of that directory (index, HEAD, logs) is what a commit writes, so it stays writable.
GIT_POINTER_FILES = ("commondir", "gitdir", "config.worktree")
# The files git keeps for ONE worktree's own state, found the same way `_branch_write_paths`
# was: by observing a real commit (and, for FETCH_HEAD/ORIG_HEAD, what `git fetch` and a
# branch-changing command write). For a LINKED worktree these live under its own
# `<common>/worktrees/<name>/` directory, granted wholesale as `linked` in `settings()`,
# minus `GIT_POINTER_FILES`. For the worktree that IS `git_dir`'s own main checkout (no
# `linked` — `_protect_rest_of_git_dir`'s `linked=None` case) the same files sit directly at
# the top of `git_dir`, so they are named here to stay writable there too, the one case
# `_branch_write_paths` and the object store do not already cover.
WORKTREE_OWN_FILES = ("HEAD", "index", "ORIG_HEAD", "COMMIT_EDITMSG", "FETCH_HEAD", "logs/HEAD")


def current_branch(worktree: Path) -> str | None:
    """The branch `worktree` is checked out on, or None for a detached HEAD (a
    reviewer's pinned commit) or an unreadable `HEAD` file.

    Read from the worktree's own `HEAD` file rather than by running git: a
    subprocess here would also be seen by any test that stubs `subprocess.Popen`
    to capture the claude launch (see `git_common_dir`'s docstring, the same
    reason)."""
    gitdir = _linked_gitdir(worktree) or (Path(worktree) / ".git")
    try:
        text = (gitdir / "HEAD").read_text().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if not text.startswith(prefix):
        return None
    return text[len(prefix):]


def _branch_write_paths(git_dir: Path, branch: str) -> list[Path]:
    """The exact files a commit and a push write for one branch: the local ref and
    its reflog, the remote-tracking ref and its reflog, and the local ref's own lock
    file (git's atomic-write mechanism for a ref). Found by observing a real commit
    and push from a linked worktree (`tests/live/test_dispatched_run_confinement.py`
    repeats the observation under the sandbox). Kept for documentation and as a hedge,
    but `_protect_rest_of_git_dir` below is what actually narrows the write (see its docstring:
    a path under the shared git directory is NOT confined to this module's `allowWrite`
    list the way a path elsewhere is)."""
    heads = f"refs/heads/{branch}"
    remote = f"refs/remotes/origin/{branch}"
    return [
        git_dir / heads, git_dir / f"{heads}.lock",
        git_dir / remote,
        git_dir / "logs" / heads,
        git_dir / "logs" / remote,
    ]


def _protect_rest_of_git_dir(git_dir: Path, branch: str | None, linked: Path | None,
                             private_dir: Path) -> list[Path]:
    """Every existing path under `git_dir` that this run's own commit, fetch and push do
    NOT write — the files a run's `denyWrite` must name explicitly to keep them refused.

    This module's `allowWrite` does NOT control write access under a shared git
    directory the way it does elsewhere: measured live (a scratch repo, settings built
    the same way `settings()` builds them, under the real sandbox), writing a path under
    `git_dir` that is absent from BOTH `allowWrite` and `denyWrite` still succeeds —
    unlike an ordinary path outside any git directory, which a bare absence from
    `allowWrite` does refuse. Only an explicit `denyWrite` entry refuses a git-directory
    path. So narrowing this write is a `denyWrite` problem, not an `allowWrite` one, and
    round 2's version of this function narrowed only `refs/heads` and `refs/remotes` —
    leaving `refs/tags`, `refs/notes`, the directory's own top-level files (`HEAD` of the
    PRIMARY checkout when this run's own worktree is linked), and every sibling linked
    worktree's own per-worktree directory covered by neither list and so still writable
    (round 2 reviewer + security-reviewer finding). This widens the same enumeration to
    the whole directory instead of naming each subdirectory by hand: every existing entry
    is either exempt (reachable from the branch's own write paths, this run's own private
    object store, or this run's own worktree files) or denied, so a new kind of
    git-directory entry this module has not seen before is denied by default rather than
    overlooked.

    What's exempt from this walk: `private_dir` (this run's own private object store —
    see `private_objects_dir`'s docstring; named explicitly rather than left to fall out
    of `linked`'s wholesale exemption below, so it stays exempt in the `linked is None`
    case too); the branch's own ref/lock/remote-tracking ref and their reflogs
    (`_branch_write_paths`); and this run's own worktree state (`WORKTREE_OWN_FILES`) —
    under `linked` wholesale when this worktree is a linked one (its `GIT_POINTER_FILES`
    are added back to the deny list by `settings()`, same as before), or at the top of
    `git_dir` itself when this worktree IS `git_dir`'s own main checkout (`linked` is
    None).

    `objects` is NOT exempt (round 6 — reviewer + security-reviewer blocking finding,
    both independently, on the SAME defect rounds 3-5 kept narrowing instead of closing):
    rounds 3-5 exempted it wholesale and then chased, round by round, which names under it
    were not content-addressed (`objects/info/alternates`, an existing pack, a name
    invented after spawn such as `multi-pack-index`) — and still left every loose object
    deletable and recreatable under its own hash-named path, because `rm` only needs
    write permission on the fanout DIRECTORY, not on the object file itself (loose
    objects are mode 0444). An ordinary git read (`cat-file`, `log -p`, `diff`, `checkout`)
    never verifies a loose object's hash against its content, so that substitution was
    silent and permanent to every reader of the shared store. `objects` now falls through
    this walk exactly like any other directory with no exempt entry inside it: denied as
    ONE path, the same way `hooks` always was. The run's own git no longer writes there at
    all — `private_objects_dir`/`dispatch_env`'s `GIT_OBJECT_DIRECTORY` redirect it to
    `private_dir` instead, and `import_private_objects` is how those writes reach the
    shared store once the run has ended.

    A directory with no exempt entry anywhere under it (a sibling worktree's own
    directory, `refs/tags`, `refs/notes`, `hooks`, now `objects` too) is denied as ONE
    path rather than file by file, so a file created there after this call still refuses.
    A directory that DOES hold an exempt entry (`refs/heads`, `refs/remotes/origin`,
    `worktrees` when this run's own `linked` is one of its children) is walked instead, so
    the exempt file inside it stays reachable.

    None for `branch` (a detached checkout — a reviewer pinned to a commit) exempts
    nothing from `_branch_write_paths`, so every existing ref is protected. A path
    created AFTER this call (a concurrent fetch, a branch this run itself creates, a
    sibling worktree added after this run started) is not enumerated and so is not
    covered — enumerating what exists is the only non-guessing way to do this, and what
    does not exist yet cannot be enumerated. `private_dir` is the one exception: it is
    named explicitly rather than discovered by enumeration, so it stays exempt whether or
    not it has been created on disk yet by the time this runs."""
    exempt: set[Path] = {private_dir}
    if branch:
        exempt |= set(_branch_write_paths(git_dir, branch))
    if linked:
        exempt.add(linked)
    else:
        exempt |= {git_dir / rel for rel in WORKTREE_OWN_FILES}

    def walk(directory: Path) -> list[Path]:
        protect: list[Path] = []
        for entry in sorted(directory.iterdir()):
            if entry in exempt:
                continue
            if entry.is_dir() and any(entry in e.parents for e in exempt):
                protect.extend(walk(entry))
            else:
                protect.append(entry)
        return protect

    return walk(git_dir)


def passwd_home() -> Path:
    """The account's home from the passwd database. The environment cannot change
    it, so a run cannot point a child at a home of its choosing."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _orient_token_file(home: Path) -> Path:
    """The orient token file under `home`: the Convivy root's place under the passwd home,
    then the checkout's file. `BR.root()` supplies the root, so the path is not built from
    a literal. What carries over to `home` is the root's place, not its basename: a root at
    `~/work/Convivy` puts the file under `~/work`. A root outside the passwd home raises
    ValueError here."""
    return home / BR.root().relative_to(BR.real_account_home()) / Path(*ORIENT_TOKEN_FILE)


def _rule_path(path: Path) -> str:
    # Permission rules take `//` for an absolute path (docs: "Read and Edit rules").
    return "//" + str(path).lstrip("/")


def tmux_socket_dir(tmp_root: Path) -> Path:
    """Where tmux keeps its sockets under a temp root: `tmux-<uid>`, as tmux itself names
    it. A run that unlinked or rebound the operator's default socket there, or renamed one
    of its own onto it, would get the next connection a co's tooling makes (agent-platform#793
    condition 4). The uid comes from the process, never a literal."""
    return tmp_root / f"tmux-{os.getuid()}"


def _unique(paths: Iterable[Path]) -> list[Path]:
    seen: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def _linked_gitdir(worktree: Path) -> Path | None:
    """The git directory a linked worktree's `.git` file names, `<common>/worktrees/<name>`.
    None when `worktree` is not a linked checkout."""
    dot_git = Path(worktree) / ".git"
    if not dot_git.is_file():
        return None
    # A linked worktree's `.git` file reads `gitdir: <path>`.
    text = dot_git.read_text().strip()
    if not text.startswith("gitdir:"):
        return None
    return (Path(worktree) / text[len("gitdir:"):].strip()).resolve()


def git_common_dir(worktree: Path) -> Path | None:
    """The common git directory of `worktree`: where a run's commits write refs and
    objects, outside the worktree itself. None when `worktree` is not a checkout.

    Read from the files git keeps rather than by running git: spawn runs this on
    every launch, and a subprocess here would also be seen by any test that stubs
    `subprocess.Popen` to capture the claude launch."""
    dot_git = Path(worktree) / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    gitdir = _linked_gitdir(worktree)
    if gitdir is None:
        return None
    # That directory's `commondir` file names the common directory relative to it.
    commondir = gitdir / "commondir"
    if commondir.is_file():
        return (gitdir / commondir.read_text().strip()).resolve()
    return gitdir


# Read by settings(), and set by the confinement test to a temporary directory holding
# stand-in credentials. spawn itself must keep the real HOME: knowledge-ctx reads the
# Library credential from there to list profiles. The variable adds a home to the denial
# and never removes one, so a run that sets it for a nested spawn only narrows that run.
EXTRA_DENY_HOME_ENV = "AGENTCTL_SANDBOX_EXTRA_DENY_HOME"


def settings(*, home: Path, worktree: Path, runs_root: Path | None = None,
             git_dir: Path | None = None, exclude_user_claude_md: bool = False,
             eval_scratch_dir: Path | None = None,
             tool_session_marker: Path | None = None,
             repo: str | None = None) -> dict:
    """The sandbox and permission settings for one run.

    `home` is the account home the run's claude is launched with. Paths under
    `Path.home()` of the calling process are denied too, so a caller whose HOME
    differs from `home` (a test, or a launcher) still denies its own grant file.
    `runs_root` is the agent-runs directory. Every run's record lives under it, so no
    run may write there: a run's own `meta.json` is the record `mcp-verdict-server`
    trusts for its role, PR and commit. `git_dir` comes from `git_common_dir`; the linked
    worktree's own git directory is read from `worktree`'s `.git` file here. `repo` is
    the bare repo name `_spawn_claude` already resolves (`args.on`); when it's in
    `POSTGRES_SOCKET_REPOS`, the agent Postgres cluster's socket (under `home`) is
    added to `network.allowUnixSockets` — every other repo (including `repo=None`)
    gets exactly today's network settings, unchanged.

    `exclude_user_claude_md` defaults to False — load the user-scope CLAUDE.md, same as
    every caller before context trim phase 2. A caller may only pass True when it can
    show the fleet blocks the fleet-carrying render (`knowledge-ctx use`'s dispatched
    `include_fleet`) composed are actually sitting in THIS worktree's own rendered
    CLAUDE.md. `cmd_spawn`'s `docs_unavailable_reason is None` alone is NOT that
    showing (security-reviewer, round 2: it stays None on a render that confirmed
    `.profile-documents.json` current while the ARCH-023/080 ownership gate still
    refused the CLAUDE.md write itself — the record and the file can go current and
    blocked independently) — an `is_local` agent, a private dispatch kept on the
    primary checkout, a render that never confirmed `profile_documents_current`, and
    one that confirmed it but had its write blocked all skip or lose the render, so
    excluding the user file for one of those would drop every `scope: fleet` rule —
    including no-agent-merges-a-pull-request and verify-before-you-report — with no
    replacement (security-reviewer, round 1 of this parameter's own PR).
    `bin/agentctl`'s `_fleet_blocks_confirmed_in_claude_md` is the one function that
    computes this; it checks `claude_md_blocked` too and then re-reads the worktree's
    own CLAUDE.md for the markers the render recorded, so "the render ran" and "the
    fleet blocks are actually there" cannot drift apart a second way.

    `eval_scratch_dir` (round 8, agent-platform#813 round 7 finding) is an
    `--eval-mode` dispatch's own directory (`create_eval_scratch_dir`'s return
    value for THIS dispatch). When set, it REPLACES the shared temp root in
    `allowWrite` rather than adding to it: an eval dispatch gets only that one
    directory, never the whole shared root — several eval arms run against the
    same corpus entry at once, and granting all of them the whole shared root is
    what let one arm plant or overwrite another's verdict before round 8. The
    narrowing is by OMISSION from `allowWrite`, not an added deny rule for the
    shared root: `eval_scratch_dir` sits physically inside that root (it is
    created under it), and a deny rule for the root would be a BROADER match
    than this function's own allow rule for the directory nested inside it —
    this module's own contract ("a deny rule is checked before any allow rule,
    so the allow list cannot re-open a denied path") means that deny would
    shadow this allow too, leaving the directory this run is actually meant to
    write to refused. Leaving the shared root out of `allowWrite` denies it on
    its own, the same as any other ordinary (non-git-directory) path this
    module never names (see `_protect_rest_of_git_dir`'s docstring for the one
    exception to that rule, which does not apply here). `None` (every non-eval
    dispatch) leaves this function's output exactly as it was before this
    parameter existed.

    `tool_session_marker` (CQ·138) is one dispatch's own
    `create_tool_session_marker` FILE — never the scratch directory that
    holds it (an earlier design granted the whole directory, which also let
    a run rename, delete or symlink over the marker itself; the run-reported
    pid it also let a run write is the forgery `create_tool_session_marker`'s
    own docstring describes closing a different way).

    Measured directly against a real dispatch (2026-10-13), for both an
    ordinary and an `--eval-mode` dispatch alike: a write grant on the
    marker FILE alone lets a tool shell's `.zshenv` open it for append, and
    also lets the run delete the file outright with `rm` (exit 0 — a
    single-file `allowWrite` grant permits unlinking the file it names). It
    does NOT let the run create a new file in the marker's own directory,
    rename that directory, or rename/replace the marker with another file —
    every one of those needs write permission on the directory entry, not
    the file's own contents, and this grant never includes the directory
    (the same distinction `_protect_rest_of_git_dir`'s docstring makes about
    a loose object's mode). A run that deletes its own marker only defeats
    its own finalize-time sweep (`_verified_tool_session_marker` fails to
    open a missing file and signals nothing for that run); it gains no
    access to any OTHER dispatch's marker or directory, since
    `tool_session_scratch_root` keeps every dispatch's directory outside
    every dispatch's `allowWrite` — see that function's own docstring for
    what would reopen that.

    Added to `allowWrite` UNCONDITIONALLY — unlike `eval_scratch_dir`, which
    replaces the shared temp root in `allowWrite` for an eval dispatch, this
    file is added on top of whatever `allowWrite` already holds, eval
    dispatch or not: an eval dispatch's `allowWrite` is narrowed to its own
    `eval_scratch_dir` alone, and without this explicit addition that
    narrowing would silently drop the tool-session mechanism for every eval
    dispatch, the same way omitting `runs_root` drops it for every dispatch
    today. `runs_root` itself is unaffected either way — this parameter only
    ever adds a path outside it, never changes `denyWrite`.

    Round 9 (agent-platform#813 round 8 finding, security-reviewer): round 8 confined
    the WRITE but not the READ — several eval arms run against the same corpus entry
    at once, each under its own `create_eval_scratch_dir` directory, and nothing
    stopped a sibling arm's sandboxed Bash (`find`, `cat`) or its Read/Glob/Grep tools
    from reaching into another arm's directory and reading its draft verdict before
    the dispatcher ever collected it. Every such directory shares `EVAL_SCRATCH_PREFIX`,
    so `filesystem.denyRead` gets one glob per temp root (`eval_deny_read`, below) that
    denies READ to all of them at once — the SAME shape the module's own write-side
    narrowing could not use (see the paragraph above): here the deny is the broad
    match and the allow is the narrow one, so `filesystem.allowRead`, confirmed in the
    installed Claude Code binary's own settings-schema strings as taking precedence
    over `denyRead` for a matching path (its own description: "Paths to re-allow
    reading within denyRead regions. Takes precedence over denyRead for matching
    paths. Use with denyRead to deny a broad region then allow back specific
    subdirectories"), re-opens only THIS dispatch's own directory. No eval arm's
    claude process ever needs to read its own scratch directory back — the eval
    system-prompt directive only tells it to WRITE there, and `_collect_eval_verdict`
    reads it back from the unsandboxed dispatcher, after the run's claude process has
    already exited — so `allowRead` is not load-bearing for the harness to function;
    it is here so an ordinary Bash `cat` of a run's own just-written file is not
    refused for no reason, matching this dispatch's own `allowWrite` grant.

    Round 10 (agent-platform#813 round 9 finding, security-reviewer): round 9's
    `denyRead`/`allowRead` pair binds `sandbox.filesystem`, which the installed Claude
    Code binary's own strings confine to sandboxed commands (Bash) — "sandboxed
    commands" there is a category distinct from "file tools" (Read, Glob, Grep, Edit,
    Write), each enforced and propagated separately. A sibling eval arm's Read or Glob
    call, not routed through Bash, was never covered by round 9's fix. The missing
    half is the `permissions.deny` `Read(...)` entry this module already pairs with
    every other `sandbox.filesystem.denyRead` entry (`DENIED_DIRS`/`DENIED_FILES`,
    above, each get both a `denyRead` glob and a `Read(...)` deny rule) — `eval_deny_read`
    gets the same pairing below. `Read(...)` governs Read, Glob and Grep together (this
    function's own comment on `permissions`, below); no separate `Glob(...)`/`Grep(...)`
    rule exists in this module's contract. Deny wins over allow in the permissions
    layer, so this new rule also denies this dispatch's Read of its OWN directory —
    no `allowRead`-equivalent is added here to carve it back out, because no eval
    arm's claude process needs to read its own scratch directory (same reasoning as
    `allowRead` above): it only writes there with `Write`, and `_collect_eval_verdict`
    reads the file back from the unsandboxed dispatcher, after the run's claude
    process has already exited. `Write` is unaffected by any `Read(...)` rule: `Edit(path)`
    is what governs `Write` and `NotebookEdit` (`SCRATCH_ROOTS`'s own comment, above;
    confirmed in the binary's own strings, which group Edit/Write/NotebookEdit as one
    permission category distinct from Read/Glob/Grep) — a `Write(path)` rule is never
    consulted, so this dispatch's `scratch_allow_roots` `Edit(...)` allow rule for its
    own directory still stands."""
    extra = [Path(os.environ[EXTRA_DENY_HOME_ENV])] if os.environ.get(EXTRA_DENY_HOME_ENV) else []
    homes = _unique([Path(home), Path.home(), *extra])
    denied_dirs = _unique([h / d for h in homes for d in DENIED_DIRS])
    denied_files = _unique([_orient_token_file(h) for h in homes]
                           + [h / f for h in homes for f in DENIED_HOME_FILES])
    claude_md_excludes = _unique([h / USER_CLAUDE_MD for h in homes]) if exclude_user_claude_md else []
    # `/tmp` is `/private/tmp` on macOS (the sandbox does not resolve links), so both
    # spellings are writable. tmux's socket directory sits under each one, and that
    # directory is refused below, by name, for every root a run could reach it through.
    tmp_roots = [os.path.realpath(tempfile.gettempdir()), "/tmp", os.path.realpath("/tmp")]
    # Round 9: one glob per temp root, denying READ to every `create_eval_scratch_dir`
    # directory at once (see this function's own docstring for why `allowRead`, not an
    # omission, is what re-opens this dispatch's own). Empty for every non-eval
    # dispatch, which this list's own absence from `denyRead` below then leaves exactly
    # as unchanged as `allowWrite`'s own eval branch already keeps it.
    eval_deny_read = (_unique(Path(f"{t}/{EVAL_SCRATCH_PREFIX}*") for t in tmp_roots)
                     if eval_scratch_dir is not None else [])
    if eval_scratch_dir is not None:
        allow_write = [Path(eval_scratch_dir)]
    else:
        allow_write = [Path(t) for t in tmp_roots]
    # CQ·138: added on top, not folded into the eval/non-eval branch above —
    # see this function's own docstring for why an eval dispatch needs this addition
    # even though its `allowWrite` is otherwise narrowed to `eval_scratch_dir` alone.
    # The FILE, never its parent directory — see this function's own docstring
    # for what a file-only grant does and does not let the run do.
    if tool_session_marker is not None:
        allow_write.append(Path(tool_session_marker))
    tmux_dirs = _unique(tmux_socket_dir(Path(t)) for t in tmp_roots)
    deny_write = [worktree / f for f in WORKTREE_EDIT_DENIED] + tmux_dirs
    if git_dir:
        linked = _linked_gitdir(worktree)
        # Round 6: `objects` under `git_dir` is no longer granted at all (see
        # `_protect_rest_of_git_dir`'s docstring) — this run's own git writes new objects
        # here instead (`private_objects_dir`'s docstring says why this exact location),
        # and `import_private_objects` moves them into the shared store once the run ends.
        # Named explicitly in `allow_write` too, as the hedge `_branch_write_paths` already
        # is for the branch's own files: `_protect_rest_of_git_dir`'s exemption is what
        # actually keeps this writable (see its docstring for why `allowWrite` alone does
        # not control a path under a shared git directory).
        private_dir = private_objects_dir(git_dir, linked)
        allow_write.append(private_dir)
        branch = current_branch(worktree)
        if branch:
            allow_write += _branch_write_paths(git_dir, branch)
        # `_protect_rest_of_git_dir` is what actually narrows the write to this run's own
        # branch and worktree (see its docstring); `packed-refs` is denied unconditionally
        # ahead of it too — an ordinary commit and push to a repo with loose refs never
        # writes it, and it can hold a stale entry for every branch at once, so no
        # single-ref deny covers what rewriting it could do, and it may not exist yet at
        # spawn for the enumeration to find.
        deny_write += [git_dir / "hooks", git_dir / "config",
                       git_dir / "packed-refs", git_dir / "packed-refs.lock"]
        deny_write += _protect_rest_of_git_dir(git_dir, branch, linked, private_dir)
        if linked:
            allow_write.append(linked)
            deny_write += [linked / f for f in GIT_POINTER_FILES]
    allow_write = _unique(allow_write)
    if runs_root:
        deny_write.append(runs_root)

    deny = []
    for d in denied_dirs:
        deny += [f"Read({_rule_path(d)}/**)", f"Edit({_rule_path(d)}/**)"]
    for f in denied_files:
        deny += [f"Read({_rule_path(f)})", f"Edit({_rule_path(f)})"]
    # Round 10 (agent-platform#813 round 9 finding): pairs `eval_deny_read`'s
    # sandbox-layer glob with the `permissions.deny` `Read(...)` rule it was missing —
    # see this function's own docstring for why the sandbox-layer pair alone leaves
    # Read/Glob/Grep uncovered. Empty for every non-eval dispatch, same as `eval_deny_read`.
    deny += [f"Read({_rule_path(p)}/**)" for p in eval_deny_read]
    deny += [f"Edit({_rule_path(worktree / f)})" for f in WORKTREE_EDIT_DENIED]
    deny += [f"Edit({_rule_path(d)}/**)" for d in tmux_dirs]
    if runs_root:
        deny += [f"Edit({_rule_path(runs_root)}/**)"]
    for h in homes:
        for f in HOME_EDIT_DENIED:
            target = h / f
            suffix = "/**" if f == "Library/LaunchAgents" else ""
            deny.append(f"Edit({_rule_path(target)}{suffix})")
    # Under `dontAsk` a file tool needs an allow rule, and these are the only ones.
    # An eval dispatch gets its own directory instead of `SCRATCH_ROOTS` (see this
    # function's `eval_scratch_dir` docstring).
    scratch_allow_roots = ([Path(eval_scratch_dir)] if eval_scratch_dir is not None
                           else list(map(Path, SCRATCH_ROOTS)))
    allow = [*TOOL_ALLOW, *(f"Edit({_rule_path(p)}/**)" for p in [worktree, *scratch_allow_roots])]

    network = {
        "allowedDomains": list(NETWORK_ALLOWED),
        "strictAllowlist": True,
        "allowMachLookup": list(MACH_LOOKUP),
    }
    if repo in POSTGRES_SOCKET_REPOS:
        # Additive only: no other repo's `network` block changes shape.
        # `allowLocalBinding` stays unset, so TCP localhost (127.0.0.1:5433 included)
        # stays closed even for these repos — only the named socket path is reachable.
        network["allowUnixSockets"] = [agent_postgres_socket_path(home)]

    return {
        "sandbox": {
            "enabled": True,
            # A run whose sandbox cannot start is refused, not run unsandboxed.
            "failIfUnavailable": True,
            # Ignores the `dangerouslyDisableSandbox` parameter a model can set on a
            # Bash call. Also makes the sandbox admin-required: a repo's own settings
            # cannot add `excludedCommands` entries that leave it.
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": [str(p) for p in allow_write],
                "denyWrite": [str(p) for p in deny_write],
                "denyRead": [str(p) for p in [*denied_dirs, *denied_files, *eval_deny_read]],
                **({"allowRead": [str(eval_scratch_dir)]} if eval_scratch_dir is not None else {}),
            },
            "network": network,
        },
        # Read rules reach Read, Grep and Glob. Edit rules reach Edit, Write and
        # NotebookEdit. A deny rule is checked before any allow rule, so the allow list
        # above cannot re-open a denied path. The run's mode is `dontAsk` (agentctl), which
        # refuses any file write this allow list does not name.
        "permissions": {"allow": allow, "deny": deny},
        # A top-level settings key (`USER_CLAUDE_MD`'s docstring), not part of
        # "sandbox" or "permissions": it governs Claude Code's own memory-file
        # loading rather than the sandbox or the tool-permission system. Excludes
        # only the User-scope CLAUDE.md under each home; a worktree's own project
        # CLAUDE.md and the rendered `.claude/CLAUDE.md` are different paths and
        # keep loading.
        "claudeMdExcludes": [str(p) for p in claude_md_excludes],
    }


def settings_json(*, home: Path, worktree: Path, runs_root: Path | None = None,
                  git_dir: Path | None = None, exclude_user_claude_md: bool = False,
                  eval_scratch_dir: Path | None = None,
                  tool_session_marker: Path | None = None,
                  repo: str | None = None) -> str:
    return json.dumps(settings(home=home, worktree=worktree, runs_root=runs_root, git_dir=git_dir,
                               exclude_user_claude_md=exclude_user_claude_md,
                               eval_scratch_dir=eval_scratch_dir,
                               tool_session_marker=tool_session_marker, repo=repo),
                      sort_keys=True, separators=(",", ":"))


def run_name(run_id: str) -> str:
    """The `--name` value a dispatched run's claude is launched with (see `RUN_NAME_PREFIX`)."""
    return f"{RUN_NAME_PREFIX}{run_id}"


# How far up the process tree the ancestor walk below looks. A run's Bash reaches
# agentctl through a handful of shells, so the claude process is near the top — but
# see `inside_dispatched_run`'s docstring: this walk is the SECONDARY signal, and
# exhausting this limit now fails closed rather than concluding "not inside".
_ANCESTOR_LIMIT = 64

# The file this process probes to ask the OS sandbox itself whether it is confined
# (see `inside_dispatched_run`). Every dispatched run's sandbox denies reading it
# (`DENIED_DIRS`, above); a co, the steward and cron run unsandboxed and read it
# fine (or find it absent), so the probe never refuses them.
_SANDBOX_PROBE_FILE = Path(ORIENT_CONTROL) / "agent-platform-token"


def _sandbox_denies_read(path: Path) -> bool:
    """True when opening `path` for reading raises PermissionError: the OS sandbox's
    own confinement, which (unlike `ps` ancestry) a process cannot shed by forking,
    double-forking, or being reparented off its original lineage — the kernel enforces
    a sandbox profile on every descendant of the process it was applied to, wherever
    that descendant is reparented to, regardless of what the process table still shows.
    Any other outcome (readable, or a different OSError such as the file being absent)
    means this read was not denied by that confinement."""
    try:
        with open(path, "rb"):
            pass
    except PermissionError:
        return True
    except OSError:
        pass
    return False


def _ancestor_walk_sees_the_marker() -> bool:
    """The secondary signal: the `--name` argument in an ancestor's argv (claude
    runs a dispatched run's Bash through its own children, so it is an ancestor of
    every command the run starts; a run cannot change an ancestor's argv). Fails
    closed on every exit except cleanly reaching pid 1 having read every ancestor in
    between: an unreadable process table, a malformed line, AND exhausting
    `_ANCESTOR_LIMIT` without ever reaching pid 1 (a run's Bash can interpose more
    ancestors than this walk will read, by nesting subshells) all return True,
    because the one caller refuses dispatch on True.

    On this machine's sandbox, `ps` itself is denied to a sandboxed process (measured:
    `ps -p $$` from a sandboxed Bash returns `operation not permitted`, exit 127), so
    this walk already fails closed on its very first iteration for every nested call,
    independent of the marker or of `_ANCESTOR_LIMIT` — the two bugs this function
    fixes are real bugs in what this code would do if `ps` worked, not reproducible as
    a live bypass on this platform today. `_sandbox_denies_read` does not depend on
    `ps` at all, which is the reason it is the primary signal rather than this one."""
    pid = os.getppid()
    for _ in range(_ANCESTOR_LIMIT):
        try:
            ps = subprocess.run(["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return True
        # Every pid here is this process's ancestor, so it exists: a non-zero exit or an
        # unreadable line means the process table is not being read, not that the chain ended.
        fields = ps.stdout.strip().split(None, 1)
        if ps.returncode != 0 or len(fields) != 2 or not fields[0].isdigit():
            return True
        if RUN_NAME_PREFIX in fields[1]:
            return True
        pid = int(fields[0])
        if pid <= 1:
            return False
    # The chain never reached pid 1 within _ANCESTOR_LIMIT hops, with every one of
    # them read cleanly and none carrying the marker. An ordinary chain is a handful
    # of shells deep; one grown past this limit on purpose is grown to evade this
    # walk, and reporting "not inside" on a chain this walk never finished reading is
    # exactly the bypass it exists to refuse.
    return True


def inside_dispatched_run() -> bool:
    """True when this process runs under a dispatched run's claude.

    Two signals, either of which is enough to answer True:

    1. (Primary) `_sandbox_denies_read` on `_SANDBOX_PROBE_FILE`: every dispatched
       run's claude is launched with the OS sandbox enabled (`settings()`'s own
       `"enabled": True`), which denies that path to every descendant regardless of
       process ancestry. This is the signal a run cannot shed by reparenting: a
       double-forked orphan is still a descendant of the sandboxed claude process as
       far as the kernel's confinement is concerned, wherever it lands once its
       immediate parent exits (pid 1, or a live subreaper — measured live, see
       `tests/live/test_dispatched_run_confinement.py::test_a_double_forked_orphan_still_cannot_dispatch`),
       even though `ps` no longer shows the relationship.
    2. (Secondary) `_ancestor_walk_sees_the_marker`: the pre-existing `ps`-ancestry
       check, kept as defence in depth for a machine where the probe file happens to
       be absent in both the sandboxed and unsandboxed case alike.

    Clearing an environment variable, which a run's shell can do with `env -u`,
    touches neither signal."""
    if _sandbox_denies_read(passwd_home() / _SANDBOX_PROBE_FILE):
        return True
    return _ancestor_walk_sees_the_marker()
