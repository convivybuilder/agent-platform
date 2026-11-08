"""Resolve an agent's effective tool policy, per runtime (ARCH-346).

WHY THIS EXISTS

One agent definition was consumed by two runtimes with different enforcement
semantics, and nothing in the artifact said which:

  - INTERACTIVE — `knowledge-ctx use` individuates `.claude/agents/<name>.md` into a
    human's clone. Claude Code reads the frontmatter and ENFORCES `tools:`.
  - AUTONOMOUS — `agentctl spawn` runs `claude -p --permission-mode bypassPermissions`
    and passes the agent body via `--append-system-prompt`. The frontmatter `tools:`
    is never passed and never read. Only `--disallowedTools` binds. The field is
    inert... except that the same spawn ALSO individuates `.claude/agents/`, so a Task
    subagent inside that run DOES get `tools:` enforced.

So one field meant "enforced", "ignored", or "enforced one level down" depending on a
runtime it never names. That is not a stale list; it is a field whose meaning is a
function of context the reader cannot see. It misled a human auditor (2026-07-13), and
it would mislead the next one. The roster read like a permission boundary while the
autonomous fleet — the part that runs unattended, all night, against attacker-authored
diffs — enforced almost none of it.

THE FIX IS NOT A BETTER LIST. It is to make the effective policy a COMPUTED, RECORDED
value: resolve(agent, runtime, repo) -> Policy, with each runtime individuating through
its own path from this one resolver. A policy that says what it enforces, names what it
does not, and is written to the run dir so what a run COULD do is a fact you read rather
than a claim you trust.

THE HONEST PART

An allow-list is only a control where the agent does not need arbitrary shell. Mined
from 9,069 real runs: `coder` ran `source .venv/bin/activate`, `python3 -c`, `make test`,
`psql`; `bug-fixer` and `refactorer` the same. Any allow-list permissive enough for them
to build software is equivalent to full Bash. Writing one would put a control-shaped
object in the roster that controls nothing — the exact failure this module exists to end.

So a build agent's policy declares `enforced=False` and carries the deny-list only. It
does not pretend. The read-only / review class DOES get a real allow-list, because it
genuinely does not need arbitrary shell — and that is the class that reviews untrusted
diffs and holds the `gh` credential, so it is the class where scoping pays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import app_prefixes
import projects
import review_protocol

AUTONOMOUS = "autonomous"
INTERACTIVE = "interactive"

# The two headless permission modes, and the whole reason this module had to be built
# twice. `--allowedTools` is INERT under bypassPermissions: the mode auto-approves
# everything, and only deny rules bind. Verified empirically 2026-07-13 — a spawn with
# `--allowedTools Read` happily ran `echo`, and a first cut of this module would have
# written `enforced: true` into the audit record while restricting exactly nothing.
# A control that reports success and does nothing is worse than no control.
#
# `dontAsk` is the mode that actually enforces: it auto-DENIES anything outside the
# allow rules (plus built-in read-only Bash), and fails fast rather than hanging an
# unattended agent on a prompt. So enforcement means changing the MODE, not adding a
# flag to bypass.
MODE_BYPASS = "bypassPermissions"
MODE_ENFORCE = "dontAsk"

# Agents that review or read, and never need arbitrary shell. These get a REAL
# allow-list. Everything else is deny-list-only, declared as such.
#
# Membership is a claim that the agent can do its job without unrestricted Bash. It is
# checked against real usage, not intent: adding an agent here whose prompt tells it to
# run a test suite would break it on the next dispatch. Verify against tool_calls.jsonl
# before extending this set.
#
# `shadow-reviewer` (CQ·526) is unioned in from `review_protocol.SHADOW_REVIEW_AGENTS`
# rather than written as its own literal: `bin/agentctl`'s `_is_review_agent` already
# sends it down `_run_session`'s review-class sandboxing path (same file, same
# widening), so a policy that did not also classify it here would hand that run
# `review_allowed=()` instead of `None` — every tool denied but `ToolSearch`, unable
# to read the diff it was dispatched to review. One shared constant is how the two
# classifications are kept from drifting apart again; see `_VERDICT_AGENTS` below for
# the matching case for verdict-posting and the draft-write scope.
ENFORCEABLE_AGENTS = frozenset({
    "reviewer",
    "security-reviewer",
    "planner",
    "platform-planner",
    "design-critic",
    "scout",
    "web-researcher",
}) | frozenset(review_protocol.SHADOW_REVIEW_AGENTS)

# `ENFORCEABLE_AGENTS` members whose allow-list binds on EVERY dispatch, with no
# `AGENTCTL_ENFORCE_TOOL_POLICY` switch to wait on (security review, PR #1044 round 1).
# `web-researcher`'s whole job is fetching and reading untrusted web pages — the one
# `ENFORCEABLE_AGENTS` member that reads adversarial content by design, the same
# property that put `reviewer`/`security-reviewer` on the equivalent unconditional path
# (`_VERDICT_ENFORCEMENT_CAVEAT`) rather than leaving it gated like `scout`/`planner`,
# where a canary period of recorded-but-unenforced runs is the right trade. `bin/agentctl`'s
# `_run_session` reads this set directly (`enforcing`, `_enforcement_active`, and the
# `review_allowed` sandbox-settings computation all widen to it) rather than through
# `_is_review_agent`, because `web-researcher` posts no verdict and must not gain the
# verdict tool or the review draft-write scope `_VERDICT_AGENTS` membership would carry —
# see that set's own comment for what it additionally grants. Kept as its own set rather
# than folded into `_VERDICT_AGENTS` so adding an agent here can never silently hand it
# `mcp__verdict__review_post` or a draft directory.
ALWAYS_ENFORCED_AGENTS = frozenset({"web-researcher"})

# The read/search surface every enforceable agent gets. These are TOOLS, not shell:
# giving the agent Grep and Read is what lets us withhold `Bash(grep ...)` without
# crippling it.
#
# `Write` is here, UNSCOPED, and that is a known hole rather than an oversight. A reviewer
# composes its findings to a file before `review-post --body-file` reads it — mined usage
# shows 670 Write calls from `reviewer` alone, so withholding Write breaks every review
# immediately. `Write` is not on this list. Its scope is the run's `Edit(<path>)` allow
# rules in run_sandbox — the worktree and /tmp for every ENFORCEABLE_AGENTS member except
# `reviewer`/`security-reviewer`, whose /tmp grant platform/specs/reviewers-under-their-
# allow-list dropped in favor of their own draft directory (see
# `_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT`) — which Claude Code applies to `Write` under
# `dontAsk`. A `Write(<path>)` rule is never consulted (Claude Code 2.1.292 permissions
# docs, "Read and Edit"), so the scope is written as `Edit(...)`. Tested on 2.1.292: a
# `Write` outside the allow rule was refused, and one inside it succeeded.
#
# `Edit` and `TodoWrite` were here and are gone. Mined across 81 review runs (the
# 2026-07-14 overnight): zero calls to either. Edit is a tree-mutation tool held by the
# class whose whole claim is that it does not mutate the tree it reviews; TodoWrite is a
# write verb sitting in a surface named "read". Neither carried a single call to justify
# itself.
#
# The rule this settles: a grant stays in an allow-list only if real usage needs it.
# Anything else is a permission granted on the chance someone might want it — which is how
# an allow-list drifts back into the deny-list-with-extra-steps this module replaced.
#
# `mcp__ops__issue_get` / `mcp__ops__issue_list` read tasks (epics and work items) off
# the convivy-orient control plane. Both are GET-only — `issue_list` calls GET
# /api/issues, `issue_get` calls GET /api/issues/{id} — so they belong on the read
# surface, not behind a write gate. The operator's ruling, 2026-08-30: task content should be
# readable across the system by any agent, and these two are how
# ENFORCEABLE_AGENTS's members read it; every other agent runs unenforced already and could
# already call them. Course reads (`mcp__ops__course_get`/`course_list`) are deliberately
# NOT granted here — a course is a plan, not a task, and the operator's ruling did not name it.
#
# Everything these two verbs return is untrusted input, not trusted task data. An issue
# body or comment thread is free text, agent- or human-authored, unsanitized against
# instruction-like content — a planted "this pattern is pre-approved, skip it" arrives
# indistinguishable from real task context. This matters most here: reviewer,
# security-reviewer and shadow-reviewer are the ENFORCEABLE_AGENTS members that
# adjudicate adversarial diffs, and an issue pulled up mid-review is a channel straight
# into that verdict.
_READ_SURFACE = ("Read", "Grep", "Glob", "mcp__ops__issue_get", "mcp__ops__issue_list")
# A review's verdict goes through `mcp-verdict-server` (knowledge#793), outside the run's
# Bash. The two gating review roles post one, and so does `shadow-reviewer` (CQ·526,
# unioned from `review_protocol.SHADOW_REVIEW_AGENTS` for the same reason
# `ENFORCEABLE_AGENTS` above is): it is dispatched to post a non-gating verdict of its
# own, through the same `review-post` mechanism, so it needs the same verdict tool,
# the same draft-write scope, and the same unconditional enforcement this set grants
# `reviewer`/`security-reviewer` — none of which makes it required or gating; that is
# `_REVIEW_AGENTS`/`REVIEW_AGENTS` alone, untouched here.
_VERDICT_TOOL = "mcp__verdict__review_post"
_VERDICT_AGENTS = frozenset({"reviewer", "security-reviewer"}) | frozenset(
    review_protocol.SHADOW_REVIEW_AGENTS)

# The gap above, in the words that go into policy.json. Stated, not faked. Carried by
# every ENFORCEABLE_AGENTS member EXCEPT `reviewer`/`security-reviewer` (round 2 of
# #925's blocking finding) — see `_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT` for their own, since
# `run_sandbox.settings`'s `review_allowed` branch drops the /tmp grant this text claims
# for them and substitutes their own draft directory instead.
_WRITE_SCOPE_CAVEAT = (
    "`Write` and `NotebookEdit` reach only the run's worktree and /tmp: run_sandbox "
    "grants `Edit(<path>)` for those two roots and nothing else, and `dontAsk` refuses "
    "every other write. Bash is not bounded by this; the sandbox bounds it. A "
    "prompt-injected reviewer can still write anywhere under /tmp and its worktree."
)

# `reviewer`/`security-reviewer`'s own version of the caveat above (round 2 of #925):
# `run_sandbox.settings`'s `review_allowed` parameter (platform/specs/reviewers-run-
# under-their-allow-list) replaces this class's `allowWrite` with its own per-dispatch
# draft directory instead of adding to the shared /tmp grant `_WRITE_SCOPE_CAVEAT`
# states, so a security-reviewer finding on #925 is what caught `_WRITE_SCOPE_CAVEAT`
# still being written, verbatim, into these two roles' own `policy.json` — a write
# scope the run does not have, in the one record meant to be "a fact you read rather
# than a claim you trust" (this module's own docstring).
#
# ROUND 3 OF #925 STATES THE SCOPE BY MECHANISM, NOT BY A LITERAL PATH (security-
# reviewer finding): the text below used to name
# `~/.local/state/agent-platform/review-drafts/<worktree name>` directly, which is
# only the direct-dispatch path's own directory (`run_sandbox.create_review_draft_dir`,
# keyed on the agentctl worktree's name, under the `agents` account's home). The
# two-account launcher (`review_launch.py`, `bin/review-launch`) runs this exact same
# `resolve()` and feeds the SAME `allowed` tuple into its OWN `run_sandbox.settings_json`
# call, but its draft directory is `run_dir / "draft"` under the `reviewers` account's
# root-owned `runs/` tree — a different path on a different account, so a note naming
# the direct-dispatch path's literal path is false for every session the launcher runs.
# The one thing both paths actually guarantee is: whichever directory a given dispatch
# created, that run's own environment names it as `AGENTCTL_REVIEW_DRAFT_DIR`
# (`bin/agentctl`'s `proc_env` for the direct path, `session_env` for the launcher) —
# so the caveat below points at that variable instead of a path.
_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT = (
    "`Write` and `NotebookEdit` reach only the run's own worktree (`Edit(<path>)`, "
    "with `.git` itself denied) and the ONE draft directory this dispatch created for "
    "it — never a shared temp root, which this class's `allowWrite` drops entirely in "
    "favor of that one directory (run_sandbox.settings's `review_allowed`/`draft_dir` "
    "parameters). The draft directory's actual path is not fixed by this note: it "
    "differs by launch path (the direct dispatch path keys it off the agentctl "
    "worktree's name under the `agents` account; the two-account launcher keys it off "
    "its own root-owned run directory under the `reviewers` account), so read it from "
    "this run's own `AGENTCTL_REVIEW_DRAFT_DIR` environment variable rather than "
    "assuming a path. Bash is not bounded by this; the sandbox bounds it. A "
    "prompt-injected reviewer can still write anywhere under its worktree (outside "
    "`.git`) or its own draft directory."
)

_ENFORCEMENT_GATE_CAVEAT = (
    "READ EVERY GUARANTEE ON THIS POLICY AS CONDITIONAL — FOR THIS AGENT. (`reviewer`, "
    "`security-reviewer` and `shadow-reviewer` are the _VERDICT_AGENTS members this "
    "does NOT apply to: see `_VERDICT_ENFORCEMENT_CAVEAT` instead, their notes[0]. "
    "`web-researcher`, the one ALWAYS_ENFORCED_AGENTS member, is the same exception by "
    "a different mechanism: see `_ALWAYS_ENFORCED_CAVEAT` instead, its own notes[0].) The "
    "allow-list binds only when `AGENTCTL_ENFORCE_TOOL_POLICY` is set AND this policy "
    "is `enforced`; the env var defaults to OFF (ARCH-346), so by default the run "
    "executes under `bypassPermissions` with NONE of these restrictions applied — "
    "only the deny-list binds. `enforced: true` here means this policy CLASS is "
    "enforceable, not that the run you are reading about was enforced. Demonstrated "
    "live during the review of PR #391: a security-reviewer dispatch ran under "
    "`bypassPermissions` and successfully executed the very `ruff --fix` the "
    "exact-match grant denies — true of security-reviewer THEN, before "
    "platform/specs/reviewers-run-under-their-allow-list made its own enforcement "
    "unconditional. This note exists because `reason` and the caveats below state "
    "their guarantees unconditionally, and a reader of this record could not "
    "otherwise tell which runs they describe."
)

# The two review roles' own version of the caveat above: unlike every other
# ENFORCEABLE_AGENTS member, their enforcement has no env-var gate at all
# (platform/specs/reviewers-run-under-their-allow-list, incident: a security-reviewer's
# `python3 - <<'EOF'` booted out seven live launchd jobs on 2026-10-16, run-4a2b213d,
# because the run sandbox's settings granted bare `Bash` to every dispatched run
# regardless of `AGENTCTL_ENFORCE_TOOL_POLICY`). 29% of 470 review runs measured
# 2026-10-14 to 2026-10-16 called an interpreter, the reviewed tree's own code, or
# `launchctl` — real usage already included the hazard a switch would have left open,
# so the operator approved overriding ARCH-346's "recorded first, enforced once real
# usage is covered" condition for these two roles alone (2026-10-17). The other
# ENFORCEABLE_AGENTS members keep that condition and the env-var switch; see
# `_ENFORCEMENT_GATE_CAVEAT` for them. The only rollback for these two is reverting
# this change — see platform/decisions/0016 on a control that is merged but not in
# force, which is exactly the state this unconditional trigger ends for this class.
_VERDICT_ENFORCEMENT_CAVEAT = (
    "ENFORCEMENT HERE IS UNCONDITIONAL — NOT GATED BY `AGENTCTL_ENFORCE_TOOL_POLICY`, "
    "AND THERE IS NO SWITCH THAT TURNS IT OFF. `reviewer` and `security-reviewer` are "
    "the two roles that hold the `gh` credential and review-record access while "
    "adjudicating an attacker-authored diff, and real usage (470 review runs measured "
    "2026-10-14 to 2026-10-16) already included 29% that called an interpreter, the "
    "reviewed tree's own code, or `launchctl` — the override the operator approved "
    "2026-10-17 of ARCH-346's 'recorded first, enforced once real usage is covered' "
    "condition, for these two roles alone. Every other ENFORCEABLE_AGENTS member "
    "keeps that condition and the env-var switch (see `_ENFORCEMENT_GATE_CAVEAT`). "
    "The only rollback for these two roles is reverting this change."
)

# `web-researcher`'s own version of the caveat above (security review, PR #1044 round
# 1): it has no env-var gate either, for a different reason than the two review roles'
# — it is the one ENFORCEABLE_AGENTS member whose whole job is reading content an
# attacker controls (a fetched web page), rather than a role whose real usage happened
# to exceed its own declared grant. `ALWAYS_ENFORCED_AGENTS` is the named set
# `bin/agentctl` reads for this, kept apart from `_VERDICT_AGENTS` because
# `web-researcher` posts no verdict and must not gain the verdict tool or the review
# draft-write scope that set's own members get.
_ALWAYS_ENFORCED_CAVEAT = (
    "ENFORCEMENT HERE IS UNCONDITIONAL — NOT GATED BY `AGENTCTL_ENFORCE_TOOL_POLICY`, "
    "AND THERE IS NO SWITCH THAT TURNS IT OFF. `web-researcher`'s whole job is fetching "
    "and reading web pages, content an attacker controls by design — unlike the other "
    "ENFORCEABLE_AGENTS members, waiting on a canary period of recorded-but-unenforced "
    "runs before enforcing is not a trade this role can make, since the first dispatch "
    "already carries the hazard. Every other ENFORCEABLE_AGENTS member not in "
    "`_VERDICT_AGENTS` keeps the env-var switch (see `_ENFORCEMENT_GATE_CAVEAT`). "
    "The only rollback for `web-researcher` is reverting this change."
)

_LINT_FIX_CAVEAT = (
    "`Bash(ruff check --isolated .)` is an EXACT match, not a prefix, and that is the "
    "point: `--isolated` suppresses config DISCOVERY but does NOT disable ruff's own "
    "`--fix` / `--unsafe-fixes` / `--fix-only`, which rewrite the tree using built-in "
    "fixers with no repo config involved. Under a trailing-wildcard grant, `ruff check "
    "--isolated --fix .` would have matched and rewritten the tree under review. The "
    "exact form denies it. Do not re-add `:*` here for convenience — a reviewer needing "
    "a narrower path should be denied and say so, not be handed the flag space back. "
    "Exact-match semantics for a rule without `:*` are Claude Code product behaviour, "
    "not this module's invention (in-repo precedent: `Bash(pwd)` in "
    "`engineering-practice/reference/claude-code-permissions-configuration.md`); retest "
    "it on a Claude Code upgrade the same way the `Write` note above is retested. "
    "Verified live on Claude Code 2.1.229 (security review, PR #391 round 5), which is "
    "the citation the `Write` and bypassPermissions notes carry and this one lacked."
)

_LINT_FIDELITY_CAVEAT = (
    "THE GRANTED LINT COMMAND IS NOT THE REPO'S CI COMMAND, and a reviewer must not read "
    "its output as CI's. `--isolated` discards ALL repo-supplied ruff config — not only a "
    "malicious `fix = true`, but the legitimate `[tool.ruff.lint.per-file-ignores]` and "
    "`select`/`ignore` a repo has deliberately chosen — so `ruff check --isolated .` can "
    "report codes `ruff check .` does not, in files no diff touched. Measured on "
    "agent-platform itself: 9 pre-existing E702/E741 findings the repo exempts on purpose "
    "(reviewer, PR #391 round 4). This is the price of ignoring attacker-controlled "
    "config, and it is paid on every repo whose lint config carries exemptions. So: lint "
    "findings under this grant are ADVISORY about the diff, and `gh pr checks` remains "
    "the authority on what CI says. Never attribute an `--isolated` finding to the diff "
    "under review without confirming CI reports it too."
)

# Read-only git. `git diff/log/show/status/rev-parse` are how a reviewer reads the
# change. Write verbs (commit, push, checkout, reset) are absent on purpose: a reviewer
# must never mutate the tree it reviews.
#
# `branch` is NOT in this prefix-wildcard tuple (round 1 of #925, security-reviewer):
# `Bash(git branch:*)` grants every `git branch` form, including `-f <name> <sha>`,
# `-D`, `-m` and `-c`, which write refs — a live permission-layer check (2026-10-17,
# `claude -p --permission-mode dontAsk`) found `git branch -f calib-probe HEAD` and
# `git branch -D calib-probe` ALLOWED under this grant. The review worktree shares
# its ref store with the primary checkout and every other worktree, and many
# branches exist only in `packed-refs`, which `run_sandbox._protect_rest_of_git_dir`'s
# enumeration-based write denial does not cover — so this grant could move or delete
# a branch other checkouts read. `_GIT_READ_BRANCH`, below, grants only the
# argument-free read forms instead, each an EXACT match with no `:*` for anything to
# append to.
_GIT_READ = tuple(f"Bash(git {v}:*)" for v in
                  ("diff", "log", "show", "status", "rev-parse", "blame"))

# Exact, argument-free `git branch` read forms (round 1 of #925) — no `-f`, `-D`,
# `-m`, `-c`, or any other write verb has a rule to match, because none of these
# four carries a trailing `:*` for one to be appended to.
_GIT_READ_BRANCH = ("Bash(git branch)", "Bash(git branch -a)", "Bash(git branch -r)",
                   "Bash(git branch --show-current)")

# More read-only git, added for the ordinary (non-eval) review policy once a review-
# class dispatch's `permissions.allow` stopped being `TOOL_ALLOW`'s bare `Bash`
# (platform/specs/reviewers-run-under-their-allow-list): `merge-base`/`ls-tree`/
# `ls-files`/`cat-file` run no program from their arguments — the filter drivers
# `cat-file` can invoke come from git config, which a run cannot write
# (`run_sandbox.py`'s `_protect_rest_of_git_dir` denies `git_dir/config` wholesale).
# `git remote -v` is granted as an EXACT match, not a prefix: `git remote` has
# subcommands (`add`, `set-url`) that mutate the repo's remotes, which a trailing
# wildcard would reopen.
_GIT_READ_MORE = tuple(f"Bash(git {v}:*)" for v in
                       ("merge-base", "ls-tree", "ls-files", "cat-file"))
_GIT_REMOTE_V = ("Bash(git remote -v)",)

# The CI surface for the ordinary (non-eval) review policy: a reviewer needs the
# reviewed commit's check-run state, and `gh api` cannot be scoped to one repo's
# commit at the permission layer — a flag value cannot be constrained independently
# of its position (see `review_tools`'s own docstring on the same gap for the `gh pr`
# grants). `agentctl review-ci` takes no arguments at all: it reads the repo and
# commit from `AGENTCTL_REPO`/`AGENTCTL_REVIEW_SHA`, never argv, so there is no flag
# space here for a crafted diff to redirect the read to another repo's commit.
# Granted as an EXACT match — there is nothing to append a flag to.
_CI_ONLY = ("Bash(agentctl review-ci)",)

# The two read-only `kb` tools for the ordinary (non-eval) review policy
# (platform/specs/reviewers-run-under-their-allow-list): both are GET-only reads of
# the knowledge base, and the reviewer roster already instructs reviewers to read
# ADRs through them. Granted as exact MCP tool names, never the whole `mcp__kb`
# server `TOOL_ALLOW` used to grant.
_REVIEW_KB_SURFACE = ("mcp__kb__kb_get", "mcp__kb__kb_search")

# `web-researcher`'s entire grant (platform/agents/web-researcher), named here
# rather than read off its roster at resolve time. `bin/agentctl`'s `--allowedTools`
# is `policy.allowed` alone — the agent's own roster `tools:` grant only ever widens
# `--tools`/the MCP server set (`_builtin_tool_surface`/`_mcp_servers_for_policy`'s own
# union), never the permission layer (`test_dispatch_does_not_widen_allowedtools_from_
# the_roster_grant` pins exactly this for `reviewer`'s own kb grant) — so the only way
# these four names reach `--allowedTools` is for this resolver to name them directly,
# the same way `_REVIEW_KB_SURFACE` above is `reviewer`/`security-reviewer`'s own grant
# named directly rather than read off their roster.
_WEB_RESEARCH_SURFACE = ("WebSearch", "WebFetch", "mcp__kb__kb_search", "mcp__kb__kb_get")

# The ONE verification command a reviewer may run against the tree under review.
#
# **`ruff` IS HERE AND `pytest` DELIBERATELY IS NOT, and the asymmetry is the whole
# point.** `ruff` is a static parser with no plugin-execution path: linting a file
# containing a top-level `os.system(...)` does not run it (confirmed empirically,
# security review of PR #391). `pytest` is the opposite — it IMPORTS and executes every
# `conftest.py` it discovers at COLLECTION time, before any test filter applies, so a
# `conftest.py` in a malicious PR executes the instant `pytest` is invoked. The same
# review confirmed that too: a sentinel `conftest.py` fired under `pytest
# --collect-only` alone, with no test ever running.
#
# Granting `Bash(pytest:*)` would therefore be arbitrary code execution over the exact
# tree a review-class agent is dispatched to DISTRUST, and it would void the control
# this allow-list leans on: code running inside that process reaches `git` through
# `subprocess`, entirely outside the Bash-tool permission engine, so "no git write
# verbs, so it cannot COMMIT or PUSH the tree it reviews" would stop being true. It
# also reopens what PR #362 (`bf5edf5`, "reading a repo must not run code from it")
# already closed on the git side, through a wider door.
#
# So reviewers lint locally and rely on CI for the suite. CI runs it in an isolated
# runner, which is the right place for attacker-authored code to execute, and green
# required CI is already a hard precondition of APPROVE.
#
# **What this costs, recorded so nobody restores `pytest` for the wrong reason.** A
# reviewer can no longer plant a failure to check whether a test actually guards its
# fix — green CI proves a suite passes, never that a test would fail without the fix.
# That capability is real and was used well twice on 2026-08-12. It comes back through
# a sandboxed runner (no network, no ambient git/`gh` credential), NOT by adding
# `pytest` here. Approved by the operator 2026-08-12, `dec_01KZW59K9G63N0MDW3XTEYPQWF`, which
# supersedes `dec_01KZW249N8MKBGP2PXXDCXAQNR` — that one approved both commands on the
# false premise, corrected here, that neither could execute code from the tree.
# **`--isolated` IS PART OF THE GRANT, not a convention the runbook asks for.** `ruff`
# does not execute the tree, but it does READ that tree's `pyproject.toml` /
# `ruff.toml`, and those are attacker-authored too. A config setting `fix = true` and
# `unsafe-fixes = true` makes the routine, mandated `ruff check .` silently rewrite the
# files under review — behaviour-changing fixes included — with no `--fix` ever typed
# by the caller (round-2 security review, PR #391). `--isolated` ignores all
# repo-supplied config, and the grant is an EXACT match rather than a prefix so the
# `--fix` family cannot be appended to it. Both halves are needed: `--isolated` closes
# the config-triggered rewrite, exactness closes the flag-triggered one. See
# `_LINT_FIX_CAVEAT`, carried in `notes` beside the other two named holes.
#
# So the execution axis and the MUTATION axis are separate questions, and `ruff` is
# only clean on the first. Granting the bare `Bash(ruff:*)` would have left a reviewer
# able to modify the tree it is dispatched to read, which is the same property
# `test_a_reviewer_cannot_mutate_the_tree_it_reviews` exists to hold.
_LINT_ONLY = ("Bash(ruff check --isolated .)",)

# The agents whose roster actually documents a lint requirement. Only these two of
# `ENFORCEABLE_AGENTS`'s members are told to run `ruff`, so granting it to the rest
# would widen the surface past any cited need — the same "a grant stays only if
# real usage justifies it" rule that dropped `Edit` and `TodoWrite` above.
_LINT_AGENTS = frozenset({"reviewer", "security-reviewer"})

# The eval-mode caveat (round-1 revise, agent-platform#813): what the allow-list
# withholds and why, for a review-class agent dispatched under `--eval-mode` (the
# review-replay eval harness replays an OLD PR's corpus entry — see `_EVAL_MODE`
# below). Both round-1 reviews found the previous design (one env var
# `cmd_review_post` read, with `gh`/`agentctl review-post` left grantable) gave the
# replayed model a cooperative-only control: `env -u AGENTCTL_EVAL_MODE agentctl
# review-post ...`, or `gh api repos/<owner>/<repo>/issues/<pr>/comments`, both
# reachable because the replayed brief is the corpus entry's ORIGINAL brief,
# verbatim, which already instructs the model to post. So this policy withholds the
# surface at the permission layer instead: no `gh` rule of any kind, and no
# `Bash(agentctl review-post:*)` rule, in `allowed` — there is no route to post
# regardless of what the model does with the brief it was given.
_EVAL_MODE_NO_POST_CAVEAT = (
    "NO `gh` GRANT AND NO `agentctl review-post` GRANT — a deliberate omission, not "
    "an oversight. `--eval-mode` replays a historical PR's ORIGINAL brief verbatim, "
    "which already instructs the model to post or to call `review-post`, so the "
    "posting surface has to be unreachable here rather than withheld by the model's "
    "cooperation. The replay writes its verdict and body into its OWN directory "
    "instead (the eval system-prompt directive says so — see "
    "`RS.create_eval_scratch_dir`); `bin/agentctl`'s own dispatcher code reads that "
    "directory back once the run has exited and copies the validated result into "
    "`eval-review.json` in the run's own directory, which the harness scores from, "
    "never from GitHub. ENFORCEMENT HERE IS "
    "UNCONDITIONAL: an eval dispatch forces `dontAsk` regardless of "
    "`AGENTCTL_ENFORCE_TOOL_POLICY` (see `_spawn_claude`) — the same unconditional "
    "enforcement the ordinary (non-eval) review policy below now also has, for the "
    "same two roles, by a different mechanism (`_VERDICT_ENFORCEMENT_CAVEAT`: no "
    "env-var gate at all, rather than a forced mode for one dispatch kind) — so "
    "this allow-list is never merely resolved-but-unapplied, eval or not."
)

# The eval replay's one write (round-6 revise, agent-platform#813): `gh` and
# `agentctl review-post` are both withheld (`_EVAL_MODE_NO_POST_CAVEAT`), so `Write`
# is the only remaining route to `eval-review.json` — bare, the same as `_READ_SURFACE`'s
# entries, since a `Write(<path>)` rule is never consulted (`_WRITE_SCOPE_CAVEAT`).
# `#811`'s `--tools` narrowing (`_builtin_tool_surface` in `bin/agentctl`) derives the
# dispatch's TOOL SURFACE from this `allowed` tuple unioned with the agent's own roster
# grant; before that merge there was no `--tools` restriction at all, so an eval dispatch
# kept the full built-in surface and `Write` was reachable regardless of whether anything
# here named it. Neither this allow-list nor the eval variant agent's own `tools:` line
# (`tools/review-eval/variant-agents/reviewer.md`) named `Write`, so the merge silently
# dropped it from every eval dispatch's surface — see `_EVAL_MODE_RESULT_WRITE_CAVEAT`
# for where that write can actually land today.
_EVAL_MODE_RESULT_WRITE = ("Write",)

# Where that write actually lands (round-6 revise, continued — agent-platform#813;
# confinement tightened round 8, round-7 finding): restoring `Write` to the tool
# surface makes Claude Code willing to call the tool again, and the eval
# system-prompt directive (`bin/agentctl`'s `_eval_mode_system_prompt`) targets
# `RS.eval_verdict_path(scratch_dir)`, a FILE INSIDE THIS DISPATCH'S OWN
# `RS.create_eval_scratch_dir` DIRECTORY — never the shared temp root a build-class
# dispatch still gets (`SCRATCH_ROOTS`; `reviewer`/`security-reviewer`'s OWN non-eval
# policy does not get it either, as of round 2 of #925 — see
# `_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT`), and never `run_dir` itself, which
# `run_sandbox.settings()` denies to every run's own write unconditionally ("no run
# may write there", its own docstring; a deny rule wins over any allow rule, so
# nothing this module's `allowed` could reopen that). Round 6 put this path under
# the shared root, keyed only by `run_id` — readable by any other run and
# discoverable with `Glob`, so a sibling eval arm (several run against the same
# corpus entry at once) could plant or overwrite this run's verdict before the
# dispatcher ever read it back; round 8 is the fix (`run_sandbox.settings`'s
# `eval_scratch_dir` parameter grants ONLY this run's own directory). `Write`
# reaches it with no further loosening of the `runs_root` denial. `bin/agentctl`'s
# own dispatcher code (`_collect_eval_verdict`) reads that directory back once
# this run's claude process has exited — outside the sandbox, after the run can no
# longer rewrite what it wrote — validates it against `RP.VERDICTS` and against this
# run's own identity (never trusting a claimed run/repo/commit/PR that disagrees),
# and writes the result into `run_dir` as `eval-review.json` itself. The write
# target is reachable end to end, not merely restored at the tool-surface layer.
_EVAL_MODE_RESULT_WRITE_CAVEAT = (
    "`Write` REACHES ONLY THE RUN'S WORKTREE AND THIS DISPATCH'S OWN "
    "`RS.create_eval_scratch_dir` DIRECTORY — NEVER A RUN'S run_dir DIRECTLY. This is "
    "this eval dispatch's OWN scope; it does not describe what `Write` reaches under "
    "the ordinary (non-eval) review policy, which has its own caveat "
    "(`_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT`) stating that scope on its own terms. The eval "
    "directive's write target is "
    "`RS.eval_verdict_path(scratch_dir)`, inside a directory "
    "`RS.create_eval_scratch_dir` creates fresh for THIS dispatch alone — not "
    "`run_dir`, which `run_sandbox.py` denies to every run's own write, "
    "unconditionally, by design, and not any directory a SIBLING eval arm (several "
    "run against the same corpus entry at once) could also reach, which would "
    "otherwise let it plant or overwrite this run's verdict before the dispatcher "
    "ever read it back. `bin/agentctl`'s own dispatcher code reads that directory "
    "back after this run's claude process has exited, validates it, and writes the "
    "result into `run_dir` itself as `eval-review.json` — the file `agentctl review-eval` "
    "actually reads. The write target is reachable end to end: this is the "
    "complete route, not a partial one."
)


@dataclass(frozen=True)
class Policy:
    """The tools a run may use, and whether that is actually enforced.

    `enforced=False` is a first-class, honest state: it means the deny-list binds and
    NOTHING else does. Callers must not read an empty `allowed` as "no tools".
    """
    agent: str
    runtime: str
    repo: str | None
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    enforced: bool = False
    permission_mode: str = MODE_BYPASS
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """The record written to the run dir. What this run COULD do, in one place."""
        return {
            "agent": self.agent,
            "runtime": self.runtime,
            "repo": self.repo,
            "enforced": self.enforced,
            "permission_mode": self.permission_mode,
            "reason": self.reason,
            "allowed": list(self.allowed),
            "denied": list(self.denied),
            "notes": self.notes,
        }


def strip_app_prefix(agent_name: str) -> str:
    """`acme-reviewer` -> `reviewer`, `deck-security-reviewer` -> `security-reviewer`.
    Internal-app crews wear their app's prefix (ARCH-177/290/340) but carry the same
    role, so policy must follow the role.

    Delegates to `app_prefixes.strip_app_prefix` — the same stripping
    `bin/agentctl`'s `_is_review_agent` uses to decide a dispatch is review-class
    (ARCH-397). Before that module existed this was its own regex naming four
    app prefixes literally, a second list that never learned several more as
    the fleet grew — so a crew-prefixed reviewer under one of the missing
    prefixes resolved here as build-class (unenforced, empty allow-list)
    while agentctl dispatched it as review-class, and it could post no verdict."""
    return app_prefixes.strip_app_prefix(agent_name or "")


def repo_slug(repo: str | None) -> str | None:
    """`acme` -> `convivy/acme`. `gh --repo` wants owner/name; agentctl carries the
    short form. Returns None when there is no repo to scope to, which callers must
    treat as "cannot scope" rather than "scope to everything".

    The owner comes from `projects.repo_github_slug` — a registered repo's own
    `repo_owners` entry, else its unit's `github_owner`, else `convivy` — so a
    repo under a different GitHub owner (homebrew-tap, under convivybuilder)
    gets its real slug here too, used only in the caveat text `review_tools`
    prints below, not to scope any actual grant (see that function's own
    docstring on why the grants themselves aren't repo-scoped)."""
    if not repo:
        return None
    return projects.repo_github_slug(repo)


def review_tools(repo: str | None) -> tuple[list[str], list[str]]:
    """The `gh` / `agentctl` surface a reviewer needs. Returns (rules, caveats).

    THE REPO-SCOPING GAP, STATED RATHER THAN FAKED.

    The goal was to pin `--repo` into the GRANT, so a prompt-injected reviewer could not
    read another repo's CI logs: a permission error rather than an instruction the model
    may or may not obey. It cannot be done with permission rules. Bash rules glob-match
    the command TEXT, and a flag's value cannot be constrained independently of its
    position — `Bash(gh pr view --repo convivy/acme:*)` does not match the command agents
    actually write, `gh pr view 129 --repo convivy/acme`. A pattern loose enough to match
    (`*convivy/acme*`) is satisfied by putting the slug in a trailing comment, so it
    scopes nothing while looking like it does.

    So these grants are NOT repo-scoped, and the caveat says so. The real fix is to stop
    handing reviewers raw `gh` and route PR reads through `agentctl` wrappers that take
    the repo from the run's own AGENTCTL_REPO — scoping enforced in code, where it holds.
    Tracked as a follow-up; not faked here.
    """
    slug = repo_slug(repo)
    if slug is None:
        # Fail closed. With no repo bound to this spawn there is nothing to scope to, and
        # granting the full gh surface "just in case" is how a broad grant returns by
        # accident.
        return ["Bash(agentctl review-post:*)"], []

    rules = [
        # The read-only PR surface. The merge and approve verbs are absent here, and the
        # deny-list blocks them for non-review agents regardless.
        "Bash(gh pr checks:*)",
        "Bash(gh pr view:*)",
        "Bash(gh pr diff:*)",
        "Bash(gh run view:*)",
        "Bash(gh pr comment:*)",
        "Bash(agentctl review-post:*)",
    ]
    caveats = [
        f"`gh` grants are NOT scoped to {slug}: permission rules cannot constrain a flag "
        f"value independently of its position, so a crafted diff could still induce a "
        f"read against another repo the gh credential reaches. Closing this needs "
        f"agentctl wrappers that take the repo from AGENTCTL_REPO in code."
    ]
    return rules, caveats


# The Claude Code builtin tool names a local persona's own `tools:` line may name
# (issue_01M6D5D0B434781TXDPMWMB2PY), used only to decide which rule in that
# declared list this resolver carries into the persona's own allow-list. Not an
# exhaustive product-wide registry of every built-in Claude Code ships — just the
# ones a frontmatter `tools:` grant can legally name, the same vocabulary
# `_builtin_tool_surface` in `bin/agentctl` already treats as the built-in
# surface's own namespace (the text before a rule's first `(`). `Bash` is included
# deliberately: a local persona that explicitly asks for it is declaring itself
# build-class on its own terms, which is a real answer, not the silent default a
# missing `tools:` line gets today.
_LOCAL_PERSONA_BUILTIN_NAMES = frozenset({
    "Read", "Grep", "Glob", "Write", "Edit", "NotebookEdit", "Bash", "Task",
    "TodoWrite", "WebFetch", "WebSearch", "ToolSearch", "Agent", "Skill",
})


def _local_persona_allowed_and_rejected(
        local_tools: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Splits a local persona's declared `tools:` rules into what this resolver
    carries into `allowed` and what it drops (issue_01M6D5D0B434781TXDPMWMB2PY):
    a bare name or a `Name(...)` rule is carried when `Name` is a recognized
    Claude Code builtin (`_LOCAL_PERSONA_BUILTIN_NAMES`); an `mcp__`-prefixed
    name is carried as written — real on a non-private dispatch, and inert on a
    private one, where `bin/agentctl`'s own wall (ADR-0043) never attaches a kb,
    ops, or other MCP server regardless of what this allow-list names, so there
    is nothing here for that wall to reopen. Anything else — a misspelled or
    unrecognized name — is REJECTED: dropped from `allowed` rather than passed
    through, failing toward fewer tools rather than guessing at an unknown
    grant's shape."""
    allowed: list[str] = []
    rejected: list[str] = []
    for rule in local_tools:
        builtin = rule.split("(", 1)[0]
        if rule.startswith("mcp__") or builtin in _LOCAL_PERSONA_BUILTIN_NAMES:
            allowed.append(rule)
        else:
            rejected.append(rule)
    return tuple(allowed), tuple(rejected)


_LOCAL_PERSONA_UNCONDITIONAL_CAVEAT = (
    "ENFORCEMENT HERE IS UNCONDITIONAL — NOT GATED BY `AGENTCTL_ENFORCE_TOOL_POLICY`, "
    "AND THERE IS NO SWITCH THAT TURNS IT OFF. This is a local persona (a "
    "`local_agents: true` crew's own `.claude/agents` definition, never a roster "
    "agent the Library serves) that declared its own `tools:` line, which is this "
    "policy's entire `allowed` list: a persona that already says it needs nothing "
    "outside its own grant must be denied everything else on its very first "
    "dispatch, not after a canary period of recorded-but-unenforced runs. Nothing "
    "named here reopens a kb, ops, or other MCP server on a private dispatch: "
    "`bin/agentctl`'s own wall (ADR-0043) denies those unconditionally, before this "
    "policy's `allowed` is ever consulted for one."
)


def resolve(agent: str, runtime: str, repo: str | None, *,
            denied: list[str] | None = None, eval_mode: bool = False,
            local_tools: tuple[str, ...] | None = None) -> Policy:
    """The one place an agent's effective tool policy is decided.

    `denied` is the caller's deny-list (agentctl's `_disallowed_tools`), passed in
    rather than recomputed here so there is exactly one deny-list in the system.

    `eval_mode` (the review-replay eval harness, agent-platform#813 round 1): a
    review-class agent dispatched with `--eval-mode` gets the eval allow-list —
    the read/search surface and read-only git, with NO `gh` rule and NO
    `Bash(agentctl review-post:*)` rule — instead of `review_tools`'s normal grant.
    `agentctl spawn` only ever passes `eval_mode=True` for `agent` in
    `_REVIEW_AGENTS` (validated before this is called); for any other agent it is
    a no-op, so `base not in ENFORCEABLE_AGENTS` below never has to branch on it.

    `local_tools` (issue_01M6D5D0B434781TXDPMWMB2PY) is a local persona's own
    declared `tools:` grant — `bin/agentctl`'s `_local_persona_tool_rules`, which
    is `None` for every agent except one resolved repo-locally (never the
    Library) whose frontmatter had a `tools:` key at all. `None` (an agent with
    no `tools:` line, or any roster/Library agent) is a no-op: `base not in
    ENFORCEABLE_AGENTS` below resolves exactly as it always has. Given a value,
    it is consulted ONLY on that same `base not in ENFORCEABLE_AGENTS` branch: a
    local persona whose own chosen name collides with a roster role
    (`ENFORCEABLE_AGENTS` member) still gets that role's own policy, unchanged.
    """
    base = strip_app_prefix(agent)
    deny = tuple(denied or ())

    if runtime == INTERACTIVE:
        # Claude Code enforces `tools:` from the individuated frontmatter itself. This
        # resolver does not second-guess it; it exists here so the interactive path has
        # a named policy rather than an implicit one.
        return Policy(
            agent=agent, runtime=runtime, repo=repo, denied=deny, enforced=True,
            permission_mode="(claude-code session)",
            reason="interactive: Claude Code enforces the individuated `tools:` frontmatter",
        )

    if base not in ENFORCEABLE_AGENTS:
        if local_tools is not None:
            # A local persona's own declared grant IS its allow-list — the fix for
            # the bug this parameter exists to close: a persona that says
            # `tools: Read, Glob` was, before this branch existed, sent to the
            # honest-but-permissive state right below (enforced=False, deny-list
            # only), which reports no real restriction while `bin/agentctl`'s own
            # `TOOL_ALLOW` default handed the run bare `Bash` and the worktree's
            # `Edit(...)` grant regardless of what the persona declared.
            allowed, rejected = _local_persona_allowed_and_rejected(local_tools)
            notes = []
            if rejected:
                notes.append(
                    "DROPPED, NOT A RECOGNIZED TOOL RULE (failing toward fewer "
                    "tools rather than guessing at an unknown grant's shape): "
                    + ", ".join(rejected))
            notes.append(_LOCAL_PERSONA_UNCONDITIONAL_CAVEAT)
            return Policy(
                agent=agent, runtime=runtime, repo=repo, denied=deny,
                allowed=allowed, enforced=True, permission_mode=MODE_ENFORCE,
                reason=("local persona with a declared `tools:` line: the "
                        "frontmatter grant IS the allow-list — a persona that "
                        "declares itself restricted must actually be denied what "
                        "it did not ask for, not fall to the build-class branch's "
                        "bare `Bash`/`Edit`/`Write` default below."),
                notes=notes,
            )
        # The honest state. Say what binds, and say plainly what does not.
        return Policy(
            agent=agent, runtime=runtime, repo=repo, denied=deny, enforced=False,
            permission_mode=MODE_ENFORCE,
            reason=("build-class agent: needs arbitrary shell (pytest, venv activation, "
                    "make, psql), so an allow-list permissive enough to work would be "
                    "equivalent to full Bash. Deny-list is the only real control here. "
                    "Its file tools are bounded by run_sandbox's allow rules (the worktree "
                    "and /tmp) under `dontAsk`, which is the one control that binds for it."),
            notes=["`tools:` in this agent's roster entry is NOT enforced on the "
                   "autonomous path — do not read it as a permission boundary."],
        )

    if base == "web-researcher":
        # Its own branch, not the generic enforceable-agent path below: that path
        # adds `_READ_SURFACE`/`_GIT_READ`/`review_tools`'s `gh` grant, which assume
        # a git- or repo-reading role every other `ENFORCEABLE_AGENTS` member has and
        # `web-researcher`'s own definition does not — its roster says "READ-ONLY,
        # with no file, shell or repository tools" (platform/agents/web-researcher).
        # `allowed` is exactly `_WEB_RESEARCH_SURFACE` — see that constant's own
        # comment for why it is named here rather than read off the roster.
        return Policy(
            agent=agent, runtime=runtime, repo=repo,
            allowed=_WEB_RESEARCH_SURFACE, denied=deny, enforced=True,
            permission_mode=MODE_ENFORCE,
            reason=("read-only web-research agent: its whole job is searching the "
                    "web and reading the knowledge base, so the allow-list is "
                    "exactly its own roster grant — WebSearch, WebFetch, and the "
                    "two read-only kb tools — with no Bash, Edit, Write, or any of "
                    "the git/gh surface the other enforceable agents get, since "
                    "its own definition grants none of those."),
            notes=[_ALWAYS_ENFORCED_CAVEAT,
                   "runs under `dontAsk`, UNCONDITIONALLY: anything outside these "
                   "rules is DENIED, not prompted, whether or not "
                   "AGENTCTL_ENFORCE_TOOL_POLICY is set."],
        )

    if eval_mode:
        allowed = (list(_READ_SURFACE) + list(_GIT_READ) + list(_GIT_READ_BRANCH)
                  + list(_EVAL_MODE_RESULT_WRITE))
        if base in _LINT_AGENTS:
            allowed += list(_LINT_ONLY)
        # _WRITE_SCOPE_CAVEAT is NOT included here (round 8): it states the ORDINARY
        # (non-eval) scope — "reaches ... /tmp" — which is no longer true for an eval
        # dispatch; _EVAL_MODE_RESULT_WRITE_CAVEAT states the eval-specific scope in
        # full instead.
        notes = [_EVAL_MODE_NO_POST_CAVEAT, _EVAL_MODE_RESULT_WRITE_CAVEAT]
        if base in _LINT_AGENTS:
            notes.append(_LINT_FIX_CAVEAT)
            notes.append(_LINT_FIDELITY_CAVEAT)
        notes.append("runs under `dontAsk`, UNCONDITIONALLY for this dispatch: "
                     "anything outside these rules is DENIED, not prompted, whether "
                     "or not AGENTCTL_ENFORCE_TOOL_POLICY is set. Built-in read-only "
                     "Bash (grep, ls, find, sed -n) is auto-permitted by the mode, "
                     "which is why no grep rule is listed.")
        return Policy(
            agent=agent, runtime=runtime, repo=repo,
            allowed=tuple(allowed), denied=deny, enforced=True,
            permission_mode=MODE_ENFORCE,
            reason=("review-replay eval harness (`--eval-mode`): same read-only "
                    "surface as the ordinary review-class policy below, minus every "
                    "`gh` rule and `Bash(agentctl review-post:*)` — a replay must "
                    "never post to GitHub or record a review stamp, and the "
                    "replayed brief cannot be trusted to ask it not to (see notes) — "
                    "PLUS `Write`, the one capability the ordinary policy lacks here: "
                    "with `gh` and `review-post` withheld, `Write` is the only way a "
                    "replay can record its verdict at all (see notes for where that "
                    "still cannot land)."),
            notes=notes,
        )

    gh_rules, caveats = review_tools(repo)
    allowed = list(_READ_SURFACE) + list(_GIT_READ) + list(_GIT_READ_BRANCH) + gh_rules
    if base in _LINT_AGENTS:
        allowed += list(_LINT_ONLY)
    if base in _VERDICT_AGENTS:
        # platform/specs/reviewers-run-under-their-allow-list: the additional
        # read-only git, the CI read, and the two `kb` reads measured against real
        # review-class usage — scoped to _VERDICT_AGENTS alone, the same "a grant
        # stays only if real usage needs it" rule _LINT_AGENTS already follows,
        # since the measurement behind these is 470 reviewer/security-reviewer
        # runs, not planner's or scout's.
        allowed += (list(_GIT_READ_MORE) + list(_GIT_REMOTE_V) + list(_CI_ONLY)
                   + list(_REVIEW_KB_SURFACE))
        # The verdict is the review's only output. Its own server posts it (run_verdict).
        allowed += [_VERDICT_TOOL]
    notes = list(caveats)
    # FIRST, because it qualifies everything after it. The two review roles get their
    # own version: their enforcement has no env-var gate at all (see that caveat).
    notes.insert(0, _VERDICT_ENFORCEMENT_CAVEAT if base in _VERDICT_AGENTS
                 else _ENFORCEMENT_GATE_CAVEAT)
    # round 2 of #925: `reviewer`/`security-reviewer`'s `allowWrite` is their own
    # draft directory, never the shared /tmp root `_WRITE_SCOPE_CAVEAT` names — see
    # `_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT`'s own comment.
    notes.append(_REVIEW_DRAFT_WRITE_SCOPE_CAVEAT if base in _VERDICT_AGENTS
                else _WRITE_SCOPE_CAVEAT)
    if base in _LINT_AGENTS:
        notes.append(_LINT_FIX_CAVEAT)
        notes.append(_LINT_FIDELITY_CAVEAT)
    notes.append("runs under `dontAsk`: anything outside these rules is DENIED, not "
                 "prompted. Built-in read-only Bash (grep, ls, find, sed -n) is "
                 "auto-permitted by the mode, which is why no grep rule is listed.")
    return Policy(
        agent=agent, runtime=runtime, repo=repo,
        allowed=tuple(allowed), denied=deny, enforced=True,
        permission_mode=MODE_ENFORCE,
        reason=("read-only/review-class agent: needs no arbitrary shell, so the "
                "allow-list is a real control — no arbitrary commands, and no git write "
                "verbs, so it cannot COMMIT or PUSH the tree it reviews. `ruff check "
                "--isolated .` is granted to reviewer/security-reviewer because it parses "
                "without executing, because `--isolated` ignores a repo-supplied config "
                "that could turn a check into a rewrite, and because it is granted as an "
                "EXACT command so the `--fix` family cannot be appended (see notes); `pytest` "
                "is withheld outright because it executes `conftest.py` from the tree "
                "under review, which would make that git-write control untrue via "
                "subprocess. It CAN still "
                "write files there: `Write` is unscoped because path-scoped Write rules "
                "do not bind (see notes), and the `gh` grant is not repo-scoped either. "
                "Two named holes, both in notes — do not read this as a sandbox."),
        notes=notes,
    )
