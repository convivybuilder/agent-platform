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
    monkeypatch.setattr(ci_result_capture.GA, "single_installation",
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
