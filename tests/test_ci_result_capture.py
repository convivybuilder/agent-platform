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
from pathlib import Path

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
        created_at="2026-10-01T12:00:00Z", updated_at=None, run_attempt=1,
        run_number=42, event="pull_request",
        head_sha="abc1234567890abc1234567890abc1234567890"):
    """One workflow run, shaped the way `GET
    /repos/.../actions/workflows/.../runs` returns it — the real response
    this module reads (see `ci_result_fetch.py`'s own `_run` fixture in the
    library repo for the same shape). `updated_at` defaults to `created_at`
    so every test written before CQ-533 moved the cursor's own basis to
    `updated_at` keeps behaving exactly as it did when the cursor read
    `created_at`; a test of the re-run fix passes a distinct `updated_at`
    (and `run_attempt`) explicitly."""
    return {
        "id": run_id,
        "head_sha": head_sha,
        "conclusion": conclusion,
        "status": status,
        "run_number": run_number,
        "run_attempt": run_attempt,
        "event": event,
        "created_at": created_at,
        "updated_at": updated_at if updated_at is not None else created_at,
        "html_url": f"https://github.com/convivy/widget/actions/runs/{run_id}",
    }


def fetcher_returning(runs):
    def _fetch(repo, workflow_file, since, token, **k):
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


def _parse_time_filter(value):
    """`value` is already url-decoded by `urllib.parse.parse_qs` — either
    `>=<since>` or `<since>..<until>`, the only two shapes `gh_runs`/
    `_gh_runs_window`/`gh_reruns` ever send for the `created` query param."""
    if ".." in value:
        lo_s, hi_s = value.split("..", 1)
        return real_datetime.datetime.fromisoformat(lo_s.replace("Z", "+00:00")), \
            real_datetime.datetime.fromisoformat(hi_s.replace("Z", "+00:00"))
    if value.startswith(">="):
        return real_datetime.datetime.fromisoformat(
            value[2:].replace("Z", "+00:00")), None
    raise AssertionError(f"unexpected time filter: {value!r}")


class _FakeGitHubActions:
    """GitHub's own real behaviour for the list-workflow-runs endpoint,
    which documents a `created` filter and silently ignores any other query
    parameter — `updated=` included, measured 2026-10-25 against a tracked
    repository's ci.yml (a one-second `updated` range and no filter
    at all both returned the pair's whole 1233-run history; see the module
    docstring's measurement section). This fake therefore filters on
    `created=` alone and NEVER on `updated=`: a query that sends only
    `updated=` gets back every run in `self.runs` (modulo `cap`/pagination),
    the same as a query with no time filter at all — on code still sending
    `updated=` (origin/main, before this fix) that means every query
    returns this fake's WHOLE run list, not a window.

    `cap`, when given, reproduces GitHub's own filter cap
    (`GITHUB_CREATED_FILTER_CAP` in the module under test): any query — a
    `created=`-filtered one or not — that matches more than `cap` runs is
    truncated to the newest `cap` of them, the same silent truncation the
    real API performs — `runs` must therefore be supplied newest-first for
    that truncation to keep the right ones. Only one page's worth of runs
    is ever configured here (well under `_PAGE_SIZE`), so each query still
    costs exactly one HTTP call."""

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
        lo, hi = _parse_time_filter(created) if created else (None, None)
        matched = []
        for run in self.runs:
            if lo is not None or hi is not None:
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


class _FakeClock:
    """A `time.monotonic` stand-in that advances by `step` on every read —
    makes a `deadline` check deterministic: the Nth call to this clock is
    the one that first finds the deadline passed, with no real sleep
    anywhere in the test. `monkeypatch.setattr(ci_result_capture.time,
    "monotonic", _FakeClock())` — every `time.monotonic()` call the module
    under test makes, not only the ones in `_gh_runs_window`, advances it."""

    def __init__(self, step=1.0, start=0.0):
        self.now = start
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


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
    signal = ci_result_capture.signal_from_run(run, "widget", "ci")
    assert signal == {
        "source": "ci",
        "kind": "test-result",
        "product": "widget",
        "stream": "ci",
        "actor": "github-actions",
        "body": "ci: success",
        "refs": {
            "conclusion": "success",
            "workflow": "ci",
            "commit": "abc1234567890abc1234567890abc1234567890",
            "run_url": "https://github.com/convivy/widget/actions/runs/900001",
        },
        "meta": {
            "run_number": 42,
            "event": "pull_request",
            "run_url": "https://github.com/convivy/widget/actions/runs/900001",
            "status": "pass",
        },
        "produced_at": "2026-10-01T12:00:00Z",
        "dedup_key": "ci:widget:ci:900001",
    }


def test_signal_from_run_reads_produced_at_from_updated_at_not_created_at(ci_result_capture):
    """Security-reviewer round 1 (CQ-533, issue_01M5FM5NFRPHFJVD8XFK85ABHP):
    a re-run keeps attempt 1's `created_at`, so a `produced_at` read from it
    would tie a later attempt's row to the wrong attempt's timestamp. Uses
    distinct `created_at`/`updated_at` values (the contract test above
    leaves them equal, so it cannot catch a regression back to
    `created_at`) — `produced_at` must equal `updated_at`, not `created_at`."""
    run = _run(run_id=900001, created_at="2026-10-01T10:00:00Z",
              updated_at="2026-10-01T10:05:00Z")
    signal = ci_result_capture.signal_from_run(run, "widget", "ci")
    assert signal["produced_at"] == "2026-10-01T10:05:00Z"


def test_signal_from_run_omits_meta_status_for_an_unmapped_conclusion(ci_result_capture):
    run = _run(conclusion="cancelled")
    signal = ci_result_capture.signal_from_run(run, "widget", "ci")
    assert "status" not in signal["meta"]
    assert signal["refs"]["conclusion"] == "cancelled"


def test_signal_from_run_raises_on_a_missing_id(ci_result_capture):
    run = _run()
    del run["id"]
    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.signal_from_run(run, "widget", "ci")


def test_signal_from_run_raises_on_a_missing_updated_at(ci_result_capture):
    """`produced_at` is read from `updated_at`, not `created_at`, from this
    PR on (security-reviewer round 1, CQ-533) — the missing-field check
    follows it: a run missing `updated_at` cannot be dated, whether or not
    it still has `created_at`."""
    run = _run()
    del run["updated_at"]
    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.signal_from_run(run, "widget", "ci")


def test_signal_from_run_appends_run_attempt_to_dedup_key_from_the_second_attempt_on(
        ci_result_capture):
    """**CQ-533 (issue_01M5FM5NFRPHFJVD8XFK85ABHP).** Attempt 1 keeps the
    unsuffixed `dedup_key` `ci_result_fetch.py` and the retired push both
    already shaped (nothing in this repo's own `bin`/`lib` matches this
    scheme to tell this job whether changing it is safe); attempt 2 and
    later get `:<run_attempt>` appended, so a re-run's own result never
    shares its failing attempt's key (on origin/main `dedup_key` ignores
    `run_attempt` entirely, so the attempt-2 assertion below is the one
    that goes red: it reads `ci:widget:ci:42`, not `ci:widget:ci:42:2`)."""
    attempt_1 = ci_result_capture.signal_from_run(
        _run(run_id=42, run_attempt=1), "widget", "ci")
    attempt_2 = ci_result_capture.signal_from_run(
        _run(run_id=42, run_attempt=2), "widget", "ci")
    assert attempt_1["dedup_key"] == "ci:widget:ci:42"
    assert attempt_2["dedup_key"] == "ci:widget:ci:42:2"


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
        "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert len(out) == 1
    assert "status=" not in seen["url"]
    assert "created=" in seen["url"], (
        "GitHub documents a `created` filter, not `updated` — see the "
        "module docstring's 2026-10-25 measurement; this is the assertion "
        "that goes red against the `updated=`-sending code it replaced")
    assert "repos/convivy/widget/actions/workflows/ci.yml/runs" in seen["url"]


def test_gh_runs_sends_a_bearer_token(ci_result_capture):
    seen = {}

    def opener(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(200, {"workflow_runs": []})

    ci_result_capture.gh_runs("convivy/widget", "ci.yml", "2026-10-01T00:00:00Z",
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
        "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert calls == [1, 2]
    assert len(out) == 102


def test_gh_runs_a_non_200_raises_capture_error(ci_result_capture):
    def opener(req, timeout=None):
        return _Resp(403, {"message": "nope"})

    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.gh_runs("convivy/widget", "ci.yml", "2026-10-01T00:00:00Z",
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
        "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

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
            "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

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
            "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)

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
        "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
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
        "convivy/widget", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=gh)

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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=lambda repo, wf, since, token, **k: ci_result_capture.gh_runs(
            repo, wf, since, token, opener=opener, **k),
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


# --- a pair over the cap: the created-vs-updated regression (2026-10-25) ----


def test_a_pair_over_the_created_filter_cap_converges_within_a_bounded_number_of_passes(
        ci_result_capture, monkeypatch):
    """**The defect this fix exists for.** `_FakeGitHubActions` filters on
    `created=` alone, GitHub's real behaviour — on origin/main `gh_runs`
    sends `updated=` instead, which this fake never honours, so EVERY query
    (no matter how narrow the range `_gh_runs_window` splits down to)
    returns this fixture's whole 12-run list, truncated to the newest 2:
    `_gh_runs_window` sees `len(runs) == cap` at every split level,
    including once the range is under one second wide, where it must raise
    rather than trust an unprovable result — `gh_runs` never returns, and
    `capture_workflow` tallies a plain `CaptureError`. `GITHUB_CREATED_FILTER_CAP`
    does not exist at all on origin/main (only `GITHUB_UPDATED_FILTER_CAP`),
    so `cap_attr` below picks whichever cap constant the loaded module
    actually defines — the same test body then runs unmodified against
    either version, rather than an `AttributeError` from `monkeypatch.setattr`
    pre-empting it on origin/main before `capture_workflow` is ever reached.
    This test's own `assert tally["errors"] == 0`, inside the loop, is the
    one that goes red there, on the very first pass (`capture_workflow`
    returns with `errors: 1` and `next_since: None` once `gh_runs` raises)."""
    cap_attr = ("GITHUB_CREATED_FILTER_CAP"
               if hasattr(ci_result_capture, "GITHUB_CREATED_FILTER_CAP")
               else "GITHUB_UPDATED_FILTER_CAP")
    monkeypatch.setattr(ci_result_capture, cap_attr, 2)
    run_count = 12
    base = real_datetime.datetime(2026, 10, 1, 0, 0, 0, tzinfo=real_datetime.timezone.utc)
    runs = [_run(run_id=i, created_at=(base + real_datetime.timedelta(minutes=i)
                                       ).strftime("%Y-%m-%dT%H:%M:%SZ"))
           for i in range(run_count)]
    gh = _FakeGitHubActions(list(reversed(runs)), cap=2)  # newest-first, per its own contract

    def fetcher(repo, workflow_file, since, token, **k):
        return ci_result_capture.gh_runs(repo, workflow_file, since, token, opener=gh)

    store = _FakeSignalStore()
    since = base.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_started = base + real_datetime.timedelta(hours=1)
    max_passes = 5
    for _ in range(max_passes):
        tally = ci_result_capture.capture_workflow(
            "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
            fetcher=fetcher, poster=store.post_signal, run_started=run_started)
        assert tally["errors"] == 0, (
            f"a pair over the cap must still converge cleanly once the "
            f"filter it sends is the one GitHub honours: {tally}")
        since = tally["next_since"]
        if since > runs[-1]["created_at"]:
            break
    else:
        raise AssertionError(
            f"did not converge within {max_passes} passes")

    assert len(store.rows) == run_count, (
        "every run must have been posted exactly once by the time the "
        "cursor passes the newest one")


# --- gh_reruns(): the bounded created lookback behind the forward cursor ----


def test_gh_reruns_queries_the_lookback_window_behind_since(ci_result_capture):
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(200, {"workflow_runs": []})

    ci_result_capture.gh_reruns(
        "test-owner/test-repo", "ci.yml", "2026-10-08T00:00:00Z", "ghs_x", opener=opener)

    assert "created=" in seen["url"]
    params = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    lo_s, hi_s = params["created"][0].split("..")
    assert hi_s == "2026-10-08T00:00:00Z"
    assert lo_s == (
        real_datetime.datetime(2026, 10, 8, tzinfo=real_datetime.timezone.utc)
        - ci_result_capture.RERUN_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_gh_reruns_finds_a_run_created_inside_the_lookback_window(ci_result_capture):
    since = "2026-10-08T00:00:00Z"
    inside = _run(run_id=1, created_at="2026-10-05T00:00:00Z",
                 updated_at="2026-10-08T01:00:00Z")
    gh = _FakeGitHubActions([inside])

    out = ci_result_capture.gh_reruns("test-owner/test-repo", "ci.yml", since, "ghs_x", opener=gh)

    assert [r["id"] for r in out] == [1]


def test_gh_reruns_misses_a_run_created_before_the_lookback_window(ci_result_capture):
    """**The documented limit.** A run created more than `RERUN_LOOKBACK`
    before `since` sits outside the window `gh_reruns` ever asks about, so
    a re-run of it — however recent its own `updated_at` — is never even
    fetched, let alone posted. This is the limit the module docstring and
    `RERUN_LOOKBACK`'s own comment name; this test pins it."""
    since = "2026-10-08T00:00:00Z"
    too_old = _run(run_id=1,
                   created_at=(real_datetime.datetime(2026, 10, 8, tzinfo=real_datetime.timezone.utc)
                              - ci_result_capture.RERUN_LOOKBACK
                              - real_datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   updated_at="2026-10-08T01:00:00Z")
    gh = _FakeGitHubActions([too_old])

    out = ci_result_capture.gh_reruns("test-owner/test-repo", "ci.yml", since, "ghs_x", opener=gh)

    assert out == []


# --- capture_reruns(): posting what the forward cursor can no longer see ---


def test_capture_reruns_posts_a_completed_rerun_whose_updated_at_passed_since(
        ci_result_capture):
    """The re-run itself: created well before `since` (invisible to
    `capture_workflow`'s own forward, `created`-filtered window) but
    touched again (re-run) after it."""
    since = "2026-10-08T00:00:00Z"
    rerun = _run(run_id=42, created_at="2026-10-01T10:00:00Z",
                run_attempt=2, updated_at="2026-10-08T01:00:00Z")
    store = _FakeSignalStore()

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=fetcher_returning([rerun]), poster=store.post_signal)

    assert tally["created"] == 1
    assert tally["errors"] == 0
    assert "next_since" not in tally, (
        "capture_reruns owns no cursor of its own")
    assert next(iter(store.rows)) == "ci:testproduct:ci:42:2"


def test_capture_reruns_ignores_a_run_whose_updated_at_did_not_pass_since(
        ci_result_capture):
    """A run created before `since` that has not been touched again since
    `since` is not a re-run — `capture_workflow`'s own forward window
    already posted it (or will, on whichever pass its `created_at` was
    first in range); `capture_reruns` must not repost it."""
    since = "2026-10-08T00:00:00Z"
    not_a_rerun = _run(run_id=1, created_at="2026-10-01T10:00:00Z",
                      updated_at="2026-10-01T10:05:00Z")
    store = _FakeSignalStore()

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=fetcher_returning([not_a_rerun]), poster=store.post_signal)

    assert tally["created"] == 0
    assert store.rows == {}


def test_capture_reruns_ignores_an_incomplete_run(ci_result_capture):
    """A run still in flight, even one whose `updated_at` passed `since`,
    is not a completed re-run yet — `capture_workflow`'s own forward
    window owns in-flight tracking; this function only ever posts a
    `completed` run."""
    since = "2026-10-08T00:00:00Z"
    in_flight = _run(run_id=1, conclusion=None, status="in_progress",
                     created_at="2026-10-01T10:00:00Z",
                     updated_at="2026-10-08T01:00:00Z")
    store = _FakeSignalStore()

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal)

    assert tally["created"] == 0
    assert store.rows == {}


def test_capture_reruns_posts_oldest_updated_at_first(ci_result_capture):
    since = "2026-10-08T00:00:00Z"
    reruns = [
        _run(run_id=2, created_at="2026-10-01T10:00:00Z", run_attempt=2,
            updated_at="2026-10-08T03:00:00Z"),
        _run(run_id=1, created_at="2026-10-01T09:00:00Z", run_attempt=2,
            updated_at="2026-10-08T01:00:00Z"),
    ]
    posted_order = []

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        posted_order.append(payload["dedup_key"])
        return "created"

    ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=fetcher_returning(reruns), poster=poster)

    assert posted_order == ["ci:testproduct:ci:1:2", "ci:testproduct:ci:2:2"], (
        "re-runs must be posted oldest updated_at first, not in fetch order")


def test_capture_reruns_a_fetch_failure_tallies_an_error(ci_result_capture):
    def fetcher(repo, workflow_file, since, token, **k):
        raise ci_result_capture.CaptureError("boom")

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", "2026-10-08T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher, poster=_FakeSignalStore().post_signal)

    assert tally["errors"] == 1
    assert tally["reason"] is not None


def test_capture_reruns_an_ordinary_post_failure_does_not_stop_the_batch(ci_result_capture):
    """A re-run sorted first whose post is always rejected must not block
    every re-run sorted after it: this function has no cursor of its own,
    sorts oldest-`updated_at` first every pass, and a run stuck behind an
    always-rejected one would otherwise age out of the lookback window
    without ever being attempted."""
    since = "2026-10-08T00:00:00Z"
    reruns = [
        _run(run_id=1, created_at="2026-10-01T09:00:00Z", run_attempt=2,
            updated_at="2026-10-08T01:00:00Z"),
        _run(run_id=2, created_at="2026-10-01T10:00:00Z", run_attempt=2,
            updated_at="2026-10-08T03:00:00Z"),
    ]
    calls = []

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        calls.append(payload["dedup_key"])
        if payload["dedup_key"] == "ci:testproduct:ci:1:2":
            return "error:500"
        return "created"

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=fetcher_returning(reruns), poster=poster)

    assert tally["errors"] == 1
    assert tally["created"] == 1
    assert calls == ["ci:testproduct:ci:1:2", "ci:testproduct:ci:2:2"], (
        "the second re-run, sorted after the rejected one, must still be "
        "attempted in this same pass")


def test_a_rerun_inside_the_lookback_reaches_the_library_end_to_end(ci_result_capture):
    """**The occurrence this fix exists to restore.** A run posted once,
    then re-run days later: `capture_workflow`'s own forward fetch (now
    `created`-filtered) never sees the re-run, since its cursor has long
    since passed the run's fixed `created_at` — `capture_reruns`, using the
    real `gh_reruns` against a fake GitHub, is what reaches it."""
    since = "2026-10-08T00:00:00Z"
    attempt_1 = _run(run_id=42, conclusion="failure", run_attempt=1,
                     created_at="2026-10-01T10:00:00Z",
                     updated_at="2026-10-01T10:05:00Z")
    attempt_2 = _run(run_id=42, conclusion="success", run_attempt=2,
                     created_at="2026-10-01T10:00:00Z",
                     updated_at="2026-10-08T01:00:00Z")
    gh = _FakeGitHubActions([attempt_2])
    store = _FakeSignalStore()
    # attempt_1 already landed on an earlier pass, before the cursor moved
    # past this run's created_at.
    store.post_signal(ci_result_capture.signal_from_run(attempt_1, "testproduct", "ci"))

    tally = ci_result_capture.capture_reruns(
        "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
        fetcher=lambda repo, wf, s, token, **k: ci_result_capture.gh_reruns(
            repo, wf, s, token, opener=gh, **k),
        poster=store.post_signal)

    assert tally["created"] == 1
    assert tally["errors"] == 0
    assert len(store.rows) == 2
    assert store.rows["ci:testproduct:ci:42:2"]["meta"]["status"] == "pass"


# --- capture_workflow(): cursor invariants ------------------------------------


def test_capture_workflow_posts_one_signal_per_completed_run(ci_result_capture):
    runs = [_run(run_id=1), _run(run_id=2), _run(run_id=3)]
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal,
        run_started=poll_1_started)
    assert store.rows == {}
    assert tally_1["errors"] == 0

    poll_2_started = real_datetime.datetime(2026, 10, 1, 10, 35, tzinfo=real_datetime.timezone.utc)
    now_complete = _run(run_id=1, conclusion="success", status="completed",
                       created_at="2026-10-01T10:00:00Z")
    tally_2 = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", tally_1["next_since"], "ghs_x", "sig_tok",
        fetcher=fetcher_returning([now_complete]), poster=store.post_signal,
        run_started=poll_2_started)
    assert tally_2["created"] == 1
    assert len(store.rows) == 1
    assert next(iter(store.rows))== "ci:widget:ci:1"


def test_a_stale_pending_run_is_reported_and_stops_holding_the_cursor(ci_result_capture, capsys):
    """A run stuck `in_progress` well past `STALE_PENDING_AFTER` must not
    freeze the cursor forever — tallied `stale_pending` (loud, not silent)
    and excluded from the watermark, so the cursor still advances."""
    stuck = _run(run_id=1, conclusion=None, status="in_progress",
                created_at="2026-09-01T00:00:00Z")  # far older than the ceiling
    run_started = real_datetime.datetime(2026, 10, 1, 10, 30, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
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
        ci_result_capture.signal_from_run(failing, "widget", "ci")["dedup_key"]})
    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
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
        "convivy/widget", "ci.yml", "widget", "2026-09-01T00:00:00Z", "ghs_x", "sig_tok",
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
    key = ci_result_capture.signal_from_run(run, "widget", "ci")["dedup_key"]
    store = _FakeSignalStore(fail_dedup_keys={key})
    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    assert tally["errors"] == 1
    assert tally["created"] == 0
    assert store.rows == {}
    assert key in store.calls, "the post must actually have been attempted"


def test_a_repost_of_the_same_run_sends_the_same_dedup_key(ci_result_capture):
    run = _run(run_id=1)
    store = _FakeSignalStore()
    first = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    second = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([run]), poster=store.post_signal)
    assert first["created"] == 1
    assert second["duplicate"] == 1
    assert store.calls == ["ci:widget:ci:1", "ci:widget:ci:1"]
    assert len(store.rows) == 1


def test_a_run_that_fails_then_passes_on_a_rerun_yields_two_test_result_rows(
        ci_result_capture):
    """**CQ-533, the dedup_key half of the fix.** A run's first attempt
    fails; its re-run keeps the same `id` but reports `run_attempt: 2` and a
    passing `conclusion`. Before this fix both attempts shared one
    `dedup_key` (no `run_attempt` in it), so the second, passing post was
    dropped as a `duplicate` and the Library's merge admission never saw
    this commit go green — `second["created"] == 1` is the assertion that
    goes red there, reading `second["duplicate"] == 1` instead, and
    `store.rows` holds only the failing row.

    The two `produced_at` assertions are security-reviewer round 1: before
    that fix both rows read the run's `created_at`, identical across a
    rerun, so `rows[0]["produced_at"] < rows[1]["produced_at"]` is the line
    that goes red there, reading two identical values instead of a strict
    order."""
    head_sha = "abc1234567890abc1234567890abc1234567890"
    attempt_1 = _run(run_id=42, conclusion="failure", run_attempt=1,
                     created_at="2026-10-01T10:00:00Z",
                     updated_at="2026-10-01T10:05:00Z", head_sha=head_sha)
    attempt_2 = _run(run_id=42, conclusion="success", run_attempt=2,
                     created_at="2026-10-01T10:00:00Z",
                     updated_at="2026-10-01T11:00:00Z", head_sha=head_sha)
    store = _FakeSignalStore()

    first = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([attempt_1]), poster=store.post_signal)
    second = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", first["next_since"], "ghs_x", "sig_tok",
        fetcher=fetcher_returning([attempt_2]), poster=store.post_signal)

    assert first["created"] == 1
    assert second["created"] == 1, (
        "the re-run's passing result must land as its own row, not be "
        "dropped as a duplicate of the failing attempt's dedup_key")
    assert len(store.rows) == 2
    rows = list(store.rows.values())
    assert rows[0]["refs"]["commit"] == head_sha
    assert rows[1]["refs"]["commit"] == head_sha
    assert rows[1]["meta"]["status"] == "pass"
    assert rows[0]["dedup_key"] != rows[1]["dedup_key"]
    assert rows[0]["produced_at"] < rows[1]["produced_at"], (
        "attempt 2's produced_at must be later than attempt 1's, so a "
        "reader ordering same-run rows by produced_at can tell them apart")


def test_a_run_that_passes_then_fails_on_a_rerun_yields_two_test_result_rows(
        ci_result_capture):
    """**Symmetric to the test above, security-reviewer round 1 (CQ-533,
    issue_01M5FM5NFRPHFJVD8XFK85ABHP).** Attempt 1 passes; a later, manually
    triggered re-run (attempt 2) fails — the direction the task brief calls
    out by name, "a row that says green when the run was red." Before the
    `produced_at` fix both rows read the run's `created_at`, identical
    across a rerun, so a reader ordering same-run rows by `produced_at`
    could not tell the stale passing row from the true failing one —
    `rows[0]["produced_at"] < rows[1]["produced_at"]` is the line that goes
    red there, reading two identical values instead of a strict order."""
    head_sha = "abc1234567890abc1234567890abc1234567890"
    attempt_1 = _run(run_id=43, conclusion="success", run_attempt=1,
                     created_at="2026-10-02T10:00:00Z",
                     updated_at="2026-10-02T10:05:00Z", head_sha=head_sha)
    attempt_2 = _run(run_id=43, conclusion="failure", run_attempt=2,
                     created_at="2026-10-02T10:00:00Z",
                     updated_at="2026-10-02T12:00:00Z", head_sha=head_sha)
    store = _FakeSignalStore()

    first = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-02T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([attempt_1]), poster=store.post_signal)
    second = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", first["next_since"], "ghs_x", "sig_tok",
        fetcher=fetcher_returning([attempt_2]), poster=store.post_signal)

    assert first["created"] == 1
    assert second["created"] == 1, (
        "the re-run's failing result must land as its own row, not be "
        "dropped as a duplicate of the passing attempt's dedup_key")
    assert len(store.rows) == 2
    rows = list(store.rows.values())
    assert rows[0]["refs"]["commit"] == head_sha
    assert rows[1]["refs"]["commit"] == head_sha
    assert rows[0]["meta"]["status"] == "pass"
    assert rows[1]["meta"]["status"] == "fail"
    assert rows[0]["dedup_key"] != rows[1]["dedup_key"]
    assert rows[0]["produced_at"] < rows[1]["produced_at"], (
        "attempt 2's produced_at must be later than attempt 1's, so a "
        "reader ordering same-run rows by produced_at can tell the true, "
        "later failing row from the stale passing one")


def test_a_sustained_429_stops_the_batch_and_is_tallied_as_an_error(ci_result_capture, capsys):
    runs = [_run(run_id=1), _run(run_id=2)]

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return "error:429"

    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        {"repo": "convivy/widget", "workflow_file": "ci.yml", "product": "widget"},))
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: runs)
    results2 = iter(["created", "duplicate", "error:429"])
    monkeypatch.setattr(ci_result_capture.SC, "post_signal",
                        lambda payload, **k: next(results2))
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 1, "a pass that hit a sustained 429 must still exit loud"
    cursor = ci_result_capture.load_cursor()
    assert cursor["convivy/widget#ci.yml"] == runs[2]["created_at"], (
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
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
        {"repo": "convivy/widget", "workflow_file": "ci.yml", "product": "widget"},))
    ci_result_capture.save_cursor({"convivy/widget#ci.yml": "2026-10-01T00:00:00Z"})
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: runs)
    results2 = iter(["error:500", "created", "error:429"])
    monkeypatch.setattr(ci_result_capture.SC, "post_signal",
                        lambda payload, **k: next(results2))
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()
    assert rc == 1
    cursor = ci_result_capture.load_cursor()
    assert cursor["convivy/widget#ci.yml"] == "2026-10-01T00:00:00Z", (
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
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning(runs), poster=poster)
    assert posted_order == [
        "ci:widget:ci:1", "ci:widget:ci:2", "ci:widget:ci:3",
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
        def _fetch(r, workflow_file, since, token, **k):
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
    def fetcher(repo, workflow_file, since, token, **k):
        raise ci_result_capture.CaptureError("boom")

    tally = ci_result_capture.capture_workflow(
        "convivy/widget", "ci.yml", "widget", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher, poster=_FakeSignalStore().post_signal)
    assert tally["errors"] == 1
    assert tally["next_since"] is None


def test_a_self_hosted_style_entry_does_not_inherit_the_default_staleness_ceiling(ci_result_capture):
    in_flight = _run(run_id=1, conclusion=None, status="in_progress",
                     created_at="2026-09-30T00:00:00Z")  # 30h before run_started
    run_started = real_datetime.datetime(2026, 10, 1, 6, 0, tzinfo=real_datetime.timezone.utc)
    store = _FakeSignalStore()
    tally = ci_result_capture.capture_workflow(
        "convivy/some-self-hosted-repo", "self-hosted.yml", "widget",
        "2026-09-30T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher_returning([in_flight]), poster=store.post_signal,
        run_started=run_started,
        stale_pending_after=real_datetime.timedelta(hours=48))
    assert tally["stale_pending"] == 0
    assert tally["next_since"] == "2026-09-30T00:00:00Z"


# --- cursor file round-trip ---------------------------------------------------


def test_cursor_round_trips_through_the_state_file(ci_result_capture):
    ci_result_capture.save_cursor({"convivy/widget#ci.yml": "2026-10-01T00:00:00Z"})
    assert ci_result_capture.load_cursor() == {"convivy/widget#ci.yml": "2026-10-01T00:00:00Z"}


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
    ci_result_capture.save_rotation_start("convivy/widget#ci.yml")
    assert ci_result_capture.load_rotation_start() == "convivy/widget#ci.yml"


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
    ci_result_capture.save_rotation_start("convivy/widget#ci.yml")
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
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    # `main()` now mints a `GhTokenRefresher`, which mints through
    # `installation_token_payload` (needs the real `expires_at`), not the
    # thin `installation_token` wrapper this used to stub.
    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    # Every main()-level test built on this stub fakes the forward fetch
    # (`gh_runs`) itself but is not about the independent re-run lookback
    # `main()` now also runs per pair (`capture_reruns`'s own default
    # fetcher, `gh_reruns`) — defaulting it to "nothing found" here keeps
    # each of those tests exercising only the forward-cursor behaviour it's
    # actually about. `gh_reruns`/`capture_reruns` themselves are tested
    # directly, never through this stub.
    monkeypatch.setattr(ci_result_capture, "gh_reruns", lambda *a, **k: [])


def test_main_posts_a_completed_run_through_the_real_signal_client(
        ci_result_capture, monkeypatch, capsys):
    """End to end through `main()`, the real `signal_client`: the request
    actually sent on the wire carries this job's own credential and the
    exact contract shape."""
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED",
                        ({"repo": "convivy/widget", "workflow_file": "ci.yml", "product": "widget"},))

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
    assert seen["body"]["dedup_key"] == "ci:widget:ci:42"
    cursor = ci_result_capture.load_cursor()
    assert "convivy/widget#ci.yml" in cursor


def test_main_also_posts_a_rerun_found_by_capture_reruns(
        ci_result_capture, monkeypatch):
    """`main()` runs `capture_reruns` for every pair alongside
    `capture_workflow`'s own forward fetch — a re-run `gh_runs` can no
    longer see (its `created_at` is behind the cursor) still reaches the
    Library through this second, independent check."""
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED",
                        ({"repo": "test-owner/test-repo", "workflow_file": "ci.yml", "product": "testproduct"},))
    ci_result_capture.save_cursor({"test-owner/test-repo#ci.yml": "2026-10-08T00:00:00Z"})

    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: [])
    rerun = _run(run_id=42, conclusion="success", run_attempt=2,
                created_at="2026-10-01T10:00:00Z",
                updated_at="2026-10-08T01:00:00Z")
    monkeypatch.setattr(ci_result_capture, "gh_reruns",
                        lambda repo, wf, since, token, **k: [rerun])

    posted = []

    def fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data))
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 0
    assert len(posted) == 1
    assert posted[0]["dedup_key"] == "ci:testproduct:ci:42:2"
    cursor = ci_result_capture.load_cursor()
    assert cursor["test-owner/test-repo#ci.yml"] > "2026-10-08T00:00:00Z", (
        "the forward cursor must still advance on wall-clock time even "
        "though the forward fetch itself returned nothing this pass")


def test_a_sustained_429_on_the_rerun_channel_does_not_freeze_the_forward_cursor(
        ci_result_capture, monkeypatch, capsys):
    """**CQ-561 revise round 1.** The forward channel delivers its one run
    cleanly while the independent re-run channel hits a sustained 429 on its
    own post. Before this fix, `main()` folds `capture_reruns`' tally
    (`error:429` included) into the same `t["errors"]` the forward cursor's
    advance gates on, so this pair's cursor is held at `since` forever even
    though the forward window posted clean — every later pass re-fetches and
    re-posts the delivered forward window as duplicates instead of reaching
    runs after it. This is the assertion that goes red on the pre-fix code."""
    since = "2026-10-08T00:00:00Z"
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "test-owner/test-repo", "workflow_file": "ci.yml", "product": "testproduct"},))
    ci_result_capture.save_cursor({"test-owner/test-repo#ci.yml": since})

    forward_run = _run(run_id=1, created_at="2026-10-08T01:00:00Z")
    rerun = _run(run_id=2, created_at="2026-10-01T10:00:00Z", run_attempt=2,
                updated_at="2026-10-08T02:00:00Z")
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, s, token, **k: [forward_run])
    monkeypatch.setattr(ci_result_capture, "gh_reruns",
                        lambda repo, wf, s, token, **k: [rerun])

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return "error:429" if payload["dedup_key"].endswith(":2") else "created"

    monkeypatch.setattr(ci_result_capture.SC, "post_signal", poster)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    cursor = ci_result_capture.load_cursor()
    assert cursor["test-owner/test-repo#ci.yml"] > since, (
        "a sustained 429 on the re-run channel alone must never hold back "
        "the forward cursor when the forward channel itself delivered "
        "cleanly")
    err = capsys.readouterr().err
    assert "test-owner/test-repo" in err and "429" in err, (
        "the re-run channel's failure must still be reported, naming the "
        "pair and the reason")


def test_a_rejected_rerun_post_does_not_freeze_the_forward_cursor(
        ci_result_capture, monkeypatch, capsys):
    """Same shape as above, with an ordinary post rejection (`error:500`) on
    the re-run channel instead of a sustained 429 — before this fix any
    non-created/duplicate result from `capture_reruns` folds into the same
    merged `t["errors"]` the forward cursor's advance gates on, with no
    channel distinction at all, so this freezes the forward cursor too."""
    since = "2026-10-08T00:00:00Z"
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "test-owner/test-repo", "workflow_file": "ci.yml", "product": "testproduct"},))
    ci_result_capture.save_cursor({"test-owner/test-repo#ci.yml": since})

    forward_run = _run(run_id=1, created_at="2026-10-08T01:00:00Z")
    rerun = _run(run_id=2, created_at="2026-10-01T10:00:00Z", run_attempt=2,
                updated_at="2026-10-08T02:00:00Z")
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, s, token, **k: [forward_run])
    monkeypatch.setattr(ci_result_capture, "gh_reruns",
                        lambda repo, wf, s, token, **k: [rerun])

    def poster(payload, *, tok=None, timeout=None, deadline=None, sleep=None):
        return "error:500" if payload["dedup_key"].endswith(":2") else "created"

    monkeypatch.setattr(ci_result_capture.SC, "post_signal", poster)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    cursor = ci_result_capture.load_cursor()
    assert cursor["test-owner/test-repo#ci.yml"] > since, (
        "a rejected post on the re-run channel alone must never hold back "
        "the forward cursor when the forward channel itself delivered "
        "cleanly")
    err = capsys.readouterr().err
    assert "test-owner/test-repo" in err and "re-run post failed" in err, (
        "the re-run channel's failure must still be reported, naming the "
        "pair and the reason")


def test_main_refuses_cleanly_when_the_key_is_missing(ci_result_capture, monkeypatch, capsys):
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem",
                        lambda *a, **k: "no key here")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])
    rc = ci_result_capture.main()
    assert rc == 1
    assert "no key here" in capsys.readouterr().err


def test_main_refuses_cleanly_when_the_install_has_no_app(
        ci_result_capture, monkeypatch, capsys):
    """The install resolver itself is where a fresh install with no App set
    up at all is refused — before `key_problem` is ever reached (CQ·604,
    issue_01M69GD1WEN67MZPSBQZD02CDN)."""
    def _raise():
        raise ci_result_capture.GA.Unreadable("no App id at .../app-id")
    monkeypatch.setattr(ci_result_capture.GA, "installed_app", _raise)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])
    rc = ci_result_capture.main()
    assert rc == 1
    assert "no App id" in capsys.readouterr().err


def test_main_does_not_advance_the_cursor_for_a_pair_whose_post_failed(
        ci_result_capture, monkeypatch, capsys):
    import signal_client as sc
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED",
                        ({"repo": "convivy/widget", "workflow_file": "ci.yml", "product": "widget"},))
    ci_result_capture.save_cursor({"convivy/widget#ci.yml": "2026-10-01T00:00:00Z"})

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
    assert cursor["convivy/widget#ci.yml"] == "2026-10-01T00:00:00Z", (
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
        {"repo": "convivy/widget", "workflow_file": "ci.yml", "product": "widget"},
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
    assert "convivy/widget#ci.yml" in cursor, (
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
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    # `main()` now mints a `GhTokenRefresher`, through `installation_token_payload`
    # — stubbed directly, same as `_stub_github_app`, so the `GA.request` fake
    # below only has to answer the installation-lookup calls this test is
    # actually about.
    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    # One `convivy`-owned pair, so `main()`'s per-owner mint loop actually
    # has an owner to resolve an installation for — an empty pair list
    # mints no token for anyone, which this test is not about.
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/widget-repo", "workflow_file": "ci.yml", "product": "widget-crew"},))
    monkeypatch.setattr(ci_result_capture, "gh_runs", lambda *a, **k: [])
    monkeypatch.setattr(ci_result_capture, "gh_reruns", lambda *a, **k: [])
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


def test_run_deadline_bounds_a_stuck_pass_rather_than_racing_the_next_fire(ci_result_capture):
    """`_RUN_DEADLINE_SECONDS` is sized against this job's own launchd
    `StartInterval` (180s, `com.convivy.ci-result-capture.plist`), the same
    pairing `pr-state-capture._RUN_DEADLINE_SECONDS` keeps against its own
    180s interval: a pass that runs long has its next scheduled fire skipped
    rather than overlapping it, so 150s bounds how long a stuck pass is
    retried, not a race against the next fire."""
    assert ci_result_capture._RUN_DEADLINE_SECONDS == 150


# --- TRACKED: CQ-533 adds knowledge's kb-write-trailer.yml -------------------


def test_main_walks_the_knowledge_trailer_workflow_pair(ci_result_capture, monkeypatch):
    """CQ-533 adds `convivy/knowledge`'s `kb-write-trailer.yml` — one of the
    required checks the Library's merge admission reads for every `kb/`
    pull request. `main()` must actually walk this pair, not merely list
    it, so a trailer row reaches the Library for a kb/ PR. Pinned via
    `TRACKED` rather than read from `fetch_tracked_pairs()` — this test is
    about `main()` actually walking a pair that is not the default
    `ci.yml`, whatever produced it; `fetch_tracked_pairs()` itself is
    tested directly, in isolation, against a stub Library."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/knowledge", "workflow_file": "ci.yml", "product": "library"},
        {"repo": "convivy/knowledge", "workflow_file": "kb-write-trailer.yml", "product": "library"},
    ))

    def fake_gh_runs(repo, workflow_file, since, token, **k):
        if (repo, workflow_file) == ("convivy/knowledge", "kb-write-trailer.yml"):
            return [_run(run_id=777, conclusion="success")]
        return []

    monkeypatch.setattr(ci_result_capture, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", lambda payload, **k: "created")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 0
    cursor = ci_result_capture.load_cursor()
    assert "convivy/knowledge#kb-write-trailer.yml" in cursor


# --- fetch_tracked_pairs(): read fresh from the Library's GET /ci/tracked --
# (CQ·571, issue_01M5VWKYRFPQ5WDCBCH271EP5R) --------------------------------


class _TrackedStub:
    """A real local HTTP server on an ephemeral port, standing in for the
    Library's `GET /ci/tracked` — records every request's path and headers,
    and answers with a fixed status/body, same shape as `test_projects.py`'s
    `_RegistryStub`."""

    def __init__(self, *, status=200, body=b"{}"):
        import http.server
        import threading
        self.requests: list[dict] = []
        self.status = status
        self.body = body
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def tracked_stub(ci_result_capture, monkeypatch):
    """A factory for `_TrackedStub`s: each one made is pointed at by
    `ci_result_capture.VP.LIBRARY_BASE_URL` (the same `vault_plane` module
    object `projects.py`'s own Library read already uses) for the rest of
    this test, and closed at teardown. Also sets the registry-reader
    credential every call to `fetch_tracked_pairs()` needs."""
    made: list[_TrackedStub] = []

    def make(**kw):
        stub = _TrackedStub(**kw)
        made.append(stub)
        monkeypatch.setattr(ci_result_capture.VP, "LIBRARY_BASE_URL", stub.url)
        return stub

    monkeypatch.setenv(ci_result_capture.PR.REGISTRY_TOKEN_ENV, "tok-under-test")
    yield make
    for s in made:
        s.close()


_SYNTHETIC_TRACKED_BODY = {
    "tracked": [
        {"repo": "owner-a/repo-a", "workflow_file": "ci.yml", "product": "widget-crew",
         "on_pull_request": True},
        {"repo": "owner-b/repo-b", "workflow_file": "build.yml", "product": "gadget-crew",
         "on_pull_request": False},
    ],
    "count": 2,
}


def test_fetch_tracked_pairs_reads_a_200_body_into_pair_dicts(ci_result_capture, tracked_stub):
    stub = tracked_stub(body=json.dumps(_SYNTHETIC_TRACKED_BODY).encode())

    pairs = ci_result_capture.fetch_tracked_pairs()

    assert pairs == (
        {"repo": "owner-a/repo-a", "workflow_file": "ci.yml", "product": "widget-crew"},
        {"repo": "owner-b/repo-b", "workflow_file": "build.yml", "product": "gadget-crew"},
    )
    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req["path"] == "/ci/tracked"
    assert req["headers"]["Library-Protocol"] == ci_result_capture.VP.PROTOCOL_VERSION
    assert req["headers"]["Authorization"] == "Bearer tok-under-test"


def _seed_cursor_bytes(ci_result_capture) -> bytes:
    """Writes a cursor file by hand (never through `save_cursor`, whose own
    key-sort would mask a byte-for-byte comparison), so a refusal test can
    assert the file this pass started with is exactly the file it ends
    with."""
    ci_result_capture.STATE_DIR.mkdir(parents=True, exist_ok=True)
    raw = b'{"convivy/kept#ci.yml": "2026-10-01T00:00:00Z"}'
    ci_result_capture._cursor_path().write_bytes(raw)
    return raw


def _assert_pass_refused(ci_result_capture, monkeypatch, capsys, expect_substr, raw):
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert expect_substr in err
    assert "tok-under-test" not in err, "the registry-reader token must never be logged"
    assert ci_result_capture._cursor_path().read_bytes() == raw, (
        "a refused pass must not touch the cursor file at all")


def test_main_refuses_the_pass_on_a_401_from_ci_tracked(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(status=401, body=b'{"error": "unauthorized"}')
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "401", raw)


def test_main_refuses_the_pass_on_a_403_from_ci_tracked(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(status=403, body=b'{"error": "forbidden: missing registry.read"}')
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "403", raw)


def test_main_refuses_the_pass_on_a_503_from_ci_tracked(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(status=503, body=b'{"error": "no usable registry"}')
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "503", raw)


def test_main_refuses_the_pass_on_a_timeout_from_ci_tracked(
        ci_result_capture, monkeypatch, capsys):
    monkeypatch.setenv(ci_result_capture.PR.REGISTRY_TOKEN_ENV, "tok-under-test")

    def _raise_timeout(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ci_result_capture.urllib.request, "urlopen", _raise_timeout)
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "TimeoutError", raw)


def test_main_refuses_the_pass_on_a_body_with_no_tracked_list(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(body=json.dumps({"count": 0}).encode())
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "no 'tracked' list", raw)


def test_main_refuses_the_pass_on_a_row_missing_workflow_file(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(body=json.dumps({
        "tracked": [{"repo": "owner-a/repo-a", "product": "widget-crew"}],
        "count": 1,
    }).encode())
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "missing", raw)


def test_main_refuses_the_pass_on_a_count_mismatch(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(body=json.dumps({
        "tracked": [{"repo": "owner-a/repo-a", "workflow_file": "ci.yml",
                     "product": "widget-crew"}],
        "count": 2,
    }).encode())
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "count", raw)


def test_main_refuses_the_pass_on_an_empty_tracked_list(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(body=json.dumps({"tracked": [], "count": 0}).encode())
    raw = _seed_cursor_bytes(ci_result_capture)
    _assert_pass_refused(ci_result_capture, monkeypatch, capsys, "empty", raw)


# --- main(): a token per owner, fed through the stub ------------------------


def test_main_mints_one_installation_token_per_distinct_owner(
        ci_result_capture, monkeypatch, tracked_stub):
    """A 200 body naming two pairs under two different owners: `main()`
    must mint one installation token per owner, not reuse a single owner's
    token for pairs it does not own."""
    tracked_stub(body=json.dumps({
        "tracked": [
            {"repo": "owner-a/repo-a", "workflow_file": "ci.yml", "product": "a"},
            {"repo": "owner-b/repo-b", "workflow_file": "ci.yml", "product": "b"},
        ],
        "count": 2,
    }).encode())
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(ci_result_capture, "gh_runs", lambda *a, **k: [])
    monkeypatch.setattr(ci_result_capture, "gh_reruns", lambda *a, **k: [])
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")

    seen_owners = []

    def fake_installation_for_owner(jwt, owner, **k):
        seen_owners.append(owner)
        return {"id": 1}

    monkeypatch.setattr(ci_result_capture.GA, "installation_for_owner",
                        fake_installation_for_owner)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 0
    assert sorted(seen_owners) == ["owner-a", "owner-b"]


def test_main_an_owners_failed_mint_leaves_its_cursors_unchanged_while_the_other_advances(
        ci_result_capture, monkeypatch, capsys, tracked_stub):
    tracked_stub(body=json.dumps({
        "tracked": [
            {"repo": "owner-a/repo-a", "workflow_file": "ci.yml", "product": "a"},
            {"repo": "owner-b/repo-b", "workflow_file": "ci.yml", "product": "b"},
        ],
        "count": 2,
    }).encode())
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})
    monkeypatch.setattr(ci_result_capture, "gh_reruns", lambda *a, **k: [])
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    ci_result_capture.save_cursor({"owner-a/repo-a#ci.yml": "2026-10-01T00:00:00Z"})

    def fake_installation_for_owner(jwt, owner, **k):
        if owner == "owner-a":
            raise ci_result_capture.GA.Unreadable("owner-a: no installation")
        return {"id": 1}

    monkeypatch.setattr(ci_result_capture.GA, "installation_for_owner",
                        fake_installation_for_owner)
    monkeypatch.setattr(ci_result_capture, "gh_runs",
                        lambda repo, wf, since, token, **k: [_run(run_id=1)])
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", lambda payload, **k: "created")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 1
    cursor = ci_result_capture.load_cursor()
    assert cursor["owner-a/repo-a#ci.yml"] == "2026-10-01T00:00:00Z", (
        "owner-a's mint failed, so its pair's cursor must stay exactly "
        "where it was")
    assert "owner-b/repo-b#ci.yml" in cursor, (
        "owner-b's pair must still advance even though owner-a's mint "
        "failed")
    assert "owner-a" in capsys.readouterr().err


def test_main_drops_a_stale_cursor_entry_and_keeps_a_deadline_interrupted_one(
        ci_result_capture, monkeypatch, tracked_stub):
    """A cursor entry for a pair no longer in this pass's list is dropped;
    one for a pair this pass's fetch budget cuts short is kept, exactly
    because the pair is still in the list — only its own cursor-advance
    rules (see `capture_workflow`'s `budget_exceeded`) decide where it
    lands."""
    tracked_stub(body=json.dumps({
        "tracked": [
            {"repo": "convivy/kept-a", "workflow_file": "ci.yml", "product": "a"},
            {"repo": "convivy/kept-b", "workflow_file": "ci.yml", "product": "b"},
        ],
        "count": 2,
    }).encode())
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    ci_result_capture.save_cursor({
        "convivy/kept-a#ci.yml": "2026-10-01T00:00:00Z",
        "convivy/kept-b#ci.yml": "2026-10-02T00:00:00Z",
        "convivy/retired#ci.yml": "2026-09-01T00:00:00Z",
    })

    run_a = _run(run_id=1, created_at="2026-10-01T10:00:00Z")
    safe_until = real_datetime.datetime(2026, 10, 2, 1, 0, 0, tzinfo=real_datetime.timezone.utc)

    def fake_gh_runs(repo, workflow_file, since, token, **k):
        if repo == "convivy/kept-a":
            return [run_a]
        raise ci_result_capture.FetchBudgetExceeded([], safe_until)

    monkeypatch.setattr(ci_result_capture, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(ci_result_capture.SC, "post_signal", lambda payload, **k: "created")
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    ci_result_capture.main()

    cursor = ci_result_capture.load_cursor()
    assert "convivy/retired#ci.yml" not in cursor, (
        "a cursor entry for a pair no longer in this pass's list must be "
        "dropped")
    assert cursor["convivy/kept-a#ci.yml"] > "2026-10-01T00:00:00Z"
    assert cursor["convivy/kept-b#ci.yml"] == safe_until.strftime("%Y-%m-%dT%H:%M:%SZ"), (
        "a pair the fetch budget interrupted must still keep its bounded "
        "cursor advance, since it is still in this pass's list")


# --- GhTokenRefresher: keep the GitHub token valid for the whole run --------


def test_gh_token_refresher_mints_lazily_and_reuses_a_token_far_from_expiry(
        ci_result_capture, monkeypatch):
    mints = []

    def fake_mint(jwt, inst_id, *, permissions=None, opener=None):
        mints.append((jwt, inst_id, permissions))
        return {"token": "ghs_1", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload", fake_mint)
    refresher = ci_result_capture.GhTokenRefresher(
        "jwt.x.y", "155330565", permissions=ci_result_capture._READ_ONLY_PERMISSIONS)

    assert mints == [], "must not mint before the first token() call"
    assert refresher.token() == "ghs_1"
    assert refresher.token() == "ghs_1"
    assert len(mints) == 1, "a token far from its own expiry must not be re-minted"
    assert mints[0][2] == ci_result_capture._READ_ONLY_PERMISSIONS


def test_gh_token_refresher_mints_a_fresh_token_before_the_held_one_is_near_expiry(
        ci_result_capture, monkeypatch):
    """Item 1's proactive half: a token within `margin` of its own expiry is
    replaced before anything gets a 401 from it."""
    now = real_datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=real_datetime.timezone.utc)
    monkeypatch.setattr(ci_result_capture, "dt", _FrozenDt)
    _FrozenClock._frozen = now

    mints = []

    def fake_mint(jwt, inst_id, *, permissions=None, opener=None):
        mints.append(1)
        expires = now + real_datetime.timedelta(minutes=10 if len(mints) == 1 else 60)
        return {"token": f"ghs_{len(mints)}",
               "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}

    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload", fake_mint)
    refresher = ci_result_capture.GhTokenRefresher(
        "jwt.x.y", "155330565", permissions=ci_result_capture._READ_ONLY_PERMISSIONS,
        margin=real_datetime.timedelta(minutes=15))

    assert refresher.token() == "ghs_1"
    assert refresher.token() == "ghs_2", (
        "the held token expires in 10 minutes, inside the 15-minute margin, "
        "so the second call must mint a replacement rather than reuse it")
    assert len(mints) == 2


def test_a_401_mints_a_fresh_token_and_retries_the_same_request_once(
        ci_result_capture, monkeypatch):
    """Item 4: a GitHub read that gets 401 against the refresher's current
    token mints exactly one replacement and retries the SAME request once
    more; the pair completes rather than failing — on origin/main (`gh_runs`
    takes only a bare token string, no refresher at all) this is the
    scenario that left every pair after a mid-run 401 failing for the rest
    of the run."""
    mints = []

    def fake_mint(jwt, inst_id, *, permissions=None, opener=None):
        mints.append(1)
        return {"token": f"ghs_{len(mints)}", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload", fake_mint)
    refresher = ci_result_capture.GhTokenRefresher(
        "jwt.x.y", "155330565", permissions=ci_result_capture._READ_ONLY_PERMISSIONS)

    seen_tokens = []

    def opener(req, timeout=None):
        tok = req.get_header("Authorization")
        seen_tokens.append(tok)
        if tok == "Bearer ghs_1":
            return _Resp(401, {"message": "Bad credentials"})
        return _Resp(200, {"workflow_runs": [_run(run_id=1)]})

    out = ci_result_capture.gh_runs(
        "test-owner/test-repo", "ci.yml", "2026-10-01T00:00:00Z", refresher, opener=opener)

    assert [r["id"] for r in out] == [1]
    assert len(mints) == 2, "exactly two mints: the first token and the one 401 retry"
    assert seen_tokens == ["Bearer ghs_1", "Bearer ghs_2"]


def test_two_401s_in_a_row_fail_the_pair_after_exactly_one_retry(
        ci_result_capture, monkeypatch):
    """Item 4: a minter whose new token also gets 401 makes the run fail
    that pair after one retry — a second 401 is a real failure, not
    retried again."""
    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload",
                        lambda *a, **k: {"token": "ghs_x",
                                        "expires_at": "2099-01-01T00:00:00Z"})
    refresher = ci_result_capture.GhTokenRefresher(
        "jwt.x.y", "155330565", permissions=ci_result_capture._READ_ONLY_PERMISSIONS)

    calls = []

    def opener(req, timeout=None):
        calls.append(1)
        return _Resp(401, {"message": "Bad credentials"})

    with pytest.raises(ci_result_capture.CaptureError) as exc:
        ci_result_capture.gh_runs(
            "test-owner/test-repo", "ci.yml", "2026-10-01T00:00:00Z", refresher, opener=opener)

    assert "401" in str(exc.value)
    assert len(calls) == 2, "exactly one retry after the first 401, no more"


def test_a_plain_token_string_still_raises_on_the_first_401_with_no_retry(
        ci_result_capture, monkeypatch):
    """A caller that passes a bare token string (every call site and test
    before this fix) has no refresher to mint a replacement from, so a 401
    still raises on the first attempt, exactly as before."""
    calls = []

    def opener(req, timeout=None):
        calls.append(1)
        return _Resp(401, {"message": "Bad credentials"})

    with pytest.raises(ci_result_capture.CaptureError):
        ci_result_capture.gh_runs(
            "test-owner/test-repo", "ci.yml", "2026-10-01T00:00:00Z", "ghs_x", opener=opener)
    assert len(calls) == 1


def test_main_re_mints_the_token_after_a_401_and_the_pass_still_completes(
        ci_result_capture, monkeypatch):
    """End to end through `main()`: the measured production symptom
    (2026-10-25) was a token that stopped working partway through a run,
    after which every later pair failed with 401 and the next pass started
    from the same cursors and failed the same way. With `main()`'s token a
    `GhTokenRefresher`, a 401 mid-run mints a replacement and the pass
    completes."""
    monkeypatch.setattr(
        ci_result_capture.GA, "installed_app",
        lambda: ci_result_capture.GA.InstalledApp(
            app_id="1234567", key_path=Path("/dev/null"), slug="test-app"))
    monkeypatch.setattr(ci_result_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(ci_result_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(ci_result_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    mints = []

    def fake_mint(jwt, inst_id, *, permissions=None, opener=None):
        mints.append(1)
        return {"token": f"ghs_{len(mints)}", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(ci_result_capture.GA, "installation_token_payload", fake_mint)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "test-owner/test-repo", "workflow_file": "ci.yml", "product": "testproduct"},))

    run = _run(run_id=42, conclusion="success")
    seen_tokens = []

    def fake_ga_request(method, url, bearer, body, opener=None):
        seen_tokens.append(bearer)
        if bearer == "ghs_1":
            return 401, {"message": "Bad credentials"}
        return 200, {"workflow_runs": [run]}

    monkeypatch.setattr(ci_result_capture.GA, "request", fake_ga_request)
    # This test is about the forward fetch's own token refresh, not the
    # independent re-run lookback `main()` also runs per pair — see
    # `_stub_github_app`'s own comment for why every other main()-level
    # test defaults this the same way.
    monkeypatch.setattr(ci_result_capture, "gh_reruns", lambda *a, **k: [])

    import signal_client as sc
    monkeypatch.setattr(sc.urllib.request, "urlopen", lambda req, timeout=None: _CreatedResp())
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    rc = ci_result_capture.main()

    assert rc == 0
    assert seen_tokens == ["ghs_1", "ghs_2"]
    assert len(mints) == 2, "the eager initial mint, plus the one 401-retry mint"
    cursor = ci_result_capture.load_cursor()
    assert "test-owner/test-repo#ci.yml" in cursor


# --- fetch budget: FetchBudgetExceeded bounds _gh_runs_window ---------------


def test_gh_runs_window_stops_at_the_deadline_with_a_proven_safe_prefix(
        ci_result_capture, monkeypatch):
    """**Item 2's fetch bound.** A capped window needing several splits to
    cover in full; the deadline passes partway through. `_gh_runs_window`
    must stop there rather than keep splitting, and `FetchBudgetExceeded`
    must carry exactly the runs from the sub-windows already proven
    complete, and `safe_until` at the boundary those stop at — never a run
    from a sub-window never queried, and never silently swallowing the
    interruption as a clean, complete result."""
    monkeypatch.setattr(ci_result_capture, "GITHUB_CREATED_FILTER_CAP", 2)
    clock = _FakeClock(step=1.0)
    monkeypatch.setattr(ci_result_capture.time, "monotonic", clock)

    runs = [
        _run(run_id=1, created_at="2026-10-01T00:00:00Z"),
        _run(run_id=2, created_at="2026-10-01T00:00:02Z"),
        _run(run_id=3, created_at="2026-10-01T00:00:04Z"),
        _run(run_id=4, created_at="2026-10-01T00:00:06Z"),
        _run(run_id=5, created_at="2026-10-01T00:00:08Z"),
    ]
    gh = _FakeGitHubActions(list(reversed(runs)), cap=2)  # newest-first, per its own contract

    since_dt = real_datetime.datetime(2026, 10, 1, 0, 0, 0, tzinfo=real_datetime.timezone.utc)
    until_dt = real_datetime.datetime(2026, 10, 1, 0, 0, 8, tzinfo=real_datetime.timezone.utc)

    with pytest.raises(ci_result_capture.FetchBudgetExceeded) as exc:
        ci_result_capture._gh_runs_window(
            "test-owner/test-repo", "ci.yml", since_dt, until_dt, "ghs_x",
            opener=gh, deadline=5.0)

    ex = exc.value
    assert [r["id"] for r in ex.runs] == [1], (
        "only run 1's own one-second sub-window had resolved as proven "
        "complete by the time the deadline hit")
    assert ex.safe_until == since_dt + real_datetime.timedelta(seconds=1)
    assert ex.safe_until < until_dt, "the deadline must have interrupted the window"


def test_capture_workflow_posts_the_budget_exceeded_prefix_and_bounds_the_cursor(
        ci_result_capture):
    """**Item 2's partial-credit contract.** `fetcher` raises
    `FetchBudgetExceeded` with one proven run and a `safe_until` boundary;
    `capture_workflow` must still post that run, tally `budget_exceeded`
    (not a plain, unexplained error), and bound `next_since` to
    `safe_until` rather than the ordinary overlap margin — the point being
    that the NEXT pass picks up from `safe_until`, not from `since` all
    over again."""
    run = _run(run_id=1, created_at="2026-10-01T00:00:00Z")
    safe_until = real_datetime.datetime(2026, 10, 1, 0, 0, 1, tzinfo=real_datetime.timezone.utc)

    def fetcher(repo, workflow_file, since, token, **k):
        raise ci_result_capture.FetchBudgetExceeded([run], safe_until)

    store = _FakeSignalStore()
    run_started = real_datetime.datetime(2026, 10, 1, 1, 0, 0, tzinfo=real_datetime.timezone.utc)
    tally = ci_result_capture.capture_workflow(
        "test-owner/test-repo", "ci.yml", "testproduct", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher, poster=store.post_signal, run_started=run_started)

    assert tally["created"] == 1
    assert store.rows, "the proven prefix must still be posted"
    assert tally["errors"] == 1
    assert tally["budget_exceeded"] is True
    assert tally["next_since"] == safe_until.strftime("%Y-%m-%dT%H:%M:%SZ"), (
        "next_since must be bounded to the proven safe_until, not the "
        "ordinary overlap margin (which here, near run_started, is much "
        "later)")


def test_an_ordinary_post_failure_after_a_budget_exceeded_fetch_blocks_the_advance(
        ci_result_capture):
    """Twin of the sustained-429 boundary test: an ordinary post failure in
    the same batch clears the budget-exceeded exception too, the same way
    it already clears the 429 one — never advance past a run that was not
    actually delivered."""
    run = _run(run_id=1, created_at="2026-10-01T00:00:00Z")
    safe_until = real_datetime.datetime(2026, 10, 1, 0, 0, 1, tzinfo=real_datetime.timezone.utc)

    def fetcher(repo, workflow_file, since, token, **k):
        raise ci_result_capture.FetchBudgetExceeded([run], safe_until)

    store = _FakeSignalStore(fail_dedup_keys={
        ci_result_capture.signal_from_run(run, "testproduct", "ci")["dedup_key"]})
    tally = ci_result_capture.capture_workflow(
        "test-owner/test-repo", "ci.yml", "testproduct", "2026-10-01T00:00:00Z", "ghs_x", "sig_tok",
        fetcher=fetcher, poster=store.post_signal)

    assert tally["errors"] == 2, "the budget exception and the ordinary post failure"
    assert tally["budget_exceeded"] is False, (
        "an ordinary post failure must clear the budget-exceeded exception")


def test_main_stops_starting_new_pairs_once_the_run_budget_is_exhausted(
        ci_result_capture, monkeypatch):
    """**Item 2's whole-run bound.** The deadline is already in the past
    once `main()` reaches the loop — every pair must be left untouched (no
    cursor key written at all, not even a failed one) rather than attempted
    or failed, and rotation resumes exactly where this pass stopped."""
    _stub_github_app(ci_result_capture, monkeypatch)
    monkeypatch.setenv(ci_result_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(ci_result_capture, "TRACKED", (
        {"repo": "convivy/a", "workflow_file": "ci.yml", "product": "a"},
        {"repo": "convivy/b", "workflow_file": "ci.yml", "product": "b"},
        {"repo": "convivy/c", "workflow_file": "ci.yml", "product": "c"},
    ))
    monkeypatch.setattr(ci_result_capture, "_RUN_DEADLINE_SECONDS", -1)
    monkeypatch.setattr(ci_result_capture.sys, "argv", ["ci-result-capture"])

    def fail_fetch(*a, **k):
        raise AssertionError("no pair's fetch may run once the budget is exhausted")

    monkeypatch.setattr(ci_result_capture, "gh_runs", fail_fetch)

    rc = ci_result_capture.main()

    assert rc == 0, "skipping every pair for budget reasons is not itself a failure"
    assert ci_result_capture.load_cursor() == {}
    assert ci_result_capture.load_rotation_start() is None, (
        "nothing was touched this pass, so no rotation file is written at "
        "all — the next pass falls back to the top the same way it would "
        "with no file ever written")


# --- recovery: a stale cursor catches up across passes under a bounded fetch -


def test_a_stale_cursor_makes_progress_across_several_passes_under_a_fetch_budget(
        ci_result_capture, monkeypatch):
    """**Item 3's recovery proof.** A cursor stuck well behind, and a
    capped window needing many splits to cover in full — more real runs
    than one pass's fetch budget can read. Each pass's own
    `_gh_runs_window` call must stop at the budget via
    `FetchBudgetExceeded` rather than either running unbounded or raising a
    plain, unadvanceable `CaptureError`; `capture_workflow` posts what was
    proven and advances the cursor to `safe_until`. Repeating with the
    advanced cursor must keep making headway until the cursor reaches the
    newest run — on origin/main (no budget bound in `_gh_runs_window` at
    all) a pass like this either runs with no bound or (once one is added
    with no partial credit) never advances past its very first cursor."""
    monkeypatch.setattr(ci_result_capture, "GITHUB_CREATED_FILTER_CAP", 2)

    run_count = 20
    base = real_datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=real_datetime.timezone.utc)
    runs = [_run(run_id=i, created_at=(base + real_datetime.timedelta(minutes=i)
                                       ).strftime("%Y-%m-%dT%H:%M:%SZ"))
           for i in range(run_count)]
    now = base + real_datetime.timedelta(minutes=run_count + 5)
    monkeypatch.setattr(ci_result_capture, "dt", _FrozenDt)
    _FrozenClock._frozen = now

    gh = _FakeGitHubActions(list(reversed(runs)), cap=2)  # newest-first, per its own contract

    clock = _FakeClock(step=1.0)
    monkeypatch.setattr(ci_result_capture.time, "monotonic", clock)

    def fetcher(repo, workflow_file, since, token, **k):
        return ci_result_capture.gh_runs(repo, workflow_file, since, token,
                                         opener=gh, deadline=k.get("deadline"))

    store = _FakeSignalStore()
    since = base.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursors_seen = [since]
    for _ in range(50):
        deadline = clock() + 12
        tally = ci_result_capture.capture_workflow(
            "test-owner/test-repo", "ci.yml", "testproduct", since, "ghs_x", "sig_tok",
            fetcher=fetcher, poster=store.post_signal, run_started=now,
            deadline=deadline)
        assert tally["errors"] == 0 or tally["budget_exceeded"], (
            f"a pair not yet caught up must fail only via the "
            f"budget-exceeded path, never an unexplained error: {tally}")
        since = tally["next_since"]
        cursors_seen.append(since)
        if since >= runs[-1]["created_at"]:
            break
    else:
        raise AssertionError("did not catch up within 50 simulated passes")

    assert len(cursors_seen) > 2, "a single pass must not have been enough"
    assert all(a <= b for a, b in zip(cursors_seen, cursors_seen[1:])), (
        "the cursor must never move backwards across passes")
    assert len(store.rows) == run_count, (
        "every run must eventually be posted exactly once as the cursor catches up")
