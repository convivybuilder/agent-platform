#!/bin/bash
# tests/bash/test_installer_test_inputs_coverage.sh — always runs (it is NOT
# one of the four files ci.yml can skip), so a hand-maintained pattern list
# can never go stale invisibly. It extracts every repo path the four
# installer test files reference literally as $REPO_ROOT/<path> — an
# INSTALLER=/AUTOPULL=/PLIST= assignment, a `cp "$REPO_ROOT/..."`, a
# `for src in "$REPO_ROOT"/...` — and fails, naming the file and the path,
# when tests/bash/installer-test-inputs.txt has no pattern matching it.
# ci.yml's `changes` job reads that same file to decide whether a PR needs
# the four heavy files; this test is what keeps the two readers honest with
# each other, on every run rather than only when someone remembers to check.
#
# WHAT THIS CANNOT SEE, by design (fuzzy tracing would just move the false
# confidence rather than remove it):
#   - A path an INSTALLER itself reads (bin/install-agent-platform-shared's
#     own call to bin/builder-root, say) rather than a path a TEST FILE
#     spells out directly. Those entries in installer-test-inputs.txt are a
#     judgment call recorded in its comments, not something extracted here.
#   - A path referenced only inside embedded interpreter code rather than a
#     shell-visible $REPO_ROOT/<path> string. test_install_run_register_api.sh
#     hands its python heredoc a bare $REPO_ROOT and the heredoc itself builds
#     Path(sys.argv[1]) / "bin" / "run-register-api" and globs
#     lib/agent-platform/*.py — none of that is a literal $REPO_ROOT/<path> in
#     the bash source, so it is not extracted here either; it is hand-listed
#     in installer-test-inputs.txt instead, with the reasoning recorded there.
#
# This test does not itself touch anything the four gated files exercise, so
# it carries none of their cost — it is pure text extraction and grep.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

PATTERNS="$HERE/installer-test-inputs.txt"
GATED="test_install_agent_platform_shared.sh test_install_run_register_api.sh test_install_new_plists.sh test_agent_platform_autopull.sh"

assert_eq "1" "$(test -r "$PATTERNS" && echo 1 || echo 0)" \
    "tests/bash/installer-test-inputs.txt exists and is readable"

active=$(mktemp)
grep -vE '^[[:space:]]*(#|$)' "$PATTERNS" > "$active" 2>/dev/null

# A PR that touches only this patterns file must still run the four tests
# whose skip it controls, rather than being graded against its own pre-edit
# version (security-reviewer, PR #571 round 2) — so the file must match its
# own repo-relative path.
assert_eq "1" "$(printf '%s\n' "tests/bash/installer-test-inputs.txt" | grep -qE -f "$active" && echo 1 || echo 0)" \
    "installer-test-inputs.txt has a pattern matching its own path"

# Extract every $REPO_ROOT/<path> (optionally with a quote between the
# variable and the slash, as in `"$REPO_ROOT"/launchd/*.plist`), strip the
# variable and any leading quote/slash, and keep only the relative path.
extracted=$(mktemp)
for name in $GATED; do
    f="$HERE/$name"
    grep -oE '\$REPO_ROOT"?/[A-Za-z0-9_./*-]+' "$f" 2>/dev/null | while IFS= read -r m; do
        p="${m#\$REPO_ROOT}"
        p="${p#\"}"
        p="${p#/}"
        printf '%s\t%s\n' "$name" "$p"
    done
done | sort -u > "$extracted"

assert_eq "1" "$([ -s "$extracted" ] && echo 1 || echo 0)" \
    "the extraction actually found \$REPO_ROOT references (a zero-hit extractor would pass vacuously)"

uncovered=0
while read -r name path; do
    [ -n "$path" ] || continue
    if ! printf '%s\n' "$path" | grep -qE -f "$active" 2>/dev/null; then
        echo "  FAIL: $name references '$path' (as \$REPO_ROOT/$path), but no pattern in tests/bash/installer-test-inputs.txt matches it"
        uncovered=$((uncovered + 1))
    fi
done < "$extracted"

assert_eq "0" "$uncovered" \
    "every \$REPO_ROOT path the four installer test files reference literally is covered by installer-test-inputs.txt"

rm -f "$active" "$extracted"
finish
