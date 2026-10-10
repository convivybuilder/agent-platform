# Pinned reviewer/security-reviewer wording — knowledge#832

`reviewer.md` and `security-reviewer.md` here are frozen copies of
`projects/agent-platform/agents/<name>.md` from `convivy/knowledge` at commit
`9a57c465b4d1335d79375d944c7921945ed97a1a` (knowledge#832, merged) — the
wording that adds the paragraph stating a defect with a concrete way it goes
wrong is a finding at its real severity, and that the occurrence bar
(knowledge#828) covers only a proposed guard, not an existing defect.

These files are never edited. They exist so `bin/review-eval`'s
`sonnet-fix832` arm can replay the corpus against this wording, for
comparison against `tools/review-eval/post828-agents` (the wording
knowledge#832 amends). Resolved via `agentctl spawn reviewer --agents-dir
tools/review-eval/fix832-agents --eval-mode ...`, the same `--agents-dir`
mechanism `tools/review-eval/variant-agents` uses.

If a future change needs the wording at a different commit, add a new
directory rather than editing these — the arm name (`sonnet-fix832`) and
this directory's whole reason for existing are tied to this exact commit.
