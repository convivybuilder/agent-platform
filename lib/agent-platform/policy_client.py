"""policy_client — the Library-owned tool deny-list the dispatch path enforces.

WHY THIS EXISTS. Orient shipped the tool-policy WRITE surface, and nothing on the
dispatch path read it — grepping `bin/` and `lib/` for `resolve_policy`,
`policy/resolve` or `/policy` returned nothing. The operator could open
`/engineering/policy`, set a denial, and be restricting nothing, with the page
looking exactly as it looks when it works. A control surface whose working and
broken states are indistinguishable is worse than an absent one.

THE ONE DETAIL THAT DECIDES WHETHER THIS BINDS. `tool_policy._ENFORCEMENT_GATE_CAVEAT`
records that with `AGENTCTL_ENFORCE_TOOL_POLICY` unset — the default (ARCH-346) —
a run executes under `bypassPermissions` with none of the allow-list applied, and
"only the deny-list binds". So the intuition is inverted from how this is usually
framed: DENY BINDS TODAY, ALLOW DOES NOT. A Library-backed policy wired through
the gated allow-list path would leave every entry the operator writes silently inert while
the stored row stayed perfectly correct — passing any verification that re-reads
the row. This module therefore produces DENIALS ONLY, merged into
`_disallowed_tools`, which is the path that already binds.

WHAT THE LIBRARY SERVES, confirmed against the live endpoint rather than a spec:

    GET /api/policy/resolve?agent=&crew=&model=
    {"agent":..., "crew":..., "model":...,
     "explicit": {},                                    per-tool overrides
     "connector_servers": [...],                        MCP connector servers
     "exfiltration_builtins": ["DesignSync","RemoteTrigger","WebFetch"],
     "default": "allow", "version": 0}

`exfiltration_builtins` and `connector_servers` are denied to every dispatched
worker by `categorical_denials`, and an explicit allow in a live record exempts
one target. A dispatch whose policy the Library does not answer is refused
(`PolicyUnavailable`, raised by `resolve`); nothing is served in its place, and
nothing is kept on disk between dispatches.
"""
from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import issue_client as IC
import vault_plane
from signal_client import token

TIMEOUT_S = 6


class PolicyUnavailable(RuntimeError):
    """The Library did not answer. The message says what was observed."""


_CLASS_FIELDS = ("exfiltration_builtins", "connector_servers")


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def resolve(agent: str, crew: str | None, model: str | None, *,
            tok: str | None = None, timeout: int = TIMEOUT_S) -> dict[str, Any]:
    """The Library's policy record for this dispatch, or raise PolicyUnavailable.

    RAISES RATHER THAN RETURNING A PERMISSIVE DEFAULT. An empty dict and "the
    Library said nothing is denied" are the same value and completely different
    facts, and the caller must be able to tell them apart — that is the whole
    lesson of the surface this replaces.

    A 200 is an answer only if it is an object with both class fields as lists
    of strings and `explicit` as an object. A record missing a field is refused,
    not read as zero classes. The Library's handler always sends `explicit`, as `{}`
    when no operator entry exists, so a body without it is not the served record.

    THE MESSAGE NAMES WHAT WAS OBSERVED: a timeout, a failed connection, or a
    bad response (an HTTP error status, a cut-short body, or a body that is not
    a policy record). The caller prints it.

    THE ADDRESS IS THE LIBRARY'S FIXED DEFAULT, read through the pinned opener, as
    the definition fetch reads it (`issue_client.get_item_from_default_library`).
    An answer from a listener the caller's environment points at could carry empty
    denials, and the bearer token would go to it. The path is `/policy/resolve`,
    the form `vault_plane.url` produces for the Library plane from `/api/policy/...`.
    """
    tok = tok or token()
    if not tok:
        raise PolicyUnavailable("no control-plane token was available, so the call was not made")
    q = urllib.parse.urlencode(
        {k: v for k, v in (("agent", agent), ("crew", crew), ("model", model))
         if v is not None})
    try:
        req = urllib.request.Request(vault_plane.fixed_library_url("/policy/resolve?" + q),
                                     headers=vault_plane.library_headers(token=tok))
        with IC.pinned_opener().open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise PolicyUnavailable(f"bad response: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise PolicyUnavailable(f"timed out after {timeout}s") from exc
        raise PolicyUnavailable(f"connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise PolicyUnavailable(f"timed out after {timeout}s") from exc
    except http.client.HTTPException as exc:
        raise PolicyUnavailable(f"bad response: {exc}") from exc
    except OSError as exc:
        raise PolicyUnavailable(f"connection failed: {exc}") from exc
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise PolicyUnavailable(f"bad response: body is not JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise PolicyUnavailable("bad response: body is not a policy object")
    for field in _CLASS_FIELDS:
        if not _is_string_list(record.get(field)):
            raise PolicyUnavailable(f"bad response: {field!r} is not a list of strings")
    if not isinstance(record.get("explicit"), dict):
        raise PolicyUnavailable("bad response: 'explicit' is missing or not an object")
    return record


def explicit_denials(record: dict[str, Any]) -> list[str]:
    """The tools an OPERATOR explicitly denied, as `--disallowedTools` values.

    ONLY `explicit`. The categorical lists (`exfiltration_builtins`,
    `connector_servers`) are `categorical_denials`' job, so the two sets stay
    separate and each is testable on its own.

    An explicit ALLOW is not a denial and must not become one through a
    truthiness test. An unrecognised verdict is ignored rather than guessed at,
    and the raw record is written to policy.json where a human can see it.
    """
    explicit = record.get("explicit") or {}
    if not isinstance(explicit, dict):
        return []
    out = [str(tool) for tool, verdict in explicit.items()
           if str(verdict).strip().lower() in ("deny", "denied", "false", "block")
           and is_safe_tool_name(str(tool))]
    return sorted(dict.fromkeys(out))


# A tool identifier, and nothing that could be read as a FLAG. These strings are
# the first network-sourced values ever to reach `claude -p`'s argv — every other
# member of `--disallowedTools` is a hardcoded literal. That flag is variadic, so
# its collection stops at the next token beginning with `-`: a policy record
# whose `explicit` dict carried a key like `--append-system-prompt` would
# terminate the list early and be re-parsed as a NEW FLAG to an unattended agent
# running under bypassPermissions — argument injection into the very process this
# policy exists to constrain (security review, PR #487).
#
# Permits what real tool names look like, including MCP's `mcp__server__tool` and
# the `Bash(git log:*)` grant form, and refuses a leading dash outright. Two shapes
# admit a `*`. The first is a whole connector server, `mcp__server__*`, which is how
# `categorical_denials` names a connector. The second is a `Name(...)` rule, whose
# parentheses admit any text without a `)`, so `Bash(*)` passes.
#
# The server segment admits `-` after its first letter: a live connector's server
# name has one (the Chrome extension's tools are `mcp__claude-in-chrome__*`), and
# this pattern used to refuse that name outright, dropping its categorical denial
# with one stderr line and running it un-denied (reviewer, security-reviewer, PR
# #784, carried rounds 2/5/6/7). The anchor stays `mcp__` followed by a letter,
# so the value still can never begin with `-` and be re-parsed as a flag.
_SAFE_TOOL_NAME = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(\([^)]*\))?"
    r"|mcp__[A-Za-z][A-Za-z0-9_-]*__\*")


def is_safe_tool_name(name: str) -> bool:
    """True if `name` may be passed to `--disallowedTools` as argv.

    REFUSES RATHER THAN ESCAPES. There is no quoting that makes a token starting
    with `-` safe in a variadic flag's collection, so the only correct answer is
    not to pass it. A rejected name is reported, never silently dropped — a
    denial that vanished quietly is the failure this whole change exists to
    close.
    """
    # `fullmatch`, NOT `match`. Python's `$` matches before a TRAILING NEWLINE,
    # so `_SAFE_TOOL_NAME.match("Bash\n")` succeeded — clearing a name holding a
    # byte outside the declared set, in a function whose contract is "refuses
    # rather than escapes" (reviewer, PR #487). No parametrised case had a
    # trailing newline, which is how it shipped.
    return bool(name) and not name.startswith("-") and bool(_SAFE_TOOL_NAME.fullmatch(name))


_ALLOW_VERDICTS = ("allow", "allowed", "true")


def _categorical_targets(record: dict[str, Any]) -> list[tuple[str, frozenset[str]]]:
    """Each categorical target, with the explicit keys that allow it.

    A connector server is allowed by its bare server name (as `connector_servers`
    names it) or by its wildcard entry. A built-in is allowed only by its own name.
    """
    out = [(str(t), frozenset({str(t)})) for t in record.get("exfiltration_builtins") or ()]
    out += [(f"mcp__{s}__*", frozenset({str(s), f"mcp__{s}__*"}))
            for s in record.get("connector_servers") or ()]
    return out


def categorical_classes(record: dict[str, Any]) -> list[str]:
    """Every target in the two categorical classes, before any explicit allow."""
    return sorted(dict.fromkeys(t for t, _ in _categorical_targets(record)))


def categorical_denials(record: dict[str, Any]) -> list[str]:
    """The categorical classes every dispatched worker is denied, unless allowed.

    `exfiltration_builtins` and `connector_servers`, applied to every dispatch.
    The Library does not model private/public scope for these (the operator, 2026-09-29),
    so the route is the one platform/decisions/0048 names: deny them to every
    worker, and grant the agents whose purpose is the tool through an explicit
    allow. A target the record's `explicit` map sets to allow is dropped, and only
    that target. Any other verdict, or none, leaves the target denied, so an
    unrecognised verdict never grants.
    """
    explicit = record.get("explicit") or {}
    allowed = set()
    if isinstance(explicit, dict):
        allowed = {str(tool) for tool, verdict in explicit.items()
                   if str(verdict).strip().lower() in _ALLOW_VERDICTS}
    return sorted(dict.fromkeys(t for t, keys in _categorical_targets(record)
                                if not keys & allowed))
