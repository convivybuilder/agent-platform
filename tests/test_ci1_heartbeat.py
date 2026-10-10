"""`bin/ci1-heartbeat` — the ci1 heartbeat (issue_01M44VMKSMHQPKJGVSZ6ZK7ZWK).

Every external read is injected: the tailscale status, the GitHub getter, and the signal post.
No test here runs tailscale, mints an App token or posts a signal, so a passing run cannot touch
the live signal store.

The tailscale fixture (tests/fixtures/ci1_heartbeat/tailscale-status.json) keeps the field names
and value shapes of a real `tailscale status --json` on macstudio (2026-10-05). The Actions fixtures
are built from the run and job keys that `GET /repos/.../actions/runs` and `.../jobs` returned to
the convivy-bosun App on the same date.
"""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import stat
import subprocess
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "ci1_heartbeat" / "tailscale-status.json"
NOW = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_module():
    # bin/ci1-heartbeat has no extension, so spec_from_file_location cannot infer a loader.
    loader = importlib.machinery.SourceFileLoader("ci1_heartbeat", str(REPO / "bin" / "ci1-heartbeat"))
    spec = importlib.util.spec_from_loader("ci1_heartbeat", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


HB = _load_module()


@pytest.fixture
def hb():
    return HB


# --- tailscale fixture helpers ------------------------------------------------------

def _status(*, online=None, last_seen=None, backend="Running", drop_ci1=False, second_ci1=None,
            online_value=None):
    # second_ci1 adds a second peer named ci1 (a rebuild's leftover entry), copied from the first
    # and then overridden by the dict it is given.
    d = copy.deepcopy(json.loads(FIXTURE.read_text()))
    d["BackendState"] = backend
    peers = d["Peer"]
    key = next(k for k, p in peers.items() if p["HostName"] == "ci1")
    if drop_ci1:
        del peers[key]
        return d
    peer = peers[key]
    if online is not None:
        peer["Online"] = online
    if online_value is not None:
        peer["Online"] = online_value
    if last_seen is not None:
        peer["LastSeen"] = last_seen
    if second_ci1 is not None:
        twin = copy.deepcopy(peer)
        twin.update(second_ci1)
        peers["nodekey:" + "f" * 64] = twin
    return d


def _seen_minutes_ago(minutes: float) -> str:
    return _iso(NOW - timedelta(minutes=minutes))


# --- GitHub fixture helpers -----------------------------------------------------------

def _run(run_id: int) -> dict:
    return {"id": run_id, "status": "queued", "name": "CI"}


def _job(name: str, *, status: str = "queued", labels=("convivy-linux",), minutes_old: float = 20) -> dict:
    created = _iso(NOW - timedelta(minutes=minutes_old))
    return {"id": 1, "name": name, "status": status, "labels": list(labels), "created_at": created,
            "html_url": "https://github.com/convivy/vivy/actions/runs/1/job/1"}


class FakeActions:
    """Answers the two Actions reads `queue_verdict` makes, paged at 100 as GitHub does."""

    def __init__(self, runs=None, jobs=None, code=200, refused=None):
        self.runs = runs or {}      # (repo, status) -> [run]
        self.jobs = jobs or {}      # (repo, run_id) -> [job]
        self.code = code
        self.refused = refused or set()  # {repo} whose reads answer 403
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        base, _, query = path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        page = int(params.get("page", "1"))
        parts = base.strip("/").split("/")          # repos/<owner>/<repo>/actions/...
        repo = f"{parts[1]}/{parts[2]}"
        if repo in self.refused:
            return 403, {"message": "Resource not accessible by integration"}
        if self.code != 200:
            return self.code, {"message": "boom"}
        if len(parts) == 5 and parts[4] == "runs":
            items = self.runs.get((repo, params["status"]), [])
            key = "workflow_runs"
        else:
            items = self.jobs.get((repo, int(parts[5])), [])
            key = "jobs"
        lo = (page - 1) * 100
        return 200, {key: items[lo:lo + 100]}


REPOS = ["convivy/vivy", "convivy/library", "convivy/agent-platform", "convivy/shipwright",
        "convivy/selector", "convivy/selector-backend", "convivy/knowledge",
        "convivy/ci1-runner-scratch"]


def _queue(hb, get, repos=REPOS):
    return hb.queue_verdict(get, repos, NOW)


# --- condition 1: ci1 in tailscale --------------------------------------------------

def test_ci1_offline_past_fifteen_minutes_fires_with_last_seen_as_the_start(hb):
    seen = _seen_minutes_ago(20)
    v = hb.ci1_verdict(_status(online=False, last_seen=seen), NOW)
    assert v.kind == "fired"
    assert v.since == datetime.fromisoformat(seen)


def test_ci1_offline_under_fifteen_minutes_is_pending_not_fired(hb):
    v = hb.ci1_verdict(_status(online=False, last_seen=_seen_minutes_ago(10)), NOW)
    assert v.kind == "pending"


def test_ci1_offline_at_exactly_fifteen_minutes_fires(hb):
    # The threshold is inclusive: "15 minutes or more" in the brief.
    v = hb.ci1_verdict(_status(online=False, last_seen=_seen_minutes_ago(15)), NOW)
    assert v.kind == "fired"


def test_ci1_online_is_clear_whatever_its_last_seen(hb):
    # An online peer carries LastSeen 0001-01-01T00:00:00Z in the real output; it must read clear.
    v = hb.ci1_verdict(_status(), NOW)
    assert v.kind == "clear"


def test_fixture_ci1_peer_has_the_real_online_and_last_seen_shape(hb):
    peers = {p["HostName"]: p for p in json.loads(FIXTURE.read_text())["Peer"].values()}
    assert peers["ci1"]["Online"] is True
    assert peers["ci1"]["LastSeen"] == "0001-01-01T00:00:00Z"
    assert peers["Macbook"]["Online"] is False
    assert peers["Macbook"]["LastSeen"].endswith("Z")


def test_offline_with_zero_last_seen_starts_at_the_first_run_that_saw_it_down(hb):
    # The zero time is the sentinel for "never seen": tailscale still says ci1 is offline, so
    # that is a fact, and its start is the first run that observed it.
    v = hb.ci1_verdict(_status(online=False, last_seen="0001-01-01T00:00:00Z"), NOW)
    assert v.kind == "pending"
    assert v.since == NOW and v.unseen is True


def test_offline_with_unparseable_last_seen_starts_at_the_first_run_that_saw_it_down(hb):
    v = hb.ci1_verdict(_status(online=False, last_seen="yesterday-ish"), NOW)
    assert v.kind == "pending" and v.unseen is True


def test_absent_ci1_is_a_fact_that_starts_at_the_first_run_that_saw_it_absent(hb):
    # A successful read with no ci1 peer means ci1 has left the tailnet.
    v = hb.ci1_verdict(_status(drop_ci1=True), NOW)
    assert v.kind == "pending" and v.since == NOW and v.unseen is True


def test_absent_ci1_past_fifteen_minutes_from_its_recorded_start_fires(hb):
    down = NOW - timedelta(minutes=15)
    v = hb.ci1_verdict(_status(drop_ci1=True), NOW, down_since=down)
    assert v.kind == "fired"
    assert v.since == down and v.unseen is True
    assert "absent from the peer list" in v.detail


def test_a_recorded_unseen_start_is_kept_not_moved_to_now(hb):
    down = NOW - timedelta(minutes=5)
    v = hb.ci1_verdict(_status(online=False, last_seen="0001-01-01T00:00:00Z"), NOW,
                       down_since=down)
    assert v.since == down


def test_a_usable_last_seen_wins_over_a_recorded_start(hb):
    seen = _seen_minutes_ago(20)
    v = hb.ci1_verdict(_status(online=False, last_seen=seen), NOW,
                       down_since=NOW - timedelta(minutes=2))
    assert v.kind == "fired" and v.unseen is False
    assert v.since == datetime.fromisoformat(seen)


def test_two_ci1_peers_one_online_is_clear(hb):
    # A rebuild can leave an offline entry beside the live one. Any online peer means ci1 is up.
    status = _status(online=False, last_seen=_seen_minutes_ago(60),
                     second_ci1={"Online": True, "LastSeen": "0001-01-01T00:00:00Z"})
    assert hb.ci1_verdict(status, NOW).kind == "clear"


def test_two_ci1_peers_both_offline_fire_from_the_later_last_seen(hb):
    # The first peer listed is the older entry, so the start is the later LastSeen, not the first.
    later = _seen_minutes_ago(20)
    status = _status(online=False, last_seen=_seen_minutes_ago(60), second_ci1={"LastSeen": later})
    v = hb.ci1_verdict(status, NOW)
    assert v.kind == "fired"
    assert v.since == datetime.fromisoformat(later) and v.unseen is False


def test_two_ci1_peers_both_offline_use_the_only_usable_last_seen(hb):
    seen = _seen_minutes_ago(20)
    status = _status(online=False, last_seen=seen,
                     second_ci1={"LastSeen": "0001-01-01T00:00:00Z"})
    v = hb.ci1_verdict(status, NOW)
    assert v.kind == "fired" and v.since == datetime.fromisoformat(seen)


def test_two_ci1_peers_both_offline_with_no_usable_last_seen_start_at_the_first_run(hb):
    status = _status(online=False, last_seen="0001-01-01T00:00:00Z",
                     second_ci1={"LastSeen": "yesterday-ish"})
    v = hb.ci1_verdict(status, NOW)
    assert v.kind == "pending" and v.since == NOW and v.unseen is True


def test_a_non_boolean_online_on_a_second_ci1_peer_is_a_check_failure(hb):
    with pytest.raises(hb.CheckFailed, match="not a boolean"):
        hb.ci1_verdict(_status(second_ci1={"Online": "false"}), NOW)


def test_stopped_local_tailscale_is_a_check_failure_not_a_dead_ci1(hb):
    # With macstudio's daemon stopped, the peers read offline. That must not read as ci1 down.
    stopped = _status(online=False, last_seen=_seen_minutes_ago(60), backend="Stopped")
    with pytest.raises(hb.CheckFailed, match="BackendState"):
        hb.ci1_verdict(stopped, NOW)


def test_non_boolean_online_is_a_check_failure(hb):
    with pytest.raises(hb.CheckFailed, match="not a boolean"):
        hb.ci1_verdict(_status(online_value="false"), NOW)


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_tailscale_status_reads_the_json_object(hb):
    body = FIXTURE.read_bytes()
    got = hb.tailscale_status(run=lambda *a, **k: _Proc(stdout=body))
    assert got["BackendState"] == "Running"


def _missing(*a, **k):
    raise FileNotFoundError("no tailscale")


def _timed_out(*a, **k):
    raise subprocess.TimeoutExpired("tailscale", 30)


@pytest.mark.parametrize("run,match", [
    (_missing, "did not run"),
    (_timed_out, "did not run"),
    (lambda *a, **k: _Proc(returncode=1, stderr=b"daemon down"), "exited 1: daemon down"),
    (lambda *a, **k: _Proc(stdout=b"not json"), "printed no JSON"),
    (lambda *a, **k: _Proc(stdout=b"[1, 2]"), "not an object"),
])
def test_tailscale_status_failures_are_check_failures(hb, run, match):
    with pytest.raises(hb.CheckFailed, match=match):
        hb.tailscale_status(run=run)


def test_tailscale_is_called_with_a_bounded_timeout(hb):
    seen = {}

    def run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return _Proc(stdout=FIXTURE.read_bytes())

    hb.tailscale_status(run=run)
    assert seen["args"] == [hb.TAILSCALE, "status", "--json"]
    assert seen["kwargs"]["timeout"] == hb.TAILSCALE_TIMEOUT


# --- condition 2: convivy-linux jobs queued in Actions -----------------------------------

def test_old_queued_convivy_linux_job_fires_with_its_created_at_as_the_start(hb):
    job = _job("build", minutes_old=20)
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [job]})
    v = _queue(hb, get)
    assert v.kind == "fired"
    assert v.since == datetime.fromisoformat(job["created_at"])
    assert "convivy/vivy" in v.detail and "`build`" in v.detail


def test_young_queued_convivy_linux_job_is_clear(hb):
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [_job("build", minutes_old=10)]})
    assert _queue(hb, get).kind == "clear"


def test_queued_job_at_exactly_fifteen_minutes_fires(hb):
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [_job("build", minutes_old=15)]})
    assert _queue(hb, get).kind == "fired"


def test_old_job_not_targeting_convivy_linux_is_clear(hb):
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [_job("lint", labels=("ubuntu-latest",),
                                                       minutes_old=90)]})
    assert _queue(hb, get).kind == "clear"


def test_old_job_already_in_progress_is_clear(hb):
    get = FakeActions(runs={("convivy/vivy", "in_progress"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [_job("build", status="in_progress",
                                                       minutes_old=90)]})
    assert _queue(hb, get).kind == "clear"


def test_queued_job_inside_an_in_progress_run_is_found(hb):
    # A multi-job run queues its later jobs while the run itself is in progress.
    get = FakeActions(runs={("convivy/vivy", "in_progress"): [_run(7)]},
                      jobs={("convivy/vivy", 7): [_job("deploy-check", minutes_old=30)]})
    assert _queue(hb, get).kind == "fired"


def test_every_listed_repository_is_read_for_both_run_statuses(hb):
    get = FakeActions()
    _queue(hb, get)
    for repo in REPOS:
        for status in ("queued", "in_progress"):
            assert any(p.startswith(f"/repos/{repo}/actions/runs?status={status}") for p in get.paths)


def test_the_oldest_job_across_repositories_is_the_one_reported(hb):
    get = FakeActions(
        runs={("convivy/vivy", "queued"): [_run(1)], ("convivy/library", "queued"): [_run(2)]},
        jobs={("convivy/vivy", 1): [_job("a", minutes_old=20)],
              ("convivy/library", 2): [_job("b", minutes_old=40)]})
    v = _queue(hb, get)
    assert "convivy/library" in v.detail and "`b`" in v.detail


def test_refused_repository_read_is_a_check_failure(hb):
    get = FakeActions(refused={"convivy/library"})
    with pytest.raises(hb.CheckFailed, match="answered 403"):
        _queue(hb, get)


def test_runs_past_the_page_cap_are_a_check_failure_not_a_partial_read(hb):
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(i) for i in range(1100)]})
    with pytest.raises(hb.CheckFailed, match="past 10 pages"):
        _queue(hb, get, repos=["convivy/vivy"])


def test_the_second_page_is_read(hb):
    # 101 queued runs: the old job's run is the only one on page two.
    runs = [_run(i) for i in range(100)] + [_run(999)]
    get = FakeActions(runs={("convivy/vivy", "queued"): runs},
                      jobs={("convivy/vivy", 999): [_job("late", minutes_old=30)]})
    assert _queue(hb, get, repos=["convivy/vivy"]).kind == "fired"


def test_repository_list_is_the_checked_in_file(hb):
    assert hb.load_repos() == REPOS


def test_repository_list_names_every_runner_group_member(hb):
    # The convivy-linux runner group holds vivy, library, agent-platform, shipwright, selector
    # and selector-backend (added 2026-10-05), knowledge (added 2026-10-07) and
    # ci1-runner-scratch (never added to the group, but its workflows use runs-on:
    # convivy-linux); a removal of any of them from the file must fail this test, not just
    # surface in a reduced REPOS.
    repos = hb.load_repos()
    for repo in ("convivy/vivy", "convivy/library", "convivy/agent-platform",
                 "convivy/shipwright", "convivy/selector", "convivy/selector-backend",
                 "convivy/knowledge", "convivy/ci1-runner-scratch"):
        assert repo in repos


def test_repository_list_skips_comments_and_blank_lines(hb, tmp_path):
    f = tmp_path / "repos.txt"
    f.write_text("# a note\n\n  convivy/vivy  \n# convivy/old\n")
    assert hb.load_repos(f) == ["convivy/vivy"]


def test_unreadable_repository_list_is_a_check_failure(hb, tmp_path):
    with pytest.raises(hb.CheckFailed, match="repository list"):
        hb.load_repos(tmp_path / "missing.txt")


def test_a_missing_app_key_is_a_check_failure_before_any_network(hb, tmp_path):
    with pytest.raises(hb.CheckFailed, match="no App key"):
        hb.github_getter(key_path=tmp_path / "absent.pem")


def test_an_app_token_refused_is_a_check_failure(hb, monkeypatch, tmp_path):
    key = tmp_path / "bosun-app.pem"
    key.write_text("placeholder")
    key.chmod(stat.S_IRUSR | stat.S_IWUSR)
    import github_app as GA

    def refuse(*a, **k):
        raise GA.Unreadable("GitHub refused an installation token (401)")

    monkeypatch.setattr(GA, "app_jwt", refuse)
    with pytest.raises(hb.CheckFailed, match="refused an installation token"):
        hb.github_getter(key_path=key)


# --- one run: filing, suppression, recovery, failures ----------------------------------

STATE_NAME = "incident.json"


def _call(hb, state_path, *, ci1, queue, post_status=None, now=NOW):
    """One heartbeat pass with the signal post stubbed. `post_status` answers every post, or,
    as a dict, answers each edge ("opened" or "cleared") on its own. `calls["post"]` records each
    signal in posting order, split into title and body; `calls["resolve"]` is the cleared ones."""
    calls = {"post": []}

    def post(text, dedup_key, edge):
        title, _, body = text.partition("\n\n")
        calls["post"].append({"title": title, "body": body, "once_key": dedup_key, "edge": edge})
        if isinstance(post_status, dict):
            return post_status.get(edge, "created")
        return post_status or "created"

    rc = hb.run_once(now=now, state_path=state_path, ci1_check=ci1, queue_check_fn=queue,
                     post=post)
    calls["resolve"] = [c for c in calls["post"] if c["edge"] == "cleared"]
    return rc, calls


def _ci1_fires():
    since = NOW - timedelta(minutes=20)
    return lambda down_since=None: HB.Verdict("fired", since, "ci1 offline in tailscale status",
                              f"tailscale status: ci1 Online=false, LastSeen {_iso(since)}")


def _queue_fires():
    since = NOW - timedelta(minutes=30)
    return lambda now: HB.Verdict("fired", since, "a convivy-linux job queued",
                                  f"convivy/vivy: job 'build' queued since {_iso(since)}")


def _clear_ci1():
    return lambda down_since=None: HB.Verdict("clear")


def _clear_queue():
    return lambda now: HB.Verdict("clear")


def _pending_ci1():
    return lambda down_since=None: HB.Verdict("pending", NOW - timedelta(minutes=5))


def _unknown_ci1():
    def boom(down_since=None):
        raise HB.CheckFailed("tailscale status did not run")
    return boom


def _unknown_queue():
    def boom(now):
        raise HB.CheckFailed("the convivy-bosun App token refused")
    return boom


def _open_state(tmp_path, once_key="k", conditions=("ci1-offline",)):
    """A state file in the shape written before incidents were per condition: one incident,
    named at the top level. It must still read as open, for each of its conditions."""
    p = tmp_path / STATE_NAME
    p.write_text(json.dumps({"once_key": once_key, "opened_at": "2026-10-05T01:30:00Z",
                             "conditions": list(conditions)}))
    return p


def _incidents(tmp_path, **keys):
    """A state file in the current shape: `keys` maps each condition name to its once_key."""
    p = tmp_path / STATE_NAME
    p.write_text(json.dumps({"incidents": {
        name: {"once_key": key, "opened_at": "2026-10-05T01:30:00Z"}
        for name, key in keys.items()}}))
    return p


def _inc(state_path, name):
    """The open incident for `name`, read the way a run reads it. A file in the old shape is
    migrated on read, and stays in the old shape on disk until a run writes it."""
    return HB.load_state(state_path)["incidents"][name]


def test_a_fired_condition_with_no_incident_logs_one_signal_and_records_it(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue())
    assert rc == 0
    assert len(calls["post"]) == 1
    record = _inc(state, "ci1-offline")
    assert record["once_key"] == calls["post"][0]["once_key"]
    assert record["opened_at"] == _iso(NOW)
    assert set(json.loads(state.read_text())["incidents"]) == {"ci1-offline"}


def test_the_signal_body_names_the_condition_since_when_and_what_to_check(hb, tmp_path):
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_fires(), queue=_clear_queue())
    body = calls["post"][0]["body"]
    assert "ci1 Online=false" in body
    assert _iso(NOW - timedelta(minutes=20)) in body
    for line in ("ssh ci1", "systemctl status garm", "garm-cli runner list",
                 "incus list --project ci-runners"):
        assert line in body


def test_the_signal_title_names_the_condition_and_its_start(hb, tmp_path):
    _, calls = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_fires(), queue=_clear_queue())
    assert calls["post"][0]["title"].startswith("ci1 heartbeat: ci1 offline")
    assert _iso(NOW - timedelta(minutes=20)) in calls["post"][0]["title"]


def test_once_key_is_stable_for_the_same_condition_and_start(hb, tmp_path):
    # A retry of the same incident must present the same key, or the Library files twice.
    _, first = _call(hb, tmp_path / "a.json", ci1=_ci1_fires(), queue=_clear_queue())
    _, second = _call(hb, tmp_path / "b.json", ci1=_ci1_fires(), queue=_clear_queue())
    assert first["post"][0]["once_key"] == second["post"][0]["once_key"]
    assert "ci1-offline@" in first["post"][0]["once_key"]


def test_an_open_incident_suppresses_a_second_signal(hb, tmp_path):
    state = _open_state(tmp_path)
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue())
    assert rc == 0
    assert calls["post"] == []
    assert _inc(state, "ci1-offline")["once_key"] == "k"


def test_recovery_logs_the_cleared_signal_with_the_recovery_time_and_clears_state(hb, tmp_path):
    state = _open_state(tmp_path)
    rc, calls = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue())
    assert rc == 0
    assert [c["edge"] for c in calls["post"]] == ["cleared"]
    assert calls["resolve"][0]["once_key"] == "ci1-heartbeat-cleared:k"
    note = calls["resolve"][0]["body"]
    assert f"First observed at {_iso(NOW)}" in note
    assert "Incident opened 2026-10-05T01:30:00Z" in note
    assert not state.exists()


@pytest.mark.parametrize("ci1,queue,still_open", [
    ("clear", "clear", set()),                  # both recover; the state file goes
    ("pending", "clear", {"ci1-offline"}),      # ci1 offline under 15 min is not recovered
    ("clear", "fired", {"queued-job"}),         # a queued job still 15 min old is not recovered
    ("unknown", "clear", {"ci1-offline"}),      # an unreadable tailscale check is not recovered
    ("clear", "unknown", {"queued-job"}),       # an unreadable Actions check is not recovered
])
def test_each_incident_recovers_only_on_its_own_clear_verdict(hb, tmp_path, ci1, queue,
                                                              still_open):
    # Two open incidents: a condition clears on its own verdict and not on the other's.
    state = _incidents(tmp_path, **{"ci1-offline": "ci1-heartbeat:ci1-offline@a",
                                    "queued-job": "ci1-heartbeat:queued-job@b"})
    ci1_fn = {"pending": _pending_ci1(), "clear": _clear_ci1(),
              "unknown": _unknown_ci1(), "fired": _ci1_fires()}[ci1]
    queue_fn = {"fired": _queue_fires(), "clear": _clear_queue(),
                "unknown": _unknown_queue()}[queue]
    rc, calls = _call(hb, state, ci1=ci1_fn, queue=queue_fn)
    cleared = {c["once_key"] for c in calls["resolve"]}
    assert len(cleared) == 2 - len(still_open)
    if still_open:
        assert set(json.loads(state.read_text())["incidents"]) == still_open
    else:
        assert not state.exists()
    assert rc == (1 if "unknown" in (ci1, queue) else 0)


def test_a_stuck_queued_incident_does_not_mute_a_later_ci1_outage(hb, tmp_path):
    """The queued-job incident stays open because its check reads UNKNOWN (a GitHub key that is
    missing, say). A ci1 outage is a different condition, so it logs its own opened signal.
    The incident is in the file as it was written before per-condition incidents, so this fails
    on the code that held one incident for every condition."""
    state = _open_state(tmp_path, once_key="ci1-heartbeat:queued-job@x", conditions=("queued-job",))
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_unknown_queue())
    assert [c["edge"] for c in calls["post"]] == ["opened"]
    assert "ci1-offline@" in calls["post"][0]["once_key"]
    assert set(json.loads(state.read_text())["incidents"]) == {"ci1-offline", "queued-job"}
    assert rc == 1


def test_the_clear_flag_ends_an_incident_and_the_next_failure_of_that_condition_opens_a_new_one(
        hb, tmp_path, monkeypatch):
    state = tmp_path / STATE_NAME
    monkeypatch.setattr(hb, "STATE_FILE", state)
    posted = []

    def post(text, dedup_key, edge):
        posted.append((edge, dedup_key))
        return "created"

    monkeypatch.setattr(hb, "_post_signal", post)
    hb.run_once(now=NOW, state_path=state, ci1_check=_ci1_fires(), queue_check_fn=_clear_queue(),
                post=post)
    first = posted[0][1]

    assert hb.main(["--clear", "ci1-offline"]) == 0
    assert posted[-1] == ("cleared", f"ci1-heartbeat-cleared-by-hand:{first}")
    assert not state.exists()

    later = NOW + timedelta(hours=1)
    since = later - timedelta(minutes=20)
    def reopen(down_since=None):
        return HB.Verdict("fired", since, "ci1 offline in tailscale status",
                          "tailscale status: ci1 Online=false")

    hb.run_once(now=later, state_path=state, ci1_check=reopen, queue_check_fn=_clear_queue(),
                post=post)
    second = _inc(state, "ci1-offline")["once_key"]
    assert second != first, "a failure that began later is a new incident"
    assert posted[-1][0] == "opened" and posted[-1][1] == second


def test_clearing_by_hand_a_condition_still_failing_readopts_it_under_the_same_key(hb, tmp_path):
    """Pinned behaviour: a manual clear does not stop the next run from reading the same failure.
    That failure has the same start, so the store answers `duplicate`, no second opened signal is
    logged, and the incident is recorded again."""
    state = _incidents(tmp_path, **{"ci1-offline": "ci1-heartbeat:ci1-offline@same"})
    hb.clear_by_hand("ci1-offline", NOW, state, lambda text, key, edge: "created")
    assert not state.exists()
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue(), post_status="duplicate")
    assert rc == 0
    assert [c["edge"] for c in calls["post"]] == ["opened"]
    assert _inc(state, "ci1-offline")["once_key"] == calls["post"][0]["once_key"]


def _store_post():
    """A post that answers as the signal store does: a dedup_key it has already seen answers
    `duplicate`. Each call is recorded with its edge, key and answer."""
    seen, posted = set(), []

    def post(text, dedup_key, edge):
        status = "duplicate" if dedup_key in seen else "created"
        seen.add(dedup_key)
        posted.append({"edge": edge, "once_key": dedup_key, "status": status})
        return status

    return post, posted


def test_a_recovery_after_clearing_by_hand_while_still_failing_logs_its_own_cleared_signal(
        hb, tmp_path):
    """Clearing by hand while the outage still fails must not swallow the recovery: the recovery's
    cleared signal has its own key, so the store logs it rather than answering `duplicate`."""
    state = tmp_path / STATE_NAME
    post, posted = _store_post()
    hb.run_once(now=NOW, state_path=state, ci1_check=_ci1_fires(), queue_check_fn=_clear_queue(),
                post=post)
    assert hb.clear_by_hand("ci1-offline", NOW, state, post) == 0
    hb.run_once(now=NOW, state_path=state, ci1_check=_ci1_fires(), queue_check_fn=_clear_queue(),
                post=post)
    rc = hb.run_once(now=NOW + timedelta(hours=1), state_path=state, ci1_check=_clear_ci1(),
                     queue_check_fn=_clear_queue(), post=post)

    assert rc == 0
    cleared = [p for p in posted if p["edge"] == "cleared"]
    assert [c["status"] for c in cleared] == ["created", "created"], posted
    assert cleared[0]["once_key"] != cleared[1]["once_key"]
    assert cleared[1]["once_key"].startswith("ci1-heartbeat-cleared:ci1-heartbeat:ci1-offline@")
    assert [p["status"] for p in posted if p["edge"] == "opened"] == ["created", "duplicate"]


def test_clearing_by_hand_an_unseen_outage_that_still_fails_opens_a_new_incident_later(
        hb, tmp_path):
    """With no usable LastSeen the start is the first run that saw ci1 down. Clearing by hand drops
    that start, so the next run starts the outage again: pending for fifteen minutes, then a second
    opened signal under a new once_key."""
    state = tmp_path / STATE_NAME
    post, posted = _store_post()
    absent = dict(drop_ci1=True)
    hb.run_once(now=NOW, state_path=state, ci1_check=_ci1_at(NOW, **absent),
                queue_check_fn=_clear_queue(), post=post)
    later = NOW + _minutes(15)
    hb.run_once(now=later, state_path=state, ci1_check=_ci1_at(later, **absent),
                queue_check_fn=_clear_queue(), post=post)
    first = [p["once_key"] for p in posted if p["edge"] == "opened"]
    assert first == [f"ci1-heartbeat:ci1-offline@{_iso(NOW)}"]

    hb.clear_by_hand("ci1-offline", later, state, post)
    after = later + _minutes(5)
    hb.run_once(now=after, state_path=state, ci1_check=_ci1_at(after, **absent),
                queue_check_fn=_clear_queue(), post=post)
    assert [p["edge"] for p in posted].count("opened") == 1, "pending, not yet re-opened"

    reopened = after + _minutes(15)
    hb.run_once(now=reopened, state_path=state, ci1_check=_ci1_at(reopened, **absent),
                queue_check_fn=_clear_queue(), post=post)
    opened = [p for p in posted if p["edge"] == "opened"]
    assert [p["status"] for p in opened] == ["created", "created"]
    assert opened[1]["once_key"] == f"ci1-heartbeat:ci1-offline@{_iso(after)}"


def test_clearing_by_hand_with_no_open_incident_logs_nothing(hb, tmp_path, monkeypatch, capsys):
    state = tmp_path / STATE_NAME
    monkeypatch.setattr(hb, "STATE_FILE", state)
    posted = []
    monkeypatch.setattr(hb, "_post_signal", lambda *a: posted.append(a) or "created")
    assert hb.main(["--clear", "queued-job"]) == 0
    assert posted == []
    assert "no open queued-job incident" in capsys.readouterr().err


def test_the_clear_flag_is_in_the_usage_text(hb, capsys):
    with pytest.raises(SystemExit) as exit_info:
        hb.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "--clear CONDITION" in out
    assert "ci1-offline, queued-job" in out


def test_a_check_failure_alone_logs_nothing(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_unknown_ci1(), queue=_clear_queue())
    assert rc == 1
    assert calls["post"] == []
    assert not state.exists()


def test_a_positively_fired_condition_still_logs_while_the_other_check_fails(hb, tmp_path):
    # ci1's offline is a fact even when the Actions read failed the same run.
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_unknown_queue())
    assert rc == 1
    assert len(calls["post"]) == 1
    assert set(json.loads(state.read_text())["incidents"]) == {"ci1-offline"}


def test_a_failed_post_exits_nonzero_and_records_no_incident(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc, _ = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue(),
                  post_status="error:503")
    assert rc == 1
    assert not state.exists()


def test_a_skipped_post_exits_nonzero(hb, tmp_path):
    rc, _ = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_fires(), queue=_clear_queue(),
                  post_status="skip")
    assert rc == 1


def test_a_duplicate_answer_records_the_existing_incident(hb, tmp_path):
    # A retry after a lost state write: the store answers duplicate for the key already logged.
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue(), post_status="duplicate")
    assert rc == 0
    assert _inc(state, "ci1-offline")["once_key"] == calls["post"][0]["once_key"]


def test_a_failed_cleared_signal_keeps_the_incident_open_for_the_next_run(hb, tmp_path):
    state = _open_state(tmp_path)
    rc, _ = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue(),
                  post_status={"cleared": "error:503"})
    assert rc == 1
    assert _inc(state, "ci1-offline")["once_key"] == "k"


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"once_key": ""}),
                                     json.dumps({"opened_at": "x"})])
def test_an_unreadable_incident_state_stops_the_run_before_any_signal(hb, tmp_path, content):
    state = tmp_path / STATE_NAME
    state.write_text(content)
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_queue_fires())
    assert rc == 1
    assert calls["post"] == [] and calls["resolve"] == []


def test_an_unreadable_incident_state_is_not_read_as_healthy(hb, tmp_path):
    state = tmp_path / STATE_NAME
    state.write_text("{not json")
    rc, calls = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue())
    assert rc == 1
    assert calls["post"] == [] and calls["resolve"] == []


def test_a_healthy_run_with_no_incident_does_nothing(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue())
    assert rc == 0
    assert calls["post"] == [] and calls["resolve"] == []
    assert not state.exists()


def test_a_pending_offline_ci1_with_no_incident_logs_nothing(hb, tmp_path):
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_pending_ci1(), queue=_clear_queue())
    assert rc == 0
    assert calls["post"] == []


def test_a_queued_job_fires_its_own_incident(hb, tmp_path):
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_clear_ci1(), queue=_queue_fires())
    assert rc == 0
    assert "queued-job@" in calls["post"][0]["once_key"]


def test_a_queued_job_in_knowledge_logs_its_own_signal(hb, tmp_path):
    # knowledge#813, 2026-10-07: convivy/knowledge's jobs sat queued for over 30 minutes while
    # knowledge was in neither the runner group nor this file, and nothing was logged. Reads the
    # shipped file through load_repos(), not the REPOS test constant, so a dropped
    # convivy/knowledge entry in the real file fails this test.
    get = FakeActions(runs={("convivy/knowledge", "queued"): [_run(1)]},
                      jobs={("convivy/knowledge", 1): [_job("validate", minutes_old=30)]})
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_clear_ci1(),
                      queue=lambda now: hb.queue_verdict(get, hb.load_repos(), now))
    assert rc == 0
    assert len(calls["post"]) == 1
    assert calls["post"][0]["edge"] == "opened"
    assert "convivy/knowledge" in calls["post"][0]["body"]


def test_both_conditions_firing_open_one_incident_each(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_queue_fires())
    assert rc == 0
    assert len(calls["post"]) == 2
    titles = sorted(c["title"] for c in calls["post"])
    assert "ci1 offline" in titles[1] and "convivy-linux job queued" in titles[0]
    assert set(json.loads(state.read_text())["incidents"]) == {"ci1-offline", "queued-job"}


# --- the signal post ---------------------------------------------------------------

def test_the_opening_signal_is_an_alert_on_the_ci1_stream_keyed_on_its_once_key(hb, monkeypatch):
    import signal_client as SC
    seen = {}

    def fake_post_alert(stream, body, **kwargs):
        seen.update(stream=stream, body=body, **kwargs)
        return "created"

    monkeypatch.setattr(SC, "post_alert", fake_post_alert)
    status = hb._post_signal("t\n\nb", "ci1-heartbeat:ci1-offline@2026-10-05T01:40:00Z", "opened")
    assert status == "created"
    assert seen["stream"] == "ci1-heartbeat"
    assert seen["product"] == "bosun"
    assert seen["dedup_key"] == "ci1-heartbeat:ci1-offline@2026-10-05T01:40:00Z"
    assert seen["meta"] == {"edge": "opened", "severity": "high"}
    assert seen["body"] == "t\n\nb"


def test_the_signal_body_is_sanitised_line_by_line(hb, monkeypatch):
    import signal_client as SC
    seen = {}
    monkeypatch.setattr(SC, "post_alert",
                        lambda stream, body, **kw: seen.update(body=body) or "created")
    hb._post_signal("header\njob \x1b[31mbuild\x1b[0m Bearer abcdefghijklmnop", "k", "opened")
    assert "\x1b" not in seen["body"]
    assert "abcdefghijklmnop" not in seen["body"]
    assert seen["body"].splitlines()[0] == "header"


def test_a_github_supplied_field_is_sanitised_before_it_is_quoted(hb):
    assert "\x1b" not in hb._inert("\x1b[31mbuild")
    assert hb._inert("a`b") == "`ab`"


def test_an_incident_logs_one_signal_per_condition_while_open_and_one_per_recovery(hb, tmp_path):
    state = tmp_path / STATE_NAME
    rc1, c1 = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue())
    rc2, c2 = _call(hb, state, ci1=_ci1_fires(), queue=_queue_fires(), now=NOW + _minutes(5))
    rc3, c3 = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue(), now=NOW + _minutes(10))
    assert (rc1, rc2, rc3) == (0, 0, 0)
    # ci1's incident is open after run 1, so run 2 opens only the queued job's, and run 3 clears both.
    assert [c["edge"] for c in c1["post"]] == ["opened"]
    assert [c["edge"] for c in c2["post"]] == ["opened"]
    assert "queued-job@" in c2["post"][0]["once_key"]
    assert sorted(c["edge"] for c in c3["post"]) == ["cleared", "cleared"]
    assert not state.exists()


def test_a_state_file_from_before_signals_still_reads_as_open_and_clears_on_recovery(hb, tmp_path):
    # An incident opened before signals recorded an issue_id beside its once_key. That state
    # still reads as open, and recovery clears it with a cleared signal.
    state = tmp_path / STATE_NAME
    state.write_text(json.dumps({"issue_id": "issue_OLD", "once_key": "k",
                                 "opened_at": "2026-10-05T01:30:00Z", "conditions": ["ci1-offline"]}))
    rc, calls = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue())
    assert rc == 0
    assert [c["edge"] for c in calls["post"]] == ["cleared"]
    assert not state.exists()


def test_the_state_file_is_private(hb, tmp_path):
    path = tmp_path / "state" / STATE_NAME
    hb.save_incident(path, {"incidents": {"ci1-offline": {"once_key": "k", "opened_at": "x"}}})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["incidents"]["ci1-offline"]["once_key"] == "k"


def test_the_check_is_wired_to_the_convivy_linux_label_and_the_fifteen_minute_threshold(hb):
    assert hb.RUNNER_LABEL == "convivy-linux"
    assert hb.THRESHOLD == timedelta(minutes=15)
    assert hb.CI_NODE == "ci1"


# --- round 2: an absent or zero-LastSeen ci1 is a fact, and a run survives any one read failing

def _ci1_at(now, **status_kwargs):
    status = _status(**status_kwargs)
    return lambda down_since=None: HB.ci1_verdict(status, now, down_since)


def _minutes(n):
    return timedelta(minutes=n)


def test_an_absent_ci1_fires_once_across_runs_with_the_same_start(hb, tmp_path):
    state = tmp_path / STATE_NAME
    zero = dict(online=False, last_seen="0001-01-01T00:00:00Z")
    rc1, c1 = _call(hb, state, ci1=_ci1_at(NOW, **zero), queue=_clear_queue(), now=NOW)
    assert rc1 == 0 and c1["post"] == []
    assert json.loads(state.read_text())["down_since"] == _iso(NOW)
    rc2, c2 = _call(hb, state, ci1=_ci1_at(NOW + _minutes(5), **zero), queue=_clear_queue(),
                    now=NOW + _minutes(5))
    assert rc2 == 0 and c2["post"] == []
    assert json.loads(state.read_text())["down_since"] == _iso(NOW)
    rc3, c3 = _call(hb, state, ci1=_ci1_at(NOW + _minutes(16), **zero), queue=_clear_queue(),
                    now=NOW + _minutes(16))
    assert rc3 == 0 and len(c3["post"]) == 1
    assert "ci1-offline@" + _iso(NOW) in c3["post"][0]["once_key"]


def test_an_absent_ci1_is_a_fact_that_fires_past_fifteen_minutes(hb, tmp_path):
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_at(NOW, drop_ci1=True),
                      queue=_clear_queue())
    assert rc == 0 and calls["post"] == []
    rc2, calls2 = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_at(NOW + _minutes(15), drop_ci1=True),
                        queue=_clear_queue(), now=NOW + _minutes(15))
    assert rc2 == 0 and len(calls2["post"]) == 1
    assert "absent from the peer list" in calls2["post"][0]["body"]


def test_a_recorded_start_survives_a_run_that_cannot_read_tailscale(hb, tmp_path):
    state = tmp_path / STATE_NAME
    _call(hb, state, ci1=_ci1_at(NOW, drop_ci1=True), queue=_clear_queue())
    rc, calls = _call(hb, state, ci1=_unknown_ci1(), queue=_clear_queue(),
                      now=NOW + _minutes(5))
    assert rc == 1 and calls["post"] == []
    assert json.loads(state.read_text())["down_since"] == _iso(NOW)
    rc3, c3 = _call(hb, state, ci1=_ci1_at(NOW + _minutes(16), drop_ci1=True),
                    queue=_clear_queue(), now=NOW + _minutes(16))
    assert rc3 == 0 and "ci1-offline@" + _iso(NOW) in c3["post"][0]["once_key"]


def test_ci1_seen_online_again_clears_the_recorded_start(hb, tmp_path):
    state = tmp_path / STATE_NAME
    _call(hb, state, ci1=_ci1_at(NOW, drop_ci1=True), queue=_clear_queue())
    assert state.exists()
    rc, calls = _call(hb, state, ci1=_ci1_at(NOW + _minutes(5)), queue=_clear_queue(),
                      now=NOW + _minutes(5))
    assert rc == 0 and calls["post"] == []
    assert not state.exists()


def test_an_absent_ci1_whose_start_cannot_be_recorded_files_nothing(hb, tmp_path, monkeypatch):
    def read_only(path, state):
        raise OSError("read-only file system")

    monkeypatch.setattr(hb, "_write_state", read_only)
    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_at(NOW, drop_ci1=True),
                      queue=_clear_queue())
    assert rc == 1
    assert calls["post"] == []


@pytest.mark.parametrize("seen", ["2026-10-05T01:40:00.1Z", "2026-10-05T01:40:00.123456789Z",
                                  "2026-10-05T01:40:00Z"])
def test_tailscale_last_seen_shapes_are_read_on_the_script_python(hb, seen):
    v = hb.ci1_verdict(_status(online=False, last_seen=seen), NOW)
    assert v.kind == "fired" and v.unseen is False


@pytest.mark.parametrize("created", ["2026-10-04T01:55:24Z", "2026-10-05T01:40:00.5Z"])
def test_github_created_at_shapes_are_read_on_the_script_python(hb, created):
    job = _job("build")
    job["created_at"] = created
    get = FakeActions(runs={("convivy/vivy", "queued"): [_run(1)]},
                      jobs={("convivy/vivy", 1): [job]})
    assert _queue(hb, get, repos=["convivy/vivy"]).kind == "fired"


def test_a_malformed_down_since_stops_the_run_before_any_signal(hb, tmp_path):
    state = tmp_path / STATE_NAME
    state.write_text(json.dumps({"down_since": "soon"}))
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_queue_fires())
    assert rc == 1
    assert calls["post"] == [] and calls["resolve"] == []


@pytest.mark.parametrize("error", [urllib.error.URLError("timed out"), TimeoutError("read timed out"),
                                   ValueError("non-JSON 200")])
def test_a_github_failure_does_not_drop_a_fired_ci1_offline(hb, tmp_path, error):
    # The error comes from the GitHub read itself, through queue_check and _paged, as GA.request raises it.
    def get(path):
        raise error

    def queue(now):
        return hb.queue_check(now, github=lambda: get)

    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=_ci1_fires(), queue=queue)
    assert rc == 1
    assert len(calls["post"]) == 1
    assert "ci1-offline@" in calls["post"][0]["once_key"]
    assert _inc(tmp_path / STATE_NAME, "ci1-offline")["once_key"] == calls["post"][0]["once_key"]


def test_a_ci1_check_exception_does_not_drop_a_fired_queued_job(hb, tmp_path):
    def broken(down_since=None):
        raise OSError("tailscale vanished")

    rc, calls = _call(hb, tmp_path / STATE_NAME, ci1=broken, queue=_queue_fires())
    assert rc == 1
    assert len(calls["post"]) == 1
    assert "queued-job@" in calls["post"][0]["once_key"]


def test_a_failed_state_write_after_logging_exits_nonzero_and_keeps_the_signal(hb, tmp_path, monkeypatch):
    def read_only(path, state):
        raise OSError("disk full")

    monkeypatch.setattr(hb, "_write_state", read_only)
    state = tmp_path / STATE_NAME
    rc, calls = _call(hb, state, ci1=_ci1_fires(), queue=_clear_queue())
    assert rc == 1
    assert len(calls["post"]) == 1
    assert not state.exists()


def test_a_failed_state_clear_after_the_cleared_signal_is_retried_and_answered_duplicate(
        hb, tmp_path, monkeypatch):
    state = _open_state(tmp_path)

    def read_only(path, st):
        raise OSError("read-only file system")

    monkeypatch.setattr(hb, "_write_state", read_only)
    rc1, c1 = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue())
    assert rc1 == 1
    assert c1["resolve"][0]["once_key"] == "ci1-heartbeat-cleared:k"
    monkeypatch.undo()
    rc2, c2 = _call(hb, state, ci1=_clear_ci1(), queue=_clear_queue(), now=NOW + _minutes(5),
                    post_status="duplicate")
    assert rc2 == 0 and [c["edge"] for c in c2["post"]] == ["cleared"]
    assert not state.exists()
