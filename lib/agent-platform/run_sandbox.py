"""The Claude Code sandbox every dispatched run is launched under.

The operator chose Option A on 2026-10-06 (Slate a-ow0q4o): the `ops-co` grant in
`~/.orient-control/ops-co-<slug>-token` must not be readable by a dispatched run — nor
should any crew's own `~/.orient-control/ops-<slug>-token` reach the run's own tool
calls, rather than only its ops MCP server process. A run executes as the same Unix
user as the co, so file modes cannot keep it out. The sandbox does: `settings_json`
builds the `--settings` value that `bin/agentctl`'s `_spawn_claude` and
`review_launch.session_command` pass to `claude -p`.

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
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import builder_root as BR
import projects

# The directory the grant lives in, under the account home a run's claude uses.
ORIENT_CONTROL = ".orient-control"

# Whole directories under the account home that a run may not read. Each one holds
# credentials that a run has no reason to read:
# - `.orient-control`: every crew's `ops-<slug>-token` and `ops-co` grant
#   (`ops-co-<slug>-token`), `agent-platform-token` (the
#   review-record and signal credential; see `mcp-verdict-server`), a private crew's
#   own `review-record-writer-<product>-token` (CQ·557, verdict-only, scoped to one
#   product — see `run_verdict.private_review_token_path`), Deck's approval
#   credential (`deck-library-token`, which the Library answers as actor `deck`) and
#   `library-client-token`.
# - `.library`: `client-tokens.json` (the `deck` and person-login secrets), a saved copy
#   of a person's login (`person_*.token.new`), `ci-fetch-token`, the Library's
#   databases and `tls/server.key`.
# - `.local/state/library`: the same `client-tokens.json`, plus `api-tokens.json`, on an
#   install where the Library moved its state here instead of `.library`
#   (issue_01M6A7CBACJG5XTR1JK4WH6PRQ) — a run reading either file reads the Library's own
#   client credential just as directly as it would under `.library`.
# - `.config/gh`: `gh`'s own config, including `hosts.yml`, where it stores the operator's
#   token when it isn't in the keychain (CQ·418). `dispatch_env` already points a run's
#   `GH_CONFIG_DIR` at an empty per-run directory, so this is the second layer: even a run
#   that reset that variable itself cannot read the real one back.
# - `Library/Keychains`: the macOS keychain files `gh auth status` reports the operator's
#   token is stored in (CQ·418). A file-read deny does not by itself stop a Keychain
#   Services call through `securityd` — see `MACH_LOOKUP`'s comment below for why that
#   path is denied by a different mechanism — but it does stop a run that tried to read
#   the keychain database directly.
# - `.ssh`: the operator's own SSH keys. A dispatched coder, run-c36c0559, read
#   `~/.ssh/github_ed25519` and pushed its branch to GitHub as the operator on
#   2026-10-19, after `agentctl publish` refused inside the run (CQ·506 step S8).
DENIED_DIRS = (".orient-control", ".library", ".local/state/library", ".config/gh",
              "Library/Keychains", ".ssh")

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

# Six more files under the account home that a run may not read (CQ·418): `.gitconfig`,
# where the operator's `credential.https://github.com.helper` points at `gh`; the
# install's own GitHub App's private key, which signs the JWT that mints every run's
# OWN installation token — that minting happens in the dispatcher (`agentctl`), before
# this run starts, and the key must never be reachable from inside one. Two paths name
# it: `.config/convivy/github-app/app.pem`, where `github_app.installed_app()` resolves
# it (issue_01M69GD1WEN67MZPSBQZD02CDN), and `.config/convivy/bosun-app.pem`, a
# pre-existing key file on the operator's machine — on an install that kept the older
# key file, `github-app/app.pem` is a symlink TO `bosun-app.pem`, so both names are
# denied, and `path_denied`'s own `Path.resolve()` denies the symlink through its
# target either way; the Library's
# bosun-ledger-reporter credential, which `agentctl` will use to POST spend entries
# (including subscription-lane ones) to the ledger — a run that could read it could
# forge spend records. Denied ahead of the Library minting it, so there is never a
# window where the file exists and is readable from inside a run.
#
# The next, `.codex/auth.json`, is the Codex CLI's own login credential, holding
# the operator's ChatGPT subscription grant since the CLI logged in on this machine
# (2026-10-17). Codex itself is launched by the dispatcher, outside a run's sandbox,
# so no run ever needs to read it. Only the file is denied, not `.codex` itself: a
# later Codex runner may still need to read the directory's own configuration.
#
# The last, `.claude/.credentials.json`, is Claude Code's own sign-in file. A
# dispatched coder read it into its run log on 2026-10-07. Claude Code itself is
# launched by the dispatcher, outside a run's sandbox, so no run ever needs to read
# it. Only the file is denied, not `.claude` itself: a run's own CLAUDE.md render
# lives under that directory.
DENIED_HOME_FILES = (".gitconfig", ".config/convivy/bosun-app.pem",
                      ".config/convivy/github-app/app.pem",
                      ".config/convivy/bosun-ledger-reporter.token",
                      ".codex/auth.json", ".claude/.credentials.json")

# Epic E33 slice 2: the account-home directories, under the home a dispatched
# run's claude is launched with, that `settings()` re-opens on top of the
# worktree itself — never `DENIED_DIRS`/`DENIED_HOME_FILES`, and never a
# directory wide enough to also hold one of them (`.claude` itself holds the
# sign-in file; `.config/convivy` holds the two private keys `DENIED_HOME_FILES`
# names). Built from the epic's own measurement of what a dispatched run reads
# outside its worktree:
# - `.claude/shell-snapshots`/`.claude/session-env`: Claude Code's own
#   shell-snapshot and session-environment capture, which its Bash tool
#   sources before every command (confirmed against the installed binary's
#   own on-disk layout).
# - the package-manager and build-tool caches a coder's own Bash reaches
#   directly: `uv`, pip, npm (never `.npmrc`, which this tuple does not name),
#   Swift Package Manager (two separate cache locations plus its own
#   `.swiftpm`), and Playwright's browser cache.
# - the symlink farm `install.sh` writes under this account's own home
#   (`.local/bin`, `.local/lib/agent-platform`) and the one
#   `bin/install-agent-platform-shared` writes the same way
#   (`.local/deploy/agent-platform-shared`) — every one of them a farm of
#   links INTO the primary checkout; `_primary_checkout_read_dirs` below is
#   where those links resolve to, read directly rather than by following the
#   link at render time.
# - the two interpreters a dispatched run's own venvs live under outside the
#   primary checkout.
READ_ALLOWLIST_RELDIRS = (
    Path(".claude") / "shell-snapshots",
    Path(".claude") / "session-env",
    Path(".cache") / "uv",
    Path("Library") / "Caches" / "pip",
    Path(".npm"),
    Path("Library") / "Caches" / "org.swift.swiftpm",
    Path("Library") / "org.swift.swiftpm",
    Path(".swiftpm"),
    Path("Library") / "Caches" / "ms-playwright",
    Path(".local") / "bin",
    Path(".local") / "lib" / "agent-platform",
    Path(".local") / "deploy" / "agent-platform-shared",
    Path(".local") / "venv-orient",
    Path(".local") / "venv-llm-gateway",
)


def _dispatch_host_platform() -> str:
    """`sys.platform`, read through one named function rather than inline, so a
    test can patch this single seam (`monkeypatch.setattr(sys, "platform", ...)`,
    the same global `pin_claude` already reads) instead of reasoning about every
    place this module might otherwise call `sys.platform` directly. Always the
    REAL platform of the machine running this dispatch, never a value derived
    from `home`/`worktree`: both can name any account's directories on this one
    machine, but the installed Claude Code binary that will execute the
    rendered settings is whatever this machine actually has."""
    return sys.platform


# Measured on an adopter's Linux install (Debian 12, Claude Code 2.1.283, bwrap),
# 2026-10-31: every dispatched run's Bash call failed at sandbox start with
# "bwrap: Can't create file at /home/<account>/.claude/session-env/.mcp.json:
# Read-only file system". Deleting `session-env` from `READ_ALLOWLIST_RELDIRS`
# moved the identical error to `.claude/shell-snapshots/.mcp.json` — proof the
# installed Linux binary re-opens BOTH of these two directories itself, under its
# own read-only bwrap bind, independently of whatever this module's own `allowRead`
# grants: removing OUR grant for one did not stop ITS crash, it only changed which
# of its two self-managed binds hit the error first. Creating an empty `.mcp.json`
# by hand inside both directories let Bash run, which is what shows the binary
# treats that filename as protected inside every directory it opens — including
# the two it opens on its own — and tries to mask it there with a file bwrap
# cannot create inside a read-only bind. This module must never precreate that
# file in a user's `~/.claude` to work around it: the two directories named here
# are simply never ours to re-open on Linux, since the binary already opens them
# itself.
#
# No other `READ_ALLOWLIST_RELDIRS`/`READ_ALLOWLIST_RELFILES` entry sits under
# `.claude` at all. `read_allowlist`'s own `.claude/projects/<slug>` entry does —
# but that directory holds this project's saved transcripts, reached only through
# this module's own `permissions.additionalDirectories` grant (Read/Grep/Glob),
# never sourced by the Bash tool before a command the way these two are; nothing
# measured here shows the binary re-opening it on its own, so it keeps its
# ordinary shape and stays in the render on every platform.
LINUX_SELF_REOPENED_RELDIRS = (
    Path(".claude") / "session-env",
    Path(".claude") / "shell-snapshots",
)

# Two files under `.config/convivy`, re-opened by name rather than as part of
# the directory that also holds `DENIED_HOME_FILES`'s `bosun-app.pem` and
# `bosun-ledger-reporter.token`:
# - `session-agent.env`: `builder_root.root()` reads this on every in-run
#   `agentctl` call (every subcommand a dispatched run's own Bash invokes
#   resolves the platform root through it).
# - `operator.json`: `publish_scan.shipwright_checkout` reads this during
#   `agentctl publish`'s own content scan, which a dispatched coder's own
#   `git push` (the installed pre-push hook) and `agentctl publish` both run
#   from inside the run.
READ_ALLOWLIST_RELFILES = (
    Path(".config") / "convivy" / "session-agent.env",
    Path(".config") / "convivy" / "operator.json",
)


def _builder_root_marker_read_path() -> Path:
    """`BR.root() / BR.MARKER`, the marker file alone, never its parent
    directory — the file an in-run `agentctl` call's own `BR.report_unconfirmed`
    reads on every `runs_root()`/`data_root()` call (epic E33 slice 2: a
    dispatched run's own `agentctl` printed `the marker check failed
    (PermissionError(1, 'Operation not permitted'))` once the home directory
    it sits under was denied wholesale). Read directly, the same way
    `_primary_checkout_read_dirs` reopens the primary checkout's paths,
    rather than through whatever `home` this dispatch was given: `BR.root()`
    is this machine's one platform root regardless of `home`."""
    return BR.root() / BR.MARKER


def _primary_checkout_read_dirs() -> "list[Path]":
    """The primary agent-platform checkout's own `bin`, `lib/agent-platform`,
    `config` and `.venv` — what every `.local/...` entry in
    `READ_ALLOWLIST_RELDIRS` ultimately resolves to (`install.sh`'s `ln -sfn`,
    `bin/install-agent-platform-shared`'s own copy). Read directly, by this
    module's own `BR.root()`, rather than by following the symlink at render
    time: the symlink lives under whichever `home` this dispatch was given,
    but the file it resolves to is this one, on THIS machine, regardless of
    `home` — there is one primary checkout per machine (company/decisions/
    0008), never one per account. Both the link (`READ_ALLOWLIST_RELDIRS`,
    under `home`) and this, its target, must be readable for a dispatched
    run's own `agentctl` invocation (`.local/bin/<tool>`, resolved through
    `Path(__file__).resolve()`) to import anything."""
    primary = BR.root() / "agent-platform"
    return [primary / "bin", primary / "lib" / "agent-platform",
            primary / "config", primary / ".venv"]


def _project_slug(path: str) -> str:
    """Claude Code's own directory name, under `<home>/.claude/projects/`, for a
    working directory's saved transcripts and spilled tool output — the same
    computation as `co_restore.project_slug`, confirmed against the installed
    binary (tests/live/test_dispatch_read_allowlist_live.py). Kept as a local,
    one-line copy rather than an import: `co_restore` pulls in `argparse`,
    `private_crew` and `tmux_transcript`, none of which this module's callers
    have any other reason to load."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def read_allowlist(*, home: Path, worktree: Path, git_dir: "Path | None" = None,
                   linked: "Path | None" = None, run_dir: "Path | None" = None,
                   tool_session_marker: "Path | None" = None,
                   scratch_roots: "Iterable[Path]" = ()) -> "list[Path]":
    """Every path, besides the worktree's own ordinary reach, that a dispatched
    run's claude may read once its home is denied wholesale (epic E33 slice 2)
    — the ONE list `settings()` renders into both `sandbox.filesystem.allowRead`
    (Bash) and `permissions.additionalDirectories` (Read, Grep, Glob).

    `worktree` is always included, even though it is also this run's cwd (and
    so already reachable for Read/Grep/Glob without being named here): the
    Bash/Seatbelt layer has no such "cwd is always open" rule, and `worktree`
    sits under `home`, which this function's caller now denies wholesale.

    `git_dir`/`linked` (from `git_common_dir`/`_linked_gitdir`), `run_dir` and
    `tool_session_marker` are each `None` for a caller that has none to give
    (a non-git agent, a caller with no run directory of its own, a dispatch
    with no tool-session marker yet) — `None` adds nothing. `run_dir` is this
    dispatch's OWN run directory, never the run-records root it sits under
    (`settings()`'s own `trial` branch never passes this parameter at all —
    see its docstring on why a trial dispatch's run directory stays closed).
    `tool_session_marker` is the marker FILE alone (CQ·138), never its own
    scratch directory's parent — see that constant's own comment for why the
    directory itself must never be granted.

    `scratch_roots` is `settings()`'s own `scratch_allow_roots` — the same
    eval/review/ordinary branching its `allowWrite` already uses (an eval
    dispatch's own scratch directory, a review dispatch's own draft directory
    or nothing, else the shared temp roots) — passed in rather than
    recomputed here, so the write and read sides can never name two different
    directories for the same dispatch.

    On Linux (`_dispatch_host_platform()`), `LINUX_SELF_REOPENED_RELDIRS` is
    dropped from `READ_ALLOWLIST_RELDIRS` before it is rendered — see that
    constant's own comment for the measured crash this avoids. macOS is
    unaffected: `_dispatch_host_platform()` there is `"darwin"`, which never
    matches, so this function's output for it is unchanged, byte for byte,
    by this exception's existence."""
    reldirs = tuple(d for d in READ_ALLOWLIST_RELDIRS if d not in LINUX_SELF_REOPENED_RELDIRS) \
        if _dispatch_host_platform().startswith("linux") else READ_ALLOWLIST_RELDIRS
    paths: list[Path] = [Path(worktree)]
    paths += [Path(home) / d for d in reldirs]
    paths += [Path(home) / f for f in READ_ALLOWLIST_RELFILES]
    paths += _primary_checkout_read_dirs()
    paths.append(_builder_root_marker_read_path())
    paths.append(Path(home) / ".claude" / "projects" / _project_slug(str(Path(worktree).resolve())))
    if git_dir is not None:
        paths.append(Path(git_dir))
    if linked is not None:
        paths.append(Path(linked))
    if run_dir is not None:
        paths.append(Path(run_dir))
    if tool_session_marker is not None:
        paths.append(Path(tool_session_marker))
    paths += [Path(p) for p in scratch_roots]
    return _unique(paths)


# The auto-memory directory Claude Code keeps per project at
# `~/.claude/projects/<slug>/memory/`, loaded as trusted notes (`MEMORY.md`, its
# index) at the start of every session on that project. Every worktree of
# agent-platform resolves to the SAME slug as the operator's own primary checkout,
# so a dispatched run in any worktree — a reviewer on an author-controlled branch, an
# `--eval-mode` replay against an untrusted model, an ordinary coder — can plant text
# there that the orchestrator's own next session loads as its own prior notes, with no
# review of its own. `*` matches any project's slug, not only the run's own repository's:
# `Edit`/`Write` take any absolute path a run names, so the deny must cover every slug
# this account could ever have, not the one dispatch happened to resolve. The directory
# this account's own interactive `claude` sessions read and write is the SAME one a
# dispatched run's claude would reach without this rule — nothing here touches that
# interactive session itself, only what a dispatched run's claude is permitted to write.
MEMORY_WRITE_DENIED_RELDIR = Path(".claude") / "projects" / "*" / "memory"

# Set unconditionally in every dispatched run's own settings, on top of the write
# deny above: Claude Code's own `autoMemoryEnabled` key (confirmed in the installed
# binary's own settings schema, 2.1.302 — "Enable auto-memory for this project. When
# false, Claude will not read from or write to the auto-memory directory") turns off
# the HARNESS's own auto-memory feature for the run that carries it: with this key
# false, Claude Code itself neither loads `MEMORY.md` into the run's own context at
# startup nor writes it afterward. That is a narrower thing than it sounds: the key
# governs only Claude Code's own auto-memory feature, not the sandbox or the
# tool-permission system, so it does not stop the run's Bash or Read tool from
# opening these files directly — that channel is left open by this key and by the
# write deny above, which covers only Edit/Write. These files hold operating notes,
# not credentials, and no occurrence has shown a run reading them to be a harm, so
# this change does not add a read deny; it closes the write channel into the
# orchestrator's trusted memory, which is what this PR is for.
AUTO_MEMORY_DISABLED = {"autoMemoryEnabled": False}

# Tools a dispatched run may call without a prompt. Under `dontAsk` anything not listed
# here, or in a rule from this module, is refused. The list is what the run records show
# coders and reviewers calling (their `tool_calls.jsonl`, 2026-08 to 2026-10). `Bash`
# is unrestricted by this list; the sandbox confines it. `Read` covers reads anywhere;
# the deny rules above still refuse the credential paths. The `mcp__` entries are whole
# servers, which the run has today under `bypassPermissions`.
#
# NEVER THE WHOLE STORY FOR reviewer/security-reviewer (platform/specs/
# reviewers-run-under-their-allow-list): a review-class dispatch's bare `Bash` grant is
# what let a security-reviewer's `python3 - <<'EOF'` boot out seven live launchd jobs
# on 2026-10-16 (`run-4a2b213d`) — the permission MODE was already `dontAsk`, but this
# list's own bare `Bash` matched anyway. `settings()`'s `review_allowed` parameter
# REPLACES this whole list, unconditionally, for those roles: see its own docstring.
#
# `WebFetch`/`WebSearch` are NOT in this list (epic E33 slice 3): web tools are
# granted per agent, through `settings()`'s own `web_tools` parameter, never
# unconditionally for every dispatch — see that parameter's docstring for the one
# place they enter a run's surface.
TOOL_ALLOW = (
    "Bash", "Read", "Grep", "Glob", "Agent", "Skill",
    "ToolSearch", "TaskOutput", "TaskStop", "Monitor", "ScheduleWakeup",
    "EnterWorktree", "ExitWorktree", "ReportFindings",
    "mcp__ops", "mcp__kb", "mcp__verdict", "mcp__claude_ai_Claude_Docs",
)

# The only two tool names `settings()`'s `web_tools` parameter may carry (epic E33
# slice 3). Not a general-purpose tool-name validator; this module names exactly
# the two built-ins the operator's ruling (2026-10-24) ties to a dispatch's own
# repo, and nothing else goes through that gate.
WEB_TOOLS = ("WebFetch", "WebSearch")

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
# dispatch's own. Also what `_existing_eval_sibling_dirs` (round 11, agent-platform#835)
# matches a temp root's entries against to find a sibling's directory by listing the
# filesystem, rather than a wildcard, for `permissions.deny`'s own `Read(...)` rule.
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


# The `mkdtemp` prefix a `--trial` dispatch's own zdotdir base directory is named
# with (agent-platform#934 round 2, reviewer finding). Distinct from
# `EVAL_SCRATCH_PREFIX`: the two serve unrelated purposes and nothing here needs
# them to share a glob.
TRIAL_ZDOTDIR_PREFIX = "agentctl-trial-zdotdir-"


def create_trial_zdotdir_base() -> Path:
    """Creates and returns a fresh directory for a `--trial` dispatch's own
    ZDOTDIR (agent-platform#934 round 2, reviewer finding) — outside `runs_root`
    AND outside the worktree, the same shape `create_eval_scratch_dir` already
    uses, and for a related reason: `settings()`'s `trial` branch now denies
    reading the WHOLE of `runs_root`, and `<run_dir>/zdotdir/.zshenv` — the one
    file every one of this run's tool shells legitimately reads for the life of
    the run, see `_write_tool_session_zdotdir`'s own docstring in `bin/agentctl`
    — sits under `run_dir`, which sits under `runs_root`. A trial dispatch's
    zdotdir moves here instead, so that read keeps working under the new deny.

    `tempfile.mkdtemp` under the resolved system temp root, mode 0700
    (`mkdtemp`'s own default — owner-only), named with `mkdtemp`'s own random
    suffix. No write grant is needed for this directory or the `.zshenv`
    written inside it: nothing in `settings()` denies reading an ordinary path
    outside `runs_root` by default, so sitting outside the new deny is the
    whole of what this directory has to do — unlike `create_eval_scratch_dir`,
    this is never added to `allowWrite`.

    Created once per dispatch, by the dispatcher itself, before this run's
    claude process ever starts — reused on every ARCH-131 fail-over respawn
    the same way `create_tool_session_scratch_dir`'s directory already is, so
    a respawn's tool shells keep reading the same `.zshenv`. `None` for every
    non-trial dispatch, which never calls this at all."""
    root = os.path.realpath(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix=TRIAL_ZDOTDIR_PREFIX, dir=root))


# Where a review-class dispatch's draft findings live once `TOOL_ALLOW` no longer
# applies to it (platform/specs/reviewers-run-under-their-allow-list): with no bare
# `Bash` and no `/tmp` grant, a reviewer's `cat >`/`mkdir -p` into the gitdir — the
# habit `review_tools`'s own measurement found, since reviewers have no `Write` tool
# today either — has nowhere to go; the gitdir is a protected path `dontAsk` refuses
# regardless of any allow rule, and a re-pin runs `clean -fdx` on the worktree
# (`_pin_existing_to_commit`), which would discard a draft left inside it.
#
# Keyed on the WORKTREE'S OWN NAME, not a run id or a random suffix: a re-dispatch onto
# the same worktree (a round-fix, or ARCH-131's own fail-over respawn) must find the
# same directory, the way `tool_session_marker` is reused across a respawn — but this is
# reused across a whole separate re-dispatch, which a fresh `mkdtemp` suffix would not
# survive. Lives under the account's own per-user state tree, the same family
# `TOOL_SESSION_SCRATCH_RELDIR` already uses for state no dispatch's `allowWrite` should
# ever cover — never under /tmp, which a review-class dispatch's grant drops entirely
# (see `settings()`'s `review_allowed` parameter).
REVIEW_DRAFT_RELDIR = Path(".local/state/agent-platform/review-drafts")


def review_draft_dir(home: Path, worktree: Path) -> Path:
    """Where `create_review_draft_dir` creates (or finds) one review-class dispatch's
    own draft directory, under the given account home — keyed on `worktree`'s own
    basename so a re-dispatch onto the same worktree finds the same directory. Read by
    both the dispatcher (to create it before the run starts) and `settings()` (to grant
    it as this dispatch's `draft_dir`)."""
    return Path(home) / REVIEW_DRAFT_RELDIR / Path(worktree).name


def create_review_draft_dir(home: Path, worktree: Path) -> Path:
    """Creates (if absent) and returns this review-class dispatch's own draft
    directory — see `review_draft_dir`'s docstring for why it is keyed on the
    worktree's name rather than a fresh `mkdtemp` suffix. Mode 0700 (owner only),
    the same as every other scratch directory this module creates. Created once per
    dispatch, by the dispatcher, before the run's claude process starts — never by
    the run itself."""
    d = review_draft_dir(home, worktree)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


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

# A dedicated Postgres cluster for the repos `postgres_socket_repos()`
# names (round-1 revise on PR #817): a product coder's test suite
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
# `_spawn_claude` already passes as `repo` (`args.on`). A repo gets the grant
# by setting `postgres_socket: true` on its own unit in the project registry —
# absent means no grant, the conservative default, since this opens a new
# sandbox exception a contributor should ask for by name rather than receive
# automatically. Read by `postgres_socket_repos()` below EACH TIME `settings()`
# needs it, never cached at import time: a module-level read made every
# importer's import fail (FileNotFoundError, not merely a missing grant) in a
# process whose registry is unreadable or absent, such as a test building a
# self-contained lib/agent-platform copy with no config/ beside it
# (tests/test_agentctl_module_resolution.py). A dispatch is a fresh process
# either way, so nothing here was ever stale from caching; there was just
# never a reason to risk the crash for it.
AGENT_POSTGRES_RELDIR = Path(".local/state/agent-platform/postgres")
AGENT_POSTGRES_PORT = 5433
AGENT_POSTGRES_ROLE = "agent"


def postgres_socket_repos() -> tuple[str, ...]:
    """Repo names granted the agent Postgres cluster's unix socket, read fresh
    from `projects.postgres_socket_repos()` on every call.

    A registry that cannot be read (missing file, permission error) degrades
    to no grant for anyone — the same conservative default an absent
    `postgres_socket` field already gets a unit — rather than crashing the
    caller; this is the one sandbox exception, not a feature anything depends
    on being present to run."""
    try:
        return tuple(projects.postgres_socket_repos())
    except OSError as e:
        print(f"run_sandbox: postgres_socket_repos() could not read the "
              f"registry ({e}); no repo gets the Postgres-socket grant this "
              f"run.", file=sys.stderr)
        return ()


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


def _branch_write_unsafe_roots(git_dir: Path) -> set[Path]:
    """The four directories a branch's write-grant DIRECTORY must never equal: the
    shared roots every branch in the repository lives (or logs) under. Shared between
    `_branch_write_dirs` and `linux_branch_write_note`, which must agree on exactly
    the same condition — one acts on it, the other explains it."""
    return {
        git_dir / "refs" / "heads",
        git_dir / "refs" / "remotes" / "origin",
        git_dir / "logs" / "refs" / "heads",
        git_dir / "logs" / "refs" / "remotes" / "origin",
    }


def _branch_write_dirs(git_dir: Path, branch: str) -> list[Path]:
    """The enclosing directory of each `_branch_write_paths` entry, deduplicated —
    what `settings()` grants instead of the exact files themselves when
    `_dispatch_host_platform()` is Linux.

    Measured on an adopter's Linux install (Debian 12.15, bwrap), 2026-10-31: every
    dispatched run's `git commit` failed with "fatal: couldn't set
    'refs/heads/<branch>'", leaving a stale `refs/heads/<branch>.lock` holding the
    new commit id — the rename that would have removed it, by replacing
    `refs/heads/<branch>` with it, never completed. The installed binary bind-mounts
    each `allowWrite` entry on its own, and a rename onto or out of a bind-mount
    point fails (Linux's `rename(2)`: "newpath and oldpath are not on the same
    mounted filesystem", true even of two mounts of the very same underlying
    directory). `_branch_write_paths` lists the ref and its `.lock` as two such
    entries, so the run's own lock-and-rename was never going to succeed. Granting
    the ref's own ENCLOSING DIRECTORY instead puts the ref and its lock file on one
    mount, the way `/tmp` and `/private/tmp` already sit in `allowWrite` as
    directories for every platform's scratch writes — so the rename succeeds.

    This does not widen what a sibling branch can have done to it, PROVIDED `branch`
    has a namespace segment of its own (`agent/<x>`, every real dispatched branch's
    own shape): a directory named here is granted to `allowWrite` BECAUSE it holds
    this run's own branch file, but `_protect_rest_of_git_dir` below is what actually
    narrows the write, and it enumerates every directory it must open this way and
    denies every entry besides the branch's own named files — including a sibling
    branch already in the same directory (`refs/heads/agent/`, shared by every
    dispatched run's own branch) at the time this module's `settings()` is called. A
    sibling branch some OTHER run creates in that same directory AFTER this one's
    `settings()` already ran carries no `denyWrite` entry of its own either way —
    `_protect_rest_of_git_dir`'s own docstring already names this as the one gap
    enumeration cannot close ("a path created after this call... is not enumerated
    and so is not covered"), on every platform, whether `allowWrite` names the file
    or (here) its directory.

    RETURNS NO ENTRIES AT ALL — not even the shared root — when `branch` has no
    namespace segment of its own (no `/`): its ref then sits directly in `refs/heads`
    itself, and the same is true of its remote-tracking ref under
    `refs/remotes/origin` and both reflogs (`_branch_write_unsafe_roots`). Granting
    that directory would hand this run write access to every branch's ref in the
    repository, present and future, not just its own (security review, PR #1111
    round 1): `current_branch()` returns whatever `HEAD` actually names, and a
    `--pr N` round-fix checks out the PR's own `headRefName` as given
    (`bin/agentctl`), with no `agent/`-prefix normalization — a hand-opened PR branch
    with no `/` (`fix-ci`, say) reaches this function with exactly that shape. There
    is no directory narrower than the shared root to grant for a branch with no
    namespace of its own, and refusing the whole dispatch over it (round 1 of this
    fix) blocked every Linux run on such a branch, including a read-only one that
    never writes at all — so round 2 grants nothing for the branch's ref, lock,
    remote-tracking ref or reflogs instead of widening to the shared root: a commit
    this run attempts fails cleanly when git tries to create the ref's `.lock` file
    (denied, not merely unopened for a rename), leaving no stale lock, while every
    other part of the dispatch — including a run that never commits — proceeds.
    `linux_branch_write_note` is the reason text for this empty grant; `settings()`
    does not call it itself, so `bin/agentctl` calls it separately to log why."""
    dirs = _unique(p.parent for p in _branch_write_paths(git_dir, branch))
    if _branch_write_unsafe_roots(git_dir).intersection(dirs):
        return []
    return dirs


def linux_branch_write_note(git_dir: Path, branch: "str | None") -> "str | None":
    """None unless this dispatch is on Linux and `branch` has no namespace segment of
    its own — in which case, the reason `_branch_write_dirs` grants it no write at all
    on its ref, lock, remote-tracking ref or reflogs, for `bin/agentctl` to log into
    the run's own stderr.log and policy.json before it calls `settings_json()` (whose
    own return value, the rendered sandbox JSON, carries no room for a reason).
    `branch` is `None` for a detached HEAD (a reviewer pinned to a commit), which
    writes no branch ref at all and so needs no note."""
    if branch is None or not _dispatch_host_platform().startswith("linux"):
        return None
    dirs = _unique(p.parent for p in _branch_write_paths(git_dir, branch))
    hit = sorted(str(p) for p in _branch_write_unsafe_roots(git_dir).intersection(dirs))
    if not hit:
        return None
    return (
        f"branch {branch!r} has no namespace segment of its own, so this Linux "
        f"dispatch grants it no write at all on its ref, lock, remote-tracking ref or "
        f"reflogs — granting {hit} instead would cover every branch in the "
        "repository, present and future, not just this run's own. A commit this run "
        "attempts fails cleanly when git tries to create the ref's lock file, leaving "
        "no stale lock; every other part of the dispatch proceeds.")


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
    nothing from `_branch_write_paths`, so every existing ref is protected. `settings()`
    passes `None` here for a real branch too, when it has no namespace segment of its
    own on a Linux dispatch (`_branch_write_dirs` already grants that branch no
    `allowWrite` entry at all) — exempting its files here, while granting them nothing,
    would leave them writable anyway (absent from both lists, per this function's own
    opening paragraph), so `settings()` withholds the exemption instead, and this walk
    protects that branch's ref, lock, remote-tracking ref and reflogs the same way it
    already protects a detached HEAD's. A path created AFTER this call (a concurrent
    fetch, a branch this run itself creates, a sibling worktree added after this run
    started) is not enumerated and so is not covered — enumerating what exists is the
    only non-guessing way to do this, and what does not exist yet cannot be enumerated.
    `private_dir` is the one exception: it is named explicitly rather than discovered by
    enumeration, so it stays exempt whether or not it has been created on disk yet by the
    time this runs."""
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


def denied_read_paths(homes: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    """(denied_dirs, denied_files): every path under the given homes that no run may
    read — `DENIED_DIRS`, `DENIED_HOME_FILES` and each home's own Orient token file.
    The ONE harness-neutral list (platform/specs/codex-coder-lane, slice 1): `settings()`
    renders it into a Claude dispatch's `sandbox.filesystem.denyRead` and
    `permissions.deny` `Read(...)` rules below, and `codex_permission_overrides` (also
    below) renders the SAME list into a Codex permission profile's filesystem overrides —
    neither renderer adds a path the other does not already deny."""
    homes = _unique(homes)
    denied_dirs = _unique([h / d for h in homes for d in DENIED_DIRS])
    denied_files = _unique([_orient_token_file(h) for h in homes]
                           + [h / f for h in homes for f in DENIED_HOME_FILES])
    return denied_dirs, denied_files


def path_denied(path: Path, homes: Iterable[Path]) -> bool:
    """Whether `path` is itself, or sits under, one of `denied_read_paths(homes)`'s own
    dirs or files — the containment question a caller outside this module needs to ask
    against the SAME list `codex_permission_overrides` renders into Codex's own
    permission profile, without re-deriving it. Both sides are resolved (`Path.resolve()`,
    which does not raise on a path that does not exist) so a symlinked home or a `/tmp`
    vs `/private/tmp` spelling does not slip through. An unresolvable `path` (a
    permission error walking its parents) is treated as denied: fail closed, the same
    posture a missing confinement stamp already takes."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return True
    dirs, files = denied_read_paths(homes)
    for f in files:
        try:
            if resolved == f.resolve():
                return True
        except OSError:
            continue
    for d in dirs:
        try:
            root = d.resolve()
        except OSError:
            continue
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


# The Codex permission-profile name `codex_permission_overrides` renders the shared
# deny list under, for a read-only (reviewer) launch. `codex_probe.launch_argv` passes
# it to `-c default_permissions="..."`; slice 2 names a second profile for a writable
# coder sandbox, built the same way from a different write set.
CODEX_READ_ONLY_PROFILE = "agentctl_read_only"


def _nested_under(candidate: Path, roots: Iterable[Path]) -> bool:
    """Whether `candidate` equals, or sits under, one of `roots` — the containment
    check `codex_permission_overrides` runs for every `denied_read_paths` entry
    against the read-allowlist it is about to reopen (epic E33 slice 4): a denied
    path found nested inside a reopened path is re-added as its own "deny" entry, so
    a broader "read" the allowlist opens never silently re-opens it too (the hand
    probe's own confirmed precedence — the more specific entry wins, whichever value
    it carries). No `denied_read_paths` entry is nested under a `read_allowlist` entry
    as of this change (`DENIED_DIRS`/`DENIED_HOME_FILES` and
    `READ_ALLOWLIST_RELDIRS`/`READ_ALLOWLIST_RELFILES` name disjoint paths under the
    same home) — this check is what keeps that true if either list ever grows into
    the other, rather than trusting the two lists to keep not overlapping by
    inspection."""
    for root in roots:
        if candidate == root or candidate.is_relative_to(root):
            return True
    return False


def codex_permission_overrides(profile: str, home: Path, *, worktree: Path,
                               git_dir: "Path | None" = None,
                               run_dir: "Path | None" = None,
                               tool_session_marker: "Path | None" = None,
                               scratch_roots: "Iterable[Path]" = (),
                               extra_deny_homes: "Iterable[Path]" = ()) -> list[str]:
    """The `codex exec -c ...` argv fragment for one Codex launch's filesystem
    permission profile (epic E33 slice 4): `:root`="read" (everything, by default),
    `home` itself ="deny" (the whole account home — the same home-wide deny a Claude
    dispatch's own `sandbox.filesystem.denyRead` already carries, see `settings()`'s
    own comment on that), and every path `read_allowlist(home=home, worktree=worktree,
    ...)` names for THIS run ="read" — the exact same list a Claude dispatch's
    sandbox reopens over its own home-wide deny, rendered into Codex's own profile
    syntax (the hand probe's own confirmed precedence: a more specific entry wins
    over a broader one, whichever value either carries) instead of Claude's
    `sandbox.filesystem.allowRead`.

    In codex-cli 0.161.0, measured live on 2026-10-26, a more specific "read" entry
    wins over a broader home "deny", whichever table order the two keys render in.

    `worktree`, `git_dir`, `run_dir`, `tool_session_marker` and `scratch_roots` are
    exactly `read_allowlist`'s own parameters, threaded straight through —
    `worktree` is the one parameter this function cannot do without (there is no
    `read_allowlist` without it, so every caller must resolve its own run's worktree
    before calling this, and a caller with none to give must refuse the dispatch
    rather than render a profile with nothing reopened at all); the rest default to
    `read_allowlist`'s own defaults (`None`/`()`), which add nothing when a caller
    has none to give. `linked` is resolved from `worktree` here, the same way
    `settings()` resolves it, rather than taken as a parameter: a caller never has a
    reason to know the linked-worktree path itself, only whether it has a `git_dir`.

    Every `denied_read_paths([home])` entry nested inside one of `read_allowlist`'s
    own paths is re-added as its own "deny" entry on top of the reopened directory's
    "read" — see `_nested_under`'s own docstring for what this guards against and
    what was found checking it.

    `extra_deny_homes` denies each one wholesale, with nothing reopened inside it —
    `codex_probe`'s own scratch canary home is the only caller that uses this: a
    throwaway directory holding only placeholder files shaped like the shared deny
    list, never a home whose own `read_allowlist` paths exist on disk to reopen.

    Returns one `-c` flag and its value, ready to extend into an argv list — never a
    whole argv, so a caller that also needs `web_search`/`default_permissions`/other
    overrides can concatenate them in whatever order it likes."""
    home = Path(home)
    worktree = Path(worktree)
    linked = _linked_gitdir(worktree) if git_dir else None
    allowed = read_allowlist(home=home, worktree=worktree, git_dir=git_dir, linked=linked,
                             run_dir=run_dir, tool_session_marker=tool_session_marker,
                             scratch_roots=scratch_roots)
    entries = {":root": "read", str(home): "deny"}
    for p in sorted(allowed, key=str):
        entries[str(p)] = "read"
    denied_dirs, denied_files = denied_read_paths([home])
    for d in sorted({*denied_dirs, *denied_files}, key=str):
        if _nested_under(d, allowed):
            entries[str(d)] = "deny"
    for h in sorted({Path(h) for h in extra_deny_homes}, key=str):
        entries[str(h)] = "deny"
    table = ", ".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in entries.items())
    return ["-c", f"permissions.{profile}.filesystem={{{table}}}"]


def _rule_path(path: Path) -> str:
    # Permission rules take `//` for an absolute path (docs: "Read and Edit rules").
    return "//" + str(path).lstrip("/")


def _existing_eval_sibling_dirs(tmp_roots: Iterable[str], own: Path) -> list[Path]:
    """Every OTHER `create_eval_scratch_dir` directory already on disk under one of
    `tmp_roots`, one entry per spelling it is actually listed under — never `own`
    (compared by name, which `mkdtemp`'s random suffix makes collision-free in
    practice; `own` itself is created, by `create_eval_scratch_dir`, before `settings()`
    is ever called with it, so it is always already on disk here).

    `settings()`'s own `return` comment states this module's contract for the
    `permissions.deny`/`permissions.allow` layer: a deny rule is checked before any
    allow rule, so the allow list cannot re-open a denied path. A wildcard covering
    `EVAL_SCRATCH_PREFIX*` therefore cannot be used for `permissions.deny`'s `Read(...)`
    rule the way it is for `sandbox.filesystem.denyRead` (that layer's `allowRead` has a
    confirmed, documented precedence over `denyRead` — see this module's own
    `settings()` docstring; `Read(...)`/`Edit(...)` have no such confirmed precedent, and
    round 10 broke this dispatch's own `Write` by assuming a `Read(...)` deny could not
    reach it): any wildcard broad enough to cover a sibling's directory also covers this
    dispatch's own, and nothing could carve it back out.

    So this lists the filesystem instead, the same way `_protect_rest_of_git_dir`
    already does for an identical problem (deny everything under a shared root except
    what is exempt, in a layer where the allow list cannot reopen a deny) — see that
    function's own docstring for why enumerating what exists, rather than guessing at a
    pattern, is the only non-guessing way to do this. The same trade-off applies here: a
    sibling directory created after this call returns is not in this list, so it is not
    denied by THIS dispatch's settings — it is denied once THAT sibling's own settings
    are rendered instead (its own enumeration, at its own spawn time, already finds this
    directory). `sandbox.filesystem`'s wildcard `denyRead`/`allowRead` pair is unaffected
    by this function and keeps covering every sibling, present or future, for the Bash
    vector (see `settings()`'s own docstring).

    THE LIMIT THIS LEAVES (agent-platform#940 round 1 revise, reviewer +
    security-reviewer): for a sibling created AFTER this dispatch's settings already
    rendered, this list cannot name it, and nothing else denies this dispatch's Read,
    Grep or Glob tools from reaching that sibling's `verdict.json` — `sandbox.filesystem`
    only binds Bash (see `settings()`'s own docstring), and `permissions.allow` grants
    the bare `Read`/`Grep`/`Glob` tools outright for every eval dispatch. This is not a
    gap a broader deny rule could close: measured directly against the installed Claude
    Code binary (2026-10-18, a live `claude -p` run with a settings file), its permission
    globs have no negated character class — `[!a]`/`[^a]` match the SAME names a bare
    `a` would, not its complement — so no pattern here can express "every eval scratch
    directory except my own," and this function's enumerate-and-exempt approach is what
    that measurement leaves. The harness this serves (`review_eval_replay.run_batch`)
    closes the gap at the one point it matters instead, with three mechanisms together
    (agent-platform#940 rounds 1-2): a per-entry lock serializes two arms of the SAME
    corpus entry WITHIN one `run_batch` call — the only case where reading a sibling's
    verdict gives the reader anything to copy (arms on different entries are reviewing
    different PRs, so one entry's verdict is useless to a reader on another); `run_one`
    kills a run whose own `agentctl wait` left its liveness unresolved (timeout or the
    waiter itself being killed), rather than returning (and so releasing that lock)
    while the run may still be alive; and `run_batch` refuses to start at all,
    dispatching nothing, when any eval-mode run is already registered and alive —
    including one left by an earlier, interrupted `run_batch` call the lock's own
    in-memory lifetime cannot see across. That refusal is a single point-in-time read
    taken before dispatch starts, so it does not detect two `run_batch` calls started
    close enough together to each read the registry before the other has dispatched
    anything. See that function's own docstring."""
    own_name = Path(own).name
    out: list[Path] = []
    for root in tmp_roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in sorted(names):
            if name == own_name or not name.startswith(EVAL_SCRATCH_PREFIX):
                continue
            out.append(Path(root) / name)
    return out


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


def _ensure_packed_refs_exists(git_dir: Path) -> None:
    """Creates an empty `packed-refs` when none exists yet, so the unconditional
    `denyWrite` entry `settings()` adds for it binds a real file rather than a path
    with nothing there.

    Occurrence (Debian 12, bwrap, run-c409dcfe): a fresh `git init` repository has
    no `packed-refs` until something packs refs, and every dispatched run's own
    `denyWrite` named it anyway. On Linux, the installed binary's own bwrap
    translation of a `denyWrite` entry whose path does not exist fabricates a
    stand-in node there instead of simply refusing writes to nothing — measured as
    a character device owned by `nobody`, unreadable by this run's own user — so
    `git commit` failed reading it ("Permission denied") though no repository
    configuration had ever asked it to. `LINUX_SELF_REOPENED_RELDIRS`'s own comment
    records the same binary fabricating a stand-in for a path inside a directory it
    reopens read-only (`.mcp.json`); this is the same behavior reached through
    `denyWrite` instead.

    `O_CREAT | O_EXCL`, so this never follows or overwrites whatever is already at
    the path: a real `packed-refs` some other git process wrote, or a symlink
    planted there, both raise `FileExistsError`, caught and left exactly as found.
    An empty `packed-refs` is a valid git state — "no ref is packed", the same
    state a repository with none at all is already in — so creating it changes
    nothing a concurrent `git pack-refs` or another session reading the same
    repository could observe, and this module's own `denyWrite` entry, added right
    after this call returns, still refuses to write it."""
    try:
        fd = os.open(str(git_dir / "packed-refs"),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        return
    os.close(fd)


def settings(*, home: Path, worktree: Path, runs_root: Path | None = None,
             git_dir: Path | None = None, exclude_user_claude_md: bool = False,
             eval_scratch_dir: Path | None = None,
             tool_session_marker: Path | None = None,
             repo: str | None = None,
             review_allowed: "tuple[str, ...] | None" = None,
             draft_dir: Path | None = None,
             trial: bool = False,
             run_dir: Path | None = None,
             web_tools: "tuple[str, ...]" = ()) -> dict:
    """The sandbox and permission settings for one run.

    `home` is the account home the run's claude is launched with. Paths under
    `Path.home()` of the calling process are denied too, so a caller whose HOME
    differs from `home` (a test, or a launcher) still denies its own grant file.
    `runs_root` is the agent-runs directory. Every run's record lives under it, so no
    run may write there: a run's own `meta.json` is the record `mcp-verdict-server`
    trusts for its role, PR and commit. `git_dir` comes from `git_common_dir`; the linked
    worktree's own git directory is read from `worktree`'s `.git` file here. `repo` is
    the bare repo name `_spawn_claude` already resolves (`args.on`); when it's in
    `postgres_socket_repos()`, the agent Postgres cluster's socket
    (under `home`) is added to `network.allowUnixSockets` — every other repo
    (including `repo=None`) gets exactly today's network settings, unchanged.

    `run_dir` (epic E33 slice 2) is this dispatch's own run directory, re-opened
    for read by `read_allowlist` (a tool shell's `.zshenv` lives under it, see
    `_write_tool_session_zdotdir`'s docstring in `bin/agentctl`) — NEVER the
    run-records root it sits under, which stays closed: an old run's own record
    can hold sensitive output a later run has no business reading. Dropped
    entirely (treated as `None`) whenever `trial` is True, whatever the caller
    passes — a trial dispatch's own `.zshenv` already lives outside `runs_root`
    (`RS.create_trial_zdotdir_base`), so its run directory has no read this
    dispatch's own tool shells need, and `trial`'s whole point is that nothing
    about this dispatch, its own run directory included, is more readable than
    before this parameter existed.

    `review_allowed` (platform/specs/reviewers-run-under-their-allow-list) is the
    resolved `tool_policy.resolve(...).allowed` tuple for a `reviewer`/`security-reviewer`/
    `shadow-reviewer` dispatch (`tool_policy._VERDICT_AGENTS`; CQ·526 added the third),
    OR for a dispatch whose agent is in `tool_policy.ALWAYS_ENFORCED_AGENTS`
    (`web-researcher`; PR #1044 round 1) — `bin/agentctl`'s own `review_allowed`
    computation widens to that set too, since both share the one property this
    parameter exists for: enforcement that cannot wait on `AGENTCTL_ENFORCE_TOOL_POLICY`.
    `None` for every other caller — the DEFAULT, which reproduces today's
    `TOOL_ALLOW`-based grant byte for byte. When given, it REPLACES `TOOL_ALLOW` as this
    run's whole `permissions.allow` base (plus `ToolSearch`, which MCP schema loading
    depends on regardless of what the policy names) — no bare `Bash`, no whole-server
    `mcp__ops`/`mcp__kb` grant, no `WebFetch`/`WebSearch`/`Agent`/`Skill`/the rest, AND sets
    `sandbox.autoAllowBashIfSandboxed: false` (see that key's own comment on the `return`
    below): the allow-list alone does not bind, because Claude Code's unset default for
    that key auto-allows any Bash command the sandbox can run no matter what
    `permissions` says (calibration, 2026-10-17, platform/decisions/0926 proposed).
    There is no env var that turns either of these off: these roles get them
    unconditionally, the fix for the incident `TOOL_ALLOW`'s own comment names.
    `draft_dir` (this dispatch's own `create_review_draft_dir`) is where its `Edit(...)`
    write grant goes instead of the shared temp root `SCRATCH_ROOTS` names for every
    other caller — `None` for `draft_dir`
    with `review_allowed` set grants no write target at all, which is a legitimate state
    (a review dispatch whose dispatcher created no draft directory, or a
    `web-researcher` dispatch, which never gets a `draft_dir` at all — `bin/agentctl`
    computes that argument only for `_is_review_agent`) — never a reason to fall back to
    `SCRATCH_ROOTS`. An `--eval-mode` dispatch takes `eval_scratch_dir`
    instead of `draft_dir` either way — see that parameter's own docstring; the two are
    mutually exclusive in practice (eval mode already withholds the posting surface
    `review_allowed` would otherwise carry) but this function does not need to enforce
    that, since `eval_scratch_dir`'s branch is checked first in both places it matters.

    `web_tools` (epic E33 slice 3) is THE ONE PLACE `WebFetch`/`WebSearch` enter a run's
    `permissions.allow`: `TOOL_ALLOW` no longer names either, so a dispatch gets neither
    tool unless its caller passes it here. The caller resolves this from the dispatched
    agent's own roster `tools:` grant (`_resolved_agent_tools` in `bin/agentctl`) —
    never from `review_allowed`, `TOOL_ALLOW`, or any other default, so an agent whose
    definition does not name a web tool cannot get one by any other route. Values
    outside `WEB_TOOLS` are not filtered here; the caller is trusted to have already
    narrowed to that set (`_resolved_agent_tools`' own validation covers the shape).
    Added to the allow base for EVERY caller, `review_allowed` included — a review-class
    dispatch's own policy never names a web tool today, so this is additive in practice,
    not a carve-out for one branch.

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
    above, each get both a `denyRead` glob and a `Read(...)` deny rule).

    Round 10 shipped that `Read(...)` rule as the SAME wildcard glob `eval_deny_read`
    uses for `sandbox.filesystem`, on the belief that `Write` is "unaffected by any
    `Read(...)` rule: `Edit(path)` is what governs `Write`". Measured against a real
    replay (agent-platform#835, 2026-10-18, run-18436a29): false. A dispatched run's
    `Write` tool refuses "File is covered by a Read deny rule in your permission
    settings and cannot be written" for a path this dispatch's OWN `Edit(...)` allow
    rule names, when that SAME path also matches a `Read(...)` deny rule — so round
    10's wildcard, which (by the same "deny wins over allow in the permissions layer"
    reasoning this function's own `return` comment states, and the module's own
    `_protect_rest_of_git_dir` already relies on elsewhere) denies this dispatch's own
    directory exactly as it denies every sibling's, denied the one write this whole
    mechanism exists for. Round 8 and round 9 were not wrong; round 10's own new rule
    is what broke the harness.

    Round 11 (agent-platform#835) fixes this the way `_protect_rest_of_git_dir`
    already handles an identical problem (deny everything, exempt what's mine,
    in a layer with no confirmed allow-overrides-deny precedent) — not with a
    counter-allow, which this function's own `return` comment says the permissions
    layer does not honor, but by never putting this dispatch's own directory in the
    `Read(...)` deny list in the first place. `_existing_eval_sibling_dirs` lists
    each temp root's actual directory entries at render time (this dispatch's OWN
    `eval_scratch_dir` already exists on disk by the time `settings()` runs —
    `create_eval_scratch_dir` creates it before returning) and denies every OTHER
    `EVAL_SCRATCH_PREFIX`-named entry found there, by its own exact path — never a
    wildcard, and never this dispatch's own name. `sandbox.filesystem`'s wildcard
    `denyRead`/`allowRead` pair is untouched: it has the documented allow-over-deny
    precedence `Read(...)` lacks, so it keeps covering a sibling dispatched AFTER this
    one's settings were rendered, for the Bash vector. The one gap this leaves is the
    same one `_protect_rest_of_git_dir`'s docstring already accepts for the identical
    reason: a sibling's directory created after this enumeration ran is not denied
    here until THAT sibling's own settings (rendered at ITS spawn, enumerating what
    exists then — which already includes this one) deny it from touching this
    directory; what this dispatch cannot enumerate, it cannot deny without guessing.

    `trial` (blind model trial, operator approval 2026-10-18): False for every caller
    today except a `--trial`-flagged `agentctl spawn`. When True, the returned dict
    gains two top-level keys that turn off the Claude Code harness's OWN commit/PR
    attribution (installed version 2.1.302, confirmed to carry both): `attribution`
    (`{"commit": "", "pr": ""}`, the empty string disabling each) and
    `includeCoAuthoredBy: false`, the older boolean the same release still reads.
    Neither key is added when `trial` is False — this function's whole output stays
    byte-identical to before this parameter existed for every non-trial dispatch.

    Round 1 (agent-platform#934, reviewer finding) denied reading four named files
    under a trial dispatch's own `run_dir` — `meta.json`, `transcript.jsonl`,
    `cost-partial.json`, `gateway_spend.jsonl` — that each wrote the model somewhere
    this run's own Bash/Read/Grep/Glob could read back, defeating
    `trial_attribution_directive` without the model ever naming itself. Round 2
    found two more on the same default dispatch path that an enumeration of
    `run_dir` alone can never close: `<run_dir>/detach.out.log`/`detach.err.log`
    (the detached dispatch's own live transcript copy) and `MODEL_OVERRIDES_LOG`
    (`runs_root/model-overrides.jsonl`), which sits directly under `runs_root`,
    never under any one run's own `run_dir` at all.

    Round 3 (agent-platform#934 round 2, reviewer finding) replaces that whole
    enumeration: when `trial` is True and `runs_root` is given, this denies
    reading the WHOLE of `runs_root` — covering every file under it, named or
    not, present or future, this run's own `run_dir` and every sibling's and the
    override log alike — in both layers this module already pairs for every other
    credential path: `sandbox.filesystem.denyRead` (covers sandboxed Bash) and the
    `permissions.deny` `Read(...)` rule (covers Read, Glob and Grep). Writing is
    already denied: `runs_root` is denied for Edit unconditionally whenever
    `runs_root` is given (below) — this only has to close the READ side. The one
    read a trial dispatch's own tool shells make under `runs_root` today —
    `_write_tool_session_zdotdir`'s `.zshenv` — is not carved back out here; it
    moves OUTSIDE `runs_root` for a trial dispatch instead, the same way
    `create_eval_scratch_dir` already sits outside `runs_root` and outside the
    worktree — see `bin/agentctl`'s own trial branch and
    `create_trial_zdotdir_base`'s docstring. `runs_root=None` (a caller with no
    `runs_root` to give, trial or not) adds nothing, the same as every other
    `runs_root`-gated rule in this function. `trial=False` never reads this
    behavior at all, so a non-trial dispatch's output is unaffected by it."""
    extra = [Path(os.environ[EXTRA_DENY_HOME_ENV])] if os.environ.get(EXTRA_DENY_HOME_ENV) else []
    homes = _unique([Path(home), Path.home(), *extra])
    denied_dirs, denied_files = denied_read_paths(homes)
    claude_md_excludes = _unique([h / USER_CLAUDE_MD for h in homes]) if exclude_user_claude_md else []
    # Every home's own auto-memory glob (see MEMORY_WRITE_DENIED_RELDIR's own comment) —
    # same "this home and the caller's own" shape as denied_dirs/denied_files above.
    memory_write_denied = _unique([h / MEMORY_WRITE_DENIED_RELDIR for h in homes])
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
    # Round 11 (agent-platform#835): `permissions.deny`'s own `Read(...)` rule, below,
    # cannot use this same wildcard — see `_existing_eval_sibling_dirs`'s own docstring
    # for why. Empty for every non-eval dispatch, same as `eval_deny_read`.
    eval_sibling_dirs = (_unique(_existing_eval_sibling_dirs(tmp_roots, eval_scratch_dir))
                         if eval_scratch_dir is not None else [])
    # agent-platform#934 round 2 (reviewer finding): the WHOLE of `runs_root`, for a
    # trial dispatch — see this function's own docstring on `trial` for why an
    # enumeration of named files under `run_dir` (round 1) cannot close this. Empty
    # for every non-trial dispatch and for a trial dispatch given no `runs_root`,
    # same shape as `eval_deny_read` just above.
    trial_deny_read_root = [Path(runs_root)] if (trial and runs_root) else []
    if eval_scratch_dir is not None:
        allow_write = [Path(eval_scratch_dir)]
    elif review_allowed is not None:
        # platform/specs/reviewers-run-under-their-allow-list: a review-class
        # dispatch's write grant never reaches the shared temp root at all — only its
        # own `draft_dir`, or nothing at all when the dispatcher gave it none (see
        # `settings()`'s own docstring on `review_allowed`/`draft_dir`).
        allow_write = [Path(draft_dir)] if draft_dir is not None else []
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
    deny_write = [worktree / f for f in WORKTREE_EDIT_DENIED] + tmux_dirs + memory_write_denied
    # Hoisted above the `if git_dir:` block (unlike before this parameter existed)
    # so `read_allowlist`, below, can read it whether or not `git_dir` was given —
    # `None` for a non-git agent or a test, exactly as it always was inside the
    # block.
    linked: Path | None = None
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
        # `protect_branch` is what `_protect_rest_of_git_dir` below exempts from its
        # own deny enumeration — ordinarily the same as `branch`, so that function's
        # own docstring's claim holds ("exempt... BECAUSE it holds this run's own
        # branch file"). It is forced to `None` instead — the same treatment a
        # detached HEAD already gets — for a Linux dispatch whose `branch` has no
        # namespace segment of its own: `_branch_write_dirs` already grants that
        # branch's ref, lock, remote-tracking ref and reflogs no `allowWrite` entry at
        # all (see its own docstring), and `allowWrite` alone does not control a path
        # under a shared git directory (`_protect_rest_of_git_dir`'s own docstring) —
        # exempting the branch's files here would leave them writable anyway, absent
        # from BOTH lists. Forcing `None` instead makes `_protect_rest_of_git_dir`
        # enumerate `refs`/`logs` wholesale, same as it already does with no branch at
        # all, so the branch's own ref, lock, remote-tracking ref and reflogs are
        # explicitly denied rather than merely ungranted.
        protect_branch = branch
        if branch:
            # `_branch_write_dirs` on Linux, not `_branch_write_paths`'s own files —
            # see its own docstring for the bind-mount-per-path rename failure this
            # avoids. macOS is unaffected: `_protect_rest_of_git_dir`'s enumeration
            # (below) is what actually narrows either grant to this run's own
            # branch, on both platforms.
            if _dispatch_host_platform().startswith("linux"):
                linux_dirs = _branch_write_dirs(git_dir, branch)
                if linux_dirs:
                    allow_write += linux_dirs
                else:
                    # No namespace segment of its own: nothing is granted, and
                    # `protect_branch = None` (above) makes the deny side explicit
                    # too, rather than merely absent. `linux_branch_write_note` is the
                    # reason text, logged by the caller (`bin/agentctl`), not by this
                    # function.
                    protect_branch = None
            else:
                allow_write += _branch_write_paths(git_dir, branch)
        # `_protect_rest_of_git_dir` is what actually narrows the write to this run's own
        # branch and worktree (see its docstring); `packed-refs` is denied unconditionally
        # ahead of it too — an ordinary commit and push to a repo with loose refs never
        # writes it, and it can hold a stale entry for every branch at once, so no
        # single-ref deny covers what rewriting it could do. `_ensure_packed_refs_exists`
        # runs first so this denyWrite entry always binds a real, empty file rather than
        # a path with nothing there (see its own docstring for why that distinction
        # matters on Linux). `packed-refs.lock` is left unconditional, with no file
        # created for it: nothing ever needs to READ a lock file, only git's own
        # pack-refs momentarily creates and removes one while writing, which this deny
        # refuses regardless of whether the lock happens to exist at this call.
        _ensure_packed_refs_exists(git_dir)
        deny_write += [git_dir / "hooks", git_dir / "config",
                       git_dir / "packed-refs", git_dir / "packed-refs.lock"]
        deny_write += _protect_rest_of_git_dir(git_dir, protect_branch, linked, private_dir)
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
    # Round 10 (agent-platform#813 round 9 finding): pairs `sandbox.filesystem`'s
    # wildcard glob with a `permissions.deny` `Read(...)` rule it was missing — see
    # this function's own docstring for why the sandbox-layer pair alone leaves
    # Read/Glob/Grep uncovered. Round 11 (agent-platform#835): built from
    # `eval_sibling_dirs` (enumerated, never this dispatch's own directory), not
    # `eval_deny_read`'s wildcard — see `_existing_eval_sibling_dirs`'s own docstring
    # for why the wildcard this dispatch's own `Write` needs cannot be reused here.
    # Empty for every non-eval dispatch, same as `eval_deny_read`.
    deny += [f"Read({_rule_path(p)}/**)" for p in eval_sibling_dirs]
    # agent-platform#934 round 2 (reviewer finding): `runs_root` itself, for a trial
    # dispatch — a directory, same shape `denied_dirs` uses above, so it takes the
    # trailing `/**`. No paired `Edit(...)` rule here: `runs_root` is already denied
    # for Edit unconditionally below, trial or not.
    deny += [f"Read({_rule_path(p)}/**)" for p in trial_deny_read_root]
    deny += [f"Edit({_rule_path(worktree / f)})" for f in WORKTREE_EDIT_DENIED]
    deny += [f"Edit({_rule_path(d)}/**)" for d in tmux_dirs]
    deny += [f"Edit({_rule_path(d)}/**)" for d in memory_write_denied]
    if runs_root:
        deny += [f"Edit({_rule_path(runs_root)}/**)"]
    for h in homes:
        for f in HOME_EDIT_DENIED:
            target = h / f
            suffix = "/**" if f == "Library/LaunchAgents" else ""
            deny.append(f"Edit({_rule_path(target)}{suffix})")
    # Under `dontAsk` a file tool needs an allow rule, and these are the only ones.
    # An eval dispatch gets its own directory instead of `SCRATCH_ROOTS` (see this
    # function's `eval_scratch_dir` docstring); a review-class dispatch gets its own
    # `draft_dir` instead, or nothing (see the `review_allowed`/`draft_dir` docstring).
    if eval_scratch_dir is not None:
        scratch_allow_roots = [Path(eval_scratch_dir)]
    elif review_allowed is not None:
        scratch_allow_roots = [Path(draft_dir)] if draft_dir is not None else []
    else:
        scratch_allow_roots = list(map(Path, SCRATCH_ROOTS))
    # Epic E33 slice 2: the one list rendered into both `sandbox.filesystem.
    # allowRead` (Bash) and `permissions.additionalDirectories` (Read, Grep,
    # Glob), below — see `read_allowlist`'s own docstring. `scratch_allow_roots`
    # (just computed) is reused rather than recomputed, so the read and write
    # sides can never name two different directories for the same dispatch; it
    # is also what makes the file-tools layer treat the shared temp roots as
    # "working directories" for an ordinary dispatch the way Bash already did
    # before `blockReadsOutsideWorkingDirectories` existed, while an eval
    # dispatch's own file-tools reach stays confined to its own scratch
    # directory, never the shared roots. `run_dir` is dropped for a `trial`
    # dispatch whatever the caller passed — see `settings()`'s own docstring on
    # `run_dir` for why.
    allow_read_list = read_allowlist(
        home=home, worktree=worktree, git_dir=git_dir, linked=linked,
        run_dir=(run_dir if not trial else None),
        tool_session_marker=tool_session_marker, scratch_roots=scratch_allow_roots)
    # The incident fix itself: a review-class dispatch's whole `permissions.allow`
    # base is its OWN resolved policy, not `TOOL_ALLOW` — see `review_allowed`'s own
    # docstring for why. `ToolSearch` is added on top regardless of what the policy
    # names (MCP schema loading depends on it); `_unique` guards against the policy
    # ever naming it too, which would otherwise duplicate the rule rather than break
    # anything.
    base_allow = (_unique([*review_allowed, "ToolSearch"]) if review_allowed is not None
                 else list(TOOL_ALLOW))
    # `web_tools` (epic E33 slice 3): the one place WebFetch/WebSearch enter this
    # list — see this function's own docstring on the parameter.
    base_allow = _unique([*base_allow, *web_tools])
    allow = [*base_allow, *(f"Edit({_rule_path(p)}/**)" for p in [worktree, *scratch_allow_roots])]

    network = {
        "allowedDomains": list(NETWORK_ALLOWED),
        "strictAllowlist": True,
        "allowMachLookup": list(MACH_LOOKUP),
        # The proxy admits `github.com` on any port, which is what let run-c36c0559
        # tunnel an SSH push to github.com:22 through it on 2026-10-19 (CQ·506 step
        # S8) after reading the key `.ssh` above now also denies. Set for every run,
        # review-class and coder alike, and for the postgres-socket branch below.
        "deniedDomains": ["*:22"],
    }
    if repo in postgres_socket_repos():
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
                # Epic E33 slice 2: `homes` denied WHOLESALE, not only the
                # specific subpaths `denied_dirs`/`denied_files` name — those
                # stay listed too (unaffected by this change, and still what
                # `permissions.deny`'s own `Read(...)` rules below are built
                # from), since a denied subpath nested inside an `allowRead`
                # entry stays denied (the probe's own confirmed precedence),
                # which is the belt this module already relies on elsewhere
                # (`_protect_rest_of_git_dir`, `_existing_eval_sibling_dirs`).
                "denyRead": [str(p) for p in
                            [*homes, *denied_dirs, *denied_files,
                             *eval_deny_read, *trial_deny_read_root]],
                "allowRead": [str(p) for p in allow_read_list],
            },
            # Calibration (2026-10-17, platform/decisions/0926 proposed): left unset,
            # Claude Code's own default auto-allows every Bash command the sandbox CAN
            # run, whatever `permissions` says — measured letting `python3 -c`,
            # `launchctl print`, `ruff check --isolated --fix` and `awk` all run under
            # the printed review-class settings. `review_allowed`'s own allow-list is
            # therefore not what binds Bash for the review-class roles; this key is. Every
            # other dispatch keeps today's default (unset) — this key is added only on
            # the `review_allowed is not None` branch, never for `TOOL_ALLOW`'s own.
            **({"autoAllowBashIfSandboxed": False} if review_allowed is not None else {}),
            "network": network,
        },
        # Read rules reach Read, Grep and Glob. Edit rules reach Edit, Write and
        # NotebookEdit — AND a `Read(...)` deny rule also refuses `Write` for a path it
        # covers, even one an `Edit(...)` allow rule also names (measured against a real
        # dispatch, agent-platform#835 round 10's own bug: "File is covered by a Read
        # deny rule... and cannot be written"), so a path this dispatch's own `Write`
        # must reach can never be named in a `Read(...)` deny rule here (see
        # `_existing_eval_sibling_dirs`'s own docstring for the one caller this binds).
        # A deny rule is checked before any allow rule, so the allow list above cannot
        # re-open a denied path. The run's mode is `dontAsk` (agentctl), which refuses
        # any file write this allow list does not name.
        #
        # Epic E33 slice 2: `blockReadsOutsideWorkingDirectories` is what denies
        # Read, Grep and Glob outside the working directories in EVERY
        # permission mode (confirmed in the installed binary's own
        # settings-schema strings; `tests/live/test_dispatch_read_allowlist_
        # live.py` is the live check) — the file-tools counterpart to
        # `sandbox.filesystem`'s home-wide `denyRead` above, since that layer
        # only ever bound Bash. `additionalDirectories` is the SAME
        # `allow_read_list` `allowRead` above renders, so the two layers can
        # never name two different sets of re-opened paths for one dispatch.
        # Set unconditionally, for every dispatch: `review_allowed`'s own
        # narrower `permissions.allow` base, above, already strips every other
        # built-in a review-class dispatch does not need, and nothing about
        # this key is specific to that branch.
        "permissions": {"allow": allow, "deny": deny,
                        "blockReadsOutsideWorkingDirectories": True,
                        "additionalDirectories": [str(p) for p in allow_read_list]},
        # A top-level settings key (`USER_CLAUDE_MD`'s docstring), not part of
        # "sandbox" or "permissions": it governs Claude Code's own memory-file
        # loading rather than the sandbox or the tool-permission system. Excludes
        # only the User-scope CLAUDE.md under each home; a worktree's own project
        # CLAUDE.md and the rendered `.claude/CLAUDE.md` are different paths and
        # keep loading.
        "claudeMdExcludes": [str(p) for p in claude_md_excludes],
        # A top-level settings key, same family as `claudeMdExcludes` above — see
        # `AUTO_MEMORY_DISABLED`'s own comment. Set unconditionally for every dispatch,
        # review-class or not, eval or not: no caller of this function has a reason to
        # read or write the auto-memory directory.
        **AUTO_MEMORY_DISABLED,
        # Blind model trial (operator approval 2026-10-18): turns off the Claude
        # Code harness's own commit/PR attribution, both the newer `attribution`
        # key and the older `includeCoAuthoredBy` boolean the same installed
        # release (2.1.302) still reads — see this function's own docstring on
        # `trial`. Added only when `trial` is True; every non-trial dispatch's
        # output is unchanged by this parameter's existence.
        **({"attribution": {"commit": "", "pr": ""}, "includeCoAuthoredBy": False}
           if trial else {}),
    }


def settings_json(*, home: Path, worktree: Path, runs_root: Path | None = None,
                  git_dir: Path | None = None, exclude_user_claude_md: bool = False,
                  eval_scratch_dir: Path | None = None,
                  tool_session_marker: Path | None = None,
                  repo: str | None = None,
                  review_allowed: "tuple[str, ...] | None" = None,
                  draft_dir: Path | None = None,
                  trial: bool = False,
                  run_dir: Path | None = None,
                  web_tools: "tuple[str, ...]" = ()) -> str:
    return json.dumps(settings(home=home, worktree=worktree, runs_root=runs_root, git_dir=git_dir,
                               exclude_user_claude_md=exclude_user_claude_md,
                               eval_scratch_dir=eval_scratch_dir,
                               tool_session_marker=tool_session_marker, repo=repo,
                               review_allowed=review_allowed, draft_dir=draft_dir,
                               trial=trial, run_dir=run_dir, web_tools=web_tools),
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
