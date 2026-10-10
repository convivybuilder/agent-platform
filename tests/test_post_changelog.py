"""`post-changelog` — the co-authored changelog entry path.

The site's changelog used to be synthesised from PR bodies by
`github_capture.signal_from_merge`, which made every entry a document written for
a reviewer. This tool takes the entry the co actually writes. These tests pin the
two properties that decide whether the log stays trustworthy: an entry either
reaches the site or the caller is told it did not.
"""
from __future__ import annotations

import pytest

import signal_client as SC


@pytest.fixture
def pc(post_changelog):
    return post_changelog


def _stub_signal(monkeypatch, pc, result="created"):
    sent = {}

    def fake(payload, **kw):
        sent.update(payload)
        sent["_kw"] = kw
        return result

    monkeypatch.setattr(pc.SC, "post_signal", fake)
    return sent


def test_the_entry_is_written_as_a_changelog_signal(pc, monkeypatch):
    sent = _stub_signal(monkeypatch, pc)
    rc = pc.main(["--product", "vivy", "*Merged:* the thing now works #1032"])
    assert rc == 0
    assert sent["kind"] == "changelog"
    assert sent["product"] == "vivy"
    assert sent["source"] == "co"          # distinguishes it from the github feed
    assert "the thing now works" in sent["body"]
    # /engineering/changelog orders by produced_at and the store leaves it NULL
    # when omitted, so an entry without it renders last or not at all — a live
    # defect the end-to-end run caught. Pin it before a refactor can drop it.
    assert sent.get("produced_at")


def test_a_signal_failure_is_a_nonzero_exit(pc, monkeypatch):
    """The site IS the deliverable. A merge ceremony must not report success for
    an entry that never landed — the failure this whole change exists to avoid is
    a changelog that looks maintained and isn't."""
    _stub_signal(monkeypatch, pc, result="error:500")
    assert pc.main(["--product", "vivy", "thing #1"]) == 1


def test_no_ops_token_is_also_a_nonzero_exit(pc, monkeypatch):
    """`post_signal` returns 'skip' with no token — silently, by design, because a
    capture failure must not surface to ITS callers. Here that same silence would
    mean a lost entry, so it is surfaced."""
    _stub_signal(monkeypatch, pc, result="skip")
    assert pc.main(["--product", "vivy", "thing #1"]) == 1


def test_an_entry_without_a_pr_link_is_refused(pc, monkeypatch):
    """A changelog entry with no click-through to the work is most of the value
    gone. vivy's script enforced this; the habit carries over."""
    _stub_signal(monkeypatch, pc)
    with pytest.raises(SystemExit):
        pc.main(["--product", "vivy", "we made things better"])


def test_an_empty_entry_is_refused(pc, monkeypatch):
    _stub_signal(monkeypatch, pc)
    with pytest.raises(SystemExit):
        pc.main(["--product", "vivy", "   "])


def test_the_dedup_key_makes_a_reposted_entry_idempotent(pc, monkeypatch):
    """A merge ceremony re-run must update the entry, not add a second one — the
    same guarantee github_capture had with `gh:<repo>:pr<n>:merged`."""
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "*Merged:* thing #1032"])
    first = sent["dedup_key"]
    sent.clear()
    pc.main(["--product", "vivy", "*Merged:* thing #1032 (reworded slightly)"])
    assert sent["dedup_key"] == first, "same PR must reuse the key regardless of wording"
    assert "pr1032" in first


def test_an_explicit_pr_flag_beats_a_number_in_the_body(pc, monkeypatch):
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "closes #99, see #1032", "--pr", "1032"])
    assert "pr1032" in sent["dedup_key"]


def test_a_pull_url_entry_gets_a_stable_pr_key_not_a_hash(pc, monkeypatch):
    """An entry whose only PR reference is a full /pull/N URL passes the refusal
    (the URL is a valid click-through), so it must also yield a pr-keyed,
    deterministic dedup_key. The body-hash fallback is str-hash under a
    randomized PYTHONHASHSEED — a re-run would duplicate the entry, not update
    it. Reworded re-post of the same PR must reuse the key."""
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "*Merged:* the thing https://github.com/convivy/vivy/pull/1032"])
    first = sent["dedup_key"]
    assert "pr1032" in first
    sent.clear()
    pc.main(["--product", "vivy", "*Merged:* the thing, reworded https://github.com/convivy/vivy/pull/1032"])
    assert sent["dedup_key"] == first


def test_the_write_is_on_the_delivery_budget_not_the_tee_s(pc, monkeypatch):
    """A changelog entry is written once per merge and must land, so it routes
    through signal_client on the DELIVERY budget — the reason the tool no longer
    hand-rolls its own POST to escape the tee's 2s. If it fell back to the
    default, a cold store could time out on a write it then completes anyway."""
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "*Merged:* thing #1032"])
    assert sent["_kw"].get("timeout") == SC._DELIVERY_TIMEOUT
    assert SC._DELIVERY_TIMEOUT > SC._TIMEOUT


def test_the_entry_carries_its_pr_ref(pc, monkeypatch):
    """The entry must be attributable to its merge. Without refs, changelog-drain
    — which decides a merge needs writing when no entry carries its PR — matched
    nothing and re-explained the same merge on every pass. Caught on the first
    live run (ARCH-411). The site reads the same fields for the card's PR link."""
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "**Merged:** a thing. #1050"])
    assert sent["refs"]["pr"] == 1050
    assert sent["refs"]["url"] == "https://github.com/convivy/vivy/pull/1050"


def test_the_pr_ref_is_an_int_so_it_compares_with_the_captured_merge(pc, monkeypatch):
    """github_capture writes refs.pr as the GitHub integer. A string "1050" here
    would never equal 1050 there, which is the same non-match by a subtler route."""
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy", "--pr", "1050", "**Merged:** a thing. #1050"])
    assert sent["refs"]["pr"] == 1050 and isinstance(sent["refs"]["pr"], int)


def test_a_url_only_entry_still_gets_a_ref(pc, monkeypatch):
    sent = _stub_signal(monkeypatch, pc)
    pc.main(["--product", "vivy",
             "**Merged:** a thing. https://github.com/convivy/vivy/pull/77"])
    assert sent["refs"]["pr"] == 77
