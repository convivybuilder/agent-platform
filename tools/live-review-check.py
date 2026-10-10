#!/usr/bin/env python3
"""Print a deny reason when a live review is pinned to a commit other than HEAD;
print nothing otherwise. Called by block-push-under-live-review.sh (ARCH-374).

A SEPARATE FILE, not a heredoc inside the hook. bash 3.2 — what /bin/bash is on
the mini and in CI (ARCH-082) — cannot parse a quoted heredoc nested in a `$( )`
command substitution. It parses fine under bash 5, which is what a bare `bash -n`
uses on this laptop, so the hook linted clean locally and failed CI. Checking
with `bash` rather than `/bin/bash` is precisely the trap ARCH-082 names.

The decision is a PURE FUNCTION (`deny_reason`) with the IO around it, so the
logic is testable without a git repo, a run registry, or a fabricated HOME. The
first version was all IO, which is why its only test could exercise the
pass-through and never the deny.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REVIEW_AGENTS = ("reviewer", "security-reviewer")


def is_review_agent(agent: str) -> bool:
    """True for `reviewer`, `security-reviewer`, and their app-prefixed forms
    (`hollis-reviewer`, `svc-security-reviewer`) — the same shape agentctl's
    _strip_app_prefix handles."""
    return any(agent == r or agent.endswith("-" + r) for r in REVIEW_AGENTS)


def repo_for(toplevel: pathlib.Path, root: pathlib.Path) -> str | None:
    """The repo name for a git toplevel, given the machine's crews `root`.

    NOT the basename: a worktree lives at <root>/worktrees/<repo>.<purpose>/,
    so the basename is "agent-platform.reviewer-xyz" and would match no run's
    meta. Reviewers run IN worktrees, so getting this wrong would miss the exact
    case this guard exists for.
    """
    convivy = root
    try:
        return toplevel.relative_to(convivy / "worktrees").parts[0].split(".", 1)[0]
    except ValueError:
        pass
    try:
        return toplevel.relative_to(convivy).parts[0]
    except ValueError:
        return None


def doomed_reviews(runs: list[dict], repo: str, local_head: str,
                   on_this_branch=None) -> list[dict]:
    """Live review runs that THIS push would invalidate.

    Getting this predicate right took three tries, and both wrong versions are
    worth recording because they fail in OPPOSITE directions.

    REPO-ONLY was too broad. It rested on "a push to any branch can move a PR
    head", which is false — pushing branch B does not move branch A's head. It
    would force killing a still-valid review on unrelated work.

    ANCESTRY-ONLY was too narrow, and traded that false positive for a false
    NEGATIVE. After a rebase the reviewed commit is rewritten out of the branch,
    so it is no longer an ancestor of HEAD; the test reads "unrelated" and allows
    a force-push that certainly does invalidate the review. Repo-only had caught
    that case, so the fix was a regression for it.

    The question both were approximating: does the reviewed commit belong to the
    branch this push will move? True by either route —

      * an ancestor of local HEAD (a normal advance past it), or
      * an ancestor of the branch's PUBLISHED head (origin/<branch>), which
        still holds after a rebase drops it from the local line.

    A review on another branch is an ancestor of neither, so it stays allowed.

    `on_this_branch(sha) -> bool` is injected so the decision stays testable
    without a git repo. Absent, the filter falls back to repo-only — broad, but
    the safe direction: a guard that cannot tell must not silently allow the case
    it exists for.
    """
    out = []
    for run in runs:
        if run.get("ended_at"):
            continue
        agent = run.get("agent") or ""
        if run.get("repo") != repo or not is_review_agent(agent):
            continue
        sha = run.get("review_sha")
        # Pinned to exactly what is being pushed: the no-op case. Denying it
        # would be noise.
        if not sha or sha == local_head:
            continue
        if on_this_branch is not None and not on_this_branch(sha):
            continue  # a review on another branch; this push cannot touch it
        out.append({"run_id": run.get("run_id") or "?", "agent": agent,
                    "review_sha": sha})
    return out


def deny_reason(doomed: list[dict], local_head: str) -> str:
    """The message, or "" when there is nothing to say."""
    if not doomed:
        return ""
    lines = [f"  {d['agent']} run {d['run_id']} is pinned to {d['review_sha'][:9]}"
             for d in doomed]
    kill = " ".join(f"--final {d['run_id']}" for d in doomed)
    return (
        "ARCH-374: a review is running against a different commit than the one "
        "you are about to push.\n" + "\n".join(lines) +
        f"\n  you are pushing {local_head[:9]}\n\n"
        "Pushing now moves the head, and ARCH-193 will DISCARD that review's "
        "stamp as a stale replay - the review completes, posts, and the gate "
        "reports \"no review posted\". Two ways forward:\n"
        "  1. wait for it to finish, batch its findings with the other "
        "reviewer's, then push once; or\n"
        f"  2. kill it and re-dispatch after the push:  agentctl kill {kill}\n")


def live_runs_with_meta(home: pathlib.Path) -> list[dict]:
    """Live runs joined to the pin recorded in each meta.json (ARCH-184).

    The registry is the liveness source; meta.json is where the pin lives. A run
    whose meta is missing or unreadable is SKIPPED — this guard prevents waste,
    and blocking every push on a half-written file is a worse failure.
    """
    sys.path.insert(0, str(home / ".local" / "lib" / "agent-platform"))
    try:
        import registry as R
        runs = R.list_runs()
    except Exception:                                            # noqa: BLE001
        return []
    # The registry's own root, so this reader looks where run records are written
    # (builder_root.runs_root(), company/decisions/0008).
    runs_root = R.RUNS_ROOT
    out = []
    for run in runs:
        run_id = run.get("run_id") or ""
        run_dir = None
        for date_dir in sorted(runs_root.glob("20*-*-*/"), reverse=True):
            cand = date_dir / run_id
            if cand.is_dir():
                run_dir = cand
                break
        if run_dir is None:
            continue
        try:
            meta = json.loads((run_dir / "meta.json").read_text())
        except Exception:                                        # noqa: BLE001
            continue
        out.append({"run_id": run_id, "agent": meta.get("agent") or "",
                    "repo": meta.get("repo"), "review_sha": meta.get("review_sha"),
                    "ended_at": run.get("ended_at")})
    return out


def _upstream_ref() -> str:
    """The current branch's published head (`origin/<branch>`), or "" when there
    is none — a branch never pushed has no published history to rewrite."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                                            # noqa: BLE001
        return ""


def _crews_root(home: pathlib.Path) -> pathlib.Path | None:
    """The machine's root, builder_root.root() (company/decisions/0008), loaded
    from the same deployed lib dir the registry comes from, or None when it cannot
    load. None names no repo, so the push is allowed: the fail-open this guard
    already takes on any error, since it prevents waste rather than an attack."""
    sys.path.insert(0, str(home / ".local" / "lib" / "agent-platform"))
    try:
        import builder_root
        return builder_root.root()
    except Exception:                                            # noqa: BLE001
        return None


def main() -> int:
    home = pathlib.Path.home()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        head = subprocess.run(["git", "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        if top.returncode != 0 or head.returncode != 0:
            return 0
        root = _crews_root(home)
        if root is None:
            return 0
        repo = repo_for(pathlib.Path(top.stdout.strip()).resolve(), root)
        if not repo:
            return 0
        local_head = head.stdout.strip()
    except Exception:                                            # noqa: BLE001
        return 0
    upstream = _upstream_ref()

    def _ancestor_of(sha: str, ref: str) -> bool:
        # `--` terminates option parsing: a review_sha read off disk must never
        # be able to act as a git flag (e.g. --upload-pack=...).
        return subprocess.run(["git", "merge-base", "--is-ancestor", "--", sha, ref],
                              capture_output=True, timeout=5).returncode == 0

    def _on_this_branch(sha: str) -> bool:
        """Does `sha` belong to the branch this push will move? True if it is an
        ancestor of local HEAD (a normal advance) OR of the published head, which
        still holds after a rebase drops it locally.

        A failure answers TRUE — falling back to the broad filter, because a
        guard that cannot tell must not silently allow the case it exists for.
        """
        try:
            if _ancestor_of(sha, "HEAD"):
                return True
            return bool(upstream) and _ancestor_of(sha, upstream)
        except Exception:                                        # noqa: BLE001
            return True

    sys.stdout.write(
        deny_reason(doomed_reviews(live_runs_with_meta(home), repo, local_head,
                                   on_this_branch=_on_this_branch),
                    local_head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
