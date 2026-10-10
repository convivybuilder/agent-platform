"""config/projects.seed.json: the registry an adopter of the exported engine
starts with, in place of this machine's own config/projects.json (platform/
decisions/0043 — nothing about a black-box crew may leave this machine).

The exporting tool's installer copies this file to config/projects.json on
the adopter's machine; that installer owns the wiring. This module only
checks that the seed itself loads through projects.py, passes the same
schema checks the real registry does, and carries nothing identifying from
the real registry.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import projects

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "config" / "projects.seed.json"
REAL = REPO / "config" / "projects.json"
SELF = Path(__file__).resolve()


@pytest.fixture
def seed_registry(monkeypatch):
    """Point the loader at the seed file, the way an adopter's PROJECTS_JSON
    override would, and restore it afterward so later tests see the real
    registry again."""
    monkeypatch.setattr(projects, "PROJECTS_JSON", SEED)
    projects._load_raw.cache_clear()
    yield
    projects._load_raw.cache_clear()


def test_seed_is_valid_json():
    with open(SEED, encoding="utf-8") as f:
        json.load(f)


def test_seed_schema_matches_the_real_registrys_checks(seed_registry):
    """The same per-unit assertions test_projects.py's test_schema_wellformed
    applies to config/projects.json, run against the seed's own units."""
    for slug, u in projects.load().items():
        for field in projects.REQUIRED_FIELDS:
            assert field in u, f"{slug} missing required field {field!r}"
        assert u["class"] in projects.CLASSES, f"{slug} has bad class {u['class']!r}"
        assert isinstance(u["display"], str) and u["display"]
        assert isinstance(u["repos"], list) and u["repos"]
        assert isinstance(u.get("aliases", []), list)
        assert u["kb"] in projects.KB_VALUES, f"{slug} has bad kb value {u['kb']!r}"
        if u["kb"] in ("central", "split"):
            assert u.get("kb_project"), f"{slug} kb={u['kb']} needs kb_project"
        assert isinstance(u["home_dir"], str) and u["home_dir"], f"{slug} needs a home_dir"
        assert isinstance(u["dirs"], list), f"{slug} dirs must be a list"
        assert u["lifecycle"] in projects.LIFECYCLES, (
            f"{slug} has bad lifecycle {u['lifecycle']!r}")
        assert isinstance(u["products"], list), f"{slug} products must be a list"
        assert u["crew"] is None or isinstance(u["crew"], str), (
            f"{slug} crew must be a slug or null")


def test_seed_has_exactly_one_project_named_library(seed_registry):
    assert set(projects.load()) == {"library"}
    u = projects.get("library")
    assert u["repos"] == ["knowledge"]
    assert u["crew"] == "library"
    assert u["kb"] == "central"
    assert u["kb_project"] == "library"
    assert u["lifecycle"] == "active"
    assert u["products"] == []


def test_seed_has_no_products_block_entries(seed_registry):
    assert projects.products() == {}


def test_seed_home_dir_is_the_root_relative_form(seed_registry):
    u = projects.get("library")
    assert u["home_dir"].startswith("<root>/")
    # registry_home() must accept the stored form without raising.
    projects.registry_home(u["home_dir"])


def test_seed_resolves_knowledge_to_library(seed_registry):
    """The same answer the real registry gives: project_for_repo("knowledge")
    is what the Library's POST /review-records resolves the adopter's
    knowledge repo through."""
    assert projects.project_for_repo("knowledge") == "library"


def test_seed_scopes_have_the_same_keys_as_the_real_registry():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    real_scopes = {k for k in real.get("_scopes", {}) if not k.startswith("_")}
    seed_scopes = {k for k in seed.get("_scopes", {}) if not k.startswith("_")}
    assert seed_scopes == real_scopes


# --- the seed must name nothing from the real registry ---------------------
#
# Derived from config/projects.json at test time, not hard-coded, so a new
# project registered there is automatically covered here too.


def _owner_tokens(real: dict) -> set[str]:
    """Every `owner` slug the real registry carries, minus the seed's own
    generic `"tbd"`: every top-level project row's own `owner`, plus every
    `_products` entry's `owner` — a product listed only under `_products`
    (not also as a top-level project row) would otherwise leave its owner
    uncollected here even though that owner slug still names a real company
    and still must not leak into the seed."""
    owners = {
        u.get("owner")
        for slug, u in real.items()
        if not slug.startswith("_") and isinstance(u, dict)
    }
    owners |= {
        p.get("owner")
        for p in real.get("_products", {}).values()
        if isinstance(p, dict)
    }
    owners.discard("tbd")
    owners.discard(None)
    return owners


def test_owner_tokens_includes_an_owner_named_only_under_products():
    """Round-6 security review, non-blocking: `_owner_tokens` must collect an
    owner slug that appears only on a `_products` entry and on no top-level
    project row. Built as a synthetic registry shape rather than relying on
    config/projects.json to currently have such a case — today every owner
    slug used under `_products` also appears on a top-level row, which is
    why this gap was invisible to a test run against the real registry."""
    fake_real = {
        "library": {"owner": "tbd"},
        "_products": {"widget": {"display": "Widget", "owner": "products-only-owner"}},
    }
    assert "products-only-owner" in _owner_tokens(fake_real)


def _forbidden_tokens(real: dict) -> set[str]:
    """Every slug, display, crew, repo, alias, home_dir, dirs and kb_project
    string, plus every kb_scope_exceptions key and value, the real registry's
    PROJECT rows (and _products entries) carry, minus the strings the seed's
    own `library` row is entitled to reuse (its identity is drawn from the
    real registry's own `library` row) and the `_scopes` keys (the seed is
    required to carry the same scope vocabulary)."""
    forbidden: set[str] = set()
    for slug, u in real.items():
        if slug.startswith("_") or not isinstance(u, dict):
            continue
        forbidden.add(slug)
        for field in ("display", "crew", "home_dir", "kb_project"):
            v = u.get(field)
            if isinstance(v, str) and v:
                forbidden.add(v)
        for field in ("repos", "aliases", "dirs"):
            for v in u.get(field, []) or []:
                if isinstance(v, str) and v:
                    forbidden.add(v)
        for exc_slug, exc in (u.get("kb_scope_exceptions") or {}).items():
            if isinstance(exc_slug, str) and exc_slug:
                forbidden.add(exc_slug)
            if isinstance(exc, dict):
                for v in exc.values():
                    if isinstance(v, str) and v:
                        forbidden.add(v)
    for slug, p in real.get("_products", {}).items():
        forbidden.add(slug)
        display = p.get("display") if isinstance(p, dict) else None
        if isinstance(display, str) and display:
            forbidden.add(display)

    library = real.get("library", {})
    allowed = {library.get(f) for f in ("display", "crew", "home_dir") if library.get(f)}
    allowed.add("library")
    allowed.update(r for r in library.get("repos", []) if isinstance(r, str))
    allowed.update(d for d in library.get("dirs", []) if isinstance(d, str))
    allowed.update({k for k in real.get("_scopes", {}) if not k.startswith("_")})

    return forbidden - allowed


def test_seed_names_nothing_from_the_real_registry():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    seed_text = SEED.read_text(encoding="utf-8")
    leaked = _leaked_tokens(seed_text, _forbidden_tokens(real))
    assert not leaked, (
        f"config/projects.seed.json carries real-registry identifiers: {sorted(leaked)}")


def test_the_leak_check_catches_a_planted_real_crew_name():
    """Demonstrates the check above actually fires: a real crew name planted
    into the seed's text is caught, which is what makes a clean run above
    mean something rather than a check that never triggers. The planted slug
    is read out of the real registry at test time (preferring a black-box
    crew, platform/decisions/0043, since that is the case the check most
    needs to catch) rather than written into this file as a literal."""
    real = json.loads(REAL.read_text(encoding="utf-8"))
    rows = {k: v for k, v in real.items() if not k.startswith("_") and isinstance(v, dict)}
    planted = next((slug for slug, u in rows.items() if u.get("private")), None)
    if planted is None:
        planted = next(iter(rows))
    planted_text = SEED.read_text(encoding="utf-8") + f'\n"{planted}"\n'
    leaked = _leaked_tokens(planted_text, _forbidden_tokens(real))
    assert planted in leaked


# --- the one tokenizer every check in this module routes through -----------
#
# Round 4 gave the pinned-hash check (below) a camelCase-aware splitter but
# left the structured registry check (`_forbidden_tokens` + the old
# `_token_pattern`) on a plain word-boundary regex, so a short slug fused with
# no separator into a surrounding camelCase identifier (round-5 security
# review, PR comment 5940145642) passed it even though the same round's
# splitter would have caught the identical fusion into a pinned word.
# `_candidates` replaces both: every check below — the registry tokens, the
# owner tokens, and the pinned hashes, on both the seed and this file's own
# source — compares against the same generated set. (Deliberately not
# reproducing the review's literal example string here: it fuses a real
# black-box crew slug, and this file is itself scanned by the checks below.)
#
# A person's name or a legal-entity name that appears only in prose `note`
# text (config/projects.json's `_scopes.company.note` and `_scopes.keel.note`,
# and several rows' own `note`) has no structured field to derive a check
# from, and writing either name here as a literal would itself be the leak
# this module exists to prevent, since this test file ships to every adopter
# the same way config/projects.seed.json does. Pinning the SHA-256 hex digest
# of each lowercased name, instead of the name itself, gives the same
# regression coverage without the literal.

_MAX_CANDIDATE_LEN = 48
"""The longest structured identifier field in config/projects.json today
(a home_dir path) is 38 characters; this leaves comfortable margin above
that. A `kb_scope_exceptions` reason is prose, not an identifier, and runs
to several hundred characters — those are matched by plain substring
containment in `_leaked_tokens` instead of by `_candidates`: fusing a whole
sentence into camelCase or an acronym by accident isn't a credible way it
ends up inside the seed, and letting the window reach that far would make
`_candidates` too slow to run against this file's own ~18KB source."""

_SEP_RE = re.compile(r"[^a-z0-9]+")


def _normalize(token: str) -> str:
    """Lowercase `token` and drop every non-alphanumeric character, so a
    forbidden token can be compared against a candidate the same way
    regardless of which one carries the hyphen, dot or space."""
    return _SEP_RE.sub("", token.lower())


def _candidate_boundaries(text: str) -> list[int]:
    """Boundary gap positions in `text`: position `i` is a boundary when a
    cut between `text[i-1]` and `text[i]` could start or end a token. A
    boundary falls at the start and end of the string, on both sides of
    every separator (any non-alphanumeric character — this is what excises
    a dotted acronym's dots and a hyphenated identifier's hyphen), at every
    lower-to-upper case transition (`fooBar` -> a boundary before the second
    `B`-led hump), at every upper-to-lower transition (an all-caps acronym
    directly followed by a lowercase word, with no capital letter marking
    the lowercase word's own start, gets a boundary right after the
    acronym's last letter — this is what ends the acronym run correctly
    where round 4's splitter landed the cut one character early), at the
    standard acronym/word split (an all-caps run directly followed by a
    Capitalized word, `ACMEBridge` or `WXYZFiling`, gets a boundary one
    letter before the Capitalized word's own start: when `text[i]` and
    `text[i+1]` are both uppercase and `text[i+2]` is lowercase, the
    boundary falls at `i+1` — `ACME|Bridge`, `WXYZ|Filing` — because the
    lowercase hump starting at `i+2` claims the uppercase letter immediately
    before it as its own initial; round 5's upper-to-lower rule alone misses
    this since it only fires on an *adjacent* upper/lower pair, which in
    `ACMEBridge` sits at `B`->`r`, one letter too late to separate `ACME`
    from `Bridge` — the same gap a real acronym-then-Capitalized-word fusion
    has, round-6 code review, deliberately not reproduced here with a real
    identifier for the reason given in the module-header comment above), and
    at every letter/digit transition.

    The acronym/word split never fires inside a run that stays uppercase
    through its end (every triple it inspects needs a lowercase third
    letter), so it keeps the `REAL` false positive the plain "before every
    uppercase letter" rule below would have reintroduced.

    A plain "before every uppercase letter" rule is deliberately not a
    separate case here: inside a run that is uppercase throughout (a Python
    constant like this module's own `REAL`), that rule would put a boundary
    before *every* letter, making any substring of the run — including an
    unrelated short token that merely happens to be a letter-run inside it
    (`REAL` contains the two-letter run that also spells a real crew slug)
    — extractable as a false positive. The transition rules above already
    place a boundary at the start and end of every genuine acronym or
    capitalized hump without that side effect, so dropping the broader rule
    loses no real coverage. The same reasoning is why no rule below ever
    splits *inside* a run that is one case throughout with no digit or
    separator in it: a token fused that way, upper or lower, carries no
    signal for this generator to cut on (`_candidates`' own docstring states
    this limit and why it is acceptable for this guard)."""
    n = len(text)
    bounds = {0, n}
    for i, c in enumerate(text):
        if not c.isalnum():
            bounds.add(i)
            bounds.add(i + 1)
    for i in range(n - 1):
        a, b = text[i], text[i + 1]
        if (a.islower() and b.isupper()) or (a.isupper() and b.islower()):
            bounds.add(i + 1)
        if (a.isalpha() and b.isdigit()) or (a.isdigit() and b.isalpha()):
            bounds.add(i + 1)
    for i in range(n - 2):
        if text[i].isupper() and text[i + 1].isupper() and text[i + 2].islower():
            bounds.add(i + 1)
    return sorted(bounds)


def _candidates(text: str, max_len: int = _MAX_CANDIDATE_LEN) -> set[str]:
    """Every lowercased, separator-stripped candidate `text` contains between
    two boundary positions, with the raw (pre-strip) span bounded to
    `max_len` characters for performance. Because a separator is excised
    rather than treated as a dead end, a dotted or hyphenated spelling with
    no other signal (each letter of a word written as its own dot- or
    hyphen-separated fragment) falls out of the same boundary pairs as a
    plain word or a camelCase fusion — the span from the boundary before the
    first letter to the boundary after the last one already covers every
    separator in between, with no separate "join the fragments" step needed.

    What this generator cannot catch: leetspeak or homoglyph substitution (a
    digit standing in for a look-alike letter, or a full-width Unicode letter
    in place of its ASCII twin — `_normalize`'s `[^a-z0-9]` strips the
    homoglyph as if it were a separator, so the two sides of it never rejoin
    into the original word), a name split by an inserted letter, or — unlike
    round 4's brute-force substring sweep, which this replaces — a short
    token fused into a run of one case throughout, upper or lower, with no
    case, digit or separator signal anywhere near the join (an all-lowercase
    run swallows a fused lowercase token exactly as an all-uppercase run
    swallows a fused uppercase one — `CONTAINSZORLIXDATA` is as invisible to
    this generator as `containszorlixdata`). Each of those requires a deliberate,
    signal-free construction that an accidental copy-paste essentially never
    produces: copying registry text into the seed by accident preserves the
    source's own casing and punctuation, and an all-caps identifier copied
    out of code keeps its underscores (`ACME_DATA` is caught, by the
    separator rule in `_candidate_boundaries`, same as any other separated
    token) — this check exists to catch that accident, not a motivated
    adversary (round 3's PR body makes the same point about the pinned
    hashes below)."""
    bounds = _candidate_boundaries(text)
    out: set[str] = set()
    n = len(bounds)
    for idx in range(n):
        i = bounds[idx]
        for j in bounds[idx + 1:]:
            if j - i > max_len:
                break
            norm = _normalize(text[i:j])
            if norm:
                out.add(norm)
    return out


def _leaked_tokens(text: str, forbidden: set[str]) -> set[str]:
    """Every token in `forbidden` that appears in `text`, fused into a
    neighboring word or not. A `forbidden` token no longer than
    `_MAX_CANDIDATE_LEN` is matched by normalized equality against
    `_candidates(text)`; a longer one (today, only a `kb_scope_exceptions`
    reason sentence) is matched by plain case-insensitive containment, per
    `_MAX_CANDIDATE_LEN`'s own docstring."""
    short = {t for t in forbidden if len(_normalize(t)) <= _MAX_CANDIDATE_LEN}
    long_tokens = forbidden - short
    candidates = _candidates(text)
    leaked = {t for t in short if _normalize(t) in candidates}
    if long_tokens:
        lowered = text.lower()
        leaked |= {t for t in long_tokens if t.lower() in lowered}
    return leaked


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Pinned as hashes rather than literals (round 3 review, PR comment 5937999208)
# for the reason given above. `test_pinned_identifier_hashes_still_match_the_
# real_registry` below ties this set to config/projects.json, so a renamed or
# rewritten source fails loudly here rather than leaving a stale, silently
# useless pin.
#
# A fourth name from the same review is deliberately not pinned: it never
# appears as an isolated word token in config/projects.json today, only fused
# into a project's own slug/display/crew/home_dir/repos string, which
# `_forbidden_tokens` above already forbids outright. Pinning it anyway would
# fail the tie-to-source test below for a real reason — no token anywhere to
# tie the pin to — rather than catch anything the structured check doesn't
# already catch; the PR body names the word and the check that confirmed this.
_PINNED_IDENTIFIER_HASHES = {
    "bfef4adc39f01b033fe749bb5f28f10b581fef319d34445d21a7bc63fe732fa3",
    "3ce0314f13387378b35de6a321a2f45fd781a0ede1307e75ba1c0c877b581369",
    "512fb3a98f2096782263721de0473e84e569e68a47f9c2f1b1bd40c2e1e44b0d",
}


def test_seed_names_nothing_matching_a_pinned_identifier_hash():
    seed_text = SEED.read_text(encoding="utf-8")
    leaked = {c for c in _candidates(seed_text) if _hash_token(c) in _PINNED_IDENTIFIER_HASHES}
    assert not leaked, (
        "config/projects.seed.json carries a word token matching a pinned "
        f"real-identifier hash: {sorted(leaked)}")


def test_pinned_identifier_hashes_still_match_the_real_registry():
    """Ties the pinned set above to config/projects.json: if the registry is
    ever edited so a pinned hash no longer matches any candidate there (a
    rename, or the carrying note text rewritten), this goes red rather than
    the pin silently protecting a name that no longer appears anywhere."""
    real_text = REAL.read_text(encoding="utf-8")
    real_hashes = {_hash_token(c) for c in _candidates(real_text)}
    stale = _PINNED_IDENTIFIER_HASHES - real_hashes
    assert not stale, (
        f"pinned hash(es) with no matching candidate in config/projects.json: {stale}")


def test_seed_does_not_name_a_real_owner(seed_registry):
    """The real registry's `owner` field names the company by its own slug on
    every row; the seed's single row must not carry it, since the seed's own
    owner is the generic `"tbd"`. Derived from config/projects.json at test
    time rather than written as a literal. The person's and legal entity's
    names are covered instead by the pinned-hash check above."""
    real = json.loads(REAL.read_text(encoding="utf-8"))
    seed_text = SEED.read_text(encoding="utf-8")
    leaked = _leaked_tokens(seed_text, _owner_tokens(real))
    assert not leaked, f"config/projects.seed.json names a real owner: {sorted(leaked)}"


# --- the test module's own source is also exported -------------------------
#
# Every check above scans config/projects.seed.json's text. None of them
# scanned *this file's* text, and this file ships to every adopter the same
# way the seed does. That gap is why two real crew identifiers, used as
# illustrative examples elsewhere in this module, sat unscanned through three
# rounds of review before being caught (round-3 review, PR comments
# 5939506691 and 5939409445) — the fix for both was simply to stop naming
# them, which is why neither name appears in this comment either.

# The library row is the one project this seed carries, and the only one
# this module's own source may legitimately name: its slug, and the one repo
# it owns. Everything else from the real registry is as forbidden here as it
# is in the seed's own text.
_SELF_SCAN_ALLOWED_TOKENS = {"library", "knowledge"}


def test_the_test_module_itself_names_nothing_from_the_real_registry():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    source = SELF.read_text(encoding="utf-8")

    forbidden = _forbidden_tokens(real) - _SELF_SCAN_ALLOWED_TOKENS
    leaked = _leaked_tokens(source, forbidden)
    assert not leaked, (
        f"tests/test_projects_seed.py names real-registry identifiers: {sorted(leaked)}")

    hash_leaked = {c for c in _candidates(source) if _hash_token(c) in _PINNED_IDENTIFIER_HASHES}
    assert not hash_leaked, (
        "tests/test_projects_seed.py carries a word token matching a pinned "
        f"real-identifier hash: {sorted(hash_leaked)}")


def test_the_module_self_scan_catches_a_planted_real_crew_name():
    """Mirrors test_the_leak_check_catches_a_planted_real_crew_name above, but
    for the module-source scan: proves the forbidden-token half of the check
    above actually fires, rather than passing because it never runs. The
    planted slug is read out of the real registry at test time, the same way
    the seed-text version does, rather than written into this file as a
    literal."""
    real = json.loads(REAL.read_text(encoding="utf-8"))
    rows = {k: v for k, v in real.items() if not k.startswith("_") and isinstance(v, dict)}
    planted = next((slug for slug, u in rows.items() if u.get("private")), None)
    if planted is None:
        planted = next(iter(rows))
    forbidden = _forbidden_tokens(real) - _SELF_SCAN_ALLOWED_TOKENS
    planted_source = SELF.read_text(encoding="utf-8") + f'\n# "{planted}"\n'
    leaked = _leaked_tokens(planted_source, forbidden)
    assert planted in leaked


def test_the_test_module_itself_does_not_name_a_real_owner():
    """Mirrors test_seed_does_not_name_a_real_owner above, but for this
    module's own source (round-5 review, reviewer comment 5940163120): the
    seed-text owner check had no counterpart scanning `SELF`. One of the real
    registry's two owner slugs is short enough that it is not separately
    pinned by hash and is not otherwise a slug/display/crew/repo/alias/
    home_dir/kb_project anywhere, so it would have passed both self-scan
    tests above if it were ever written into this file's own prose — this
    test is the only thing that would have caught it, which is why it is
    worth having even though neither owner slug appears here today."""
    real = json.loads(REAL.read_text(encoding="utf-8"))
    source = SELF.read_text(encoding="utf-8")
    leaked = _leaked_tokens(source, _owner_tokens(real))
    assert not leaked, f"tests/test_projects_seed.py names a real owner: {sorted(leaked)}"


# --- the candidate generator's fusion property, not just these examples ----
#
# Round 4's plant checks only planted bare, unfused literals, so the gap in
# finding 1 was never exercised (round-5 security review, PR comment
# 5940145642). The tests below build fused forms programmatically instead of
# hard-coding one example per finding, across every shape a round so far has
# found or a reviewer named: camelCase, PascalCase, an all-caps acronym
# followed by a lowercase word, an all-caps acronym followed directly by a
# Capitalized word, kebab-case, snake_case, an all-caps acronym fused into a
# snake_case identifier, a dotted-letter spelling, inside a URL path, inside
# an email address, a possessive, and a digit fused directly onto the word.


def _fused_forms(word: str) -> dict[str, str]:
    """`word` embedded in a neighboring identifier or sentence, in every
    fusion shape this round's review covered. Each shape still carries a
    boundary signal at the join (a case change, a digit, or a separator like
    `/`, `@`, `'`, `.`, `-` or `_`) — an accidental copy-paste preserves the
    source's own casing and punctuation, so this is the realistic "fused but
    not deliberately disguised" shape `_candidates` is built to catch, not
    the signal-free, single-case run `_candidates`' own docstring already
    discloses as out of scope."""
    cap = word.capitalize()
    up = word.upper()
    return {
        "camelCase": f"prefix{cap}Suffix",
        "PascalCase": f"{cap}Suffix",
        "ALLCAPS-then-lowercase": f"prefix{up}suffix",
        "ALLCAPS-then-Capitalized": f"{up}Suffix",
        "kebab-case": f"prefix-{word}-suffix",
        "snake_case": f"prefix_{word}_suffix",
        "ALLCAPS_snake_case": f"PREFIX_{up}_SUFFIX",
        "dotted-letters": f"an acronym written {'.'.join(up)}. in prose",
        "inside-a-url-path": f"https://example.com/api/{word}/resource",
        "inside-an-email-address": f"contact.{word}@example.com",
        "possessive": f"{cap}'s report",
        "digit-adjacent": f"{word}2026",
    }


def test_the_candidate_generator_catches_every_fusion_shape_of_a_forbidden_token():
    """Property test, not an example test: every short structured forbidden
    token (a slug, crew, display, repo or alias string `_forbidden_tokens`
    derives from config/projects.json — not a free-text name, which has no
    structured source to pull from) must survive being fused in each shape
    `_fused_forms` builds. The real registry has hyphenated multi-word
    slugs too (round-6 code review, non-blocking; deliberately not named
    here, for the same reason the ordinary-words list below doesn't quote
    its tokens either), and those were previously skipped by the `t.isalpha()`
    filter below since a hyphen isn't alphabetic. A hyphenated slug is
    normalized the same way `_candidates` itself normalizes a candidate
    before fusing it — `_fused_forms` builds its shapes by capitalizing and
    upper-casing its input, which only makes sense word-by-word, and the
    assertion below already compares against `_normalize(word)` rather than
    the raw token, so fusing the pre-normalized form exercises the identical
    comparison without a hyphen in the way."""
    real = json.loads(REAL.read_text(encoding="utf-8"))
    tokens = _forbidden_tokens(real)
    words = {t for t in tokens if t.isalpha() and 2 <= len(t) <= 15}
    words |= {
        _normalize(t) for t in tokens
        if "-" in t and 2 <= len(_normalize(t)) <= 15
    }
    assert words, "no alphabetic forbidden token available to fuse"
    for word in words:
        for shape, fused in _fused_forms(word).items():
            candidates = _candidates(fused)
            assert _normalize(word) in candidates, (
                f"{shape} fusion of {word!r} ({fused!r}) produced no matching "
                f"candidate (first 20 of {len(candidates)}): {sorted(candidates)[:20]}")


# A stand-in for the three hash-pinned free-text names (`_PINNED_IDENTIFIER_
# HASHES` above): the property test below has to embed its target word
# directly in generated strings, and writing one of the three real pinned
# words here — even only as a fused-and-discarded test input — would itself
# be exactly the literal this module exists to keep out of its own source
# (and the self-scan tests above would catch it). `_hash_token` and
# `_candidates` don't know or care whether a token is one of the real three;
# proving the fusion property against a synthetic word of the same shape
# (lowercase, a handful of letters) exercises the identical code path.
_SYNTHETIC_PINNED_WORD = "zorlix"
_SYNTHETIC_PINNED_HASH = _hash_token(_SYNTHETIC_PINNED_WORD)


def test_the_candidate_generator_catches_every_fusion_shape_of_a_pinned_word():
    for shape, fused in _fused_forms(_SYNTHETIC_PINNED_WORD).items():
        hashes = {_hash_token(c) for c in _candidates(fused)}
        assert _SYNTHETIC_PINNED_HASH in hashes, (
            f"{shape} fusion of the synthetic pinned word ({fused!r}) produced "
            "no candidate matching its hash")


def test_the_acronym_split_does_not_fire_inside_an_all_uppercase_run():
    """Round-6 code review, blocking: the acronym/word split added to
    `_candidate_boundaries` (the ALLCAPS-then-Capitalized shape above) must
    not reintroduce the false positive the module docstring explicitly
    disclaims. `REAL` — this module's own module-level name, an all-
    uppercase run with no trailing lowercase word to justify a split —
    contains, as a plain substring, the same short letter-run as a real
    crew slug this file deliberately never spells out (same reason the
    ordinary-words list below doesn't quote its tokens either). Taken on its
    own, `REAL` must survive as exactly one candidate, with no internal
    boundary the acronym rule (or any other rule) could use to extract that
    substring independently."""
    assert _candidates("REAL") == {"real"}


# The inverse of the property above: a short forbidden token must not fire on
# an unrelated word that merely contains it as a plain mid-word substring,
# with no case, digit or separator boundary around it. Keeping this list is
# what proves the generator's boundary rules — not just the length of the
# window — are doing the false-positive-avoidance work (round-5 review,
# reviewer comment 5940163120). Each word below contains one of the real
# registry's short (two- or three-letter) forbidden tokens this way; which
# token is deliberately not spelled out in this comment, since a bare quoted
# copy of it would itself be exactly the fused-token literal the self-scan
# tests above exist to catch.
_ORDINARY_WORDS_CONTAINING_SHORT_TOKENS_AS_SUBSTRINGS = (
    "search",
    "team",
    "area",
    "beacon",
    "yeast",
)


def test_the_leak_check_does_not_false_positive_on_ordinary_words():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    forbidden = _forbidden_tokens(real)
    for word in _ORDINARY_WORDS_CONTAINING_SHORT_TOKENS_AS_SUBSTRINGS:
        leaked = _leaked_tokens(word, forbidden)
        assert not leaked, f"{word!r} false-positived on forbidden token(s) {leaked}"
