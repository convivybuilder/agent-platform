"""`bin/ci-result-capture` (CQ-455, issue_01M4J1ASMSFFPC6S0RQ6VVVT57) — the
Bosun's read-only, convivy-bosun-App-authenticated capture of every tracked
repository's CI results, posted to the Library as `kind: test-result`
signals.

No network and no `gh` binary anywhere in this file. `gh_runs` and the
signal poster are the two I/O seams (mirroring `test_ci_result_fetch.py` in
the library repo, the shape this module's cursor logic is carried over
from) — every test here either calls a pure transform directly or passes a
fake through `capture_workflow`'s `fetcher`/`poster` parameters, or drives
`main()` end to end with the GitHub-App calls and `urllib.request.urlopen`
both faked.

Covers, by name, the done-when this file was written against:
  - test_signal_from_run_matches_the_library_fetcher_s_contract_field_for_field
  - test_an_in_progress_run_is_not_posted_and_holds_the_cursor_at_its_created_at
  - test_an_in_flight_run_is_posted_once_a_later_poll_sees_it_complete
  - test_a_stale_pending_run_is_reported_and_stops_holding_the_cursor
  - test_a_stale_pending_run_s_line_does_not_claim_the_cursor_advances_when_a_post_also_fails
  - test_a_stale_pending_run_alone_still_lets_the_cursor_advance
  - test_a_post_error_leaves_the_pair_s_cursor_unadvanced
  - test_a_repost_of_the_same_run_sends_the_same_dedup_key
  - test_gh_runs_below_the_cap_costs_exactly_one_request
  - test_gh_runs_splits_a_capped_window_and_dedupes_the_shared_boundary
  - test_a_capped_range_under_one_second_wide_raises_and_leaves_the_cursor_unadvanced
  - test_main_does_not_abort_the_run_when_one_pair_raises_unexpectedly
"""
from __future__ import annotations

import datetime as real_datetime
import json
import urllib.parse

import pytest


@pytest.fixture(autouse=True)
def _state_dir(ci_result_capture, tmp_path, monkeypatch):
    """Every test gets its own `STATE_DIR`, never the real
    `~/.local/state/agent-platform/ci-result-capture/` —
    `ci_result_capture` is a session-scoped module fixture, so without this
    a test calling `main()`/`save_cursor` would write real cursor state
    under the operator's home directory."""
    monkeypatch.setattr(ci_result_capture, "STATE_DIR", tmp_path / "ci-result-capture-state")


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(run_id=900001, conclusion="success", status="completed",
        created_at="2026-10-01T12:00:00Z", run_number=42, event="pull_request",
        head_sha="abc1234567890abc1234567890abc1234567890"):
    """One workflow run, shaped the way `GET
    /repos/.../actions/workflows/.../runs` returns it — the real response
    this module reads (see `ci_result_fetch.py`'s own `_run` fixture in the
    library repo for the same shape)."""
    return {
        "id": run_id,
        "head_sha": head_sha,
        "conclusion": conclusion,
        "status": status,
        "run_number": run_number,
        "event": event,
        "created_at": created_at,
        "html_url": f"https://github.com/convivy/vivy/actions/runs/{run_id}",
    }


def fetcher_returning(runs):
    def _fetch(repo, workflow_file, since, token):
        return runs
    return _fetch


class _FrozenClock(real_datetime.datetime):
    """Subclasses the real `datetime.datetime` so everything else the module
    under test does through `dt.datetime.*` (fromisoformat, strftime,
    arithmetic against a real timedelta) behaves exactly as in production;
    only `now()` is pinned — the same `_FrozenClock`/`_FrozenDt` pair
    `ci_result_fetch.py`'s own test file uses to pin `gh_runs`'s "now" for
    the created-filter-cap split without touching the real clock anywhere
    else in the process."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


class _FrozenDt:
    """Stands in for the module's own `dt` (the whole `datetime` module as
    it sees it) — only `.datetime` is swapped for `_FrozenClock`; `.timedelta`
    and `.timezone` stay the real ones (e.g. the module-level `OVERLAP`
    constant, built at import time from the real module, keeps working
    unmodified against the frozen clock)."""
    datetime = _FrozenClock
    timedelta = real_datetime.timedelta
    timezone = real_datetime.timezone


def _parse_created_filter(value):
    """`value` is already url-decoded by `urllib.parse.parse_qs` — either
    `>=<since>` or `<since>..<until>`, the only two shapes `gh_runs`/
    `_gh_runs_window` ever send."""
    if ".." in value:
        lo_s, hi_s = value.split("..", 1)
        return real_datetime.datetime.fromisoformat(lo_s.replace("Z", "+00:00")), \
            real_datetime.datetime.fromisoformat(hi_s.replace("Z", "+00:00"))
    if value.startswith(">="):
        return real_datetime.datetime.fromisoformat(
            value[2:].replace("Z", "+00:00")), None
    raise AssertionError(f"unexpected created filter: {value!r}")


class _FakeGitHubActions:
    """`cap`, when given, reproduces GitHub's own `created`-filter cap
    (`GITHUB_CREATED_FILTER_CAP` in the module under test): any query whose
    filter matches more than `cap` runs is truncated to the newest `cap` of
    them, the same silent truncation the real API performs — `runs` must
    therefore be supplied newest-first for that truncation to keep the
    right ones. Only one page's worth of runs is ever configured here (well
    under `_PAGE_SIZE`), so each query still costs exactly one HTTP call."""

    def __init__(self, runs, cap=None):
        self.runs = runs
        self.cap = cap
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        created = params.get("created", [None])[0]
        page = int(params.get("page", ["1"])[0])
        per_page = int(params.get("per_page", ["100"])[0])
        lo, hi = _parse_created_filter(created) if created else (None, None)
        matched = []
        for run in self.runs:
            run_dt = real_datetime.datetime.fromisoformat(
                run["created_at"].replace("Z", "+00:00"))
            if lo is not None and run_dt < lo:
                continue
            if hi is not None and run_dt > hi:
                continue
            matched.append(run)
        if self.cap is not None and len(matched) > self.cap:
            matched = matched[:self.cap]
        start = (page - 1) * per_page
        return _Resp(200, {"workflow_runs": matched[start:start + per_page]})


class _FakeSignalStore:
    """Mirrors the real signal store's `dedup_key` idempotency: the first
    post of a key is 'created' and stored; every later one is 'duplicate'.
    `fail_dedup_keys` names keys this fake always refuses (an ordinary
    transient post failure, never stored), so a test can prove a failed
    post does not get treated as delivered."""

    def __init__(self, fail_dedup_keys=()):
        self.rows: dict[str, dict] = {}
        self.calls: list[str] = []
        self._fail = set(fail_dedup_keys)

    def post_signal(self, payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        key = payload["dedup_key"]
        self.calls.append(key)
        if key in self._fail:
            return "error:500"
        if key in self.rows:
            return "duplicate"
        self.rows[key] = payload
        return "created"


# --- pure transforms ---------------------------------------------------------


def test_workflow_slug_strips_the_directory_and_extension(ci_result_capture):
    assert ci_result_capture.workflow_slug("ci.yml") == "ci"
    assert ci_result_capture.workflow_slug("e2e-stage-smoke.yml") == "e2e-stage-smoke"


def test_conclusion_to_meta_status_maps_only_success_and_failure(ci_result_capture):
    assert ci_result_capture.conclusion_to_meta_status("success") == "pass"
    assert ci_result_capture.conclusion_to_meta_status("failure") == "fail"
    assert ci_result_capture.conclusion_to_meta_status("cancelled") is None
    assert ci_result_capture.conclusion_to_meta_status(None) is None


def test_signal_from_run_matches_the_library_fetcher_s_contract_field_for_field(ci_result_capture):
    """Byte-for-byte against the contract CQ-455 names (also
    `ci_result_fetch.signal_from_run`'s own shape in the library repo), so
    both jobs can run during changeover with no duplicate row."""
    run = _run(run_id=900001, conclusion="success", status="completed",
               created_at="2026-10-01T12:00:00Z", run_number=42, event="pull_request",
               head_sha="abc1234567890abc1234567890abc1234567890")
    signal = ci_result_capture.signal_from_run(run, "vivy", "ci")
    assert signal == {
        "source": "ci",
        "kind": "test-result",
        "product": "vivy",
        "stream": "ci",
        "actor": "github-actions",
        "body": "ci: success",
        "refs": {
            "conclusion": "success",
            "workflow": "ci",
            "commit": "abc1234567890abc1234567890abc1234567890",
            "run_url": "https://github.com/convivy/vivy/actions/runs/900001",
        },
        "meta": {
            "run_number": 42,
            "event": "pull_request",
            "run_url": "https://github.com/convivy/vivy/actions/runs/900001",
            "status": "pass",
        },
        "produced_at": "2026-10-01T12:00:00Z",
        "dedup_key": "ci:vivy:ci:900001",
    }


def test_signal_from_run_omits_meta_status_for_an_unmapped_conclusion(ci_result_capture):
    run = _run(conclusion="cancelled")
    signal = ci_result_capture.signal_from_run(run, "vivy", "ci")
    assert "status" not in signal["meta"]
    assert signal["refs"]["conclusion"] == "cancelled"


def test_signal_from_run_raises_on_a_missing_id(ci_result_capture):
    run = _run()
    del run["id"]
    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.signal_from_run(run, "vivy", "ci")


def test_signal_from_run_raises_on_a_missing_created_at(ci_result_capture):
    run = _run()
    del run["created_at"]
    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.signal_from_run(run, "vivy", "ci")


# --- gh_runs(): the GET, its query params, pagination -----------------------


def test_gh_runs_fetches_every_status_not_just_completed(ci_result_capture):
    """The root cause of the production bug this cursor design is carried
    over to avoid (see the module docstring): the query must never bake in
    `status=completed`, so an in-flight run is seen at all."""
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(200, {"workflow_runs": [_run()]})

    out = ci_result_capture.gh_runs(
        "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert len(out) == 1
    assert "status=" not in seen["url"]
    assert "created=" in seen["url"]
    assert "repos/convivy/vivy/actions/workflows/ci.yml/runs" in seen["url"]


def test_gh_runs_sends_a_bearer_token(ci_result_capture):
    seen = {}

    def opener(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(200, {"workflow_runs": []})

    ci_result_capture.gh_runs("convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z",
                             "ghs_x", opener=opener)
    assert seen["auth"] == "Bearer ghs_x"


def test_gh_runs_paginates_until_a_short_page(ci_result_capture):
    page1 = [_run(run_id=1000 + i) for i in range(100)]
    page2 = [_run(run_id=2000), _run(run_id=2001)]
    pages = {1: page1, 2: page2}
    calls = []

    def opener(req, timeout=None):
        page = int(req.full_url.split("&page=")[1].split("&")[0])
        calls.append(page)
        return _Resp(200, {"workflow_runs": pages[page]})

    out = ci_result_capture.gh_runs(
        "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert calls == [1, 2]
    assert len(out) == 102


def test_gh_runs_a_non_200_raises_capture_error(ci_result_capture):
    def opener(req, timeout=None):
        return _Resp(403, {"message": "nope"})

    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.gh_runs("convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z",
                                 "ghs_x", opener=opener)


# --- gh_runs(): retrying a transient 5xx page (issue_01M4XKXB63T8B4RJNAKFPQN7HF) --


def _never_sleep(seconds):
    raise AssertionError(f"must not sleep (asked to sleep {seconds}s)")


def test_gh_runs_retries_a_502_page_and_completes(ci_result_capture, monkeypatch):
    """The occurrence this fix answers: one transient 502 on a page must not
    throw away the fetch. The same page succeeding on a later attempt
    returns every run GitHub ever had for it, not just what the first
    attempt saw (on a254a3f5, with no retry at all, this raises
    `CaptureError` on the first 502 instead)."""
    monkeypatch.setattr(ci_result_capture.time, "sleep", lambda seconds: None)
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            return _Resp(502, {"message": "bad gateway"})
        return _Resp(200, {"workflow_runs": [_run(run_id=1), _run(run_id=2)]})

    out = ci_result_capture.gh_runs(
        "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

    assert sorted(r["id"] for r in out) == [1, 2]
    assert len(calls) == 2, "the 502 must be retried on the same page, not skipped"


def test_gh_runs_a_502_on_every_attempt_raises_naming_the_page_and_filter(
        ci_result_capture, monkeypatch):
    """Retries exhausted (`_GH_5XX_RETRY_ATTEMPTS`) must still raise, naming
    the page and the `created` filter so a held pair's stderr says which
    request failed — and must not retry forever (on a254a3f5 this already
    raises on the very first attempt, so `len(calls) == 1` there, not
    `_GH_5XX_RETRY_ATTEMPTS`, and the message lacks the page and filter)."""
    monkeypatch.setattr(ci_result_capture.time, "sleep", lambda seconds: None)
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(502, {"message": "bad gateway"})

    with pytest.raises(ci_result_capture.CaptureError) as exc:
        ci_result_capture.gh_runs(
            "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

    message = str(exc.value)
    assert "page 1" in message
    assert "502" in message
    assert "created=" in message
    assert len(calls) == ci_result_capture._GH_5XX_RETRY_ATTEMPTS, (
        "must exhaust every retry attempt, no more and no fewer")


def test_gh_runs_a_404_raises_on_the_first_attempt_with_no_retry(
        ci_result_capture, monkeypatch):
    """A 4xx is not the transient failure this fix answers — it still raises
    at once, with no retry and no sleep (unchanged from a254a3f5)."""
    monkeypatch.setattr(ci_result_capture.time, "sleep", _never_sleep)
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(404, {"message": "not found"})

    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.gh_runs(
            "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

    assert len(calls) == 1, "a 404 must not be retried"


# --- gh_runs(): GitHub's own created-filter cap (reviewer, PR #877 round 1) --


def test_gh_runs_below_the_cap_costs_exactly_one_request(ci_result_capture):
    """The ordinary case — every tracked pair's incremental poll, and most
    pairs' very first fetch — must cost exactly the one `>=since` query
    `gh_runs` always made before this fix, not an extra round-trip to find
    out the window is small."""
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(200, {"workflow_runs": [_run(run_id=1), _run(run_id=2)]})

    out = ci_result_capture.gh_runs(
        "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert len(out) == 2
    assert len(calls) == 1, "a window below the cap must cost exactly one query"


def test_gh_runs_splits_a_capped_window_and_dedupes_the_shared_boundary(
        ci_result_capture, monkeypatch):
    """**The bug this PR exists to fix.** GitHub caps a `created`-filtered
    query at `GITHUB_CREATED_FILTER_CAP` results and returns the newest of
    them with no error — before this fix, `gh_runs` trusted that truncated
    page as the whole window, and `capture_workflow`'s cursor would then
    advance past every OLDER run the cap hid (reachable on this job's first
    run for `convivy/knowledge#ci.yml`, the pair the Library's own fetcher
    measured hitting the cap on 2026-10-01). The cap is lowered to 2 here so
    a small, fast fixture exercises the real split rather than needing 1001
    fake runs; `run_id=3`, created exactly at the midpoint the first split
    produces, sits on the shared inclusive boundary between the two halves
    and must still come back exactly once."""
    monkeypatch.setattr(ci_result_capture, "GITHUB_CREATED_FILTER_CAP", 2)
    monkeypatch.setattr(ci_result_capture, "dt", _FrozenDt)
    _FrozenClock._frozen = real_datetime.datetime(
        2026, 10, 1, 0, 0, 8, tzinfo=real_datetime.timezone.utc)
    runs = [  # newest first, matching GitHub's own ordering
        _run(run_id=5, created_at="2026-10-01T00:00:08Z"),
        _run(run_id=4, created_at="2026-10-01T00:00:06Z"),
        _run(run_id=3, created_at="2026-10-01T00:00:04Z"),  # the shared boundary
        _run(run_id=2, created_at="2026-10-01T00:00:02Z"),
        _run(run_id=1, created_at="2026-10-01T00:00:00Z"),
    ]
    gh = _FakeGitHubActions(runs, cap=2)

    out = ci_result_capture.gh_runs(
        "convivy/vivy", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=gh)

    assert sorted(r["id"] for r in out) == [1, 2, 3, 4, 5], (
        "every run in a window over GitHub's own cap must still come back, "
        "not just the newest `cap` of them the first query alone returned "
        "(on the unfixed code this assertion is the one that goes red: out "
        "== [id 5, id 4] only, the newest 2)")
    assert len(gh.urls) > 1, (
        "plant check: the split path must actually have run more than the "
        "one initial query")


def test_a_capped_range_under_one_second_wide_raises_and_leaves_the_cursor_unadvanced(
        ci_result_capture, monkeypatch, capsys):
    """**The terminal fail-closed branch.** A query whose range has already
    been split down to under one second wide and STILL comes back at the
    cap cannot be split any further and cannot prove it covered its range —
    `_gh_runs_window` must raise rather than trust it, and `capture_workflow`
    must then leave this pair's cursor exactly where it was, the same as any
    other `CaptureError`. The fake `gh` here always reports exactly the cap,
    no matter how narrow the queried range is, forcing every split to keep
    splitting until the one-second floor is hit (on the unfixed code — which
    has no split at all — this scenario cannot even arise, since the first
    capped query is simply returned as the whole window; this test's
    `errors == 1`/`next_since is None` assertions go red there because
    `gh_runs` never raises and `capture_workflow` reports a clean run)."""
    monkeypatch.setattr(ci_result_capture, "GITHUB_CREATED_FILTER_CAP", 2)
    monkeypatch.setattr(ci_result_capture, "dt", _FrozenDt)
    _FrozenClock._frozen = real_datetime.datetime(
        2026, 10, 1, 0, 0, 8, tzinfo=real_datetime.timezone.utc)

    def opener(req, timeout=None):
        return _Resp(200, {"workflow_runs": [_run(run_id=1), _run(run_id=2)]})

    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=lambda repo, wf, since, token: ci_result_capture.gh_runs(
            repo, wf, since, token, opener=opener),
        poster=store.post_signal)

    assert tally["errors"] == 1
    assert tally["next_since"] is None, (
        "the pair's cursor must not move past a range this call could not "
        "prove it fetched in full")
    assert store.rows == {}
    err = capsys.readouterr().err
    assert "under one second wide" in err and "cannot split it any further" in err, (
        "must fail via the dedicated terminal branch's own message, not via "
        "some other error (e.g. a RecursionError from an unbounded "
        "recursion) that happens to tally the same error count")


# --- capture_workflow(): cursor invariants ------------------------------------


def test_capture_workflow_posts_one_signal_per_completed_run(ci_result_capture):
    runs = [_run(run_id=1), _run(run_id=2), _run(run_id=3)]
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=store.post_signal)
    assert tally["created"] == 3
    assert tally["duplicate"] == 0
    assert tally["errors"] == 0
    assert len(store.rows) == 3


def test_an_in_progress_run_is_not_posted_and_holds_the_cursor_at_its_created_at(ci_result_capture):
    in_flight = _run(run_id=1, conclusion=None, status="in_progress",
                     created_at="2026-10-01T10:00:00Z")
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal,
        run_started=run_started)
    assert tally["created"] == 0
    assert store.rows == {}
    assert tally["stale_pending"] == 0
    assert tally["next_since"] == "2026-10-01T10:00:00Z", (
        "the cursor must hold back to the in-flight run's own created_at, "
        "not advance past it on wall-clock time alone")


def test_an_in_flight_run_is_posted_once_a_later_poll_sees_it_complete(ci_result_capture):
    """The sequence the cursor design exists for: poll 1 sees the run still
    running and holds the cursor at its created_at; poll 2, re-querying
    from that exact cursor, sees it completed and posts it."""
    store = _FakeSignalStore()
    poll_1_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    in_flight = _run(run_id=1, conclusion=None, status="in_progress",
                     created_at="2026-10-01T10:00:00Z")
    tally_1 = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal,
        run_started=poll_1_started)
    assert store.rows == {}
    assert tally_1["errors"] == 0

    poll_2_started = real_datetime.datetime(2026, 10, 1, 10, 35, tzinfo=real_datetime.timezone.utc)
    now_complete = _run(run_id=1, conclusion="success", status="completed",
                       created_at="2026-10-01T10:00:00Z")
    tally_2 = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", tally_1["next_since"], "ghs_x", "sig_tok",
        fetcher=fetcher_returning([now_complete]), poster=store.post_signal,
        run_started=poll_2_started)
    assert tally_2["created"] == 1
    assert len(store.rows) == 1
    assert next(iter(store.rows))== "ci:vivy:ci:1"


def test_a_stale_pending_run_is_reported_and_stops_holding_the_cursor(ci_result_capture, capsys):
    """A run stuck `in_progress` well past `STALE_PENDING_AFTER` must not
    freeze the cursor forever — tallied `stale_pending` (loud, not silent)
    and excluded from the watermark, so the cursor still advances."""
    stuck = _run(run_id=1, conclusion=None, status="in_progress",
                created_at="2026-09-01T00:00:00Z")  # far older than the ceiling
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([stuck]), poster=store.post_signal,
        run_started=run_started)
    assert tally["stale_pending"] == 1
    assert tally["created"] == 0
    assert tally["errors"] == 0
    assert tally["next_since"] == (
        run_started - ci_result_capture.OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ"), (
        "nothing legitimately in flight — the cursor must fall back to the "
        "ordinary overlap margin, not stay pinned to the stuck run")
    assert "STALE_PENDING" in capsys.readouterr().err


def test_a_stale_pending_run_s_line_does_not_claim_the_cursor_advances_when_a_post_also_fails(
        ci_result_capture, capsys):
    """The STALE_PENDING line is printed before this pair's posting loop
    even runs, so it cannot know yet whether an ordinary post failure later
    in the same batch will hold the cursor back. On
    aa675f1282911b84f4203fa813317da0a2ed66ca the line always says "cursor
    advances past it", which is false here: an ordinary post failure in the
    same batch leaves `main()`'s cursor exactly where it was — this is the
    assertion that goes red there."""
    stuck = _run(run_id=1, conclusion=None, status="in_progress",
                created_at="2026-09-01T00:00:00Z")  # far older than the ceiling
    failing = _run(run_id=2, created_at="2026-10-01T09:00:00Z")
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore(fail_dedup_keys={
        ci_result_capture.signal_from_run(failing, "vivy", "ci")["dedup_key"]})
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([stuck, failing]), poster=store.post_signal,
        run_started=run_started)
    assert tally["stale_pending"] == 1
    assert tally["errors"] == 1
    stderr = capsys.readouterr().err
    assert "STALE_PENDING" in stderr
    assert "cursor advances" not in stderr, (
        "an ordinary post failure in this same batch holds the cursor back "
        "— the stale-pending line must not claim it advances"
    )


def test_a_stale_pending_run_alone_still_lets_the_cursor_advance(ci_result_capture, capsys):
    """Twin of the test above with no ordinary post failure in the batch:
    whatever the stale-pending line says must be true of the persisted
    cursor here too. `main()` advances a pair's cursor when `errors == 0`,
    and a stale-pending run alone does not set `errors`, so the cursor
    moves to the ordinary overlap margin."""
    stuck = _run(run_id=1, conclusion=None, status="in_progress",
                created_at="2026-09-01T00:00:00Z")  # far older than the ceiling
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([stuck]), poster=store.post_signal,
        run_started=run_started)
    assert tally["stale_pending"] == 1
    assert tally["errors"] == 0
    assert tally["next_since"] == (
        run_started - ci_result_capture.OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "STALE_PENDING" in capsys.readouterr().err


def test_a_post_error_leaves_the_pair_s_cursor_unadvanced(ci_result_capture):
    """`capture_workflow` itself still computes `next_since`, but `main()`
    only persists a pair's cursor when its `errors` tally is zero — a post
    failure must leave that pair's cursor exactly where it was so the next
    poll re-covers the window rather than skipping over a run that was
    never actually delivered."""
    run = _run(run_id=1)
    key = ci_result_capture.signal_from_run(run, "vivy", "ci")["dedup_key"]
    store = _FakeSignalStore(fail_dedup_keys={key})
    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    assert tally["errors"] == 1
    assert tally["created"] == 0
    assert store.rows == {}
    assert key in store.calls, "the post must actually have been attempted"


def test_a_repost_of_the_same_run_sends_the_same_dedup_key(ci_result_capture):
    run = _run(run_id=1)
    store = _FakeSignalStore()
    first = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    second = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    assert first["created"] == 1
    assert second["duplicate"] == 1
    assert store.calls == ["ci:vivy:ci:1", "ci:vivy:ci:1"]
    assert len(store.rows) == 1


def test_a_sustained_429_stops_the_batch_and_is_tallied_as_an_error(ci_result_capture, capsys):
    runs = [_run(run_id=1), _run(run_id=2)]

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return "error:429"

    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster)
    assert tally["errors"] == 1
    assert tally["created"] == 0
    assert "sustained 429" in capsys.readouterr().err


# --- CQ-455: a rate-limited batch still advances through what it delivered --


def test_a_sustained_429_advances_the_cursor_to_the_first_unposted_run(
        ci_result_capture, monkeypatch, capsys):
    """**The behaviour this fix adds.** Five completed runs, posted
    created/duplicate/error:429 in that order: the loop stops on the third,
    and the cursor this pair's caller should persist is that third run's own
    `created_at`, not held all the way back to `since` the way the unfixed
    code holds it (on origin/main `next_since` is computed from
    `run_started`/in-flight runs alone with no notion of "the first unposted
    run", so it does not equal `runs[2]["created_at"]` here — this is the
    assertion that goes red there). `main()` must then actually persist it
    for a pair whose only error is this one."""
    runs = [_run(run_id=i, created_at=f"2026-10-01T10:0{i}:00Z") for i in range(5)]
    results = iter(["created", "duplicate", "error:429"])

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return next(results)

    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster)
    assert tally["next_since"] == runs[2]["created_at"]
    assert tally["errors"] == 1
    assert tally["rate_limited"] is True
    assert "cursor advanced through what was delivered" in capsys.readouterr().err, (
        "no ordinary post failure preceded this 429, so the stderr line must "
        "say the cursor advanced")

    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},))
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: runs)
    results2 = iter(["created", "duplicate", "error:429"])
    monkeypatch.setattr(ci_result_capture.SC, "post_signal",
                        lambda payload, **k: next(results2))
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 1, "a pass that hit a sustained 429 must still exit loud"
    cursor = ci_result_capture.load_cursor()
    assert cursor["convivy/vivy#ci.yml"] == runs[2]["created_at"], (
        "main() must persist the advanced cursor for a pair whose only "
        "error is the sustained 429")


def test_a_sustained_429_s_cursor_is_bounded_by_an_earlier_in_flight_run(
        ci_result_capture):
    """Same shape as the test above, but an in-flight run created before the
    third (soon-to-be-unposted) run is in the same batch — the existing
    in-flight watermark rule still wins, per the module docstring's
    cursor-invariants section: the cursor is never later than the earliest
    run still running. On origin/main this does not hold either, for the
    same reason as the test above."""
    completed = [_run(run_id=i, created_at=f"2026-10-01T10:0{i}:00Z") for i in range(5)]
    in_flight = _run(run_id=99, conclusion=None, status="in_progress",
                     created_at="2026-10-01T10:01:30Z")  # between runs[1] and runs[2]
    runs = completed + [in_flight]
    results = iter(["created", "duplicate", "error:429"])
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return next(results)

    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster, run_started=run_started)
    assert tally["next_since"] == in_flight["created_at"], (
        "the in-flight run is older than the first unposted completed run, "
        "so it is the one that must bound the cursor")
    assert tally["rate_limited"] is True


def test_an_ordinary_post_failure_in_the_same_batch_still_blocks_the_advance(
        ci_result_capture, monkeypatch, capsys):
    """A batch with an ordinary (non-429) post failure never gets the
    sustained-429 exception, even when a 429 also happens later in the same
    batch — this is already origin/main's behaviour (an ordinary failure
    already blocks the advance there), so this is not expected to go red;
    it pins the boundary of the new exception instead."""
    runs = [_run(run_id=i, created_at=f"2026-10-01T10:0{i}:00Z") for i in range(3)]
    results = iter(["error:500", "created", "error:429"])

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return next(results)

    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster)
    assert tally["errors"] == 2
    assert tally.get("rate_limited") is not True
    stderr = capsys.readouterr().err
    assert "cursor advanced through what was delivered" not in stderr, (
        "an ordinary post failure preceded this 429, so the stderr line "
        "must not claim the cursor advanced — this is the assertion that "
        "goes red against 0131b5606faa9c4d7f48732dba3619934794f830"
    )
    assert "cursor stays where it was" in stderr

    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},))
    ci_result_capture.save_cursor({"convivy/vivy#ci.yml": "2026-10-01T00:00:00Z"})
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: runs)
    results2 = iter(["error:500", "created", "error:429"])
    monkeypatch.setattr(ci_result_capture.SC, "post_signal",
                        lambda payload, **k: next(results2))
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 1
    cursor = ci_result_capture.load_cursor()
    assert cursor["convivy/vivy#ci.yml"] == "2026-10-01T00:00:00Z", (
        "an ordinary post failure anywhere in the batch must keep today's "
        "behaviour: the cursor does not move at all, 429 in the same batch "
        "or not")


def test_completed_runs_are_posted_oldest_created_at_first(ci_result_capture):
    """`gh_runs` (and GitHub itself) returns newest first; `capture_workflow`
    must sort explicitly before posting, since the sustained-429 cursor
    advance above only holds when the first unposted run is also the oldest
    one. On origin/main there is no sort at all, so the runs are posted in
    whatever order `fetcher` returned them — newest first here — and this
    assertion goes red."""
    runs = [  # newest first, matching GitHub's own ordering
        _run(run_id=3, created_at="2026-10-01T10:02:00Z"),
        _run(run_id=2, created_at="2026-10-01T10:01:00Z"),
        _run(run_id=1, created_at="2026-10-01T10:00:00Z"),
    ]
    posted_order = []

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        posted_order.append(payload["dedup_key"])
        return "created"

    ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster)
    assert posted_order == [
        "ci:vivy:ci:1", "ci:vivy:ci:2", "ci:vivy:ci:3",
    ], "runs must be posted oldest created_at first, not in fetch order"


def test_a_shared_per_pass_rate_limit_still_lets_every_pair_catch_up(
        ci_result_capture):
    """**The test of the defect (CQ-455).** Three tracked pairs, each with
    more completed runs than one pass's shared post budget can deliver —
    the same shape as the measured production run, where the pairs early in
    `TRACKED` finish and advance while the later ones run out of budget at
    the same point every pass. With the fix, every pair's cursor reaches its
    newest run within a bounded number of passes, because a rate-limited
    pair keeps the ground it gained instead of re-asking for the same
    already-delivered prefix forever. On origin/main (`cursor[key] =
    t["next_since"]` only when `t["errors"] == 0`) the last pair's cursor
    never moves past its very first fetch no matter how many passes run —
    this test's final assertion is the one that goes red there; written
    with `t.get("rate_limited")` rather than `t["rate_limited"]` so it fails
    that assertion on the unfixed tally shape rather than raising a
    KeyError."""
    pair_count = 3
    runs_per_pair = 10
    pairs = [
        {"repo": f"convivy/pair{i}", "workflow_file": "ci.yml", "product": f"p{i}"}
        for i in range(pair_count)
    ]
    all_runs = {
        p["repo"]: [
            _run(run_id=1000 * i + j, created_at=f"2026-10-01T{10 + j:02d}:00:00Z")
            for j in range(runs_per_pair)
        ]
        for i, p in enumerate(pairs)
    }

    def fetcher_for(repo):
        def _fetch(r, workflow_file, since, token):
            return [run for run in all_runs[repo] if run["created_at"] >= since]
        return _fetch

    class _BudgetedPoster:
        """One shared budget per pass, the same shared rate limiter CQ-455
        measured — not a separate budget per pair."""

        def __init__(self, per_pass):
            self.per_pass = per_pass
            self.remaining = per_pass
            self.rows: dict[str, dict] = {}

        def post_signal(self, payload, *, tok=None, timeout=None, deadline=None,
                        sleep=None):
            if self.remaining <= 0:
                return "error:429"
            self.remaining -= 1
            key = payload["dedup_key"]
            if key in self.rows:
                return "duplicate"
            self.rows[key] = payload
            return "created"

    poster = _BudgetedPoster(per_pass=5)
    no_cursor_since = "2026-10-01T00:00:00Z"
    cursor: dict[str, str] = {}
    max_passes = 50
    for _ in range(max_passes):
        poster.remaining = poster.per_pass
        for p in pairs:
            key = f"{p['repo']}#{p['workflow_file']}"
            since = cursor.get(key, no_cursor_since)
            t = ci_result_capture.capture_workflow(
                p["repo"], p["workflow_file"], p["product"], since, "ghs_x", "sig_tok",
                fetcher=fetcher_for(p["repo"]), poster=poster.post_signal)
            if t["errors"] == 0 or t.get("rate_limited"):
                cursor[key] = t["next_since"]
        newest = {p["repo"]: all_runs[p["repo"]][-1]["created_at"] for p in pairs}
        if all(cursor.get(f"{p['repo']}#{p['workflow_file']}", "") > newest[p["repo"]]
              for p in pairs):
            break

    last_key = f"{pairs[-1]['repo']}#{pairs[-1]['workflow_file']}"
    assert cursor.get(last_key, "") > all_runs[pairs[-1]["repo"]][-1]["created_at"], (
        "every pair, including the last one in TRACKED, must reach its "
        "newest run within a bounded number of passes even under a shared "
        "per-pass post budget")


def test_a_fetch_failure_tallies_an_error_and_sets_no_next_since(ci_result_capture):
    def fetcher(repo, workflow_file, since, token):
        raise ci_result_capture.CaptureError("boom")

    tally = ci_result_capture.capture_workflow(
        "convivy/vivy", "ci.yml", "vivy", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher, poster=_FakeSignalStore().post_signal)
    assert tally["errors"] == 1
    assert tally["next_since"] is None


def test_a_self_hosted_style_entry_does_not_inherit_the_default_staleness_ceiling(ci_result_capture):
    in_flight = _run(run_id=1, conclusion=None, status="in_progress",
                     created_at="2026-09-30T00:00:00Z")  # 30h before run_started
    run_started = real_datetime.datetime(2026, 10, 1, 6, 0, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/some-self-hosted-repo", "self-hosted.yml", "vivy",
        "2026-09-30T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal,
        run_started=run_started,
        stale_pending_after=real_datetime.timedelta(hours=48))
    assert tally["stale_pending"] == 0
    assert tally["next_since"] == "2026-09-30T00:00:00Z"


# --- cursor file round-trip ---------------------------------------------------


def test_cursor_round_trips_through_the_state_file(ci_result_capture):
    ci_result_capture.save_cursor({"convivy/vivy#ci.yml": "2026-10-01T00:00:00Z"})
    assert ci_result_capture.load_cursor() == {"convivy/vivy#ci.yml": "2026-10-01T00:00:00Z"}


def test_load_cursor_with_no_file_is_empty(ci_result_capture):
    assert ci_result_capture.load_cursor() == {}


def test_load_cursor_with_a_corrupt_file_is_empty(ci_result_capture):
    ci_result_capture.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ci_result_capture.STATE_DIR / ci_result_capture.CURSOR_FILE_NAME).write_text("{not json")
    assert ci_result_capture.load_cursor() == {}


def test_save_cursor_writes_via_a_temp_file_and_replace(ci_result_capture):
    """No `.tmp` file survives a clean write — proof the atomic swap, not a
    direct truncate-and-write, is what landed the content."""
    ci_result_capture.save_cursor({"a": "b"})
    leftovers = list(ci_result_capture.STATE_DIR.glob("*.tmp"))
    assert leftovers == []


# --- rotation_order(): the pass's walk order --------------------------------


_ROTATION_TRACKED = (
    {"repo": "convivy/a", "workflow_file": "ci.yml", "product": "a"},
    {"repo": "convivy/b", "workflow_file": "ci.yml", "product": "b"},
    {"repo": "convivy/c", "workflow_file": "ci.yml", "product": "c"},
)


def test_rotation_order_with_no_start_key_walks_from_the_top(ci_result_capture):
    assert ci_result_capture.rotation_order(_ROTATION_TRACKED, None) == _ROTATION_TRACKED


def test_rotation_order_starting_at_a_key_wraps_around(ci_result_capture):
    order = ci_result_capture.rotation_order(_ROTATION_TRACKED, "convivy/b#ci.yml")
    assert [e["repo"] for e in order] == ["convivy/b", "convivy/c", "convivy/a"], (
        "every pair must still run exactly once per pass, just starting at "
        "the saved key and wrapping")


def test_rotation_order_with_an_unknown_key_falls_back_to_the_top(ci_result_capture):
    """`TRACKED` can change between deploys — a key it no longer holds must
    not refuse the run, just start this pass at the top like no file at
    all."""
    order = ci_result_capture.rotation_order(_ROTATION_TRACKED, "convivy/retired#ci.yml")
    assert order == _ROTATION_TRACKED


def test_rotation_order_with_empty_tracked_returns_empty(ci_result_capture):
    assert ci_result_capture.rotation_order((), "convivy/a#ci.yml") == ()


# --- _delivered(): whether a pair's batch put anything in the Archives -----


def test_delivered_is_true_for_a_duplicate_with_zero_created(ci_result_capture):
    """A duplicate still spent a `POST /signals` slot and proves the pair
    got its turn this pass — the first symptom
    issue_01M4XNWSQTKYM42Z7YEG70RFQE measured was a pair that got NO turn
    at all, not one that only posted duplicates."""
    assert ci_result_capture._delivered({"created": 0, "duplicate": 1}) is True


def test_delivered_is_false_for_a_rate_limited_pair_that_posted_nothing(ci_result_capture):
    """A sustained 429 on the very first payload tallies `errors: 1,
    rate_limited: True` but delivers nothing — this must not count as this
    pair having had its turn, or a starved pair would never earn priority
    on the next pass."""
    assert ci_result_capture._delivered({"created": 0, "duplicate": 0}) is False


# --- rotation file round-trip -------------------------------------------------


def test_rotation_round_trips_through_the_state_file(ci_result_capture):
    ci_result_capture.save_rotation_start("convivy/vivy#ci.yml")
    assert ci_result_capture.load_rotation_start() == "convivy/vivy#ci.yml"


def test_load_rotation_start_with_no_file_is_none(ci_result_capture):
    assert ci_result_capture.load_rotation_start() is None


def test_load_rotation_start_with_a_corrupt_file_is_none(ci_result_capture):
    ci_result_capture.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ci_result_capture.STATE_DIR / ci_result_capture.ROTATION_FILE_NAME).write_text("{not json")
    assert ci_result_capture.load_rotation_start() is None


def test_load_rotation_start_with_a_non_string_value_is_none(ci_result_capture):
    ci_result_capture.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ci_result_capture.STATE_DIR / ci_result_capture.ROTATION_FILE_NAME).write_text(
        json.dumps({"start": 5}))
    assert ci_result_capture.load_rotation_start() is None


def test_save_rotation_start_writes_via_a_temp_file_and_replace(ci_result_capture):
    """No `.tmp` file survives a clean write — proof the atomic swap, not a
    direct truncate-and-write, is what landed the content."""
    ci_result_capture.save_rotation_start("convivy/vivy#ci.yml")
    leftovers = list(ci_result_capture.STATE_DIR.glob("*.tmp"))
    assert leftovers == []


# --- main(): end to end, GA and the wire both faked --------------------------


class _CreatedResp:
    status = 201

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_github_app(ci_result_capture, monkeypatch):
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(ci_result_capture.GA, "installation_token", lambda *a, **k: "ghs_x")


def test_main_posts_a_completed_run_through_the_real_signal_client(
        ci_result_capture, monkeypatch, capsys):
    """End to end through `main()`, the real `signal_client`: the request
    actually sent on the wire carries this job's own credential and the
    exact contract shape."""
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED",
                        ({"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},))

    run = _run(run_id=42, conclusion="success")

    def fake_gh_runs(repo, workflow_file, since, token, **k):
        return [run]

    monkeypatch.setattr(ci_result_capture, "gh_runs", fake_gh_runs)

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data)
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 0
    assert seen["auth"] == "Bearer own-secret"
    assert seen["body"]["source"] == "ci"
    assert seen["body"]["kind"] == "test-result"
    assert seen["body"]["actor"] == "github-actions"
    assert seen["body"]["dedup_key"] == "ci:vivy:ci:42"
    cursor = ci_result_capture.load_cursor()
    assert "convivy/vivy#ci.yml" in cursor


def test_main_refuses_cleanly_when_the_key_is_missing(ci_result_capture, monkeypatch, capsys):
    monkeypatch.setattr(ci_result_capture.GA, "key_problem",
                        lambda *a, **k: "no key here")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])
    rc = ci_result_capture.main()
    assert rc == 1
    assert "no key here" in capsys.readouterr().err


def test_main_does_not_advance_the_cursor_for_a_pair_whose_post_failed(
        ci_result_capture, monkeypatch, capsys):
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED",
                        ({"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},))
    ci_result_capture.save_cursor({"convivy/vivy#ci.yml": "2026-10-01T00:00:00Z"})

    run = _run(run_id=42, conclusion="success")
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, workflow_file, since, token, **k: [run])

    def fake_urlopen(req, timeout=None):
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 1
    cursor = ci_result_capture.load_cursor()
    assert cursor["convivy/vivy#ci.yml"] == "2026-10-01T00:00:00Z", (
        "a pair whose post failed must keep its old cursor value, not "
        "advance past a run that was never actually delivered")


def test_main_does_not_abort_the_run_when_one_pair_raises_unexpectedly(
        ci_result_capture, monkeypatch, capsys):
    """**The outer safety net**, carried over from `ci_result_fetch.py`'s
    own design: `capture_workflow` only catches `CaptureError`; something
    else (here, a transport failure `gh_runs` itself doesn't wrap) must not
    abort the whole run and cost every pair after it its turn this cycle.
    On the unfixed code `main()`'s loop has no `try` around the
    `capture_workflow` call, so the `ValueError` below propagates straight
    out of `main()` uncaught — this test errors rather than merely failing
    an assertion, and the second pair's cursor is never written because
    that line of the loop is never reached."""
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/broken", "workflow_file": "ci.yml", "product": "broken"},
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))

    run = _run(run_id=42, conclusion="success")

    def fake_gh_runs(repo, workflow_file, since, token, **k):
        if repo == "convivy/broken":
            raise ValueError("transport exploded, not a CaptureError")
        return [run]

    monkeypatch.setattr(ci_result_capture, "gh_runs", fake_gh_runs)

    def fake_urlopen(req, timeout=None):
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 1, "the pair that raised must still fail the run's exit code"
    cursor = ci_result_capture.load_cursor()
    assert "convivy/broken#ci.yml" not in cursor, (
        "the pair that raised must not have its cursor advanced")
    assert "convivy/vivy#ci.yml" in cursor, (
        "a later pair must still get its own turn and its own cursor "
        "advance, not be skipped because an earlier pair raised something "
        "capture_workflow doesn't catch")
    assert "unexpected failure" in capsys.readouterr().err


def test_main_resolves_the_convivy_bosun_apps_own_installation_with_two_installed(
        ci_result_capture, monkeypatch):
    """The App's installation listing carries TWO installations (`convivy`
    and `convivybuilder`, once installed for homebrew-tap, PR #884) —
    `GA.single_installation`'s "exactly one installation total" count would
    refuse this with "count: the App has 2 installations, not exactly one"
    and `main()` would return 1. `GA.installation_for_owner` asks GitHub for
    `convivy`'s installation directly and never lists installations at all,
    so `main()` still reaches 0. `GA.request` is the real function; only the
    wire underneath it (`opener`) is faked, so this exercises the actual
    per-owner HTTP lookup (`GA.request`, the real function `installation_for_owner`
    calls), not a stand-in for it."""
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(ci_result_capture, "TRACKED", ())
    seen = []

    def fake_request(method, url, bearer, body, opener=None):
        seen.append(url)
        if url.endswith("/app/installations?per_page=100"):
            return 200, [{"id": 1, "account": {"login": "convivy"}},
                        {"id": 2, "account": {"login": "convivybuilder"}}]
        if url.endswith("/orgs/convivy/installation"):
            return 200, {"id": 1, "account": {"login": "convivy"}}
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(ci_result_capture.GA, "request", fake_request)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 0, "resolving convivy's own installation must succeed with two installed"
    assert any(u.endswith("/orgs/convivy/installation") for u in seen)
    assert not any("/app/installations" in u for u in seen), (
        "installation_for_owner must never list installations to find its own")


# --- main(): the rotation start it saves between passes (issue_01M4XNWSQTKYM42Z7YEG70RFQE) --


def _one_run_per_repo(repos):
    """A `gh_runs` fake for tests that only care about rotation order, not
    cursor arithmetic: one completed run per repo in `repos`, with a
    distinct `run_id` so each repo's dedup key is its own."""
    run_ids = {repo: i for i, repo in enumerate(repos)}

    def _fetch(repo, workflow_file, since, token, **k):
        return [_run(run_id=run_ids[repo])]
    return _fetch


def test_main_starts_the_next_pass_after_the_last_delivering_pair(
        ci_result_capture, monkeypatch):
    """Three pairs, `a` and `b` deliver, `c` hits a sustained 429 and
    delivers nothing — the next pass must start right after `b`, the last
    pair THIS pass actually delivered a signal for, not at `c` just because
    `c` is last in `TRACKED` or because `c` is where this pass stopped."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/a", "workflow_file": "ci.yml", "product": "a"},
        {"repo": "convivy/b", "workflow_file": "ci.yml", "product": "b"},
        {"repo": "convivy/c", "workflow_file": "ci.yml", "product": "c"},
    ))
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        _one_run_per_repo(["convivy/a", "convivy/b", "convivy/c"]))

    def fake_post(payload, **k):
        return "created" if payload["product"] in ("a", "b") else "error:429"

    monkeypatch.setattr(ci_result_capture.SC, "post_signal", fake_post)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    assert ci_result_capture.load_rotation_start() == "convivy/c#ci.yml"


def test_main_wraps_the_start_to_the_top_when_the_last_pair_delivers(
        ci_result_capture, monkeypatch):
    """The last pair in the walk order delivering must wrap the next
    start back to the top, not fall off the end of `TRACKED`."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/a", "workflow_file": "ci.yml", "product": "a"},
        {"repo": "convivy/b", "workflow_file": "ci.yml", "product": "b"},
    ))
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        _one_run_per_repo(["convivy/a", "convivy/b"]))
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", lambda payload, **k: "created")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    assert ci_result_capture.load_rotation_start() == "convivy/a#ci.yml"


def test_main_keeps_the_previous_start_when_no_pair_delivers_this_pass(
        ci_result_capture, monkeypatch):
    """Every pair hits a sustained 429 on its only run — nothing is
    delivered this pass, so the saved start must stay exactly where this
    pass began, not reset to the top or move anywhere else."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/a", "workflow_file": "ci.yml", "product": "a"},
        {"repo": "convivy/b", "workflow_file": "ci.yml", "product": "b"},
        {"repo": "convivy/c", "workflow_file": "ci.yml", "product": "c"},
    ))
    ci_result_capture.save_rotation_start("convivy/b#ci.yml")
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        _one_run_per_repo(["convivy/a", "convivy/b", "convivy/c"]))
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", lambda payload, **k: "error:429")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    assert ci_result_capture.load_rotation_start() == "convivy/b#ci.yml"


def test_the_occurrence_a_pair_starved_in_pass_one_delivers_in_pass_two(
        ci_result_capture, monkeypatch):
    """**The production shape (issue_01M4XNWSQTKYM42Z7YEG70RFQE, 2026-10-14).**
    Four tracked pairs, each with exactly one completed run outstanding, and a stub poster that
    lets only the first two posts of each pass through before 429ing the
    rest — the same shared, per-minute Library budget, simplified to reset
    once per pass. On the unfixed code `main()` always starts pass 2 back
    at `TRACKED[0]`, so `p2` and `p3` would be starved again exactly as in
    pass 1; with the fix, pass 2 starts where pass 1's last delivering pair
    (`p1`) left off, so `p2` gets the front of pass 2's budget instead of
    the tail."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/p0", "workflow_file": "ci.yml", "product": "p0"},
        {"repo": "convivy/p1", "workflow_file": "ci.yml", "product": "p1"},
        {"repo": "convivy/p2", "workflow_file": "ci.yml", "product": "p2"},
        {"repo": "convivy/p3", "workflow_file": "ci.yml", "product": "p3"},
    ))
    monkeypatch.setattr(ci_result_capture, "dt", _FrozenDt)
    _FrozenClock._frozen = real_datetime.datetime(
        2026, 10, 1, 11, 0, 0, tzinfo=real_datetime.timezone.utc)

    runs_by_repo = {
        "convivy/p0": [_run(run_id=0, created_at="2026-10-01T10:00:00Z")],
        "convivy/p1": [_run(run_id=1, created_at="2026-10-01T10:01:00Z")],
        "convivy/p2": [_run(run_id=2, created_at="2026-10-01T10:02:00Z")],
        "convivy/p3": [_run(run_id=3, created_at="2026-10-01T10:03:00Z")],
    }

    def fetcher(repo, workflow_file, since, token, **k):
        return [r for r in runs_by_repo[repo] if r["created_at"] >= since]

    monkeypatch.setattr(ci_result_capture, "gh_runs", fetcher)

    class _BudgetedPoster:
        """Mirrors `test_a_shared_per_pass_rate_limit_still_lets_every_pair_catch_up`'s
        own fake: one shared per-pass post budget, not a separate one per
        pair — the shape issue_01M4XNWSQTKYM42Z7YEG70RFQE measured in
        production."""

        def __init__(self, per_pass):
            self.per_pass = per_pass
            self.remaining = per_pass
            self.rows: dict[str, dict] = {}

        def post_signal(self, payload, *, tok=None, timeout=None, deadline=None, sleep=None):
            if self.remaining <= 0:
                return "error:429"
            self.remaining -= 1
            key = payload["dedup_key"]
            if key in self.rows:
                return "duplicate"
            self.rows[key] = payload
            return "created"

    poster = _BudgetedPoster(per_pass=2)
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", poster.post_signal)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()
    cursor_after_pass_1 = ci_result_capture.load_cursor()
    assert cursor_after_pass_1["convivy/p2#ci.yml"] == "2026-10-01T10:02:00Z", (
        "p2 must be stuck at its own run's created_at — starved at 0 "
        "this pass, exactly the production symptom")
    assert ci_result_capture.load_rotation_start() == "convivy/p2#ci.yml", (
        "pass 2 must start at the pair that was starved, not back at p0")

    poster.remaining = poster.per_pass
    ci_result_capture.main()
    cursor_after_pass_2 = ci_result_capture.load_cursor()
    assert cursor_after_pass_2["convivy/p2#ci.yml"] == "2026-10-01T10:55:00Z", (
        "p2 must have advanced past its own run in pass 2 — on the unfixed "
        "code (always restarting at TRACKED[0]) this assertion is the one "
        "that goes red: p2 is starved identically in both passes and its "
        "cursor never moves off 2026-10-01T10:02:00Z")
