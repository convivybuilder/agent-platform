"""Live check: every verb's token mint, against the install's own real GitHub
App on convivy/agent-platform (E10 S1, platform/specs/github-credential-seam).

Mints a real installation token for each verb in `github_app.VERB_PERMISSIONS`,
scoped to `convivy/agent-platform`, and asserts GitHub granted exactly what was
asked: `repository_selection: selected`, exactly one repository (this one), and
the granted permissions equal to `github_app.expected_granted_permissions`,
the requested ones plus `metadata: read`, which GitHub adds to every
installation token whether or not it was requested (observed against the
real convivy-bosun App on 2026-10-18, every verb). Also asserts the mint log
picked up one line per mint, plus a second line for a mint made through
`github_app.mint_token`'s revoke-on-exit wrapper.

Resolves the App through `github_app.installed_app()`, never a literal —
reads the real key at whatever path the install's own `create-github-app`
wrote (`~/.config/convivy/github-app/app.pem`; a symlink to the pre-existing
`bosun-app.pem` on an install that kept the older key file) and calls the real
GitHub API. This is NOT run by a coder's sandbox, which denies that key, and
it is not part of the CI gate (no key there either). Run it by hand, after
merge:

    AGENTCTL_LIVE_GITHUB_MINT=1 .venv/bin/python -m pytest \\
        tests/live/test_github_mint_live.py -s

Every mint this test makes is revoked before the test returns, whichever verb:
the "read" verb is minted through `github_app.mint` directly (never revoked in
production, since it is the per-run token a dispatched run keeps for its own
lifetime) and revoked by hand here so this check leaves nothing live past its
own run.
"""
from __future__ import annotations

import json
import os

import pytest

import github_app as GA

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTCTL_LIVE_GITHUB_MINT") != "1",
    reason="live GitHub mint check: set AGENTCTL_LIVE_GITHUB_MINT=1 to mint real tokens",
)

REPO_SLUG = "convivy/agent-platform"


@pytest.fixture()
def mint_log(monkeypatch, tmp_path):
    log_path = tmp_path / "mint-log.jsonl"
    monkeypatch.setattr(GA, "mint_log_path", lambda: log_path)
    return log_path


@pytest.fixture()
def app():
    """The install's own GitHub App — resolved here, in a fixture, never at
    module level: this module is collected on every pytest run (`pytestmark`
    skips the TESTS, not the import), so resolving at import time would raise
    on any machine with no App set up, breaking collection for the whole
    suite."""
    return GA.installed_app()


# `rehearsal` is excluded here: unlike every other verb it refuses
# REPO_SLUG outright (E10 S11, platform/specs/github-credential-seam — it
# mints only for `<owner>/builder-merge-rehearsal` with owner listed in the
# operator's own config), so it needs tools/merge-refusal-rehearsal's own
# live run against a throwaway repository, not this one.
@pytest.mark.parametrize("verb", sorted(v for v in GA.VERB_PERMISSIONS if v != "rehearsal"))
def test_mint_grants_exactly_the_verbs_requested_permissions_on_one_repo(mint_log, app, verb):
    payload = GA.mint(REPO_SLUG, verb, key_path=app.key_path, app_id=app.app_id)
    try:
        assert payload["repository_selection"] == "selected"
        repos = payload.get("repositories") or []
        assert len(repos) == 1
        assert repos[0]["full_name"] == REPO_SLUG
        assert payload.get("permissions") == GA.expected_granted_permissions(verb)
    finally:
        assert GA.revoke_installation_token(payload["token"]) is True

    lines = mint_log.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["verb"] == verb
    assert entry["repo"] == REPO_SLUG
    assert entry["permissions"] == GA.expected_granted_permissions(verb)
    assert entry["repository_selection"] == "selected"
    assert "token" not in entry


def test_mint_token_mints_and_revokes_against_the_real_api(mint_log, app):
    with GA.mint_token(REPO_SLUG, "pr-comment", key_path=app.key_path, app_id=app.app_id) as token:
        assert token

    lines = mint_log.read_text().strip().splitlines()
    assert len(lines) == 2  # the mint, then the revoke
    revoke_entry = json.loads(lines[-1])
    assert revoke_entry["verb"] == "pr-comment"
    assert revoke_entry["repo"] == REPO_SLUG
    assert revoke_entry["revoked"] is True
