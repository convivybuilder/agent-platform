"""Test double for notify, shadowing the real one during the bash suite.

notify is the "a push" leg of the side-effect list the guard exists to cover —
it POSTs to ntfy.sh / pushover for genuinely-urgent alerts. The real module has
an `AGENT_PLATFORM_PUSH_DRY_RUN` escape for tests, but that is the discipline
pattern this directory exists to replace: it holds only while the author of the
next write site remembers to set it. This double makes the shadow structural,
the same way the other mocks in this directory do.

`push` records through `_mock_base.record`; the module constants are defined
literally so a reference to one does not trip the catch-all; anything else is
captured by `__getattr__`.
"""
from __future__ import annotations

import _mock_base

KEYCHAIN_SERVICE = "AGENT_PLATFORM_PUSH_CRED"
ENV_DRY_RUN = "AGENT_PLATFORM_PUSH_DRY_RUN"
SOURCE = "agent-platform"
TIMEOUT = 5


def push(*args, **kwargs) -> bool:
    _mock_base.record("notify", "push", args, kwargs)
    return True


def __getattr__(name: str):
    return _mock_base.catchall("notify", name)
