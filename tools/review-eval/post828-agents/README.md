# Pinned reviewer/security-reviewer wording — post knowledge#828

`reviewer.md` and `security-reviewer.md` here are frozen copies of
`projects/agent-platform/agents/<name>.md` from `convivy/knowledge` at commit
`7f9a8c2df20772611ac9b41e81b633c28a26620f` — the wording knowledge#828
(merged 2026-10-08) rewrote both definitions to: it removes the "look
again" skepticism push, requires a named occurrence for a requested guard,
and stops a settled review point from being re-raised.

These files are never edited. They exist so `bin/review-eval`'s
`sonnet-post828` arm can replay the corpus against this wording, for
comparison against `tools/review-eval/pre828-agents` (the wording before
knowledge#828). Resolved via `agentctl spawn reviewer --agents-dir
tools/review-eval/post828-agents --eval-mode ...`, the same `--agents-dir`
mechanism `tools/review-eval/variant-agents` uses.

If a future change needs the wording at a different commit, add a new
directory rather than editing these — the arm names (`sonnet-pre828`,
`sonnet-post828`) and this directory's whole reason for existing are tied to
this exact commit.
