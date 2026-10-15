"""The verdict a dispatched reviewer posts from outside its sandbox (agent-platform#793).

A reviewer run's Bash cannot post its own verdict. The sandbox refuses the loopback
Library, and the credential that writes a review record is unreadable to the run. So
the verdict goes through `bin/mcp-verdict-server`, a stdio MCP server that Claude Code
starts for the run. Claude starts it outside the sandbox. Spawn passes it the run id in
its launch environment, which the run cannot change after launch.

A Codex reviewer has no MCP server to start — nothing in its read-only sandbox can
start one, and nothing in the sandbox could reach the Library credential either way —
so `agentctl` posts FOR it once the run exits, reading the same run id from the
environment it launched the run with. Both callers resolve and post through
`post_verdict`, below, the one function either route calls — not two copies of the
same refusals and the same posting command.

Before it posts, the server reads the run's own meta.json (through
`review_protocol.resolve_run_facts`, the same reader the merge gate uses) and refuses
unless the run is a review-class dispatch pinned to a PR. A verdict argument that names
another PR, repository or commit is refused as well. The posting itself is
`agentctl review-post`, pinned to the run's commit, so every check that command makes
still applies, including the APPROVE gate on CI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import projects
import review_protocol as RP

# Read by `review-post` for the Library address it writes a record to. Unset, that write
# goes to the fixed Library address (`vault_plane.DEFAULT_LIBRARY_URL`). Set to a loopback
# URL, the write goes there, and only then. The stand-in listener in the live test is the
# one user. A process that cannot read the Library token gains nothing by redirecting its
# write, and the token is the only thing that authorises it.
RECORD_URL_ENV = "AGENTCTL_REVIEW_RECORD_URL"

SERVER_NAME = "verdict"


class Refused(Exception):
    """A post the verdict tool will not make. The message is the reason the run is told."""


@dataclass(frozen=True)
class Target:
    """The PR a run's verdict is posted to, as its own dispatch recorded it."""
    run_id: str
    agent: str
    pr: str
    repo: str
    commit: str


def _slug(repo: str) -> str:
    # The same expansion `--on` has always carried, now the one function
    # agentctl, tool_policy and this module all call through
    # (projects.repo_github_slug): an owner-less name is convivy/<name>,
    # unless the repo's own registry entry names a different owner.
    return projects.repo_github_slug(repo)


def target_for(run_id: str, *, pr: str | None = None, repo: str | None = None,
               reviewed_commit: str | None = None, runs_root: Path | None = None) -> Target:
    """The run's PR, repository and commit, after checking the arguments name the same ones.

    Every argument is optional. One that is given must match the run's record."""
    facts = RP.resolve_run_facts(run_id, runs_root)
    if not facts.found:
        raise Refused(f"run {run_id!r} has no readable run record, so there is no PR to post to")
    if not RP.is_review_class(facts.agent, RP.REVIEW_AGENTS):
        raise Refused(f"{facts.agent} is not a reviewer. Only reviewer and security-reviewer "
                      f"post a verdict.")
    if facts.pr in (None, "", 0):
        raise Refused("this run was not dispatched with --pr, so no PR is pinned to post to")
    if not re.fullmatch(r"[0-9a-f]{40}", facts.review_sha or ""):
        raise Refused("this run's record pins no full review commit")
    target = Target(run_id=run_id, agent=facts.agent, pr=str(facts.pr),
                    repo=_slug(facts.repo), commit=facts.review_sha)
    if pr is not None and str(pr) != target.pr:
        raise Refused(f"this run is pinned to PR #{target.pr}; the call names PR #{pr}")
    if repo is not None and _slug(repo) != target.repo:
        raise Refused(f"this run is pinned to {target.repo}; the call names {repo}")
    if reviewed_commit:
        typed = reviewed_commit.lower()
        if len(typed) < 7 or not target.commit.startswith(typed):
            raise Refused(f"this run is pinned to commit {target.commit}; the call names "
                          f"{reviewed_commit}")
    return target


def post(target: Target, verdict: str, body: str, *, agentctl: Path,
         runner=subprocess.run) -> subprocess.CompletedProcess:
    """Post the verdict through `agentctl review-post`, pinned to the run's own commit."""
    if verdict not in RP.VERDICTS:
        raise Refused(f"verdict must be one of {', '.join(RP.VERDICTS)}")
    if not body.strip():
        raise Refused("the review body is empty")
    # A private directory, so the body file is not visible to any other process.
    with tempfile.TemporaryDirectory(prefix="verdict-") as scratch:
        body_file = Path(scratch) / "review.md"
        body_file.write_text(body)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "AGENTCTL_RUN_ID": target.run_id, "AGENTCTL_REVIEW_SHA": target.commit}
        if os.environ.get(RECORD_URL_ENV):
            env[RECORD_URL_ENV] = os.environ[RECORD_URL_ENV]
        cmd = [str(agentctl), "review-post", "--pr", target.pr, "--verdict", verdict,
               "--agent", target.agent, "--body-file", str(body_file), "--repo", target.repo]
        return runner(cmd, capture_output=True, text=True, env=env)


def post_verdict(run_id: str, verdict: str, body: str, *, agentctl: Path,
                 pr: str | None = None, repo: str | None = None,
                 reviewed_commit: str | None = None,
                 runner=subprocess.run) -> tuple[Target, subprocess.CompletedProcess]:
    """Resolve `run_id`'s target and post `verdict`/`body` through `post`.

    The one function both the MCP verdict server (a Claude run) and a dispatcher-side
    poster (a Codex run, which cannot start an MCP server of its own) call, so there is
    exactly one posting path rather than two copies of `target_for` + `post`."""
    target = target_for(run_id, pr=pr, repo=repo, reviewed_commit=reviewed_commit)
    done = post(target, verdict, body, agentctl=agentctl, runner=runner)
    return target, done


def mcp_config(run_id: str, server: Path) -> str:
    """The `--mcp-config` value spawn passes a dispatched run's claude.

    Inline JSON, not a file, so no file the run could write sits in the launch path.
    The run id is set in the server's launch environment. The run cannot change it."""
    env = {"AGENTCTL_RUN_ID": run_id}
    if os.environ.get(RECORD_URL_ENV):
        env[RECORD_URL_ENV] = os.environ[RECORD_URL_ENV]
    return json.dumps({"mcpServers": {SERVER_NAME: {
        "type": "stdio", "command": str(server), "args": [], "env": env}}},
        sort_keys=True, separators=(",", ":"))
