"""Tests for `bin/transcript-capture` (issue_01M0BRGVMXGQKM4GXBEFG2Z7PS).

Exclusion logic gets the most scrutiny: it must be decided from the directory
name alone, before opening anything inside it, and must survive case and
worktree-prefix variation — the exact bug class found (and fixed) three times
elsewhere in this repo on 2026-08-19 (tools/session-event-log.py PR #413,
bin/agentctl PR #409).
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error

import pytest


@pytest.fixture()
def tc(transcript_capture):
    return transcript_capture


def _home_prefix(tc, slug: str) -> str:
    """The sanitized directory-name prefix `slug`'s registered home_dir
    produces, computed via the module's own `_sanitize` and
    `projects.home_path` — the same primitives `_project_prefixes` uses — so
    the expectation follows `Path.home()` on whatever machine runs the test
    rather than a literal path hardcoded to one contributor's laptop."""
    return tc._sanitize(tc.projects.home_path(slug))


def _worktree_prefix(tc, repo: str, suffix: str) -> str:
    """The sanitized worktree-collection dirname a `<repo>.<suffix>` session
    directory produces, computed WITHOUT calling the module's own `_sanitize`
    — every '/' and '.' becomes '-', hardcoded here to match this platform's
    verified real `~/.claude/projects` naming (the real worktree
    `agent-platform.black-box` names its project dir
    `...-worktrees-agent-platform-black-box`, not
    `...-worktrees-agent-platform.black-box`). A regression in `_sanitize`
    (e.g. forgetting to convert '.') must fail this test, not compute its own
    expectation from the same buggy function (`_WORKTREES_ROOT` is just a
    root-path constant, not the function under test)."""
    root = str(tc._WORKTREES_ROOT)
    return f"{root}/{repo}.{suffix}".replace("/", "-").replace(".", "-")


# --- black-box exclusion ---------------------------------------------------------


def test_private_home_dir_prefix_is_excluded(tc):
    idx = tc.all_project_prefixes()
    private = tc.projects.private_slugs()
    assert "cfo" in private  # the one registered black-box crew today
    bb = tc.black_box_prefixes(idx, private)
    home = _home_prefix(tc, "cfo")
    assert tc.dir_matches_prefixes(home, bb)
    # a real subdirectory of the black-box home also excludes
    assert tc.dir_matches_prefixes(home + "-sub", bb)


def test_case_insensitive_bypass_is_closed(tc):
    """The exact repro named in the dispatch: a session whose cwd was reached
    via different case (~/convivy/cfo instead of ~/Convivy/CFO) sanitizes to a
    differently-cased directory name that still names the same on-disk
    directory under this machine's case-insensitive APFS default — it must
    still be excluded, not silently let through by a naive string compare."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    home = _home_prefix(tc, "cfo")
    assert tc.dir_matches_prefixes(home.lower(), bb)
    assert tc.dir_matches_prefixes(home.upper(), bb)
    assert tc.dir_matches_prefixes(home.swapcase(), bb)


def test_worktree_prefix_is_excluded(tc):
    """A session run from a CFO worktree (~/Convivy/worktrees/CFO.<purpose>)
    excludes too, not just the primary home directory."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    worktree = _worktree_prefix(tc, "CFO", "some-purpose")
    assert tc.dir_matches_prefixes(worktree, bb)
    # case-insensitive here too
    assert tc.dir_matches_prefixes(worktree.lower(), bb)


def test_real_cfo_worktree_session_is_excluded_arch_dot_sanitize(tc):
    """The security-reviewer's exact repro on PR #415: a real CFO worktree
    session, following this platform's own `<repo>.<purpose>` convention (the
    same one this test's own worktree uses), produces an on-disk
    `~/.claude/projects` dirname with the '.' already converted to '-' by
    Claude Code itself — verified empirically against this machine's real
    entries (`agent-platform.black-box` -> `...-agent-platform-black-box`).
    Before this fix, `_sanitize` left '.' alone, so the computed worktree
    prefix kept a literal trailing dot the real dirname never has, and this
    exact session was NOT excluded — its full content would have been posted
    to the shared Library Archives. `_worktree_prefix` builds the expected
    dirname independently of `_sanitize` (see its docstring), so this test
    would have failed against the pre-fix code instead of passing by
    comparing the buggy function to itself."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    real_dirname = _worktree_prefix(tc, "CFO", "quarterly-close")
    assert tc.dir_matches_prefixes(real_dirname, bb)


def test_scratchpad_shaped_cfo_session_is_excluded(tc):
    """Verified live on this machine: a scratchpad cwd rooted OUTSIDE
    ~/Convivy entirely (`/private/tmp/claude-<uid>/<origin-path>-scratchpad-
    <name>`, real entries exist today for agent-platform, convivy-deck, and
    substrate) embeds the sanitized origin path as a SUBSTRING partway
    through the `~/.claude/projects` dirname, not as a leading prefix — a
    pure `startswith` check can never catch this regardless of how many
    prefixes are enumerated (security-reviewer, PR #415, run-3a454557). This
    is the exact real shape: the private home_dir fragment sits after
    `claude-501-`, bounded by '-' on both sides, never at the front of the
    dirname."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    home = _home_prefix(tc, "cfo")
    scratchpad_dirname = f"-private-tmp-claude-501-{home}-somehash-scratchpad-probe"
    assert tc.dir_matches_prefixes(scratchpad_dirname, bb)


def test_scratchpad_shaped_dirname_with_partial_overlap_is_not_excluded(tc):
    """A scratchpad-shaped dirname for a DIFFERENT project whose sanitized
    identifier merely shares its LEADING characters with a private crew's
    home fragment — with no '-' boundary between the shared characters and
    what follows — must not be excluded. A naive substring `in` check would
    false-positive here (the private fragment IS a substring of the longer,
    unrelated one); the boundary-delimited check must not."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    home = _home_prefix(tc, "cfo")
    scratchpad_dirname = f"-private-tmp-claude-501-{home}extraproject-somehash-scratchpad-probe"
    assert not tc.dir_matches_prefixes(scratchpad_dirname, bb)


def test_cfo_nested_repo_worktree_is_excluded_today(tc):
    """The other naming shape this machine's real `~/.claude/projects` holds
    that the exclusion never named: a worktree nested INSIDE a repo's own
    checkout (`<repo>/.claude/worktrees/<name>`, distinct from the
    `~/Convivy/worktrees/<repo>.<purpose>` shape — real entries exist today
    for agent-platform and selector). For CFO specifically this is just an
    ordinary subdirectory of its home_dir (CFO's repo checkout IS its home
    directory), so it's caught by the plain home-fragment boundary match."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    home = _home_prefix(tc, "cfo")
    nested = f"{home}-.claude-worktrees-some-task"
    assert tc.dir_matches_prefixes(nested, bb)


def test_nested_repo_worktree_excluded_for_a_future_crew_with_a_dirs_entry(tc, tmp_path, monkeypatch):
    """CFO's nested in-repo worktree shape (previous test) happens to work
    today only because CFO's repo checkout equals its home_dir basename. A
    FUTURE private crew whose repo checkout is a `dirs` (ARCH-426) entry
    distinct from home_dir — the shape `svc` already uses for
    `convivy-services` — must still have this nested-worktree shape excluded,
    via the `dirs_for` fragment this PR adds rather than via home_dir."""
    home = tmp_path / "acme-home"
    code = tmp_path / "acme-code"  # repo checkout, NOT nested under home
    monkeypatch.setattr(tc.projects, "home_path",
                         lambda slug: home if slug == "acme" else None)
    monkeypatch.setattr(tc.projects, "dirs_for",
                         lambda slug: [home, code] if slug == "acme" else [])
    prefixes = tc._project_prefixes("acme", {"repos": ["acme-code"]})
    nested_worktree = tc._sanitize(code / ".claude" / "worktrees" / "some-task")
    assert tc.dir_matches_prefixes(nested_worktree, prefixes)


def test_delimited_occurrence_scans_past_unboundaried_match_to_find_a_later_one(tc):
    """Regression for the multi-occurrence scan loop itself: an early match of
    `fragment` that fails its boundary check must not stop the scan — the loop
    must keep going and find a later, genuinely-delimited occurrence of the same
    fragment further along the same string. Every fragment in every other test in
    this file occurs at most once in its tested dirname, so a regression that made
    the loop stop at the first raw `str.find` hit (an easy "simplification") would
    slip through unnoticed without this test (reviewer, PR #415, run-4c62edc4).
    `"CFO"` occurs twice in `"-CFOX-CFO-foo"`: once as the unboundaried tail of
    `"CFOX"` (left-ok, but followed by `"X"` — fails the right-boundary check),
    and once genuinely delimited on both sides later in the string."""
    assert tc._delimited_occurrence("-CFOX-CFO-foo", "CFO", require_right_boundary=True)


def test_prefix_collision_does_not_false_positive(tc):
    """A directory that merely STARTS WITH the same characters as a private
    home_dir but names a different real directory (no '/' boundary) must NOT
    be excluded — over-exclusion silently drops legitimate data too."""
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    assert not tc.dir_matches_prefixes(_home_prefix(tc, "cfo") + "extra", bb)
    assert not tc.dir_matches_prefixes(
        _worktree_prefix(tc, "CFOX", "overnight"), bb)


def test_non_private_project_is_not_excluded(tc):
    idx = tc.all_project_prefixes()
    bb = tc.black_box_prefixes(idx, tc.projects.private_slugs())
    assert not tc.dir_matches_prefixes(_home_prefix(tc, "library"), bb)
    assert not tc.dir_matches_prefixes(_home_prefix(tc, "bosun"), bb)


def test_exclusion_is_rederived_from_registry_not_hardcoded(tc, monkeypatch):
    """The exclusion set must be computed FRESH from projects.private_slugs()
    every call, not hardcoded to 'cfo' — so a newly-registered black-box crew
    is excluded with no code change, per ADR-0043."""
    idx = tc.all_project_prefixes()
    library = _home_prefix(tc, "library")
    # A project that is NOT private today must not be excluded...
    assert not tc.dir_matches_prefixes(library, tc.black_box_prefixes(idx, {"cfo"}))
    # ...but once the registry says it's private, it must be.
    bb_widened = tc.black_box_prefixes(idx, {"cfo", "library"})
    assert tc.dir_matches_prefixes(library, bb_widened)


def test_dirs_for_directories_get_exclusion_coverage_arch426(tc, tmp_path, monkeypatch):
    """A private crew's `dirs` registry field (ARCH-426) names a directory
    that is NOT nested under its home_dir — `svc` already uses this today for
    `convivy-services`, a public project. Before this fix, `_project_prefixes`
    only ever called `home_path(slug)`, so the first private crew registered
    with a non-empty `dirs` entry would get zero exclusion coverage for that
    directory tree. Uses a synthetic project so this doesn't depend on any
    real registry entry ever adding a `dirs` field."""
    home = tmp_path / "acme-home"
    extra = tmp_path / "acme-code"
    monkeypatch.setattr(tc.projects, "home_path",
                         lambda slug: home if slug == "acme" else None)
    monkeypatch.setattr(tc.projects, "dirs_for",
                         lambda slug: [home, extra] if slug == "acme" else [])
    prefixes = tc._project_prefixes("acme", {"repos": ["acme-code"]})
    extra_subdir = tc._sanitize(extra) + "-somefile"
    assert tc.dir_matches_prefixes(extra_subdir, prefixes)
    # home is still covered, and not duplicated by the dirs_for loop
    home_frag = tc._sanitize(home)
    assert sum(1 for p in prefixes if p.value == home_frag) == 1


def test_resolve_product_matches_registered_project(tc):
    idx = tc.all_project_prefixes()
    private = tc.projects.private_slugs()
    assert tc.resolve_product(_home_prefix(tc, "library"), idx, private) == "library"


def test_resolve_product_is_none_for_an_unregistered_dir(tc):
    """The raw-dirname fallback this replaced is what captured `-` and ask."""
    idx = tc.all_project_prefixes()
    private = tc.projects.private_slugs()
    assert tc.resolve_product("-Users-operator-some-unregistered-place", idx, private) is None
    assert tc.resolve_product("-", idx, private) is None


def test_resolve_product_never_returns_a_private_slug(tc):
    """Belt-and-suspenders: even if a caller forgot the upstream exclusion
    check, resolve_product must never label a turn's product with a
    black-box slug."""
    idx = tc.all_project_prefixes()
    private = tc.projects.private_slugs()
    got = tc.resolve_product(_home_prefix(tc, "cfo"), idx, private)
    assert got != "cfo"


def test_main_captures_only_dirs_a_registered_public_project_owns(tc, tmp_path, monkeypatch):
    """The allowlist, proven through `main()`'s own loop against the real
    registry rather than a helper. Each dir holds one quiet, postable turn, so
    a dir that is skipped proves the skip rather than an empty file.

    `-` is where launchd's headless `claude -p` jobs land (cwd `/`), and EA's
    mailbox prompts reached the Library from it. Ask's dir is unregistered.
    The `/private/tmp` dirname resolves to nothing at all, so the test shows the
    control fails closed on an arbitrary dir, not only on the two known leaks.
    `library` is the control: without a dir that does post, a run that captured
    nothing for some other reason would pass too."""
    ask = f"{tc.BR.root()}/ask".replace("/", "-").replace(".", "-")
    stray = "-private-tmp-xyz-unregistered-probe"
    library = _home_prefix(tc, "library")
    projects_dir = tmp_path / "projects"
    quiet = time.time() - tc.LIVE_THRESHOLD_SECONDS - 60
    for name in ("-", ask, stray, library):
        d = projects_dir / name
        d.mkdir(parents=True)
        f = d / "sess.jsonl"
        _write_lines(f, [{"type": "user", "sessionId": "sess", "uuid": "u1",
                          "message": {"role": "user", "content": f"turn from {name}"}}])
        os.utime(f, (quiet, quiet))

    opened = []
    real_read = tc._read_new_lines
    monkeypatch.setattr(tc, "_read_new_lines",
                        lambda path, start: opened.append(path.parent.name) or real_read(path, start))
    posted = []

    class _Resp:
        status = 201
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tc, "CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr(tc, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setenv("TRANSCRIPT_CAPTURE_TOKEN", "tok")
    monkeypatch.setattr(tc.sys, "argv", ["transcript-capture"])

    tc.main()

    assert opened == [library]
    assert [(p["product"], p["body"]) for p in posted] == [("library", f"you: turn from {library}")]


# --- turn extraction ---------------------------------------------------------------


def _rec(**kw) -> str:
    return json.dumps(kw)


def test_turn_extraction_groups_prompt_and_speech(tc):
    lines = [
        _rec(type="user", sessionId="s1", uuid="u1", timestamp="2026-08-19T00:00:00Z",
             message={"role": "user", "content": "hello there"}),
        _rec(type="assistant", sessionId="s1", uuid="u2",
             message={"id": "msg_1", "content": [{"type": "text", "text": "hi back"}]},
             requestId="req_1"),
        _rec(type="user", sessionId="s1", uuid="u3", timestamp="2026-08-19T00:01:00Z",
             message={"role": "user", "content": "second prompt"}),
    ]
    offsets = [0] * len(lines)  # byte offsets are irrelevant to grouping itself
    rows = tc.classify_lines(lines, offsets)
    turns = tc.group_turns(rows)
    assert len(turns) == 2
    assert [r["kind"] for r in turns[0]["rows"]] == ["prompt", "speech"]
    assert [r["kind"] for r in turns[1]["rows"]] == ["prompt"]


def test_duplicate_assistant_records_are_deduped_arch383(tc):
    """The sharp edge named in the KB contract: Claude Code can write the
    identical assistant response into the transcript more than once
    (same message.id + requestId). A turn's body must not repeat it."""
    dup = {"id": "msg_1", "content": [{"type": "text", "text": "hi back"}]}
    lines = [
        _rec(type="user", sessionId="s1", uuid="u1", message={"role": "user", "content": "hi"}),
        _rec(type="assistant", sessionId="s1", uuid="u2", message=dup, requestId="req_1"),
        _rec(type="assistant", sessionId="s1", uuid="u2dup", message=dup, requestId="req_1"),
        _rec(type="assistant", sessionId="s1", uuid="u2dup2", message=dup, requestId="req_1"),
    ]
    offsets = [0] * len(lines)
    rows = tc.classify_lines(lines, offsets)
    speech = [r for r in rows if r["kind"] == "speech"]
    assert len(speech) == 1


def test_assistant_record_missing_dedupe_keys_is_never_collapsed(tc):
    """A record missing message.id or requestId must never be folded into
    another keyless one — the contract's explicit rule."""
    msg = {"content": [{"type": "text", "text": "no ids here"}]}
    lines = [
        _rec(type="user", sessionId="s1", uuid="u1", message={"role": "user", "content": "hi"}),
        _rec(type="assistant", sessionId="s1", uuid="u2", message=msg),
        _rec(type="assistant", sessionId="s1", uuid="u3", message=msg),
    ]
    offsets = [0] * len(lines)
    rows = tc.classify_lines(lines, offsets)
    speech = [r for r in rows if r["kind"] == "speech"]
    assert len(speech) == 2


def test_malformed_line_degrades_without_crashing_the_file(tc):
    lines = [
        _rec(type="user", sessionId="s1", uuid="u1", message={"role": "user", "content": "before"}),
        "not json at all {{{",
        "[1, 2, 3]",  # valid JSON, not a dict
        _rec(type="user", sessionId="s1", uuid="u2", message={"role": "user", "content": "after"}),
    ]
    offsets = [0] * len(lines)
    rows = tc.classify_lines(lines, offsets)
    prompts = [r["body"] for r in rows if r["kind"] == "prompt"]
    assert prompts == ["before", "after"]


def test_tool_calls_and_results_are_excluded_from_turns(tc):
    """Only FULL prompt/speech rows enter a turn — tool_use/tool_result and
    reasoning are COLLAPSED/HIDDEN by classify() and must never leak in."""
    lines = [
        _rec(type="user", sessionId="s1", uuid="u1", message={"role": "user", "content": "run it"}),
        _rec(type="assistant", sessionId="s1", uuid="u2", message={
            "id": "msg_1", "requestId": "req_1",
            "content": [
                {"type": "text", "text": "sure"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            ]}, requestId="req_1"),
        _rec(type="user", sessionId="s1", uuid="u3", toolUseResult="x",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}]}),
    ]
    offsets = [0] * len(lines)
    rows = tc.classify_lines(lines, offsets)
    kinds = {r["kind"] for r in rows}
    assert kinds == {"prompt", "speech"}


def test_released_queued_command_flows_through_the_full_pipeline(tc):
    """The KB contract names two mutually-exclusive paths a released queued
    message can take; this is the `attachment`/`queued_command` one (the
    other is an ordinary `user` record carrying `promptSource: "queued"`).
    `classify_lines` delegates to `session_view.classify()` for this, which
    already handles it correctly — this test exercises that delegation
    end to end (classify_lines -> group_turns -> turn_payload), not the
    classification logic itself."""
    lines = [
        _rec(type="attachment", sessionId="s1", uuid="u1",
             attachment={"type": "queued_command", "commandMode": "prompt",
                         "origin": {"kind": "human"}, "prompt": "queued message"}),
        _rec(type="assistant", sessionId="s1", uuid="u2", requestId="req_1",
             message={"id": "msg_1",
                      "content": [{"type": "text", "text": "reply to queued"}]}),
        _rec(type="user", sessionId="s1", uuid="u3",
             message={"role": "user", "content": "next prompt"}),
    ]
    offsets = [0] * len(lines)
    rows = tc.classify_lines(lines, offsets)
    turns = tc.group_turns(rows)
    assert len(turns) == 2
    payload = tc.turn_payload("s1", "library", "operator", turns[0])
    assert payload["body"] == "you: queued message\n\nclaude: reply to queued"


def test_turn_payload_shape_and_dedup_key(tc):
    turn = {"start_byte": 42, "rows": [
        {"kind": "prompt", "label": "you", "body": "hello"},
        {"kind": "speech", "label": "claude", "body": "hi", "_timestamp": "2026-08-19T00:00:00Z"},
    ]}
    payload = tc.turn_payload("sess-abc", "library", "operator", turn)
    assert payload["source"] == "claude-transcript"
    assert payload["kind"] == "transcript-turn"
    assert payload["actor"] == "operator"
    assert payload["product"] == "library"
    assert payload["stream"] == "sess-abc"
    assert payload["body"] == "you: hello\n\nclaude: hi"
    assert payload["content"] == payload["body"]
    assert payload["dedup_key"] == "claude-transcript:sess-abc:42"
    assert payload["produced_at"] == "2026-08-19T00:00:00Z"
    for field in ("source", "kind", "actor", "product", "stream", "body",
                  "content", "dedup_key"):
        assert isinstance(payload[field], str)


def test_turn_payload_body_survives_a_lone_surrogate(tc):
    """A transcript line can carry a lone Unicode surrogate (valid JSON, not
    valid UTF-8, e.g. from a truncated multi-byte character upstream). Before
    this fix, `body` was built from the raw text while `content` went through
    a UTF-8-sanitizing roundtrip — so `body` alone could still hold the
    surrogate. The Library's `POST /signals` has no surrogate guard on
    `body` (only on `content`), so an unencodable `body` reaches SQLite's
    INSERT and raises uncaught, and `capture_file` never advances a session's
    cursor past a turn whose POST failed — a permanent, silent stall for that
    session. `body` must be as UTF-8-safe as `content`."""
    turn = {"start_byte": 0, "rows": [
        {"kind": "prompt", "label": "you", "body": "before \ud800 after"},
    ]}
    payload = tc.turn_payload("s1", "library", "operator", turn)
    payload["body"].encode("utf-8")  # must not raise UnicodeEncodeError
    payload["content"].encode("utf-8")  # must not raise UnicodeEncodeError
    assert payload["body"] == payload["content"]


def test_turn_payload_body_clips_to_200000_chars_content_stays_full(tc):
    """`POST /signals` refuses a `body` over `store.MAX_TEXT` (200,000
    characters, library#201) with a 400 rather than truncating it the way it
    used to. `body` must be clipped to that limit while `content` (the blob
    column, bounded only by `_MAX_CONTENT_BYTES`) keeps the full text."""
    sanitized = "you: " + ("a" * 250_000)
    turn = {"start_byte": 0, "rows": [{"kind": "prompt", "label": "you", "body": "a" * 250_000}]}
    payload = tc.turn_payload("s1", "library", "operator", turn)
    assert len(payload["body"]) == 200_000
    assert payload["body"] == sanitized[:200_000]
    assert payload["content"] == sanitized
    assert len(payload["content"]) == 250_005


def test_turn_payload_body_equals_content_under_the_limit(tc):
    """A turn whose sanitized text is at or under `store.MAX_TEXT` must keep
    `body` identical to `content` — no clip applies."""
    turn = {"start_byte": 0, "rows": [{"kind": "prompt", "label": "you", "body": "a" * 199_995}]}
    payload = tc.turn_payload("s1", "library", "operator", turn)
    assert payload["body"] == payload["content"]
    assert len(payload["body"]) == 200_000


def test_turn_payload_body_encodes_as_utf8_after_clipping_a_surrogate(tc):
    """The clip is a plain character slice of the already-UTF-8-sanitized
    string — the lone surrogate is replaced before the clip ever runs — so a
    turn that is both over `_MAX_BODY_CHARS` and carries a surrogate must
    still yield a `body` that encodes as UTF-8."""
    body_text = ("a" * 199_990) + "\ud800" + ("b" * 20)
    turn = {"start_byte": 0, "rows": [{"kind": "prompt", "label": "you", "body": body_text}]}
    payload = tc.turn_payload("s1", "library", "operator", turn)
    assert len(payload["body"]) == 200_000
    payload["body"].encode("utf-8")  # must not raise UnicodeEncodeError


def test_turn_payload_none_when_no_visible_text(tc):
    turn = {"start_byte": 0, "rows": [{"kind": "prompt", "label": "you", "body": "   "}]}
    assert tc.turn_payload("s1", "library", "operator", turn) is None


def test_dedup_key_stable_across_reruns_of_the_same_window(tc):
    """The same turn, reprocessed (e.g. because it was the held-back trailing
    turn on a prior run), must produce the identical dedup_key."""
    turn = {"start_byte": 100, "rows": [{"kind": "prompt", "label": "you", "body": "hi"}]}
    p1 = tc.turn_payload("s1", "library", "operator", turn)
    p2 = tc.turn_payload("s1", "library", "operator", dict(turn))
    assert p1["dedup_key"] == p2["dedup_key"]


# --- cursor resume -----------------------------------------------------------------


def _write_lines(path, records):
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_capture_file_holds_back_a_still_growing_trailing_turn(tc, tmp_path):
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "timestamp": "2026-08-19T00:00:00Z",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "sessionId": "s1", "uuid": "u2", "requestId": "req_1",
         "message": {"id": "msg_1", "content": [{"type": "text", "text": "first reply"}]}},
    ])
    posted = []
    new_cursor, tally = tc.capture_file(
        path, 0, time.time(), "library", "operator", "tok",
        poster=lambda payload, token: posted.append(payload) or "created")
    # the only turn found is the trailing one, and the file was just
    # written (well within LIVE_THRESHOLD_SECONDS) — held back, not posted
    assert posted == []
    assert tally == {"created": 0, "duplicate": 0, "skipped": 0, "errors": 0}
    assert new_cursor == 0  # rewound to the turn's own start


def test_capture_file_posts_a_turn_once_closed_by_the_next_prompt(tc, tmp_path):
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "timestamp": "2026-08-19T00:00:00Z",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "sessionId": "s1", "uuid": "u2", "requestId": "req_1",
         "message": {"id": "msg_1", "content": [{"type": "text", "text": "first reply"}]}},
    ])
    now = time.time()
    posted = []
    poster = lambda payload, token: posted.append(payload) or "created"  # noqa: E731
    cur1, _ = tc.capture_file(path, 0, now, "library", "operator", "tok", poster=poster)
    assert posted == []

    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u3",
         "timestamp": "2026-08-19T00:02:00Z",
         "message": {"role": "user", "content": "second prompt"}},
    ])
    cur2, tally2 = tc.capture_file(path, cur1, now, "library", "operator", "tok", poster=poster)
    assert len(posted) == 1
    assert posted[0]["body"] == "you: first prompt\n\nclaude: first reply"
    assert tally2["created"] == 1
    assert cur2 != cur1  # advanced past the now-closed first turn


def test_capture_file_resume_never_reposts_an_already_closed_turn(tc, tmp_path):
    """The full incremental cycle: process once, append more, process again —
    the first (closed) turn is posted exactly once across both runs, never
    duplicated."""
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "sessionId": "s1", "uuid": "u2", "requestId": "req_1",
         "message": {"id": "msg_1", "content": [{"type": "text", "text": "first reply"}]}},
        {"type": "user", "sessionId": "s1", "uuid": "u3",
         "message": {"role": "user", "content": "second prompt"}},
    ])
    now = time.time()
    posted = []
    poster = lambda payload, token: posted.append(payload) or "created"  # noqa: E731
    cur1, tally1 = tc.capture_file(path, 0, now, "library", "operator", "tok", poster=poster)
    assert tally1["created"] == 1  # the first turn closed by the second prompt
    assert [p["dedup_key"] for p in posted] == ["claude-transcript:s1:0"]

    # more lines land, closing the second turn too
    _write_lines(path, [
        {"type": "assistant", "sessionId": "s1", "uuid": "u4", "requestId": "req_2",
         "message": {"id": "msg_2", "content": [{"type": "text", "text": "second reply"}]}},
        {"type": "user", "sessionId": "s1", "uuid": "u5",
         "message": {"role": "user", "content": "third prompt"}},
    ])
    cur2, tally2 = tc.capture_file(path, cur1, now, "library", "operator", "tok", poster=poster)
    assert tally2["created"] == 1  # only the newly-closed second turn
    dedup_keys = [p["dedup_key"] for p in posted]
    assert len(dedup_keys) == len(set(dedup_keys))  # no duplicate POST for turn 1


def test_capture_file_closes_trailing_turn_once_file_goes_quiet(tc, tmp_path):
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "message": {"role": "user", "content": "only prompt"}},
    ])
    now = time.time()
    posted = []
    poster = lambda payload, token: posted.append(payload) or "created"  # noqa: E731
    cur1, tally1 = tc.capture_file(path, 0, now, "library", "operator", "tok", poster=poster)
    assert posted == []  # held back, file just written

    # simulate the file having gone quiet past LIVE_THRESHOLD_SECONDS
    later = now + tc.LIVE_THRESHOLD_SECONDS + 1
    cur2, tally2 = tc.capture_file(path, cur1, later, "library", "operator", "tok", poster=poster)
    assert tally2["created"] == 1
    assert posted[0]["body"] == "you: only prompt"


def test_capture_file_keeps_old_cursor_on_post_error(tc, tmp_path):
    """A file whose posts errored this run must not have its cursor advanced
    — the caller (main()) relies on this via tally['errors']."""
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "message": {"role": "user", "content": "prompt"}},
        {"type": "user", "sessionId": "s1", "uuid": "u2",
         "message": {"role": "user", "content": "prompt2"}},
    ])
    now = time.time()
    _, tally = tc.capture_file(
        path, 0, now, "library", "operator", "tok",
        poster=lambda payload, token: "error:500")
    assert tally["errors"] >= 1


def test_capture_file_resets_cursor_when_file_shrank(tc, tmp_path):
    """A cursor byte offset past the current file size (rotation/replacement)
    must not seek past EOF — restart from 0 rather than reading nothing
    forever."""
    path = tmp_path / "s1.jsonl"
    _write_lines(path, [
        {"type": "user", "sessionId": "s1", "uuid": "u1",
         "message": {"role": "user", "content": "prompt"}},
    ])
    now = time.time() + 10_000  # force "quiet" so the lone turn posts
    posted = []
    new_cursor, tally = tc.capture_file(
        path, 999_999, now, "library", "operator", "tok",
        poster=lambda payload, token: posted.append(payload) or "created")
    assert len(posted) == 1
    assert tally["errors"] == 0


# --- POST payload / HTTP shape -----------------------------------------------------


def test_post_signal_sends_expected_headers_and_body(tc, monkeypatch):
    captured = {}

    class _Resp:
        status = 201
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    # tc.post_signal delegates to signal_client.post_signal (ARCH-353/356,
    # issue_01M3SS45WJBKZM93Z2PE4H2XDJ), so the real POST is made with `tc.SC`'s
    # own urllib.request, not a module-level one tc itself still imports.
    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    payload = {"source": "claude-transcript", "kind": "transcript-turn",
               "actor": "operator", "product": "library", "body": "hi",
               "content": "hi", "dedup_key": "k1"}
    result = tc.post_signal(payload, "sekret")
    assert result == "created"
    # The plane is pinned to Orient for every client test (conftest's
    # `_pin_the_plane`); the Library-vs-Orient URL/header shape itself is
    # test_vault_plane.py's job, not this client's.
    assert captured["url"] == tc.SC.vault_plane.url("/api/signals")
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer sekret"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == payload


def test_post_signal_reports_duplicate_on_200(tc, monkeypatch):
    class _Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    assert tc.post_signal({"dedup_key": "k1"}, "tok") == "duplicate"


def test_post_signal_reports_http_error(tc, monkeypatch):
    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", raise_http)
    assert tc.post_signal({"dedup_key": "k1"}, "tok") == "error:400"


# --- 429 / Retry-After (work item issue_01M3SS45WJBKZM93Z2PE4H2XDJ) ------------


class _CreatedResp:
    status = 201
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_post_signal_retries_a_429_after_its_retry_after_wait(tc, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "2"}, None)
        return _CreatedResp()

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    slept = []
    result = tc.post_signal({"dedup_key": "k1"}, "tok",
                            deadline=tc.time.monotonic() + 600, sleep=slept.append)
    assert result == "created"
    assert calls["n"] == 2
    assert slept == [2.0]


def test_post_signal_with_no_deadline_never_retries_a_429(tc, monkeypatch):
    """`main()` always computes and passes a run `deadline` (see
    `_RUN_DEADLINE_SECONDS`), but `tc.post_signal`'s own default is
    `deadline=None` — and with no deadline, signal_client.post_signal fails
    fast on the first 429 rather than retrying into a caller's budget that
    was never given. Covers the no-deadline branch directly, since every
    other 429 test here supplies one."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "1"}, None)

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    slept = []
    result = tc.post_signal({"dedup_key": "k1"}, "tok", sleep=slept.append)
    assert result == "error:429"
    assert calls["n"] == 1
    assert slept == []


def test_a_run_of_300_signals_lands_all_of_them_under_a_429_storm(tc, monkeypatch):
    """Done-when: against a fake Library returning 429 with Retry-After: 2 on
    every third call, a run of 300 signals lands all 300 and reports nothing
    lost. The sleep is injected so the test does not wait real seconds."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "2"}, None)
        return _CreatedResp()

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    deadline = tc.time.monotonic() + 600
    slept = []
    results = [tc.post_signal({"dedup_key": f"k{i}"}, "tok", deadline=deadline,
                              sleep=slept.append)
              for i in range(300)]
    assert results.count("created") == 300
    assert not [r for r in results if r.startswith("error")]
    assert slept  # the 429s really were hit and really were waited out


def test_a_deadline_expiring_mid_retry_reports_the_unsent_signal_as_a_failure(tc, monkeypatch):
    """The capture's own deadline ends while a 429 is still being waited out:
    the signal comes back as 'error:429', never silently dropped — and the
    cursor logic (test_capture_file_keeps_old_cursor_on_post_error) re-covers
    it next run rather than losing it."""
    def always_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "2"}, None)

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", always_429)
    monkeypatch.setattr(tc.SC.time, "monotonic", lambda: 100.0)
    slept = []
    result = tc.post_signal({"dedup_key": "k1"}, "tok", deadline=101.0, sleep=slept.append)
    assert result == "error:429"
    assert slept == []  # the deadline check runs before the wait, not after


def test_main_reports_a_signal_still_unsent_at_the_deadline_as_a_failure(
        tc, tmp_path, monkeypatch):
    """End to end through main(): a 429-storm signal that never lands inside
    the run's deadline is counted in tally["errors"] (so main() exits 1 and
    the cursor does not advance), the same visible-failure contract every
    other delivery error already has — never a silent drop."""
    quiet = time.time() - tc.LIVE_THRESHOLD_SECONDS - 60
    projects_dir = tmp_path / "projects"
    library = _home_prefix(tc, "library")
    d = projects_dir / library
    d.mkdir(parents=True)
    f = d / "sess.jsonl"
    _write_lines(f, [{"type": "user", "sessionId": "sess", "uuid": "u1",
                      "message": {"role": "user", "content": "only prompt"}}])
    os.utime(f, (quiet, quiet))

    def always_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "2"}, None)

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", always_429)
    # The run's deadline is computed as time.monotonic() + _RUN_DEADLINE_SECONDS
    # inside main() itself, once; `time` is the one module object shared by
    # main() and signal_client.post_signal's own deadline check, so a clock
    # that jumps 1000s on every call makes the first 429 land past the
    # deadline computed from the FIRST call, without ever looping.
    tick = {"n": 0}

    def fake_monotonic():
        tick["n"] += 1000.0
        return tick["n"]

    monkeypatch.setattr(tc.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(tc, "CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr(tc, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setenv("TRANSCRIPT_CAPTURE_TOKEN", "tok")
    monkeypatch.setattr(tc.sys, "argv", ["transcript-capture"])

    with pytest.raises(SystemExit) as exc:
        tc.main()
    assert exc.value.code == 1
    assert not tc.CURSOR_FILE.exists() or json.loads(tc.CURSOR_FILE.read_text()) == {}


# --- attempts: this wrapper asks for only the 429 retry, never 503/timeout -----


def test_post_signal_makes_exactly_one_post_on_a_503(tc, monkeypatch):
    """No `attempts` is passed to signal_client.post_signal by this wrapper
    (see `_POST_TIMEOUT`'s neighbouring comment) — attempts defaults to 1,
    which never retries a 503/timeout/connection-error. Round 1 (e15a9552)
    passed `attempts=_POST_ATTEMPTS` (3) here as a side effect of wanting the
    429 retry, which also retried a 503 this job never asked for; a later
    change reintroducing `attempts > 1` would silently bring that back."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    result = tc.post_signal({"dedup_key": "k1"}, "tok")
    assert result == "error:503"
    assert calls["n"] == 1


def test_post_signal_makes_exactly_one_post_on_a_timeout(tc, monkeypatch):
    """Same contract as the 503 case above, for the other branch
    `signal_client._retryable` would otherwise retry: a timeout."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(tc.SC.urllib.request, "urlopen", fake_urlopen)
    result = tc.post_signal({"dedup_key": "k1"}, "tok")
    assert result == "error:TimeoutError"
    assert calls["n"] == 1


# --- token resolution ----------------------------------------------------------------


def test_capture_token_prefers_env_var(tc, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_CAPTURE_TOKEN", "from-env")
    assert tc.capture_token() == "from-env"


def test_capture_token_falls_back_to_file(tc, monkeypatch, tmp_path):
    monkeypatch.delenv("TRANSCRIPT_CAPTURE_TOKEN", raising=False)
    token_file = tmp_path / "transcript-capture-token"
    token_file.write_text("from-file\n")
    monkeypatch.setattr(tc, "TOKEN_FILE", token_file)
    assert tc.capture_token() == "from-file"


# --- its own credential, with a fallback to the shared one (issue_01M3VY56TWPFFHQ50QTFM5P07K) --
#
# library#176 rate-limits POST /signals to 120/minute per authenticated actor;
# this job alone can peak at 576/minute, so sharing the agent-platform token
# with every other sender let it eat the whole bucket. These lock in the
# fallback to the shared token while the Library's minted file is not yet
# installed, and that the fallback never fires once the job's own credential
# is present.


def test_capture_token_falls_back_to_the_shared_token_with_a_warning(tc, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TRANSCRIPT_CAPTURE_TOKEN", raising=False)
    monkeypatch.setattr(tc, "TOKEN_FILE", tmp_path / "missing")
    monkeypatch.setattr(tc.SC, "token", lambda: "shared-secret")
    assert tc.capture_token() == "shared-secret"
    err = capsys.readouterr().err
    assert "sharing the agent-platform signal bucket" in err
    assert str(tmp_path / "missing") in err


def test_capture_token_never_falls_back_when_its_own_credential_is_set(tc, monkeypatch, capsys):
    monkeypatch.setenv("TRANSCRIPT_CAPTURE_TOKEN", "own-secret")
    monkeypatch.setattr(tc.SC, "token", lambda: "shared-secret")
    assert tc.capture_token() == "own-secret"
    assert capsys.readouterr().err == ""


def test_capture_token_exits_when_neither_credential_is_available(tc, monkeypatch, tmp_path):
    monkeypatch.delenv("TRANSCRIPT_CAPTURE_TOKEN", raising=False)
    monkeypatch.setattr(tc, "TOKEN_FILE", tmp_path / "missing")
    monkeypatch.setattr(tc.SC, "token", lambda: None)
    with pytest.raises(SystemExit):
        tc.capture_token()
