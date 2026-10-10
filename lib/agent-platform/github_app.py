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
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"

# Never the bare "openssl" — see the module docstring's "RUNS /usr/bin/openssl
# BY ABSOLUTE PATH" note (round 3 of #732, finding 2).
OPENSSL = "/usr/bin/openssl"


class Unreadable(Exception):
    """A fact this call depends on could not be read, so the caller should refuse."""


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
    req = urllib.request.Request(
        url, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {bearer}",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload


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


def installation_token(jwt: str, installation_id: str, *, repositories: list[str] | None = None,
                       permissions: dict[str, str] | None = None,
                       opener=urllib.request.urlopen) -> str:
    """A token for one installation, expiring within the hour. Unscoped (reaches
    every repository and permission the installation holds) unless `repositories`
    and/or `permissions` narrow it — a caller that only ever needs to read should
    pass `permissions` naming just the reads it makes, so a bug that tried to
    write would be refused by GitHub even if it slipped past review."""
    body = None
    if repositories is not None or permissions is not None:
        body = {}
        if repositories is not None:
            body["repositories"] = repositories
        if permissions is not None:
            body["permissions"] = permissions
    code, payload = request("POST", f"{API}/app/installations/{installation_id}/access_tokens",
                            jwt, body, opener=opener)
    token = payload.get("token") if code == 201 else None
    if not token:
        raise Unreadable(f"GitHub refused an installation token ({code}): "
                         f"{payload.get('message', 'no message')}")
    return token


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
