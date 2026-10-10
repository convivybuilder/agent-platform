"""`issue-pr-reconcile` — closing a `pr-open` issue once its fix PR merges.

The properties worth pinning are the ones that decide whether an unwatched sweep can
be trusted to mutate the queue. Resolving the wrong issue is worse than resolving
none, so most of these tests are about what it REFUSES to do: it never derives a repo
from `product`, it never treats a `gh` failure as a verdict, and it never closes an
issue whose PR was abandoned rather than merged.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def rec(issue_pr_reconcile):
    return issue_pr_reconcile


def _issue(iid="issue_1", product="selector", **kw):
    out = {"id": iid, "product": product, "title": "a title"}
    out.update(kw)
    return out


# --- repo resolution: the one path that could silently corrupt the queue ---------

def test_prefers_server_derived_pr_repo(rec):
    """`pr_repo` is org-pinned by the server; `pr_url` is stored as supplied. When
    both are present the derived one wins, so a crafted url cannot redirect the check."""
    ref = rec.pr_ref(_issue(pr_repo="convivy/selector-backend", pr=13,
                            pr_url="https://github.com/convivy/selector/pull/13"))
    assert ref == ("convivy/selector-backend", "13")


def test_falls_back_to_pr_url_when_repo_absent(rec):
    assert rec.pr_ref(_issue(pr_url="https://github.com/convivy/vivy/pull/1095")) == \
        ("convivy/vivy", "1095")


def test_reads_the_loopback_refs_shape(rec):
    """The loopback `/api/issues` returns the stored `refs` dict; the MCP tool
    flattens the same values to the top level. Reading only the flattened shape made
    every real issue look unresolvable while the sweep reported a clean pass — a
    silent no-op that no test using the flattened shape could catch."""
    issue = _issue(refs={"url": "https://github.com/convivy/sous-chef/pull/417",
                         "pr": "417", "repo": "convivy/sous-chef"})
    assert rec.pr_ref(issue) == ("convivy/sous-chef", "417")


def test_refs_url_alone_resolves(rec):
    assert rec.pr_ref(_issue(refs={"url": "https://github.com/convivy/vivy/pull/9"})) == \
        ("convivy/vivy", "9")


def test_refs_repo_is_still_org_pinned(rec):
    assert rec.pr_ref(_issue(refs={"repo": "evil/x", "pr": "1"})) is None


def test_a_non_dict_refs_does_not_crash_the_sweep(rec):
    assert rec.pr_ref(_issue(refs="nonsense")) is None
    assert rec.pr_ref(_issue(refs=None)) is None


def test_never_derives_repo_from_product(rec):
    """The failure this tool must not have. `selector` spans convivy/selector AND
    convivy/selector-backend, so product+pr resolves to the wrong repo and would mark
    live work fixed against a coincidentally-numbered PR. Absent both references, the
    answer is None — not a guess from `product`."""
    assert rec.pr_ref(_issue(product="selector", pr=13)) is None
    assert rec.pr_ref(_issue(product="selector")) is None


@pytest.mark.parametrize("url", [
    "https://github.com/evil/selector/pull/1",       # out of org
    "https://github.com/convivy/selector/pull/abc",  # not a number
    "https://github.com/convivy/selector/issues/1",  # not a PR
    "http://github.com/convivy/selector/pull/1",     # not https
    "https://github.com.evil.test/convivy/x/pull/1",  # host suffix
    "",
])
def test_rejects_a_pr_url_that_is_not_a_convivy_pr(rec, url):
    assert rec.pr_ref(_issue(pr_url=url)) is None


def test_rejects_an_out_of_org_pr_repo(rec):
    assert rec.pr_ref(_issue(pr_repo="evil/selector", pr=1)) is None


@pytest.mark.parametrize("repo", ["convivy/-selector", "convivy/-", "convivy/"])
def test_rejects_a_repo_name_that_could_read_as_a_flag(rec, repo):
    """A repo name starting with a hyphen has no business being built into an argv,
    whether or not any current caller could produce one."""
    assert rec.pr_ref(_issue(pr_repo=repo, pr=1)) is None
    assert rec.pr_ref(_issue(pr_url=f"https://github.com/{repo}/pull/1")) is None


# --- gh failure is not a verdict -------------------------------------------------

def test_gh_failure_reads_as_unknown_not_as_unmerged(rec, monkeypatch):
    """A non-zero `gh` must not read as "not merged" (silently nothing, forever) nor
    as "merged" (closes live issues). It reads as None and the caller reports it."""
    class _R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: _R())
    assert rec.pr_state("convivy/vivy", "1") is None


def test_unparseable_gh_output_reads_as_unknown(rec, monkeypatch):
    class _R:
        returncode = 0
        stdout = "not json"
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: _R())
    assert rec.pr_state("convivy/vivy", "1") is None


def test_gh_timeout_reads_as_unknown(rec, monkeypatch):
    def _boom(*a, **k):
        raise OSError("gh missing")
    monkeypatch.setattr(rec.subprocess, "run", _boom)
    assert rec.pr_state("convivy/vivy", "1") is None


def test_gh_is_resolved_absolutely_for_launchd(rec):
    """`safe_which` resolves `gh` from a fixed list of system directories
    (platform/decisions/0090), never a hardcoded account-specific guess."""
    assert rec.GH_BIN.startswith("/")


def test_gh_bin_env_override_wins(monkeypatch):
    """GH_BIN the environment variable (how tests pin it) beats `safe_which`."""
    from conftest import _load_cli
    monkeypatch.setenv("GH_BIN", "/custom/gh")
    monkeypatch.setattr("safe_path.safe_which", lambda name: "/opt/homebrew/bin/gh")
    assert _load_cli("issue-pr-reconcile").GH_BIN == "/custom/gh"


def test_gh_bin_falls_back_to_safe_which_without_override(monkeypatch):
    """Resolved from wherever `safe_which` actually finds gh, not a hardcoded
    guess — /usr/local/bin here specifically, to tell this apart from a stale
    /opt/homebrew/bin default that would answer the same by coincidence."""
    from conftest import _load_cli
    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setattr("safe_path.safe_which", lambda name: "/usr/local/bin/gh")
    assert _load_cli("issue-pr-reconcile").GH_BIN == "/usr/local/bin/gh"


def test_gh_bin_is_none_with_no_path_and_no_override(monkeypatch):
    """Neither GH_BIN nor PATH resolves gh: GH_BIN is None rather than a bare
    "gh" — a bare name would hand `subprocess.run` the unfiltered PATH to
    search, reopening exactly the gap `safe_which` exists to close. `main()`
    fails closed on this (see test_main_fails_closed_when_gh_bin_is_none),
    rather than ever reaching a `gh` subprocess call built from it."""
    from conftest import _load_cli
    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setattr("safe_path.safe_which", lambda name: None)
    assert _load_cli("issue-pr-reconcile").GH_BIN is None


def test_main_fails_closed_when_gh_bin_is_none(monkeypatch, tmp_path):
    """main() must refuse to run rather than let a later bare "gh" call reach
    subprocess's own unfiltered PATH search."""
    from conftest import _load_cli
    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setattr("safe_path.safe_which", lambda name: None)
    mod = _load_cli("issue-pr-reconcile")
    assert mod.GH_BIN is None
    monkeypatch.setattr(mod.sys, "argv", ["issue-pr-reconcile"])

    def _boom(*a, **k):
        raise AssertionError("must not reach the issue queue with no safe gh")

    monkeypatch.setattr(mod.ic, "list_issues_by_state", _boom)
    assert mod.main() == 1


def test_gh_bin_a_planted_gh_under_a_fake_home_is_not_chosen(monkeypatch, tmp_path):
    """The real end-to-end exercise of `safe_which`'s plant protection, with
    no mock: a planted `gh` under a fake HOME, ahead on PATH, must not become
    GH_BIN — `safe_which` never reads PATH or HOME, so the real gh on this
    machine must be the one resolved regardless (platform/decisions/0090's
    Handoff). No literal path: this machine's real answer is computed before
    HOME/PATH are poisoned, not hardcoded — the literal `/opt/homebrew/bin/gh`
    this test used before round 13 is wrong on the GitHub Actions macOS
    runner, which puts `gh` somewhere else."""
    import safe_path as SP
    from conftest import _load_cli
    baseline = SP.safe_which("gh")
    if baseline is None:
        pytest.skip("no real gh on this machine's fixed system list")

    fake_home = tmp_path / "fake-home"
    planted_dir = fake_home / ".local" / "bin"
    planted_dir.mkdir(parents=True)
    (planted_dir / "gh").write_text("#!/bin/sh\n")
    (planted_dir / "gh").chmod(0o755)

    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(planted_dir))
    got = _load_cli("issue-pr-reconcile").GH_BIN
    assert got is not None and not got.startswith(str(fake_home)), \
        f"the planted gh under the fake HOME was chosen: {got}"
    assert got == baseline


# --- the sweep ------------------------------------------------------------------

def _run(rec, monkeypatch, issues, states, argv=("issue-pr-reconcile",),
        reconcile_status="ok"):
    """Drive main() over synthetic issues; `states` maps "repo#num" → gh state.

    `reconcile_status` is the status every stubbed `ic.reconcile_pr` call
    returns, so a test can drive a write failure through it without touching
    every other case."""
    resolved = []
    reconciled = []
    alerts = []
    monkeypatch.setattr(rec.ic, "list_issues_by_state", lambda *a, **k: issues)
    monkeypatch.setattr(rec, "pr_state", lambda repo, num: states.get(f"{repo}#{num}"))
    monkeypatch.setattr(rec.ic, "resolve_issue",
                        lambda iid, res, **kw: resolved.append((iid, res, kw)) or {"status": "ok"})
    monkeypatch.setattr(rec.ic, "reconcile_pr",
                        lambda iid, outcome, **kw: reconciled.append((iid, outcome, kw))
                        or {"status": reconcile_status})
    monkeypatch.setattr(rec, "post_alert",
                        lambda stream, body, **kw: alerts.append((body, kw.get("dedup_key"))))
    monkeypatch.setattr(rec.sys, "argv", list(argv))
    rc = rec.main()
    return rc, resolved, reconciled, alerts


def test_merged_pr_resolves_its_issue_fixed(rec, monkeypatch):
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1095)]
    rc, resolved, reconciled, _ = _run(rec, monkeypatch, issues, {"convivy/vivy#1095": "MERGED"})
    assert rc == 0
    assert len(resolved) == 1
    iid, resolution, kw = resolved[0]
    assert (iid, resolution) == ("issue_1", "fixed")
    assert kw["fix_pr"] == "https://github.com/convivy/vivy/pull/1095"
    assert reconciled == [], (
        "a plain merged PR resolves through issue_resolve, not reconcile-pr — that "
        "route is for the three cases this sweep used to only report")


def test_open_pr_calls_reconcile_pr_abandoned_and_moves_nothing(rec, monkeypatch):
    """An open PR is indistinguishable from one still being worked, so the sweep must
    not move the item itself. It reports the new Library route
    (convivy/library#116) instead, which sets `needs: operator` and nothing else —
    this test pins that the SWEEP still resolves and alerts nothing on its own."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "OPEN"})
    assert resolved == []
    assert alerts == []
    assert reconciled == [("issue_1", "abandoned", {})]


def test_abandoned_pr_is_reported_never_resolved(rec, monkeypatch):
    """A PR closed without merging means the fix was abandoned, so the issue is live
    work again. Closing it would bury real work; reopening it would guess at why it
    was abandoned. It alerts instead, and calls reconcile-pr("closed") so the Library
    returns the item to `open` rather than leaving it stuck in `pr-open` forever."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "CLOSED"})
    assert resolved == []
    assert len(alerts) == 1
    assert "closed without merging" in alerts[0][0]
    assert reconciled == [("issue_1", "closed", {})]


def test_an_epic_with_a_merged_pr_is_never_resolved(rec, monkeypatch):
    """An epic ships in slices, so the first slice to merge must not close it.

    `issue_link_pr` is the only way to record which PR is doing the work, so an agent
    shipping step 1 of 5 links it to the epic itself — and this sweep read that as
    done. Caught live on 2026-08-25 against a five-step epic that was the only
    `pr-open` item in the queue, so that night's pass would have resolved exactly one
    issue and been wrong about it.

    The false close is the expensive direction: nothing looks at a terminal state
    again, so the remaining four steps would have gone silently.

    It now also calls reconcile-pr("merged"), which unsticks the epic back to `open`
    (never resolves it) so the epic is claimable again rather than fenced forever.
    """
    issues = [_issue("issue_epic", pr_repo="convivy/agent-platform", pr=445, type="epic")]
    rc, resolved, reconciled, alerts = _run(rec, monkeypatch, issues,
                                            {"convivy/agent-platform#445": "MERGED"})
    assert rc == 0
    assert resolved == [], "an epic must never be closed on one merged PR"
    assert len(alerts) == 1
    assert "epic" in alerts[0][0].lower()
    assert "definition_of_done" in alerts[0][0], (
        "the alert has to say what the human should check, not merely that it stopped")
    assert reconciled == [("issue_epic", "merged", {})]


def test_the_epic_hold_fires_on_the_raw_loopback_shape(rec, monkeypatch):
    """`type` must survive the shape that `pr_ref` had to be patched for.

    This file already learned once that the two clients disagree: the loopback
    `/api/issues` nests PR linkage under `refs` while the MCP tool flattens it, and
    reading only the flattened shape made every issue look unresolvable while the
    sweep reported a clean pass. If `type` were ever absent specifically on the
    loopback shape, the epic hold would silently do nothing and reproduce the exact
    false close it exists to prevent — with no signal that it happened.

    So this drives the hold through an issue whose PR reference arrives ONLY as a
    `refs` dict, the way the loopback client actually returns it.
    """
    issue = _issue("issue_epic", type="epic",
                   refs={"url": "https://github.com/convivy/agent-platform/pull/445",
                         "pr": "445", "repo": "convivy/agent-platform"})
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, [issue],
                                           {"convivy/agent-platform#445": "MERGED"})
    assert resolved == [], "the hold must fire on the loopback shape, not only the flat one"
    assert len(alerts) == 1
    assert "definition_of_done" in alerts[0][0]
    assert reconciled == [("issue_epic", "merged", {})]


def test_the_epic_alert_is_deduped_on_the_epic_and_its_pr(rec, monkeypatch):
    """A held epic is an EXPECTED, long-lived state, not an exception.

    It sits between slices for days or weeks, and the combined alert carries no
    suppression — so folding epics into it would repost the same body every night
    until a human acted. The file's own comment says that trains the alerts page to
    be ignored, which costs the alerts that matter, and this is not one to ignore.

    Keyed on the epic AND the merged PR, so it fires on the TRANSITION and again
    when the next slice's PR merges, but not on the nights in between.
    """
    issues = [_issue("issue_epic", pr_repo="convivy/agent-platform", pr=445, type="epic")]
    _, _, _, alerts = _run(rec, monkeypatch, issues,
                           {"convivy/agent-platform#445": "MERGED"})
    assert len(alerts) == 1
    assert alerts[0][1] == "reconcile-epic-held:issue_epic:convivy/agent-platform#445"

    # the next slice is a different key, so it alerts again rather than staying mute
    issues = [_issue("issue_epic", pr_repo="convivy/agent-platform", pr=460, type="epic")]
    _, _, _, alerts2 = _run(rec, monkeypatch, issues,
                            {"convivy/agent-platform#460": "MERGED"})
    assert alerts2[0][1] != alerts[0][1]


def test_an_exception_state_alert_is_not_deduped(rec, monkeypatch):
    """The epic dedup must not leak onto the states that SHOULD renag.

    An abandoned PR or a failed write is an exception nobody has dealt with yet;
    those repeat deliberately until someone does.
    """
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "CLOSED"})
    assert len(alerts) == 1
    assert alerts[0][1] is None, "the exception alert must carry no dedup key"


@pytest.mark.parametrize("state,expect_alert", [("OPEN", False), ("CLOSED", True)])
def test_an_epic_whose_pr_did_not_merge_takes_the_ordinary_path(
        rec, monkeypatch, state, expect_alert):
    """The hold is reachable only after MERGED, and this pins that against a refactor
    moving the check earlier. An epic with an open PR is simply still in flight; an
    epic whose PR was closed unmerged is abandoned work and must still be reported."""
    issues = [_issue("issue_epic", pr_repo="convivy/vivy", pr=1, type="epic")]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": state})
    assert resolved == []
    assert bool(alerts) is expect_alert
    if expect_alert:
        assert "closed without merging" in alerts[0][0]
        assert "definition_of_done" not in alerts[0][0], (
            "an unmerged epic is abandoned work, not a held one — the two must not "
            "report the same way")
        assert reconciled == [("issue_epic", "closed", {})]
    else:
        assert reconciled == [("issue_epic", "abandoned", {})], (
            "an epic type must not change which outcome an OPEN PR reports")


# --- a blocked item's merged PR must not resolve it -----------------------------

def test_a_blocked_item_with_a_merged_pr_is_never_resolved(rec, monkeypatch):
    """A merged PR does not finish an item whose prerequisites are unmet. Caught
    live on 2026-10-07: `issue_01M3BFJFKDCYPP4417RGZC75FJ` was resolved fixed
    because its PR merged while it read `blocked: true` and its prerequisite — a
    route that still had not been built — was unmet. The false close hid the
    missing route from everyone until Deck investigated."""
    issues = [_issue("issue_1", pr_repo="convivy/convivy-deck", pr=326, blocked=True,
                     prerequisites=[{"text": "run register's docs_given route built"}])]
    rc, resolved, reconciled, alerts = _run(rec, monkeypatch, issues,
                                            {"convivy/convivy-deck#326": "MERGED"})
    assert rc == 0
    assert resolved == [], "a blocked item must never be closed on its merged PR"
    assert reconciled == [("issue_1", "merged", {})]
    assert len(alerts) == 1
    assert "prerequisites" in alerts[0][0].lower()
    assert "docs_given route built" in alerts[0][0]


def test_a_blocked_merged_item_appears_in_its_own_report_section(rec, monkeypatch, capsys):
    issues = [_issue("issue_1", pr_repo="convivy/convivy-deck", pr=326, blocked=True,
                     prerequisites=[{"text": "run register's docs_given route built"}])]
    _run(rec, monkeypatch, issues, {"convivy/convivy-deck#326": "MERGED"})
    out = capsys.readouterr().out
    assert "blocked item with a merged PR" in out
    assert "docs_given route built" in out


def test_a_blocked_merged_items_prerequisite_text_is_sanitised(rec, monkeypatch):
    """`prerequisites[].text` is free text an agent or human authored, not a
    platform-derived value, so it gets the same treatment as a title."""
    hostile = "fix it\x1b[31mRED\x1b[0m sk-abcdefghijklmnop"
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True,
                     prerequisites=[{"text": hostile}])]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"})
    assert len(alerts) == 1
    assert "\x1b" not in alerts[0][0]
    assert "sk-abcdefghijklmnop" not in alerts[0][0]
    assert "sk-[redacted]" in alerts[0][0]


def test_a_blocked_merged_item_with_an_item_prerequisite_names_it(rec, monkeypatch):
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True,
                     prerequisites=[{"item": "issue_other"}])]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"})
    assert len(alerts) == 1
    assert "issue_other" in alerts[0][0]


def test_a_blocked_merged_items_item_prerequisite_is_sanitised_and_capped(
        rec, monkeypatch, capsys):
    """`prerequisites[].item` is written by whoever filed or corrected the item,
    not a platform-derived value, same as `text` — it gets the same sanitise-
    and-cap treatment, in both the report and the alert."""
    # The long tail is space-separated so no single run hits the 32-char
    # credential-redaction threshold — it must be the 70-char cap, not
    # `_redact_long_run`, that truncates it.
    hostile = "fix it\x1b[31mRED\x1b[0m sk-abcdefghijklmnop " + "lorem " * 20
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True,
                     prerequisites=[{"item": hostile}])]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"})
    out = capsys.readouterr().out
    assert len(alerts) == 1
    for text in (alerts[0][0], out):
        assert "\x1b" not in text
        assert "sk-abcdefghijklmnop" not in text
        assert "sk-[redacted]" in text
        assert "lorem " * 20 not in text, "the 70-char cap must have truncated this"
        assert "..." in text


def test_a_blocked_merged_item_with_no_prerequisites_recorded_says_so(rec, monkeypatch):
    """`blocked` can be true with an empty or absent `prerequisites` list (an older
    Library deploy, or a race on the write) — the report must say so rather than
    rendering an empty string that reads as though nothing is wrong."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True)]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"})
    assert len(alerts) == 1
    assert "(none recorded)" in alerts[0][0]


def test_an_epic_that_is_also_blocked_takes_the_epic_path(rec, monkeypatch):
    """The epic check runs first: an epic's per-slice hold is a separate judgment
    from `blocked`, and the two reports must not both fire for one item."""
    issues = [_issue("issue_epic", pr_repo="convivy/agent-platform", pr=445, type="epic",
                     blocked=True, prerequisites=[{"text": "something"}])]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues,
                                           {"convivy/agent-platform#445": "MERGED"})
    assert resolved == []
    assert reconciled == [("issue_epic", "merged", {})]
    assert len(alerts) == 1
    assert "definition_of_done" in alerts[0][0], "the epic path must be the one that fires"
    assert "prerequisites are unmet" not in alerts[0][0]


def test_a_blocked_merged_alert_is_deduped_on_the_item_and_its_pr(rec, monkeypatch):
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True,
                     prerequisites=[{"text": "x"}])]
    _, _, _, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"})
    assert alerts[0][1] == "reconcile-blocked-merged-held:issue_1:convivy/vivy#1"

    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=2, blocked=True,
                     prerequisites=[{"text": "x"}])]
    _, _, _, alerts2 = _run(rec, monkeypatch, issues, {"convivy/vivy#2": "MERGED"})
    assert alerts2[0][1] != alerts[0][1], "a new merged PR is a new transition"


def test_an_unblocked_item_with_a_merged_pr_still_resolves(rec, monkeypatch):
    """The hold is scoped to `blocked` items — an ordinary item, with `blocked`
    false or absent, keeps closing on its merged PR exactly as before."""
    for blocked_value in (False, None):
        issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=7, blocked=blocked_value)]
        _, resolved, reconciled, _ = _run(rec, monkeypatch, issues, {"convivy/vivy#7": "MERGED"})
        assert len(resolved) == 1, f"blocked={blocked_value!r} must still resolve"
        assert reconciled == [], f"blocked={blocked_value!r} must not call reconcile-pr"


def test_dry_run_writes_nothing_for_the_blocked_case(rec, monkeypatch):
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, blocked=True,
                     prerequisites=[{"text": "x"}])]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"},
                                           argv=("issue-pr-reconcile", "--dry-run"))
    assert resolved == []
    assert reconciled == []
    assert alerts == []


def test_a_non_epic_with_a_merged_pr_still_resolves(rec, monkeypatch):
    """The hold is scoped to epics. Every other type keeps closing on its merged PR —
    that is the whole job, and a guard that quietly stopped doing it would recreate
    the 39-stale-issues backlog this tool was written to end."""
    for kind in ("bug", "task", "improvement", "feature", "nit", None):
        issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=7, type=kind)]
        _, resolved, reconciled, _ = _run(rec, monkeypatch, issues, {"convivy/vivy#7": "MERGED"})
        assert len(resolved) == 1, f"type={kind!r} must still resolve"
        assert reconciled == [], f"type={kind!r} must not call reconcile-pr"


def test_unresolvable_reference_is_skipped_and_alerted(rec, monkeypatch):
    issues = [_issue("issue_1", product="selector", pr=13)]  # no repo, no url
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {})
    assert resolved == []
    assert reconciled == []
    assert len(alerts) == 1
    assert "cannot be resolved" in alerts[0][0]


def test_a_hostile_title_is_sanitised_before_it_reaches_any_output(
        rec, monkeypatch, capsys):
    """An issue title is free text authored by agents, not a platform-authored
    value, so it gets the same treatment as any other API response body. An ANSI
    escape or credential in a title must not survive to the console rows or the
    alert."""
    title = "close\x1b[31mRED\x1b[0m sk-abcdefghijklmnop"
    issues = [_issue("issue_1", product="selector", title=title)]  # no ref → unresolvable
    _, resolved, _, alerts = _run(rec, monkeypatch, issues, {})
    assert resolved == []
    assert len(alerts) == 1
    assert "\x1b" not in alerts[0][0]
    assert "sk-abcdefghijklmnop" not in alerts[0][0]
    assert "sk-[redacted]" in alerts[0][0]
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "sk-abcdefghijklmnop" not in out


def test_a_truncate_first_order_cannot_bisect_a_credential(rec, monkeypatch, capsys):
    """The length cap runs inside sanitise, so a credential near the boundary is
    redacted whole.

    A truncate-first order would cut this value to a 4-char fragment — too short
    for _CRED_PREFIX_RE's \\S{8,} or _LONG_RUN_RE's 32-char minimum — and the
    fragment would survive to the console rows and the alert.
    """
    # sk- sits at index 63, so a 70-char truncate-first cut leaves only "zzzz"
    # of the value, which both redaction regexes miss.
    title = "-" * 62 + " sk-" + "z" * 40
    issues = [_issue("issue_1", product="selector", title=title)]  # no ref → unresolvable
    _, resolved, _, alerts = _run(rec, monkeypatch, issues, {})
    assert resolved == []
    assert len(alerts) == 1
    assert "sk-[red" in alerts[0][0]  # redaction ran; the cap may trim the marker
    assert "sk-zzzz" not in alerts[0][0]
    assert "z" not in alerts[0][0]
    out = capsys.readouterr().out
    assert "sk-[red" in out
    assert "sk-zzzz" not in out
    assert "z" not in out


def test_a_routine_sweep_stays_quiet(rec, monkeypatch):
    """Every night resolving merged PRs is the normal path. Alerting on it would train
    the alerts page to be ignored, which costs the alerts that do matter."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1),
              _issue("issue_2", pr_repo="convivy/vivy", pr=2)]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues,
                                           {"convivy/vivy#1": "MERGED", "convivy/vivy#2": "OPEN"})
    assert len(resolved) == 1
    assert alerts == []
    assert reconciled == [("issue_2", "abandoned", {})]


def test_dry_run_writes_nothing(rec, monkeypatch):
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    _, resolved, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": "MERGED"},
                                           argv=("issue-pr-reconcile", "--dry-run"))
    assert resolved == []
    assert reconciled == []
    assert alerts == []


@pytest.mark.parametrize("state,issue_type,expected_outcome", [
    ("OPEN", None, "abandoned"),
    ("CLOSED", None, "closed"),
    ("MERGED", "epic", "merged"),
])
def test_dry_run_never_calls_reconcile_pr(rec, monkeypatch, state, issue_type, expected_outcome):
    """The three new reconcile-pr call sites must respect --dry-run exactly like the
    ordinary resolve write does — this pins each of them individually rather than
    trusting one MERGED/non-epic case to stand in for all four branches."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, type=issue_type)]
    _, _, reconciled, _ = _run(rec, monkeypatch, issues, {"convivy/vivy#1": state},
                               argv=("issue-pr-reconcile", "--dry-run"))
    assert reconciled == [], f"outcome={expected_outcome!r} must not write during --dry-run"


@pytest.mark.parametrize("state,issue_type,expected_outcome", [
    ("OPEN", None, "abandoned"),
    ("CLOSED", None, "closed"),
    ("MERGED", "epic", "merged"),
])
def test_a_failed_reconcile_write_is_reported_not_swallowed(
        rec, monkeypatch, state, issue_type, expected_outcome):
    """A non-2xx from the Library's reconcile-pr route must surface, the same
    contract `write_failed` already holds for a failed `resolve_issue` call — a
    write that silently failed leaves the queue knowably wrong with nothing saying
    so."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1, type=issue_type)]
    _, _, reconciled, alerts = _run(rec, monkeypatch, issues, {"convivy/vivy#1": state},
                                    reconcile_status="error:500")
    assert reconciled == [("issue_1", expected_outcome, {})]
    reconcile_alerts = [a for a in alerts if "reconcile-pr write failed" in a[0]]
    assert len(reconcile_alerts) == 1, alerts


def test_unreadable_queue_exits_nonzero(rec, monkeypatch):
    """An unreachable queue is a failure, not an empty queue. Exiting 0 here is the
    ARCH-311 silent no-op — the class of bug this tool exists to end."""
    def _boom(*a, **k):
        raise rec.ic.IssueQueueUnavailable("down")
    monkeypatch.setattr(rec.ic, "list_issues_by_state", _boom)
    monkeypatch.setattr(rec.sys, "argv", ["issue-pr-reconcile"])
    assert rec.main() == 1


def test_a_failed_resolve_does_not_count_as_resolved(rec, monkeypatch):
    """A write that failed must not be reported as a close, or the summary asserts a
    queue state that never happened."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    alerts = []
    monkeypatch.setattr(rec.ic, "list_issues_by_state", lambda *a, **k: issues)
    monkeypatch.setattr(rec, "pr_state", lambda repo, num: "MERGED")
    monkeypatch.setattr(rec.ic, "resolve_issue", lambda *a, **k: {"status": "error:500"})
    monkeypatch.setattr(rec, "post_alert",
                        lambda stream, body, **kw: alerts.append((body, kw.get("dedup_key"))))
    monkeypatch.setattr(rec.sys, "argv", ["issue-pr-reconcile"])
    assert rec.main() == 0  # a single bad write is not a failed sweep
    assert len(alerts) == 1
    assert "resolve write failed" in alerts[0][0]


def test_a_write_failure_alerts_but_a_gh_failure_does_not(rec, monkeypatch):
    """The two are kept apart on purpose. A gh failure is transient and self-healing.
    A write that failed after GitHub CONFIRMED the merge leaves the queue knowably
    wrong, with only a log file saying so."""
    issues = [_issue("issue_1", pr_repo="convivy/vivy", pr=1)]
    alerts = []
    monkeypatch.setattr(rec.ic, "list_issues_by_state", lambda *a, **k: issues)
    monkeypatch.setattr(rec, "pr_state", lambda repo, num: None)  # gh could not answer
    monkeypatch.setattr(rec.ic, "resolve_issue", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(rec, "post_alert",
                        lambda stream, body, **kw: alerts.append((body, kw.get("dedup_key"))))
    monkeypatch.setattr(rec.sys, "argv", ["issue-pr-reconcile"])
    assert rec.main() == 0
    assert alerts == []
