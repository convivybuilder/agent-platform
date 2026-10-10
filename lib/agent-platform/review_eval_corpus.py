"""The review-replay eval harness's corpus builder (agent-platform#807).

Builds the answer-keyed sample of past reviews a candidate model is replayed
against. Two kinds, both drawn from real dispatch run records
(`<runs_root>/<date>/run-*/meta.json`), never invented:

  CONFIRMED — a REQUEST_CHANGES at commit S on a PR, where the same role's
    next review of that PR, at a DIFFERENT head, is APPROVE. The REQUEST_CHANGES
    review's findings are the answer key: a later head fixed them, so they were
    real.
  CLEAN     — an APPROVE at commit S where S is the PR's head when it merged,
    and no later review of that PR (any role) found anything. A "nothing to fix
    here" round, used to measure how much a candidate invents on a sound diff.

`extract_findings` turns a review's prose into a short answer-key list. The
one callable in this module that calls a model; every selection function above
it is pure and network-free, so corpus SELECTION is fully testable on synthetic
run records (see tests/test_review_eval_corpus.py) without ever reaching one.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import dispatch_env as DE
from review_protocol import REVIEW_AGENTS, _review_base, _sha_match

DEFAULT_SINCE_DATE = "2026-09-20"
DEFAULT_SAMPLE_SIZE = 50


@dataclass
class ReviewRecord:
    """One review-class dispatch, as read off its own run directory. Carries
    only what corpus selection needs — not the full meta.json."""
    run_id: str
    run_dir: Path
    repo: str
    pr: str
    role: str  # the role BASE (reviewer / security-reviewer), crew prefix stripped
    sha: str
    verdict: str
    started_at: str


@dataclass
class CorpusEntry:
    kind: str  # "confirmed" | "clean"
    run_id: str
    repo: str
    pr: str
    role: str
    sha: str
    system_prompt_path: str
    user_prompt_path: str
    answer_key: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0

    @property
    def entry_id(self) -> str:
        """Stable id for resumability keys: a corpus entry is uniquely a
        (repo, pr, role, sha) tuple, which is also what a replay is pinned to —
        two entries at the same commit would replay identically, so the id
        deliberately collapses them rather than disambiguating by run_id."""
        return f"{self.repo}/{self.pr}/{self.role}/{self.sha[:12]}"

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind, "run_id": self.run_id, "repo": self.repo,
            "pr": self.pr, "role": self.role, "sha": self.sha,
            "system_prompt_path": self.system_prompt_path,
            "user_prompt_path": self.user_prompt_path,
            "answer_key": list(self.answer_key),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "usd": self.usd,
        }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def scan_review_records(runs_root: Path, *, since_date: str = DEFAULT_SINCE_DATE
                        ) -> list[ReviewRecord]:
    """Every review-class run record (reviewer/security-reviewer, any crew
    prefix) under `runs_root`, from `since_date` (inclusive, `YYYY-MM-DD`,
    compared lexicographically against the date directory name) on, that
    carries a `review_verdict`, a `pr` and a `review_sha`. A record missing
    any of the three was never posted (or predates the posted-record fields)
    and cannot anchor a corpus entry either way."""
    out: list[ReviewRecord] = []
    if not runs_root.is_dir():
        return out
    for date_dir in sorted(runs_root.glob("20*-*-*/")):
        if date_dir.name < since_date:
            continue
        for run_dir in sorted(date_dir.glob("run-*/")):
            meta = _read_json(run_dir / "meta.json")
            if not meta:
                continue
            agent = meta.get("agent") or ""
            role = _review_base(agent)
            if role not in REVIEW_AGENTS:
                continue
            verdict = meta.get("review_verdict") or ""
            pr = meta.get("review_pr") or meta.get("pr")
            sha = meta.get("review_sha") or ""
            repo = meta.get("repo") or ""
            if not (verdict and pr and sha and repo):
                continue
            out.append(ReviewRecord(
                run_id=meta.get("run_id") or run_dir.name, run_dir=run_dir,
                repo=repo, pr=str(pr), role=role, sha=sha, verdict=verdict,
                started_at=meta.get("started_at") or ""))
    return out


def _group_by_pr_role(records: list[ReviewRecord]) -> dict[tuple[str, str, str], list[ReviewRecord]]:
    groups: dict[tuple[str, str, str], list[ReviewRecord]] = {}
    for r in records:
        groups.setdefault((r.repo, r.pr, r.role), []).append(r)
    for g in groups.values():
        g.sort(key=lambda r: r.started_at)
    return groups


def find_confirmed_rounds(records: list[ReviewRecord]) -> list[ReviewRecord]:
    """A REQUEST_CHANGES at sha S whose role's NEXT review of that PR, at a
    different head, is APPROVE. Excludes a REQUEST_CHANGES with no later
    review at all, and an APPROVE that repeats the same sha (nothing changed,
    so nothing was confirmed fixed)."""
    out: list[ReviewRecord] = []
    for group in _group_by_pr_role(records).values():
        for i in range(len(group) - 1):
            cur, nxt = group[i], group[i + 1]
            if (cur.verdict == "REQUEST_CHANGES" and nxt.verdict == "APPROVE"
                    and nxt.sha != cur.sha):
                out.append(cur)
    return out


def find_clean_rounds(records: list[ReviewRecord], *,
                      merged_head: Callable[[str, str], str | None]
                      ) -> list[ReviewRecord]:
    """An APPROVE at sha S where S is the PR's head when it merged (per
    `merged_head(repo, pr)`, which returns the merged head sha or None for an
    unmerged/unknown PR), and no later review of that PR — any role — raised a
    REQUEST_CHANGES. `merged_head` is injected so selection stays network-free
    and testable; the real implementation shells out to `gh`."""
    out: list[ReviewRecord] = []
    by_pr: dict[tuple[str, str], list[ReviewRecord]] = {}
    for r in records:
        by_pr.setdefault((r.repo, r.pr), []).append(r)
    for (repo, pr), pr_records in by_pr.items():
        head = merged_head(repo, pr)
        if not head:
            continue
        pr_records_sorted = sorted(pr_records, key=lambda r: r.started_at)
        for r in pr_records_sorted:
            if r.verdict != "APPROVE" or not _sha_match(r.sha, head):
                continue
            later_request_changes = any(
                other.started_at > r.started_at and other.verdict == "REQUEST_CHANGES"
                for other in pr_records_sorted)
            if not later_request_changes:
                out.append(r)
    return out


def sample_deterministic(items: list, k: int, seed: int) -> list:
    """`k` items from `items`, deterministic under `seed`, mixed rather than
    clustered by whatever order `items` arrived in (repo/role tend to cluster
    contiguously since callers build `items` by scanning one repo/date-range
    at a time). A seeded Fisher-Yates-via-`random.sample` shuffle, not a
    stratified quota — "mixed across repos and roles" is a sampling-bias
    concern, not a guaranteed-representation one, and a quota would need to
    decide proportions this harness has no basis for."""
    import random
    if k >= len(items):
        return list(items)
    return random.Random(seed).sample(items, k)


_DEFAULT_EXTRACT_PROMPT = (
    "Extract each distinct finding from this code review as a short, "
    "self-contained statement (one sentence, no severity tag, no file:line "
    "unless load-bearing to the statement). Reply with a JSON array of "
    "strings and nothing else. An explicit \"no findings\" review is an "
    "empty array.\n\nReview:\n\n{body}"
)


def default_finding_extractor(review_body: str, *, model: str = "claude-haiku-4-5"
                              ) -> list[str]:
    """Calls the live `claude` CLI once to turn a review's prose into a short
    answer-key list. THE ONE MODEL CALL IN THIS MODULE — every caller above it
    is pure. Never invoked by this repo's own test suite (tests inject a stub
    extractor); the co runs the real `corpus build` command, which uses this
    by default, after review."""
    prompt = _DEFAULT_EXTRACT_PROMPT.format(body=review_body)
    # No explicit env means a bare inherit, which carries the launching session's tmux
    # identity and the operator's git/gh credential helpers straight through —
    # `dispatch_env()` strips/redirects both (CQ·418 round 4).
    result = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json", prompt],
        capture_output=True, text=True, timeout=120, env=DE.dispatch_env())
    if result.returncode != 0:
        raise RuntimeError(f"finding extraction failed (exit {result.returncode}): "
                           f"{result.stderr.strip()}")
    payload = json.loads(result.stdout)
    text = payload.get("result") if isinstance(payload, dict) else None
    if text is None:
        text = result.stdout
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"finding extraction returned no JSON array: {text!r}")
    findings = json.loads(match.group(0))
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        raise RuntimeError(f"finding extraction returned a malformed list: {findings!r}")
    return findings


def _review_body_for(run_dir: Path) -> str | None:
    """The review's prose, from whichever file this run actually wrote.
    `review-post` (`_persist_review_before_wait`, ARCH-397) writes
    `unposted-review.md` as a pre-wait safety copy on every real (non-dry-run)
    call, posted or not — so it is present and carries the full body for
    essentially every corpus source run, not only one whose post failed.
    `eval-review.json`'s `body` field is the fallback for an eval replay's own
    run directory (a later corpus build over harness output, not a real
    corpus source run, but harmless to support the same way)."""
    unposted = run_dir / "unposted-review.md"
    if unposted.is_file():
        text = unposted.read_text()
        # Strip the rescue marker/preamble so the answer-key extractor sees the
        # same prose a GitHub reader would — the three lines above "Written by
        # review-post itself..." are bookkeeping, not review content.
        marker = "\n\n**Comment by"
        idx = text.find(marker)
        return text[idx + 2:] if idx != -1 else text
    eval_artifact = _read_json(run_dir / "eval-review.json")
    if eval_artifact:
        return eval_artifact.get("body")
    return None


def build_corpus_entry(record: ReviewRecord, *, kind: str,
                       extractor: Callable[[str], list[str]] = default_finding_extractor
                       ) -> CorpusEntry | None:
    """One `CorpusEntry` from a `ReviewRecord`, or None when the run directory
    is missing a piece this needs (the prose body, or the prompt files every
    `agentctl spawn` run writes under `prompts/`) — a corpus build skips such
    a record rather than guessing at its content."""
    system_path = record.run_dir / "prompts" / "system.md"
    user_path = record.run_dir / "prompts" / "user.md"
    if not (system_path.is_file() and user_path.is_file()):
        return None
    body = _review_body_for(record.run_dir)
    if body is None:
        return None
    answer_key = extractor(body) if kind == "confirmed" else []
    cost = _read_json(record.run_dir / "cost.json") or {}
    return CorpusEntry(
        kind=kind, run_id=record.run_id, repo=record.repo, pr=record.pr,
        role=record.role, sha=record.sha,
        system_prompt_path=str(system_path), user_prompt_path=str(user_path),
        answer_key=answer_key,
        tokens_in=int(cost.get("tokens_in") or 0),
        tokens_out=int(cost.get("tokens_out") or 0),
        usd=float(cost.get("usd") or 0.0))


def select_baseline_subset(corpus: dict, *, n_confirmed: int = 15, n_clean: int = 15,
                           seed: int) -> dict:
    """The fixed subset every Anthropic baseline and every gateway candidate's
    "restricted to the same subset" number runs on — 15 CONFIRMED + 15 CLEAN
    entries by default, sampled deterministically from `seed` out of the full
    corpus. A model set that defaults to the full corpus (deepseek) still
    gets this subset computed once, for the side-by-side comparison; it is
    not what deepseek's own "full corpus" numbers run on."""
    confirmed = sample_deterministic(corpus["confirmed"], n_confirmed, seed)
    clean = sample_deterministic(corpus["clean"], n_clean, seed + 1)
    return {**corpus, "confirmed": confirmed, "clean": clean}


def build_corpus(runs_root: Path, *, since_date: str = DEFAULT_SINCE_DATE,
                 sample_size: int = DEFAULT_SAMPLE_SIZE, seed: int,
                 merged_head: Callable[[str, str], str | None],
                 extractor: Callable[[str], list[str]] = default_finding_extractor
                 ) -> dict:
    """The full corpus: up to `sample_size` CONFIRMED entries and up to
    `sample_size` CLEAN entries, each sampled deterministically from `seed`.
    Returns the JSON-ready dict `corpus build` writes to disk."""
    records = scan_review_records(runs_root, since_date=since_date)
    confirmed_records = sample_deterministic(
        find_confirmed_rounds(records), sample_size, seed)
    clean_records = sample_deterministic(
        find_clean_rounds(records, merged_head=merged_head), sample_size, seed + 1)

    confirmed = [e for r in confirmed_records
                if (e := build_corpus_entry(r, kind="confirmed", extractor=extractor))]
    clean = [e for r in clean_records
            if (e := build_corpus_entry(r, kind="clean", extractor=extractor))]
    return {
        "since_date": since_date, "seed": seed, "sample_size": sample_size,
        "confirmed": [e.as_dict() for e in confirmed],
        "clean": [e.as_dict() for e in clean],
    }
