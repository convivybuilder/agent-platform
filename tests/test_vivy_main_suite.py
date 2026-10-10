"""Tests for vivy_main_suite — the nightly vivy main-branch test runner.

Covers: failing suite → logs an alert signal with SHA and failing test names;
passing suite → silent (no signal logged); dedup_key stable within a week;
rebreak after fix gets a fresh key in the new week; checkout hard-reset before
each run; harness error (suite could not run) → exit 2, no failure signal, but
harness alert emitted to the plane; alert post failure does not change exit 2;
_weekly_once_key uses ISO %G-W%V format and is stable within an ISO week.

Does NOT run vivy's suite — fixtures and fakes only.
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

import vivy_main_suite as vms

# Fixed once_key used across tests that need a deterministic value.
_FIXED_ONCE_KEY = "vivy-main-suite-broken-2026-W30"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _junitxml(
    path: Path,
    failures: list[tuple[str, str]] = (),
    passes: list[tuple[str, str]] = (),
) -> None:
    root = ET.Element("testsuites")
    ts = ET.SubElement(root, "testsuite")
    for classname, name in passes:
        ET.SubElement(ts, "testcase", classname=classname, name=name)
    for classname, name in failures:
        tc = ET.SubElement(ts, "testcase", classname=classname, name=name)
        ET.SubElement(tc, "failure", message="AssertionError")
    ET.ElementTree(root).write(path)


# ---------------------------------------------------------------------------
# _extract_failed_tests
# ---------------------------------------------------------------------------


def test_extract_no_failures(tmp_path):
    xml = tmp_path / "r.xml"
    _junitxml(xml, passes=[("tests.test_foo", "test_ok")])
    assert vms._extract_failed_tests(xml) == []


def test_extract_finds_failures(tmp_path):
    xml = tmp_path / "r.xml"
    _junitxml(xml, failures=[
        ("tests.test_foo", "test_bar"),
        ("tests.test_baz", "test_qux"),
    ])
    result = vms._extract_failed_tests(xml)
    assert "tests.test_foo::test_bar" in result
    assert "tests.test_baz::test_qux" in result


def test_extract_missing_file(tmp_path):
    assert vms._extract_failed_tests(tmp_path / "missing.xml") == []


def test_extract_malformed_xml(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml at all")
    assert vms._extract_failed_tests(bad) == []


# ---------------------------------------------------------------------------
# _weekly_once_key — ISO format shape and stability
# ---------------------------------------------------------------------------


def test_weekly_once_key_iso_shape():
    """Key matches the ISO-week pattern %G-W%V without pinning a literal."""
    assert re.fullmatch(r"vivy-main-suite-broken-\d{4}-W\d{2}", vms._weekly_once_key())


def test_weekly_once_key_stable_within_iso_week(monkeypatch):
    """Two datetimes in the same ISO week produce the same key."""
    monday = datetime(2026, 7, 20, tzinfo=timezone.utc)  # ISO 2026-W30
    friday = datetime(2026, 7, 24, tzinfo=timezone.utc)  # ISO 2026-W30

    results = []
    for dt in (monday, friday):
        monkeypatch.setattr(vms, "_utcnow", lambda d=dt: d)
        results.append(vms._weekly_once_key())

    assert results[0] == results[1]
    assert results[0].endswith("-W30")


# ---------------------------------------------------------------------------
# reset_to_origin_main
# ---------------------------------------------------------------------------


def test_reset_returns_sha(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "show-ref" in cmd:
            return _Proc(returncode=0, stdout="f" * 40 + "\n")
        if "rev-parse" in cmd:
            return _Proc(returncode=0, stdout="abc123def456\n")
        return _Proc(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = vms.reset_to_origin_main(tmp_path)

    assert sha == "abc123def456"
    assert any("fetch" in c for c in calls)
    # Reset to the exactly-resolved sha, never to a ref name git would search for.
    assert any(c[-3:] == ["reset", "--hard", "f" * 40] for c in calls)
    assert any("rev-parse" in c for c in calls)


def test_reset_fetch_called_before_reset(tmp_path, monkeypatch):
    order: list[str] = []

    def fake_run(cmd, **kwargs):
        if "show-ref" in cmd:
            return _Proc(returncode=0, stdout="f" * 40 + "\n")
        if "fetch" in cmd:
            order.append("fetch")
        elif "reset" in cmd:
            order.append("reset")
        elif "rev-parse" in cmd:
            order.append("rev-parse")
        return _Proc(returncode=0, stdout="sha0\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    vms.reset_to_origin_main(tmp_path)

    assert order.index("fetch") < order.index("reset")


# ---------------------------------------------------------------------------
# ensure_checkout
# ---------------------------------------------------------------------------


def test_ensure_checkout_skips_if_exists(tmp_path, monkeypatch):
    clones: list = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: clones.append(cmd) or _Proc(0))
    existing = tmp_path / "checkout"
    existing.mkdir()
    vms.ensure_checkout(existing, "git@github.com:convivy/vivy.git")
    assert not any("clone" in str(c) for c in clones)


def test_ensure_checkout_clones_when_absent(tmp_path, monkeypatch):
    cloned: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cloned.append(list(cmd))
        return _Proc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    vms.ensure_checkout(tmp_path / "checkout", "git@github.com:convivy/vivy.git")
    assert any("clone" in c for c in cloned)


def test_ensure_checkout_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: _Proc(returncode=1, stderr="permission denied"),
    )
    with pytest.raises(RuntimeError, match="git clone failed"):
        vms.ensure_checkout(tmp_path / "checkout", "git@github.com:convivy/vivy.git")


# ---------------------------------------------------------------------------
# signal_failure — carries SHA and failing tests
# ---------------------------------------------------------------------------


def test_failing_suite_logs_signal_with_sha_and_tests(monkeypatch, tmp_path):
    """A failing suite logs an alert signal with the SHA and failing test names,
    keyed on the weekly dedup_key, and never creates a work item."""
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: _FIXED_ONCE_KEY)
    posted: list[dict] = []
    monkeypatch.setattr(vms, "post_alert",
                        lambda stream, body, *, product="platform", dedup_key=None, **kw:
                        posted.append(dict(stream=stream, body=body, product=product,
                                           dedup_key=dedup_key, meta=kw.get("meta"))) or "created")

    sha = "deadbeef12345678deadbeef1234567890abcdef"
    results = [
        vms.RunResult(
            exit_code=1,
            failed_tests=["tests.test_alerts::test_end_to_end"],
            stage="serial",
        ),
    ]
    assert vms.signal_failure(sha, results, tmp_path) == "created"

    assert len(posted) == 1
    assert posted[0]["meta"] == {"severity": "high"}, "the item this replaced was filed high"
    sig = posted[0]
    assert sig["stream"] == "vivy-main-suite"
    assert sig["product"] == "vivy"
    title, _, rest = sig["body"].partition("\n\n")
    assert sha[:12] in title
    assert sha in rest
    assert "tests.test_alerts::test_end_to_end" in rest
    assert sig["dedup_key"] == _FIXED_ONCE_KEY
    assert not hasattr(vms, "post_issue"), "the failure path must not import the item client"


def test_dedup_key_is_stable_within_week(monkeypatch, tmp_path):
    """Within the same week the dedup_key is identical, so a persistent break logs one signal."""
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: _FIXED_ONCE_KEY)
    keys: list[str | None] = []
    monkeypatch.setattr(vms, "post_alert",
                        lambda stream, body, *, product="platform", dedup_key=None, **kw:
                        keys.append(dedup_key) or "duplicate")

    for sha in ("aaa111", "bbb222"):
        vms.signal_failure(
            sha,
            [vms.RunResult(exit_code=1, stage="serial")],
            tmp_path,
        )

    assert len(keys) == 2
    assert keys[0] == keys[1] == _FIXED_ONCE_KEY


def test_rebreak_after_fix_logs_new_signal(monkeypatch, tmp_path):
    """A break in a later week gets a new dedup_key, so it logs a fresh signal.

    The store's dedup_key is unique for all time, so a bare key would answer a
    later break `duplicate` forever. The weekly bucket in _weekly_once_key
    prevents this: a break in a new week generates a new key.
    """
    issued_keys = iter([
        "vivy-main-suite-broken-2026-W30",
        "vivy-main-suite-broken-2026-W31",
    ])
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: next(issued_keys))

    keys: list[str | None] = []
    monkeypatch.setattr(vms, "post_alert",
                        lambda stream, body, *, product="platform", dedup_key=None, **kw:
                        keys.append(dedup_key) or "created")

    # Week 30: initial break logged.
    vms.signal_failure(
        "sha_week30",
        [vms.RunResult(exit_code=1, stage="serial")],
        tmp_path,
    )
    # Week 31: a new break after the week-30 signal's condition cleared.
    vms.signal_failure(
        "sha_week31",
        [vms.RunResult(exit_code=1, stage="serial")],
        tmp_path,
    )

    assert keys[0] == "vivy-main-suite-broken-2026-W30"
    assert keys[1] == "vivy-main-suite-broken-2026-W31"
    assert keys[0] != keys[1]


# ---------------------------------------------------------------------------
# run() — orchestration
# ---------------------------------------------------------------------------


def _patch_run(
    monkeypatch,
    tmp_path: Path,
    *,
    sha: str = "cafe0000000000000000000000000000",
    serial_exit: int = 0,
    shard_exits: tuple[int, int, int] = (0, 0, 0),
    alembic_ok: bool = True,
    venv_exists: bool = True,
    checkout_failure: bool = False,
    reset_failure: bool = False,
) -> tuple[dict, list, Path]:
    """Wire up run()'s collaborators as fakes.

    Returns (posted, alert_calls, fake_venv). `posted` captures the failure
    signals (dedup_key prefixed `vivy-main-suite-broken-`); `alert_calls` captures
    the harness alerts on exit-2 paths.
    """
    monkeypatch.setattr(vms, "check_live_runs", lambda: [])
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: _FIXED_ONCE_KEY)

    if checkout_failure:
        def _boom(*a, **kw):
            raise RuntimeError("git clone failed: permission denied")
        monkeypatch.setattr(vms, "ensure_checkout", _boom)
    else:
        monkeypatch.setattr(vms, "ensure_checkout", lambda *a, **kw: None)

    if reset_failure:
        def _reset_boom(*a, **kw):
            raise subprocess.CalledProcessError(1, "git reset")
        monkeypatch.setattr(vms, "reset_to_origin_main", _reset_boom)
    else:
        monkeypatch.setattr(vms, "reset_to_origin_main", lambda *a: sha)

    def fake_alembic(venv, checkout_dir, db_url):
        return _Proc(returncode=0 if alembic_ok else 1,
                     stderr="alembic boom" if not alembic_ok else "")
    monkeypatch.setattr(vms, "run_alembic", fake_alembic)

    def fake_serial(venv, checkout_dir, db_url, log_dir, datestamp):
        xml = log_dir / f"serial-{datestamp}.xml"
        log_dir.mkdir(parents=True, exist_ok=True)
        fs = [("tests.test_serial", "test_fail")] if serial_exit != 0 else []
        _junitxml(xml, failures=fs)
        return vms.RunResult(
            exit_code=serial_exit,
            failed_tests=vms._extract_failed_tests(xml),
            log_path=log_dir / f"serial-{datestamp}.log",
            stage="serial",
        )
    monkeypatch.setattr(vms, "run_serial_gate", fake_serial)

    shard_exit_seq = list(shard_exits)

    def fake_shard(venv, checkout_dir, db_url, log_dir, datestamp, group):
        exit_code = shard_exit_seq[group - 1]
        xml = log_dir / f"shard-{group}-{datestamp}.xml"
        log_dir.mkdir(parents=True, exist_ok=True)
        fs = [("tests.test_shard", f"test_fail_{group}")] if exit_code != 0 else []
        _junitxml(xml, failures=fs)
        return vms.RunResult(
            exit_code=exit_code,
            failed_tests=vms._extract_failed_tests(xml),
            log_path=log_dir / f"shard-{group}-{datestamp}.log",
            stage=f"shard-{group}",
        )
    monkeypatch.setattr(vms, "run_shard", fake_shard)

    posted: list[dict] = []
    alert_calls: list[dict] = []

    def fake_post_alert(stream, body, *, product="platform",
                        dedup_key=None, **kw):
        call = {"stream": stream, "body": body, "product": product,
                "dedup_key": dedup_key}
        (posted if dedup_key.startswith("vivy-main-suite-broken-") else alert_calls).append(call)
        return "created"
    monkeypatch.setattr(vms, "post_alert", fake_post_alert)

    fake_venv = tmp_path / "venv"
    if venv_exists:
        fake_venv.mkdir()
    return posted, alert_calls, fake_venv


def test_run_passing_suite_returns_0_and_logs_nothing(monkeypatch, tmp_path):
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 0
    assert not posted
    assert not alerts


def test_run_failing_serial_returns_1_and_logs_signal(monkeypatch, tmp_path):
    sha = "deadbeef12345678deadbeef1234567890abcdef"
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path,
                                           sha=sha, serial_exit=1)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 1
    assert posted[0]["product"] == "vivy"
    assert sha in posted[0]["body"]
    assert posted[0]["dedup_key"] == _FIXED_ONCE_KEY
    assert not alerts


def test_run_failing_shard_returns_1_and_logs_signal(monkeypatch, tmp_path):
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path,
                                           shard_exits=(0, 1, 0))
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 1
    assert posted
    assert not alerts


def test_run_harness_error_alembic_returns_2_no_issue_but_alerts(monkeypatch, tmp_path):
    """alembic failure is a harness error; no plane issue, but an alert is filed."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path, alembic_ok=False)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
    assert alerts
    assert any("alembic" in a["body"].lower() for a in alerts)


def test_run_missing_venv_returns_2_no_issue_but_alerts(monkeypatch, tmp_path):
    """Missing venv is a harness error; no plane issue, but an alert is filed."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path, venv_exists=False)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
    assert alerts
    assert any("venv" in a["body"].lower() for a in alerts)


def test_run_checkout_failure_returns_2_no_issue_but_alerts(monkeypatch, tmp_path):
    """A checkout creation failure is a harness error; no plane issue, but an alert is filed."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path, checkout_failure=True)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
    assert alerts
    assert any("checkout" in a["body"].lower() for a in alerts)


def test_run_reset_failure_returns_2_no_issue_but_alerts(monkeypatch, tmp_path):
    """A reset-to-origin/main failure is a harness error; no plane issue, but an alert is filed."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path, reset_failure=True)
    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
    assert alerts
    assert any("reset" in a["body"].lower() or "origin" in a["body"].lower()
               for a in alerts)


def test_run_harness_alert_redacts_credentials_in_exception_text(monkeypatch, tmp_path):
    """A credential-shaped substring in a harness exception must not survive
    into the alert body (external output on a tiered page)."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("clone failed: token=ghp_abcdef1234567890abcdef1234567890abcd")
    monkeypatch.setattr(vms, "ensure_checkout", _boom)

    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
    assert alerts
    body = alerts[0]["body"]
    assert "harness error" in body
    assert "token=[redacted]" in body
    assert "ghp_abcdef1234567890abcdef1234567890abcd" not in body


def test_run_harness_alert_uses_weekly_dedup_key(monkeypatch, tmp_path):
    """Harness alerts carry a dedup_key so a broken runner fires at most once per week."""
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path, alembic_ok=False)
    vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert alerts
    assert all(a["dedup_key"] is not None for a in alerts)
    assert all("vivy-main-suite-harness-error" in a["dedup_key"] for a in alerts)


def test_run_reset_called_before_suite(monkeypatch, tmp_path):
    """The checkout is hard-reset to origin/main before any test stage runs."""
    call_order: list[str] = []

    monkeypatch.setattr(vms, "check_live_runs", lambda: [])
    monkeypatch.setattr(vms, "ensure_checkout", lambda *a, **kw: None)
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: _FIXED_ONCE_KEY)

    def fake_reset(*a):
        call_order.append("reset")
        return "sha0" * 8

    monkeypatch.setattr(vms, "reset_to_origin_main", fake_reset)

    def fake_alembic(*a, **kw):
        call_order.append("alembic")
        return _Proc(0)

    monkeypatch.setattr(vms, "run_alembic", fake_alembic)

    def fake_serial(*a, **kw):
        call_order.append("serial")
        return vms.RunResult(exit_code=0, stage="serial")

    monkeypatch.setattr(vms, "run_serial_gate", fake_serial)

    def fake_shard(venv, checkout_dir, db_url, log_dir, datestamp, group):
        call_order.append(f"shard-{group}")
        return vms.RunResult(exit_code=0, stage=f"shard-{group}")

    monkeypatch.setattr(vms, "run_shard", fake_shard)
    monkeypatch.setattr(vms, "post_alert", lambda *a, **kw: None)

    fake_venv = tmp_path / "venv"
    fake_venv.mkdir()
    vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )

    assert call_order[0] == "reset"
    assert "serial" in call_order
    assert call_order.index("reset") < call_order.index("serial")
    assert all(f"shard-{g}" in call_order for g in (1, 2, 3))


def test_run_all_shards_run_even_on_failure(monkeypatch, tmp_path):
    """All three shards run even when shard 1 fails (fail-fast: false)."""
    ran_groups: list[int] = []

    monkeypatch.setattr(vms, "check_live_runs", lambda: [])
    monkeypatch.setattr(vms, "ensure_checkout", lambda *a, **kw: None)
    monkeypatch.setattr(vms, "reset_to_origin_main", lambda *a: "sha0")
    monkeypatch.setattr(vms, "run_alembic", lambda *a, **kw: _Proc(0))
    monkeypatch.setattr(vms, "_weekly_once_key", lambda: _FIXED_ONCE_KEY)
    monkeypatch.setattr(
        vms, "run_serial_gate",
        lambda *a, **kw: vms.RunResult(exit_code=0, stage="serial"),
    )

    def fake_shard(venv, checkout_dir, db_url, log_dir, datestamp, group):
        ran_groups.append(group)
        return vms.RunResult(exit_code=1 if group == 1 else 0,
                             stage=f"shard-{group}")

    monkeypatch.setattr(vms, "run_shard", fake_shard)
    monkeypatch.setattr(vms, "post_alert", lambda *a, **kw: None)

    fake_venv = tmp_path / "venv"
    fake_venv.mkdir()
    vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )

    assert ran_groups == [1, 2, 3]


def test_run_signal_carries_sha_in_body(monkeypatch, tmp_path):
    """The failure signal body includes the exact origin/main SHA."""
    sha = "abc123def456abc123def456abc123def456abcd"
    posted, alerts, fake_venv = _patch_run(monkeypatch, tmp_path,
                                           sha=sha, serial_exit=1)
    vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert sha in posted[0]["body"]
    assert not alerts


def test_run_exit_2_when_harness_alert_raises(monkeypatch, tmp_path):
    """exit-2 returns 2 even when post_alert raises; the alert is best-effort."""
    posted, _alerts, fake_venv = _patch_run(monkeypatch, tmp_path, alembic_ok=False)

    def raising_alert(stream, body, **kw):
        raise OSError("plane unreachable")

    monkeypatch.setattr(vms, "post_alert", raising_alert)

    rc = vms.run(
        checkout_dir=tmp_path / "checkout",
        venv=fake_venv,
        log_dir=tmp_path / "logs",
    )
    assert rc == 2
    assert not posted
