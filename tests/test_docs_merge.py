"""`agentctl docs merge` (issue_01M3SGQ4QTQ8FB0DN0X9G54ZXP): an agent merge of
a reviewed documentation PR, once the adopter's policy permits it, in any
repository of the install — not `kb_merge`'s knowledge repository alone.

`refusals()` is the whole control, so each condition is shown tripping on its
own against an otherwise admissible PR: a test that only checks the happy path
would pass with every condition deleted.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import agent_policy as AP
import docs_merge as DM
import kb_merge as KM
import review_protocol as RP

HEAD = "a" * 40
REPOS = ("acme/docs-repo",)


def _policy_text(documents=True, rounds=5):
    return (f"policy: platform/agent-policy\nschema_version: 1\n"
            f"agents_may_merge_knowledge: false\n"
            f"agents_may_merge_knowledge_instructions: false\n"
            f"agents_may_merge_documents: {str(documents).lower()}\n"
            f"review_rounds_per_role: {rounds}\nmax_concurrent: 16\n"
            f"max_workers_per_team: 4\nmax_live_supervisors: 4\n")


def _policy(**kw):
    return AP.load(_policy_text(**kw))


def _change(path="docs/a.md", text="one\n"):
    """A plain addition of a regular document file."""
    return KM.Change(None, None, None, path, "100644", text)


def _stamp(agent, run_id, commit=HEAD, verdict="APPROVE"):
    return RP.ReviewStamp(version="1", pr="7", repo="acme/docs-repo", reviewed_commit=commit,
                          verdict=verdict, agent=agent, run_id=run_id)


def _facts(**over):
    base = dict(
        repo="acme/docs-repo", number=7, state="OPEN", head_at_diff=HEAD,
        head_at_review=HEAD, base_ref="main", cross_repo=False, review_ok=True,
        review_reasons=[],
        stamps=[_stamp("reviewer", "run-00000001"), _stamp("security-reviewer", "run-00000002")],
        policy=_policy(), changes=[_change()])
    base.update(over)
    return DM.Facts(**base)


def _config(**over):
    base = dict(repos=REPOS, owner="acme", app_id="1", key_path=None)
    base.update(over)
    return DM.DocsMergeConfig(**base)


# --- refusals(): the admissible case and each condition alone ----------------


def test_an_admissible_pr_is_admitted():
    assert DM.refusals(_facts(), _config()) == []


@pytest.mark.parametrize("over, expect", [
    (dict(state="MERGED"), "not open"),
    (dict(state="CLOSED"), "not open"),
    (dict(base_ref="release"), "not 'main'"),
    (dict(cross_repo=True), "comes from a fork"),
    (dict(head_at_review="b" * 40), "head moved"),
    (dict(read_error="boom"), "PR could not be read"),
    (dict(git_problem="could not fetch (offline)"), "changed files could not be read"),
    (dict(changes=None), "changed files could not be read"),
    (dict(review_ok=False, review_reasons=["no review posted by reviewer"]),
     "review-status: no review posted by reviewer"),
    (dict(review_ok=False, review_reasons=[]), "review-status` is not PASS"),
])
def test_each_condition_refuses_on_its_own(over, expect):
    why = DM.refusals(_facts(**over), _config())
    assert any(expect in w for w in why), why


def test_the_policy_false_missing_and_malformed_each_refuse_naming_the_key():
    why = DM.refusals(_facts(policy=_policy(documents=False)), _config())
    assert why == ["the policy sets agents_may_merge_documents to false"]

    why = DM.refusals(_facts(policy=None), _config())
    assert why == ["the agent policy was not read"]

    fb = AP.Fallback("the policy file is missing at origin/main")
    why = DM.refusals(_facts(policy=fb), _config())
    assert len(why) == 1 and "agent policy fell back" in why[0] and "missing" in why[0], why

    fb2 = AP.Fallback("line 3 is not exactly `key: value`")
    why = DM.refusals(_facts(policy=fb2), _config())
    assert len(why) == 1 and "not exactly `key: value`" in why[0], why


def test_a_repository_not_in_the_installs_configured_set_refuses():
    why = DM.refusals(_facts(repo="acme/other"), _config())
    assert len(why) == 1 and "is not one of this install's documents-mergeable" in why[0]
    assert "acme/docs-repo" in why[0], why


def test_missing_merge_identity_configuration_refuses():
    why = DM.refusals(_facts(), None, "docs-merge-app.json does not exist")
    assert why == ["the merge identity could not be read: docs-merge-app.json does not exist"]


def test_missing_config_still_runs_the_rest_of_the_checks():
    why = DM.refusals(_facts(state="MERGED"), None, "docs-merge-app.json does not exist")
    assert any("merge identity could not be read" in w for w in why), why
    assert any("not open" in w for w in why), why


# --- file classification: a plain doc passes, everything else refuses naming -


def test_a_code_file_change_refuses_naming_that_file():
    """A non-.md file is an instruction under `agent_policy.classify`'s own
    rules (it "does not end in .md"), so this refuses via the instruction
    branch rather than the plain "not a document" one — either way, named."""
    why = DM.refusals(_facts(changes=[_change(path="lib/x.py", text="print(1)\n")]), _config())
    assert len(why) == 1 and "lib/x.py" in why[0] and "is an instruction" in why[0], why


def test_a_docs_only_pr_with_several_files_is_admitted():
    changes = [_change(path="docs/a.md"), _change(path="docs/sub/b.md", text="two\n")]
    assert DM.refusals(_facts(changes=changes), _config()) == []


def test_a_markdown_file_outside_docs_refuses_naming_that_file():
    """Ends in .md and carries no front matter, so it is NOT an instruction —
    this exercises the plain "not a document" branch (outside a top-level
    docs/ directory), distinct from the instruction branch above."""
    why = DM.refusals(_facts(changes=[_change(path="README.md", text="hello\n")]), _config())
    assert why == ["README.md is not a document: agents merge only a file inside a "
                   "top-level docs/ directory with no front matter"]


def test_a_deleted_file_outside_docs_refuses_on_the_base_side():
    deleted = KM.Change("lib/old.py", "100644", "print(1)\n", None, None, None)
    why = DM.refusals(_facts(changes=[deleted]), _config())
    assert len(why) == 1 and "lib/old.py" in why[0] and "is an instruction" in why[0], why


def test_a_rename_from_outside_docs_into_docs_refuses():
    renamed = KM.Change("lib/old.md", "100644", "one\n", "docs/new.md", "100644", "one\n")
    why = DM.refusals(_facts(changes=[renamed]), _config())
    assert len(why) == 1 and "lib/old.md -> docs/new.md" in why[0], why


def test_a_docs_md_file_with_front_matter_refuses():
    text = "---\nkind: reference\n---\nbody\n"
    why = DM.refusals(_facts(changes=[_change(text=text)]), _config())
    assert len(why) == 1 and "docs/a.md" in why[0] and "not a document" in why[0], why


def test_an_instruction_file_under_docs_refuses():
    text = "---\nkind: agent\ntitle: t\n---\nbody\n"
    why = DM.refusals(_facts(changes=[_change(path="docs/agents/x.md", text=text)]), _config())
    assert len(why) == 1 and "docs/agents/x.md" in why[0] and "is an instruction" in why[0], why


def test_an_unclassifiable_file_refuses_naming_it():
    unreadable = KM.Change(None, None, None, "docs/binary.md", "100644", None)
    why = DM.refusals(_facts(changes=[unreadable]), _config())
    assert len(why) == 1 and "docs/binary.md" in why[0] and "cannot be classified" in why[0], why


# --- review-status PASS and the rounds-per-role cap ---------------------------


def test_review_status_not_pass_refuses():
    why = DM.refusals(_facts(review_ok=False, review_reasons=["security-reviewer latest "
                                                              "verdict is REQUEST_CHANGES, "
                                                              "not APPROVE"]), _config())
    assert any("REQUEST_CHANGES" in w for w in why), why


def test_a_sixth_round_refuses_and_the_fifth_does_not():
    five = [_stamp("reviewer", f"run-0000000{i}") for i in range(1, 6)]
    six = five + [_stamp("reviewer", "run-00000009")]
    sec = [_stamp("security-reviewer", "run-000000aa")]
    assert DM.refusals(_facts(stamps=five + sec), _config()) == []
    why = DM.refusals(_facts(stamps=six + sec), _config())
    assert len(why) == 1 and "reviewer ran 6 rounds" in why[0], why


def test_the_round_cap_is_the_policys_number_not_five():
    def rounds(n):
        return [_stamp("reviewer", f"run-r{i:07d}") for i in range(n)] + [
            _stamp("security-reviewer", "run-000000aa")]
    three = _policy(rounds=3)
    assert DM.refusals(_facts(policy=three, stamps=rounds(3)), _config()) == []
    why = DM.refusals(_facts(policy=three, stamps=rounds(4)), _config())
    assert len(why) == 1 and "reviewer ran 4 rounds, past the cap of 3" in why[0], why


def test_a_fallback_policy_caps_rounds_at_the_files_named_five():
    fb = AP.Fallback("the policy file is missing at origin/main")
    six = [_stamp("reviewer", f"run-r{i:07d}") for i in range(6)]
    why = DM.refusals(_facts(policy=fb, stamps=six + [_stamp("security-reviewer",
                                                             "run-000000aa")]), _config())
    assert any("past the cap of 5" in w for w in why), why


def test_a_stamp_with_no_run_id_makes_the_round_count_unreadable():
    stamps = [_stamp("reviewer", ""), _stamp("security-reviewer", "run-00000002")]
    why = DM.refusals(_facts(stamps=stamps), _config())
    assert any("round count cannot be read" in w for w in why), why


# --- head-moved between the two checks ----------------------------------------


def test_a_head_that_moves_between_the_file_check_and_the_review_check_refuses():
    why = DM.refusals(_facts(head_at_diff=HEAD, head_at_review="c" * 40), _config())
    assert len(why) == 1 and "head moved" in why[0]
    assert HEAD[:12] in why[0] and ("c" * 12) in why[0], why


def test_an_unmoved_head_does_not_refuse():
    assert DM.refusals(_facts(head_at_diff=HEAD, head_at_review=HEAD), _config()) == []


# --- several failures at once --------------------------------------------------


def test_every_failing_condition_is_reported_not_just_the_first():
    why = DM.refusals(
        _facts(state="CLOSED", policy=_policy(documents=False),
              changes=[_change(path="lib/x.py", text="print(1)\n")],
              review_ok=False, review_reasons=["no review posted by reviewer"]),
        None, "docs-merge-app.json does not exist")
    assert len(why) == 5, why
    joined = "\n".join(why)
    assert "merge identity could not be read" in joined
    assert "not open" in joined
    assert "agents_may_merge_documents to false" in joined
    assert "lib/x.py" in joined
    assert "review-status: no review posted by reviewer" in joined


# --- docs_merge.py's own configuration, docs-merge-app.json -------------------


_OMIT = object()


def _write_config(path, **over):
    data = {"repos": ["acme/docs-repo", "acme/other-repo"], "app_id": "555"}
    for k, v in over.items():
        if v is _OMIT:
            data.pop(k, None)
        else:
            data[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_a_missing_config_file_refuses_naming_the_file(tmp_path):
    path = tmp_path / "docs-merge-app.json"
    with pytest.raises(DM.Unreadable) as e:
        DM.load_config(path)
    assert str(e.value) == f"{path} does not exist"


def test_unreadable_json_refuses_naming_the_file(tmp_path):
    path = tmp_path / "docs-merge-app.json"
    path.write_text("{not json")
    with pytest.raises(DM.Unreadable) as e:
        DM.load_config(path)
    assert str(e.value).startswith(f"{path} is not valid JSON:")


def test_a_json_document_that_is_not_an_object_refuses_naming_the_file(tmp_path):
    path = tmp_path / "docs-merge-app.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(DM.Unreadable) as e:
        DM.load_config(path)
    assert str(e.value) == f"{path} does not hold a JSON object"


@pytest.mark.parametrize("field, bad", [
    ("repos", _OMIT), ("repos", "acme/docs-repo"), ("repos", []),
    ("repos", ["acme/docs-repo", ""]), ("repos", ["no-slash"]),
    ("repos", ["too/many/slashes"]), ("repos", ["/name"]), ("repos", ["owner/"]),
    ("app_id", _OMIT), ("app_id", 555), ("app_id", ""),
    ("key_path", 7), ("key_path", ""),
])
def test_a_missing_or_wrongly_typed_field_refuses_naming_the_file_and_field(
        tmp_path, field, bad):
    path = _write_config(tmp_path / "docs-merge-app.json", **{field: bad})
    with pytest.raises(DM.Unreadable) as e:
        DM.load_config(path)
    assert str(e.value).startswith(str(path)) and field in str(e.value), e.value


def test_repos_naming_more_than_one_owner_refuses(tmp_path):
    path = _write_config(tmp_path / "docs-merge-app.json", repos=["acme/x", "other-org/y"])
    with pytest.raises(DM.Unreadable) as e:
        DM.load_config(path)
    assert "more than one owner" in str(e.value)


def test_key_path_is_optional_and_defaults_beside_the_file(tmp_path):
    path = _write_config(tmp_path / "sub" / "docs-merge-app.json", key_path=_OMIT)
    cfg = DM.load_config(path)
    assert cfg.key_path == path.parent / "docs-merge-app.pem"


def test_an_explicit_key_path_is_honoured(tmp_path):
    where = tmp_path / "elsewhere" / "key.pem"
    path = _write_config(tmp_path / "docs-merge-app.json", key_path=str(where))
    assert DM.load_config(path).key_path == where


def test_owner_is_derived_from_repos_not_a_second_field(tmp_path):
    path = _write_config(tmp_path / "docs-merge-app.json", repos=["acme/x", "acme/y"])
    cfg = DM.load_config(path)
    assert cfg.repos == ("acme/x", "acme/y") and cfg.owner == "acme"


def test_repo_root_uses_the_worktrees_layout(monkeypatch, tmp_path):
    import builder_root as BR
    monkeypatch.setattr(BR, "root", lambda: tmp_path)
    assert DM.repo_root("acme/docs-repo") == tmp_path / "docs-repo"


@pytest.mark.parametrize("repo, ok", [
    ("acme/docs-repo", True),
    ("acme-org/name.ext", True),
    ("docs-repo", False),       # no slash
    ("acme/docs/repo", False),  # two slashes
    ("/docs-repo", False),      # empty owner
    ("acme/", False),           # empty name
    ("acme/.", False),          # name is `.`
    ("acme/..", False),         # name is `..` — the path-traversal case repo_root() is built from
])
def test_valid_repo_shape(repo, ok):
    assert DM.valid_repo_shape(repo) is ok


# --- the credential: installation_for, merge_token, merge_pr ------------------
#
# Mirrors kb_merge.py's own tests for its `knowledge_installation`,
# `installation_token` and `merge_pr` twins (tests/test_kb_merge.py).


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _routes(table):
    """An opener answering each (method, url fragment) with a canned (status,
    payload), recording every request as (method, url, auth, json body)."""
    seen = []

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                     json.loads(req.data) if req.data else None))
        for (method, frag), resp in table.items():
            if req.get_method() == method and frag in req.full_url:
                return _Resp(*resp)
        raise AssertionError(f"unexpected {req.get_method()} {req.full_url}")
    return opener, seen


ONE_INSTALL = [{"id": 161212648, "account": {"login": "acme"}}]
ONE_REPO_LISTING = {"total_count": 1, "repositories": [{"full_name": "acme/docs-repo"}]}
TWO_REPOS = DM.DocsMergeConfig(repos=("acme/docs-repo", "acme/other-repo"),
                               owner="acme", app_id="1", key_path=None)
TWO_REPOS_LISTING = {"total_count": 2, "repositories": [{"full_name": "acme/docs-repo"},
                                                        {"full_name": "acme/other-repo"}]}


def _installation_table(installs=ONE_INSTALL, repos=ONE_REPO_LISTING):
    return {("GET", "/app/installations"): (200, installs),
            ("POST", "/access_tokens"): (201, {"token": "ghs_listing"}),
            ("GET", "/installation/repositories"): (200, repos),
            ("DELETE", "/installation/token"): (204, {})}


def test_the_installation_is_derived_and_checked_to_reach_exactly_the_configured_set():
    opener, seen = _routes(_installation_table())
    assert DM.installation_for("jwt.x.y", _config(), opener=opener) == "161212648"
    paths = [(m, u.split("api.github.com")[1].split("?")[0]) for m, u, _, _ in seen]
    assert paths == [("GET", "/app/installations"),
                     ("POST", "/app/installations/161212648/access_tokens"),
                     ("GET", "/installation/repositories"),
                     ("DELETE", "/installation/token")]
    # The listing token is minted UNSCOPED, or it could only ever see the
    # configured repos and would show nothing about what else the
    # installation reaches.
    assert seen[1][3] is None
    assert seen[2][2] == "Bearer ghs_listing"
    # And it is that token, not the JWT, that is revoked, straight after listing.
    assert seen[3][2] == "Bearer ghs_listing"


def test_the_listing_token_is_revoked_even_when_the_listing_fails():
    """Every permission the installation holds, in a token nobody will use again:
    a refused or failed listing must not leave it live for the hour."""
    table = _installation_table()
    table[("GET", "/installation/repositories")] = (403, {"message": "nope"})
    opener, seen = _routes(table)
    with pytest.raises(DM.Unreadable):
        DM.installation_for("jwt.x.y", _config(), opener=opener)
    assert [(m, a) for m, _, a, _ in seen][-1] == ("DELETE", "Bearer ghs_listing")


def test_a_revocation_that_fails_does_not_fail_the_check():
    """The token lives in this process's memory and expires within the hour, so
    a refused or unreachable revoke is no reason to refuse the merge."""
    table = _installation_table()
    table[("DELETE", "/installation/token")] = (401, {"message": "Bad credentials"})
    opener, _ = _routes(table)
    assert DM.installation_for("jwt.x.y", _config(), opener=opener) == "161212648"

    def down_on_delete(req, timeout=None):
        if req.get_method() == "DELETE":
            raise urllib.error.URLError("nodename nor servname provided")
        return _routes(_installation_table())[0](req, timeout)
    assert DM.installation_for("jwt.x.y", _config(), opener=down_on_delete) == "161212648"


@pytest.mark.parametrize("installs, repos, named", [
    (ONE_INSTALL * 2, TWO_REPOS_LISTING, "count"),
    ([], TWO_REPOS_LISTING, "count"),
    ([{"id": 1, "account": {"login": "someone-else"}}], TWO_REPOS_LISTING, "owner"),
    (ONE_INSTALL, {"total_count": 3, "repositories": [{"full_name": "acme/docs-repo"},
                                                      {"full_name": "acme/other-repo"},
                                                      {"full_name": "acme/extra"}]},
     "repo list"),  # reaches MORE than config.repos
    (ONE_INSTALL, ONE_REPO_LISTING, "repo list"),  # reaches FEWER than config.repos
])
def test_a_widened_installation_is_refused_by_name(installs, repos, named):
    """A widened (or narrowed) installation must read as what it is, not as a
    token error — reaching more or fewer repos than config.repos both refuse
    by name rather than being granted."""
    opener, _ = _routes(_installation_table(installs, repos))
    with pytest.raises(DM.Unreadable) as e:
        DM.installation_for("jwt.x.y", TWO_REPOS, opener=opener)
    assert str(e.value).startswith(named + ":"), e.value


def test_the_merge_token_is_scoped_to_one_repo_and_two_permissions():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(),
                    auth=req.get_header("Authorization"), body=json.loads(req.data))
        return _Resp(201, {"token": "ghs_x"})

    assert DM.merge_token("jwt.x.y", "161212648", "acme/docs-repo", opener=opener) == "ghs_x"
    assert seen["url"].endswith("/app/installations/161212648/access_tokens")
    assert seen["method"] == "POST" and seen["auth"] == "Bearer jwt.x.y"
    assert seen["body"] == {"repositories": ["docs-repo"],
                            "permissions": {"contents": "write", "pull_requests": "write"}}


def test_the_merge_token_narrows_to_the_one_repo_being_merged():
    """The installation may reach every configured repo; the token minted for
    one merge call reaches only the repo that call is about, not the
    installation's full configured set."""
    seen = []

    def spy(req, timeout=None):
        seen.append(json.loads(req.data)["repositories"])
        return _Resp(201, {"token": "ghs_x"})

    DM.merge_token("j", "1", "acme/docs-repo", opener=spy)
    DM.merge_token("j", "1", "acme/other-repo", opener=spy)
    assert seen == [["docs-repo"], ["other-repo"]]


def test_the_merge_pins_the_checked_head_and_uses_a_merge_commit():
    seen = {}

    def opener(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), body=json.loads(req.data))
        return _Resp(200, {"merged": True, "sha": "f" * 40})

    ok, msg = DM.merge_pr(7, HEAD, "ghs_x", "acme/docs-repo", opener=opener)
    assert ok and msg == "merged as " + "f" * 12
    assert seen["url"].endswith("/repos/acme/docs-repo/pulls/7/merge")
    assert seen["method"] == "PUT"
    assert seen["body"] == {"sha": HEAD, "merge_method": "merge"}


def test_a_merge_github_refuses_is_reported_as_refused():
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"message": "Head branch was modified."}'))

    ok, msg = DM.merge_pr(7, HEAD, "ghs_x", "acme/docs-repo", opener=opener)
    assert not ok and "409" in msg and "Head branch was modified" in msg


# --- the review floor never reaches docs_merge's cap ----------------------------------
#
# docs_merge reads only `review_rounds_per_role`. That is right only while every file the
# review floor places, positively or as a tie-break, is refused by `refusals()` before the
# cap is read, so these pin the claim, one fixture per floor reason: each is refused here as
# an instruction or a non-document. The symlink and the control character are tie-breaks for
# the cap, and `classify` still refuses them. The decision under `decisions/` names the case
# that is refused as a non-document, because its front matter makes `classify` report it as
# not a document.

FLOOR_FILES = [
    KM.Change(None, None, None, "docs/agents/x.md", "100644", "instructions\n"),
    KM.Change(None, None, None, "docs/x.md", "100644", "---\nkind: runbook\ntitle: t\n---\nbody\n"),
    KM.Change(None, None, None, "docs/decisions/0001-x.md", "100644",
              "---\nkind: decision\naudience: [team, coder]\ntitle: t\n---\nbody\n"),
    KM.Change(None, None, None, "docs/x.txt", "100644", "plain\n"),
    KM.Change(None, None, None, "docs/CLAUDE.md", "100644", "plain\n"),
    KM.Change(None, None, None, "docs/link.md", "120000", "plain.md"),
    KM.Change(None, None, None, "docs/np.md", "100644", "hi\x07there\n"),
    KM.Change(None, None, None, "docs/decisions/0003-z.md", "100644",
              "---\nkind: note\naudience: [coder]\ntitle: t\n---\nbody\n"),
    # Front matter the reader refuses (a list item), with a raw floor `kind:` line.
    KM.Change(None, None, None, "docs/listed.md", "100644",
              "---\nkind: runbook\napplies_to:\n  - item\ntitle: t\n---\nbody\n"),
    # A decision whose audience names an entry on neither roster.
    KM.Change(None, None, None, "docs/decisions/0004-z.md", "100644",
              "---\nkind: decision\naudience: [team, zebra]\ntitle: t\n---\nbody\n"),
]


@pytest.mark.parametrize("change", FLOOR_FILES, ids=[
    "instruction-directory", "kind-runbook", "decision-naming-a-role", "not-markdown",
    "named-instruction-file", "symlink", "non-printable-character",
    "decisions-directory-of-another-kind-naming-a-role",
    "unreadable-front-matter-with-a-raw-floor-kind", "decision-naming-an-unknown-entry"])
def test_a_file_in_the_review_floor_is_refused_by_docs_merge(change):
    assert AP.review_floor_of([change]).floor
    why = DM.refusals(_facts(changes=[change]), _config())
    assert any(change.path_after in w and ("is an instruction" in w or "is not a document" in w)
               for w in why), why
