"""`agentctl crew create <id>` — one command that makes a working crew.

issue_01M4AB9X62112MCG8B5501CMKV, slice 3 of the crew-creation epic
(issue_01M2NYBBY0FXCDHWWD8XBR9RR3). Before this, the `new-crew` skill had a
person create the directory, the tmux session and the Deck spec by hand, grep
two files for the agent prefix, and never call `crew register`. Three crews
missed a different site that way. This module composes the steps that already
exist, in the order the epic's slice plan gives:

  1. register the crew (`crew_registration.register`, the same path
     `agentctl crew register` takes),
  2. create the working directory and its git repository,
  3. write the Deck crew spec Deck reads from `crews.d`,
  4. propose the default profile through `kb_propose`,
  5. run the first `knowledge-ctx use` for the crew,
  6. load the profile so the Library serves it,
  7. start the crew's tmux session,
  8. run `crew doctor <id>` and exit with its status.

EACH STEP REPORTS WHAT IT DID, AND A REFUSAL STOPS THERE. Earlier steps stay
reported, and nothing after the refusal runs.

PENDING IS NOT A REFUSAL. Steps 5 and 6 need the Library to serve
`<id>/daytime`. A profile is served only after its proposal merges and the
Library's loader runs on the Library's host. Until then:

  - step 5 is pending, because `knowledge-ctx use` would render the wrong
    profile. A project with no profile files in the knowledge checkout falls
    back to `platform/default` with only a stderr line, so running it early
    would individuate the crew into the platform's agents;
  - step 6 is pending, with the loader command that finishes it;
  - step 7 is pending, because a `co` session started before individuation
    leaves a plain session, and `knowledge-ctx` refuses to write into a
    directory a Claude session is running in;
  - step 8 runs anyway, so the report names every site still missing.

RE-RUNS FINISH. `create` run again on a partly created crew does the steps
that are still undone and reports the ones already done. Each step checks what
exists before it acts:

  - step 1 accepts a registry row identical to the one this run would write,
    and refuses a different one, naming the fields that differ;
  - step 3 keeps an existing spec for the same crew, with its `sessionId`;
  - step 4 does not propose twice. It skips when the profile is already in the
    knowledge checkout or served, and it reports the URL of a proposal an
    earlier run recorded in `.git/crew-create/`;
  - step 5 skips the render when `.claude/.active-profile` already names the
    crew's profile;
  - step 7 reuses the crew's session when one of that exact name is running.

So after the proposal merges and the Library loads it, the same command
finishes the crew. That is the run the throwaway-crew trial in the epic's
definition of done makes.

The external calls (the Library proposal, the served-profile read, the doctor)
arrive through `Env`, so a test drives the steps against a stub Library and
never reaches the real one. The tmux socket comes from `CREW_TMUX_SOCKET`, and
unset means the default server, which is production. Every tmux call here
strips `TMUX` and `TMUX_PANE`, so a `create` run from inside a pane cannot reach
the operator's server through inherited state.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import crew_registration as CR
import projects

DONE = "done"
PENDING = "pending"
REFUSED = "refused"
FAILED = "failed"

DEFAULT_VARIANT = "daytime"
TMUX_TIMEOUT = 15
ISSUE = "issue_01M4AB9X62112MCG8B5501CMKV"

# tmux's wording when no server listens on the socket. An empty session list,
# not a refusal: a socket with no server holds no sessions.
_NO_SERVER = ("no server running", "error connecting to")


class ProposeError(Exception):
    """The proposal was refused, or could not be made."""


@dataclass(frozen=True)
class Env:
    """Everything `create` reaches outside this module. Production passes the
    real callables; a test passes stubs and temp paths."""
    registry_path: Path | None
    crews_dir: Path
    knowledge_ctx: Path
    knowledge_root: Path
    library_loader: Path
    host: str
    propose: Callable[[str, str, str, str], str]
    served: Callable[[str], tuple[dict | None, str]]
    doctor: Callable[[str], int]
    tmux_socket: str | None = None
    emit: Callable[[str], None] = print


def profile_path(crew: str) -> str:
    return f"projects/{crew}/profiles/{DEFAULT_VARIANT}.yaml"


def default_profile(crew: str) -> str:
    """The starting dispatch profile for a new crew: the two review agents every
    crew that opens PRs needs (a missing stamp fails the merge gate), and scout.
    Anything wider is a knowledge decision, made through a later proposal."""
    return (
        f"profile: {crew}/{DEFAULT_VARIANT}\n"
        "\n"
        f"# The starting dispatch profile for the {crew} crew, written by\n"
        f"# `agentctl crew create` ({ISSUE}). It gives the crew its review agents and\n"
        "# scout. Add documents to it through a later knowledge proposal.\n"
        "\n"
        "include:\n"
        "  - kind: agent\n"
        "    audience_contains:\n"
        "      - reviewer\n"
        "      - security-reviewer\n"
        "      - scout\n"
    )


def _report(env: Env, crew: str, step: str, state: str, detail: str) -> None:
    env.emit(f"crew create {crew}: {step} — {state}: {detail}")


def _refuse(env: Env, crew: str, step: str, detail: str) -> int:
    _report(env, crew, step, REFUSED, detail)
    env.emit(f"crew create {crew}: stopped at {step}; nothing after it ran.")
    return 1


def _write_json(path: Path, body: dict[str, Any]) -> None:
    """Atomic, and mode 0644, which every spec in crews.d carries. `mkstemp` makes
    0600, and Deck reads these files from another process on the same account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tmux(args: list[str], socket: str | None) -> subprocess.CompletedProcess:
    cmd = ["tmux", *(["-L", socket] if socket else []), *args]
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=TMUX_TIMEOUT, check=False)


def _session_rows(socket: str | None) -> list[tuple[str, str]] | None:
    """`(session_id, session_name)` for each session on the socket. `[]` when no
    server listens there; None when tmux failed another way."""
    cp = _tmux(["list-sessions", "-F", "#{session_id} #{session_name}"], socket)
    if cp.returncode != 0:
        if any(marker in (cp.stderr or "") for marker in _NO_SERVER):
            return []
        return None
    rows = []
    for line in cp.stdout.splitlines():
        sid, _, name = line.partition(" ")
        if sid and name:
            rows.append((sid, name))
    return rows


def _step_register(env: Env, crew: str, stored_spec, row: dict) -> int | None:
    """Step 1. Returns a refusal status, or None when the row is registered."""
    registry = CR.resolved_registry_path(env.registry_path)
    try:
        data = CR.load_registry(registry)
    except CR.RegistrationRefused as exc:
        return _refuse(env, crew, "step 1 register", str(exc))
    if crew in data:
        differ = sorted(k for k in set(row) | set(data[crew]) if row.get(k) != data[crew].get(k))
        if differ:
            return _refuse(env, crew, "step 1 register",
                           f"{crew!r} is registered in {registry} with different fields "
                           f"({', '.join(differ)}); not overwritten")
        _report(env, crew, "step 1 register", DONE, f"already registered in {registry}")
        return None
    try:
        path, _row = CR.register(stored_spec, registry_path=env.registry_path)
    except CR.RegistrationRefused as exc:
        return _refuse(env, crew, "step 1 register", str(exc))
    _report(env, crew, "step 1 register", DONE, f"registered in {path}")
    return None


def _step_directory(env: Env, crew: str, working_dir: Path) -> int | None:
    """Step 2. Dispatch finds a crew's checkout by its `.git`, so this must be a
    repository before anything reads the directory. `git init` makes no commit
    and needs no identity."""
    if working_dir.exists() and not working_dir.is_dir():
        return _refuse(env, crew, "step 2 directory",
                       f"{working_dir} exists and is not a directory")
    working_dir.mkdir(parents=True, exist_ok=True)
    if (working_dir / ".git").exists():
        _report(env, crew, "step 2 directory", DONE,
                f"{working_dir} is already a git repository")
        return None
    cp = subprocess.run(["git", "-C", str(working_dir), "init", "-b", "main"],
                        capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        return _refuse(env, crew, "step 2 directory",
                       f"git init failed: {(cp.stderr or cp.stdout).strip()[-200:]}")
    _report(env, crew, "step 2 directory", DONE,
            f"created {working_dir} as a git repository")
    return None


def _step_deck_spec(env: Env, crew: str, display: str, working_dir: Path) -> tuple[int | None, dict]:
    """Step 3. The keys follow the specs crews.d already holds (see session_crews,
    the reader). `sessionId` is added by step 7, once the session exists."""
    spec_path = env.crews_dir / f"{crew}.json"
    want = {
        "id": crew,
        "displayName": display,
        "workingDirectory": str(working_dir),
        "host": env.host,
        "sessions": [],
    }
    if not spec_path.exists():
        _write_json(spec_path, want)
        _report(env, crew, "step 3 deck spec", DONE, f"wrote {spec_path}")
        return None, want
    try:
        have = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _refuse(env, crew, "step 3 deck spec",
                       f"{spec_path} exists and is not readable JSON; not overwritten"), {}
    if not isinstance(have, dict) or any(
            have.get(k) != want[k] for k in ("id", "displayName", "workingDirectory")):
        return _refuse(env, crew, "step 3 deck spec",
                       f"{spec_path} names a different crew; not overwritten"), {}
    _report(env, crew, "step 3 deck spec", DONE,
            f"{spec_path} already written (its sessionId is kept)")
    return None, have


def _step_proposal(env: Env, crew: str, working_dir: Path, served: bool) -> int | None:
    """Step 4. The Library's proposal route opens the pull request. This module never
    writes under `projects/` in the knowledge checkout."""
    rel = profile_path(crew)
    if (env.knowledge_root / rel).is_file():
        _report(env, crew, "step 4 profile proposal", DONE,
                f"{rel} is in the knowledge checkout, so it has merged")
        return None
    if served:
        _report(env, crew, "step 4 profile proposal", DONE,
                f"{crew}/{DEFAULT_VARIANT} is served by the Library, so it has merged")
        return None
    marker = working_dir / ".git" / "crew-create" / f"{crew}.proposal"
    if marker.is_file():
        _report(env, crew, "step 4 profile proposal", PENDING,
                f"{rel} was proposed as {marker.read_text(encoding='utf-8').strip()}; "
                f"it merges on its own review, so not proposed again")
        return None
    try:
        url = env.propose(rel, default_profile(crew),
                          f"Add the {crew} crew's default profile",
                          f"Written by `agentctl crew create {crew}` ({ISSUE}).")
    except ProposeError as exc:
        return _refuse(env, crew, "step 4 profile proposal", str(exc))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(url + "\n", encoding="utf-8")
    _report(env, crew, "step 4 profile proposal", DONE, f"{rel}: {url}")
    return None


def _step_render(env: Env, crew: str, working_dir: Path) -> int | None:
    """Step 5. Individuation: `use` writes the profile's agents into `.claude/`."""
    want = f"{crew}/{DEFAULT_VARIANT}"
    try:
        active = (working_dir / ".claude" / ".active-profile").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    if active == want:
        _report(env, crew, "step 5 knowledge-ctx use", DONE, f"already individuated as {want}")
        return None
    cp = subprocess.run([str(env.knowledge_ctx), "use", DEFAULT_VARIANT], cwd=str(working_dir),
                        capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or "").strip().splitlines()
        return _refuse(env, crew, "step 5 knowledge-ctx use",
                       f"exit {cp.returncode}: {tail[-1][:200] if tail else 'no output'}")
    _report(env, crew, "step 5 knowledge-ctx use", DONE,
            f"individuated {want} into {working_dir}/.claude")
    return None


def _step_tmux(env: Env, crew: str, working_dir: Path, body: dict) -> int | None:
    """Step 7. Reuses a session of the crew's exact name when one is running. A
    prefix collision with another session is refused, because tmux resolves a
    bare `-t` target by prefix."""
    rows = _session_rows(env.tmux_socket)
    if rows is None:
        return _refuse(env, crew, "step 7 tmux session", "tmux could not list its sessions")
    sid = next((s for s, n in rows if n == crew), None)
    if sid is None:
        for other_sid, name in rows:
            if crew.startswith(name) or name.startswith(crew):
                return _refuse(
                    env, crew, "step 7 tmux session",
                    f"session {name!r} ({other_sid}) is a prefix of {crew!r} or the reverse; "
                    f"tmux resolves a bare -t target by prefix, so the two would collide")
        cp = _tmux(["new-session", "-d", "-s", crew, "-c", str(working_dir),
                    'co; exec "$SHELL"'], env.tmux_socket)
        if cp.returncode != 0:
            return _refuse(env, crew, "step 7 tmux session",
                           f"tmux new-session failed: {(cp.stderr or '').strip()[-200:]}")
        sid = next((s for s, n in (_session_rows(env.tmux_socket) or []) if n == crew), None)
        if sid is None:
            return _refuse(env, crew, "step 7 tmux session",
                           f"tmux created no session named {crew!r} on the socket")
        _report(env, crew, "step 7 tmux session", DONE, f"started {crew} as {sid}")
    else:
        _report(env, crew, "step 7 tmux session", DONE, f"{crew} is already running as {sid}")
    body["sessionId"] = sid
    _write_json(env.crews_dir / f"{crew}.json", body)
    return None


def create(crew: str, *, display: str, working_dir: Path, spec: Any, env: Env) -> int:
    """Run the eight steps for `crew`. Returns the exit status: 1 on a refusal,
    otherwise the doctor's status."""
    root = projects.crews_root().resolve()
    working_dir = working_dir.resolve()
    try:
        rel = working_dir.relative_to(root)
    except ValueError:
        return _refuse(env, crew, "step 0 preflight",
                       f"{working_dir} is not under {root}; the registry stores a crew's "
                       f"home root-relative, so the directory must sit under the crews root")

    # The registry stores a home root-relative (`<root>/<rel>`) because
    # `projects._expand_own_home` refuses every other form.
    stored = replace(spec, home_dir=f"<root>/{rel.as_posix()}")
    try:
        row = CR.build_row(stored)
    except CR.RegistrationRefused as exc:
        return _refuse(env, crew, "step 1 register", str(exc))
    if (refused := _step_register(env, crew, stored, row)) is not None:
        return refused
    if (refused := _step_directory(env, crew, working_dir)) is not None:
        return refused
    refused, body = _step_deck_spec(env, crew, display, working_dir)
    if refused is not None:
        return refused

    # Steps 4 to 7 turn on the Library serving the profile. Asking it is the check
    # at each of them, since the merge state of the proposal is not visible here.
    served_body, err = env.served(crew)
    if served_body is None and err != "404":
        return _refuse(env, crew, "step 4 profile proposal",
                       f"could not ask the Library whether it serves "
                       f"{crew}/{DEFAULT_VARIANT}: {err}")
    served = served_body is not None
    if (refused := _step_proposal(env, crew, working_dir, served)) is not None:
        return refused

    loader = (f"python3.11 {env.library_loader} profiles {env.knowledge_root} "
              f"--actor \"crew create {crew}\"")
    if served:
        if (refused := _step_render(env, crew, working_dir)) is not None:
            return refused
        _report(env, crew, "step 6 load profile", DONE,
                f"the Library serves {crew}/{DEFAULT_VARIANT}")
        if (refused := _step_tmux(env, crew, working_dir, body)) is not None:
            return refused
    else:
        _report(env, crew, "step 5 knowledge-ctx use", PENDING,
                f"the Library does not serve {crew}/{DEFAULT_VARIANT} yet, and `use` would "
                f"render platform/default. Run `crew create {crew}` again once step 6 is done.")
        _report(env, crew, "step 6 load profile", PENDING,
                f"after {profile_path(crew)} merges, run on the Library's host: {loader}")
        _report(env, crew, "step 7 tmux session", PENDING,
                "starts after step 5: a `co` session started now would run as a plain "
                "session, and knowledge-ctx refuses to individuate under a live Claude session")

    # Step 8. The doctor's status is the command's status. A pending crew cannot
    # pass it, since the profile site reads MISSING until the Library serves it.
    env.emit(f"crew create {crew}: step 8 crew doctor — running")
    rc = env.doctor(crew)
    _report(env, crew, "step 8 crew doctor", DONE if rc == 0 else FAILED, f"exit {rc}")
    return rc
