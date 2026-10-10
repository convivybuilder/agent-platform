"""Every plist whose program dispatches agents must tag AGENT_RUN_ORIGIN.

THE INCIDENT. com.convivy.issue-queue-drain fired at 03:30 on 2026-09-03 and
dispatched a planning-orchestrator that ran three hours, spawned 61 runs, and
spent $89.02 — every one of them stamped `origin=interactive` in its
meta.json, because nothing between the plist and `agentctl spawn` ever set
AGENT_RUN_ORIGIN. `interactive` is deliberately absent from
budget.CAPPED_ORIGINS (it's the operator's uncapped in-the-loop work), so the
overnight pool's soft/hard caps never applied to any of it.

Every OTHER scheduled dispatcher goes through a shell launcher that exports
the origin before calling `agentctl spawn` (launch-overnight, launch-
overnight-hollis, launch-weekly-curation, launch-weekly-consulting).
issue-queue-drain is a Python program its plist invokes directly, with no
launcher, so it never picked up the convention — fixed by giving it its own
`os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")`.

WHY A GENERIC SWEEP AND NOT A NAMED LIST OF TODAY'S JOBS. A test hardcoding
today's dispatchers proves nothing about a fifth one added next month — the
exact gap that let issue-queue-drain go unnoticed. So this walks
launchd/*.plist, resolves each one's ProgramArguments (or Program) to the
bin/ source it actually runs, and for every program whose OWN source calls
`agentctl spawn` requires that either that source or the plist's
EnvironmentVariables sets AGENT_RUN_ORIGIN.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import plistlib
import re
from pathlib import Path

import budget
import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHD = REPO / "launchd"
BIN = REPO / "bin"

# The budget gate's OWN capped set rather than a copy restated here. A
# hardcoded list would keep passing after someone changed which origins are
# capped, which is the one way this sweep could go quietly wrong. conftest.py
# puts lib/agent-platform on sys.path ahead of collection.
CAPPED_ORIGINS = frozenset(budget.CAPPED_ORIGINS)

# Both live locations a plist may point a program at; either resolves to the
# same source file in this repo's bin/.
_PROGRAM_PREFIXES = (
    "__HOME__/.local/deploy/agent-platform-shared/bin/",
    "__HOME__/.local/bin/",
)

# Interpreters/shells that appear as argv[0] ahead of the real script and
# carry no dispatch logic of their own.
_INTERPRETERS = {
    "/bin/sh",
    "/opt/homebrew/bin/python3.11",
    "__HOME__/.local/deploy/agent-platform-shared/venv/bin/python3",
}

# Known pre-existing gaps this change does not fix, in the same shape as
# test_plists_are_deploy_pinned.py's NOT_YET_DEPLOY_PINNED. Both plists
# invoke their program directly with no launcher — exactly the shape
# com.convivy.issue-queue-drain.plist was in — and neither the program nor the
# plist sets AGENT_RUN_ORIGIN. This sweep found them; fixing them is out of
# scope for the issue-queue-drain incident (see the PR body). Deleting a
# line here is how a fix is recorded.
# Plists whose program has no source in this repo's bin/, so there is nothing
# here to inspect for a dispatch. Named rather than skipped: an unresolvable
# plist and a compliant one used to produce the same green, which is how a job
# under a new deploy-path convention would fall through the hole
# com.convivy.issue-queue-drain fell through (round-1 review). Both of these are
# genuine system programs, not agent-platform code.
KNOWN_UNRESOLVABLE = {
    "com.convivy.ip-forwarding.plist": "/usr/sbin/sysctl, a system binary",
    "com.convivy.sshd-watchdog.plist": "/usr/local/sbin/sshd-watchdog, not in this repo",
    "com.convivy.agent-postgres.plist": "/opt/homebrew/opt/postgresql@15/bin/postgres, a system binary — see bin/agent-postgres-provision",
}

KNOWN_UNFIXED = {
    "com.convivy.changelog-drain.plist": "pre-existing gap, out of scope for this fix",
    "com.convivy.dependabot-review.plist": "pre-existing gap, out of scope for this fix",
}


def _argv(path: Path) -> list[str]:
    """Every argv entry of a plist, however the job names its program.

    A plist may use `ProgramArguments` (an array) or the bare `Program` key.
    """
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    argv = list(data.get("ProgramArguments") or [])
    program = data.get("Program")
    if isinstance(program, str):
        argv.append(program)
    return [a for a in argv if isinstance(a, str)]


def _resolve_program(argv: list[str]) -> Path | None:
    """The bin/ source a plist's argv actually runs, or None if there is no
    repo source behind it (a bare system binary).

    RESOLUTION IS BY BASENAME, not by deploy-path prefix, and that is the fix
    for a hole the round-1 review found. The two hardcoded prefixes missed
    `$HOME/.local/deploy/run-register-api/bin/run-register-api` —
    a per-service deploy root — so that plist resolved to None, and None was
    treated everywhere as "nothing to check" rather than "could not classify
    this". A sweep whose unresolvable case and whose compliant case produce the
    same green is not a sweep, which is precisely the failure this file exists
    to prevent. Any argv entry whose basename names a real file in bin/ now
    resolves, whatever deploy root it came from.
    """
    for entry in argv:
        if entry in _INTERPRETERS:
            continue
        if not (entry.startswith("/") or entry.startswith("__HOME__")):
            continue
        candidate = BIN / Path(entry).name
        if candidate.exists():
            return candidate
    return None


def _is_unresolvable(plist_path: Path) -> bool:
    """True if the plist names a program with no source in this repo's bin/.

    Kept separate from "resolved but sets no origin" so the two cannot be
    confused for one another in a test's assertion.
    """
    return _resolve_program(_argv(plist_path)) is None


def _code_lines(source: Path) -> list[str]:
    """The program's source with COMMENTS AND DOCSTRINGS removed, keeping every
    ordinary string literal.

    The distinction is prose-as-a-statement versus a string inside an
    expression, and BOTH halves are load-bearing:

    Dropping docstrings is what the round-1 review's widened resolver forced.
    `bin/run-register-api` discusses `agentctl spawn <cfo-agent>` in its module
    docstring, which opens with a quote rather than a `#`, so `#`-stripping
    alone read a file that TALKS about a call as one that MAKES one.

    Keeping ordinary literals is what stops the cure being worse: the real
    dispatch is `cmd = [str(AGENTCTL), "spawn", ...]`, where the subcommand is
    itself a string. Stripping every string hides the very call this sweep
    exists to find — it made `bin/issue-queue-drain`, the program this whole
    change is about, look like it dispatches nothing.

    Shell sources have no comparable cheap parser and keep the `#` heuristic.
    A shell file discussing agentctl in prose would fail toward FLAGGING, which
    is the safe direction.
    """
    text = source.read_text()
    lines = text.splitlines()
    if not re.match(r"#!.*python", (lines[0] if lines else "")):
        return [ln for ln in lines if not ln.strip().startswith("#")]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Unparseable: fall back rather than skip. Skipping is the exact failure
        # mode this sweep exists to prevent.
        return [ln for ln in lines if not ln.strip().startswith("#")]
    prose: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for n in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                prose.add(n)
    return [ln for i, ln in enumerate(lines, 1)
            if i not in prose and not ln.strip().startswith("#")]


def _dispatches_agents(source: Path) -> bool:
    """True if the program's own CODE invokes `agentctl spawn` — a mention in a
    comment or a docstring is not a dispatch."""
    for line in _code_lines(source):
        if re.search(r"agentctl", line, re.I) and re.search(r"\bspawn\b", line):
            return True
    return False


# An UNCONDITIONAL ASSIGNMENT of the variable to a capped origin. Not a mention,
# not a read, and — the round-2 finding — not a CONDITIONAL set either. `%s` is
# filled with each capped origin in turn.
#
# THE SEPARATOR MUST BE `=` AND NEVER `,`, and getting that wrong made this
# guard certify the exact bug the change it guards was written to remove. Both
# reviewers found it from opposite ends of one family: every form that passes
# the name and the value as two ARGUMENTS rather than assigning one to the other.
#
#   os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")   # sets only if absent
#   os.environ.get("AGENT_RUN_ORIGIN", "scheduled")          # sets nothing at all
#
# The first is what bin/issue-queue-drain used to do and what this PR removed,
# because it loses to a poisoned inherited value; the second is the shape
# bin/agentctl itself uses to READ the origin, so it is the likeliest thing a
# future author copies. With `,` in the alternation the sweep reported both as
# compliant, so a program could reintroduce the incident with this check
# actively vouching for it.
#
# `_READS_ORIGIN` below is a second, independent refusal rather than a tidier
# regex, because these two mistakes cost a review round each and the argument
# form is the one an author reaches for by habit.
# THE ASSIGNMENT MUST ALSO REACH THE CHILD PROCESS, which is the round-4 finding
# and the fifth member of this family. A bare shell assignment on its own line —
# `AGENT_RUN_ORIGIN=scheduled`, or `declare`/`local`/`readonly` without `-x` —
# sets a SHELL variable that bash never puts in a child's environment. The
# `agentctl spawn` such a dispatcher later calls would see no origin at all, so
# counting it as compliant certifies exactly the uncapped incident this file
# exists to prevent. Only three shell forms actually export:
#
#   export AGENT_RUN_ORIGIN=scheduled          the four real launchers use this
#   declare -x AGENT_RUN_ORIGIN=scheduled      and typeset/readonly with -x
#   AGENT_RUN_ORIGIN=scheduled some-command    an inline prefix, exported to that
#                                              command only — so it must be
#                                              FOLLOWED by one, and a bare
#                                              assignment at end of line is not
#
# Python's `os.environ[...] = ...` writes the process environment directly and is
# matched by its own alternative, which requires the closing `]` that a shell
# assignment never has.
#
# Two exporting shapes are deliberately NOT matched: a bare assignment followed
# by a separate `export AGENT_RUN_ORIGIN` line, and a bare assignment under
# `set -a`. Both span lines, and this matcher reads one line at a time. Missing a
# real set FLAGS a compliant job, which is the safe direction and the same
# trade-off already accepted for os.environ.update() and os.putenv().
_ASSIGNS_ORIGIN = (
    r"""(?:"""
    # Python, and it must be os.environ SPECIFICALLY. Matching any object's
    # ["AGENT_RUN_ORIGIN"] key certified `meta[...] = "scheduled"` — a metadata
    # dict that reaches no child — as compliant (round-5 review). Requiring
    # `environ` costs a false NEGATIVE on a custom dict later passed as
    # subprocess(env=...), which flags a compliant job; accepting any object
    # costs a false POSITIVE that vouches for a broken one. Only one of those
    # two directions is safe.
    r"""os\.environ\s*\[\s*["']AGENT_RUN_ORIGIN["']\s*\]\s*=\s*["']?%(v)s\b"""
    # Shell, exported explicitly.
    r"""|export\s+AGENT_RUN_ORIGIN\s*=\s*["']?%(v)s\b"""
    r"""|(?:declare|typeset|readonly)\s+(?:-\w+\s+)*-\w*x\w*\s+"""
    r"""AGENT_RUN_ORIGIN\s*=\s*["']?%(v)s\b"""
    # Shell inline prefix. What follows must plausibly BEGIN A COMMAND, not
    # merely be non-whitespace. `\S` accepted `#`, `;` and `&`, so
    # `AGENT_RUN_ORIGIN=scheduled  # for the spawn below` read as an export —
    # verified against a real shell by the round-5 review, which spawned
    # children and found the variable absent from every one. A comment, a
    # separator, a subshell close and a redirect all leave the assignment bare.
    # The `(?!\d*[<>])` is the fd-number case: the exclusion class inspects only
    # the FIRST character after the value, so `2>log`, `1>>out` and `0<in` all
    # led with a digit and passed as "a command follows". Both reviewers found
    # it independently and both spawned a real shell to confirm the variable is
    # absent from a child — a bare assignment with only a redirect forks
    # nothing, so it stays a shell variable.
    r"""|(?:^|[;&|]\s*|\s)AGENT_RUN_ORIGIN=["']?%(v)s["']?\s+(?!\d*[<>]|\{\w+\}[<>])[^\s#;&|)<>]"""
    r""")"""
)

# Forms that are NOT an unconditional set, refused outright whatever else the
# line looks like. Two shapes, found one review round apart, same family.
#
# CALL-ARGUMENT forms pass the name and the value as two arguments:
#   os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")   conditional set
#   os.environ.get("AGENT_RUN_ORIGIN", "scheduled")          pure read
#
# SHELL PARAMETER EXPANSION is the same mistake in the other language, and it is
# the one this file most needed to catch, since three of the four dispatchers it
# scans are shell:
#   : "${AGENT_RUN_ORIGIN=scheduled}"    sets ONLY if unset — bash's setdefault
#   : "${AGENT_RUN_ORIGIN:=scheduled}"   sets only if unset or empty
#   "${AGENT_RUN_ORIGIN:-scheduled}"     read with a default, sets nothing
#
# The `=` forms are exactly the footgun bin/issue-queue-drain's fix removed:
# they lose to a poisoned inherited value. The read-with-default form is the
# pattern already common in these scripts, so the assignment cousin is what a
# future author reaches for by analogy. Refusing the whole `${...}` shape is
# deliberate — relying on _ASSIGNS_ORIGIN to not accidentally match it is how
# the colon form came to be rejected by luck rather than by design.
_READS_ORIGIN = re.compile(
    r"""\.\s*(?:get|setdefault|pop|getenv)\s*\(\s*["']AGENT_RUN_ORIGIN["']"""
    r"""|getenv\s*\(\s*["']AGENT_RUN_ORIGIN["']"""
    r"""|\$\{\s*AGENT_RUN_ORIGIN\s*[:]?[-=?+]""",
)


def _line_sets_origin(line: str) -> bool:
    """Whether ONE source line unconditionally sets the origin to a capped
    value — the exact predicate `_sets_origin` applies per line, factored out so
    the tests below exercise the real classifier rather than a copy of it."""
    if _READS_ORIGIN.search(line):
        return False
    return any(re.search(_ASSIGNS_ORIGIN % {"v": re.escape(o)}, line)
               for o in CAPPED_ORIGINS)


def _sets_origin(plist_path: Path, source: Path) -> bool:
    """True if the job actually sets AGENT_RUN_ORIGIN to a CAPPED origin.

    Two things this refuses to accept, because a sweep that can be satisfied
    without capping anything is worse than no sweep (security review, round 1):

    THE NAME IS NOT THE SETTING. `_dispatches_agents` above already strips
    comment lines for exactly this reason, and this function did not. A future
    dispatcher that merely MENTIONS AGENT_RUN_ORIGIN in a header comment — the
    likely outcome of copying this program's own comment block while dropping
    its assignment — would otherwise pass while stamping every run
    `interactive`.

    THE KEY IS NOT THE VALUE. A plist carrying
    `<key>AGENT_RUN_ORIGIN</key><string>interactive</string>` sets the variable
    and caps nothing. `interactive` is telemetry.DEFAULT_ORIGIN, so it is both
    the easiest value to land on by accident and the only one that reproduces
    the incident this sweep exists to prevent.
    """
    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)
    env = data.get("EnvironmentVariables") or {}
    if "AGENT_RUN_ORIGIN" in env:
        return env["AGENT_RUN_ORIGIN"] in CAPPED_ORIGINS
    for line in _code_lines(source):
        if "AGENT_RUN_ORIGIN" not in line:
            continue
        if _line_sets_origin(line):
            return True
    return False


def _plists() -> list[Path]:
    return sorted(LAUNCHD.glob("*.plist"))


def test_there_are_plists_to_check():
    """A glob that silently matched nothing would make every test below vacuous."""
    assert len(_plists()) > 20


def test_every_agent_dispatching_job_sets_agent_run_origin():
    """The check that would have caught issue-queue-drain going uncapped.

    A program reached only via a shell launcher inherits AGENT_RUN_ORIGIN
    from whatever the launcher exports; a program a plist invokes directly,
    with no launcher, must set its own default — issue-queue-drain's fix.
    """
    offenders = {}
    for plist_path in _plists():
        if plist_path.name in KNOWN_UNFIXED:
            continue
        source = _resolve_program(_argv(plist_path))
        if source is None or not _dispatches_agents(source):
            continue
        if not _sets_origin(plist_path, source):
            offenders[plist_path.name] = source.name
    assert not offenders, (
        "These plists dispatch agents (their program calls `agentctl spawn`) but "
        "neither the program nor the plist's EnvironmentVariables sets "
        "AGENT_RUN_ORIGIN, so every run they spawn is stamped `interactive` and "
        "the budget gate's CAPPED_ORIGINS never sees it — this is exactly how "
        "com.convivy.issue-queue-drain went uncapped. Fix: add "
        '`os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")` near the top of '
        f"the program, the way bin/issue-queue-drain now does: {offenders}"
    )


def test_the_known_unfixed_allowlist_holds_no_stale_entry():
    """A fixed plist must lose its line, or the allowlist stops meaning anything."""
    stale = []
    for name in KNOWN_UNFIXED:
        plist_path = LAUNCHD / name
        if not plist_path.exists():
            stale.append(name)
            continue
        source = _resolve_program(_argv(plist_path))
        if source is None or not _dispatches_agents(source):
            stale.append(name)
            continue
        if _sets_origin(plist_path, source):
            stale.append(name)
    assert not stale, (
        f"These are listed as known-unfixed but no longer need to be — delete "
        f"their lines from KNOWN_UNFIXED: {stale}"
    )


# `test_issue_queue_drain_plist_sets_agent_run_origin` stood here, pinning that
# one plist's program set AGENT_RUN_ORIGIN. The plist was deleted on 2026-09-10
# when the overnight cron fleet was retired (the operator), so the test had nothing to
# read. The general sweep above still covers every plist that remains, which is
# what protects the next scheduled dispatcher.
#
# WHAT WENT WITH IT, stated plainly rather than glossed: both the sweep and the
# child-process runtime check build their targets by walking `_plists()`, so with
# this plist gone NEITHER touches `bin/issue-queue-drain` any more. The only test
# still reading that file is the dispatcher-classification one below, which does
# not check that it sets AGENT_RUN_ORIGIN — the very thing the 2026-09-03 uncapped
# firing in this file's docstring was about. An earlier version of this comment
# claimed the env-handling tests still exercised it; they do not (reviewer,
# PR #520). The script has no caller left in the tree either, so nothing runs it
# today — but if it is ever wired to a trigger again, that coverage has to come
# back with it.
def test_an_unresolvable_plist_must_be_NAMED_not_silently_skipped():
    """The hole this closes: `_resolve_program` returning None was treated
    everywhere as "nothing to check", so a plist the resolver could not classify
    produced exactly the same green as a compliant one. A check whose
    could-not-look state and whose looked-and-it-was-fine state are
    indistinguishable is not a check — and it is the same shape as the gap that
    let com.convivy.issue-queue-drain run uncapped for months.

    A new unresolvable plist now fails here until someone names it and says why.
    """
    unresolvable = {
        pl.name for pl in _plists() if _resolve_program(_argv(pl)) is None
    }
    unnamed = unresolvable - set(KNOWN_UNRESOLVABLE)
    assert not unnamed, (
        "These plists name a program with no source in bin/, so this sweep "
        "cannot tell whether they dispatch agents or set an origin. Add each to "
        "KNOWN_UNRESOLVABLE with a reason, or fix resolution to see it: "
        f"{sorted(unnamed)}"
    )


def test_the_unresolvable_allowlist_holds_no_stale_entry():
    """A plist that becomes resolvable must lose its line, or the allowlist
    stops meaning anything — the same discipline KNOWN_UNFIXED is held to."""
    stale = {
        name for name in KNOWN_UNRESOLVABLE
        if (LAUNCHD / name).exists()
        and _resolve_program(_argv(LAUNCHD / name)) is not None
    }
    assert not stale, (
        f"resolvable now, so remove from KNOWN_UNRESOLVABLE: {sorted(stale)}")


def test_the_resolver_sees_a_program_under_ANY_deploy_root():
    """Resolution is by basename, and this pins the specific case that exposed
    the hole: com.convivy.run-register-api.plist points at
    $HOME/.local/deploy/run-register-api/bin/run-register-api, a
    per-service deploy root matching neither of the two prefixes the resolver
    used to hardcode."""
    plist = LAUNCHD / "com.convivy.run-register-api.plist"
    if not plist.exists():
        pytest.skip("com.convivy.run-register-api.plist is gone")
    resolved = _resolve_program(_argv(plist))
    assert resolved is not None, "a per-service deploy root must still resolve"
    assert resolved.name == "run-register-api"
    assert resolved.exists()


def test_prose_about_agentctl_spawn_is_not_a_dispatch():
    """bin/run-register-api discusses `agentctl spawn <cfo-agent>` in its module
    docstring and calls nothing. Stripping only `#` lines counted it as a
    dispatcher, which the widened resolver surfaced. Pins the fix, and pins the
    other half too — bin/issue-queue-drain's real dispatch passes the subcommand
    as the string literal "spawn", so a fix that stripped every string would
    have hidden the one program this whole change is about."""
    prose = BIN / "run-register-api"
    real = BIN / "issue-queue-drain"
    if not prose.exists() or not real.exists():
        pytest.skip("expected bin/ programs are absent")
    assert _dispatches_agents(prose) is False, "a docstring mention is not a call"
    assert _dispatches_agents(real) is True, "a real dispatch must still be seen"


# Every form is checked against the SAME helper the sweep uses, so this cannot
# drift from the real classifier the way a hand-rolled copy would.
_SETS = [
    'os.environ["AGENT_RUN_ORIGIN"] = "scheduled"',
    "os.environ['AGENT_RUN_ORIGIN'] = 'scheduled'",
    'export AGENT_RUN_ORIGIN=scheduled',
    'export AGENT_RUN_ORIGIN="curation"',
    # An inline prefix exports to the command it precedes, so it counts — but
    # only because a command FOLLOWS it. See the bare form in _DOES_NOT_SET.
    'AGENT_RUN_ORIGIN=scheduled exec "$@"',
    'declare -x AGENT_RUN_ORIGIN=scheduled',
    'readonly -x AGENT_RUN_ORIGIN=scheduled',
    'declare -gx AGENT_RUN_ORIGIN=scheduled',
    # A line continuation is one logical statement in bash, so the command on
    # the next physical line does receive it.
    'AGENT_RUN_ORIGIN=scheduled \\',
    'os.environ ["AGENT_RUN_ORIGIN"] = "scheduled"',
]
_DOES_NOT_SET = [
    # Conditional set: what bin/issue-queue-drain used to do, removed by this
    # PR because it loses to a poisoned inherited value.
    'os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")',
    # Pure reads. The first is the shape bin/agentctl itself uses, so it is the
    # likeliest thing a future author copies.
    'origin = os.environ.get("AGENT_RUN_ORIGIN", "scheduled")',
    'origin = os.getenv("AGENT_RUN_ORIGIN", "scheduled")',
    'old = os.environ.pop("AGENT_RUN_ORIGIN", "scheduled")',
    # Comparisons.
    'if os.environ["AGENT_RUN_ORIGIN"] == "scheduled":',
    'if os.environ.get("AGENT_RUN_ORIGIN") == "scheduled":',
    # Prose.
    '# AGENT_RUN_ORIGIN is scheduled for this job',
    'AGENT_RUN_ORIGIN must be "scheduled" here',
    # Shell parameter expansion — the same family in the other language, and the
    # half that matters most here since three of the four dispatchers this sweep
    # scans are shell. The `=` forms set ONLY IF UNSET, so they lose to exactly
    # the poisoned inherited value that bin/issue-queue-drain's fix removed; the
    # `-` forms read with a default and set nothing. The read-with-default shape
    # is already common in these scripts, which is why a future author reaches
    # for its assignment cousin by analogy.
    ': "${AGENT_RUN_ORIGIN=scheduled}"',
    ': "${AGENT_RUN_ORIGIN:=scheduled}"',
    'o="${AGENT_RUN_ORIGIN-scheduled}"',
    'o="${AGENT_RUN_ORIGIN:-scheduled}"',
    'o="${AGENT_RUN_ORIGIN:?scheduled}"',
    'o="${AGENT_RUN_ORIGIN:+scheduled}"',
    # A SHELL VARIABLE IS NOT AN ENVIRONMENT VARIABLE, which is the round-4
    # finding. bash never hands an unexported variable to a child, so the
    # `agentctl spawn` these dispatchers go on to call would see no origin at
    # all — the uncapped incident again, with this sweep vouching for it. The
    # bare form differs from the inline prefix in _SETS by exactly one thing:
    # nothing follows it, so there is no command to export to.
    'AGENT_RUN_ORIGIN=scheduled',
    'declare AGENT_RUN_ORIGIN=scheduled',
    'local AGENT_RUN_ORIGIN=scheduled',
    'readonly AGENT_RUN_ORIGIN=scheduled',
    'typeset AGENT_RUN_ORIGIN=scheduled',
    # WHAT FOLLOWS AN INLINE PREFIX MUST PLAUSIBLY BEGIN A COMMAND. Requiring
    # only non-whitespace let a comment marker, a separator, a subshell close
    # and a redirect all pass as "a command follows", so a bare assignment
    # certified as an export. The round-5 review verified these against a real
    # shell rather than the regex: it spawned children and found
    # AGENT_RUN_ORIGIN absent from every one.
    'AGENT_RUN_ORIGIN=scheduled  # for the spawn below',
    'AGENT_RUN_ORIGIN=scheduled ; agentctl spawn foo',
    'AGENT_RUN_ORIGIN=scheduled & wait',
    'AGENT_RUN_ORIGIN=scheduled >file',
    # File-descriptor-numbered redirects: the digit led, so the exclusion class
    # never saw the operator behind it.
    'AGENT_RUN_ORIGIN=scheduled 2>/dev/null',
    'AGENT_RUN_ORIGIN=scheduled 2>&1',
    'AGENT_RUN_ORIGIN=scheduled 1>>/tmp/log.txt',
    'AGENT_RUN_ORIGIN=scheduled 0<input',
    # Bash's OTHER fd-redirect spelling opens with a brace, so neither the
    # digit lookahead nor the character class caught it.
    'AGENT_RUN_ORIGIN=scheduled {fd}>log',
    'AGENT_RUN_ORIGIN=scheduled {fd}>>log',
    'AGENT_RUN_ORIGIN=scheduled {lockfd}<lockfile',
    # A dict write to something that is not os.environ reaches no child. Both
    # reviewers found this; they disagreed on whether to close it, because
    # requiring os.environ costs a false negative on a custom dict later passed
    # as subprocess(env=...). Closed anyway: a false negative FLAGS a compliant
    # job, while accepting any object VOUCHES for a broken one, and only one of
    # those directions is safe.
    'meta["AGENT_RUN_ORIGIN"] = "scheduled"',
    'debug_info["AGENT_RUN_ORIGIN"] = "scheduled"',
]


@pytest.mark.parametrize("line", _SETS)
def test_a_real_assignment_is_counted_as_setting(line, tmp_path):
    src = tmp_path / "prog"
    src.write_text("#!/usr/bin/env python3.11\n" + line + "\n")
    assert _line_sets_origin(line), f"a genuine assignment was missed: {line!r}"


@pytest.mark.parametrize("line", _DOES_NOT_SET)
def test_a_read_or_conditional_set_is_NOT_counted_as_setting(line):
    """The round-2 finding, generalised past the two forms the reviewers named.

    Both findings were one family: every form passing the NAME and the VALUE as
    two ARGUMENTS rather than assigning one to the other. `setdefault` is the
    conditional set this PR removed from bin/issue-queue-drain because it loses
    to a poisoned inherited environment; `.get` is a pure read. With `,` in the
    separator alternation the sweep called both of them compliant, so a future
    program could reintroduce the incident with this very check vouching for it.

    A guard that certifies the bug it exists to prevent is worse than no guard,
    because it also stops anyone looking.
    """
    assert not _line_sets_origin(line), (
        f"a non-setting line was counted as setting the origin: {line!r}")


# ===========================================================================
# THE RUNTIME TEST. Everything above asks what a line LOOKS like; this asks
# what it DOES.
#
# WHY IT EXISTS. `_line_sets_origin` is a text matcher standing in for a
# runtime property — whether executing a line causes a CHILD PROCESS to inherit
# AGENT_RUN_ORIGIN. Eight review rounds on this file each found a real hole in
# that matcher, every one a different spelling of the same fact: `2>/dev/null`,
# `{fd}>log`, a trailing comment, a separator, a bare assignment, a conditional
# `${VAR=x}`, a `.get()` default, a dict that is not os.environ. Asked whether
# the family was closed, the reviewer said a ninth spelling should be expected
# under a design that reasons about text.
#
# So these tests hand the question to the interpreter. bash decides whether bash
# exported something; Python decides for Python. A spelling nobody thought of
# is adjudicated correctly the first time it is written down, because nothing
# here reasons about syntax.
#
# THE PROBE IS A REAL CHILD PROCESS, which is the whole point. `printenv` only
# ever sees the exported environment it was handed, so an unexported shell
# variable is invisible to it — exactly the distinction eight rounds of regex
# kept failing to draw.
# ===========================================================================

# (classifier input, bash to run). They differ only for the inline-prefix forms,
# where the line IS a command invocation, so the probe has to BE that command.
_RUNTIME_SHELL = [
    # (source line as the sweep sees it, executable form, expected_exported)
    ('export AGENT_RUN_ORIGIN=scheduled',
     'export AGENT_RUN_ORIGIN=scheduled', True),
    ('declare -x AGENT_RUN_ORIGIN=scheduled',
     'declare -x AGENT_RUN_ORIGIN=scheduled', True),
    ('AGENT_RUN_ORIGIN=scheduled exec "$@"',
     'AGENT_RUN_ORIGIN=scheduled printenv AGENT_RUN_ORIGIN; exit', True),
    ('AGENT_RUN_ORIGIN=scheduled', 'AGENT_RUN_ORIGIN=scheduled', False),
    ('declare AGENT_RUN_ORIGIN=scheduled', 'declare AGENT_RUN_ORIGIN=scheduled', False),
    ('readonly AGENT_RUN_ORIGIN=scheduled', 'readonly AGENT_RUN_ORIGIN=scheduled', False),
    ('AGENT_RUN_ORIGIN=scheduled  # for the spawn below',
     'AGENT_RUN_ORIGIN=scheduled  # for the spawn below', False),
    ('AGENT_RUN_ORIGIN=scheduled ; agentctl spawn foo',
     'AGENT_RUN_ORIGIN=scheduled ; true', False),
    ('AGENT_RUN_ORIGIN=scheduled >file',
     'AGENT_RUN_ORIGIN=scheduled >/dev/null', False),
    ('AGENT_RUN_ORIGIN=scheduled 2>/dev/null',
     'AGENT_RUN_ORIGIN=scheduled 2>/dev/null', False),
    ('AGENT_RUN_ORIGIN=scheduled 2>&1', 'AGENT_RUN_ORIGIN=scheduled 2>&1', False),
    ('AGENT_RUN_ORIGIN=scheduled {fd}>log',
     'AGENT_RUN_ORIGIN=scheduled {fd}>/dev/null', False),
    (': "${AGENT_RUN_ORIGIN=scheduled}"', ': "${AGENT_RUN_ORIGIN=scheduled}"', False),
    (': "${AGENT_RUN_ORIGIN:=scheduled}"', ': "${AGENT_RUN_ORIGIN:=scheduled}"', False),
    ('o="${AGENT_RUN_ORIGIN:-scheduled}"', 'o="${AGENT_RUN_ORIGIN:-scheduled}"', False),
]


def _bash_exports(snippet: str) -> bool:
    """Run `snippet` in bash, then ask a CHILD whether it inherited the var."""
    script = snippet + '\nprintenv AGENT_RUN_ORIGIN || true\n'
    cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                        timeout=20, env={"PATH": os.environ.get("PATH", "")})
    return "scheduled" in cp.stdout


@pytest.mark.parametrize("line,snippet,expected", _RUNTIME_SHELL,
                         ids=[c[0] for c in _RUNTIME_SHELL])
def test_bash_itself_agrees_with_the_classifier(line, snippet, expected):
    """The classifier's verdict must match what bash actually does.

    This is the test that closes the family rather than one more spelling of it.
    If a future edit widens or narrows `_ASSIGNS_ORIGIN` in a way that disagrees
    with the shell, this fails and names the line — no reviewer has to notice.
    """
    runtime = _bash_exports(snippet)
    assert runtime is expected, (
        f"the EXPECTATION is wrong, not the classifier: bash "
        f"{'exported' if runtime else 'did not export'} AGENT_RUN_ORIGIN for "
        f"{snippet!r}")
    assert _line_sets_origin(line) is runtime, (
        f"classifier says {_line_sets_origin(line)} but bash says {runtime} "
        f"for {line!r} — the text matcher and the shell disagree")


_RUNTIME_PYTHON = [
    ('os.environ["AGENT_RUN_ORIGIN"] = "scheduled"', True),
    ('os.environ.setdefault("AGENT_RUN_ORIGIN", "scheduled")', True),
    ('meta = {}; meta["AGENT_RUN_ORIGIN"] = "scheduled"', False),
    ('_ = os.environ.get("AGENT_RUN_ORIGIN", "scheduled")', False),
]


@pytest.mark.parametrize("stmt,expected", _RUNTIME_PYTHON,
                         ids=[c[0][:48] for c in _RUNTIME_PYTHON])
def test_python_itself_decides_whether_a_child_inherits(stmt, expected):
    """Same adjudication for the Python branch, by a real child process.

    `setdefault` is deliberately here and is the one case where the classifier
    is INTENTIONALLY stricter than the runtime. It really does export when the
    variable is unset — but it loses to a poisoned inherited value, which is the
    incident this PR exists to close, so the sweep must keep refusing it. The
    assertion below pins that divergence as a decision rather than letting it
    look like a bug.
    """
    script = (
        "import os, subprocess, sys\n"
        f"{stmt}\n"
        "subprocess.run([sys.executable, '-c',"
        " \"import os;print(os.environ.get('AGENT_RUN_ORIGIN','__UNSET__'))\"])\n"
    )
    cp = subprocess.run([sys.executable, "-c", script], capture_output=True,
                        text=True, timeout=30,
                        env={"PATH": os.environ.get("PATH", "")})
    inherited = "scheduled" in cp.stdout
    assert inherited is expected, (
        f"expectation wrong: child {'saw' if inherited else 'did not see'} the "
        f"variable after {stmt!r}")
    if "setdefault" in stmt:
        assert _line_sets_origin(stmt) is False, (
            "setdefault must stay REFUSED even though it exports: it loses to a "
            "poisoned inherited value, which is the incident this PR closes")
    else:
        assert _line_sets_origin(stmt) is inherited, (
            f"classifier and Python disagree for {stmt!r}")


# ---------------------------------------------------------------------------
# The same question asked of the REAL dispatchers, not of a curated case list.
# ---------------------------------------------------------------------------

def _prelude_through_origin(source: Path) -> str | None:
    """The program's source truncated after the first line that MENTIONS
    AGENT_RUN_ORIGIN in code.

    DELIBERATELY DOES NOT USE `_line_sets_origin`. The runtime test exists to
    adjudicate that classifier, so depending on it to find the cut point would
    make the check partly circular — and it would report the wrong thing. When a
    dispatcher exports an UNCAPPED origin, a classifier-driven cut finds no
    "setting" line and the failure reads "no origin-setting line found", sending
    the reader after a missing line instead of the wrong value. Cutting on the
    bare mention runs that line for real, so the probe reports `interactive` and
    the failure names what actually happened.
    """
    lines = source.read_text().splitlines()
    code = set(_code_lines(source))
    for i, line in enumerate(lines):
        if "AGENT_RUN_ORIGIN" in line and line in code:
            return "\n".join(lines[: i + 1])
    return None


def _dispatchers_with_sources() -> list[tuple[str, Path]]:
    out = []
    for pl in _plists():
        if pl.name in KNOWN_UNRESOLVABLE or pl.name in KNOWN_UNFIXED:
            continue
        src = _resolve_program(_argv(pl))
        if src is not None and _dispatches_agents(src):
            out.append((pl.name, src))
    return out


def test_there_are_dispatchers_to_run():
    """A discovery step that silently found none would make the test below
    vacuous — the failure mode this whole file is about."""
    assert _dispatchers_with_sources(), "no agent-dispatching plists resolved"


@pytest.mark.parametrize(
    "name,source", _dispatchers_with_sources(),
    ids=[n for n, _ in _dispatchers_with_sources()])
def test_a_real_dispatchers_origin_reaches_a_child_process(name, source, tmp_path):
    """Run the actual program's prelude and ask a CHILD what it inherited.

    This is the property every regex in this file approximates, asked directly
    of the real dispatcher. It needs no maintenance when someone invents a new
    way to spell an assignment, because the interpreter answers.

    Only the prelude runs — everything up to and including the origin
    assignment — so no dispatcher does any of its actual work here. All four
    shell launchers set the origin within their first handful of lines, before
    they read a queue or reach the network.
    """
    prelude = _prelude_through_origin(source)
    assert prelude is not None, (
        f"{name}: no origin-setting line found in {source.name}, so this test "
        f"cannot verify it — that is a failure, not a skip")

    is_python = re.match(r"#!.*python", source.read_text().partition("\n")[0])
    script = tmp_path / ("p.py" if is_python else "p.sh")
    if is_python:
        script.write_text(
            prelude
            + "\nimport subprocess as _s, sys as _y\n"
            "_s.run([_y.executable, '-c', \"import os;"
            "print(os.environ.get('AGENT_RUN_ORIGIN','__UNSET__'))\"])\n")
        cmd = [sys.executable, str(script)]
    else:
        script.write_text(prelude + "\nprintenv AGENT_RUN_ORIGIN || echo __UNSET__\n")
        cmd = ["bash", str(script)]

    # A MINIMAL PATH AND A THROWAWAY HOME, and this is containment rather than
    # tidiness. This test runs REAL production launcher code in a CHILD process,
    # so conftest's `_no_live_ops_writes` fixture — which patches urlopen inside
    # the pytest process — does not reach it. Meanwhile ~/.local/bin/agentctl is
    # a symlink to the PRIMARY checkout's live bin/agentctl, so a real PATH puts
    # the actual dispatcher one command away from a `pytest` run.
    #
    # All five preludes are inert today; the point is that nothing structural
    # stops a future edit adding a preflight `agentctl spawn`, a curl, or a read
    # of a credential under $HOME ABOVE the origin line. With /bin:/usr/bin and
    # HOME in tmp_path, such an edit fails the test instead of quietly dispatching
    # against the live ops plane with real credentials (security review, round 9).
    #
    # Nothing verified here needs more: a shell or interpreter, and printenv.
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                        cwd=str(tmp_path),
                        env={"PATH": "/bin:/usr/bin", "HOME": str(tmp_path)})
    got = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else "__NO_OUTPUT__"
    assert got in CAPPED_ORIGINS, (
        f"{name}: a child of {source.name} inherited AGENT_RUN_ORIGIN={got!r}, "
        f"which is not one of the capped origins {sorted(CAPPED_ORIGINS)}. Every "
        f"run it spawns would escape the budget gate.\n"
        f"stderr: {cp.stderr[-400:]}")
