"""Stamped-review protocol + merge gate (lib/agent-platform/review_protocol.py).

The invariants these guard (ARCH-184 phase 2): a review stamp round-trips
losslessly so the gate reads back exactly what review-post wrote; an
unparseable or unknown-verdict stamp counts as NO stamp (the gate must never
treat garbage as an approval); and the gate PASSes iff every required
reviewer's LATEST stamp is an APPROVE at the PR's current head with CI green —
the stale-review near-miss (PA, 2026-06-17) is unmergeable by construction.
"""
from __future__ import annotations

import pytest

import review_protocol as RP


def _stamp(agent, commit, verdict="APPROVE", *, pr="7", repo="convivy/x", run_id="run-1"):
    return RP.ReviewStamp(version=RP.PROTOCOL_VERSION, pr=pr, repo=repo,
                          reviewed_commit=commit, verdict=verdict, agent=agent, run_id=run_id)


# --- header / parse round trip ----------------------------------------------

def test_render_parse_round_trip():
    header = RP.render_header(pr="7", repo="convivy/agent-platform",
                              reviewed_commit="abc123def456", verdict="APPROVE",
                              agent="reviewer", run_id="run-ea0e")
    # the stamp is an invisible HTML comment...
    assert header.startswith("<!--") and header.rstrip().endswith("-->")
    s = RP.parse(header)
    assert s is not None
    assert (s.pr, s.repo, s.reviewed_commit, s.verdict, s.agent, s.run_id) == (
        "7", "convivy/agent-platform", "abc123def456", "APPROVE", "reviewer", "run-ea0e")
    assert s.version == RP.PROTOCOL_VERSION


def test_parse_finds_stamp_embedded_in_a_full_comment():
    # review-post wraps the header in a human-visible line + prose body; parse
    # must still recover the stamp from the whole comment body.
    header = RP.render_header(pr="9", repo="convivy/x", reviewed_commit="deadbeef00",
                              verdict="REQUEST_CHANGES", agent="security-reviewer", run_id="r2")
    body = f"{header}\n\n**review · security-reviewer → REQUEST_CHANGES**\n\nFound an issue.\n"
    s = RP.parse(body)
    assert s is not None and s.verdict == "REQUEST_CHANGES" and s.agent == "security-reviewer"


def test_parse_returns_none_for_no_marker():
    assert RP.parse("just a normal human comment, lgtm 👍") is None
    assert RP.parse("") is None
    assert RP.parse(None) is None  # tolerate a None body from the gh JSON


def test_parse_returns_none_for_unknown_verdict():
    # an unparseable/garbage verdict is no stamp — the gate must not count it.
    bad = ("<!-- agentctl-review\nv: 1\npr: 7\nrepo: x\nreviewed-commit: abc\n"
           "verdict: LGTM\nagent: reviewer\nrun-id: r\n-->")
    assert RP.parse(bad) is None


def test_parse_verdict_is_case_insensitive():
    body = ("<!-- agentctl-review\nv: 1\npr: 7\nrepo: x\nreviewed-commit: abc\n"
            "verdict: approve\nagent: reviewer\nrun-id: r\n-->")
    s = RP.parse(body)
    assert s is not None and s.verdict == "APPROVE"


def test_render_header_round_trips_reviewed_base():
    """Round 3 of #732, finding 4: `review-launch` is the first poster to
    supply `reviewed_base` (platform/specs/builder-runs-as-two-accounts
    Handoff: "Stamp header on the PR comment keeps today's fields plus
    `reviewed_base`") — it must round-trip through `render_header`/`parse`
    the same way every other field does."""
    header = RP.render_header(pr="7", repo="convivy/agent-platform",
                              reviewed_commit="abc123def456", verdict="APPROVE",
                              agent="reviewer", run_id="run-ea0e", reviewed_base="main")
    s = RP.parse(header)
    assert s is not None
    assert s.reviewed_base == "main"


def test_render_header_omits_the_reviewed_base_line_when_unset():
    """`review-post`'s own stamps never pass `reviewed_base` — the field
    must default to the empty string, not a literal `"None"` or a blank
    `reviewed-base:` line, so older posters are unaffected."""
    header = RP.render_header(pr="7", repo="x", reviewed_commit="abc", verdict="APPROVE",
                              agent="reviewer", run_id="r")
    assert "reviewed-base" not in header
    s = RP.parse(header)
    assert s is not None and s.reviewed_base == ""


# --- the merge gate ---------------------------------------------------------

REQ = ["reviewer", "security-reviewer"]


def test_gate_passes_when_all_approve_at_head_and_ci_green():
    head = "f" * 40
    stamps = [_stamp("reviewer", head), _stamp("security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert r.ok and r.reasons == []
    assert r.approvals == {"reviewer": head, "security-reviewer": head}


def test_gate_fails_on_stale_approve():
    head, old = "f" * 40, "a" * 40
    stamps = [_stamp("reviewer", head), _stamp("security-reviewer", old)]  # sec is stale
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("security-reviewer" in x and "stale" in x for x in r.reasons)


def test_gate_fails_when_a_required_reviewer_missing():
    head = "f" * 40
    r = RP.evaluate_gate(head=head, stamps=[_stamp("reviewer", head)],
                         required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("no review posted by security-reviewer" in x for x in r.reasons)


def test_gate_fails_on_red_ci_even_with_approvals():
    head = "f" * 40
    stamps = [_stamp("reviewer", head), _stamp("security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=False)
    assert not r.ok
    assert any("CI is not green" in x for x in r.reasons)


def test_gate_latest_stamp_wins_request_changes_overrides_earlier_approve():
    # chronological order: an APPROVE, then a later REQUEST_CHANGES at the same
    # head, must NOT pass — the last word per agent governs.
    head = "f" * 40
    stamps = [
        _stamp("reviewer", head, "APPROVE"),
        _stamp("security-reviewer", head, "APPROVE"),
        _stamp("reviewer", head, "REQUEST_CHANGES"),  # reviewer changed its mind
    ]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("reviewer latest verdict is REQUEST_CHANGES" in x for x in r.reasons)


def test_gate_refuses_empty_head_instead_of_passing_vacuously():
    # without a real head, reviewed_commit == head would be "" == "" → vacuous PASS.
    # The gate must refuse to decide, not fail open (both reviewers flagged this).
    import pytest
    with pytest.raises(ValueError):
        RP.evaluate_gate(head="", stamps=[_stamp("reviewer", "")], required_agents=REQ, ci_green=True)


def test_gate_approvals_excludes_stale_and_request_changes_stamps():
    # `approvals` records only genuine current approvals — a FAIL result's
    # approvals never contains a stale or REQUEST_CHANGES stamp.
    head, old = "f" * 40, "a" * 40
    stamps = [_stamp("reviewer", old, "APPROVE"),          # stale
              _stamp("security-reviewer", head, "REQUEST_CHANGES")]  # not an approval
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert r.approvals == {}


def test_gate_latest_approve_clears_an_earlier_request_changes():
    # the re-review path: REQUEST_CHANGES at an old commit, then APPROVE at head.
    head, old = "f" * 40, "a" * 40
    stamps = [
        _stamp("reviewer", old, "REQUEST_CHANGES"),
        _stamp("reviewer", head, "APPROVE"),
        _stamp("security-reviewer", head, "APPROVE"),
    ]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert r.ok


# --- the gate and internal-app crews (ARCH-177) -----------------------------
# THE BUG THIS PINS. The gate keyed `latest` on the stamp's raw agent name while
# `required_agents` held the unprefixed roles, so a `sel-reviewer` APPROVE was
# invisible to it. Every prefixed crew — hollis, selector, svc, legacy pa — was
# unmergeable by construction: `review-status` said "no review posted by reviewer"
# at a PR carrying two valid APPROVEs, and because `_mark_pr_ready` runs the same
# function (ARCH-364), the PR never left draft and its full suite never ran.

@pytest.mark.parametrize("prefix", RP.INTERNAL_APP_PREFIXES)
def test_gate_passes_for_every_internal_app_crew_prefix(prefix):
    """Parametrized over the prefix tuple itself, so a crew added later is covered
    the moment its prefix lands — the 2026-07-12 drift went unnoticed precisely
    because nothing enumerated the list."""
    head = "f" * 40
    stamps = [_stamp(f"{prefix}reviewer", head),
              _stamp(f"{prefix}security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert r.ok, f"{prefix} crew cannot satisfy the gate: {r.reasons}"
    assert r.approvals == {"reviewer": head, "security-reviewer": head}


def test_gate_accepts_required_agents_given_in_prefixed_form():
    # `--required sel-reviewer sel-security-reviewer` names the same two roles.
    head = "f" * 40
    stamps = [_stamp("sel-reviewer", head), _stamp("sel-security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps,
                         required_agents=["sel-reviewer", "sel-security-reviewer"],
                         ci_green=True)
    assert r.ok


def test_gate_still_fails_a_prefixed_crew_with_one_reviewer_missing():
    # The prefix equivalence must not collapse the two roles into one satisfied slot.
    head = "f" * 40
    r = RP.evaluate_gate(head=head, stamps=[_stamp("sel-reviewer", head)],
                         required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("no review posted by security-reviewer" in x for x in r.reasons)


def test_gate_still_fails_a_stale_prefixed_approve():
    head, old = "f" * 40, "a" * 40
    stamps = [_stamp("hollis-reviewer", head),
              _stamp("hollis-security-reviewer", old)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("security-reviewer" in x and "stale" in x for x in r.reasons)


def test_gate_prefixed_request_changes_overrides_an_earlier_prefixed_approve():
    head = "f" * 40
    stamps = [_stamp("svc-reviewer", head, "APPROVE"),
              _stamp("svc-reviewer", head, "REQUEST_CHANGES"),
              _stamp("svc-security-reviewer", head, "APPROVE")]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("reviewer latest verdict is REQUEST_CHANGES" in x for x in r.reasons)


def test_gate_unprefixed_stamp_still_satisfies_an_unprefixed_requirement():
    # The shared-crew path must be untouched by the normalization.
    head = "f" * 40
    stamps = [_stamp("reviewer", head), _stamp("security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert r.ok


def test_gate_does_not_count_a_non_review_agent_that_merely_shares_a_prefix():
    # `sel-coder` strips to `coder`, which is not a required role. The gate must not
    # treat any prefixed agent as a reviewer.
    head = "f" * 40
    stamps = [_stamp("sel-coder", head), _stamp("sel-security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("no review posted by reviewer" in x for x in r.reasons)


# --- stamp authenticity (ARCH-193) ------------------------------------------
# Every agentctl run is bypassPermissions, so any agent CAN post a stamp; the
# gate authenticates a stamp against the run it claims before counting it.

REVIEW_AGENTS = ("reviewer", "security-reviewer")


def _facts(agent, *, repo="convivy/x", review_sha="", found=True,
           posted_verdict="REQUEST_CHANGES", posted_pr="8"):
    # What review-post recorded when the run posted. The defaults differ from `_stamp`'s
    # (APPROVE on PR 7), so authenticate must read the record: a test that passes a
    # default stamp against default facts is refused, and a test expecting a genuine run
    # names the posted verdict and PR it means.
    return RP.RunFacts(found=found, agent=agent, repo=repo, review_sha=review_sha,
                       posted_verdict=posted_verdict, posted_pr=posted_pr)


def test_authenticate_passes_for_a_real_reviewer_run():
    s = _stamp("reviewer", "f" * 40, run_id="run-real")
    assert RP.authenticate(s, _facts("reviewer", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_passes_for_pa_review_variant():
    # pa-reviewer is review-class once the pa- prefix is stripped (ARCH-177).
    s = _stamp("pa-reviewer", "f" * 40, run_id="run-real")
    assert RP.authenticate(s, _facts("pa-reviewer", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_passes_for_every_internal_app_crew():
    """ARCH-193's authenticity gate is the check of record for a review stamp. Its
    _review_base once stripped only `pa-`, while agentctl's prefix list had grown
    `hollis-` — so the gate rejected EVERY Hollis review stamp as "not a review-class
    agent", silently, on a live crew. Pin every crew's review pair through the gate."""
    for prefix in RP.INTERNAL_APP_PREFIXES:
        for role in ("reviewer", "security-reviewer"):
            agent = f"{prefix}{role}"
            s = _stamp(agent, "f" * 40, run_id="run-real")
            assert RP.authenticate(
                s, _facts(agent, posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS
            ) is None, agent


def test_authenticate_accepts_a_prefixed_run_stamping_the_bare_role():
    """THE AUTO-DISPATCH GAP (hollis co, 2026-07-23). An auto-dispatched review RUNS
    as the product-prefixed crew member but STAMPS the bare roster role. That is the
    same reviewer, so it must authenticate — hollis PR #550 carried two genuine
    APPROVEs while the gate reported "no review posted by security-reviewer", and the
    PR could not clear until someone re-ran a review that had already passed.

    Third instance of this bug class: the membership check above and `evaluate_gate`
    both already normalize on `_review_base`; this equality did not."""
    for prefix in RP.INTERNAL_APP_PREFIXES:
        for role in ("reviewer", "security-reviewer"):
            s = _stamp(role, "f" * 40, run_id="run-real")          # stamped bare
            assert RP.authenticate(
                s, _facts(f"{prefix}{role}", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS
            ) is None, f"{prefix}{role} stamping {role}"


def test_authenticate_accepts_a_bare_run_stamping_the_prefixed_role():
    """The mirror case, so the normalization is not accidentally one-directional."""
    for prefix in RP.INTERNAL_APP_PREFIXES:
        s = _stamp(f"{prefix}reviewer", "f" * 40, run_id="run-real")
        assert RP.authenticate(
            s, _facts("reviewer", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS) is None, prefix


def test_authenticate_accepts_the_same_role_across_two_crews():
    """Documents a boundary this change deliberately moved. Normalizing on
    `_review_base` means a `hollis-reviewer` run stamping `pa-reviewer` also
    authenticates — different crew, same ROLE.

    Intentional, and it grants nothing: both names collapse to the same required
    slot in `evaluate_gate`, so a cross-crew label buys no reviewer the item did not
    already have. Pinned so a future tightening has to be a deliberate decision
    rather than a silent one (reviewer, #222)."""
    s = _stamp("pa-reviewer", "f" * 40, run_id="run-real")
    assert RP.authenticate(
        s, _facts("hollis-reviewer", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_still_rejects_a_crew_member_claiming_the_OTHER_role():
    """The normalization must strip the PREFIX only. A run that reviewed as
    `hollis-reviewer` must never be able to satisfy the security-reviewer slot —
    that would let one dispatch stand in for both required reviewers."""
    for prefix in RP.INTERNAL_APP_PREFIXES:
        s = _stamp("security-reviewer", "f" * 40, run_id="run-real")
        reason = RP.authenticate(s, _facts(f"{prefix}reviewer"),
                                 pr="7", review_agents=REVIEW_AGENTS)
        assert reason is not None, f"{prefix}reviewer must not stamp security-reviewer"
        assert "claims agent" in reason
    # and the same across bare names
    s = _stamp("security-reviewer", "f" * 40, run_id="run-real")
    assert RP.authenticate(s, _facts("reviewer", posted_verdict="APPROVE", posted_pr="7"), pr="7", review_agents=REVIEW_AGENTS) is not None


def test_authenticate_still_rejects_a_worker_from_any_crew():
    """The prefix strip must not smuggle a worker onto the review surface: a
    <crew>-coder stamp is still 'not a review-class agent'."""
    for prefix in RP.INTERNAL_APP_PREFIXES:
        agent = f"{prefix}coder"
        s = _stamp(agent, "f" * 40, run_id="run-real")
        reason = RP.authenticate(s, _facts(agent), pr="7", review_agents=REVIEW_AGENTS)
        assert reason and "not a review-class agent" in reason, agent


def test_agentctl_and_review_protocol_prefixes_match(agentctl):
    """The two lists drifted once and both halves failed silently (2026-07-12):
    agentctl denied the svc-/sel- reviewers the PR surface, and review_protocol's
    gate rejected every hollis- review stamp. They cannot be one shared constant
    (agentctl imports libs from the deployed ~/.local path, so a branch's agentctl
    sees main's review_protocol) — so this test IS the seam that holds them."""
    assert tuple(agentctl._INTERNAL_APP_PREFIXES) == tuple(RP.INTERNAL_APP_PREFIXES)


def test_agentctl_and_review_protocol_stale_replay_marker_match(agentctl):
    """Same seam as the prefixes sync test: the caller recognises the commit-binding
    rejection by its named marker, so the two sides must not drift; a reword of one
    without the other is exactly the silent degradation the PR #330 marker fix exists
    to kill."""
    assert agentctl._STALE_REPLAY_MARKER == RP.STALE_REPLAY_MARKER


def test_agentctl_and_review_protocol_review_agents_match(agentctl):
    """Same seam as the two sync tests above, for the third list `authenticate()`'s
    review-class test depends on: `bin/run-register-api`'s `/runs/<run_id>/facts`
    route (issue_01M2K5HRWWVPWTC90NQTFEA8RK) calls `RP.is_review_class` with
    `RP.REVIEW_AGENTS` rather than agentctl's own `_REVIEW_AGENTS`, so the two must
    not drift or the route's `review_role` field could disagree with the gate about
    who is review-class."""
    assert tuple(agentctl._REVIEW_AGENTS) == tuple(RP.REVIEW_AGENTS)


def test_is_review_class_applies_the_same_rule_authenticate_does():
    """`is_review_class` is `authenticate()`'s membership check extracted to a named
    function (issue_01M2K5HRWWVPWTC90NQTFEA8RK) — a bare or crew-prefixed reviewer
    role passes, a bare or crew-prefixed non-review role does not."""
    for prefix in ("",) + RP.INTERNAL_APP_PREFIXES:
        for role in ("reviewer", "security-reviewer"):
            assert RP.is_review_class(f"{prefix}{role}", REVIEW_AGENTS), f"{prefix}{role}"
        assert not RP.is_review_class(f"{prefix}coder", REVIEW_AGENTS)


def test_authenticate_rejects_stamp_without_run_id():
    s = _stamp("reviewer", "f" * 40, run_id="")
    reason = RP.authenticate(s, _facts("reviewer"), pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "no run-id" in reason


def test_authenticate_rejects_unresolvable_run_id():
    s = _stamp("reviewer", "f" * 40, run_id="run-ghost")
    reason = RP.authenticate(s, RP.RunFacts(found=False), pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "no dispatch run" in reason


def test_authenticate_rejects_worker_run_posing_as_reviewer():
    # a coder stamped agent=reviewer but used its OWN run-id → the run is a coder.
    s = _stamp("reviewer", "f" * 40, run_id="run-coder")
    reason = RP.authenticate(s, _facts("coder"), pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "not a review-class agent" in reason


def test_authenticate_rejects_agent_mismatch():
    # run is review-class, but the stamp claims a different reviewer than the run ran.
    s = _stamp("reviewer", "f" * 40, run_id="run-sec")
    reason = RP.authenticate(s, _facts("security-reviewer"), pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "claims agent" in reason


def test_authenticate_rejects_repo_replay():
    s = _stamp("reviewer", "f" * 40, repo="convivy/x", run_id="run-other-repo")
    reason = RP.authenticate(s, _facts("reviewer", repo="convivy/y"),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "repo" in reason


def test_authenticate_accepts_owner_name_vs_short_repo_form():
    """ARCH-198 fix: a reviewer stamps `convivy/constance` (owner/name, from
    `gh pr view`) but the run registry records the short `constance` (`--on`).
    Basename normalization must authenticate these as the same repo — without it
    EVERY overnight PR failed the stamp gate despite a valid APPROVE."""
    s = _stamp("reviewer", "f" * 40, repo="convivy/constance", run_id="run-1")
    reason = RP.authenticate(s, _facts("reviewer", repo="constance", posted_verdict="APPROVE", posted_pr="7"),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason is None  # authentic — owner prefix tolerated
    # and the reverse form pairing is equally fine
    s2 = _stamp("reviewer", "f" * 40, repo="constance", run_id="run-1")
    assert RP.authenticate(s2, _facts("reviewer", repo="convivy/constance", posted_verdict="APPROVE", posted_pr="7"),
                           pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_still_rejects_cross_repo_with_owner_forms():
    """The basename normalization must NOT weaken the cross-repo-replay guard:
    different repo names (even with matching owners) still fail."""
    s = _stamp("reviewer", "f" * 40, repo="convivy/vivy", run_id="run-x")
    reason = RP.authenticate(s, _facts("reviewer", repo="constance"),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "repo" in reason


def test_authenticate_rejects_stale_commit_replay():
    # the real attack: replay a reviewer's run-id (which reviewed `old`) onto a
    # stamp claiming the current head. The run's pinned review_sha gives it away.
    head, old = "f" * 40, "a" * 40
    s = _stamp("reviewer", head, run_id="run-old")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=old),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason


def test_authenticate_stale_replay_rejection_embeds_marker_constant():
    # The commit-binding rejection must embed RP.STALE_REPLAY_MARKER, since the
    # caller's head-advance disambiguation recognises the rejection by this token;
    # a reword that drops it would silently kill the disambiguation while the gate
    # stayed closed (PR #330 review suggestion). This test is what makes such a
    # reword loud. The head-advanced variant must NOT carry the token: it is the
    # recognition key for the hard rejection, and the two must not be confusable.
    pin, stamped = "a" * 40, "f" * 40
    s = _stamp("reviewer", stamped, run_id="run-old")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and RP.STALE_REPLAY_MARKER in reason
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS, head_advanced=True)
    assert reason and RP.STALE_REPLAY_MARKER not in reason


def test_authenticate_forward_advanced_head_reported_distinctly():
    # THE BENIGN RACE (issue_01KYRQBNXRHB2PT8BKKNEW745V). The run was pinned to a
    # commit the PR head has since moved PAST (the caller proved it a descendant via
    # the compare API, so head_advanced=True). The reviewer read a real commit that
    # was superseded — not a replay — so the rejection must say re-dispatch, not
    # stale replay.
    pin, stamped = "a" * 40, "f" * 40
    s = _stamp("reviewer", stamped, run_id="run-race")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS, head_advanced=True)
    assert reason and "head advanced during review" in reason
    assert "stale replay" not in reason
    assert "re-dispatch" in reason


def test_authenticate_genuine_replay_rejected_when_head_not_advanced():
    # head_advanced=False: the caller determined the head did NOT advance past the
    # pin (it diverged, sits behind, or is identical). The binding rejection keeps
    # its original wording — the stamp names a commit the run was not pinned to read.
    pin, stamped = "a" * 40, "f" * 40
    s = _stamp("reviewer", stamped, run_id="run-old")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS, head_advanced=False)
    assert reason and "stale replay" in reason


def test_authenticate_indeterminate_ancestry_falls_back_to_rejection():
    # The caller could not determine whether the head advanced (no repo slug, gh
    # failure) → None. FAIL CLOSED: the stamp is a replay until forward movement is
    # proven, so the rejection wording is unchanged.
    pin, stamped = "a" * 40, "f" * 40
    s = _stamp("reviewer", stamped, run_id="run-old")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS, head_advanced=None)
    assert reason and "stale replay" in reason
    # the default (a caller that does not pass the flag at all) must behave the same
    reason = RP.authenticate(s, _facts("reviewer", review_sha=pin),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason


def test_authenticate_commit_binding_passes_when_review_sha_matches():
    head = "f" * 40
    s = _stamp("reviewer", head, run_id="run-cur")
    assert RP.authenticate(s, _facts("reviewer", review_sha=head, posted_verdict="APPROVE", posted_pr="7"),
                           pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_degrades_gracefully_for_legacy_run_without_review_sha():
    # a run dir from before review_sha was persisted: the commit-binding check is
    # skipped (empty), the weaker agent/repo checks still apply.
    s = _stamp("reviewer", "f" * 40, run_id="run-legacy")
    assert RP.authenticate(s, _facts("reviewer", review_sha="", posted_verdict="APPROVE", posted_pr="7"),
                           pr="7", review_agents=REVIEW_AGENTS) is None


# --- the posted verdict and PR (CQ·399) --------------------------------------
# review-post records the verdict and PR a run posted into that run's meta.json.
# A stamp authenticates only if it carries the recorded verdict, names the recorded
# PR, and that PR is the one the gate evaluates. Each forgery below changes one of
# the three and nothing else, so the reason it is refused names that one field.


def test_authenticate_refuses_an_approve_edited_from_a_genuine_request_changes_stamp():
    head = "f" * 40
    facts = _facts("reviewer", review_sha=head, posted_verdict="REQUEST_CHANGES", posted_pr="7")
    edited = _stamp("reviewer", head, verdict="APPROVE", pr="7", run_id="run-rc")
    reason = RP.authenticate(edited, facts, pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "verdict" in reason
    genuine = _stamp("reviewer", head, verdict="REQUEST_CHANGES", pr="7", run_id="run-rc")
    assert RP.authenticate(genuine, facts, pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_refuses_a_genuine_stamp_copied_from_another_pr_at_the_same_commit():
    head = "f" * 40
    facts = _facts("reviewer", review_sha=head, posted_verdict="APPROVE", posted_pr="9")
    copied = _stamp("reviewer", head, verdict="APPROVE", pr="9", run_id="run-9")
    reason = RP.authenticate(copied, facts, pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "PR 9" in reason and "PR 7" in reason
    assert RP.authenticate(copied, facts, pr="9", review_agents=REVIEW_AGENTS) is None


def test_authenticate_accepts_a_genuine_stamp_and_the_gate_counts_it():
    head = "f" * 40
    facts = _facts("reviewer", review_sha=head, posted_verdict="APPROVE", posted_pr="7")
    stamp = _stamp("reviewer", head, verdict="APPROVE", pr="7", run_id="run-ok")
    assert RP.authenticate(stamp, facts, pr="7", review_agents=REVIEW_AGENTS) is None
    gate = RP.evaluate_gate(head=head, stamps=[stamp], required_agents=["reviewer"],
                            ci_green=True)
    assert gate.ok and gate.approvals == {"reviewer": head}


@pytest.mark.parametrize("posted_verdict,posted_pr", [("", ""), ("", "7"), ("APPROVE", "")])
def test_authenticate_refuses_a_stamp_whose_run_record_lacks_the_posted_fields(
        posted_verdict, posted_pr):
    head = "f" * 40
    facts = _facts("reviewer", review_sha=head, posted_verdict=posted_verdict,
                   posted_pr=posted_pr)
    stamp = _stamp("reviewer", head, verdict="APPROVE", pr="7", run_id="run-legacy")
    reason = RP.authenticate(stamp, facts, pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "records no posted verdict or PR" in reason


def test_resolve_run_facts_reads_the_posted_verdict_and_pr(tmp_path, monkeypatch):
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-posted1", date="2026-10-06")
    T.write_meta(run_dir, run_id="run-posted1", agent="reviewer", task="t",
                 started_at="2026-10-06T00:00:00Z", repo="convivy/x", worktree=str(tmp_path),
                 profile="p", model="claude-sonnet-4-6", review_sha="c" * 40, pr=7)
    T.update_meta(run_dir, review_verdict="REQUEST_CHANGES", review_pr="7")

    facts = RP.resolve_run_facts("run-posted1")

    assert (facts.posted_verdict, facts.posted_pr) == ("REQUEST_CHANGES", "7")


# --- SHA abbreviation normalization (_sha_match) ----------------------------
# THE BUG THIS PINS (sous-chef PR #380, 2026-07-27). `agentctl review-post`
# stamped `reviewed-commit: 018b9e141` (a 9-char git abbreviation). The run's
# pinned review_sha was the full 40-char form. String equality failed, the stamp
# was declared a stale replay, and its REQUEST_CHANGES was thrown away.
# review-status then reported PASS — the merge gate failed open.
#
# The error message was self-refuting: both sides were truncated to [:9] for
# display, so it printed the same value twice.

FULL_SHA = "018b9e1419e93c2c57ba00165729c50363dd3d5d"
ABBREV_SHA = "018b9e141"  # 9-char prefix — the exact form from PR #380


def test_authenticate_accepts_abbreviated_stamp_commit():
    """The primary failure mode: stamp carries 9-char abbreviation, run has the
    full 40-char SHA. Must authenticate, not reject as stale replay."""
    s = _stamp("reviewer", ABBREV_SHA, run_id="run-real")
    assert RP.authenticate(s, _facts("reviewer", review_sha=FULL_SHA, posted_verdict="APPROVE", posted_pr="7"),
                           pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_accepts_abbreviated_review_sha():
    """Mirror case: run has an abbreviated review_sha, stamp has the full form."""
    s = _stamp("reviewer", FULL_SHA, run_id="run-real")
    assert RP.authenticate(s, _facts("reviewer", review_sha=ABBREV_SHA, posted_verdict="APPROVE", posted_pr="7"),
                           pr="7", review_agents=REVIEW_AGENTS) is None


def test_authenticate_rejects_genuinely_different_abbreviated_commit():
    """A stamp whose abbreviated commit is NOT a prefix of the pinned SHA must
    still be rejected. The fix must not weaken the stale-replay guard."""
    s = _stamp("reviewer", "aaaaaaaaa", run_id="run-old")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=FULL_SHA),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason


def test_authenticate_rejects_too_short_prefix():
    """A prefix shorter than _MIN_SHA_PREFIX chars must not match, even if it
    happens to be the correct prefix. A 1-character coincidence must not pass."""
    s = _stamp("reviewer", FULL_SHA[:4], run_id="run-real")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=FULL_SHA),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason


def test_authenticate_rejects_empty_reviewed_commit_even_with_matching_review_sha():
    """An empty reviewed-commit must never become a wildcard that passes the
    commit-binding check."""
    s = _stamp("reviewer", "", run_id="run-real")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=FULL_SHA),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason


def test_authenticate_mismatch_message_renders_shas_distinguishably():
    """Rejection message must render both SHAs so they read differently.

    THE BUG THIS PINS (vivy PR #1084, 2026-07-28). Two SHAs share a long
    common prefix but diverge at character 22. Both sides were truncated to
    [:9] for display, so the rejection printed the same 9-char value on each
    side of '!=' — 'X != X' — making the guard look broken when it was correct.
    """
    sha_a = "b82674f97ad3f06b0f87c04710bc9847105cefa7"
    sha_b = "b82674f97ad3f06b0f87c04710cc9847105cefa7"  # diverges at char 22
    s = _stamp("reviewer", sha_a, run_id="run-stale")
    reason = RP.authenticate(s, _facts("reviewer", review_sha=sha_b),
                             pr="7", review_agents=REVIEW_AGENTS)
    assert reason and "stale replay" in reason
    # Extract what was printed on each side of ' != '
    parts = reason.split(" != ")
    assert len(parts) == 2, f"expected exactly one ' != ' in reason: {reason!r}"
    lhs = parts[0].split()[-1]    # last token before ' != '
    rhs = parts[1].split()[-3]   # SHA is third-from-last: '... <sha> (stale replay)'
    assert lhs != rhs, (
        f"rejection message renders both SHAs as {lhs!r} — mismatch is invisible"
    )


def test_gate_passes_for_approve_with_abbreviated_reviewed_commit():
    """evaluate_gate must accept an APPROVE whose reviewed_commit is an
    abbreviation of the current PR head."""
    head = FULL_SHA
    stamps = [_stamp("reviewer", ABBREV_SHA), _stamp("security-reviewer", head)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert r.ok, f"gate should pass but got: {r.reasons}"


def test_gate_still_rejects_abbreviated_approve_at_wrong_commit():
    """An abbreviated commit that is NOT a prefix of head must still be stale."""
    head = FULL_SHA
    other_abbrev = "aaaaaaaaa"
    stamps = [_stamp("reviewer", head), _stamp("security-reviewer", other_abbrev)]
    r = RP.evaluate_gate(head=head, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    assert any("security-reviewer" in x and "stale" in x for x in r.reasons)


def test_gate_stale_approve_message_renders_shas_distinguishably():
    """evaluate_gate stale-APPROVE rejection must render both SHAs distinguishably.

    Mirror of test_authenticate_mismatch_message_renders_shas_distinguishably for
    the evaluate_gate path (review_protocol.py line ~311). Two SHAs share a long
    common prefix but diverge at character 22. If [:9] truncation were restored
    at that site, the rejection would print the same 9-char value on each side,
    hiding the mismatch from the operator reading the diagnostic.
    """
    sha_a = "b82674f97ad3f06b0f87c04710bc9847105cefa7"
    sha_b = "b82674f97ad3f06b0f87c04710cc9847105cefa7"  # diverges at char 22
    stamps = [_stamp("reviewer", sha_a), _stamp("security-reviewer", sha_a)]
    r = RP.evaluate_gate(head=sha_b, stamps=stamps, required_agents=REQ, ci_green=True)
    assert not r.ok
    stale_reasons = [x for x in r.reasons if "stale" in x]
    assert stale_reasons, f"expected a stale reason, got: {r.reasons}"
    reason = stale_reasons[0]
    # Message format: "{agent} APPROVE is stale — reviewed {sha}, head is {sha} (re-review needed)"
    assert "reviewed " in reason and "head is " in reason, f"unexpected format: {reason!r}"
    reviewed_sha = reason.split("reviewed ")[1].split(",")[0]
    head_sha = reason.split("head is ")[1].split(" (")[0]
    assert reviewed_sha != head_sha, (
        f"stale-APPROVE message renders both SHAs as {reviewed_sha!r} — mismatch is invisible"
    )


# --- find_run_dir / resolve_run_facts: the shared I/O loader ARCH-193's merge
# gate (bin/agentctl) and run-register-api's /runs/<run_id>/facts route (the
# Library's review-record write route) both call, so they can never disagree
# about a run's facts (issue_01M2K5HRWWVPWTC90NQTFEA8RK). test_agentctl.py's
# test_find_run_dir_rejects_path_traversal / test_resolve_run_facts_reads_meta_
# end_to_end already pin agentctl's thin wrappers over these; these pin the
# shared functions' own DEFAULT runs_root (telemetry.RUNS_ROOT), which is the
# form run-register-api actually calls.


def test_find_run_dir_default_runs_root_reads_telemetrys_runs_root(tmp_path, monkeypatch):
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    T.create_run_dir("run-abc123", date="2026-09-01")

    assert RP.find_run_dir("run-abc123") is not None
    assert RP.find_run_dir("run-does-not-exist") is None
    for bad in ("../../etc", "..", "a/b", "run-x/../../etc", ".", ""):
        assert RP.find_run_dir(bad) is None


def test_find_run_dir_refuses_a_duplicate_under_two_date_dirs(tmp_path, monkeypatch):
    """A worker can `mkdir <root>/2099-01-01/<run-id>` alongside its own real
    run directory — the exact plant issue_01M1JWB7M75SZ160VRJRCGARBF names.
    The later (sorts-first) date directory must not silently win: this must
    raise rather than return either candidate."""
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    real = T.create_run_dir("run-dup1", date="2026-09-01")
    planted = T.create_run_dir("run-dup1", date="2099-01-01")

    with pytest.raises(RP.AmbiguousRunDir) as exc_info:
        RP.find_run_dir("run-dup1")

    assert exc_info.value.run_id == "run-dup1"
    assert set(exc_info.value.dirs) == {real, planted}


def test_resolve_run_facts_default_runs_root_carries_pr_and_started_at(tmp_path, monkeypatch):
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-facts1", date="2026-09-01")
    T.write_meta(run_dir, run_id="run-facts1", agent="reviewer", task="t",
                started_at="2026-09-01T00:00:00Z", repo="convivy/x", worktree=str(tmp_path),
                profile="p", model="claude-sonnet-4-6", review_sha="c" * 40, pr=99)

    facts = RP.resolve_run_facts("run-facts1")

    assert facts.found
    assert (facts.agent, facts.repo, facts.review_sha, facts.pr, facts.started_at) == (
        "reviewer", "convivy/x", "c" * 40, 99, "2026-09-01T00:00:00Z")


def test_resolve_run_facts_legacy_meta_with_no_review_sha_key_degrades_to_empty_string(
        tmp_path, monkeypatch):
    """A run predating ARCH-184's `review_sha` field carries no such key at all
    (unlike `T.write_meta`, which always writes it, even as `null`) — this
    reproduces a genuine pre-field record, hand-written the same way
    `authenticate`'s own legacy test does."""
    import json
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "2026-01-01" / "run-legacy1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-legacy1", "agent": "reviewer", "task": "t",
        "started_at": "2026-01-01T00:00:00Z", "repo": "convivy/x",
    }), encoding="utf-8")

    facts = RP.resolve_run_facts("run-legacy1")

    assert facts.found and facts.review_sha == "" and facts.pr is None


def test_resolve_run_facts_refuses_a_duplicate_run_id_rather_than_picking_one(
        tmp_path, monkeypatch):
    """The merge gate's stamp authentication and run-register-api's
    `/runs/<run_id>/facts` route both read through `resolve_run_facts`
    (ARCH-193); an ambiguous run-id must degrade to `found=False` — the same
    fail-closed answer an unknown run-id already produces — rather than
    authenticating against whichever of the two directories won a sort
    (issue_01M1JWB7M75SZ160VRJRCGARBF). The planted directory here carries a
    review-class meta.json, so a resolver that picked it (instead of
    refusing) would authenticate a forged stamp."""
    import json
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    real = T.create_run_dir("run-dup2", date="2026-09-01")
    T.write_meta(real, run_id="run-dup2", agent="coder", task="t",
                started_at="2026-09-01T00:00:00Z", repo="convivy/x", worktree=str(tmp_path),
                profile="p", model="claude-sonnet-4-6")
    planted = T.create_run_dir("run-dup2", date="2099-01-01")
    (planted / "meta.json").write_text(json.dumps({
        "run_id": "run-dup2", "agent": "reviewer", "repo": "convivy/x",
        "review_sha": "c" * 40,
    }), encoding="utf-8")

    facts = RP.resolve_run_facts("run-dup2")

    assert facts.found is False


# --- resolve_run_facts / _read_meta_bounded: a run's own writable directory
# must not be able to turn a facts read into a denial-of-service against
# bin/run-register-api's KeepAlive daemon (round-2 security review on PR #593,
# run-21e68140). Each of these reproduces a shape a run can create in place of
# its own meta.json; each confirms `resolve_run_facts` fails closed
# (`found=False`) rather than raising, hanging, or reading unboundedly.


def test_resolve_run_facts_rejects_a_symlink_at_meta_json_and_returns_promptly(
        tmp_path, monkeypatch):
    """`rm meta.json && ln -s /dev/zero meta.json` in a run's own directory —
    the plain `Path.read_text()` this used to call would read forever. The
    `lstat` in `_open_meta_nofollow` refuses the symlink before any `open()`
    is attempted, so this must return immediately; the alarm below only
    proves the immediacy (`bin/run-observer`'s own FIFO regression test uses
    the same SIGALRM pattern for the same reason)."""
    import os as _os
    import signal
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-symlink1", date="2026-09-01")
    _os.symlink("/dev/zero", run_dir / "meta.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("resolve_run_facts hung on a symlinked meta.json")))
    signal.alarm(10)
    try:
        facts = RP.resolve_run_facts("run-symlink1")
    finally:
        signal.alarm(0)

    assert facts.found is False


def test_resolve_run_facts_rejects_a_fifo_with_no_writer_and_returns_promptly(
        tmp_path, monkeypatch):
    """`rm meta.json && mkfifo meta.json` with nothing on the write end — a
    plain `open()` on this blocks at the syscall and never returns
    (`bin/run-observer`'s own regression, module docstring)."""
    import os as _os
    import signal
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-fifo1", date="2026-09-01")
    _os.mkfifo(run_dir / "meta.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("resolve_run_facts hung on a FIFO meta.json")))
    signal.alarm(10)
    try:
        facts = RP.resolve_run_facts("run-fifo1")
    finally:
        signal.alarm(0)

    assert facts.found is False


def test_resolve_run_facts_rejects_a_meta_json_over_the_size_cap(tmp_path, monkeypatch):
    """A many-gigabyte regular file is the third shape a run can put in place
    of its own meta.json; this is the version of that shape a test can afford
    to write — one byte over `_MAX_META_BYTES`, still comfortably above any
    real meta.json (the largest under ~/Convivy/agent-runs measures 29224
    bytes)."""
    import json
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-huge1", date="2026-09-01")
    oversized = json.dumps({
        "agent": "reviewer", "repo": "convivy/x", "review_sha": "c" * 40,
        "pad": "x" * RP._MAX_META_BYTES,
    })
    assert len(oversized) > RP._MAX_META_BYTES
    (run_dir / "meta.json").write_text(oversized, encoding="utf-8")

    facts = RP.resolve_run_facts("run-huge1")

    assert facts.found is False


def test_resolve_run_facts_rejects_a_directory_at_meta_json(tmp_path, monkeypatch):
    """`meta.json` replaced with a directory of the same name — `os.open`
    would succeed on a directory opened read-only, and a plain read then
    raises `IsADirectoryError`; the `lstat`-based `S_ISREG` check refuses this
    before that read is ever attempted."""
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    run_dir = T.create_run_dir("run-dir1", date="2026-09-01")
    (run_dir / "meta.json").mkdir()

    facts = RP.resolve_run_facts("run-dir1")

    assert facts.found is False


def test_resolve_run_facts_still_succeeds_for_a_normal_run_after_a_hostile_neighbor(
        tmp_path, monkeypatch):
    """The positive control: none of the four rejections above leaves any
    process-global state (an open fd, a cache) that would make a SUBSEQUENT,
    well-formed request fail too — the exact property the daemon needs, since
    one poisoned run's directory must not degrade every other run's facts."""
    import os as _os
    import telemetry as T
    monkeypatch.setattr(T, "RUNS_ROOT", tmp_path)
    hostile = T.create_run_dir("run-hostile1", date="2026-09-01")
    _os.symlink("/dev/zero", hostile / "meta.json")
    RP.resolve_run_facts("run-hostile1")

    run_dir = T.create_run_dir("run-normal1", date="2026-09-01")
    T.write_meta(run_dir, run_id="run-normal1", agent="reviewer", task="t",
                started_at="2026-09-01T00:00:00Z", repo="convivy/x", worktree=str(run_dir),
                profile="p", model="claude-sonnet-4-6", review_sha="d" * 40, pr=5)

    facts = RP.resolve_run_facts("run-normal1")

    assert facts.found and facts.agent == "reviewer" and facts.review_sha == "d" * 40
