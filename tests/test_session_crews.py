"""`session-agent crews` — the crew specs new-crew writes, served to Deck.

The failure these cover is the one that happened: placard was created through
the only sanctioned path on 2026-09-05, its spec sat in crews.d with no
`project`, and Deck drew a rail row with no cockpit until the operator set the field by
hand. So the tests drive real files in a temp crews.d against a temp registry
and a temp index, and the property under test is that a spec written the way
the skill writes it is enough on its own.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import projects
import pytest
import session_crews as SCR

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "lib" / "knowledge" / "schema.sql"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host: crews.d, a registry with one project whose home is under the
    fake HOME, and an index holding that project's bead. Returns the crews.d
    path; each test writes the specs it needs."""
    home = tmp_path / "home"
    (home / "Convivy" / "placard").mkdir(parents=True)
    (home / "Convivy" / "shipwright" / "inbox").mkdir(parents=True)
    (home / "Convivy" / "EA" / "mail").mkdir(parents=True)
    (home / "Convivy" / "old-thing").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(SCR, "_real_account_home", lambda: home)
    # Registry homes resolve against the machine's root, which ignores HOME; the
    # explicit override is how a test moves it (company/decisions/0008).
    monkeypatch.setattr(SCR.projects, "ROOT_OVERRIDE", home / "Convivy")

    registry = tmp_path / "projects.json"
    unit = {"class": "project", "display": "X", "owner": "convivy", "dirs": [],
            "crew": None, "lifecycle": "active", "products": [], "aliases": [],
            "kb": "local"}
    registry.write_text(json.dumps({
        "placard": {**unit, "home_dir": "~/Convivy/placard", "repos": ["placard"]},
        "shipwright": {**unit, "home_dir": "~/Convivy/shipwright", "repos": ["shipwright"]},
        "ea": {**unit, "home_dir": "~/Convivy/EA", "repos": ["EA"], "private": True},
        "old-thing": {**unit, "home_dir": "~/Convivy/old-thing", "repos": ["old-thing"],
                      "lifecycle": "sunset"},
    }))
    monkeypatch.setattr(projects, "PROJECTS_JSON", registry)
    projects._load_raw.cache_clear()

    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA.read_text())
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, body, "
        "body_hash, updated_at) VALUES (?, 'files', ?, 'bead', ?, 't', 'current', 'b', 'h', 'now')",
        ("placard/bead/placard", "projects/placard/bead/placard.md", "placard"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(SCR, "KB_INDEX", db)

    crews_d = home / ".config" / "convivy" / "deck" / "crews.d"
    crews_d.mkdir(parents=True)
    monkeypatch.setattr(SCR, "CREWS_D", crews_d)
    yield crews_d
    projects._load_raw.cache_clear()


def _spec(crews_d: Path, crew_id: str, **fields) -> Path:
    body = {"id": crew_id, "displayName": crew_id.title(),
            "workingDirectory": f"{crews_d.parents[3]}/Convivy/{crew_id}",
            "sessionId": "$58", "host": "mini", "sessions": []}
    body.update(fields)
    p = crews_d / f"{crew_id}.json"
    p.write_text(json.dumps(body))
    return p


def test_no_crews_d_reads_as_no_crews(host):
    """A poll must not error a rail over a host that has never created a crew."""
    host.rmdir()
    assert SCR.crews() == {"crews": []}


def test_a_spec_as_the_skill_writes_it_is_enough_for_a_cockpit(host):
    """The placard case. No project, no bead in the file; the host fills both
    from the registry and the index, and says which it filled."""
    _spec(host, "placard")
    row = SCR.crews()["crews"][0]
    assert row["id"] == "placard"
    assert row["project"] == "placard"
    assert row["bead"] == "placard/bead/placard"
    assert row["derived"] == ["project", "bead"]
    assert row["sessionId"] == "$58"


def test_a_spec_that_chose_its_fields_keeps_them(host):
    """`bead` and `project` are orthogonal on purpose: shipwright works the
    Builder's bead under its own project."""
    _spec(host, "shipwright", bead="company/bead/convivy-builder")
    row = SCR.crews()["crews"][0]
    assert row["project"] == "shipwright"
    assert row["bead"] == "company/bead/convivy-builder"
    assert row["derived"] == ["project"]
    _spec(host, "placard", project="shipwright", bead="platform/bead/platform")
    row = next(r for r in SCR.crews()["crews"] if r["id"] == "placard")
    assert row["project"] == "shipwright" and row["bead"] == "platform/bead/platform"
    assert row["derived"] == []


def test_a_private_crews_row_is_not_served(host):
    """ADR-0043, the posture run-register-api already takes for /runs and
    /sessions: a black-box crew's row is dropped whole, not field-redacted. EA's
    spec is on the host today with its real directory and `project: ea`."""
    _spec(host, "placard")
    _spec(host, "ea", project="ea", workingDirectory=f"{host.parents[3]}/Convivy/EA")
    assert [r["id"] for r in SCR.crews()["crews"]] == ["placard"]


def test_a_spec_cannot_claim_a_private_project_by_name(host):
    """A public directory naming `project: ea` would bind a cockpit to a private
    crew's Library content; the row is withheld, never served with the field
    stripped."""
    _spec(host, "placard", project="ea")
    assert SCR.crews()["crews"] == []


def test_a_directory_inside_a_private_home_is_withheld_even_unnamed(host):
    """The second path the security reviewer named: no `project` in the spec, a
    `workingDirectory` under (or symlinked into) a private home. Inode, not
    string: the symlink case is the one a string compare misses."""
    _spec(host, "placard", workingDirectory=f"{host.parents[3]}/Convivy/EA/mail")
    assert SCR.crews()["crews"] == []
    link = host.parents[3] / "ea-link"
    link.symlink_to(host.parents[3] / "Convivy" / "EA")
    _spec(host, "placard", workingDirectory=str(link))
    assert SCR.crews()["crews"] == []


def test_an_unreadable_registry_serves_nothing(host, monkeypatch):
    """Cannot tell is not public. A registry hiccup costs a poll's worth of
    rows; the other direction costs a private crew's path on every poll."""
    _spec(host, "placard")
    missing = host.parents[3] / "no-registry.json"
    monkeypatch.setattr(projects, "PROJECTS_JSON", missing)
    projects._load_raw.cache_clear()
    assert SCR.crews() == {"crews": []}


def test_a_sunset_project_is_not_a_cockpits_home(host):
    """The walk covers active slugs only, as the contract says: a crew whose
    directory sits under a wound-down project's home gets no derived project,
    and its row is served with the field empty rather than pointed at history."""
    _spec(host, "old-thing", workingDirectory=f"{host.parents[3]}/Convivy/old-thing")
    row = SCR.crews()["crews"][0]
    assert row["id"] == "old-thing"
    assert row["project"] is None
    assert row["derived"] == []


def test_a_named_project_must_be_a_registered_slug(host):
    """A spec naming a project the registry does not know is treated as naming
    none, so the cockpit derives from the directory instead."""
    _spec(host, "placard", project="not-a-project")
    row = SCR.crews()["crews"][0]
    assert row["project"] == "placard"
    assert row["derived"] == ["project", "bead"]


def test_a_cockpit_inside_the_project_home_still_resolves(host):
    """The lookup walks up, by inode, so a subdirectory of the home is that
    project, and a symlinked spelling of the path is too."""
    _spec(host, "shipwright", workingDirectory=f"{host.parents[3]}/Convivy/shipwright/inbox")
    assert SCR.crews()["crews"][0]["project"] == "shipwright"
    link = host.parents[3] / "pl-link"
    link.symlink_to(host.parents[3] / "Convivy" / "placard")
    _spec(host, "placard", workingDirectory=str(link))
    row = next(r for r in SCR.crews()["crews"] if r["id"] == "placard")
    assert row["project"] == "placard"


def test_a_file_that_is_not_a_spec_is_skipped_and_the_rest_served(host):
    """Deck skips a row it cannot parse; this host never serves one it cannot
    vouch for. Bad JSON, an id that disagrees with the filename, and an id
    outside the session-name charset are each a skipped file, nothing more."""
    _spec(host, "placard")
    (host / "broken.json").write_text("{not json")
    (host / "renamed.json").write_text(json.dumps({"id": "other", "displayName": "x"}))
    (host / "bad id.json").write_text(json.dumps({"id": "bad id"}))
    (host / "notes.txt").write_text("ignored")
    out = SCR.crews()["crews"]
    assert [r["id"] for r in out] == ["placard"]


def test_fields_are_typed_bounded_and_unknown_keys_dropped(host):
    """Every field is data from another process. A control character in a path,
    a child session with a bad tmux id, and a key the contract does not name
    all fall away rather than reach Deck. (A control character in the
    directory withholds the whole row; see the relative-path test.)"""
    _spec(host, "placard",
          sessionId="58", secret="drop me",
          sessions=[{"name": "placard--build", "displayName": "Build", "sessionId": "$19"},
                    {"name": "placard--bad", "sessionId": "nope"},
                    {"name": "bad name!"}, "not a row"])
    row = SCR.crews()["crews"][0]
    assert row["sessionId"] is None
    assert "secret" not in row
    assert [c["name"] for c in row["sessions"]] == ["placard--build", "placard--bad"]
    assert row["sessions"][1]["sessionId"] is None
    assert set(row) == {"id", "displayName", "workingDirectory", "sessionId", "host",
                        "project", "bead", "sessions", "derived"}


def test_a_relative_working_directory_withholds_the_row(host):
    """Without a usable directory the privacy question has no answer, and the
    skill says the field must never be omitted, so the row is not served."""
    _spec(host, "placard", workingDirectory="Convivy/placard")
    assert SCR.crews()["crews"] == []


def test_an_absent_index_costs_the_bead_and_nothing_else(host, monkeypatch):
    monkeypatch.setattr(SCR, "KB_INDEX", host.parents[3] / "missing.db")
    _spec(host, "placard")
    row = SCR.crews()["crews"][0]
    assert row["project"] == "placard"
    assert row["bead"] is None
    assert row["derived"] == ["project"]


def test_two_beads_for_a_project_default_to_none(host):
    """Guessing between two beads is worse than none."""
    conn = sqlite3.connect(str(SCR.KB_INDEX))
    conn.execute(
        "INSERT INTO docs (id, source, source_ref, kind, project, title, status, body, "
        "body_hash, updated_at) VALUES ('placard/bead/other', 'files', 'x', 'bead', "
        "'placard', 't', 'current', 'b', 'h2', 'now')")
    conn.commit()
    conn.close()
    _spec(host, "placard")
    assert SCR.crews()["crews"][0]["bead"] is None


def test_the_name_charset_is_the_one_every_verb_shares():
    """Copied into session_crews rather than imported (see the module's import
    note); this is what keeps the copy honest."""
    import tmux_transcript as TT
    assert SCR.SESSION_NAME_RE.pattern == TT.SESSION_NAME_RE.pattern


def test_the_caps_hold_and_truncate_silently(host):
    """The contract calls the caps load-bearing and truncation silent, so a
    refactor that dropped a slice must fail here rather than at crew #201."""
    big = host / "big.json"
    big.write_text(json.dumps({"id": "big", "workingDirectory": str(host.parents[3] / "Convivy" / "placard"),
                               "pad": "x" * (SCR.MAX_SPEC_BYTES + 1)}))
    assert [r["id"] for r in SCR.crews()["crews"]] == []
    big.unlink()

    children = [{"name": f"placard--{i}", "sessionId": f"${i}"} for i in range(SCR.MAX_CHILD_SESSIONS + 5)]
    _spec(host, "placard", host="h" * (SCR.MAX_TEXT + 1), sessions=children)
    row = SCR.crews()["crews"][0]
    assert row["host"] is None
    assert len(row["sessions"]) == SCR.MAX_CHILD_SESSIONS

    for i in range(SCR.MAX_CREWS + 3):
        _spec(host, f"c{i:03d}", workingDirectory=str(host.parents[3] / "Convivy" / "placard"))
    assert len(SCR.crews()["crews"]) == SCR.MAX_CREWS


def test_one_bad_spec_costs_one_row_never_the_listing(host):
    """A 4 KiB file of nested brackets raised RecursionError out of json.loads,
    which is a RuntimeError and slipped every except clause, and the whole
    listing went down on every poll until the file was removed. One file, one
    row (security-reviewer, #507 round 1)."""
    _spec(host, "placard")
    (host / "nested.json").write_text("[" * 20000 + "]" * 20000)
    assert [r["id"] for r in SCR.crews()["crews"]] == ["placard"]


def test_an_unreadable_private_home_withholds_rather_than_serves(host, monkeypatch):
    """Cannot tell is withheld on every path, including a samefile that raises
    for the one private home that would have matched."""
    _spec(host, "placard")
    real = SCR.os.path.samefile

    def flaky(a, b):
        if str(b).endswith("/EA"):
            raise OSError("transient")
        return real(a, b)
    monkeypatch.setattr(SCR.os.path, "samefile", flaky)
    assert SCR.crews()["crews"] == []


def test_the_size_cap_is_a_bounded_read_not_a_stat(host, monkeypatch):
    """The cap holds on what is read. A stat that reports the file small (the
    writer swapped it after the stat) must not let an over-cap file through;
    against the stat-then-read version this served the row."""
    import os

    real_stat = os.stat

    def lying_stat(p, *a, **kw):
        st = real_stat(p, *a, **kw)
        if str(p).endswith("placard.json"):
            return os.stat_result((*st[:6], 10, *st[7:]))
        return st
    monkeypatch.setattr(os, "stat", lying_stat)
    big = host / "placard.json"
    big.write_text(json.dumps({"id": "placard", "workingDirectory": str(host.parents[3] / "Convivy" / "placard"),
                               "pad": "x" * (SCR.MAX_SPEC_BYTES + 1)}))
    assert SCR.crews()["crews"] == []


def test_crews_are_sorted_by_id(host):
    for cid in ("shipwright", "placard"):
        _spec(host, cid)
    assert [r["id"] for r in SCR.crews()["crews"]] == ["placard", "shipwright"]
