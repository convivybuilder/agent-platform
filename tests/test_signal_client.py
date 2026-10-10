"""Tests for the loopback signal-store client (Slack-retirement Phase B, ARCH-353).

Locks in: token resolution (env > file > None), the tee kill-switch, channel→
product mapping, text extraction from Slack payloads, and the best-effort POST
(never raises; skip without a token).
"""
import json

import pytest

import signal_client as sc


def test_token_prefers_env_then_file(tmp_path, monkeypatch):
    monkeypatch.setenv(sc.TOKEN_ENV, "env-secret")
    assert sc.token() == "env-secret"
    monkeypatch.delenv(sc.TOKEN_ENV, raising=False)
    tf = tmp_path / "tok"
    tf.write_text("file-secret\n")
    monkeypatch.setattr(sc, "TOKEN_FILE", tf)
    assert sc.token() == "file-secret"


def test_token_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv(sc.TOKEN_ENV, raising=False)
    monkeypatch.setattr(sc, "TOKEN_FILE", tmp_path / "missing")
    assert sc.token() is None



def test_product_for_channel():
    assert sc.product_for_channel("vivy-explanations") == "vivy"
    assert sc.product_for_channel("sous-chef-bugs") == "sous-chef"
    assert sc.product_for_channel("selector-features") == "selector"
    assert sc.product_for_channel("svc-changelog") == "convivy-app-kit"
    assert sc.product_for_channel("cost") == "platform"
    assert sc.product_for_channel("alerts-code-health") == "platform"
    assert sc.product_for_channel("") == "platform"


def test_extract_text_top_level():
    assert sc.extract_text({"text": "hello"}) == "hello"


def test_extract_text_from_blocks():
    payload = {"blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": "line one"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "footer"}]},
    ]}
    out = sc.extract_text(payload)
    assert "line one" in out and "footer" in out


def test_extract_text_empty():
    assert sc.extract_text({}) == ""
    assert sc.extract_text({"blocks": [{"type": "divider"}]}) == ""
    assert sc.extract_text("not a dict") == ""


def test_post_signal_skips_without_token(monkeypatch):
    monkeypatch.setattr(sc, "token", lambda: None)
    assert sc.post_signal({"source": "slack"}) == "skip"


def test_post_signal_without_token_warns_once_to_stderr(monkeypatch, capsys):
    """knowledge#793 round 2: a run's Bash-side post (the telemetry tee) is dropped
    for want of a token inside a dispatched run's sandbox. The drop must be visible
    once, not silent, and not repeated on every later post in the same process."""
    monkeypatch.setattr(sc, "token", lambda: None)
    monkeypatch.setattr(sc, "_WARNED_NO_TOKEN", False)
    assert sc.post_signal({"source": "slack"}) == "skip"
    assert "no token" in capsys.readouterr().err
    assert sc.post_signal({"source": "slack"}) == "skip"
    assert capsys.readouterr().err == ""


def test_post_signal_created(monkeypatch):
    captured = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    assert sc.post_signal({"source": "slack", "kind": "notification"}, tok="t") == "created"
    assert captured["url"].endswith("/api/signals")
    assert captured["auth"] == "Bearer t"


def test_post_signal_swallows_errors(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("loopback down")
    monkeypatch.setattr(sc.urllib.request, "urlopen", boom)
    assert sc.post_signal({"source": "slack"}, tok="t").startswith("error:")



def test_post_report_and_alert_shape(monkeypatch):
    sent = []
    monkeypatch.setattr(sc, "post_signal", lambda payload, **k: sent.append(payload) or "created")
    sc.post_report("model-scorecard", "the text", meta={"rows": [1, 2]}, dedup_key="k1")
    sc.post_alert("overnight-healthcheck", "boom", meta={"problems": ["x"]})
    rep, alr = sent
    assert rep["kind"] == "digest" and rep["stream"] == "model-scorecard"
    assert rep["source"] == "platform" and rep["meta"]["rows"] == [1, 2] and rep["dedup_key"] == "k1"
    assert alr["kind"] == "alert" and alr["stream"] == "overnight-healthcheck"
    assert alr["meta"]["problems"] == ["x"]


def test_post_structured_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(sc, "post_signal", boom)
    assert sc.post_structured("digest", "s", "b").startswith("error:")


def test_the_tee_keeps_the_short_budget(monkeypatch):
    """The tee's write is on _TIMEOUT (2s) — a slow store must never delay a
    live Slack message. This is the budget the delivery path must NOT share."""
    seen = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    sc.post_signal({"source": "slack"}, tok="t")
    assert seen["timeout"] == sc._TIMEOUT == 2


def test_a_structured_emit_uses_the_delivery_budget_not_the_tee_s(monkeypatch):
    """A structured report/alert is a one-shot scheduled job that wants delivery,
    so its write runs on _DELIVERY_TIMEOUT, not the tee's 2s. A store slower than
    2s (a cold process) would otherwise report a timeout on a write it completes
    anyway — a dropped alert that reads as delivered."""
    seen = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(sc, "token", lambda: "t")
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    sc.post_alert("budget-cap", "hard cap crossed", meta={"pool": "overnight"})
    assert seen["timeout"] == sc._DELIVERY_TIMEOUT
    assert seen["timeout"] > sc._TIMEOUT


def test_post_structured_default_timeout_is_unchanged(monkeypatch):
    """A caller that passes no `timeout=` (every existing launchd job — the cost
    report, budget watchdog, metric alerts, the scorecard) still gets the full
    delivery budget. The finalize-path override (agentctl's per-run
    _post_model_resolution_signal, PR #477 round 2) is a per-call-site decision,
    not a change to the default."""
    seen = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(sc, "token", lambda: "t")
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    sc.post_structured("digest", "s", "b")
    assert seen["timeout"] == sc._DELIVERY_TIMEOUT == 30


def test_post_structured_accepts_a_shorter_timeout_override(monkeypatch):
    """A caller on a latency-sensitive hot path (agentctl's per-run finalize
    post) can pass a shorter `timeout=` than the 30s one-shot-job default, so a
    slow-but-not-down store doesn't add dead time to every run's exit."""
    seen = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(sc, "token", lambda: "t")
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    sc.post_structured("digest", "s", "b", timeout=3)
    assert seen["timeout"] == 3


def test_a_dropped_structured_delivery_is_logged_not_silent(monkeypatch, capsys):
    """The failure the old swallow-everything hid: every caller discards the
    return, so an 'error:' result vanished. A wanted delivery that did not land
    now reaches the job's log."""
    monkeypatch.setattr(sc, "post_signal", lambda payload, **k: "error:timeout")
    assert sc.post_alert("budget-cap", "body") == "error:timeout"
    err = capsys.readouterr().err
    assert "NOT delivered" in err and "budget-cap" in err and "error:timeout" in err


def test_a_landed_or_skipped_structured_emit_stays_quiet(monkeypatch, capsys):
    """created / duplicate / skip are not failures — only 'error:' is. A
    token-less box (skip) or an idempotent re-post (duplicate) must not spam the
    log, or the signal that matters drowns."""
    for result in ("created", "duplicate", "skip"):
        monkeypatch.setattr(sc, "post_signal", lambda payload, _r=result, **k: _r)
        assert sc.post_report("model-scorecard", "body") == result
        assert capsys.readouterr().err == ""


def test_post_structured_carries_produced_at_only_when_given(monkeypatch):
    """A caller that knows when the reported thing was true passes it through;
    every other caller's payload is unchanged, so the store leaves it null."""
    sent = []
    monkeypatch.setattr(sc, "post_signal", lambda payload, **k: sent.append(payload) or "created")
    sc.post_structured("pr_state", "pr-state", "b", produced_at="2026-09-14T17:18:59Z")
    sc.post_structured("digest", "s", "b")
    assert sent[0]["produced_at"] == "2026-09-14T17:18:59Z"
    assert "produced_at" not in sent[1]


# --- delivery retries: only what can be sent twice without a second row --------


def _sequence(monkeypatch, results):
    """Stand in for one POST per call, returning `results` in order. An entry
    may be a plain result string (wrapped as `(result, None)`, no Retry-After)
    or a `(result, retry_after)` tuple for a 429 case."""
    calls = []

    def once(payload, tok, timeout):
        calls.append(payload)
        r = results[len(calls) - 1]
        return r if isinstance(r, tuple) else (r, None)

    monkeypatch.setattr(sc, "_post_once", once)
    return calls


@pytest.mark.parametrize("failure", ["error:503", "error:TimeoutError", "error:URLError"])
def test_a_refusal_or_timeout_is_retried_when_a_dedup_key_makes_it_idempotent(monkeypatch, failure):
    """The eight lost model_resolution records on 2026-09-15/16 all logged
    `error:TimeoutError`; each carried `model-resolution-<run>` as its dedup_key."""
    calls = _sequence(monkeypatch, [failure, failure, "duplicate"])
    slept = []
    assert sc.post_signal({"dedup_key": "model-resolution-run-x"}, tok="t", attempts=3,
                          sleep=slept.append) == "duplicate"
    assert len(calls) == 3 and len(slept) == 2


@pytest.mark.parametrize("failure", ["error:503", "error:TimeoutError", "error:URLError"])
def test_without_a_dedup_key_nothing_is_retried(monkeypatch, failure):
    """The Library's busy handler: "the body does not say whether anything was
    written", so even a 503 could become a second row."""
    calls = _sequence(monkeypatch, [failure, "created"])
    assert sc.post_signal({"kind": "alert"}, tok="t", attempts=3,
                          sleep=lambda s: None) == failure
    assert len(calls) == 1


def test_a_client_error_is_not_retried(monkeypatch):
    calls = _sequence(monkeypatch, ["error:400", "created"])
    assert sc.post_signal({"dedup_key": "k"}, tok="t", attempts=3,
                          sleep=lambda s: None) == "error:400"
    assert len(calls) == 1


def test_attempts_stop_at_the_limit_and_return_the_last_error(monkeypatch):
    calls = _sequence(monkeypatch, ["error:503"] * 5)
    assert sc.post_signal({"dedup_key": "k"}, tok="t", attempts=3,
                          sleep=lambda s: None) == "error:503"
    assert len(calls) == 3


def test_the_tee_default_makes_one_attempt(monkeypatch):
    """The tee sits on a live message path with a 2s budget; it never waits to retry."""
    calls = _sequence(monkeypatch, ["error:503", "created"])
    assert sc.post_signal({"dedup_key": "k"}, tok="t") == "error:503"
    assert len(calls) == 1


def test_post_structured_retries(monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "post_signal", lambda payload, **kw: seen.update(kw) or "created")
    sc.post_structured("model_resolution", "model-resolution", "b", dedup_key="k", timeout=3)
    assert seen["attempts"] == sc._DELIVERY_ATTEMPTS > 1


# --- 429 / Retry-After: wait and resend, bounded only by the deadline ----------


def test_a_429_is_retried_after_its_retry_after_wait(monkeypatch):
    calls = _sequence(monkeypatch, [("error:429", "2"), "created"])
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", attempts=3, sleep=slept.append,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert len(calls) == 2
    assert slept == [2.0]


def test_a_429_keeps_resending_past_the_attempts_budget(monkeypatch):
    """429 is not bounded by `attempts` — only the deadline stops it. Four 429s
    in a row, with `attempts=3`, still land the fifth try."""
    calls = _sequence(monkeypatch, [("error:429", "1")] * 4 + ["created"])
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", attempts=3, sleep=slept.append,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert len(calls) == 5
    assert slept == [1.0, 1.0, 1.0, 1.0]


def test_a_429_is_not_gated_on_dedup_key(monkeypatch):
    """Unlike a 503 (ambiguous: might already have written), a 429 means the
    rate limiter rejected the request before any write — safe to resend even
    with no dedup_key."""
    calls = _sequence(monkeypatch, [("error:429", "1"), "created"])
    result = sc.post_signal({"kind": "alert"}, tok="t", attempts=2, sleep=lambda s: None,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert len(calls) == 2


def test_a_429_is_retried_with_no_attempts_budget_at_all(monkeypatch):
    """429 retry is keyed on `deadline` alone, independent of `attempts` —
    `attempts=1` (the default) still retries a 429 when a `deadline` is
    given, because `attempts` only bounds the OTHER retry branch (503/
    timeout/connection-error). transcript-capture relies on exactly this:
    it passes a `deadline` but no `attempts`, wanting the 429 retry without
    also picking up a 503 retry it never asked for."""
    calls = _sequence(monkeypatch, [("error:429", "1"), "created"])
    result = sc.post_signal({"dedup_key": "k"}, tok="t", sleep=lambda s: None,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert len(calls) == 2


def test_a_429_with_a_missing_retry_after_falls_back_to_a_bounded_wait(monkeypatch):
    _sequence(monkeypatch, [("error:429", None), "created"])
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", attempts=2, sleep=slept.append,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert slept == [sc._RETRY_AFTER_FALLBACK_SECONDS]


def test_a_429_with_an_unparseable_retry_after_falls_back_to_a_bounded_wait(monkeypatch):
    _sequence(monkeypatch, [("error:429", "not-a-number"), "created"])
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", attempts=2, sleep=slept.append,
                            deadline=sc.time.monotonic() + 600)
    assert result == "created"
    assert slept == [sc._RETRY_AFTER_FALLBACK_SECONDS]


def test_a_negative_retry_after_falls_back_to_a_bounded_wait(monkeypatch):
    _sequence(monkeypatch, [("error:429", "-5"), "created"])
    slept = []
    sc.post_signal({"dedup_key": "k"}, tok="t", attempts=2, sleep=slept.append,
                   deadline=sc.time.monotonic() + 600)
    assert slept == [sc._RETRY_AFTER_FALLBACK_SECONDS]


def test_the_tee_default_never_retries_a_429(monkeypatch):
    """attempts=1 (the tee's default) makes exactly one POST, 429 included —
    the tee's 2s budget must never be delayed by a retry it never asked for.
    The tee also never passes a `deadline`, which is what actually gates the
    429 branch now; see test_a_429_is_retried_with_no_attempts_budget_at_all
    for the case where `attempts` alone does not stop a 429 retry."""
    calls = _sequence(monkeypatch, [("error:429", "2"), "created"])
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", sleep=slept.append)
    assert result == "error:429"
    assert len(calls) == 1
    assert slept == []


def test_a_429_still_unresolved_at_the_deadline_is_reported_as_a_failure_not_dropped(monkeypatch):
    """The capture's own deadline ends mid-retry: the unsent signal comes back
    as 'error:429' (a failure a caller can see and count), never silently
    dropped."""
    calls = _sequence(monkeypatch, [("error:429", "2")] * 5)
    # deadline=3.0, a 2s wait from "now"=2.0 would land at 4.0 — past it.
    monkeypatch.setattr(sc.time, "monotonic", lambda: 2.0)
    slept = []
    result = sc.post_signal({"dedup_key": "k"}, tok="t", attempts=2, sleep=slept.append,
                            deadline=3.0)
    assert result == "error:429"
    assert len(calls) == 1
    assert slept == []


def test_a_429_with_no_deadline_counts_against_attempts_on_the_backoff(monkeypatch):
    """A caller with no `deadline` retries a 429 inside `attempts`, like a 503,
    on the jittered backoff. The 60s `Retry-After` is not waited on: a caller's
    worst case is computed from `attempts` and the backoff, and a 429 must not
    stretch it. `random.uniform` returns its upper bound, so the sleeps are the
    longest the backoff allows: 1.5s then 3.0s, 4.5s in all."""
    calls = _sequence(monkeypatch, [("error:429", "60")] * 3)
    monkeypatch.setattr(sc.random, "uniform", lambda a, b: b)
    slept = []
    result = sc.post_signal({"kind": "alert"}, tok="t", attempts=3, sleep=slept.append)
    assert result == "error:429"
    assert len(calls) == 3
    assert slept == [1.5, 3.0]


def test_the_tee_default_with_no_deadline_still_returns_the_first_429(monkeypatch):
    """`attempts=1` with no `deadline` is the tee: one POST, and the 429 comes
    back at once, so the tee's 2s budget never waits on a retry."""
    calls = _sequence(monkeypatch, [("error:429", "1")] * 2)
    slept = []
    result = sc.post_signal({"kind": "alert"}, tok="t", attempts=1, sleep=slept.append)
    assert result == "error:429"
    assert len(calls) == 1
    assert slept == []


def test_post_structured_with_no_deadline_retries_a_429_within_its_attempts(monkeypatch, tmp_path):
    """The real call surface every non-capture caller uses (agentctl's
    finalize post, budget-watchdog, metric-alerts, model-scorecard, and the
    rest all go through `post_structured`, none of them passing `deadline`)
    — through the real `post_signal`, not a mock of it, so this exercises
    the same code path `test_post_structured_retries` above bypasses."""
    monkeypatch.setenv(sc.TOKEN_ENV, "env-secret")
    monkeypatch.setattr(sc, "TOKEN_FILE", tmp_path / "missing")
    calls = _sequence(monkeypatch, [("error:429", "1"), "created"])
    slept = []
    result = sc.post_structured("alert", "budget-cap", "over budget", dedup_key="k",
                                sleep=slept.append)
    assert result == "created"
    assert len(calls) == 2
    assert len(slept) == 1


def test_a_429_then_success_logs_the_alert(monkeypatch, tmp_path):
    """A rate-limited alert is resent and lands. Before this change, the first
    429 returned and the alert was lost with nothing retried."""
    monkeypatch.setenv(sc.TOKEN_ENV, "env-secret")
    monkeypatch.setattr(sc, "TOKEN_FILE", tmp_path / "missing")
    calls = _sequence(monkeypatch, [("error:429", "1"), "created"])
    result = sc.post_alert("primary-mutation", "body", product="vivy", dedup_key="k",
                           sleep=lambda s: None)
    assert result == "created"
    assert len(calls) == 2


def test_an_alert_makes_three_attempts_and_waits_at_most_four_and_a_half_seconds(monkeypatch,
                                                                                tmp_path):
    """The bound the finalize and park comments state, for a store that stops answering: three POSTs,
    each with its own 5s socket timeout, and at most 4.5s of backoff between them. Three timeouts with
    the backoff at its upper bound is 3 x 5 + 4.5 = 19.5 seconds. A trickling store is not covered."""
    monkeypatch.setenv(sc.TOKEN_ENV, "env-secret")
    monkeypatch.setattr(sc, "TOKEN_FILE", tmp_path / "missing")
    monkeypatch.setattr(sc.random, "uniform", lambda a, b: b)
    calls = _sequence(monkeypatch, ["error:TimeoutError"] * 3)
    slept = []
    result = sc.post_alert("primary-mutation", "body", product="vivy", dedup_key="k",
                           timeout=5, sleep=slept.append)
    assert result == "error:TimeoutError"
    assert len(calls) == 3
    assert sum(slept) == 4.5
    assert 3 * 5 + sum(slept) == 19.5


def test_post_once_captures_the_retry_after_header_on_a_real_429(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise sc.urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                        {"Retry-After": "7"}, None)

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    result, retry_after = sc._post_once({"dedup_key": "k"}, "t", 2)
    assert result == "error:429"
    assert retry_after == "7"


def test_post_once_reports_no_retry_after_on_a_non_429_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise sc.urllib.error.HTTPError(req.full_url, 503, "Busy", {}, None)

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    result, retry_after = sc._post_once({"dedup_key": "k"}, "t", 2)
    assert result == "error:503"
    assert retry_after is None


def test_post_structured_passes_deadline_and_sleep_through_to_post_signal(monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "post_signal", lambda payload, **kw: seen.update(kw) or "created")

    def my_sleep(s):
        return None

    sc.post_structured("pr_state", "pr-state", "b", dedup_key="k", deadline=42.0, sleep=my_sleep)
    assert seen["deadline"] == 42.0
    assert seen["sleep"] is my_sleep


def test_post_structured_passes_tok_through_to_post_signal(monkeypatch):
    """A caller with its own credential (pr-state-capture, transcript-capture —
    issue_01M3VY56TWPFFHQ50QTFM5P07K) passes `tok=` here so `post_signal`
    presents it instead of falling back to the shared agent-platform token."""
    seen = {}
    monkeypatch.setattr(sc, "post_signal", lambda payload, **kw: seen.update(kw) or "created")
    sc.post_structured("pr_state", "pr-state", "b", tok="own-credential")
    assert seen["tok"] == "own-credential"


def test_post_structured_with_no_tok_leaves_post_signal_to_fall_back(monkeypatch):
    """Every caller except the two captures passes no `tok` — `post_signal`'s
    own `tok or token()` must still reach the shared credential unchanged."""
    seen = {}
    monkeypatch.setattr(sc, "post_signal", lambda payload, **kw: seen.update(kw) or "created")
    sc.post_structured("digest", "s", "b")
    assert seen["tok"] is None


def test_post_structured_tok_reaches_the_authorization_header_over_the_shared_token(monkeypatch):
    """End to end through the real `post_signal`/`_post_once`: a caller's own
    `tok=` must reach the wire, not the shared token `sc.token()` would
    otherwise supply."""
    seen = {}

    class _Resp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(sc, "token", lambda: "shared-token")
    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    sc.post_structured("pr_state", "pr-state", "b", tok="own-token")
    assert seen["auth"] == "Bearer own-token"
