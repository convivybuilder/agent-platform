"""issue_01M2GDVPR81Z0X991Z7XD3VR0M step 5 (the operator's 2026-09-14 ruling): overnight-orchestrator,
knowledge-scribe, kb-sync-verifier and `agentctl full-monty` are retired. The operator,
2026-09-14: "we're doing things differently now, events-based rather than on cron."

The same epic's comment 2661 (the operator, 2026-09-14, ~18:20 MST) retired three more:
the overnight stage agents overnight-triage, overnight-fix-drain and
overnight-verify. `bin/launch-overnight` (agent-platform#578, merged as
adff106) was their only dispatcher and is deleted.

Two shapes, checked separately:

  1. NAME_RE — one of the seven retired agent names (or the hollis-orchestrator
     variant) used as an exact Python string literal: `== "kb-sync-verifier"`, a
     set/dict element, an argument. This is what SPECIAL-CASES an agent by name —
     dispatch, tool denial, purpose derivation. A prose mention inside a longer
     sentence ("the overnight-orchestrator's 240-min in-flight estimate") does not
     match; those are genuine incident history and are not what this guards. Three
     exceptions, each scoped to a single constant rather than a whole file:
       - lib/knowledge/knowledge.py's ALLOWED_AUDIENCE set names all seven, as
         `audience` values a document can still carry (documents on knowledge/main
         already do) — that isn't dispatch special-casing.
       - bin/orchestrator-tokens' ORCHESTRATION_STAGE_AGENTS classifies the token
         cost of runs already recorded on disk; with no dispatcher left it reads
         old data rather than producing new dispatch.
       - lib/agent-platform/registry.py's _SUPERVISOR_STAGE_AGENTS must stay in
         step with `is_orchestration_agent` per that file's own comment (a second,
         drifted definition of the same set is exactly the ARCH-422 hole it
         exists to close); with no dispatcher the entries have no effect, and
         registry.py is outside this PR's scope.
     NAME_SCOPED_EXCLUSIONS strips just each named constant's literal block before
     scanning — a special-case added anywhere ELSE in one of these three files
     still trips the guard below.
  2. CMD_RE — the literal invocation phrase `agentctl scribe verify`, `agentctl scribe
     apply` or `agentctl full-monty`, wherever it appears (code or comment), since the
     three subcommands are gone and any surviving mention is an instruction or a
     dispatch path pointed at a command that no longer exists.

Both patterns are red against origin/main before this PR and green after it, except
for the explicit allowlists below — files or constants this PR deliberately keeps
naming these strings, and why.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

NAME_RE = re.compile(
    r'''(?<![\w-])["'](overnight-orchestrator|hollis-overnight-orchestrator|'''
    r'''knowledge-scribe|kb-sync-verifier|overnight-triage|overnight-fix-drain|'''
    r'''overnight-verify)["'](?![\w-])'''
)
CMD_RE = re.compile(r"agentctl\s+(?:scribe\s+(?:verify|apply)|full-monty)\b")

SCAN_DIRS = ("bin", "lib/agent-platform", "lib/knowledge")

# NAME_RE exclusions, each scoped to a single constant's literal block rather
# than a whole file:
#   - lib/knowledge/knowledge.py's ALLOWED_AUDIENCE set names all seven
#     strings deliberately, as legal `audience` values a document can still
#     carry (see that file's own comment on the set). Already scoped before
#     this PR; the three new names added to that same set are covered for
#     free since the sub is over the whole set literal.
#   - bin/orchestrator-tokens' ORCHESTRATION_STAGE_AGENTS sums the token cost
#     of past runs by agent name (see that file's own comment); reading old
#     data isn't the special-casing shape this test guards against.
#   - lib/agent-platform/registry.py's _SUPERVISOR_STAGE_AGENTS is required by
#     that file's own comment to mirror ORCHESTRATION_STAGE_AGENTS exactly;
#     changing it is out of scope for this PR (registry.py guards fleet
#     concurrency and isn't touched here).
# Each is a roster of readable history or a mirror of one, not the
# special-casing shape (dispatch, tool denial, purpose derivation) this test
# guards against, so only the text inside each named constant's literal is
# exempted.
NAME_SCOPED_EXCLUSIONS = {
    "lib/knowledge/knowledge.py": re.compile(
        r"ALLOWED_AUDIENCE = \{\n.*?\n\}\n", re.DOTALL
    ),
    "bin/orchestrator-tokens": re.compile(
        r"ORCHESTRATION_STAGE_AGENTS = \{.*?\}\n", re.DOTALL
    ),
    "lib/agent-platform/registry.py": re.compile(
        r"_SUPERVISOR_STAGE_AGENTS = frozenset\(\{.*?\}\)\n", re.DOTALL
    ),
}

# Files this PR deliberately leaves naming a retired command, and why. Every entry
# needs a reason; an entry with no reason is a bug the next reader won't be able to
# tell from a genuine exception.
ALLOWED_CMD_MENTIONS = {
    # The lab-app template's `<prefix>-overnight-orchestrator` grants
    # `Bash(overnight-manifest:*)` — a known dangling reference (see this PR's body),
    # named in the epic as the one file to leave and note rather than edit.
    "bin/create-lab-app",
    # A still-live negative instruction to the dispatched `explainer` agent: its
    # shared roster definition grants Bash(agentctl scribe verify:*)/
    # Bash(agentctl scribe apply:*) for its OTHER invocation (KB fact-sync), and that
    # grant is not dropped until knowledge#471 merges — after this PR, per the epic's
    # ordering. Telling explainer not to use those verbs stays correct (and newly
    # relevant: the subcommand is gone, so the attempt would now hard-fail) until
    # that follow-up PR narrows the roster itself.
    "bin/changelog-drain",
    # Historical-record readers/pools (this PR's KEEP list): budget.py's `full_monty`
    # capped-origin pool, telemetry.py's `VALID_ORIGINS`, and kb_audit.py's `SCRIBE_RE`
    # (which recognizes the commit-message signature `agentctl scribe apply` left by
    # the retired write path on documents already merged to knowledge/main) all keep
    # reading rows/commits a live producer wrote before this PR. Producers removed,
    # readers kept.
    "lib/agent-platform/budget.py",
    "lib/agent-platform/telemetry.py",
    "lib/agent-platform/kb_audit.py",
}


def _scan():
    name_hits: dict[str, list[str]] = {}
    cmd_hits: dict[str, list[str]] = {}
    for d in SCAN_DIRS:
        base = REPO / d
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(REPO))
            try:
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            scoped = NAME_SCOPED_EXCLUSIONS.get(rel)
            scan_text = scoped.sub("", text) if scoped else text
            names = NAME_RE.findall(scan_text)
            if names:
                name_hits[rel] = names
            if CMD_RE.search(text) and rel not in ALLOWED_CMD_MENTIONS:
                cmd_hits[rel] = CMD_RE.findall(text)
    return name_hits, cmd_hits


def test_no_file_special_cases_a_retired_agent_by_name():
    """NAME_RE: no file under bin/ or lib/ compares, stores, or keys on
    overnight-orchestrator / hollis-overnight-orchestrator / knowledge-scribe /
    kb-sync-verifier / overnight-triage / overnight-fix-drain / overnight-verify
    as an exact string — the shape special-casing takes."""
    name_hits, _ = _scan()
    assert not name_hits, (
        f"a retired agent name is still used as an exact string literal: {name_hits}. "
        "That is the special-casing shape (dispatch, tool denial, purpose "
        "derivation) this removal exists to close — not a prose mention."
    )


def test_no_file_names_a_retired_subcommand_as_a_command():
    """CMD_RE: no file outside ALLOWED_CMD_MENTIONS invokes or instructs toward
    `agentctl scribe verify`, `agentctl scribe apply` or `agentctl full-monty` —
    all three subcommands are deleted from bin/agentctl."""
    _, cmd_hits = _scan()
    assert not cmd_hits, (
        f"a retired agentctl subcommand is still named as a command: {cmd_hits}. "
        "`scribe verify`, `scribe apply` and `full-monty` no longer exist as "
        "agentctl subcommands; add the file to ALLOWED_CMD_MENTIONS with a reason "
        "only if the mention is a deliberate historical-record exception."
    )


def test_the_allowlist_still_names_real_files():
    """A file removed from the tree should leave this allowlist too — an entry
    naming nothing is a dead exception nobody will think to prune."""
    missing = sorted(
        p
        for p in (*ALLOWED_CMD_MENTIONS, *NAME_SCOPED_EXCLUSIONS)
        if not (REPO / p).is_file()
    )
    assert not missing, (
        f"ALLOWED_CMD_MENTIONS/NAME_SCOPED_EXCLUSIONS names files that no "
        f"longer exist: {missing}"
    )
