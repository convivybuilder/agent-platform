"""Shared plumbing for the bash suite's ops-client doubles.

WHY THIS EXISTS
===============
Each module in this directory (signal_client.py, issue_client.py,
decision_client.py, notify.py) shadows the real lib module of the same name for
the bash suite, so a write the suite never anticipated is captured instead of
reaching the live operating plane. On 2026-07-29 that capture was the whole
protection, and it was not enough: an inlined alert emit was captured silently,
the suite passed, and three real OVERNIGHT-SUPERVISOR-STARVED alerts had already
reached /engineering/alerts by the time anyone looked. A swallowed write and a
sent write are indistinguishable from inside a passing suite, so capture must
fail the test that caused it.

The python suite's `_no_live_ops_writes` autouse fixture (tests/conftest.py)
fails any test that reaches the ops plane. This is the bash analogue, split into
the same shape the fixture uses: a double on the write path, and a failure
channel that run.sh reads.

Two environment variables decide where a captured call goes:

- `OVERNIGHT_MOCK_LOG` set → the write is ACKNOWLEDGED. The test set this to
  assert on the call itself (the guard-probe in test_ops_write_guard.sh), so
  the record goes to that file and nowhere else.
- `OVERNIGHT_OPS_GUARD` set, no OVERNIGHT_MOCK_LOG → the write is UNANTICIPATED.
  `run.sh` exports this to a fresh file before each test and fails the test if
  the file is non-empty afterwards.
- Neither set → a test run directly, not via run.sh; the call is announced on
  stderr and swallowed.

THE BOUNDARY, STATED HONESTLY. These doubles cover the ops clients, which is
where the failure that spawned them wrote. A block that imports urllib.request
(or requests) directly and POSTs to the ops host bypasses them — that residue is
covered by the python suite's urlopen interception, not here.
"""
from __future__ import annotations

import json
import os
import sys


def record(module: str, fn: str, args: tuple, kwargs: dict) -> None:
    """Capture one attempted write to the plane, per the variable rules above.

    The two capture paths fail differently on an unwritable destination, and
    the difference is load-bearing:
    - OVERNIGHT_MOCK_LOG unwritable → swallow, fall back to stderr. The test
      that set the log asserts on its contents, so an empty log fails the test
      itself; raising would crash the exercised code for a failure the test
      already sees.
    - OVERNIGHT_OPS_GUARD unwritable → re-raise. run.sh detects a write only by
      a non-empty guard file, so a swallowed guard write would let the suite
      pass with the write undetected — the silent capture this mechanism exists
      to close. A lost guard write is a broken harness, not a write to smooth
      over.
    """
    line = json.dumps({"module": module, "fn": fn,
                       "args": [str(a) for a in args],
                       "kwargs": {k: str(v) for k, v in kwargs.items()}})
    ack = os.environ.get("OVERNIGHT_MOCK_LOG")
    if ack:
        try:
            with open(ack, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        except OSError:
            pass
        print(f"[bash-suite mock] {module}.{fn}() captured, but OVERNIGHT_MOCK_LOG "
              f"is unwritable — recording to stderr only", file=sys.stderr)
        return
    guard = os.environ.get("OVERNIGHT_OPS_GUARD")
    if guard:
        try:
            with open(guard, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            print(f"[bash-suite mock] {module}.{fn}() called from a test — "
                  f"OVERNIGHT_OPS_GUARD is unwritable, so this write cannot be "
                  f"recorded and the guard will NOT catch it",
                  file=sys.stderr)
            raise
        print(f"[bash-suite mock] {module}.{fn}() called from a test — captured "
              f"to OVERNIGHT_OPS_GUARD, NOT sent; run.sh will fail this test",
              file=sys.stderr)
        return
    print(f"[bash-suite mock] {module}.{fn}() called — captured, NOT sent.",
          file=sys.stderr)


def catchall(module: str, name: str):
    """A module __getattr__ helper: capture a call this double has no stub for.

    The point is to catch the write nobody anticipated, including from a client
    function that did not exist when the double was written. An unknown attribute
    must not fall through to a real client — and must not raise either, which
    would turn a silent production write into a noisy test failure for the wrong
    reason. It does announce itself on stderr in addition to recording, so a
    capture no test is watching is still visible in the suite output.
    """
    def _f(*args, **kwargs):
        print(f"[bash-suite mock] {module}.{name}() called — this double has no "
              f"explicit stub for it; the call was captured, not sent.",
              file=sys.stderr)
        record(module, name, args, kwargs)
    return _f
