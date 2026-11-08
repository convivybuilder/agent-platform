"""`agentctl crew roster <crew> <agent> [<agent> ...]` — lib/agent-platform/
crew_roster.py's module docstring has the why this is a standalone command
rather than a step of `crew create` or `crew register`.

NO TEST REACHES THE REAL LIBRARY. Each one hands `push_roster_name` (or,
through `cmd_crew_roster`, `_doctor_auth`) a fake opener — the same seam
`test_crew_doctor.py`'s `test_served_profile_uses_the_given_opener_not_
urlopen` uses — rather than a real local HTTP server, which this sandbox's
own port-bind restriction (`socket.bind`: `PermissionError: [Errno 1]
Operation not permitted`) refuses even for the pre-existing `_Listener`/
`_StubLibrary` tests in `test_crew_doctor.py`/`test_crew_create.py`.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib" / "agent-platform"))
import crew_roster as ROSTER  # noqa: E402

CREW = "made-up-test-crew"


class _FakeOpener:
    """One `PUT /roster/{name}` per name in `replies` (or `errors`), in the
    order `push_roster_name` is called. `replies[name]` is a `(created_at,
    updated_at)` pair — the upsert shape `roster_add` itself returns, told
    apart by whether the two match; `errors[name]` is `(status, error_text)`
    for an HTTPError."""

    def __init__(self, replies: dict | None = None, errors: dict | None = None):
        self.replies = replies or {}
        self.errors = errors or {}
        self.requests: list[urllib.request.Request] = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        name = urllib.parse.unquote(req.full_url.rsplit("/", 1)[-1])
        if name in self.errors:
            status, error_text = self.errors[name]
            body = json.dumps({"error": error_text}).encode("utf-8")
            raise urllib.error.HTTPError(req.full_url, status, error_text, {},
                                        io.BytesIO(body))
        created_at, updated_at = self.replies[name]
        row = {"name": name, "created_at": created_at, "updated_at": updated_at,
               "created_by": "test", "updated_by": "test"}
        return io.BytesIO(json.dumps(row).encode("utf-8"))


# --- push_roster_name --------------------------------------------------------

def test_a_first_push_is_added():
    opener = _FakeOpener(replies={"made-up-coder": ("2026-01-01T00:00:00Z",
                                                    "2026-01-01T00:00:00Z")})
    status, detail = ROSTER.push_roster_name(
        "made-up-coder", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert (status, detail) == (ROSTER.ADDED, "")
    assert opener.requests[0].get_method() == "PUT"


def test_a_repeated_push_of_an_existing_name_is_a_no_op_success():
    """The route upserts: a name already on the roster still answers 200,
    with `updated_at` moved past `created_at`."""
    opener = _FakeOpener(replies={"made-up-coder": ("2026-01-01T00:00:00Z",
                                                    "2026-01-02T00:00:00Z")})
    status, detail = ROSTER.push_roster_name(
        "made-up-coder", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert (status, detail) == (ROSTER.ALREADY, "")


def test_a_refused_name_reports_the_librarys_text():
    opener = _FakeOpener(errors={"bad name": (
        400, "name must be ASCII letters, digits, '.', '_' or '-'")})
    status, detail = ROSTER.push_roster_name(
        "bad name", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert status == ROSTER.REFUSED
    assert "ASCII letters" in detail


def test_the_missing_grant_is_told_apart_from_an_ordinary_refusal():
    """The Library's 403 names the capability itself — `refusals.forbidden`'s
    exact wording — and that is what tells this apart from a 403 for some
    other reason."""
    opener = _FakeOpener(errors={"made-up-coder": (
        403, "this identity lacks 'roster.write'")})
    status, detail = ROSTER.push_roster_name(
        "made-up-coder", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert status == ROSTER.GRANT_MISSING
    assert "roster.write" in detail


def test_a_403_for_a_different_reason_is_not_mistaken_for_the_missing_grant():
    opener = _FakeOpener(errors={"made-up-coder": (
        403, "this identity lacks 'some.other.capability'")})
    status, detail = ROSTER.push_roster_name(
        "made-up-coder", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert status == ROSTER.REFUSED


def test_push_roster_name_uses_the_given_opener_not_urlopen(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not call the process-wide urlopen")
    monkeypatch.setattr(ROSTER.urllib.request, "urlopen", boom)
    opener = _FakeOpener(replies={"made-up-coder": ("2026-01-01T00:00:00Z",
                                                    "2026-01-01T00:00:00Z")})
    status, _ = ROSTER.push_roster_name(
        "made-up-coder", base_url="http://127.0.0.1:1", headers={}, opener=opener)
    assert status == ROSTER.ADDED


# --- cmd_crew_roster (CLI level) --------------------------------------------

def _auth(agentctl, headers=None):
    return agentctl._DoctorAuth(token="t", base_url="http://127.0.0.1:1",
                                headers=headers or {}, pinned=False)


def test_cmd_crew_roster_pushes_every_name_and_exits_zero(agentctl, monkeypatch, capsys):
    opener = _FakeOpener(replies={
        "made-up-coder": ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        "made-up-reviewer": ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    })
    monkeypatch.setattr(agentctl, "_doctor_auth", lambda: _auth(agentctl))
    # `cmd_crew_roster` imports `crew_roster` lazily, so this is the same
    # module object `ROSTER` above is bound to (cached in `sys.modules`) —
    # patching ITS `urllib.request.urlopen` (the global every un-opened
    # `push_roster_name` call falls back to) is what reaches it with no
    # real socket.
    monkeypatch.setattr(ROSTER.urllib.request, "urlopen",
                        lambda req, timeout=None: opener.open(req, timeout))
    rc = agentctl.cmd_crew_roster(
        argparse.Namespace(crew=CREW, agents=["made-up-coder", "made-up-reviewer"]))
    assert rc == 0
    assert [r.full_url.rsplit("/", 1)[-1] for r in opener.requests] == [
        "made-up-coder", "made-up-reviewer"]
    out = capsys.readouterr().out
    assert "made-up-coder — added" in out
    assert "made-up-reviewer — added" in out


def test_cmd_crew_roster_stops_at_the_first_refusal(agentctl, monkeypatch, capsys):
    opener = _FakeOpener(errors={"made-up-coder": (
        400, "name must be ASCII letters, digits, '.', '_' or '-'")})
    monkeypatch.setattr(agentctl, "_doctor_auth", lambda: _auth(agentctl))
    monkeypatch.setattr(ROSTER.urllib.request, "urlopen",
                        lambda req, timeout=None: opener.open(req, timeout))
    rc = agentctl.cmd_crew_roster(
        argparse.Namespace(crew=CREW, agents=["made-up-coder", "made-up-reviewer"]))
    assert rc == 1
    # the second name is never attempted once the first is refused
    assert [r.full_url.rsplit("/", 1)[-1] for r in opener.requests] == ["made-up-coder"]
    err = capsys.readouterr().err
    assert "refused" in err and "made-up-coder" in err


def test_cmd_crew_roster_skips_cleanly_on_a_missing_grant(agentctl, monkeypatch, capsys):
    opener = _FakeOpener(errors={"made-up-coder": (
        403, "this identity lacks 'roster.write'")})
    monkeypatch.setattr(agentctl, "_doctor_auth", lambda: _auth(agentctl))
    monkeypatch.setattr(ROSTER.urllib.request, "urlopen",
                        lambda req, timeout=None: opener.open(req, timeout))
    rc = agentctl.cmd_crew_roster(
        argparse.Namespace(crew=CREW, agents=["made-up-coder", "made-up-reviewer"]))
    assert rc == 2
    # the capability check runs before the Library looks at the name, so every
    # other name would fail identically — this command does not try them.
    assert [r.full_url.rsplit("/", 1)[-1] for r in opener.requests] == ["made-up-coder"]
    err = capsys.readouterr().err
    assert "roster.write" in err and "skipped" in err
