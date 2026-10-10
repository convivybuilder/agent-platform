"""A review that was written and never posted must be recoverable without hand-parsing.

ARCH-397 marks a review run errored when it ends with no stamp, which is right: the
absence has to be loud, because a merge gate reading "no review" while the caller
reads "completed" is the false confidence that rule exists to remove.

What it did not do was say where the review went. It usually went nowhere — it sat
complete in the run log, and recovering it meant reading JSONL by hand. That happened
four times in one Keel session on 2026-08-12/13, each time recovering a full review
that had already been paid for.

These tests drive the real helper against real run-dir fixtures, because the thing
under test is what it manages to extract from files another process wrote.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

AGENTCTL = Path(__file__).resolve().parents[1] / "bin" / "agentctl"


def _load():
    spec = importlib.util.spec_from_loader("agentctl_mod", loader=None)
    mod = importlib.util.module_from_spec(spec)
    # agentctl resolves its lib dir from `__file__` (ARCH-229), which a bare
    # module_from_spec(loader=None) never sets -- exec'ing its source without
    # this raises NameError before a single test can run.
    mod.__dict__["__file__"] = str(AGENTCTL)
    src = AGENTCTL.read_text()
    exec(compile(src, str(AGENTCTL), "exec"), mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(autouse=True)
def _isolate_digest_index(mod, tmp_path, monkeypatch):
    """No test may touch the real `~/.agentctl/rescued-review-digests`.

    AUTOUSE, not per-test, because the failure is silent and permanent: a test that
    forgets writes a digest into the developer's actual home directory forever, and
    nothing deletes it. That then refuses unrelated `review-post` calls whose body
    normalises to the same digest — seven previously-passing tests in
    `test_agentctl.py` failed exactly this way after one run of this module
    (reviewer, PR #395 round 3). Opting in per test is how the first version got it
    wrong in eleven of thirteen."""
    monkeypatch.setattr(mod, "RESCUE_DIGEST_INDEX", tmp_path / "digest-index")


def _run_dir(tmp_path: Path, *, result: str | None, called_post: bool,
             transcript: bool = True, mention_only: bool = False,
             truncated: bool = False) -> Path:
    """A run dir shaped like a real one. `transcript` absent models a removed
    record; `mention_only` models a run that merely READ the string."""
    d = tmp_path / "run-fixture"
    d.mkdir(parents=True)
    lines = [json.dumps({"type": "system", "subtype": "thinking_tokens"})]
    if result is not None:
        lines.append(json.dumps({"type": "result", "subtype": "success", "result": result}))
    body = "\n".join(lines) + "\n"
    if truncated:
        body += '{"type":"result","subtype":"suc'   # died mid-write
    (d / "detach.out.log").write_text(body)

    if transcript:
        blocks = [{"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]
        if mention_only:
            blocks.append({"type": "tool_use", "name": "Bash",
                           "input": {"command": "grep -rn 'review-post' bin/"}})
        if called_post:
            blocks.append({"type": "tool_use", "name": "Bash",
                           "input": {"command": "agentctl review-post --pr 9 --verdict APPROVE "
                                                "--body-file /tmp/r.md"}})
        (d / "transcript.jsonl").write_text("\n".join(
            json.dumps({"type": "assistant", "message": {"content": [b]}}) for b in blocks) + "\n")

    # `tool_calls.jsonl` carries result_summary free text — the log the first
    # version of this check wrongly read. Seed it with a mention to prove the
    # check no longer consults it.
    (d / "tool_calls.jsonl").write_text(json.dumps(
        {"tool_name": "Read", "input": {"file_path": "x.py"},
         "result_summary": "... see agentctl review-post for details ..."}) + "\n")
    return d


def test_a_written_review_is_saved_where_a_human_can_read_it(mod, tmp_path):
    """The whole point: the review existed, and finding it took hand-parsing."""
    body = "## Findings\n\n- **Blocking:** the thing is broken.\n\n**Verdict:** thumbs-down"
    d = _run_dir(tmp_path, result=body, called_post=False)
    text, attempted = mod._rescue_unposted_review(d, "run-abc")
    assert text == body
    saved = (d / "unposted-review.md").read_text()
    assert body in saved
    assert "deliberate override" in saved, "the file must say relaying it takes an override"


def test_it_distinguishes_a_refused_post_from_a_forgotten_one(mod, tmp_path):
    """A run that CALLED review-post and was refused behaved correctly — most often
    because the head moved and ARCH-184's pin turned the post into a refusal. Reporting
    that identically to a run that simply forgot cost a re-dispatch of a run that had
    done nothing wrong (2026-08-13)."""
    forgot = _run_dir(tmp_path / "a", result="body", called_post=False)
    tried = _run_dir(tmp_path / "b", result="body", called_post=True)
    assert mod._rescue_unposted_review(forgot, "run-1")[1] is False
    assert mod._rescue_unposted_review(tried, "run-2")[1] is True


def test_merely_reading_the_string_is_not_an_attempt(mod, tmp_path):
    """The first version substring-matched `tool_calls.jsonl`, which stores
    `result_summary` free text — so any run that READ the words "review-post" was
    marked as having tried to post. Every reviewer of this repository does that
    repeatedly, and a PR author can arrange it deliberately (security-reviewer,
    PR #395). The fixture seeds exactly that bait in both places."""
    d = _run_dir(tmp_path, result="body", called_post=False, mention_only=True)
    assert mod._rescue_unposted_review(d, "run-bait")[1] is False


def test_an_unreadable_transcript_reports_unknown_rather_than_no(mod, tmp_path):
    """"It did not try" and "we cannot tell" are different claims, and reporting the
    second as the first is the false confidence ARCH-397 exists to remove."""
    d = _run_dir(tmp_path, result="body", called_post=True, transcript=False)
    assert mod._rescue_unposted_review(d, "run-notrans")[1] is None


def test_a_truncated_log_still_yields_the_last_complete_review(mod, tmp_path):
    """This path runs after a run DIED, so a half-written final line is the expected
    input rather than an edge case."""
    d = _run_dir(tmp_path, result="the real review", called_post=False, truncated=True)
    text, _ = mod._rescue_unposted_review(d, "run-trunc")
    assert text == "the real review"


def test_the_saved_file_carries_the_marker_review_post_refuses(mod, tmp_path):
    """The prose warning was a control aimed at agents, which are its readers. The
    marker is what `review-post` can actually enforce."""
    d = _run_dir(tmp_path, result="body", called_post=False)
    mod._rescue_unposted_review(d, "run-marked")
    saved = (d / "unposted-review.md").read_text()
    assert mod.RESCUED_REVIEW_MARKER in saved
    assert (d / "unposted-review.md").stat().st_mode & 0o077 == 0, "must be owner-only"


def test_a_run_with_no_review_text_saves_nothing_rather_than_an_empty_file(mod, tmp_path):
    """An empty rescue file would read as 'the review was recovered and it was blank',
    which is the same false-confidence shape this whole path exists to remove."""
    d = _run_dir(tmp_path, result=None, called_post=False)
    text, _ = mod._rescue_unposted_review(d, "run-xyz")
    assert text is None
    assert not (d / "unposted-review.md").exists()


def test_a_missing_run_dir_is_survivable_and_reports_unknown(mod, tmp_path):
    """Finalize must never die on a telemetry read — the run's real status matters
    more than its rescue. And with no transcript to read, the honest answer about
    whether it tried to post is "unknown", not "no"."""
    text, attempted = mod._rescue_unposted_review(tmp_path / "nope", "run-gone")
    assert text is None
    assert attempted is None, "no transcript means we cannot tell, not that it did not try"


def test_the_rescue_can_never_take_down_finalize(mod, tmp_path, monkeypatch):
    """The rescue is an optional enrichment on the finalize path, and an escape from
    it would propagate past `R.remove_run` and `_reap_review_worktree` — leaving the
    run registered and its worktree leaked, which is far worse than losing a rescue.

    The inner function catches OSError around its reads and its write. That is not
    enough: a UnicodeEncodeError on review prose under a stripped locale is not an
    OSError, and neither is a TypeError from an unexpected record shape (reviewer,
    PR #395). So the outer function catches everything, the same rule
    `_scan_rate_limits` states for itself.

    Raising a non-OSError from the inner function is the only honest way to test a
    total catch — an OSError would pass even if the broadening were reverted."""
    def boom(*_a, **_k):
        raise UnicodeEncodeError("ascii", "—", 0, 1, "ordinal not in range(128)")
    monkeypatch.setattr(mod, "_rescue_unposted_review_inner", boom)
    assert mod._rescue_unposted_review(tmp_path, "run-boom") == (None, None)

    def other_boom(*_a, **_k):
        raise TypeError("unexpected record shape")
    monkeypatch.setattr(mod, "_rescue_unposted_review_inner", other_boom)
    assert mod._rescue_unposted_review(tmp_path, "run-boom2") == (None, None)


def test_the_saved_file_survives_non_ascii_review_prose(mod, tmp_path):
    """Em dashes, smart quotes and arrows are ordinary in review prose, and the write
    must not depend on the locale to handle them. `T.write_owner_only` encodes UTF-8
    explicitly rather than deferring to `getpreferredencoding`, which is what
    `Path.write_text()` would have done."""
    body = "## Findings\n\n- **Blocking:** an em dash — a smart quote ’ and an arrow →\n"
    d = _run_dir(tmp_path, result=body, called_post=False)
    text, _ = mod._rescue_unposted_review(d, "run-unicode")
    assert text == body
    assert "→" in (d / "unposted-review.md").read_text(encoding="utf-8")


def test_stripping_the_marker_does_not_evade_the_refusal(mod, tmp_path, monkeypatch):
    """The marker alone was a one-line evasion of a control whose entire purpose is to
    cost more than one line — the same "an agent reads it and may skip it" weakness
    that made a prose warning insufficient in the first place (security-reviewer,
    PR #395 round 2).

    So the digest is the control. A body with the marker deleted, reflowed, or
    trailing-trimmed still matches; a genuinely rewritten review does not, which is
    correct — somebody who rewrites it has written one."""
    body = "## Findings\n\n- **Blocking:** the thing is broken.\n\n**Verdict:** thumbs-down"
    d = _run_dir(tmp_path, result=body, called_post=False)
    mod._rescue_unposted_review(d, "run-dig")

    known = {ln.split("\t")[-1] for ln in mod.RESCUE_DIGEST_INDEX.read_text().splitlines()}
    assert mod._rescued_digest(body) in known

    saved = (d / "unposted-review.md").read_text()
    stripped = "\n".join(ln for ln in saved.splitlines() if mod.RESCUED_REVIEW_MARKER not in ln)
    assert mod.RESCUED_REVIEW_MARKER not in stripped, "fixture must actually strip it"
    # The header travels with the file; what a relayer would paste is the review.
    assert mod._rescued_digest(body) in known, "the review's own digest is what is indexed"
    assert mod._rescued_digest(body + "\n\n") == mod._rescued_digest(body), \
        "trailing whitespace must not evade it"

    rewritten = "## Findings\n\nSomething entirely different was found here."
    assert mod._rescued_digest(rewritten) not in known, \
        "a genuine rewrite is a new review and must post freely"


def test_a_missing_digest_index_does_not_break_posting(mod, tmp_path, monkeypatch):
    """A posting path must not fail because telemetry is absent — the refusal is a
    guard on a rare case, and breaking every post to enforce it would be worse than
    the thing it prevents."""
    assert not mod.RESCUE_DIGEST_INDEX.exists()
    assert mod._rescued_digest("anything") is not None


def test_one_bad_record_cannot_erase_a_review_already_recovered(mod, tmp_path):
    """A line that parses as JSON but is not a dict slips past the substring
    pre-filter and raises AttributeError on `.get` — which the outer total catch
    would turn into a complete loss, discarding a review extracted from an EARLIER
    line. That is the exact dollar this feature exists to save (reviewer, PR #395
    round 2)."""
    d = tmp_path / "run-fixture"
    d.mkdir(parents=True)
    (d / "detach.out.log").write_text(
        json.dumps({"type": "result", "subtype": "success", "result": "the real review"}) + "\n"
        + json.dumps([{"type": "result", "result": "a list, not a record"}]) + "\n")
    (d / "transcript.jsonl").write_text("")
    text, attempted = mod._rescue_unposted_review(d, "run-badrec")
    assert text == "the real review", "a malformed later line must not erase an earlier result"
    assert attempted is False


def test_a_non_string_result_does_not_erase_the_review(mod, tmp_path):
    """`result` is trusted to be a string because the digest calls .splitlines() on
    it. A dict raises AttributeError, escapes to the outer total catch, and discards
    the recovered text AND the correctly-computed `attempted` flag — after which the
    finalize message says "No review text was found in its log either", which is
    false: text was found and the code choked on its shape (reviewer, PR #395
    round 3).

    Third round in a row this PR has produced a per-record type surprise that reaches
    the outer catch and erases a result already in hand. The guard here matches the
    one this file already applies to the identical field when scanning transcripts."""
    d = tmp_path / "run-fixture"
    d.mkdir(parents=True)
    (d / "detach.out.log").write_text(
        json.dumps({"type": "result", "result": "the real review"}) + "\n"
        + json.dumps({"type": "result", "result": {"nested": "not a string"}}) + "\n")
    (d / "transcript.jsonl").write_text("")
    text, attempted = mod._rescue_unposted_review(d, "run-dictresult")
    assert text == "the real review"
    assert attempted is False, "the flag must survive a malformed later record too"


def test_the_digest_index_is_scoped_to_its_pr(mod, tmp_path):
    """An unbounded content-only index grows a false-positive surface forever: a
    fresh review whose normalised text collides with any rescue ever recorded would
    be refused on a PR that has nothing to do with it. A rescue is only relevant to
    the PR it was written about (reviewer, PR #395 round 3)."""
    d = _run_dir(tmp_path, result="a review body", called_post=False)
    mod._rescue_unposted_review(d, "run-scoped", "395")
    line = mod.RESCUE_DIGEST_INDEX.read_text().strip()
    pr, digest = line.split("\t")
    assert pr == "395"
    assert digest == mod._rescued_digest("a review body")


def test_no_test_in_this_module_can_reach_the_real_index(mod):
    """The autouse fixture is the control. Asserting it holds is cheap, and the
    failure it prevents is silent, permanent and lands in a developer's home."""
    assert "agent-runs" not in str(mod.RESCUE_DIGEST_INDEX)
    assert str(Path.home()) not in str(mod.RESCUE_DIGEST_INDEX), \
        "the index under test must never be the real one"
