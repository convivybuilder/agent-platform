"""Shared helpers for the three posters that report a run's cost to the
Library's spend ledger (`POST /ledger-entries`): `bin/agentctl`'s live
checkpoint (`_post_live_ledger_delta`), posting while the run is still going;
`bin/agentctl`'s `_finalize_run`, posting the moment it finishes; and
`bin/model-scorecard`'s daily sweep, backstopping any run the other two
missed. One module so the lane rule, the posting contract, the
`"disproportionate"`-refusal record (`record_disproportionate_refusal`,
`refused_figure_unchanged`), and the work-item each post attaches
(`resolve_item_id`) can't drift between the three.

`is_gateway_model`/`is_codex_model` are taken as parameters rather than
imported here, because the source of truth for both (`GATEWAY_MODEL_PREFIXES`,
the Codex-slug pattern) lives in `bin/agentctl`, which is not an importable
module in the ordinary sense (extension-less, loaded by `model-scorecard` via
`SourceFileLoader` instead) — passing the caller's own predicates avoids
either loading agentctl a second time from inside itself or re-deriving the
same rule here, which is exactly the drift this module exists to prevent.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable

import issue_client
import run_sandbox as RS
import telemetry as T


def ledger_lane(model: str | None, *, is_gateway_model: Callable[[str], bool],
                is_codex_model: Callable[[str], bool]) -> str | None:
    """The Library's ledger lane for `model` — "gateway", "subscription",
    "openai-subscription", or None when no model is on the row at all (E1·6;
    the lane for Codex, slice 4 of platform/specs/codex-coder-lane).

    A Codex model (`is_codex_model`) gets its own lane, "openai-subscription",
    rather than sharing "subscription" with the Anthropic subscription: Codex
    runs are billed at OpenAI's API rates (`telemetry.CODEX_BILLING_SOURCE`,
    kept a distinct `billing_source` from both `subscription_credit` and the
    gateway's `provider_api:*` for the same reason — the operator's
    2026-09-23 decision), and sharing "subscription" would sum two separate
    subscriptions' estimates into one figure, the exact cross-lane mixing
    this field exists to prevent. Like "subscription", it never enters the
    Library's `total_usd` or any cap (`store.NON_ENFORCEABLE_LEDGER_LANES`)."""
    if not model:
        return None
    if is_gateway_model(model):
        return "gateway"
    if is_codex_model(model):
        return "openai-subscription"
    return "subscription"


# Recorded on a run's own meta.json, beside `ledger_post_status`, when
# `resolve_item_id` could not even fetch the item corpus to try its PR match
# (the Library was unreachable) — never when the PR cleanly resolved to no
# item or to more than one (those are a plain None, not a failure). Read by
# nothing in this module; a record for a human reading the run's own files,
# the same posture as `REFUSED_USD_FIELD` and friends below.
ITEM_LOOKUP_ERROR_FIELD = "ledger_item_lookup_error"


def resolve_item_id(run_dir: Path | None, *, issue: str | None, repo: str | None,
                    items: list[dict] | None = None,
                    deadline_s: float | None = None) -> str | None:
    """The work item a ledger post should attach for this run: `issue` (meta.
    json's own `--issue`) when given, else the item `run_dir`'s own
    `pr-link.json` resolves to through the SAME PR-match rule
    `cost_join.build_item_costs` applies on the run-cost side
    (`cost_join.item_for_pr`) — so the two sides of E16's ledger-attribution
    split can't drift. Shared by every poster: `bin/agentctl`'s live
    checkpoint and its at-finish post, and `bin/model-scorecard`'s sweep.

    Returns None — never raises — when: `issue` is falsy and `run_dir` is
    None (a synthetic row with no real run directory); `run_dir` has no
    readable `pr-link.json`, or one with neither a `pr_url` nor a
    `pr_number`; the PR it names links zero or more than one item (the same
    "no-link"/"pr-ambiguous" outcomes `build_item_costs` reports); or the
    item fetch itself failed — including a `deadline_s` that ran out before
    the fetch finished. That last case — a failed lookup — is recorded on
    `run_dir`'s own meta.json as `ITEM_LOOKUP_ERROR_FIELD`, best-effort,
    before returning None: a failed lookup must never block or fail the
    caller's own ledger post, only leave the run without an item and a
    record of why.

    `items` lets a caller that already fetched the full item corpus once for
    a whole pass (`model-scorecard`'s sweep, over every row in its window)
    reuse it here instead of this function fetching it again per run; when
    omitted (every `bin/agentctl` caller, each resolving one run at a time),
    this fetches it itself (`issue_client.list_all_items`) — and `deadline_s`
    is ignored, since there is nothing left to fetch.

    `deadline_s`, when `items` is omitted, is a TOTAL seconds budget for that
    fetch (`issue_client.list_all_items` reads seven states, each paginated,
    so a per-request timeout alone leaves the fetch unbounded overall —
    `issue_client.deadline_in`/`list_all_items`'s own docstrings). Converted
    to an absolute deadline once, here, right before the call. `None` (no
    caller before this parameter existed) leaves the fetch unbounded, exactly
    as before; every `bin/agentctl` caller now passes one — see
    `_LIVE_LEDGER_ITEM_LOOKUP_DEADLINE_S`/`_FINISH_ITEM_LOOKUP_DEADLINE_S`
    there for which, and why."""
    if issue:
        return issue
    if run_dir is None:
        return None
    try:
        pr_link = json.loads((run_dir / "pr-link.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(pr_link, dict):
        return None
    pr_url, pr_number = pr_link.get("pr_url"), pr_link.get("pr_number")
    if not pr_url and pr_number is None:
        return None
    if items is None:
        try:
            deadline = (issue_client.deadline_in(deadline_s)
                       if deadline_s is not None else None)
            items = issue_client.list_all_items(deadline=deadline)
        except issue_client.IssueQueueUnavailable as e:
            try:
                T.update_meta(run_dir, **{ITEM_LOOKUP_ERROR_FIELD:
                                          f"{e.__class__.__name__}: {e}"})
            except (OSError, ValueError):
                pass
            return None
    # Imported here, not at module level: `cost_join` does its own
    # `sys.path.insert(0, ~/.local/lib/agent-platform)` at IMPORT time (its own
    # module docstring), and `ledger` is one of the two lib modules the test
    # harness (`tests/conftest.py`) imports before its farm-vs-repo path pin
    # even starts — a module-level import here would make `cost_join`'s own
    # path insert run that early too, ahead of every safeguard conftest.py
    # built for exactly this shape of problem, and poison every sibling
    # `ledger` itself imports right after (`issue_client`, `telemetry`) to the
    # DEPLOYED copy for the rest of the process. Deferred to call time, this
    # resolves `cost_join` the ordinary way, well after that pin has already
    # run.
    import cost_join
    return cost_join.item_for_pr(pr_url, pr_number, repo, items)


# Written to meta.json once a live post is refused `"disproportionate"` — a
# RECORD for a human reading the run's own files, never read back by any of
# this module's own callers (round 2, CQ·446: a field on a run's own
# meta.json is as trustworthy as a number the run types in itself, and the
# one place that must actually stop live posting after a refusal —
# `agentctl._stream_transcript`'s own supervising loop — keeps that flag in
# its own in-memory `live_state` for exactly that reason; see
# `_post_live_ledger_delta`'s docstring there).
LIVE_STOPPED_FIELD = "ledger_live_stopped_status"

# The refusal record every poster writes through `record_disproportionate_
# refusal` below (E1·9, budget-enforcement epic Clause 6: a refusal is
# surfaced against the run that produced it and never resubmitted
# unchanged). `REFUSED_USD_FIELD`/`REFUSED_REASON_FIELD` predate this
# function and keep their names so `agentctl status`'s print and
# `report_to_ledger`'s own error text (`refused_figure_unchanged`'s skip
# reason) keep reading them unchanged; `REFUSED_POSTER_FIELD`/
# `REFUSED_DEDUP_KEY_FIELD` are new, naming which of the three posters hit
# the refusal and the exact dedup_key it was sent under.
#
# DISPLAY ONLY (round 2, security review of PR #1021 round 1, blocking
# finding): these four meta.json fields are written for a human reading the
# run's own files (`agentctl status`) and for `agentctl status` alone. A
# DISPATCHED run holds `AGENTCTL_RUN_DIR` and unrestricted Bash for its
# whole execution window, so it can write any value it likes to its own
# meta.json — including, before this round, setting `REFUSED_USD_FIELD` to
# its own true remaining spend and making every later poster's skip check
# believe that figure had already been refused, so it was never sent to the
# Library at all. That is the exact shape `LIVE_STOPPED_FIELD`'s own comment
# above already rules out for the live-posting stop flag; this round closes
# the same hole for the refusal skip. Nothing in this module reads these
# four fields back to decide anything — the decision reads
# `refusal_marker_path`'s file instead, under the account's own state tree,
# which no dispatched run's sandbox grants write access to (see that
# function's own docstring).
REFUSED_USD_FIELD = "ledger_report_refused_usd"
REFUSED_REASON_FIELD = "ledger_report_refused_reason"
REFUSED_POSTER_FIELD = "ledger_report_refused_poster"
REFUSED_DEDUP_KEY_FIELD = "ledger_report_refused_dedup_key"

# The three callers that can hit a `"disproportionate"` refusal: agentctl's
# mid-run checkpoint, agentctl's at-finish post, and model-scorecard's daily
# sweep backstop. A closed set so a typo'd poster name is a KeyError-shaped
# failure at the call site rather than a silent new value nothing reads.
REFUSAL_POSTERS = ("live", "finish", "sweep")

# Where the refusal record `refused_figure_unchanged` actually decides against
# lives, relative to the account home — the same family `run_sandbox.
# TOOL_SESSION_SCRATCH_RELDIR` and `REVIEW_DRAFT_RELDIR` already use for state
# no dispatch's `allowWrite` should ever name (round 2, PR #1021: the prior
# design kept this decision on the run's own meta.json, writable by the
# dispatched run itself for its whole execution window — see
# `REFUSED_USD_FIELD`'s own comment above for the hole that left open). A
# DISPATCHED run has no credential and no sandbox grant that reaches this
# path (confirmed against `run_sandbox.settings()`: no branch of its
# `allow_write` ever names this directory, an ancestor of it, or the account
# home itself), so a marker found here was written by the dispatcher's own
# poster code, never by the run it describes.
REFUSAL_MARKER_RELDIR = Path(".local/state/agent-platform/ledger-refusals")

# The charset every untrusted run_id is checked against before becoming a
# path component here — the same shape `review_launch._RUN_ID_RE`,
# `review_protocol._RUN_ID_RE` and `session_state._SESSION_ID_RE` already use
# for an identical problem (an identifier from outside this module becoming
# part of a filesystem path). Excludes `/` and `..`, so a crafted run_id
# cannot walk the marker path outside `REFUSAL_MARKER_RELDIR`.
_RUN_ID_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def refusal_marker_path(home: Path, run_id: str) -> Path:
    """Where `run_id`'s own disproportionate-refusal marker lives, under the
    given account `home` (`REFUSAL_MARKER_RELDIR`). Raises `ValueError` for a
    `run_id` that doesn't fullmatch `_RUN_ID_PATH_RE` — this function is the
    one place that turns an untrusted run_id into a path, so every caller
    below is protected by the same check."""
    if not isinstance(run_id, str) or not _RUN_ID_PATH_RE.fullmatch(run_id):
        raise ValueError(f"run_id {run_id!r} is not safe to use as a path component")
    return Path(home) / REFUSAL_MARKER_RELDIR / f"{run_id}.json"


def write_refusal_marker(run_id: str, *, usd: float, reason: str | None, poster: str,
                         dedup_key: str, home: Path | None = None) -> None:
    """Write `run_id`'s own disproportionate-refusal marker — the ONE record
    `refused_figure_unchanged` reads back to decide a skip (see
    `REFUSAL_MARKER_RELDIR`'s own comment for why this lives outside
    meta.json). Called only from `record_disproportionate_refusal`, below,
    which is itself the one place any of the three posters records a
    refusal — so the two writes (this marker, and meta.json's own display
    fields) can never drift apart.

    `home` defaults to `run_sandbox.passwd_home()` — the real account home,
    by design, the same as every other reader of this state family (never
    `Path.home()`/`$HOME`, which a caller's own environment can redirect);
    a caller may override it (tests, and nothing else).

    Best-effort, like every other write to a run's own record from this
    module: a failure here (disk full, a denied path, a malformed run_id)
    must never take down the poster that found the refusal, and its only
    cost is that a LATER call sends this exact figure once more — the safe
    direction, since the worst a missing marker costs is one redundant
    refused POST at the Library, never a figure that should have been
    refused being posted silently."""
    home = Path(home) if home is not None else RS.passwd_home()
    try:
        path = refusal_marker_path(home, run_id)
    except ValueError:
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({
            "run_id": run_id, "usd": usd, "reason": reason,
            "poster": poster, "dedup_key": dedup_key,
        }))
        os.replace(tmp, path)
    except OSError:
        pass


def _read_refused_marker_usd(run_id: str, home: Path | None) -> float | None:
    """The `usd` figure recorded on `run_id`'s own refusal marker, or None
    for anything that isn't a clean, finite number — a missing file, an
    unreadable one, malformed JSON, a non-dict body, a missing or
    non-numeric `usd`, or a non-finite one (`"nan"`/`"Infinity"`, the exact
    shape a forged record could carry). None is the "nothing to compare
    against" case `refused_figure_unchanged` treats as "not refused before",
    so any of these failure shapes resolves to the same safe outcome: send
    the figure, worst case once more than necessary."""
    home = Path(home) if home is not None else RS.passwd_home()
    try:
        path = refusal_marker_path(home, run_id)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        usd = float(data.get("usd"))
    except (TypeError, ValueError):
        return None
    return usd if math.isfinite(usd) else None


def record_disproportionate_refusal(run_dir: Path, *, usd: float, reason: str | None,
                                    poster: str, dedup_key: str,
                                    home: Path | None = None) -> None:
    """Record one `"disproportionate"` refusal — the ONE place any of the
    three posters (`poster` — one of `REFUSAL_POSTERS`) writes this record,
    so the shape can't drift between them. Writes TWO things, in order:
    `run_id`'s own refusal marker (`write_refusal_marker`, under
    `REFUSAL_MARKER_RELDIR` — the record `refused_figure_unchanged` actually
    decides against), and `run_dir`'s meta.json (`REFUSED_USD_FIELD` and
    friends — display only; see that field's own comment for why).

    `usd` is the figure that was actually refused (the poster's own
    post-reduction remainder, never the run's raw, un-reduced cost — see
    `post_run_ledger_entry`'s and `report_to_ledger`'s own docstrings on why
    that distinction is what makes `refused_figure_unchanged` below a sound
    comparison across passes). `reason` is the Library's own refusal text
    (`issue_client.post_ledger_entry`'s `"error"`). `dedup_key` is the key
    the refused report was sent under — `run_id` for the finish post and the
    sweep (they share one key), or `ledger.live_dedup_key(run_id, seq)` for a
    live delta. `home` is forwarded to `write_refusal_marker` (tests only).

    Best-effort, like every other write to a run's own record from this
    module: a failure to record a refusal must never take down the poster
    that found it."""
    write_refusal_marker(run_dir.name, usd=usd, reason=reason, poster=poster,
                         dedup_key=dedup_key, home=home)
    try:
        T.update_meta(
            run_dir,
            **{REFUSED_USD_FIELD: usd, REFUSED_REASON_FIELD: reason,
               REFUSED_POSTER_FIELD: poster, REFUSED_DEDUP_KEY_FIELD: dedup_key})
    except (OSError, ValueError):
        pass


def refused_figure_unchanged(usd: float, *, run_id: str, home: Path | None = None) -> bool:
    """True when `usd` — the figure a poster is about to send — is EXACTLY
    the figure already refused and recorded for `run_id` (`refusal_marker_
    path`'s own file under `home`, never `run_id`'s meta.json — round 2,
    security review of PR #1021 round 1: meta.json is writable by the
    DISPATCHED run for its whole execution window, so a decision keyed on it
    is as trustworthy as a number the run types in itself; see
    `REFUSED_USD_FIELD`'s own comment). The one comparison both the
    at-finish post (`post_run_ledger_entry`) and the sweep
    (`model-scorecard`'s `report_to_ledger`) make before resending, so the
    two posters can't drift on what counts as "the same figure" (E1·9,
    Clause 6: never resubmitted unchanged).

    No recorded marker (missing, unreadable, malformed, or carrying a
    non-numeric/non-finite `usd` — see `_read_refused_marker_usd`) always
    compares False — there is nothing to compare against, so there is
    nothing to skip, and the figure is sent. That is the deliberately SAFE
    direction: this record only decides whether to SKIP a resend, never
    whether to enforce a cap, so the worst a missing or forged marker costs
    is one redundant refused POST at the Library, never a budget bypass — a
    run cannot forge this marker into existence to suppress a post, since
    nothing in its sandbox grants it write access to `REFUSAL_MARKER_RELDIR`
    (see that constant's own comment).

    `home` defaults to `run_sandbox.passwd_home()`; a caller may override it
    (tests only — see `write_refusal_marker`'s own docstring)."""
    refused = _read_refused_marker_usd(run_id, home)
    return refused is not None and refused == usd


# Bound on `read_live_ledger_state`'s own read, for the two callers (below,
# and `model-scorecard`'s `report_to_ledger`) that run outside any run's own
# critical path — unhurried relative to `issue_client`'s general _TIMEOUT.
# `agentctl`'s live checkpoint, which DOES sit inline in a run's own drain
# loop, passes its own shorter bound instead (`_LIVE_LEDGER_READ_TIMEOUT_S`).
_DEFAULT_LEDGER_READ_TIMEOUT_S = 5.0


def read_live_ledger_state(
        run_id: str, *, timeout: float = _DEFAULT_LEDGER_READ_TIMEOUT_S) -> dict[str, Any]:
    """The Library's OWN record of what has already been reported for
    `run_id` (`GET /ledger-entries?run_id=...`, via
    `issue_client.list_ledger_entries`) — THE ONLY account of a run's
    already-reported spend that the run itself cannot write.

    Replaces the pre-round-2 design, which trusted a cumulative figure the
    live reporter cached on the run's own meta.json
    (`ledger_live_reported`/`ledger_live_seq`): a dispatched run holds
    `AGENTCTL_RUN_DIR` and unrestricted Bash, so it could set that figure to
    `1e12` and have every later poster compute a non-positive remainder and
    post nothing — permanently erasing its own spend — or set it to `"nan"`/
    `"-Infinity"` and have a non-finite amount reach the Library outright
    (security review, PR #1018 round 1). The Library's ledger cannot be
    forged this way: a dispatched run has no credential that can write to
    it (`issue_client.post_ledger_entry`'s own docstring — the posting
    credential it reads is outside the run's sandbox), so reading the
    ledger itself, fresh, every time, is the one source every caller below
    can actually trust.

    Returns `{"lane_totals": {lane: summed usd over this run's NON-VOIDED
    entries for that lane}, "entry_count": the number of entries the read
    returned, VOIDED ONES INCLUDED}`.

    `entry_count`, not a locally-tracked sequence number, is what
    `_post_live_ledger_delta` derives its next live dedup_key's sequence
    from: a key that already stands at the Library is counted here and can
    never be reused for a different amount, while a key whose earlier POST
    attempt never actually reached, or was never recorded by, the Library is
    safe to reuse, since nothing stands under it there to collide with.

    RAISES `issue_client.IssueQueueUnavailable` when the read itself fails
    (unreachable Library, a non-2xx, a malformed body — `list_ledger_entries`'s
    own posture), and `ValueError` when any entry carries a non-finite or
    negative `usd` — a fault in what the Library served, not something to
    silently coerce or drop from the sum. Either way, the caller's own
    contract is to post NOTHING this checkpoint and let the next one
    recompute from scratch; nothing here falls back to a cached or estimated
    figure."""
    entries = issue_client.list_ledger_entries(run_id=run_id, timeout=timeout)
    lane_totals: dict[str, float] = {}
    for entry in entries:
        raw = entry.get("usd")
        try:
            usd = float(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"ledger entry {entry.get('dedup_key')!r} for {run_id!r} carries "
                f"a non-numeric usd {raw!r}") from e
        if not math.isfinite(usd) or usd < 0:
            raise ValueError(
                f"ledger entry {entry.get('dedup_key')!r} for {run_id!r} carries "
                f"a non-finite or negative usd {raw!r}")
        if entry.get("voided_at"):
            continue
        lane = entry.get("lane")
        if lane:
            lane_totals[lane] = lane_totals.get(lane, 0.0) + usd
    return {"lane_totals": lane_totals, "entry_count": len(entries)}


def live_dedup_key(run_id: str, seq: int) -> str:
    """The dedup_key for the `seq`th live delta post of `run_id` — distinct
    from the bare `run_id` string `post_run_ledger_entry`'s own at-finish
    post (and `model-scorecard`'s backstop) use, so a live delta can never
    collide with, or be mistaken for, a run's final entry."""
    return f"{run_id}:live:{seq}"


def remaining_after_live(final_usd: float, lane_totals: dict[str, Any] | None,
                         lane: str) -> float:
    """`final_usd` minus whatever is already posted for `lane`
    (`lane_totals` — `read_live_ledger_state(run_id)["lane_totals"]`, the
    Library's OWN sum for this run, or None/malformed when the read found
    nothing for the lane) — the ONE place this subtraction happens, so the
    live delta, agentctl's at-finish post and `model-scorecard`'s backstop
    sweep can't drift on how it's done, and so a run whose at-finish post
    failed outright is still not double-counted when the sweep later posts
    its own backstop entry under the same `dedup_key=run_id`.

    A lane with no recorded total subtracts 0.0 — the ordinary case for a
    run with nothing posted for that lane yet. Rounded to 6 decimals,
    matching every other USD figure this module posts; the caller decides
    what to do with a zero or negative result (never post it — see
    `post_run_ledger_entry`)."""
    reported = 0.0
    if isinstance(lane_totals, dict):
        try:
            reported = float(lane_totals.get(lane, 0.0) or 0.0)
        except (TypeError, ValueError):
            reported = 0.0
    return round(final_usd - reported, 6)


def post_run_ledger_entry(run_dir: Path, *, run_id: str, repo: str | None,
                         model: str | None, agent: str | None, cost_usd: float,
                         is_gateway_model: Callable[[str], bool],
                         is_codex_model: Callable[[str], bool],
                         item_id: str | None = None, day: str | None = None,
                         tok: str | None = None, home: Path | None = None) -> str:
    """Best-effort post of ONE run's REMAINING cost to the Library's ledger,
    under `dedup_key=run_id` — the same key `model-scorecard`'s own sweep
    posts under, so whichever of the two writers reaches the Library first for
    a given run wins, and the other's post answers with that same entry rather
    than double-counting.

    `cost_usd` here is the run's FULL final cost, same as always — this
    function is what subtracts whatever is already posted for the resolved
    lane, read FRESH from the Library's own ledger (`remaining_after_live`,
    fed by `read_live_ledger_state(run_id)` — CQ·446 round 2; the run's own
    `meta.json` is never consulted for this, since it is writable by the run
    being measured) before posting, so the live deltas plus this one entry
    sum to `cost_usd` rather than double-counting whatever already reached
    the Library mid-run. A remainder that is zero or negative (the live
    reporter already covered the whole run, or overshot it on a last
    checkpoint that landed after this call's own `cost_usd` was computed)
    posts NOTHING — `issue_client.post_ledger_entry` is never called — and
    that is recorded as `"skip:already-reported-live"`.

    A `cost_usd` that is not finite and non-negative, or a ledger read that
    fails (`read_live_ledger_state` raising `IssueQueueUnavailable` or
    `ValueError`), also posts NOTHING: the caller cannot tell how much of
    this run is already accounted for, so guessing would risk double- or
    under-counting it. Both record their own `"error:*"` status rather than
    `"recorded"`, which is exactly what leaves the run open to
    `model-scorecard`'s next sweep pass instead of being skipped as done.

    **A remainder that EXACTLY matches a refusal already recorded for this
    run (`refused_figure_unchanged`, reading `run_id`'s own refusal marker —
    never the run's own meta.json, which a DISPATCHED run can write for its
    whole execution window; see that function's own docstring) posts
    NOTHING** — status `"skip:refused-unchanged"` — whether that refusal was
    this function's own prior call, the live checkpoint's
    (`_post_live_ledger_delta`), or the sweep's: the Library already refused
    this exact figure once (E1·9, Clause 6), so resending it unchanged would
    only be refused again. A remainder that differs (the run's cost was
    corrected since the refusal) posts as usual. The marker is read fresh
    here rather than carried by the caller, and a missing, unreadable or
    malformed one reads as "no prior refusal" rather than failing the post
    — the safe direction (see `refused_figure_unchanged`'s own docstring).

    A `"disproportionate"` answer from the post itself is recorded through
    `record_disproportionate_refusal` (`poster="finish"`, `dedup_key=run_id`)
    before this function returns. `home` is forwarded to both (tests only —
    see `write_refusal_marker`'s own docstring).

    Never raises, and the caller must call this only after a run's cost is
    final: a failed or refused post must never change the run's own exit
    status or outcome, and a row posted under this `dedup_key` before the
    cost is final pins a wrong figure at the Library forever (see
    `model-scorecard`'s `read_final_cost`/`is_cost_final` docstrings for the
    same hazard on its side).

    Returns the post's status string — "recorded", "not-recorded",
    "disproportionate", "skip:no-lane", "skip:already-reported-live",
    "skip:refused-unchanged", or one of `issue_client.post_ledger_entry`'s
    own "skip:*"/"error:*" values — and records that same string on the
    run's own `meta.json` as `ledger_post_status`. That mark is what
    `model-scorecard`'s sweep reads to skip a run already `recorded` rather
    than re-posting it; any other value leaves the run open to the sweep's
    next pass."""
    status = "error:unresolved-lane"
    try:
        if not isinstance(cost_usd, (int, float)) or not math.isfinite(cost_usd) or cost_usd < 0:
            status = "error:invalid-cost"
        else:
            lane = ledger_lane(model, is_gateway_model=is_gateway_model,
                               is_codex_model=is_codex_model)
            if lane is None:
                status = "skip:no-lane"
            else:
                try:
                    state = read_live_ledger_state(run_id)
                except (issue_client.IssueQueueUnavailable, ValueError) as e:
                    status = f"error:ledger-read-{e.__class__.__name__}"
                else:
                    remaining = remaining_after_live(cost_usd, state["lane_totals"], lane)
                    if remaining <= 0:
                        status = "skip:already-reported-live"
                    elif refused_figure_unchanged(remaining, run_id=run_id, home=home):
                        status = "skip:refused-unchanged"
                    else:
                        kwargs: dict[str, Any] = {}
                        if agent:
                            kwargs["agent"] = agent
                        if model:
                            kwargs["model"] = model
                        if item_id:
                            kwargs["item_id"] = item_id
                        res = issue_client.post_ledger_entry(
                            repo, remaining, run_id=run_id, dedup_key=run_id, lane=lane,
                            day=day, tok=tok, **kwargs)
                        status = res.get("status") or "error:bad-status"
                        if status == "disproportionate":
                            record_disproportionate_refusal(
                                run_dir, usd=remaining, reason=res.get("error"),
                                poster="finish", dedup_key=run_id, home=home)
    except Exception as e:  # defence in depth: issue_client.post_ledger_entry
        # already never raises, but this call runs inline in agentctl's own
        # finalize path, where nothing here may ever take the run down with it.
        status = f"error:{e.__class__.__name__}"
    try:
        T.update_meta(run_dir, ledger_post_status=status)
    except (OSError, ValueError):
        pass
    return status
