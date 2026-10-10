"""`issues-waiting` — the "what needs a human here" line a co session opens with.

operator, 2026-07-26: "these are the issues that will get lost if we don't have a
system." The team site's Home strip is THEIR surface; `co` had none — it read the
auth token, the model lane and the knowledge repo at startup and never touched
the ops plane, so no interactive session ever learned what was waiting for it.

Two properties carry the whole thing. It must never take a session down, since
it runs on the startup path of every launch. And it must not claim a filter was
applied when it was not: an unknown query parameter is IGNORED by the store, so
against a server predating the `needs` column this asks for operator-only issues
and is handed every open issue. Observed live before the fix — it reported "3
issues need you on vivy" when the true answer was zero.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".local" / "lib" / "agent-platform"))


@pytest.fixture
def iw(issues_waiting):
    return issues_waiting


def _issue(iid, needs="operator", product="vivy"):
    return {"id": f"issue_{iid}", "product": product, "needs": needs,
            "title": f"thing {iid}", "url": f"/engineering/issues/issue_{iid}"}


# --- the stale-server guard --------------------------------------------------


class _Resp:
    """Minimal stand-in for urlopen's context manager."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(iw, monkeypatch, items):
    """Point _fetch at a store returning `items`, with a token present."""
    import signal_client as SC
    monkeypatch.setattr(SC, "token", lambda: "TEST-TOKEN", raising=False)
    # No BASE_URL patch: `_fetch` takes its URL from `vault_plane.url()` now, so
    # patching that constant would be a line that reads as configuration and
    # configures nothing (reviewer, convivy/agent-platform#400).
    monkeypatch.setattr(iw.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"items": items}))


def test_a_stale_server_that_ignored_the_filter_yields_nothing(iw, monkeypatch):
    """THE regression this guard exists for. An unknown query parameter is IGNORED
    by the store, so against a server predating the `needs` column this asks for
    operator-only issues and is handed every open issue. Observed live on
    2026-07-26: it reported "3 issues need you on vivy" when the answer was zero —
    confident, wrong, and exactly the failure that teaches a reader to ignore the
    line. The absent field is the signal."""
    _serve(iw, monkeypatch, [{"id": "issue_A", "title": "no needs key"},
                             {"id": "issue_B", "title": "also pre-column"}])
    assert iw._fetch("vivy") == []


def test_only_operator_items_survive(iw, monkeypatch):
    """The client agrees with the server rather than assuming it was obeyed."""
    _serve(iw, monkeypatch, [_issue(1, "operator"), _issue(2, "agent"),
                             _issue(3, "either"), {"id": "issue_4"}])
    assert [i["id"] for i in iw._fetch("vivy")] == ["issue_1"]


def test_no_token_yields_nothing_rather_than_raising(iw, monkeypatch):
    import signal_client as SC
    monkeypatch.setattr(SC, "token", lambda: None, raising=False)
    assert iw._fetch("vivy") == []


# --- output ------------------------------------------------------------------


def test_nothing_waiting_prints_nothing(iw, monkeypatch, capsys):
    monkeypatch.setattr(iw, "_fetch", lambda p: [])
    assert iw.main(["vivy"]) == 0
    assert capsys.readouterr().err == ""


def test_a_count_line_names_the_product_and_links(iw, monkeypatch, capsys):
    monkeypatch.setattr(iw, "_fetch", lambda p: [_issue(1), _issue(2)])
    assert iw.main(["vivy"]) == 0
    err = capsys.readouterr().err
    assert "2 issues need you on vivy" in err
    # A full URL, per ARCH-043 — the operator cmd-clicks these.
    assert "https://team.convivy.com/engineering/issues?needs=operator" in err


def test_one_issue_reads_as_singular(iw, monkeypatch, capsys):
    monkeypatch.setattr(iw, "_fetch", lambda p: [_issue(1)])
    iw.main(["vivy"])
    assert "1 issue needs you" in capsys.readouterr().err


def test_the_line_goes_to_stderr(iw, monkeypatch, capsys):
    """Like every other co startup line, so it never lands in something capturing
    stdout."""
    monkeypatch.setattr(iw, "_fetch", lambda p: [_issue(1)])
    iw.main(["vivy"])
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err


def test_list_mode_prints_each_issue(iw, monkeypatch, capsys):
    monkeypatch.setattr(iw, "_fetch", lambda p: [_issue(1), _issue(2)])
    iw.main(["vivy", "--list"])
    err = capsys.readouterr().err
    assert "issue_1" in err and "issue_2" in err


# --- terminal safety ---------------------------------------------------------


def test_a_title_cannot_inject_terminal_escapes(iw, monkeypatch, capsys):
    """A title is free text an agent writes, and a terminal EXECUTES escape
    sequences rather than displaying them. The concrete attack: a title that
    clears the screen and plants a line the operator reads as co's own output — which lands
    during startup, mixed in with co's real status lines, so forged output
    inherits co's credibility (security review, #255)."""
    forged = "\x1b[H\x1b[2Jco: session healthy, no issues waiting\x1b[0m"
    monkeypatch.setattr(iw, "_fetch",
                        lambda p: [{"id": "issue_1", "title": forged, "url": "/x"}])
    iw.main(["vivy", "--list"])
    err = capsys.readouterr().err
    assert "\x1b" not in err
    # The text survives, stripped of its teeth — the reader still sees what was
    # filed, which is the point of showing it at all.
    assert "session healthy" in err


def test_control_bytes_are_stripped_from_every_store_field(iw, monkeypatch, capsys):
    monkeypatch.setattr(iw, "_fetch", lambda p: [
        {"id": "iss\x07ue_1", "title": "a\x00b", "url": "/x\x1b[31m"}])
    iw.main(["vivy", "--list"])
    err = capsys.readouterr().err
    assert not any(ch in err for ch in ("\x07", "\x00", "\x1b"))


def test_a_newline_in_a_title_cannot_forge_a_second_line(iw, monkeypatch, capsys):
    """One line per field is the layout that makes the list readable; a title
    carrying a newline would forge an extra row."""
    monkeypatch.setattr(iw, "_fetch", lambda p: [
        {"id": "issue_1", "title": "real\n  issue_9  fake entry", "url": "/x"}])
    iw.main(["vivy", "--list"])
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 2          # the id/title line and the url line, no more


def test_the_count_line_survives_a_hostile_product_name(iw, monkeypatch, capsys):
    """`product` is a local directory basename — trusted in the sense that matters,
    but a directory name can carry control bytes and a rule with an exception is
    the one that gets forgotten."""
    monkeypatch.setattr(iw, "_fetch", lambda p: [_issue(1)])
    iw.main(["vi\x1b[2Jvy"])
    assert "\x1b" not in capsys.readouterr().err


# --- it must never take a session down ---------------------------------------


@pytest.mark.parametrize("boom", [
    OSError("network down"), ValueError("bad json"), RuntimeError("no token"),
    KeyError("schema drift"),
])
def test_any_failure_is_silent_and_exits_zero(iw, monkeypatch, capsys, boom):
    """It runs on the startup path of every co launch. A store that is down, slow
    or schema-drifted costs a line of output, never a session."""
    def raise_it(product):
        raise boom

    monkeypatch.setattr(iw, "_fetch", raise_it)
    assert iw.main(["vivy"]) == 0
    assert capsys.readouterr().err == ""
