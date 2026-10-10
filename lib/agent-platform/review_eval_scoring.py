"""The review-replay eval harness's scoring (agent-platform#807).

Every judgment call here — matching a candidate finding to the answer key,
and classifying an unmatched finding as real or not against the diff — is
made by an injected callable (`match_fn` / `judge_fn`), never by this module
directly. That keeps every function here pure and network-free, so scoring
MECHANICS (recall arithmetic, the noise-floor band, report assembly) are
fully testable with a stubbed judge; only the judge prompts themselves need a
live model, and only the co runs those, after review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MatchResult:
    candidate: str
    matched_key_index: int | None  # index into the answer_key list, or None
    reason: str


@dataclass
class JudgedFinding:
    finding: str
    real: bool
    reason: str


@dataclass
class EntryScore:
    entry_id: str
    matches: list[MatchResult] = field(default_factory=list)
    judged: list[JudgedFinding] = field(default_factory=list)
    answer_key_size: int = 0

    @property
    def matched_key_indices(self) -> set[int]:
        return {m.matched_key_index for m in self.matches if m.matched_key_index is not None}

    @property
    def recall(self) -> float | None:
        """Fraction of the answer key matched by at least one candidate
        finding. None (not zero) when the answer key is empty — a CLEAN
        round's recall is undefined, not 0/0 rounded down, so a report can
        tell "no key to recall" apart from "missed everything"."""
        if self.answer_key_size == 0:
            return None
        return len(self.matched_key_indices) / self.answer_key_size

    @property
    def unmatched_findings(self) -> list[str]:
        return [m.candidate for m in self.matches if m.matched_key_index is None]

    @property
    def judged_real_count(self) -> int:
        return sum(1 for j in self.judged if j.real)

    @property
    def judged_false_count(self) -> int:
        return sum(1 for j in self.judged if not j.real)


def match_findings(candidate_findings: list[str], answer_key: list[str], *,
                   match_fn: Callable[[str, list[str]], tuple[int | None, str]]
                   ) -> list[MatchResult]:
    """Match each candidate finding to at most one answer-key item.
    `match_fn(candidate, answer_key) -> (matched_index_or_None, reason)` is
    the Anthropic-judge call in production; tests pass a stub. Every decision
    and its one-line reason is kept in the returned `MatchResult`s — the
    harness's audit requirement — never just a boolean."""
    out = []
    for candidate in candidate_findings:
        idx, reason = match_fn(candidate, answer_key)
        out.append(MatchResult(candidate=candidate, matched_key_index=idx, reason=reason))
    return out


def judge_unmatched(unmatched: list[str], diff_text: str, *,
                    judge_fn: Callable[[str, str], tuple[bool, str]]
                    ) -> list[JudgedFinding]:
    """Classify every unmatched finding as real or not against the diff.
    `judge_fn(finding, diff_text) -> (is_real, reason)` is the Opus-judge call
    in production (claude-opus-5-5, on the subscription); tests pass a stub.
    Every unmatched finding is judged — on CONFIRMED rounds and CLEAN rounds
    alike, per the harness's spec: a finding matching nothing in the key is
    not automatically false."""
    out = []
    for finding in unmatched:
        is_real, reason = judge_fn(finding, diff_text)
        out.append(JudgedFinding(finding=finding, real=is_real, reason=reason))
    return out


def score_entry(entry_id: str, candidate_findings: list[str], answer_key: list[str],
                diff_text: str, *,
                match_fn: Callable[[str, list[str]], tuple[int | None, str]],
                judge_fn: Callable[[str, str], tuple[bool, str]]) -> EntryScore:
    """The full per-entry score: match every candidate finding against the
    key, then judge every finding that matched nothing."""
    matches = match_findings(candidate_findings, answer_key, match_fn=match_fn)
    score = EntryScore(entry_id=entry_id, matches=matches, answer_key_size=len(answer_key))
    score.judged = judge_unmatched(score.unmatched_findings, diff_text, judge_fn=judge_fn)
    return score


def aggregate_recall(scores: list[EntryScore]) -> float | None:
    """Mean recall over every score that HAS a defined recall (answer_key_size
    > 0) — a batch of CLEAN-round scores (no key) averages to None rather
    than silently treating "no key" as 0 and dragging recall down."""
    defined = [s.recall for s in scores if s.recall is not None]
    if not defined:
        return None
    return sum(defined) / len(defined)


def noise_floor_band(recalls: list[float]) -> dict:
    """The tie-breaking band from a set of recall numbers meant to measure
    the SAME thing twice: sonnet-5's two independent replays scored against
    the key, and each replay scored against the other. `recalls` is whatever
    the caller assembled (typically [recall(run_a, key), recall(run_b, key),
    recall(run_a, run_b)]) — this function only computes the spread, so the
    assembly stays visible at the call site rather than hidden in here."""
    return {"low": min(recalls), "high": max(recalls),
           "mid": sum(recalls) / len(recalls)}


def within_band(value: float, band: dict, *, slack: float = 0.0) -> bool:
    """Whether `value` counts as TIED with the noise floor — inside
    `[low - slack, high + slack]`. `slack` defaults to 0 (the band's own
    measured spread, no extra grace) and exists so a caller can widen it by a
    stated amount rather than silently redefining the band."""
    return band["low"] - slack <= value <= band["high"] + slack


def verdict_agreement(candidate_verdict: str | None, original_verdict: str) -> bool | None:
    """Whether a replay's verdict matches the original run's. None when the
    candidate produced no parseable verdict at all (a looped/errored/capped
    run) — not counted as either agreement or disagreement."""
    if candidate_verdict is None:
        return None
    return candidate_verdict == original_verdict


@dataclass
class ModelReport:
    """One model's numbers over a set of rounds — the harness's three
    required-per-round-and-in-total figures, plus cost/ops accounting."""
    model_key: str
    recall_confirmed: float | None
    judged_real_extra: int
    judged_false_extra: int
    verdict_agreement_rate: float | None
    usd_total: float
    tokens_in_total: int
    tokens_out_total: int
    runs_total: int
    runs_errored: int
    runs_not_run: int

    def as_dict(self) -> dict:
        return {
            "model": self.model_key,
            "recall_confirmed": self.recall_confirmed,
            "judged_real_extra": self.judged_real_extra,
            "judged_false_extra": self.judged_false_extra,
            "verdict_agreement_rate": self.verdict_agreement_rate,
            "usd_total": self.usd_total,
            "tokens_in_total": self.tokens_in_total,
            "tokens_out_total": self.tokens_out_total,
            "runs_total": self.runs_total,
            "runs_errored": self.runs_errored,
            "runs_not_run": self.runs_not_run,
        }


def build_model_report(model_key: str, confirmed_scores: list[EntryScore],
                       verdict_agreements: list[bool | None],
                       replay_results: list[dict]) -> ModelReport:
    """One `ModelReport` from a model's scored CONFIRMED entries, its
    per-entry verdict agreements, and its raw replay result dicts (for cost
    and run-health accounting). A failed replay scores zero recall for its
    own entry (an empty-findings `EntryScore` already does that) and its cost
    still counts — callers must include a failed run's `EntryScore` in
    `confirmed_scores`, not drop it."""
    defined_agreements = [a for a in verdict_agreements if a is not None]
    agreement_rate = (sum(defined_agreements) / len(defined_agreements)
                      if defined_agreements else None)
    return ModelReport(
        model_key=model_key,
        recall_confirmed=aggregate_recall(confirmed_scores),
        judged_real_extra=sum(s.judged_real_count for s in confirmed_scores),
        judged_false_extra=sum(s.judged_false_count for s in confirmed_scores),
        verdict_agreement_rate=agreement_rate,
        usd_total=sum(r.get("usd") or 0.0 for r in replay_results),
        tokens_in_total=sum(r.get("tokens_in") or 0 for r in replay_results),
        tokens_out_total=sum(r.get("tokens_out") or 0 for r in replay_results),
        runs_total=len(replay_results),
        runs_errored=sum(1 for r in replay_results if r.get("error") and not r.get("not_run")),
        runs_not_run=sum(1 for r in replay_results if r.get("not_run")),
    )


def render_markdown_table(reports: list[ModelReport]) -> str:
    header = ("| model | recall (confirmed) | judged-real extra | judged-false extra | "
             "verdict agreement | $ total | runs | errored | not run |")
    sep = "|" + "---|" * 9
    rows = [header, sep]
    for r in reports:
        recall = f"{r.recall_confirmed:.0%}" if r.recall_confirmed is not None else "—"
        agree = f"{r.verdict_agreement_rate:.0%}" if r.verdict_agreement_rate is not None else "—"
        rows.append(f"| {r.model_key} | {recall} | {r.judged_real_extra} | "
                    f"{r.judged_false_extra} | {agree} | ${r.usd_total:.2f} | "
                    f"{r.runs_total} | {r.runs_errored} | {r.runs_not_run} |")
    return "\n".join(rows)


def sample_for_audit(judged: list[tuple[str, str, JudgedFinding, str]], *, n: int = 10,
                     seed: int) -> dict:
    """A random, deterministic sample of up to `n` judged-real and `n`
    judged-false findings, with the diff hunk and the judge's reason, for a
    person to check by hand. `judged` is `(model_key, entry_id, judged_finding,
    diff_text)` tuples gathered across every model and entry that was judged —
    assembly stays at the call site, same as `noise_floor_band`."""
    import random
    real = [t for t in judged if t[2].real]
    false = [t for t in judged if not t[2].real]
    rnd = random.Random(seed)

    def _sample(items):
        return items if len(items) <= n else rnd.sample(items, n)

    def _render(items):
        return [{"model": model, "entry_id": entry_id, "finding": jf.finding,
                "reason": jf.reason, "diff_hunk": diff_text}
               for model, entry_id, jf, diff_text in items]

    return {"judged_real": _render(_sample(real)), "judged_false": _render(_sample(false))}


def build_report(reports: list[ModelReport]) -> tuple[str, dict]:
    """The harness's final output: a markdown table and the same data as a
    JSON-ready dict."""
    markdown = render_markdown_table(reports)
    payload = {"models": [r.as_dict() for r in reports]}
    return markdown, payload
