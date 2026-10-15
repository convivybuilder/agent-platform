"""`agentctl` materializes a dispatched agent from a Library checkout grant
(issue_01M4AEY9RGJVTCBNEY3YN49DCG), beside `resolve_agent`'s roster render.

Phase 5's own done-clause (Library epic issue_01KZBWEYHJ1QCKBR5BTM6G6DQ9):
"a client runs unchanged against a stub Library returning
`{persona_md, docs[], model, budget, limits}`" is this file's
`test_a_dispatch_materializes_from_a_checkout_grant_with_no_knowledge_checkout`
— the exact call, `agentctl.resolve_agent_from_grant(issue_id, ...)`, that a
real Library answers with no change on this side.

A new file rather than additions to test_agentctl.py: CQ·371 is changing
knowledge-ctx and `resolve_agent` in the same window, and this path is
deliberately beside both, not through either.
"""
from __future__ import annotations

import hashlib
import io
import json
import types

import pytest


def _doc(doc_id: str, body: str = "reference material.") -> tuple[str, dict]:
    """A document the Library's `GET /reference/documents/{id}` would serve,
    and the `body_sha256` a checkout grant would list for it."""
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return sha, {"id": doc_id, "source_ref": f"projects/{doc_id}.md", "kind": "reference",
                 "title": doc_id, "body": body, "body_sha256": sha}


def _grant(*, persona_md: str = "# a persona\n\nplaceholder.", docs=None,
          model: str = "sonnet", cap_usd=5.0, limits=None,
          slip_id: str = "slip_test") -> dict:
    """One `/items/{id}/checkout` response: the unchanged `item`/`lease`/
    `policy`/`adjacency` alongside the call-slip grant (ADR-0062)."""
    return {
        "item": {"id": "issue_test"}, "lease": {}, "policy": {}, "claimed": True,
        "adjacency": {}, "slip_id": slip_id, "persona_md": persona_md,
        "docs": docs if docs is not None else [],
        "bundle": {"profile": None, "available": True},
        "model": model, "budget": {"cap_usd": cap_usd},
        "limits": limits if limits is not None else {},
    }


class _Served(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeGrantLibrary:
    """Stands in for the Library's fixed-address opener, for the three routes
    `resolve_agent_from_grant` calls: `POST /items/{id}/checkout`,
    `POST /items/{id}/release` and `GET /reference/documents/{id}`.

    `checkout_responses` maps an issue id to `(status, body)`; a status other
    than 200 is served the way urllib raises a non-2xx answer, body included,
    the same shape a 409 "already claimed" carries. `unreachable` fails every
    call the way a refused connection does. Every checkout/release call is
    kept in `checkout_log`/`release_log`, so a test can assert whether the
    lease taken to read the grant was released."""

    def __init__(self):
        self.checkout_responses: dict[str, tuple[int, dict]] = {}
        self.docs: dict[str, dict] = {}
        self.unreachable = False
        self.checkout_log: list[dict] = []
        self.release_log: list[dict] = []

    def open(self, req, timeout=None):
        import urllib.error
        import urllib.parse
        if self.unreachable:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        parts = urllib.parse.urlsplit(req.full_url)
        method = req.get_method()
        if method == "POST" and parts.path.endswith("/checkout"):
            issue_id = urllib.parse.unquote(parts.path[len("/items/"):-len("/checkout")])
            payload = json.loads(req.data.decode("utf-8")) if req.data else {}
            self.checkout_log.append({"issue_id": issue_id, **payload})
            status, body = self.checkout_responses.get(
                issue_id, (404, {"error": "no such item"}))
            if status != 200:
                raise urllib.error.HTTPError(
                    req.full_url, status, "refused", None,
                    io.BytesIO(json.dumps(body).encode("utf-8")))
            return _Served(json.dumps(body).encode("utf-8"))
        if method == "POST" and parts.path.endswith("/release"):
            issue_id = urllib.parse.unquote(parts.path[len("/items/"):-len("/release")])
            payload = json.loads(req.data.decode("utf-8")) if req.data else {}
            self.release_log.append({"issue_id": issue_id, **payload})
            return _Served(b"{}")
        if method == "GET" and parts.path.startswith("/reference/documents/"):
            doc_id = urllib.parse.unquote(parts.path[len("/reference/documents/"):])
            if doc_id not in self.docs:
                raise urllib.error.HTTPError(req.full_url, 404, "not found", None, None)
            return _Served(json.dumps(self.docs[doc_id]).encode("utf-8"))
        raise AssertionError(f"unexpected request: {method} {parts.path}")


@pytest.fixture
def grant_library(agentctl, tmp_path, monkeypatch):
    """The Library's fixed-address opener. HOME is repointed at an empty temp
    dir with no knowledge checkout and no knowledge index, and touching the
    knowledge index raises — this materialization path never reads either,
    and a test that did would fail loudly rather than quietly passing against
    the real machine's index."""
    import issue_client
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CONVIVY_SESSION_DIR", raising=False)
    token = tmp_path / "agent-platform-token"
    token.write_text("test-token\n")
    monkeypatch.setattr(agentctl.SC, "TOKEN_FILE", token)
    fake = _FakeGrantLibrary()
    monkeypatch.setattr(issue_client, "pinned_opener", lambda: fake)
    monkeypatch.setattr(agentctl, "KB_INDEX", tmp_path / "no-index" / "index.db")
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tmp_path / "no-checkout")
    monkeypatch.setattr(
        agentctl.sqlite3, "connect",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("grant materialization touched the knowledge index")))
    return fake


# --- the invariant's own test (Library epic issue_01KZBWEYHJ1QCKBR5BTM6G6DQ9) ------


def test_a_dispatch_materializes_from_a_checkout_grant_with_no_knowledge_checkout(
        agentctl, grant_library):
    sha, served = _doc("platform/reference/one")
    grant_library.docs["platform/reference/one"] = served
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(persona_md="# the persona\n\nbody.", model="opus", cap_usd=12.5,
                   docs=[{"id": "platform/reference/one", "body_sha256": sha}]))

    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")

    assert agent["body"] == "# the persona\n\nbody."
    assert agent["model_alias"] == "opus"
    assert agent["grant"]["budget"]["cap_usd"] == 12.5
    assert agent["grant"]["documents"] == [served]
    assert agent["grant"]["slip_id"] == "slip_test"
    assert agent["local"] is False

    # the lease taken to read the grant is released, not held across the call
    assert grant_library.checkout_log == [{"issue_id": "issue_test"}]
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_an_empty_docs_list_is_not_malformed(agentctl, grant_library):
    """`docs` is empty whenever the checkout named no profile (Library app.py
    `_call_slip_grant`) — a valid grant, not a missing field."""
    grant_library.checkout_responses["issue_test"] = (200, _grant(docs=[]))
    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert agent["grant"]["documents"] == []


def test_a_null_spend_cap_is_not_malformed(agentctl, grant_library):
    """`budget.cap_usd` is `None` until the Library resolves a real cap
    (Library app.py `_call_slip_grant`) — still a valid grant."""
    grant_library.checkout_responses["issue_test"] = (200, _grant(cap_usd=None))
    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert agent["grant"]["budget"]["cap_usd"] is None


# --- the checkout names the dispatched role and its dispatch profile --------------
# (issue_01M35KSS8E603FE0EPYR0M5SBN). Before this fix `_checkout_item_grant`'s body
# carried only `owner`/`run_id`: a `spawn --materialize-from-grant --issue` dispatch
# never told the Library which role or which dispatch profile it was for, so the
# Library's call-slip grant minted with no persona (`persona_md` began "# default")
# and no resolved model (the sentinel "model-resolution-not-implemented") — the
# occurrence this closes (run-a8621f37, 2026-10-13, a platform-coder dispatch for
# work item E1·9).


def test_the_checkout_names_the_dispatched_role_as_persona(
        agentctl, grant_library, tmp_path):
    """Fails against the unfixed code: there, the checkout body carried no
    `persona` at all, no matter what role name the dispatch gave.

    `agents_dir` names an empty directory so the builtins lookup
    (`resolve_agent`, CQ·434) fails locally without reaching the Library's
    agent listing, which this fake does not stub — persona is sent from
    `agent_name` itself, independent of whether that lookup resolves."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="platform-coder",
        agents_dir=str(tmp_path / "empty-agents"))

    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "platform-coder"}]


def test_the_checkout_names_the_profile_the_run_executes_under(
        agentctl, grant_library, tmp_path):
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="platform-coder",
        agents_dir=str(tmp_path / "empty-agents"), profile="agent-platform/overnight")

    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "platform-coder",
         "profile": "agent-platform/overnight"}]


def test_no_role_or_profile_sends_neither_field(agentctl, grant_library):
    """Back-compat default: a caller naming neither still sends the body this
    function always sent before this fix."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")

    assert grant_library.checkout_log == [{"issue_id": "issue_test"}]


# --- `spawn --materialize-from-grant` sends the checkout the same profile the --
# run executes under, whether named by an explicit `--profile` or left to
# `default_profile` to compute (issue_01M35KSS8E603FE0EPYR0M5SBN).


def _stub_through_the_budget_gate(agentctl, monkeypatch):
    """Stubs everything `_spawn_dispatch` touches between the checkout and
    `_budget_gate`, so a test can stop a grant-materialized dispatch right
    after the checkout and read what it sent — the same posture
    `test_a_project_with_no_ledger_cap_is_never_refused_on_money` above takes."""
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "_library_denials", lambda *a, **kw: [])
    monkeypatch.setattr(agentctl, "resolve_agent", lambda *a, **kw: {"builtins": None})
    monkeypatch.setattr(agentctl, "_fetch_work_item", lambda issue_id: ({}, None))

    class _ReachedBudgetGate(Exception):
        pass

    def _boom(*a, **kw):
        raise _ReachedBudgetGate()
    monkeypatch.setattr(agentctl, "_budget_gate", _boom)
    return _ReachedBudgetGate


def test_spawn_dispatch_sends_an_explicit_profile_to_the_checkout(
        agentctl, grant_library, monkeypatch):
    grant_library.checkout_responses["issue_test"] = (200, _grant())
    reached = _stub_through_the_budget_gate(agentctl, monkeypatch)

    args = _spawn_args(materialize_from_grant=True, issue="issue_test",
                       agent="platform-coder", profile="agent-platform/overnight")
    with pytest.raises(reached):
        agentctl._spawn_dispatch(args)

    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "platform-coder",
         "profile": "agent-platform/overnight"}]


def test_spawn_dispatch_sends_the_default_profile_to_the_checkout_when_none_given(
        agentctl, grant_library, monkeypatch):
    """No `--profile`: the checkout is told the same `default_profile` value
    the run itself computes just afterward (`agent-platform/daytime` for
    `platform-coder`, pinned by `test_default_profile_by_repo_and_role`)."""
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    grant_library.checkout_responses["issue_test"] = (200, _grant())
    reached = _stub_through_the_budget_gate(agentctl, monkeypatch)

    args = _spawn_args(materialize_from_grant=True, issue="issue_test",
                       agent="platform-coder", profile=None)
    with pytest.raises(reached):
        agentctl._spawn_dispatch(args)

    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "platform-coder",
         "profile": "agent-platform/daytime"}]


# --- a missing or malformed field refuses the dispatch, naming the field ----------


def _missing(field):
    def strip(g):
        del g[field]
        return g
    return strip


@pytest.mark.parametrize("field,edit", [
    ("persona_md", _missing("persona_md")),
    ("docs", _missing("docs")),
    ("model", _missing("model")),
    ("budget", _missing("budget")),
    ("limits", _missing("limits")),
], ids=["persona_md", "docs", "model", "budget", "limits"])
def test_a_grant_missing_a_field_refuses_the_dispatch_naming_it(
        agentctl, grant_library, field, edit):
    grant_library.checkout_responses["issue_test"] = (200, edit(_grant()))
    with pytest.raises(SystemExit, match=field):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    # a malformed grant does not strand the lease it took to read it
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_a_blank_persona_md_refuses(agentctl, grant_library):
    grant_library.checkout_responses["issue_test"] = (200, _grant(persona_md="   "))
    with pytest.raises(SystemExit, match="persona_md"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")


def test_a_non_numeric_spend_cap_refuses(agentctl, grant_library):
    g = _grant()
    g["budget"]["cap_usd"] = "twelve"
    grant_library.checkout_responses["issue_test"] = (200, g)
    with pytest.raises(SystemExit, match="cap_usd"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")


def test_a_sentinel_model_refuses_naming_the_item_and_sentinel(agentctl, grant_library):
    """The Library sends this literal string as `model` when the checkout
    resolved none (no model_choice, no persona agent-definition pin inside
    the caller's bound dispatch profile) — a deliberate sentinel, confirmed
    by the Library's crew on 2026-10-14, that must never reach `--model`
    (run-a8621f37: spawned with it, the Anthropic API answered 404, and the
    run ended after 4 seconds with no known tier to fail over to)."""
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(model="model-resolution-not-implemented"))
    with pytest.raises(SystemExit, match="issue_test.*model-resolution-not-implemented"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    # the lease taken to read the grant is released on this refusal too
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_a_sentinel_model_with_surrounding_whitespace_still_refuses(agentctl, grant_library):
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(model="  model-resolution-not-implemented\n"))
    with pytest.raises(SystemExit, match="issue_test.*model-resolution-not-implemented"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_a_docs_entry_with_no_id_refuses(agentctl, grant_library):
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(docs=[{"body_sha256": "a" * 64}]))
    with pytest.raises(SystemExit, match="'id'"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")


# --- a document whose body does not match its hash refuses ------------------------


def test_a_document_whose_body_does_not_match_its_own_hash_refuses(agentctl, grant_library):
    sha, served = _doc("platform/reference/one", body="original.")
    served["body"] = "tampered."  # body_sha256 still says "original."
    grant_library.docs["platform/reference/one"] = served
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(docs=[{"id": "platform/reference/one", "body_sha256": sha}]))
    with pytest.raises(SystemExit, match="does not match its own body_sha256"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_a_document_changed_between_the_grant_and_its_body_refuses(agentctl, grant_library):
    sha, served = _doc("platform/reference/one", body="original.")
    grant_library.docs["platform/reference/one"] = served
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(docs=[{"id": "platform/reference/one", "body_sha256": "f" * 64}]))
    with pytest.raises(SystemExit, match="changed .*between the checkout grant"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")


# --- a blocked (checked out but not claimed) item refuses naming the block -------


def test_a_blocked_item_refuses_naming_the_block_not_a_malformed_grant(
        agentctl, grant_library):
    """`claimed: false` (platform/decisions/0079): the checkout took the
    lease but minted no call slip because the item has unmet prerequisites,
    so `persona_md` and the rest of the grant are absent by design. The
    refusal must name the block, not read this as a corrupt grant."""
    grant_library.checkout_responses["issue_test"] = (200, {
        "item": {"id": "issue_test"}, "lease": {}, "policy": {},
        "claimed": False, "unmet_count": 2,
        "hint": "held but not claimed, because the item has unmet "
                "prerequisites: judge or correct it under this lease and "
                "release it, or check out again once it is unblocked to "
                "claim it"})
    with pytest.raises(SystemExit, match="blocked") as exc_info:
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert "persona_md" not in str(exc_info.value)
    assert "2" in str(exc_info.value)
    # the lease the checkout took is still released
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


# --- a held item or an unreachable Library refuses too, no fallback ---------------


def test_a_held_item_refuses_rather_than_falling_back_to_the_roster_render(
        agentctl, grant_library):
    grant_library.checkout_responses["issue_test"] = (409, {"error": "already claimed"})
    with pytest.raises(SystemExit, match="already claimed"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    # nothing was taken, so there is nothing to release
    assert grant_library.release_log == []


def test_an_unreachable_library_refuses_the_dispatch(agentctl, grant_library):
    grant_library.unreachable = True
    with pytest.raises(SystemExit, match="unreachable"):
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert grant_library.release_log == []


# --- a project-ledger soft/hard cap refuses admission distinctly (E1·8,
# issue_01M35KSM0KFKJ4P3DN1G19KYBD) --------------------------------------------
#
# `_refuse_on_capped_ledger` (library app.py) answers `/items/{id}/checkout` with
# 409 and a `ledger` dict when the dispatched project's spend has reached its
# soft cap, or — until revocation exists — its hard cap. Before this change
# `_checkout_item_grant` read every non-200 answer the same way (bare
# `SystemExit`, the "already claimed" shape `test_a_held_item_refuses_rather_
# than_falling_back_to_the_roster_render` above pins), so a caller had no way
# to tell a money refusal apart from a routing one without parsing the message
# text, and nothing recorded it as a run.


def test_a_soft_capped_checkout_raises_LedgerCapRefused_not_bare_SystemExit(
        agentctl, grant_library):
    """Fails against the unfixed code on the TYPE of the exception raised, not
    on whether one is raised at all: before this change this same 409 took the
    generic branch and raised `SystemExit`, which `_spawn_dispatch` could not
    tell apart from "already claimed" or an unreachable Library."""
    grant_library.checkout_responses["issue_test"] = (409, {
        "error": "the ledger for 'agent-platform' has reached its soft_cap for "
                 "2026-10-13",
        "hint": "this is not a reason to try another item",
        "ledger": {"id": 7, "scope_kind": "project", "scope_id": "agent-platform",
                   "day": "2026-10-13", "state": "soft_capped",
                   "cap": "soft_cap", "cap_usd": 30.0, "total_usd": 31.42},
    })
    with pytest.raises(agentctl._LedgerCapRefused) as exc_info:
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert exc_info.value.body["ledger"]["cap"] == "soft_cap"
    assert exc_info.value.body["ledger"]["total_usd"] == 31.42
    # the Library checks the ledger ahead of the item lock, so no lease was
    # taken and there is nothing to release
    assert grant_library.release_log == []


def test_a_hard_capped_checkout_also_raises_LedgerCapRefused(agentctl, grant_library):
    """Until revocation exists, a hard-capped total refuses admission the same
    way a soft cap does (same route, same shape, `cap: hard_cap`)."""
    grant_library.checkout_responses["issue_test"] = (409, {
        "error": "the ledger for 'agent-platform' has reached its hard_cap for "
                 "2026-10-13",
        "ledger": {"id": 7, "scope_kind": "project", "scope_id": "agent-platform",
                   "day": "2026-10-13", "state": "hard_capped",
                   "cap": "hard_cap", "cap_usd": 80.0, "total_usd": 85.0},
    })
    with pytest.raises(agentctl._LedgerCapRefused) as exc_info:
        agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert exc_info.value.body["ledger"]["cap"] == "hard_cap"


# --- selected explicitly by `spawn --materialize-from-grant` ----------------------


def _spawn_args(**overrides):
    base = dict(agent="reviewer", task="t", on="agent-platform", profile=None,
               agents_dir=None, parent=None, pr=None, review_sha=None,
               model=None, reason=None, issue=None, materialize_from_grant=False)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_spawn_without_issue_refuses_materialize_from_grant_before_any_library_call(
        agentctl, monkeypatch):
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)

    def _unreached(*a, **kw):
        raise AssertionError("resolve_agent_from_grant must not run without --issue")
    monkeypatch.setattr(agentctl, "resolve_agent_from_grant", _unreached)

    args = _spawn_args(materialize_from_grant=True, issue=None)
    assert agentctl.cmd_spawn(args) == 2


def test_spawn_with_materialize_from_grant_and_issue_takes_the_grant_path_not_the_roster(
        agentctl, monkeypatch):
    from conftest import stub_profile_listing
    stub_profile_listing(agentctl, monkeypatch)
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)

    class _ReachedGrantPath(Exception):
        pass

    def _grant_path(issue_id, repo=None, agent_name=None, agents_dir=None, profile=None):
        assert issue_id == "issue_test" and repo == "agent-platform"
        # CQ·434: the role name travels with the dispatch so the grant path can
        # carry that role's `builtins:` the same way the roster path does.
        assert agent_name == "reviewer" and agents_dir is None
        # issue_01M35KSS8E603FE0EPYR0M5SBN: no --profile given, so this is the
        # same default_profile(repo, args.agent) the run executes under.
        assert profile == "agent-platform/daytime"
        raise _ReachedGrantPath()
    monkeypatch.setattr(agentctl, "resolve_agent_from_grant", _grant_path)
    monkeypatch.setattr(agentctl, "resolve_agent",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("the roster render must not run when "
                                          "--materialize-from-grant is set")))

    args = _spawn_args(materialize_from_grant=True, issue="issue_test")
    with pytest.raises(_ReachedGrantPath):
        agentctl.cmd_spawn(args)


def test_spawn_without_materialize_from_grant_still_uses_the_roster_render(
        agentctl, monkeypatch):
    """The default path is unchanged: with the flag absent, `--issue` alone
    does not divert a dispatch into the grant path."""
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)

    class _ReachedRosterPath(Exception):
        pass

    def _roster_path(name, repo=None, agents_dir=None):
        raise _ReachedRosterPath()
    monkeypatch.setattr(agentctl, "resolve_agent", _roster_path)
    monkeypatch.setattr(agentctl, "resolve_agent_from_grant",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("the grant path must not run without "
                                          "--materialize-from-grant")))

    args = _spawn_args(materialize_from_grant=False, issue="issue_test")
    with pytest.raises(_ReachedRosterPath):
        agentctl.cmd_spawn(args)


# --- E1·8 (issue_01M35KSM0KFKJ4P3DN1G19KYBD): a soft cap refuses a NEW dispatch
# for the capped project without disturbing a run already in flight, and a
# project with no caps is never refused on money -------------------------------


def test_a_soft_capped_project_refuses_a_new_dispatch_while_a_run_already_going_is_untouched(
        agentctl, grant_library, runs_root, tmp_path, monkeypatch):
    """Both halves of the epic's done-clause in one test (issue_01KZBWEYGQ5JXE96V1D0DYX5RA,
    clause 5): a run that started before the project's ledger crossed its soft cap
    is not touched by a later dispatch's refusal and finishes normally, while that
    later dispatch — for the SAME project, after the crossing — is refused with the
    Library's own reason (which cap, the total, the day) recorded in its own run dir.

    Fails against the unfixed code on two assertions: `_checkout_item_grant`'s only
    refusal shape was a bare `SystemExit`, so `agentctl._spawn_dispatch` would
    propagate it uncaught instead of returning exit code 3; and nothing wrote a run
    record for the refusal at all, so `outcome["exit_status"] == "project_cap_refused"`
    had no run dir to read it from.
    """
    import telemetry as T
    monkeypatch.setattr(agentctl, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(T, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl.T, "new_run_id", lambda: "run-refused-01")
    # The role-name builtins lookup (CQ·434) and the --issue model-choice read
    # (model-lane epic issue_01M35KQ8WTW8R3WSKXS81TV2DZ) both run before the
    # checkout and are orthogonal to the ledger gate under test here; stubbed
    # so neither reaches the fake Library's unstubbed endpoints.
    monkeypatch.setattr(agentctl, "resolve_agent", lambda *a, **kw: {"builtins": None})
    monkeypatch.setattr(agentctl, "_fetch_work_item", lambda issue_id: ({}, None))
    today = T.today_dir()

    # A run already going, started before the ledger crossed its soft cap.
    live = T.create_run_dir("run-live-01", date=today)
    T.write_meta(live, run_id="run-live-01", agent="platform-coder", task="earlier work",
                started_at=T.now_iso(), repo="agent-platform",
                worktree=str(tmp_path / "wt"), profile="agent-platform/overnight",
                model="claude-sonnet-5")

    # The project's ledger has now crossed its soft cap.
    grant_library.checkout_responses["issue_test"] = (409, {
        "error": f"the ledger for 'agent-platform' has reached its soft_cap for {today}",
        "ledger": {"id": 7, "scope_kind": "project", "scope_id": "agent-platform",
                   "day": today, "state": "soft_capped",
                   "cap": "soft_cap", "cap_usd": 30.0, "total_usd": 31.42},
    })

    args = _spawn_args(materialize_from_grant=True, issue="issue_test",
                       agent="platform-coder", task="new work",
                       profile="agent-platform/overnight")
    rc, run_id = agentctl._spawn_dispatch(args)
    assert rc == 3
    assert run_id is None  # the same "no id allocated on this refusal" shape budget_refused uses

    # The refusal got its OWN run dir, distinct from budget_refused.
    refused = T.run_dir("run-refused-01", date=today)
    outcome = T.read_outcome(refused)
    assert outcome["exit_status"] == "project_cap_refused"
    assert outcome["exit_status"] != "budget_refused"
    for needle in ("soft_cap", "31.42", today):
        assert needle in outcome["error_reason"], outcome["error_reason"]

    # The LIVE run was never touched by the refusal: still no outcome.json.
    assert not (live / "outcome.json").exists()
    # It goes on to finish normally, unaffected by the refusal that happened
    # in between.
    T.write_outcome(live, exit_status="completed")
    assert T.read_outcome(live)["exit_status"] == "completed"


def test_a_project_with_no_ledger_cap_is_never_refused_on_money(
        agentctl, grant_library, monkeypatch):
    """E1·8 clause 10 (issue_01KZBWEYGQ5JXE96V1D0DYX5RA): the ordinary case — a
    project the Library has set no caps on — is admitted and the dispatch
    proceeds. The stub Library's checkout response carries no `ledger` key at
    all, the same shape `_grant()` already builds for every OTHER test in this
    file's successful-checkout path (`_refuse_on_capped_ledger` returns None,
    not a `state: "ok"` row, when there is no ledger row to refuse on). What's
    new here is pinning that this reaches all the way to the budget gate
    rather than being refused — the dispatch is never even offered a chance to
    be refused on money."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())
    monkeypatch.setattr(agentctl, "_warn_deployed_tree", lambda: None)
    monkeypatch.setattr(agentctl, "_spawn_lib_gate", lambda: None)
    monkeypatch.setattr(agentctl, "_library_denials", lambda *a, **kw: [])
    # Orthogonal to the ledger gate under test — see the twin test above.
    monkeypatch.setattr(agentctl, "resolve_agent", lambda *a, **kw: {"builtins": None})
    monkeypatch.setattr(agentctl, "_fetch_work_item", lambda issue_id: ({}, None))

    class _ReachedBudgetGate(Exception):
        pass

    def _boom(*a, **kw):
        raise _ReachedBudgetGate()
    monkeypatch.setattr(agentctl, "_budget_gate", _boom)

    args = _spawn_args(materialize_from_grant=True, issue="issue_test",
                       profile="agent-platform/daytime")
    with pytest.raises(_ReachedBudgetGate):
        agentctl._spawn_dispatch(args)


# --- the grant's budget.cap_usd reaches the per-run dollar cap decision -----------
# (issue_01M174CABDWSEP2EEVY6JXZ5VB, the operator's 2026-10-07 ruling, slate a-1ommg6) ------
# Modeled on the Library's own tests/test_checkout.py::
# test_budget_cap_usd_carries_the_projects_per_run_cap — this is the agentctl
# side of that same number: a project's per-run cap, served in the checkout
# grant, reaching `_run_cost_cap_for_agent` unchanged.


def test_the_librarys_served_cap_usd_reaches_run_cost_cap_for_agent(
        agentctl, grant_library):
    """The number a stub Library serves at checkout — not a hand-built dict —
    is what `_run_cost_cap_for_agent` reads back as this run's dollar cap."""
    grant_library.checkout_responses["issue_test"] = (200, _grant(cap_usd=2.5))

    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")

    model = agentctl.model_id(agent["model_alias"])
    assert agentctl._run_cost_cap_for_agent(model, agent) == (2.5, "grant")


def test_the_librarys_served_null_cap_reaches_run_cost_cap_for_agent_as_grant_none(
        agentctl, grant_library):
    """A project with no per-run cap serves `cap_usd: null` (Library app.py);
    that still reaches here as source "grant", not "none" — the Library
    decided there is no cap, which is different from this dispatch never
    having a grant at all."""
    grant_library.checkout_responses["issue_test"] = (200, _grant(cap_usd=None))

    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")

    model = agentctl.model_id(agent["model_alias"])
    assert agentctl._run_cost_cap_for_agent(model, agent) == (None, "grant")


# --- `agent_name` carries the dispatched ROLE's `builtins:` through (CQ·434, --------
# issue_01M4D39EFVVJS9Q5XTN7HEFTEM). #818's round-1 reviewer found that this path
# never read `builtins:` at all, so a coder dispatched through `spawn --issue
# --materialize-from-grant` got none of #818's --tools saving. Rather than teach
# this path a second `builtins:` reader, `resolve_agent_from_grant` now calls the
# exact `resolve_agent` the roster path already calls for the dispatch's own role
# name, and carries through only its `builtins` field — so the two paths can
# never answer differently for the same role.


def test_resolve_agent_from_grant_carries_the_named_roles_builtins(
        agentctl, grant_library, tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "coder.md").write_text(
        "---\nmodel: sonnet\nbuiltins: [Bash, Read, Edit, Write, Glob, Grep]\n---\n"
        "You are a test agent.\n")
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    grant_agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="coder",
        agents_dir=str(agents_dir))
    roster_agent = agentctl.resolve_agent(
        "coder", repo="agent-platform", agents_dir=str(agents_dir))

    assert grant_agent["builtins"] == roster_agent["builtins"] == [
        "Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    # Not just the field: the --tools surface each produces is identical too.
    assert (agentctl._resolved_agent_builtins(grant_agent, "coder")
            == agentctl._resolved_agent_builtins(roster_agent, "coder")
            == ("Bash", "Read", "Edit", "Write", "Glob", "Grep"))


def test_resolve_agent_from_grant_without_builtins_matches_the_roster_path(
        agentctl, grant_library, tmp_path):
    """A role whose definition carries no `builtins:` key resolves to `None`
    (no override) through the grant path exactly as it does through the
    roster path — unchanged by this fix on either side."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "reviewer2.md").write_text(
        "---\nmodel: sonnet\ntools: [Read, Bash]\n---\nYou are a test agent.\n")
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    grant_agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="reviewer2",
        agents_dir=str(agents_dir))
    roster_agent = agentctl.resolve_agent(
        "reviewer2", repo="agent-platform", agents_dir=str(agents_dir))

    assert grant_agent["builtins"] is None
    assert roster_agent["builtins"] is None
    assert agentctl._resolved_agent_builtins(grant_agent, "reviewer2") is None
    assert agentctl._resolved_agent_builtins(roster_agent, "reviewer2") is None


def test_resolve_agent_from_grant_without_agent_name_leaves_builtins_unset(
        agentctl, grant_library):
    """The back-compat default: every pre-existing call in this file omits
    `agent_name`, and must keep getting no builtins override at all —
    `_resolved_agent_builtins` reads the absent key the same as `None`."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())
    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")
    assert agent.get("builtins") is None
    assert agentctl._resolved_agent_builtins(agent, "issue_test") is None


# --- #825 round-1: a role that never resolves outside the grant must not claim ----
# the work item and then fail on it. This function's own docstring says the grant
# path never falls back to the roster render for the dispatched AGENT — resolving
# `agent_name` is only borrowing the roster's `builtins:` reader for a role name,
# and that lookup now runs before `_checkout_item_grant` so a role that does not
# resolve anywhere never takes the checkout lease at all.


def test_an_unresolvable_role_dispatches_with_builtins_unset_and_does_not_claim_first(
        agentctl, grant_library, tmp_path, capsys):
    """Fails against b2034817: there, `resolve_agent(agent_name, ...)` ran
    AFTER the checkout, so an unresolvable role raised SystemExit with the
    item already checked out (claiming it) — the dispatch failed with
    "agent ... not found" instead of proceeding.

    `agents_dir` names an empty directory so the lookup fails locally,
    without reaching the Library's agent listing (which this fake does not
    stub) — the failure mode under test is the role not resolving, not the
    transport it would otherwise resolve through."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="no-such-role",
        agents_dir=str(tmp_path / "empty-agents"))

    assert agent.get("builtins") is None
    assert agentctl._resolved_agent_builtins(agent, "no-such-role") is None
    # persona names the dispatched ROLE regardless of whether the builtins
    # lookup above resolved it locally (issue_01M35KSS8E603FE0EPYR0M5SBN).
    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "no-such-role"}]
    assert "no-such-role" in capsys.readouterr().err


def test_a_resolvable_role_still_carries_builtins_through_the_fixed_order(
        agentctl, grant_library, tmp_path):
    """Fails against b2034817 on the ordering, not the value: there the
    lookup ran after the checkout and still carried `builtins:` through for
    a resolvable role, so this pins that the carry survives moving the
    lookup earlier."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "coder.md").write_text(
        "---\nmodel: sonnet\nbuiltins: [Bash, Read, Edit, Write, Glob, Grep]\n---\n"
        "You are a test agent.\n")
    grant_library.checkout_responses["issue_test"] = (200, _grant())

    agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="coder",
        agents_dir=str(agents_dir))

    assert agent["builtins"] == ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    assert grant_library.checkout_log == [
        {"issue_id": "issue_test", "persona": "coder"}]


def test_the_role_lookup_runs_before_the_checkout(agentctl, grant_library, monkeypatch):
    """A stub `resolve_agent` records when it was called relative to the
    checkout call, directly pinning the order #825 round-1 requires.

    Fails against b2034817, where the call order is reversed: `order` would
    read `["checkout", "resolve_agent"]`."""
    grant_library.checkout_responses["issue_test"] = (200, _grant())
    order: list[str] = []
    real_checkout = agentctl._checkout_item_grant

    def _logging_checkout(issue_id, **kw):
        order.append("checkout")
        return real_checkout(issue_id, **kw)
    monkeypatch.setattr(agentctl, "_checkout_item_grant", _logging_checkout)

    def _logging_resolve_agent(name, repo=None, agents_dir=None):
        order.append("resolve_agent")
        return {"builtins": ["Read"]}
    monkeypatch.setattr(agentctl, "resolve_agent", _logging_resolve_agent)

    agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="coder")

    assert order == ["resolve_agent", "checkout"]
    assert agent["builtins"] == ["Read"]


# --- the Library's placeholder persona (no persona storage or registry exists ----
# yet) is not real persona content: the dispatched role's roster render supplies
# the body instead, and refuses by name when no role resolves to fall back to
# (run-60cafe23, 2026-10-14, a platform-coder dispatch for work item CQ·480 that ran
# with exactly the placeholder as its system prompt — the steward ruled: treat it
# the way #890 treats the Library's model sentinel, preferring the roster fallback
# over a bare refusal). Built from the literal placeholder text, not imported from
# `agentctl`'s own sentinel constant, so a change to that constant can't silently
# carry both sides of the comparison.


_LIBRARYS_PLACEHOLDER_SENTENCE = (
    "Persona content is not resolved yet — no persona storage or registry "
    "exists. This is a placeholder pinning the call slip's shape ahead of it."
)


def _librarys_placeholder_persona_md(heading: str) -> str:
    """The exact body `_persona_md` (Library app.py) builds when it has no
    persona to resolve: `f"# {heading}\\n\\n" + ` the sentence above."""
    return f"# {heading}\n\n{_LIBRARYS_PLACEHOLDER_SENTENCE}"


def _write_role_agent(agents_dir, name: str, *, body: str = "You are a test agent.\n"):
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(f"---\nmodel: sonnet\n---\n{body}")


@pytest.mark.parametrize("heading", ["platform-coder", "default"])
def test_a_placeholder_persona_with_a_resolvable_role_takes_the_rosters_body(
        agentctl, grant_library, tmp_path, heading):
    """Fails against the unfixed code: there, `agent["body"]` is the Library's
    placeholder sentence itself, no matter what `heading` the Library used to
    build it or whether the dispatched role resolves."""
    agents_dir = tmp_path / "agents"
    _write_role_agent(agents_dir, "platform-coder", body="You are platform-coder.\n")
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(persona_md=_librarys_placeholder_persona_md(heading)))

    agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="platform-coder",
        agents_dir=str(agents_dir))

    assert agent["body"] == "You are platform-coder.\n"
    assert agent["persona_source"] == "roster"
    # the lease taken to read the grant is still released, same as every
    # successful checkout
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_real_persona_content_keeps_it_and_records_grant(agentctl, grant_library):
    """The twin of the test above: persona content that is NOT the Library's
    placeholder is used unchanged, and `persona_source` names the grant as its
    origin, not the roster."""
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(persona_md="# the persona\n\nreal body, not a placeholder."))

    agent = agentctl.resolve_agent_from_grant("issue_test", repo="agent-platform")

    assert agent["body"] == "# the persona\n\nreal body, not a placeholder."
    assert agent["persona_source"] == "grant"


@pytest.mark.parametrize("agent_name", [None, "no-such-role"],
                         ids=["no_role_named", "role_does_not_resolve"])
def test_a_placeholder_persona_with_no_resolvable_role_refuses_and_releases_the_lease(
        agentctl, grant_library, tmp_path, agent_name):
    """Fails against the unfixed code: there, a placeholder `persona_md` is
    read as valid persona content no matter what, so this would succeed with
    the placeholder sentence as `agent["body"]` instead of refusing."""
    agents_dir = tmp_path / "empty-agents"
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(persona_md=_librarys_placeholder_persona_md("default")))

    with pytest.raises(SystemExit, match="issue_test.*placeholder"):
        agentctl.resolve_agent_from_grant(
            "issue_test", repo="agent-platform", agent_name=agent_name,
            agents_dir=str(agents_dir))

    # the lease taken to read the grant is released on this refusal too
    assert grant_library.release_log == [{"issue_id": "issue_test"}]


def test_a_placeholder_persona_with_the_same_grant_and_a_resolvable_role_dispatches(
        agentctl, grant_library, tmp_path):
    """The twin of the refusal above, same grant: a role that DOES resolve
    reaches a materialized agent rather than the refusal."""
    agents_dir = tmp_path / "agents"
    _write_role_agent(agents_dir, "platform-coder", body="You are platform-coder.\n")
    grant_library.checkout_responses["issue_test"] = (
        200, _grant(persona_md=_librarys_placeholder_persona_md("default")))

    agent = agentctl.resolve_agent_from_grant(
        "issue_test", repo="agent-platform", agent_name="platform-coder",
        agents_dir=str(agents_dir))

    assert agent["body"] == "You are platform-coder.\n"
    assert agent["persona_source"] == "roster"
