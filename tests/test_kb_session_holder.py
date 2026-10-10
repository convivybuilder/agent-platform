"""A kb lease names the session that took it, so one co session's checkout
refuses another's write (operator, 2026-09-11)."""
from argparse import Namespace

import pytest

import kb_writes

DOC = "projects/shared/reference/seeded.md"
SESSION_A = "3f6c2b1e-8a4d-4f0a-9c1e-2b7d5e8a1f00"
SESSION_B = "9d2e4c7a-1b3f-4e8d-a6c5-0f1e2d3c4b5a"


@pytest.fixture
def store(agentctl, tmp_path, monkeypatch):
    monkeypatch.setattr(kb_writes, "DB", tmp_path / "kb-writes.db")
    monkeypatch.setattr(agentctl, "_kb_writes", lambda: kb_writes)
    monkeypatch.setattr(agentctl, "_kb_resolve_ref", lambda doc: (doc, None))
    monkeypatch.setattr(agentctl, "_kb_blob_at_origin", lambda ref: ("body\n", None))
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return kb_writes


def _checkout(agentctl, monkeypatch, session_id):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    return agentctl.cmd_kb_checkout(Namespace(doc=DOC, holder=None, note=None))


def test_a_second_session_is_refused_the_first_sessions_checkout(
        agentctl, store, monkeypatch, capsys):
    assert _checkout(agentctl, monkeypatch, SESSION_A) == 0
    capsys.readouterr()
    assert _checkout(agentctl, monkeypatch, SESSION_B) == 3
    err = capsys.readouterr().err
    assert f"leased by session:{SESSION_A}" in err


def test_the_same_session_refreshes_its_own_checkout(agentctl, store, monkeypatch):
    assert _checkout(agentctl, monkeypatch, SESSION_A) == 0
    assert _checkout(agentctl, monkeypatch, SESSION_A) == 0


def test_the_holder_names_the_session(agentctl, monkeypatch):
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_A)
    holder = agentctl._kb_holder()
    assert holder == f"session:{SESSION_A}"
    assert kb_writes.valid_holder(holder), "a session holder must pass the store's own check"


def test_a_dispatched_run_is_still_named_by_its_run_id(agentctl, monkeypatch):
    """A dispatched worker carries both; its run-id is the useful name in a conflict."""
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-7f3a91")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION_A)
    assert agentctl._kb_holder() == "run-7f3a91"


def test_outside_any_session_the_user_fallback_remains(agentctl, monkeypatch):
    import getpass
    for var in ("AGENTCTL_RUN_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert agentctl._kb_holder() == f"session:{getpass.getuser()}"


@pytest.mark.parametrize("verb", ["checkout", "release"])
def test_a_malformed_holder_is_refused_before_the_store_sees_it(
        agentctl, store, monkeypatch, capsys, verb):
    """kb apply and kb propose already refused one; checkout and release now match
    (security review, PR 532). A space is outside HOLDER_RE."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not a valid id")
    cmd = agentctl.cmd_kb_checkout if verb == "checkout" else agentctl.cmd_kb_release
    rc = cmd(Namespace(doc=DOC, holder=None, note=None))
    assert rc == 1
    assert "not a valid identity" in capsys.readouterr().err
    assert store.lease_for(DOC) is None, "nothing may reach the lease store"
