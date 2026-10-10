"""Leases, the write policy, and the sha precondition (ARCH-408).

The two protections do different jobs and are tested as different things: the
lease stops two sessions doing work that will be thrown away, and the content-sha
stops the lost update. A test suite that conflated them would let a regression in
the sha check hide behind a passing lease test.
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import kb_writes as kw

T0 = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(kw, "DB", tmp_path / "kb-writes.db")
    kw.ensure_db()
    return tmp_path / "kb-writes.db"


# --- leases ------------------------------------------------------------------


def test_acquire_grants_a_free_lease():
    lease, conflict = kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    assert conflict is None
    assert lease["holder"] == "run-1"
    assert lease["expires_at"] == "2026-07-25T16:00:00Z"      # 4h TTL


def test_a_foreign_unexpired_lease_conflicts_and_names_the_holder():
    """A conflict nobody can attribute is a conflict nobody can resolve, so the
    error carries the holder and the expiry rather than just refusing."""
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    lease, conflict = kw.acquire("projects/shared/reference/a.md", "run-2",
                                 now=T0 + timedelta(minutes=5))
    assert lease is None
    assert conflict["error"] == "leased"
    assert conflict["holder"] == "run-1"
    assert conflict["expires_at"] == "2026-07-25T16:00:00Z"


def test_same_holder_refreshes_silently():
    """Re-checking-out inside one session is normal, not an error."""
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    lease, conflict = kw.acquire("projects/shared/reference/a.md", "run-1",
                                 now=T0 + timedelta(hours=1))
    assert conflict is None
    assert lease["expires_at"] == "2026-07-25T17:00:00Z"       # pushed out, not refused


def test_an_expired_lease_is_taken_over_and_journaled():
    """The dead-session case: a co that checks out a doc and dies must not hold
    it forever. The takeover is recorded, because a silent one is unexplainable
    to whoever lost the lease."""
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    lease, conflict = kw.acquire("projects/shared/reference/a.md", "run-2",
                                 now=T0 + timedelta(hours=5))
    assert conflict is None
    assert lease["holder"] == "run-2"
    takeovers = [j for j in kw.recent_journal() if j["result"] == "takeover"]
    assert len(takeovers) == 1
    assert takeovers[0]["holder"] == "run-2"


def test_lease_for_treats_an_expired_row_as_absent():
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    assert kw.lease_for("projects/shared/reference/a.md", now=T0) is not None
    assert kw.lease_for("projects/shared/reference/a.md",
                        now=T0 + timedelta(hours=5)) is None


def test_release_only_works_for_the_holder():
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    assert kw.release("projects/shared/reference/a.md", "run-2", now=T0) is False
    assert kw.lease_for("projects/shared/reference/a.md", now=T0) is not None
    assert kw.release("projects/shared/reference/a.md", "run-1", now=T0) is True
    assert kw.lease_for("projects/shared/reference/a.md", now=T0) is None


def test_active_leases_excludes_expired_ones():
    kw.acquire("projects/shared/reference/a.md", "run-1", now=T0)
    kw.acquire("projects/shared/reference/b.md", "run-2",
               now=T0 + timedelta(hours=3))
    later = T0 + timedelta(hours=5)
    refs = [x["source_ref"] for x in kw.active_leases(now=later)]
    assert refs == ["projects/shared/reference/b.md"]


def test_acquire_rejects_an_anonymous_holder():
    with pytest.raises(ValueError):
        kw.acquire("projects/shared/reference/a.md", "", now=T0)


# --- content sha -------------------------------------------------------------


def test_content_sha_covers_frontmatter_not_just_the_body():
    """The index hashes the body alone. A frontmatter-only edit — a status flip,
    an audience change — must still invalidate a concurrent writer's base, so the
    precondition hash has to span the whole file."""
    a = "---\nstatus: draft\n---\n\nSame body.\n"
    b = "---\nstatus: current\n---\n\nSame body.\n"
    assert kw.content_sha(a) != kw.content_sha(b)


def test_content_sha_of_nothing_is_stable():
    """A create passes the sha of the empty string as its base, so this value is
    part of the contract rather than an accident."""
    assert kw.content_sha("") == kw.content_sha("")
    assert len(kw.content_sha("")) == 64


# --- write policy ------------------------------------------------------------


def test_direct_provenance_may_write_an_ordinary_doc():
    ok, why = kw.policy_for("projects/vivy/decisions/0001-x.md", "in-session")
    assert ok and why == ""


@pytest.mark.parametrize("provenance", ["verifier-pass", "generated", "keel", "steward"])
def test_provenances_taken_off_the_direct_path_no_longer_write_directly(provenance):
    """platform/decisions/0081 (operator, 2026-09-13, amended about 22:20 MST): only a
    decision the operator states themself, in session, writes directly under `in-session`.
    `verifier-pass` and `generated` came off once the Library reported neither
    had a live use left; `keel` and `steward` came off because both widened
    `in-session` past what the ruling now allows. All four now take the PR
    path, the same as any other change."""
    ok, why = kw.policy_for("projects/vivy/decisions/0001-x.md", provenance)
    assert not ok
    assert "kb_propose" in why
    assert "0081" in why


@pytest.mark.parametrize("ref", [
    "projects/company/briefs/ai-transformation-briefing-2026-07-25.md",
    "projects/platform/briefs/ai-engineering-briefing-2026-07-25.md",
    "projects/vivy/decisions/0001-x.md",
    "projects/newproject/briefs/x.md",
])
def test_generated_no_longer_writes_directly(ref):
    """Moved from tests/test_briefing_cage_write.py: the old path allowlist
    (GENERATED_PROJECTS/GENERATED_PATH_RE) granted `generated` a direct write
    on exactly these brief paths before platform/decisions/0081 narrowed the
    cage to `in-session` only. Kept to show the refusal holds even on the
    paths the allowlist used to grant."""
    ok, why = kw.policy_for(ref, "generated")
    assert not ok
    assert "kb_propose" in why


def test_generated_still_cannot_touch_the_roster():
    """Moved from tests/test_briefing_cage_write.py. The PR-only rule outranks
    every provenance, this one included."""
    ok, why = kw.policy_for("projects/agent-platform/agents/reviewer.md", "generated")
    assert not ok
    assert "kb_propose" in why


def test_generated_is_recognized_but_not_direct():
    """Moved from tests/test_briefing_cage_write.py."""
    assert "generated" in kw.NO_LONGER_DIRECT
    assert "generated" not in kw.DIRECT_PROVENANCE
    assert "generated" in kw.PROVENANCE_KINDS


def test_proposal_provenance_never_writes_to_main():
    ok, why = kw.policy_for("projects/vivy/decisions/0001-x.md", "proposal")
    assert not ok
    assert "kb_propose" in why


def test_unknown_provenance_is_refused():
    """Fail closed: an unrecognised provenance must never fall through to allowed."""
    ok, why = kw.policy_for("projects/vivy/decisions/0001-x.md", "trust-me")
    assert not ok
    assert "unknown provenance" in why


@pytest.mark.parametrize("ref", [
    "projects/agent-platform/agents/reviewer.md",
    "projects/agent-platform/agents/nested/thing.md",
    "projects/knowledge/profiles/daytime.yaml",
    "projects/vivy/profiles/overnight.yaml",
])
@pytest.mark.parametrize("provenance", ["verifier-pass", "in-session", "keel", "steward"])
def test_roster_and_profile_paths_are_pr_only_under_every_provenance(ref, provenance):
    """The bootstrapping guard. The roster the platform dispatches lives inside
    the repo the crew edits, so a bad commit here is a platform-wide outage — the
    watcher skips an invalid agent doc and resolve_agent then reports it missing.
    No provenance may bypass review for these paths."""
    ok, why = kw.policy_for(ref, provenance)
    assert not ok
    assert "kb_propose" in why


@pytest.mark.parametrize("ref", [
    "projects/some-future-project/agents/reviewer.md",
    "projects/some-future-project/profiles/daytime.yaml",
])
def test_a_roster_path_in_an_unknown_project_is_pr_only(ref):
    """The guard is a SEGMENT, so it covers a project that does not exist yet.

    This is the property PR G bought by dropping `PR_ONLY_PREFIXES`. The prefix it
    replaced named one directory, so Phase 0 step 5's re-file of the roster from
    `shared` onto `agent-platform` would have taken all 29 files out of the cage
    with nothing failing — the cage keeps working and simply stops covering them.
    Carrying both paths through three PRs is what it cost to avoid that, and this
    test is what makes the next such move cost nothing.
    """
    assert kw.is_pr_only(ref) is True


@pytest.mark.parametrize("ref", [
    "PROJECTS/AGENT-PLATFORM/AGENTS/reviewer.md",
    "Projects/Agent-Platform/Agents/reviewer.md",
    "projects/agent-platform/AGENTS/reviewer.md",
    "projects/vivy/PROFILES/overnight.yaml",
    "projects/vivy/Profiles/overnight.yaml",
])
def test_case_variation_cannot_evade_the_pr_only_guard(ref):
    """macOS ships a case-insensitive filesystem, so an uppercased path opens the
    SAME inode as the roster file and git stages the real entry — while a
    case-sensitive match reads it as an ordinary doc and allows the write.

    That defeated the only hard control this module has, on the platform it runs
    on. Found by security review of #243, reproduced live, fixed here.
    """
    assert kw.is_pr_only(ref) is True
    ok, why = kw.policy_for(ref, "in-session")
    assert not ok
    assert "kb_propose" in why


def test_a_separator_swap_cannot_evade_the_guard():
    assert kw.is_pr_only("projects\\agent-platform\\agents\\reviewer.md") is True


@pytest.mark.parametrize("ref", [
    # Every block serving today, by its real repo path.
    "projects/platform/instruction-blocks/verify-before-you-report.md",
    "projects/platform/instruction-blocks/act-rather-than-ask.md",
    "projects/platform/instruction-blocks/a-guard-needs-a-seen-occurrence.md",
    "projects/platform/instruction-blocks/no-agent-merges-a-pull-request.md",
    "projects/platform/instruction-blocks/review-a-pull-request.md",
    "projects/platform/instruction-blocks/open-a-pull-request.md",
    "projects/platform/instruction-blocks/working-with-the-operator.md",
    # And the segment property: a project that does not exist yet is covered.
    "projects/some-future-project/instruction-blocks/whatever.md",
])
@pytest.mark.parametrize("provenance", ["verifier-pass", "in-session", "keel", "steward"])
def test_instruction_blocks_are_pr_only_under_every_provenance(ref, provenance):
    """An instruction block is composed into the CLAUDE.md of every session
    its selectors bind, so a commit here changes what agents obey — the same
    property that made the roster PR-only, at a path this list predates.

    `in-session` is the parameter that matters. The other three no longer write
    to main under any path (platform/decisions/0081), so only this one could have
    reached a block directly, and it is honoured rather than verified.
    """
    assert kw.is_pr_only(ref) is True
    ok, why = kw.policy_for(ref, provenance)
    assert not ok
    assert "kb_propose" in why
    # The refusal must name what this path IS. `agentctl kb apply` writes this
    # string verbatim into the write journal's `detail` column, so a refusal
    # attributing an instruction block to the roster is a permanent audit
    # record of a reason that did not apply.
    assert "instruction-block path" in why
    assert "roster" not in why and "profile" not in why


def test_every_guarded_segment_has_its_own_refusal_reason():
    """A segment added to the tuple without a reason would raise KeyError inside
    `policy_for` — at the moment of refusing a write, which is the worst place to
    discover it. Pinned so the two structures cannot drift apart."""
    assert set(kw.PR_ONLY_SEGMENTS) | {kw.POLICY_PATH} == set(kw.PR_ONLY_REASONS)


@pytest.mark.parametrize("ref,what,cost", [
    ("projects/agent-platform/agents/reviewer.md", "roster path",
     "breaks agent dispatch platform-wide"),
    ("projects/vivy/profiles/overnight.yaml", "profile path",
     "breaks agent dispatch platform-wide"),
    ("projects/platform/instruction-blocks/act-rather-than-ask.md",
     "instruction-block path", "changes what agents obey"),
])
def test_the_refusal_names_the_protection_it_hit(ref, what, cost):
    """Three guarded things, two different consequences, and the caller is told
    which one it hit. The roster and profile wording is unchanged from before
    this segment existed, so an instruction block gaining an accurate message did
    not cost the other two theirs.

    BOTH HALVES OF THE TUPLE ARE ASSERTED, not just the `what`. Swapping the
    cost strings between two segments would leave the key sets equal and every
    `what` correct while writing a wrong consequence into the write journal's
    permanent `detail` column — a mistake only the second assertion can catch.
    """
    ok, why = kw.policy_for(ref, "in-session")
    assert not ok
    assert what in why
    assert cost in why


@pytest.mark.parametrize("ref", [
    "PROJECTS/PLATFORM/INSTRUCTION-BLOCKS/verify-before-you-report.md",
    "projects/platform/Instruction-Blocks/verify-before-you-report.md",
    "projects\\platform\\instruction-blocks\\verify-before-you-report.md",
])
def test_case_or_separator_variation_cannot_evade_the_block_guard(ref):
    """The same evasions security review of #243 found against the roster guard,
    run against the new segment rather than assumed to carry over — the folding
    happens once in `is_pr_only`, but a segment added without this test is a
    segment nobody checked on the case-insensitive filesystem this runs on."""
    assert kw.is_pr_only(ref) is True


def test_a_doc_about_instruction_blocks_is_not_pr_only():
    """The guard keys on the path segment, not the subject. A decision record or
    an explainer discussing instruction blocks is ordinary content and must stay
    writable, or the guard has quietly annexed the documentation about itself."""
    ok, _ = kw.policy_for(
        "projects/platform/decisions/0082-instruction-blocks.md", "in-session")
    assert ok


def test_a_doc_merely_mentioning_agents_is_not_pr_only():
    """The guard keys on a path SEGMENT, not on the word — a decision doc about
    the roster is ordinary content.

    Worth more under the segment guard than it was under the prefix: `/agents/`
    needs the slashes on both sides, and this is what pins that a filename or a
    slug containing the word does not trip it.
    """
    ok, _ = kw.policy_for("projects/platform/decisions/0007-agents-roster.md", "in-session")
    assert ok


# --- trailer -----------------------------------------------------------------


def test_a_holder_carrying_a_newline_is_rejected():
    """A newline in the holder splits the commit message and plants a SECOND
    `KB-Write:` line. The bypass audit greps for exactly that string, so a forged
    line means the audit reads the attacker's claim instead of the cage's record."""
    forged = "run-1\nKB-Write: update projects/agent-platform/agents/reviewer.md in-session run-1"
    assert kw.valid_holder(forged) is False
    with pytest.raises(ValueError):
        kw.write_trailer("update", "projects/vivy/x.md", "in-session", forged)


@pytest.mark.parametrize("holder", ["run-7f3a91", "session:operator", "a", "A1._-:"])
def test_ordinary_holder_identities_are_accepted(holder):
    assert kw.valid_holder(holder) is True


@pytest.mark.parametrize("holder", ["", " ", "run 1", "run;1", "x" * 65, "-leading"])
def test_malformed_holder_identities_are_rejected(holder):
    assert kw.valid_holder(holder) is False


def test_a_message_cannot_smuggle_its_own_trailer():
    assert kw.message_is_trailer_safe("An ordinary body.\nWith two lines.") is True
    assert kw.message_is_trailer_safe(
        "Looks fine.\nKB-Write: update projects/shared/agents/x.md steward run-9") is False
    assert kw.message_is_trailer_safe(
        "Indented is still a trailer.\n   KB-Write: x y z w") is False


def test_a_message_cannot_smuggle_a_person_line():
    assert kw.message_is_trailer_safe("Fine.\nKB-Person: operator@example.com") is False
    assert kw.message_is_trailer_safe("Indented.\n   KB-Person: operator@example.com") is False


def test_person_may_write_an_ordinary_doc():
    ok, why = kw.policy_for("projects/platform/reference/seeded.md", "person")
    assert ok and why == ""


@pytest.mark.parametrize("ref, segment", [
    ("projects/agent-platform/agents/reviewer.md", "roster"),
    ("projects/agent-platform/profiles/daytime.yaml", "profile"),
    ("projects/agent-platform/instruction-blocks/fleet.md", "instruction-block"),
])
def test_person_is_still_refused_on_a_guarded_path_and_the_reason_names_it(ref, segment):
    """Written on the reason, not the boolean: before `person` was a provenance
    the refusal was 'unknown provenance', which also refuses and proves nothing
    about the guard."""
    ok, why = kw.policy_for(ref, "person")
    assert not ok
    assert segment in why
    assert "unknown provenance" not in why


def test_person_trailer_names_the_email_on_one_line():
    assert kw.person_trailer("operator@example.com") == "KB-Person: operator@example.com"


@pytest.mark.parametrize("email", ["a b@example.com", "a@example.com\nKB-Write: x", " a@example.com",
                                   "a@example.com\t", "", "a" * 250 + "@example.com"])
def test_person_trailer_refuses_whitespace_and_an_unbounded_value(email):
    with pytest.raises(ValueError):
        kw.person_trailer(email)


def test_a_whitespace_field_cannot_produce_a_malformed_trailer():
    """The trailer is four space-separated fields, so a path containing a space
    yields a line an audit scanner misparses. No policy depends on trailer
    content, but a record read wrongly is worth as little as one unreadable."""
    with pytest.raises(ValueError):
        kw.write_trailer("update", "projects/my doc.md", "steward", "run-1")
    with pytest.raises(ValueError):
        kw.write_trailer("update", "projects/x.md", "in session", "run-1")


def test_trailer_carries_everything_an_audit_needs():
    """Any commit on knowledge/main without one of these and without a merge
    parent is a write that bypassed the cage."""
    line = kw.write_trailer("update", "projects/vivy/decisions/0001-x.md",
                            "in-session", "run-7f3a91")
    assert line == "KB-Write: update projects/vivy/decisions/0001-x.md in-session run-7f3a91"


# --- journal -----------------------------------------------------------------


def test_journal_records_refusals_which_git_cannot():
    """A refused write produces no commit, so the git log cannot hold it. That is
    exactly what this table is for."""
    kw.journal("apply", source_ref="projects/vivy/x.md", holder="run-1",
               provenance="in-session", result="stale", detail="base=abc current=def")
    entries = kw.recent_journal()
    assert entries[0]["result"] == "stale"
    assert entries[0]["provenance"] == "in-session"


def test_journal_failure_never_breaks_the_write(monkeypatch, tmp_path):
    """Best-effort by design: the audit record must not be able to fail the thing
    it is describing.

    The failure modelled is a real one — a store path under an existing FILE, so
    creating its parent directory raises OSError rather than a database error.
    That distinction caught a genuine gap: the handler originally caught only
    `sqlite3.Error`, so an unwritable location would have propagated out of a
    best-effort call and failed the write it was merely describing.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(kw, "DB", blocker / "sub" / "kb-writes.db")
    kw.journal("apply", source_ref="x", result="ok")     # must not raise


POLICY = "projects/platform/policy/agent-policy.yaml"


@pytest.mark.parametrize("ref", [
    POLICY,
    POLICY.upper(),
    POLICY.replace("/", "\\"),
    POLICY.upper().replace("/", "\\"),
])
@pytest.mark.parametrize("provenance", list(kw.PROVENANCE_KINDS))
def test_the_agent_policy_file_is_pr_only_under_every_provenance(ref, provenance):
    """One exact path, folded for case and separators like every other guard, so
    the case-insensitive disk cannot open it. `in-session` and `person` are the
    provenances that would otherwise write directly."""
    assert kw.is_pr_only(ref)
    ok, why = kw.policy_for(ref, provenance)
    assert not ok
    assert "agent policy file" in why and "kb_propose" in why


def test_a_direct_in_session_write_of_the_policy_file_names_the_policy():
    ok, why = kw.policy_for(POLICY, "in-session")
    assert not ok
    assert "agent policy file" in why and "kb_propose" in why


@pytest.mark.parametrize("ref", [
    "projects/platform/policy/other.yaml",
    "projects/constance/policy/agent-policy.yaml",
    "projects/platform/policy/agent-policy.yml",
    "projects/platform/agent-policy.yaml",
])
def test_only_the_exact_policy_path_is_guarded(ref):
    assert not kw.is_pr_only(ref)


# --- journal: author_kind / person --------------------------------------------

OLD_JOURNAL_SCHEMA = """
CREATE TABLE leases (
    source_ref   TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    note         TEXT
);
CREATE TABLE journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    verb         TEXT NOT NULL,
    source_ref   TEXT,
    holder       TEXT,
    provenance   TEXT,
    result       TEXT NOT NULL,
    detail       TEXT
);
"""


def _seed_old_shape_db(path) -> None:
    """A journal table exactly as it existed before `author_kind`/`person`,
    carrying one row — the shape 1,093 of 1,429 real rows are in today."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(OLD_JOURNAL_SCHEMA)
        conn.execute(
            "INSERT INTO journal (ts, verb, source_ref, holder, provenance, result, detail) "
            "VALUES ('2026-01-01T00:00:00Z', 'apply', 'projects/vivy/x.md', 'run-1', "
            "'in-session', 'ok', NULL)")
        conn.commit()
    finally:
        conn.close()


def test_migrating_an_old_shape_db_leaves_existing_rows_unknown(tmp_path, monkeypatch):
    """The migration must not reinterpret a blank `provenance` row as anyone's
    write — every row from before this column existed reads as unknown, which
    fails closed as not a person."""
    db_path = tmp_path / "old.db"
    _seed_old_shape_db(db_path)
    monkeypatch.setattr(kw, "DB", db_path)

    kw.ensure_db()

    rows = kw.recent_journal()
    assert len(rows) == 1
    assert rows[0]["provenance"] == "in-session"      # untouched
    assert rows[0]["author_kind"] is None
    assert rows[0]["person"] is None


def test_migrating_an_old_shape_db_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    _seed_old_shape_db(db_path)
    monkeypatch.setattr(kw, "DB", db_path)

    kw.ensure_db()
    kw.ensure_db()   # must not raise a second time, or duplicate the column

    columns = [r[1] for r in sqlite3.connect(str(db_path)).execute(
        "PRAGMA table_info(journal)")]
    assert columns.count("author_kind") == 1
    assert columns.count("person") == 1
    assert len(kw.recent_journal()) == 1


def _open_and_acquire(db_path_str, holder, barrier):
    """Run in a forked child: open the shared unmigrated db through the real
    `ensure_db`/`acquire` path, at the same moment as its siblings. A separate
    process rather than a thread, so this is a separate sqlite3 connection
    racing the others for the same file lock, the shape the reviewer's finding
    was about (PR #691 round 1)."""
    import kb_writes as kw2
    from pathlib import Path
    kw2.DB = Path(db_path_str)
    barrier.wait()
    kw2.acquire(f"projects/x/{holder}.md", holder)


def test_six_processes_opening_an_unmigrated_db_at_once_all_succeed(tmp_path):
    """Reproduces the reviewer's round-1 finding: six processes calling
    `acquire()` against a fresh-off-deploy, unmigrated db raced the
    check-then-ALTER in `_migrate_journal_author_kind` and three of six crashed
    with `sqlite3.OperationalError: duplicate column name`. Against the
    unfixed migration this failed every one of 20 runs; against the fix here,
    0 of 20 (see the PR body for the full count).

    Fork, not spawn: a forked child inherits this test process's already
    path-configured interpreter, so it can `import kb_writes` without redoing
    the sys.path setup conftest.py did for the parent.
    """
    ctx = mp.get_context("fork")
    db_path = tmp_path / "unmigrated.db"
    _seed_old_shape_db(db_path)

    n = 6
    barrier = ctx.Barrier(n)
    procs = [ctx.Process(target=_open_and_acquire,
                          args=(str(db_path), f"run-{i}", barrier))
             for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    exit_codes = [p.exitcode for p in procs]
    assert exit_codes == [0] * n, exit_codes

    columns = [r[1] for r in sqlite3.connect(str(db_path)).execute(
        "PRAGMA table_info(journal)")]
    assert columns.count("author_kind") == 1
    assert columns.count("person") == 1


def test_ensure_db_on_a_migrated_db_takes_no_write_lock(store):
    """Reproduces the reviewer's round-2 finding: taking BEGIN IMMEDIATE
    before checking whether a migration is needed means every open of an
    already-migrated db — and `ensure_db` runs on every kb_writes operation —
    serializes on a write lock it never needed. Hold a write transaction open
    on the store from a second connection and confirm `ensure_db` returns
    right away instead of waiting out `MIGRATION_BUSY_TIMEOUT_MS`.

    Fails against 38a3f43e: that revision's migration takes BEGIN IMMEDIATE
    unconditionally, so it queues behind the held lock and this blocks for
    the full busy timeout before raising `sqlite3.OperationalError`.
    """
    blocker = sqlite3.connect(str(store))
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO leases (source_ref, holder, acquired_at, expires_at) "
        "VALUES ('projects/x/blocker.md', 'blocker', 'a', 'b')")
    try:
        start = time.monotonic()
        kw.ensure_db()
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 1.0, elapsed


def test_check_constraint_refuses_an_unrecognized_author_kind(store):
    conn = sqlite3.connect(str(store))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO journal (ts, verb, result, author_kind) "
            "VALUES ('t', 'apply', 'ok', 'robot')")


def test_check_constraint_refuses_a_person_name_under_the_agent_kind(store):
    conn = sqlite3.connect(str(store))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO journal (ts, verb, result, author_kind, person) "
            "VALUES ('t', 'apply', 'ok', 'agent', 'operator@example.com')")


def test_check_constraint_refuses_the_person_kind_with_no_name(store):
    conn = sqlite3.connect(str(store))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO journal (ts, verb, result, author_kind) "
            "VALUES ('t', 'apply', 'ok', 'person')")


def test_check_constraint_allows_unknown_author_kind_with_no_name(store):
    """The NULL/NULL pair every pre-existing row carries must stay insertable —
    the CHECK targets a mismatch, not the absence of both."""
    conn = sqlite3.connect(str(store))
    conn.execute("INSERT INTO journal (ts, verb, result) VALUES ('t', 'apply', 'ok')")
    conn.commit()


def test_journal_always_records_agent_regardless_of_provenance():
    """`journal` has no kwarg that could carry 'person' through — passing
    `provenance='person'` must not be read as a claim about who composed it."""
    kw.journal("apply", source_ref="x", holder="run-1", provenance="person", result="ok")
    entry = kw.recent_journal()[0]
    assert entry["author_kind"] == "agent"
    assert entry["person"] is None


def test_journal_person_records_the_named_person():
    kw.journal_person("apply", person="operator@example.com", source_ref="x",
                      holder="run-1", provenance="person", result="ok")
    entry = kw.recent_journal()[0]
    assert entry["author_kind"] == "person"
    assert entry["person"] == "operator@example.com"


@pytest.mark.parametrize("person", ["", None])
def test_journal_person_refuses_an_unverified_person(person):
    with pytest.raises(ValueError):
        kw.journal_person("apply", person=person, source_ref="x", result="ok")
    assert kw.recent_journal() == []
