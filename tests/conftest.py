"""Phase A test harness for agent-platform (substrate ARCH-098).

Two jobs:

1. Make the platform's lib modules importable from THIS checkout. The lib
   modules import each other bare (`worktrees.py` does `import registry`), and
   `bin/agentctl` inserts `~/.local/lib/agent-platform` at import time — a
   deployed path that does not exist in CI. Putting the repo's own lib dirs on
   `sys.path` (ahead of collection) makes both the direct `import budget` in a
   test and agentctl's internal imports resolve to the SOURCE in this checkout,
   not a deployed symlink. On a dev machine where `~/.local/...` symlinks back
   to the repo it's the same file either way; in CI only this path exists.

2. Load the hyphenated `bin/` CLIs (not importable as normal modules) via
   importlib, exposing them as fixtures.

Filesystem-touching modules (budget, registry) read their RUNS_ROOT-family
paths from module constants; the `runs_root` fixture repoints them at a tmp dir
so tests never touch the real ~/Convivy/agent-runs.
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import urllib.parse
import tempfile
import urllib.request
from pathlib import Path

import pytest

# NO TEST WRITES THE LIVE RUN RECORDS. Run-record paths resolve through
# builder_root.data_root(), which honours BUILDER_ROOT and deliberately ignores
# HOME, so repointing HOME no longer isolates them. Setting BUILDER_ROOT here,
# before any module computes a path, keeps every test and every subprocess that
# inherits this environment out of the machine's real agent-runs. A test that
# needs its own data root sets BUILDER_ROOT itself.
os.environ["BUILDER_ROOT"] = tempfile.mkdtemp(prefix="agent-platform-tests-data-root-")

REPO = Path(__file__).resolve().parent.parent

# Every reader of `operator_config.get()` is pointed at a synthetic file for
# the whole test session, never at this machine's real
# $HOME/.config/convivy/operator.json. Set this early, before any lib import,
# for the same reason as BUILDER_ROOT above: `knowledge.py` reads it at MODULE
# IMPORT TIME to build ALLOWED_AUDIENCE/ALLOWED_CREWS, and a test file can
# import it before any fixture runs.
#
# `roster_crew` alone is read out of this repo's own config/projects.json
# (never a literal here) because `test_allowed_crews_is_the_registrys_crew_set`
# requires knowledge.ALLOWED_CREWS to contain the SAME slug the registry
# actually uses — a synthetic placeholder would make that real parity check
# fail. The one structural trait that names the operator's own crew among the
# registry's private ones is a `display` ending in ".com" (the same convention
# `bin/co`'s window-naming comments describe); falling back to a synthetic
# slug keeps this working where config/projects.json has no such entry.
def _default_roster_crew() -> str:
    try:
        registry = json.loads((REPO / "config" / "projects.json").read_text())
    except (OSError, json.JSONDecodeError):
        return "operator-crew"
    for slug, entry in registry.items():
        if isinstance(entry, dict) and entry.get("private") and \
           str(entry.get("display", "")).endswith(".com"):
            return slug
    return "operator-crew"


_operator_config_dir = tempfile.mkdtemp(prefix="agent-platform-tests-operator-config-")
_operator_config_path = Path(_operator_config_dir) / "operator.json"
_operator_config_path.write_text(json.dumps({
    "excluded_crews": ["private-a", "private-b", "private-c", "private-d",
                       "private-e", "private-f", "private-g"],
    "operator_marker": "operator",
    "roster_crew": _default_roster_crew(),
    "github_login": "operator-login",
    "tailnet_host": "tailnet.example",
}))
os.environ["CONVIVY_OPERATOR_CONFIG"] = str(_operator_config_path)

for _libdir in ("lib/agent-platform", "lib/knowledge"):
    _p = str(REPO / _libdir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A dispatched run's own environment carries `dispatch_env`'s GIT_OBJECT_DIRECTORY /
# GIT_ALTERNATE_OBJECT_DIRECTORIES, redirecting git at the run's private object store
# (substrate ARCH-098+ — agent-platform#793). A git fixture's subprocess inherits this
# conftest's environment, so `git init --bare` under a dispatched run's test suite wrote
# into that private store and left the new repo with no objects/ directory — reproduced
# 2026-10-07 (issue_01M4CWH0XFCV5CYMZVNDYSMP9C). Stripped here, mirroring the filter
# `run_sandbox.import_private_objects` already applies, and BEFORE the pin loop below
# imports every lib module: `primary_guard._SAFE_GIT_ENV` is one of several module-level
# `{**os.environ, ...}` snapshots taken at import time, so stripping after that loop had
# already run left every one of them holding the redirect anyway — this is the first lib
# import in the whole file, before anything has had a chance to shadow it with the farm.
import run_sandbox  # noqa: E402

for _var in run_sandbox.GIT_OBJECT_REDIRECT_VARS:
    os.environ.pop(_var, None)

# Pin the lib modules to THIS repo's copies before anything else can import them.
#
# `bin/agentctl` does `sys.path.insert(0, ~/.local/lib/agent-platform)` at import —
# the DEPLOYED path, which symlinks to whatever the primary checkout is on. So the
# moment a test imported agentctl first, every lib module resolved from MAIN, and a
# branch's tests silently exercised main's code: `test_review_protocol.py` was
# testing the deployed review_protocol, not the one in the diff under review. Import
# them eagerly here so sys.modules caches the repo's copies and agentctl's later
# import returns those. Found 2026-07-12, when two new review_protocol tests passed
# alone and failed in the full suite — the ordering was the only difference.
# STRIP the deployed path outright, and keep it stripped. Inserting the repo's
# lib dirs ahead of it is NOT enough: `cost_join.py` does its own
# `sys.path.insert(0, ~/.local/lib/agent-platform)` at import time, so the moment
# it loads, the deployed tree is back at position 0 and every module imported
# AFTER it resolves from whatever the primary checkout is on — i.e. main.
#
# That is how issue_client's tests passed alone and failed in the full suite on
# 2026-07-19: the pin below imported issue_client, but cost_join (earlier in the
# alphabet) had already re-shadowed the path, so the "pinned" module WAS the
# deployed one. The 2026-07-12 fix this replaces pinned by name and could not
# see that, because the shadowing happens between the insert and the import.
_DEPLOYED = str(Path.home() / ".local" / "lib" / "agent-platform")

# THE FARM MUST NEVER REACH sys.path AT ALL, not even for one nested import.
# Unshadowing it between the pin loop's own top-level iterations (below) is not
# enough: `co_rollover` imports `tmux_transcript`, which re-inserts the farm and
# imports `private_crew`, which imports `projects` — three modules the loop has
# not reached yet, all inside `co_rollover`'s OWN import, with no unshadow call
# in between. `private_crew` and `projects` resolve from the farm and get
# cached in sys.modules under those names before the loop's own turn for either
# one ever runs, leaving its "already imported" check a no-op; `_repin_from_
# the_farm` below then fixes `sys.modules["projects"]` itself, but a module that
# had ALREADY done `import projects` against the stale sys.modules entry — like
# `session_crews`, whose own `import projects` line runs later in this same
# loop and returns the cached farm copy — keeps that stale reference as its own
# attribute, unaffected by the later swap (issue_01M3P6AQS6EFX2MCP8WMKEH82P:
# `test_an_unreadable_registry_serves_nothing` failed because `session_crews.
# projects` was that farm copy, so monkeypatching THIS checkout's `projects.
# PROJECTS_JSON` never touched the module `session_crews.crews()` actually
# calls). Patching `sys.path.insert` itself, rather than repairing the path
# after each top-level file, closes this for every import order — nested or
# not — for the rest of the process, so no module downstream can bind an
# attribute to a farm copy in the first place.
#
# `sys.path` is a plain `list`, which has no `__dict__` — `sys.path.insert =
# ...` raises `AttributeError: 'list' object attribute 'insert' is read-only`
# rather than shadowing the method on this one instance. Replacing the object
# itself with a subclass that overrides `insert` is what actually intercepts
# every `sys.path.insert(0, ...)` call from here on, wherever it's made.
class _NoFarmPath(list):
    def insert(self, index, path):
        if path == _DEPLOYED:
            return
        super().insert(index, path)


sys.path = _NoFarmPath(sys.path)


def _unshadow() -> None:
    """Remove the deployed lib dir from sys.path, wherever it has reappeared."""
    while _DEPLOYED in sys.path:
        sys.path.remove(_DEPLOYED)


_unshadow()

import vault_plane  # noqa: E402


# DERIVED, not hand-listed: a module absent from a hand-written list is silently
# exempt from the pin, and a guard that must be edited to cover something new
# will keep missing the new thing. Unshadow before EACH import, since any one of
# them may re-insert the deployed path on its way in.
#
# A module that cannot be imported is SKIPPED rather than fatal: this is a pin,
# not a smoke test, and one unimportable module must not fail collection.
_unshadow()

# telemetry is claimed BY NAME before the loop, and the reason is specific:
# cost_join inserts the deployed path and imports telemetry DURING its own
# import, so an unshadow between top-level iterations never runs in time — by
# the loop's turn, sys.modules["telemetry"] is already main's copy. Claiming the
# name first is what closes that. (Round-2 reviewer catch on PR #167: the strip
# alone was true for every module except this one.)
#
# A two-pass "register all, then execute" scheme was tried instead and is worse:
# registering a module before executing it breaks every module-level
# `from X import Y`, because X is present but empty. It broke decision_client.
importlib.import_module("telemetry")

_unshadow()
for _src in sorted((REPO / "lib" / "agent-platform").glob("*.py")):
    if _src.stem.startswith("_"):
        continue
    _unshadow()
    try:
        importlib.import_module(_src.stem)
    except Exception:                                            # noqa: BLE001
        pass
_unshadow()


# VERIFY THE PIN, because unshadowing before each import is not sufficient and the
# `telemetry` note above is the proof: a module that inserts the deployed path
# DURING its own import and then imports a sibling caches the farm's copy under
# that name, and the loop's later turn for that sibling is a no-op on an already
# populated sys.modules.
#
# `telemetry` was closed by claiming its name first. That is a hand-list of one,
# and a hand-list is what this file's own comment warns will keep missing the new
# thing — which it then did: `session_input` inserted the farm and imported
# `session_state`, so every test ran against MAIN's session_state while
# signal_client came from the tree. The suite tested two trees at once, and the
# symptom was a credential-redaction assertion failing on a branch whose
# sanitiser was correct (PR #331).
#
# Checking WHERE each module actually came from is derived rather than listed, so
# it catches the next one without being edited. Re-import rather than only
# reporting: at collection time nothing else holds a reference yet, so swapping
# the module object is safe here and is not once tests are running.
def _repin_from_the_farm() -> list[str]:
    """Re-import any lib module that resolved from the deployed farm. Returns the
    names that could not be recovered."""
    lib = str((REPO / "lib" / "agent-platform").resolve())
    unrecovered = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if not f.startswith(_DEPLOYED):
            continue
        if not (REPO / "lib" / "agent-platform" / f"{name}.py").is_file():
            continue  # a farm module this repo does not own; leave it
        del sys.modules[name]
        _unshadow()
        try:
            importlib.import_module(name)
        except Exception:                                        # noqa: BLE001
            unrecovered.append(name)
            continue
        got = getattr(sys.modules.get(name), "__file__", "") or ""
        if not got.startswith(lib):
            unrecovered.append(name)
    return unrecovered


_stuck = _repin_from_the_farm()
_unshadow()
if _stuck:
    # LOUD. A module that still resolves from the farm means the suite is testing
    # a tree other than the one under review, which is worse than a failing test:
    # it is a PASS that means nothing. Failing collection is the proportionate
    # response.
    raise RuntimeError(
        f"these lib modules still resolve from {_DEPLOYED} after re-pinning: "
        f"{_stuck}. The suite would be testing that tree, not this one.")


def _load_cli(filename: str):
    """Import a hyphenated `bin/` CLI as a module (not importable normally).

    Executes the file's top-level code (defs + imports). The `if __name__ ==
    "__main__"` guard means `main()` does NOT run on import.
    """
    # bin/ CLIs are extension-less, so spec_from_file_location can't infer a
    # loader (returns None). Hand it an explicit SourceFileLoader.
    modname = filename.replace("-", "_")
    path = REPO / "bin" / filename
    loader = importlib.machinery.SourceFileLoader(modname, str(path))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_test_holds_the_library_app_key(monkeypatch, tmp_path_factory):
    """FAIL CLOSED: no in-process test can mint a real `convivy-library` token,
    and every test reads today's Convivy identity from a config fixture rather
    than a Convivy value held in `kb_merge.py` itself
    (issue_01M3SGPX7AMVR2SKREVPWEPKN0 — the module holds none any more).

    `kb apply`, `kb propose` and `kb merge` act as the App, and this machine holds
    the App's key. A test that drives any of them without stubbing the identity
    would mint a real token, and `kb apply` or `kb propose` would then push to the
    real `https://github.com/convivy/knowledge.git`. That happened once, in the
    first run of the change that introduced the push identity; GitHub rejected the
    push as a non-fast-forward, and only that. Pointing the configured key at
    nothing makes an un-stubbed caller take the real refusal path instead. Every
    reader of the identity (`key_problem`, `app_jwt`, `push_credential`, `refusals`)
    resolves `kb_merge.load_config()` at call time, so repointing `CONFIG_PATH`
    reaches them all. A test that means to exercise the credential passes its own
    `key_path` and an injected opener; a test of the config itself repoints
    `CONFIG_PATH` again.

    THIS HOLDS IN-PROCESS ONLY. A test that ran `bin/agentctl kb apply|propose|merge`
    as a subprocess would import a fresh `kb_merge`, which computes `CONFIG_PATH`
    from `builder_root.real_account_home()`. That reads the passwd database, so
    neither `$HOME` nor `BUILDER_ROOT` redirects it, and the subprocess would find
    the real file (or none). No such test exists today; the first one must stub
    the identity itself and not lean on this fixture.
    """
    import json

    import kb_merge
    d = tmp_path_factory.mktemp("no-app-key")
    config_path = d / "library-app.json"
    config_path.write_text(json.dumps({
        "repo": "convivy/knowledge",
        "app_id": "4923626",
        "bot_login": "convivy-library[bot]",
        "bot_user_id": 328494887,
        "key_path": str(d / "library-app.pem"),
    }))
    monkeypatch.setattr(kb_merge, "CONFIG_PATH", config_path)


@pytest.fixture(autouse=True)
def _no_test_holds_the_docs_merge_app_key(monkeypatch, tmp_path_factory):
    """FAIL CLOSED, the same contract `_no_test_holds_the_library_app_key`
    holds just above: no in-process test can mint a real documents-merge
    token by accident. Every `docs_merge` test either reads this fixture's
    inert identity (`key_problem` refuses — no key file at the configured
    path) or repoints `docs_merge.CONFIG_PATH`/passes its own `key_path`
    itself, exactly as the Library fixture's docstring describes for
    `kb_merge`.
    """
    import json

    import docs_merge
    d = tmp_path_factory.mktemp("no-docs-merge-app-key")
    config_path = d / "docs-merge-app.json"
    config_path.write_text(json.dumps({
        "repos": ["example/repo"],
        "app_id": "0",
        "key_path": str(d / "docs-merge-app.pem"),
    }))
    monkeypatch.setattr(docs_merge, "CONFIG_PATH", config_path)


@pytest.fixture(autouse=True)
def _default_run_github_token(agentctl, monkeypatch):
    """Every real `agentctl spawn` now mints a per-run convivy-bosun installation token
    before the worktree is allocated (CQ·418, `_mint_run_github_token`) — a genuine App
    JWT signature and a POST to GitHub. Almost none of this suite's many spawn tests are
    testing THAT; they are testing spawn's other behaviour, which must keep passing the
    way it did before this gate existed, so the mint is stubbed here to succeed with a
    fixed fake token by default.

    This fails OPEN (succeed), the opposite of `_no_test_holds_the_library_app_key`'s
    fail-closed default just above — correct for each: `kb_merge`'s App actions are a
    small, deliberate set of tests that each know the gate is there, so refusing by
    default is harmless and catches an unstubbed one reaching the network. Spawn is
    exercised by most of this file; refusing by default here would turn every one of
    those tests into a failure of this gate instead of whatever they actually drive.

    A test of the mint ITSELF (success, failure, scope) restores the real function —
    `agentctl._real_mint_run_github_token`, stashed below the first time this fixture
    runs — with `monkeypatch.setattr(agentctl, "_mint_run_github_token",
    agentctl._real_mint_run_github_token)`, then stubs `agentctl.GA`'s primitives
    itself; test_agentctl.py's CQ·418 section does exactly this."""
    if not hasattr(agentctl, "_real_mint_run_github_token"):
        agentctl._real_mint_run_github_token = agentctl._mint_run_github_token
    monkeypatch.setattr(agentctl, "_mint_run_github_token",
                        lambda repo: ("ghs_test_stub_token", None))


@pytest.fixture
def library_identity(agentctl, monkeypatch):
    """The App identity, stubbed for the cage tests. Every git call made as the App
    is recorded (as its argument tuple) in `.calls`, and a call naming `kb_merge.
    push_url()` goes to the cage's `origin` instead. A call naming `origin` itself
    fails the test: that is the ssh remote, which authenticates as the operator.
    `.mints` records, per identity obtained, the scope it asked for: "read"
    (`contents: read`), "push" or "push+pr".
    `.real` is the unstubbed `_kb_library`, for a test of the refusal.

    The stub keeps the real `_KbLibrary.gh`, so a `gh` call still goes through
    `subprocess.run` with its real environment, for a test to intercept.

    ALSO PINS `_kb_origin_is_local` to False and `_kb_origin_is_github` to True.
    The cage's own `origin` remote is a local bare repo — the Stub above needs
    it real and reachable, so the git calls it translates from
    `kb_merge.push_url()` land somewhere — so read literally, `git remote
    get-url origin` would resolve to a local path and every App-identity path
    this fixture exists to exercise (including a test that restores `.real`
    to check the identity's own refusal) would instead take the local-origin
    branch, or — once past that — read its refusal in the unrecognised-remote
    wording rather than the App-identity one a real GitHub install would show
    (issue_01M3M67C7VB7ZVYQ0MBKHZVYBS). Pinning both keeps this fixture's
    contract — "this cage speaks to the App" — true of the origin-branch
    decision and its refusal wording too, independent of what transport the
    Stub actually reaches."""
    import kb_merge
    from types import SimpleNamespace
    calls, mints = [], []
    real = agentctl._kb_library
    monkeypatch.setattr(agentctl, "_kb_origin_is_local", lambda root: False)
    monkeypatch.setattr(agentctl, "_kb_origin_is_github", lambda root: True)

    class Stub(agentctl._KbLibrary):
        def __call__(self, root, *a):
            assert "origin" not in a, f"git was pointed at the ssh remote: {a}"
            calls.append(a)
            return agentctl._kb_git_at(
                root, *["origin" if x == kb_merge.push_url() else x for x in a])

    def library(pull_requests=False, read_only=False):
        mints.append("read" if read_only else "push+pr" if pull_requests else "push")
        if read_only:
            return Stub("ghs_read_test")
        return Stub("ghs_push_test", "ghs_pr_test" if pull_requests else None)

    monkeypatch.setattr(agentctl, "_kb_library", library)
    return SimpleNamespace(calls=calls, mints=mints, real=real)


@pytest.fixture(scope="session")
def agentctl():
    """The `bin/agentctl` module. Used for its pure helpers (model_id,
    derive_purpose, _slug, _parse_frontmatter, …) — most touch neither disk
    nor network, so no isolation fixture is needed for them. `default_profile`
    is the exception (CQ·371: its `GET /profiles` read is a real Library call)
    — see `stub_profile_listing` below."""
    return _load_cli("agentctl")


def stub_profile_listing(agentctl, monkeypatch, variants=()):
    """Stubs `agentctl._library_profile_variants` — `default_profile`'s one
    Library read (`GET /profiles`, CQ·371) — to `variants` ((project, variant)
    pairs), with no network call and no read of the real credential file.

    Defaults to an empty listing, which matches Convivy's own Library:
    `default_profile`'s own docstring notes it serves no `platform/default`
    row, so an empty listing produces the same `has_default=False` branch
    production takes, and every pinned `default_profile` assertion this stubs
    is unaffected by `variants` being empty rather than exhaustive.

    A test exercising the real fetch path instead patches `agentctl.
    _library_get` directly, underneath this (see `tests/test_project_
    resolver.py`'s `_agentctl_serves`)."""
    monkeypatch.setattr(agentctl, "_library_profile_variants", lambda: list(variants))


@pytest.fixture(scope="session")
def review_launch_bin():
    """The `bin/review-launch` module (the root-owned launcher's thin
    wrapper, not the testable core in `lib/agent-platform/review_launch.py`,
    imported there as `RL`). Importing it only runs its top-level `def`s and
    `import`s — `pwd.getpwnam("reviewers")` is called inside
    `ClaudeSession.__init__`, not at module scope, so this never needs a
    real `reviewers` account on the machine running the test."""
    return _load_cli("review-launch")


@pytest.fixture(scope="session")
def session_view_cli():
    """The `bin/session-view` CLI module, for its argv and stdin handling.

    Its `SV` is THIS checkout's library, and only because the eager pinning at
    the top of this file already put the repo's `session_view` in `sys.modules`.
    The script's own `sys.path.insert(0, ~/.local/lib/agent-platform)` points at
    the deployed primary, so without that pinning it would bind whatever is
    installed. Under pytest the cache wins; run the same script as a subprocess
    and the deployed library wins instead, which is a real way to "verify" a
    branch against code it does not contain.

    Monkeypatch `mod.SV` with the `SV` fixture anyway when the behaviour under
    test reaches the library — a test needs its own patchable module object for
    the stubs it installs, rather than mutating the one every other test shares.
    """
    return _load_cli("session-view")


@pytest.fixture(scope="session")
def session_agent_cli():
    """The `bin/session-agent` CLI module, for its `--follow` printing loop
    (`_follow`/`_emit`) — the orphan-exit and stopped-state logic itself lives
    in `session_state.follow_states`, tested directly against the `SS` fixture
    in `test_session_verbs.py`."""
    return _load_cli("session-agent")


@pytest.fixture(scope="session")
def co_rollover_cli():
    """The `bin/co-rollover` CLI module, for `_cmd_spawn`/`_cmd_picked_up`'s
    own ordering and argv-building logic — `mod.CR`/`mod.RP` are the real
    `co_rollover`/`rollover_policy` library modules (this file's own
    `import ... as CR`/`as RP`, executed on load), monkeypatched per test so
    nothing here touches a real tmux server or the real cache directory."""
    return _load_cli("co-rollover")


@pytest.fixture(scope="session")
def issue_eligible():
    """The `bin/issue-eligible` CLI — for its exit-code contract (ARCH-362), where
    0 means the gate RAN and non-zero means it could not see the queue."""
    return _load_cli("issue-eligible")


@pytest.fixture(scope="session")
def knowledge_ctx():
    """The `bin/knowledge-ctx` module — for detect_project (the cwd/worktree →
    KB-project mapping that Phase B extends to add agent-platform)."""
    return _load_cli("knowledge-ctx")


@pytest.fixture(scope="session")
def agent_summary():
    """The `bin/agent-summary` module — for the ARCH-091 cost-window math."""
    return _load_cli("agent-summary")


@pytest.fixture(scope="session")
def work_item_cost():
    """The `bin/work-item-cost` module — the E16·4 run-cost-by-work-item report."""
    return _load_cli("work-item-cost")


@pytest.fixture(scope="session")
def post_changelog():
    """The `bin/post-changelog` module — the co-authored changelog entry path."""
    return _load_cli("post-changelog")


@pytest.fixture(scope="session")
def pin_claude():
    """The `bin/pin-claude` module — the reviewers' Claude binary pin
    (platform/decisions/0090): fetch, hash, verify against Anthropic's
    manifest, write atomically only on a full match."""
    return _load_cli("pin-claude")


@pytest.fixture(scope="session")
def issues_waiting():
    """The `bin/issues-waiting` module — the co-startup "needs a human" line."""
    return _load_cli("issues-waiting")


@pytest.fixture(scope="session")
def changelog_drain():
    """The `bin/changelog-drain` module — the merge → explainer → entry pass."""
    return _load_cli("changelog-drain")


@pytest.fixture(scope="session")
def run_observer():
    """The `bin/run-observer` module — reports runs that never finalized."""
    return _load_cli("run-observer")


@pytest.fixture(scope="session")
def issue_pr_reconcile():
    """The `bin/issue-pr-reconcile` module — closes pr-open issues whose PR merged."""
    return _load_cli("issue-pr-reconcile")


@pytest.fixture(scope="session")
def post_release_note():
    """The `bin/post-release-note` module — the release-note path to the site."""
    return _load_cli("post-release-note")


@pytest.fixture(scope="session")
def pr_state_capture():
    """The `bin/pr-state-capture` module (issue_01M2KV16ZRG3N49GRER8Q7545H) — the
    Bosun's read-only, convivy-bosun-App-authenticated PR-state signal capture."""
    return _load_cli("pr-state-capture")


@pytest.fixture(scope="session")
def ci_result_capture():
    """The `bin/ci-result-capture` module (CQ-455,
    issue_01M4J1ASMSFFPC6S0RQ6VVVT57) — the Bosun's read-only,
    convivy-bosun-App-authenticated CI-result signal capture."""
    return _load_cli("ci-result-capture")


@pytest.fixture(scope="session")
def github_activity_capture():
    """The `bin/github-activity-capture` module (E25,
    issue_01M4ZB5C5ZX0MV206064MTRAE0) — the Bosun's read-only,
    convivy-bosun-App-authenticated GitHub review/comment/changelog signal
    capture, calling `lib/agent-platform/github_activity.py`'s
    `capture_repository` per installation's repository."""
    return _load_cli("github-activity-capture")


@pytest.fixture(scope="session")
def co_working_on():
    """The `bin/co-working-on` module — the writer for Deck's cockpit "working
    on" line, whose reader is `session_state.working_on`."""
    return _load_cli("co-working-on")


@pytest.fixture(scope="session")
def co_ask_cli():
    """The `bin/co-ask` module — the co's half of the Slate (`slate.py`),
    whose operator-facing half is `session-agent slate*` (CQ-358)."""
    return _load_cli("co-ask")


@pytest.fixture(scope="session")
def limit_resume_cli():
    """The `bin/limit-resume` module — the entry point that wires
    `limit_resume.run_once` to the real tmux calls and guards against two
    firings overlapping. The decision logic itself is `limit_resume`, importable
    directly since lib/agent-platform is already on `sys.path`."""
    return _load_cli("limit-resume")


@pytest.fixture(scope="session")
def memory_archive_cli():
    """The `bin/memory-archive` module — the entry point that wires
    `memory_archive.run` to the real Archives calls and guards against two
    firings overlapping. The decision logic itself is `memory_archive`,
    importable directly since lib/agent-platform is already on `sys.path`."""
    return _load_cli("memory-archive")


@pytest.fixture(scope="session")
def model_scorecard():
    """The `bin/model-scorecard` module — the ARCH-107 success-vs-cost harvester."""
    return _load_cli("model-scorecard")


@pytest.fixture(scope="session")
def security_scorecard():
    """The `bin/security-scorecard` module — the ARCH-306 security-catch harvester."""
    return _load_cli("security-scorecard")


@pytest.fixture(scope="session")
def warrant_scorecard():
    """The `bin/warrant-scorecard` module — the ARCH-309 warrant/spurious-work harvester."""
    return _load_cli("warrant-scorecard")


@pytest.fixture(scope="session")
def velocity_report():
    """The `bin/velocity-report` module — the engineering-velocity rollup."""
    return _load_cli("velocity-report")


@pytest.fixture(scope="session")
def agent_activity_report():
    """The `bin/agent-activity-report` module (ARCH-241) — the /activity + cost-per-
    outcome snapshot producer. Tests exercise its surfacing policy (PA task-text
    redaction, ARCH-243) without touching the live corpus."""
    return _load_cli("agent-activity-report")


@pytest.fixture(scope="session")
def orchestrator_tokens():
    """The `bin/orchestrator-tokens` module — the ARCH-129 token/turn report."""
    return _load_cli("orchestrator-tokens")


@pytest.fixture(scope="session")
def link_project_inboxes():
    """The `bin/link-project-inboxes` CLI (ARCH-426) — for `link_one`, whose
    refusal to replace a directory holding real notes is the contract worth
    pinning."""
    return _load_cli("link-project-inboxes")


@pytest.fixture(scope="session")
def transcript_capture():
    """The `bin/transcript-capture` module (issue_01M0BRGVMXGQKM4GXBEFG2Z7PS) —
    interactive session transcripts into the Library's Archives, excluding
    black-box crews at the directory level."""
    return _load_cli("transcript-capture")


@pytest.fixture(scope="session")
def review_eval_cli():
    """The `bin/review-eval` module (agent-platform#807) — its CLI wiring
    (argparse, STANDARD_ARMS, subset selection), exercised against synthetic/
    empty corpora and results dirs. Importing it runs no model/gh/gateway
    call; only actually invoking a subcommand that needs one would, and no
    test here does."""
    return _load_cli("review-eval")


@pytest.fixture(scope="session")
def kb_eval():
    """The `bin/kb-eval` module — for its pure scoring helpers (score_case,
    aggregate, kind_of), exercised with a stub search callable so no index or
    FastMCP import is needed (load_kb_search() is the only FastMCP-dependent path
    and is never called here)."""
    return _load_cli("kb-eval")


def _stub_fastmcp():
    """Install a stubbed FastMCP so the test venv needn't install the `mcp`
    package; @mcp.tool() becomes a passthrough, leaving tool fns directly
    callable. Shared by the mcp_kb_server and mcp_ops_server fixtures."""
    import types

    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn  # passthrough — leaves the tool fns directly callable

        def run(self):
            pass

    fastmcp.FastMCP = _FastMCP
    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.fastmcp"] = fastmcp


@pytest.fixture(scope="session")
def mcp_kb_server():
    """The `bin/mcp-kb-server` module, FastMCP-stubbed. Used for its pure query
    helper (_build_match)."""
    _stub_fastmcp()
    return _load_cli("mcp-kb-server")


@pytest.fixture(scope="session")
def mcp_ops_server():
    """The `bin/mcp-ops-server` module (ARCH-349 signal memory), FastMCP-stubbed —
    pure helpers (_shape_item, _token, _call) + directly-callable tool fns."""
    _stub_fastmcp()
    return _load_cli("mcp-ops-server")


@pytest.fixture(scope="session")
def mcp_verdict_server():
    """The `bin/mcp-verdict-server` module, FastMCP-stubbed: its `review_post` tool body is
    directly callable (run_verdict holds the refusals and the posting)."""
    _stub_fastmcp()
    return _load_cli("mcp-verdict-server")


@pytest.fixture(scope="session")
def metric_alerts():
    """The `bin/metric-alerts` module — the ARCH-112 threshold-alert evaluator."""
    return _load_cli("metric-alerts")


@pytest.fixture(scope="session")
def run_register_api():
    """The `bin/run-register-api` module — serves the live active-runs register
    for the Library to pull from (2026-08-20 live-status chain direction)."""
    return _load_cli("run-register-api")


@pytest.fixture(scope="session")
def agent_cost_report():
    """The `bin/agent-cost-report` module — the ARCH-136 per-model spend report
    that writes the snapshot /spending and /cost-performance read."""
    return _load_cli("agent-cost-report")


@pytest.fixture(scope="session")
def budget_watchdog():
    """The `bin/budget-watchdog` module — for its cap-crossing alert path."""
    return _load_cli("budget-watchdog")


@pytest.fixture(scope="session")
def session_follow_watchdog():
    """The `bin/session-follow-watchdog` module — detects a leaked
    `session-agent state --follow` process by duplicate-per-session, the
    third layer behind PR #392's orphan-loop fix."""
    return _load_cli("session-follow-watchdog")


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    """Repoint budget + registry at an isolated tmp agent-runs root so tests
    never read or write the real ~/Convivy/agent-runs. Returns the root Path."""
    import budget
    import registry

    root = tmp_path / "agent-runs"
    root.mkdir()
    monkeypatch.setattr(budget, "RUNS_ROOT", root)
    monkeypatch.setattr(registry, "RUNS_ROOT", root)
    monkeypatch.setattr(registry, "ACTIVE_PATH", root / "active.json")
    monkeypatch.setattr(registry, "LOCK_PATH", root / ".active.lock")
    monkeypatch.setattr(registry, "STALE_LOG", root / "stale-runs.jsonl")
    # The limits come from the knowledge repo's policy file, read once per process.
    # Point them at a directory that is not a repository (the fallback 16/4/4) and
    # drop any limits an earlier test cached, so no test reads the machine's policy.
    monkeypatch.setattr(registry, "KNOWLEDGE_REPO", tmp_path / "no-knowledge-repo")
    monkeypatch.setattr(registry, "_LIMITS", None)
    return root


# --- no test may reach the LIVE ops plane (2026-07-23) -----------------------
#
# THE LEAK THIS CLOSES. `post_bug` gained a SECOND write in ARCH-373 — an issue on
# the ops queue, alongside its Slack post. One pre-ARCH-373 test still mocked only
# Slack, so from that day every agent run executing the suite filed a real issue:
# 148 `[possible bug] something looks off` rows against vivy between 2026-07-19 and
# 07-23, one per run (the once_key carries AGENTCTL_RUN_ID, so each run minted a
# fresh one). They outnumbered the real queue 148-to-27, and each looked like a
# plausible report, so the pile never read as a defect.
#
# The point fix was one missing stub. This generalises it: a suite must not reach
# the live plane BY ACCIDENT.
#
# GUARDED BY DESTINATION, NOT BY MODULE. The first version patched `urlopen` on
# `issue_client` and `decision_client` individually, and review found that
# understated by half: `signal_client` writes signals the same way, and
# `bin/mcp-ops-server` is a fourth client that `_load_cli` never registers in
# sys.modules, so no per-module loop can reach it. A banner promising "no test may
# reach the live plane" while covering half the clients is worse than no banner —
# it stops the next person looking.
#
# Every client resolves `urllib.request.urlopen` through the SAME module object at
# call time, so patching it once covers all four, plus any client added later.
# Refusal is keyed on the ops-plane HOST, so a test doing unrelated HTTP is
# unaffected and reaches the real urlopen.
#
# Tests that legitimately exercise a client patch `urlopen` themselves, inside the
# test body and therefore after this fixture, so their patch wins. Tests that
# install a fake client in sys.modules never get here either.
# **BOTH PLANES, because at the write cutover the vaults move to the Library and
# a guard that knows only Orient's host stops guarding the thing it exists to
# protect.** After the cutover the live vault is the Library, so a test reaching
# 127.0.0.1:8100 would be writing to the real Chart House and the real Archives
# with nothing in the way — the same failure this fixture already prevents on
# Orient, in the direction nobody had a reason to look. Found by
# `test_vault_plane.py`'s sweep, which is what that sweep is for.
#
# The Library's loopback address is written out here as well as read from
# `vault_plane`, and the literal is the point rather than a duplicate: this guard
# has to refuse a test that reaches a live plane WHATEVER the configuration says,
# including through a bug that bypasses `vault_plane` entirely — which is exactly
# the bug class this module's sweep exists to catch. The dynamic entry follows
# configuration; the literal does not.
_OPS_HOSTS = {
    urllib.parse.urlsplit(vault_plane.ORIENT_BASE_URL).netloc,
    "127.0.0.1:8100",
}
if vault_plane.LIBRARY_BASE_URL:
    _OPS_HOSTS.add(urllib.parse.urlsplit(vault_plane.LIBRARY_BASE_URL).netloc)
_OPS_HOSTS.discard("")


@pytest.fixture(autouse=True)
def _tests_are_not_dispatched_runs(monkeypatch):
    """`agentctl spawn` refuses inside a dispatched run (run_sandbox.inside_dispatched_run).
    A coder runs this suite inside its own run, so the suite's in-process spawn tests would
    all be refused there. The refusal has its own tests, which set the answer themselves.
    run_sandbox's ancestry tests call the original function, captured at import below."""
    import run_sandbox
    monkeypatch.setattr(run_sandbox, "inside_dispatched_run", lambda: False)


@pytest.fixture(autouse=True)
def _no_unpriced_model_alerts(monkeypatch):
    """`cost_from_usage` raises an operating-plane alert for an unpriced model.
    Every test that prices a made-up model would otherwise start a thread and
    write claim files; a test of the alert sets `T._ALERT_RUNNER` itself."""
    import telemetry
    monkeypatch.setattr(telemetry, "_ALERT_RUNNER", lambda fn: None)


@pytest.fixture(autouse=True)
def _no_live_ops_writes(monkeypatch):
    real_urlopen = urllib.request.urlopen
    attempted = []

    def _guarded(req, *a, **k):
        url = getattr(req, "full_url", None) or (req if isinstance(req, str) else "")
        if any(host in str(url) for host in _OPS_HOSTS):
            attempted.append(str(url))
            raise RuntimeError(
                f"a test tried to reach a LIVE vault plane ({url}). Patch urlopen "
                "on the client under test, as tests/test_issue_client.py does, or "
                "install a fake module in sys.modules.")
        return real_urlopen(req, *a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", _guarded)
    yield
    if attempted:
        # Loud even when the caller swallows the error, which the clients do by
        # design — a queue failure must not cost the Slack post. (`post_bug`
        # was the other such caller; deleted 2026-09-11, ADR-0073.)
        pytest.fail(f"live ops-plane call attempted {len(attempted)}x: {attempted}")


# --- no test reaches the Library through pinned_opener() (PR #801) ----------
#
# THE GAP THE GUARD ABOVE HAS. `issue_client.pinned_opener()` builds its OWN
# `OpenerDirector` (a fresh `build_opener(...)`, not the module-level default),
# so a call through it — `.open(req)` — never passes through `urllib.request.
# urlopen`, and `_no_live_ops_writes` above, which patches only `urlopen`,
# cannot see it. `agentctl._library_get` (`default_profile`'s `GET /profiles`
# and `resolve_agent`'s `GET /reference/documents`) and `issue_client.
# get_item_from_default_library`/`post_review_record` all go through this
# opener. On PR #801's CI run (37586286188), ~20 `default_profile` tests
# reached it unfaked: a hosted runner with no real credential turned that into
# a loud `FileNotFoundError`-driven refusal, and a machine WITH the real
# `~/.orient-control/agent-platform-token` would have turned it into a silent
# pass against the real Library instead.
#
# `_guard_pinned_opener` is a plain function (not hidden in the fixture
# closure) so `tests/test_conftest_library_opener_guard.py` can exercise it
# directly, the way `test_conftest_farm_guard.py` exercises `_NoFarmPath`.
#
# SOME CALLERS SWALLOW THE ERROR. `get_item_from_default_library`'s own
# docstring says "Never raises" — it catches bare `Exception` — so a guard
# that only raises inside the patched opener would be absorbed into an
# `"error:RuntimeError"` result and never fail the test. Collecting attempted
# calls and failing at teardown, as `_no_live_ops_writes` does above, catches
# that case too.
#
# Tests that legitimately exercise this opener patch `issue_client.
# pinned_opener` themselves (`tests/test_agentctl.py`'s `library` fixture) or
# point it at a real LOCAL listener on an ephemeral port, never `_OPS_HOSTS`
# (`tests/test_crew_doctor.py`'s `listeners`, `tests/test_issue_client.py`'s).
def _guard_pinned_opener(real_pinned_opener, attempted):
    def guarded_pinned_opener():
        opener = real_pinned_opener()
        real_open = opener.open

        def _guarded_open(req, *a, **k):
            url = getattr(req, "full_url", None) or (req if isinstance(req, str) else "")
            if any(host in str(url) for host in _OPS_HOSTS):
                attempted.append(str(url))
                raise RuntimeError(
                    f"a test tried to reach a LIVE Library through pinned_opener() "
                    f"({url}). Patch issue_client.pinned_opener, as "
                    "tests/test_agentctl.py's `library` fixture does.")
            return real_open(req, *a, **k)

        opener.open = _guarded_open
        return opener
    return guarded_pinned_opener


@pytest.fixture(autouse=True)
def _no_live_pinned_opener_calls(monkeypatch):
    import issue_client
    attempted: list[str] = []
    monkeypatch.setattr(issue_client, "pinned_opener",
                        _guard_pinned_opener(issue_client.pinned_opener, attempted))
    yield
    if attempted:
        pytest.fail(f"live Library call via pinned_opener() attempted "
                    f"{len(attempted)}x: {attempted}")


# --- no test reads the Library's roster (E9·3) -------------------------------
#
# knowledge.py admits a document's `audience` against the Library's roster, read
# from GET /roster on every decision. A validation in any test would therefore reach
# the live Library, and `_no_live_ops_writes` above would fail it. This stubs the
# read with the seed the loader reads from the literal, so validation sees the same
# names it always did. test_knowledge_roster.py restores the real read to test it.
@pytest.fixture(autouse=True)
def _library_roster_is_the_seed(monkeypatch):
    import knowledge
    seed = frozenset(knowledge.ALLOWED_AUDIENCE)
    monkeypatch.setattr(knowledge, "_fetch_roster", lambda: seed, raising=False)
    monkeypatch.setattr(knowledge, "_roster_cache", None, raising=False)


# --- the `needs_kb` marker (substrate ARCH-436, PR G) ------------------------
#
# THE PROBLEM IT NAMES. Several guards in this suite assert that a constant here
# agrees with the knowledge repo — ALLOWED_PROJECTS against the KB validator,
# KNOWN_KB_PROJECTS against the real project directories, AGENTS_DIR against the
# real roster. CI checks out agent-platform alone, so every one of them called
# `pytest.skip` there. Both readings then agree and both are worthless: main is
# green because the check did not run, and a machine WITH the KB fails the same
# assertions the moment anyone looks. That is how PR E2's drift survived until
# PR F found three more stale copies by hand.
#
# THE FIX IS NOT TO MAKE CI CHECK OUT THE KB. That needs a standing credential
# with read access to the whole private knowledge repo, available to every
# workflow run, for a handful of tests. The same structural constraint produced
# `bin/check-cli-contracts` (a cross-repo check that cannot run in CI because CI
# has no sibling checkouts) and it takes the same answer: run it where the
# checkouts are. `bin/check-kb-guards` runs `pytest -m needs_kb` daily on the
# mini and files an issue on breach.
#
# So the marker does two jobs. It centralises the skip, replacing hand-rolled
# `pytest.skip` bodies that each had to re-derive the checkout path. And it makes
# the set ENUMERABLE — `-m needs_kb` is what the scheduled runner selects, so a
# guard added later joins the daily check by carrying the marker, with no second
# place to register it. A registry you must remember to update is the drift shape
# this whole pass is undoing.
#
# KNOWLEDGE_REPO overrides the location, which is how a paired cross-repo change
# is verified from either side before both halves have merged (PR C's reviewer
# used it for exactly that).
def knowledge_repo() -> Path:
    """The knowledge checkout these guards read, honouring KNOWLEDGE_REPO.

    Defaults to `<root>/knowledge`, the checkout the code under test reads, and not
    a literal ~/Convivy/knowledge: on a machine whose root is elsewhere the two
    disagree, and a `needs_kb` guard then neither skips nor finds its files
    (#553 round 3). Imported here so it follows whatever root the run resolves."""
    override = os.environ.get("KNOWLEDGE_REPO")
    if override:
        return Path(override)
    import builder_root  # noqa: PLC0415 - resolved at call time, see above
    return builder_root.root() / "knowledge"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_kb: asserts agreement with the knowledge repo; skipped where no "
        "checkout exists (CI). Run daily by bin/check-kb-guards.")


def pytest_runtest_setup(item):
    if item.get_closest_marker("needs_kb") and not (knowledge_repo() / "projects").is_dir():
        pytest.skip(
            f"no knowledge checkout at {knowledge_repo()}; this guard runs where "
            f"the KB is (bin/check-kb-guards), not in CI")


# --- the plane every client test runs against -----------------------------------
#
# PIN IT, so no test depends on the DEPLOYED default. `vault_plane.VAULTS_ON_LIBRARY`
# is read from the environment at import and decides whether a client builds Orient's
# path shape or the Library's. Roughly three dozen assertions across
# `test_ops_mcp.py`, `test_issue_client.py`, `test_signal_client.py` and
# `test_decision_client.py` spell out a literal `/api/...` URL — they are testing
# what each client SENDS, and the plane is incidental to that.
#
# Those assertions were written when unset meant Orient, so they silently inherited
# the production default and all 32 of them broke the moment it inverted. A test that
# changes colour because a deployment constant moved was never testing the client; it
# was testing the constant, once, by accident. Pinning here makes the dependency
# explicit and means the next plane change touches this fixture rather than every
# assertion in four files.
#
# Orient's shape is the one those literals hold, and translation to the Library's
# shape is covered directly and in both directions by `test_vault_plane.py`, so
# nothing loses coverage by pinning here. `test_vault_plane.py`'s own `on_library` /
# `on_orient` fixtures set the same attribute per test and run after this one, so
# that file keeps deciding its own plane.
# --- no test may reach the LIVE knowledge index (issue_01M2C0K5C228VGJRV04THX67Q4) ---
#
# THE INCIDENT. `lib/knowledge/indexer.py` binds `DB` (from `KB_INDEX_DB`, default
# `~/.knowledge-index/index.db`) and `PROJECTS` once at import. `full_scan_and_
# reconcile()` treats any file missing from `PROJECTS` as deleted and drops its row
# from whatever `DB` still is. On 2026-09-12 a suite run had `builder_root.CONFIG`
# pointed at a missing file, which resolved `PROJECTS` to a directory that does not
# exist; `test_indexer_root_marker.py`'s `machine` fixture patched `KNOWLEDGE` but
# not `DB`, so the reconcile deleted every row from the real, live index and broke
# every roster dispatch on the machine for six minutes.
#
# GUARDED BY DEFAULT, autouse, so a test that forgets to repoint `indexer.DB` still
# gets an empty, schema-initialized, per-test file rather than the real index. A
# test that wants its own database (`test_kb_sync.py`'s `corpus`/`kb_repos`
# fixtures) repoints `indexer.DB` again after this runs, in its own fixture body —
# that later `monkeypatch.setattr` simply wins, teardown restores this one's value
# first regardless.
#
# Uses `tmp_path_factory`, not the test's own `tmp_path`: several tests (the
# `cache` fixture in `test_co_working_on.py`) repurpose `tmp_path` as a directory
# they assert is exactly empty, and a file dropped into it by this fixture broke
# four of them the first time this landed.
@pytest.fixture(autouse=True)
def _isolate_knowledge_index_db(monkeypatch, tmp_path_factory):
    import indexer

    db = tmp_path_factory.mktemp("isolated-index") / "index.db"
    monkeypatch.setattr(indexer, "DB", db)
    indexer.ensure_db()
    return db


@pytest.fixture(autouse=True)
def _pin_the_plane(monkeypatch):
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)


@pytest.fixture(autouse=True)
def _policy_client_live_empty(monkeypatch):
    """Every dispatch asks the Library for its tool policy, and a dispatch the Library does not
    answer is refused. So the suite's default answer is a live record with no classes and no
    operator denials: tests about other things run as they did, and a test about policy stubs
    `policy_client.resolve` itself, with a live answer or an unanswered one.
    """
    try:
        import policy_client
    except Exception:                       # module not importable in some envs
        return
    def _no_policy(*a, **k):
        return {"agent": None, "crew": None, "model": None, "explicit": {},
                "exfiltration_builtins": [], "connector_servers": [],
                "default": "allow", "version": 0}
    monkeypatch.setattr(policy_client, "resolve", _no_policy)

    # The per-dispatch memo is PROCESS-scoped, so without clearing it one test's
    # answer leaks into the next and a later test reads a cached verdict instead
    # of exercising its own stub — a test passing for the previous test's reason.
    # Found by ATTRIBUTE rather than by path: `_load_cli` registers the module
    # under a name derived from the filename and its `__file__` need not match a
    # path this fixture reconstructs. Matching on the attribute cannot miss the
    # module the tests actually hold, which a path comparison did — the two
    # tests that then failed passed in isolation and failed in a full run, which
    # is the signature of exactly this.
    import sys as _sys
    for mod in list(_sys.modules.values()):
        memo = getattr(mod, "_LIBRARY_DENIALS_MEMO", None)
        if isinstance(memo, dict):
            memo.clear()


# --- no test writes under the REAL tool-session scratch root (CQ·138 round 6) ---
#
# `run_sandbox.create_tool_session_scratch_dir` creates one dispatch's scratch
# directory under `tool_session_scratch_root(home)`, read through `RS.passwd_home()`
# — the real account home, by design (that function cannot be redirected by $HOME
# the way `Path.home()` can; see its own docstring). A test that drives
# `_run_session`/`_spawn_claude` for real without stubbing `create_tool_session_
# scratch_dir` therefore creates, and never removes, a real directory under THIS
# machine's own `~/.local/state/agent-platform/tool-sessions` (reviewer, PR #879,
# comment 6166526636: found live in `tests/test_spawn_sandbox.py`'s 11 call sites
# and one test in `tests/test_codex_review_dispatch.py`).
#
# `_tool_session_scratch_dir_off_the_real_account_home`, below, is the fix a test
# file applies: `tests/test_agentctl.py` and `tests/test_spawn_sandbox.py` apply it
# to every test in the file via a module-wide `pytestmark`; the one real-dispatch
# test each in `tests/test_codex_review_dispatch.py` and `tests/
# test_strict_mcp_config_live.py` apply it directly with `pytest.mark.usefixtures(
# ...)`.
#
# `_no_directory_appears_under_the_real_tool_session_scratch_root`, below that, is
# the backstop for the WHOLE suite, not only the files named above: it records what
# the real root already holds before this session runs, and fails if anything new
# appears after it, whichever test created it — including one added later that
# forgets to apply the fixture above. It only ever lists the root (`Path.iterdir`);
# it never opens, renames or removes anything under it, this machine's own account
# included.
@pytest.fixture
def _tool_session_scratch_dir_off_the_real_account_home(agentctl, tmp_path_factory, monkeypatch):
    """CQ·138: a real (non-dry-run) dispatch creates its own tool-session scratch
    directory under the account's REAL home (`run_sandbox.tool_session_scratch_root`)
    rather than the shared system temp root — `passwd_home()`, unlike `Path.home()`,
    cannot be redirected by an environment variable, by design (see that function's
    own docstring for why a real dispatch needs it to stay that way). Left unstubbed,
    a test driving `_run_session`/`_spawn_claude` for real would create (and never
    remove) a real directory under THIS machine's own `~/.local/state/agent-platform/
    tool-sessions` — which this test harness's own sandbox refuses to write to at
    all, and which `_no_directory_appears_under_the_real_tool_session_scratch_root`,
    below, fails the whole session over. Defaults to a fresh tmp directory for every
    test that applies it; a test that stubs `create_tool_session_scratch_dir` itself,
    later in its own body, wins over this default."""
    real_create = agentctl.RS.create_tool_session_scratch_dir
    fake_home = tmp_path_factory.mktemp("tool-session-home")
    monkeypatch.setattr(agentctl.RS, "create_tool_session_scratch_dir",
                        lambda home=None: real_create(fake_home))


@pytest.fixture(scope="session", autouse=True)
def _no_directory_appears_under_the_real_tool_session_scratch_root():
    """The backstop described above the fixture this depends on: records what
    `run_sandbox.tool_session_scratch_root(run_sandbox.passwd_home())` already holds
    before this session runs, and fails if a NEW entry appears there after it,
    whichever test created it — not only the files `_tool_session_scratch_dir_off_
    the_real_account_home` is applied to today. Only ever lists the root
    (`Path.iterdir`); never opens, renames or removes anything under it."""
    root = run_sandbox.tool_session_scratch_root(run_sandbox.passwd_home())
    before = set(root.iterdir()) if root.is_dir() else set()
    yield
    after = set(root.iterdir()) if root.is_dir() else set()
    new = after - before
    assert not new, (
        f"{len(new)} new entry(ies) appeared under the REAL tool-session scratch "
        f"root ({root}) during this test session: {sorted(p.name for p in new)}. "
        "A test drove real dispatch code (_run_session/_spawn_claude/"
        "create_tool_session_scratch_dir) without stubbing it onto a temporary "
        "home — apply _tool_session_scratch_dir_off_the_real_account_home.")
