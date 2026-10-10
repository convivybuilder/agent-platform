---
name: product-manager
description: Sample fixture for interactive_policy_bench's tests.
tools: Read, Grep, Glob, Write, Task, Bash(agentctl session-dir:*), Bash(git rev-parse:*)
model: opus
---

Not the real individuated product-manager agent — that file lives outside
this repo at `~/Convivy/constance-commercial/.claude/agents/product-manager.md`,
a machine-specific path that does not exist in CI. This fixture carries the
same two `Bash(...)` grants so `policy_matcher.extract_allow_prefixes` and
`bench.build_scratch_repo` are exercised against realistic frontmatter shape
without depending on that path.
