# Agent telemetry schema — moved

The run-directory contract now lives in the knowledge base:

**`agent-platform/contracts/run-dir-schema`** — `<root>/knowledge/projects/agent-platform/contracts/run-dir-schema.md`

Read that, not a copy. A repo-side duplicate of a KB doc goes stale and misleads
whoever reads it next, which is what happened to this file (ARCH-355).

## Why this is a stub rather than a deletion

Two claims in the old version were wrong in load-bearing ways, and both are worth
recognizing if you find them quoted elsewhere:

- **Run-id keyspace.** It described `run-<8-char-base32>` giving "~1.1T unique
  values." `new_run_id()` is `secrets.token_hex(4)` — hex, so 16⁸ ≈ 4.3 billion.
  (1.1T is 32⁸, the base32 figure, which is where the number came from.) The
  `telemetry.py` docstring carried the same error and was corrected alongside
  this stub.
- **Concurrency.** It described "the at-most-4 concurrent runs." The global
  ceiling has been 16 since ARCH-206; 4 is now only the per-team worker cap.

It was also silent on the local-day / UTC-timestamp split, on the `sub-*` and
`co-*` directory kinds sharing one namespace, on five `meta.json` fields, and on
half the checks `validate_run_dir` performs.

The KB doc covers all of it, and names the prefix filter that keeps a `co-*`
handoff directory out of the cost corpus.
