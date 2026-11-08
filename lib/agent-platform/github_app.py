"""Shared GitHub App authentication primitives.

One JWT/installation-token implementation, parameterised by key path, app id and
installation, used by every caller that authenticates to GitHub as a Convivy
GitHub App rather than as the operator's personal login. Two callers today:

- `kb_merge.py` — the Library merges its own knowledge PRs as `convivy-library`,
  an App holding `contents:write` and `pull_requests:write` on `convivy/knowledge`
  alone (platform/decisions/0078).
- `bin/pr-state-capture` — the Bosun reads per-repository pull-request state as
  `convivy-bosun`, an App installed org-wide, for a read-only capture (see that
  script's module docstring for why this is a separate read from the gates').

`mint` and `mint_token` are `bin/agentctl`'s own minter for the convivy-bosun App,
one function for every verb (E10 S1, platform/specs/github-credential-seam, the
operator's 2026-10-17 ruling that `agentctl` is the only thing on this machine
that issues GitHub credentials): `VERB_PERMISSIONS` names the one permissions
table each verb asks GitHub for, and every mint appends one line to the mint
log (`mint_log_path`) recording what GitHub granted, never the token itself.
`_mint_run_github_token` in `bin/agentctl` is `mint`'s first caller, for the
per-run read token a dispatched run gets; `cmd_publish` is `mint_token`'s first
caller (E10 S2), pushing and fetching over `push_env`'s environment rather than
`origin`; later slices add further `mint_token` callers for pr-open, verdict and
PR comments.

Nothing here decides WHICH facts admit an action (a merge, in kb_merge's case) —
that stays in the caller, per platform/decisions/0056's "the conditions are the
control, not the credential." This module only mints tokens and makes requests.

Stdlib only, like `agentctl`: the App's JWT is signed by the system `openssl`
rather than a Python JWT package.

`app_jwt` RUNS `/usr/bin/openssl` BY ABSOLUTE PATH, NEVER THE BARE NAME (round 3
of #732, finding 2). `bin/review-launch` runs this module as root, reached only
through `sudo`, whose `env_reset` (kept) builds `PATH` from `/etc/sudoers`'
`secure_path` if one is configured and otherwise falls back to a default this
module must not depend on either way — a bare `"openssl"` would resolve through
whatever that turns out to be, which on this machine's `PATH` for an interactive
shell is Homebrew's copy (owned by the operator's own account and the `admin` group, which can write its
`bin`), not the OS-shipped one. `kb_merge.py` and `bin/pr-state-capture`, this
module's other two callers, both run as `agents` under a fixed environment of
their own (ARCH-346) and were never exposed to that `PATH`-dependence the way a
root caller is, but the absolute path costs them nothing either.
"""
from __future__ import annotations

import base64
import dataclasses
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, ExitStack
from pathlib import Path

import builder_root as BR
import crew_key as CK
import operator_config

API = "https://api.github.com"

# Never the bare "openssl" — see the module docstring's "RUNS /usr/bin/openssl
# BY ABSOLUTE PATH" note (round 3 of #732, finding 2).
OPENSSL = "/usr/bin/openssl"

# Every verb the convivy-bosun App mints a token for, and the one permissions
# table each asks GitHub for (E10 S1, platform/specs/github-credential-seam,
# the operator's 2026-10-17 ruling that `agentctl` is the only thing on this
# machine that issues GitHub credentials). "read" is `bin/agentctl`'s own
# `_RUN_TOKEN_PERMISSIONS`, moved here so the per-run token and every other
# verb mint through one copy of the table rather than two that could drift.
# metadata:read is implicit on every installation token (`IMPLICIT_PERMISSIONS`,
# below) and is not named here. Asking for a key the installation itself does
# not grant gets the WHOLE token refused by GitHub with 422, not just that key
# dropped (see `_RUN_TOKEN_PERMISSIONS`'s old comment in `bin/agentctl`) — which
# is why `mint` drops `workflows` from the publish request below when the
# installation does not hold it, rather than asking for it unconditionally.
#
# publish, pr-open, pr-ready, verdict, pr-comment, pr-close, pr-reopen and
# release are the operator's own choice of least permission for each call,
# not a derived default:
#   - publish: contents:write (push/merge) and pull_requests:read, plus
#     workflows:write ONLY WHEN THE INSTALLATION ALREADY HOLDS IT (`mint`
#     checks the installation object it already read, below, and asks for
#     `workflows` on the publish request only then). Convivy's own grant of
#     workflows rests on the operator's 2026-10-17 choice, made for
#     repositories that hold no Actions secrets and run on a fenced runner;
#     an adopter App does not get `workflows` by default, so publish asks
#     for it only when the installation holds it, rather than failing every
#     adopter publish with GitHub's whole-token 422.
#   - pr-open: pull_requests:write to open the PR, contents:read to read the
#     branch it opens against.
#   - pr-ready: pull_requests:write and contents:write. Measured against a
#     real draft PR on 2026-10-18 (the outage that followed E10 S3/#931):
#     GitHub's `markPullRequestReadyForReview` GraphQL mutation, which
#     `gh pr ready` calls, refuses an App installation token holding only
#     contents:read with "Resource not accessible by integration", and
#     succeeds once the token also holds contents:write. A separate verb
#     from pr-open keeps the wider contents:write scoped to this one call,
#     leaving `gh pr create`/`gh pr edit` on pr-open's narrower contents:read.
#   - verdict: pull_requests:write to post the review verdict as a PR review;
#     contents, checks and actions:read to read what it is verdicting on.
#   - pr-comment: pull_requests:write alone.
#   - pr-close, pr-reopen: pull_requests:write alone, same as pr-comment —
#     split into their own verbs so the mint log names the action a caller
#     took rather than reading "pr-comment" against a close or a reopen.
#   - release: contents:write alone. A release carries no PR, so no
#     pull_requests scope at all. Also the verb `agentctl tag-create` mints —
#     an annotated tag carries no PR either, and needs nothing `release
#     create` doesn't already ask for.
#   - rehearsal: contents:write and pull_requests:write — the two permissions
#     a merge and a direct push to a default branch both need (E10 S11,
#     platform/specs/github-credential-seam, "The rehearsal"). `mint` refuses
#     this verb for any repository but `<owner>/REHEARSAL_REPOSITORY_NAME`
#     with `owner` listed in the install's own operator config, before any
#     network call — see the refusal below.
#   - run-control: actions:write alone, for `agentctl run-rerun`/`run-cancel`
#     (`gh run rerun`/`gh run cancel`) — the operator's 2026-11-05 ruling
#     raising the App's Actions permission from read to write for these two
#     calls only, nothing else. GitHub's REST reference documents both
#     endpoints these two `gh` subcommands call — `POST .../actions/runs/
#     {run_id}/rerun` and `POST .../actions/runs/{run_id}/cancel` — as
#     requiring the "actions" permission at "write" for a GitHub App token,
#     and names no other permission for either, so this verb carries only
#     that one key.
VERB_PERMISSIONS = {
    "read": {"contents": "read", "pull_requests": "read",
             "checks": "read", "actions": "read"},
    "publish": {"contents": "write", "workflows": "write", "pull_requests": "read"},
    "pr-open": {"pull_requests": "write", "contents": "read"},
    "pr-ready": {"pull_requests": "write", "contents": "write"},
    "verdict": {"pull_requests": "write", "contents": "read",
                "checks": "read", "actions": "read"},
    "pr-comment": {"pull_requests": "write"},
    "pr-close": {"pull_requests": "write"},
    "pr-reopen": {"pull_requests": "write"},
    "release": {"contents": "write"},
    "rehearsal": {"contents": "write", "pull_requests": "write"},
    "run-control": {"actions": "write"},
}

# GitHub adds this permission to every installation token it mints, whether
# or not it was requested.
IMPLICIT_PERMISSIONS = {"metadata": "read"}

# The one repository name the `rehearsal` verb will ever mint for (E10 S11,
# platform/specs/github-credential-seam, amended by knowledge#1113). Fixed
# here, in reviewed code, rather than read from config: the owners pairing
# with it are install-specific (an org and the operator's own GitHub login,
# never a literal in this module — platform/decisions/0100, the E36 grep
# this file ships under in a Builder release) and live in the install's own
# operator config instead (`rehearsal_repository_owners`). A caller who can
# write that file can therefore only widen `rehearsal` onto MORE repositories
# literally named `builder-merge-rehearsal`, never onto a real one.
REHEARSAL_REPOSITORY_NAME = "builder-merge-rehearsal"


def expected_granted_permissions(verb: str) -> dict[str, str]:
    """What GitHub should grant a token minted for `verb`: `VERB_PERMISSIONS[verb]`
    plus `IMPLICIT_PERMISSIONS` — the comparison `mint`'s callers check the
    real grant against, since GitHub's response never equals the bare request
    (observed against the real convivy-bosun App on 2026-10-18, every verb)."""
    return {**VERB_PERMISSIONS[verb], **IMPLICIT_PERMISSIONS}


class Unreadable(Exception):
    """A fact this call depends on could not be read, so the caller should refuse."""


class NotInstalled(Unreadable):
    """The App has no installation on this owner at all — GitHub's own
    confirmation (`installation_for_owner`'s org lookup AND its user-endpoint
    fallback both answered 404), not merely a lookup that failed to read.
    A subclass of `Unreadable` so every caller that does not distinguish the
    two (bin/agentctl, bin/ci-result-capture, bin/ci1-heartbeat, kb_audit.py)
    keeps behaving exactly as before; `bin/pr-state-capture` is the one
    caller that catches this ahead of `Unreadable` to treat a confirmed
    absence differently from a failed read (operator's decision, 2026-10-15
    — see that script's module docstring)."""


def key_problem(path: Path, *, label: str = "the App") -> str | None:
    """Why the App key cannot be used, or None. Refuses a key another user could
    read or that another user owns, as ssh refuses a loose private key."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return (f"no App key at {path}. Generate one for {label} on GitHub and "
                f"save it there with mode 600")
    except OSError as e:
        return f"cannot read {path}: {e}"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if st.st_uid != os.getuid():
        return f"{path} is owned by uid {st.st_uid}, not this user"
    if st.st_mode & 0o077:
        return f"{path} is mode {oct(st.st_mode & 0o777)}; it must be 600"
    return None


@dataclasses.dataclass(frozen=True)
class InstalledApp:
    """The install's own GitHub App, as `installed_app()` read it: a numeric id,
    a private key path, and a slug when GitHub returned one for the install to
    record. `slug` is `None` when `app-slug` is absent — connect-github's own
    check requires only `app-id` and `app.pem`, so a resolved App with no slug
    yet is not an error. `key_path` is `app.pem` resolved (`Path.resolve()`),
    so it names the real key file even when `app.pem` is a symlink."""

    app_id: str
    key_path: Path
    slug: str | None


def installed_app_dir() -> Path:
    """Where the install's own `create-github-app`/`connect-github` (Shipwright)
    write this App's id, key and slug: `app-id`, `app.pem` and `app-slug` under
    the real account home's `.config/convivy/github-app`. The real account
    home, never `$HOME`, so a dispatched run's sandboxed home cannot redirect
    it — the same posture every other trust-anchor path in this module's
    callers already takes (`builder_root.real_account_home`)."""
    return BR.real_account_home() / ".config" / "convivy" / "github-app"


def installed_app() -> InstalledApp:
    """The install's own GitHub App, read fresh from `installed_app_dir()` on
    every call — never cached, never overridden by an environment variable,
    and no App id literal anywhere in this module or its callers.

    Raises `Unreadable`, naming the path and that the install's
    `create-github-app` makes it, when `app-id` is missing, empty or
    unreadable, or when `app.pem` is missing. This does not check the key's
    permissions — a caller that mints a token still calls `key_problem` on
    the returned `key_path` itself (`mint()` already does), so a key with a
    bad mode is refused exactly as it was before this resolver existed.

    `key_path` is `app.pem` RESOLVED (`Path.resolve()`), not `app.pem`
    itself: on an install that keeps the App key under its older name,
    `app.pem` is a symlink to `bosun-app.pem` (the `DENIED_HOME_FILES`
    comment in `run_sandbox.py` names both for exactly this), and
    `key_problem`'s `lstat()` refuses a symlink outright regardless of what
    it points to or that target's own mode — resolving here, once, is what
    lets `key_problem` check the real key file's own regular-file, owner and
    mode (CQ·612, issue_01M69GD1WEN67MZPSBQZD02CDN). A dangling symlink is
    still caught above, by `is_file()` on the unresolved `app.pem`, which
    follows the link and answers False when its target is missing — this
    resolves only once that check has already passed."""
    directory = installed_app_dir()
    id_path = directory / "app-id"
    key_path = directory / "app.pem"
    howto = ("The install's create-github-app sets up this install's own "
             "GitHub App and writes this file; run it first.")
    try:
        app_id = id_path.read_text().strip()
    except OSError as e:
        raise Unreadable(f"no App id at {id_path} ({e}). {howto}") from e
    if not app_id:
        raise Unreadable(f"{id_path} is empty. {howto}")
    if not key_path.is_file():
        raise Unreadable(f"no App key at {key_path}. {howto}")
    key_path = key_path.resolve()
    try:
        slug = directory.joinpath("app-slug").read_text().strip() or None
    except OSError:
        slug = None
    return InstalledApp(app_id=app_id, key_path=key_path, slug=slug)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(key_path: Path, app_id: str, now: int | None = None) -> str:
    """A GitHub App JWT (RS256), signed by `openssl`. Valid for nine minutes and
    backdated one, GitHub's documented allowance for clock drift."""
    now = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id},
                                separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key_path)],
                       input=signing_input, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise Unreadable("openssl could not sign the App JWT: "
                         + r.stderr.decode("utf-8", "replace").strip())
    return f"{header}.{claims}.{_b64url(r.stdout)}"


def request(method: str, url: str, bearer: str, body: dict | None,
           opener=urllib.request.urlopen) -> tuple[int, dict]:
    """One GitHub call. Returns `(status, payload)` on a clean read, success
    or HTTP error alike; raises `ConnectionError` (an `OSError`, chained from
    the original) when GitHub answers but the body is truncated before it
    can be read in full (`http.client.IncompleteRead`, GitHub's own
    occurrence — see `bin/github-activity-capture`'s module docstring) — on
    either the success path's `resp.read()` or the error path's `e.read()`,
    since an `HTTPError`'s body is read the same way and can truncate the
    same way. A caller already treats a transport failure (`urllib.error.
    URLError`, a socket timeout) as `OSError`; this folds a truncated body
    into that same vocabulary rather than letting its own
    `http.client.HTTPException` reach a caller that is not watching for it."""
    req = urllib.request.Request(
        url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {bearer}",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as resp:
            try:
                raw = resp.read()
            except http.client.HTTPException as e:
                raise ConnectionError(f"{url}: the response body was truncated "
                                     f"before it could be read ({e})") from e
            return resp.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except http.client.HTTPException as read_err:
            raise ConnectionError(f"{url}: the error response body was truncated "
                                 f"before it could be read ({read_err})") from read_err
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload


def graphql(query: str, variables: dict, bearer: str,
           *, opener=urllib.request.urlopen) -> dict:
    """One GitHub GraphQL call (`POST /graphql`), through `request`'s own
    transport — same bearer, same timeout, same truncated-body handling.
    Returns the response's `data` object on a clean 200 with no top-level
    `errors` array; raises `Unreadable` on a non-200, on an `errors` array
    (GitHub's own partial-failure signal for a GraphQL call — a field it
    could not resolve, distinct from the transport failure `request` already
    raises as `ConnectionError`), or on a response carrying no `data` object
    at all. First caller: `bin/pr-state-capture`'s open-PR `mergeable`/
    `reviewDecision` read (CQ·636) — see that script for why those two
    fields have no REST equivalent efficient enough to read per repo in one
    call."""
    code, payload = request("POST", f"{API}/graphql", bearer,
                            {"query": query, "variables": variables}, opener=opener)
    if code != 200:
        raise Unreadable(f"GraphQL call failed ({code}): {payload!r:.200}")
    if isinstance(payload, dict) and payload.get("errors"):
        raise Unreadable(f"GraphQL call returned errors: {payload['errors']!r:.300}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise Unreadable(f"GraphQL call returned no data: {payload!r:.200}")
    return data


def installations(jwt: str, opener=urllib.request.urlopen) -> list[dict]:
    """Every installation of the App making this JWT's claim."""
    code, payload = request("GET", f"{API}/app/installations?per_page=100", jwt, None,
                            opener=opener)
    if code != 200 or not isinstance(payload, list):
        raise Unreadable(f"the App's installations could not be read ({code})")
    return payload


def single_installation(jwt: str, owner: str, opener=urllib.request.urlopen) -> dict:
    """The App's one installation, owned by `owner` — refused by name ("count",
    "owner") so a widened or moved installation is seen for what it is rather
    than read as a token error."""
    payload = installations(jwt, opener=opener)
    if len(payload) != 1:
        raise Unreadable(f"count: the App has {len(payload)} installations, not exactly one")
    inst = payload[0]
    got_owner = ((inst.get("account") or {}).get("login")) or "?"
    if got_owner != owner:
        raise Unreadable(f"owner: the installation belongs to {got_owner!r}, not {owner!r}")
    return inst


def installation_for_owner(jwt: str, owner: str, *, label: str = "the App",
                           opener=urllib.request.urlopen) -> dict:
    """The App's installation on `owner` directly, by GitHub's per-owner lookup
    (`GET /orgs/{owner}/installation`, falling back to `GET /users/{owner}/installation`
    on a 404 — an owner is one or the other) — the right call for an App that may be
    installed on more than one owner, where `single_installation`'s "exactly one
    installation total" count would refuse every owner the moment a second one exists.
    Raises `NotInstalled` naming `owner` and `label` only when BOTH lookups answer
    404 — GitHub's own confirmation there is no installation. Any other status on
    either lookup is a plain `Unreadable` naming the code, since that is a failed
    read, not a confirmed absence, and must not be mistaken for one."""
    code, payload = request("GET", f"{API}/orgs/{owner}/installation", jwt, None, opener=opener)
    if code == 200:
        return payload
    if code != 404:
        raise Unreadable(f"{label}'s installation on {owner!r} could not be read ({code})")
    code, payload = request("GET", f"{API}/users/{owner}/installation", jwt, None, opener=opener)
    if code == 200:
        return payload
    if code != 404:
        raise Unreadable(f"{label}'s installation on {owner!r} could not be read ({code})")
    raise NotInstalled(f"{label} is not installed on {owner!r}")


def installation_token_payload(jwt: str, installation_id: str, *,
                               repositories: list[str] | None = None,
                               permissions: dict[str, str] | None = None,
                               opener=urllib.request.urlopen) -> dict:
    """GitHub's own token-creation response — `token`, `expires_at`,
    `permissions` and `repository_selection` AS GITHUB GRANTED THEM, which can
    differ from what was asked for. `installation_token` (below) is the thin
    "just the token string" wrapper every caller before this one uses;
    `mint` (further below) is the only caller that needs the rest of the
    payload, to log what was actually granted rather than what was asked for."""
    body = None
    if repositories is not None or permissions is not None:
        body = {}
        if repositories is not None:
            body["repositories"] = repositories
        if permissions is not None:
            body["permissions"] = permissions
    code, payload = request("POST", f"{API}/app/installations/{installation_id}/access_tokens",
                            jwt, body, opener=opener)
    if code != 201 or not payload.get("token"):
        raise Unreadable(f"GitHub refused an installation token ({code}): "
                         f"{payload.get('message', 'no message')}")
    return payload


def installation_token(jwt: str, installation_id: str, *, repositories: list[str] | None = None,
                       permissions: dict[str, str] | None = None,
                       opener=urllib.request.urlopen) -> str:
    """A token for one installation, expiring within the hour. Unscoped (reaches
    every repository and permission the installation holds) unless `repositories`
    and/or `permissions` narrow it — a caller that only ever needs to read should
    pass `permissions` naming just the reads it makes, so a bug that tried to
    write would be refused by GitHub even if it slipped past review."""
    return installation_token_payload(jwt, installation_id, repositories=repositories,
                                      permissions=permissions, opener=opener)["token"]


def revoke_installation_token(token: str, opener=urllib.request.urlopen) -> bool:
    """Revoke the installation token that authenticates this call, so it cannot be
    used again before its hour is out. True when GitHub answered 204.

    Best effort: a token that stays live is in this process's memory alone and
    expires within the hour, so a failure here is no reason to refuse the work
    the token was minted for."""
    try:
        code, _ = request("DELETE", f"{API}/installation/token", token, None, opener=opener)
    except OSError:
        return False
    return code == 204


# --- the mint log -------------------------------------------------------------
#
# Every convivy-bosun token this module mints appends one line here: never the
# token itself, never a prefix or hash of it, only what GitHub reports it
# granted. Beside agentctl's other per-component state under
# ~/.local/state/agent-platform (`pr-state-capture`, `ci-result-capture` and
# `github_activity.py` each keep their own `STATE_DIR` there the same way),
# directory 0700, file 0600, created if missing. Both modes are corrected
# with an explicit chmod/fchmod in `_append_mint_log` below, since `mkdir`'s
# and `os.open`'s mode arguments are masked by the umask and are no-ops on a
# directory or file that already existed (round 1 of #927) — the same fix
# those siblings apply to their own `STATE_DIR`.


def mint_log_path() -> Path:
    """Where every mint (and, for a revoking mint, every revoke) is recorded.
    Computed fresh on every call, never cached at import time, so a test can
    redirect it either by monkeypatching this function directly or by setting
    `$HOME` before calling (this reads `Path.home()`, not the passwd-database
    `builder_root.real_account_home()` the App key path itself in
    `bin/agentctl` uses, so a stub HOME is enough on its own)."""
    return Path.home() / ".local" / "state" / "agent-platform" / "github-mint" / "mint-log.jsonl"


def _mint_caller() -> str:
    """Who asked for this token: `AGENTCTL_RUN_ID` when this process is a
    dispatched run, else the tmux session name a `co` crew runs inside
    (`crew_key.tmux_session_name`, "the co session name the environment
    gives" — it reads `$TMUX` and asks tmux directly, never a value this
    process could spoof by setting an env var of its own), else this
    process's own `argv[0]`."""
    run_id = os.environ.get("AGENTCTL_RUN_ID")
    if run_id:
        return run_id
    session = CK.tmux_session_name()
    if session:
        return session
    return sys.argv[0] if sys.argv else "?"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_mint_log(entry: dict) -> None:
    path = mint_log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # mkdir's mode is a no-op on an already-existing dir
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)  # same no-op gap as above, for an already-existing file
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def mint(repo_slug: str, verb: str, *, key_path: Path, app_id: str,
         label: str = "the install's GitHub App",
         opener=urllib.request.urlopen) -> dict:
    """The one path every verb's token mints through (E10 S1,
    platform/specs/github-credential-seam): refuses an unknown verb before any
    network call, reads the App key, mints a token scoped to `repo_slug`
    ("owner/repo") and `verb`'s permissions (`VERB_PERMISSIONS`), and appends
    one line to the mint log (`mint_log_path`) recording what GitHub actually
    granted — never the token itself.

    The permissions requested are `VERB_PERMISSIONS[verb]` with one exception:
    on `publish`, `workflows` is left out of the request when the installation
    object (already read, to get its id) does not carry `workflows` in its own
    `permissions`. GitHub refuses the WHOLE token with 422 when a request names
    a key the installation does not hold, so an adopter App without `workflows`
    would otherwise fail every publish; this is the one key this module asks
    for conditionally, not a general intersection against the installation.

    Returns GitHub's own token-creation payload (`installation_token_payload`),
    not just the token string, so a caller that logs the grant reads it off
    the response rather than the request.

    If the log write fails, this attempts to revoke the just-minted token and
    raises `Unreadable` naming the log path, chained from the original error —
    never the bare `OSError` the log write itself raised, since every caller
    of a mint (`bin/agentctl`'s `_mint_run_github_token` today) already
    catches `Unreadable` for every other refusal and would otherwise crash
    uncaught on this one. The raised message states the revoke's own outcome
    rather than assuming it: whether the token was revoked, or, when the
    revoke itself failed, that it was not and remains live until the expiry
    GitHub returned in the mint response — the revoke's return value is read,
    never assumed, since a failed revoke left unreported would be a live
    token nobody downstream knows to treat as live. An unlogged token is as
    bad as an unrevoked one, and this is the one place every verb's mint
    passes through, so it is the one place that guarantee needs to live
    rather than one per caller."""
    if verb not in VERB_PERMISSIONS:
        raise Unreadable(f"verb: {verb!r} names no permissions table "
                         f"(have: {', '.join(sorted(VERB_PERMISSIONS))})")
    if verb == "rehearsal":
        rehearsal_owner, _, rehearsal_repo = repo_slug.partition("/")
        try:
            rehearsal_owners = operator_config.get_optional("rehearsal_repository_owners")
        except operator_config.OperatorConfigError:
            rehearsal_owners = None
        # `isinstance(..., list)` is the only shape check this needs: exact
        # string equality below never matches a non-string entry, and `in`
        # on a list never does the substring/key match a bare string or dict
        # would, so a malformed value (a string, a non-list, a list holding
        # a non-string entry) simply matches nothing rather than crashing.
        if (
            rehearsal_repo != REHEARSAL_REPOSITORY_NAME
            or not isinstance(rehearsal_owners, list)
            or rehearsal_owner not in rehearsal_owners
        ):
            raise Unreadable(
                f"rehearsal: refused for {repo_slug!r} — this verb mints only for "
                f"<owner>/{REHEARSAL_REPOSITORY_NAME} with owner exactly equal to "
                f"one entry of this install's own rehearsal_repository_owners")
    problem = key_problem(key_path, label=label)
    if problem:
        raise Unreadable(problem)
    owner, _, repo_name = repo_slug.partition("/")
    jwt = app_jwt(key_path, app_id)
    inst = installation_for_owner(jwt, owner, label=label, opener=opener)
    inst_id = str(inst.get("id") or "")
    if not inst_id:
        raise Unreadable("the installation carries no id")
    permissions = VERB_PERMISSIONS[verb]
    # `workflows` is the one key this table asks for conditionally: publish
    # drops it from the request unless the installation object already read
    # above carries it (`inst["permissions"]`, from `GET /{orgs,users}/{owner}/
    # installation` — no extra GitHub call). Every other key is still
    # requested and refused loudly by GitHub when the installation lacks it;
    # this is the one optional key on the one verb, not a general intersection.
    if verb == "publish" and "workflows" not in (inst.get("permissions") or {}):
        permissions = {k: v for k, v in permissions.items() if k != "workflows"}
    payload = installation_token_payload(
        jwt, inst_id, repositories=[repo_name],
        permissions=permissions, opener=opener)
    try:
        _append_mint_log({
            "time": _utc_now(),
            "verb": verb,
            "repo": repo_slug,
            "permissions": payload.get("permissions") or {},
            "repository_selection": payload.get("repository_selection"),
            "caller": _mint_caller(),
            "expires_at": payload.get("expires_at"),
        })
    except OSError as e:
        revoked = revoke_installation_token(payload["token"], opener=opener)
        if revoked:
            revoke_note = "the just-minted token was revoked"
        else:
            revoke_note = ("the just-minted token was not revoked and remains live "
                           f"until it expires at {payload.get('expires_at')}")
        raise Unreadable(f"the mint log at {mint_log_path()} could not be "
                         f"written ({e}); {revoke_note}") from e
    return payload


@contextmanager
def mint_token(repo_slug: str, verb: str, *, key_path: Path, app_id: str,
               label: str = "the install's GitHub App",
               opener=urllib.request.urlopen):
    """`mint()`, yielding the token, and revoking it when the block exits —
    including when it raises. For a caller whose need for the token ends with
    the call or calls it makes (publish, pr-open, verdict, pr-comment,
    pr-close, pr-reopen, release, and `tag-create`'s own two writes under
    this one mint); a caller
    whose token must outlive a single call (the per-run read token
    `bin/agentctl` hands a dispatched run) calls `mint()` directly instead and
    never revokes.

    The revoke's own outcome is a second mint-log line, best effort: a
    failure to write THAT line is not reason to raise, since by then the
    token is already revoked (or, worst case, left to expire within the hour
    on its own) — unlike a failure to log the mint itself, which `mint()`
    treats as reason to revoke and raise."""
    payload = mint(repo_slug, verb, key_path=key_path, app_id=app_id, label=label, opener=opener)
    token = payload["token"]
    try:
        yield token
    finally:
        revoked = revoke_installation_token(token, opener=opener)
        try:
            _append_mint_log({
                "time": _utc_now(),
                "verb": verb,
                "repo": repo_slug,
                "caller": _mint_caller(),
                "revoked": revoked,
            })
        except OSError:
            pass


@contextmanager
def gh_env(token: str, base: dict | None = None):
    """The environment for one `gh` invocation as the convivy-bosun App (E10 S3,
    platform/specs/github-credential-seam), modelled on `kb_merge.gh_env`:
    `GH_TOKEN` outranks `GITHUB_TOKEN` and the stored login in
    `~/.config/gh/hosts.yml`, so with it set `gh` never reads that file for a
    credential, and prompts are disabled so a rejected token fails rather than
    asking for another.

    UNLIKE `kb_merge.gh_env`, this also points `GH_CONFIG_DIR` at a fresh, empty
    directory, created on entry and removed on exit. `kb_merge`'s App runs as
    `agents`, a separate account with no `gh` login of its own (ARCH-346), so
    there is no `hosts.yml` for it to read or write. `pr-open` and `pr-ready`
    run as the operator's own account, which does have one, so without this
    `gh` would fall back to reading it the moment `GH_TOKEN` were ever absent,
    and could write a new entry into it on an unexpected prompt. An empty,
    throwaway `GH_CONFIG_DIR` makes both impossible rather than merely unlikely."""
    env = dict(os.environ if base is None else base)
    env["GH_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    config_dir = tempfile.mkdtemp(prefix="agentctl-gh-config-")
    env["GH_CONFIG_DIR"] = config_dir
    try:
        yield env
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


# --- read-only `gh` callers (company/decisions/0027, moving the launchd
# scorecards and harvesters off the operator's own `gh` login) -------------
#
# `mint_token` + `gh_env` composed for the one verb every read-only harvester
# in bin/ needs: `read_token_env` for a caller that touches one repository at
# a time and can scope the token to exactly the calls it makes for that
# repository; `read_token_pool` for a caller whose own repo-by-repo work is
# interleaved (a shared per-run cache keyed some other way, revisited out of
# repository order) and would otherwise re-mint the same repository many
# times over one pass.


@contextmanager
def read_token_env(repo_slug: str, *, label: str = "the install's GitHub App",
                   opener=urllib.request.urlopen):
    """One `read`-scoped token for `repo_slug`, built into the environment one
    `gh` subprocess (or a short contiguous run of them, all against this one
    repository) runs in; minted on entry, revoked on exit. Raises `Unreadable`
    (via `installed_app`/`mint_token`) before any `gh` process ever runs,
    which is what lets a caller's own existing gh-failure path also be a mint-
    failure path: catch `Unreadable` around the `with` statement and take
    exactly the branch a failed `gh` call already takes there."""
    app = installed_app()
    with mint_token(repo_slug, "read", key_path=app.key_path, app_id=app.app_id,
                    label=label, opener=opener) as token:
        with gh_env(token) as env:
            yield env


@contextmanager
def read_token_pool(*, label: str = "the install's GitHub App",
                    opener=urllib.request.urlopen):
    """Yields `env_for(repo_slug)`: a `read`-scoped environment minted at most
    once per repository for as long as the pool stays open, however many
    times `env_for` is called for it and in whatever order repositories are
    first seen. Every token the pool minted is revoked when the pool itself
    closes (`ExitStack`, unwound in reverse mint order).

    For a caller like `dependabot-review` or `security-scorecard`, whose own
    loop finishes one repository's reads before starting the next,
    `read_token_env` revokes sooner and is the better fit. This is for a
    caller (a scorecard's cross-repo ledger join, a corpus builder's
    callback) that cannot cheaply restructure its own loop to be contiguous
    per repository, where opening and closing `read_token_env` around each
    call would mint the same repository over and over in one pass.

    `env_for` raises `Unreadable` on the repository's first call only (the
    same failure `read_token_env`'s own `with` statement would raise) and
    caches nothing for a repository whose mint failed, so a caller that
    catches it, logs its own existing gh-failure path, and moves on to the
    next repository will mint fresh on any later call for that same
    repository rather than replaying the recorded failure."""
    stack = ExitStack()
    envs: dict[str, dict] = {}

    def env_for(repo_slug: str) -> dict:
        if repo_slug not in envs:
            envs[repo_slug] = stack.enter_context(
                read_token_env(repo_slug, label=label, opener=opener))
        return envs[repo_slug]

    try:
        yield env_for
    finally:
        stack.close()


# --- pushing as the App (E10 S2, platform/specs/github-credential-seam) -----
#
# Modelled on `kb_merge.push_env`/`kb_merge.scrub` (the Library's own pattern for
# pushing to `convivy/knowledge` as `convivy-library`), kept here rather than
# imported from there since `kb_merge.py` is the Library's module and this one is
# the Bosun's, and a caller naming a repository per call (every verb in
# `VERB_PERMISSIONS` but the Library's single configured one) needs a `url` it
# supplies rather than the Library's own fixed `push_url()`.


def push_env(token: str, url: str, *, base: dict | None = None) -> dict:
    """The environment for one `git` invocation that pushes or fetches `url` as
    the App.

    **WHERE THE TOKEN LANDS.** It travels in `GIT_CONFIG_*` environment variables,
    which set an `http.<url>.extraHeader` for that one process. It is not in argv,
    not in the URL, and not in any file: `GIT_CONFIG_*` never writes `.git/config`,
    and nothing is `git remote set-url`ed. The header is scoped to `url`, so git
    sends it nowhere else.

    **COMPOSES WITH `base`'S OWN `GIT_CONFIG_*` PAIRS.** `base` may already carry
    a `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` set of its own
    (a dispatched run's own object-store redirect, for one); this appends its two
    pairs after whatever count `base` already declares, rather than starting back
    at 0 and overwriting an index `base` was using.

    **THE HTTPS CREDENTIAL HELPER IS CLOSED OFF, NOT MERELY UNUSED.** An empty
    `credential.helper` resets every configured helper (the last entry wins, and
    this one is last), so a rejected token fails rather than falling back to the
    operator's `gh auth git-credential` or the Keychain. Askpass is emptied and
    prompts disabled, `GIT_ALLOW_PROTOCOL` limits the invocation to `url`'s own
    scheme, and every inherited `GIT_TRACE*` is removed — a trace would otherwise
    print the header, token included, to this process's stderr or a configured
    trace file."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.startswith("GIT_TRACE")]:
        del env[key]
    try:
        existing_count = int(env.get("GIT_CONFIG_COUNT") or "0")
    except ValueError:
        existing_count = 0
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    pairs = [("credential.helper", ""),
             (f"http.{url}.extraHeader", f"Authorization: Basic {basic}")]
    for offset, (key, value) in enumerate(pairs):
        idx = existing_count + offset
        env[f"GIT_CONFIG_KEY_{idx}"] = key
        env[f"GIT_CONFIG_VALUE_{idx}"] = value
    env["GIT_CONFIG_COUNT"] = str(existing_count + len(pairs))
    env["GIT_ALLOW_PROTOCOL"] = urllib.parse.urlparse(url).scheme
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


def scrub(text: str, token: str) -> str:
    """`text` with the token, and its Basic-auth encoding, removed.

    The token is never in argv or a URL, so git has no reason to print it. This
    is the backstop for a caller that prints git's stderr on a failed push, and
    it holds whatever a git trace setting inherited from the environment would
    otherwise have emitted."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    for secret in (basic, token):
        if secret:
            text = text.replace(secret, "***")
    return text
