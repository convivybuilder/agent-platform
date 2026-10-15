"""Unit tests for lib/agent-platform/run_verdict.py: the refusals `mcp-verdict-server` applies
before a dispatched reviewer's verdict is posted (knowledge#793).

The live check in `tests/live/test_reviewer_verdict_path.py` runs a real sandboxed
reviewer through the server and checks the record that lands on a stand-in listener.
These tests pin each refusal the server makes, and the arguments the posting passes on.
"""

import json
import subprocess
import types
import pytest

import projects
import run_verdict as RV

COMMIT = "f2afe367" + "0" * 32
RUN = "run-verdict-test"


def _run_dir(runs_root, *, agent="reviewer", pr=99999, repo="agent-platform",
             review_sha=COMMIT, run_id=RUN):
    d = runs_root / "2026-10-06" / run_id
    d.mkdir(parents=True)
    meta = {"run_id": run_id, "agent": agent, "repo": repo, "pr": pr, "review_sha": review_sha,
            "task": "t", "started_at": "2026-10-06T00:00:00Z", "worktree": "/wt",
            "profile": "p", "model": "x"}
    (d / "meta.json").write_text(json.dumps(meta))
    return d


@pytest.fixture
def runs_root(tmp_path):
    root = tmp_path / "agent-runs"
    root.mkdir()
    return root


def _refusal(run_id, runs_root, **kw):
    with pytest.raises(RV.Refused) as e:
        RV.target_for(run_id, runs_root=runs_root, **kw)
    return str(e.value)


def test_a_reviewer_dispatched_with_a_pr_is_pinned_to_its_pr_repo_and_commit(runs_root):
    _run_dir(runs_root)
    t = RV.target_for(RUN, runs_root=runs_root)
    assert (t.pr, t.repo, t.commit, t.agent) == ("99999", "convivy/agent-platform", COMMIT, "reviewer")


def test_a_security_reviewer_and_a_crew_variant_are_review_class(runs_root):
    _run_dir(runs_root, agent="svc-security-reviewer")
    assert RV.target_for(RUN, runs_root=runs_root).agent == "svc-security-reviewer"


@pytest.mark.parametrize("agent", ["platform-coder", "scout", "bug-fixer", "platform-architect"])
def test_a_coder_or_any_other_role_is_refused_before_anything_is_read(runs_root, agent):
    _run_dir(runs_root, agent=agent)
    assert "is not a reviewer" in _refusal(RUN, runs_root)


def test_a_run_not_dispatched_with_pr_is_refused(runs_root):
    _run_dir(runs_root, pr=None)
    assert "not dispatched with --pr" in _refusal(RUN, runs_root)


def test_a_run_whose_record_pins_no_full_commit_is_refused(runs_root):
    _run_dir(runs_root, review_sha="")
    assert "pins no full review commit" in _refusal(RUN, runs_root)


def test_an_unknown_run_is_refused(runs_root):
    assert "no readable run record" in _refusal("run-nope", runs_root)


def test_a_call_naming_another_pr_is_refused(runs_root):
    _run_dir(runs_root)
    assert "pinned to PR #99999" in _refusal(RUN, runs_root, pr="12345")


def test_a_call_naming_the_same_pr_is_accepted(runs_root):
    _run_dir(runs_root)
    assert RV.target_for(RUN, runs_root=runs_root, pr="99999").pr == "99999"


def test_a_call_naming_another_repository_is_refused_and_a_bare_or_owned_name_is_matched(runs_root):
    _run_dir(runs_root)
    assert "pinned to convivy/agent-platform" in _refusal(RUN, runs_root, repo="constance")
    assert "pinned to convivy/agent-platform" in _refusal(RUN, runs_root, repo="convivy/constance")
    assert RV.target_for(RUN, runs_root=runs_root, repo="convivy/agent-platform").repo == \
        "convivy/agent-platform"
    assert RV.target_for(RUN, runs_root=runs_root, repo="agent-platform").repo == \
        "convivy/agent-platform"


def test_a_repo_with_a_registered_non_convivy_owner_is_slugged_correctly(runs_root):
    """Before homebrew-tap's `repo_owners` entry existed, this read
    convivy/homebrew-tap, the wrong owner."""
    _run_dir(runs_root, repo="homebrew-tap")
    assert RV.target_for(RUN, runs_root=runs_root).repo == "convivybuilder/homebrew-tap"


def test_an_nyh_repo_is_slugged_under_its_units_github_owner(runs_root):
    """Round-1 review (PR #881, comment 6150937461): before this call routed
    through `projects.repo_github_slug`, it built `convivy/nyh-arch-tools`
    unconditionally, ignoring the unit's own github_owner — a 404ing slug for
    a merge-gate `_head_advanced_beyond_pin` or stamp-posting call against an
    NYH repo."""
    owner = projects.get("nyh-arch-tools")["github_owner"]
    assert owner and owner != "convivy"
    _run_dir(runs_root, repo="nyh-arch-tools")
    assert RV.target_for(RUN, runs_root=runs_root).repo == f"{owner}/nyh-arch-tools"


def test_a_call_naming_another_commit_is_refused(runs_root):
    _run_dir(runs_root)
    assert "pinned to commit" in _refusal(RUN, runs_root, reviewed_commit="deadbeef" + "1" * 32)


def test_a_call_naming_a_prefix_of_the_pinned_commit_is_accepted_at_seven_or_more(runs_root):
    _run_dir(runs_root)
    assert RV.target_for(RUN, runs_root=runs_root, reviewed_commit=COMMIT[:7]).commit == COMMIT
    # Six characters is too short to name a commit, so it is refused as a different one.
    assert "pinned to commit" in _refusal(RUN, runs_root, reviewed_commit=COMMIT[:6])


def test_the_posting_pins_review_post_to_the_runs_own_commit_and_identity(runs_root, tmp_path):
    _run_dir(runs_root)
    target = RV.target_for(RUN, runs_root=runs_root)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="posted", stderr="")

    done = RV.post(target, "APPROVE", "## review\nlooks right", agentctl=tmp_path / "agentctl",
                   runner=fake_run)
    assert done.returncode == 0
    cmd, kw = calls[0]
    assert cmd[0] == str(tmp_path / "agentctl") and cmd[1] == "review-post"
    assert cmd[cmd.index("--pr") + 1] == "99999"
    assert cmd[cmd.index("--verdict") + 1] == "APPROVE"
    assert cmd[cmd.index("--agent") + 1] == "reviewer"
    assert cmd[cmd.index("--repo") + 1] == "convivy/agent-platform"
    # review-post reads the pin from its environment, so the pin is what it stamps.
    assert kw["env"]["AGENTCTL_RUN_ID"] == RUN
    assert kw["env"]["AGENTCTL_REVIEW_SHA"] == COMMIT
    # The body travels as a file in a private directory, which the call deletes afterwards.
    body_path = cmd[cmd.index("--body-file") + 1]
    assert "verdict-" in body_path


def test_the_posting_environment_carries_no_run_directory_or_credential(runs_root, tmp_path,
                                                                        monkeypatch):
    _run_dir(runs_root)
    monkeypatch.setenv("AGENT_SIGNAL_TOKEN", "must-not-travel")
    monkeypatch.setenv("AGENTCTL_RUN_DIR", "/elsewhere")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    RV.post(RV.target_for(RUN, runs_root=runs_root), "REQUEST_CHANGES", "body",
            agentctl=tmp_path / "agentctl", runner=fake_run)
    assert set(seen) <= {"PATH", "HOME", "AGENTCTL_RUN_ID", "AGENTCTL_REVIEW_SHA",
                         RV.RECORD_URL_ENV}
    assert "AGENT_SIGNAL_TOKEN" not in seen


def test_the_stand_in_address_reaches_the_posting_only_when_set(runs_root, tmp_path, monkeypatch):
    _run_dir(runs_root)
    monkeypatch.setenv(RV.RECORD_URL_ENV, "http://127.0.0.1:55555")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    RV.post(RV.target_for(RUN, runs_root=runs_root), "REQUEST_CHANGES", "body",
            agentctl=tmp_path / "agentctl", runner=fake_run)
    assert seen[RV.RECORD_URL_ENV] == "http://127.0.0.1:55555"


@pytest.mark.parametrize("verdict,body", [("LGTM", "x"), ("APPROVE", "   \n")])
def test_a_bad_verdict_or_an_empty_body_is_refused_before_posting(runs_root, tmp_path, verdict, body):
    _run_dir(runs_root)
    target = RV.target_for(RUN, runs_root=runs_root)

    def never(cmd, **kw):
        raise AssertionError("review-post must not run")

    with pytest.raises(RV.Refused):
        RV.post(target, verdict, body, agentctl=tmp_path / "agentctl", runner=never)


def test_the_mcp_config_names_the_server_and_carries_the_run_id_in_its_env(tmp_path, monkeypatch):
    # CQ·418: `mcp_config` builds its own env from scratch — it never passes through
    # `dispatch_env()` — so a reviewer's verdict-server launch must carry neither
    # GH_TOKEN/GITHUB_TOKEN nor any of the git-credential redirect vars that function
    # now sets. Pinned exhaustively (the full key set, not just AGENTCTL_RUN_ID) so a
    # future change that folds this config through dispatch_env would fail here.
    monkeypatch.delenv(RV.RECORD_URL_ENV, raising=False)
    server = tmp_path / "mcp-verdict-server"
    cfg = json.loads(RV.mcp_config(RUN, server))
    entry = cfg["mcpServers"][RV.SERVER_NAME]
    assert entry["command"] == str(server)
    assert entry["env"]["AGENTCTL_RUN_ID"] == RUN
    assert entry["type"] == "stdio"
    assert set(entry["env"]) == {"AGENTCTL_RUN_ID"}
    assert "GH_TOKEN" not in entry["env"] and "GITHUB_TOKEN" not in entry["env"]


def test_review_post_refuses_a_record_seam_that_is_not_loopback(agentctl, monkeypatch):
    for bad in ("https://example.com:8100", "http://127.0.0.1", "http://10.0.0.5:8100",
                "http://127.0.0.1:8100/path", "http://user:pw@127.0.0.1:8100"):
        monkeypatch.setenv(RV.RECORD_URL_ENV, bad)
        base, err = agentctl._review_record_base()
        assert base is None and "must be http://127.0.0.1:<port>" in err, bad


def test_review_post_takes_the_loopback_seam_and_the_fixed_address_when_unset(agentctl, monkeypatch):
    monkeypatch.setenv(RV.RECORD_URL_ENV, "http://127.0.0.1:55555")
    assert agentctl._review_record_base() == ("http://127.0.0.1:55555", "")
    monkeypatch.delenv(RV.RECORD_URL_ENV)
    assert agentctl._review_record_base() == (None, "")


def test_the_server_returns_a_refusal_string_rather_than_raising(mcp_verdict_server, monkeypatch):
    # The tool body: an unreadable run comes back as text for the run, not as an exception.
    monkeypatch.setattr(RV.RP, "resolve_run_facts",
                        lambda run_id, runs_root=None: types.SimpleNamespace(found=False))
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-nope")
    assert mcp_verdict_server.review_post(verdict="APPROVE", body="x").startswith("refused: ")


def test_the_stand_in_record_post_sends_the_token_to_the_listener_and_returns_its_record(
        agentctl, tmp_path, monkeypatch):
    # The stand-in path review-post takes when RECORD_URL_ENV is set (agentctl's
    # `_post_review_record_to`). It is what the live check drives, so it is pinned here too.
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = {}

    class Listener(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            seen["body"] = _json.loads(self.rfile.read(size))
            seen["auth"] = self.headers.get("Authorization")
            seen["path"] = self.path
            out = b'{"id": "rr-1"}'
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Listener)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = tmp_path / "agent-platform-token"
        token.write_text("tok-from-test\n")
        monkeypatch.setattr(agentctl.SC, "TOKEN_FILE", token)
        out = agentctl._post_review_record_to(f"http://127.0.0.1:{server.server_address[1]}", {"pr": 1})
    finally:
        server.shutdown()
        server.server_close()
    assert out == {"status": "ok", "record": {"id": "rr-1"}}
    assert seen == {"body": {"pr": 1}, "auth": "Bearer tok-from-test", "path": "/review-records"}


def test_the_stand_in_record_post_sends_nothing_without_a_token(agentctl, tmp_path, monkeypatch):
    # The module agentctl reads its token from. A patch on another import of the module would
    # leave the real operator token file in place.
    monkeypatch.setattr(agentctl.SC, "TOKEN_FILE", tmp_path / "absent")
    out = agentctl._post_review_record_to("http://127.0.0.1:9", {"pr": 1})
    assert out["status"] == "skip"
