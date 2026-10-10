"""tools/review-eval/variant-agents/reviewer.md — the review-replay eval
harness's prompt-variant arm (agent-platform#807): today's reviewer
definition with the skepticism push removed and a plain "no findings can be
correct" statement added, resolved the same way `agentctl spawn
--agents-dir` resolves any local agent override.
"""
from __future__ import annotations

from pathlib import Path

VARIANT_DIR = Path(__file__).resolve().parent.parent / "tools" / "review-eval" / "variant-agents"
ROSTER_SNAPSHOT = (Path(__file__).resolve().parent / "fixtures" / "review_eval"
                   / "reviewer_roster_snapshot_2026-10-07.md")

_REMOVED_1 = ("Your job is to find issues. Default to skepticism. If the diff looks fine "
             "on first read, look again — what is it pretending to be that it isn't?")
_ADDED_1 = ("Your job is to find issues. An approval with no findings is the correct "
           "outcome when the diff is sound; it is not evidence that you looked too lightly.")
_REMOVED_2 = "You have no fast-bail (unlike `security-reviewer`). "


def test_variant_file_exists_at_the_name_agents_dir_resolution_expects():
    assert (VARIANT_DIR / "reviewer.md").is_file()


def test_resolve_agent_loads_the_variant_with_the_two_sentences_removed_and_one_added(
        agentctl):
    agent = agentctl.resolve_agent("reviewer", repo="agent-platform",
                                   agents_dir=str(VARIANT_DIR))
    body = agent["body"]
    assert agent["local"] is True
    assert "Default to skepticism" not in body
    assert "If the diff looks fine on first read, look again" not in body
    assert "You have no fast-bail" not in body
    assert "correct outcome when the diff is sound" in body
    # unrelated content is untouched — this is a targeted edit, not a rewrite
    assert "Green required CI is a hard precondition of APPROVE" in body
    assert "agentctl review-post --pr <PR-num>" in body


def _parse_frontmatter_body(text: str) -> str:
    """The same split `agentctl.resolve_agent`/`_parse_frontmatter` does,
    duplicated narrowly so this test does not depend on importing `agentctl`
    just to compare two files' bodies."""
    assert text.startswith("---")
    end = text.find("\n---", 3)
    return text[end + len("\n---"):].lstrip("\n")


def test_the_variant_differs_from_the_roster_snapshot_only_by_the_sanctioned_edit():
    """Round-1 review (agent-platform#813): the variant's own header claimed
    'everything else is untouched' while silently dropping three more
    passages — a false comment, and one that also confounded the eval's own
    experiment (the variant arm is meant to isolate ONE variable). Pin the
    actual diff against a frozen snapshot of the roster this file is built
    from, so any future accidental drop/edit beyond the sanctioned two-out
    one-in change fails loudly here instead of surviving as an undisclosed
    difference."""
    snapshot_body = _parse_frontmatter_body(ROSTER_SNAPSHOT.read_text())
    variant_body = _parse_frontmatter_body(
        (VARIANT_DIR / "reviewer.md").read_text())

    assert snapshot_body.count(_REMOVED_1) == 1
    assert snapshot_body.count(_REMOVED_2) == 1
    expected = snapshot_body.replace(_REMOVED_1, _ADDED_1).replace(_REMOVED_2, "")

    assert variant_body == expected
