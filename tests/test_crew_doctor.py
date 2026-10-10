"""`crew doctor` — does each site fail for ITS OWN reason?

The definition of done (`issue_01M2NYBBY0FXCDHWWD8XBR9RR3`) asks that the doctor
"is shown to fail when each site is removed in turn". That is a per-site
requirement and it is stated that way for a reason: **a doctor that fails for the
wrong reason passes exactly like a correct one.** Asserting only that a broken
crew exits non-zero would hold just as well if every check were replaced by
`return MISSING`, so each test below breaks ONE site and asserts that THAT site's
line is the one that changed.

The tests also pin the four bugs found while building it, each of which produced
a confident, wrong report about a healthy crew:

  - a string comparison of timestamps in different formats (Signals "stale")
  - counting documents authored under a slug instead of the ones its profile
    resolves (shipwright "broken", signals nearly)
  - comparing `crew_for_repo` against the registry KEY instead of the crew name
    (convivy-deck "broken")
  - a hardcoded `daytime` variant (ea and convivy-commercial "broken")
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib" / "agent-platform"))
import crew_doctor as CD  # noqa: E402
import issue_client  # noqa: E402
import vault_plane  # noqa: E402


def _state(results, fragment):
    """The state of the one site whose name contains FRAGMENT."""
    hits = [r for r in results if fragment in r.site]
    assert len(hits) == 1, f"{fragment!r} matched {[r.site for r in hits]}"
    return hits[0].state


class _Listener:
    """A real local HTTP server on an ephemeral port that records what reaches
    it. The same pattern `test_issue_client.py` uses for `get_item_from_
    default_library` — a redirect refusal is a property of the real opener
    chain, not something a stubbed transport can stand in for."""

    def __init__(self, reply=None):
        import http.server
        import threading
        self.requests = []
        outer = self
        reply = reply or (200, {}, b'{"profile": {}, "documents": []}')

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                code, hdrs, body = reply
                self.send_response(code)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def listeners():
    made = []

    def make(reply=None):
        made.append(_Listener(reply))
        return made[-1]
    yield make
    for x in made:
        x.close()


# --- states ----------------------------------------------------------------

def test_an_unchecked_site_is_not_a_passing_one():
    """UNKNOWN exits non-zero with MISSING. A site the doctor could not evaluate
    has not established that the crew works, and treating "could not check" as a
    pass is how a guard becomes decoration."""
    assert CD.UNKNOWN in CD.FAILING_STATES
    assert CD.MISSING in CD.FAILING_STATES
    assert CD.PRESENT not in CD.FAILING_STATES
    assert CD.NOT_APPLICABLE not in CD.FAILING_STATES


def test_unknown_and_missing_stay_distinguishable():
    """They exit the same way and must READ differently: the Library being
    mid-WAL-switch is not a broken crew, and reporting it as one sends the
    reader after the wrong thing entirely."""
    assert CD.UNKNOWN != CD.MISSING
    unreachable = CD.Result("profile served (x/daytime)", "Library", CD.UNKNOWN,
                            "Library did not answer: connection refused")
    assert not unreachable.ok
    assert "Library did not answer" in unreachable.line()


# --- dispatch: the key is not the crew -------------------------------------

def test_dispatch_compares_against_the_crew_name_not_the_registry_key(monkeypatch):
    """`convivy-deck` is the project slug; its `crew` is `deck`, which is what
    `crew_for_repo` returns. A draft compared against the key and reported a
    healthy crew broken. Several projects may share one crew (ARCH-426)."""
    monkeypatch.setattr(CD.projects, "crew_for_repo", lambda repo: "deck")
    unit = {"repos": ["convivy-deck"], "crew": "deck"}
    assert CD.check_dispatch("convivy-deck", unit).state == CD.PRESENT


def test_dispatch_fails_when_a_repo_resolves_to_a_different_crew(monkeypatch):
    """The site removed: work landing on someone else's board is worse than work
    landing nowhere, so this must fail rather than merely be quiet."""
    monkeypatch.setattr(CD.projects, "crew_for_repo", lambda repo: "someone-else")
    unit = {"repos": ["convivy-deck"], "crew": "deck"}
    res = CD.check_dispatch("convivy-deck", unit)
    assert res.state == CD.MISSING
    assert "someone-else" in res.detail and "deck" in res.detail


# --- the agent prefix ------------------------------------------------------

def test_a_prefix_resolving_to_the_primary_checkout_fails():
    """`derive_purpose` returning None means the PRIMARY checkout — a write-class
    worker committing into the crew's shared tree, which is the failure this site
    exists to catch. Verified live: `zzz-coder` returns None today."""
    res = CD.check_agent_prefix("x", {"agent_prefix": "zzz-"},
                                derive_purpose=lambda *a, **k: None)
    assert res.state == CD.MISSING
    assert "PRIMARY" in res.detail


def test_a_prefix_resolving_to_a_worktree_passes():
    res = CD.check_agent_prefix("x", {"agent_prefix": "sig-"},
                                derive_purpose=lambda name, task, pr=None: f"{name}-slug")
    assert res.state == CD.PRESENT


def test_a_crew_with_no_prefix_is_not_asserted_to_pass():
    """A check that cannot fail is not a check. Until the registry carries
    `agent_prefix` (slice 2), a prefix-less crew is N/A rather than PRESENT."""
    assert CD.check_agent_prefix("x", {}, derive_purpose=lambda *a, **k: None
                                 ).state == CD.NOT_APPLICABLE


# --- co --resolve ----------------------------------------------------------

def test_co_without_the_flag_reads_as_unchecked_not_as_a_broken_crew(tmp_path):
    """`co` forwards an unknown flag to `claude`, so an older `co` answers with
    claude's error and a zero-ish exit. A draft read that as every crew being
    MISSING — with claude's "(Did you mean --resume?)" as the detail, having
    started a process per crew to get it. The `crew=` line is the only evidence."""
    fake = tmp_path / "co"
    fake.write_text("#!/bin/sh\necho 'Error (Did you mean --resume?)' >&2\nexit 1\n")
    fake.chmod(0o755)
    res = CD.check_co_resolves("x", tmp_path, co_bin=fake)
    assert res.state == CD.UNKNOWN
    assert "no --resolve" in res.detail


def test_co_refusing_a_directory_is_a_missing_site(tmp_path):
    fake = tmp_path / "co"
    fake.write_text("#!/bin/sh\necho 'co: no crew resolves for /x' >&2\nexit 1\n")
    fake.chmod(0o755)
    assert CD.check_co_resolves("x", tmp_path, co_bin=fake).state == CD.MISSING


def test_co_naming_the_crew_is_the_evidence(tmp_path):
    fake = tmp_path / "co"
    fake.write_text("#!/bin/sh\necho 'crew=signals label=Signals dir=/x'\n")
    fake.chmod(0o755)
    res = CD.check_co_resolves("x", tmp_path, co_bin=fake)
    assert res.state == CD.PRESENT
    assert "crew=signals" in res.detail


def test_every_co_launch_goes_through_the_one_choke_point():
    """FIFTEEN branches used to `exec claude` directly, and under `--resolve` any
    of them would have STARTED A SESSION — which `crew doctor --all` would then do
    once per crew that resolves to none. A guard before each would rot: the next
    branch added would not have it and nothing would say so. This test is what
    keeps the single choke point true."""
    co = (Path(__file__).resolve().parents[1] / "bin" / "co").read_text()
    bare = [ln.strip() for ln in co.splitlines()
            if re.match(r"^\s*exec claude\b", ln)]
    assert bare == ['exec claude "$@"'], \
        f"a launch bypasses _co_exec_claude: {bare}"
    assert co.count("_co_exec_claude ") >= 15


def test_no_launcher_renames_a_tmux_window_it_does_not_own():
    """The sibling of the check above, after the same failure twice.

    Untargeted, tmux resolves a command to the current session's ACTIVE window,
    so a rename with no `-t` lands on whatever the operator was last looking at.
    It renamed the operator's main Ask window on 2026-09-06
    (issue_01M1VTRHKRERY7MJEYCF4WR2G2), and on 2026-09-16 it labelled the live
    `bosun` window `operator.com` and earlier `Shipwright`
    (issue_01M2PXARYZ2HA08G9MJ4QPQYW9), which was first read as a lost session.

    `bin/co` is guarded behaviourally — `tests/bash/test_co_style_targets.sh`
    drives it against a tmux spy and asserts it calls nothing when there is no
    pane. `bin/oc` gets this static check instead, and the asymmetry is
    deliberate rather than an oversight: `oc` resolves `builder-root` and
    `gateway-vkey` by absolute path beside itself and then `exec`s a
    third-party binary, so driving it means copying the script beside three
    stubs — a large harness for a three-line guard on a trial launcher
    (ARCH-133). This catches what actually recurs, an edit reintroducing the
    untargeted form. It would not catch the guard being right and the control
    flow around it being wrong, and nothing here claims otherwise.

    AN ALLOWLIST, NOT A PATTERN FOR THE BAD SHAPE. Matching `rename-window`
    without `-t` would be satisfied by a call reaching tmux through a
    variable-held subcommand or split across a line continuation. Enumerating
    what to reject is a list someone has to keep exhaustive, and the bug being
    guarded is one that came back through a case the previous guard did not
    enumerate. So this names every tmux invocation these two files may contain,
    on any subcommand, and anything else fails.

    WHY TWO FILES AND NOT A `bin/*` GLOB. A glob reads only `bin/`, and tmux is
    driven from `lib/agent-platform/` as well: `bin/session-agent` invokes it
    nowhere itself and mutates tmux state entirely through `session_input.py`
    and `session_lifecycle.py`, which it imports. So the glob would read as
    comprehensive while never opening a file where a mutating call is likely to
    be added, and a guard that looks wider than it is stops the next author
    looking further.

    THIS TEST MAKES NO CLAIM ABOUT `lib/`. Three drafts of this paragraph tried
    to summarise what is there — a file count, an exception count, a map of
    which script reaches which module — and review found each one wrong. Every
    one was a hand-maintained enumeration, the thing this test's own allowlist
    exists to avoid, so the summary is absent rather than corrected a fourth
    time. Whoever needs to know about `lib/` has to go and look.
    """
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    calls = [f"bin/{name}: {ln.strip()}"
             for name in ("co", "oc")
             for ln in (bin_dir / name).read_text().splitlines()
             # A WORD BOUNDARY ON BOTH SIDES, not `tmux[ "]`. Requiring a space
             # or quote after the name lets `tmux;` and a tab-separated call
             # slip the enumeration, and an allowlist is only as good as the
             # step that feeds it. The trailing class also keeps `tmux_target`
             # and `_co_tmux_session` out, since `_` is a word character.
             if re.search(r"(?<![-\w])tmux(?![-\w])", ln)
             and not ln.lstrip().startswith("#")]
    allowed = {
        # bin/co's six styling calls. `tmux_target` is `-t "$TMUX_PANE"` or the
        # function has already returned, so none of these can go out untargeted.
        'bin/co: tmux set-option "${tmux_target[@]}" allow-passthrough on 2>/dev/null',
        'bin/co: tmux set-option "${tmux_target[@]}" status-style '
        '"bg=${accent_hex},fg=${status_fg}" 2>/dev/null',
        'bin/co: tmux set-option "${tmux_target[@]}" window-status-current-style '
        '"bg=${accent_hex},fg=${status_fg},bold" 2>/dev/null',
        'bin/co: tmux set-option "${tmux_target[@]}" status-left '
        '" ${label//\\#/##}  " 2>/dev/null',
        'bin/co: tmux set-option "${tmux_target[@]}" status-left-length 20 2>/dev/null',
        'bin/co: tmux rename-window "${tmux_target[@]}" "$label" 2>/dev/null',
        # A READ, already targeted. It asks which session this pane is in and
        # changes nothing, so it cannot retarget anything.
        'bin/co: _co_tmux_session=$(tmux display-message -p -t "$TMUX_PANE" '
        "'#{session_name}' 2>/dev/null) \\",
        # The same read, under `_co_reindividuate`, asking whether `-r` is
        # running in a subcrew child's tmux session so it can skip
        # `knowledge-ctx use --force` (issue_01M49WBSYTDF6W534TJDEKD3XR).
        # Already targeted, changes nothing.
        'bin/co: _co_subcrew_session=$(tmux display-message -p -t "$TMUX_PANE" '
        "'#{session_name}' 2>/dev/null) \\",
        # bin/oc's one call, targeted and inside its own pane guard.
        'bin/oc: tmux rename-window -t "$TMUX_PANE" "oc-trial" 2>/dev/null || true',
    }
    unexpected = [c for c in calls if c not in allowed]
    assert unexpected == [], (
        "a tmux invocation in bin/co or bin/oc is not on this test's allowlist. "
        "If it acts on this session's own pane it must name that pane with -t "
        "and be skipped when there is none; if it does not, say why here and "
        f"add it: {unexpected}"
    )
    # `bin/co-rollover` is deliberately out of scope: it drives tmux about OTHER
    # panes, spawning and killing the successor's window, and names its targets
    # explicitly. That is a different question from this one.


# --- the index -------------------------------------------------------------

def test_the_index_check_asks_about_the_profiles_documents(tmp_path):
    """NOT `WHERE id LIKE '<project>/%'`. A profile draws documents from across
    projects — shipwright's resolves 141 whose ids begin `agent-platform/` — while
    the count authored under its own slug is 0. That draft reported shipwright
    broken on a number that was never the question."""
    import sqlite3
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE docs (id TEXT)")
    con.executemany("INSERT INTO docs VALUES (?)",
                    [("agent-platform/agents/planner",), ("company/decisions/0001",)])
    con.commit()
    con.close()
    # Ids from another project, which is the normal case, and none under
    # `shipwright/` at all.
    res = CD.check_index_serves(["agent-platform/agents/planner",
                                 "company/decisions/0001"], index_db=db)
    assert res.state == CD.PRESENT


def test_the_index_check_names_the_documents_it_cannot_find(tmp_path):
    """The site removed. A count sends someone to look; a name says where."""
    import sqlite3
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE docs (id TEXT)")
    con.execute("INSERT INTO docs VALUES ('a/one')")
    con.commit()
    con.close()
    res = CD.check_index_serves(["a/one", "a/two"], index_db=db)
    assert res.state == CD.MISSING
    assert "a/two" in res.detail


def test_a_missing_index_is_unchecked_rather_than_broken(tmp_path):
    assert CD.check_index_serves(["a/one"], index_db=tmp_path / "nope.db"
                                 ).state == CD.UNKNOWN


# --- the variant -----------------------------------------------------------

def test_the_variant_comes_from_the_crew_not_from_a_constant(tmp_path):
    """`ea` runs `platform/ea-daytime` and `convivy-commercial` runs
    `company/commercial`. A doctor hardcoding `daytime` reported both broken for
    running the profile they are supposed to run."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".active-profile").write_text("platform/ea-daytime\n")
    assert CD.active_variant(tmp_path) == "ea-daytime"


def test_a_crew_that_never_individuated_falls_back_to_the_default(tmp_path):
    assert CD.active_variant(tmp_path) == "daytime"


# --- the crew's agents -----------------------------------------------------

def _crew_dir(tmp_path, *, agents=(), active=None):
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    for a in agents:
        (tmp_path / ".claude" / "agents" / a).write_text("x")
    if active is not None:
        (tmp_path / ".claude" / ".active-profile").write_text(active)
    return tmp_path


def test_no_agents_is_the_signals_failure_and_must_fail(tmp_path):
    """The site removed, and the one that made Signals unusable: `co -r` did
    nothing but run a skill, because nothing had ever rendered its agents."""
    res = CD.check_crew_context(_crew_dir(tmp_path), "signals", "daytime")
    assert res.state == CD.MISSING
    assert "knowledge-ctx use" in res.detail


def test_agents_committed_in_the_repo_count(tmp_path):
    """`convivy-deck` and `hollis` keep `.claude/agents/` committed and never
    individuate, which `bin/co`'s deck branch says in as many words. A draft
    checked for the marker file and reported both broken, with `svc` and
    `write-better`. The site is whether a session finds agents."""
    res = CD.check_crew_context(_crew_dir(tmp_path, agents=["coder.md"]), "deck", "daytime")
    assert res.state == CD.PRESENT
    assert "never individuated" in res.detail


def test_an_individuated_crew_running_another_profile_fails(tmp_path):
    home = _crew_dir(tmp_path, agents=["coder.md"], active="other/daytime")
    res = CD.check_crew_context(home, "signals", "daytime")
    assert res.state == CD.MISSING
    assert "other/daytime" in res.detail


def test_an_individuated_crew_on_its_own_profile_passes(tmp_path):
    home = _crew_dir(tmp_path, agents=["coder.md", "reviewer.md"], active="signals/daytime")
    res = CD.check_crew_context(home, "signals", "daytime")
    assert res.state == CD.PRESENT
    assert "2 agents" in res.detail


# --- the registry ----------------------------------------------------------

def test_an_unregistered_crew_fails_at_the_first_site(monkeypatch):
    monkeypatch.setattr(CD.projects, "get", lambda s: None)
    monkeypatch.setattr(CD.projects, "resolve_project", lambda *a, **k: None)
    res, unit = CD.check_registry("nope")
    assert res.state == CD.MISSING and unit is None


def test_an_archived_crew_is_not_active(monkeypatch):
    monkeypatch.setattr(CD.projects, "get", lambda s: {"lifecycle": "archived"})
    assert CD.check_registry("old")[0].state == CD.MISSING


def test_diagnose_stops_rather_than_cascading_for_an_unknown_crew(monkeypatch):
    """Eight more MISSING lines for a crew that does not exist buries the one
    line that matters."""
    monkeypatch.setattr(CD.projects, "get", lambda s: None)
    monkeypatch.setattr(CD.projects, "resolve_project", lambda *a, **k: None)
    results = CD.diagnose("nope", derive_purpose=lambda *a, **k: None,
                          allowed=set(), id_pattern=re.compile("x"))
    assert len(results) == 1
    assert not results[0].ok


# --- an adopter's crew (no registry row at all) -----------------------------
#
# `resolve_project`'s own identity fallback -- a name with a `projects/<name>/`
# directory in the knowledge tree resolves to itself -- is what gives an
# adopter's crew its crew-scoped instruction blocks with no row in
# config/projects.json (company decision 0011). `check_registry` used to read
# that absence as MISSING and stop, so every later site went unchecked, and
# Shipwright's fresh install reported `tidytags` as "1 of 1 site(s) need
# attention" with nothing past the first line (issue_01M3NRPP5AWKZ223KVGZ2XW5FC).

def test_check_registry_admits_an_adopters_crew_with_no_registry_row(monkeypatch):
    monkeypatch.setattr(CD.projects, "get", lambda s: None)
    monkeypatch.setattr(CD.projects, "resolve_project",
                        lambda name, root=None: name if name == "tidytags" else None)
    res, unit = CD.check_registry("tidytags", knowledge_root="/kb")
    assert res.state == CD.PRESENT and unit is None
    assert "projects/tidytags/" in res.detail and "no registry row" in res.detail


def test_check_registry_stays_missing_for_a_name_no_knowledge_tree_resolves(monkeypatch):
    """The other side: absence from the registry is not by itself evidence of
    an adopter's crew, and this must not start reporting every typo PRESENT."""
    monkeypatch.setattr(CD.projects, "get", lambda s: None)
    monkeypatch.setattr(CD.projects, "resolve_project", lambda name, root=None: None)
    res, unit = CD.check_registry("nope", knowledge_root="/kb")
    assert res.state == CD.MISSING and unit is None


def test_check_home_derives_an_adopters_home_via_worktrees(monkeypatch, tmp_path):
    """`unit=None` is the adopter signal; the home comes from the same
    primary-checkout lookup `agentctl spawn` already uses for any repo, not
    from a `home_dir` field an adopter's entry does not have."""
    monkeypatch.setattr(CD.W, "desired_path", lambda repo, purpose: tmp_path)
    res, home = CD.check_home("tidytags", None)
    assert res.state == CD.PRESENT and home == tmp_path


def test_check_home_reports_missing_when_worktrees_cannot_resolve_the_repo(monkeypatch):
    def boom(repo, purpose):
        raise ValueError(f"unknown repo: {repo!r}; known primaries: []")
    monkeypatch.setattr(CD.W, "desired_path", boom)
    res, home = CD.check_home("tidytags", None)
    assert res.state == CD.MISSING and home is None
    assert "unknown repo" in res.detail


def test_diagnose_walks_every_site_for_an_adopters_crew(monkeypatch, tmp_path):
    """THE BUG THIS PINS. `tidytags`, individuated from the seeded default
    profile with no registry row, reported one line and stopped. Every site
    after the first must now run and report on its own evidence, the same as
    a registered crew's."""
    import sqlite3

    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "coder.md").write_text("x")
    (home / ".claude" / ".active-profile").write_text("tidytags/daytime\n")

    (tmp_path / "knowledge" / "projects" / "tidytags" / "profiles").mkdir(parents=True)
    (tmp_path / "knowledge" / "projects" / "tidytags" / "profiles" / "daytime.yaml"
     ).write_text("include:\n  - kind: reference\n    project: tidytags\n")

    index_db = tmp_path / "index.db"
    con = sqlite3.connect(index_db)
    con.execute("CREATE TABLE docs (id TEXT, kind TEXT, project TEXT, "
               "status TEXT, audience TEXT DEFAULT '[]', "
               "source_ref TEXT DEFAULT '', title TEXT DEFAULT '', body TEXT DEFAULT '')")
    con.execute("INSERT INTO docs (id, kind, project, status, audience) VALUES "
               "('tidytags/reference/one', 'reference', 'tidytags', 'current', '[]')")
    con.commit()
    con.close()

    monkeypatch.setattr(CD.projects, "get", lambda s: None)
    monkeypatch.setattr(CD.projects, "resolve_project",
                        lambda name, root=None: "tidytags" if name == "tidytags" else None)
    monkeypatch.setattr(CD.projects, "crew_for_repo",
                        lambda r: "tidytags" if r == "tidytags" else None)
    monkeypatch.setattr(CD.W, "desired_path", lambda repo, purpose: home)
    monkeypatch.setattr(CD, "resolve_profile_project", lambda h, **kw: ("tidytags", ""))
    monkeypatch.setattr(CD, "check_co_resolves",
                        lambda *a, **kw: CD.Result("co -r resolves the crew", "Bosun", CD.PRESENT))

    # The Library serves the profile this crew was individuated from. The site
    # reads the Library alone, so the checkout file is not part of this setup.
    monkeypatch.setattr(CD, "_served_profile", lambda *a, **kw: (
        {"profile": {"updated_at": "2026-09-01T00:00:00+00:00"}, "count": 1,
         "documents": [{"id": "tidytags/reference/one"}]}, ""))

    res = CD.diagnose("tidytags", derive_purpose=lambda *a, **k: "wt",
                      allowed={"tidytags"},
                      id_pattern=re.compile(r"^tidytags/[a-z]+/[a-z0-9-]+$"),
                      root=tmp_path, index_db=index_db)

    sites = [r.site for r in res]
    assert sites == [
        "registry entry (config/projects.json)",
        "home directory",
        "dispatch resolves (crew_for_repo)",
        "agent prefix dispatches to a worktree",
        "co -r resolves the crew",
        "KB slug admits a document",
        "knowledge-ctx resolves the crew's project",
        "profile served (tidytags/daytime)",
        "index serves the profile's documents",
        "a session finds agents here",
    ], sites
    bad = [(r.site, r.state, r.detail) for r in res if not r.ok]
    assert not bad, bad
    assert "no registry row" in res[0].detail
    assert _state(res, "agent prefix") == CD.NOT_APPLICABLE, \
        "slice 2's agent_prefix field does not exist for an adopter's entry either"
    profile = [r for r in res if "profile served" in r.site][0]
    assert "1 docs" in profile.detail, profile.detail


def test_diagnose_still_uses_the_registered_repos_for_a_registry_crew(monkeypatch, tmp_path):
    """The adopter branch must not swallow a registered crew's own declared
    repos into the single-name fallback -- several projects may share one crew
    (ARCH-426), so a registered unit's `repos` list has to stay whatever it
    declares."""
    unit = {"lifecycle": "active", "crew": "deck", "repos": ["convivy-deck", "deck-worker"],
            "kb": "central", "kb_project": "deck", "display": "Deck"}
    home = tmp_path / "home"
    home.mkdir()
    res = _wire(monkeypatch, tmp_path, unit=unit, home=home, project="deck")
    assert _state(res, "dispatch resolves") == CD.PRESENT
    dispatch = [r for r in res if "dispatch resolves" in r.site][0]
    assert "2 repo(s)" in dispatch.detail


# --- what --all walks ------------------------------------------------------

def test_all_walks_the_registry_rather_than_a_list_of_its_own(monkeypatch):
    """A crew registered tomorrow must be checked tomorrow with no edit here —
    a second hand-kept list of crews is the failure this epic exists to end."""
    monkeypatch.setattr(CD.projects, "units", lambda: {
        "live": {"lifecycle": "active"},
        "gone": {"lifecycle": "archived"},
        "also": {"lifecycle": "active"},
    })
    assert CD.active_crews() == ["also", "live"]


# --- the command itself ----------------------------------------------------
#
# THE ONE THING NOTHING CALLED WAS THE COMMAND (reviewer, PR #609). Every test
# above exercises `crew_doctor.py`'s pure functions, which is exactly the shape
# this PR argues against everywhere else: the thesis is "check by calling the
# thing", and the thing a human or a scheduled guard runs is
# `agentctl crew doctor`. These call it.

import argparse  # noqa: E402
import builtins  # noqa: E402
import subprocess  # noqa: E402


def _args(**kw):
    ns = argparse.Namespace(crew=None, all_crews=False, variant="daytime")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_the_command_refuses_with_no_crew_and_no_all(agentctl, capsys):
    """Exit 2, not 0 with an empty report. A doctor that checks nothing and says
    nothing is indistinguishable from a doctor that checked everything."""
    assert agentctl.cmd_crew_doctor(_args()) == 2
    assert "name a crew, or pass --all" in capsys.readouterr().err


def test_the_command_exits_non_zero_when_any_site_fails(agentctl, monkeypatch, capsys):
    """The exit code is what a scheduled guard reads, so it is pinned here
    rather than inferred from the printed lines."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: [CD.Result("a site", "Bosun", CD.MISSING, "gone")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "a site" in out and "[Bosun]" in out


def test_the_command_exits_zero_when_every_site_passes(agentctl, monkeypatch, capsys):
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: [CD.Result("a site", "Bosun", CD.PRESENT, "fine"),
                            CD.Result("another", "Library", CD.NOT_APPLICABLE, "n/a")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 0
    assert "OK" in capsys.readouterr().out


def test_one_failing_crew_fails_the_whole_all_run(agentctl, monkeypatch, capsys):
    """AGGREGATION IS THE POINT OF `--all`. A run over 22 crews that exits 0
    because the LAST one passed would be worse than no check: the guard would go
    green while a crew is broken."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    monkeypatch.setattr(CD, "active_crews", lambda: ["good", "bad", "alsogood"])
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: [CD.Result(
            "a site", "Bosun",
            CD.MISSING if crew == "bad" else CD.PRESENT, "")])
    assert agentctl.cmd_crew_doctor(_args(all_crews=True)) == 1
    out = capsys.readouterr().out
    assert out.count("crew doctor:") == 3, "every crew is reported, not just the first failure"
    assert "bad — 1 of 1" in out


def test_an_unchecked_site_fails_the_command_too(agentctl, monkeypatch, capsys):
    """UNKNOWN exits non-zero at the COMMAND level, not merely in the dataclass.
    The Library being unreachable must not read as a clean bill of health."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: [CD.Result("profile served", "Library", CD.UNKNOWN,
                                      "Library did not answer: connection refused")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 1
    assert "UNKNOWN" in capsys.readouterr().out


# --- the token an install actually writes -----------------------------------
#
# `SC.token()` reads `AGENT_SIGNAL_TOKEN` or `~/.orient-control/agent-platform-
# token` — a name only Convivy's own machine has minted by hand. An adopter
# install writes `library-token`, `library-client-token` and `ops-mcp-token`
# there and never `agent-platform-token`, so the doctor 401'd on every adopter
# crew while working here (issue_01M3NW81FC33701HQE3WV7HFRE).
#
# THE FALLBACK TOKEN NEVER PAIRS WITH THE ENV-DERIVED URL (security-reviewer,
# PR #708 round 2). `library-token`/`ops-mcp-token` resolve, in the Library's
# own identity.py, to an `agent`-/`system`-kind credential holding far more
# than the `roster.read` this command needs — `merge.admit`, `ledger.spend`,
# `item.resolve` among them — so it must go only to `vault_plane.
# fixed_library_url()`, never to `vault_plane.base_url()`, an address
# `LIBRARY_BASE_URL` lets the environment repoint anywhere `https://`. Every
# case below sets `LIBRARY_BASE_URL` to an address distinct from the fixed
# default so a test that accidentally reads the wrong one is caught rather
# than passing by coincidence.

_OFF_BOX_LIBRARY_URL = "https://library.elsewhere.example"


def test_sc_token_wins_when_present(agentctl, monkeypatch, tmp_path):
    """UNCHANGED for `SC.token()`'s other callers: when it resolves, nothing
    under `.orient-control` is even looked at, and it goes to `vault_plane.
    base_url()` exactly as before — including when that address is not the
    fixed default."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: "convivys-own-token")
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", True)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    auth = agentctl._doctor_auth()
    assert auth.token == "convivys-own-token"
    assert auth.base_url == _OFF_BOX_LIBRARY_URL == vault_plane.base_url()
    assert auth.pinned is False


def test_falls_back_to_library_token_an_install_writes(agentctl, monkeypatch, tmp_path):
    """THE BUG THIS PINS. No `AGENT_SIGNAL_TOKEN`, no `agent-platform-token` —
    exactly an adopter install — but `library-token` IS there, minted by the
    Library's own README specifically "for `agentctl` and Orient's own
    tooling", so the doctor sends it rather than 401ing — and it goes to the
    Library's FIXED default address, never the off-box one `LIBRARY_BASE_URL`
    names here."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    (tmp_path / "library-token").write_text("lib-tok\n")
    auth = agentctl._doctor_auth()
    assert auth.token == "lib-tok"
    assert auth.base_url == vault_plane.fixed_library_url("")
    assert auth.base_url != _OFF_BOX_LIBRARY_URL
    assert auth.pinned is True


def test_falls_back_to_ops_mcp_token_when_library_token_is_absent(agentctl, monkeypatch, tmp_path):
    """The ops MCP server's own credential — a real Library client in this
    repo, sending a token gated the same way (`roster.read`) — is the next
    fallback rather than a 401, and it is bound to the fixed address the same
    way `library-token` is."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    (tmp_path / "ops-mcp-token").write_text("ops-tok\n")
    auth = agentctl._doctor_auth()
    assert auth.token == "ops-tok"
    assert auth.base_url == vault_plane.fixed_library_url("")
    assert auth.base_url != _OFF_BOX_LIBRARY_URL
    assert auth.pinned is True


def test_library_token_is_preferred_over_ops_mcp_token(agentctl, monkeypatch, tmp_path):
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    (tmp_path / "library-token").write_text("lib-tok\n")
    (tmp_path / "ops-mcp-token").write_text("ops-tok\n")
    auth = agentctl._doctor_auth()
    assert auth.token == "lib-tok"
    assert auth.pinned is True


def test_no_token_anywhere_is_none_not_a_crash(agentctl, monkeypatch, tmp_path):
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path / "nonexistent")
    auth = agentctl._doctor_auth()
    assert auth.token is None
    assert auth.pinned is False
    assert auth.base_url == vault_plane.base_url()


def test_a_fallback_tokens_headers_carry_the_library_protocol_header_regardless_of_plane(
        agentctl, monkeypatch, tmp_path):
    """The fixed address is always the Library, so its headers must always
    carry `Library-Protocol` — even when `VAULTS_ON_LIBRARY=0` has this
    process pointed at Orient for everything else, which is the one case
    `vault_plane.headers()` would have omitted it."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)
    (tmp_path / "library-token").write_text("lib-tok\n")
    auth = agentctl._doctor_auth()
    assert auth.headers.get("Authorization") == "Bearer lib-tok"
    assert auth.headers.get(vault_plane.PROTOCOL_HEADER) == vault_plane.PROTOCOL_VERSION


# --- `_served_profile`'s opener: threaded through, not silently dropped -----

def test_served_profile_uses_the_given_opener_not_urlopen(monkeypatch):
    """A caller that hands `_served_profile` an opener (the doctor's
    fallback-token path) must have its request go through THAT opener — never
    the process-wide one `urlopen` reaches for, which is exactly the seam a
    broader-than-`roster.read` credential must not cross."""
    seen = {}

    class FakeOpener:
        def open(self, req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.headers)
            import io
            return io.BytesIO(b'{"profile": {}, "documents": []}')

    def boom(*a, **kw):
        raise AssertionError("must not call the process-wide urlopen")
    monkeypatch.setattr(CD.urllib.request, "urlopen", boom)

    body, err = CD._served_profile(
        "p", "daytime", base_url="http://127.0.0.1:8100",
        headers={"Authorization": "Bearer tok"}, opener=FakeOpener())
    assert err == "" and body == {"profile": {}, "documents": []}
    assert seen["url"] == "http://127.0.0.1:8100/profiles/p/daytime"
    assert seen["headers"].get("Authorization") == "Bearer tok"


def test_served_profile_without_an_opener_uses_urlopen_unchanged(monkeypatch):
    """UNCHANGED for every existing caller: no `opener` keeps using `urlopen`,
    the process-wide default `vault_plane` installs."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        import io
        return io.BytesIO(b'{"profile": {}, "documents": []}')
    monkeypatch.setattr(CD.urllib.request, "urlopen", fake_urlopen)

    body, err = CD._served_profile("p", "daytime", base_url="http://x", headers={})
    assert err == "" and seen["url"] == "http://x/profiles/p/daytime"


def test_a_redirect_from_the_fixed_url_is_not_followed_with_the_token(listeners):
    """END TO END through the real hardened opener: the fixed-address read's
    own `pinned_opener` is what the doctor's fallback-token path reuses, and
    this proves it actually refuses a redirect rather than merely being
    passed in unused."""
    elsewhere = listeners()
    target = listeners((302, {"Location": elsewhere.url + "/profiles/p/daytime"}, b""))
    body, err = CD._served_profile(
        "p", "daytime", base_url=target.url,
        headers={"Authorization": "Bearer lib-tok"},
        opener=issue_client.pinned_opener())
    assert body is None and err == "Library returned HTTP 302"
    assert len(target.requests) == 1
    assert elsewhere.requests == [], "the redirect target must never see the request or the token"


# --- `cmd_crew_doctor` end to end: the wiring the security review named ----

def test_the_command_sends_the_installs_token_when_sc_token_is_absent(
        agentctl, monkeypatch, tmp_path, capsys):
    """END TO END: `cmd_crew_doctor` itself, not just the helper, uses the
    fallback — the exact adopter-install scenario Shipwright measured — and
    pairs it with the fixed address and the pinned opener, never the
    off-box `LIBRARY_BASE_URL` set here."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    (tmp_path / "library-token").write_text("lib-tok\n")
    sentinel_opener = object()
    monkeypatch.setattr(issue_client, "pinned_opener", lambda: sentinel_opener)
    seen = {}
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: seen.update(kw) or [CD.Result("a site", "Bosun", CD.PRESENT, "fine")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 0
    assert seen["headers"].get("Authorization") == "Bearer lib-tok"
    assert seen["base_url"] == vault_plane.fixed_library_url("")
    assert seen["base_url"] != _OFF_BOX_LIBRARY_URL
    assert seen["opener"] is sentinel_opener


def test_the_command_sends_the_ops_mcp_fallback_to_the_fixed_url_too(
        agentctl, monkeypatch, tmp_path):
    monkeypatch.setattr(agentctl.SC, "token", lambda: None)
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    (tmp_path / "ops-mcp-token").write_text("ops-tok\n")
    sentinel_opener = object()
    monkeypatch.setattr(issue_client, "pinned_opener", lambda: sentinel_opener)
    seen = {}
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: seen.update(kw) or [CD.Result("a site", "Bosun", CD.PRESENT, "fine")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 0
    assert seen["headers"].get("Authorization") == "Bearer ops-tok"
    assert seen["base_url"] == vault_plane.fixed_library_url("")
    assert seen["base_url"] != _OFF_BOX_LIBRARY_URL
    assert seen["opener"] is sentinel_opener


def test_the_command_keeps_the_env_derived_url_when_sc_token_is_present(
        agentctl, monkeypatch, tmp_path):
    """The other side of the pairing: `SC.token()`'s own token still goes to
    `vault_plane.base_url()` — unchanged — with no pinned opener, even though
    that address is not the fixed default here."""
    monkeypatch.setattr(agentctl.SC, "token", lambda: "convivys-own-token")
    monkeypatch.setattr(agentctl, "_ORIENT_CONTROL_DIR", tmp_path)
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", True)
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", _OFF_BOX_LIBRARY_URL)
    (tmp_path / "library-token").write_text("lib-tok\n")   # present but must be ignored
    seen = {}
    monkeypatch.setattr(
        CD, "diagnose",
        lambda crew, **kw: seen.update(kw) or [CD.Result("a site", "Bosun", CD.PRESENT, "fine")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 0
    assert seen["headers"].get("Authorization") == "Bearer convivys-own-token"
    assert seen["base_url"] == _OFF_BOX_LIBRARY_URL == vault_plane.base_url()
    assert seen["opener"] is None


def test_the_subcommand_is_wired_into_the_real_parser():
    """`crew doctor --help` through the actual binary, so the argparse wiring —
    the subparser, `--all`, `--variant` — is exercised rather than assumed."""
    cp = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "agentctl"),
         "crew", "doctor", "--help"],
        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0, cp.stderr
    for flag in ("--all", "--variant"):
        assert flag in cp.stdout, cp.stdout


def test_naming_the_subcommand_without_a_verb_refuses():
    """`crew` alone is not a command; argparse must say so rather than default
    to something."""
    cp = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "agentctl"), "crew"],
        capture_output=True, text=True, timeout=120)
    assert cp.returncode != 0


# --- untrusted text on the way to a terminal -------------------------------
#
# `detail` carries another process's stderr, Library-served strings, document ids
# and filesystem paths, and `crew doctor --all` is meant to run unattended across
# every crew. That is the sink `primary_guard.safe_path` was built for in
# agent-platform#608 the same day (security-reviewer, PR #609).

@pytest.mark.parametrize("raw,must_not_contain", [
    ("erase\x1b[2K\x1b[1Ksafe", "\x1b"),   # ANSI: erases the line reporting the problem
    ("two\nlines", "\n"),                  # a second line forges a second site
    ("rtl‮gnp.txt", "‮"),        # bidi: displays as another path
])
def test_a_detail_cannot_hand_a_terminal_control_characters(raw, must_not_contain):
    line = CD.Result("a site", "Bosun", CD.MISSING, raw).line()
    assert must_not_contain not in line
    assert "a site" in line and "MISSING" in line


def test_every_detail_producer_is_covered_because_escaping_is_at_the_choke_point(tmp_path):
    """NOT ONE TEST PER PRODUCER. The four producers — co's stderr,
    knowledge-ctx's stderr, the Library's fields, the index's ids — escape
    because `Result.line` does it, so a fifth producer added later is covered
    without an edit. This asserts the property at the choke point, through a real
    producer, so it cannot pass by the producer happening to be clean."""
    fake = tmp_path / "co"
    fake.write_text("#!/bin/sh\nprintf 'co: no crew for \\033[2Kdir\\n' >&2\nexit 1\n")
    fake.chmod(0o755)
    res = CD.check_co_resolves("x", tmp_path, co_bin=fake)
    assert res.state == CD.MISSING
    assert "\x1b" in res.detail, "the raw detail really does carry the escape"
    assert "\x1b" not in res.line(), "and the rendered line does not"


def test_a_readable_detail_is_left_readable():
    """Escaping must not disturb what an operator has to recognise — including
    the non-ASCII names the KB legitimately holds."""
    for plain in ("128 docs",
                  "/srv/convivy/naïve-crew", "crew=signals label=Signals"):
        assert plain in CD.Result("s", "o", CD.PRESENT, plain).line()


def test_no_detail_is_built_with_repr():
    """ONE ESCAPING MECHANISM, HELD OVER THE WHOLE FILE.

    `repr()` escapes control bytes as a side effect of quoting, so an `!r` in a
    detail is a second, undeclared escape that compounds with `Result.line`'s —
    and is the one a later edit drops while tidying a format string, removing
    protection nobody knew was there.

    A per-site fix is what this replaces: round 2 removed the single `!r` a
    reviewer named and left six, including the Library-served profile detail,
    which is the sharpest case in the module. This is the family, checked as one.
    """
    src = (Path(__file__).resolve().parents[1]
           / "lib" / "agent-platform" / "crew_doctor.py").read_text()
    offenders = [ln.strip() for ln in src.splitlines()
                 if "!r}" in ln and not ln.lstrip().startswith("#")]
    assert offenders == [], f"use written quotes and let Result.line escape: {offenders}"


def test_the_command_refuses_rather_than_passing_when_the_validators_will_not_import(
        agentctl, monkeypatch, capsys):
    """THE ONE BRANCH ROUND 2'S TESTS DID NOT REACH (reviewer, PR #609).

    It is not deploy-window-only — `lib/knowledge/knowledge.py` is in-repo and
    the import can genuinely fail — so it carries no `pragma: no cover` and
    needs a test. What it must do is REFUSE: a doctor that cannot check the KB
    site must not print a report that looks like a pass, which is the whole
    failure mode this command exists to end.
    """
    real_import = builtins.__import__

    def no_knowledge(name, *a, **kw):
        if name == "knowledge":
            raise ImportError("no module named knowledge")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_knowledge)
    monkeypatch.delitem(sys.modules, "knowledge", raising=False)

    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 2
    captured = capsys.readouterr()
    assert "cannot use the KB validators" in captured.err
    assert "crew doctor:" not in captured.out, "it must not print a report at all"


@pytest.mark.parametrize("present,missing", [
    (("ID_PATTERN", "project_catalogue"), "ALLOWED_PROJECTS"),
    (("ALLOWED_PROJECTS", "project_catalogue"), "ID_PATTERN"),
    (("ALLOWED_PROJECTS", "ID_PATTERN"), "project_catalogue"),
    ((), "ALLOWED_PROJECTS, ID_PATTERN, project_catalogue"),
])
def test_a_validators_module_missing_an_attribute_refuses_and_names_it(
        agentctl, monkeypatch, capsys, present, missing):
    """THE LOGIC ROUND 3 SHIPPED WAS THE LOGIC ROUND 3 DID NOT TEST (reviewer,
    PR #609). The new branch exists for a module that IMPORTS and is unusable —
    the plain `ImportError` path was already covered — and nothing forced that
    case, so the reason the branch was rewritten went unverified.

    It must name the attribute that is actually absent. A module holding
    `ALLOWED_PROJECTS` but no `ID_PATTERN` reporting "has no
    ALLOWED_PROJECTS/ID_PATTERN" sends the reader to the wrong half of the file.
    """
    import types
    stub = types.ModuleType("knowledge")
    for name in present:
        setattr(stub, name, object())
    monkeypatch.setitem(sys.modules, "knowledge", stub)

    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 2
    captured = capsys.readouterr()
    assert f"has no {missing}" in captured.err, captured.err
    assert "crew doctor:" not in captured.out, "it must not print a report at all"


def test_a_complete_validators_module_is_not_refused(agentctl, monkeypatch):
    """The other side, so the refusal cannot be passing by refusing everything.
    A module carrying both names gets through to the real checks."""
    import types
    stub = types.ModuleType("knowledge")
    stub.ALLOWED_PROJECTS = {"x"}
    stub.ID_PATTERN = re.compile(r"^x/[a-z]+/[a-z0-9-]+$")
    stub.project_catalogue = lambda root: types.SimpleNamespace(
        allowed={"x"}, id_pattern=stub.ID_PATTERN)
    monkeypatch.setitem(sys.modules, "knowledge", stub)
    monkeypatch.setattr(agentctl.SC, "token", lambda: "t")
    monkeypatch.setattr(CD, "diagnose",
                        lambda crew, **kw: [CD.Result("s", "o", CD.PRESENT, "")])
    assert agentctl.cmd_crew_doctor(_args(crew="x")) == 0


# --- check_profile_served's outcomes ---------------------------------------
#
# The Library is this site's only source. The checkout file is never read, so its
# presence and its commit date cannot change the verdict.

_LOADED = "2026-09-16T21:11:00+00:00"
_SERVED = ({"profile": {"updated_at": _LOADED}, "count": 5, "documents": []}, "")


def _profile_case(tmp_path, monkeypatch, *, served, on_disk=True):
    """One `check_profile_served` call with the Library answer stubbed."""
    if on_disk:
        (tmp_path / "projects" / "p" / "profiles").mkdir(parents=True, exist_ok=True)
        (tmp_path / "projects" / "p" / "profiles" / "daytime.yaml").write_text("x")
    monkeypatch.setattr(CD, "_served_profile", lambda *a, **kw: served)
    return CD.check_profile_served("p", variant="daytime",
                                   base_url="http://x", headers={})


@pytest.mark.parametrize("on_disk", [True, False], ids=["checkout-has-file", "no-checkout-file"])
def test_a_served_profile_is_present_whatever_the_checkout_holds(tmp_path, monkeypatch, on_disk):
    """THE BUG THIS PINS. Every correctly served crew read MISSING on a machine
    with no knowledge checkout, because the check looked for the checkout file.
    A checkout file, present or absent, does not change a served profile's verdict."""
    res, _ = _profile_case(tmp_path, monkeypatch, served=_SERVED, on_disk=on_disk)
    assert res.state == CD.PRESENT, res.detail
    assert "5 docs" in res.detail


@pytest.mark.parametrize("on_disk", [True, False], ids=["checkout-has-file", "no-checkout-file"])
def test_a_profile_the_library_does_not_serve_is_missing_whatever_the_checkout_holds(
        tmp_path, monkeypatch, on_disk):
    res, _ = _profile_case(tmp_path, monkeypatch, served=(None, "404"), on_disk=on_disk)
    assert res.state == CD.MISSING
    assert "not served by the Library" in res.detail


def test_an_unreachable_library_is_unchecked_and_the_detail_names_the_library(tmp_path, monkeypatch):
    """The steward read a WAL-switch `unable to open database file` as a
    permissions problem. A doctor calling that a broken crew sends the next
    reader the same way. This drives the real `_served_profile` through a failed
    `urlopen`, so the detail is the text the Library path produces."""
    import urllib.error

    def down(*a, **kw):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(CD.urllib.request, "urlopen", down)
    res, _ = CD.check_profile_served("p", variant="daytime",
                                     base_url="http://x", headers={})
    assert res.state == CD.UNKNOWN
    assert res.detail.startswith("Library did not answer"), res.detail


def test_the_resolved_documents_flow_on_to_the_index_check(tmp_path, monkeypatch):
    """The second return value is what `check_index_serves` consumes, so a
    profile check that reports correctly while returning nothing would silently
    make the index site vacuous."""
    _, doc_ids = _profile_case(
        tmp_path, monkeypatch,
        served=({"profile": {"updated_at": _LOADED}, "count": 2,
                 "documents": [{"id": "a/one"}, {"id": "b/two"}, {"no": "id"}]}, ""))
    assert doc_ids == ["a/one", "b/two"], "entries without an id are dropped, not crashed on"


@pytest.mark.parametrize("code,expect", [
    (500, "Library returned HTTP 500"), (403, "Library returned HTTP 403")])
def test_a_library_error_other_than_404_is_unchecked(code, expect, monkeypatch):
    """`_served_profile`'s non-404 branch. A 500 is the Library failing, not the
    crew — it must not read as an absent profile, and the detail says whose
    failure it is."""
    import urllib.error

    def boom(*a, **kw):
        raise urllib.error.HTTPError("u", code, "err", {}, None)

    monkeypatch.setattr(CD.urllib.request, "urlopen", boom)
    body, err = CD._served_profile("p", "daytime", base_url="http://x", headers={})
    assert body is None and err == expect


# --- the three siblings nobody had looked at (PR #609 round 5) --------------
#
# Same gap as round 2's (the CLI entrypoint) and round 4's (check_profile_served),
# recurring in three functions: check_home, check_kb_admits and
# resolve_profile_project had no coverage, direct or indirect. The one diagnose()
# test stops at the registry check before reaching them, and every CLI test stubs
# diagnose entirely.

def test_a_crew_with_no_home_dir_in_its_entry_fails(monkeypatch):
    monkeypatch.setattr(CD.projects, "home_path", lambda slug: None)
    res, home = CD.check_home("x", {})
    assert res.state == CD.MISSING and home is None


def test_a_home_dir_that_does_not_exist_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(CD.projects, "home_path", lambda slug: tmp_path / "nope")
    res, home = CD.check_home("x", {})
    assert res.state == CD.MISSING and home is None
    assert "does not exist" in res.detail


def test_a_home_dir_that_exists_passes_and_is_returned(monkeypatch, tmp_path):
    """The path is RETURNED, not just checked — every later site runs against
    it, so a check that passed while handing back None would break the rest."""
    monkeypatch.setattr(CD.projects, "home_path", lambda slug: tmp_path)
    res, home = CD.check_home("x", {})
    assert res.state == CD.PRESENT and home == tmp_path


_ID = re.compile(r"^(alpha|beta)/[a-z][a-z-]*/[a-z0-9][a-z0-9-]*$")


def test_a_local_kb_crew_is_not_asked_about_the_central_slug():
    """`kb: local` declares all knowledge lives in the crew's own repo, so this
    is not a site for it — reporting MISSING would be the check disagreeing with
    the registry about what the crew is."""
    assert CD.check_kb_admits("x", {"kb": "local", "kb_project": None},
                              allowed={"alpha"}, id_pattern=_ID
                              ).state == CD.NOT_APPLICABLE


def test_a_slug_outside_allowed_projects_fails():
    res = CD.check_kb_admits("x", {"kb": "central", "kb_project": "gamma"},
                             allowed={"alpha"}, id_pattern=_ID)
    assert res.state == CD.MISSING and "ALLOWED_PROJECTS" in res.detail


def test_a_slug_the_id_pattern_rejects_fails():
    """ALLOWED_PROJECTS and ID_PATTERN are two separate gates and a crew can
    pass one and fail the other — which is the shape that lets a crew be
    dispatchable and unable to write."""
    res = CD.check_kb_admits("x", {"kb": "central", "kb_project": "beta"},
                             allowed={"beta"}, id_pattern=re.compile("^alpha/"))
    assert res.state == CD.MISSING and "ID_PATTERN" in res.detail


def test_a_slug_both_gates_admit_passes():
    assert CD.check_kb_admits("x", {"kb": "central", "kb_project": "alpha"},
                              allowed={"alpha"}, id_pattern=_ID).state == CD.PRESENT


def _fake_kctx(tmp_path, *, stdout="", stderr="", rc=0):
    p = tmp_path / "knowledge-ctx"
    p.write_text(f"#!/bin/sh\nprintf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit {rc}\n")
    p.chmod(0o755)
    return p


def test_the_project_comes_from_what_knowledge_ctx_says(tmp_path):
    """THE SHARPEST OF THE THREE. This regex is the only thing between
    knowledge-ctx's stdout and the `project` string that the profile URL and the
    agent-directory check are both keyed on, and it had never been driven by a
    real or faked invocation. Run against a fake binary, not a stubbed parse."""
    kctx = _fake_kctx(tmp_path, stdout="profiles for shipwright:\\n  daytime\\n")
    project, why = CD.resolve_profile_project(tmp_path, knowledge_ctx=kctx)
    assert project == "shipwright" and why == ""


def test_a_missing_knowledge_ctx_is_unchecked_not_a_broken_crew(tmp_path):
    project, why = CD.resolve_profile_project(tmp_path, knowledge_ctx=tmp_path / "nope")
    assert project is None and "not found" in why


def test_knowledge_ctx_refusing_the_directory_reports_its_reason(tmp_path):
    """The real failure for cfo, operator and the nyh crews — the reason has to
    survive to the report, or the operator is told only that something is wrong."""
    kctx = _fake_kctx(tmp_path, stderr="knowledge-ctx: no project mapping for cwd\\n", rc=1)
    project, why = CD.resolve_profile_project(tmp_path, knowledge_ctx=kctx)
    assert project is None and "no project mapping" in why


def test_output_the_regex_cannot_parse_is_reported_rather_than_guessed(tmp_path):
    """A zero exit with unexpected output must not yield a wrong project: every
    downstream site is keyed on this string."""
    kctx = _fake_kctx(tmp_path, stdout="something else entirely\\n")
    project, why = CD.resolve_profile_project(tmp_path, knowledge_ctx=kctx)
    assert project is None and "named no project" in why


# --- the two private helpers (PR #609 round 6) ------------------------------

def test_own_bin_prefers_this_checkout_over_the_deployed_copy(tmp_path):
    """THE BUG THIS PINS SHIPPED IN AN EARLIER ROUND OF THIS PR. `_own_bin` used
    `parent.parent`, copied from `bin/agentctl`'s own lookup — but this module
    sits one level deeper, at lib/agent-platform/, so that resolved to `lib/bin`,
    silently fell through to the DEPLOYED binary, and every crew reported UNKNOWN
    with a detail about `co` having no `--resolve`. The failure read as a finding
    about the crews.

    A checkout must exercise its OWN tooling: `co --resolve` does not exist on
    main while this branch is in review, so resolving to the deployed copy makes
    the check unable to answer.
    """
    here = Path(__file__).resolve().parents[1]
    got = CD._own_bin("co", tmp_path)
    assert got == here / "bin" / "co", got
    assert got.exists(), "the checkout's own bin/co, not lib/bin/co"


def test_own_bin_falls_back_to_the_machine_root_when_the_checkout_lacks_it(tmp_path):
    """The fallback is what lets a deployed agentctl work outside a checkout."""
    got = CD._own_bin("definitely-not-a-real-bin", tmp_path)
    assert got == tmp_path / "agent-platform" / "bin" / "definitely-not-a-real-bin"


# --- diagnose()'s own wiring (PR #609 round 7) ------------------------------
#
# Every SITE had coverage; the function that COMPOSES them did not, beyond the
# early registry stop. The decisions below are made in `diagnose` and nowhere
# else, so a site passing in isolation says nothing about them — and one of them,
# the variant selection, is where a real bug lived (ea and convivy-commercial
# reported broken for running their own profiles).

def _wire(monkeypatch, tmp_path, *, unit, home=None, project="p",
          served=None, kctx_ok=True):
    """`diagnose` with only its I/O boundaries stubbed."""
    monkeypatch.setattr(CD.projects, "get", lambda s: unit)
    monkeypatch.setattr(CD.projects, "home_path", lambda s: home)
    monkeypatch.setattr(CD.projects, "crew_for_repo", lambda r: (unit or {}).get("crew"))
    monkeypatch.setattr(CD, "_own_bin", lambda name, base: tmp_path / name)
    monkeypatch.setattr(CD, "resolve_profile_project",
                        lambda h, **kw: (project, "") if kctx_ok else (None, "no mapping"))
    monkeypatch.setattr(CD, "_served_profile", lambda *a, **kw: served or (
        {"profile": {"updated_at": "2026-01-01T00:00:00+00:00"}, "count": 1,
         "documents": [{"id": "a/one"}]}, ""))
    monkeypatch.setattr(CD, "check_co_resolves",
                        lambda *a, **kw: CD.Result("co -r resolves the crew", "Bosun", CD.PRESENT))
    return CD.diagnose("x", derive_purpose=lambda *a, **k: "wt",
                       allowed={"p"}, id_pattern=re.compile(r"^p/[a-z]+/[a-z0-9-]+$"),
                       root=tmp_path, index_db=tmp_path / "none.db")


_ACTIVE = {"lifecycle": "active", "crew": "x", "repos": ["r"], "kb": "central",
           "kb_project": "p", "display": "X"}


def test_diagnose_stops_when_the_home_directory_is_missing(monkeypatch, tmp_path):
    """Reporting eight more MISSING lines for a crew whose directory is not there
    buries the one line that matters."""
    res = _wire(monkeypatch, tmp_path, unit=_ACTIVE, home=tmp_path / "nope")
    assert [r.site for r in res] == ["registry entry (config/projects.json)", "home directory"]
    assert res[-1].state == CD.MISSING


def test_diagnose_skips_the_central_sites_for_a_local_kb_crew(monkeypatch, tmp_path):
    """The short-circuit lives in `diagnose`, not in any site. A `kb: local` crew
    keeps all knowledge in its own repo, so the profile, index and individuation
    sites are not sites for it — reporting them MISSING would be the checker
    disagreeing with the registry about what the crew is."""
    home = tmp_path / "home"
    home.mkdir()
    res = _wire(monkeypatch, tmp_path, unit={**_ACTIVE, "kb": "local"}, home=home)
    sites = [r.site for r in res]
    assert "central knowledge sites" in sites
    assert not any("profile served" in s for s in sites)
    assert res[-1].state == CD.NOT_APPLICABLE


def test_diagnose_stops_when_knowledge_ctx_cannot_name_the_project(monkeypatch, tmp_path):
    """Everything downstream is keyed on that string, so continuing with a guess
    would report the wrong profile as this crew's."""
    home = tmp_path / "home"
    home.mkdir()
    res = _wire(monkeypatch, tmp_path, unit=_ACTIVE, home=home, kctx_ok=False)
    assert res[-1].site == "knowledge-ctx resolves the crew's project"
    assert res[-1].state == CD.UNKNOWN and "no mapping" in res[-1].detail
    assert not any("profile served" in r.site for r in res)


def test_diagnose_checks_the_variant_the_crew_actually_runs(monkeypatch, tmp_path):
    """THE LINE A REAL BUG LIVED ON. `ea` runs `platform/ea-daytime` and
    `convivy-commercial` runs `company/commercial`; a hardcoded `daytime`
    reported both broken for running their own profiles. The selection happens
    in `diagnose`, so no site test reaches it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".active-profile").write_text("p/ea-daytime\n")
    res = _wire(monkeypatch, tmp_path, unit=_ACTIVE, home=home)
    assert any("profile served (p/ea-daytime)" == r.site for r in res), [r.site for r in res]


def test_an_explicit_variant_still_wins_over_the_crews_own(monkeypatch, tmp_path):
    """So a caller can ask about a variant the crew does not run."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".active-profile").write_text("p/ea-daytime\n")
    monkeypatch.setattr(CD.projects, "get", lambda s: _ACTIVE)
    monkeypatch.setattr(CD.projects, "home_path", lambda s: home)
    monkeypatch.setattr(CD.projects, "crew_for_repo", lambda r: "x")
    monkeypatch.setattr(CD, "_own_bin", lambda name, base: tmp_path / name)
    monkeypatch.setattr(CD, "resolve_profile_project", lambda h, **kw: ("p", ""))
    monkeypatch.setattr(CD, "_served_profile", lambda *a, **kw: (None, "404"))
    monkeypatch.setattr(CD, "check_co_resolves",
                        lambda *a, **kw: CD.Result("co", "Bosun", CD.PRESENT))
    res = CD.diagnose("x", derive_purpose=lambda *a, **k: "wt", allowed={"p"},
                      id_pattern=re.compile("^p/"), variant="overnight",
                      root=tmp_path, index_db=tmp_path / "none.db")
    assert any("profile served (p/overnight)" == r.site for r in res), [r.site for r in res]


def test_the_profiles_documents_are_what_the_index_site_is_asked_about(monkeypatch, tmp_path):
    """THE HANDOFF, which lives in `diagnose`. `check_profile_served` returns the
    resolved ids and `check_index_serves` consumes them; if the wiring dropped
    them, the index site would report on an empty list and pass vacuously for
    every crew."""
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "coder.md").write_text("x")
    (home / ".claude" / ".active-profile").write_text("p/daytime\n")
    seen = {}
    monkeypatch.setattr(CD, "check_index_serves",
                        lambda ids, **kw: seen.setdefault("ids", ids) and None
                        or CD.Result("index", "Bosun", CD.PRESENT))
    _wire(monkeypatch, tmp_path, unit=_ACTIVE, home=home,
          served=({"profile": {"updated_at": "2026-01-01T00:00:00+00:00"}, "count": 2,
                   "documents": [{"id": "a/one"}, {"id": "b/two"}]}, ""))
    assert seen["ids"] == ["a/one", "b/two"], seen
