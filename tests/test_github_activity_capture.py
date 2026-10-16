"""`lib/agent-platform/github_activity.py`'s reads and cursor (E25,
issue_01M4ZB5C5ZX0MV206064MTRAE0) — the 5xx-retrying GitHub GET, the paged
repository-wide comment lists, the pull-request list's early stop at the
cursor, `capture_repository`'s per-repository pass, and the cursor store.

No network anywhere in this file: every test passes a fake `opener`
(matching `urllib.request.urlopen`'s call shape) and synthetic GitHub JSON —
no real repo names, logins or bodies. `tests/test_github_activity_signals.py`
covers the pure kind transforms this module also holds; this file covers
everything built on top of them.
"""
from __future__ import annotations

import datetime as dt
import stat
import urllib.parse

import pytest

import github_activity as ga


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Every test gets its own `STATE_DIR`, never the real
    `~/.local/state/agent-platform/github-activity-capture/`."""
    monkeypatch.setattr(ga, "STATE_DIR", tmp_path / "github-activity-capture-state")


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = ga.json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _never_sleep(seconds):
    raise AssertionError(f"must not sleep (asked to sleep {seconds}s)")


def _dt(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _poster(outcome="created"):
    """A `post` callable that records every payload it was given and always
    returns `outcome` — the fixed GitHub-level result `capture_repository`
    branches on."""
    posted = []

    def post(payload):
        posted.append(payload)
        return outcome

    post.posted = posted
    return post


def _pr(number, updated_at, merged_at=None, title="a change", body=""):
    return {
        "number": number,
        "updated_at": updated_at,
        "merged_at": merged_at,
        "title": title,
        "body": body,
        "user": {"login": "widget-author"},
        "html_url": f"https://github.com/acme/widget/pull/{number}",
    }


def _review(review_id, state, body=""):
    return {
        "id": review_id,
        "state": state,
        "body": body,
        "user": {"login": "widget-reviewer"},
        "html_url": "",
        "submitted_at": "2026-10-14T12:00:00Z",
    }


def _rc(comment_id, pr_number, body="a review comment"):
    return {
        "id": comment_id,
        "pull_request_url": f"https://api.github.com/repos/acme/widget/pulls/{pr_number}",
        "body": body,
        "user": {"login": "widget-reviewer"},
        "html_url": "",
        "created_at": "2026-10-14T12:00:00Z",
    }


def _ic(comment_id, pr_number, body="a pr comment"):
    return {
        "id": comment_id,
        "issue_url": f"https://api.github.com/repos/acme/widget/issues/{pr_number}",
        "body": body,
        "user": {"login": "widget-author"},
        "html_url": f"https://github.com/acme/widget/pull/{pr_number}#issuecomment-{comment_id}",
        "created_at": "2026-10-14T12:00:00Z",
    }


class _FakeGitHub:
    """Routes a fake `opener` call to canned pages by URL shape, and records
    every URL requested so a test can assert which endpoints were (or were
    not) hit."""

    def __init__(self, *, review_comments=None, conversation_comments=None,
                pr_pages=None, reviews=None):
        self.review_comments = review_comments if review_comments is not None else []
        self.conversation_comments = (conversation_comments
                                      if conversation_comments is not None else [])
        self.pr_pages = pr_pages if pr_pages is not None else [[]]
        self.reviews = reviews if reviews is not None else {}
        self.calls: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        path = urllib.parse.urlsplit(url).path
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if path.endswith("/pulls/comments"):
            return _Resp(200, self.review_comments)
        if path.endswith("/issues/comments"):
            return _Resp(200, self.conversation_comments)
        if path.endswith("/reviews"):
            number = int(path.rsplit("/pulls/", 1)[1].split("/", 1)[0])
            return _Resp(200, self.reviews.get(number, []))
        if path.endswith("/pulls"):
            page = int(query.get("page", "1"))
            idx = page - 1
            return _Resp(200, self.pr_pages[idx] if idx < len(self.pr_pages) else [])
        raise AssertionError(f"unexpected URL in test fake: {url}")


# --- _github_get(): the shared 5xx retry ------------------------------------


# Pinned literally (502, 503, 504), not parametrized from `ga._GH_RETRYABLE_STATUSES`
# itself — a test drawing its cases from the same set it is meant to check would
# stay green no matter what that set held, including an edit that silently
# widened or narrowed it.
@pytest.mark.parametrize("status", (502, 503, 504))
def test_github_get_retries_a_5xx_then_succeeds(status):
    calls = []
    slept = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            return _Resp(status, {"message": "retryable"})
        return _Resp(200, {"ok": True})

    out = ga._github_get("https://api.github.com/x", "tok", opener=opener,
                         sleep=slept.append)
    assert out == {"ok": True}
    assert len(calls) == 2, f"the {status} must be retried, not thrown away"
    assert slept == [ga._GH_5XX_RETRY_BACKOFF_SECONDS]


def test_github_get_500_is_not_retried():
    """500 is a server error too, but it is not in the retryable set — only
    502/503/504 (a gateway down, unavailable, or a timeout) are transient in
    the way that set exists to answer (issue_01M4XKXB63T8B4RJNAKFPQN7HF). A
    500 must raise on the first attempt, with no retry and no sleep."""
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(500, {"message": "internal server error"})

    with pytest.raises(ga.FetchError) as exc:
        ga._github_get("https://api.github.com/x", "tok", opener=opener,
                       sleep=_never_sleep)

    assert "500" in str(exc.value)
    assert len(calls) == 1, "a 500 must not be retried"


def test_github_get_502_three_times_raises_naming_the_url():
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(502, {"message": "bad gateway"})

    with pytest.raises(ga.FetchError) as exc:
        ga._github_get("https://api.github.com/x", "tok", opener=opener,
                       sleep=lambda s: None)

    assert "https://api.github.com/x" in str(exc.value)
    assert "502" in str(exc.value)
    assert len(calls) == ga._GH_5XX_RETRY_ATTEMPTS, (
        "must exhaust every retry attempt, no more and no fewer")


def test_github_get_404_raises_with_the_sleep_never_called():
    def opener(req, timeout=None):
        return _Resp(404, {"message": "not found"})

    with pytest.raises(ga.FetchError) as exc:
        ga._github_get("https://api.github.com/x", "tok", opener=opener,
                       sleep=_never_sleep)

    assert "404" in str(exc.value)


# --- fetch_review_comments() / fetch_conversation_comments(): paging --------


def test_fetch_review_comments_sends_since_sort_direction_and_pages():
    page1 = [_rc(i, 42) for i in range(100)]
    page2 = [_rc(999, 42)]
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        if query.get("page") == "2":
            return _Resp(200, page2)
        assert query["since"] == "2026-10-01T00:00:00Z"
        assert query["sort"] == "updated"
        assert query["direction"] == "asc"
        assert query["per_page"] == "100"
        return _Resp(200, page1)

    out = ga.fetch_review_comments("acme/widget", "2026-10-01T00:00:00Z", "tok",
                                   opener=opener, sleep=_never_sleep)
    assert len(out) == 101, "must keep paging until a page shorter than 100"
    assert len(calls) == 2


def test_fetch_conversation_comments_sends_since_sort_and_direction():
    captured = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp(200, [])

    ga.fetch_conversation_comments("acme/widget", "2026-10-01T00:00:00Z", "tok",
                                   opener=opener, sleep=_never_sleep)
    assert captured["url"].split("?")[0].endswith("/repos/acme/widget/issues/comments")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(captured["url"]).query))
    assert query["since"] == "2026-10-01T00:00:00Z"
    assert query["sort"] == "updated"
    assert query["direction"] == "asc"


# --- fetch_updated_pull_requests(): the early stop --------------------------


def test_fetch_updated_pull_requests_stops_at_the_cursor_and_never_requests_page_two():
    since = "2026-10-10T00:00:00Z"
    page1 = [
        _pr(103, "2026-10-12T00:00:00Z"),
        _pr(102, "2026-10-11T00:00:00Z"),
        _pr(101, "2026-10-09T00:00:00Z"),   # older than `since` — stop here
        _pr(100, "2026-10-01T00:00:00Z"),
    ]
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(200, page1)

    out = ga.fetch_updated_pull_requests("acme/widget", since, "tok",
                                         opener=opener, sleep=_never_sleep)
    assert [pr["number"] for pr in out] == [103, 102]
    assert len(calls) == 1, "page 2 must never be requested once an older PR is seen"


def test_fetch_updated_pull_requests_requests_the_next_page_when_page_one_is_full():
    since = "2026-01-01T00:00:00Z"
    page1 = [_pr(1000 - i, "2026-10-12T00:00:00Z") for i in range(ga._PAGE_SIZE)]
    page2 = [_pr(10, "2026-10-01T00:00:00Z")]
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        if query.get("page") == "2":
            return _Resp(200, page2)
        return _Resp(200, page1)

    out = ga.fetch_updated_pull_requests("acme/widget", since, "tok",
                                         opener=opener, sleep=_never_sleep)
    assert len(out) == ga._PAGE_SIZE + 1
    assert len(calls) == 2, "a full page must be followed by a second page"


def test_fetch_updated_pull_requests_includes_a_pull_request_tied_exactly_on_since():
    """The boundary is inclusive: `_parse_ts(updated_at) < since_dt` is a
    strict less-than, so a pull request updated in the same second as
    `since` is still "on/after since" and must be returned, not treated as
    the stop condition. A one-character change to `<=` would drop it while
    every other test here stays green."""
    since = "2026-10-10T00:00:00Z"
    page1 = [
        _pr(103, "2026-10-12T00:00:00Z"),  # newer than since
        _pr(102, since),                   # tied exactly on since — must be kept
        _pr(101, "2026-10-09T00:00:00Z"),  # older than since — stop here
        _pr(100, "2026-10-01T00:00:00Z"),
    ]
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(200, page1)

    out = ga.fetch_updated_pull_requests("acme/widget", since, "tok",
                                         opener=opener, sleep=_never_sleep)
    assert [pr["number"] for pr in out] == [103, 102], (
        "the pull request tied exactly on `since` must be included")
    assert len(calls) == 1, "page 2 must never be requested once an older PR is seen"


def test_fetch_updated_pull_requests_stops_partway_through_a_later_page():
    since = "2026-10-10T00:00:00Z"
    page1 = [_pr(1000 - i, "2026-10-12T00:00:00Z") for i in range(ga._PAGE_SIZE)]
    page2 = [
        _pr(50, "2026-10-11T00:00:00Z"),
        _pr(49, "2026-10-11T00:00:00Z"),
        _pr(48, "2026-09-01T00:00:00Z"),   # older than since — stop here
        _pr(47, "2026-08-01T00:00:00Z"),
    ]
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        if query.get("page") == "2":
            return _Resp(200, page2)
        if query.get("page") == "3":
            raise AssertionError("page 3 must never be requested")
        return _Resp(200, page1)

    out = ga.fetch_updated_pull_requests("acme/widget", since, "tok",
                                         opener=opener, sleep=_never_sleep)
    assert [pr["number"] for pr in out[-2:]] == [50, 49], (
        "only the newer page-2 pull requests must be kept")
    assert len(out) == ga._PAGE_SIZE + 2
    assert len(calls) == 2, "page 2 must be requested, page 3 must not"


# --- capture_repository(): early stop narrows which reviews are read -------


def test_capture_repository_reads_reviews_only_for_pull_requests_updated_since_the_cursor():
    since = "2026-10-10T00:00:00Z"
    pass_start = _dt("2026-10-15T00:00:00Z")
    fake = _FakeGitHub(
        pr_pages=[[_pr(2, "2026-10-12T00:00:00Z"), _pr(1, "2026-10-09T00:00:00Z")]],
        reviews={2: [_review(901, "APPROVED")], 1: [_review(902, "APPROVED")]},
    )
    post = _poster("created")

    result = ga.capture_repository("acme/widget", since, "tok", post, pass_start,
                                   opener=fake, sleep=_never_sleep)

    requested_review_paths = {urllib.parse.urlsplit(u).path for u in fake.calls
                              if urllib.parse.urlsplit(u).path.endswith("/reviews")}
    assert requested_review_paths == {"/repos/acme/widget/pulls/2/reviews"}
    assert result["signals"]["review_verdict"] == 1


# --- capture_repository(): the cursor rule ----------------------------------


def test_clean_pass_new_cursor_equals_pass_start_minus_ten_minutes():
    pass_start = _dt("2026-10-15T00:00:00Z")
    fake = _FakeGitHub()
    post = _poster("created")

    result = ga.capture_repository("acme/widget", "2026-10-14T00:00:00Z", "tok", post,
                                   pass_start, opener=fake, sleep=_never_sleep)

    assert result["may_advance"] is True
    assert result["new_cursor"] == "2026-10-14T23:50:00Z"


def test_repository_with_no_cursor_reads_from_pass_start_minus_24_hours():
    pass_start = _dt("2026-10-15T00:00:00Z")
    fake = _FakeGitHub()
    post = _poster("created")

    ga.capture_repository("acme/widget", None, "tok", post, pass_start,
                          opener=fake, sleep=_never_sleep)

    since_values = {dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["since"]
                    for u in fake.calls
                    if "since" in dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))}
    assert since_values == {"2026-10-14T00:00:00Z"}


def test_cursor_holds_on_a_failed_read_and_advances_on_the_next_clean_pass():
    pass_start = _dt("2026-10-15T00:00:00Z")
    cursor = "2026-10-14T00:00:00Z"
    post = _poster("created")

    def failing_opener(req, timeout=None):
        return _Resp(500, {"message": "boom"})

    held = ga.capture_repository("acme/widget", cursor, "tok", post, pass_start,
                                 opener=failing_opener, sleep=lambda s: None)
    assert held["read_errors"] > 0
    assert held["may_advance"] is False
    assert held["new_cursor"] == cursor, "a failed read must not move the cursor"

    fake = _FakeGitHub()
    advanced = ga.capture_repository("acme/widget", cursor, "tok", post, pass_start,
                                     opener=fake, sleep=_never_sleep)
    assert advanced["may_advance"] is True
    assert advanced["new_cursor"] == ga._format_ts(pass_start - ga.OVERLAP)


def test_cursor_holds_on_a_skipped_post():
    pass_start = _dt("2026-10-15T00:00:00Z")
    fake = _FakeGitHub(review_comments=[_rc(1, 42)])
    post = _poster("skip")

    result = ga.capture_repository("acme/widget", "2026-10-14T00:00:00Z", "tok", post,
                                   pass_start, opener=fake, sleep=_never_sleep)

    assert result["posts"]["skip"] == 1
    assert result["may_advance"] is False, "a 'skip' post must not count as success"


def test_cursor_holds_on_a_429_post():
    """ci-result-capture advances a batch's cursor through a sustained 429;
    E25 has no such exception, so a 429 here holds the cursor like any other
    post failure."""
    pass_start = _dt("2026-10-15T00:00:00Z")
    fake = _FakeGitHub(review_comments=[_rc(1, 42)])
    post = _poster("error:429")

    result = ga.capture_repository("acme/widget", "2026-10-14T00:00:00Z", "tok", post,
                                   pass_start, opener=fake, sleep=_never_sleep)

    assert result["posts"]["error"] == 1
    assert result["may_advance"] is False


# --- the cursor file ---------------------------------------------------------


def test_cursor_round_trips_through_the_state_file():
    ga.save_cursor({"acme/widget": "2026-10-14T00:00:00Z"})
    assert ga.load_cursor() == {"acme/widget": "2026-10-14T00:00:00Z"}


def test_load_cursor_with_no_file_is_empty():
    assert ga.load_cursor() == {}


def test_load_cursor_with_a_corrupt_file_is_empty():
    ga.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ga.STATE_DIR / ga.CURSOR_FILE_NAME).write_text("{not json")
    assert ga.load_cursor() == {}


def test_save_cursor_writes_via_a_temp_file_and_replace():
    ga.save_cursor({"acme/widget": "2026-10-14T00:00:00Z"})
    leftovers = list(ga.STATE_DIR.glob("*.tmp"))
    assert leftovers == [], "a temp file must not survive a successful save"


def test_save_cursor_sets_directory_and_file_modes():
    ga.save_cursor({"acme/widget": "2026-10-14T00:00:00Z"})
    assert stat.S_IMODE(ga.STATE_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE((ga.STATE_DIR / ga.CURSOR_FILE_NAME).stat().st_mode) == 0o600
