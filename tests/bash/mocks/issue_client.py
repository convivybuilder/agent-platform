"""Test double for issue_client, shadowing the real one during the bash suite.

The same failure mode as signal_client — an inlined issue write in a
bash-exercised script would reach the LIVE operating plane unless the suite
stubbed it by name. This double is the structural answer: a test that puts
this directory first on PYTHONPATH (see signal_client.py's docstring for who
does that today) gets this instead of the real client for any inline python
block that does `import issue_client`, whether or not the author knew the
block existed.

Every plane-touching call records through `_mock_base.record`: to
`OVERNIGHT_MOCK_LOG` when the test acknowledged the write, to
`OVERNIGHT_OPS_GUARD` when it is unanticipated (run.sh then fails the test). A
new issue-client function no one has seen yet falls through `__getattr__` and
is captured rather than reaching the network. Returns are stand-ins (`{}`,
`[]`); a passing suite never sees them, and a caller that branches on one has
already produced a write the guard exists to catch.
"""
from __future__ import annotations

import _mock_base

ISSUE_COMMENT_KINDS = ("note", "diagnosis", "question", "decline")


class IssueQueueUnavailable(RuntimeError):
    """Stand-in for the real exception, so an `except` clause resolves."""


def session_owner() -> str | None:
    return None


def list_open_issues(*args, **kwargs) -> list:
    _mock_base.record("issue_client", "list_open_issues", args, kwargs)
    return []


def list_issues_by_state(*args, **kwargs) -> list:
    _mock_base.record("issue_client", "list_issues_by_state", args, kwargs)
    return []


def list_claimable_issues(*args, **kwargs) -> list:
    _mock_base.record("issue_client", "list_claimable_issues", args, kwargs)
    return []


def claim_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "claim_issue", args, kwargs)
    return {}


def release_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "release_issue", args, kwargs)
    return {}


def comment_on_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "comment_on_issue", args, kwargs)
    return {}


def link_pr_to_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "link_pr_to_issue", args, kwargs)
    return {}


def escalate_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "escalate_issue", args, kwargs)
    return {}


def resolve_issue(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "resolve_issue", args, kwargs)
    return {}


def list_open_bugs(*args, **kwargs) -> list:
    _mock_base.record("issue_client", "list_open_bugs", args, kwargs)
    return []


def claim_bug(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "claim_bug", args, kwargs)
    return {}


def link_pr(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "link_pr", args, kwargs)
    return {}


def escalate_bug(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "escalate_bug", args, kwargs)
    return {}


def resolve_bug(*args, **kwargs) -> dict:
    _mock_base.record("issue_client", "resolve_bug", args, kwargs)
    return {}


def __getattr__(name: str):
    return _mock_base.catchall("issue_client", name)
