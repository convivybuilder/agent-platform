"""tools/merge-refusal-rehearsal — the merge-refusal rehearsal (E10 S11,
platform/specs/github-credential-seam, "The rehearsal", amended by
knowledge#1113).

No real key, no real GitHub call, no real git remote anywhere here: every
HTTP request goes through `FakeGitHub`, a fake `urlopen`-shaped opener that
never leaves this process, and every `git` invocation goes through
`FakeGit`, a stub that returns canned `subprocess.CompletedProcess` results.
Every owner and repository named below is synthetic (example-org,
example-person).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

import operator_config

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "merge-refusal-rehearsal"


def _load():
    loader = importlib.machinery.SourceFileLoader("merge_refusal_rehearsal", str(TOOL))
    spec = importlib.util.spec_from_loader("merge_refusal_rehearsal", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def rsa_key(tmp_path):
    """`github_app.mint`'s own `key_problem` refuses a missing key before any
    network call, same as `tests/test_github_mint.py`'s fixture of the same
    name — a throwaway key, never the real App's."""
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    key.chmod(0o600)
    return key


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    """`run_repository` always mints through `github_app.mint`'s own
    `rehearsal` guard, so every test here runs against a throwaway operator
    config naming the one synthetic owner (`example-org`) the fakes below
    probe — never this machine's real file (`CONVIVY_OPERATOR_CONFIG`,
    wired exactly as `tests/test_operator_config.py` does)."""
    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"rehearsal_repository_owners": ["example-org"]}))
    monkeypatch.setenv(operator_config.ENV_VAR, str(config_path))
    loaded = _load()
    monkeypatch.setattr(loaded.GA, "mint_log_path", lambda: tmp_path / "mint-log.jsonl")
    # `main()` resolves the install's own App through `GA.installed_app()` before
    # probing any owner — a synthetic App, never this machine's real one.
    monkeypatch.setattr(
        loaded.GA, "installed_app",
        lambda: loaded.GA.InstalledApp(app_id="1234567", key_path=Path("/dev/null"),
                                       slug="test-app"))
    return loaded


REAL_TOKEN = "ghs_should_never_appear_anywhere"


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


class FakeGitHub:
    """A fake opener for one repository's whole probe: the App's org
    installation, the access-token mint/revoke pair, and every REST call
    `run_repository` makes afterward. `self.merged` is flipped by a test's
    own `wait_for_operator` stub to simulate the operator's browser merge;
    `main_sha_sequence` is read once per call to `GET .../git/ref/heads/main`
    (before, after, and — only once step 6 runs — final), repeating its last
    element once exhausted."""

    def __init__(self, *, merge_status=404, merge_body=None, pr_number=7,
                main_sha_sequence=("a" * 40,), rulesets_payload=None):
        self.calls: list[dict] = []
        self.token = REAL_TOKEN
        self.merge_status = merge_status
        self.merge_body = merge_body if merge_body is not None else {"message": "Not Found"}
        self.pr_number = pr_number
        self._main_shas = list(main_sha_sequence)
        self._sha_calls = 0
        self.merged = False
        self.rulesets_payload = rulesets_payload if rulesets_payload is not None else []

    def _next_sha(self) -> str:
        idx = min(self._sha_calls, len(self._main_shas) - 1)
        self._sha_calls += 1
        return self._main_shas[idx]

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        self.calls.append({"method": method, "url": url, "body": body})
        if method == "GET" and url.endswith("/installation"):
            return _Resp(200, {"id": 1, "account": {"login": "example-org"}})
        if method == "POST" and url.endswith("/access_tokens"):
            return _Resp(201, {"token": self.token, "permissions": (body or {}).get("permissions"),
                               "repository_selection": "selected",
                               "expires_at": "2026-01-01T00:00:00Z"})
        if method == "DELETE" and url.endswith("/installation/token"):
            return _Resp(204, {})
        if method == "GET" and url.endswith("/rulesets?includes_parents=true"):
            return _Resp(200, self.rulesets_payload)
        if method == "GET" and "/rulesets/" in url:
            return _Resp(200, {"bypass_actors": []})
        if method == "POST" and url.endswith("/pulls"):
            return _Resp(201, {"number": self.pr_number})
        if method == "PUT" and url.endswith("/merge"):
            return _Resp(self.merge_status, self.merge_body)
        if method == "GET" and url.endswith(f"/pulls/{self.pr_number}"):
            return _Resp(200, {"merged": self.merged})
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _Resp(200, {"object": {"sha": self._next_sha()}})
        raise AssertionError(f"unstubbed request: {method} {url}")


class FakeGit:
    """A stub for `run_git`: `clone`/`checkout`/`commit` each succeed unless
    overridden; successive `push` calls are answered in order from
    `push_results` (branch push, then the straight-to-main push)."""

    def __init__(self, *, clone_rc=0, checkout_rc=0, commit_rc=0,
                push_results=((0, ""), (0, ""))):
        self.calls: list[list[str]] = []
        self.rc = {"clone": clone_rc, "checkout": checkout_rc, "commit": commit_rc}
        self._push_results = list(push_results)
        self._push_index = 0

    def __call__(self, args, cwd=None, env=None, **kw):
        self.calls.append(list(args))
        verb = next((a for a in args if a in ("clone", "checkout", "commit", "push", "add")), None)
        if verb == "push":
            rc, err = self._push_results[self._push_index]
            self._push_index += 1
            return subprocess.CompletedProcess(args, rc, stdout="", stderr=err)
        rc = self.rc.get(verb, 0)
        if verb == "clone" and rc == 0:
            # No real git runs here — `run_repository` writes REHEARSAL.md
            # straight into the clone directory afterward, so a successful
            # fake clone still has to leave that directory behind.
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")


def _run(mod, github, git, rsa_key, tmp_path, *, owner="example-org"):
    def _wait():
        github.merged = True

    return mod.run_repository(
        owner, key_path=rsa_key, app_id="555555",
        opener=github, run_git=git, wait_for_operator=_wait,
        workdir=tmp_path)


# --- the pure classifiers ----------------------------------------------------

def test_classify_attempt_reads_a_success_as_admitted(mod):
    assert mod.classify_attempt(200, "") == "admitted"
    assert mod.classify_attempt(201, "") == "admitted"


@pytest.mark.parametrize("status", [401, 404])
def test_classify_attempt_reads_401_and_404_as_not_tested(mod, status):
    assert mod.classify_attempt(status, "") == "not-tested"


def test_classify_attempt_reads_a_403_permission_refusal_as_not_tested(mod):
    assert mod.classify_attempt(403, "Resource not accessible by integration") == "not-tested"


def test_classify_attempt_reads_any_other_refusal_as_refused_by_rule(mod):
    assert mod.classify_attempt(
        403, "At least 1 approving review is required by reviewers with write access."
    ) == "refused-by-rule"
    assert mod.classify_attempt(405, "Pull Request is not mergeable") == "refused-by-rule"


def test_classify_push_reply_reads_a_clean_exit_as_admitted(mod):
    assert mod.classify_push_reply(0, "") == "admitted"


def test_classify_push_reply_reads_the_permission_wording_as_not_tested(mod):
    assert mod.classify_push_reply(
        1, "remote: Resource not accessible by integration\n") == "not-tested"


def test_classify_push_reply_reads_a_named_status_the_same_way_as_classify_attempt(mod):
    assert mod.classify_push_reply(1, "The requested URL returned error: 404") == "not-tested"


def test_classify_push_reply_reads_a_generic_refusal_as_refused_by_rule(mod):
    assert mod.classify_push_reply(
        1, "remote: Changes must be made through a pull request (GH013)\n") == "refused-by-rule"


# --- verdict_for --------------------------------------------------------------

def test_verdict_for_reads_a_clean_run_as_refused_by_rule(mod):
    result = {
        "merge_attempt": {"verdict": "refused-by-rule"},
        "push_attempt": {"verdict": "refused-by-rule"},
        "main_moved": False,
        "operator_merge_admitted": True,
    }
    assert mod.verdict_for(result) == "REFUSED-BY-RULE"


def test_verdict_for_reads_a_failed_positive_control_as_not_tested(mod):
    assert mod.verdict_for({"positive_control_failed": True}).startswith("NOT TESTED")


def test_verdict_for_reads_a_permission_refusal_as_not_tested(mod):
    result = {
        "merge_attempt": {"verdict": "not-tested"},
        "push_attempt": {"verdict": "refused-by-rule"},
        "main_moved": False,
        "operator_merge_admitted": True,
    }
    assert mod.verdict_for(result) == "NOT TESTED"


def test_verdict_for_reads_main_moving_as_inconclusive(mod):
    result = {
        "merge_attempt": {"verdict": "refused-by-rule"},
        "push_attempt": {"verdict": "refused-by-rule"},
        "main_moved": True,
        "operator_merge_admitted": True,
    }
    assert "main moved" in mod.verdict_for(result)


def test_verdict_for_reads_an_unadmitted_operator_merge_as_inconclusive(mod):
    result = {
        "merge_attempt": {"verdict": "refused-by-rule"},
        "push_attempt": {"verdict": "refused-by-rule"},
        "main_moved": False,
        "operator_merge_admitted": False,
    }
    assert "operator" in mod.verdict_for(result)


# --- run_repository, end to end against the fakes ----------------------------

def test_run_repository_reads_a_clean_refusal_as_refused_by_rule(mod, rsa_key, tmp_path):
    github = FakeGitHub(
        merge_status=403,
        merge_body={"message": "At least 1 approving review is required."},
        main_sha_sequence=("a" * 40, "a" * 40, "b" * 40))
    git = FakeGit(push_results=(
        (0, ""),
        (1, "remote: Changes must be made through a pull request (GH013)\n")))

    result = _run(mod, github, git, rsa_key, tmp_path)

    assert result["verdict"] == "REFUSED-BY-RULE"
    assert result["main_moved"] is False
    assert result["operator_merge_admitted"] is True


def test_run_repository_reads_a_403_permission_refusal_as_not_tested(mod, rsa_key, tmp_path):
    github = FakeGitHub(
        merge_status=403, merge_body={"message": "Resource not accessible by integration"},
        main_sha_sequence=("a" * 40, "a" * 40, "a" * 40))
    git = FakeGit(push_results=(
        (0, ""), (1, "remote: Resource not accessible by integration\n")))

    result = _run(mod, github, git, rsa_key, tmp_path)

    assert result["merge_attempt"]["verdict"] == "not-tested"
    assert result["verdict"] == "NOT TESTED"


def test_run_repository_reads_a_404_as_not_tested(mod, rsa_key, tmp_path):
    github = FakeGitHub(merge_status=404, merge_body={"message": "Not Found"},
                        main_sha_sequence=("a" * 40, "a" * 40, "a" * 40))
    git = FakeGit(push_results=(
        (0, ""), (1, "remote: Changes must be made through a pull request (GH013)\n")))

    result = _run(mod, github, git, rsa_key, tmp_path)

    assert result["merge_attempt"]["verdict"] == "not-tested"
    assert result["verdict"] == "NOT TESTED"


def test_run_repository_raises_merge_admitted_and_revokes_the_token(mod, rsa_key, tmp_path):
    github = FakeGitHub(merge_status=200, merge_body={"merged": True},
                        main_sha_sequence=("a" * 40,))
    git = FakeGit()

    with pytest.raises(mod.MergeAdmitted) as e:
        _run(mod, github, git, rsa_key, tmp_path)

    assert e.value.result["verdict"] == "MERGE ADMITTED"
    deletes = [c for c in github.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1, "the token must be revoked even when the merge was admitted"


def test_run_repository_revokes_the_token_when_the_positive_control_fails(mod, rsa_key, tmp_path):
    github = FakeGitHub()
    git = FakeGit(clone_rc=1)

    result = _run(mod, github, git, rsa_key, tmp_path)

    assert result["positive_control_failed"] is True
    assert result["verdict"].startswith("NOT TESTED")
    deletes = [c for c in github.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1, "a failed positive control must still revoke the token"
    # The positive control never reached GitHub's write endpoints.
    assert not any(c["method"] in ("POST", "PUT") and "/pulls" in c["url"] for c in github.calls)


def test_no_token_in_the_transcript_or_output(mod, rsa_key, tmp_path, capsys):
    github = FakeGitHub(
        merge_status=403, merge_body={"message": "At least 1 approving review is required."},
        main_sha_sequence=("a" * 40, "a" * 40, "b" * 40))
    leaking_stderr = (f"fatal: remote rejected (basic auth token {github.token!r}) "
                      f"Changes must be made through a pull request\n")
    git = FakeGit(push_results=((0, ""), (1, leaking_stderr)))

    result = _run(mod, github, git, rsa_key, tmp_path)

    out_path = tmp_path / "transcript.json"
    mod._write_transcript(out_path, [result])
    transcript_text = out_path.read_text()
    captured = capsys.readouterr()

    assert github.token not in transcript_text
    assert github.token not in json.dumps(result)
    assert github.token not in captured.out
    assert github.token not in captured.err


# --- main(): the whole-run stop on an admitted merge --------------------------

def test_main_stops_at_once_on_an_admitted_merge_and_never_probes_a_second_owner(
        mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "rehearsal_owners", lambda: ["example-org", "example-person"])
    calls = []

    def _fake_run_repository(owner, **kw):
        calls.append(owner)
        raise mod.MergeAdmitted({"repo": f"{owner}/builder-merge-rehearsal",
                                 "verdict": "MERGE ADMITTED"})

    monkeypatch.setattr(mod, "run_repository", _fake_run_repository)
    out_path = tmp_path / "transcript.json"

    rc = mod.main(["--out", str(out_path)])

    assert rc == 3
    assert calls == ["example-org"], "a second owner must never be probed once one is admitted"
    transcript = json.loads(out_path.read_text())
    assert transcript[0]["verdict"] == "MERGE ADMITTED"
    assert "MERGE ADMITTED" in capsys.readouterr().err


def test_main_refuses_with_no_owners_configured(mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mod, "rehearsal_owners", lambda: [])

    rc = mod.main(["--out", str(tmp_path / "transcript.json")])

    assert rc == 1
    assert "rehearsal_repository_owners" in capsys.readouterr().err
