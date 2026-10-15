"""GitHub activity signal shaping, matching convivy-orient/github_capture.py's
pure transforms byte-for-byte (E25, issue_01M4ZB5C5ZX0MV206064MTRAE0).

Epic E25 moves GitHub activity capture from Orient's own job into the Bosun's
launchd jobs. Both writers run during the changeover and the Library
deduplicates on dedup_key, so every field here must match what Orient's
github_capture.py builds today — a differing field creates a duplicate row or
a mismatch in the read-back. These are pure functions only: no network, no
subprocess, no file IO. Nothing calls them yet; this module has no caller.

Four kinds: review_verdict, changelog, review_comment and pr_comment. Comments
on GitHub issues are deliberately not captured — work items live in the
Library's corrections queue, and GitHub issues are not a Convivy surface.
"""
from __future__ import annotations

_CHANGELOG_BODY_CLIP = 4000


def _pr_number_from_url(url: str) -> int | None:
    """…/pulls/123 or …/issues/123 → 123."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def signal_from_review(repo: str, pr_number: int, review: dict) -> dict | None:
    """A review VERDICT event. Empty-body COMMENTED reviews are shells wrapping
    inline comments (captured separately) — skip those; keep APPROVED /
    CHANGES_REQUESTED even with an empty body, because the verdict IS the signal."""
    state = (review.get("state") or "").upper()
    body = (review.get("body") or "").strip()
    if state == "COMMENTED" and not body:
        return None
    if not state:
        return None
    return {
        "source": "github_review",
        "kind": "review_verdict",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (review.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr": pr_number, "url": review.get("html_url") or ""},
        "meta": {"state": state},
        "produced_at": review.get("submitted_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:review:{review.get('id')}",
    }


def signal_from_merge(repo: str, pr: dict) -> dict | None:
    """A merged-PR event — the in-box changelog's raw material. The merge IS
    the record: title + description become the changelog entry's summary,
    produced_at is when the merge happened (not when it was captured).
    Unmerged PRs (open, closed-without-merge) return None — the changelog
    only ever shows what actually shipped."""
    merged_at = pr.get("merged_at")
    if not merged_at:
        return None
    number = pr.get("number")
    author = (pr.get("user") or {}).get("login") or "unknown"
    title = (pr.get("title") or "").strip()
    description = (pr.get("body") or "").strip()
    body = f"{title}\n\n{description}" if description else title
    if len(body) > _CHANGELOG_BODY_CLIP:
        body = body[:_CHANGELOG_BODY_CLIP].rstrip() + " …"
    return {
        "source": "github",
        "kind": "changelog",
        "stream": repo,
        "actor": author,
        "product": repo,
        "body": body,
        "refs": {"pr": number, "url": pr.get("html_url") or "",
                 "merged_at": merged_at, "author": author},
        "meta": {},
        "produced_at": merged_at,
        "dedup_key": f"gh:{repo}:pr{number}:merged",
    }


def signal_from_review_comment(repo: str, rc: dict) -> dict | None:
    """An inline review comment (repo-wide /pulls/comments endpoint)."""
    pr_number = _pr_number_from_url(rc.get("pull_request_url") or "")
    body = (rc.get("body") or "").strip()
    if pr_number is None or not body:
        return None
    refs = {"pr": pr_number, "url": rc.get("html_url") or ""}
    if rc.get("path"):
        refs["path"] = rc["path"]
    return {
        "source": "github_review",
        "kind": "review_comment",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (rc.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": refs,
        "meta": {},
        "produced_at": rc.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:rc:{rc.get('id')}",
    }


def signal_from_issue_comment(repo: str, ic: dict) -> dict | None:
    """A conversation comment (repo-wide /issues/comments endpoint — covers both
    PRs and issues; only PR comments are captured, selected by html_url)."""
    pr_number = _pr_number_from_url(ic.get("issue_url") or "")
    body = (ic.get("body") or "").strip()
    if pr_number is None or not body:
        return None
    if "/pull/" not in (ic.get("html_url") or ""):
        return None
    return {
        "source": "github_review",
        "kind": "pr_comment",
        "product": repo,
        "stream": f"{repo}#{pr_number}",
        "actor": (ic.get("user") or {}).get("login") or "unknown",
        "body": body,
        "refs": {"pr": pr_number, "url": ic.get("html_url") or ""},
        "meta": {},
        "produced_at": ic.get("created_at") or "",
        "dedup_key": f"gh:{repo}:pr{pr_number}:ic:{ic.get('id')}",
    }
