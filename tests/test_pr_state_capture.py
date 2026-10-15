"""`bin/pr-state-capture` (issue_01M2KV16ZRG3N49GRER8Q7545H) — the Bosun's
read-only, convivy-bosun-App-authenticated per-repository PR-state signal
capture, separate from the gates' live GitHub read (see the script's module
docstring for why the two must not share a source).

Uses a recorded-shape fixture for the `GET /repos/.../pulls` payload
(tests/fixtures/pr_state_capture/pulls.json) rather than the real GitHub API,
and copies/mutates it to exercise the required transitions: a new PR, an
unchanged PR (no second row), a PR whose head sha moves, a PR whose state
moves, a PR whose draft flag moves with no new commit, a PR whose title moves
with no new commit, and a closed PR. Also covers sanitising GitHub-authored
free text (title, author login) before it reaches an emitted signal's body
or meta — see bin/pr-state-capture's `rows_from_pulls` docstring.

Round 4 adds coverage for `_identity_title`'s two fixes: the five
Default_Ignorable codepoints outside category Cf that round 3 missed
(`_ROUND4_IGNORABLE_CHARS`), and the NFKC compatibility fold applied only to
the identity value (a fullwidth letter, a ligature, a circled digit each
folding to the same dedup key as their plain ASCII form, while their
displayed `title` stays distinct).

Round 5 splits DISPLAY from IDENTITY further: `signal_client.sanitise`
(display) now keeps ZWJ, the variation selectors, ZWNJ and soft hyphen,
because removing them can change what a reader sees (a ZWJ-joined family
emoji splitting into four separate people; a gitmoji recycle symbol losing
its forced emoji presentation), while `_identity_title` still removes ALL
of them via `signal_client.is_default_ignorable` directly — the dedup key
cares only whether a reader can tell two titles apart, not whether the
Default_Ignorable property happens to leave a compositional trace
(`_ROUND5_KEPT_IN_DISPLAY_CHARS`).

Send-only-what-changed (issue_01M3WQQ1STSGNZ6SKSMFGSBS4X): the "429 /
Retry-After" and "send-state / changed-rows-only" sections below cover the
local delivered-state record (`load_delivered`/`save_delivered`), the
newest-updated-first ordering across repos, and the pass-wide stop on a
429 that survives `signal_client.post_signal`'s own retry/backoff — the fix
for the Library's POST /signals 429 flood that held pr-state-capture's own
`err.log` at 6 MB (43,452 "NOT delivered: error:429" lines on 2026-10-01).
The `_state_dir` fixture below points every test's `STATE_DIR` at a fresh
`tmp_path`, autoused, so no test ever reads or writes the real
`~/.local/state/agent-platform/pr-state-capture/`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import plistlib
from pathlib import Path

import pytest

import projects

FIX = Path(__file__).resolve().parent / "fixtures" / "pr_state_capture" / "pulls.json"
PLIST = (Path(__file__).resolve().parent.parent / "launchd"
         / "com.convivy.pr-state-capture.plist")


@pytest.fixture(autouse=True)
def _state_dir(pr_state_capture, tmp_path, monkeypatch):
    """Every test in this module gets its own `STATE_DIR`, never the real
    one — `pr_state_capture` is a session-scoped module fixture, so without
    this a test calling `main()`/`save_delivered` would write real send-state
    under the operator's home directory."""
    monkeypatch.setattr(pr_state_capture, "STATE_DIR", tmp_path / "pr-state-capture-state")

# A 200-char title prefix made of short space-separated words, not one run of
# 32+ alphanumeric characters — `sanitise`'s long-run redaction would collapse
# a 200-char run of one letter to a constant "[redacted]", which defeats the
# point of a test asserting two titles differing only past character 200.
_LONG_PREFIX = "word " * 40
assert len(_LONG_PREFIX) == 200


def _pulls():
    return copy.deepcopy(json.loads(FIX.read_text()))


def _pulls_with_title(number: int, title: str) -> list[dict]:
    """A fixture copy with PR `number`'s title replaced — the only way to
    exercise `title_hash`, which `rows_from_pulls` derives from the full
    sanitised title rather than something a test can override on a row dict
    directly (see `dedup_key_for`'s docstring)."""
    pulls = _pulls()
    for p in pulls:
        if p["number"] == number:
            p["title"] = title
    return pulls


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- repos_to_capture(): derived from the registry, private projects excluded ----


def test_repos_to_capture_is_derived_from_the_registry(pr_state_capture):
    repos = pr_state_capture.repos_to_capture()
    assert "convivy/agent-platform" in repos
    assert "convivy/knowledge" in repos
    assert repos == sorted(repos)


@pytest.fixture
def synthetic_project_registry(tmp_path, monkeypatch):
    """Point `projects.PROJECTS_JSON` at a registry naming one public and one
    private synthetic crew (test_projects.py's `crafted_registry` fixture does
    the same for the cross-user-path tests), so the exclusion mechanism below
    is exercised without the operator's own crew slugs."""
    registry = {
        "public-x": {"class": "project", "display": "Public X", "owner": "convivy",
                     "home_dir": "<root>/public-x", "dirs": [], "crew": "public-x",
                     "lifecycle": "active", "products": [], "repos": ["public-x"],
                     "aliases": [], "kb": "local"},
        "private-a": {"class": "project", "display": "Private A", "owner": "convivy",
                      "home_dir": "<root>/private-a", "dirs": [], "crew": "private-a",
                      "lifecycle": "active", "products": [], "repos": ["private-a"],
                      "aliases": [], "kb": "local", "private": True},
    }
    p = tmp_path / "synthetic-projects.json"
    p.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_repos_to_capture_excludes_private_projects(pr_state_capture, synthetic_project_registry):
    """A black-box crew (ADR-0043, `private: true`) is excluded the same way its
    other output never reaches a shared store; a public crew in the same
    registry is not."""
    repos = pr_state_capture.repos_to_capture()
    assert "convivy/public-x" in repos
    assert "convivy/private-a" not in repos


@pytest.fixture
def synthetic_registry_with_a_non_convivy_owner(tmp_path, monkeypatch):
    """A project with one plain repo and one repo pinned to a non-`convivy`
    GitHub owner via `repo_owners` — the shape `config/projects.json` uses
    for Shipwright's homebrew-tap (under `convivybuilder`), isolated from
    today's real registry so this test does not depend on that entry
    staying as it is."""
    registry = {
        "shipyard": {"class": "project", "display": "Shipyard", "owner": "convivy",
                    "home_dir": "<root>/shipyard", "dirs": [], "crew": "shipyard",
                    "lifecycle": "active", "products": [], "kb": "local",
                    "repos": ["shipyard", "shipyard-tap"], "aliases": [],
                    "repo_owners": {"shipyard-tap": "shipyard-builder"}},
    }
    p = tmp_path / "synthetic-projects.json"
    p.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "PROJECTS_JSON", p)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_repos_to_capture_resolves_a_repo_under_its_own_registered_owner(
        pr_state_capture, synthetic_registry_with_a_non_convivy_owner):
    """A repo named in a project's `repo_owners` resolves to THAT owner, not
    `convivy` — this is the fix: `repos_to_capture` used to build every slug
    as `f"convivy/{r}"` regardless of `repo_owners`, which is how
    convivy/homebrew-tap (never on GitHub) replaced the real
    convivybuilder/homebrew-tap."""
    repos = pr_state_capture.repos_to_capture()
    assert "shipyard-builder/shipyard-tap" in repos
    assert "convivy/shipyard-tap" not in repos


def test_repos_to_capture_keeps_an_unowned_repo_s_slug_and_state_key_unchanged(
        pr_state_capture, synthetic_registry_with_a_non_convivy_owner):
    """A repo with no `repo_owners`/`github_owner` override still resolves to
    `convivy/<repo>`, exactly the slug the old hardcoded `f"{OWNER}/{r}"`
    gave it, so a delivered-state record written under that filename before
    this change still matches — `_state_path` keys on the slug and is
    otherwise untouched by this change."""
    repos = pr_state_capture.repos_to_capture()
    assert "convivy/shipyard" in repos
    assert pr_state_capture._state_path("convivy/shipyard").name == "convivy__shipyard.json"


# --- list_pulls(): the GET, its query params, and pagination --------------------


def test_list_pulls_requests_state_all_sorted_by_updated(pr_state_capture):
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        return _Resp(200, _pulls())

    out = pr_state_capture.list_pulls("convivy/agent-platform", "ghs_x", opener=opener)
    assert len(out) == 4
    assert seen["method"] == "GET" and seen["auth"] == "Bearer ghs_x"
    assert "state=all" in seen["url"] and "sort=updated" in seen["url"]
    assert "repos/convivy/agent-platform/pulls" in seen["url"]


def test_list_pulls_paginates_until_a_short_page(pr_state_capture):
    page1 = [dict(p, number=1000 + i) for i, p in enumerate(_pulls() * 25)][:100]
    page2 = _pulls()[:2]
    pages = {1: page1, 2: page2}
    calls = []

    def opener(req, timeout=None):
        page = int(req.full_url.split("&page=")[1].split("&")[0])
        calls.append(page)
        return _Resp(200, pages[page])

    out = pr_state_capture.list_pulls("convivy/agent-platform", "ghs_x", opener=opener)
    assert calls == [1, 2]
    assert len(out) == 102


def test_list_pulls_a_non_200_is_unreadable(pr_state_capture):
    def opener(req, timeout=None):
        return _Resp(403, {"message": "nope"})

    with pytest.raises(pr_state_capture.GA.Unreadable):
        pr_state_capture.list_pulls("convivy/agent-platform", "ghs_x", opener=opener)


# --- rows_from_pulls() / _state(): field mapping, merged folded in ---------------


def test_rows_from_pulls_maps_every_field_the_panel_needs(pr_state_capture):
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    by_number = {r["number"]: r for r in rows}

    open_row = by_number[601]
    assert open_row["repo"] == "convivy/agent-platform"
    assert open_row["title"] == "feat(bosun): read PR state as the convivy-bosun App"
    assert open_row["title_hash"] == hashlib.sha256(
        open_row["title"].encode("utf-8")).hexdigest()[:16]
    assert open_row["author"] == "convivy-bosun[bot]"
    assert open_row["draft"] is False
    assert open_row["head_sha"] == "1111111111111111111111111111111111aaaa"
    assert open_row["state"] == "OPEN"

    draft_row = by_number[598]
    assert draft_row["draft"] is True
    assert draft_row["state"] == "OPEN"


def test_rows_from_pulls_sanitises_a_hostile_title(pr_state_capture):
    """`title` is GitHub-authored free text (any collaborator can set it),
    not platform-authored — it must come back through the same untrusted-
    output contract as every other API-response value (signal_client.sanitise).
    One PR carries markup, a newline, an ANSI escape and a very long run, all
    in the title, exercising every one of sanitise's transforms at once."""
    pulls = _pulls()
    pulls[0]["title"] = (
        "[click me](javascript:alert(1)) line one\nline two\x1b[31mRED\x1b[0m "
        + ("x" * 300)
    )
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls)
    title = rows[0]["title"]
    assert "\n" not in title
    assert "\x1b" not in title
    assert len(title) <= 203  # SC.sanitise's max_len=200 plus "..."


def test_rows_from_pulls_sanitises_a_hostile_author_login(pr_state_capture):
    pulls = _pulls()
    pulls[0]["user"]["login"] = "evil\x1b[31m\nlogin" + ("x" * 200)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls)
    author = rows[0]["author"]
    assert "\n" not in author
    assert "\x1b" not in author
    assert len(author) <= 103  # SC.sanitise's max_len=100 plus "..."


def test_emit_never_carries_unsanitised_github_text_into_the_signal(pr_state_capture, monkeypatch):
    """End-to-end: the hostile values land in the emitted body and meta only
    in their sanitised form, whichever emitter call built them."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    pulls = _pulls()
    pulls[0]["title"] = "hostile\ntitle\x1b[31m" + ("x" * 300)
    pulls[0]["user"]["login"] = "hostile\nauthor\x1b[31m"
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls)
    pr_state_capture.emit(rows[0])
    posted = next(iter(store.rows.values()))
    assert "\n" not in posted["body"] and "\x1b" not in posted["body"]
    assert "\n" not in posted["meta"]["title"] and "\x1b" not in posted["meta"]["title"]
    assert "\n" not in posted["meta"]["author"] and "\x1b" not in posted["meta"]["author"]


def test_rows_from_pulls_strips_a_bidi_override_before_display(pr_state_capture):
    """A right-to-left override (U+202E) can make Deck's rendered title read
    as something other than its own bytes — round 3's second finding on top
    of the dedup-key one."""
    pulls = _pulls_with_title(601, "safe‮txt.exe")
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls)
    title = next(r for r in rows if r["number"] == 601)["title"]
    assert "‮" not in title
    assert title == "safetxt.exe"


def test_a_merged_pr_reads_as_merged_not_closed(pr_state_capture):
    """The raw REST list endpoint reports only open/closed plus `merged_at`;
    Deck must see the same OPEN/CLOSED/MERGED vocabulary `gh`-backed reads use
    elsewhere on this platform (agentctl's own `pr_state` helper)."""
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    by_number = {r["number"]: r for r in rows}
    assert by_number[592]["state"] == "MERGED"


def test_a_closed_unmerged_pr_is_captured_and_reads_as_closed(pr_state_capture):
    """Recently-closed PRs are what Deck needs this capture for — confirm a
    closed-without-merging PR is both present in the rows and correctly
    distinguished from a merged one."""
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    by_number = {r["number"]: r for r in rows}
    assert by_number[587]["state"] == "CLOSED"


# --- dedup_key_for(): the signal's own identity is the dedup mechanism ----------


def test_dedup_key_is_stable_for_an_unchanged_row(pr_state_capture):
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    assert (pr_state_capture.dedup_key_for(row)
            == pr_state_capture.dedup_key_for(dict(row)))


def test_dedup_key_changes_when_the_head_sha_moves(pr_state_capture):
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    moved = dict(row, head_sha="5555555555555555555555555555555555eeee")
    assert pr_state_capture.dedup_key_for(row) != pr_state_capture.dedup_key_for(moved)


def test_dedup_key_changes_when_the_state_moves(pr_state_capture):
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    moved = dict(row, state="MERGED")
    assert pr_state_capture.dedup_key_for(row) != pr_state_capture.dedup_key_for(moved)


def test_dedup_key_is_stable_across_different_repos_only_when_repo_differs(pr_state_capture):
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    other_repo = dict(row, repo="convivy/library")
    assert pr_state_capture.dedup_key_for(row) != pr_state_capture.dedup_key_for(other_repo)


def test_dedup_key_changes_when_the_draft_flag_moves(pr_state_capture):
    """A draft-to-ready flip usually carries no new commit, so `head_sha` and
    `state` both hold — the draft flag has to be its own axis in the key or
    Deck's draft badge goes stale on exactly that transition."""
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    draft_row = next(r for r in rows if r["number"] == 598)
    assert draft_row["draft"] is True
    flipped = dict(draft_row, draft=False)
    assert pr_state_capture.dedup_key_for(draft_row) != pr_state_capture.dedup_key_for(flipped)


def test_dedup_key_changes_when_the_title_moves(pr_state_capture):
    """The key's title component is `title_hash`, derived inside
    `rows_from_pulls` — a retitled row has to come from a re-run of
    `rows_from_pulls` against a changed title, not a hand-edited `title`
    field on a copied row, since editing `title` alone leaves `title_hash`
    (and therefore the key) untouched."""
    row = next(r for r in pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
              if r["number"] == 601)
    retitled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "a different title"))
        if r["number"] == 601)
    assert pr_state_capture.dedup_key_for(row) != pr_state_capture.dedup_key_for(retitled)


def test_dedup_key_changes_when_titles_differ_only_after_character_200(pr_state_capture):
    """The round-3 fix for the reviewer's own round-2 suggestion: the key's
    title component is a hash of the FULL sanitised title, not the 200-char
    display value, so two titles differing only past that cutoff still mint
    distinct keys — even though their displayed `title` is identical."""
    prefix = _LONG_PREFIX
    row_a = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, prefix + "AAA"))
        if r["number"] == 601)
    row_b = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, prefix + "BBB"))
        if r["number"] == 601)
    assert row_a["title"] == row_b["title"]  # both truncate to the same display value
    assert pr_state_capture.dedup_key_for(row_a) != pr_state_capture.dedup_key_for(row_b)


def test_dedup_key_is_unchanged_when_a_trailing_invisible_character_toggles(pr_state_capture):
    """The round-3 finding itself: a trailing zero-width space is an invisible
    edit, so it must NOT mint a new key — sanitise strips the character before
    the hash is taken, on both sides."""
    row = next(r for r in pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
              if r["number"] == 601)
    toggled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform",
        _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App​"))
        if r["number"] == 601)
    assert pr_state_capture.dedup_key_for(row) == pr_state_capture.dedup_key_for(toggled)


# --- round 4: Default_Ignorable widening + NFKC identity folding ---------------
#
# security-reviewer's round-4 finding: category Cf alone (round 3) still let
# five named codepoints outside Cf toggle the key with no visible edit, and
# separately, NFC alone (round 3) left compatibility-equivalent forms — a
# fullwidth letter, a ligature, a circled digit — minting distinct keys for
# text a human reads as identical. Both are fixed via `_identity_title`
# (NFKC on top of `SC.sanitise`'s now-widened Default_Ignorable strip).

_ROUND4_IGNORABLE_CHARS = (
    "͏",  # COMBINING GRAPHEME JOINER (category Mn, not Cf)
    "️",  # VARIATION SELECTOR-16 (category Mn, not Cf)
    "ㅤ",  # HANGUL FILLER (category Lo, not Cf)
    "ﾠ",  # HALFWIDTH HANGUL FILLER (category Lo, not Cf)
    "ᅟ",  # HANGUL CHOSEONG FILLER (category Lo, not Cf)
)


def test_dedup_key_is_unchanged_for_every_round_4_named_character(pr_state_capture):
    """Each of the five codepoints security-reviewer's round-4 finding named
    (confirmed by running `sanitise()` against `base` and `base + <char>`),
    asserted individually not to change the key — the same shape as round 3's
    own per-character test, for the characters that escaped round 3's fix."""
    base_row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls()) if r["number"] == 601)
    for ch in _ROUND4_IGNORABLE_CHARS:
        toggled = next(r for r in pr_state_capture.rows_from_pulls(
            "convivy/agent-platform",
            _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App" + ch))
            if r["number"] == 601)
        assert pr_state_capture.dedup_key_for(base_row) == pr_state_capture.dedup_key_for(toggled), ch


def test_dedup_key_is_the_same_for_a_fullwidth_and_ascii_title(pr_state_capture):
    """NFC (round 3) leaves a fullwidth letter distinct from its ASCII
    equivalent; NFKC (round 4, applied only to the identity value) folds it,
    since a human reads "Ａ" and "A" as the same title."""
    row_fullwidth = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "Ａ fix")) if r["number"] == 601)
    row_ascii = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "A fix")) if r["number"] == 601)
    assert row_fullwidth["title"] != row_ascii["title"]  # still visually distinct on display
    assert (pr_state_capture.dedup_key_for(row_fullwidth)
            == pr_state_capture.dedup_key_for(row_ascii))


def test_dedup_key_is_the_same_for_a_ligature_and_its_expansion(pr_state_capture):
    row_ligature = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "ﬁx the bug")) if r["number"] == 601)
    row_expanded = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "fix the bug")) if r["number"] == 601)
    assert (pr_state_capture.dedup_key_for(row_ligature)
            == pr_state_capture.dedup_key_for(row_expanded))


def test_dedup_key_is_the_same_for_a_circled_digit_and_its_digit(pr_state_capture):
    row_circled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "step ①")) if r["number"] == 601)
    row_digit = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "step 1")) if r["number"] == 601)
    assert (pr_state_capture.dedup_key_for(row_circled)
            == pr_state_capture.dedup_key_for(row_digit))


def test_dedup_key_still_changes_for_two_genuinely_different_titles(pr_state_capture):
    """The identity folding must not make everything collide — two titles
    that read differently to a human still mint distinct keys."""
    row_a = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "fix the login bug")) if r["number"] == 601)
    row_b = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "add the export feature")) if r["number"] == 601)
    assert (pr_state_capture.dedup_key_for(row_a)
            != pr_state_capture.dedup_key_for(row_b))


def test_a_default_ignorable_character_toggle_mints_no_new_row(pr_state_capture, monkeypatch):
    """End-to-end version of the round-4 finding: a PR author toggling one of
    the five named codepoints on their own title must not mint a new
    "transition" row, the same property round 3 established for category Cf."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    row = next(r for r in pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
              if r["number"] == 601)
    first = pr_state_capture.emit(row)
    for ch in _ROUND4_IGNORABLE_CHARS:
        toggled = next(r for r in pr_state_capture.rows_from_pulls(
            "convivy/agent-platform",
            _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App" + ch))
            if r["number"] == 601)
        result = pr_state_capture.emit(toggled)
        assert result == "duplicate", ch
    assert first == "created"
    assert len(store.rows) == 1


def test_a_fullwidth_and_ascii_title_mint_only_one_row(pr_state_capture, monkeypatch):
    """End-to-end version of the compatibility-folding fix: a fullwidth and
    an ASCII title that read as the same word must produce exactly one row,
    even though their displayed `title` differs."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    row_fullwidth = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "Ａ fix")) if r["number"] == 601)
    row_ascii = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "A fix")) if r["number"] == 601)
    first = pr_state_capture.emit(row_fullwidth)
    second = pr_state_capture.emit(row_ascii)
    assert (first, second) == ("created", "duplicate")
    assert len(store.rows) == 1


def test_a_plain_title_renders_unchanged_for_display(pr_state_capture):
    """Ordinary text is not touched by either the Default_Ignorable widening
    or the identity-only NFKC fold — the display value still reads exactly as
    its author wrote it."""
    row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform",
        _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App"))
        if r["number"] == 601)
    assert row["title"] == "feat(bosun): read PR state as the convivy-bosun App"


# --- round 5: DISPLAY keeps ZWJ/VS/ZWNJ/soft-hyphen, IDENTITY still folds them ---
#
# reviewer's round-4 finding: round 4's full-property strip broke real
# rendering for a ZWJ-joined family emoji and a VS16-selected gitmoji.
# security-reviewer's round-4 finding: the name-substring heuristic behind
# `is_default_ignorable` missed real codepoints. Round 5 fixes both — display
# (`signal_client.sanitise`) now keeps ZWJ, the variation selectors, ZWNJ and
# soft hyphen; `is_default_ignorable` is now an authoritative range-table
# lookup — while `_identity_title` still removes ALL of them, so a title
# differing only by one of these four still mints no new dedup row even
# though `row["title"]` now visibly differs.

_ROUND5_KEPT_IN_DISPLAY_CHARS = (
    "‍",  # ZERO WIDTH JOINER
    "️",  # VARIATION SELECTOR-16
    "‌",  # ZERO WIDTH NON-JOINER
    "­",  # SOFT HYPHEN
)


def test_dedup_key_is_unchanged_for_every_round_5_kept_in_display_character(pr_state_capture):
    """Identity still folds these four, even though display (round 5) no
    longer strips them."""
    base_row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls()) if r["number"] == 601)
    for ch in _ROUND5_KEPT_IN_DISPLAY_CHARS:
        toggled = next(r for r in pr_state_capture.rows_from_pulls(
            "convivy/agent-platform",
            _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App" + ch))
            if r["number"] == 601)
        assert (pr_state_capture.dedup_key_for(base_row)
                == pr_state_capture.dedup_key_for(toggled)), ch


def test_display_title_diverges_from_identity_for_a_round_5_kept_character(pr_state_capture):
    """The other half of the split: display DOES show the difference it is
    now allowed to show, even while identity treats the row as unchanged."""
    base_row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls()) if r["number"] == 601)
    toggled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform",
        _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App‍"))
        if r["number"] == 601)
    assert base_row["title"] != toggled["title"]  # a trailing ZWJ survives display
    assert pr_state_capture.dedup_key_for(base_row) == pr_state_capture.dedup_key_for(toggled)


def test_zwj_joined_family_emoji_title_is_displayed_intact(pr_state_capture):
    """The reviewer's round-4 verification case, at the row level: a
    ZWJ-joined family emoji in a PR title survives display unmangled."""
    family_title = "fix 👨‍👩‍👧‍👦 rendering"
    row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, family_title)) if r["number"] == 601)
    assert row["title"] == family_title


def test_recycle_gitmoji_title_keeps_its_variation_selector(pr_state_capture):
    """Same case for VARIATION SELECTOR-16: a gitmoji-style "recycle" title
    keeps its forced emoji presentation through display."""
    recycle_title = "♻️ refactor: extract shared auth"
    row = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, recycle_title)) if r["number"] == 601)
    assert row["title"] == recycle_title


def test_a_round_5_kept_character_toggle_mints_no_new_row(pr_state_capture, monkeypatch):
    """End-to-end version of the above: a PR author toggling one of these
    four codepoints on their own title must not mint a new "transition" row,
    even though the emitted body text now differs (display keeps them)."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    row = next(r for r in pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
              if r["number"] == 601)
    first = pr_state_capture.emit(row)
    for ch in _ROUND5_KEPT_IN_DISPLAY_CHARS:
        toggled = next(r for r in pr_state_capture.rows_from_pulls(
            "convivy/agent-platform",
            _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App" + ch))
            if r["number"] == 601)
        result = pr_state_capture.emit(toggled)
        assert result == "duplicate", ch
    assert first == "created"
    assert len(store.rows) == 1


# --- emit() / a fake store: a repeated capture produces no second row ----------


class _FakeStore:
    """Mirrors the real signal store's dedup_key idempotency (the same
    mechanism `test_agentctl.py` calls out for `once_key`): the FIRST post of a
    dedup_key is 'created' and stored; every later one is 'duplicate' and
    dropped, so the store's row count is the ground truth for "no second row"."""

    def __init__(self, fail_numbers=()):
        self.rows: dict[str, dict] = {}
        # Every call this store ever saw, successful or not — the row count
        # alone cannot distinguish "attempted and deduped" from "never
        # attempted", which is exactly what the send-only-what-changed tests
        # need to tell apart (see the "send-state / changed-rows-only"
        # section below).
        self.calls: list[str] = []
        # PR numbers this store always refuses (an "error:boom", never
        # stored) — the send-only-what-changed retry test's way of keeping
        # one PR permanently undelivered while its neighbours succeed.
        self._fail_numbers = set(fail_numbers)

    @staticmethod
    def sanitise(s, *, max_len=None):
        # rows_from_pulls calls SC.sanitise directly, so a fake standing in for
        # the whole SC module needs one too — delegate to the real
        # implementation rather than a stub, so a test that swaps this store
        # in still sees real sanitisation on the row it builds. Imported here,
        # not at module scope, because pr_state_capture's own sys.path.insert
        # (adding lib/agent-platform) has not run yet at test-collection time.
        import signal_client
        return signal_client.sanitise(s, max_len=max_len)

    @staticmethod
    def is_default_ignorable(ch):
        # _identity_title (round 5) calls SC.is_default_ignorable directly,
        # on top of SC.sanitise — same delegation reasoning as sanitise above.
        import signal_client
        return signal_client.is_default_ignorable(ch)

    def post_structured(self, kind, stream, body, *, product=None, meta=None,
                        dedup_key=None, produced_at=None, timeout=None,
                        deadline=None, sleep=None, tok=None):
        self.calls.append(dedup_key)
        if (meta or {}).get("number") in self._fail_numbers:
            return "error:boom"
        if dedup_key in self.rows:
            return "duplicate"
        self.rows[dedup_key] = {"kind": kind, "stream": stream, "body": body,
                                "product": product, "meta": meta,
                                "produced_at": produced_at, "tok": tok}
        return "created"


def test_a_new_pr_produces_one_row(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    result = pr_state_capture.emit(row)
    assert result == "created"
    assert len(store.rows) == 1


def test_an_unchanged_pr_captured_twice_produces_no_second_row(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    first = pr_state_capture.emit(row)
    second = pr_state_capture.emit(dict(row))  # a second, independent capture pass
    assert (first, second) == ("created", "duplicate")
    assert len(store.rows) == 1


def test_a_head_sha_move_produces_a_second_row(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    pr_state_capture.emit(row)
    moved = dict(row, head_sha="5555555555555555555555555555555555eeee")
    result = pr_state_capture.emit(moved)
    assert result == "created"
    assert len(store.rows) == 2


def test_a_state_move_produces_a_second_row(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    pr_state_capture.emit(row)
    moved = dict(row, state="MERGED")
    result = pr_state_capture.emit(moved)
    assert result == "created"
    assert len(store.rows) == 2


def test_a_draft_to_ready_flip_with_no_new_commit_produces_a_second_row(pr_state_capture, monkeypatch):
    """The round-2 fix: a PR marked ready-for-review from draft usually carries
    no new commit, so `head_sha` and `state` are unchanged — before the draft
    flag was in the dedup key this was silently treated as a duplicate, and
    Deck's draft badge never cleared."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    draft_row = next(r for r in rows if r["number"] == 598)
    first = pr_state_capture.emit(draft_row)
    ready_row = dict(draft_row, draft=False)  # same head_sha, same state
    second = pr_state_capture.emit(ready_row)
    assert (first, second) == ("created", "created")
    assert len(store.rows) == 2


def test_a_title_edit_with_no_new_commit_produces_a_second_row(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    pr_state_capture.emit(row)
    # same head_sha, same state — only the title (and so title_hash) moves
    retitled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, "a corrected title"))
        if r["number"] == 601)
    result = pr_state_capture.emit(retitled)
    assert result == "created"
    assert len(store.rows) == 2


def test_titles_differing_only_after_character_200_produce_two_rows(pr_state_capture, monkeypatch):
    """End-to-end version of the round-3 dedup-key fix: the reviewer's own
    round-2 suggestion was that keying on the 200-char display title would
    collapse two distinct long titles into one row — confirmed here that both
    now reach the store as `created`."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    prefix = _LONG_PREFIX
    row_a = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, prefix + "AAA"))
        if r["number"] == 601)
    row_b = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls_with_title(601, prefix + "BBB"))
        if r["number"] == 601)
    first = pr_state_capture.emit(row_a)
    second = pr_state_capture.emit(row_b)
    assert (first, second) == ("created", "created")
    assert len(store.rows) == 2


def test_a_trailing_invisible_character_toggle_mints_no_new_row(pr_state_capture, monkeypatch):
    """The round-3 finding itself, end to end: a PR author toggling a
    trailing zero-width space on their own title is an invisible edit and
    must not mint an unbounded sequence of "transition" rows."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    row = next(r for r in pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
              if r["number"] == 601)
    toggled = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform",
        _pulls_with_title(601, "feat(bosun): read PR state as the convivy-bosun App​"))
        if r["number"] == 601)
    first = pr_state_capture.emit(row)
    second = pr_state_capture.emit(toggled)
    assert (first, second) == ("created", "duplicate")
    assert len(store.rows) == 1


def test_a_repeated_capture_over_an_unchanged_title_mints_nothing(pr_state_capture, monkeypatch):
    """Same PR, same title, captured twice (two independent `rows_from_pulls`
    calls, as two real capture passes would produce) — confirms the ordinary
    no-op path still holds now that the key depends on a freshly-computed
    hash rather than a copied field."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    first_pass = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls()) if r["number"] == 601)
    second_pass = next(r for r in pr_state_capture.rows_from_pulls(
        "convivy/agent-platform", _pulls()) if r["number"] == 601)
    first = pr_state_capture.emit(first_pass)
    second = pr_state_capture.emit(second_pass)
    assert (first, second) == ("created", "duplicate")
    assert len(store.rows) == 1


def test_a_closed_pr_is_emitted_like_any_other(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 587)
    result = pr_state_capture.emit(row)
    assert result == "created"
    assert list(store.rows.values())[0]["meta"]["state"] == "CLOSED"


# --- the job's own signal credential (issue_01M3VY56TWPFFHQ50QTFM5P07K) --------
#
# library#176 rate-limits POST /signals to 120/minute per authenticated actor;
# this job alone peaked at 1,878/minute and, since #731, retries a 429 against
# its own run deadline — sharing the agent-platform token with every other
# sender let one burst hold the whole bucket for minutes. These lock in its
# own actor/credential and the fallback to the shared token while the Library's
# minted file is not yet installed.


def test_signal_token_prefers_env_then_file(pr_state_capture, tmp_path, monkeypatch):
    monkeypatch.setenv(pr_state_capture.SIGNAL_TOKEN_ENV, "env-secret")
    assert pr_state_capture.signal_token() == "env-secret"
    monkeypatch.delenv(pr_state_capture.SIGNAL_TOKEN_ENV, raising=False)
    tf = tmp_path / "pr-state-capture-token"
    tf.write_text("file-secret\n")
    monkeypatch.setattr(pr_state_capture, "SIGNAL_TOKEN_FILE", tf)
    assert pr_state_capture.signal_token() == "file-secret"


def test_signal_token_absent_is_none(pr_state_capture, tmp_path, monkeypatch):
    monkeypatch.delenv(pr_state_capture.SIGNAL_TOKEN_ENV, raising=False)
    monkeypatch.setattr(pr_state_capture, "SIGNAL_TOKEN_FILE", tmp_path / "missing")
    assert pr_state_capture.signal_token() is None


def test_emit_forwards_tok_to_post_structured(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    rows = pr_state_capture.rows_from_pulls("convivy/agent-platform", _pulls())
    row = next(r for r in rows if r["number"] == 601)
    pr_state_capture.emit(row, tok="own-token")
    assert list(store.rows.values())[0]["tok"] == "own-token"


def test_main_uses_its_own_credential_and_never_sends_the_shared_token(
        pr_state_capture, monkeypatch, capsys):
    """End to end through main(), the real signal_client: when the job's own
    credential is set, the request on the wire carries it, not the shared
    agent-platform token — and no fallback warning is printed."""
    import signal_client as sc
    monkeypatch.setenv(pr_state_capture.SIGNAL_TOKEN_ENV, "own-secret")
    monkeypatch.setattr(sc, "token", lambda: "shared-secret")
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/agent-platform"])
    monkeypatch.setattr(pr_state_capture, "list_pulls", lambda repo, token, **k: _pulls())

    seen_auth = []

    def fake_urlopen(req, timeout=None):
        seen_auth.append(req.headers.get("Authorization"))
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])

    rc = pr_state_capture.main()
    assert rc == 0
    assert seen_auth  # at least one signal was posted
    assert set(seen_auth) == {"Bearer own-secret"}
    assert "sharing the agent-platform signal bucket" not in capsys.readouterr().err


def test_main_falls_back_to_the_shared_token_with_a_warning_when_its_own_is_absent(
        pr_state_capture, tmp_path, monkeypatch, capsys):
    """Until the Library mints and installs `pr-state-capture`'s own
    credential, the run must keep posting (through the shared bucket), not
    fail — with one stderr line naming the missing file."""
    import signal_client as sc
    monkeypatch.delenv(pr_state_capture.SIGNAL_TOKEN_ENV, raising=False)
    monkeypatch.setattr(pr_state_capture, "SIGNAL_TOKEN_FILE", tmp_path / "missing")
    monkeypatch.setattr(sc, "token", lambda: "shared-secret")
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/agent-platform"])
    monkeypatch.setattr(pr_state_capture, "list_pulls", lambda repo, token, **k: _pulls())

    seen_auth = []

    def fake_urlopen(req, timeout=None):
        seen_auth.append(req.headers.get("Authorization"))
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])

    rc = pr_state_capture.main()
    assert rc == 0
    assert seen_auth
    assert set(seen_auth) == {"Bearer shared-secret"}
    err = capsys.readouterr().err
    assert "sharing the agent-platform signal bucket" in err
    assert str(tmp_path / "missing") in err


# --- 429 / Retry-After (work item issue_01M3SS45WJBKZM93Z2PE4H2XDJ) ------------
#
# These go through the REAL signal_client (not _FakeStore), with a fake
# urlopen standing in for the Library, because the thing under test is
# signal_client.post_signal's 429 retry/deadline behaviour reached through
# emit() — a fake store that never 429s would prove nothing here.


class _CreatedResp:
    status = 201
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _row(n: int) -> dict:
    """A minimal row carrying every field dedup_key_for and emit() need,
    distinct per `n` so each of the 300 lands as its own 'created' row."""
    return {
        "repo": "convivy/agent-platform", "number": n, "title": f"pr {n}",
        "title_hash": f"hash{n}", "author": "someone", "draft": False,
        "head_sha": f"{n:040x}", "state": "OPEN", "updated_at": "",
    }


def test_a_run_of_300_signals_lands_all_of_them_under_a_429_storm(
        pr_state_capture, monkeypatch):
    """Done-when: against a fake Library returning 429 with Retry-After: 2 on
    every third call, a run of 300 signals lands all 300 and reports nothing
    lost. The sleep is injected so the test does not wait real seconds."""
    import signal_client as sc
    monkeypatch.setattr(sc, "token", lambda: "t")
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise sc.urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    deadline = sc.time.monotonic() + 600
    slept = []
    results = [pr_state_capture.emit(_row(n), deadline=deadline, sleep=slept.append)
              for n in range(300)]
    assert results.count("created") == 300
    assert not [r for r in results if r.startswith("error")]
    assert slept  # the 429s really were hit and really were waited out


def test_a_deadline_expiring_mid_retry_reports_the_unsent_signal_as_a_failure(
        pr_state_capture, monkeypatch):
    """The capture's own deadline ends while a 429 is still being waited out:
    the signal comes back as 'error:429', never silently dropped."""
    import signal_client as sc
    monkeypatch.setattr(sc, "token", lambda: "t")

    def always_429(req, timeout=None):
        raise sc.urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)

    monkeypatch.setattr(sc.urllib.request, "urlopen", always_429)
    monkeypatch.setattr(sc.time, "monotonic", lambda: 100.0)
    slept = []
    result = pr_state_capture.emit(_row(1), deadline=101.0, sleep=slept.append)
    assert result == "error:429"
    assert slept == []  # the deadline check runs before the wait, not after


def _wire_429_main(pr_state_capture, monkeypatch, *, repos, pulls_by_repo, urlopen):
    """Common wiring for the main()-level 429 tests below: the real
    signal_client, a fake GitHub App auth/installation path, and a caller-
    supplied `urlopen` standing in for the Library."""
    import signal_client as sc
    monkeypatch.setattr(sc, "token", lambda: "t")
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture", lambda: repos)
    monkeypatch.setattr(pr_state_capture, "list_pulls",
                        lambda repo, token, **k: pulls_by_repo[repo])
    monkeypatch.setattr(sc.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])
    return sc


def test_main_stops_the_whole_pass_across_repos_on_a_429_and_logs_one_summary_line(
        pr_state_capture, monkeypatch, capsys):
    """A 429 that survives the retry/backoff stops the REST of the pass across
    EVERY repo, not just the repo the failing row belonged to. convivy-deck's
    one pending row is given an `updated_at` older than the agent-platform
    row that hits the 429 (2026-09-12 vs. that row's 2026-09-15T20:31:07Z), so
    in the newest-updated-first order it is still queued, from a DIFFERENT
    repo than the one that failed, at the moment the pass stops; the fake
    `urlopen` always succeeds for convivy-deck (it keys the 429 on the posted
    row's own repo, not a raw call count), so if the code under test ever
    attempts that row it lands as delivered. A per-repo stop (tracking only
    the failing repo and skipping just its own remaining rows) WOULD attempt
    and deliver convivy-deck's row, since convivy-deck itself never saw a
    429 — asserting that row was neither emitted (one fewer `urlopen` call
    than a per-repo stop would make) nor recorded as delivered is what a
    per-repo stop gets wrong and a pass-wide stop gets right. Exit code is 0
    (expected, not a failure) and exactly one summary line is logged — not
    the old per-row "NOT delivered" flood that held the real err.log at 6 MB
    (43,452 such lines on 2026-10-01)."""
    deck_pulls = [dict(_pulls()[0], number=9001,
                       # Older than 592's 2026-09-15T20:31:07Z, newer than
                       # 587's 2026-09-09T08:12:33Z: it sorts AFTER the row
                       # that hits the 429 and BEFORE the pass would otherwise
                       # reach it, so it is still pending when the pass stops.
                       updated_at="2026-09-12T00:00:00Z")]
    pulls_by_repo = {
        "convivy/agent-platform": _pulls(),  # 601, 598, 592, 587 (newest first)
        "convivy/convivy-deck": deck_pulls,  # 9001, sorts between 592 and 587
    }
    calls = {"n": 0}
    ap_calls = {"n": 0}

    def urlopen(req, timeout=None):
        # Keyed on the posted row's own repo (from the request body), not a
        # raw call count: convivy-deck's row would otherwise land on
        # whichever call number it happens to be and could spuriously 429
        # (or not) regardless of what the code under test does with it. This
        # way convivy-deck ALWAYS succeeds if the code ever attempts it, so a
        # per-repo stop (which does attempt it) is caught delivering it, not
        # caught by a coincidental failure of its own.
        calls["n"] += 1
        repo = json.loads(req.data)["meta"]["repo"]
        if repo != "convivy/agent-platform":
            return _CreatedResp()
        ap_calls["n"] += 1
        if ap_calls["n"] <= 2:
            return _CreatedResp()
        raise sc.urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)

    sc = _wire_429_main(pr_state_capture, monkeypatch,
                        repos=["convivy/agent-platform", "convivy/convivy-deck"],
                        pulls_by_repo=pulls_by_repo, urlopen=urlopen)
    # Same fast-expire trick as the deadline test above: a clock that jumps
    # far on every read makes the FIRST 429 this run ever sees land past its
    # own deadline immediately, so no real sleep ever happens.
    tick = {"n": 0}

    def fake_monotonic():
        tick["n"] += 1000.0
        return tick["n"]

    monkeypatch.setattr(pr_state_capture.time, "monotonic", fake_monotonic)

    rc = pr_state_capture.main()
    assert rc == 0
    # Pending order is 601, 598, 592, 9001, 587. Calls 1 and 2 (601, 598)
    # succeed; call 3 (592, still agent-platform) hits the 429 and the pass
    # stops there, leaving 9001 (convivy-deck) and 587 (agent-platform) unsent.
    assert calls["n"] == 3
    err = capsys.readouterr().err
    assert "stopped on the Library's rate limit" in err
    assert "sent 2" in err and "3 change(s) left pending" in err
    assert err.count("NOT delivered") == 0  # no per-row flood, not even for the one 429
    # The pass-wide assertion: convivy-deck's row is from a repo that never
    # itself saw a 429, but it was still pending when agent-platform's did,
    # and the stop must have swallowed it too.
    assert pr_state_capture.load_delivered("convivy/convivy-deck") == {}
    assert set(pr_state_capture.load_delivered("convivy/agent-platform")) == {"601", "598"}


def test_main_reports_a_non_429_delivery_error_as_a_failure(pr_state_capture, monkeypatch, capsys):
    """A delivery error that is NOT a 429 (the Library itself failing, a
    connection error) still fails the pass, exactly as before this change —
    only a 429 gets the "stop the pass, exit 0" treatment."""
    def urlopen(req, timeout=None):
        raise sc.urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    sc = _wire_429_main(pr_state_capture, monkeypatch,
                        repos=["convivy/agent-platform"],
                        pulls_by_repo={"convivy/agent-platform": _pulls()}, urlopen=urlopen)

    rc = pr_state_capture.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "NOT delivered" in err and "error:500" in err
    assert "stopped on the Library's rate limit" not in err


def test_main_sends_pending_rows_newest_updated_first_on_the_next_pass_after_a_429(
        pr_state_capture, monkeypatch, capsys):
    """The rows a 429-stopped pass left pending are exactly what the NEXT
    pass sends — and still newest-updated first, so the pass that follows a
    rate-limit stop drains the backlog in the same priority order a fresh
    pass would use."""
    pulls = _pulls()  # newest-updated first: 601, 598, 592, 587
    calls = {"n": 0}

    def urlopen_429_after_one(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _CreatedResp()
        raise sc.urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)

    sc = _wire_429_main(pr_state_capture, monkeypatch,
                        repos=["convivy/agent-platform"],
                        pulls_by_repo={"convivy/agent-platform": pulls},
                        urlopen=urlopen_429_after_one)
    tick = {"n": 0}
    monkeypatch.setattr(pr_state_capture.time, "monotonic",
                        lambda: tick.update(n=tick["n"] + 1000.0) or tick["n"])

    assert pr_state_capture.main() == 0  # PR 601 sent, 598/592/587 left pending

    seen_numbers = []

    def urlopen_records_order(req, timeout=None):
        body = json.loads(req.data.decode())
        seen_numbers.append(body["meta"]["number"])
        return _CreatedResp()

    monkeypatch.setattr(sc.urllib.request, "urlopen", urlopen_records_order)
    assert pr_state_capture.main() == 0
    assert seen_numbers == [598, 592, 587]  # newest-updated first, among what was pending


# --- the read-only contract, made visible ---------------------------------------


def test_the_script_names_no_mutating_http_verb(pr_state_capture):
    src = Path(pr_state_capture.__file__).read_text()
    for verb in ("PATCH", "PUT", "DELETE"):
        assert f'"{verb}"' not in src and f"'{verb}'" not in src, verb
    assert 'GA.request("POST"' not in src and "GA.request('POST'" not in src


def test_the_installation_token_is_minted_read_only(pr_state_capture, monkeypatch):
    """Even though the App holds `pull_requests: write`, the token THIS script
    mints is scoped to read alone, so a bug here would be refused by GitHub."""
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    calls = []

    def fake_installation_token(jwt, inst_id, **kw):
        calls.append(kw.get("permissions"))
        return "ghs_readonly"

    monkeypatch.setattr(pr_state_capture.GA, "installation_token", fake_installation_token)
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/agent-platform"])
    monkeypatch.setattr(pr_state_capture, "list_pulls", lambda repo, token, **k: [])
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])
    rc = pr_state_capture.main()
    assert rc == 0
    assert calls == [{"pull_requests": "read", "metadata": "read"}]


# --- main(): the full pass, wired end to end ------------------------------------


def test_main_captures_every_repo_and_reports_delivery_failures(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/agent-platform", "convivy/convivy-deck"])

    pulls_by_repo = {"convivy/agent-platform": _pulls(), "convivy/convivy-deck": []}
    monkeypatch.setattr(pr_state_capture, "list_pulls",
                        lambda repo, token, **k: pulls_by_repo[repo])
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])

    rc = pr_state_capture.main()
    assert rc == 0
    assert len(store.rows) == 4  # every PR in the one non-empty repo, one row each


def test_main_refuses_cleanly_when_the_key_is_missing(pr_state_capture, monkeypatch, capsys):
    monkeypatch.setattr(pr_state_capture.GA, "key_problem",
                        lambda *a, **k: "no App key at /nope")
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])
    rc = pr_state_capture.main()
    assert rc == 1
    assert "no App key" in capsys.readouterr().err


def test_main_reports_a_repo_it_could_not_read_but_continues(pr_state_capture, monkeypatch, capsys):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/broken-repo", "convivy/agent-platform"])

    def fake_list_pulls(repo, token, **k):
        if repo == "convivy/broken-repo":
            raise pr_state_capture.GA.Unreadable(f"{repo}: pull requests could not be read (403)")
        return _pulls()

    monkeypatch.setattr(pr_state_capture, "list_pulls", fake_list_pulls)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])

    rc = pr_state_capture.main()
    assert rc == 1
    assert "convivy/broken-repo" in capsys.readouterr().err
    assert len(store.rows) == 4  # the readable repo still got captured


# --- installed_repos() and absent repos: a repo with no GitHub copy is skipped ---


def _wire_main(pr_state_capture, monkeypatch, *, selection, repos, present=None,
               pulls_by_repo=None):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565,
                                                 "repository_selection": selection})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture", lambda: repos)
    listed = []

    def fake_installed_repos(token, **k):
        listed.append(token)
        if isinstance(present, Exception):
            raise present
        return present

    monkeypatch.setattr(pr_state_capture, "installed_repos", fake_installed_repos)
    read = []

    def fake_list_pulls(repo, token, **k):
        read.append(repo)
        if repo not in (pulls_by_repo or {}):
            raise pr_state_capture.GA.Unreadable(f"{repo}: pull requests could not be read (404)")
        return pulls_by_repo[repo]

    monkeypatch.setattr(pr_state_capture, "list_pulls", fake_list_pulls)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])
    return store, listed, read


def test_installed_repos_paginates_and_lowercases(pr_state_capture):
    page1 = {"repositories": [{"full_name": f"convivy/Repo-{i}"} for i in range(100)]}
    page2 = {"repositories": [{"full_name": "convivy/Agent-Platform"}]}
    pages = {1: page1, 2: page2}
    calls = []

    def opener(req, timeout=None):
        assert "/installation/repositories" in req.full_url
        page = int(req.full_url.split("&page=")[1])
        calls.append(page)
        return _Resp(200, pages[page])

    out = pr_state_capture.installed_repos("ghs_x", opener=opener)
    assert calls == [1, 2]
    assert len(out) == 101
    assert "convivy/agent-platform" in out and "convivy/repo-0" in out


def test_installed_repos_a_non_200_is_unreadable(pr_state_capture):
    def opener(req, timeout=None):
        return _Resp(403, {"message": "nope"})

    with pytest.raises(pr_state_capture.GA.Unreadable):
        pr_state_capture.installed_repos("ghs_x", opener=opener)


def test_main_skips_a_repo_not_on_github_when_the_installation_covers_all(
        pr_state_capture, monkeypatch, capsys):
    store, listed, read = _wire_main(
        pr_state_capture, monkeypatch, selection="all",
        repos=["convivy/agent-platform", "convivy/vivy-commercial"],
        present={"convivy/agent-platform"},
        pulls_by_repo={"convivy/agent-platform": _pulls()})

    rc = pr_state_capture.main()
    assert rc == 0
    assert listed == ["ghs_x"]
    assert read == ["convivy/agent-platform"]  # the absent repo is never requested
    assert "convivy/vivy-commercial: not on GitHub" in capsys.readouterr().err
    assert len(store.rows) == 4


def test_main_under_a_selected_installation_still_fails_on_an_unlisted_repo(
        pr_state_capture, monkeypatch, capsys):
    """Absence from a "selected" installation can mean the App was never granted
    the repo, so it is read and its 404 fails the pass exactly as before."""
    store, listed, read = _wire_main(
        pr_state_capture, monkeypatch, selection="selected",
        repos=["convivy/agent-platform", "convivy/not-granted"],
        present={"convivy/agent-platform"},
        pulls_by_repo={"convivy/agent-platform": _pulls()})

    rc = pr_state_capture.main()
    assert rc == 1
    assert listed == []  # the list is not consulted at all
    assert read == ["convivy/agent-platform", "convivy/not-granted"]
    assert "convivy/not-granted: pull requests could not be read (404)" in capsys.readouterr().err


def test_main_under_all_still_fails_on_a_404_for_a_listed_repo(pr_state_capture, monkeypatch):
    store, listed, read = _wire_main(
        pr_state_capture, monkeypatch, selection="all",
        repos=["convivy/agent-platform"],
        present={"convivy/agent-platform"},
        pulls_by_repo={})

    assert pr_state_capture.main() == 1
    assert read == ["convivy/agent-platform"]


def test_main_reads_every_repo_and_fails_when_the_repository_list_is_unreadable(
        pr_state_capture, monkeypatch, capsys):
    store, listed, read = _wire_main(
        pr_state_capture, monkeypatch, selection="all",
        repos=["convivy/agent-platform", "convivy/vivy-commercial"],
        present=pr_state_capture.GA.Unreadable("the installation's repositories could not be read (502)"),
        pulls_by_repo={"convivy/agent-platform": _pulls()})

    rc = pr_state_capture.main()
    assert rc == 1
    assert read == ["convivy/agent-platform", "convivy/vivy-commercial"]
    err = capsys.readouterr().err
    assert "could not be read (502); reading every registered repo instead" in err
    assert len(store.rows) == 4


def test_installed_repos_refuses_a_listing_past_the_page_cap(pr_state_capture):
    """A truncated listing must never be returned: a repo on an unread page would
    read as absent and be skipped, which is the silent loss this list exists to
    rule out. Unlike list_pulls, which degrades to a partial read, this raises."""
    full = {"repositories": [{"full_name": f"convivy/r{i}"} for i in range(100)]}
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        return _Resp(200, full)

    with pytest.raises(pr_state_capture.GA.Unreadable, match="refusing to treat"):
        pr_state_capture.installed_repos("ghs_x", opener=opener)
    assert len(calls) == pr_state_capture._MAX_PAGES


def test_a_row_is_stamped_produced_at_with_the_prs_updated_at(pr_state_capture, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    pulls = _pulls()
    for row in pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls):
        pr_state_capture.emit(row)
    stamped = {r["meta"]["number"]: r["produced_at"] for r in store.rows.values()}
    assert stamped == {p["number"]: p["updated_at"] for p in pulls}


def test_updated_at_moving_alone_is_not_a_new_row(pr_state_capture, monkeypatch):
    """A comment moves updated_at without changing anything the panel draws, so it
    must not reach the dedup key; the first row keeps its original stamp."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    pulls = _pulls()
    for row in pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls):
        pr_state_capture.emit(row)
    first = {k: v["produced_at"] for k, v in store.rows.items()}
    for p in pulls:
        p["updated_at"] = "2026-09-16T23:59:59Z"
    for row in pr_state_capture.rows_from_pulls("convivy/agent-platform", pulls):
        assert pr_state_capture.emit(row) == "duplicate"
    assert {k: v["produced_at"] for k, v in store.rows.items()} == first


def test_a_row_is_scoped_to_its_repos_registry_project(pr_state_capture, monkeypatch):
    """The product the Library gives the same repo's review records, so state and
    reviews join on product. Under the Slack-channel mapping both of these were
    `platform`."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    for repo in ("convivy/convivy-deck", "convivy/knowledge", "convivy/agent-platform"):
        for row in pr_state_capture.rows_from_pulls(repo, _pulls()[:1]):
            pr_state_capture.emit(row)
    by_repo = {r["meta"]["repo"]: r["product"] for r in store.rows.values()}
    assert by_repo == {"convivy/convivy-deck": "convivy-deck",
                       "convivy/knowledge": "library",
                       "convivy/agent-platform": "bosun"}


# --- send-state / changed-rows-only (issue_01M3WQQ1STSGNZ6SKSMFGSBS4X) ---------
#
# `load_delivered`/`save_delivered`/`_changed_rows` are what stop a pass from
# re-POSTing every PR it reads: a row is emitted only when its identity
# (head_sha, state, draft, title_hash — `_delivered_identity`) differs from
# what `STATE_DIR` last recorded as delivered for that PR. `store.calls`
# (not `store.rows`) is the signal here, since an unattempted row and a
# deduped one both leave `store.rows` unchanged.


def _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls, *,
                           repo="convivy/agent-platform"):
    monkeypatch.setattr(pr_state_capture, "SC", store)
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        lambda jwt, owner, **k: {"id": 155330565})
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture", lambda: [repo])
    monkeypatch.setattr(pr_state_capture, "list_pulls", lambda r, token, **k: pulls)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])


def test_an_unchanged_pr_is_not_emitted_on_the_second_pass(pr_state_capture, monkeypatch):
    store = _FakeStore()
    pulls = _pulls()
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)

    assert pr_state_capture.main() == 0
    assert len(store.calls) == 4  # first pass: nothing delivered yet, everything is new

    after_first_pass = len(store.calls)
    assert pr_state_capture.main() == 0
    assert len(store.calls) == after_first_pass  # nothing changed: no second attempt at all


def test_a_head_sha_change_is_emitted_again_on_the_next_pass(pr_state_capture, monkeypatch):
    store = _FakeStore()
    pulls = _pulls()
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)
    assert pr_state_capture.main() == 0
    baseline = len(store.calls)

    for p in pulls:
        if p["number"] == 601:
            p["head"]["sha"] = "5" * 40
    assert pr_state_capture.main() == 0
    assert len(store.calls) == baseline + 1
    assert store.calls[-1].split(":")[2] == "5" * 40  # the moved PR, and only it, resent


def test_a_state_change_is_emitted_again_on_the_next_pass(pr_state_capture, monkeypatch):
    store = _FakeStore()
    pulls = _pulls()
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)
    assert pr_state_capture.main() == 0
    baseline = len(store.calls)

    for p in pulls:
        if p["number"] == 601:
            p["state"] = "closed"
    assert pr_state_capture.main() == 0
    assert len(store.calls) == baseline + 1


def test_a_title_change_is_emitted_again_on_the_next_pass(pr_state_capture, monkeypatch):
    store = _FakeStore()
    pulls = _pulls()
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)
    assert pr_state_capture.main() == 0
    baseline = len(store.calls)

    for p in pulls:
        if p["number"] == 601:
            p["title"] = "a corrected title"
    assert pr_state_capture.main() == 0
    assert len(store.calls) == baseline + 1


def test_a_row_whose_emit_fails_is_retried_next_pass_and_its_record_not_updated(
        pr_state_capture, monkeypatch):
    """PR 601 always fails to deliver; its neighbours succeed. The second
    pass must attempt 601 again (its record was never written) while
    leaving the successfully-delivered neighbours alone."""
    store = _FakeStore(fail_numbers={601})
    pulls = _pulls()
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)

    assert pr_state_capture.main() == 1  # 601's error:boom fails the pass
    first_pass = list(store.calls)
    assert len(first_pass) == 4

    assert pr_state_capture.main() == 1  # still fails: 601 is retried, same error
    second_pass = store.calls[len(first_pass):]
    assert len(second_pass) == 1  # only the still-undelivered PR is attempted again
    assert "601" in second_pass[0] or ":601:" in second_pass[0]


def test_a_missing_record_emits_everything_without_crashing(pr_state_capture, capsys):
    delivered = pr_state_capture.load_delivered("convivy/agent-platform")
    assert delivered == {}
    assert "no delivered-state record yet" in capsys.readouterr().err


def test_a_corrupt_record_emits_everything_without_crashing(pr_state_capture, capsys):
    pr_state_capture.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = pr_state_capture._state_path("convivy/agent-platform")
    path.write_text("{not json")

    delivered = pr_state_capture.load_delivered("convivy/agent-platform")
    assert delivered == {}
    assert "is corrupt" in capsys.readouterr().err


def test_the_record_s_directory_and_file_modes_are_0700_and_0600(pr_state_capture):
    pr_state_capture.save_delivered("convivy/agent-platform", {"601": {"state": "OPEN"}})
    dir_mode = pr_state_capture.STATE_DIR.stat().st_mode & 0o777
    file_mode = pr_state_capture._state_path("convivy/agent-platform").stat().st_mode & 0o777
    assert dir_mode == 0o700
    assert file_mode == 0o600


def test_save_delivered_is_called_once_per_repo_not_once_per_pr(pr_state_capture, monkeypatch):
    store = _FakeStore()
    pulls = _pulls()  # 4 PRs, one repo
    _wire_single_repo_main(pr_state_capture, monkeypatch, store, pulls)

    writes = []
    real_save = pr_state_capture.save_delivered

    def counting_save(repo, delivered):
        writes.append(repo)
        real_save(repo, delivered)

    monkeypatch.setattr(pr_state_capture, "save_delivered", counting_save)
    assert pr_state_capture.main() == 0
    assert writes == ["convivy/agent-platform"]  # one write, not four


def test_main_resolves_the_convivy_bosun_apps_own_installation_with_two_installed(
        pr_state_capture, monkeypatch):
    """The App's installation listing carries TWO installations (`convivy`
    and `convivybuilder`, once installed for homebrew-tap, PR #884) —
    `GA.single_installation`'s "exactly one installation total" count would
    refuse this with "count: the App has 2 installations, not exactly one"
    and `main()` would return 1. `GA.installation_for_owner` asks GitHub for
    `convivy`'s installation directly and never lists installations at all,
    so `main()` still reaches 0. `GA.request` is the real function; only the
    wire underneath it is faked, so this exercises the actual per-owner
    HTTP lookup, not a stand-in for it."""
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")
    monkeypatch.setattr(pr_state_capture.GA, "installation_token", lambda *a, **k: "ghs_x")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture",
                        lambda: ["convivy/agent-platform"])
    seen = []

    def fake_request(method, url, bearer, body, opener=None):
        seen.append(url)
        if url.endswith("/app/installations?per_page=100"):
            return 200, [{"id": 1, "account": {"login": "convivy"}},
                        {"id": 2, "account": {"login": "convivybuilder"}}]
        if url.endswith("/orgs/convivy/installation"):
            return 200, {"id": 1, "account": {"login": "convivy"}}
        if "/repos/convivy/agent-platform/pulls" in url:
            return 200, []
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(pr_state_capture.GA, "request", fake_request)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])

    rc = pr_state_capture.main()

    assert rc == 0, "resolving convivy's own installation must succeed with two installed"
    assert any(u.endswith("/orgs/convivy/installation") for u in seen)
    assert not any("/app/installations" in u for u in seen), (
        "installation_for_owner must never list installations to find its own")


# --- main(): each owner's repos read under its own installation and token -----
# (CQ-479, issue_01M4VM4H1KYNA5BKCRM6H0VY7B) — homebrew-tap is registered
# under the convivybuilder GitHub owner, a separate convivy-bosun App
# installation from convivy's, so a pass covering both owners must resolve
# and mint a token for each rather than reusing one for every repo.


def _wire_multi_owner_main(pr_state_capture, monkeypatch, *, repos, installations,
                           pulls_by_repo):
    """Wiring for a pass spanning more than one GitHub owner. `installations`
    maps owner -> the installation dict `GA.installation_for_owner` would
    return for it, OR an `Exception` instance for a test that wants that
    owner's lookup to raise it (a real failure, e.g. a non-404). An owner
    missing from the map raises `GA.NotInstalled` naming that owner, as
    GitHub's own double-404 does for an owner the App is not installed on
    (operator's decision, 2026-10-15 — see the script's module docstring).
    Each owner's installation id gets its OWN minted token
    (`f"token-for-{inst_id}"`), so a test can tell which owner's token a
    given `list_pulls` call actually carried. `pulls_by_repo` may likewise
    map a repo to an `Exception` instance for a test that wants that repo's
    `list_pulls` call to fail."""
    store = _FakeStore()
    monkeypatch.setattr(pr_state_capture, "SC", store)
    monkeypatch.setattr(pr_state_capture.GA, "key_problem", lambda *a, **k: None)
    monkeypatch.setattr(pr_state_capture.GA, "app_jwt", lambda *a, **k: "jwt.x.y")

    def fake_installation_for_owner(jwt, owner, **k):
        if owner not in installations:
            raise pr_state_capture.GA.NotInstalled(
                f"the convivy-bosun App is not installed on {owner!r}")
        val = installations[owner]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(pr_state_capture.GA, "installation_for_owner",
                        fake_installation_for_owner)
    monkeypatch.setattr(pr_state_capture.GA, "installation_token",
                        lambda jwt, inst_id, **k: f"token-for-{inst_id}")
    monkeypatch.setattr(pr_state_capture, "repos_to_capture", lambda: repos)

    read_with_token: dict[str, str] = {}

    def fake_list_pulls(repo, token, **k):
        read_with_token[repo] = token
        val = pulls_by_repo[repo]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(pr_state_capture, "list_pulls", fake_list_pulls)
    monkeypatch.setattr(pr_state_capture.sys, "argv", ["pr-state-capture"])
    return store, read_with_token


def test_main_reads_each_owner_s_repos_under_its_own_installation_and_token(
        pr_state_capture, monkeypatch):
    """convivy/agent-platform and convivybuilder/homebrew-tap in one pass:
    each is read with ITS OWN owner's installation token (111's for the
    first, 222's for the second — two different tokens), and both repos'
    rows are emitted. Red against origin/main on the token-inequality
    assertion below: the old `main()` resolves only the hardcoded
    `OWNER = "convivy"` installation once and hands that SAME token to
    every repo `repos_to_capture()` returns, so the two tokens recorded
    here would come back equal rather than per-owner."""
    store, read_with_token = _wire_multi_owner_main(
        pr_state_capture, monkeypatch,
        repos=["convivy/agent-platform", "convivybuilder/homebrew-tap"],
        installations={"convivy": {"id": 111}, "convivybuilder": {"id": 222}},
        pulls_by_repo={"convivy/agent-platform": _pulls(),
                       "convivybuilder/homebrew-tap": _pulls()[:1]})

    rc = pr_state_capture.main()

    assert rc == 0
    assert read_with_token["convivy/agent-platform"] == "token-for-111"
    assert read_with_token["convivybuilder/homebrew-tap"] == "token-for-222"
    assert read_with_token["convivy/agent-platform"] != read_with_token["convivybuilder/homebrew-tap"]
    emitted_repos = {r["meta"]["repo"] for r in store.rows.values()}
    assert emitted_repos == {"convivy/agent-platform", "convivybuilder/homebrew-tap"}


def test_main_skips_an_owner_with_no_installation_but_still_reads_every_other_owner(
        pr_state_capture, monkeypatch, capsys):
    """convivybuilder has no convivy-bosun App installation (the App is
    about to be installed there, but is not yet): convivybuilder/homebrew-tap
    is never read, stderr names convivybuilder and its one skipped repo, and
    the pass exits 0 — but convivy/agent-platform is still read and its PRs
    still emitted in the same pass (operator's decision, 2026-10-15: an
    uninstalled owner is skipped, not failed). Red against origin/main on
    the exit-code assertion below: before this change `GA.installation_for_owner`
    raised a plain `GA.Unreadable` for a missing installation, which `main()`
    caught in the branch that sets `exit_code = 1`, so this same scenario
    returned 1."""
    store, read_with_token = _wire_multi_owner_main(
        pr_state_capture, monkeypatch,
        repos=["convivy/agent-platform", "convivybuilder/homebrew-tap"],
        installations={"convivy": {"id": 111}},  # no entry for convivybuilder: NotInstalled
        pulls_by_repo={"convivy/agent-platform": _pulls(),
                       "convivybuilder/homebrew-tap": _pulls()[:1]})

    rc = pr_state_capture.main()

    err = capsys.readouterr().err
    assert rc == 0
    assert "convivy/agent-platform" in read_with_token
    assert "convivybuilder/homebrew-tap" not in read_with_token
    emitted_repos = {r["meta"]["repo"] for r in store.rows.values()}
    assert emitted_repos == {"convivy/agent-platform"}
    assert ("convivybuilder: the convivy-bosun App is not installed on this "
            "owner; its 1 registered repos are skipped") in err


def test_main_still_fails_when_a_skipped_owner_is_accompanied_by_a_real_failure(
        pr_state_capture, monkeypatch, capsys):
    """The guard the operator asked for explicitly: skipping an uninstalled
    owner must never mask an unrelated real failure elsewhere in the same
    pass. convivybuilder has no installation (skipped, no exit_code) while
    convivy/agent-platform's own `list_pulls` fails with a non-200 — the
    pass still exits 1."""
    store, read_with_token = _wire_multi_owner_main(
        pr_state_capture, monkeypatch,
        repos=["convivy/agent-platform", "convivybuilder/homebrew-tap"],
        installations={"convivy": {"id": 111}},  # no entry for convivybuilder: NotInstalled
        pulls_by_repo={
            "convivy/agent-platform": pr_state_capture.GA.Unreadable(
                "convivy/agent-platform: pull requests could not be read (500)"),
            "convivybuilder/homebrew-tap": _pulls()[:1],
        })

    rc = pr_state_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "convivybuilder" in err  # still named and still skipped, not failed
    assert "convivy/agent-platform" in read_with_token  # attempted, then failed
    assert "convivybuilder/homebrew-tap" not in read_with_token  # never read: no installation
    assert len(store.rows) == 0


def test_main_still_fails_when_a_skipped_owner_is_accompanied_by_a_covered_owners_installation_failure(
        pr_state_capture, monkeypatch, capsys):
    """Same guard, the other failure site: convivy's OWN installation lookup
    fails with a non-404 (a real read failure, not a confirmed absence) —
    still exit 1, even though convivybuilder is correctly only skipped."""
    store, read_with_token = _wire_multi_owner_main(
        pr_state_capture, monkeypatch,
        repos=["convivy/agent-platform", "convivybuilder/homebrew-tap"],
        installations={
            "convivy": pr_state_capture.GA.Unreadable(
                "the convivy-bosun App's installation on 'convivy' could not be read (500)"),
            # no entry for convivybuilder: NotInstalled
        },
        pulls_by_repo={"convivybuilder/homebrew-tap": _pulls()[:1]})

    rc = pr_state_capture.main()

    err = capsys.readouterr().err
    assert rc == 1
    assert "convivybuilder" in err
    assert "convivy/agent-platform" not in read_with_token
    assert "convivybuilder/homebrew-tap" not in read_with_token  # also skipped: no installation
    assert len(store.rows) == 0


def test_plist_start_interval_is_180_seconds():
    """GitHub-derived state must land within 3 minutes, the pace the crews
    work at (the operator, 2026-10-15) — StartInterval is seconds, not
    minutes, so this is 180, not 3."""
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["StartInterval"] == 180
