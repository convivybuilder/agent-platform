"""Test double for signal_client, shadowing the real one during the bash suite.

WHY THIS EXISTS
===============
On 2026-07-29 the ARCH-423 round-1 fix inlined an alert emit inside
`wait_for_supervisor_slot`. The bash suite stubbed the supervisor-slot query but
not the emit, so every run of `tests/bash/run.sh` posted a REAL
`OVERNIGHT-SUPERVISOR-STARVED` alert to the live operating plane. Three runs,
three junk alerts on /engineering/alerts, each reading "no supervisor slot freed
within 0h" — the fingerprint, since the test sets the timeout to 0.

The instance was fixed by extracting `_emit_starved_alert` so the suite could
stub it. That protection is DISCIPLINE: it holds only while every future author
remembers to put each side effect behind a stubbable function. The person who
wrote that rule down in a commit message shipped this very defect an hour later,
which is about as clear a demonstration as exists that discipline is not a
control.

This module is the structural version, the bash analogue of the python suite's
`_no_live_ops_writes`. A test wires the shadow itself by putting this directory
first on PYTHONPATH — `tests/bash/lib.sh`'s `_BASH_TEST_MOCKS` names the path,
and `tests/bash/test_ops_write_guard.sh` is the test that does the wiring today
— so any inline python block run under that PYTHONPATH that does `import
signal_client` gets this instead of the real client, whether or not the author
knew the block existed.

`_mock_base.record` decides where a call goes: to `OVERNIGHT_MOCK_LOG` when the
test acknowledged the write and asserts on it, to `OVERNIGHT_OPS_GUARD` when it
is unanticipated (run.sh then fails the test), and to stderr alone when neither
is set. The pure helpers `token`, `product_for_channel` and `extract_text` never
touch the plane and are stubbed without recording.
"""
from __future__ import annotations

import _mock_base

BASE_URL = "http://mock.invalid"
TOKEN_ENV = "AGENT_SIGNAL_TOKEN"


def token() -> str | None:
    return None


def product_for_channel(label: str) -> str:
    return "platform"


def extract_text(payload: object) -> str:
    return ""


def post_signal(*args, **kwargs) -> str:
    _mock_base.record("signal_client", "post_signal", args, kwargs)
    return "mock"


def post_structured(*args, **kwargs) -> str:
    _mock_base.record("signal_client", "post_structured", args, kwargs)
    return "mock"


def post_report(*args, **kwargs) -> str:
    _mock_base.record("signal_client", "post_report", args, kwargs)
    return "mock"


def post_alert(*args, **kwargs) -> str:
    _mock_base.record("signal_client", "post_alert", args, kwargs)
    return "mock"


def __getattr__(name: str):
    return _mock_base.catchall("signal_client", name)
