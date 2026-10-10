"""Did a dispatched run write into a PRIMARY checkout it does not own?

WHAT THIS CATCHES. Worktree isolation is what stops a dispatched worker touching
the operator's checkouts. Nothing enforces it. A run is bound to its worktree only by the
process `cwd`, so any absolute path the agent builds — by analogy from a brief, a
memory rule, or a habit — resolves against whatever root it names, and the primary
is the root it is most likely to name. Diagnosed on
`issue_01KZ7E0YNV84W55796CZ8ETX6X`: **56 runs wrote into a product primary across
29 days, 15 agent types and 7 repos**, and a further 131 issued state-mutating git
there, 8 of them irrecoverable (`stash drop`, `reset --hard`, `checkout -- .`,
`branch -D`). One run leaked across repos entirely, a convivy-orient worktree
writing into `agent-platform/bin/`.

WHY A GUARD AND NOT A PROMPT FIX. 26 of the 56 leaks had no brief path to correct,
the pattern spans 15 agent types, and two repos with no absolute-path convention
at all leaked anyway. A prompt correction reaches the cases it names; the ambient
floor is 0.3% of all runs and no wording reaches it. **And the failure is silent**
— a stale duplicate sitting untracked in a primary is invisible until something
blocks on it, which is why the original went unnoticed through a whole build, two
review rounds and a merge.

WHY REFUSE RATHER THAN WARN. `agentctl publish` warned twice during the incident
that produced the diagnosis, and nobody connected the warnings to anything. A
warning that has already failed is not a fix. A run that mutated a primary is
marked `primary_mutated`, which is not a success status, so an orchestrator
reading the outcome cannot mistake it for a clean finish.

WHAT IT COMPARES. Two cheap facts per primary, taken at spawn and again at
finalize: the set of paths `git status --porcelain` reports, and `HEAD`. That
covers a run that ADDS files (the original leak), one that REMOVES pre-existing
dirt from the dirty set (`reset --hard`, `checkout -- .` and `git stash` all remove
entries). It also NOTICES a checkout moving, and reports that without acting on
it — see `Mutation.is_working_tree_mutation` for why a HEAD move alone must never
downgrade a run. There are 20 primaries as of 2026-08-04 and a run reads the 19 it
does not own, in about 0.5s, so the spawn-plus-finalize pair costs under a second
per run.

WHAT IT DOES NOT CATCH, stated so nobody reads a pass as more than it is:

  - A run that mutates and then perfectly restores, inside its own lifetime.
  - `git branch -D` of a branch that is not HEAD, which changes neither fact.
  - `git checkout <sha>` in a primary whose tree was CLEAN, and any other HEAD
    move that leaves the old commit still reachable from some ref. Reported, not
    downgraded: nothing was lost, and branch state has its own owner. A HEAD move
    that ORPHANS the old commit — `reset --hard` rolling back unpushed work — IS
    downgraded; see `_orphaned_head`.
  - A successful `git cherry-pick` into a clean primary (security-reviewer, PR
    #364). It advances HEAD and leaves the tree clean, and the old commit stays
    reachable, so nothing here fires. Nothing is destroyed, but a commit the
    primary did not ask for is now on its branch.
  - A write into a primary's `.git/` administrative area. `git status` does not
    report those, which is also why the by-design `.git/worktrees/<wt>/pr-body.md`
    write needs no exclusion here.
  - Anything in a repo that was unreadable at either end; see below.

**IT SEES THE FILESYSTEM, NOT THE PROCESS** (reviewer, PR #362), so a mutation
made by a CONCURRENT run, or by the operator running `git pull` in a primary, falls inside
every overlapping run's window and is reported by all of them. On a night running
20 workers that would turn one real leak into 20 identical reports, and a
fabricated finding is the one thing that would get this guard switched off.

Two things narrow it. `Mutation.attribution` checks whether each added path ALSO
exists at the same relative path inside the reporting run's worktree — which is
what a leak IS, the same write landing at the wrong root — so the author says
"THIS RUN almost certainly" and the bystanders say "possibly ANOTHER run or a
human". And the report names every other run that was live at finalize, so an
operator seeing several reports can tell one cause from several. Neither is
proof; both turn an undifferentiated pile into a ranked one.

**THE ECHO CHECK ITSELF NEEDED NARROWING, THREE TIMES.** First on a NAME basis
(`issue_01KZHB6601KBX76W999JC7RDW8`): a bare filename match treated
`requirements.txt` existing in both the mutated repo and the reporting run's
worktree the same as a distinctive path like `app/services/web_push/client.py`
— but the first sits in nearly every Python repo the fleet owns and carries
almost no authorship signal, while the second does. Measured, not theoretical:
a bug-fixer run that cost $0.0073 making two ops MCP calls, no file writes at
all, was named the author of `app.py` and `vectors.py` in an unrelated repo on
a `requirements.txt` echo alone. The fix at the time was a denylist of
filenames common enough that a NAME match alone could not prove anything.

That denylist does not reach a document repo (`issue_01M0VJRRKF56N4E985RD4E77QZ`),
where the ubiquitous set is not a handful of filenames but every tracked
document: in `knowledge`, every worktree is a full checkout of the same tree,
so any path committed on `main` exists at the same relative location in every
worktree by construction, whoever is editing it. Three runs in one session —
`run-819d4ddd`, `run-d60297fb`, `run-65db5005` — were each named "THIS RUN
almost certainly" for documents another session's PR branch was concurrently
editing; `head_before` equalled `head_after` on all three, so none of them had
committed anything, and the flagged paths were tracked on `origin/main` and
merely modified, not new.

The generalization that reaches both: a path already tracked in the primary
at the run's BASELINE (before the mutation) is exactly the shape a document
repo's every-worktree-has-it echo takes, whether or not its name is on any
list — so for a path in that shape, presence in the worktree proves nothing
and CONTENT identical to the primary's post-mutation copy is required before
it counts (`_tracked_at_baseline`, `_echo_content_matches`). A path that did
NOT exist in the primary before the run — the shape a genuine leak takes, a
new file landing at the wrong root — still attributes on presence alone, same
as before. This retires the name denylist entirely: it was one hardcoded case
of "presence proves nothing here," and the general rule now covers it without
a list to keep current — for the exact original incident, a MODIFICATION to an
already-committed `requirements.txt`. It does not reach every case the old
denylist did: a FRESHLY CREATED copy of a ubiquitous filename, never tracked
in that primary before, is untracked-at-baseline and still attributes on
presence alone under the new rule, where the name denylist would have
required a content match regardless of tracked history. Accepted, since this
module's philosophy is that a new path at that relative location is exactly
what a leak IS.

The third narrowing is RENDER OUTPUT (`RENDER_OUTPUT_PREFIXES`), and it is a
case the tracked-at-baseline rule cannot reach. The individuator writes a
crew's `.claude/agents` and `.claude/skills` into its primary, and the SAME
relative paths into every worktree it individuates, so those paths are
present in every live run's worktree by construction — the guaranteed-not-
evidential shape again, but reached by individuation rather than by git, so
the files are untracked in the primary and `_tracked_at_baseline` reads them
as the genuine-leak shape. On 2026-09-16 three runs (run-b3cc8ff7,
run-697c5d52, run-8901fd71) were each failed `primary_mutated` for
`signals/.claude/skills/{handoff-leave,handoff-pickup,rollover}/SKILL.md`,
which the steward had copied in by hand. So a render path never ATTRIBUTES.

**IT IS STILL A MUTATION, AND THAT DISTINCTION IS THE WHOLE FIX** (reviewer and
security-reviewer, PR #608). The first attempt dropped these paths from the
dirty set at both ends, which removed the false failures and also made
`.claude/agents/` the one place in any primary a run could write unwatched —
`tool_policy.py` reads `.claude/agents/<name>.md` and enforces its `tools:`,
so a poisoned definition written into another crew's primary would be loaded
by that crew's next session with no signal anywhere that it had been written.
It also added the third path `platform/decisions/0025` says must not exist:
every working-tree mutation either fails its run or logs a signal. Excluding
these paths from ATTRIBUTION alone keeps both — nothing is dropped, the three
false failures become one `needs_an_owner` issue per repo per day, and a
payload that differs from what the individuator renders is reported exactly
like any other unattributed write.

**AND THE RANKING NOW DECIDES, WHERE IT ONLY ADVISED** (`issue_01KZ94PCSF9HFQ9487FR4A3V5F`).
#362 chose to rank rather than filter, on the reasoning that a guard swallowing a
real leak to look tidy is worse than one that over-reports. The reasoning was
right and the cost model was wrong: across 2026-08-04/05 six runs were marked
`primary_mutated` and five were innocent **by this module's own attribution**,
each report saying so in writing before its run was failed. So a mutation now
downgrades only the run the evidence points at (`Mutation.downgrades_run`), and
an unattributed one LOGS A SIGNAL instead (`Mutation.needs_an_owner`), which is
the half that keeps a real leak loud. The two must ship together: relaxing the
downgrade alone would make an unattributed leak genuinely quiet, which is the
failure this guard exists to prevent.

**THIS GUARD IS NOT ADVERSARY-PROOF, AND THAT IS BY DESIGN** (security-reviewer,
PR #362). It watches honest mistakes, and every dispatched agent runs with
`bypassPermissions`, so an agent that WANTS to hide can:

  - delete or rewrite its own `started_primaries.json` between spawn and finalize,
    after which there is no baseline and therefore no finding;
  - make a primary unreadable at finalize only, since a repo present on one side
    of the diff is skipped, then restore it afterwards;
  - write the leaked file into BOTH the primary and its own worktree, then delete
    the worktree copy before finalize. `echoed_in_worktree` is read once at diff
    time, so the echo that would have named it is gone and `downgrades_run` is
    False (security-reviewer, PR #367). NEWLY REACHABLE with that PR: while the
    downgrade hung on `is_working_tree_mutation`, worktree state could not affect
    the status. It is also the WEAKEST of the three — the other two suppress the
    guard outright, leaving no baseline and therefore no finding, while this one
    only converts a downgrade into `needs_an_owner`, so a signal is still logged
    and a human still reads it.

All three are named here because the alternative is worse than the gap: an
auditor reading a `primary_mutated`-free history would otherwise over-read it as
proof that no run ever touched a primary. It is evidence about mistakes, not
about intent. Closing these needs a control the agent cannot reach — a baseline
written outside the run dir, or a `PreToolUse` deny — which is a different change
with a different cost, not a tweak to this one.

FAIL-SAFE, DELIBERATELY. A repo that cannot be read at snapshot time is OMITTED
from the baseline rather than recorded as clean, and a repo missing from either
side is skipped at diff time. A false `primary_mutated` would train its reader to
ignore the status, which costs more than the miss. The same reasoning the ARCH-397
and `committed_not_pushed` downgrades in `_finalize_run` already use.

SCOPE. This is about a run mutating a checkout it does not own. Whether a primary
sits on the right BRANCH is a different question with a different owner
(`bin/check-primary-checkouts`, ARCH-437), and folding the two would make one
signal mean two things.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import builder_root as BR

# The machine's one root (company/decisions/0008). The name predates the
# resolver and is kept because tests patch it.
CONVIVY_ROOT = BR.root()

# Written by `git worktree add` into the OWNING primary's tree. `git status` does
# not report anything under `.git/`, so these never surface — kept as the record
# of what was checked, since a future switch to a dirtier snapshot would need it.
BY_DESIGN_GIT_WRITES = (".git/worktrees/", ".git/config", ".git/pr-body.md")

_BASELINE_FILENAME = "started_primaries.json"

# RENDER OUTPUT: paths whose presence in a run's worktree is guaranteed by the
# individuator rather than earned by authorship. `knowledge-ctx use` writes a
# crew's `.claude/agents` and `.claude/skills` into its primary AND the same
# relative paths into every worktree it individuates, so a render in one crew's
# primary echoes in every live run's worktree and would name each of them the
# author — which is what failed run-b3cc8ff7, run-697c5d52 and run-8901fd71 on
# 2026-09-16 over `signals/.claude/skills/*/SKILL.md`.
#
# THIS SUPPRESSES ATTRIBUTION, NOT DETECTION, and the difference is load-bearing
# (PR #608's round 1 got it wrong in the other direction). Dropping these paths
# from the dirty set would leave `.claude/agents/` — the file `tool_policy.py`
# reads and enforces `tools:` from — as the one place a run could write into
# another crew's primary with nothing watching, and would add the third path
# `platform/decisions/0025` forbids. A render path still becomes a `Mutation`,
# still reaches `needs_an_owner`, and still logs a signal; it just never says
# "THIS RUN". See `diff()`, which is the only place this is applied.
RENDER_OUTPUT_PREFIXES = (".claude/agents/", ".claude/skills/")


def _is_render_output(path: str) -> bool:
    return path.startswith(RENDER_OUTPUT_PREFIXES)


# A PATH OUT OF THIS MODULE IS UNTRUSTED INPUT, and it became so HERE: reading
# `git status` with `-z` turns git's `core.quotePath` C-quoting off so the echo
# check can find the file on disk, and the same change hands every consumer raw
# attacker-chosen bytes. A Unix filename may hold a newline, a backtick or an
# ESC. The paths this module reports come from primaries a run does not own,
# which is precisely where a hostile write lands, so every surface that RENDERS
# one has to escape it (security-reviewer, PR #608 rounds 1 and 2 — the markdown
# bodies in `bin/agentctl` first, then `describe()`'s stderr line).
#
# "Plain text" is not a safe destination: a terminal interprets what it is
# handed, so `evil\x1b[2K\x1b[1Asafe.py` can erase the very line reporting the
# leak. Nor is a browser, which applies the Unicode Bidi Algorithm by default:
# U+202E in a filename makes it DISPLAY as an unrelated, innocuous name to the
# operator deciding how to respond, the Trojan Source class (CVE-2021-42574).
# Escaped rather than stripped, because the path is the evidence an operator
# acts on and a silently shortened one sends them looking for a file that does
# not exist.
#
# THE RULE IS A CATEGORY TEST RATHER THAN A LIST, because rounds 1-3 of PR #608
# each extended an enumerated escape set — newline and backtick for the markdown
# bodies, then ESC and the C0 range for `describe()`'s stderr line, then the bidi
# controls — and a list that grows once per review round is a list that is wrong
# between rounds. `Cc` covers the control ranges (C0, DEL, and C1, which the
# earlier `ord(ch) < 0x20 or ord(ch) == 0x7F` test did not reach); `Cf` covers
# the bidi overrides U+202A-U+202E, the U+2066-U+2069 isolates, the zero-width
# joiners and the U+E0020-U+E007F tag block; `Zl`/`Zp` the line and paragraph
# separators; `Cs` lone surrogates, which strict decoding should make
# unreachable and which cost nothing to name.
#
# WHAT IT CLOSES IS STRUCTURE AND VISIBLE IDENTITY, NOT INVISIBILITY, and an
# earlier draft of this comment claimed otherwise — that escaping these
# categories "admits no further members". THAT WAS FALSE, and both reviewers
# caught it (PR #608 round 4). `General_Category` and Unicode's
# `Default_Ignorable_Code_Point` are ORTHOGONAL properties, so no set of
# categories captures "renders invisibly". Known to pass through here:
#
#   - variation selectors U+FE00-U+FE0F and U+E0100-U+E01EF, category `Mn`
#   - Hangul fillers U+115F, U+1160, U+3164, U+FFA0, category `Lo`
#   - reserved-but-Default_Ignorable ranges such as U+2065 and U+FFF0-U+FFF8,
#     category `Cn`
#
# so a filename can still carry an invisible payload into a rendered body. It
# cannot forge structure or disguise itself as a DIFFERENT visible path, which
# is what the escaping above exists for, and the exact bytes remain in
# `primary_mutations.json`. **`signal_client.is_default_ignorable` is this
# repo's property-correct oracle** — a transcribed range table, because
# `unicodedata` does not expose the property — and `signal_client` learned this
# same lesson the same way, by having a category-plus-heuristic test broken by
# these same counterexamples. It is not imported here because it pulls
# `vault_plane` and `urllib` in with it, and this guard runs at every spawn and
# finalize and reads nothing it does not have to. Sharing that table is
# `issue_01M2P2RWS2S3R7WSEB2JT50H4X`.
#
# NOR IS `unicodedata.category` VERSION-INDEPENDENT: this machine carries four
# Python builds reporting four Unicode versions, and `signal_client` pins and
# warns about exactly that drift for its table. Nothing here does, which is part
# of the same follow-up. The categories used above are stable ones — a
# reclassification would be `Cn` gaining an assignment, which only ever makes
# this escape less, never more.
#
# What deliberately passes through is everything a human can see, including the
# non-ASCII names `-z` was adopted to record exactly (`naïve.py` is `Ll`, a
# space is `Zs`). Homoglyphs and combining marks stay: they are printable
# content and the general Unicode-spoofing surface, strictly broader than
# rendering a path faithfully, and no escaping of a filename closes them
# (security-reviewer, PR #608 rounds 3 and 4, which agreed both times).
#
# `extra` takes characters a destination needs neutralised beyond that —
# `bin/agentctl` passes the backtick, printable content that markdown cannot
# escape inside a code span.
_UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _escaped(ch: str) -> str:
    """Python's own escape spelling, so the output is unambiguous at any width."""
    cp = ord(ch)
    if cp <= 0xFF:
        return f"\\x{cp:02x}"
    if cp <= 0xFFFF:
        return f"\\u{cp:04x}"
    return f"\\U{cp:08x}"


def safe_path(path: str, *, extra: str = "") -> str:
    """One reported path, safe to write to a terminal or a markdown body."""
    out = []
    for ch in path:
        if ch == "\\":
            out.append("\\\\")          # first, so the escaping stays reversible
        elif ch in extra:
            out.append(_escaped(ch))
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif unicodedata.category(ch) in _UNPRINTABLE_CATEGORIES:
            out.append(_escaped(ch))
        else:
            out.append(ch)
    return "".join(out)

# READING A REPO MUST NOT RUN CODE FROM IT, and this guard is the one place where
# that is not paranoia (security-reviewer, PR #362). `git status` executes
# `core.fsmonitor` from the repo's own `.git/config` — and writes into `.git/` are
# exactly what this guard cannot see, so an agent that sets `core.fsmonitor` in a
# primary during run N would have it executed by run N+1's scan. The guard would
# be the persistence vector for the blind spot it declares. `-c` overrides the
# repo-level config; `GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_GLOBAL=/dev/null` stop
# the system and user files contributing their own.
#
# `--no-optional-locks` is separate and just as load-bearing: `git status`
# normally REFRESHES THE INDEX, so an auditor without it writes to every checkout
# it audits. A read-only guard has to actually be read-only.
#
# The `alias.status` / `alias.rev-parse` overrides are DEFENCE IN DEPTH, not a
# fix: a round-2 review reported a hostile alias in a repo's local config as a
# second execution vector, and it does not reproduce. Git ignores an alias that
# shadows a built-in command, verified on git 2.50.1 — the alias never fires,
# `status` and `rev-parse` run normally. Kept because they cost nothing and pin
# the behaviour: if a future git ever honours such an alias, these overrides
# already blunt it and `test_a_hostile_alias_cannot_hijack_the_read` says so.
_SAFE_GIT = ("git", "--no-optional-locks",
             "-c", "core.fsmonitor=false",
             "-c", "alias.status=",
             "-c", "alias.rev-parse=")
_SAFE_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
}

# A path existing at the same relative location in a mutated primary and in the
# reporting run's own worktree is `Mutation.attribution`'s authorship signal —
# but only when presence there is informative. It is not, for a path that was
# ALREADY in the primary's committed tree before the run started: every
# worktree of the same repo family checks out that path too, so its presence
# is guaranteed rather than evidential (`issue_01M0VJRRKF56N4E985RD4E77QZ`,
# and before it the narrower `requirements.txt`/`.github/workflows/*` case,
# `issue_01KZHB6601KBX76W999JC7RDW8`). A path that did NOT exist in the
# primary at baseline carries no such guarantee — a genuinely new file at that
# relative path is exactly what a leak IS — so it still attributes on
# presence alone.
def _tracked_at_baseline(primary: Path, head_before: str | None, path: str) -> bool:
    """Did this path already exist in the primary's committed tree BEFORE the
    run started, rather than being created fresh during its window?

    A path clean in `git status` at baseline is either tracked-and-unmodified
    or absent entirely — the dirty-set snapshot alone cannot tell those apart,
    and only the first is the "every worktree already has this" shape. This
    reads it back from the primary's own history at `head_before`, which is
    exactly the commit the baseline snapshot was taken against.

    Fails closed AGAINST attribution, matching `_echo_content_matches`: no
    baseline HEAD, or a read that errors for any reason, is treated as
    "tracked", which routes to the content-match check rather than the
    presence-only one. The alternative — treating a read failure as "not
    tracked" — would resolve every unreadable case toward the more sensitive
    old behaviour, reopening exactly the crying-wolf failure this predicate
    exists to close. Losing a genuine attribution to a read failure is not
    silent either way: `needs_an_owner` still files it. A missing `head_before`
    reaches the same "tracked" return by a different precondition — the
    baseline snapshot never resolved `HEAD` at all — so a brand-new leaked file
    can be excused from presence-only attribution this way too, not only via an
    already-tracked path; `needs_an_owner` still files it either way.

    THE READ ITSELF IS TWO GIT CALLS, NOT ONE, because `git cat-file -e
    <rev>:<path>` exits 128 for two unrelated reasons it reports identically:
    the path genuinely absent at that revision, and the revision itself not
    resolving (a bad SHA, or `-C` pointed at the wrong or a broken repo).
    `subprocess.run(check=False)` never raises for either, so a bare
    `cp.returncode == 0` cannot tell "absent" from "errored" and silently
    picks the wrong one for the second case — verified empirically against a
    scratch repo: a revision that doesn't resolve produces the exact same
    "path does not exist" message and exit code as a revision that does
    resolve but never had that path. `git rev-parse --verify <rev>^{commit}`
    first isolates the "does this revision even resolve here" question, which
    it does not conflate with path lookup; only once that succeeds does a
    `cat-file -e` 128 unambiguously mean "absent", because the revision and
    the root are now known-good. The realistic trigger is `head_before`
    becoming unreachable and pruned between the baseline snapshot and the
    diff — a `reset --hard` racing a `git gc --auto` — the same class of
    "commit no longer resolves" `_orphaned_head` exists to detect from the
    other direction.
    """
    if not head_before:
        return True
    try:
        verify = subprocess.run([*_SAFE_GIT, "-C", str(primary), "rev-parse", "--verify",
                                  f"{head_before}^{{commit}}"],
                                 capture_output=True, text=True, timeout=60,
                                 check=False, env=_SAFE_GIT_ENV)
        if verify.returncode != 0:
            # Can't tell "revision doesn't resolve" apart from any other git
            # error here either — and per the docstring above, that is exactly
            # the case this predicate must fail closed toward "tracked" for.
            return True
        cp = subprocess.run([*_SAFE_GIT, "-C", str(primary), "cat-file", "-e",
                             f"{head_before}:{path}"],
                            capture_output=True, text=True, timeout=60,
                            check=False, env=_SAFE_GIT_ENV)
    except (OSError, subprocess.SubprocessError):
        return True
    return cp.returncode == 0


def _echo_content_matches(worktree_path: Path, primary_path: Path) -> bool:
    """For a path tracked in the primary before the run, do the two files
    holding it now hold identical bytes?

    Presence alone proves nothing for a path every worktree already has —
    two unrelated repos both have a `requirements.txt`, and every worktree of
    the same document repo already has any given committed doc. Byte-identical
    content at that path is real authorship evidence regardless: an unrelated
    or merely-concurrent file doesn't coincidentally match byte-for-byte, while
    a genuine echo (leaked or authored) does.

    Fails closed, matching the rest of this module's read posture: any read
    error counts as no match rather than a guess.

    WHAT THIS GIVES UP, stated because it is a real trade rather than a corner
    case. A leak that overwrites a path already tracked at baseline, written
    straight into the primary without the run ever touching its own worktree
    copy at that same path, leaves the worktree copy holding whatever it held
    before — stale, un-leaked bytes. That fails the content check, so the
    mutation reads as "possibly ANOTHER run" rather than naming the run that
    actually wrote it. This may be the COMMON shape a leak into an
    already-tracked path takes, not the rare one: a run building an absolute
    path by analogy from a brief has no reason to also edit its own worktree's
    copy of that file. `needs_an_owner` still logs a signal for the
    unattributed mutation either way, so the leak is never silent — only its
    author goes unnamed. The alternative, attributing on presence alone for a
    tracked-at-baseline path, is exactly the crying-wolf failure this whole
    fix exists to close (`issue_01M0VJRRKF56N4E985RD4E77QZ`); this module
    chooses the quieter false negative over the loud false positive for that
    class of path.
    """
    try:
        return worktree_path.read_bytes() == primary_path.read_bytes()
    except OSError:
        return False


@dataclass(frozen=True)
class Mutation:
    """One primary the run changed, and how."""
    repo: str
    added: tuple[str, ...]      # paths dirty at the end that were clean at the start
    removed: tuple[str, ...]    # paths dirty at the start that are gone at the end
    head_before: str | None
    head_after: str | None
    # Added paths that ALSO exist in this run's own worktree at the same relative
    # path. See `attribution` — this is what separates the run that caused a
    # mutation from the ones that merely overlapped it in time.
    echoed_in_worktree: tuple[str, ...] = ()

    @property
    def head_moved(self) -> bool:
        return (self.head_before is not None
                and self.head_after is not None
                and self.head_before != self.head_after)

    # True when the commit the primary was on at spawn is no longer reachable from
    # ANY ref. See `_orphaned_head` — this is the difference between a human
    # switching branches and a run discarding work.
    orphaned_head: bool = False

    @property
    def is_working_tree_mutation(self) -> bool:
        """Did files actually change, as opposed to the checkout having moved?

        THIS is what may downgrade a run's status. A HEAD move alone must not,
        and the first live firing of this guard proved why: a reviewer run
        against `knowledge` was killed and its finished review discarded because
        a HUMAN — the co session that had dispatched it — committed on a branch
        in that primary while the run was in flight. The run touched nothing.

        A branch switch does not disturb the dirty set: tracked content is
        committed, so it never appears in `git status`, and untracked files carry
        across. So `added` and `removed` stay clean under ordinary human work, with
        one exception: a human who commits or deletes a file that was dirty at
        baseline removes it from the dirty set, so it shows up as `removed` (see
        `fingerprint`). The real cases still show: a leaked file appears in `added`,
        and `reset --hard` or `git stash` removes pre-existing dirt, which shows up
        as `removed`.

        A HEAD move with no file change is left to report only, UNLESS it left the
        old commit unreachable — see `orphaned_head`. That is the one HEAD move
        that destroys something, and it is `reset --hard` rolling back work the
        human had not pushed, one of the eight irrecoverable operations the
        original diagnosis counted.

        Everything else about a HEAD move belongs to `bin/check-primary-checkouts`
        (ARCH-437), and this module's own SCOPE note says folding the two would
        make one signal mean two things.
        """
        return bool(self.added or self.removed or self.orphaned_head)

    @property
    def attribution(self) -> str:
        """How confident we are that THIS run caused this mutation.

        The guard sees the filesystem, not the process that changed it, so on a
        night running 20 workers a single real leak would otherwise flag all 20.
        A leaked file almost always exists at the same relative path inside the
        author's worktree — that is what a leak IS, the same write landing at the
        wrong root — so its presence there is a strong authorship signal and its
        absence is a strong hint at someone else.

        EXCEPT for a path that was already in the primary's committed tree
        before the run started — every worktree of that repo family checks
        that path out too, so presence there is guaranteed rather than
        evidential, whether the repo is a code tree with a few ubiquitous
        filenames (`requirements.txt`, `.github/workflows/ci.yml` —
        `issue_01KZHB6601KBX76W999JC7RDW8`) or a document repo where nearly
        every tracked path has this shape (`issue_01M0VJRRKF56N4E985RD4E77QZ`).
        For a path in that shape, presence alone is excluded before
        `echoed_in_worktree` is ever populated — see `_tracked_at_baseline` —
        so it alone cannot produce a "THIS RUN" verdict. Byte-identical
        CONTENT at that path still can; see `_echo_content_matches`.

        AND EXCEPT for render output, where not even content can, because the
        individuator wrote both copies from one source — see
        `RENDER_OUTPUT_PREFIXES`. Such a path drops out of
        `echoed_in_worktree` entirely and the mutation reads as "possibly
        ANOTHER run or a human", which logs a signal rather than failing a
        bystander.
        """
        if self.echoed_in_worktree:
            return "THIS RUN almost certainly: the same paths exist in its worktree"
        if self.added or self.removed:
            return ("possibly ANOTHER run or a human: none of these paths exist in "
                    "this run's worktree")
        return "cause unclear: HEAD moved with no file change"

    @property
    def attributed_to_this_run(self) -> bool:
        """Does the evidence name the REPORTING run, rather than the window it sat in?

        `attribution` ranks in prose; this is the same judgement as a boolean, and
        it is what the downgrade now hangs on. See `downgrades_run`.
        """
        return bool(self.echoed_in_worktree)

    @property
    def downgrades_run(self) -> bool:
        """May this mutation mark the reporting run `primary_mutated`?

        Only when the run is BOTH the kind of change worth acting on and the run
        the evidence points at. #362 chose to rank rather than filter, reasoning
        that a guard swallowing a real leak to look tidy is worse than one that
        over-reports. The reasoning was right and the cost model was wrong
        (`issue_01KZ94PCSF9HFQ9487FR4A3V5F`): six runs were marked
        `primary_mutated` across 2026-08-04/05 and **five were innocent by the
        guard's own attribution**, which each of their reports said in writing
        before failing them.

        Failing a bystander buys nothing. The mutation is already durable in
        `run_dir/primary_mutations.json` and on stderr, and `primary_mutated` is
        in `WAIT_FAILURE_STATUSES`, so the only effect is to destroy the standing
        of a run that did nothing wrong — a reviewer's finished review discarded,
        a coder's committed work left unpublished.

        WHAT THIS GIVES UP, because it is not nothing. `echoed_in_worktree` is
        computed from `added` alone, so the destructive classes — `removed` (the
        dirty set losing entries to `reset --hard`, `git stash`, `checkout -- .`)
        and `orphaned_head` — carry NO attribution signal and therefore never
        downgrade any more. That is deliberate: a removed file cannot echo in
        the author's worktree, so before this change those classes downgraded
        every run alive at the time and named none of them, which is what the
        observed false positive actually was. The signal is not lost; it is
        re-routed. An unattributed working-tree mutation now logs a signal (see
        `needs_an_owner`), which reaches a human, where failing an arbitrary
        bystander reached nobody.
        """
        return self.is_working_tree_mutation and self.attributed_to_this_run

    @property
    def needs_an_owner(self) -> bool:
        """A real change to a checkout, with nobody the evidence can pin it on.

        The other half of the same fix, and the half that must not be dropped:
        relaxing `downgrades_run` on its own would make an unattributed leak
        genuinely quiet, which is the failure this guard exists to prevent. The
        #362 security review named a stderr line in a run log nobody greps as the
        weakness of report-only; a logged signal is what answers it.
        """
        return self.is_working_tree_mutation and not self.attributed_to_this_run

    def fingerprint(self) -> str:
        """A stable id for the CHANGE, so concurrent runs logging it agree.

        The whole point of the issue is that several runs share one window and see
        one mutation, so they must produce ONE `dedup_key`, not one each. This
        digest is the repo alone, not the paths, the run, the worktree, or the
        HEADs, none of which the reporting runs agree about. The call site
        (`bin/agentctl`, `_file_unattributed_mutations`) appends the UTC day and
        the removed-set hash (see below), so one destruction of one checkout on one
        day still becomes one signal, and a recurrence tomorrow logs again.

        WAS keyed on the repo plus the sorted path sets too, on the theory that
        runs sharing a window would see the same set. They don't: two runs
        observing one mutation almost always have DIFFERENT windows, since they
        spawn and finalize at different times, so a longer window catches
        unrelated churn a shorter one misses and the two path sets diverge. That
        was measured, not assumed: 80 issues filed against roughly 5-8 real
        events, 77 distinct fingerprints among them — the dedup this docstring
        used to estimate at "two issues rather than six" was off by an order of
        magnitude. Dropping the paths from the key fixed that.

        IT IS NOT ALONE ANY MORE, AND THE ADDED SET IS NOT IN IT EITHER. Two
        distinct destructions to one repo on one day reached one key, and the
        second was dropped: its paths survived only in its own run's
        `primary_mutations.json`, and a destructive second change is the one that
        matters. The call site appends a hash of what the mutation removed from the
        dirty set (`_destruction_hash`): the REMOVED path set, and the orphaned
        commit's id when the HEAD orphaned one. So each distinct destruction logs once.

        The removed set is baseline dirt that is no longer dirty. That covers
        `reset --hard` and `checkout -- .`, which discard it, and `git stash`, which
        keeps it in the stash. It also covers a baseline dirty file that a human
        then committed (nothing lost, the content is in a commit) or deleted (the
        content is gone unless it was first committed, or stashed in a stash that
        still exists). The key does not tell those apart, so a human committing
        baseline dirt logs a signal too. The added set stays out of the key, because
        it is the set that diverges between windows (above). Every additive-only
        change has the empty removed set and no orphaned commit, so a second
        additive-only change on a day that already logged one answers `duplicate`,
        and its paths stay in its own run's file. A repeat of the same destruction on
        one day also answers `duplicate`.

        The observed path list reaches a human in the signal BODY
        (`_unattributed_signal_body`).
        """
        return hashlib.sha256(self.repo.encode("utf-8")).hexdigest()[:12]

    def describe(self) -> str:
        """The one-line stderr summary `bin/agentctl` prints at finalize.

        Paths go through `safe_path` because this line is written to a terminal
        and read from a captured log, and since the `-z` read they arrive
        verbatim from a checkout the run does not own.
        """
        bits = []
        if self.added:
            bits.append(f"{len(self.added)} file(s) appeared: "
                        + ", ".join(safe_path(p) for p in self.added[:5])
                        + (" …" if len(self.added) > 5 else ""))
        if self.removed:
            bits.append(f"{len(self.removed)} pre-existing change(s) REMOVED from the dirty set: "
                        + ", ".join(safe_path(p) for p in self.removed[:5])
                        + (" …" if len(self.removed) > 5 else ""))
        if self.head_moved:
            bits.append(f"HEAD moved {self.head_before[:9]} -> {self.head_after[:9]}")
        return f"{self.repo}: " + "; ".join(bits) + f"  [{self.attribution}]"


def discover_primaries(root: Path | None = None) -> list[Path]:
    """Depth-1 checkouts under <root>. Only a primary has a real `.git`
    DIRECTORY; a linked worktree has a `.git` FILE, which is what separates them.

    DELIBERATELY NOT `worktrees.discover_primaries()`, which looks like the same
    function and answers a different question. That one lists valid SPAWN TARGETS,
    so it carries `DISCOVERY_EXCLUDES = {"agent-platform"}` — correct there, and a
    blind spot here on the highest-stakes checkout on the box. `~/.local/bin` and
    `~/.local/lib` are symlinks into agent-platform's working tree (ARCH-229), so a
    write there is read by cron and by the 01:00 overnight, and a run already
    leaked into it cross-repo (a convivy-orient worktree writing
    `agent-platform/bin/mcp-ops-server`). "Where may a run be dispatched" and "what
    must a run not touch" are near-inverses; sharing one list would import the
    exclusion that matters most.
    """
    base = root or CONVIVY_ROOT
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / ".git").is_dir():
            out.append(child)
    return out


def _read_repo(path: Path) -> dict | None:
    """`git status --porcelain` paths plus HEAD, or None if either read fails.

    Returning None rather than an empty snapshot is the whole fail-safe: an empty
    set would later read as "this repo was clean at spawn", turning every
    pre-existing untracked file into a fabricated leak.
    """
    try:
        # `-uall` because the default collapses a whole new untracked DIRECTORY to
        # one `app/` entry, so a run that leaks a package reports as a single vague
        # line instead of naming the files an operator has to inspect. Measured
        # over one run's 19 readable primaries it costs nothing (0.22s vs 0.38s,
        # inside noise) and returns 256 entries where the default returns 178.
        # `-z`: paths come back verbatim, never C-quoted. Without it a name git
        # quotes (a space, a non-ASCII byte, a double quote) was recorded with its
        # escapes, which is not a path, so the worktree echo check could never
        # find it and such a leak went unattributed.
        st = subprocess.run([*_SAFE_GIT, "-C", str(path),
                             "status", "--porcelain=v1", "-z", "-uall"],
                            capture_output=True, text=True, timeout=60,
                            check=False, env=_SAFE_GIT_ENV)
        if st.returncode != 0:
            return None
        head = subprocess.run([*_SAFE_GIT, "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=60,
                              check=False, env=_SAFE_GIT_ENV)
    except (OSError, subprocess.SubprocessError):
        return None

    return {
        "dirty": sorted(_parse_status_z(st.stdout)),
        "head": head.stdout.strip() if head.returncode == 0 else None,
    }


def _parse_status_z(out: str) -> set[str]:
    """Paths from `git status --porcelain=v1 -z`: NUL-separated `XY path` entries,
    where a rename or copy is followed by one more entry holding its source path.
    Both ends of a rename are real paths and both are recorded.

    THE WORKTREE COLUMN (`entry[1]`) IS CHECKED FOR `R`/`C` TOO, and on git
    2.50.1 that branch never fires: without `--find-renames`, which this call
    does not pass, git reports a rename only in the index column, and a
    worktree-only rename comes back as separate `D` and `??` entries (verified
    empirically, reviewer, PR #608). It is kept because the porcelain v1 format
    documents `XY` as two independent status codes and a reader consuming the
    second entry only when the FIRST column says so would, if git ever did
    report `R` there, silently treat that source path as the next entry's
    status codes and mis-record every remaining path in the read. The cost of
    the branch is one comparison; the cost of being wrong is the rest of the
    parse."""
    dirty: set[str] = set()
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        dirty.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(entries) and entries[i]:
                dirty.add(entries[i])
            i += 1
    return dirty


def _orphaned_head(repo: Path, head_before: str | None, head_after: str | None) -> bool:
    """Did this HEAD move leave the OLD commit unreachable from every ref?

    THE DISCRIMINATOR, and getting it right is what separates a destroyed commit
    from an ordinary branch switch. A round-2 review proposed ancestry instead —
    treat `head_after` being an ancestor of `head_before` as a backward, therefore
    destructive, move. That is wrong, and wrong in the direction that caused the
    incident this whole PR exists to fix: a human on a feature branch running
    `git checkout main` produces exactly that ancestry, loses nothing, and would
    be flagged. Verified against a scratch repo before writing this.

    Reachability answers the question the ancestry test was reaching for. After
    `git checkout main` the old HEAD is still on the feature branch, so nothing is
    lost. After `reset --hard HEAD~1` the branch ref has moved off it and no ref
    contains it, so the commit is orphaned and its content is gone from every
    branch — recoverable only from the reflog, by an operator who knows to look.

    FAIL-SAFE, and this is not decoration. This function can only ever ADD a
    downgrade, so a bug here re-creates the work-destroying false positive it is
    meant to sit beside. Anything unreadable, unparseable or ambiguous returns
    False, which leaves the run exactly as the rest of this module would have.
    """
    if not head_before or not head_after or head_before == head_after:
        return False
    try:
        cp = subprocess.run([*_SAFE_GIT, "-C", str(repo), "branch", "--all",
                             "--contains", head_before],
                            capture_output=True, text=True, timeout=60,
                            check=False, env=_SAFE_GIT_ENV)
    except (OSError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0:
        # git errors when the commit is already unreachable AND gc'd, but it also
        # errors for reasons that say nothing. Do not guess.
        return False
    return not cp.stdout.strip()


def snapshot(*, owned: Path | None = None, root: Path | None = None) -> dict[str, dict]:
    """Read every primary except the one this run legitimately occupies.

    `owned` is the run's own worktree. When it IS a primary — a local-agent repo,
    or an orchestrator dispatched into its own checkout — writing there is the
    run's job, so that repo is excluded rather than reported every time.
    """
    owned_resolved = None
    if owned is not None:
        try:
            owned_resolved = owned.resolve()
        except OSError:
            owned_resolved = None

    out: dict[str, dict] = {}
    for primary in discover_primaries(root):
        try:
            if owned_resolved is not None and primary.resolve() == owned_resolved:
                continue
        except OSError:
            continue
        state = _read_repo(primary)
        if state is not None:          # unreadable → omitted, never "clean"
            out[primary.name] = state
    return out


def diff(before: dict[str, dict], after: dict[str, dict],
         *, worktree: Path | None = None,
         root: Path | None = None) -> list[Mutation]:
    """Primaries whose dirty set or HEAD changed between the two snapshots.

    A repo present in only one snapshot is skipped: it was unreadable at one end,
    or the run's ownership exclusion differed, and neither is evidence of a leak.

    `worktree` enables attribution. Without it every finding reads as equally
    likely to be this run's, which on a 20-worker overnight turns one real leak
    into 20 identical-looking reports. A path already tracked in the primary at
    `head_before` (present before the run's window opened, not created during
    it) counts as an echo only when its CONTENT also matches the primary's
    current copy, not on presence alone — see `_tracked_at_baseline` and
    `_echo_content_matches`. Render output never counts as an echo at all: the
    individuator puts those same relative paths in every worktree, so presence
    is guaranteed rather than evidential — see `RENDER_OUTPUT_PREFIXES`.

    ATTRIBUTION IS THE ONLY THING ANY OF THESE NARROW. Each drops a path from
    `echoed_in_worktree`; none drops it from `added` or `removed`. A mutation
    that loses every echo this way is still a mutation and still reaches
    `needs_an_owner`, which is what keeps `platform/decisions/0025` true.
    Every working-tree mutation then either fails its run or reaches a signal
    store. A store answer other than `created` or `duplicate`, or an exception,
    prints `COULD NOT LOG` and leaves the paths only in `primary_mutations.json`.
    The store answers `duplicate` for a repeat of the same destruction on
    one day, and for a second additive-only change on a day that already logged
    one (`fingerprint`), so those two are recorded only in their own run's
    `primary_mutations.json`.
    """
    base = root or CONVIVY_ROOT

    def root_of(name: str) -> Path:
        return base / name

    out: list[Mutation] = []
    for repo in sorted(set(before) & set(after)):
        b, a = before[repo], after[repo]
        b_dirty, a_dirty = set(b.get("dirty") or ()), set(a.get("dirty") or ())
        added = tuple(sorted(a_dirty - b_dirty))
        removed = tuple(sorted(b_dirty - a_dirty))
        echoed: tuple[str, ...] = ()
        if worktree is not None and added:
            try:
                echoed = tuple(
                    p for p in added
                    if not _is_render_output(p)
                    and (worktree / p).exists()
                    and (not _tracked_at_baseline(root_of(repo), b.get("head"), p)
                         or _echo_content_matches(worktree / p, root_of(repo) / p)))
            except OSError:
                echoed = ()
        orphaned = False
        if b.get("head") and a.get("head") and b["head"] != a["head"] and root_of:
            orphaned = _orphaned_head(root_of(repo), b.get("head"), a.get("head"))
        m = Mutation(repo=repo, added=added, removed=removed,
                     head_before=b.get("head"), head_after=a.get("head"),
                     echoed_in_worktree=echoed, orphaned_head=orphaned)
        if added or removed or m.head_moved:
            out.append(m)
    return out


def write_baseline(run_dir: Path, snap: dict[str, dict]) -> None:
    """Persist the spawn-time snapshot beside the run's other started_* facts.

    Best-effort: a baseline that cannot be written means the check is skipped for
    this run, which must never abort a dispatch.
    """
    try:
        (run_dir / _BASELINE_FILENAME).write_text(json.dumps(snap, indent=2, sort_keys=True))
    except OSError:
        pass


def read_baseline(run_dir: Path) -> dict[str, dict] | None:
    """The spawn-time snapshot, or None if this run has no usable baseline."""
    path = run_dir / _BASELINE_FILENAME
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
