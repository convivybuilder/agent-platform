"""GitHub activity signal shaping and capture (E25, issue_01M4ZB5C5ZX0MV206064MTRAE0).

Epic E25 moves GitHub activity capture from Orient's own job into the Bosun's
launchd jobs. Both writers run during the changeover and the Library
deduplicates on dedup_key, so every field the transforms below build must
match what Orient's github_capture.py builds today — a differing field
creates a duplicate row or a mismatch in the read-back.

Four kinds: review_verdict, changelog, review_comment and pr_comment. Comments
on GitHub issues are deliberately not captured — work items live in the
Library's corrections queue, and GitHub issues are not a Convivy surface.

Besides the pure transforms, this module holds the reads: a 5xx-retrying
GitHub GET through `github_app.request`, the paged repository-wide comment
lists, the pull-request list with its early stop at the cursor, and
`capture_repository`, one repository's whole pass. Nothing calls
`capture_repository` yet; PR 3 adds the launchd job that does. No `gh`
binary and no personal GitHub credential anywhere here — every read takes
its token as an argument, minted by the caller from the convivy-bosun App.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import github_app as GA

_CHANGELOG_BODY_CLIP = 4000

_PAGE_SIZE = 100

# A page that comes back 502, 503 or 504 is retried before the read counts as
# failed — mirrors bin/ci-result-capture's own retry, answering the same
# class of transient GitHub 5xx (issue_01M4XKXB63T8B4RJNAKFPQN7HF). Any other
# non-200 raises at once, with no retry.
_GH_RETRYABLE_STATUSES = (502, 503, 504)
_GH_5XX_RETRY_ATTEMPTS = 3
_GH_5XX_RETRY_BACKOFF_SECONDS = 2.0

# STATE, not configuration shipped with the code (platform/decisions/0059 rule
# 3) — the cursor outlives any one deploy. Mirrors ci-result-capture's and
# pr-state-capture's own STATE_DIR placement and mode.
STATE_DIR = Path.home() / ".local" / "state" / "agent-platform" / "github-activity-capture"
CURSOR_FILE_NAME = "cursor.json"

# A repository with no stored cursor starts from this far before pass start —
# a bounded recent window, not a full-history backfill.
NO_CURSOR_WINDOW = dt.timedelta(hours=24)

# GitHub's own eventual-consistency margin: an event that landed moments
# before this pass read "now" might not yet be visible to the API, so the
# next cursor sits a little behind "now" rather than exactly at it. Signals
# the overlap re-reads are absorbed by dedup_key, never a second row.
OVERLAP = dt.timedelta(minutes=10)

_ISO = "%Y-%m-%dT%H:%M:%SZ"


class FetchError(RuntimeError):
    """A GitHub read failed and retrying (where applicable) did not resolve
    it. Raised naming the URL and the status GitHub answered with."""


def _format_ts(value: dt.datetime) -> str:
    return value.strftime(_ISO)


def _parse_ts(value: str) -> dt.datetime:
    """GitHub's timestamps are always `YYYY-MM-DDTHH:MM:SSZ` (UTC, zero
    padded, no fractional seconds); swapping the trailing `Z` for an explicit
    offset is enough for `fromisoformat`."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pr_number_from_url(url: str) -> int | None:
    """…/pulls/123 or …/issues/123 → 123."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def signal_from_review(repo: str, pr_number: int, review: dict) -> dict | None:
    """A review VERDICT event. Empty-body COMMENTED reviews are shells wrapping
    inline comments (captured separately) — skip those; keep APPROVED /
    CHANGES_REQUESTED even with an empty body, because the verdict IS the signal."""
    state = (review.get("state") or "").upper()
    body = (review.get("body") or "").strip()
    if state == "COMMENTED" and not body:
        return None
    if not state:
        return None
    return {
        "source": "github_review",
        "kind": "review_verdict",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (review.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr": pr_number, "url": review.get("html_url") or ""},
        "meta": {"state": state},
        "produced_at": review.get("submitted_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:review:{review.get('id')}",
    }


def signal_from_merge(repo: str, pr: dict) -> dict | None:
    """A merged-PR event — the in-box changelog's raw material. The merge IS
    the record: title + description become the changelog entry's summary,
    produced_at is when the merge happened (not when it was captured).
    Unmerged PRs (open, closed-without-merge) return None — the changelog
    only ever shows what actually shipped."""
    merged_at = pr.get("merged_at")
    if not merged_at:
        return None
    number = pr.get("number")
    author = (pr.get("user") or {}).get("login") or "unknown"
    title = (pr.get("title") or "").strip()
    description = (pr.get("body") or "").strip()
    body = f"{title}\n\n{description}" if description else title
    if len(body) > _CHANGELOG_BODY_CLIP:
        body = body[:_CHANGELOG_BODY_CLIP].rstrip() + " …"
    return {
        "source": "github",
        "kind": "changelog",
        "stream": repo,
        "actor": author,
        "product": repo,
        "body": body,
        "refs": {"pr": number, "url": pr.get("html_url") or "",
                 "merged_at": merged_at, "author": author},
        "meta": {},
        "produced_at": merged_at,
        "dedup_key": f"gh:{repo}:pr{number}:merged",
    }


def signal_from_review_comment(repo: str, rc: dict) -> dict | None:
    """An inline review comment (repo-wide /pulls/comments endpoint)."""
    pr_number = _pr_number_from_url(rc.get("pull_request_url") or "")
    body = (rc.get("body") or "").strip()
    if pr_number is None or not body:
        return None
    refs = {"pr": pr_number, "url": rc.get("html_url") or ""}
    if rc.get("path"):
        refs["path"] = rc["path"]
    return {
        "source": "github_review",
        "kind": "review_comment",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (rc.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": refs,
        "meta": {},
        "produced_at": rc.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:rc:{rc.get('id')}",
    }


def signal_from_issue_comment(repo: str, ic: dict) -> dict | None:
    """A conversation comment (repo-wide /issues/comments endpoint — covers both
    PRs and issues; only PR comments are captured, selected by html_url)."""
    pr_number = _pr_number_from_url(ic.get("issue_url") or "")
    body = (ic.get("body") or "").strip()
    if pr_number is None or not body:
        return None
    if "/pull/" not in (ic.get("html_url") or ""):
        return None
    return {
        "source": "github_review",
        "kind": "pr_comment",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (ic.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr": pr_number, "url": ic.get("html_url") or ""},
        "meta": {},
        "produced_at": ic.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:ic:{ic.get('id')}",
    }


# --- the fetch: a 5xx-retrying GET, paged lists, the pull-request early stop -


def _github_get(url: str, token: str, *, opener, sleep) -> dict | list:
    """One page's GET, retried up to `_GH_5XX_RETRY_ATTEMPTS` times in total
    when GitHub answers 502/503/504. Any other non-200 still raises
    `FetchError` on the first attempt, with no retry and no sleep."""
    for attempt in range(1, _GH_5XX_RETRY_ATTEMPTS + 1):
        code, payload = GA.request("GET", url, token, None, opener=opener)
        if code == 200:
            return payload
        if code not in _GH_RETRYABLE_STATUSES or attempt == _GH_5XX_RETRY_ATTEMPTS:
            raise FetchError(f"GitHub GET {url} failed ({code})")
        print(f"github_activity: {url} returned {code}; retrying "
              f"(attempt {attempt + 1}/{_GH_5XX_RETRY_ATTEMPTS})", file=sys.stderr)
        sleep(_GH_5XX_RETRY_BACKOFF_SECONDS)
    raise AssertionError("unreachable: the loop above always returns or raises")


def _paged_list(url: str, params: dict, token: str, *, opener, sleep) -> list[dict]:
    """Every item across as many pages as GitHub holds, paged until a page
    shorter than `_PAGE_SIZE` comes back."""
    out: list[dict] = []
    page = 1
    while True:
        query = dict(params, page=page, per_page=_PAGE_SIZE)
        full_url = f"{url}?{urllib.parse.urlencode(query)}"
        items = _github_get(full_url, token, opener=opener, sleep=sleep)
        if not isinstance(items, list):
            raise FetchError(f"GitHub GET {full_url} returned a non-list body")
        out.extend(items)
        if len(items) < _PAGE_SIZE:
            return out
        page += 1


def fetch_review_comments(repo: str, since: str, token: str, *,
                          opener=urllib.request.urlopen, sleep=time.sleep) -> list[dict]:
    """The repository-wide inline review-comment list, updated on/after `since`."""
    return _paged_list(f"{GA.API}/repos/{repo}/pulls/comments",
                       {"since": since, "sort": "updated", "direction": "asc"},
                       token, opener=opener, sleep=sleep)


def fetch_conversation_comments(repo: str, since: str, token: str, *,
                                opener=urllib.request.urlopen, sleep=time.sleep) -> list[dict]:
    """The repository-wide conversation-comment list, updated on/after
    `since`. Covers both pull-request and issue comments with no `issues`
    permission needed (verified live 2026-10-15); `signal_from_issue_comment`
    drops anything whose `html_url` lacks "/pull/"."""
    return _paged_list(f"{GA.API}/repos/{repo}/issues/comments",
                       {"since": since, "sort": "updated", "direction": "asc"},
                       token, opener=opener, sleep=sleep)


def fetch_reviews(repo: str, pr_number: int, token: str, *,
                  opener=urllib.request.urlopen, sleep=time.sleep) -> list[dict]:
    """Every review submitted on one pull request."""
    return _paged_list(f"{GA.API}/repos/{repo}/pulls/{pr_number}/reviews", {},
                       token, opener=opener, sleep=sleep)


def fetch_updated_pull_requests(repo: str, since: str, token: str, *,
                                opener=urllib.request.urlopen,
                                sleep=time.sleep) -> list[dict]:
    """Every pull request updated on/after `since`, read newest-updated
    first and stopping at the first pull request whose `updated_at` is
    older than `since` — the next page is never requested once that pull
    request is seen. Replaces Orient's `--paginate` over every pull request."""
    since_dt = _parse_ts(since)
    out: list[dict] = []
    page = 1
    while True:
        url = (f"{GA.API}/repos/{repo}/pulls?state=all&sort=updated&direction=desc"
              f"&per_page={_PAGE_SIZE}&page={page}")
        items = _github_get(url, token, opener=opener, sleep=sleep)
        if not isinstance(items, list):
            raise FetchError(f"GitHub GET {url} returned a non-list body")
        stopped = False
        for pr in items:
            updated_at = pr.get("updated_at")
            if not updated_at or _parse_ts(updated_at) < since_dt:
                stopped = True
                break
            out.append(pr)
        if stopped or len(items) < _PAGE_SIZE:
            return out
        page += 1


# --- the per-repository capture pass -----------------------------------------


def capture_repository(repo: str, cursor: str | None, token: str, post, pass_start: dt.datetime,
                       *, opener=urllib.request.urlopen, sleep=time.sleep) -> dict:
    """One repository's capture pass: reads every inline review comment,
    conversation comment and updated-or-merged pull request since `cursor`
    (or `NO_CURSOR_WINDOW` before `pass_start` when `cursor` is falsy — there
    is no full-history backfill), posts each resulting signal through
    `post`, and returns a tally.

    `post` is a one-argument callable (signal payload -> 'created' |
    'duplicate' | 'skip' | 'error:<code>', `signal_client.post_signal`'s own
    return vocabulary) — the caller binds whatever credential, timeout and
    deadline it needs as a closure or `functools.partial`.

    Returns a dict: `signals` (a count per kind), `posts` (a count per
    outcome — `created`, `duplicate`, `skip`, `error`), `read_errors` (how
    many of this pass's GitHub reads failed), `may_advance` (whether this
    pass earned the cursor advance below) and `new_cursor` (`pass_start`
    less `OVERLAP` when `may_advance`, else `cursor` unchanged).

    `may_advance` is true only when every read in this pass succeeded AND
    every post returned `created` or `duplicate` — a `skip` (no signal
    credential) or any `error:...` (including a 429 or a 409) holds the
    cursor exactly where it was, so the next pass re-covers what this one
    did not finish delivering. `dedup_key` absorbs whatever a re-covered
    pass re-posts that already landed."""
    since = cursor if cursor else _format_ts(pass_start - NO_CURSOR_WINDOW)
    signals_tally = {"review_verdict": 0, "changelog": 0, "review_comment": 0, "pr_comment": 0}
    posts_tally = {"created": 0, "duplicate": 0, "skip": 0, "error": 0}
    read_errors = 0
    payloads: list[dict] = []

    try:
        for rc in fetch_review_comments(repo, since, token, opener=opener, sleep=sleep):
            sig = signal_from_review_comment(repo, rc)
            if sig is not None:
                payloads.append(sig)
    except FetchError as ex:
        print(f"github_activity: {repo}: {ex}", file=sys.stderr)
        read_errors += 1

    try:
        for ic in fetch_conversation_comments(repo, since, token, opener=opener, sleep=sleep):
            sig = signal_from_issue_comment(repo, ic)
            if sig is not None:
                payloads.append(sig)
    except FetchError as ex:
        print(f"github_activity: {repo}: {ex}", file=sys.stderr)
        read_errors += 1

    try:
        prs = fetch_updated_pull_requests(repo, since, token, opener=opener, sleep=sleep)
    except FetchError as ex:
        print(f"github_activity: {repo}: {ex}", file=sys.stderr)
        read_errors += 1
        prs = []

    for pr in prs:
        number = pr.get("number")
        try:
            reviews = fetch_reviews(repo, number, token, opener=opener, sleep=sleep)
        except FetchError as ex:
            print(f"github_activity: {repo}: {ex}", file=sys.stderr)
            read_errors += 1
            reviews = []
        for review in reviews:
            sig = signal_from_review(repo, number, review)
            if sig is not None:
                payloads.append(sig)
        merge_sig = signal_from_merge(repo, pr)
        if merge_sig is not None:
            payloads.append(merge_sig)

    posts_clean = True
    for sig in payloads:
        signals_tally[sig["kind"]] += 1
        result = post(sig)
        if result in ("created", "duplicate"):
            posts_tally[result] += 1
        elif result == "skip":
            posts_tally["skip"] += 1
            posts_clean = False
        else:
            posts_tally["error"] += 1
            posts_clean = False

    may_advance = read_errors == 0 and posts_clean
    new_cursor = _format_ts(pass_start - OVERLAP) if may_advance else cursor
    return {
        "signals": signals_tally,
        "posts": posts_tally,
        "read_errors": read_errors,
        "may_advance": may_advance,
        "new_cursor": new_cursor,
    }


# --- the cursor store ---------------------------------------------------------


def _cursor_path() -> Path:
    return STATE_DIR / CURSOR_FILE_NAME


def load_cursor() -> dict:
    """Every repository's stored cursor, keyed `"owner/repo"` -> an ISO
    timestamp, or `{}` when nothing has been recorded yet, the file cannot
    be read, or it is not valid JSON — all three mean "no repository has a
    cursor yet" to the caller."""
    try:
        raw = _cursor_path().read_text()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"github_activity: cursor file at {_cursor_path()} is corrupt; "
              f"treating every repository as having no cursor", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_cursor(cursor: dict) -> None:
    """Atomic write: a pid-unique temp file in `STATE_DIR`, then
    `os.replace` — a process killed mid-write must never leave the cursor
    file holding a truncated JSON fragment, which `load_cursor`'s broad
    `except` would read as "no repository has a cursor", triggering a
    refetch of every repository's `NO_CURSOR_WINDOW` (safe via `dedup_key`,
    but slow)."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)  # mkdir's mode is a no-op on an already-existing dir
    path = _cursor_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(cursor, sort_keys=True))
    os.replace(tmp, path)
