#!/usr/bin/env python3
"""Calibration helper (platform/specs/reviewers-run-under-their-allow-list): prints
the EXACT `--settings` JSON a `reviewer`/`security-reviewer` dispatch on a given
worktree would get — the same `run_sandbox.settings_json(...)` call
`bin/agentctl`'s `_spawn_claude` makes, with the same resolved `tool_policy.resolve`
allow-list.

NOT A TEST. Nothing here asserts anything or spends a `claude` run; it only prints a
string. The rollout's calibration step (platform/specs/reviewers-run-under-their-
allow-list, "Rollout, and the check that would show it broken", step 1) runs the
printed settings through a real `claude -p` by hand, against each probe shape in the
plan's own table — this script's only job is making sure the settings used for that
calibration are not hand-approximated.

Usage:

    .venv/bin/python -I tests/live/print_review_settings.py --worktree <path> \\
        [--agent reviewer|security-reviewer] [--repo <bare-repo-name>] \\
        [--home <path>] [--no-create-draft-dir]

`--worktree` must exist (it is read for `.git`, to resolve `git_dir`/the current
branch exactly as a real dispatch would — see `run_sandbox.settings`'s own
docstring); it is never written to by this script. `--home` defaults to
`run_sandbox.passwd_home()`, the same account-home value a real dispatch uses;
override it to calibrate against a scratch home instead of the real one. The draft
directory is created (not merely computed) by default, matching what the real
dispatcher does before a run's claude ever starts — `tests/live/`'s own `-I` run
(this module touches no credential and reads no network) is safe to create it under
a scratch `--home`; pass `--no-create-draft-dir` to print the settings without
touching the filesystem at all (the directory's path still appears in the output,
whether or not it exists).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib" / "agent-platform"))

import run_sandbox as RS  # noqa: E402
import tool_policy as TP  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--worktree", required=True, type=Path)
    p.add_argument("--agent", default="reviewer", choices=["reviewer", "security-reviewer"])
    p.add_argument("--repo", default=None)
    p.add_argument("--home", default=None, type=Path)
    p.add_argument("--no-create-draft-dir", dest="create_draft_dir", action="store_false")
    args = p.parse_args(argv)

    worktree = args.worktree.resolve()
    if not worktree.is_dir():
        print(f"print_review_settings: {worktree} is not a directory", file=sys.stderr)
        return 2
    home = (args.home or RS.passwd_home()).resolve()
    git_dir = RS.git_common_dir(worktree)

    policy = TP.resolve(args.agent, TP.AUTONOMOUS, args.repo, denied=[])
    draft_dir = (RS.create_review_draft_dir(home, worktree) if args.create_draft_dir
                else RS.review_draft_dir(home, worktree))

    print(RS.settings_json(
        home=home, worktree=worktree, git_dir=git_dir, repo=args.repo,
        review_allowed=tuple(policy.allowed), draft_dir=draft_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
