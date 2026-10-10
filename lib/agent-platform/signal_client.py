"""Loopback signal-store client for platform jobs (ARCH-353/356).

A thin client the platform's own launchd/bin jobs use to write a signal to the
Convivy signal store (the mini's loopback control plane, POST /api/signals). It
mirrors convivy-orient/github_capture.py's post shape: a Bearer token, a JSON
body, best-effort, never raises.

Auth: an agent-kind bearer in the store's api-tokens.json (name `agent-platform`,
products `["*"]`, NO `kind:system` — these are internal-only reports and must
never hold signal_customer). The secret is read from AGENT_SIGNAL_TOKEN or the
0600 file ~/.orient-control/agent-platform-token; absent → the client no-ops
(so a box without the token, or a disabled deploy, degrades silently).
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import vault_plane

# **THE PLANE IS `vault_plane`'s TO DECIDE, and this constant is kept only for
# the message it appears in.** The vaults move to the Library at the write
# cutover, and a client that decided its own base URL is a client that can be
# missed when they do — silently, since Orient's tables survive the move and
# still answer 200. Five modules imported this name to build URLs with; they now
# call `vault_plane.url`.
BASE_URL = vault_plane.ORIENT_BASE_URL
TOKEN_ENV = "AGENT_SIGNAL_TOKEN"
TOKEN_FILE = Path.home() / ".orient-control" / "agent-platform-token"
_BODY_CLIP = 200_000                   # mirrors the store's MAX_TEXT
_TIMEOUT = 2                           # short: the tee must not delay delivery
# The delivery-wanting path. post_structured's callers are scheduled launchd
# jobs (the cost report, budget watchdog, metric alerts, the scorecard) that run
# once and exit — they have all the time in the world and CARE that the write
# lands, the exact inverse of the tee. The 2s budget was never theirs; it is the
# tee's, and putting a delivery-critical write on it means a store that takes
# longer than 2s reports a timeout on a write it then completes anyway — a
# dropped alert that looks delivered. 30s matches github_capture's budget for
# the same one-shot-and-cares reason. (convivy-orient #110 already brought a warm
# insert to ~0.18s; this is the headroom for a cold process, not the common case.)
_DELIVERY_TIMEOUT = 30

# channel-label prefix → product. The store requires a non-empty product; ops
# channels (cost, alerts, briefings, docs-validation, general-*) have no single
# product, so they carry the "platform" label. product is only a scope label —
# the agent-platform token holds products ["*"], so any label is writable.
_PRODUCT_PREFIXES = ("vivy", "constance", "sous-chef", "selector", "hollis")


def token() -> str | None:
    """The bearer secret from AGENT_SIGNAL_TOKEN or the 0600 token file, or None
    (absent → the client no-ops, degrading silently)."""
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


# Set once this process has printed the no-token notice below, so a caller that posts
# many signals in one run (a dispatched run's Bash, whose sandbox denies TOKEN_FILE
# — knowledge#793) gets one line, not one per post.
_WARNED_NO_TOKEN = False


def _warn_no_token_once() -> None:
    global _WARNED_NO_TOKEN
    if _WARNED_NO_TOKEN:
        return
    _WARNED_NO_TOKEN = True
    print(f"signal_client: no token ({TOKEN_ENV} unset and {TOKEN_FILE} unreadable); "
          "dropping this and any further signal post", file=sys.stderr)



def product_for_channel(label: str) -> str:
    """Best-effort product from a channel key/id. A per-product channel
    (`vivy-explanations`, `sous-chef-bugs`) maps to its product; `svc-*` is the
    app-kit suite; everything else is `platform`."""
    lbl = (label or "").lower()
    for prod in _PRODUCT_PREFIXES:
        if lbl.startswith(prod):
            return prod
    if lbl.startswith("svc"):
        return "convivy-app-kit"
    return "platform"


def extract_text(payload: Any) -> str:
    """Pull human-readable text out of a Slack message payload — the top-level
    `text`, else the concatenated text of its block-kit sections/headers/context
    elements. Returns "" when there's nothing to capture (an image-only or empty
    payload) so the tee skips it."""
    if not isinstance(payload, dict):
        return ""
    top = payload.get("text")
    if isinstance(top, str) and top.strip():
        return top.strip()
    parts: list[str] = []
    for block in payload.get("blocks", []) or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("text")
        if isinstance(bt, dict) and isinstance(bt.get("text"), str):
            parts.append(bt["text"])
        for el in block.get("elements", []) or []:
            if isinstance(el, dict) and isinstance(el.get("text"), str):
                parts.append(el["text"])
    return "\n".join(p for p in parts if p.strip()).strip()


# Delivery attempts for a caller that wants the write to land (post_structured).
# The Library answers 503 when another writer holds its database lock past its
# 5s busy timeout, and a caller on a shorter budget sees a timeout instead. On
# 2026-09-15/16 eight model_resolution records were lost that way, every one
# logged `error:TimeoutError` by agentctl's 3s finalize post.
_DELIVERY_ATTEMPTS = 3


def _retryable(result: str, payload: dict) -> bool:
    """Whether a failed POST may be sent again, counting against `attempts`.

    A 429 always may be sent again, with no `dedup_key` needed. THIS IS A CONTRACT
    FROM THE LIBRARY, NOT AN ASSUMPTION MADE HERE: in the library repo, app.py's
    rate gate is "the last thing that can still say no before the first byte is
    written" (library#176, commit aa8af0271e1a) — every other refusal (auth,
    scope, a malformed field) already returns before it, and it itself returns
    before `store.insert_signal` runs. tests/test_signal_rate_limit.py's own
    docstring states the same thing: the 429 response names the limit, the window
    and `Retry-After` "before anything is written". A 429 is therefore proof that
    nothing was written, which is why this guarantee covers ONLY 429.

    A 503, a timeout or a connection error may be sent again only when
    `dedup_key` makes a second POST a no-op. A 503 is not proof nothing was
    written: the Library's busy handler says "the body does not say whether
    anything was written", since a route can commit one step and hit the lock on
    a later one (security-reviewer, #604). A timeout is ambiguous the same way.
    Any other status is the request itself, and sending it again cannot help."""
    if result == "error:429":
        return True
    return bool(payload.get("dedup_key")) and result in (
        "error:503", "error:TimeoutError", "error:URLError")


# Issue 2026-09-30 (work item issue_01M3SS45WJBKZM93Z2PE4H2XDJ): the Library's
# POST /signals carries a 120-signals-a-minute-per-submitter ceiling (merged as
# library#176, not yet deployed). Over the limit it answers 429 with a
# Retry-After header. bin/pr-state-capture (peaked at 1,878 signals/minute on
# 2026-09-16) and bin/transcript-capture (576/minute) both post one signal at a
# time through this function and are the only callers that run hot enough to
# reach the ceiling — every other caller (post_report/post_alert/post_structured
# callers, bin/post-release-note) posts at most a handful of signals per run.
#
# FALLBACK WAIT when Retry-After is missing or unparseable: a Library serving
# 429 at all is already under load, so retrying immediately would hit the same
# limit again; this waits a short, bounded interval instead of guessing a
# longer one. 2 seconds is the Retry-After value the done-when's fake Library
# uses, which keeps a missing-header retry and a stated-header retry visually
# the same length in the common case this was sized against.
_RETRY_AFTER_FALLBACK_SECONDS = 2.0


def _parse_retry_after(raw: str | None) -> float:
    """Seconds to wait, from a 429's `Retry-After` header value. The Library's
    contract states the limit and the window as a plain number of seconds
    (never an HTTP-date); a missing or unparseable value falls back to
    `_RETRY_AFTER_FALLBACK_SECONDS` rather than retrying immediately into the
    same limit, or raising and losing the signal over a malformed header."""
    if raw is not None:
        try:
            seconds = float(raw.strip())
        except (TypeError, ValueError):
            seconds = None
        else:
            if seconds >= 0:
                return seconds
    return _RETRY_AFTER_FALLBACK_SECONDS


def post_signal(payload: dict, *, tok: str | None = None, timeout: float = _TIMEOUT,
                attempts: int = 1, sleep=time.sleep, deadline: float | None = None) -> str:
    """POST one signal to the loopback store. Returns 'created' | 'duplicate' |
    'skip' (no token) | 'error:<code>'. Never raises — a capture failure must not
    surface to the caller.

    `timeout` defaults to the tee's short budget (`_TIMEOUT`). A delivery-wanting
    caller passes `_DELIVERY_TIMEOUT`; post_structured does this for you.
    The return string is how a caller that CARES learns the outcome — the swallow
    is about not raising, not about hiding, so a caller is free to branch on it.

    `attempts <= 1` (the default) makes exactly one POST and never retries a
    503/timeout/connection-error — this is what keeps the tee's 2s budget
    from ever being delayed by a retry it never asked for.

    A 429 is retried in one of two ways, by what the caller passes:

      - With its own `deadline` (a `time.monotonic()` value), every 429 waits
        out its `Retry-After` (see `_parse_retry_after`) and resends the same
        payload, for as long as `deadline` allows, not a fixed number of tries,
        since the Library's own window is what bounds how long a retry is still
        worth making. A 429 still unresolved when the deadline is reached
        returns 'error:429', a reported failure, never a silent drop. Only the
        two captures (pr-state-capture, transcript-capture) pass a deadline.
      - With no `deadline`, a 429 counts against `attempts` like a 503 does,
        and waits the same jittered backoff. `Retry-After` is not waited on
        here: it can name the whole window, and a caller's worst-case bound is
        computed from `attempts` and the backoff alone. Most `post_structured`
        callers take this path, so a 429 is retried inside their attempt
        budget rather than returned on the first refusal.

    `attempts <= 1` (the tee's default, and a caller passing no `deadline` and
    no `attempts`) makes exactly one POST, 429 included, and returns it."""
    tok = tok or token()
    if not tok:
        _warn_no_token_once()
        return "skip"
    result, retry_after = _post_once(payload, tok, timeout)
    backoff_attempt = 1
    while True:
        if result == "error:429" and deadline is not None:
            # Resent with NO dedup_key gate, unlike the 503/timeout/
            # connection-error branch below — the Library's own rate gate is
            # "the last thing that can still say no before the first byte is
            # written" (library#176, commit aa8af0271e1a) and returns before
            # store.insert_signal runs, so a 429 is proof nothing was
            # written yet. See `_retryable`'s docstring for the full
            # contract and why that guarantee covers ONLY 429.
            wait = _parse_retry_after(retry_after)
            if time.monotonic() + wait > deadline:
                return result
            sleep(wait)
            result, retry_after = _post_once(payload, tok, timeout)
            continue
        if attempts <= 1 or backoff_attempt >= attempts or not _retryable(result, payload):
            return result
        # Backoff with jitter, so writers refused together do not collide again.
        sleep(min(2 ** (backoff_attempt - 1), 4) * random.uniform(0.5, 1.5))
        result, retry_after = _post_once(payload, tok, timeout)
        backoff_attempt += 1


def _post_once(payload: dict, tok: str, timeout: float) -> tuple[str, str | None]:
    """One POST attempt. Returns (result, retry_after) — `retry_after` is the
    raw `Retry-After` header value on a 429, else None; `post_signal` is the
    only caller and is where it gets parsed and acted on."""
    try:
        req = urllib.request.Request(
            vault_plane.url("/api/signals"),
            data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.headers(token=tok, json_body=True),
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return ("created" if resp.status == 201 else "duplicate"), None
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After") if e.code == 429 else None
        return f"error:{e.code}", retry_after
    except Exception as e:  # loopback down, timeout, anything — swallow
        return f"error:{type(e).__name__}", None


def post_structured(kind: str, stream: str, body: str, *,
                    product: str = "platform", meta: dict | None = None,
                    dedup_key: str | None = None,
                    produced_at: str | None = None,
                    timeout: float = _DELIVERY_TIMEOUT,
                    deadline: float | None = None,
                    sleep=time.sleep,
                    tok: str | None = None) -> str:
    """Emit a STRUCTURED report/alert signal (ARCH-353 Build 2c) — a platform job
    that has the underlying data posts kind=digest (a report) or kind=alert with
    a `meta` dict the team-site render can format richly (a scorecard table, a
    pending-item checklist), while `body` keeps the human-readable text so it
    still reads before the rich render lands.

    `tok`, like `deadline`/`sleep` below, passes straight through to
    `post_signal` — a caller with its own credential (pr-state-capture,
    transcript-capture, each minted its own actor and 120/minute bucket under
    library#176) passes it here; `tok=None` (the default) keeps every other
    caller on `post_signal`'s own `tok or token()` fallback to the shared
    agent-platform credential, unchanged.

    Best-effort like the tee — never raises, so it cannot break the job — but
    NOT silent like the tee. A structured report/alert wants delivery, so a write
    that does not land is logged to stderr (the launchd job's log). Every current
    caller discards the return, which is fine for a value that read as delivered;
    it is not fine for one that quietly did not. Defaults to the delivery budget,
    not the tee's 2s, for the reason on _DELIVERY_TIMEOUT — but that budget is
    sized for a one-shot launchd job, not a caller sitting inline on a hot path
    (agentctl's per-run finalize post); such a caller passes its own shorter
    `timeout` rather than inheriting 30s on every call.

    `produced_at` is when the reported thing was true, as ISO-8601; omitted, the
    store leaves it null and a reader has only `ingested_at`.

    `deadline` and `sleep` pass straight through to `post_signal` — a caller
    with its own run budget (pr-state-capture, transcript-capture) passes a
    `time.monotonic()` deadline so a 429 storm fails loudly at the budget's
    edge instead of running past it; `sleep` is the test seam, overridden in
    tests so a retry loop does not consume real wall-clock time. Every other
    `post_structured` caller passes no `deadline` (the default), so a 429 is
    retried inside the same `_DELIVERY_ATTEMPTS` budget as a 503 or a timeout,
    on the jittered backoff, and never waits out `Retry-After`. The worst case
    is `_DELIVERY_ATTEMPTS` POSTs at `timeout` each plus at most 4.5 seconds of
    backoff: 19.5 seconds at the default `timeout` of 5, for a store that stops
    answering. urllib applies `timeout` to each socket operation, not to the whole
    attempt, so a store that trickles bytes can take longer than that."""
    payload = {
        "source": "platform",
        "kind": kind,
        "product": product,
        "actor": "platform",
        "stream": stream,
        "body": (body or "")[:_BODY_CLIP],
        "meta": meta or {},
    }
    if dedup_key:
        payload["dedup_key"] = dedup_key
    if produced_at:
        payload["produced_at"] = produced_at
    try:
        result = post_signal(payload, tok=tok, timeout=timeout, attempts=_DELIVERY_ATTEMPTS,
                             deadline=deadline, sleep=sleep)
    except Exception as e:  # defence in depth — post_signal already swallows
        print(f"structured signal NOT delivered for {stream}: {type(e).__name__}",
              file=sys.stderr)
        return f"error:{type(e).__name__}"
    # 'created'/'duplicate' landed; 'skip' is a deliberate token-less no-op. Only
    # an 'error:' is a delivery that was wanted and did not happen — the failure
    # the old swallow-everything hid from a caller that never checked the string.
    if result.startswith("error"):
        print(f"structured signal NOT delivered for {stream}: {result}", file=sys.stderr)
    return result


def post_report(stream: str, body: str, **kw) -> str:
    """A recurring report → /memory/reports (kind=digest)."""
    return post_structured("digest", stream, body, **kw)


def post_alert(stream: str, body: str, **kw) -> str:
    """An operational alert → /engineering/alerts (kind=alert)."""
    return post_structured("alert", stream, body, **kw)


# --- sanitise: escape stripping + credential redaction ----------------------
#
# The audience shift (issue_01KYSG57XQMP3PF2W76QJ775B5): every alert body that
# used to go to a private Slack channel now renders on a tiered-readership page.
# Platform-authored values — product names, run ids, repo names, elapsed times,
# dollar figures — are fine as they are. Output the platform did not author —
# subprocess stderr/stdout, log lines, exception text, API response bodies, file
# contents — must pass through `sanitise` before it lands in a body or meta.
# This is the ONE shared implementation; the two prior variants
# (bin/overnight-healthcheck._sanitise_launcher_line, #317, and
# lib/agent-platform/session_state._sanitise_error, #315) were flagged in review
# for drifting from each other. Live here rather than in a new module because the
# ~/.local/lib/agent-platform farm only links modules that existed when
# link-farm.sh last ran — every post_alert caller already imports this module,
# so there is no unlinked-module window (the picker_render outage, session_state).
#
# The result is inert text: no escape sequence survives to inject structure, and
# no credential-shaped value survives to be read off the page. Redaction is
# deliberate, not paranoid — a bare 40-hex legacy GitHub PAT is
# indistinguishable from a git sha and therefore passes unredacted; prefixed
# tokens are caught by their prefix instead.
#
# INVISIBLE CHARACTERS are handled by TWO SEPARATE RULES, because "does this
# codepoint have its own glyph" and "does removing it change what a reader
# sees" are different questions (round 5, #595). `is_default_ignorable` below
# is the Unicode Default_Ignorable_Code_Point property (UAX #44) itself, by
# lookup against the property's own published range table — used AS-IS for
# IDENTITY (`bin/pr-state-capture._identity_title`'s dedup key, which wants
# every member of the property gone, full stop, regardless of visible effect).
# DISPLAY (this function, `sanitise`, every other caller) strips the same
# property MINUS a small keep set: round 4 stripped the full property here
# too and broke real rendering — `sanitise("👨‍👩‍👧‍👦")` (a ZWJ-joined family)
# came back as four separate person emoji, and `sanitise("♻️")` (recycle +
# VARIATION SELECTOR-16) lost its forced emoji presentation — because a
# codepoint can have no glyph of its own while still controlling how ADJACENT
# codepoints render (joining them into one grapheme, or selecting which of
# several presentations they take). "Invisible in isolation" and "safe to
# delete" are not the same property; `_DISPLAY_KEEP_CODEPOINTS` below names
# every codepoint where the two diverge, and why. The RTLO-style rendering
# spoof round 2 flagged for bidi overrides is unaffected — none of those
# codepoints are in the keep set.
#
# NORMALISED TO NFC (canonical composition) here, not NFKC. NFC only folds a
# decomposed sequence (e.g. "e" + combining acute) into its precomposed form
# ("é") — two byte-different but visually-identical titles collapse to the
# same sanitised string, which is exactly the identity-stability property a
# dedup key needs. NFKC goes further and also applies *compatibility*
# decomposition (full-width "Ａ" → "A", the "ﬁ" ligature → "fi", "①" → "1"),
# which changes what the text actually says — out of scope for THIS function,
# which every caller uses to render text for DISPLAY (a terminal line, an
# alert body, a menu description — see the caller list two paragraphs below);
# rewriting what a human reads is not this helper's job. `bin/pr-state-capture`
# needs exactly that extra fold for its dedup-key IDENTITY value (round-3's
# non-blocking finding: NFC leaves compatibility-equivalent forms distinct), so
# it applies NFKC itself, locally, on top of this function's output — see its
# own `_identity_title`. Two derivations of the same input, kept apart because
# they answer different questions: does this read the same to a human
# (display, NFC) vs. does this hash the same as something a human cannot tell
# apart (identity, NFKC).
#
# RENDERING, so a reader knows what this contract defends against:
# /engineering/alerts renders card bodies from Slack mrkdwn to safe HTML
# (convivy-orient ops_reports_build._slack_mrkdwn_to_html) — every value is
# HTML-escaped before formatting, links come only from the <https://…|label>
# Slack form and carry an http(s) scheme. `*bold*`, `_italic_`, backticks and
# Slack-form links therefore still render; `#` headings and `[label](url)` do
# not. That rendering is injection-free; the inert-text contract above is what
# keeps it so. Stripping mrkdwn markers, if ever wanted, belongs in the renderer,
# not here — platform-authored bodies legitimately use those characters.

# Escape stripping: covers CSI (\x1b[31m), DEC private (\x1b[?25l),
# OSC introducers (\x1b]...), SS3 (\x1bO), and Fe (\x1bM, \x1b7, \x1b=).
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"[\[\]()#;?]*(?:\d{1,4}(?:;\d{0,4})*)?[0-9A-ORZcf-nqry=><~]"
    r"|[\x30-\x5f]"
    r")"
)
# C0 controls that survive the ANSI strip — BEL (\x07), etc. — whose
# removal prevents any interpretation as a terminal title or hyperlink.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")

# Whitespace-like characters to collapse into spaces: newline, CR, tab,
# vertical tab, form feed, NEL (\x85), Unicode line/paragraph separators.
_NEWLINE_RE = re.compile(r"[\n\r\t\v\f\x85  ]+")

_MULTISPACE_RE = re.compile(r" +")

# Credential prefixes — redact the value following the known prefix. \S
# already matches every _DISPLAY_KEEP_CODEPOINTS character (none of them are
# whitespace), so this regex needs no interspersion tolerance of its own.
_CRED_PREFIX_RE = re.compile(
    r"(sk-|ghp_|github_pat_|Bearer\s|token=|key=|password=)\S{8,}",
    re.IGNORECASE,
)

# The Unicode version _DEFAULT_IGNORABLE_RANGES below was transcribed from.
_PINNED_UNIDATA_VERSION = "14.0.0"


def _check_unidata_version() -> None:
    """Warn once, to stderr, if the running interpreter's `unicodedata`
    reports a Unicode version other than `_PINNED_UNIDATA_VERSION` —
    `_DEFAULT_IGNORABLE_RANGES` is a point-in-time transcription of that
    property's published range table (see its own comment), not a live
    lookup, so a newer interpreter can know about Default_Ignorable
    codepoints this table has never heard of (Unicode only ever adds to the
    property or converts a reserved range to an assigned one; it does not
    retract members).

    Chosen behaviour on a mismatch: WARN, keep stripping the pinned table
    exactly as before. The alternative that "fails closed" by refusing to
    strip would leave every Default_Ignorable codepoint in DISPLAY output —
    including the bidi overrides this module exists to neutralise (round 2's
    RTLO-style spoof) — which reopens the worse hole to guard against the
    lesser one of an outdated table. Warning changes no behaviour on
    mismatch or match alike, so it cannot silently widen or narrow what
    `sanitise` strips; it only flags that the table wants re-transcribing
    against the new version's DerivedCoreProperties.txt.

    ROUND 7 (#595, security-reviewer non-blocking finding): this print is not
    the mechanism that makes a drift visible — nothing in this codebase reads
    the launchd job's `.err.log` it lands in. The mechanism is
    `test_pinned_unidata_version_matches_running_interpreter` in
    tests/test_sanitise.py, which asserts the same comparison directly and
    fails CI the day the interpreter's Unicode version moves, forcing the
    table to be re-transcribed deliberately before anything ships against it
    — the print stays as a free, zero-behaviour-change secondary signal for a
    deployed interpreter that diverges from the one CI ran against, a gap the
    test can't see."""
    running = unicodedata.unidata_version
    if running != _PINNED_UNIDATA_VERSION:
        print(
            f"signal_client: unicodedata.unidata_version is {running!r}, not "
            f"the {_PINNED_UNIDATA_VERSION!r} _DEFAULT_IGNORABLE_RANGES was "
            "transcribed from — Default_Ignorable stripping still runs "
            "against the pinned table (see its comment); re-transcribe it "
            "from the new version's DerivedCoreProperties.txt.",
            file=sys.stderr,
        )


_check_unidata_version()


# Default_Ignorable_Code_Point, Unicode 14.0.0 — `_check_unidata_version`
# above warns at import if the running interpreter's `unicodedata` disagrees,
# since this table is a point-in-time transcription, not a live lookup.
# Transcribed verbatim from the "Default_Ignorable_Code_Point" section of
# https://www.unicode.org/Public/14.0.0/ucd/DerivedCoreProperties.txt
# (retrieved 2026-09-16; that section's own "# Total code points: 4174" line
# matches summing these 27 ranges — see the boundary/count tests in
# tests/test_sanitise.py). This is the property's own published range table,
# not a derivation from a proxy signal: round 4's `is_default_ignorable`
# matched category Cf outright plus a `unicodedata.name()` substring check for
# the residual Mn/Lo members it knew about, and security-reviewer's round-4
# finding showed that heuristic misses real, assigned codepoints outside its
# two named substrings — U+17B4/17B5 KHMER VOWEL INHERENT AQ/AA (category Mn,
# named "KHMER VOWEL INHERENT...", matching neither "VARIATION SELECTOR" nor
# "COMBINING GRAPHEME JOINER") and the reserved ranges Unicode preemptively
# marks Default_Ignorable for future assignment (U+2065, U+FFF0-FFF8,
# U+E0000, U+E0002-E001F, U+E0080-E00FF, U+E01F0-E0FFF — category Cn, so
# `unicodedata.name()` returns "" and no name-substring check can ever match
# them). Python's `unicodedata` has no direct lookup for this property; the
# property's own range table, transcribed once here, has no such gap because
# it is not trying to infer the property from something else.
_DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),       # SOFT HYPHEN
    (0x034F, 0x034F),       # COMBINING GRAPHEME JOINER
    (0x061C, 0x061C),       # ARABIC LETTER MARK
    (0x115F, 0x1160),       # HANGUL CHOSEONG FILLER..HANGUL JUNGSEONG FILLER
    (0x17B4, 0x17B5),       # KHMER VOWEL INHERENT AQ..KHMER VOWEL INHERENT AA
    (0x180B, 0x180D),       # MONGOLIAN FREE VARIATION SELECTOR ONE..THREE
    (0x180E, 0x180E),       # MONGOLIAN VOWEL SEPARATOR
    (0x180F, 0x180F),       # MONGOLIAN FREE VARIATION SELECTOR FOUR
    (0x200B, 0x200F),       # ZERO WIDTH SPACE..RIGHT-TO-LEFT MARK
    (0x202A, 0x202E),       # LEFT-TO-RIGHT EMBEDDING..RIGHT-TO-LEFT OVERRIDE
    (0x2060, 0x2064),       # WORD JOINER..INVISIBLE PLUS
    (0x2065, 0x2065),       # <reserved-2065>
    (0x2066, 0x206F),       # LEFT-TO-RIGHT ISOLATE..NOMINAL DIGIT SHAPES
    (0x3164, 0x3164),       # HANGUL FILLER
    (0xFE00, 0xFE0F),       # VARIATION SELECTOR-1..VARIATION SELECTOR-16
    (0xFEFF, 0xFEFF),       # ZERO WIDTH NO-BREAK SPACE
    (0xFFA0, 0xFFA0),       # HALFWIDTH HANGUL FILLER
    (0xFFF0, 0xFFF8),       # <reserved-FFF0>..<reserved-FFF8>
    (0x1BCA0, 0x1BCA3),     # SHORTHAND FORMAT LETTER OVERLAP..UP STEP
    (0x1D173, 0x1D17A),     # MUSICAL SYMBOL BEGIN BEAM..END PHRASE
    (0xE0000, 0xE0000),     # <reserved-E0000>
    (0xE0001, 0xE0001),     # LANGUAGE TAG
    (0xE0002, 0xE001F),     # <reserved-E0002>..<reserved-E001F>
    (0xE0020, 0xE007F),     # TAG SPACE..CANCEL TAG
    (0xE0080, 0xE00FF),     # <reserved-E0080>..<reserved-E00FF>
    (0xE0100, 0xE01EF),     # VARIATION SELECTOR-17..VARIATION SELECTOR-256
    (0xE01F0, 0xE0FFF),     # <reserved-E01F0>..<reserved-E0FFF>
)  # sorted, non-overlapping, ascending — is_default_ignorable relies on the order


def is_default_ignorable(ch: str) -> bool:
    """True for a single codepoint in the Unicode Default_Ignorable_Code_Point
    property (UAX #44) — a lookup against `_DEFAULT_IGNORABLE_RANGES` above,
    the property's own published range table.

    This answers ONE question: is `ch` formally Default_Ignorable. It does
    not ask whether removing `ch` changes what a reader sees — variation
    selectors and the zero-width joiner are members of this property and
    also visibly change adjacent rendering, which is why IDENTITY
    (`bin/pr-state-capture._identity_title`) calls this function directly,
    while DISPLAY (`sanitise` below) additionally consults
    `_DISPLAY_KEEP_CODEPOINTS` before deleting anything this function
    returns True for."""
    cp = ord(ch)
    for lo, hi in _DEFAULT_IGNORABLE_RANGES:
        if cp < lo:
            return False
        if cp <= hi:
            return True
    return False


# DISPLAY keeps these Default_Ignorable codepoints because deleting them
# changes what a reader sees — the round-4 blocking finding, plus two more
# characters this round was asked to decide (#595 round 5):
#
#   U+200D            ZERO WIDTH JOINER — joins adjacent emoji into one
#                      glyph; `sanitise("👨‍👩‍👧‍👦")` (a ZWJ-joined family) came
#                      back as four separate person emoji under round 4's
#                      full-property strip (reviewer, round 4, verified).
#   U+FE00-U+FE0F,
#   U+E0100-U+E01EF   VARIATION SELECTOR-1..256 — select which presentation
#                      an emoji or CJK ideograph takes; `sanitise("♻️")`
#                      (recycle + VS16) lost the forced emoji presentation a
#                      gitmoji-style "recycle" title depends on, rendering as
#                      a plain glyph (reviewer, round 4, verified).
#   U+200C            ZERO WIDTH NON-JOINER — forces two Arabic-script
#                      letters (Persian, Urdu) into their isolated/final
#                      forms instead of joining, and suppresses conjunct
#                      ligature formation in Devanagari (Hindi); removing it
#                      joins glyphs the author deliberately kept apart, the
#                      same class of harm as removing ZWJ.
#   U+00AD            SOFT HYPHEN — invisible everywhere except exactly at a
#                      line-break position, where a renderer that wraps text
#                      (an alert body, a Deck card) shows it as a hyphen; its
#                      absence is a visible difference there, conditional
#                      but real.
#
# NOT kept — each because it has no rendering effect of its own to lose, so
# removing it cannot change what a reader sees:
#   U+200B  ZERO WIDTH SPACE           — a word-break opportunity hint only;
#                                        no glyph, no joining, no selection.
#   U+2060  WORD JOINER                — the inverse of ZWSP (blocks a
#                                        break); same reasoning.
#   U+FEFF  ZERO WIDTH NO-BREAK SPACE  — legacy BOM / word-joiner alias, same
#                                        reasoning; never renders a glyph in
#                                        body text.
#   U+200E, U+200F, U+061C,
#   U+202A-U+202E, U+2066-U+2069       — the bidi mark/embedding/override/
#                                        isolate controls: the round-2 RTLO-
#                                        style rendering-order spoof this
#                                        module exists to close. U+061C
#                                        ARABIC LETTER MARK is the same class
#                                        as LRM/RLM (U+200E/U+200F) — a
#                                        strong-direction, zero-width control
#                                        that disambiguates which way an
#                                        adjacent number reads in
#                                        bidirectional text, with no glyph of
#                                        its own — so it belongs in this list
#                                        on its own terms, not merely because
#                                        round 5 named it nowhere (round-5
#                                        reviewer suggestion, round 6). Kept
#                                        OUT of the keep set deliberately.
#
# Every other Default_Ignorable codepoint (U+180B-U+180F, the Hangul filler
# codepoints, U+17B4-U+17B5, the shorthand/musical format controls, the tag
# characters, the reserved ranges) stays stripped from display as before:
# nothing in this round's review passes named one of them as visibly
# affected by removal, and this set only grows when a reader-visible case is
# demonstrated (see tests/test_sanitise.py for the cases that ARE
# demonstrated).
#
# Kept as an explicit (lo, hi) tuple, not just the derived frozenset below,
# because `_LONG_RUN_RE` (round 6, #595) needs the same set expressed as a
# regex character class — a secret with one of these spliced in must still
# redact, and building the class from this tuple instead of hand-copying the
# five ranges again is what keeps the two views from drifting apart.
_DISPLAY_KEEP_RANGES: tuple[tuple[int, int], ...] = (
    (0x200C, 0x200C),
    (0x200D, 0x200D),
    (0x00AD, 0x00AD),
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)

_DISPLAY_KEEP_CODEPOINTS: frozenset[int] = frozenset(
    cp for lo, hi in _DISPLAY_KEEP_RANGES for cp in range(lo, hi + 1)
)


def _strip_default_ignorable_for_display(s: str) -> str:
    """DISPLAY's narrower strip: every Default_Ignorable codepoint except
    `_DISPLAY_KEEP_CODEPOINTS` — see that set's comment for the per-character
    reasoning. IDENTITY (`bin/pr-state-capture._identity_title`) calls
    `is_default_ignorable` directly instead, with no keep list, because it
    wants the full property gone regardless of visible effect."""
    return "".join(
        ch for ch in s
        if not is_default_ignorable(ch) or ord(ch) in _DISPLAY_KEEP_CODEPOINTS
    )


# Long base64/hex runs — redact unless the run is a pure-lowercase hex
# string (a git sha or similar diagnostic token).
#
# ROUND 6 (#595, security-reviewer blocking finding): this used to be a flat
# `[A-Za-z0-9+/=]{32,}`, checked BEFORE `_strip_default_ignorable_for_display`
# widened past category Cf and round 5 carved out `_DISPLAY_KEEP_CODEPOINTS`
# to survive that strip. A secret with a kept codepoint (ZWJ, a variation
# selector, ZWNJ, soft hyphen) spliced in reaches this regex intact — round 5
# kept it there on purpose, for real rendering — and the flat character class
# does not span the splice, so each side falls under the 32-char minimum and
# the whole secret is emitted verbatim. Regressing from "redacted before this
# PR" to "not redacted" is worse than the spoofing round 5 set out to fix.
#
# Fix: let each base64/hex character in the run be followed by any number of
# `_DISPLAY_KEEP_RANGES` codepoints, so the match spans the splice instead of
# breaking on it — the run "still counts" as contiguous for redaction purposes
# even though display keeps the interspersed characters. This is the
# "tolerant pattern" shape rather than the "redact spans mapped back from a
# fully-stripped copy" shape: the two are equivalent for this case (nothing
# else in the pipeline currently changes the string's length between the
# display strip and here), and a tolerant pattern keeps the git-sha exemption
# below working with one extra `sub` on the matched span, rather than a
# second copy of the whole string plus an index-mapping step.
#
# ROUND 7 (#595, security-reviewer suggestion) tried to narrow this: an
# ordinary soft-hyphen-hinted compound word ("supercalifragilistic..." joined
# at its syllable breaks) also spans 32+ characters under this tolerance and
# was redacted whole, so round 7 added a `core.isalpha()` carve-out in
# `_redact_long_run` — a tolerance-formed match with no digit or symbol was
# treated as joined text instead of a credential. The considered alternative
# of dropping soft hyphen from `_DISPLAY_KEEP_RANGES` was rejected then, and
# still doesn't work: `_strip_default_ignorable_for_display` runs BEFORE this
# regex, so the same letters reach `_LONG_RUN_RE` already solid, still
# redacted, without removing the tolerance this comment is about.
#
# WITHDRAWN IN ROUND 8 (#595, security-reviewer blocking finding): round 7's
# carve-out rests on "a real secret always carries a digit or symbol," which
# is false. A BIP-39 recovery phrase is drawn from a fixed 2048-word list
# that is lowercase a-z by specification — no digit, no symbol, ever — so a
# 12-word phrase with one soft hyphen spliced in (a word processor can insert
# one on its own, at a wrap point) cleared round 7's carve-out and was
# emitted whole: a full wallet credential surviving verbatim. Any alphabetic
# secret plus one spliced ignorable character hits the same gap; no
# syntactic test (entropy, case, length) separates a passphrase from a
# soft-hyphenated compound word, because they are the same shape — a BIP-39
# phrase and a fabricated compound word are indistinguishable by content
# alone. This also inverted the ordering the round-6 paragraph above states:
# a partial credential leak is strictly worse than over-redacting a word, and
# round 7 accepted a FULL leak of a live credential class to avoid a cosmetic
# false positive. Round 6's security-reviewer had already raised the
# compound-word over-redaction as NON-BLOCKING, for exactly this reason —
# accepting it here is consistent with both reviews, not a reversal of
# either. So `_redact_long_run` no longer carves out all-alphabetic
# tolerance-formed matches: every tolerance-formed match of 32+ characters
# redacts regardless of content, as it did after round 6. The accepted cost,
# stated plainly: an ordinary long compound word joined by soft hyphens (or
# any other kept codepoint) now displays as redacted. That is a deliberate
# choice, not an oversight — over-redaction is cosmetic and visible to the
# person who wrote the word; a leaked credential is neither.
_DISPLAY_KEEP_CHAR_CLASS = "".join(
    f"{chr(lo)}-{chr(hi)}" if hi > lo else chr(lo)
    for lo, hi in _DISPLAY_KEEP_RANGES
)
_LONG_RUN_RE = re.compile(
    r"(?:[A-Za-z0-9+/=][" + _DISPLAY_KEEP_CHAR_CLASS + r"]*){32,}"
)
_KEEP_CHAR_RE = re.compile(f"[{_DISPLAY_KEEP_CHAR_CLASS}]")


def _redact_long_run(m: re.Match) -> str:
    s = m.group(0)
    # The hex-exemption check runs on the run with any interspersed
    # `_DISPLAY_KEEP_RANGES` characters removed — a spliced 40-hex sha-shaped
    # run is still a sha-shaped run, and this is the same test round 1 used
    # before interspersion tolerance existed. The exempted return keeps `s`
    # (with the splice) so display is unaffected either way.
    core = _KEEP_CHAR_RE.sub("", s)
    # 40-char pure-lowercase hex exemption: a git sha in captured output is
    # diagnostic, and redacting it loses real value. A bare 40-hex legacy
    # GitHub PAT (pre-ghp_ era, same 40-hex shape) is indistinguishable from
    # a sha and therefore passes unredacted. Prefixed tokens (github_pat_,
    # ghp_, sk-, Bearer, token=, key=, password=) are caught by
    # _CRED_PREFIX_RE instead.
    if len(core) == 40 and not set(core) - set("0123456789abcdef"):
        return s
    # ROUND 8 (#595, security-reviewer blocking finding): round 7 added a
    # `core.isalpha()` carve-out here — a tolerance-formed match with no
    # digit or symbol in its real characters was returned unredacted, on the
    # theory that a base64/hex/API-key secret always carries one. A BIP-39
    # recovery phrase does not: it is drawn from a fixed, lowercase-a-z-only
    # 2048-word list, so a spliced phrase cleared this carve-out and leaked
    # whole. See the ROUND 8 paragraph above `_LONG_RUN_RE` for the full
    # case. Withdrawn, not replaced — the reviewer's own analysis is that a
    # BIP-39 phrase and a fabricated compound word are the same shape, so no
    # syntactic test separates them. Every tolerance-formed match reaching
    # this point (past the hex exemption above) redacts regardless of
    # content, matching round 6's behaviour.
    return "[redacted]"


def _sanitise_steps(s: str, *, long_run: bool) -> str:
    """The steps `sanitise` and `sanitise_path` share, in order. `long_run` selects whether
    `_LONG_RUN_RE` applies; that is the only step the two callers differ on."""
    s = unicodedata.normalize("NFC", s)
    s = _ANSI_RE.sub("", s)
    s = _CONTROL_CHARS_RE.sub("", s)
    s = _strip_default_ignorable_for_display(s)
    s = _CRED_PREFIX_RE.sub(r"\1[redacted]", s)
    if long_run:
        s = _LONG_RUN_RE.sub(_redact_long_run, s)
    s = _NEWLINE_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s)
    return s.strip()


def sanitise(s: str, *, max_len: int | None = None) -> str:
    """Normalise to NFC, strip escape sequences and the display-unsafe subset
    of Default_Ignorable Unicode characters (see `_DISPLAY_KEEP_CODEPOINTS`
    above), collapse whitespace, redact credentials.

    `max_len`, when given, caps the result at that many characters plus a
    literal "...". The healthcheck (launcher-stderr line into meta) and the
    kb-audit/preflight bodies pass no cap; session_state's import-failure line
    passes its 500.
    """
    s = _sanitise_steps(s, long_run=True)
    if max_len is not None and len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def sanitise_path(s: str) -> str:
    """`sanitise` for a removed file's name: every step except the long-run rule.

    A removed file's name is the only record of the work it held, and `_LONG_RUN_RE` would
    redact a 32-character name as a credential. Every other step still applies. A credential
    prefix in the name is redacted when it appears as written or with a character this function
    removes. A whitespace control or a kept format character inside the prefix lets the value
    through. That gap is known and is not acted on, because no occurrence of it is named.
    """
    return _sanitise_steps(s, long_run=False)


def sanitise_body(text: str) -> str:
    """`sanitise` applied line by line, so a multi-line body keeps its line breaks.

    A scheduled job's alert body carries text it did not author: test output, git
    paths, launchd argv, pane text. `sanitise` collapses newlines, which would
    flatten a whole body into one line, so a body goes through here instead. Each
    line is sanitised on its own, so each line loses its indentation and any run of
    repeated spaces, and a credential-shaped value on it is redacted when it appears
    as written or with a character `sanitise` removes; a whitespace control or a
    kept format character inside the prefix lets it through, the same known gap
    `sanitise_path` has, not acted on."""
    return "\n".join(sanitise(line) for line in text.splitlines())


