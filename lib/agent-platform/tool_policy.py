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

import re
from dataclasses import dataclass, field

import projects

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
ENFORCEABLE_AGENTS = frozenset({
    "reviewer",
    "security-reviewer",
    "planner",
    "platform-planner",
    "design-critic",
    "scout",
})

# The read/search surface every enforceable agent gets. These are TOOLS, not shell:
# giving the agent Grep and Read is what lets us withhold `Bash(grep ...)` without
# crippling it.
#
# `Write` is here, UNSCOPED, and that is a known hole rather than an oversight. A reviewer
# composes its findings to a file before `review-post --body-file` reads it — mined usage
# shows 670 Write calls from `reviewer` alone, so withholding Write breaks every review
# immediately. `Write` is not on this list. Its scope is the run's `Edit(<path>)` allow
# rules in run_sandbox (the worktree and /tmp), which Claude Code applies to `Write`
# under `dontAsk`. A `Write(<path>)` rule is never consulted (Claude Code 2.1.292
# permissions docs, "Read and Edit"), so the scope is written as `Edit(...)`. Tested on
# 2.1.292: a `Write` outside the allow rule was refused, and one inside it succeeded.
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
# indistinguishable from real task context. This matters most here: reviewer and
# security-reviewer are the two ENFORCEABLE_AGENTS members that adjudicate adversarial
# diffs, and an issue pulled up mid-review is a channel straight into that verdict.
_READ_SURFACE = ("Read", "Grep", "Glob", "mcp__ops__issue_get", "mcp__ops__issue_list")
# A review's verdict goes through `mcp-verdict-server` (knowledge#793), outside the run's
# Bash. Only the two review roles post one.
_VERDICT_TOOL = "mcp__verdict__review_post"
_VERDICT_AGENTS = frozenset({"reviewer", "security-reviewer"})

# The gap above, in the words that go into policy.json. Stated, not faked.
_WRITE_SCOPE_CAVEAT = (
    "`Write` and `NotebookEdit` reach only the run's worktree and /tmp: run_sandbox "
    "grants `Edit(<path>)` for those two roots and nothing else, and `dontAsk` refuses "
    "every other write. Bash is not bounded by this; the sandbox bounds it. A "
    "prompt-injected reviewer can still write anywhere under /tmp and its worktree."
)

_ENFORCEMENT_GATE_CAVEAT = (
    "READ EVERY GUARANTEE ON THIS POLICY AS CONDITIONAL. The allow-list binds only "
    "when `AGENTCTL_ENFORCE_TOOL_POLICY` is set AND this policy is `enforced`; the "
    "env var defaults to OFF (ARCH-346), so by default the run executes under "
    "`bypassPermissions` with NONE of these restrictions applied — only the deny-list "
    "binds. `enforced: true` here means this policy CLASS is enforceable, not that the "
    "run you are reading about was enforced. Demonstrated live during the review of "
    "PR #391: a security-reviewer dispatch ran under `bypassPermissions` and "
    "successfully executed the very `ruff --fix` the exact-match grant denies. This "
    "note exists because `reason` and the caveats below state their guarantees "
    "unconditionally, and a reader of this record could not otherwise tell which runs "
    "they describe."
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
_GIT_READ = tuple(f"Bash(git {v}:*)" for v in
                  ("diff", "log", "show", "status", "rev-parse", "blame", "branch"))

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
    "`AGENTCTL_ENFORCE_TOOL_POLICY` (see `_spawn_claude`), so — unlike the ordinary "
    "review policy below — this allow-list is never merely resolved-but-unapplied."
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
# `RS.create_eval_scratch_dir` DIRECTORY — never the shared temp root every other
# dispatch gets (`SCRATCH_ROOTS`), and never `run_dir` itself, which
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
    "`Write` REACHES THE WORKTREE AND THIS DISPATCH'S OWN DIRECTORY ONLY — UNLIKE "
    "THE ORDINARY (non-eval) REVIEW POLICY, WHERE IT REACHES THE WHOLE SHARED TEMP "
    "ROOT (`_WRITE_SCOPE_CAVEAT`) — NEVER A RUN'S run_dir DIRECTLY. The eval "
    "directive's write target is "
    "`RS.eval_verdict_path(scratch_dir)`, inside a directory "
    "`RS.create_eval_scratch_dir` creates fresh for THIS dispatch alone — not "
    "`run_dir`, which `run_sandbox.py` denies to every run's own write, "
    "unconditionally, by design, and not the shared temp root every non-eval "
    "dispatch still gets, which a sibling eval arm could otherwise plant a verdict "
    "into. `bin/agentctl`'s own dispatcher code reads that directory back after "
    "this run's claude process has exited, validates it, and writes the result "
    "into `run_dir` itself as `eval-review.json` — the file `agentctl review-eval` "
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
    """`hollis-reviewer` -> `reviewer`. Internal-app crews wear their app's prefix
    (ARCH-177/290/340) but carry the same role, so policy must follow the role."""
    return re.sub(r"^(hollis|pa|sel|svc)-", "", agent_name or "")


def repo_slug(repo: str | None) -> str | None:
    """`vivy` -> `convivy/vivy`. `gh --repo` wants owner/name; agentctl carries the
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
    position — `Bash(gh pr view --repo convivy/vivy:*)` does not match the command agents
    actually write, `gh pr view 129 --repo convivy/vivy`. A pattern loose enough to match
    (`*convivy/vivy*`) is satisfied by putting the slug in a trailing comment, so it
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


def resolve(agent: str, runtime: str, repo: str | None, *,
            denied: list[str] | None = None, eval_mode: bool = False) -> Policy:
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

    if eval_mode:
        allowed = list(_READ_SURFACE) + list(_GIT_READ) + list(_EVAL_MODE_RESULT_WRITE)
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
    allowed = list(_READ_SURFACE) + list(_GIT_READ) + gh_rules
    if base in _LINT_AGENTS:
        allowed += list(_LINT_ONLY)
    if base in _VERDICT_AGENTS:
        # The verdict is the review's only output. Its own server posts it (run_verdict).
        allowed += [_VERDICT_TOOL]
    notes = list(caveats)
    # FIRST, because it qualifies everything after it.
    notes.insert(0, _ENFORCEMENT_GATE_CAVEAT)
    notes.append(_WRITE_SCOPE_CAVEAT)
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
