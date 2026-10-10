"""`post-release-note` — the release note's path to the team site.

`/products/release-notes` has rendered `release_note` signals since ARCH-360 item
5b and nothing ever fed it: on 2026-07-26 the store held exactly one, a smoke test
reading "delete me". Every real note went to `#vivy-releases` instead. These tests
pin the properties that decide whether the page stays trustworthy — the note
reaches the site or the caller is told it did not, and the contract fields match
what the page reads.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def prn(post_release_note):
    return post_release_note


def _stub_signal(monkeypatch, prn, result="created"):
    sent = {}

    def fake(payload, **kw):
        sent.update(payload)
        sent["_kw"] = kw
        return result

    monkeypatch.setattr(prn.SC, "post_signal", fake)
    return sent


def _ok(prn, monkeypatch, *extra):
    sent = _stub_signal(monkeypatch, prn)
    rc = prn.main(["--product", "vivy", "--version", "release-2026-07-26",
                   *extra, "## Shipped\n- a thing"])
    return sent, rc


# --- the contract with the page ----------------------------------------------


def test_the_note_is_written_with_the_fields_the_page_reads(prn, monkeypatch):
    """`kind` IS the page's query — release_notes_build.live_html filters on it
    alone, so a wrong value renders nowhere. `source` is not filtered; it matches
    what the page's capture form sends, which is convention rather than a gate."""
    sent, rc = _ok(prn, monkeypatch)
    assert rc == 0
    assert sent["kind"] == "release_note"
    assert sent["source"] == "release-notes"
    assert sent["product"] == "vivy"
    assert sent["body"].startswith("## Shipped")


def test_the_version_is_the_stream_because_it_heads_the_card(prn, monkeypatch):
    sent, _ = _ok(prn, monkeypatch)
    assert sent["stream"] == "release-2026-07-26"


def test_produced_at_is_set_so_the_note_sorts(prn, monkeypatch):
    """The page orders on produced_at first and the store leaves it NULL when the
    payload omits it, so a note without one sorts behind every note that has one."""
    sent, _ = _ok(prn, monkeypatch)
    assert sent["produced_at"]


def test_the_note_carries_a_ref_to_its_release(prn, monkeypatch):
    """post-changelog shipped WITHOUT refs and its entries could not be matched to
    their merges (ARCH-411). The same omission here makes a note unattributable."""
    sent, _ = _ok(prn, monkeypatch)
    assert sent["refs"]["version"] == "release-2026-07-26"
    assert sent["refs"]["url"] == (
        "https://github.com/convivy/vivy/releases/tag/release-2026-07-26")


def test_a_rerun_of_the_same_release_is_idempotent(prn, monkeypatch):
    """A release is cut once per version per product, so that pair is the identity
    and a repeated ceremony is a no-op rather than a second copy of the note."""
    sent, _ = _ok(prn, monkeypatch)
    assert sent["dedup_key"] == "release_note:vivy:release-2026-07-26"


def test_the_dedup_key_matches_the_pages_own_capture_form(prn, monkeypatch):
    """The two write paths must dedup against EACH OTHER, not merely each against
    itself. The form's JS sends `release_note:<product>:<version>`; a near-miss
    prefix here would pass every other test while silently doubling any note that
    took both routes — and the store IGNORES a collision rather than updating, so
    the extra row could only be deleted, never repaired (code review, #251).

    Asserted against the literal string the form builds, so a change to either
    side that breaks the pairing fails here.
    """
    sent, _ = _ok(prn, monkeypatch)
    product, version = "vivy", "release-2026-07-26"
    form_key = "release_note:" + product + ":" + version   # release_notes_build's JS
    assert sent["dedup_key"] == form_key


# --- refusals ------------------------------------------------------------------


@pytest.mark.parametrize("version", [
    "a version with spaces",
    "Shipped the new turnover flow and fixed the digest",   # prose, not a version
    "../../etc/passwd",
    "v1/../../x",
    "",
    "x" * 65,
])
def test_a_version_that_is_not_a_version_is_refused(prn, monkeypatch, version):
    """It heads the card and is half the idempotency key, so prose here would both
    render wrong and defeat the dedup."""
    _stub_signal(monkeypatch, prn)
    with pytest.raises(SystemExit):
        prn.main(["--product", "vivy", "--version", version, "a note"])


@pytest.mark.parametrize("product", [
    "not a product", "../../etc", "vivy/../x", "", "x" * 65,
])
def test_a_product_that_is_not_a_repo_name_is_refused(prn, monkeypatch, product):
    """`product` reaches the same dedup key and the same refs.url path segment as
    `version`, and was the only one of the pair left ungated — an asymmetry with
    no reason behind it (security review, #251)."""
    _stub_signal(monkeypatch, prn)
    with pytest.raises(SystemExit):
        prn.main(["--product", product, "--version", "release-2026-07-26", "a note"])


def test_a_duplicate_warns_loudly_and_still_succeeds(prn, monkeypatch, capsys):
    """The dedup key is shared with the page's capture form, so a note already
    submitted there for this version OWNS the key: this call writes nothing and
    the site keeps the earlier note. Exiting 0 SILENTLY made the ceremony report
    success over content it did not write (security review, #251).

    Still 0, not 1 — re-running a release ceremony is legitimate, and the script's
    steps are individually retried — so the operator gets a loud warning on stderr
    rather than a failure that would break a valid retry.
    """
    _stub_signal(monkeypatch, prn, result="duplicate")
    rc = prn.main(["--product", "vivy", "--version", "release-2026-07-26", "a note"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "NOT replaced" in err
    assert "release-2026-07-26" in err          # names the version it is about
    assert "/products/release-notes" in err     # and where to go look


def test_an_empty_note_is_refused(prn, monkeypatch):
    _stub_signal(monkeypatch, prn)
    with pytest.raises(SystemExit):
        prn.main(["--product", "vivy", "--version", "release-2026-07-26", "   "])


def test_a_note_can_be_read_from_a_file(prn, monkeypatch, tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("## Shipped\n- from a file", encoding="utf-8")
    sent = _stub_signal(monkeypatch, prn)
    rc = prn.main(["--product", "vivy", "--version", "release-2026-07-26",
                   "--file", str(p)])
    assert rc == 0 and "from a file" in sent["body"]


# --- fail-safe -----------------------------------------------------------------


def test_a_failed_signal_write_fails_the_command(prn, monkeypatch):
    """The site is the deliverable. A release ceremony must not report success on
    a note that never landed."""
    _stub_signal(monkeypatch, prn, result="error:503")
    assert prn.main(["--product", "vivy", "--version", "release-2026-07-26", "note"]) == 1


def test_no_token_fails_rather_than_passing_quietly(prn, monkeypatch):
    _stub_signal(monkeypatch, prn, result="skip")
    assert prn.main(["--product", "vivy", "--version", "release-2026-07-26", "note"]) == 1
