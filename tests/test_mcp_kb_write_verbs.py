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
    """The server module with its cage calls AND its Library calls intercepted.

    `fake_run` answers a `kb checkout --sha-only` call (the resolution step
    `kb_update`/`kb_create` now run before a write) with a synthetic payload
    derived from the id it was given, independently of `fake_run.payload`/
    `.rc` — those two control the FINAL subcommand's own result, exactly as
    every test below already assumes, so a test that never touches the
    Library side keeps working unchanged.

    `fake_library` defaults to admitting every write: no checkout ever held,
    every take succeeds and echoes the owner it was sent — the same default
    neutrality `fake_run` already gives the cage side. A test exercising the
    Library's own refusal paths overrides `fake_library.respond`.

    `kb leases --json` is its own branch too (`leases_payload`/`leases_rc`,
    defaulting to an empty list) — `kb_checkout` now reads it BEFORE its own
    real checkout call, to tell a lease that call is about to take from one
    the holder already had; sharing `.payload` with the checkout call itself
    would make the two calls answer each other's requests.
    """
    calls = []

    def fake_run(cmd, *a, **kw_):
        calls.append(cmd)
        if "--sha-only" in cmd:
            payload = getattr(fake_run, "sha_only_payload", None)
            rc = getattr(fake_run, "sha_only_rc", 0)
            if payload is None:
                doc = cmd[-1]
                source_ref = doc if doc.endswith((".md", ".yaml", ".yml")) \
                    else f"projects/{doc}.md"
                payload = json.dumps({"source_ref": source_ref, "exists": True,
                                      "content": "", "content_sha": "deadbeef",
                                      "lease": None})
            return subprocess.CompletedProcess(cmd, rc, payload, "" if rc == 0 else "refused")
        if cmd[1:3] == ["kb", "leases"]:
            payload = getattr(fake_run, "leases_payload", "[]")
            rc = getattr(fake_run, "leases_rc", 0)
            return subprocess.CompletedProcess(cmd, rc, payload, "" if rc == 0 else "refused")
        payload = getattr(fake_run, "payload", "")
        rc = getattr(fake_run, "rc", 0)
        return subprocess.CompletedProcess(cmd, rc, payload, "" if rc == 0 else "refused")

    monkeypatch.setattr(mcp_kb_server.subprocess, "run", fake_run)
    mcp_kb_server._calls = calls
    mcp_kb_server._fake = fake_run

    library_calls = []

    def fake_library(method, path, body=None):
        library_calls.append((method, path, body))
        return fake_library.respond(method, path, body)

    def _admit(method, path, body):
        if method == "GET":
            return 200, {"checkout": None}
        if method == "POST" and path.endswith("/checkout"):
            owner = (body or {}).get("owner")
            return 200, {"checkout": {"id": "co-test", "doc_id": "x", "holder": owner,
                                      "run_id": None, "expires_at": "later"}}
        return 200, {"checkout": None}

    fake_library.respond = _admit
    monkeypatch.setattr(mcp_kb_server, "_library_request", fake_library)
    mcp_kb_server._library_calls = library_calls
    mcp_kb_server._fake_library = fake_library
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


# --- the Library's checkout (CQ·96, issue_01M0YAS4S64P5TXN7S2C17GHF6) -------
#
# `kb_update`/`kb_create` go through `_apply_through_library`: resolve via
# `kb checkout --sha-only`, take-or-reuse the Library's checkout, call `kb
# apply`, release only what was newly taken. Each pairing below isolates one
# branch by comparing it against its twin, so the delta — not either case
# alone — is what proves the behaviour (per the issue's own test discipline).


def _apply_call(srv):
    """The `kb apply` subcommand call, found among everything `fake_run` saw —
    not assumed to be `_calls[-1]`, since a release goes through
    `_library_request`, not `subprocess.run`, but a resolution step
    (`--sha-only`) does share the list."""
    applies = [c for c in srv._calls if c[1:3] == ["kb", "apply"]]
    assert len(applies) == 1, srv._calls
    return applies[0]


def test_update_with_no_checkout_held_takes_one_writes_and_releases_it(srv):
    _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    cmd = _apply_call(srv)
    holder = cmd[cmd.index("--holder") + 1]
    assert holder  # the session's own computed identity
    posts = [p for m, p, b in srv._library_calls if m == "POST"]
    assert any(p.endswith("/checkout") for p in posts), srv._library_calls
    assert any(p.endswith("/checkin") for p in posts), srv._library_calls
    checkin = next((b for m, p, b in srv._library_calls if p.endswith("/checkin")), None)
    assert checkin == {"owner": holder}


def test_update_with_a_checkout_the_caller_already_holds_does_not_release_it(srv):
    held = {"id": "co1", "holder": None, "expires_at": "later"}

    def respond(method, path, body):
        if method == "GET":
            held["holder"] = held["holder"] or srv._fake_holder
            return 200, {"checkout": dict(held)}
        raise AssertionError(f"no POST expected once a live checkout is already held: {method} {path}")

    # Discover this session's own holder the same way the server does, so the
    # stub can report a checkout already held BY it (not by someone else).
    srv._fake_holder = srv._session_holder()
    srv._fake_library.respond = respond
    _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    cmd = _apply_call(srv)
    assert cmd[cmd.index("--holder") + 1] == srv._fake_holder
    # No POST at all: `respond` would have raised. The GET alone establishes reuse.
    assert all(m == "GET" for m, _p, _b in srv._library_calls)


def test_update_refused_by_another_holders_checkout_makes_no_commit(srv):
    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "session:other",
                                      "expires_at": "later"}}
        raise AssertionError("no POST expected: the GET already found a live, foreign checkout")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert out["ok"] is False
    assert out["error"] == "leased"
    assert "session:other" in out["stderr"]
    assert not any(c[1:3] == ["kb", "apply"] for c in srv._calls)


def test_the_same_call_with_the_checkout_free_commits(srv):
    """The twin of the refusal above: identical call, only the Library's
    answer differs, and only that difference flips the outcome."""
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert out["ok"] is True
    assert _apply_call(srv)  # raises if no apply call landed


def test_library_returns_a_differing_holder_kb_apply_receives_the_returned_value(srv):
    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": None}
        if method == "POST" and path.endswith("/checkout"):
            # The Library strips/clips; simulate it returning something other
            # than what was sent.
            return 200, {"checkout": {"id": "co1", "holder": "session:normalized",
                                      "expires_at": "later"}}
        if method == "POST" and path.endswith("/checkin"):
            return 200, {"checkout": {}}
        raise AssertionError(f"unexpected call: {method} {path}")

    srv._fake_library.respond = respond
    _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    cmd = _apply_call(srv)
    assert cmd[cmd.index("--holder") + 1] == "session:normalized"
    checkin = next((b for m, p, b in srv._library_calls if p.endswith("/checkin")), None)
    assert checkin == {"owner": "session:normalized"}


def test_library_unreachable_refuses_with_its_own_message(srv):
    def respond(method, path, body):
        raise srv._LibraryUnreachable("unreachable (URLError)")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert out["ok"] is False
    assert out["error"] == "library-unreachable"
    assert "unreachable" in out["stderr"]
    assert not any(c[1:3] == ["kb", "apply"] for c in srv._calls)
    # Distinct from a foreign-holder refusal (test_update_refused_by_another_
    # holders_checkout_makes_no_commit): a reader needs to know whether to
    # wait for a holder or retry once the Library answers again.
    assert out["error"] != "leased"


def test_kb_checkout_with_a_caller_passed_owner_records_it_in_both_stores(srv):
    srv._fake.payload = json.dumps({"source_ref": "projects/x/a.md", "exists": True,
                                    "content": "body", "content_sha": "abc",
                                    "lease": {"holder": "co-library-0251"}})
    out = _fn(srv.kb_checkout)("x/a", owner="co-library-0251")
    cage_call = srv._calls[-1]
    assert cage_call[cage_call.index("--holder") + 1] == "co-library-0251"
    checkout_post = next((b for m, p, b in srv._library_calls
                          if m == "POST" and p.endswith("/checkout")), None)
    assert checkout_post == {"owner": "co-library-0251"}
    assert out["library_checkout"]["holder"] == "co-library-0251"


# --- one holder across calls (CQ·96 revise, defect 1) ------------------------


def test_kb_checkout_owner_then_kb_update_same_owner_reuses_the_checkout(srv):
    """A caller who passed `owner` to kb_checkout must pass the SAME value to
    kb_update, or `_apply_through_library` resolves a different holder (its
    own session's) and finds the Library checkout held by the first — this is
    the Library's own usage (comment 5320: owner "co-library-0251")."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1",
                                    "lease": {"holder": "co-library-0251"}})
    checkout_out = _fn(srv.kb_checkout)("shared/reference/a", owner="co-library-0251")
    assert checkout_out["ok"] is True

    srv._calls.clear()
    srv._library_calls.clear()

    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "co-library-0251",
                                      "expires_at": "later"}}
        raise AssertionError(f"no POST expected once a live checkout is already "
                             f"held: {method} {path}")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha1", "in-session", content="x",
                             owner="co-library-0251")
    assert out["ok"] is True
    cmd = _apply_call(srv)
    assert cmd[cmd.index("--holder") + 1] == "co-library-0251"
    # Reused, not released: a POST would have raised in `respond` above.
    assert all(m == "GET" for m, _p, _b in srv._library_calls)


def test_kb_update_without_owner_after_kb_checkout_with_owner_is_refused_as_leased(srv):
    """Twin: the identical pairing, but kb_update omits `owner` and falls back
    to this session's own holder — which the Library's checkout (still held
    by the owner kb_checkout passed) refuses as leased."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1",
                                    "lease": {"holder": "co-library-0251"}})
    _fn(srv.kb_checkout)("shared/reference/a", owner="co-library-0251")

    srv._calls.clear()
    srv._library_calls.clear()

    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "co-library-0251",
                                      "expires_at": "later"}}
        raise AssertionError("no POST expected: the GET already found a live, "
                             "foreign checkout")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha1", "in-session", content="x")
    assert out["ok"] is False
    assert out["error"] == "leased"
    assert "co-library-0251" in out["stderr"]
    assert not any(c[1:3] == ["kb", "apply"] for c in srv._calls)


# --- a refused checkout must not leave agentctl's lease behind (defect 2) ----


def test_kb_checkout_refused_by_another_holder_releases_the_lease_it_took(srv):
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    srv._fake.leases_payload = "[]"  # nothing held before this call

    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "session:other",
                                      "expires_at": "later"}}
        raise AssertionError("no POST expected: the GET found a live, foreign checkout")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "leased"
    releases = [c for c in srv._calls if c[1:3] == ["kb", "release"]]
    assert len(releases) == 1, srv._calls


def test_kb_checkout_refused_leaves_a_pre_existing_lease_alone(srv):
    """Twin: the identical refusal, but this holder's agentctl lease predates
    the call — already listed in `kb leases` — so the refusal must not
    release a lease this call did not take."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    holder = srv._session_holder()
    srv._fake.leases_payload = json.dumps(
        [{"source_ref": "projects/shared/reference/a.md", "holder": holder,
          "expires_at": "later"}])

    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "session:other",
                                      "expires_at": "later"}}
        raise AssertionError("no POST expected: the GET found a live, foreign checkout")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "leased"
    assert not any(c[1:3] == ["kb", "release"] for c in srv._calls)


def test_kb_checkout_refused_as_unreachable_releases_the_lease_it_took(srv):
    """The unreachable refusal releases likewise — a distinct code path from
    the foreign-holder refusal above, with the same obligation."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    srv._fake.leases_payload = "[]"

    def respond(method, path, body):
        raise srv._LibraryUnreachable("unreachable (URLError)")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "library-unreachable"
    releases = [c for c in srv._calls if c[1:3] == ["kb", "release"]]
    assert len(releases) == 1, srv._calls


# --- an unreadable `kb leases --json` must not be read as a pre-existing ----
# --- lease (reviewer, round 1): it still has to release what it took --------


def _foreign_holder_checkout_respond(method, path, body):
    if method == "GET":
        return 200, {"checkout": {"id": "co1", "holder": "session:other",
                                  "expires_at": "later"}}
    raise AssertionError("no POST expected: the GET found a live, foreign checkout")


def test_kb_checkout_releases_the_lease_when_leases_exits_nonzero(srv):
    """`agentctl kb leases --json` can itself fail — concretely, a concurrent
    lease write can leave `cmd_kb_leases` hitting a locked sqlite db (no
    `busy_timeout` set on that connection) while the checkout call right after
    it succeeds normally. `_leased_source_refs` must still read as 'nothing
    held before', so the refusal below releases the lease this call took."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    srv._fake.leases_rc = 1
    srv._fake.leases_payload = ""

    srv._fake_library.respond = _foreign_holder_checkout_respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "leased"
    releases = [c for c in srv._calls if c[1:3] == ["kb", "release"]]
    assert len(releases) == 1, srv._calls


def test_kb_checkout_releases_the_lease_when_leases_output_is_not_json(srv):
    """Twin: the subcommand exits 0 but its stdout is not JSON at all."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    srv._fake.leases_rc = 0
    srv._fake.leases_payload = "not json"

    srv._fake_library.respond = _foreign_holder_checkout_respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "leased"
    releases = [c for c in srv._calls if c[1:3] == ["kb", "release"]]
    assert len(releases) == 1, srv._calls


def test_kb_checkout_releases_the_lease_when_leases_output_is_not_a_list(srv):
    """Twin: the stdout parses as JSON but is not a list — e.g. an object,
    which has no meaning as a set of leases."""
    srv._fake.payload = json.dumps({"source_ref": "projects/shared/reference/a.md",
                                    "exists": True, "content": "body",
                                    "content_sha": "sha1", "lease": {}})
    srv._fake.leases_rc = 0
    srv._fake.leases_payload = json.dumps({"not": "a list"})

    srv._fake_library.respond = _foreign_holder_checkout_respond
    out = _fn(srv.kb_checkout)("shared/reference/a")
    assert out["ok"] is False and out["error"] == "leased"
    releases = [c for c in srv._calls if c[1:3] == ["kb", "release"]]
    assert len(releases) == 1, srv._calls


# --- a checkout record with no holder is unparseable, not a crash -----------


def test_a_taken_checkout_with_no_holder_refuses_as_unreachable_not_a_crash(srv):
    """`checkout["holder"]` further down would raise KeyError on a checkout
    the Library answers with no `holder` at all. This must read as the
    Library answering something unparseable instead."""
    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": None}
        if method == "POST" and path.endswith("/checkout"):
            return 200, {"checkout": {"id": "co1", "expires_at": "later"}}  # no "holder"
        raise AssertionError(f"unexpected call: {method} {path}")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert out["ok"] is False
    assert out["error"] == "library-unreachable"
    assert not any(c[1:3] == ["kb", "apply"] for c in srv._calls)


def test_an_already_held_checkout_with_no_holder_refuses_as_unreachable_not_a_crash(srv):
    """Twin: the same malformed record, found already live via GET rather
    than just taken via POST."""
    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "expires_at": "later"}}  # no "holder"
        raise AssertionError(f"no POST expected: the GET already found a live "
                             f"checkout: {method} {path}")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_update)("shared/reference/a", "sha", "in-session", content="x")
    assert out["ok"] is False
    assert out["error"] == "library-unreachable"
    assert not any(c[1:3] == ["kb", "apply"] for c in srv._calls)


# --- kb_release must release the Library checkout kb_checkout took (defect 3) -


def test_kb_release_checks_in_the_library_checkout_this_holder_holds(srv):
    holder = srv._session_holder()
    checkins = []

    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": holder, "expires_at": "later"}}
        if method == "POST" and path.endswith("/checkin"):
            checkins.append(body)
            return 200, {"checkout": {}}
        raise AssertionError(f"unexpected call: {method} {path}")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_release)("shared/reference/a")
    assert out["ok"] is True
    assert out["library_release"] == {"released": True}
    assert checkins == [{"owner": holder}]


def test_kb_release_leaves_another_holders_library_checkout_alone(srv):
    """Twin: a live Library checkout held by someone else must not be checked
    in by this holder's release."""
    def respond(method, path, body):
        if method == "GET":
            return 200, {"checkout": {"id": "co1", "holder": "session:other",
                                      "expires_at": "later"}}
        raise AssertionError("no checkin expected: the checkout is held by someone else")

    srv._fake_library.respond = respond
    out = _fn(srv.kb_release)("shared/reference/a")
    assert out["ok"] is True
    assert out["library_release"]["released"] is False
