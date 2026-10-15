"""Tests for the GitHub activity signal-shaping transforms (E25,
issue_01M4ZB5C5ZX0MV206064MTRAE0) — github_activity.py.

Each kind's whole returned payload is checked against a hand-written
expected dict, so a wrong field anywhere fails the test, not only the one a
narrower assertion happened to name. All GitHub JSON here is synthetic: no
real repo names, logins or bodies.
"""
import github_activity as ga


def test_signal_from_review_approved():
    review = {
        "id": 9001,
        "state": "APPROVED",
        "body": "looks good",
        "user": {"login": "widget-reviewer"},
        "html_url": "https://github.com/acme/widget/pull/42#pullrequestreview-9001",
        "submitted_at": "2026-10-01T12:00:00Z",
    }
    assert ga.signal_from_review("widget", 42, review) == {
        "source": "github_review",
        "kind": "review_verdict",
        "product": "widget",
        "stream": "widget#42",
        "actor": "widget-reviewer",
        "body": "looks good",
        "refs": {"pr": 42,
                 "url": "https://github.com/acme/widget/pull/42#pullrequestreview-9001"},
        "meta": {"state": "APPROVED"},
        "produced_at": "2026-10-01T12:00:00Z",
        "dedup_key": "gh:widget:pr42:review:9001",
    }


def test_signal_from_review_dedup_key_format():
    review = {"id": 123, "state": "APPROVED", "body": "ok",
               "user": {"login": "a"}, "html_url": "", "submitted_at": ""}
    sig = ga.signal_from_review("some-repo", 7, review)
    assert sig["dedup_key"] == "gh:some-repo:pr7:review:123"


def test_signal_from_review_commented_empty_body_is_none():
    review = {"id": 1, "state": "COMMENTED", "body": "",
               "user": {"login": "a"}, "html_url": "", "submitted_at": ""}
    assert ga.signal_from_review("widget", 1, review) is None


def test_signal_from_review_commented_empty_body_whitespace_only_is_none():
    review = {"id": 1, "state": "COMMENTED", "body": "   ",
               "user": {"login": "a"}, "html_url": "", "submitted_at": ""}
    assert ga.signal_from_review("widget", 1, review) is None


def test_signal_from_review_approved_empty_body_is_a_signal():
    review = {"id": 2, "state": "APPROVED", "body": "",
               "user": {"login": "a"}, "html_url": "", "submitted_at": ""}
    sig = ga.signal_from_review("widget", 1, review)
    assert sig is not None
    assert sig["body"] == ""
    assert sig["meta"] == {"state": "APPROVED"}


def test_signal_from_review_no_state_is_none():
    review = {"id": 3, "state": "", "body": "x",
               "user": {"login": "a"}, "html_url": "", "submitted_at": ""}
    assert ga.signal_from_review("widget", 1, review) is None


def test_signal_from_merge():
    pr = {
        "number": 55,
        "user": {"login": "widget-author"},
        "title": "Add the frobnicator",
        "body": "Closes the ticket about frobnication.",
        "merged_at": "2026-10-02T08:30:00Z",
        "html_url": "https://github.com/acme/widget/pull/55",
    }
    assert ga.signal_from_merge("widget", pr) == {
        "source": "github",
        "kind": "changelog",
        "stream": "widget",
        "actor": "widget-author",
        "product": "widget",
        "body": "Add the frobnicator\n\nCloses the ticket about frobnication.",
        "refs": {"pr": 55, "url": "https://github.com/acme/widget/pull/55",
                 "merged_at": "2026-10-02T08:30:00Z", "author": "widget-author"},
        "meta": {},
        "produced_at": "2026-10-02T08:30:00Z",
        "dedup_key": "gh:widget:pr55:merged",
    }


def test_signal_from_merge_dedup_key_format():
    pr = {"number": 9, "user": {"login": "a"}, "title": "t", "body": "",
          "merged_at": "2026-10-02T08:30:00Z", "html_url": ""}
    sig = ga.signal_from_merge("other-repo", pr)
    assert sig["dedup_key"] == "gh:other-repo:pr9:merged"


def test_signal_from_merge_unmerged_pr_is_none():
    pr = {"number": 55, "user": {"login": "a"}, "title": "t", "body": "",
          "merged_at": None, "html_url": ""}
    assert ga.signal_from_merge("widget", pr) is None


def test_signal_from_merge_unmerged_pr_missing_merged_at_is_none():
    pr = {"number": 55, "user": {"login": "a"}, "title": "t", "body": "",
          "html_url": ""}
    assert ga.signal_from_merge("widget", pr) is None


def test_signal_from_merge_clips_long_body():
    pr = {
        "number": 1,
        "user": {"login": "a"},
        "title": "x" * 4001,
        "body": "",
        "merged_at": "2026-10-02T08:30:00Z",
        "html_url": "",
    }
    sig = ga.signal_from_merge("widget", pr)
    assert sig["body"] == "x" * 4000 + " …"
    assert len(sig["body"]) == 4000 + len(" …")


def test_signal_from_review_comment():
    rc = {
        "id": 777,
        "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/42",
        "body": "nit: typo here",
        "user": {"login": "widget-reviewer"},
        "html_url": "https://github.com/acme/widget/pull/42#discussion_r777",
        "created_at": "2026-10-01T11:00:00Z",
        "path": "src/widget.py",
    }
    assert ga.signal_from_review_comment("widget", rc) == {
        "source": "github_review",
        "kind": "review_comment",
        "product": "widget",
        "stream": "widget#42",
        "actor": "widget-reviewer",
        "body": "nit: typo here",
        "refs": {"pr": 42,
                 "url": "https://github.com/acme/widget/pull/42#discussion_r777",
                 "path": "src/widget.py"},
        "meta": {},
        "produced_at": "2026-10-01T11:00:00Z",
        "dedup_key": "gh:widget:pr42:rc:777",
    }


def test_signal_from_review_comment_no_path():
    rc = {
        "id": 778,
        "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/42",
        "body": "another comment",
        "user": {"login": "a"},
        "html_url": "",
        "created_at": "",
    }
    sig = ga.signal_from_review_comment("widget", rc)
    assert "path" not in sig["refs"]


def test_signal_from_review_comment_dedup_key_format():
    rc = {"id": 42, "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/9",
          "body": "x", "user": {"login": "a"}, "html_url": "", "created_at": ""}
    sig = ga.signal_from_review_comment("some-repo", rc)
    assert sig["dedup_key"] == "gh:some-repo:pr9:rc:42"


def test_signal_from_review_comment_empty_body_is_none():
    rc = {"id": 1, "pull_request_url": "https://api.github.com/repos/acme/widget/pulls/42",
          "body": "", "user": {"login": "a"}, "html_url": "", "created_at": ""}
    assert ga.signal_from_review_comment("widget", rc) is None


def test_signal_from_review_comment_no_number_in_url_is_none():
    rc = {"id": 1, "pull_request_url": "", "body": "x",
          "user": {"login": "a"}, "html_url": "", "created_at": ""}
    assert ga.signal_from_review_comment("widget", rc) is None


def test_signal_from_issue_comment_on_a_pull_request():
    ic = {
        "id": 555,
        "issue_url": "https://api.github.com/repos/acme/widget/issues/42",
        "body": "ship it",
        "user": {"login": "widget-author"},
        "html_url": "https://github.com/acme/widget/pull/42#issuecomment-555",
        "created_at": "2026-10-01T10:00:00Z",
    }
    assert ga.signal_from_issue_comment("widget", ic) == {
        "source": "github_review",
        "kind": "pr_comment",
        "product": "widget",
        "stream": "widget#42",
        "actor": "widget-author",
        "body": "ship it",
        "refs": {"pr": 42, "url": "https://github.com/acme/widget/pull/42#issuecomment-555"},
        "meta": {},
        "produced_at": "2026-10-01T10:00:00Z",
        "dedup_key": "gh:widget:pr42:ic:555",
    }


def test_signal_from_issue_comment_on_an_issue_is_none():
    ic = {
        "id": 556,
        "issue_url": "https://api.github.com/repos/acme/widget/issues/43",
        "body": "same here",
        "user": {"login": "someone"},
        "html_url": "https://github.com/acme/widget/issues/43#issuecomment-556",
        "created_at": "2026-10-01T10:05:00Z",
    }
    assert ga.signal_from_issue_comment("widget", ic) is None


def test_signal_from_issue_comment_empty_body_is_none():
    ic = {"id": 1, "issue_url": "https://api.github.com/repos/acme/widget/issues/42",
          "body": "", "user": {"login": "a"}, "html_url": "", "created_at": ""}
    assert ga.signal_from_issue_comment("widget", ic) is None


def test_signal_from_issue_comment_no_number_in_url_is_none():
    ic = {"id": 1, "issue_url": "", "body": "x",
          "user": {"login": "a"}, "html_url": "", "created_at": ""}
    assert ga.signal_from_issue_comment("widget", ic) is None
