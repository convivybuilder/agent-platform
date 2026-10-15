"""One resolver for directory/repo name -> KB project, and the default-profile
fallback, shared by bin/knowledge-ctx and bin/agentctl.

Every test builds its own KNOWLEDGE_ROOT under tmp_path; none reads the live KB.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import projects

# The table as it stood before the two copies were merged (both were equal).
# Enumerated, not sampled: a changed or dropped row must fail here.
EXPECTED = {
    'constance': 'constance',
    'vivy': 'vivy',
    'sous-chef': 'sous-chef',
    'vivy-ownerrez-sandbox-tools': 'vivy',
    'vivy-commercial': 'vivy',
    'constance-commercial': 'constance',
    'constance-updates': 'constance-landing',
    'convivy-commercial': 'company',
    'dev-setup': 'platform',
    'agent-platform': 'agent-platform',
    'knowledge': 'knowledge',
    'selector': 'selector',
    'hollis': 'hollis',
    'convivy-app-kit': 'convivy-app-kit',
    'convivy-services': 'convivy-services',
    'convivy-orient': 'convivy-orient',
    'www': 'www',
    'convivy-deck': 'deck',
    'placard': 'placard',
    'selector-backend': 'selector',
    'constance-landing': 'constance-landing',
    'vivy-landing': 'vivy-landing',
    'substrate': 'platform',
    'EA': 'platform',
    'shipwright': 'shipwright',
    'homebrew-tap': 'shipwright',
    'signals': 'signals',
    'write-better': 'write-better',
    'library': 'library',
    'archy': 'archy',
}

DEFAULT_YAML = "name: default\n"


@pytest.fixture(autouse=True)
def _no_unfaked_library(monkeypatch):
    """Every Library call ends in `urllib.request.urlopen`. A test that reaches it
    without the `_library_serves` double fails here, loudly, rather than sending
    a request with the client token to the live Library."""
    def unfaked(req, timeout=None):
        raise AssertionError(
            f"a test reached the Library unfaked: {req.get_method()} {req.full_url}")
    monkeypatch.setattr("urllib.request.urlopen", unfaked)


def test_the_guard_refuses_a_library_call_rather_than_sending_it(monkeypatch):
    """Proves the guard above is live: a raw request to a `.invalid` host (which
    can never resolve) must raise the guard's AssertionError, not a URLError
    from the network, so no request is ever sent."""
    import urllib.request
    req = urllib.request.Request("http://library.invalid/profiles")
    with pytest.raises(AssertionError, match="reached the Library unfaked"):
        urllib.request.urlopen(req, timeout=1)


def _kb(tmp_path, *project_dirs, default=False):
    root = tmp_path / "knowledge"
    for name in project_dirs:
        (root / "projects" / name / "profiles").mkdir(parents=True)
    if default:
        d = root / "projects" / "platform" / "profiles"
        d.mkdir(parents=True, exist_ok=True)
        (d / "default.yaml").write_text(DEFAULT_YAML)
    (root / "projects").mkdir(parents=True, exist_ok=True)
    return root


def _ctx(knowledge_ctx, monkeypatch, tmp_path, kb, agentctl=None):
    monkeypatch.setattr(knowledge_ctx, "CONVIVY", tmp_path)
    monkeypatch.setattr(knowledge_ctx, "KNOWLEDGE", kb)
    _library_serves(knowledge_ctx, monkeypatch, kb)
    if agentctl is not None:
        _agentctl_serves(agentctl, monkeypatch, kb)


def _agentctl_serves(agentctl, monkeypatch, kb):
    """`default_profile`'s Library read (CQ·371): `GET /profiles` served from
    this test's knowledge tree, the same `served()` list `_library_serves`
    builds for `knowledge-ctx` — one fixture layout, two clients. Patches
    `agentctl._library_get` directly, below `pinned_opener`'s real socket, so
    a test that forgets this helper reaches for the fixed Library address
    instead of silently passing."""
    def served():
        return sorted((f.parent.parent.name, f.stem)
                      for f in (kb / "projects").glob("*/profiles/*.yaml"))

    real_library_get = agentctl._library_get

    def fake_get(path, name):
        if path == "/profiles":
            return {"profiles": [{"project": p, "variant": v} for p, v in served()],
                    "count": len(served())}
        return real_library_get(path, name)
    monkeypatch.setattr(agentctl, "_library_get", fake_get)


def _library_serves(knowledge_ctx, monkeypatch, kb):
    """The Library's profile set, as a test double: every `projects/<p>/profiles/
    <v>.yaml` in this test's knowledge tree is a profile the Library lists and
    resolves. Read lazily, so a file a test writes after `_ctx` is served too.
    knowledge-ctx reads none of these files itself; the Library holds them in
    production, and this stands in for it."""
    def served():
        return sorted((f.parent.parent.name, f.stem)
                      for f in (kb / "projects").glob("*/profiles/*.yaml"))

    def request(method, path, body=None):
        if method == "GET" and path == "/profiles":
            return {"profiles": [{"project": p, "variant": v} for p, v in served()],
                    "count": len(served())}
        if method == "GET" and path.startswith("/profiles/"):
            _, _, project, variant = path.split("/", 3)
            if (project, variant) not in served():
                raise knowledge_ctx.BundleRefused(
                    f"the Library refused {method} {path}: HTTP 404 not found",
                    status=404, error="not found")
            return {"profile": {"project": project, "variant": variant},
                    "documents": [], "count": 0, "rules": [], "claude_md_extras": []}
        raise AssertionError(f"unexpected Library call: {method} {path}")
    monkeypatch.setattr(knowledge_ctx, "_library_request", request)


def test_the_explicit_table_is_unchanged():
    assert projects.EXPLICIT_PROJECT_BY_NAME == EXPECTED


def test_vivy_landing_dispatches_under_crew_vivy(tmp_path):
    # Without the registry entry, crew_for_repo falls through to the explicit row
    # and returns the nonexistent crew "vivy-landing"; EXPECTED does not see crew.
    kb = _kb(tmp_path)
    assert projects.crew_for_repo("vivy-landing", kb) == "vivy"


@pytest.mark.parametrize("name,project", sorted(EXPECTED.items()))
def test_every_registered_name_resolves_as_before_in_both_clis(
        knowledge_ctx, agentctl, tmp_path, monkeypatch, name, project):
    kb = _kb(tmp_path)   # empty KB: explicit rows must not need a directory
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb, agentctl=agentctl)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    assert projects.resolve_project(name, kb) == project
    assert knowledge_ctx.detect_project(tmp_path / name) == project
    assert knowledge_ctx.detect_project(tmp_path / "worktrees" / f"{name}.x") == project
    assert agentctl.default_profile(name, "coder").split("/")[0] == project


# The eight crews with a recorded narrowing: the seven the crew-creation epic's
# done-clause 1 records as deliberate KEEP exceptions (epic comment 4425,
# issue_01M2NYBBY0FXCDHWWD8XBR9RR3, or for constance-updates,
# issue_01M3V1J89CG19ZGD3JY4WFY7V0 comment 4432), plus vivy-landing (orchestrator
# ruling, 2026-10-02). Named here rather than read off
# `projects.kb_scope_exceptions()`, so this stays a real check against
# config/projects.json's content rather than testing the registry against itself.
_RECORDED_EXCEPTION_REPOS = sorted({
    "knowledge", "convivy-app-kit", "convivy-services", "www",
    "shipwright", "constance-landing", "constance-updates", "vivy-landing",
    "homebrew-tap",
})


@pytest.mark.parametrize("repo", _RECORDED_EXCEPTION_REPOS)
def test_a_recorded_exceptions_narrow_scope_is_what_resolution_returns(
        knowledge_ctx, agentctl, tmp_path, monkeypatch, repo):
    """issue_01M3V1J89CG19ZGD3JY4WFY7V0: the registry's recorded narrowing for
    each of the eight recorded narrowings is what both CLIs actually resolve, not
    merely a literal that happens to agree with it today."""
    exc = projects.kb_scope_exceptions().get(repo)
    assert exc is not None, f"{repo!r} has no recorded registry kb_scope_exceptions entry"
    kb = _kb(tmp_path)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb, agentctl=agentctl)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    assert projects.resolve_project(repo, kb) == exc["kb_project"]
    assert knowledge_ctx.detect_project(tmp_path / repo) == exc["kb_project"]
    assert agentctl.default_profile(repo, "coder").split("/")[0] == exc["kb_project"]


def test_an_explicit_row_wins_over_a_same_named_directory(tmp_path):
    kb = _kb(tmp_path, "vivy-commercial")
    assert projects.resolve_project("vivy-commercial", kb) == "vivy"


def test_an_unlisted_name_with_a_project_directory_resolves_in_both_clis(
        knowledge_ctx, agentctl, tmp_path, monkeypatch):
    kb = _kb(tmp_path, "datecli")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb, agentctl=agentctl)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    assert knowledge_ctx.detect_project(tmp_path / "datecli") == "datecli"
    assert knowledge_ctx.detect_project(tmp_path / "worktrees" / "datecli.fix") == "datecli"
    (kb / "projects" / "datecli" / "profiles" / "daytime.yaml").write_text("x: 1\n")
    assert agentctl.default_profile("datecli", "coder") == "datecli/daytime"


def test_an_unlisted_name_with_no_directory_is_still_refused(
        knowledge_ctx, agentctl, tmp_path, monkeypatch):
    kb = _kb(tmp_path)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb, agentctl=agentctl)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.detect_project(tmp_path / "datecli")
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    with pytest.raises(SystemExit) as exc:
        agentctl.default_profile("datecli", "coder")
    assert "datecli" in str(exc.value)


@pytest.mark.parametrize("name", ["..", ".hidden", "a/b", ""])
def test_a_name_that_is_not_a_plain_directory_name_resolves_to_nothing(tmp_path, name):
    kb = _kb(tmp_path)
    (tmp_path / "knowledge" / "x").mkdir()
    assert projects.resolve_project(name, kb) is None


def test_knowledge_ctx_falls_back_to_default_for_an_unresolved_crew(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    project, resolved = knowledge_ctx.detect_project_or_default(tmp_path / "datecli")
    assert (project, resolved) == ("platform", False)
    profile, project, name = knowledge_ctx.load_profile_or_default(
        project, "daytime", resolved, False)
    assert (profile["profile"], project, name) == (
        {"project": "platform", "variant": "default"}, "platform", "default")
    assert "platform/default" in capsys.readouterr().err


def test_knowledge_ctx_falls_back_when_the_project_has_no_profiles(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    (kb / "projects" / "datecli" / "profiles").rmdir()
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    project, resolved = knowledge_ctx.detect_project_or_default(tmp_path / "datecli")
    assert (project, resolved) == ("datecli", True)
    _, used_project, used_name = knowledge_ctx.load_profile_or_default(
        project, "daytime", resolved, False)
    assert (used_project, used_name) == ("platform", "default")
    assert "platform/default" in capsys.readouterr().err


def test_a_project_that_has_the_profile_keeps_it_even_with_a_default_present(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    (kb / "projects" / "datecli" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    profile, project, name = knowledge_ctx.load_profile_or_default(
        "datecli", "daytime", True, False)
    assert (profile["profile"], project, name) == (
        {"project": "datecli", "variant": "daytime"}, "datecli", "daytime")
    assert capsys.readouterr().err == ""


def test_use_default_loads_a_crews_own_default_yaml_not_the_platform_fallback(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """A crew whose profiles directory holds only `default.yaml` of its own (the
    adopter-install shape measured on issue_01M3P0BQG471R8AT9SCJCFTHP9) must get
    that file back, not silently substitute platform's."""
    kb = _kb(tmp_path, "birdlog", default=True)
    (kb / "projects" / "birdlog" / "profiles" / "default.yaml").write_text(
        "profile: birdlog/default\n")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    profile, project, name = knowledge_ctx.load_profile_or_default(
        "birdlog", "default", True, False)
    assert (profile["profile"], project, name) == (
        {"project": "birdlog", "variant": "default"}, "birdlog", "default")
    assert capsys.readouterr().err == ""


def test_a_crew_with_only_default_yaml_still_refuses_a_name_it_lacks(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "birdlog", default=True)
    (kb / "projects" / "birdlog" / "profiles" / "default.yaml").write_text(
        "profile: birdlog/default\n")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default("birdlog", "daytime", True, False)
    assert exc.value.code == 1   # "profile not found", as before
    assert "using" not in capsys.readouterr().err


def test_a_restricted_directory_never_takes_the_default(
        knowledge_ctx, tmp_path, monkeypatch):
    kb = _kb(tmp_path, "platform", default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default("platform", "ea-daytime", True, True)
    assert exc.value.code == 1   # "profile not found", as before


def test_with_no_default_yaml_the_old_exit_codes_hold(
        knowledge_ctx, tmp_path, monkeypatch):
    kb = _kb(tmp_path, "datecli")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.detect_project_or_default(tmp_path / "nothing-here")
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default("datecli", "daytime", True, False)
    assert exc.value.code == 1


def test_list_in_an_unmapped_directory_exits_42_even_with_a_default(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """bin/co takes its generic plain-session tail only on `list` exiting 42, so
    `list` must not report `default` for a directory that maps to no project."""
    kb = _kb(tmp_path, default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / "datecli").mkdir()
    monkeypatch.chdir(tmp_path / "datecli")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "list"])
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.main()
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    assert capsys.readouterr().out == ""


def test_list_offers_the_default_for_a_mapped_project_with_no_profiles(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / "datecli").mkdir()
    monkeypatch.chdir(tmp_path / "datecli")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "list"])
    assert knowledge_ctx.main() == 0
    assert "default" in capsys.readouterr().out


def test_a_resolved_project_with_profiles_never_takes_the_default_for_a_wrong_name(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    (kb / "projects" / "datecli" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default("datecli", "daytmie", True, False)
    assert exc.value.code == 1   # "profile not found", as before
    assert "using" not in capsys.readouterr().err


def test_allow_default_false_loads_the_named_profile_or_refuses(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    """An explicit `spawn --profile` passes --no-default-fallback."""
    kb = _kb(tmp_path, "datecli", default=True)   # a project with no profiles
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default(
            "datecli", "daytime", True, False, allow_default=False)
    assert exc.value.code == 1
    assert "using" not in capsys.readouterr().err
    # `resolved` False is the case that used to leak: the placeholder project
    # "platform" made this load platform/daytime instead of refusing.
    (kb / "projects" / "platform" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.load_profile_or_default(
            "platform", "daytime", False, False, allow_default=False)
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    assert "using" not in capsys.readouterr().err


def _use(knowledge_ctx, monkeypatch, tmp_path, kb, *flags):
    """Run `knowledge-ctx use daytime` in an unmapped directory; return the
    (project, name) pairs handed to load_profile and the exit code."""
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / "brandnew").mkdir()
    monkeypatch.chdir(tmp_path / "brandnew")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "daytime", "--force", *flags])
    loaded = []

    def fake_load(project, name):
        loaded.append((project, name))
        raise SystemExit("stop before render")
    monkeypatch.setattr(knowledge_ctx, "load_profile", fake_load)
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.main()
    return loaded, exc.value.code


def test_use_with_no_default_fallback_in_an_unmapped_directory_exits_42(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, default=True)
    (kb / "projects" / "platform" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    loaded, code = _use(knowledge_ctx, monkeypatch, tmp_path, kb, "--no-default-fallback")
    assert code == knowledge_ctx.NO_PROJECT_MAPPING
    assert loaded == []


def test_use_in_an_unmapped_directory_never_loads_platform_under_the_requested_name(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, default=True)
    (kb / "projects" / "platform" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    loaded, _ = _use(knowledge_ctx, monkeypatch, tmp_path, kb)
    assert loaded == [("platform", "default")]


def test_the_agentctl_profile_path_to_an_unmapped_repo_refuses(
        agentctl, knowledge_ctx, tmp_path, monkeypatch, capsys):
    """`spawn --on brandnew --profile daytime` → activate_profile(allow_default=False)
    → knowledge-ctx use --no-default-fallback, run here in-process."""
    kb = _kb(tmp_path, default=True)
    (kb / "projects" / "platform" / "profiles" / "daytime.yaml").write_text("own: 1\n")
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    wt = tmp_path / "brandnew"
    wt.mkdir()
    loaded = []
    monkeypatch.setattr(knowledge_ctx, "load_profile",
                        lambda p, n: loaded.append((p, n)) or {})

    def fake_run(cmd, cwd=None, **kw):
        monkeypatch.chdir(cwd)
        monkeypatch.setattr("sys.argv", ["knowledge-ctx", *cmd[1:]])
        try:
            knowledge_ctx.main()
            code = 0
        except SystemExit as e:
            code = e.code
        return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    monkeypatch.setattr(agentctl, "_ancestor_pids", lambda: [])
    with pytest.raises(SystemExit):
        agentctl.activate_profile(wt, "brandnew/daytime", allow_default=False)
    assert loaded == []


def test_resolve_project_needs_the_on_disk_spelling(tmp_path):
    kb = _kb(tmp_path, "vivy-real")
    (kb / "projects" / "casey").mkdir()
    assert projects.resolve_project("casey", kb) == "casey"
    assert projects.resolve_project("Casey", kb) is None
    assert projects.resolve_project("CASEY", kb) is None


def test_the_fallback_line_names_the_real_cause(
        knowledge_ctx, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    knowledge_ctx.load_profile_or_default(
        "platform", "daytime", False, False, where="/x/brandnew")
    err = capsys.readouterr().err
    assert "knowledge-ctx: /x/brandnew maps to no project; using platform/default" in err
    assert "KNOWLEDGE_CTX_DEFAULT_PROFILE profile=platform/default" in err
    assert "no platform/daytime profile" not in err   # never claims a profile is missing
    knowledge_ctx.load_profile_or_default("datecli", "daytime", True, False)
    assert ("knowledge-ctx: project datecli has no profiles; using platform/default"
            in capsys.readouterr().err)


def test_ea_takes_no_fallback_through_main(
        knowledge_ctx, tmp_path, monkeypatch):
    """The wiring, not just the helper: `use ea-daytime` in an EA directory with a
    default.yaml present and platform/ea-daytime.yaml absent must refuse."""
    kb = _kb(tmp_path, "platform", default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / "EA").mkdir()
    monkeypatch.chdir(tmp_path / "EA")
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "ea-daytime", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.main()
    assert exc.value.code == 1   # "profile not found"; the default was not taken


def test_agentctl_default_profile_falls_back_and_names_it(
        agentctl, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    assert agentctl.default_profile("datecli", "coder") == "platform/default"
    assert "platform/default" in capsys.readouterr().err
    assert agentctl.default_profile("unlisted-and-no-dir", "coder") == "platform/default"


def test_agentctl_keeps_its_refusals_with_no_default_yaml(agentctl, tmp_path, monkeypatch):
    kb = _kb(tmp_path, "datecli")
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    # resolved project, profile file absent, no default: unchanged, names the project
    assert agentctl.default_profile("datecli", "coder") == "datecli/daytime"
    with pytest.raises(SystemExit):
        agentctl.default_profile("unlisted-and-no-dir", "coder")


def test_agentctl_never_defaults_a_project_that_has_profiles(agentctl, tmp_path, monkeypatch):
    kb = _kb(tmp_path, "datecli", default=True)
    (kb / "projects" / "datecli" / "profiles" / "overnight.yaml").write_text("x: 1\n")
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    # has profiles, lacks `daytime`: returned as-is, so it fails as not-found later
    assert agentctl.default_profile("datecli", "coder") == "datecli/daytime"


def test_agentctl_never_defaults_a_project_whose_only_profile_is_its_own_default(
        agentctl, tmp_path, monkeypatch):
    """A committed `default.yaml` is a profile of the project's own (like
    `overnight.yaml` above), so this is the same NO FALLBACK PRODUCT case, not a
    reason to fall back to platform/default."""
    kb = _kb(tmp_path, "birdlog", default=True)
    (kb / "projects" / "birdlog" / "profiles" / "default.yaml").write_text(
        "profile: birdlog/default\n")
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    assert agentctl.default_profile("birdlog", "coder") == "birdlog/daytime"


def test_agentctl_names_the_cause_when_it_defaults(agentctl, tmp_path, monkeypatch, capsys):
    kb = _kb(tmp_path, "datecli", default=True)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    agentctl.default_profile("datecli", "coder")
    assert "project datecli has no profiles" in capsys.readouterr().err
    agentctl.default_profile("nope", "coder")
    assert "maps to no project" in capsys.readouterr().err


def test_agentctl_default_profile_resolves_with_no_knowledge_checkout_on_disk(
        agentctl, tmp_path, monkeypatch):
    """CQ·371 (issue_01M43AE2QD6H3JZMKM2RR2NK2W): `default_profile`'s existence
    checks must answer from the Library even when `KNOWLEDGE_ROOT` names a
    directory that was never created — a machine with its knowledge checkout
    removed still resolves a dispatch's profile. `vivy` resolves through
    `projects.EXPLICIT_PROJECT_BY_NAME`, not a disk read, so this isolates the
    has-profiles/has-default checks this item moves to the Library."""
    never_created = tmp_path / "no-such-knowledge-checkout"
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", never_created)

    def fake_get(path, name):
        assert path == "/profiles"
        return {"profiles": [{"project": "platform", "variant": "default"},
                             {"project": "vivy", "variant": "daytime"}],
                "count": 2}
    monkeypatch.setattr(agentctl, "_library_get", fake_get)
    assert not never_created.exists()
    assert agentctl.default_profile("vivy", "coder") == "vivy/daytime"


def test_an_unreachable_library_refuses_default_profile_by_naming_it(
        agentctl, tmp_path, monkeypatch):
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tmp_path / "knowledge")

    def refuse(path, name):
        raise SystemExit(f"dispatch refused: the Library is unreachable; cannot resolve "
                         f"{name!r}")
    monkeypatch.setattr(agentctl, "_library_get", refuse)
    with pytest.raises(SystemExit, match="the Library is unreachable"):
        agentctl.default_profile("datecli", "coder")


class _Run:
    returncode = 0
    stdout = ""

    def __init__(self, stderr):
        self.stderr = stderr


def _stub_ctx(agentctl, monkeypatch, stderr):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Run(stderr)
    monkeypatch.setattr(agentctl.subprocess, "run", fake_run)
    monkeypatch.setattr(agentctl, "_ancestor_pids", lambda: [])
    return calls


def test_activate_profile_re_emits_the_fallback_and_reports_the_loaded_profile(
        agentctl, tmp_path, monkeypatch, capsys):
    calls = _stub_ctx(agentctl, monkeypatch, (
        "knowledge-ctx: project datecli has no profiles; using platform/default\n"
        "KNOWLEDGE_CTX_DEFAULT_PROFILE profile=platform/default "
        "reason=project datecli has no profiles\n"))
    loaded = {}
    agentctl.activate_profile(tmp_path, "datecli/daytime", loaded=loaded)
    err = capsys.readouterr().err
    assert "platform/default" in err and "project datecli has no profiles" in err
    assert loaded == {"profile": "platform/default", "profile_documents_current": False}
    assert "--no-default-fallback" not in calls[0]


def test_activate_profile_says_nothing_and_reports_nothing_without_a_fallback(
        agentctl, tmp_path, monkeypatch, capsys):
    _stub_ctx(agentctl, monkeypatch, "")
    loaded = {}
    agentctl.activate_profile(tmp_path, "datecli/daytime", loaded=loaded)
    # `profile_documents_current` is always set when `loaded` is a dict
    # (round 4) — False here because the stub's stderr carries no marker.
    assert capsys.readouterr().err == "" and loaded == {"profile_documents_current": False}


def test_an_explicit_profile_forbids_the_fallback(agentctl, tmp_path, monkeypatch):
    calls = _stub_ctx(agentctl, monkeypatch, "")
    agentctl.activate_profile(tmp_path, "constance/daytme", allow_default=False)
    assert "--no-default-fallback" in calls[0]


def test_cmd_spawn_wires_the_fallback_into_the_call_and_the_recorded_profile(agentctl):
    """Pinned on source, like the other cmd_spawn call-site tests: driving the whole
    of cmd_spawn just to reach this call is what those tests avoid."""
    src = Path(agentctl.__file__).read_text()
    call = re.search(r"activate_profile\(worktree, profile,(.*?)\)\n", src, re.S)
    assert call, "the activate_profile call site moved or was renamed"
    assert "allow_default=not args.profile" in call.group(1), (
        "an explicit --profile must load that profile or refuse")
    assert "loaded=_loaded" in call.group(1)
    assert 'profile = _loaded.get("profile", profile)' in src, (
        "meta.json must record the profile knowledge-ctx actually rendered")


def test_default_yaml_is_not_a_profile_of_its_own(agentctl):
    variants = [("platform", "default")]
    assert not agentctl._variants_has_profiles(variants, "platform")
    variants.append(("platform", "daytime"))
    assert agentctl._variants_has_profiles(variants, "platform")


def test_default_yaml_is_a_profile_of_its_own_for_any_other_project(agentctl):
    """The platform-only exclusion (above) is not a blanket rule: `default.yaml`
    is platform's fallback file, not a reserved name, so a project that commits
    its own has a profile of its own."""
    variants = [("platform", "default")]
    assert not agentctl._variants_has_profiles(variants, "birdlog")
    variants.append(("birdlog", "default"))
    assert agentctl._variants_has_profiles(variants, "birdlog")


# --- private crews never take the default (platform/decisions/0043) -----------
#
# The allowlist and the explicit table key on the exact string `EA`, but the disk
# is case-insensitive and a resolved cwd keeps the operator's spelling, so `ea`
# reached the fallback. `CFO` and `privatecrew` have neither a project directory nor
# an allowlist row. All of them come from the registry, driven here from a fixture.

def _private(slug, display, home):
    return {"class": "project", "display": display, "home_dir": f"<root>/{home}",
            "repos": [home], "aliases": [], "crew": slug, "kb": "local",
            "private": True}


FIXTURE_REGISTRY = {
    "_meta": {},
    "cfo": _private("cfo", "CFO", "CFO"),
    "ea": _private("ea", "EA", "EA"),
    "privatecrew": _private("privatecrew", "privatecrew.com", "privatecrew"),
    "datecli": {"class": "project", "display": "DateCLI", "repos": ["datecli"],
                "aliases": [], "kb": "central"},
}


@pytest.fixture
def fixture_registry(monkeypatch):
    monkeypatch.setattr(projects, "_load_raw", lambda: FIXTURE_REGISTRY)


def _use_default(knowledge_ctx, monkeypatch, tmp_path, dirname):
    kb = _kb(tmp_path, "platform", default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / dirname).mkdir()
    monkeypatch.chdir(tmp_path / dirname)
    # The index is a per-machine file a CI runner does not have (the run that
    # first shipped this died on "index missing at ~/.knowledge-index/index.db").
    # These tests settle which profile is chosen, so the render that reads the
    # index is stubbed and the pointer at a missing file makes any other read loud.
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "no-such-index.db")
    monkeypatch.setattr(knowledge_ctx, "render_source", lambda *a, **k: ([], {}))
    monkeypatch.setattr(knowledge_ctx, "fetch_instruction_blocks", lambda: [])
    monkeypatch.setattr(knowledge_ctx, "all_agent_and_skill_ids",
                        lambda: {"agent": set(), "skill": set()})
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "use", "default", "--dry-run"])
    return knowledge_ctx.main()


@pytest.mark.parametrize("dirname", ["EA", "ea", "CFO", "cfo", "privatecrew", "PrivateCrew"])
def test_a_private_crew_directory_takes_no_default_under_any_spelling(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry, dirname):
    with pytest.raises(SystemExit) as exc:
        _use_default(knowledge_ctx, monkeypatch, tmp_path, dirname)
    # EA resolves explicitly to `platform`, so it is refused by the allowlist (43);
    # every other spelling resolves to nothing and is refused by the fallback (42).
    assert exc.value.code in (knowledge_ctx.NO_PROJECT_MAPPING,
                              knowledge_ctx.PROFILE_NOT_ALLOWED)
    assert "using" not in capsys.readouterr().err


@pytest.mark.parametrize("dirname", ["ea", "cfo", "CFO", "privatecrew", "PrivateCrew"])
def test_the_refusal_says_the_directory_is_a_private_crew(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry, dirname):
    with pytest.raises(SystemExit) as exc:
        _use_default(knowledge_ctx, monkeypatch, tmp_path, dirname)
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    assert "private crew" in capsys.readouterr().err


def test_the_allowlist_applies_to_a_lowercase_ea(knowledge_ctx):
    assert knowledge_ctx.allowed_profiles("ea") == {"ea-daytime"}
    assert knowledge_ctx.allowed_profiles("EA") == {"ea-daytime"}
    assert knowledge_ctx.allowed_profiles("datecli") is None
    with pytest.raises(SystemExit) as exc:
        knowledge_ctx.check_profile_allowed("ea", "default")
    assert exc.value.code == knowledge_ctx.PROFILE_NOT_ALLOWED


def test_a_private_crew_is_refused_by_display_alias_and_directory_name(monkeypatch):
    reg = {"_meta": {}, "quiet": {"class": "project", "display": "Quiet Box",
                                  "repos": ["quiet-repo"], "aliases": ["qb"],
                                  "home_dir": "<root>/QuietDir", "private": True}}
    monkeypatch.setattr(projects, "_load_raw", lambda: reg)
    for name in ("quiet", "QUIET BOX", "Quiet-Repo", "QB", "quietdir"):
        assert projects.default_profile_refused(name), name
    assert not projects.default_profile_refused("datecli")


def test_an_unreadable_registry_takes_no_fallback(
        knowledge_ctx, agentctl, tmp_path, monkeypatch):
    def boom():
        raise FileNotFoundError("projects.json")
    monkeypatch.setattr(projects, "_load_raw", boom)
    with pytest.raises(SystemExit) as exc:
        _use_default(knowledge_ctx, monkeypatch, tmp_path, "datecli")
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tmp_path / "knowledge")
    _agentctl_serves(agentctl, monkeypatch, tmp_path / "knowledge")
    with pytest.raises(SystemExit):
        agentctl.default_profile("datecli", "coder")


def test_a_non_private_unlisted_crew_still_falls_back(
        knowledge_ctx, agentctl, tmp_path, monkeypatch, capsys, fixture_registry):
    assert _use_default(knowledge_ctx, monkeypatch, tmp_path, "datecli") == 0
    assert "platform/default" in capsys.readouterr().err
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", tmp_path / "knowledge")
    _agentctl_serves(agentctl, monkeypatch, tmp_path / "knowledge")
    assert agentctl.default_profile("datecli", "coder") == "platform/default"


def _list(knowledge_ctx, monkeypatch, tmp_path, dirname, kb):
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    (tmp_path / dirname).mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / dirname)
    monkeypatch.setattr("sys.argv", ["knowledge-ctx", "list"])
    return knowledge_ctx.main()


# `list` is what bin/co reads: exit 42 in a directory that maps to no project is what
# sends co to its plain-session tail. A default.yaml once made it print `default` and
# exit 0 there (reviewer, PR #677 round 1), so only a KB that HAS one can see the
# regression. These were a bash test that ran the CLI under a bare `python3`; the bash
# CI job installs no PyYAML, so all four assertions died with exit 1 there.
@pytest.mark.parametrize("default", [True, False])
def test_list_in_an_unmapped_directory_exits_42_with_or_without_a_default(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry, default):
    kb = _kb(tmp_path, "datecli", default=default)
    with pytest.raises(SystemExit) as exc:
        _list(knowledge_ctx, monkeypatch, tmp_path, "brandnew", kb)
    assert exc.value.code == knowledge_ctx.NO_PROJECT_MAPPING
    assert capsys.readouterr().out == ""


def test_list_offers_the_default_profile_to_a_mapped_project_with_none(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry):
    kb = _kb(tmp_path, "datecli", default=True)
    assert _list(knowledge_ctx, monkeypatch, tmp_path, "datecli", kb) == 0
    assert "default" in capsys.readouterr().out


@pytest.mark.parametrize("repo", ["cfo", "CFO", "privatecrew", "PrivateCrew", "ea"])
def test_agentctl_gives_a_private_crew_no_default(
        agentctl, tmp_path, monkeypatch, capsys, fixture_registry, repo):
    kb = _kb(tmp_path, default=True)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", kb)
    _agentctl_serves(agentctl, monkeypatch, kb)
    with pytest.raises(SystemExit):
        agentctl.default_profile(repo, "coder")
    assert "private crew" in capsys.readouterr().err


# --- an unregistered crew resolves from the knowledge repo --------------------
#
# `crew_for_repo` returned None for any repo config/projects.json does not list,
# so an adopter's crew composed none of its crew-scoped instruction blocks and
# nothing said so.

import json  # noqa: E402

CREW_BLOCK = "platform/instruction-blocks/casey-only"


def _crew_block_row(crews):
    import hashlib
    body = "A rule for this crew.\n"
    return {"id": CREW_BLOCK, "project": "platform", "status": "current", "body": body,
            "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "frontmatter_extra": json.dumps({"scope": "crew", "binds_crews": crews})}


def _use_crew(knowledge_ctx, monkeypatch, tmp_path, dirname, *, with_project_dir):
    """Run a real `knowledge-ctx use default --force` in `<tmp>/<dirname>` against a
    hermetic registry and knowledge root; return (exit code, .claude dir)."""
    kb = _kb(tmp_path, "platform", *([dirname] if with_project_dir else []), default=True)
    _ctx(knowledge_ctx, monkeypatch, tmp_path, kb)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / dirname).mkdir()
    monkeypatch.chdir(tmp_path / dirname)
    monkeypatch.setattr(knowledge_ctx, "DB_PATH", tmp_path / "no-such-index.db")
    monkeypatch.setattr(knowledge_ctx, "render_source", lambda *a, **k: ([], {}))
    monkeypatch.setattr(knowledge_ctx, "fetch_instruction_blocks",
                        lambda: [_crew_block_row([dirname])])
    monkeypatch.setattr(knowledge_ctx, "all_agent_and_skill_ids",
                        lambda: {"agent": set(), "skill": set()})
    monkeypatch.setattr("sys.argv",
                        ["knowledge-ctx", "use", "default", "--force", "--force-anyway"])
    code = knowledge_ctx.main()
    return code, tmp_path / dirname / ".claude"


def test_an_unregistered_crew_with_a_project_directory_gets_its_crew_blocks(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry):
    code, claude = _use_crew(knowledge_ctx, monkeypatch, tmp_path, "casey",
                             with_project_dir=True)
    assert code == 0
    record = json.loads((claude / ".profile-documents.json").read_text())
    assert [b["id"] for b in record["instruction_blocks"]] == [CREW_BLOCK]
    assert record["crew"] == {"name": "casey", "resolved": True}
    assert "KNOWLEDGE_CTX_NO_CREW" not in capsys.readouterr().err


def test_an_unregistered_crew_with_no_directory_says_so_on_stderr_and_in_the_record(
        knowledge_ctx, tmp_path, monkeypatch, capsys, fixture_registry):
    code, claude = _use_crew(knowledge_ctx, monkeypatch, tmp_path, "casey",
                             with_project_dir=False)
    assert code == 0
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "KNOWLEDGE_CTX_NO_CREW" in ln]
    assert len(lines) == 1 and "repo=casey" in lines[0]
    assert "no crew is registered or found in the knowledge repo" in lines[0]
    record = json.loads((claude / ".profile-documents.json").read_text())
    assert record["instruction_blocks"] == []
    assert record["crew"]["resolved"] is False and record["crew"]["name"] is None
    assert "'casey'" in record["crew"]["reason"]
    assert "not composed" in record["crew"]["reason"]


@pytest.mark.parametrize("name", ["ea", "Cfo", "PrivateCrew"])
def test_a_private_crew_under_a_case_variant_resolves_to_nothing(
        tmp_path, fixture_registry, name):
    """The variant is not a registered spelling, so only the directory rule could
    answer, and a `projects/<variant>/` directory exists to tempt it."""
    kb = _kb(tmp_path, name)
    assert projects.resolve_project(name, kb) == name
    assert projects.crew_for_repo(name, kb) is None


def test_every_registered_repo_and_alias_resolves_as_before(tmp_path):
    """Enumerated from the real registry, against a knowledge root that has a
    `projects/<name>/` for every repo, so the directory rule cannot be what answers."""
    names = [n for u in projects.load().values()
             for n in [*u.get("repos", []), *u.get("aliases", [])]]
    assert names
    kb = _kb(tmp_path, *names)
    for u in projects.load().values():
        for n in [*u.get("repos", []), *u.get("aliases", [])]:
            assert projects.crew_for_repo(n, kb) == u.get("crew"), n
            assert projects.crew_for_repo(n) == u.get("crew"), n
    assert projects.crew_for_repo("not-a-repo", kb) is None


def test_an_explicit_row_naming_another_project_is_not_a_crew(tmp_path, fixture_registry):
    kb = _kb(tmp_path, "platform")
    assert projects.resolve_project("dev-setup", kb) == "platform"
    assert projects.crew_for_repo("dev-setup", kb) is None


def test_activate_profile_re_emits_the_no_crew_line(
        agentctl, tmp_path, monkeypatch, capsys):
    _stub_ctx(agentctl, monkeypatch, (
        "KNOWLEDGE_CTX_NO_CREW repo=casey reason=crew-scoped instruction blocks were "
        "not composed for 'casey': no crew is registered or found in the knowledge repo\n"))
    agentctl.activate_profile(tmp_path, "casey/daytime")
    err = capsys.readouterr().err
    assert "casey" in err and "not composed" in err
    assert "no crew is registered or found in the knowledge repo" in err


def test_default_profile_has_no_knowledge_tree_profile_existence_read():
    """CQ·371 (issue_01M43AE2QD6H3JZMKM2RR2NK2W): `default_profile`'s own body
    may not read `projects.has_profiles`/`projects.default_profile_path` — both
    walked `projects/<project>/profiles/` on disk, which does not exist on a
    machine with no knowledge checkout. Neither function exists in
    `projects.py` any more; both checks are now the Library's `GET /profiles`
    (`_library_profile_variants`/`_variants_has_profiles`)."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "bin" / "agentctl").read_text()
    assert "projects.has_profiles(" not in src
    assert "projects.default_profile_path(" not in src
    tree = ast.parse(src)
    checked = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "default_profile":
            checked = True
            body = ast.get_source_segment(src, node)
            assert not re.search(r"\.is_file\(\)", body), (
                "default_profile reads a knowledge-tree path directly")
    assert checked
    projects_src = (Path(__file__).resolve().parent.parent
                    / "lib" / "agent-platform" / "projects.py").read_text()
    assert "def has_profiles(" not in projects_src
    assert "def default_profile_path(" not in projects_src
