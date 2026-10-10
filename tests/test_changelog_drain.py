"""`changelog-drain` — the pass that gets an entry written for every merge.

The properties worth pinning are the ones that decide whether the log stays
trustworthy and whether the pass stays affordable: a merge that already has an
entry is never rewritten, a repo agentctl cannot dispatch against is skipped by
name rather than retried forever, a truncated read is reported rather than
passing as clean, and an explainer that returns nothing never posts an empty
entry (ARCH-411).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def cd(changelog_drain):
    return changelog_drain


def _merge(pr, product="vivy", produced_at="2026-07-26T10:00:00Z"):
    return {"product": product, "source": "github", "kind": "changelog",
            "refs": {"pr": pr, "url": f"https://github.com/convivy/{product}/pull/{pr}"},
            "body": f"feat: thing {pr}\n\n## Summary\n\nraw pr body",
            "produced_at": produced_at}


def _entry(pr, product="vivy"):
    return {"product": product, "source": "co", "kind": "changelog",
            "refs": {"pr": pr}, "body": f"**Merged:** a thing. #{pr}"}


def _stub_reads(monkeypatch, cd, merges, entries):
    def fake(path, params):
        return merges if params.get("source") == "github" else entries
    monkeypatch.setattr(cd, "_get", fake)


def test_a_merge_without_an_entry_is_pending(cd, monkeypatch):
    _stub_reads(monkeypatch, cd, [_merge(1), _merge(2)], [_entry(1)])
    pending, warnings = cd._pending("2026-07-26T00:00:00Z")
    assert [p["refs"]["pr"] for p in pending] == [2]
    assert warnings == []


def test_the_same_pr_number_in_two_products_is_two_pieces_of_work(cd, monkeypatch):
    """The key is (product, pr). Keying on the PR number alone would let vivy#7
    suppress the entry for constance#7 — different work, same number."""
    _stub_reads(monkeypatch, cd,
                [_merge(7, "vivy"), _merge(7, "constance")],
                [_entry(7, "vivy")])
    pending, _ = cd._pending("2026-07-26T00:00:00Z")
    assert [(p["product"], p["refs"]["pr"]) for p in pending] == [("constance", 7)]


def test_a_merge_captured_twice_yields_one_dispatch(cd, monkeypatch):
    _stub_reads(monkeypatch, cd, [_merge(3), _merge(3)], [])
    pending, _ = cd._pending("2026-07-26T00:00:00Z")
    assert len(pending) == 1


def test_a_merge_with_no_pr_number_is_skipped(cd, monkeypatch):
    """post-changelog refuses an entry with no PR link, so a merge we cannot name
    a PR for has nothing postable — dispatching for it would burn a run to
    produce something that is then refused."""
    nameless = _merge(1)
    nameless["refs"] = {}
    _stub_reads(monkeypatch, cd, [nameless], [])
    pending, _ = cd._pending("2026-07-26T00:00:00Z")
    assert pending == []


def test_pending_is_oldest_first(cd, monkeypatch):
    """A burst larger than --max-dispatch drains in order; newest-first would
    leave the oldest merges permanently at the back of the queue."""
    _stub_reads(monkeypatch, cd,
                [_merge(2, produced_at="2026-07-26T12:00:00Z"),
                 _merge(1, produced_at="2026-07-26T09:00:00Z")], [])
    pending, _ = cd._pending("2026-07-26T00:00:00Z")
    assert [p["refs"]["pr"] for p in pending] == [1, 2]


def test_a_capped_merges_read_warns_rather_than_reporting_a_clean_pass(cd, monkeypatch):
    """A truncated MERGES read is safe — this pass sees a prefix and the rest is
    picked up next pass — but a drain that reported success over it would look
    like it had covered work it never saw."""
    _stub_reads(monkeypatch, cd, [_merge(i) for i in range(cd._API_PAGE_CAP)], [])
    _, warnings = cd._pending("2026-07-01T00:00:00Z")
    assert warnings and "cap" in warnings[0]


def test_a_capped_entries_read_refuses_the_pass(cd, monkeypatch):
    """A truncated ENTRIES read is NOT safe, and the asymmetry is the point.
    `have` is what suppresses an already-written merge, so an incomplete `have`
    makes written merges look unwritten and the drain re-explains them —
    duplicate entries and duplicate spend. Refusing beats advising the operator
    to narrow the window and hoping they do (code review, #250)."""
    _stub_reads(monkeypatch, cd,
                [_merge(1)], [_entry(i) for i in range(cd._API_PAGE_CAP)])
    with pytest.raises(SystemExit) as exc:
        cd._pending("2026-07-01T00:00:00Z")
    assert "incomplete" in str(exc.value)


def test_a_stuck_explainer_is_one_failure_not_a_lost_pass(cd, monkeypatch):
    """Uncaught, TimeoutExpired aborted the whole loop: every merge behind the
    stuck one was skipped and the wrote/failed line never printed (code review,
    #250)."""
    def _hang(*a, **k):
        raise cd.subprocess.TimeoutExpired(cmd="agentctl", timeout=1800)

    monkeypatch.setattr(cd.subprocess, "run", _hang)
    assert cd._spawn("vivy", "1050", "a task") is None


def test_example_run_id_in_transcript_does_not_steal_the_real_one(cd, monkeypatch):
    """Regression for issue_01KYGCTN0MZQR531ZVN2APSB3V.

    PRs about agentctl itself quote example run-ids (run-7f3a91, run-abc123) in
    their diffs. The explainer echoes those strings in its transcript, which
    streams to stdout. The old bare re.search over combined stdout+stderr took the
    FIRST match anywhere — so the example run-id, which appears early in the
    transcript, beat the real one that came later in stderr.

    The fix anchors the search to the 'spawned ...' line in stderr only. The
    scenario here: stdout carries an example run-id before the real one, stderr
    carries the real one on the spawned line. The caller must return the real one.
    """
    class _Done:
        returncode = 0
        # stdout contains the streamed transcript — the agent echoed an example run-id
        stdout = '{"type":"text","text":"see run-7f3a91 for an example"}\n'
        # stderr contains the actual spawned line
        stderr = "spawned run-deadbeef  pid=99 agent=explainer worktree=/tmp/x\n"

    monkeypatch.setattr(cd.subprocess, "run", lambda *a, **k: _Done())
    assert cd._spawn("vivy", "243", "a task") == "run-deadbeef"


def test_the_pass_continues_past_a_stuck_explainer(cd, monkeypatch, tmp_path):
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    (tmp_path / "Convivy" / "vivy" / ".git").mkdir(parents=True)
    _stub_reads(monkeypatch, cd, [_merge(1), _merge(2)], [])
    tried = []

    def _spawn(repo, pr, task):
        tried.append(pr)
        return None if pr == "1" else "run-abc123"

    monkeypatch.setattr(cd, "_spawn", _spawn)
    monkeypatch.setattr(cd, "_final_output", lambda run_id: "**Merged:** x. #2")
    posted = []
    monkeypatch.setattr(cd, "_post", lambda r, p, e: posted.append(p) or True)

    rc = cd.main(["--since", "2026-07-26T00:00:00Z"])
    assert tried == ["1", "2"]      # the second merge was still attempted
    assert posted == ["2"]
    assert rc == 1                  # and the failure is still reported


def test_only_repos_with_a_primary_checkout_are_dispatchable(cd, monkeypatch, tmp_path):
    """`agentctl spawn --on <repo>` needs a primary checkout (ARCH-391); a merge
    in a repo without one can never get an entry, so it is skipped by name rather
    than retried every pass forever."""
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    (tmp_path / "Convivy" / "vivy" / ".git").mkdir(parents=True)
    assert cd._dispatchable("vivy") is True
    assert cd._dispatchable("selector-backend") is False


def test_the_task_demands_a_pr_reference_and_a_bare_final_output(cd):
    task = cd._task_for(_merge(42), "42")
    assert "#42" in task
    assert "FINAL MESSAGE must be the entry text" in task
    assert "CommonMark" in task
    # The agent must not post — the drain does, which is what keeps the posting
    # tool out of five repo-local agent definitions.
    assert "Do not post anything yourself" in task
    # The PR is merged and its branch is gone, so the brief must say to read it
    # from main rather than expect a checked-out PR branch.
    assert "ALREADY" in task and "MERGED" in task


def test_the_task_writes_for_the_operator_not_for_the_pr_author(cd):
    """The manual per-product changelog step is retired (2026-08-10) and this
    brief is the sole author of team-site entries now, so the voice complaint
    that kept the manual step alive has to land here: no conventional-commits
    prefix, module path, or build-step code surviving into the entry, and no
    bare internal identifier without a gloss."""
    task = cd._task_for(_merge(42), "42")
    assert "conventional-commits prefix" in task
    assert "B3/P1" in task  # a build-step code, named as the thing to strip
    assert "user- or operator-visible effect" in task
    assert "gloss" in task
    # A worked before/after pair, not just the instruction — the docstring's own
    # example teaches the pattern better than the rule alone.
    assert "should not become" in task and "should become" in task
    # The security carve-out: a security fix names the category of risk closed,
    # never the exploitable mechanism — this entry publishes to the public site,
    # often before every affected deployment has the fix.
    assert "category of risk" in task
    assert "exploitable mechanism" in task
    assert "security fix" in task


def test_the_spawn_resolves_the_explainer_from_a_donor_dir(cd, monkeypatch, tmp_path):
    """Without --agents-dir the first backfill failed 40 of 88 merges: the five
    LOCAL_AGENT_REPOS resolve agents from their own .claude/agents/ but none
    defines an explainer; selector-backend had no knowledge-ctx project mapping at
    the time. agentctl treats a directory-resolved agent as local — reads the
    definition off disk, skips profile activation — so one donor dir covers both.
    selector-backend gained a mapping in PR #267; this test guards the
    LOCAL_AGENT_REPOS explainer gap."""
    donor = tmp_path / "Convivy" / "vivy" / ".claude" / "agents"
    donor.mkdir(parents=True)
    (donor / "explainer.md").write_text("---\nname: explainer\n---\nbody")
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")

    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", "spawned run-abc123  pid=1 agent=explainer"

    monkeypatch.setattr(cd.subprocess, "run",
                        lambda cmd, *a, **k: (seen.update(cmd=cmd), _Done())[1])
    assert cd._spawn("convivy-deck", "33", "a task") == "run-abc123"
    assert "--agents-dir" in seen["cmd"]
    assert str(donor) in seen["cmd"]


def test_the_donor_is_passed_for_every_repo_not_just_the_broken_ones(cd, monkeypatch,
                                                                    tmp_path):
    """Branching on which repos are local would mean carrying a copy of
    agentctl's LOCAL_AGENT_REPOS here, to drift against the real one. One
    resolution path fleet-wide instead."""
    donor = tmp_path / "Convivy" / "vivy" / ".claude" / "agents"
    donor.mkdir(parents=True)
    (donor / "explainer.md").write_text("---\nname: explainer\n---\nbody")
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")

    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", "spawned run-abc123  pid=1 agent=explainer"

    monkeypatch.setattr(cd.subprocess, "run",
                        lambda cmd, *a, **k: (seen.update(cmd=cmd), _Done())[1])
    cd._spawn("vivy", "1050", "a task")      # an index-resolving repo
    assert "--agents-dir" in seen["cmd"]


def test_a_missing_donor_does_not_take_the_drain_down(cd, monkeypatch, tmp_path):
    """A checkout that is absent or mid-rebase must degrade to the old behaviour,
    not crash the pass."""
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    assert cd._agents_dir() is None

    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", "spawned run-abc123  pid=1 agent=explainer"

    monkeypatch.setattr(cd.subprocess, "run",
                        lambda cmd, *a, **k: (seen.update(cmd=cmd), _Done())[1])
    assert cd._spawn("vivy", "1050", "a task") == "run-abc123"
    assert "--agents-dir" not in seen["cmd"]


def test_the_first_existing_donor_wins(cd, monkeypatch, tmp_path):
    second = tmp_path / "Convivy" / "constance" / ".claude" / "agents"
    second.mkdir(parents=True)
    (second / "explainer.md").write_text("---\nname: explainer\n---\nbody")
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    # vivy (first in the list) has no checkout here, so constance is chosen.
    assert cd._agents_dir() == second


def _agent_shape(path):
    """The semantically load-bearing parts of an agent definition: its tool set
    and model. Raw bytes will not do — individuation reshapes the frontmatter
    into Claude Code's name/description/tools form, so the donor and the indexed
    source differ textually while meaning the same thing."""
    import re as _re
    text = path.read_text(encoding="utf-8")
    fm = text.split("---", 2)[1] if text.startswith("---") else ""
    tools = _re.search(r"^tools:\s*\[?(.*?)\]?\s*^(?=\w+:)", fm, _re.S | _re.M)
    model = _re.search(r"^model:\s*(\S+)", fm, _re.M)
    # Collapse internal whitespace, not just newlines: the donor's tools line is
    # long enough that YAML folds it mid-token, so `Bash(agentctl\n  scribe
    # apply:*)` would otherwise read as a different tool from the canonical's
    # single-line form. That folding is formatting, not drift.
    tool_set = frozenset(
        _re.sub(r"\s+", " ", t).strip()
        for t in (tools.group(1) if tools else "").split(",")
        if t.strip())
    return tool_set, (model.group(1) if model else None)


@pytest.mark.needs_kb
def test_no_donor_has_drifted_from_the_indexed_explainer(cd):
    """The donors govern what every changelog explainer runs across the whole
    fleet, so a PR touching vivy's explainer.md is a fleet-wide policy change
    reviewed only against vivy's bar — the cross-repo effect is invisible from
    inside that repo (security review, #252). This converts that implicit social
    contract into a failing test.

    `needs_kb` carries the skip, which is the CI case; it is a local and on-host
    guard, and the alternative — no check at all — was the finding.

    THE CANONICAL PATH IS A KB PATH, and this guard read the pre-re-file
    `projects/shared/agents/explainer.md` until PR G. The file simply stopped
    existing when ARCH-426 step 5 moved the roster, so the `is_file()` test above
    skipped and the fleet-wide drift guard reported nothing at all — for the same
    reason, and in the same direction, as every other break this pass found. The
    assertion below now fails rather than skips when the canonical file is
    missing, because "the roster moved again" and "there is no KB here" are
    different facts and only the marker may decide the second.
    """
    from conftest import knowledge_repo

    home = cd.pathlib.Path.home()
    canonical = knowledge_repo() / "projects" / "agent-platform" / "agents" / "explainer.md"
    assert canonical.is_file(), (
        f"the indexed explainer is not at {canonical}. The roster has moved "
        f"again; this guard silently stops checking anything until it follows.")
    want = _agent_shape(canonical)
    checked = 0
    for repo in cd._EXPLAINER_DONORS:
        donor = home / "Convivy" / repo / ".claude" / "agents" / "explainer.md"
        if not donor.is_file():
            continue
        checked += 1
        assert _agent_shape(donor) == want, (
            f"{repo}'s individuated explainer has drifted from the indexed source. "
            f"It governs changelog-drain dispatches for EVERY repo, so this is a "
            f"fleet-wide change — re-individuate it rather than editing it in place.")
    if not checked:
        pytest.skip("no donor checkout present")


def test_the_task_forbids_the_scribe_tools_the_shared_definition_carries(cd):
    """The shared explainer carries agentctl scribe permissions for its other
    invocations. Writing a changelog entry needs none, and until --agents-dir
    landed these dispatches never ran in the repo-local repos at all — so the
    reachable surface grew (security review, #252). The definition is shared and
    not this caller's to narrow; the brief is."""
    task = cd._task_for(_merge(42), "42")
    assert "agentctl scribe" in task and "Do NOT run" in task
    assert "read-and-summarize only" in task


def test_the_spawn_never_passes_pr(cd, monkeypatch):
    """`agentctl spawn --pr N` pins a worktree to the PR head and REFUSES a
    closed or merged one. Every PR this command touches is merged by definition,
    so passing it fails 100% of the time — which is exactly what happened on the
    first live run (exit 2, "refusing to dispatch against a closed/merged PR").
    The explainer reads the merge from main instead."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, "", "spawned run-abc123 (detached)  agent=explainer"

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    assert cd._spawn("vivy", "1050", "a task") == "run-abc123"
    assert "--pr" not in seen["cmd"]
    assert "--on" in seen["cmd"] and "vivy" in seen["cmd"]
    assert "--foreground" in seen["cmd"]


def test_final_output_reads_the_last_result_line(cd, monkeypatch, tmp_path):
    run = tmp_path / "2026-07-26" / "run-abc123"
    run.mkdir(parents=True)
    (run / "transcript.jsonl").write_text(
        '{"type":"assistant","message":{}}\n'
        '{"type":"result","result":"first"}\n'
        '{"type":"result","result":"**Merged:** the real entry. #9"}\n')
    monkeypatch.setattr(cd, "RUNS_ROOT", tmp_path)
    assert cd._final_output("run-abc123") == "**Merged:** the real entry. #9"


def test_final_output_is_none_when_the_run_produced_nothing(cd, monkeypatch, tmp_path):
    run = tmp_path / "2026-07-26" / "run-abc123"
    run.mkdir(parents=True)
    (run / "transcript.jsonl").write_text('{"type":"result","result":"   "}\n')
    monkeypatch.setattr(cd, "RUNS_ROOT", tmp_path)
    assert cd._final_output("run-abc123") is None


def test_final_output_rejects_a_traversing_run_id(cd, monkeypatch, tmp_path):
    """The run-id reaches a path join; a value carrying separators must never
    walk outside the runs root."""
    monkeypatch.setattr(cd, "RUNS_ROOT", tmp_path)
    assert cd._final_output("../../etc/passwd") is None
    assert cd._final_output("run-abc/../..") is None


def test_final_output_refuses_a_run_id_planted_under_two_date_dirs(cd, monkeypatch, tmp_path):
    """A run-id resolved under two date directories (a planted duplicate,
    issue_01M1JWB7M75SZ160VRJRCGARBF) must not let the newer-sorting directory's
    transcript silently win and supply the posted changelog entry."""
    real = tmp_path / "2026-07-26" / "run-facade1"
    real.mkdir(parents=True)
    (real / "transcript.jsonl").write_text('{"type":"result","result":"real entry. #9"}\n')
    planted = tmp_path / "2099-01-01" / "run-facade1"
    planted.mkdir(parents=True)
    (planted / "transcript.jsonl").write_text('{"type":"result","result":"forged entry. #9"}\n')
    monkeypatch.setattr(cd, "RUNS_ROOT", tmp_path)
    assert cd._final_output("run-facade1") is None


def test_a_run_that_returns_nothing_posts_nothing(cd, monkeypatch, tmp_path):
    """An empty entry must never reach the site — the failure is a reported
    failure, not a blank changelog row."""
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    (tmp_path / "Convivy" / "vivy" / ".git").mkdir(parents=True)
    _stub_reads(monkeypatch, cd, [_merge(5)], [])
    monkeypatch.setattr(cd, "_spawn", lambda repo, pr, task: "run-deadbee")
    monkeypatch.setattr(cd, "_final_output", lambda run_id: None)
    posted = []
    monkeypatch.setattr(cd, "_post", lambda r, p, e: posted.append((r, p, e)) or True)

    rc = cd.main(["--since", "2026-07-26T00:00:00Z"])
    assert posted == []
    assert rc == 1          # a failed pass exits non-zero


def test_a_dry_run_spawns_nothing(cd, monkeypatch, tmp_path):
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    (tmp_path / "Convivy" / "vivy" / ".git").mkdir(parents=True)
    _stub_reads(monkeypatch, cd, [_merge(5)], [])

    def boom(*a, **k):
        raise AssertionError("--dry-run must not spawn")

    monkeypatch.setattr(cd, "_spawn", boom)
    assert cd.main(["--since", "2026-07-26T00:00:00Z", "--dry-run"]) == 0


def test_max_dispatch_bounds_one_pass(cd, monkeypatch, tmp_path):
    """The cost guard. ~53 merges/day fleet-wide, so an unbounded backfill or a
    burst could spawn far more agents than a pass should."""
    monkeypatch.setattr(cd.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    # Checkouts resolve from builder_root.root(), which never reads Path.home().
    monkeypatch.setattr(cd.BR, "root", lambda *a, **k: tmp_path / "Convivy")
    (tmp_path / "Convivy" / "vivy" / ".git").mkdir(parents=True)
    _stub_reads(monkeypatch, cd, [_merge(i) for i in range(1, 11)], [])
    spawned = []
    monkeypatch.setattr(cd, "_spawn",
                        lambda repo, pr, task: spawned.append(pr) or "run-abc123")
    monkeypatch.setattr(cd, "_final_output", lambda run_id: "**Merged:** x. #1")
    monkeypatch.setattr(cd, "_post", lambda r, p, e: True)

    cd.main(["--since", "2026-07-26T00:00:00Z", "--max-dispatch", "3"])
    assert len(spawned) == 3
    assert spawned == ["1", "2", "3"]      # oldest first, not an arbitrary slice
