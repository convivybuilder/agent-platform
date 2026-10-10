"""Test double for decision_client, shadowing the real one during the bash suite.

Same contract as the other mocks in this directory: a decision write inlined
into a bash-exercised script would otherwise reach the live operating plane.
Calls record through `_mock_base.record` and land in `OVERNIGHT_MOCK_LOG`
(acknowledged) or `OVERNIGHT_OPS_GUARD` (unanticipated — run.sh fails the test);
anything this double has no stub for is captured by `__getattr__` rather than
falling through to the network.
"""
from __future__ import annotations

import _mock_base


def post_decision(*args, **kwargs) -> dict:
    _mock_base.record("decision_client", "post_decision", args, kwargs)
    return {}


def get_decision(*args, **kwargs) -> dict:
    _mock_base.record("decision_client", "get_decision", args, kwargs)
    return {}


def list_decisions(*args, **kwargs) -> dict:
    _mock_base.record("decision_client", "list_decisions", args, kwargs)
    return {}


def __getattr__(name: str):
    return _mock_base.catchall("decision_client", name)
