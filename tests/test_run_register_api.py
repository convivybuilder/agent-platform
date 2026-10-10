"""bin/run-register-api — the live active-runs register served for the Library
to pull from (the operator's 2026-08-20 direction: "chain pull — Orient/Deck from Chart
House, Chart House from Bosun"), plus the /sessions route added 2026-09-03 for
Orient's /crews (issue_01M1MBH9920RGQZMSES9BC1SB2). Only the pure payload
builders are unit-tested here, matching this repo's convention for the
bin/session-view-style socket servers: the HTTP/socket plumbing is thin IO
glue around a well-tested pure function, not itself unit tested (see
test_session_view.py, which tests lib/agent-platform/session_view.py's pure
functions and never bin/session-view's Handler/_serve_unix)."""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

import pytest

import private_crew
import projects
import session_lifecycle
import session_state


def _snapshot(run_register_api):
    """The per-request registry snapshot `Handler.do_GET` builds once (round
    4) and threads into `_runs_payload`/`_sessions_payload` as an argument —
    resolved directly here for tests that call those payload builders without
    going through `do_GET` itself."""
    return run_register_api._resolve_registry_snapshot()


def _add_run(registry, **overrides):
    entry = {
        "run_id": "run-1", "agent": "reviewer", "task": "review PR 1",
        "started_at": "2026-08-20T00:00:00Z", "repo": "agent-platform",
        "worktree": f"{Path.home()}/Convivy/worktrees/agent-platform.reviewer-1",
        "pid": os.getpid(),
    }
    entry.update(overrides)
    registry.add_run(entry)
    return entry


def test_runs_payload_reflects_the_live_register(run_register_api, runs_root):
    import registry
    _add_run(registry, run_id="run-1")
    _add_run(registry, run_id="run-2", parent_run="run-1")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["generated_at"]
    run_ids = {r["run_id"] for r in payload["runs"]}
    assert run_ids == {"run-1", "run-2"}


def test_runs_payload_serves_only_the_registers_own_fields(run_register_api, runs_root):
    """No cost, no model, no status/outcome — the register (and so this
    endpoint) holds only what registry.add_run's REQUIRED_ENTRY_FIELDS carry,
    per the Steward-coordinated scope: "the live process register and only
    that." A field creeping in here (e.g. from a future registry change) would
    be exactly the kind of scope growth this test is meant to catch."""
    import registry
    entry = _add_run(registry, run_id="run-1", parent_run="run-0")

    [row] = run_register_api._runs_payload(_snapshot(run_register_api))["runs"]

    assert set(row) == set(run_register_api.RUN_FIELDS) | {"model", "model_resolved", "model_source"}
    for key in ("run_id", "agent", "task", "started_at", "repo", "worktree", "parent_run"):
        assert row[key] == entry[key]
    assert row["pid"] == entry["pid"]


def test_runs_payload_drops_reserved_slot_internals(run_register_api, runs_root):
    """registry.reserve_slot's two-phase admission stores a transient
    "reserved": True on the entry until attach_pid fills in the real pid — an
    internal concurrency-admission detail, not part of this endpoint's
    documented 8-field contract. Security review on this PR (#417) found the
    original implementation passed registry.list_runs() through verbatim,
    which would have leaked this (and would leak whatever field registry.py
    gains next, with zero review here) — the fix is the explicit RUN_FIELDS
    allowlist this test pins."""
    import registry
    import telemetry
    registry.reserve_slot({
        # reserve_slot reaps a reserved entry once it's older than
        # registry.RESERVATION_TIMEOUT_SECONDS (60s) — a fixed past timestamp
        # would already read as stale by the time the real wall clock passes
        # it, so this needs to be "now" like the real caller (agentctl) sends.
        "run_id": "run-reserved", "agent": "reviewer", "task": "t",
        "started_at": telemetry.now_iso(), "repo": "agent-platform",
        "worktree": f"{Path.home()}/Convivy/worktrees/agent-platform.reviewer-1",
    })

    [row] = run_register_api._runs_payload(_snapshot(run_register_api))["runs"]

    assert "reserved" not in row
    assert row["pid"] is None
    assert set(row) == set(run_register_api.RUN_FIELDS) | {"model", "model_resolved", "model_source"}


def test_runs_payload_empty_register_is_an_empty_list_not_an_error(run_register_api, runs_root):
    payload = run_register_api._runs_payload(_snapshot(run_register_api))
    assert payload["runs"] == []
    assert payload["generated_at"]


def test_runs_payload_read_fresh_no_cache(run_register_api, runs_root):
    """A second call sees a run added between calls — no cache, no TTL, per the
    module's "read live, don't cache" contract (the /engineering/roster
    pattern)."""
    import registry
    assert run_register_api._runs_payload(_snapshot(run_register_api))["runs"] == []
    _add_run(registry, run_id="run-late")
    assert [r["run_id"] for r in run_register_api._runs_payload(_snapshot(run_register_api))["runs"]] == ["run-late"]


# --- /runs: a private crew's dispatch prompt must never reach this route -----


def test_a_private_crews_run_is_absent_from_runs_payload(
        run_register_api, runs_root, fake_home, projects_registry):
    """`_run_is_private` now answers through `projects.dispatch_is_private`
    (not `private_crew.is_private_slug`), so the registry is set up the same
    way the `/sessions` tests below set it up — via `projects_registry`,
    which owns `projects.PROJECTS_JSON` and its `lru_cache`, the reader this
    check actually goes through."""
    import registry
    projects_registry({
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
    })
    _add_run(registry, run_id="run-public", repo="agent-platform")
    _add_run(registry, run_id="run-private", repo="cfo",
             task="the private crew's actual dispatch prompt")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    run_ids = {r["run_id"] for r in payload["runs"]}
    assert run_ids == {"run-public"}
    assert "the private crew's actual dispatch prompt" not in json.dumps(payload)


def test_an_unreadable_registry_excludes_the_run_rather_than_including_it(
        run_register_api, runs_root, fake_home, monkeypatch, tmp_path):
    """`projects.dispatch_is_private` RAISES on an unreadable registry rather
    than returning a tri-state UNKNOWN (unlike `private_crew.is_private_slug`,
    which this route used before this fix) — `_run_is_private` must catch
    that and treat it as a confirmed-private answer, not let it propagate or
    default to public. `_runs_payload` no longer clears `projects._load_raw`'s
    cache itself (that moved to `Handler.do_GET` — round 3), so this test
    clears it directly, the same way `projects_registry`'s `_write` does,
    rather than relying on the payload builder to do it."""
    import registry
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "does-not-exist.json")
    projects._load_raw.cache_clear()
    _add_run(registry, run_id="run-1", repo="agent-platform")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []


def test_a_run_whose_repo_is_confirmed_public_is_served(
        run_register_api, runs_root, fake_home, projects_registry):
    """The positive control for the two tests above: the filter is a real
    gate, not a blanket drop — a repo the registry confirms is NOT private
    still comes through."""
    import registry
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    _add_run(registry, run_id="run-1", repo="agent-platform")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert [r["run_id"] for r in payload["runs"]] == ["run-1"]


def test_agents_dir_bypass_is_closed_for_runs_too(
        run_register_api, runs_root, fake_home, projects_registry):
    """THE SAME BYPASS `session_state._crew_is_shareable` already closed one
    route over (agent-platform#465, round 2): `resolve_agent()` (bin/agentctl)
    honours `--agents-dir` before it ever looks at `repo`, so `agentctl spawn
    <cfo-agent> --on some-public-repo --agents-dir
    ~/Convivy/CFO/.claude/agents` writes `repo: "some-public-repo"` (a public
    slug) into the registry while loading a black-box crew's agent. Round 1
    of this PR checked only `repo` and missed exactly this
    (security-reviewer, round 2). The real `meta.json` is written to disk
    here rather than mocking `_run_meta_path`'s lookup, so this exercises the
    actual read path `_run_is_private` now goes through."""
    import registry
    projects_registry({
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "some-public-repo": {"home_dir": "~/Convivy/some-public-repo"},
    })
    private_agents_dir = fake_home / "Convivy" / "CFO" / ".claude" / "agents"
    private_agents_dir.mkdir(parents=True)
    (fake_home / "Convivy" / "some-public-repo").mkdir(parents=True)

    started_at = "2026-08-29T10:00:00Z"
    local_day = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    run_dir = fake_home / "Convivy" / "agent-runs" / local_day / "run-bypass"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(
        {"agent_source": str(private_agents_dir / "cfo-classify.md")}))

    _add_run(registry, run_id="run-bypass", repo="some-public-repo",
             started_at=started_at,
             task="the private crew's actual dispatch prompt")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []
    assert "the private crew's actual dispatch prompt" not in json.dumps(payload)


def test_a_private_slug_with_no_home_directory_still_catches_the_bypass(
        run_register_api, runs_root, fake_home, projects_registry):
    """The round-5 regression: `_resolve_registry_snapshot` used to drop a
    slug from its `homes` dict whenever `home.is_dir()` was False, so
    `_run_is_private`'s call into `dispatch_is_private` got `None` back for
    `home_paths_snapshot.get("cfo")` and skipped the agent_source
    containment check entirely — the exact bypass rounds 2 and 4 exist to
    close, reopened for any private slug whose `home_dir` is not on disk
    right now (not yet created, or briefly moved). `cfo`'s home_dir is
    registered but its directory is never created here — every other bypass
    test in this file creates the directory first, which is why the suite
    could not see this (reviewer + security-reviewer, round 5)."""
    import registry
    projects_registry({
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "some-public-repo": {"home_dir": "~/Convivy/some-public-repo"},
    })
    (fake_home / "Convivy" / "some-public-repo").mkdir(parents=True)
    private_agents_dir = fake_home / "Convivy" / "CFO" / ".claude" / "agents"

    started_at = "2026-08-29T10:00:00Z"
    local_day = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    run_dir = fake_home / "Convivy" / "agent-runs" / local_day / "run-bypass"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(
        {"agent_source": str(private_agents_dir / "cfo-classify.md")}))

    _add_run(registry, run_id="run-bypass", repo="some-public-repo",
             started_at=started_at,
             task="the private crew's actual dispatch prompt")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []
    assert "the private crew's actual dispatch prompt" not in json.dumps(payload)


def test_a_private_slugs_unreadable_home_path_fails_the_whole_snapshot_closed(
        run_register_api, runs_root, fake_home, projects_registry):
    """`_resolve_registry_snapshot` used to `continue` past ANY slug whose
    `home_path()` raised, silently omitting that one crew's containment
    check while every other slug's still applied. For a PRIVATE slug this
    must fail the WHOLE snapshot instead — the same "cannot confirm public"
    answer an unreadable registry already produces — rather than serving
    `/runs` with one private crew's protection quietly missing
    (security-reviewer, round 5). `cfo`'s `home_dir` here is not `~/`-relative,
    which `projects._expand_own_home` raises on."""
    import registry
    projects_registry({
        "cfo": {"home_dir": "not-tilde-relative", "private": True},
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
    })
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _add_run(registry, run_id="run-1", repo="agent-platform")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []


def test_a_private_slugs_missing_home_dir_fails_the_whole_snapshot_closed(
        run_register_api, runs_root, fake_home, projects_registry):
    """`projects.home_path()` returns `None` — not a raise — for a registered
    slug whose `home_dir` key is missing or empty (its own docstring: "None
    means UNREGISTERED. A registered project whose `home_dir` is malformed
    raises instead, because returning None for both would let a crafted or
    corrupt registry read as 'no such project' and be skipped silently").
    `_resolve_registry_snapshot` used to `continue` past this `None` the same
    as an actually-unregistered slug, so a private slug missing `home_dir`
    landed in `private_slugs` but never in `homes`, and `dispatch_is_private`
    read `home_paths_snapshot.get("cfo")` as `None` and skipped the
    agent_source containment check — the same bypass the raise case (round 5,
    above) closes, just reached through the other branch (round 6). This must
    fail the WHOLE snapshot closed, the same as the raise case."""
    import registry
    projects_registry({
        "cfo": {"private": True},
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
    })
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _add_run(registry, run_id="run-1", repo="agent-platform")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []


def test_a_non_slug_shaped_repo_with_no_meta_is_excluded_not_served(
        run_register_api, runs_root, fake_home, projects_registry):
    """The exact non-exception path round 4's correctness review traced:
    `repo="../CFO"` fails `dispatch_is_private`'s exact `repo.lower() in
    private_slugs()` match, so the repo-only half never fires; this run
    carries no `run_id` `meta.json` either, so `agent_source` stays `None`
    and the agent_source half answers a clean `False` too — not an
    exception, so `_run_is_private`'s `except Exception` never fires either.
    Before the slug-shape guard, `dispatch_is_private` returned `False` and
    this run was served with its raw repo/task/worktree despite naming the
    private crew. `_REPO_SLUG_RE` now excludes it before `dispatch_is_private`
    is ever called."""
    import registry
    projects_registry({"cfo": {"home_dir": "~/Convivy/CFO", "private": True}})
    _add_run(registry, run_id="run-badslug", repo="../CFO",
             task="the private crew's actual dispatch prompt")

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []
    assert "the private crew's actual dispatch prompt" not in json.dumps(payload)


def test_the_runs_payload_privacy_filter_is_not_stale_for_the_daemons_uptime(
        run_register_api, runs_root, fake_home, tmp_path, projects_registry):
    """The `/runs` twin of
    `test_the_sessions_payload_privacy_filter_is_not_stale_for_the_daemons_uptime`
    below — and the one that actually proves the reload is INHERITED from
    `Handler.do_GET` rather than remembered by the payload builder's own call.
    That /sessions test would pass unchanged against round 2's shape, where
    `_sessions_payload` called the reload itself at its own top — it proves
    only "a reload happens somewhere before the read," not that today's route
    structurally inherits one. `_runs_payload` has never had a reload call of
    its own to fall back on, so THIS is the one that goes red if `do_GET`'s
    reload (or the round-4 snapshot resolve right after it) were ever
    removed. Drives the real request path via `_do_get`/`Handler.do_GET`,
    mutates the registry file directly mid-process, and requests `/runs`
    again in the same daemon process — reproducing the operator editing
    config/projects.json while the daemon keeps running."""
    import registry
    registry_path = projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _add_run(registry, run_id="run-1", repo="agent-platform")

    first = _do_get(run_register_api, "/runs")
    assert [r["run_id"] for r in first["runs"]] == ["run-1"]

    registry_path.write_text(
        json.dumps({"agent-platform": {"home_dir": "~/Convivy/agent-platform", "private": True}}),
        encoding="utf-8")

    second = _do_get(run_register_api, "/runs")
    assert second["runs"] == []


# --- /runs/<run_id>/facts: a run's review-authentication facts, so the
# Library can decide review-record run-verified status without trusting the
# review-post caller (issue_01M2K5HRWWVPWTC90NQTFEA8RK) -----------------------


def _facts_run_dir(fake_home, monkeypatch):
    """Point `telemetry.RUNS_ROOT` at the same `<fake_home>/Convivy/agent-runs`
    directory `session_state._run_meta_path` independently resolves via
    `builder_root.runs_root()` reading the `BUILDER_ROOT` env var `fake_home`
    sets — so `_facts_payload`'s run-dir lookup and `_run_is_private`'s own
    agent_source lookup agree on where a run's meta.json lives. `telemetry.
    RUNS_ROOT` is bound once at import (unlike `BR.runs_root()`, re-read every
    call), so this is monkeypatched explicitly rather than relying on the env
    var alone, the same way `test_agentctl.py`'s `_resolve_run_facts` tests do."""
    import telemetry
    root = fake_home / "Convivy" / "agent-runs"
    monkeypatch.setattr(telemetry, "RUNS_ROOT", root)
    return telemetry


def test_facts_route_serves_an_ended_runs_facts_through_the_real_handler(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """/runs (registry.list_runs()) holds only LIVE runs; this run is never
    added to the registry at all, proving the route resolves the run
    directory directly rather than through it — the shape the Library's
    write route needs, since a reviewer run has almost always already ended
    by the time a review record is written."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-ended1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-ended1", agent="reviewer", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6",
                         review_sha="a" * 40, pr=42)

    payload = _do_get(run_register_api, "/runs/run-ended1/facts")

    assert payload == {"found": True, "agent": "reviewer", "repo": "agent-platform",
                       "review_sha": "a" * 40, "pr": 42, "review_role": "reviewer"}


def test_facts_route_rejects_an_unknown_id_and_every_traversal_shape(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """`review_protocol.find_run_dir` validates the run-id shape BEFORE any
    filesystem access, so a real run existing on disk must not leak through a
    malformed id naming it by coincidence of prefix/suffix. Each shape is
    driven through `Handler.do_GET` (not the pure `resolve_run_facts` alone),
    proving the route's own path-to-run_id extraction never decodes or
    normalizes an id into something that WOULD resolve."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    telemetry.create_run_dir("run-real", date="2026-09-01")  # a date dir to walk past

    for run_id in ("run-does-not-exist", "..", "../etc", "%2e%2e%2f", "/etc/passwd"):
        payload = _do_get(run_register_api, f"/runs/{run_id}/facts")
        assert payload == {"found": False}, run_id

    # An empty run-id can't be represented as a non-empty URL path segment, so
    # this one shape is driven straight through the payload builder `do_GET`
    # itself calls — still the real handler code, not a re-implementation of it.
    assert run_register_api._facts_payload("", _snapshot(run_register_api)) == {"found": False}


def test_facts_route_refuses_a_run_id_planted_under_two_date_dirs(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A run can `mkdir <runs>/2099-01-01/<run-id>` alongside its own real
    directory (issue_01M1JWB7M75SZ160VRJRCGARBF) — the route must report
    `found: False` rather than authenticating the Library's write against
    whichever directory `review_protocol.find_run_dir` would have picked."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-dup-facts", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-dup-facts", agent="reviewer", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6",
                         review_sha="a" * 40, pr=42)
    # The plant carries its own readable (forged) meta.json — sorting ahead of the
    # real directory under the old first-match resolver — so a resolver that picked
    # it (instead of refusing) would return ITS facts rather than `found: False`.
    planted = telemetry.create_run_dir("run-dup-facts", date="2099-01-01")
    telemetry.write_meta(planted, run_id="run-dup-facts", agent="reviewer", task="t",
                         started_at="2099-01-01T00:00:00Z", repo="agent-platform",
                         worktree=str(planted), profile="p", model="claude-sonnet-4-6",
                         review_sha="b" * 40, pr=99)

    payload = _do_get(run_register_api, "/runs/run-dup-facts/facts")

    assert payload == {"found": False}


def test_facts_route_reveals_only_found_and_private_for_a_private_crews_run(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """Applied through `_run_is_private`, the exact function `_runs_payload`
    already filters every `/runs` row through — never a copy of that check."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"cfo": {"home_dir": "~/Convivy/CFO", "private": True}})
    (fake_home / "Convivy" / "CFO").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-private1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-private1", agent="reviewer", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="cfo",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6",
                         review_sha="b" * 40, pr=7)

    payload = _do_get(run_register_api, "/runs/run-private1/facts")

    assert payload == {"found": True, "private": True}


def test_facts_route_returns_an_empty_review_sha_for_a_legacy_meta_with_none(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A run predating ARCH-184's `review_sha` field carries meta.json with no
    such key at all (`T.write_meta` always writes the key, even as `null`, so
    this is hand-written to reproduce a genuinely pre-field record).
    `review_protocol.resolve_run_facts` coerces the missing value to `""` —
    never `None` — because `authenticate()` skips empty values, not falsy
    ones, and a served `null` would round-trip as the JSON `null` rather than
    the empty string `authenticate()`'s skip check expects."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.RUNS_ROOT / "2026-01-01" / "run-legacy1"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-legacy1", "agent": "reviewer", "task": "t",
        "started_at": "2026-01-01T00:00:00Z", "repo": "agent-platform",
    }), encoding="utf-8")

    payload = _do_get(run_register_api, "/runs/run-legacy1/facts")

    assert payload == {"found": True, "agent": "reviewer", "repo": "agent-platform",
                       "review_sha": "", "pr": None, "review_role": "reviewer"}


def _write_run(telemetry, run_id, *, agent, date="2026-09-01", repo="agent-platform"):
    run_dir = telemetry.create_run_dir(run_id, date=date)
    telemetry.write_meta(run_dir, run_id=run_id, agent=agent, task="t",
                         started_at=f"{date}T00:00:00Z", repo=repo,
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6",
                         review_sha="c" * 40, pr=1)


def test_facts_route_review_role_is_the_bare_role_for_a_bare_reviewer(
        run_register_api, fake_home, projects_registry, monkeypatch):
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _write_run(telemetry, "run-rev1", agent="reviewer")

    payload = _do_get(run_register_api, "/runs/run-rev1/facts")

    assert payload["review_role"] == "reviewer"


def test_facts_route_review_role_is_the_base_role_for_a_crew_prefixed_reviewer(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A crew-prefixed reviewer (`hollis-` is one of `review_protocol.
    INTERNAL_APP_PREFIXES`) is review-class the same way `authenticate()` treats it
    (ARCH-177) — `review_role` reports the stripped BASE role, `reviewer`, not the
    prefixed run name, so the Library can compare it against the bare role
    `evaluate_gate` and `authenticate()` both key on."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _write_run(telemetry, "run-rev2", agent="hollis-reviewer")

    payload = _do_get(run_register_api, "/runs/run-rev2/facts")

    assert payload["agent"] == "hollis-reviewer"
    assert payload["review_role"] == "reviewer"


def test_facts_route_review_role_for_a_security_reviewer(
        run_register_api, fake_home, projects_registry, monkeypatch):
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _write_run(telemetry, "run-sec1", agent="security-reviewer")

    payload = _do_get(run_register_api, "/runs/run-sec1/facts")

    assert payload["review_role"] == "security-reviewer"


def test_facts_route_review_role_is_empty_for_a_non_review_agent(
        run_register_api, fake_home, projects_registry, monkeypatch):
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    _write_run(telemetry, "run-coder1", agent="coder")

    payload = _do_get(run_register_api, "/runs/run-coder1/facts")

    assert payload["review_role"] == ""


# --- /runs/<run_id>/facts and /runs: a run's OWN writable directory must not
# be able to turn a request into a denial-of-service against this KeepAlive
# daemon (round-2 security review on PR #593, run-21e68140). Each hostile
# shape below is driven through the real `do_GET` handler, and each is
# followed by a normal request proving the daemon still serves everything
# else afterward — the property that actually matters for a long-lived
# process.


def test_facts_route_returns_unreadable_promptly_for_a_symlinked_meta_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """`rm meta.json && ln -s /dev/zero meta.json` in the run's own directory.
    A plain `Path.read_text()` (what this route called before this fix) reads
    /dev/zero forever; this must return `{"found": False}` immediately."""
    import os as _os
    import signal
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-symlink1", date="2026-09-01")
    _os.symlink("/dev/zero", run_dir / "meta.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("/runs/<id>/facts hung on a symlinked meta.json")))
    signal.alarm(10)
    try:
        payload = _do_get(run_register_api, "/runs/run-symlink1/facts")
    finally:
        signal.alarm(0)

    assert payload == {"found": False}


def test_facts_route_returns_unreadable_promptly_for_a_fifo_meta_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """`rm meta.json && mkfifo meta.json` with no writer — a plain `open()`
    blocks at the syscall forever."""
    import os as _os
    import signal
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-fifo1", date="2026-09-01")
    _os.mkfifo(run_dir / "meta.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("/runs/<id>/facts hung on a FIFO meta.json")))
    signal.alarm(10)
    try:
        payload = _do_get(run_register_api, "/runs/run-fifo1/facts")
    finally:
        signal.alarm(0)

    assert payload == {"found": False}


def test_facts_route_returns_unreadable_for_a_meta_json_over_the_size_cap(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A many-gigabyte regular file is the shape a real attack would use; one
    byte over `review_protocol._MAX_META_BYTES` is the version a test can
    afford to write."""
    import review_protocol as RP
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-huge1", date="2026-09-01")
    oversized = json.dumps({
        "agent": "reviewer", "repo": "agent-platform", "review_sha": "c" * 40,
        "pad": "x" * RP._MAX_META_BYTES,
    })
    assert len(oversized) > RP._MAX_META_BYTES
    (run_dir / "meta.json").write_text(oversized, encoding="utf-8")

    payload = _do_get(run_register_api, "/runs/run-huge1/facts")

    assert payload == {"found": False}


def test_facts_route_returns_unreadable_for_a_directory_at_meta_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-dir1", date="2026-09-01")
    (run_dir / "meta.json").mkdir()

    payload = _do_get(run_register_api, "/runs/run-dir1/facts")

    assert payload == {"found": False}


def test_a_normal_facts_request_still_succeeds_after_each_hostile_meta_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """The positive control the four tests above need: none of the hostile
    shapes leaves process-global state (an open fd, a stuck handler thread)
    that degrades a LATER, well-formed request on this same daemon process —
    the property that actually matters for a KeepAlive server."""
    import os as _os
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)

    symlink_dir = telemetry.create_run_dir("run-hostile-symlink", date="2026-09-01")
    _os.symlink("/dev/zero", symlink_dir / "meta.json")
    fifo_dir = telemetry.create_run_dir("run-hostile-fifo", date="2026-09-01")
    _os.mkfifo(fifo_dir / "meta.json")
    dir_dir = telemetry.create_run_dir("run-hostile-dir", date="2026-09-01")
    (dir_dir / "meta.json").mkdir()

    for run_id in ("run-hostile-symlink", "run-hostile-fifo", "run-hostile-dir"):
        assert _do_get(run_register_api, f"/runs/{run_id}/facts") == {"found": False}

    run_dir = telemetry.create_run_dir("run-normal1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-normal1", agent="reviewer", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6",
                         review_sha="e" * 40, pr=3)

    payload = _do_get(run_register_api, "/runs/run-normal1/facts")

    assert payload == {"found": True, "agent": "reviewer", "repo": "agent-platform",
                       "review_sha": "e" * 40, "pr": 3, "review_role": "reviewer"}


def test_runs_payload_survives_a_private_runs_symlinked_meta_json_and_still_serves_the_rest(
        run_register_api, runs_root, fake_home, projects_registry, monkeypatch):
    """`_run_is_private`'s own `meta.json` read (for the `agent_source`
    `--agents-dir` bypass check) is the WIDER exposure: it runs on every row
    of every `/runs` request, for any currently-registered run, not only one
    a caller names by run-id. A registered PRIVATE crew's run whose
    `meta.json` is a symlink to `/dev/zero` must not hang or crash `/runs` —
    it stays excluded (the repo itself is private, checked before
    `agent_source` is ever consulted, so a symlinked meta.json cannot smuggle
    it through), and a normal PUBLIC run in the same register is still
    served."""
    import os as _os
    import signal
    import registry
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
    })
    (fake_home / "Convivy" / "CFO").mkdir(parents=True)
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)

    # `_run_meta_path` (unlike the facts route's glob-based `find_run_dir`)
    # locates the run directory by DAY, computed from `started_at` in local
    # time — the same local-day arithmetic `test_agents_dir_bypass_is_closed_
    # for_runs_too` above computes explicitly, for the same reason.
    started_at = "2026-08-29T10:00:00Z"
    local_day = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    hostile_dir = telemetry.create_run_dir("run-hostile-symlink2", date=local_day)
    _os.symlink("/dev/zero", hostile_dir / "meta.json")
    _add_run(registry, run_id="run-hostile-symlink2", repo="cfo",
             started_at=started_at,
             task="the private crew's actual dispatch prompt")
    _add_run(registry, run_id="run-normal2", repo="agent-platform", started_at=started_at)

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("/runs hung reading a registered run's symlinked meta.json")))
    signal.alarm(10)
    try:
        payload = run_register_api._runs_payload(_snapshot(run_register_api))
    finally:
        signal.alarm(0)

    assert [r["run_id"] for r in payload["runs"]] == ["run-normal2"]
    assert "the private crew's actual dispatch prompt" not in json.dumps(payload)


# --- /runs/<run_id>/docs_fetched: the documents a LIVE dispatch has read so
# far, for Deck's Dispatched panel (issue_01M3BFGKYNHFN29R43PAZXRRHV) --------


def _append_tool_call_line(run_dir, line: str) -> None:
    """Append one raw line (with its own trailing newline) directly to
    `tool_calls.jsonl` — not `telemetry.append_tool_call`, so a test can
    write a deliberately truncated final line (no trailing newline, cut
    mid-value) the real writer would never produce."""
    with (run_dir / "tool_calls.jsonl").open("a") as f:
        f.write(line)


def test_docs_fetched_route_serves_exactly_the_kb_and_signal_fetches_with_ids_and_times(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A live run's tool_calls.jsonl holding a kb_get, a signal_get, a
    non-fetch tool call (Read) and a truncated last line: the route serves
    exactly the two fetches, each with its id and when it was read, and
    tolerates the truncated line rather than erroring on it. No
    docs_consulted.json, no outcome.json — this route works entirely off the
    live tool_calls.jsonl, unlike the finalize-time derivation in
    bin/agentctl."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-live-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-live-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")

    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-42"}, "result_summary": "found",
        "duration_ms": 8, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:03:00Z", "tool_name": "Read",
        "input": {"file_path": "/tmp/x"}, "result_summary": "ok",
        "duration_ms": 3, "is_error": False}) + "\n")
    # A truncated last line — the file is still being appended to. No
    # trailing newline, cut mid-value.
    _append_tool_call_line(run_dir, '{"timestamp": "2026-09-01T00:04:00Z", "tool_na')

    payload = _do_get(run_register_api, "/runs/run-live-docs1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": "2026-09-01T00:01:00Z"},
        {"id": "sig-42", "kind": "signal", "fetched_at": "2026-09-01T00:02:00Z"},
    ], "searches": []}


def test_docs_fetched_route_serves_nothing_for_an_unknown_run_id(run_register_api):
    payload = _do_get(run_register_api, "/runs/run-does-not-exist/docs_fetched")
    assert payload == {"found": False}


def test_a_private_runs_docs_fetched_route_serves_nothing(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """Applied through `_run_is_private`, the same function `_facts_payload`
    and `_runs_payload` already filter every row through — never a copy of
    that check. A private crew's fetched document id must not leak even
    though it names no task text itself: the kb id SENTINEL below never
    reaches the response."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"cfo": {"home_dir": "~/Convivy/CFO", "private": True}})
    (fake_home / "Convivy" / "CFO").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-private-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-private-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="cfo",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "SENTINEL-cfo-only-doc"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-private-docs1/docs_fetched")

    assert payload == {"found": True, "private": True}
    assert "SENTINEL-cfo-only-doc" not in json.dumps(payload)


def test_docs_fetched_grows_when_a_line_is_appended_without_any_finalize(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """The live-ness this route exists for: a second fetch appended to
    tool_calls.jsonl shows up on the next request with no outcome.json, no
    docs_consulted.json rewrite, no finalize at all — unlike
    bin/agentctl's `_finalize_run`, which only derives `queried_at_runtime`
    once the run has already ended."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-growing-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-growing-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")

    first = _do_get(run_register_api, "/runs/run-growing-docs1/docs_fetched")
    assert [d["id"] for d in first["docs_fetched"]] == ["shared/agents/architect"]
    assert not (run_dir / "outcome.json").exists()

    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-99"}, "result_summary": "found",
        "duration_ms": 8, "is_error": False}) + "\n")

    second = _do_get(run_register_api, "/runs/run-growing-docs1/docs_fetched")
    assert [d["id"] for d in second["docs_fetched"]] == ["shared/agents/architect", "sig-99"]


def test_docs_fetched_route_survives_a_deeply_nested_an_invalid_utf8_and_a_non_object_line(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """Three adversarial lines a run's own writable tool_calls.jsonl can hold
    — a deeply nested line (RecursionError), a line with an invalid UTF-8
    byte sequence (UnicodeDecodeError), and a line that parses clean but to
    a non-object (names no tool call) — interspersed among two genuine
    fetches. Before round 2's fix, `_iter_tool_call_lines` caught only
    `json.JSONDecodeError`, so the first two lines crashed this route with no
    response at all (security-reviewer, round 1 of agent-platform#742)."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-adversarial-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-adversarial-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")

    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")
    # A deeply nested line: json.loads raises RecursionError, not JSONDecodeError.
    _append_tool_call_line(run_dir, ("[" * 3000) + ("]" * 3000) + "\n")
    # An invalid UTF-8 byte sequence embedded in an otherwise JSON-shaped
    # line: json.loads decodes bytes itself before parsing, and this raises
    # UnicodeDecodeError, not JSONDecodeError — a leading raw invalid byte
    # instead gets rejected as "not JSON" (JSONDecodeError) before decoding
    # even starts, which would not exercise this path.
    with (run_dir / "tool_calls.jsonl").open("ab") as f:
        f.write(b'{"tool_name": "mcp__kb__kb_get", "input": {"id": "\xff"}}\n')
    # Parses clean, but to a non-object — names no tool call.
    _append_tool_call_line(run_dir, "[1, 2, 3]\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-42"}, "result_summary": "found",
        "duration_ms": 8, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-adversarial-docs1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": "2026-09-01T00:01:00Z"},
        {"id": "sig-42", "kind": "signal", "fetched_at": "2026-09-01T00:02:00Z"},
    ], "searches": []}


def test_docs_fetched_route_excludes_a_kb_get_miss_a_leased_checkout_and_a_signal_error(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """Record shapes copied from the real pipeline, not invented: `kb_get`
    returns Python None for an unindexed id, which FastMCP's
    `_convert_to_content` turns into an empty content-block list, which
    `bin/agentctl` joins to the empty string — `result_summary: ""`.
    `kb_checkout` always returns a dict; `bin/mcp-kb-server`'s `_run_kb`
    builds `{"ok": False, "exit_code": 3, ...}` for a lease conflict (exit
    code 3 named "leased"). `signal_get` always returns a JSON string;
    `bin/mcp-ops-server`'s `_call` returns `{"error": ...}` for an unknown
    id. None of these three set `is_error` — Claude Code's transcript only
    sets that from a genuine tool-level exception, and none of these three
    raise (reviewer, round 1 of agent-platform#742). A successful kb_get
    alongside them proves they are excluded for being misses, not because
    every kb_get call is dropped."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-miss-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-miss-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")

    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/no-such-doc"}, "result_summary": "",
        "duration_ms": 5, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__kb__kb_checkout",
        "input": {"id": "shared/agents/architect"},
        "result_summary": json.dumps({
            "ok": False, "exit_code": 3, "stdout": "",
            "stderr": "leased by alice until 2026-09-01T01:00:00Z",
            "error": "leased"}, indent=2),
        "duration_ms": 9, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:03:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-does-not-exist"},
        "result_summary": json.dumps({"error": "HTTP 404: Not Found"}),
        "duration_ms": 6, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:04:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"},
        "result_summary": json.dumps({
            "id": "shared/agents/architect", "project": "shared", "kind": "agent",
            "body": "..."}, indent=2),
        "duration_ms": 7, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-miss-docs1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": "2026-09-01T00:04:00Z"},
    ], "searches": []}


def test_docs_fetched_route_drops_an_oversized_or_control_character_id(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A run's own writable tool_calls.jsonl can hold any `input.id` it
    chooses to write, not just one a real kb_get/kb_checkout call produced.
    A multi-kilobyte id, or one holding a C0 control character or DEL, is
    dropped rather than served in this daemon's JSON response — a real id is a
    short printable token (`project/kind/slug`), nowhere near either shape
    (security-reviewer, round 1 of agent-platform#742)."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-oversized-id-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-oversized-id-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")

    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "x" * 2000}, "result_summary": "found",
        "duration_ms": 5, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-\x01-control"}, "result_summary": "found",
        "duration_ms": 5, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:03:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 5, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-oversized-id-docs1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": "2026-09-01T00:03:00Z"},
    ], "searches": []}
    assert not (run_dir / "outcome.json").exists()


def _docs_run_dir(fake_home, monkeypatch, projects_registry, run_id, repo="agent-platform",
                  private=False):
    """A run directory for `run_id` under a registered `repo` (private when
    asked), with its meta.json written. The registry entry's home directory
    is created too, since the route's privacy check resolves it."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    entry = {"home_dir": f"~/Convivy/{repo}"}
    if private:
        entry["private"] = True
    projects_registry({repo: entry})
    (fake_home / "Convivy" / repo).mkdir(parents=True)
    run_dir = telemetry.create_run_dir(run_id, date="2026-09-01")
    telemetry.write_meta(run_dir, run_id=run_id, agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo=repo,
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    return run_dir


def test_docs_fetched_route_lists_each_search_with_its_query_in_call_order(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """One kb_search and one signal_search: the route serves both as rows in
    call order, each with the query it was made with and when it ran. No
    document is served, since a search returns snippets and adds nothing to
    `docs_fetched`."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-order1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "deploy area cutover"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_search",
        "input": {"query": "reliability payload", "limit": 10}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-order1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "kb_search", "query": "deploy area cutover", "searched_at": "2026-09-01T00:01:00Z"},
        {"tool": "signal_search", "query": "reliability payload", "searched_at": "2026-09-01T00:02:00Z"},
    ]}


def test_a_search_leaves_docs_fetched_as_it_was_for_a_run_that_also_fetched(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A kb_search whose result names a document the run never fetched: that
    id stays out of `docs_fetched`, which holds exactly the two genuine
    fetches, as it did before searches were listed at all."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-fetch1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "architect"},
        "result_summary": json.dumps({"result": [
            {"id": "shared/agents/snippet-only-doc", "snippet": "…"}]}),
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:03:00Z", "tool_name": "mcp__ops__signal_get",
        "input": {"signal_id": "sig-42"}, "result_summary": "found",
        "duration_ms": 8, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:04:00Z", "tool_name": "mcp__ops__signal_search",
        "input": {"query": "reliability"}, "result_summary": "{}",
        "duration_ms": 8, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-fetch1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": "2026-09-01T00:01:00Z"},
        {"id": "sig-42", "kind": "signal", "fetched_at": "2026-09-01T00:03:00Z"},
    ], "searches": [
        {"tool": "kb_search", "query": "architect", "searched_at": "2026-09-01T00:02:00Z"},
        {"tool": "signal_search", "query": "reliability", "searched_at": "2026-09-01T00:04:00Z"},
    ]}


def test_a_search_without_a_string_query_serves_a_null_query_and_a_non_dict_input_is_skipped(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A filters-only kb_search (no `query` key) and one whose `query` is not
    a string both serve `query: null`: the search still happened, so its row
    stays. A search whose recorded `input` is not a dict names nothing and is
    skipped, the same as a malformed fetch."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-null1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"kind": "decision", "project": "platform"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": 7}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:03:00Z", "tool_name": "mcp__kb__kb_search",
        "input": "not-a-dict", "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-null1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "kb_search", "query": None, "searched_at": "2026-09-01T00:01:00Z"},
        {"tool": "kb_search", "query": None, "searched_at": "2026-09-01T00:02:00Z"},
    ]}


def test_an_over_long_search_query_is_truncated_to_512_characters(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A 600-character query is served as its first 512 characters, so a run
    cannot make this daemon's response carry an unbounded query string."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-long1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "a" * 600}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-long1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "kb_search", "query": "a" * 512, "searched_at": "2026-09-01T00:01:00Z"},
    ]}


def test_a_control_character_in_a_search_query_is_replaced_with_a_space(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A newline, a NUL and a DEL in a query each become one space, so the
    row stays and its words stay apart, with no C0 control character or DEL in
    the served string."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-ctrl1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "deploy\nwatch\x00area\x7fend"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-ctrl1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "kb_search", "query": "deploy watch area end",
         "searched_at": "2026-09-01T00:01:00Z"},
    ]}


def test_an_errored_search_is_absent_from_searches(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A kb_search that raised (`is_error` true) returned nothing and is
    absent; the successful search after it is served."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-err1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "failing query"},
        "result_summary": "Error executing tool kb_search: database disk image is malformed",
        "duration_ms": 350, "is_error": True}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "working query"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-err1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "kb_search", "query": "working query", "searched_at": "2026-09-01T00:02:00Z"},
    ]}


def test_an_in_band_error_search_is_absent_from_searches(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A signal_search with a negative offset returns `{"error": ...}` as an
    ordinary result, so `is_error` stays false. It returned no search, so its
    row is absent; the successful search after it is served."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-band1")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__ops__signal_search",
        "input": {"query": "bad offset", "offset": -1},
        "result_summary": json.dumps({"error": "offset must be non-negative"}),
        "duration_ms": 8, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:02:00Z", "tool_name": "mcp__ops__signal_search",
        "input": {"query": "good offset", "offset": 0},
        "result_summary": json.dumps({"count": 0, "items": []}),
        "duration_ms": 8, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-band1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [], "searches": [
        {"tool": "signal_search", "query": "good offset", "searched_at": "2026-09-01T00:02:00Z"},
    ]}


def test_an_over_long_timestamp_is_served_as_null_on_a_search_row_and_a_fetch_row(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A run writes its own tool_calls.jsonl, so the timestamp it records can
    be any length. A 1000-character one is served as null on the search row
    (`searched_at`) and on the fetch row (`fetched_at`), and both rows are
    kept: the calls still happened."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-ts-long1")
    long_ts = "2026-09-01T" + "0" * 1000
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": long_ts, "tool_name": "mcp__kb__kb_search",
        "input": {"query": "deploy"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": long_ts, "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-ts-long1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": None},
    ], "searches": [
        {"tool": "kb_search", "query": "deploy", "searched_at": None},
    ]}


def test_a_control_character_timestamp_is_served_as_null_on_a_search_row_and_a_fetch_row(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A timestamp with a newline in it is served as null on both rows, and
    both rows are kept. The newline would otherwise reach the response as an
    escape sequence in the timestamp's text."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-ts-ctrl1")
    ctrl_ts = "2026-09-01T00:01:00Z\nforged"
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": ctrl_ts, "tool_name": "mcp__kb__kb_search",
        "input": {"query": "deploy"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": ctrl_ts, "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-ts-ctrl1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": None},
    ], "searches": [
        {"tool": "kb_search", "query": "deploy", "searched_at": None},
    ]}


def test_a_normal_timestamp_is_served_unchanged_on_a_search_row_and_a_fetch_row(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A fractional-second timestamp with a numeric offset (32 characters, no
    control character) is under the cap, so both rows serve it as written."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-ts-ok1")
    ts = "2026-09-01T00:01:00.123456+00:00"
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": ts, "tool_name": "mcp__kb__kb_search",
        "input": {"query": "deploy"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": ts, "tool_name": "mcp__kb__kb_get",
        "input": {"id": "shared/agents/architect"}, "result_summary": "found",
        "duration_ms": 12, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-ts-ok1/docs_fetched")

    assert payload == {"found": True, "docs_fetched": [
        {"id": "shared/agents/architect", "kind": "knowledge", "fetched_at": ts},
    ], "searches": [
        {"tool": "kb_search", "query": "deploy", "searched_at": ts},
    ]}


def test_a_private_runs_answer_carries_no_searches_key(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A private crew's search query is free text the agent typed, so it is
    never served: the private answer has no `searches` key, and the sentinel
    query below reaches neither the payload nor its JSON."""
    run_dir = _docs_run_dir(fake_home, monkeypatch, projects_registry, "run-search-private1",
                            repo="cfo", private=True)
    _append_tool_call_line(run_dir, json.dumps({
        "timestamp": "2026-09-01T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "SENTINEL-cfo-search-query"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    payload = _do_get(run_register_api, "/runs/run-search-private1/docs_fetched")

    assert payload == {"found": True, "private": True}
    assert "SENTINEL-cfo-search-query" not in json.dumps(payload)


def test_the_route_resolves_the_run_directory_once(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """The route calls `find_run_dir` once. The run-id resolves to two
    directories: a public run on the first lookup, and a private run carrying
    a SENTINEL search on any later one. A second lookup would judge the public
    directory and then read the private one, serving the SENTINEL query. This
    pins the lookup count and that the SENTINEL is not served. It does not show
    that the two file opens cannot diverge; both are still by path (see the
    route docstring)."""
    import review_protocol
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
    })
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    (fake_home / "Convivy" / "CFO").mkdir(parents=True)
    public_dir = telemetry.create_run_dir("run-swap-docs1", date="2026-09-01")
    telemetry.write_meta(public_dir, run_id="run-swap-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(public_dir), profile="p", model="claude-sonnet-4-6")
    private_dir = telemetry.create_run_dir("run-swap-docs1", date="2026-09-02")
    telemetry.write_meta(private_dir, run_id="run-swap-docs1", agent="coder", task="t",
                         started_at="2026-09-02T00:00:00Z", repo="cfo",
                         worktree=str(private_dir), profile="p", model="claude-sonnet-4-6")
    _append_tool_call_line(private_dir, json.dumps({
        "timestamp": "2026-09-02T00:01:00Z", "tool_name": "mcp__kb__kb_search",
        "input": {"query": "SENTINEL-cfo-swapped-query"}, "result_summary": "{}",
        "duration_ms": 30, "is_error": False}) + "\n")

    lookups = []

    def first_lookup_public_then_private(run_id, runs_root=None):
        lookups.append(run_id)
        return public_dir if len(lookups) == 1 else private_dir

    monkeypatch.setattr(review_protocol, "find_run_dir", first_lookup_public_then_private)

    payload = _do_get(run_register_api, "/runs/run-swap-docs1/docs_fetched")

    assert len(lookups) == 1
    assert payload == {"found": True, "docs_fetched": [], "searches": []}
    assert "SENTINEL-cfo-swapped-query" not in json.dumps(payload)


# --- /runs/<run_id>/docs_given: the documents a dispatch run was GIVEN at
# dispatch time, for Deck's "Documents it was given" pane
# (issue_01M464MZ31N7AFTBHRE6SXVQ6X) ------------------------------------------


def test_docs_given_route_serves_nothing_for_an_unknown_run_id(run_register_api):
    payload = _do_get(run_register_api, "/runs/run-does-not-exist/docs_given")
    assert payload == {"found": False}


def test_a_private_runs_docs_given_route_serves_nothing(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """Applied through `_run_is_private`, the same function `_facts_payload`
    and `_docs_fetched_payload` already filter every row through — never a
    copy of that check. A private crew's given document id must not leak."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"cfo": {"home_dir": "~/Convivy/CFO", "private": True}})
    (fake_home / "Convivy" / "CFO").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-private-docs-given1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-private-docs-given1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="cfo",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    telemetry.write_docs_consulted(
        run_dir, profile="p", individuated_agents=["coder"], individuated_skills=[],
        queried_at_runtime=[],
        materialized_documents=[{"id": "SENTINEL-cfo-given-doc", "kind": "agent",
                                  "body_sha256": "a" * 64}])

    payload = _do_get(run_register_api, "/runs/run-private-docs-given1/docs_given")

    assert payload == {"found": True, "private": True}
    assert "SENTINEL-cfo-given-doc" not in json.dumps(payload)


def test_docs_given_route_serves_the_runs_docs_consulted_json_unchanged(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """The route serves `docs_consulted.json`'s own object verbatim, `docs`
    key included — never a re-shaped projection of it."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-given-docs1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-given-docs1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    telemetry.write_docs_consulted(
        run_dir, profile="p", individuated_agents=["coder"], individuated_skills=["skill-a"],
        queried_at_runtime=[{"query": "q", "searched_at": "2026-09-01T00:01:00Z"}],
        materialized_documents=[
            {"id": "shared/agents/coder", "kind": "agent", "body_sha256": "b" * 64},
            {"id": "shared/instructions/x", "kind": "instruction", "body_sha256": "c" * 64,
             "scope": "repo"},
        ])
    on_disk = json.loads((run_dir / "docs_consulted.json").read_text())

    payload = _do_get(run_register_api, "/runs/run-given-docs1/docs_given")

    assert payload == {"found": True, "docs_consulted": on_disk}
    assert payload["docs_consulted"]["docs"][1]["scope"] == "repo"


def test_docs_given_route_serves_null_for_an_absent_docs_consulted_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """A dispatch still running, or whose render was skipped, has written no
    `docs_consulted.json` yet — served as `null`, not `{"found": False}` (the
    run itself is found; only the file is missing) and not an empty dict."""
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-no-docs-given1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-no-docs-given1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    assert not (run_dir / "docs_consulted.json").exists()

    payload = _do_get(run_register_api, "/runs/run-no-docs-given1/docs_given")

    assert payload == {"found": True, "docs_consulted": None}


def test_docs_given_route_returns_unreadable_promptly_for_a_symlinked_docs_consulted_json(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """`rm docs_consulted.json && ln -s /dev/zero docs_consulted.json` in the
    run's own directory. A plain `Path.read_text()` reads /dev/zero forever;
    this must return `docs_consulted: None` immediately, the same refusal
    `RP._read_meta_bounded` already gives a symlinked `meta.json`."""
    import os as _os
    import signal
    telemetry = _facts_run_dir(fake_home, monkeypatch)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir(parents=True)
    run_dir = telemetry.create_run_dir("run-symlinked-docs-given1", date="2026-09-01")
    telemetry.write_meta(run_dir, run_id="run-symlinked-docs-given1", agent="coder", task="t",
                         started_at="2026-09-01T00:00:00Z", repo="agent-platform",
                         worktree=str(run_dir), profile="p", model="claude-sonnet-4-6")
    _os.symlink("/dev/zero", run_dir / "docs_consulted.json")

    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(
        AssertionError("/runs/<id>/docs_given hung on a symlinked docs_consulted.json")))
    signal.alarm(10)
    try:
        payload = _do_get(run_register_api, "/runs/run-symlinked-docs-given1/docs_given")
    finally:
        signal.alarm(0)

    assert payload == {"found": True, "docs_consulted": None}


# --- /sessions: what each co session is doing, for Orient's /crews -----------


@pytest.fixture
def projects_registry(monkeypatch, tmp_path):
    """Point projects.PROJECTS_JSON at a file this test writes, and clear the
    lru_cache both before use and on teardown — `_load_raw` takes no
    arguments, so its cache is a single slot: a stale entry from one test
    would otherwise leak into the next regardless of PROJECTS_JSON being
    reverted (the same reason test_projects.py's `crafted_registry` clears it
    in both places)."""
    def _write(entries):
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        monkeypatch.setattr(projects, "PROJECTS_JSON", path)
        projects._load_raw.cache_clear()
        return path
    yield _write
    projects._load_raw.cache_clear()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A real HOME, so `projects.home_path`'s `~/`-relative expansion and
    containment check (lib/agent-platform/projects.py's _expand_own_home)
    resolve against a directory this test controls rather than the real
    account's home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Run records resolve through builder_root.runs_root(), which ignores HOME.
    monkeypatch.setenv("BUILDER_ROOT", str(home / "Convivy"))
    # Crew homes resolve against the machine's root, which ignores HOME.
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", home / "Convivy")
    return home


def _sentinel(tag: str) -> str:
    """A distinct, unguessable string per field, so a leak test can search
    for exactly one field's value rather than a name that might coincide with
    another field's — the same reason a shared word like "task" would be a
    weak needle."""
    return f"SENTINEL-{tag}-{uuid.uuid4().hex[:12]}"


def _fake_full_session_state(name: str) -> dict:
    """A session_state()-shaped payload with every string field set to its
    OWN distinct sentinel — including the three paths the module docstring
    found the dispatch prompt hiding at (`agent_runs[].task`,
    `vitals.agent_runs[].task`, `working_on_derived.text`) and every other
    DO-NOT-SERVE field. Used to prove the row builder leaks none of them,
    by searching the SERIALISED response for each sentinel rather than
    asserting the absence of a fixed set of key names."""
    return {
        "session": name,
        "state": _sentinel("state"),
        "reason": _sentinel("reason"),
        "gate": {"kind": "question", "rendered": _sentinel("gate_rendered")},
        "alive": True,
        "progressing": False,
        "transcript_age": 12.5,
        "transcript_mtime": 1234567.0,
        "agents": [_sentinel("agents")],
        "agent_runs": [{"run_id": _sentinel("agent_runs_run_id"),
                        "task": _sentinel("agent_runs_task"),
                        "agent": _sentinel("agent_runs_agent")}],
        "agents_fleet_wide": True,
        "working_on": {"text": _sentinel("working_on_text"), "age": 5.0,
                       "since": 100.0, "stale": False},
        "working_on_derived": {"text": _sentinel("working_on_derived_text"),
                               "identity": _sentinel("working_on_derived_identity")},
        "cwd": _sentinel("cwd"),
        "pane": _sentinel("pane"),
        "pid": 424242,
        "session_id": _sentinel("session_id"),
        "held": {"text": _sentinel("held_text")},
        "last_event": _sentinel("last_event"),
        "last_event_age": 3.25,
        "vitals": {
            "model": _sentinel("vitals_model"),
            "ctx_tokens": 1000, "ctx_window": 200000, "ctx_pct": 42.5,
            "tier": _sentinel("vitals_tier"), "sub": _sentinel("vitals_sub"),
            "age": 1.1, "stale": False,
            "pr": {"number": 1, "title": _sentinel("vitals_pr_title")},
            "agents": [_sentinel("vitals_agents")],
            "agent_runs": [{"run_id": _sentinel("vitals_agent_runs_run_id"),
                            "task": _sentinel("vitals_agent_runs_task")}],
            "fleet_wide": False,
            "remote_control": {"reachable": True, "note": _sentinel("remote_control_note")},
        },
    }


def _register_and_place(fake_home, tmp_path, projects_registry, slug, *, private=False):
    """Register `slug` with a real, existing home_dir under `fake_home`, and
    return that directory — the shape `_project_slug_for_cwd` and
    `projects.private_slugs()` both need to resolve it."""
    home = fake_home / "Convivy" / slug
    home.mkdir(parents=True)
    unit = {"home_dir": f"~/Convivy/{slug}"}
    if private:
        unit["private"] = True
    projects_registry({slug: unit})
    return home


def test_a_session_resolving_to_no_registered_project_is_absent(
        run_register_api, fake_home, projects_registry, monkeypatch):
    """`ask` is the live case: the operator's personal-assistant tmux session, whose
    cwd (~/Convivy/ask in production) is not, and must not become, a
    registered project home."""
    ask_dir = fake_home / "Convivy" / "ask"
    ask_dir.mkdir(parents=True)
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    (fake_home / "Convivy" / "agent-platform").mkdir()

    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "ask", "path": str(ask_dir), "windows": 1, "attached": True},
    ]})
    called = []
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: called.append(name) or _fake_full_session_state(name))

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))

    assert payload["sessions"] == []
    # Never even READ: the row is built from session_state()'s own payload,
    # so a session excluded by the cwd filter must never reach that call —
    # not merely have its result discarded afterward.
    assert called == []


def test_a_private_projects_session_is_absent(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    home = _register_and_place(fake_home, tmp_path, projects_registry, "cfo", private=True)
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "cfo", "path": str(home), "windows": 1, "attached": True},
    ]})
    called = []
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: called.append(name) or _fake_full_session_state(name))

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))

    assert payload["sessions"] == []
    assert called == []


def test_an_unreadable_registry_excludes_every_session_rather_than_serving_them(
        run_register_api, tmp_path, monkeypatch):
    """The `/sessions` twin of
    `test_an_unreadable_registry_excludes_the_run_rather_than_including_it`:
    before this fix, `_sessions_payload` called `PJ.private_slugs()` directly
    with no try/except, so a missing or corrupt `config/projects.json` raised
    out of `_load_raw` and crashed the request instead of serving an empty
    list. `_project_slug_for_cwd`'s own `PJ.load()` call is unreachable here —
    `private_slugs()` is checked first and fails closed before the session
    loop ever runs — but the sessions list below still has an entry in it, so
    a regression that (wrongly) skipped the filter and served everything
    unfiltered would also be caught."""
    home = tmp_path / "bosun"
    home.mkdir()
    monkeypatch.setattr(projects, "PROJECTS_JSON", tmp_path / "does-not-exist.json")
    projects._load_raw.cache_clear()
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(home), "windows": 1, "attached": True},
    ]})
    called = []
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: called.append(name) or _fake_full_session_state(name))

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))

    assert payload["sessions"] == []
    assert called == []


def test_a_public_registered_projects_session_is_present(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    home = _register_and_place(fake_home, tmp_path, projects_registry, "bosun")
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(home), "windows": 1, "attached": True},
    ]})
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: _fake_full_session_state(name))

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))

    assert [s["session"] for s in payload["sessions"]] == ["bosun"]
    assert payload["generated_at"]


def test_the_sessions_payload_key_set_is_exactly_the_allowlist(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    """So a field added to the source (session_state()'s payload) later
    cannot appear here without a test change forcing a reviewer to look at
    it."""
    home = _register_and_place(fake_home, tmp_path, projects_registry, "bosun")
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(home), "windows": 1, "attached": True},
    ]})
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: _fake_full_session_state(name))

    [row] = run_register_api._sessions_payload(_snapshot(run_register_api))["sessions"]

    assert set(row) == set(run_register_api.SESSION_FIELDS)
    assert set(row["working_on"]) == set(run_register_api.WORKING_ON_FIELDS)


def test_the_sessions_payload_never_leaks_a_dispatch_prompt(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    """The load-bearing test: search the SERIALISED response for every
    sentinel from a source payload where each string field is distinct,
    rather than asserting the absence of the three named keys that already
    failed to catch this twice (module docstring). Every DO-NOT-SERVE field's
    sentinel must be absent; every served field's sentinel must be present,
    so the test can't pass by accident of serving nothing at all."""
    home = _register_and_place(fake_home, tmp_path, projects_registry, "bosun")
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(home), "windows": 1, "attached": True},
    ]})
    fake_state = _fake_full_session_state("bosun")
    monkeypatch.setattr(session_state, "session_state", lambda name, **kw: fake_state)

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))
    raw = json.dumps(payload)

    must_be_absent = [
        fake_state["reason"],
        fake_state["gate"]["rendered"],
        fake_state["agent_runs"][0]["task"],          # path 1: agent_runs[].task
        fake_state["agent_runs"][0]["run_id"],
        fake_state["agent_runs"][0]["agent"],
        fake_state["working_on_derived"]["text"],      # path 3: working_on_derived.text
        fake_state["working_on_derived"]["identity"],
        fake_state["cwd"],
        fake_state["pane"],
        fake_state["session_id"],
        fake_state["held"]["text"],
        fake_state["last_event"],
        fake_state["vitals"]["model"],
        fake_state["vitals"]["tier"],
        fake_state["vitals"]["sub"],
        fake_state["vitals"]["pr"]["title"],
        fake_state["vitals"]["agents"][0],
        fake_state["vitals"]["agent_runs"][0]["task"],  # path 2: vitals.agent_runs[].task
        fake_state["vitals"]["agent_runs"][0]["run_id"],
        fake_state["vitals"]["remote_control"]["note"],
        str(fake_state["pid"]),
    ]
    for sentinel in must_be_absent:
        assert sentinel not in raw, f"leaked a field this route must not serve: {sentinel!r}"

    must_be_present = [
        fake_state["state"],
        fake_state["working_on"]["text"],
    ]
    for sentinel in must_be_present:
        assert sentinel in raw, f"a field this route MUST serve went missing: {sentinel!r}"

    [row] = payload["sessions"]
    assert row["ctx_pct"] == fake_state["vitals"]["ctx_pct"]
    assert row["last_event_age"] == fake_state["last_event_age"]
    assert row["agent_run_count"] == len(fake_state["agent_runs"])
    assert row["working_on"] == {"text": fake_state["working_on"]["text"],
                                 "age": fake_state["working_on"]["age"],
                                 "since": fake_state["working_on"]["since"],
                                 "stale": fake_state["working_on"]["stale"]}


def test_a_session_row_built_through_the_real_session_state_leaks_neither_agent_runs_nor_derived_text(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    """Every other `_session_row`/`_sessions_payload` test in this file
    monkeypatches `session_state.session_state` wholesale, so none of them
    ever runs the real `_crew_is_shareable`/`_dispatched_by` code
    `session_state()` calls unconditionally to build `agent_runs` and
    `working_on_derived` — both of which call `projects.dispatch_is_private`
    LIVE, with no awareness of this route's per-request registry snapshot at
    all (round 5 finding: `_resolve_registry_snapshot`'s docstring narrows
    round 4's "no function on the request path calls back into projects.py"
    claim to exactly this pair). This test drives the REAL
    `session_state()` — stubbing only the seams below it (tmux pane,
    run-dir lookup, hook log, transcript fold), the same seam
    `test_session_state.py`'s `_working_session` helper uses — so a live
    run and a live dispatch actually flow through those two live privacy
    reads, and asserts neither the live run's task nor the derived
    working-on text reaches `_session_row`'s output: only `agent_run_count`
    (a length) and `working_on` (a different, non-derived field) are
    supposed to.
    """
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    project_home = fake_home / "Convivy" / "agent-platform"
    project_home.mkdir(parents=True)
    # `private_crew.py` reads its own hard-coded registry path
    # (`_REGISTRY`), independent of `projects.PROJECTS_JSON` — pointed at an
    # empty registry so `_crew_may_share` (gating `working_on_derived`) does
    # not depend on whatever private crews happen to be registered on the
    # machine actually running this suite.
    empty_registry = tmp_path / "private-crew-registry.json"
    empty_registry.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(private_crew, "_REGISTRY", str(empty_registry))

    # Underscore-joined, not `_sentinel`'s hyphenated form: a hyphen right
    # after "task" spells "sk-", which `signal_client.sanitise`'s credential
    # regex (correctly, if incidentally) treats as a Stripe-style secret
    # prefix and redacts — a false positive this test must dodge rather than
    # a leak it should be proving.
    agent_runs_task = f"MARKER_agent_runs_task_{uuid.uuid4().hex[:12]}"
    dispatch_task = f"MARKER_dispatch_task_{uuid.uuid4().hex[:12]}"
    session_id = "aaaa"
    started_at = "2026-08-29T10:00:00Z"
    local_day = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")

    active_json = tmp_path / "active.json"
    active_json.write_text(json.dumps({"runs": [
        {"run_id": "run-live", "agent": "coder", "task": agent_runs_task,
         "started_at": started_at, "repo": "agent-platform", "pid": 4242},
    ]}), encoding="utf-8")
    monkeypatch.setattr(session_state, "ACTIVE_JSON", active_json)
    agent_runs_root = fake_home / "Convivy" / "agent-runs"
    monkeypatch.setattr(session_state, "AGENT_RUNS_ROOT", agent_runs_root)

    run_dir = agent_runs_root / local_day / "run-live"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({
        # `run_id` matches the `active.json` entry above, as `write_meta`'s
        # `REQUIRED_META_FIELDS` guarantees for any real run — `_dispatched_by`
        # now drops a row whose own `run_id` disagrees with the one its
        # `active.json` entry located it by (a worker can write its own
        # `meta.json`, #457 round 2), so an omitted `run_id` here would read
        # as exactly that mismatch and silently drop this fixture's run.
        "run_id": "run-live", "co_session": session_id, "repo": "agent-platform",
        "task": dispatch_task, "started_at": started_at,
    }), encoding="utf-8")

    transcript_dir = fake_home / ".claude" / "projects" / "-proj"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "assistant", "version": "2.1.226",
         "timestamp": "2026-08-09T00:00:00Z", "message": {"content": []}}) + "\n")
    log = transcript_dir / "hooks.jsonl"
    log.write_text('{"event": "UserPromptSubmit"}\n', encoding="utf-8")

    monkeypatch.setattr(session_state.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(session_state.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": session_id, "cwd": str(project_home), "transcript_path": str(transcript)})
    monkeypatch.setattr(session_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(session_state, "_hook_logs", lambda sid, cwd=None, days=3: [log])
    monkeypatch.setattr(session_state, "state_from_events", lambda events: {
        "state": "working", "gate": None, "gate_ts": None,
        "last_event": "PreToolUse", "last_event_age": 0.1, "unparsed_records": 0,
    })

    # `_project_slug_for_cwd` (run-register-api's own `/sessions` row filter)
    # reads the tmux session's `path` from `session_lifecycle.sessions()`,
    # not from `session_state()`'s own resolved `cwd` — both point at the
    # registered project's home here so the row survives that filter.
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(project_home), "windows": 1, "attached": True},
    ]})

    # The real function, undisturbed — proves this reaches the actual
    # `_crew_is_shareable`/`_dispatched_by` code rather than a stand-in.
    real_state = session_state.session_state("bosun")
    assert len(real_state["agent_runs"]) == 1
    assert real_state["agent_runs"][0]["task"] == agent_runs_task
    assert real_state["working_on_derived"]["text"] == dispatch_task

    payload = run_register_api._sessions_payload(_snapshot(run_register_api))

    [row] = payload["sessions"]
    assert set(row) == set(run_register_api.SESSION_FIELDS)
    assert row["agent_run_count"] == 1
    raw = json.dumps(payload)
    assert agent_runs_task not in raw
    assert dispatch_task not in raw


def _do_get(run_register_api, path: str) -> dict:
    """Drive `Handler.do_GET` for `path` without a real socket — `do_GET`
    only touches `self.path` and `self._send_json`, neither of which needs
    the `BaseHTTPRequestHandler` connection machinery `__init__` sets up, so
    a bare, un-initialized instance is enough to exercise routing plus
    whatever `do_GET` itself does before dispatch (the registry reload,
    round 3)."""
    handler = object.__new__(run_register_api.Handler)
    handler.path = path
    sent = []
    handler._send_json = lambda code, obj: sent.append((code, obj))
    handler.do_GET()
    [(code, obj)] = sent
    assert code == 200
    return obj


def test_the_sessions_payload_privacy_filter_is_not_stale_for_the_daemons_uptime(
        run_register_api, fake_home, tmp_path, projects_registry, monkeypatch):
    """Simulates the daemon, not a test. `run-register-api` is a `KeepAlive`
    daemon that serves `/sessions` over and over in the SAME process, but
    `projects._load_raw` is `@lru_cache(maxsize=None)` — this file's own
    `projects_registry` fixture calls `cache_clear()` before AND after every
    test, which is correct hygiene and exactly why every other test here
    cannot see this bug. This one requests `/sessions` through
    `Handler.do_GET` (not `_sessions_payload` directly — the payload builder
    no longer clears the registry cache itself; that moved to `do_GET`,
    round 3, precisely so a route's own builder doesn't have to remember to),
    mutates the registry file directly (never through `projects_registry`'s
    `_write`, which would clear the cache itself and do this test's job for
    it) to mark `bosun` private, and requests `/sessions` again in the SAME
    process — reproducing what actually happens when the operator edits
    config/projects.json while the daemon keeps running. Against the code
    before the reload was structural, the second call still served `bosun`;
    that is what this test is pinned against, now proven through the actual
    request path rather than a direct call to a builder that no longer does
    the reloading."""
    home = _register_and_place(fake_home, tmp_path, projects_registry, "bosun")
    registry_path = tmp_path / "projects.json"
    monkeypatch.setattr(session_lifecycle, "sessions", lambda: {"sessions": [
        {"name": "bosun", "path": str(home), "windows": 1, "attached": True},
    ]})
    monkeypatch.setattr(session_state, "session_state",
                        lambda name, **kw: _fake_full_session_state(name))

    first = _do_get(run_register_api, "/sessions")
    assert [s["session"] for s in first["sessions"]] == ["bosun"]

    registry_path.write_text(
        json.dumps({"bosun": {"home_dir": "~/Convivy/bosun", "private": True}}),
        encoding="utf-8")

    second = _do_get(run_register_api, "/sessions")
    assert second["sessions"] == []


# --- /runs: `model` and `model_source` (issue_01M38WVHGYP89Q3B0Z1QMGAP3H) ----


def _write_meta(fake_home, run_id, started_at, meta):
    local_day = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d")
    run_dir = fake_home / "Convivy" / "agent-runs" / local_day / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(meta))


@pytest.mark.parametrize("meta, model, source", [
    ({"model": "sonnet", "model_resolved": "sonnet", "model_choice": "roster_default"},
     "sonnet", "roster"),
    ({"model": "opus", "model_resolved": "opus", "model_choice": "typed_override"},
     "opus", "override-typed"),
    ({"model": "opus", "model_resolved": "opus", "model_choice": "co_override"},
     "opus", "override-co"),
    ({"model": "opus", "model_resolved": "opus", "model_choice": "item_override"},
     "opus", "override-item"),
    ({"model": "opus", "model_resolved": "opus", "model_choice": "operator_override",
      "model_override_channel": "work_item:x"},
     "opus", "override-item"),
    ({"model": "opus", "model_resolved": "opus", "model_choice": "operator_override"},
     "opus", "override-unattributed"),
    ({"model": "sonnet", "model_resolved": "opus", "model_choice": "harness_fallback"},
     "sonnet", "failover"),
    ({"model": "sonnet", "model_resolved": "opus", "model_choice": "roster_default"},
     "sonnet", "failover"),
])
def test_a_run_serves_the_model_that_ran_and_how_it_was_chosen(
        run_register_api, runs_root, fake_home, projects_registry, meta, model, source):
    import registry
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    started_at = "2026-09-24T10:00:00Z"
    _write_meta(fake_home, "run-m", started_at, meta)
    _add_run(registry, run_id="run-m", started_at=started_at)

    [row] = run_register_api._runs_payload(_snapshot(run_register_api))["runs"]

    assert row["model"] == model
    assert row["model_source"] == source


@pytest.mark.parametrize("meta, model, resolved", [
    ({"model": "haiku", "model_resolved": "deepseek", "model_choice": "harness_fallback"},
     "haiku", "deepseek"),
    ({"model": "sonnet", "model_resolved": "sonnet", "model_choice": "roster_default"},
     "sonnet", "sonnet"),
    ({"model": "sonnet", "model_choice": "roster_default"}, "sonnet", None),
])
def test_a_run_serves_the_model_its_dispatch_intended_beside_the_one_that_ran(
        run_register_api, runs_root, fake_home, projects_registry, meta, model, resolved):
    import registry
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    started_at = "2026-09-24T10:00:00Z"
    _write_meta(fake_home, "run-r", started_at, meta)
    _add_run(registry, run_id="run-r", started_at=started_at)

    [row] = run_register_api._runs_payload(_snapshot(run_register_api))["runs"]

    assert row["model"] == model
    assert row["model_resolved"] == resolved


def test_a_run_with_no_readable_meta_serves_no_model_source(
        run_register_api, runs_root, fake_home, projects_registry):
    """`roster` would assert something nothing on disk supports."""
    import registry
    projects_registry({"agent-platform": {"home_dir": "~/Convivy/agent-platform"}})
    _add_run(registry, run_id="run-nometa")

    [row] = run_register_api._runs_payload(_snapshot(run_register_api))["runs"]

    assert row["model"] is None
    assert row["model_source"] is None
    assert row["model_resolved"] is None


def test_a_private_run_serves_neither_model_field(
        run_register_api, runs_root, fake_home, projects_registry):
    import registry
    projects_registry({
        "cfo": {"home_dir": "~/Convivy/CFO", "private": True},
        "agent-platform": {"home_dir": "~/Convivy/agent-platform"},
    })
    started_at = "2026-09-24T10:00:00Z"
    _write_meta(fake_home, "run-priv", started_at,
                {"model": "secret-model-x", "model_resolved": "secret-model-x",
                 "model_choice": "operator_override"})
    _add_run(registry, run_id="run-priv", repo="cfo", started_at=started_at)

    payload = run_register_api._runs_payload(_snapshot(run_register_api))

    assert payload["runs"] == []
    assert "secret-model-x" not in json.dumps(payload)
    assert "model_source" not in json.dumps(payload)
    assert "model_resolved" not in json.dumps(payload)
