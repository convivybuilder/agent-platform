"""Which service holds the vaults, and the one place that decides.

**THE FAILURE THIS EXISTS TO PREVENT IS SILENT** (the cutover sequence, Stage B
step 7). At the write cutover the Chart House, the Archives and the decision
plane move out of Orient's control database into the Library's own file, and
Orient's copy of those tables survives until Stage C — days later. So a client
still posting to Orient in between gets a `200`, writes to a database nobody
reads any more, and reports success. Nothing anywhere says otherwise.

**Which means the client list is the whole safety property, and a list nobody
can check is the wrong artifact.** The specification named seven files; a survey
on 2026-08-16 found twelve, because five of them reach the base URL by importing
another module's constant rather than reading the environment, so grepping for
`ORIENT_BASE_URL` undercounts. That is the shape of the mistake this module
removes: every client asks here, and `test_vault_plane.py` asserts that nothing
outside this file builds a control-plane URL. The count becomes a check.

**The two planes are not the same shape**, which is why pointing `ORIENT_BASE_URL`
at port 8100 does not work and why step 7 is code rather than configuration:

| | Orient | the Library |
|---|---|---|
| prefix | `/api/…` | none |
| work items | `issues` | `items` |
| version | — | a required `Library-Protocol` header |

**Call sites keep writing Orient's path shape**, which is what every one of them
is written in today, and this translates. That keeps the change to each client to
one import and one call, so the diff a reviewer reads is the routing rather than
thirty rewritten string literals.

**The plane is chosen by `VAULTS_ON_LIBRARY`, and unset means the LIBRARY.** It
meant Orient until 2026-08-18, so that adopting this module changed nothing before
the window; the window passed and that default became the failure mode. The switch
reached 8 of 83 launchd jobs, no login shell and no git hook, so everything else
went on writing to the superseded copy and going on getting a `200`. `0` is the
explicit opt-out and is what a rollback sets; any value that is neither `0` nor `1`
raises rather than being guessed at.

**A switch distinct from the address, because Orient already has the address.**
`convivy-orient/library_client.py` reads `LIBRARY_BASE_URL` with a default of
`http://127.0.0.1:8100` — it is Orient's read client of the Library and it has
been pointed there since P5. Reusing that name as the cutover switch would give
one variable two meanings, so setting it to move the Library to another port
would silently move every capture job's writes with it, days early. Where the
Library IS and whether the vaults are THERE are different questions.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

# Orient's control plane. Reached only on the rollback, `VAULTS_ON_LIBRARY=0`
# — the cutover moved the default to the Library, below.
ORIENT_BASE_URL = os.environ.get("ORIENT_BASE_URL", "http://127.0.0.1:8099")

# Where the Library is. The same variable and the same default Orient's
# `library_client` has used since the read rewire, so one address answers for
# both and neither has to be told about the other.
# The same bar Orient's `library_client._resolve_base_url` holds ITS read path
# to, applied here to the write path (reviewer, convivy/convivy-orient#175). Both
# read this variable and both attach a bearer token, so without it, pointing the
# address at a remote plain-http host would make Orient's read client refuse to
# import while every write client here sent the token in the clear — one address
# at two safety bars.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _refuse_cleartext(raw: str) -> str:
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS and not raw.startswith("https://"):
        raise RuntimeError(
            f"LIBRARY_BASE_URL={raw!r} is neither loopback nor https — refusing "
            "rather than sending the bearer token in cleartext over the network. "
            "Use an https:// URL for a remote Library, or leave LIBRARY_BASE_URL "
            "unset to use the local default.")
    return raw


LIBRARY_BASE_URL = _refuse_cleartext(
    os.environ.get("LIBRARY_BASE_URL", "http://127.0.0.1:8100").strip())

# Whether the vaults are THERE, which is the cutover itself. UNSET NOW MEANS THE
# LIBRARY, and the inversion is the point: this defaulted to Orient so the module
# could land before the window changing nothing (ADR-0016), which was right until
# the window passed and became the failure mode the moment it did.
#
# THE CUTOVER RAN AND THE DEFAULT DID NOT FOLLOW IT. `VAULTS_ON_LIBRARY=1` reached
# 8 of 83 installed launchd jobs, no login shell, and no git hook — so every other
# writer kept posting into Orient's superseded copy and kept getting a `200`.
# Measured 2026-08-18: 9 work items and 8 signals existed only in Orient's copy,
# two of them filed minutes before the measurement. Seven of the nine came from
# `check-primary-checkouts`, which routes through this module correctly and simply
# runs from a git hook with nothing to export the variable.
#
# A default meaning "the store we migrated away from" needs 75 more correct edits
# to be safe and one omission to be wrong, and it silently captures every job and
# session created after the cutover. Defaulting to the Library makes the safe path
# the one you get by doing nothing — the property the original default had, for the
# window in which Orient was the safe answer.
#
# The parse is a function so it can be TESTED. As a bare expression its only
# observable was a module constant fixed at import, so the sole test naming it
# asserted through a monkeypatched attribute and passed whatever the parse did —
# which is why an inverted default could not have been caught by the suite that
# existed. A function takes the environment as an argument and has no import-time
# state to reload around.
#
# EXACTLY `0` AND `1` ARE RECOGNISED; UNSET MEANS THE LIBRARY AND ANYTHING ELSE
# RAISES. Two weaker rules were considered and both fail silently in one direction
# (Orient co, convivy-orient#176, whose reasoning this follows):
#
#   * `== "1"`, so every other spelling means Orient. `VAULTS_ON_LIBRARY=true` is a
#     plausible thing to write and would go straight back to the store nobody reads.
#     `security-reviewer` on PR #406 found the sharper case: a templated deploy that
#     emits an empty value instead of omitting the key reverts that job to the
#     superseded store with nothing to say so.
#   * An accept-set of falsey words, so `0`, `false`, `no` and `off` opt out. This
#     narrows the silent-rollback path without closing it, and **a list that teaches
#     a generalization it does not honour is worse than no list**: an operator who
#     learns that natural falsey words work writes `disabled` or `n`, and that one
#     silently means the Library against a mental model now confidently wrong.
#
# Raising is the only rule that fails loudly in BOTH directions. `true` cannot
# silently mean Orient; `false` cannot silently mean the Library. An unknown value
# means the operator's intent is unknown, and guessing an intent routes writes into
# a store nobody reads — so the operator learns at the moment of the mistake rather
# than from a store that quietly stopped growing.
#
# It is also what this module already does one constant up: `_refuse_cleartext`
# raises at import on a misconfigured `LIBRARY_BASE_URL` for the same reason. The
# plane switch has the same character and should not be the one setting that
# guesses.
#
# THE COST, STATED RATHER THAN BURIED: a bad value breaks the importer instead of
# misrouting it, and this is imported at module level, so the failure is an uncaught
# traceback at process start rather than a bad request later.
#
# THE BLAST RADIUS IS ONE JOB, NOT THE FLEET, which is narrower than an earlier draft
# of this comment claimed. There is no shared source for the variable: each launchd
# plist sets it independently, `launchctl getenv` returns nothing, and no shell
# startup file exports it — so a mistyped value takes down the one job that carries
# it (security-reviewer, PR #406, checked rather than reasoned).
#
# And it refuses nothing that exists today: across 83 installed launchd jobs the
# variable is `1` on 8 and absent on the other 75, with no shell startup file and no
# git hook setting it. Verified 2026-08-18 by reading the VALUES rather than counting
# which files set the key, independently on both sides of this pair.
#
# KEPT IN LOCKSTEP WITH `convivy-orient/vault_plane.py` (convivy-orient#176), a
# deliberate copy rather than a shared import. If only one side inverts, or the two
# read the same variable differently, the halves disagree about which store is
# authoritative and the split this fixes simply moves.
_ON, _OFF = "1", "0"


def _plane_from_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process should talk to the Library.

    Unset means yes. `1` means yes and `0` means no. Every other value raises,
    including an empty one, because an intent nobody can read is not a default.
    """
    src = os.environ if env is None else env
    raw = src.get("VAULTS_ON_LIBRARY")
    if raw is None:
        return True
    value = raw.strip()
    if value == _ON:
        return True
    if value == _OFF:
        return False
    raise RuntimeError(
        f"VAULTS_ON_LIBRARY={raw!r} is neither {_ON!r} (the Library) nor {_OFF!r} "
        f"(Orient, the rollback). Refusing to guess which store to write to: an "
        f"unrecognised value would otherwise route writes silently, and Orient's "
        f"copy of the vaults is superseded. Unset the variable for the default.")


VAULTS_ON_LIBRARY = _plane_from_env()

# The protocol version this client speaks. The Library refuses a request that
# declares none, so a client that reached it without this header would fail on
# every call rather than on the interesting ones.
PROTOCOL_HEADER = "Library-Protocol"
PROTOCOL_VERSION = "1"

# Orient's noun for a work item, and the Library's. The rename is the only part
# of the mapping that is not mechanical, and it is the part a hand-edited client
# would get wrong: `/api/issues` looks like it should become `/issues`, which the
# Library does not serve at all.
_ORIENT_ITEMS = "/api/issues"
_LIBRARY_ITEMS = "/items"


def on_library() -> bool:
    """Whether this process talks to the Library rather than to Orient."""
    return VAULTS_ON_LIBRARY


def base_url() -> str:
    return LIBRARY_BASE_URL if on_library() else ORIENT_BASE_URL


def translate(path: str) -> str:
    """One of Orient's API paths in the shape the configured plane serves it.

    Against Orient the path is returned untouched. That made adopting this module
    a no-op while Orient was the default; since the inversion the untouched path is
    the OPT-OUT branch, reached only on `VAULTS_ON_LIBRARY=0`. Not on `false` or
    `off`: an accept-set of falsey words was considered and rejected below, and
    those spellings raise rather than arriving here.

    A query string is carried across without being touched: only the path is
    rewritten, so `/api/issues?state=open` keeps its filters.

    The cut is at the first `?` OR `#`. No call site can produce a fragment today
    — every caller-influenced id is `quote(x, safe="")`-escaped before it arrives
    — so this closes a gap nothing reaches rather than one anything exploits
    (security-reviewer, convivy/agent-platform#400). It costs one line, and the
    failure it would otherwise allow is a path that skips the `issues` → `items`
    rewrite and 404s on a route that does not exist.
    """
    if not on_library():
        return path
    cut = re.search(r"[?#]", path)
    head, sep, query = (path[:cut.start()], path[cut.start()], path[cut.start() + 1:]) \
        if cut else (path, "", "")
    if head == _ORIENT_ITEMS or head.startswith(_ORIENT_ITEMS + "/"):
        head = _LIBRARY_ITEMS + head[len(_ORIENT_ITEMS):]
    elif head.startswith("/api/"):
        head = head[len("/api"):]
    return head + sep + query


def url(path: str) -> str:
    """A full URL for one of Orient's API paths, on whichever plane is live."""
    return f"{base_url()}{translate(path)}"


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to carry the bearer token downhill.

    **`_refuse_cleartext` checks the CONFIGURED address once, at import, and a
    redirect is how that check gets bypassed at run time** (security-reviewer,
    convivy/agent-platform#400). `urlopen`'s default opener follows 301, 302,
    303, 307 and 308, and `HTTPRedirectHandler.redirect_request` copies every
    header except `Content-Length` and `Content-Type` onto the new request, with
    no scheme or host check. So an `https://` Library that answers `Location:
    http://anywhere` gets `Authorization: Bearer <token>` resent in the clear —
    the precise thing the import-time check exists to prevent, defeated by the
    origin it was told to trust.

    Reaching this needs control of the Library origin or whatever fronts it,
    rather than a passive network position. That is the actor the guard names, so
    the guard has to hold against it — and against the same actor sending the
    token somewhere ENCRYPTED that it does not belong. Refusing only cleartext
    left `Location: https://attacker.example.com` carrying
    `Authorization: Bearer <token>` intact, which is exfiltration with the
    transport doing its job (security-reviewer, round 3). So the host has to
    match as well as the scheme.

    **The rule is that a vault request stays where it was sent, and "where" means
    the ORIGIN — scheme, host and port.** A same-origin redirect is ordinary (a
    trailing slash, a path move) and is followed; anything else is refused rather
    than stripped of its token, because a Library that answers somewhere else is a
    configuration change rather than a request to follow, and a silently
    de-authenticated retry would come back 401 and send its operator to the wrong
    layer.

    **Comparing the host alone was not enough, and loopback is why**
    (security-reviewer, round 4). `LIBRARY_BASE_URL` defaults to
    `http://127.0.0.1:8100` and the deployment runs several services side by side
    on loopback ports, so a host-only check follows `Location:
    http://127.0.0.1:9999` and hands the bearer token to whatever is listening
    there. That needs a second bug — an open redirect in the Library — to reach,
    but the docstring claimed an origin guarantee the code did not enforce, which
    is the part that had to be fixed either way.

    The refusal is a `HTTPError` rather than a silent stop, because a client that
    quietly received no answer would report the plane unreachable and send its
    operator to the wrong layer.
    """

    # A URL's origin, in the sense RFC 6454 means it: scheme, host AND port. A
    # missing port is the scheme's default, so `https://host` and
    # `https://host:443` are one origin rather than two.
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    @classmethod
    def _origin(cls, url: str) -> tuple:
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        return (scheme, (parsed.hostname or "").lower(),
                parsed.port or cls._DEFAULT_PORTS.get(scheme))

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        host = (target.hostname or "").lower()
        if host not in _LOOPBACK_HOSTS and not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing a redirect from {req.full_url} to {newurl}: it is "
                f"neither loopback nor https, and following it would resend the "
                f"bearer token in cleartext", headers, fp)
        if self._origin(newurl) != self._origin(req.full_url):
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing a redirect from {req.full_url} to {newurl}: it "
                f"leaves the origin the request was sent to, and following it "
                f"would hand the bearer token to {host or 'another host'}",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# **INSTALLED AS THE PROCESS DEFAULT, rather than handed to callers as a wrapper
# they each have to remember.** Fourteen call sites across seven files call
# `urllib.request.urlopen`, and a control that depends on all fourteen adopting a
# new name is a control the fifteenth will miss — which is the same failure this
# whole module exists to remove, one layer down.
#
# It is also the seam the test suite already owns: `tests/conftest.py` refuses a
# live-plane call by patching `urllib.request.urlopen`, and every client test
# stubs the same name. A wrapper would have moved the seam out from under all of
# them, which is how the first version of this arrived at 96 failing tests.
#
# Process-wide is broader than the vault, and that is acceptable in one
# direction only: this handler can refuse a redirect and can never permit one the
# stdlib would have refused. Nothing else in the process loses an option.
urllib.request.install_opener(urllib.request.build_opener(_NoDowngrade()))


def library_url(path: str) -> str:
    """A URL on the Library itself, whichever plane the VAULTS are on.

    For a Library route that has no Orient counterpart (`/bundles`, which
    `bin/knowledge-ctx` calls to render a dispatched agent's documents).
    `url()` answers "where do the vaults live", and after a rollback to
    Orient it would send a `/bundles` request to a service that does not
    serve it. Living here keeps the address in the one module the sweep in
    `tests/test_vault_plane.py` lets decide it, and inherits `_refuse_cleartext`
    and the redirect guard installed below."""
    return f"{LIBRARY_BASE_URL}{path}"


# The Library's shipped address, as a literal that reads NO environment variable.
DEFAULT_LIBRARY_URL = "http://127.0.0.1:8100"


def fixed_library_url(path: str) -> str:
    """A URL on the Library's default address, ignoring `LIBRARY_BASE_URL`,
    `VAULTS_ON_LIBRARY` and `ORIENT_BASE_URL`.

    For a read whose RESULT is trusted to authorise something, so its source
    must be one the calling process cannot repoint by setting an environment
    variable (`agentctl spawn --issue`'s model choice, which exempts a dispatch
    from the origin refusal). Every other client uses `url()` / `library_url()`."""
    return f"{DEFAULT_LIBRARY_URL}{path}"


def library_headers(token: str | None = None, *, json_body: bool = False) -> dict:
    """`headers()` for a Library route: the protocol header is always sent,
    since the Library requires it and this is never Orient."""
    out = headers(token, json_body=json_body)
    out[PROTOCOL_HEADER] = PROTOCOL_VERSION
    return out


def describe() -> str:
    """The plane in words, for a message.

    **So a client never has to interpolate the raw address**, which is the one
    shape that reads as innocent and composes into a request one edit later. The
    sweep in `test_vault_plane.py` flags an f-string that joins anything to a base
    address, and it was right to flag the error message this replaced: the module
    that owns the routing should own how the routing is named.
    """
    return f"{'the Library' if on_library() else 'Orient'} at {base_url()}"


def headers(token: str | None = None, *, json_body: bool = False) -> dict:
    """The headers a request to the configured plane needs.

    The protocol header is added only on the Library, because Orient has no
    opinion about it and sending it there would be a header this repo has to
    explain to the next reader of Orient's access log.
    """
    out: dict[str, str] = {}
    if token:
        out["Authorization"] = f"Bearer {token}"
    if json_body:
        out["Content-Type"] = "application/json"
    if on_library():
        out[PROTOCOL_HEADER] = PROTOCOL_VERSION
    return out
