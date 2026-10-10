"""bosun_authz — the Bosun's per-call authorization decision (E12·3, stage one).

STAGE ONE IS A POLICY AND AUDIT LAYER, NOT A CONTAINMENT BOUNDARY
(platform/decisions/0057). Nothing launches a dispatched agent's processes as
another account yet, so the boundary a `BoundaryGrant` names does not exist as
an operating-system fact — it is a recorded decision, not an enclosure. Never
describe this module, its callers or its output as isolation or containment.

WHAT THIS MODULE IS. The Materializer will eventually present every tool call
an agent makes, plus the agent's token, to the Bosun; the Bosun answers allow
or deny. This module IS that answer, built as a pure function so it can be
tested without a network, a daemon or an endpoint — none of which exist yet
and none of which this module adds. `decide()` is the whole surface a future
transport wraps.

TWO AUTHORIZATION SHAPES, because one shape cannot cover both halves of the
traffic (platform/decisions/0057, "Two authorization shapes"). A request that
NAMES its target — Read, Edit, Write, a document fetch, a work-item claim —
gets object-scoped authorization: one object, checked against the session's
declared ceiling. A request that EXECUTES — Bash, Grep, Glob, a test run —
gets a boundary grant instead, because its object set is discovered by running
it rather than declared up front (ADR-0057: "a recursive search reads a
tree"). AN UNKNOWN TOOL IS CLASSIFIED AS EXECUTES, never silently treated as
object-scoped, because the only safe default for a request this module cannot
parse is the one that gets no exemption from the ceiling at all.

NO NETWORK, NO FILE WRITES, NO ENVIRONMENT READS AT IMPORT. Every value this
module needs is passed in by the caller — the session grant, the already
Library-resolved permission set, the request, and the clock. Reading
`policy.json`, `tool_calls.jsonl` or `meta.json` off disk is the caller's job
(see `bin/agentctl`'s `authz replay`), not this module's.

A PATH-SHAPED OBJECT IS CHECKED LEXICALLY, NEVER RESOLVED, because resolving
a path means opening the filesystem and this module has none. A `..`
segment is denied outright rather than collapsed: collapsing `a/link/..`
with something like `os.path.normpath` is wrong the moment `link` is a
symlink, and a pure function cannot tell a symlink from a real directory.
This is also why this module cannot see a symlink planted inside a ceiling
(e.g. `/work/allowed/escape -> /etc`) that a lexically-contained path then
walks through — a lexical check cannot catch that, full stop. THE CALLER'S
CONTRACT (see `decide()`): the eventual enforcement point that actually
opens a file — the Individuator, once it exists — must resolve the path to
its real, symlink-free form (`os.path.realpath` or equivalent) before
calling `decide()`, and must open it without following a link at the final
component (e.g. `O_NOFOLLOW`). `decide()`'s answer is only as exact as the
path it is handed; it does not perform that resolution and must not be
described as if it does. A replay run (`bin/agentctl`'s `authz replay`)
feeds `decide()` a path recorded by the agent at call time, not a resolved
one, so its denial/allow counts inherit this same limit.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Ceiling scopes. A MINIMAL REPRESENTATION: a flat set of opaque "kind:value"
# strings, rather than a schema per resource type. One string type is enough
# to (a) serialize and compare trivially, (b) support a single prefix-match
# rule for "this object is inside that scope" across every kind, and (c)
# still keep a path-shaped scope from ever matching a document or work-item
# object, because the kind prefix makes the two string spaces disjoint. A
# richer per-kind structure would buy nothing this slice needs and would be
# a second thing to keep in sync with the classifier below.
# ---------------------------------------------------------------------------

PATH = "path"
DOC = "doc"
ISSUE = "issue"

# The sentinel entry for an unrestricted ceiling — the pre-ceiling default, a
# session that declares no reduction at registration (platform/decisions/0092
# Handoff: "`ceiling` is stored as an opaque string you give meaning to").
# `ceiling_covers` special-cases it below rather than letting it fall through
# the ordinary prefix match, because "*" is not itself a well-formed
# "kind:value" scope string and must never be compared as one.
FULL_CEILING = "*"


def _normalize_absolute_path(value: str) -> str:
    """The one lexical-normalization rule a PATH-kind value passes through,
    on both the request side (`extract_object`) and the ceiling side
    (`scope`), so the two sides can never disagree about what counts as "the
    same path". Collapses what is safe to collapse without a filesystem —
    repeated separators, a `.` segment, a trailing separator — and denies, by
    raising ValueError, anything this lexical check cannot judge safely:

    - a value that is not absolute;
    - a value containing a `..` segment. This is a denial, not something to
      normalize away: collapsing `a/link/..` lexically is wrong the instant
      `link` is a symlink, and this module has no filesystem access to tell
      (see the module docstring's caller contract).
    """
    if not value.startswith("/"):
        raise ValueError(f"not an absolute path: {value!r}")
    segments: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"path contains a '..' segment: {value!r}")
        segments.append(part)
    return "/" + "/".join(segments)


def scope(kind: str, value: str) -> str:
    """Build one ceiling entry. `kind` is one of PATH, DOC, ISSUE.

    For PATH, `value` is lexically normalized through
    `_normalize_absolute_path` before the scope string is built, and raises
    ValueError for a relative path or one containing a `..` segment — see
    that function and the module docstring for why `..` is refused rather
    than resolved.
    """
    if kind == PATH:
        value = _normalize_absolute_path(value)
    return f"{kind}:{value}"


def _normalize_ceiling_entry(entry: str) -> str:
    """Re-run a ceiling entry through `_normalize_absolute_path` if it is
    PATH-kind, so `SessionGrant.__post_init__` validates an entry the same
    way regardless of whether it was built with `scope()`. Raises ValueError
    the same way `scope()` does for a relative or `..`-bearing value;
    non-PATH entries (and anything not shaped like `kind:value`) pass
    through unchanged.
    """
    kind, sep, value = entry.partition(":")
    if sep and kind == PATH:
        return f"{kind}:{_normalize_absolute_path(value)}"
    return entry


def ceiling_covers(ceiling: frozenset[str], object_ref: str) -> bool:
    """True if `object_ref` (itself a "kind:value" scope string) falls under
    some entry already in `ceiling`.

    Containment is prefix match on a `/`-bounded segment, not raw `startswith`
    — "path:$HOME/foo" must not cover "path:$HOME/foobar". The kind prefix
    on both sides is what keeps a path entry from ever covering a doc or
    issue object: different kinds produce strings that cannot be a prefix of
    one another. This is a plain string comparison and relies entirely on
    both `ceiling` and `object_ref` already having passed through `scope()`'s
    PATH normalization — this function does no normalization itself.

    `FULL_CEILING` covers every object, checked first and before the loop
    below ever runs a prefix match against it.
    """
    if FULL_CEILING in ceiling:
        return True
    for entry in ceiling:
        if object_ref == entry:
            return True
        prefix = entry if entry.endswith("/") else entry + "/"
        if object_ref.startswith(prefix):
            return True
    return False


def ceiling_text_from_entries(entries: Iterable[str]) -> str:
    """Serialize a set of `scope()` strings into the shape registration's
    opaque `ceiling` TEXT column stores (platform/decisions/0092 Handoff):
    a JSON array, sorted for a stable, diffable value. The empty set
    serializes to `"[]"` — a ceiling whose entries match nothing, distinct
    from `FULL_CEILING`'s `"full"` text form below."""
    return json.dumps(sorted(entries))


def ceiling_entries_from_text(raw: str) -> frozenset[str]:
    """Parse registration's stored `ceiling` TEXT column into the
    `frozenset[str]` `SessionGrant.ceiling` expects — the vocabulary that
    opaque string carries (platform/decisions/0092 Handoff: "`ceiling` is
    stored as an opaque string you give meaning to"). Two shapes:

    - the literal text `"full"`: the sentinel for an unrestricted ceiling.
      Returned as `frozenset({FULL_CEILING})`, which `ceiling_covers`
      special-cases to cover every object.
    - a JSON array of `scope()` strings (`ceiling_text_from_entries`'s own
      output, e.g. `'["path:/work/allowed"]'`): the ceiling covers whatever
      those entries match, checked by `ceiling_covers`. How much that is
      is not visible from the array's shape — an entry like `"path:/"`
      matches every absolute path, so an array can cover as much as
      `"full"` does for that kind. Only `ceiling_covers`, never the shape
      of what is stored, says how much a ceiling grants.

    `FULL_CEILING` (`"*"`) is refused as a member of the JSON array, so the
    exact sentinel string has one spelling rather than two (round-2
    security-review suggestion on E12 slice 3b). This does not make an
    array narrower than `"full"`: it removes one special-case spelling of
    "everything" from the array shape, not the broader entries that can
    still match everything (round 3). A registration carrying `FULL_CEILING`
    inside the array is refused as `malformed_ceiling` before it is ever
    stored (`handle_registration` already wraps this function in a
    try/except for exactly this ValueError).

    Raises ValueError for anything else — unparseable JSON, JSON that
    parses but is not an array of strings, or an array containing
    `FULL_CEILING` — so the caller denies the call this ceiling would
    otherwise govern, rather than guessing at a meaning for a value this
    vocabulary does not define.
    """
    if raw == "full":
        return frozenset({FULL_CEILING})
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"ceiling is neither 'full' nor valid JSON: {e}") from e
    if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
        raise ValueError(f"ceiling JSON must be an array of strings: {raw!r}")
    if FULL_CEILING in parsed:
        raise ValueError(
            f"ceiling JSON array must not contain {FULL_CEILING!r} — the literal "
            f"text 'full' is the one spelling of the unrestricted sentinel: {raw!r}")
    return frozenset(_normalize_ceiling_entry(v) for v in parsed)


# ---------------------------------------------------------------------------
# The classifier (platform/decisions/0057 / ADR-0057).
# ---------------------------------------------------------------------------

NAMES_TARGET = "names_target"
EXECUTES = "executes"

# Each entry: tool name -> (ceiling kind, the input keys that carry the
# object, tried in order). Only tools this module can confidently resolve to
# one named object are listed; everything else classifies as EXECUTES by the
# default in `classify()` below — including every tool invented after this
# module was written, which is the point of a default rather than a negative
# list.
_NAMES_TARGET_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Read": (PATH, ("file_path",)),
    "Edit": (PATH, ("file_path",)),
    "Write": (PATH, ("file_path",)),
    "NotebookEdit": (PATH, ("notebook_path", "file_path")),
    # A document fetch: the Library returns one document for one id.
    "mcp__kb__kb_get": (DOC, ("id",)),
    "mcp__kb__kb_checkout": (DOC, ("id",)),
    # A work-item claim or read: one issue, named by id, before anything runs.
    "mcp__ops__issue_checkout": (ISSUE, ("issue_id",)),
    "mcp__ops__issue_claim": (ISSUE, ("issue_id",)),
    "mcp__ops__issue_get": (ISSUE, ("issue_id",)),
}


def classify(tool_name: str, tool_input: dict | None = None) -> str:
    """NAMES_TARGET or EXECUTES, per platform/decisions/0057.

    `tool_input` is accepted (and part of the documented signature — "using
    the tool name and input as they appear in a run's tool_calls.jsonl") for
    forward compatibility with a tool whose classification could someday
    depend on its arguments rather than its name alone; today every member of
    `_NAMES_TARGET_TOOLS` is classified by name only. Any tool not in that
    table — including one this module has never heard of — is EXECUTES: an
    unrecognised request gets no ceiling exemption.
    """
    return NAMES_TARGET if tool_name in _NAMES_TARGET_TOOLS else EXECUTES


def extract_object(tool_name: str, tool_input: dict | None) -> str | None:
    """The one object a names-its-target request names, as a ceiling scope
    string, or None if the tool is unrecognised or its input carries none of
    the expected keys.

    Raises ValueError, via `scope()`'s PATH normalization, if a path-carrying
    tool's value is relative or contains a `..` segment — `decide()` turns
    that into a denial rather than letting an unjudgeable path reach
    `ceiling_covers`. This applies to every key a path-carrying tool is
    checked under (e.g. NotebookEdit's `notebook_path` and its `file_path`
    fallback), not only the first.
    """
    spec = _NAMES_TARGET_TOOLS.get(tool_name)
    if spec is None:
        return None
    kind, keys = spec
    tool_input = tool_input or {}
    for key in keys:
        value = tool_input.get(key)
        if value:
            return scope(kind, str(value))
    return None


# ---------------------------------------------------------------------------
# The session grant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionGrant:
    """What one dispatched session may touch, declared at registration
    (platform/decisions/0057, "The session declares its ceiling at
    registration").

    `ceiling` binds object-scoped requests only. It does not and cannot bind
    an executing request — that half's object set is discovered by running
    it, which is exactly why stage one is a policy and audit layer rather
    than a containment boundary (see module docstring).

    Every PATH-kind entry in `ceiling` is normalized through the same
    `_normalize_absolute_path` the request side uses, and construction raises
    ValueError for a relative entry or one containing a `..` segment — at
    this point, not deferred to the first comparison — regardless of whether
    the entry arrived via `scope()` or was built some other way. An entry
    widening the ceiling via `..` is refused rather than stored.
    """

    session_id: str
    ceiling: frozenset[str] = frozenset()
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = frozenset(_normalize_ceiling_entry(entry) for entry in self.ceiling)
        object.__setattr__(self, "ceiling", normalized)


# ---------------------------------------------------------------------------
# The agent permission set — already resolved by the caller via the
# Library's /policy/resolve (lib/agent-platform/policy_client.py). This
# module never fetches it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionSet:
    """The tool names this agent is denied outright, independent of the
    ceiling or the request's class. `denied` entries may end in `*` for a
    prefix match (an MCP server's whole surface, e.g. `mcp__slack__*`), the
    same convention `policy_client`/`tool_policy` already use."""

    denied: frozenset[str] = frozenset()

    def denies(self, tool_name: str) -> bool:
        for pattern in self.denied:
            if pattern == tool_name:
                return True
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return True
        return False


def permissions_from_policy_record(record: dict[str, Any] | None) -> PermissionSet:
    """Build a `PermissionSet` from an already-resolved policy record — the
    shape `tool_policy.Policy.as_dict()` writes to a run's `policy.json`, or
    the Library's own `/policy/resolve` record once a caller has merged its
    denials (`policy_client.explicit_denials` / `categorical_denials`). Only
    `denied` is read; this function does not fetch or validate the record."""
    if not isinstance(record, dict):
        return PermissionSet()
    denied = record.get("denied") or ()
    return PermissionSet(denied=frozenset(str(d) for d in denied))


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectScopedAuthorization:
    """Allow, for one object, one call."""

    object_ref: str


@dataclass(frozen=True)
class BoundaryGrant:
    """Allow, for an executing call whose object set is discovered by running
    it. NOT a containment boundary (see module docstring) — this names the
    tool the grant was issued for, nothing more."""

    tool_name: str


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    authorization: "ObjectScopedAuthorization | BoundaryGrant | None" = None


def decide(request: dict[str, Any], grant: SessionGrant, permissions: PermissionSet,
          now: datetime) -> Decision:
    """Allow or deny one tool call, per platform/decisions/0057.

    `request` is a single `tool_calls.jsonl` entry's shape: at minimum
    `{"tool_name": ..., "input": {...}}`. Order of checks, each short-circuiting:

    1. Revocation. Checked on every call, before anything else — "the Bosun
       is on the path for every call, so a revocation check there is nearly
       free" (platform/decisions/0057). A revoked session is denied
       regardless of class or ceiling.
    2. The agent's own permission set. A tool the Library already denied this
       agent is denied here too, regardless of class or ceiling.
    3. Classification. An executing request gets a boundary grant and is
       never checked against the ceiling.
    4. An object-scoped request is denied if its tool's input carries no
       extractable object, if that object is a path this module cannot judge
       lexically safe (relative, or containing a `..` segment — see the
       module docstring's caller contract on why a `..` is refused rather
       than resolved), or if the object falls outside the grant's declared
       ceiling.
    """
    tool_name = request.get("tool_name", "")
    tool_input = request.get("input") or {}

    if grant.revoked_at is not None and now >= grant.revoked_at:
        return Decision(False, f"session {grant.session_id!r} revoked at "
                               f"{grant.revoked_at.isoformat()}")

    if permissions.denies(tool_name):
        return Decision(False, f"{tool_name!r} is denied by the agent's permission set")

    kind = classify(tool_name, tool_input)

    if kind == EXECUTES:
        return Decision(
            True,
            "executing call: boundary grant, object set discovered by execution, "
            "not checked against the ceiling",
            BoundaryGrant(tool_name=tool_name),
        )

    try:
        object_ref = extract_object(tool_name, tool_input)
    except ValueError as e:
        return Decision(False, f"{tool_name!r} names an object path this module "
                               f"refuses to judge lexically: {e}")
    if object_ref is None:
        return Decision(False, f"{tool_name!r} names a target but no object could be "
                               f"read from its input")
    if not ceiling_covers(grant.ceiling, object_ref):
        return Decision(False, f"{object_ref} is outside the session's declared ceiling")
    return Decision(True, "object-scoped authorization",
                     ObjectScopedAuthorization(object_ref=object_ref))


# ---------------------------------------------------------------------------
# Replay — feeding a recorded run's tool_calls.jsonl through classify()/
# decide() and counting the result. Pure: the caller reads the files
# (bin/agentctl's `authz replay`) and hands the already-parsed entries in.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplaySummary:
    total: int
    by_class: dict[str, int] = field(default_factory=dict)
    by_verdict: dict[str, int] = field(default_factory=dict)
    by_class_verdict: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_class": dict(self.by_class),
            "by_verdict": dict(self.by_verdict),
            "by_class_verdict": dict(self.by_class_verdict),
        }


def replay(requests: Iterable[dict[str, Any]], grant: SessionGrant,
          permissions: PermissionSet, now: datetime) -> ReplaySummary:
    """Run every entry in `requests` through `decide()` and count by
    classification and verdict. Read-only over its arguments; writes
    nothing."""
    by_class: Counter[str] = Counter()
    by_verdict: Counter[str] = Counter()
    by_class_verdict: Counter[str] = Counter()
    total = 0
    for req in requests:
        total += 1
        tool_name = req.get("tool_name", "")
        tool_input = req.get("input") or {}
        kind = classify(tool_name, tool_input)
        decision = decide(req, grant, permissions, now)
        verdict = "allow" if decision.allow else "deny"
        by_class[kind] += 1
        by_verdict[verdict] += 1
        by_class_verdict[f"{kind}/{verdict}"] += 1
    return ReplaySummary(total=total, by_class=dict(by_class), by_verdict=dict(by_verdict),
                         by_class_verdict=dict(by_class_verdict))


def grant_from_meta(meta: dict[str, Any]) -> SessionGrant:
    """A `SessionGrant` inferred from a run's own `meta.json` — the shape the
    replay command builds, per issue_01M174BDTWWJCDMN8GMJVCWXQ4.

    A MEASUREMENT PROXY, NOT A REAL DECLARED CEILING. No run registers a
    session or declares a ceiling today (sibling issue_01M174B5JA52ZZXTAAJEG2E7EB
    is unbuilt), so the only scoped fact `meta.json` already commits to is the
    run's own worktree. A real ceiling would also name the documents and work
    items the session is entitled to touch; this proxy names none of that, so
    replaying a real run's `mcp__kb__kb_get` / `mcp__ops__issue_get` calls
    against it will mostly come back denied. That is a property of the proxy,
    not a finding about the agents replayed — see the PR body for the
    measured numbers and this caveat restated against them.
    """
    ceiling: set[str] = set()
    worktree = meta.get("worktree")
    if worktree:
        ceiling.add(scope(PATH, str(worktree)))
    return SessionGrant(session_id=str(meta.get("run_id", "")),
                        ceiling=frozenset(ceiling))
