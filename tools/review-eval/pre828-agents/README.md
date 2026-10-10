# Pinned reviewer/security-reviewer wording — pre knowledge#828

`reviewer.md` and `security-reviewer.md` here are frozen copies of
`projects/agent-platform/agents/<name>.md` from `convivy/knowledge` at commit
`c4ccad6433d3ce3a4488fc462c32642c4ecd94b7` — the wording in force immediately
before knowledge#828 (merged 2026-10-08) rewrote both definitions.

These files are never edited. They exist so `bin/review-eval`'s
`sonnet-pre828` arm can replay the corpus against the OLD reviewer wording,
for comparison against `tools/review-eval/post828-agents` (the wording
after knowledge#828). Resolved via `agentctl spawn reviewer --agents-dir
tools/review-eval/pre828-agents --eval-mode ...`, the same `--agents-dir`
mechanism `tools/review-eval/variant-agents` uses.

If a future change needs the wording at a different commit, add a new
directory rather than editing these — the arm names (`sonnet-pre828`,
`sonnet-post828`) and this directory's whole reason for existing are tied to
this exact commit.
