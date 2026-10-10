"""Stamped-review protocol — the trustworthy review record (ARCH-184 phase 2).

A review verdict posted to a PR carries a machine-parseable header so the merge
gate (`agentctl review-status`) can trust it without trusting any agent's
narration: the exact commit reviewed, the verdict, which reviewer, and the
dispatched run it came from. The header is an HTML comment (invisible in
rendered markdown) plus a human-visible line.

The authoritative fields — `reviewed-commit` and `run-id` — are stamped by
`agentctl review-post` from the harness env the Phase-1 pin set, NOT typed by the
agent. So an APPROVE can never claim a commit it didn't review, and the gate
counts an APPROVE only when its `reviewed-commit` equals the PR's current head.
That is what makes a stale review (PA's near-miss) or a coder's "merge anyway"
claim unmergeable by construction.

Authenticity (ARCH-193): because every agentctl run is dispatched
`bypassPermissions`, the per-agent `tools:` scoping is not actually enforced — any
agent can post a `gh pr comment` with a hand-written APPROVE stamp. So the gate
also `authenticate()`s each stamp: its `run-id` must resolve to a dispatch run
whose meta.json independently records a review-class agent, the same repo, and the
same pinned reviewed-commit. A worker forging a stamp fails because its own run is
not a review-class run, and a real reviewer's run-id replayed onto a newer head
fails the commit-binding check.

The posted record (CQ·399): `review-post` writes the verdict and PR it posted into
the run's meta.json. A stamp edited to another verdict, or copied onto another PR,
fails the match against that record, and so does a run whose record has neither
field.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import telemetry as T

PROTOCOL_VERSION = "1"
_MARKER = "agentctl-review"
VERDICTS = ("APPROVE", "REQUEST_CHANGES")

# Token embedded in the commit-binding rejection so callers can recognise that
# rejection by symbol rather than by the message's prose: the head-advance
# disambiguation (issue_01KYRQBNXRHB2PT8BKKNEW745V) keys off it, and a reword of
# the surrounding text must keep the token in place or the disambiguation
# silently stops firing. `bin/agentctl` carries a bound copy (its
# `_STALE_REPLAY_MARKER`); a sync test keeps the two equal.
STALE_REPLAY_MARKER = "stale replay"

_BLOCK_RE = re.compile(rf"<!--\s*{_MARKER}\s*(.*?)-->", re.DOTALL)


@dataclass
class ReviewStamp:
    version: str
    pr: str
    repo: str
    reviewed_commit: str
    verdict: str
    agent: str
    run_id: str
    # True when this APPROVE was recorded with --ignore-ci, i.e. the ARCH-345 CI gate
    # was bypassed and the reviewed commit's CI was never verified green. Absent on
    # older stamps, which is why it defaults False rather than being required.
    ci_ignored: bool = False
    # True when this review was posted with --ignore-dispatchable (ARCH-392): the repo
    # WAS an agentctl dispatch target, so a verified review was available, and the
    # unverified path was taken anyway. Same reason ci_ignored exists — an auditor
    # reading stamps must be able to tell a bypass from a legitimate case without
    # parsing prose, and on a run-id-less stamp there is no run dir to ask instead.
    dispatchable_ignored: bool = False
    # The PR's base branch at the moment this stamp was posted (round 3 of #732,
    # finding 4; platform/specs/builder-runs-as-two-accounts Handoff: "Stamp header
    # on the PR comment keeps today's fields plus `reviewed_base`"). Absent on
    # every stamp posted before this field existed, which is why it defaults to
    # the empty string rather than being required — `review-launch` is the first
    # (and so far only) poster that fills it in.
    reviewed_base: str = ""
    # The authenticated run's pinned full `review_sha`, set by `bin/agentctl`'s
    # `_authenticated_stamps` from the run's meta.json and never parsed from the comment, so
    # a comment cannot name it. Empty when the stamp has no authenticated run or the run pins
    # no sha. The merge gate reads a stamp's head from this pin, not from `reviewed_commit`.
    pinned_sha: str = ""


def render_header(*, pr: str, repo: str, reviewed_commit: str, verdict: str,
                  agent: str, run_id: str, ci_ignored: bool = False,
                  dispatchable_ignored: bool = False, reviewed_base: str = "") -> str:
    """The HTML-comment stamp prepended to a posted review. Invisible in render.

    `ci_ignored` marks an APPROVE recorded with --ignore-ci (ARCH-345). Without it the
    stamp of a CI-bypassed APPROVE is byte-identical to a legitimately-gated one, so
    the escape hatch on the integrity control leaves no durable trace — the review that
    found this said it plainly, and an audit trail you have to have been watching the
    console for is not an audit trail.

    `dispatchable_ignored` marks a review posted with --ignore-dispatchable (ARCH-392),
    by the same argument: the visible banner tells a human, and this tells a machine.

    `reviewed_base` (round 3 of #732, finding 4) is the PR's base branch as read at
    post time — `review-launch` is the first poster to supply it, so the field is
    omitted (not a blank line) when a caller leaves it unset, the same convention
    `ci_ignored`/`dispatchable_ignored` already use.
    """
    return (
        f"<!-- {_MARKER}\n"
        f"v: {PROTOCOL_VERSION}\n"
        f"pr: {pr}\n"
        f"repo: {repo}\n"
        f"reviewed-commit: {reviewed_commit}\n"
        f"verdict: {verdict}\n"
        f"agent: {agent}\n"
        f"run-id: {run_id}\n"
        + ("ci-ignored: true\n" if ci_ignored else "")
        + ("dispatchable-ignored: true\n" if dispatchable_ignored else "")
        + (f"reviewed-base: {reviewed_base}\n" if reviewed_base else "")
        + "-->"
    )


def parse(body: str) -> ReviewStamp | None:
    """Parse the first review stamp out of a comment body, or None if absent /
    malformed / carrying an unknown verdict (an unparseable stamp is no stamp —
    the gate must not count it)."""
    m = _BLOCK_RE.search(body or "")
    if not m:
        return None
    kv: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        kv[k.strip()] = v.strip()
    verdict = kv.get("verdict", "").upper()
    if verdict not in VERDICTS:
        return None
    return ReviewStamp(
        version=kv.get("v", ""),
        pr=kv.get("pr", ""),
        repo=kv.get("repo", ""),
        reviewed_commit=kv.get("reviewed-commit", ""),
        verdict=verdict,
        agent=kv.get("agent", ""),
        run_id=kv.get("run-id", ""),
        ci_ignored=kv.get("ci-ignored", "").strip().lower() == "true",
        dispatchable_ignored=(
            kv.get("dispatchable-ignored", "").strip().lower() == "true"),
        reviewed_base=kv.get("reviewed-base", ""),
    )


@dataclass
class RunFacts:
    """What meta.json records about the dispatch run a stamp claims to come from.
    `found` is False when the stamp's run-id resolves to no run dir at all.

    `pr` and `started_at` exist for `run-register-api`'s `/runs/<run_id>/facts`
    route (ARCH-193 phase 2 — the Library's review-record write route), which
    reads the same meta.json this dataclass already projects; `authenticate()`
    below reads neither.

    `run_dir` is the directory `resolve_run_facts` read meta.json from, so a
    caller that also reads a file in the run directory can use this one
    resolution rather than look the run-id up a second time. None when found
    is False.

    `posted_verdict` and `posted_pr` are what `review-post` recorded in meta.json
    when the run posted (`review_verdict`, `review_pr`; CQ·399). Empty for a run that
    has not posted and for every run that predates those fields. `authenticate`
    refuses a stamp against an empty value."""
    found: bool
    agent: str = ""
    repo: str = ""
    review_sha: str = ""
    pr: "int | str | None" = None
    started_at: str = ""
    run_dir: "Path | None" = None
    posted_verdict: str = ""
    posted_pr: str = ""


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AmbiguousRunDir(Exception):
    """`find_run_dir` raises this when `run_id` resolves under more than one
    date directory, instead of silently returning the first match sorted
    newest-first (issue_01M1JWB7M75SZ160VRJRCGARBF). Every run is dispatched
    as the same OS user that owns `runs_root`, so any worker can
    `mkdir <root>/2099-01-01/<run-id>` and plant a `meta.json` there — a
    directory that sorts ahead of the real one and, under the old
    first-match behaviour, silently won every lookup: `agentctl status`,
    `logs`, `publish`'s committed_not_pushed promotion, run-cost paths, and
    the merge gate's review-stamp authentication. A resolver that picks one
    of several candidates can't tell the planted one from the real one, so
    it must refuse instead.

    `dirs` is every matching directory found, sorted for a deterministic
    message; `run_id` is the id that was ambiguous."""

    def __init__(self, run_id: str, dirs: "tuple[Path, ...]"):
        self.run_id = run_id
        self.dirs = dirs
        super().__init__(
            f"run-id {run_id!r} resolves to {len(dirs)} run directories: "
            + ", ".join(str(d) for d in dirs))


def find_run_dir(run_id: str, runs_root: "Path | None" = None) -> "Path | None":
    """Resolve `run_id` to its run directory under `runs_root` (default
    `telemetry.RUNS_ROOT`): None if it doesn't resolve anywhere, the one
    match if it resolves under exactly one date directory, or raises
    `AmbiguousRunDir` if it resolves under more than one — never picks one of
    several candidates (issue_01M1JWB7M75SZ160VRJRCGARBF).

    THE SHAPE CHECK RUNS BEFORE ANY FILESYSTEM ACCESS. A run-id reaches this
    from untrusted free text — a review stamp's `run-id` field (ARCH-193), or
    a run-register-api URL path segment — so `../`, an encoded traversal, an
    absolute path, or an empty string is rejected before the date-dir glob
    below ever runs, rather than being joined into a path that could walk
    outside `runs_root`. Real run-ids (`run-<hex>`) and run-dir names carry no
    dots or separators, so this rejects only malformed/hostile input.

    THE ONE RUN-ID RESOLVER, shared by `bin/agentctl` (the merge gate's stamp
    authentication) and `bin/run-register-api` (the `/runs/<run_id>/facts`
    route the Library reads for the same purpose) — both need to agree on
    which run directory a run-id names, or the two could authenticate a stamp
    against a run the other would have refused to resolve at all. Fixing the
    ambiguity check here fixes it for every caller of either at once.
    """
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    root = runs_root if runs_root is not None else T.RUNS_ROOT
    matches = tuple(sorted(
        candidate for date_dir in root.glob("20*-*-*/")
        if (candidate := date_dir / run_id).is_dir()))
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousRunDir(run_id, matches)
    return matches[0]


# meta.json under a real run directory is tens of KB at most — the largest
# found under `<root>/agent-runs` (2026-09-15 measurement, 3300+ runs) is
# 29224 bytes (run-2d90676b, 2026-07-29). 1 MiB is roughly 36x that, and
# matches bin/run-observer's own `_MAX_META_BYTES` for the same file read out
# of the same run-writable directory by that daemon's unattended sweep.
_MAX_META_BYTES = 1024 * 1024


def _open_meta_nofollow(meta_path: Path) -> "int | None":
    """A read fd for `meta_path`, or None unless it is (and stays) a plain
    regular file — never raises.

    THE SAME PATTERN `bin/run-observer`'s `_open_regular_file` ALREADY APPLIES
    to this same file in the same run directory, for the same reason: a run's
    directory is writable by that run, so `meta.json` is not guaranteed to be
    a regular file by the time anything reads it. `os.lstat` rejects a
    symlink, FIFO, socket, device or directory before any `open()` is
    attempted at all, so an adversarial replacement is refused without a
    syscall that could block or follow it. `O_NOFOLLOW` then closes the
    TOCTOU window between that `lstat` and the `open` (a symlink swapped in
    during that window), and `O_NONBLOCK` keeps a FIFO that reappears in the
    same window from blocking the open (opening a FIFO for read is normally
    non-blocking regardless, but this makes it explicit rather than relying
    on that default). `os.fstat` on the resulting descriptor re-checks
    `S_ISREG` for the same reason `run-observer`'s docstring gives:
    `O_NOFOLLOW` refuses a symlink and nothing else, so this is the second,
    file-descriptor-bound half of the same check the `lstat` already made by
    name.
    """
    try:
        st = meta_path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(meta_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _read_meta_bounded(meta_path: Path) -> "dict | None":
    """`meta_path`'s JSON content as a dict, or None — never raising, and
    refusing anything a run's own writable directory could turn into a
    denial-of-service against the reader.

    `resolve_run_facts` (below) feeds two callers that must not go down over
    this: `bin/agentctl`'s merge-gate authenticator (a one-shot CLI, where the
    exposure was always bounded to that one invocation) and
    `bin/run-register-api`'s `/runs/<run_id>/facts` route — the one KeepAlive
    daemon in this chain, serving every crew. Before this function existed,
    `resolve_run_facts` called `telemetry.read_meta`, which is a plain
    `Path.read_text()`: a run can `rm -f meta.json` in its own directory and
    replace it with a symlink to `/dev/zero` (unbounded read, exhausting the
    daemon's memory), a FIFO with no writer (a blocking read that hangs the
    handler thread forever), or a many-gigabyte regular file, and one GET
    against that run-id would take the whole daemon down for every crew it
    serves. `_open_meta_nofollow` above refuses the symlink/FIFO/socket/
    device/directory cases outright; this function adds the size cap and the
    parse.

    CAPPED AT `_MAX_META_BYTES`: reads one byte past the cap so a file
    exactly at the boundary is not mistaken for one under it, then rejects
    anything that reached the cap as unreadable rather than truncating and
    parsing a partial JSON document (which would fail to parse anyway, for a
    well-formed file past the cap — but a file crafted to parse as valid JSON
    up to exactly the cap is not a case worth trusting either).

    Any exception — including a `RecursionError` from deeply-nested JSON,
    which is not a `ValueError` and would otherwise escape a narrower catch —
    degrades to `None`. Both callers already treat that as "cannot confirm,"
    never as an error to propagate: `resolve_run_facts` turns it into
    `found=False`, and the Library stores a review record it cannot verify
    this way as `unverified`, the same fail-closed answer an unknown run-id
    already produces.
    """
    fd = _open_meta_nofollow(meta_path)
    if fd is None:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(_MAX_META_BYTES + 1)
        if len(raw) > _MAX_META_BYTES:
            return None
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — any failure here degrades to unreadable
        return None
    return data if isinstance(data, dict) else None


def resolve_run_facts(run_id: str, runs_root: "Path | None" = None) -> RunFacts:
    """Read back what meta.json records about the dispatch run `run_id` names
    (ARCH-193): its agent, repo, pinned review-sha, PR and start time.
    Returns `found=False` for an empty/unknown/malformed run-id, a meta.json
    that is not (or stops being) a plain regular file, an oversized meta.json,
    or unreadable/unparseable meta.json — never raises on bad input, since
    both callers (the merge gate's stamp authenticator, and the Library's
    run-verified check) must fail closed rather than 500 on a forged or
    hostile run-id.

    A NON-REGULAR meta.json (a symlink, FIFO, socket, device or directory
    left where a run's real meta.json should be) is reported exactly like any
    other unreadable meta.json — `found=False` — rather than a distinct
    result. There is no third state a caller could do anything different
    with: the Library already stores anything not run-verified as
    `unverified`, and the merge gate already treats an unresolvable run-id as
    an unverifiable stamp, so a finer-grained "found but unreadable" would be
    a distinction with no consumer.

    READS THROUGH `_read_meta_bounded`, NOT `telemetry.read_meta` — see that
    function's docstring for why a plain `Path.read_text()` on a file inside a
    run's own writable directory is a denial-of-service surface for
    `bin/run-register-api`'s KeepAlive daemon, and why this must not change
    `telemetry.read_meta` itself (kept exactly as `read_meta` for its many
    other one-shot-process callers, where an unbounded read of an adversarial
    file was never the exposure this closes).

    THE ONE LOADER, shared by `bin/agentctl`'s `_resolve_run_facts` (feeding
    `authenticate()`) and `bin/run-register-api`'s `_facts_payload` (feeding
    the Library's write route) — see `find_run_dir`'s docstring for why a
    second copy of either function would let the two disagree about a run's
    facts. `bin/agentctl`'s merge gate gains the same DoS protection as a
    side effect of sharing this loader, not from any change of its own.

    AN AMBIGUOUS `run_id` (found under more than one date directory —
    `find_run_dir` raises `AmbiguousRunDir` rather than picking one) also
    degrades to `found=False`: both callers already treat "cannot confirm"
    as `unverified`/unauthenticated, and picking one of the candidates here
    is exactly the bug (issue_01M1JWB7M75SZ160VRJRCGARBF) — the merge gate
    and the Library's write route must refuse an ambiguous stamp, not
    authenticate it against whichever directory happened to be planted.
    """
    if not run_id:
        return RunFacts(found=False)
    try:
        d = find_run_dir(run_id, runs_root)
    except AmbiguousRunDir:
        return RunFacts(found=False)
    if d is None:
        return RunFacts(found=False)
    meta = _read_meta_bounded(d / "meta.json")
    if meta is None:
        return RunFacts(found=False)
    return RunFacts(found=True, agent=str(meta.get("agent") or ""),
                    repo=str(meta.get("repo") or ""),
                    review_sha=str(meta.get("review_sha") or ""),
                    pr=meta.get("pr"),
                    started_at=str(meta.get("started_at") or ""),
                    run_dir=d,
                    posted_verdict=str(meta.get("review_verdict") or ""),
                    posted_pr=str(meta.get("review_pr") or ""))


# The internal-app crew prefixes. Must equal agentctl._INTERNAL_APP_PREFIXES, which
# test_agentctl_and_review_protocol_prefixes_match enforces. (Two literals rather
# than one shared constant on purpose — agentctl imports its libs from the deployed
# ~/.local/lib path, so a branch's agentctl gets MAIN's review_protocol; see the
# note at agentctl's copy. The test is what holds them together across that seam.)
#
# They drifted once (2026-07-12): agentctl's list had grown `hollis-`, while this
# module's `_review_base` still stripped only `pa-`. So `_review_base` returned
# "hollis-reviewer" unchanged, that name isn't in `review_agents`, and the ARCH-193
# authenticity gate — the check of record for a review stamp — rejected EVERY Hollis
# review stamp as "not a review-class agent". Silently, on a live crew.
#
# When you add a crew, add its prefix in BOTH places, in the same commit. Since
# 2026-09-06 agentctl's `spawn` refuses a crew variant whose prefix is in neither,
# so a forgotten crew fails at its first dispatch rather than writing into a
# shared checkout (`platform-` was the second crew to do that).
INTERNAL_APP_PREFIXES = ("hollis-", "svc-", "sel-", "pa-", "deck-", "orient-", "platform-",
                         "wb-", "sig-", "archy-")

# Review-class agents (role base, after stripping an internal-app prefix). Must
# equal agentctl._REVIEW_AGENTS, which test_agentctl_and_review_protocol_review_
# agents_match enforces — same seam as INTERNAL_APP_PREFIXES above, and for the
# same reason (agentctl imports its libs from the deployed ~/.local path, so a
# branch's agentctl gets MAIN's review_protocol). `authenticate()` below takes
# this as a caller-supplied parameter rather than reading it directly, so
# agentctl keeps passing its own copy; this module's copy exists for any other
# caller (`bin/run-register-api`'s `/runs/<run_id>/facts` route) that needs the
# same review-class test and has no reason to own a third copy of the list.
REVIEW_AGENTS = ("reviewer", "security-reviewer")


def _review_base(agent_name: str) -> str:
    """Role base for review-class membership: an internal-app variant
    (`hollis-reviewer`, `svc-security-reviewer`, the legacy `pa-reviewer`)
    reviews the same as its product counterpart (ARCH-177)."""
    for p in INTERNAL_APP_PREFIXES:
        if agent_name.startswith(p):
            return agent_name[len(p):]
    return agent_name


def is_review_class(agent_name: str, review_agents: tuple[str, ...]) -> bool:
    """Whether `agent_name` is a review-class agent: its role base (an
    internal-app prefix stripped, per `_review_base`) is a member of
    `review_agents`. This is exactly the membership test `authenticate()`
    applies to a stamp's run facts — extracted here so a second caller (the
    `/runs/<run_id>/facts` route's `review_role` field) can ask the same
    question without re-implementing it or risking the two drift apart."""
    return _review_base(agent_name) in review_agents


def _repo_basename(repo: str) -> str:
    """The bare repo name, tolerant of the owner prefix. A reviewer sources the
    stamp's repo from `gh pr view` as `convivy/constance` (owner/name), but the
    run registry records the short `--on` form `constance`; without this the
    authenticity compare `!=`'d those and failed EVERY overnight PR despite a valid
    APPROVE (ARCH-198 stamp-format bug). Basename comparison keeps the cross-repo
    -replay guard intact (Convivy repo names are unique) while accepting both
    forms. `convivy/constance` and `constance` → `constance`."""
    return repo.rsplit("/", 1)[-1]


_MIN_SHA_PREFIX = 7  # git's minimum default abbreviation length


def _sha_match(a: str, b: str) -> bool:
    """Two SHA identifiers refer to the same commit if one is a prefix of the other
    (git abbreviations are valid commit identifiers). Both must be non-empty — an
    empty string must not become a wildcard. A prefix shorter than _MIN_SHA_PREFIX
    characters is rejected; a 1-character coincidence must not match.
    """
    if not a or not b:
        return False
    if len(a) < _MIN_SHA_PREFIX or len(b) < _MIN_SHA_PREFIX:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def authenticate(stamp: ReviewStamp, facts: RunFacts, *, pr: str,
                 review_agents: tuple[str, ...],
                 head_advanced: bool | None = None) -> str | None:
    """Confirm a posted stamp is authentic for PR `pr`, the PR the caller is
    evaluating, or return a human-readable reason it can't be trusted.

    Two halves, both required. `authenticate_run` proves the stamp came from a
    review-class run on this repo at its pinned commit (ARCH-193).
    `_posted_record_mismatch` proves the stamp carries the verdict and PR its run
    recorded when it posted, and that the PR is `pr` (CQ·399). `pr` has no default,
    so a caller cannot leave the PR unchecked. Returns None iff both halves pass.
    """
    reason = authenticate_run(stamp, facts, review_agents=review_agents,
                              head_advanced=head_advanced)
    if reason is not None:
        return reason
    return _posted_record_mismatch(stamp, facts, pr=pr)


def _posted_record_mismatch(stamp: ReviewStamp, facts: RunFacts, *, pr: str) -> str | None:
    """The record half of `authenticate` (CQ·399): the stamp's verdict must be the one
    its run recorded at post time, its PR must be the recorded PR, and that PR must be
    `pr`, the PR being evaluated. A record missing either field is unreadable for this
    purpose and refuses every stamp from that run.

    THE LIMIT: the record is meta.json, which any process running as this user can
    write. Such a process with Bash can get a counted APPROVE that no reviewer posted,
    by claim-first (`review-post --run-id` naming the reviewer's run before it posts) or
    by writing `review_verdict` and `review_pr` into that run's meta.json and then
    posting a stamp. This check does not see either route; it passes whatever the record
    holds. What this closes is the copy: a stamp edited to another verdict, or a genuine
    stamp copied onto a different PR at the same commit.
    """
    if not facts.posted_verdict or not facts.posted_pr:
        return (f"run {stamp.run_id} records no posted verdict or PR, so stamp "
                f"{stamp.verdict} for PR {stamp.pr} cannot be checked — stamp not trusted")
    if stamp.verdict != facts.posted_verdict:
        return (f"stamp verdict {stamp.verdict} != run {stamp.run_id}'s posted verdict "
                f"{facts.posted_verdict} — edited stamp not trusted")
    if str(stamp.pr) != facts.posted_pr:
        return (f"stamp is for PR {stamp.pr}, but run {stamp.run_id} posted on PR "
                f"{facts.posted_pr} — stamp not trusted")
    if str(stamp.pr) != str(pr):
        return f"stamp is for PR {stamp.pr}, not PR {pr} — stamp not trusted"
    return None


def authenticate_run(stamp: ReviewStamp, facts: RunFacts, *,
                     review_agents: tuple[str, ...],
                     head_advanced: bool | None = None) -> str | None:
    """The run-identity half of `authenticate` (ARCH-193): confirm a posted stamp
    provably originates from a real review-class run, or return a human-readable
    reason it can't be trusted.

    Every agentctl run is dispatched `bypassPermissions`, so the `tools:` scoping
    is not enforced and *any* agent — a coder included — physically can post a
    `gh pr comment` carrying a hand-written APPROVE stamp. The merge gate must
    therefore not trust the stamp's self-declared `agent`; it confirms the stamp's
    `run-id` resolves to a dispatch run whose meta.json independently records a
    review-class agent, the same repo, and (when pinned) the same reviewed commit.

    "INDEPENDENTLY" HAS A NARROWER MEANING THAN IT SOUNDS: meta.json is
    written inside the run's OWN directory, by the run it describes — its
    `agent`, `repo` and `review_sha` fields are self-reported, not attested by
    anything outside that run. This check defeats REPLAY (a stamp pointing at
    someone else's run) and CROSS-ROLE stamping by an honest-but-buggy run
    (one role's run-id stamping another role's verdict); it does not defend
    against a run that deliberately rewrites its own meta.json before
    stamping, because no such forging run has been seen and no guard is built
    for a threat nobody has observed (see the project convention on
    speculative guards). So a passing `authenticate_run()` means "meta.json
    currently says this run was a review-class run on this repo at this
    commit," not a cryptographic attestation of it.

    The checks, each defeating a concrete forgery:
      - no run-id            → the stamp is unverifiable (the Agent-tool fallback
                               path has no run dir; it opts in via --allow-unverified).
      - run-id not found     → a fabricated run-id.
      - run's agent not review-class → a worker stamped its OWN run-id (e.g. coder).
      - role mismatch        → a run claimed a role it did not perform, e.g. a
                               `reviewer` run stamping `security-reviewer` to fill
                               both required slots. Compared on `_review_base`, so a
                               crew prefix (`hollis-`) does not count as a mismatch.
      - repo mismatch        → a reviewer run-id replayed from another repo's PR.
      - review_sha mismatch  → a reviewer run-id replayed onto a newer head than it
                               actually reviewed (the stale-replay attack).

    The last check distinguishes two otherwise-identical mismatches by whether the
    PR head advanced past the run's pin during the review (issue_01KYRQBNXRHB2PT8BKKNEW745V).
    A reviewer is pinned to the PR head at spawn; Dependabot (or a push) can move the
    head while the review is in flight, and the reviewer then stamps a commit the run
    was never pinned to. That is a benign race, not a replay — the reviewer read a real
    commit the branch has since superseded. The caller decides it via ancestry and
    passes `head_advanced=True`; the rejection is then reported as a re-dispatch rather
    than a trust violation. `head_advanced` False or None (indeterminate — the caller
    could not prove forward movement) keeps the stale-replay wording: the pin is the
    only record of what the run was dispatched to read, and a stamp naming anything
    else is a replay until shown otherwise.

    `facts.review_sha`/`facts.repo` are skipped when empty so a run from before
    this field existed (or a repo the stamp omitted) degrades to the weaker checks
    rather than failing closed on every legacy review. Returns None iff authentic.
    """
    if not stamp.run_id:
        return "stamp carries no run-id (unverifiable origin)"
    if not facts.found:
        return f"run-id {stamp.run_id} resolves to no dispatch run"
    if not is_review_class(facts.agent, review_agents):
        return (f"run {stamp.run_id} ran agent '{facts.agent}', "
                f"not a review-class agent — stamp not trusted")
    # Compare ROLES, not raw names — `hollis-security-reviewer` and
    # `security-reviewer` are the same reviewer, one deployed under a prefixed crew.
    #
    # THE THIRD INSTANCE OF THIS BUG CLASS (role equality). The membership check
    # directly above already uses `_review_base`; `evaluate_gate` matches both
    # sides on it too (see its docstring, which calls itself the second instance).
    # This equality did not, so an auto-dispatched review that RAN as
    # `hollis-security-reviewer` and STAMPED `security-reviewer` was rejected as
    # inauthentic — and hollis PR #550 carried two genuine APPROVEs while the gate
    # reported "no review posted by security-reviewer" (hollis co, 2026-07-23).
    # Every prefixed crew hits it on the auto-dispatch path.
    #
    # THE FOURTH INSTANCE was the SHA equality seven lines below: the pinned
    # review_sha is always the full 40-char form; a stamp carrying a 9-char git
    # abbreviation never compared equal. The gate discarded the stamp as a stale
    # replay, and since a discarded stamp is absent — not a blocker — a
    # REQUEST_CHANGES silently became a PASS (sous-chef PR #380, 2026-07-27).
    # Fixed with `_sha_match`, which accepts a prefix of at least _MIN_SHA_PREFIX
    # chars. An empty or too-short value still fails; a genuinely different commit
    # still fails. The file has no further un-normalized comparisons.
    #
    # This grants nothing. A run that executed `hollis-reviewer` still cannot stamp
    # as `security-reviewer`: those bases differ and the check still rejects it. Only
    # the prefix — a deployment detail of WHICH crew ran the role — is normalized
    # away, exactly as the two checks either side of it already do.
    if _review_base(facts.agent) != _review_base(stamp.agent):
        return (f"stamp claims agent '{stamp.agent}' but run {stamp.run_id} "
                f"ran '{facts.agent}'")
    if facts.repo and stamp.repo and _repo_basename(facts.repo) != _repo_basename(stamp.repo):
        return f"stamp repo '{stamp.repo}' != run repo '{facts.repo}'"
    if facts.review_sha and not _sha_match(facts.review_sha, stamp.reviewed_commit):
        # THE HEAD-ADVANCE vs THE REPLAY (issue_01KYRQBNXRHB2PT8BKKNEW745V). The pin is
        # taken at spawn; if the PR head then moves forward to a descendant before the
        # reviewer stamps, the stamp names a commit the run was not pinned to and this
        # check fires. That is not a trust violation — the reviewer read a real commit
        # the branch has since superseded — so a caller that PROVED the head advanced
        # (head_advanced=True) gets a re-dispatch message instead of the replay one.
        # False or None (indeterminate) keeps the hard rejection, which is the point of
        # the pin: a stamp naming anything but the pinned commit is a replay until the
        # forward movement is shown.
        if head_advanced:
            return (f"head advanced during review: run {stamp.run_id} was pinned to "
                    f"{facts.review_sha}, and the PR head has since moved forward to a "
                    f"descendant. The review covers a superseded commit; re-dispatch "
                    f"the reviewer against the new head.")
        return (f"stamp reviewed-commit {stamp.reviewed_commit or '?'} != "
                f"run's pinned review-sha {facts.review_sha} "
                f"({STALE_REPLAY_MARKER})")
    return None


@dataclass
class GateResult:
    ok: bool
    head: str
    reasons: list[str] = field(default_factory=list)     # why-not; empty iff ok
    approvals: dict[str, str] = field(default_factory=dict)  # agent -> commit, only for a current APPROVE


def evaluate_gate(*, head: str, stamps: list[ReviewStamp], required_agents: list[str],
                  ci_green: bool) -> GateResult:
    """Pure merge-gate decision (ARCH-184). PASS iff, for EVERY required reviewer,
    the latest stamp is an APPROVE whose reviewed-commit == the PR's current head,
    AND required CI is green. A reviewer whose latest stamp is at an older commit
    is stale (the head moved under it) and fails the gate — the deterministic form
    of 're-review every revision'.

    `stamps` must be in chronological order (oldest first); the last stamp per
    agent wins, so a later REQUEST_CHANGES correctly overrides an earlier APPROVE.

    Both sides are matched on `_review_base`, so an internal-app crew satisfies the
    gate: `sel-security-reviewer`'s APPROVE counts as `security-reviewer`, the same
    equivalence `authenticate` already applies to review-class membership (ARCH-177).
    Without it the gate keyed on the raw name, found none of the required roles, and
    reported "no review posted by reviewer" at a PR carrying two valid APPROVEs —
    which under ARCH-364 also meant `_mark_pr_ready` never fired, so the PR stayed a
    draft and its full suite never ran. Every prefixed crew (hollis, selector, svc,
    legacy pa) was unmergeable by construction. This is the second instance of that
    bug class: see the INTERNAL_APP_PREFIXES note above for the 2026-07-12 drift that
    broke the authenticity check the same way, one layer up.

    This grants no authority. `authenticate` has already proved each stamp came from
    a real review-class run whose meta.json records that same agent and repo, so
    stripping a prefix here only lets a proven reviewer fill the role it holds.

    Raises ValueError on an empty `head` — without a real head, the `reviewed_commit
    == head` equality would pass vacuously for an empty-commit stamp, so the gate
    refuses to decide rather than fail open (the CLI already guards this upstream;
    the guard keeps a direct library caller safe by default too).
    """
    if not head:
        raise ValueError("evaluate_gate: a non-empty head commit is required")

    latest: dict[str, ReviewStamp] = {}
    for s in stamps:
        latest[_review_base(s.agent)] = s

    reasons: list[str] = []
    approvals: dict[str, str] = {}
    for agent in required_agents:
        # `--required sel-reviewer` and `--required reviewer` mean the same role.
        s = latest.get(_review_base(agent))
        if s is None:
            reasons.append(f"no review posted by {agent}")
            continue
        if s.verdict != "APPROVE":
            reasons.append(f"{agent} latest verdict is {s.verdict}, not APPROVE")
        elif not _sha_match(s.reviewed_commit, head):
            reasons.append(
                f"{agent} APPROVE is stale — reviewed {s.reviewed_commit or '?'}, "
                f"head is {head or '?'} (re-review needed)")
        else:
            # `approvals` records only a genuine, current approval (APPROVE at head),
            # so a caller inspecting it on a FAIL never sees a stale/REQUEST_CHANGES stamp.
            approvals[agent] = s.reviewed_commit
    if not ci_green:
        reasons.append("required CI is not green")

    return GateResult(ok=not reasons, head=head, reasons=reasons, approvals=approvals)
