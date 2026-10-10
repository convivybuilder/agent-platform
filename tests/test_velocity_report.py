"""Tests for bin/velocity-report (the engineering-velocity rollup) — the pure
aggregation, derived rates, regexes, and bucketing config."""


def test_derived_rates(velocity_report):
    m = velocity_report
    base = m._blank_metrics()
    base.update(loc_added=1000, loc_removed=300, prs_merged=20, reverts=2,
                ci_runs=10, ci_first_pass=8, scored=5, survived=4)
    d = m.derived(base)
    assert d["loc_net"] == 700
    assert d["change_failure_rate"] == 0.1          # 2/20
    assert d["ci_first_pass_pct"] == 80.0           # 8/10
    assert d["survival_pct"] == 80.0                # 4/5


def test_derived_none_on_zero_denominators(velocity_report):
    d = velocity_report.derived(velocity_report._blank_metrics())
    assert d["loc_net"] == 0
    assert d["change_failure_rate"] is None         # no merges
    assert d["ci_first_pass_pct"] is None
    assert d["survival_pct"] is None


def test_add_sums_ints_and_concats_lists(velocity_report):
    m = velocity_report
    dst, src = m._blank_metrics(), m._blank_metrics()
    dst.update(commits=5, no_gh_repos=["a"])
    src.update(commits=3, no_gh_repos=["b"])
    m._add(dst, src)
    assert dst["commits"] == 8
    assert dst["no_gh_repos"] == ["a", "b"]


def test_feat_and_revert_regexes(velocity_report):
    m = velocity_report
    assert m._FEAT_RE.search("feat: add X")
    assert m._FEAT_RE.search("feat(api)!: change")
    assert m._FEAT_RE.search("[nightly] feat: y")
    assert not m._FEAT_RE.search("fix: a bug")
    # revert is anchored at the subject start (git's `Revert "..."`)
    assert m._REVERT_RE.search('Revert "feat: add X"')
    assert m._REVERT_RE.search("Revert: bad change")
    # the fix: a subject merely mentioning revert is NOT counted
    assert not m._REVERT_RE.search("feat: add a revert button")
    assert not m._REVERT_RE.search("this reverts commit abc")  # body phrase, not a subject
    assert not m._REVERT_RE.search("refactor the parser")


def test_build_alltime_aggregates_per_bucket_and_total(velocity_report, monkeypatch):
    m = velocity_report
    # tiny bucket map so the test is deterministic
    monkeypatch.setattr(m, "BUCKETS", {"A": ["r1", "r2"], "B": ["r3"]})
    monkeypatch.setattr(m, "loc_now", lambda r: {"r1": 100, "r2": 50, "r3": 10}[r])
    monkeypatch.setattr(m, "commits_total", lambda r: {"r1": 5, "r2": 3, "r3": 1}[r])
    monkeypatch.setattr(m, "prs_total", lambda r: {"r1": 4, "r2": None, "r3": 2}[r])  # r2 no gh
    monkeypatch.setattr(m, "repo_path", lambda r: __import__("pathlib").Path("/nope") / r)

    at = m.build_alltime()
    a = at["buckets"]["A"]
    assert a["loc_now"] == 150 and a["commits_total"] == 8 and a["prs_total"] == 4  # None→0
    assert [r["repo"] for r in a["repos"]] == ["r1", "r2"]
    assert a["repos"][1]["no_gh"] is True  # r2's prs_total was None
    assert a["pr_stale"] is False          # a None (no-gh) is not a fetch failure
    assert at["total"] == {"loc_now": 160, "commits_total": 9, "prs_total": 6, "pr_stale": False}


def test_bucketing_config_and_gh_owner(velocity_report):
    m = velocity_report
    assert set(m.BUCKETS) == {
        "Vivy", "Constance", "Sous Chef", "Hollis", "Selector", "App Kit",
        "Orient", "Convivy general", "Engineering-internal",
        # three NYH projects, one bucket each (ARCH-426 amendment) — NYH Studio is
        # the PRODUCT they build, which is not a velocity bucket
        "NYH Arch Tools", "East Texas Circle", "Park Villa",
        # The Keel's two halves plus the steward, split apart on 2026-08-17 (the operator).
        # ARCH-426 read the Keel's repos as four projects sharing a co; 2026-08-06
        # merged them into one; this splits them again on a different line. The cut
        # is the WIRE rather than the repo list: the Library serves the vaults, the
        # Bosun dispatches and measures the crews, and each gets a crew so the two
        # can move in parallel instead of queueing behind one co. "Keel" is a scope
        # now — nothing is built at it, so nothing rolls up to it.
        "Library", "Bosun", "Steward", "Deck",
        # placard (2026-09-05): property instruction pages reached by a QR code
        # at the property. Listed here in the same change as BUCKETS and the
        # config/projects.json entry, which is the point of this assertion — it
        # is the ONE drift check in a registration chain that otherwise spans
        # seven hand-maintained lists across three repos, and it is the reason
        # the velocity side did not become another forgotten copy.
        "Placard",
        # shipwright (2026-09-06): the crew working the company bead, added with
        # its BUCKETS row and registry entry in one change for the same reason.
        "Shipwright",
        # signals (2026-09-16): the internal product crew, registered with its
        # BUCKETS row in one change.
        "Signals",
        # archy (2026-10-02): the small-business agentic ops product, registered
        # with its BUCKETS row and registry entry in one change.
        "Archy",
        "Write Better"}
    # every repo appears in exactly one bucket
    flat = [r for repos in m.BUCKETS.values() for r in repos]
    assert len(flat) == len(set(flat))
    # each NYH project reports for itself; "NYH Studio" is no longer a bucket
    assert m.BUCKETS["NYH Arch Tools"] == ["nyh-arch-tools"]
    assert m.BUCKETS["East Texas Circle"] == ["nyh-etc"]
    assert m.BUCKETS["Park Villa"] == ["nyh-park-villa"]
    assert "NYH Studio" not in m.BUCKETS
    # the shared KB slice still has to land somewhere — an ADR under `nyh-studio`
    # cannot be attributed to one property, so it counts for the studio-wide project
    assert m.ADR_PROJECT_BUCKET["nyh-studio"] == "NYH Arch Tools"
    # nyh resolves to the configured github_login; everything else to convivy
    assert m.gh_full("nyh-arch-tools") == "operator-login/nyh-arch-tools"
    assert m.gh_full("vivy") == "convivy/vivy"


def test_engineering_internal_survives_with_no_repos_as_the_adr_fallback(velocity_report):
    """It is no longer a PR bucket — every repo it held is now named on its own
    project, and the two sunsetted ones left ~/Convivy/ entirely (ARCH-427). It
    exists ONLY so an ADR whose KB project slug maps to no bucket (`shared`,
    `engineering`, `convivy`, `utility`) still lands somewhere. Deleting it would
    drop those ADRs from the report silently, which is why this is asserted rather
    than left for a reader to infer."""
    m = velocity_report
    assert m.BUCKETS["Engineering-internal"] == []
    # the fallback still routes: the company-altitude KB slugs map to no bucket
    for slug in ("shared", "engineering", "convivy", "utility"):
        assert slug not in m.ADR_PROJECT_BUCKET
        assert m.ADR_PROJECT_BUCKET.get(slug, "Engineering-internal") == "Engineering-internal"


def test_the_sunsetted_kit_repos_are_gone_from_every_bucket(velocity_report):
    """ARCH-427 archived them out of the working set, so they must not roll up
    anywhere — a stale entry would keep reporting a repo that is no longer there."""
    gone = {"multitenant-registry", "multi-agent-slack-bot-kit", "agentic-starter-kit"}
    flat = {r for repos in velocity_report.BUCKETS.values() for r in repos}
    assert not (flat & gone), f"sunsetted repos still bucketed: {flat & gone}"


def test_repo_path_maps_nyh_umbrella_subdir(velocity_report):
    m = velocity_report
    # NYH Studio repos live one level down under the umbrella, not flat under ~/Convivy —
    # the bug that read 0 commits/LOC for them (the operator, 2026-07-01).
    assert m.repo_path("nyh-park-villa") == m.CONVIVY / "nyh-architecture" / "nyh-park-villa"
    assert m.repo_path("nyh-arch-tools") == m.CONVIVY / "nyh-architecture" / "nyh-arch-tools"
    # a flat repo is unchanged
    assert m.repo_path("vivy") == m.CONVIVY / "vivy"


def test_adr_project_routing_matches_explanations_buckets(velocity_report):
    # an ADR's project decides its bucket (a constance ADR counts for Constance)
    m = velocity_report
    assert m.ADR_PROJECT_BUCKET["constance"] == "Constance"
    assert m.ADR_PROJECT_BUCKET["hollis"] == "Hollis"
    assert "shared" not in m.ADR_PROJECT_BUCKET  # falls through to Engineering-internal


def test_every_adr_bucket_is_a_live_bucket(velocity_report):
    """An ADR_PROJECT_BUCKET VALUE has to be a real BUCKETS key, or its ADRs count
    zero in the per-bucket breakdown while the grand total stays right — a wrong
    number that reads as a real one.

    This is not hypothetical: the 2026-08-06 Keel fold renamed three buckets and
    left this map pointing at the old display names, and nothing failed."""
    m = velocity_report
    stale = {slug: b for slug, b in m.ADR_PROJECT_BUCKET.items() if b not in m.BUCKETS}
    assert not stale, f"ADR_PROJECT_BUCKET routes to buckets that no longer exist: {stale}"


def test_the_arch_log_is_attributed_to_a_live_bucket(velocity_report, monkeypatch):
    """`substrate/decisions.md` counts for whichever bucket owns the substrate repo.

    The attribution compares a bucket name against a literal, so a bucket rename
    silently drops every ARCH entry from the per-bucket `arch` field while the
    grand total stays correct — the same failure shape as the test above, and the
    bug that shipped in this PR's first head.

    Asserted through BEHAVIOUR rather than through the source text: stub
    `arch_entries` to a known count, build a window over two buckets, and check the
    number lands on the substrate-owning one and nowhere else. The earlier version
    of this test read `build_window`'s source and looked for the literal — which
    passes today and would either break on a harmless refactor or wave through a
    real regression, depending on which way the text moved (reviewer, round 2)."""
    from datetime import datetime, timezone
    m = velocity_report
    owning = [b for b, repos in m.BUCKETS.items() if "substrate" in repos]
    assert len(owning) == 1, f"substrate must sit in exactly one bucket, found {owning}"
    arch_bucket = owning[0]

    monkeypatch.setattr(m, "BUCKETS", {arch_bucket: ["substrate"], "Other": ["vivy"]})
    _stub_window_deps(m, monkeypatch)
    monkeypatch.setattr(m, "arch_entries", lambda s, u=None: 42)
    monkeypatch.setattr(m, "gh_pr_metrics", lambda r, s, u=None: {})

    out = m.build_window(30, datetime(2026, 6, 30, tzinfo=timezone.utc))
    assert out["buckets"][arch_bucket]["arch"] == 42, (
        f"the ARCH log did not land on {arch_bucket!r}, the bucket that owns substrate")
    assert out["buckets"]["Other"]["arch"] == 0, "the ARCH log leaked into another bucket"
    assert out["total"]["arch"] == 42


# --- transient-failure hardening (the operator 2026-06-30): never let a failed GitHub fetch
# masquerade as no-GitHub / a false 0; carry the last-known-good value forward -------

import pytest  # noqa: E402


def test_is_on_github_distinguishes_clone_remote_from_gh_only(velocity_report, monkeypatch, tmp_path):
    m = velocity_report
    # not cloned locally → assumed on GitHub (gh-only repos like nyh); a failed query
    # is then a transient error to carry forward, not a false "no remote".
    monkeypatch.setattr(m, "repo_path", lambda r: tmp_path / "absent")
    assert m._is_on_github("nyh") is True
    # cloned with a github origin → True
    p = tmp_path / "vivy"
    (p / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "repo_path", lambda r: p)
    monkeypatch.setattr(m, "_git", lambda r, a: "git@github.com:convivy/vivy.git")
    assert m._is_on_github("vivy") is True
    # cloned, no github origin → False (a local-only *-commercial site)
    monkeypatch.setattr(m, "_git", lambda r, a: None)
    assert m._is_on_github("vivy-commercial") is False


def test_gh_pr_metrics_raises_on_fetch_error_but_none_for_no_gh(velocity_report, monkeypatch):
    m = velocity_report
    monkeypatch.setattr(m, "_is_on_github", lambda r: False)
    assert m.gh_pr_metrics("commercial", "2026-06-01") is None          # no remote → None
    monkeypatch.setattr(m, "_is_on_github", lambda r: True)
    monkeypatch.setattr(m, "_gh_json", lambda *a, **k: None)             # retries exhausted
    with pytest.raises(m.FetchError):
        m.gh_pr_metrics("vivy", "2026-06-01")
    monkeypatch.setattr(m, "_gh_json",
                        lambda *a, **k: [{"title": "feat: x"}, {"title": "fix: y"}])
    assert m.gh_pr_metrics("vivy", "2026-06-01") == {"prs_merged": 2, "features": 1}


def _stub_window_deps(m, monkeypatch):
    # deps take an optional until bound (as-of history) — accept and ignore it.
    monkeypatch.setattr(m, "git_metrics", lambda r, s, u=None: {
        "has_local": True, "commits": 5, "loc_added": 0, "loc_removed": 0, "reverts": 0})
    monkeypatch.setattr(m, "scorecard_quality", lambda s, u=None: {})
    monkeypatch.setattr(m, "adrs_minted", lambda s, u=None: {})
    monkeypatch.setattr(m, "arch_entries", lambda s, u=None: 0)


def test_build_window_carries_forward_prs_on_fetch_error(velocity_report, monkeypatch):
    from datetime import datetime, timezone
    m = velocity_report
    monkeypatch.setattr(m, "BUCKETS", {"Vivy": ["vivy"]})
    _stub_window_deps(m, monkeypatch)

    def boom(r, s, u=None):
        raise m.FetchError("gh down")
    monkeypatch.setattr(m, "gh_pr_metrics", boom)
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)

    prev = {"windows": {"30": {"buckets": {"Vivy": {"prs_merged": 243, "features": 10}}}}}
    v = m.build_window(30, now, prev)["buckets"]["Vivy"]
    assert v["prs_merged"] == 243 and v["features"] == 10   # last-known-good, NOT a false 0
    assert v["pr_stale"] is True
    assert m.build_window(30, now, prev)  # (sanity: no crash)
    # total is flagged stale so the dashboard can show the number is held
    assert m.build_window(30, now, prev)["total"]["pr_stale"] is True
    # with no prior snapshot it can't fabricate — stays 0 but is still flagged stale,
    # so the page shows "—/stale", never a confident 0.
    v0 = m.build_window(30, now, {})["buckets"]["Vivy"]
    assert v0["prs_merged"] == 0 and v0["pr_stale"] is True


def test_build_alltime_carries_forward_prs_total_on_fetch_error(velocity_report, monkeypatch):
    import pathlib
    m = velocity_report
    monkeypatch.setattr(m, "BUCKETS", {"Vivy": ["vivy"]})
    monkeypatch.setattr(m, "loc_now", lambda r: 100)
    monkeypatch.setattr(m, "commits_total", lambda r: 5)
    monkeypatch.setattr(m, "repo_path", lambda r: pathlib.Path("/nope") / r)

    def boom(r):
        raise m.FetchError("gh down")
    monkeypatch.setattr(m, "prs_total", boom)

    prev = {"alltime": {"buckets": {"Vivy": {"repos": [{"repo": "vivy", "prs_total": 600}]}}}}
    at = m.build_alltime(prev)
    row = at["buckets"]["Vivy"]["repos"][0]
    assert row["prs_total"] == 600         # carried forward, not flipped to 0/no_gh
    assert row["pr_stale"] is True and row["no_gh"] is False
    # staleness propagates to the bucket + grand total (symmetric with build_window)
    assert at["buckets"]["Vivy"]["pr_stale"] is True and at["total"]["pr_stale"] is True


def test_is_on_github_rejects_lookalike_host(velocity_report, monkeypatch, tmp_path):
    """github.com must be the URL host, not any substring — a look-alike like
    github.com.evil.example is NOT GitHub (security review, PR #94)."""
    m = velocity_report
    p = tmp_path / "r"
    (p / ".git").mkdir(parents=True)
    monkeypatch.setattr(m, "repo_path", lambda r: p)
    monkeypatch.setattr(m, "_git", lambda r, a: "https://github.com.evil.example/x/y.git")
    assert m._is_on_github("r") is False
    monkeypatch.setattr(m, "_git", lambda r, a: "ssh://git@github.com/convivy/vivy.git")
    assert m._is_on_github("r") is True


# --- velocity index (weighted 0–100) ----------------------------------------

def test_norm_rate_anchors_on_frozen_baseline(velocity_report):
    m = velocity_report
    assert m._norm_rate(10, 10) == 50.0     # at baseline → 50
    assert m._norm_rate(20, 10) == 100.0    # 2× baseline → 100
    assert m._norm_rate(40, 10) == 100.0    # capped, never above 100
    assert m._norm_rate(0, 10) == 0.0       # real baseline, fell to 0 → genuine stall → 0
    # no frozen baseline: activity reads a neutral 50; no basis at all → None (drops out)
    assert m._norm_rate(5, 0) == 50.0
    assert m._norm_rate(0, 0) is None


def test_velocity_index_weights_and_renormalizes_over_present(velocity_report):
    m = velocity_report
    # 7-day window; baseline 10 merged/wk and 2 feat/wk → this window sits exactly at
    # baseline on both rate components (→ 50 each).
    base = {"prs_merged": 10.0, "features": 2.0}
    full = {"prs_merged": 10, "features": 2, "survival_pct": 80.0,
            "ci_first_pass_pct": 90.0, "change_failure_rate": 0.1}
    r = m.velocity_index(full, 7, base)
    # 50·.35 + 80·.25 + 90·.20 + 50·.10 + 90·.10 = 69.5
    assert r["score"] == 69.5
    assert set(r["components"]) == {"throughput", "survival", "ci_first_pass",
                                    "features", "stability"}
    assert r["components"]["stability"]["value"] == 90.0   # (1 − 0.1)·100

    # drop survival (no scored runs) → its .25 weight renormalizes across the rest.
    partial = {**full, "survival_pct": None}
    r2 = m.velocity_index(partial, 7, base)
    # (50·.35 + 90·.20 + 50·.10 + 90·.10) / .75 = 49.5/.75 = 66.0
    assert r2["score"] == 66.0
    assert "survival" not in r2["components"]
    assert round(sum(c["weight"] for c in r2["components"].values()), 3) == 1.0


def test_velocity_index_none_when_no_basis(velocity_report):
    m = velocity_report
    # no baseline, no activity, no quality rows → no basis to score (an unmeasurable
    # local-only bucket), so the index is None (page shows "—"), not a misleading 0.
    r = m.velocity_index({"prs_merged": 0, "features": 0}, 30, {})
    assert r["score"] is None and r["components"] == {}
    # but a real baseline that fell to zero IS a genuine stall → scores 0, not None.
    r2 = m.velocity_index({"prs_merged": 0, "features": 0}, 30,
                          {"prs_merged": 5.0, "features": 1.0})
    assert r2["score"] == 0.0
    assert set(r2["components"]) == {"throughput", "features"}


def test_attach_index_writes_score_into_buckets_and_total(velocity_report):
    m = velocity_report
    windows = {"7": {
        "buckets": {"Vivy": {"prs_merged": 20, "features": 4, "survival_pct": 100.0,
                             "ci_first_pass_pct": 100.0, "change_failure_rate": 0.0}},
        "total": {"prs_merged": 20, "features": 4, "survival_pct": 100.0,
                  "ci_first_pass_pct": 100.0, "change_failure_rate": 0.0},
    }}
    baseline = {"per_week": {"Vivy": {"prs_merged": 10.0, "features": 2.0},
                             "TOTAL": {"prs_merged": 10.0, "features": 2.0}}}
    m.attach_index(windows, baseline)
    # every component maxed (2× baseline throughput/features, 100% quality) → 100.
    assert windows["7"]["buckets"]["Vivy"]["velocity_index"]["score"] == 100.0
    assert windows["7"]["total"]["velocity_index"]["score"] == 100.0


# --- weekly history + backfill ----------------------------------------------

def test_iso_week_start_is_the_monday(velocity_report):
    from datetime import date
    m = velocity_report
    assert m._iso_week_start(date(2026, 6, 30)) == "2026-06-29"   # Tue → that Monday
    assert m._iso_week_start(date(2026, 6, 29)) == "2026-06-29"   # Monday → itself


def test_history_point_extracts_scores_and_stale(velocity_report):
    m = velocity_report
    window = {
        "buckets": {"Vivy": {"velocity_index": {"score": 72.0}},
                    "Constance": {"velocity_index": {"score": 55.5}}},
        "total": {"velocity_index": {"score": 66.0}, "pr_stale": True},
    }
    pt = m._history_point(window, "2026-06-29", "2026-06-30")
    assert pt == {"week": "2026-06-29", "date": "2026-06-30", "total": 66.0,
                  "buckets": {"Vivy": 72.0, "Constance": 55.5}, "pr_stale": True}


def test_upsert_history_replaces_same_week_and_sorts(velocity_report, tmp_path):
    m = velocity_report
    path = tmp_path / "velocity-history.jsonl"
    m.upsert_history({"week": "2026-06-22", "total": 60.0}, path)
    m.upsert_history({"week": "2026-06-15", "total": 58.0}, path)
    m.upsert_history({"week": "2026-06-22", "total": 64.0}, path)   # same week → replace
    rows = m._load_jsonl(path)
    assert [r["week"] for r in rows] == ["2026-06-15", "2026-06-22"]  # sorted, deduped
    assert rows[1]["total"] == 64.0                                   # latest value wins


def test_backfill_skips_existing_and_stale_weeks(velocity_report, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    m = velocity_report
    path = tmp_path / "velocity-history.jsonl"
    # a real (live-recorded) point already exists for one of the weeks we'd backfill.
    m.upsert_history({"week": "2026-06-22", "total": 99.0, "date": "2026-06-24"}, path)

    calls = []

    def fake_build_window(days, asof, prev=None, until=None):
        calls.append(asof.date().isoformat())
        # the oldest reconstructed week's fetch "failed" (stale) → must be skipped.
        stale = asof.date().isoformat() == "2026-06-08"
        vi = {"velocity_index": {"score": 70.0}}
        return {"buckets": {"Vivy": dict(vi)},
                "total": {**vi, "pr_stale": stale}}
    monkeypatch.setattr(m, "build_window", fake_build_window)
    monkeypatch.setattr(m, "attach_index", lambda w, b: None)   # scores already stubbed in

    now = datetime(2026, 6, 30, tzinfo=timezone.utc)  # Tue; this Monday = 2026-06-29
    n = m.backfill_history(3, now, {}, path)
    weeks = [r["week"] for r in m._load_jsonl(path)]
    # weeks looked at: 06-22 (existing, skipped before build), 06-15, 06-08 (stale, skipped)
    assert "2026-06-22" not in calls              # existing week never recomputed
    assert weeks == ["2026-06-15", "2026-06-22"]  # 06-08 dropped as stale; existing kept
    assert n == 1  # return is newly-reconstructed points (06-15 only), not the file total
    # the pre-existing point is untouched (not overwritten by backfill)
    assert next(r for r in m._load_jsonl(path) if r["week"] == "2026-06-22")["total"] == 99.0


def test_gh_pr_metrics_until_bounds_the_merged_range(velocity_report, monkeypatch):
    m = velocity_report
    monkeypatch.setattr(m, "_is_on_github", lambda r: True)
    seen = {}

    def capture(args, tries=3):
        seen["args"] = args
        return []
    monkeypatch.setattr(m, "_gh_json", capture)
    m.gh_pr_metrics("vivy", "2026-06-01")
    assert "merged:>=2026-06-01" in seen["args"]              # live: open-ended
    m.gh_pr_metrics("vivy", "2026-06-01", "2026-06-08")
    assert "merged:2026-06-01..2026-06-08" in seen["args"]    # as-of: bounded both ends


def _tiny_repo(path, files):
    """A real one-commit git repo, so the pathspec is exercised by git itself
    rather than by a stub that would agree with whatever we wrote."""
    import subprocess
    path.mkdir(parents=True)
    def run(*a):
        subprocess.run(["git", "-C", str(path), *a], capture_output=True, check=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    for name, body in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    run("add", "-A")
    run("commit", "-qm", "seed")
    return path


def test_markdown_is_not_a_line_of_code(velocity_report, monkeypatch, tmp_path):
    """The operator, 2026-09-05: "LOC is rough anyway, but adding *.md distorts it even
    more." `knowledge` reported 935,175 net lines over 30 days off an import of
    928,787 lines on a single day — one migration wearing the shape of velocity.

    Both measures have to drop the prose, not just the windowed one: `loc_now`
    feeds the all-time cell and `git_metrics` the 30-day cell, and an exclusion
    applied to one of them would make the two disagree in a way that reads as a
    data bug rather than as a missing filter.
    """
    p = _tiny_repo(tmp_path / "kb", {
        "doc.md": "prose\n" * 100,
        "projects/platform/adr.md": "more prose\n" * 50,
        "scripts/run.py": "x = 1\n" * 10,
    })
    monkeypatch.setattr(velocity_report, "repo_path", lambda r: p)
    monkeypatch.setattr(velocity_report, "main_ref", lambda r: "main")

    metrics = velocity_report.git_metrics("kb", "2000-01-01T00:00:00Z")

    assert metrics["loc_added"] == 10, "only scripts/run.py should count"
    assert velocity_report.loc_now("kb") == 10


def test_the_exclusion_spares_code_that_merely_sits_beside_prose(velocity_report,
                                                                monkeypatch, tmp_path):
    """The blanket pattern is wider than the KB — a README in a code repo goes
    too, and that is intended. What must NOT go is the code around it, which is
    the failure mode of excluding a repo or a directory instead of a filetype.
    """
    p = _tiny_repo(tmp_path / "svc", {
        "README.md": "readme\n" * 200,
        "src/app.py": "y = 2\n" * 30,
        "docs/guide.md": "guide\n" * 80,
    })
    monkeypatch.setattr(velocity_report, "repo_path", lambda r: p)
    monkeypatch.setattr(velocity_report, "main_ref", lambda r: "main")

    assert velocity_report.git_metrics("svc", "2000-01-01T00:00:00Z")["loc_added"] == 30
    assert velocity_report.loc_now("svc") == 30


def test_a_stale_base_branch_does_not_get_its_content_counted_twice(velocity_report,
                                                                   monkeypatch, tmp_path):
    """The bug `--first-parent` fixes, reproduced rather than described.

    `knowledge` reported 1,067,337 net lines against a true 141,790 because
    branches cut from a pre-migration base carried the corpus in their own
    commits. Summing every commit counts that content even though the merge
    discarded it, so it lands in the metric without ever landing in the tree.

    THE FIXTURE HAS TO DISCARD THE BRANCH'S VERSION, and that is the whole
    subtlety. A first attempt gave both sides BYTE-IDENTICAL content, which
    reproduced nothing: git's history simplification prunes the redundant commit
    as TREESAME under the `-- .` pathspec, so the unfixed script scored it
    correctly and the test passed against the very bug it was written for. Here
    the two sides differ and `-X ours` keeps main's, so the branch's 100 lines
    are real to `git log` and absent from the result.

    Counting every commit gives 202 for a tree holding 102 lines. Walking
    first-parent gives 102, which is what `loc_now` independently measures.
    """
    import subprocess
    p = tmp_path / "kb"
    p.mkdir()

    def run(*a):
        subprocess.run(["git", "-C", str(p), *a], capture_output=True, check=True)

    def head():
        return subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (p / "a.py").write_text("a = 1\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    base = head()

    (p / "big.py").write_text("".join(f"m{i}\n" for i in range(100)))
    run("add", "-A")
    run("commit", "-qm", "the corpus, on main")

    run("checkout", "-q", "-b", "stale", base)
    (p / "big.py").write_text("".join(f"s{i}\n" for i in range(100)))
    (p / "small.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "the corpus again, from a stale base")

    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-X", "ours", "-m", "merge stale", "stale")

    monkeypatch.setattr(velocity_report, "repo_path", lambda r: p)
    monkeypatch.setattr(velocity_report, "main_ref", lambda r: "main")

    tree = 1 + 100 + 1                      # a.py, main's big.py, small.py
    assert velocity_report.loc_now("kb") == tree

    added = velocity_report.git_metrics("kb", "2000-01-01T00:00:00Z")["loc_added"]
    assert added == tree, (
        "first-parent must not count the discarded branch copy; summing every "
        "commit gives 202 against a 102-line tree")


def test_work_merged_inside_the_window_counts_even_if_its_commit_predates_it(
        velocity_report, monkeypatch, tmp_path):
    """The other direction the traversal gets wrong, and the one that matters
    most here because this tool ONLY ever runs windowed.

    A branch commit dated before `--since` is filtered out of a full traversal,
    even when the merge that put it on main falls inside the window. So the
    work landed in the period and the metric does not see it. `--first-parent`
    reads it off the merge's diff against main, where it belongs.

    Measured on this exact fixture: full traversal 10, first-parent 30, for a
    branch that contributed 20 lines to main during the window.

    THIS REPLACED A TEST THAT ASSERTED THE SAME NUMBER UNDER EVERY VARIANT.
    Its predecessor built a well-formed branch inside the window and asserted
    31, which `git log`, `--first-parent`, `--no-merges` and `--first-parent -m`
    all produce — so it would have passed against the unfixed script while its
    docstring claimed to guard the fix. Only a bare `-m` moved it. That is the
    third instance in this PR of a green test meaning less than its name.
    """
    import subprocess
    p = tmp_path / "svc"
    p.mkdir()

    def run(*a, when=None):
        env = None
        if when:
            import os
            env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(p), *a], capture_output=True, check=True, env=env)

    OLD, NEW = "2026-01-01T00:00:00Z", "2026-09-01T00:00:00Z"
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (p / "a.py").write_text("a = 1\n")
    run("add", "-A")
    run("commit", "-qm", "base", when=OLD)

    run("checkout", "-q", "-b", "feature")
    (p / "feature.py").write_text("f = 1\n" * 20)
    run("add", "-A")
    run("commit", "-qm", "feature, before the window", when=OLD)

    run("checkout", "-q", "main")
    (p / "main.py").write_text("m = 1\n" * 10)
    run("add", "-A")
    run("commit", "-qm", "on main, inside the window", when=NEW)
    run("merge", "-q", "--no-ff", "-m", "merge feature, inside the window",
        "feature", when=NEW)

    monkeypatch.setattr(velocity_report, "repo_path", lambda r: p)
    monkeypatch.setattr(velocity_report, "main_ref", lambda r: "main")

    added = velocity_report.git_metrics("svc", "2026-06-01T00:00:00Z")["loc_added"]
    assert added == 30, (
        "the branch's 20 lines reached main inside the window; a full traversal "
        "filters its commit out by date and reports 10")


# --- the producer trusting a working checkout instead of the authoritative ref
# (2026-09-07). Three defects of one shape: shallowness, HEAD-relative decision
# counts, and an --out that redirected only half the output set. Each test below
# was confirmed RED against the unfixed script before the fix landed. ----------

def _repo_with_real_history(path, *, branch="main"):
    """A real git repo with a real history — these defects live in git's own
    behaviour, so a stubbed _git would test the stub."""
    import subprocess
    path.mkdir(parents=True, exist_ok=True)

    def run(*a, when=None):
        env = None
        if when:
            import os
            env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", "-C", str(path), *a], capture_output=True, check=True, env=env)

    run("init", "-q", "-b", branch)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    return run


def test_is_shallow_tells_a_grafted_clone_from_a_complete_one(velocity_report, tmp_path):
    """The defect this guards produced NO distinguishing evidence: ~/Convivy/knowledge
    was shallow at 337 commits from 2026-08-23 to 2026-09-06 and the rollup reported
    its truncated counts as measurement. A shallow repo and a young repo count alike,
    so the only way to know is to ask git.

    Asserted in BOTH directions on real clones, because a check that only ever sees
    the failing state cannot show it distinguishes anything."""
    import subprocess
    m = velocity_report
    origin = tmp_path / "origin"
    run = _repo_with_real_history(origin)
    for i in range(5):
        (origin / f"f{i}.txt").write_text(f"{i}\n")
        run("add", "-A")
        run("commit", "-qm", f"commit {i}")

    deep, shallow = tmp_path / "deep", tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", str(origin), str(deep)],
                   capture_output=True, check=True)
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(origin), str(shallow)],
                   capture_output=True, check=True)

    paths = {"deep-repo": deep, "shallow-repo": shallow}
    original = m.repo_path
    m.repo_path = lambda r: paths.get(r, original(r))
    try:
        assert m.is_shallow("shallow-repo") is True, "a --depth 1 clone must read as shallow"
        assert m.is_shallow("deep-repo") is False, (
            "a complete clone must read as whole — a check that says 'shallow' for "
            "everything would pass the assertion above and mean nothing")
        assert m.is_shallow("no-such-repo") is False, (
            "a path that is not a git repo contributes nothing to the counts either "
            "way, so it must not be reported as truncated history")
    finally:
        m.repo_path = original


def test_build_reports_which_buckets_have_truncated_history(velocity_report, monkeypatch):
    """`degraded` is always present and names the affected buckets, so a renderer
    reads one field instead of inferring truncation from numbers that cannot show
    it. Orient's /velocity carried a hand-written note for exactly this.

    READS `build()`'s ACTUAL RETURN VALUE. The first version of this test called
    `shallow_repos()` and then re-derived the bucket filter with a copy of the
    comprehension inside `build()`, so it tested its own duplicate: a `build()`
    that hardcoded `[]`, dropped a key, or swapped the two fields would have left
    it green (reviewer, round 1)."""
    from datetime import datetime, timezone
    m = velocity_report
    monkeypatch.setattr(m, "BUCKETS", {"Truncated": ["a", "b"], "Whole": ["c"]})
    monkeypatch.setattr(m, "build_window", lambda *a, **k: {"buckets": {}, "total": {}})
    monkeypatch.setattr(m, "build_alltime", lambda *a, **k: {})
    monkeypatch.setattr(m, "attach_index", lambda *a, **k: None)
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(m, "is_shallow", lambda r: r == "b")
    deg = m.build(now)["degraded"]
    assert deg["shallow_repos"] == ["b"], "build() must report the shallow repo it found"
    assert deg["shallow_buckets"] == ["Truncated"], (
        "the bucket holding the shallow repo must be named, and the whole one must not — "
        "swapping the two fields or filtering the wrong way round has to fail here")

    monkeypatch.setattr(m, "is_shallow", lambda r: False)
    assert m.build(now)["degraded"] == {"shallow_repos": [], "shallow_buckets": []}, (
        "the healthy state must be present as empty lists rather than a missing field — "
        "a consumer has to be able to tell 'checked, all whole' from 'never checked'")


def test_adrs_minted_counts_against_main_not_the_checkouts_head(velocity_report,
                                                                monkeypatch, tmp_path):
    """~/Convivy/knowledge is a SHARED checkout every agent on this machine reads,
    and branch work in it is a known hazard with a written rescue procedure. This
    `git log` carried no ref until 2026-09-07, so it followed HEAD and counted an
    unmerged ADR as minted history — a number that lands on /velocity with nothing
    marking it degraded.

    RED against the unfixed script: with HEAD on the branch it returned 1."""
    m = velocity_report
    kb = tmp_path / "knowledge"
    run = _repo_with_real_history(kb)
    (kb / "README.md").write_text("x\n")
    run("add", "-A")
    run("commit", "-qm", "root", when="2026-09-01T00:00:00Z")

    adr = kb / "projects" / "platform" / "decisions"
    adr.mkdir(parents=True)
    run("checkout", "-q", "-b", "unmerged")
    (adr / "0099-not-merged-yet.md").write_text("# draft\n")
    run("add", "-A")
    run("commit", "-qm", "an ADR that only exists on a branch", when="2026-09-02T00:00:00Z")

    monkeypatch.setattr(m, "repo_path", lambda r: kb)
    monkeypatch.setattr(m, "main_ref", lambda r: "main")

    assert m.adrs_minted("2026-08-01T00:00:00Z") == {}, (
        "an ADR sitting on an unmerged branch is not minted history; counting it "
        "means the checkout's HEAD is being measured instead of main")

    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "merge the ADR", "unmerged",
        when="2026-09-03T00:00:00Z")
    assert m.adrs_minted("2026-08-01T00:00:00Z") == {"Engineering-internal": 1}, (
        "once merged to main the same ADR must count — otherwise this test would "
        "pass against a function that always returns nothing")

    # `until_iso` alongside the ref, the combination as-of history reconstruction
    # uses, proving the two coexist rather than one swallowing the other
    # (reviewer, round 1).
    #
    # THE BOUND IS AGAINST THE ADR COMMIT'S OWN DATE (2026-09-02), not the merge's
    # (2026-09-03). `git log --until` filters each commit by its own timestamp
    # while walking, so a bound between the two still admits it — the first
    # version of this assertion used 2026-09-02 and went red for that reason.
    assert m.adrs_minted("2026-08-01T00:00:00Z", "2026-09-01T12:00:00Z") == {}, (
        "an upper bound before the ADR's own commit must exclude it, ref still applied")


def test_arch_entries_counts_against_main_not_the_checkouts_head(velocity_report,
                                                                 monkeypatch, tmp_path):
    """Same defect, same fix, the other call site. ~/Convivy/substrate is a working
    checkout committed in directly."""
    m = velocity_report
    sub = tmp_path / "substrate"
    run = _repo_with_real_history(sub)
    # The root commit sits BEFORE the window, so main contributes nothing to the
    # count on its own and the assertion below is about the branch alone.
    (sub / "decisions.md").write_text("# log\n\n## ARCH-001 the first\n")
    run("add", "-A")
    run("commit", "-qm", "root", when="2026-01-01T00:00:00Z")

    run("checkout", "-q", "-b", "unmerged")
    (sub / "decisions.md").write_text("# log\n\n## ARCH-001 the first\n\n## ARCH-002 unmerged\n")
    run("add", "-A")
    run("commit", "-qm", "an ARCH entry on a branch", when="2026-09-02T00:00:00Z")

    monkeypatch.setattr(m, "repo_path", lambda r: sub)
    monkeypatch.setattr(m, "main_ref", lambda r: "main")

    assert m.arch_entries("2026-08-15T00:00:00Z") == 0, (
        "ARCH-002 is on an unmerged branch; counting it measures HEAD, not main")

    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "-m", "merge it", "unmerged", when="2026-09-03T00:00:00Z")
    assert m.arch_entries("2026-08-15T00:00:00Z") == 1, (
        "after the merge it must count — otherwise a function returning 0 forever "
        "would satisfy the assertion above")

    # Bounded before the ARCH entry's own commit (2026-09-02), for the reason
    # spelled out in the adrs_minted test above.
    assert m.arch_entries("2026-08-15T00:00:00Z", "2026-09-01T12:00:00Z") == 0, (
        "the same upper-bound check as adrs_minted: the ref and --until must both "
        "apply, rather than the newly-inserted ref displacing the bound")


def test_out_redirects_the_history_and_baseline_too(velocity_report, monkeypatch, tmp_path):
    """`--out` pointed only the snapshot at a scratch path while the history and
    baseline stayed module constants, so the obvious way to test this producer —
    run it to a temp file — appended to the live trend series.

    RED against the unfixed script on BOTH halves, which took two goes. The first
    version stubbed `_load_baseline` to return a dict unconditionally, so
    `baseline` was never None, the mint branch never ran, and the baseline
    assertion passed vacuously against fixed and unfixed alike — the test failed
    on the history assertion above it and the baseline one never executed
    (reviewer, round 1). Stubbing `compute_baseline` instead leaves the real
    `_load_baseline` reading the real path, so the mint branch runs and actually
    writes."""
    m = velocity_report
    live, scratch = tmp_path / "live", tmp_path / "scratch"
    live.mkdir()
    monkeypatch.setattr(m, "HISTORY_PATH", live / "velocity-history.jsonl")
    monkeypatch.setattr(m, "BASELINE_PATH", live / "velocity-baseline.json")

    w = {"total": {"velocity_index": {"score": 1.0}, "pr_stale": False}, "buckets": {}}
    monkeypatch.setattr(m, "build", lambda *a, **k: {
        "degraded": {"shallow_repos": [], "shallow_buckets": []},
        "windows": {str(m.INDEX_WINDOW): w},
    })
    # Only the expensive computation is stubbed. `_load_baseline` stays real, so it
    # reads the path main() hands it, finds nothing there, and the mint branch fires.
    monkeypatch.setattr(m, "compute_baseline", lambda *a, **k: {
        "created": "2026-09-07", "window_days": 90, "per_week": {}})

    assert m.main(["--out", str(scratch / "velocity.json")]) == 0

    assert (scratch / "velocity.json").exists(), "the snapshot went to the scratch path"
    assert (scratch / "velocity-history.jsonl").exists(), (
        "the history must follow --out, so a test run has a complete output set")
    assert (scratch / "velocity-baseline.json").exists(), (
        "the freshly minted baseline must follow --out too — without this the "
        "assertion below passes whether or not the baseline half was ever fixed")
    assert not (live / "velocity-history.jsonl").exists(), (
        "a run redirected with --out must not append to the live trend series")
    assert not (live / "velocity-baseline.json").exists(), (
        "nor re-freeze the live baseline")
