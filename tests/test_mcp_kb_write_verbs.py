"""The mcp-kb write verbs — thin wrappers, and thin is the requirement.

Every policy decision (the write lock, the sha precondition, the provenance
table, the roster/profile PR-only rule) lives in the cage, because the cage is
what every caller goes through — this server, a shell, a launchd job. A policy
check duplicated here would be a second copy to drift, and the copy an attacker
routes around by calling agentctl directly. So these tests assert the verbs
MARSHAL correctly and decide nothing.
"""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest


@pytest.fixture
def srv(mcp_kb_server, monkeypatch):
    """The server module with its cage calls intercepted."""
    calls = []

    def fake_run(cmd, *a, **kw_):
        calls.append(cmd)
        payload = getattr(fake_run, "payload", "")
        rc = getattr(fake_run, "rc", 0)
        return subprocess.CompletedProcess(cmd, rc, payload, "" if rc == 0 else "refused")

    monkeypatch.setattr(mcp_kb_server.subprocess, "run", fake_run)
    mcp_kb_server._calls = calls
    mcp_kb_server._fake = fake_run
    return mcp_kb_server


def _fn(tool):
    """FastMCP wraps a tool; reach the underlying function."""
    return getattr(tool, "fn", tool)


# --- marshalling -------------------------------------------------------------


def test_update_passes_the_precondition_and_provenance_through(srv):
    _fn(srv.kb_update)("shared/reference/a", "sha123", "steward",
                       edits=[{"old": "a", "new": "b"}])
    cmd = srv._calls[-1]
    assert cmd[1:3] == ["kb", "apply"]
    assert cmd[cmd.index("--base-sha") + 1] == "sha123"
    assert cmd[cmd.index("--provenance") + 1] == "steward"
    assert json.loads(cmd[cmd.index("--edits") + 1]) == [{"old": "a", "new": "b"}]


def test_create_uses_the_empty_string_sha_as_its_precondition(srv):
    """A create must be refused as STALE if the doc already exists, rather than
    silently overwriting — which is the same lost-update the precondition exists
    to stop, in its most destructive form."""
    _fn(srv.kb_create)("projects/vivy/decisions/0069-x.md", "body", "steward")
    cmd = srv._calls[-1]
    assert cmd[cmd.index("--base-sha") + 1] == hashlib.sha256(b"").hexdigest()


def test_update_with_content_permits_overwrite_but_edits_does_not(srv):
    _fn(srv.kb_update)("shared/reference/a", "sha", "steward", content="whole body")
    assert "--allow-overwrite" in srv._calls[-1]
    _fn(srv.kb_update)("shared/reference/a", "sha", "steward",
                       edits=[{"old": "a", "new": "b"}])
    assert "--allow-overwrite" not in srv._calls[-1]


def test_propose_sends_no_provenance(srv):
    """A proposal is authorized by the review cycle, not by a claimed provenance.
    Passing one would imply the caller could grade its own change."""
    _fn(srv.kb_propose)("agent-platform/agents/reviewer", "Retitle the reviewer",
                        rationale="because", edits=[{"old": "a", "new": "b"}])
    cmd = srv._calls[-1]
    assert cmd[1:3] == ["kb", "propose"]
    assert "--provenance" not in cmd


def test_propose_delete_passes_every_path_and_the_flag(srv):
    """The lease and path rules are the cage's; the wrapper only has to deliver
    every path and the flag, with no content riding along."""
    _fn(srv.kb_propose)("projects/x/a.md", "Retire x", delete=True,
                        also_delete=["projects/x/profiles/daytime.yaml"])
    cmd = srv._calls[-1]
    assert cmd[1:3] == ["kb", "propose"]
    assert cmd[-3:] == ["--", "projects/x/a.md", "projects/x/profiles/daytime.yaml"]
    assert "--delete" in cmd
    assert "--edits" not in cmd and "--content" not in cmd


@pytest.mark.parametrize("call", [
    lambda s: _fn(s.kb_propose)("projects/x/a.md", "T", delete=True,
                                also_delete=["--holder", "session:victim"]),
    lambda s: _fn(s.kb_propose)("--holder=session:victim", "T", delete=True,
                                also_delete=["projects/x/a.md"]),
    lambda s: _fn(s.kb_checkout)("--holder=session:victim"),
    lambda s: _fn(s.kb_update)("--holder=session:victim", "sha", "steward", content="b"),
    lambda s: _fn(s.kb_create)("--holder=session:victim", "b", "steward"),
    lambda s: _fn(s.kb_release)("--holder=session:victim"),
])
def test_a_caller_value_never_reaches_argv_ahead_of_the_end_of_options(srv, call):
    """Security review of PR 537: an `also_delete` entry of `--holder` was
    parsed as the cage's --holder option, which this server does not expose,
    and a delete then passed its lease check under someone else's name. Every
    caller-supplied document value must come after `--`."""
    call(srv)
    cmd = srv._calls[-1]
    assert "--" in cmd
    end = cmd.index("--")
    assert not any("session:victim" in a for a in cmd[:end]), cmd


def test_propose_without_delete_sends_no_flag(srv):
    _fn(srv.kb_propose)("projects/x/a.md", "Edit x", edits=[{"old": "a", "new": "b"}])
    assert "--delete" not in srv._calls[-1]


def test_merge_names_the_pr_and_adds_dry_run_only_when_asked(srv):
    """The merge conditions are the cage's (platform/decisions/0078); the wrapper
    only names the PR and carries the flag, and never adds one it was not asked
    for, since a dropped --dry-run would turn a check into a merge."""
    _fn(srv.kb_merge)(409)
    cmd = srv._calls[-1]
    assert cmd[1:3] == ["kb", "merge"]
    assert cmd[cmd.index("--pr") + 1] == "409"
    assert "--dry-run" not in cmd
    _fn(srv.kb_merge)(409, dry_run=True)
    assert "--dry-run" in srv._calls[-1]


def test_checkout_parses_the_cage_payload(srv):
    srv._fake.payload = json.dumps({"source_ref": "projects/x/a.md", "exists": True,
                                    "content": "body", "content_sha": "abc",
                                    "lease": {"holder": "run-1"}})
    out = _fn(srv.kb_checkout)("x/a", note="editing")
    assert out["ok"] and out["content_sha"] == "abc"
    assert "--note" in srv._calls[-1]


def test_release_and_leases_reach_the_right_subcommands(srv):
    _fn(srv.kb_release)("x/a")
    assert srv._calls[-1][1:3] == ["kb", "release"]
    srv._fake.payload = "[]"
    out = _fn(srv.kb_leases)()
    assert srv._calls[-1][1:4] == ["kb", "leases", "--json"]
    assert out["leases"] == []


# --- error surfacing ---------------------------------------------------------


@pytest.mark.parametrize("rc,name", [
    (2, "refused-before-acting"), (3, "leased"), (4, "refused-policy"), (5, "stale"),
    (6, "invalid"), (7, "push-race"),
])
def test_exit_codes_surface_as_distinct_named_errors(srv, rc, name):
    """The codes ARE the contract. A caller seeing `stale` re-reads and retries;
    one seeing `refused-policy` must switch to kb_propose instead. Collapsing
    them to a single 'failed' would make those two indistinguishable."""
    srv._fake.rc = rc
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session",
                             edits=[{"old": "a", "new": "b"}])
    assert out["ok"] is False
    assert out["error"] == name
    assert out["exit_code"] == rc


def test_a_merge_that_exits_2_is_not_labelled_as_a_missing_identity(srv):
    """`kb merge` exits 2 for a PR it cannot read as well as for a missing App
    key. A name that claims the key is missing sends a caller who mistyped a PR
    number looking for a credential; the name must be true of both."""
    srv._fake.rc = 2
    out = _fn(srv.kb_merge)(99999)
    assert out["ok"] is False
    assert out["error"] == "refused-before-acting"
    assert "identity" not in out["error"]


def test_an_unreachable_cage_is_reported_not_raised(srv, mcp_kb_server, monkeypatch):
    def boom(*a, **kw_):
        raise OSError("no such binary")
    monkeypatch.setattr(mcp_kb_server.subprocess, "run", boom)
    out = _fn(mcp_kb_server.kb_release)("x/a")
    assert out["ok"] is False and out["error"] == "cage-unreachable"


def test_unparseable_checkout_output_is_not_reported_as_success(srv):
    """A malformed payload must not read as a successful checkout — a caller
    would then write against a sha it never received."""
    srv._fake.payload = "not json"
    out = _fn(srv.kb_checkout)("x/a")
    assert out["ok"] is False and out["error"] == "unparseable-cage-output"


# --- content is literal, always -----------------------------------------------


@pytest.mark.parametrize("verb", ["kb_update", "kb_create", "kb_propose"])
def test_content_that_names_a_real_file_is_still_sent_as_literal_text(srv, tmp_path, verb):
    """`--content` used to mean "text, unless it names a file, in which case read
    that". A guard at this boundary could not close it: two processes each calling
    is_file() cannot agree when a file may appear between them, so the check was a
    race rather than a control (security review, #247).

    The flag was split instead — the cage reads from disk only via
    `--content-file`, which these verbs never pass. So a document that happens to
    name a real file is simply that document.
    """
    draft = tmp_path / "draft.md"
    draft.write_text("Someone else's document.")

    fn = _fn(getattr(srv, verb))
    if verb == "kb_update":
        fn("shared/reference/a", "sha", "in-session", content=str(draft))
    elif verb == "kb_create":
        fn("projects/vivy/decisions/0069-x.md", str(draft), "in-session")
    else:
        fn("shared/reference/a", "A title", content=str(draft))

    cmd = srv._calls[-1]
    assert "--content-file" not in cmd, "the verbs must never ask the cage to read a file"
    assert cmd[cmd.index("--content") + 1] == str(draft)


# --- the thinness requirement ------------------------------------------------


def test_the_verbs_carry_no_policy_of_their_own(srv):
    """A roster path is refused by the CAGE, not here. This verb must forward it
    unchanged — if the server started refusing locally, the two would drift and
    the local copy would be the one an attacker bypasses by calling agentctl."""
    srv._fake.rc = 4
    out = _fn(srv.kb_update)("projects/agent-platform/agents/reviewer.md", "sha",
                             "in-session", edits=[{"old": "a", "new": "b"}])
    assert out["error"] == "refused-policy"
    # forwarded verbatim rather than short-circuited, after the end of options
    assert srv._calls[-1][-2:] == ["--", "projects/agent-platform/agents/reviewer.md"]


def test_propose_batch_passes_the_list_as_one_argument(srv):
    """The whole batch is one argv element after --batch, so no entry can be read
    as an option, and no single-document change rides along."""
    import json
    batch = [{"id": "projects/x/a.md", "edits": [{"old": "a", "new": "b"}]},
             {"id": "--holder=session:victim", "content": "x"}]
    _fn(srv.kb_propose)("projects/x/a.md", "Add role", batch=batch)
    cmd = srv._calls[-1]
    i = cmd.index("--batch")
    sent = json.loads(cmd[i + 1])
    assert sent[0] == batch[0]
    # A content entry may replace its document, as a single content call does.
    assert sent[1] == dict(batch[1], allow_overwrite=True)
    assert cmd[cmd.index("--"):] == ["--", "projects/x/a.md"]
    assert "--edits" not in cmd and "--content" not in cmd and "--delete" not in cmd


# --- `person` is the editor's provenance, not a model's ------------------------


def test_update_refuses_person_itself_and_names_the_editor(srv):
    r = _fn(srv.kb_update)("shared/reference/a", "sha", "person",
                           edits=[{"old": "a", "new": "b"}])
    assert r["ok"] is False and r["error"] == "refused-policy"
    assert "Orient editor" in r["stderr"]
    assert srv._calls == []   # decided here, without shelling out


def test_create_refuses_person_itself_and_names_the_editor(srv):
    r = _fn(srv.kb_create)("projects/vivy/decisions/0069-x.md", "body", "person")
    assert r["ok"] is False and r["error"] == "refused-policy"
    assert "Orient editor" in r["stderr"]
    assert srv._calls == []


def test_the_tool_descriptions_say_person_is_refused(srv):
    for verb in (srv.kb_update, srv.kb_create):
        doc = getattr(verb, "description", None) or _fn(verb).__doc__
        assert "person" in doc and "Orient editor" in doc


def test_in_session_still_reaches_the_cage(srv):
    _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert srv._calls[-1][srv._calls[-1].index("--provenance") + 1] == "in-session"
