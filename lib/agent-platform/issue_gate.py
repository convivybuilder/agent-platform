"""The overnight drain's dequeue gate (ARCH-361 Part B).

What this exists to stop: on 2026-07-17 the overnight drain opened convivy/vivy
#971 for work a daytime co session was already doing on #944. That was a
duplicate *branch*, not merely a duplicate dequeue — two workers built the same
thing twice, and a human had to untangle it.

Two independent signals say "leave this one alone", and both are needed:

1. **A live claim.** A working session holds the issue. The server already
   answers this: `state=claimable` returns open issues plus those whose claim has
   expired, so a live claim is filtered out server-side and an expired one (a
   session that died) is correctly still available.

2. **Work already in flight.** An issue can have a branch or a PR without an
   unexpired claim — the claim may have expired while the branch lived on, or the
   work may have started before claiming was wired up. This is precisely the
   2026-07-17 case, so the claim check alone would not have prevented it.

Signal 2 has its own two sources, because neither is sufficient:

  * the issue's own `refs` (`refs.pr` / `refs.branch`), which is authoritative
    when the worker linked its work back, and silent when it did not;
  * a scan of the product repo for an `agent/` branch whose purpose carries
    this issue's id, which catches the branch a worker opened without ever
    linking it — again, the 2026-07-17 case. See the CQ·354 note below for
    what this scanned before and why it changed.

This module is deliberately CODE rather than prompt text. The dequeue decision
used to live in the triage stage's prompt, and ARCH-311 is the standing lesson
that a prompt-driven step feeding a deterministic consumer fails silently: the
night simply does nothing, or does the wrong thing, and nobody notices until
morning. A gate that decides correctness belongs where it can be tested.

Failure posture: this gate FAILS CLOSED when the branch scan itself errors
(no network, no safe `gh`, an unregistered product) — that is treated as
"cannot prove this issue is free", so the issue is skipped rather than
handed to a worker. Duplicating work is the expensive failure; deferring an
issue by one night is cheap. This does not cover an issue id too short to
produce a fragment at all (`_issue_id_fragment` returns `None` below
`_ID_FRAGMENT_LEN` characters): that id is skipped from the fragment match
rather than failing the scan, since no id in use today is short enough to
take that path — see `_issue_id_fragment`'s docstring.

ROUND 7: the branch scan reads GitHub's REST API (`gh api`) rather than
running `git ls-remote` against a local product checkout. Rounds 1-6 kept
finding another lever reachable through that checkout's config — the SSH
identity lookup, the PATH a bare `git`/`ssh` would have searched, repo-local
`.git/config` itself (`credential.helper`, the `ext::` transport, and round
6's finding, `core.askPass` plus `GIT_CONFIG_PARAMETERS`, both of which ran a
planted program even with `GIT_TERMINAL_PROMPT=0` set) — because every one of
those levers lives inside a checkout this gate had to `-C` into, and a
product checkout's `.git/config` is shared, live and unreviewed across every
worktree a dispatched coder holds of that product. Reading the API instead
means no checkout, no `-C`, no git or ssh process, so none of that exists to
reach; see `branches_referencing_issues`'s docstring for the detail.

ROUND 8 (PR comment 5939772429): round 7's `gh api` call inherited the whole
environment, which is exactly how gh's OWN config file reaches it — `gh`
reads `http_unix_socket` from `$GH_CONFIG_DIR`/`~/.config/gh/config.yml`
regardless of how the token is supplied, and routing the call through a
planted socket serving an empty result is the round-6 duplicate-work outcome
again, one layer up. Every `gh api` call got a from-scratch environment and
its own fresh `GH_CONFIG_DIR`, created and removed around that one call, so
the account's real config file was never consulted for routing.

Round 7 also read `config/projects.json` through `projects.get`, which
resolves into this process's own working tree — the same checkout a
dispatched coder's worktree shares live, agent-writable refs and config
with, which is precisely why round 7 stopped `-C`ing into a checkout for the
branch scan itself. Round 8 moved that read onto GitHub's contents API
against agent-platform's own `origin/main` instead, under the same per-call
isolation as the branch scan, so a dispatched coder's edit to the working
tree's copy (or to this repository's local refs or config) cannot change
which repo gets scanned until a human merges it to `main`.

CQ·354 (built on #736): the scan matched `refs/heads/issue/<id>`, a branch
name nothing in this codebase ever creates — `agentctl` names a dispatched
worker's branch `agent/<purpose>`, where `purpose` is derived from the
dispatcher's free-text `--task`, not from the issue id alone (`derive_purpose`
in `bin/agentctl`). Measured 2026-10-01: `matching-refs` on `heads/issue/`
returned 0 on every product repo checked, and 252 on `heads/agent/` for
agent-platform alone. The scan now matches `heads/agent/`, and recognizes a
branch as belonging to an issue by checking whether the issue id's own
normalized form (`branch_naming.slugify`, the SAME transform `agentctl`
itself applies when it builds a branch name — imported here rather than
reimplemented, so the two cannot drift) appears, truncated to
`_ID_FRAGMENT_LEN` characters, inside the branch's purpose. See
`_issue_id_fragment`'s docstring for the length and the collision reasoning,
and the PR body for the measurements behind both. The old `issue/<id>`
pattern is dropped rather than kept alongside the new one: nothing in
`bin/`, `lib/`, `sbin/`, or `tools/` on this branch creates such a ref (the
only other hit was this module's own skip-reason message, now reworded).

ROUND 9 (round-8 security review, PR comment on #736): round 8's per-call
`GH_CONFIG_DIR` closed the deterministic hole but not a timing one. The
directory round 8 created lived under the per-user temp root with a fixed,
grep-able prefix (`issue-gate-gh-config-`), and a same-account process — the
exact attacker this whole fix chain exists to stop, since a dispatched
coder and the overnight drain run as the same account — could watch for a
new directory with that prefix and write a `config.yml` carrying
`http_unix_socket` into it in the few milliseconds between `mkdtemp()`
returning and `gh`'s `execve()` reading it. Winning that race reopens the
round-6/round-7 outcome this whole chain exists to close: a forged, empty
API response read as "no branch exists". Round 7's own precedent for this
class was removing the lever rather than hardening the same shape again
(dropping the checkout rather than pinning more git config keys); round 9
does the same here by dropping `gh` the binary from both API calls. The
registry read and the branch scan now make their own in-process HTTPS
requests to `api.github.com` (`_github_api_get`, stdlib `urllib` only), with
the token carried as an `Authorization: Bearer` header built directly —
there is no `gh` config file left for any process to plant a routing key
into, because no `gh` process runs for either call. `_resolve_gh_token`
still runs `gh auth token` under the account's real, inherited environment,
unchanged from round 8; see its docstring for why that one call stays as
it is.

CQ·407: on a single-account Builder install, agents authenticate to GitHub as
a GitHub App (Shipwright epic E11) rather than as the operator, so there is
no `gh auth token` to read. `_resolve_token` now checks for the install's
configured App-token minter (`github-app-token`, shipwright#143/comment 4926)
first, and runs `gh auth token` only when none is configured. A configured
minter that fails is never followed by a `gh` fallback: on a single-account
install that would authenticate as the operator, exactly what E11 removes.
See `_resolve_token`'s docstring for the configuration check and
`_resolve_minted_token`'s for how the minter is run.

CQ·354 ROUND 2 (PR #758 security review): two findings, no behavior change.
(1) A pushed `agent/` branch whose name is crafted to contain several issue
ids' fragments would defer every one of those issues at once, where the old
`issue/<id>` pattern bound one branch to one issue. Not changed: pushing
such a branch already requires the same repository access as the operator's
own account, which can claim, close, or edit any of those issues directly,
so the crafted name grants nothing new; substring matching is also what
keeps every real dispatch-brief shape visible to this scan, and the
direction that matters here is a false negative (the 2026-07-17 case this
gate exists to stop), not a false positive. (2) `_issue_id_fragment`
returning `None` for an id too short to fragment-match, and the scan
skipping that id, reads as a contradiction of the FAILS CLOSED posture
above — it was undocumented as such. See `_issue_id_fragment`'s docstring
and the skip in `branches_referencing_issues` for the corrected text: that
id is skipped from the fragment match, not failed closed, and no id in use
today is short enough to take that path.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

import branch_naming as BN
import issue_client
import projects
import safe_path as SP

# The branch prefix every dispatched worker's branch actually has
# (`bin/agentctl`'s `derive_purpose` + `worktrees.branch_for_purpose`):
# `agent/<purpose>`. Measured 2026-10-01: 252 matches on agent-platform alone
# for `heads/agent/`, 0 on `heads/issue/` across every product repo checked —
# see the module docstring's CQ·354 note.
_AGENT_BRANCH_PREFIX = "refs/heads/agent/"

# How many leading characters of an issue id's own normalized form
# (`branch_naming.slugify`) must survive, as a contiguous substring, inside a
# candidate `agent/` branch's purpose for that branch to count as "in flight
# for this issue". See `_issue_id_fragment`'s docstring for why this length
# and what it costs.
_ID_FRAGMENT_LEN = 18

_GH_API_TIMEOUT = 30  # a few paginated REST calls per product per night; not latency-critical
_GH_TOKEN_TIMEOUT = 10  # a local credential read (`gh auth token`), not a network call

# The App-token minter (shipwright#143, relayed in CQ·407 comment 4926):
# command name, its configuration directory under Builder's config root, and
# the env var that overrides that directory (tests only, per the minter's own
# `config_dir()`). A minter is configured when `_MINTER_APP_ID_FILE` exists
# under `_github_app_dir()`.
_MINTER_NAME = "github-app-token"
_GITHUB_APP_DIR_ENV = "BUILDER_GITHUB_APP_DIR"
_MINTER_APP_ID_FILE = "app-id"

# Where the install links `_MINTER_NAME`: `~/.local/bin`, Builder's own
# linked-command directory (shipwright CQ·59's `connect-github`, `bin_dir()`
# — the real file is placed under `~/.local/deploy/github-app` and this is the
# symlink into it, the same directory `co`/`agentctl`/the rest of this
# platform's linked commands live in). See `_minter_bin_dir`'s docstring. See
# `_resolve_minted_token`'s docstring for why this is checked directly rather
# than through `$PATH`.

# The minter mints over the network on a cache miss (an installation token
# from GitHub), unlike `gh auth token`'s local credential read, so this uses
# the same bound as a REST call rather than `_GH_TOKEN_TIMEOUT`.
_MINTER_TIMEOUT = _GH_API_TIMEOUT

# agent-platform's own GitHub identity, named here as a fixed literal rather
# than read from any checkout's remote — the same reason `POLICY_PATH` in
# `agent_policy.py` and the knowledge repo's path elsewhere are literals: the
# repository this module reads its own registry from is not itself a
# variable this process should resolve from agent-influenced state.
_REGISTRY_REPO_SLUG = "convivy/agent-platform"
_REGISTRY_PATH = "config/projects.json"

# `{slug}` is `owner/repo`, filled in by `_github_repo_for`. The trailing
# slash makes this a RAW STRING prefix match against `refs/heads/agent/...`
# (GitHub's matching-refs endpoint matches on the literal string after
# `refs/`, not on path components) — without it, `heads/agent` would also
# match an unrelated branch like `agent-not-really-a-worker-branch`.
_MATCHING_REFS_PATH = "repos/{slug}/git/matching-refs/heads/agent/"

_CONTENTS_PATH = "repos/{slug}/contents/{path}?ref=main"

# ROUND 9: both calls made through `_github_api_get` go straight to the REST
# API over HTTPS — no `gh` process, so no `gh` config file for anything to
# plant a routing key into. `_API_HOST` is also what every pagination
# `Link: rel="next"` URL is checked against before it is followed.
_API_HOST = "api.github.com"
_API_BASE = f"https://{_API_HOST}/"
_API_VERSION = "2022-11-28"
_USER_AGENT = "agent-platform-issue-gate"

# Same reasoning as `issue_client.list_issues_by_state`'s `_MAX_PAGES`: this
# runs unattended from launchd, where an unbounded pagination loop against a
# misbehaving (or cycling) `Link` header is a machine hammering an API all
# night with nobody watching. A ceiling turns that into one loud failure.
_MAX_API_PAGES = 100


class BranchScanError(RuntimeError):
    """The repo could not be scanned, so no issue can be proven free of a branch."""


def _resolve_gh_token(gh_bin: str, *, timeout: int) -> str:
    """The calling account's own GitHub token, via `gh auth token`.

    Run with the account's real, inherited environment and config, no
    `env=` override — deliberately, not merely left over from before round
    8 isolated the other two calls. This subcommand reads the stored
    credential (`hosts.yml` or the system keyring) and issues NO HTTP
    request, so there is nothing here for a planted `http_unix_socket` or
    `api_host` to intercept, and an attacker who could substitute the
    returned token grants themselves nothing beyond what same-account write
    access to this process already grants. A future edit must not isolate
    this call "for consistency" with `_github_api_get` without re-deriving
    that this reasoning still holds — the two calls are isolated for
    different reasons and only one of them makes a network request. The
    returned token is passed explicitly to each `_github_api_get` call as
    its `Authorization` header. Never logged.
    """
    try:
        out = subprocess.run(
            [gh_bin, "auth", "token"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise BranchScanError(f"gh auth token failed: {e}") from e
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise BranchScanError(f"gh auth token exited {out.returncode} with no token: "
                              f"{out.stderr.strip()[:200]}")
    return token


def _github_app_dir() -> pathlib.Path:
    """The App-token minter's configuration directory: `_GITHUB_APP_DIR_ENV`
    when set (tests only — the minter's own `config_dir()` reads the same
    override), else `~/.config/convivy/github-app`, matching the minter's own
    default so the two agree on where "configured" means."""
    override = os.environ.get(_GITHUB_APP_DIR_ENV)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".config" / "convivy" / "github-app"


def _minter_configured(app_dir: pathlib.Path) -> bool:
    """Whether the install has configured an App-token minter: `app_dir`
    holds an `app-id` file (shipwright#143/comment 4926). This only checks
    for the file's presence — the minter itself, not this gate, is
    responsible for the key and the installation id being valid."""
    return (app_dir / _MINTER_APP_ID_FILE).is_file()


def _minter_bin_dir() -> pathlib.Path:
    """Where the install links `_MINTER_NAME`: reads `HOME` via
    `pathlib.Path.home()`, then appends the fixed relative path
    `.local/bin`, matching shipwright's installer (`bin/connect-github`'s
    `bin_dir`, shipwright#145). See this module's comment above
    `_MINTER_NAME` for what directory this is."""
    return pathlib.Path.home() / ".local" / "bin"


def _resolve_minted_token(*, timeout: int) -> str:
    """The calling account's GitHub token, via the install's configured
    App-token minter (`github-app-token`), run with no arguments.

    Found at `_minter_bin_dir() / _MINTER_NAME` — see `_minter_bin_dir`'s
    docstring for what directory that is. It no longer searches `$PATH`:
    `shutil.which(_MINTER_NAME)` was used here before this check, which did
    consult `$PATH`, and a dispatched agent can shadow a `$PATH` entry at no
    cost. Checking the fixed path directly removes that dependency. The
    minter is this install's one GitHub credential source once configured,
    so there is no narrower account-controlled identity for a planted
    same-named file at that path to impersonate into: same-account write
    access already grants whatever such a substitution would (same
    reasoning `_resolve_gh_token`'s own docstring gives for leaving `gh auth
    token` itself unisolated).

    On exit 0 with one token line printed, returns it. On a non-zero exit, a
    timeout, or empty stdout, raises `BranchScanError` naming the minter and
    carrying its one-sentence stderr message (contract: comment 4926) — this
    is never followed by a `gh auth token` fallback, because on a
    single-account App install that would authenticate as the operator,
    exactly what a configured minter exists to prevent. The token itself is
    never included in any error or log.
    """
    minter = _minter_bin_dir() / _MINTER_NAME
    if not (minter.is_file() and os.access(minter, os.X_OK)):
        raise BranchScanError(f"{_MINTER_NAME} is configured but not found at {minter}")
    try:
        out = subprocess.run(
            [str(minter)], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise BranchScanError(f"{_MINTER_NAME} failed: {e}") from e
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise BranchScanError(f"{_MINTER_NAME} exited {out.returncode} with no token: "
                              f"{out.stderr.strip()[:200]}")
    return token


def _resolve_token(*, gh_timeout: int = _GH_TOKEN_TIMEOUT,
                   minter_timeout: int = _MINTER_TIMEOUT) -> str:
    """The calling account's GitHub token: through the install's configured
    App-token minter when one is configured (`_minter_configured`), else
    through `gh auth token` unchanged (`_resolve_gh_token`). Resolved once
    per scan and reused for every `_github_api_get` call it feeds — see
    `branches_referencing_issues`."""
    if _minter_configured(_github_app_dir()):
        return _resolve_minted_token(timeout=minter_timeout)
    gh_bin = SP.safe_which("gh")
    if gh_bin is None:
        raise BranchScanError("no safe gh on PATH to resolve a token")
    return _resolve_gh_token(gh_bin, timeout=gh_timeout)


def _opener() -> urllib.request.OpenerDirector:
    """Built fresh per call. `ProxyHandler({})` applies no proxy from any
    environment variable (`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` and their
    lowercase forms, which the stock `ProxyHandler` would otherwise read) —
    those are agent-settable and would otherwise route this call, and the
    bearer token riding on it, through a listener of the agent's choosing.
    Default certificate verification applies; nothing here overrides it.

    Only the `ProxyHandler` slot is overridden. `build_opener` still installs
    its other defaults, including `HTTPRedirectHandler`, so a 3xx response
    from `api.github.com` is followed automatically, carrying the
    `Authorization` header to wherever `Location` points — with no check that
    `Location`'s host is `_API_HOST`, unlike the equivalent check
    `_next_page_url` applies to a `Link` header's `rel="next"` URL. Round 9
    review found this; the PR body explains why it stays unclosed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _next_page_url(link_header: str | None) -> str | None:
    """The `rel="next"` URL out of a `Link` response header, or None when
    there isn't one. Raises BranchScanError if that URL's host is anything
    other than `_API_HOST`, or if the URL itself doesn't parse. This is not
    the only place a response's own content gets followed: `_opener()`
    installs no custom `HTTPRedirectHandler`, so urllib's default one still
    follows a 3xx response from `api.github.com` and carries the
    `Authorization` header to wherever `Location` points, with no host check
    of its own — see `_opener()`'s docstring."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url_part = section[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        if 'rel="next"' not in ";".join(section[1:]):
            continue
        next_url = url_part[1:-1]
        try:
            parsed = urllib.parse.urlsplit(next_url)
            off_host = parsed.scheme != "https" or parsed.hostname != _API_HOST
        except ValueError as e:
            # `urlsplit` itself raises a bare `ValueError` on a malformed bracketed
            # netloc (e.g. "https://[bad/x" -> "Invalid IPv6 URL") before this
            # function's own off-host check ever runs, and `.hostname`/`.port` on an
            # otherwise-parsed result can raise too (an out-of-range port). Either
            # way this is a `Link` header this function's own docstring already
            # claims to reject, not a new trigger (round-11 security review, PR
            # comment on #736).
            raise BranchScanError(
                f"GitHub API pagination Link had an unparseable next URL: "
                f"{next_url!r:.200} ({e})") from e
        if off_host:
            raise BranchScanError(
                f"GitHub API pagination Link pointed off {_API_HOST}: {next_url!r:.200}")
        return next_url
    return None


def _github_api_get(path: str, *, token: str, timeout: int, paginate: bool = False):
    """GET `https://api.github.com/<path>`, in-process over HTTPS (stdlib
    `urllib` only) — no `gh` process, so no `gh` config file for anything to
    plant a routing key into; see the module docstring's ROUND 9 note.

    Returns the parsed JSON body (`paginate=False`), or a list concatenating
    every page's JSON array body, followed via the `Link` header's
    `rel="next"` until there is none (`paginate=True`) — the endpoints this
    reads either return a single object (contents) or a single JSON array
    across however many pages GitHub splits it into (matching-refs).

    Raises BranchScanError on anything that stops this from being a clean,
    complete read: an HTTP error status (including 404 and 403), a network,
    timeout or malformed/truncated-response failure (`OSError` or
    `http.client.HTTPException`), a request that `urllib.request.Request`
    itself rejects (`ValueError`, which includes a `Link` header's next URL
    failing to parse), a response body that is not valid JSON, a paginated
    response whose body is not a JSON array, a `Link` header whose next URL
    points off `api.github.com`, or more pages than `_MAX_API_PAGES`.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    opener = _opener()
    url = _API_BASE + path
    pages: list[Any] = []
    for _ in range(_MAX_API_PAGES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                link_header = resp.headers.get("Link")
        except urllib.error.HTTPError as e:
            raise BranchScanError(f"GitHub API GET {path} failed: HTTP {e.code} {e.reason}") from e
        except (OSError, http.client.HTTPException, ValueError) as e:
            # `OSError` covers `URLError`, connection failures, `ssl.SSLError` and
            # timeouts. It does NOT cover `http.client.HTTPException` (`IncompleteRead`
            # on a truncated body, `BadStatusLine` on a malformed status line, etc) —
            # those come from `h.getresponse()`/`resp.read()` and are not `OSError`
            # subclasses, so a truncated or malformed response would otherwise escape
            # this function uncaught, past `eligible_issues`'s `except BranchScanError`,
            # and abort a caller's whole per-product loop instead of costing
            # one product one quiet night (round-9 review, PR comment on #736).
            # `ValueError` covers `Request(url, ...)` itself rejecting `url` (its
            # constructor parses the URL the same way `urlsplit` does) — from round 2
            # of this loop onward, `url` is the next-page URL `_next_page_url` already
            # validated, but the construction is folded into this same try rather than
            # left to raise bare, consistent with `_next_page_url`'s own fix this round
            # (round-11 security review, PR comment on #736).
            raise BranchScanError(f"GitHub API GET {path} failed: {e}") from e
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as e:
            # `ValueError` also covers `json.JSONDecodeError` (a subclass) and
            # `UnicodeDecodeError`, which `json.loads` raises from its own internal
            # `bytes.decode()` step on a body whose bytes aren't valid in the
            # encoding it detects — a corrupted or non-UTF-8 response, the same
            # "malformed response" trigger class this function's docstring already
            # claims to cover (round-10 security review, PR comment on #736).
            # `RecursionError` covers a pathologically deeply nested body, which
            # `json.loads`'s recursive parser raises as neither `ValueError` nor
            # anything this `except` previously named.
            raise BranchScanError(f"GitHub API GET {path} returned unparseable JSON: {e}") from e
        if not paginate:
            return payload
        if not isinstance(payload, list):
            raise BranchScanError(
                f"GitHub API GET {path} paginated response was not a JSON array: {payload!r:.200}")
        pages.extend(payload)
        next_url = _next_page_url(link_header)
        if next_url is None:
            return pages
        url = next_url
    raise BranchScanError(f"GitHub API GET {path} still had more pages after "
                          f"{_MAX_API_PAGES}; refusing to keep paging")


def _registry_at_origin_main(*, token: str, timeout: int = _GH_API_TIMEOUT) -> dict[str, Any]:
    """`config/projects.json` as committed at agent-platform's own
    `origin/main`, read through GitHub's contents API rather than through
    this process's own working tree.

    `projects.get` (round 7's source) resolves into the tree this module's
    own `~/.local/lib` symlink points at — the SAME checkout a dispatched
    platform-coder's worktree shares live, agent-writable refs and config
    with (`git worktree add` links, it does not clone). `-C`ing into that
    checkout for a `git show origin/main:...` would be reachable through
    exactly the levers round 7 removed `-C` for in the first place. The
    contents API removes the checkout from this read too, matching
    `branches_referencing_issues`.

    Raises BranchScanError when the call fails, or when the response is not
    the base64-encoded JSON file this registry is.
    """
    path = _CONTENTS_PATH.format(slug=_REGISTRY_REPO_SLUG, path=_REGISTRY_PATH)
    payload = _github_api_get(path, token=token, timeout=timeout)
    try:
        if not isinstance(payload, dict):
            raise TypeError(f"contents response was {type(payload).__name__}, not an object")
        content = base64.b64decode(payload["content"])
        return json.loads(content)
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        # `ValueError` covers `json.JSONDecodeError`, `UnicodeDecodeError` out of
        # `json.loads`, and `binascii.Error` out of `b64decode` (all three are
        # `ValueError` subclasses); `RecursionError` covers a pathologically
        # nested body, which `json.loads` raises as neither `ValueError` nor
        # anything this `except` previously named (round-10 security review, PR
        # comment on #736; same gap as `_github_api_get`'s own parse site).
        raise BranchScanError(
            f"the project registry at {_REGISTRY_REPO_SLUG}@main could not be parsed: {e}") from e


def _github_repo_for(product: str, *, token: str | None = None,
                      timeout: int = _GH_API_TIMEOUT) -> str:
    """`owner/repo` for `product`'s primary GitHub repository, read from the
    project registry (`config/projects.json`) at agent-platform's own
    `origin/main` rather than from any checkout — this process's own tree
    included; see `_registry_at_origin_main`'s docstring for why.

    `token` lets a caller that already resolved one (`branches_
    referencing_issues`) avoid doing so twice; a caller with none resolves
    its own via `_resolve_token`.

    The GitHub owner comes from `projects.repo_github_slug`, given this
    already-resolved `unit` so it never has to search `load()` (which reads
    this process's OWN tree, the thing this function's registry-at-origin-
    read exists to avoid): the repo's own entry in the unit's `repo_owners`
    map, else the unit's `github_owner` field, else `"convivy"` — NOT from
    `owner`, which `config/projects.json`'s own `_meta.fields` documents as
    "pure metadata", not a GitHub account, and which reads `"nyh"` for the
    three NYH projects even though their repositories live under the
    operator's personal account (round-7 security review, PR comment
    5939963599). Mirrors the old `repo_path_for`'s assumption (rounds 1-6)
    that a product's primary repo shares the product's own slug — true for
    every product this gate runs against today (`vivy`, `constance`,
    `sous-chef` each list their own slug first under `repos` in the
    registry).
    """
    if token is None:
        token = _resolve_token()
    registry = _registry_at_origin_main(token=token, timeout=timeout)
    unit = registry.get(product) if isinstance(registry, dict) else None
    if not unit:
        raise BranchScanError(f"{product!r} is not a registered project "
                              f"(config/projects.json at {_REGISTRY_REPO_SLUG}@main)")
    repos = unit.get("repos") or []
    if not repos:
        raise BranchScanError(f"{product!r} has no repos listed in the project registry")
    return projects.repo_github_slug(repos[0], unit=unit)


def _issue_id_fragment(issue_id: str) -> str | None:
    """The leading `_ID_FRAGMENT_LEN` characters of `issue_id`'s own
    normalized form (`branch_naming.slugify`, the same transform `agentctl`
    applies when it builds `derive_purpose`'s branch-name slug), or `None`
    when `issue_id` normalizes to fewer characters than that. A `None` here
    makes the caller skip this id from the fragment match entirely (see the
    scan loop in `branches_referencing_issues`) — the id is treated as
    unmatchable, not as a signal to fail the scan closed. Every id the queue
    issues today is a 26-character ULID, far longer than `_ID_FRAGMENT_LEN`,
    so this branch is unreached in current use.

    WHY A PREFIX, AND WHY THIS LENGTH. A dispatched worker's branch is
    `agent/<purpose>`, where `purpose` comes from `derive_purpose`'s own
    `_slug(task, prefix=agent_name)` — the id, when the dispatcher's task
    text names one, sits inside a slug of the WHOLE task text truncated to
    32 characters. `_slug`'s truncation always drops characters off the
    RIGHT end of that 32-character window, never the middle, so whatever
    of the id survives is always a PREFIX of the id's own normalized
    form — never a different substring. Checking a FIXED-length prefix is
    therefore a safe lower bound: if the branch carries at least
    `_ID_FRAGMENT_LEN` characters of the id, this catches it, regardless of
    how much more of the id (if any) also survived.

    18 was chosen off two real branches of dispatches for THIS gate's own
    kind of brief ("Fix/Build/... work item issue_<id> ..."): this module's
    own CQ·354 fix (`agent/platform-coder-fix-work-item-issue-01m3wjmxzh5h-
    1ff2b000`, 12 surviving id characters after the 6-character literal
    `issue-`) and #736's (`...-work-item-issue-01m3tyeyr8bv2e7e-...`, 16).
    18 catches both. A dispatch brief with a longer lead-in than either of
    those (bin/agentctl's own test fixture "Implement Course item
    issue_<id>" is a documented worse case, at 4) will not be caught — a
    known residual gap of coupling branch identity to a free-text `--task`,
    not something a fixed prefix length can close without this length
    collapsing most of an open queue into one fragment (see below). See the
    PR body for the measurement this length is based on.

    COLLISION. The first 10 characters of a ULID encode its creation
    timestamp at ~1ms resolution; `_ID_FRAGMENT_LEN=18` is 6 literal
    characters (`issue-`) + 12 id characters, i.e. the FULL 10-character
    timestamp plus only 2 characters of the 16-character random suffix (32
    possible values each, so 1-in-1024 for those two to agree). Two
    currently-open issues collide on this fragment only if they were
    created within about the same millisecond AND happen to share those 2
    characters — implausible for ordinarily-spaced issue creation, but NOT
    implausible for several issues filed back-to-back in one process (e.g.
    a review round filing a handful of findings as separate issues), more
    so if the id generator is a monotonic ULID that keeps leading random
    bits fixed within a burst. `eligible_issues` treats a collision as
    "fails closed" for every id sharing the fragment — the SAME safe
    direction this whole gate already picks when it cannot prove an issue
    is free (see the module docstring): a false match costs one issue a
    night's delay, not a duplicate build.
    """
    normalized = BN.slugify(issue_id)
    if len(normalized) < _ID_FRAGMENT_LEN:
        return None
    return normalized[:_ID_FRAGMENT_LEN]


def branches_referencing_issues(product: str, issue_ids: Iterable[str],
                                *, timeout: int = _GH_API_TIMEOUT) -> dict[str, str]:
    """Which of `issue_ids` already have an `agent/` branch on GitHub, read
    via the REST API: `{issue_id: matching_branch_name}` for every id in
    `issue_ids` whose fragment (`_issue_id_fragment`) is a substring of some
    `agent/<purpose>` branch's name — several ids may map to the same
    branch name on a fragment collision; see `_issue_id_fragment`'s
    docstring.

    ROUND 7. This used to run `git -C <checkout> ls-remote --heads origin`
    (rounds 1-6). Every round closed one more config file a dispatched coder
    could reach through that `-C <checkout>` and have honoured the next time
    this ran: the SSH identity/ProxyCommand lookup, the PATH a bare "git" or
    "ssh" would have searched, then repo-local `.git/config` itself —
    `credential.helper`, the `ext::` transport, and (round 6's finding,
    reproduced directly against that round's code — see the PR body)
    `core.askPass`, which ran even with `GIT_TERMINAL_PROMPT=0` set because
    git runs askpass before it honours that variable, and
    `GIT_CONFIG_PARAMETERS`, which carries config with no file at all and
    which the round-6 strip loop never covered (it only stripped
    `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`). There was
    always one more key to pin, because the checkout itself was the input.

    Reading GitHub's API instead removes the checkout: no `-C`, no git
    process, no ssh process, so none of the above exists to reach. The
    owner/repo scanned comes from `_github_repo_for` (the registry, itself
    read at `origin/main` as of round 8), not from any checkout's remote, so
    repointing a checkout's `origin` at an empty mirror — round 6 review's
    third finding, which would have made the old scan see zero branches and
    hand out duplicate work — has nothing to act on either; reproduced
    directly against round 6's code in the PR body.

    ROUND 9: the registry read and this scan now make their own in-process
    HTTPS requests (`_github_api_get`) rather than running `gh api` at all,
    closing the TOCTOU the round-8 security review found in round 8's
    per-call `GH_CONFIG_DIR` isolation — see the module docstring.

    CQ·354: the scan itself now matches `heads/agent/` (what `agentctl`
    actually names a dispatched worker's branch) instead of `heads/issue/`
    (what nothing creates), and recognizes an issue's branch by fragment
    containment instead of parsing an id straight out of the ref name — see
    `_issue_id_fragment`.

    CQ·407: the token comes from `_resolve_token`, which runs the install's
    configured App-token minter when one is configured, else `gh auth
    token` unchanged — resolved once here and reused for both this call and
    `_github_repo_for`'s.

    Raises BranchScanError on anything that stops this from being a clean,
    complete read: the product unregistered, no token source available (no
    safe `gh`, or a configured minter that fails), a call failing, timing
    out, or returning malformed JSON, or pagination stopping partway through
    a result set.
    """
    token = _resolve_token()
    repo_slug = _github_repo_for(product, token=token, timeout=timeout)
    refs = _github_api_get(
        _MATCHING_REFS_PATH.format(slug=repo_slug), token=token,
        timeout=timeout, paginate=True,
    )
    branch_names = []
    for item in refs:
        ref = item.get("ref") if isinstance(item, dict) else None
        if isinstance(ref, str) and ref.startswith(_AGENT_BRANCH_PREFIX):
            branch_names.append(ref[len("refs/heads/"):])
    found: dict[str, str] = {}
    for issue_id in issue_ids:
        if not issue_id:
            continue
        fragment = _issue_id_fragment(issue_id)
        if not fragment:
            # Too short to fragment-match (see `_issue_id_fragment`'s
            # docstring) — this id gets no branch match here, not a failed
            # scan; unreached today since every id is a 26-character ULID.
            continue
        for branch_name in branch_names:
            if fragment in branch_name:
                found[issue_id] = branch_name
                break
    return found


def _in_flight_reason(item: dict[str, Any], branches: dict[str, str]) -> str | None:
    """Why this issue is already being worked, or None if it is free."""
    refs = item.get("refs") or {}
    if isinstance(refs, dict):
        if refs.get("pr"):
            return f"a PR is already linked ({refs['pr']})"
        if refs.get("branch"):
            return f"a branch is already linked ({refs['branch']})"
    branch_name = branches.get(item.get("id"))
    if branch_name:
        return f"a branch ({branch_name}) matches this issue's id fragment"
    return None


def eligible_issues(product: str, *,
                    items: list[dict] | None = None) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split a product's claimable issues into (eligible, skipped-with-reason).

    Returns the skip reasons rather than discarding them: a gate that silently
    drops work looks identical to a gate that found nothing to do, and the caller
    must be able to log WHY a night declined an issue.
    """
    items = issue_client.list_claimable_issues(product) if items is None else items
    if not items:
        return [], []
    issue_ids = [i.get("id") for i in items if i.get("id")]
    try:
        branches = branches_referencing_issues(product, issue_ids)
    except BranchScanError as e:
        # Fail closed: without the scan we cannot tell a free issue from one that
        # already has a branch, and handing out a duplicate is the costly error.
        return [], [(i, f"branch scan unavailable ({e})") for i in items]

    eligible, skipped = [], []
    for item in items:
        reason = _in_flight_reason(item, branches)
        (skipped.append((item, reason)) if reason else eligible.append(item))
    return eligible, skipped
