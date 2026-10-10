#!/bin/bash
# Tests for bin/issue-drain's review-round cap wording.
#
# The cap used to be hand-written as "5-round cap" three times in the brief
# (platform/decisions/0083 raised it 3 -> 5 and needed the same hand edit; the operator
# raised it again 5 -> 7 on 2026-10-01, sig_01M3W9600TWE8SVKQNXX6G282A). It now
# reads `review_rounds_code` and `review_rounds_per_role` from the policy file at
# the knowledge repo's origin/main (agent_policy.load), falling back to
# agent_policy.FALLBACK_REVIEW_ROUNDS_CODE and FALLBACK_REVIEW_ROUNDS_PER_ROLE
# when the file or a value cannot be read.
#
# issue-drain's whole job past that point is `exec co planning-orchestrator
# "$prompt"`, and `root` (the product checkout's location) comes from
# `builder-root --root`, a TRUST ANCHOR that reads the real account's
# ~/.config/convivy/session-agent.env and ignores BUILDER_ROOT — so it cannot
# be pointed at a fixture the way `registry.KNOWLEDGE_REPO` can. Rather than
# exec `co` or touch the real session-agent.env, these tests extract the
# self-contained cap-computation + prompt-assignment block (from `data_root=`
# through the end of the `prompt=` string) and run just that under a
# BUILDER_ROOT fixture — the same technique test_builder_root_shell.sh uses to
# exercise install-decision-hooks' resolution lines without running the whole
# installer.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

git_c() { git -C "$1" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "${@:2}" >/dev/null; }

SHIPPED_POLICY="$REPO_ROOT/tests/fixtures/agent-policy-6fe9537.yaml"

# make_knowledge <builder_root_dir> [review_rounds_per_role] [review_rounds_code]
# Builds <dir>/knowledge as a git repo whose origin/main holds the shipped
# policy file with review_rounds_per_role replaced, and review_rounds_code
# appended when given (the shipped file has no such line). With no second
# argument there is no policy file at all, so AP.load falls back.
make_knowledge() {
    local kroot="$1/knowledge"
    mkdir -p "$kroot"
    git_c "$kroot" init -q -b main
    if [ -n "${2:-}" ]; then
        mkdir -p "$kroot/projects/platform/policy"
        sed -E "s/^review_rounds_per_role: .*/review_rounds_per_role: $2/" \
            "$SHIPPED_POLICY" > "$kroot/projects/platform/policy/agent-policy.yaml"
        if [ -n "${3:-}" ]; then
            echo "review_rounds_code: $3" >> "$kroot/projects/platform/policy/agent-policy.yaml"
        fi
    else
        echo "no policy here" > "$kroot/README.md"
    fi
    git_c "$kroot" add -A
    git_c "$kroot" commit -q -m knowledge
    git_c "$kroot" update-ref refs/remotes/origin/main HEAD
}

EXTRACT="$(mktemp -d)/block.sh"
sed -n '/^data_root=/,/^When every open issue is worked/p' "$REPO_ROOT/bin/issue-drain" > "$EXTRACT"
echo 'echo "$prompt"' >> "$EXTRACT"

# brief_for <builder_root_dir>: the prompt issue-drain would build, with
# $_self_path pointed at the real bin/issue-drain (so the extracted block's
# sibling lookups — ../builder-root, ../lib/agent-platform — resolve exactly
# as they do in the real script) and $BUILDER_ROOT pointed at the fixture.
brief_for() {
    BUILDER_ROOT="$1" _self_path="$REPO_ROOT/bin/issue-drain" product=vivy \
        /bin/bash "$EXTRACT" 2>/dev/null
}

# The numbers come from agent_policy itself, so a change to the module's constants
# cannot leave this test pinning stale literals.
ap_value() { PYTHONPATH="$REPO_ROOT/lib/agent-platform" /usr/bin/python3 -c "import agent_policy as AP; print(AP.$1)"; }
FB_CODE=$(ap_value FALLBACK_REVIEW_ROUNDS_CODE)
FB_DOC=$(ap_value FALLBACK_REVIEW_ROUNDS_PER_ROLE)
DECIDED_CODE=$(ap_value DECIDED_REVIEW_ROUNDS_CODE)

# expected_cap_sentence <code> <document>: the sentence the brief must carry when the
# code cap exceeds the document cap. The wording is pinned here on purpose; only the
# numbers are derived.
expected_cap_sentence() {
    printf 'The review-round cap is %s rounds for a pull request that changes code or any file an agent is instructed to act on, and %s rounds for one that changes only documents. A pull request that changes code or any file an agent is instructed to act on runs rounds past %s only while no finding in the latest round repeats one an earlier round raised.' "$1" "$2" "$2"
}

# expected_cap_sentence_without_progress <code> <document>: the form for equal caps (the
# fallback). No round runs past the document cap, so the progress clause is not stated.
expected_cap_sentence_without_progress() {
    printf 'The review-round cap is %s rounds for a pull request that changes code or any file an agent is instructed to act on, and %s rounds for one that changes only documents.' "$1" "$2"
}

# The sentence the brief must carry, with the numbers the test chose. Code 13 and
# document 9 are neither the fallbacks nor the decided code cap (11), nor the
# 2026-10-01 document raise (7), so this cannot pass by coincidence.
CAP_SENTENCE_13_9=$(expected_cap_sentence 13 9)
CAP_SENTENCE_FALLBACK=$(expected_cap_sentence_without_progress "$FB_CODE" "$FB_DOC")

echo "== the brief states both policy caps, and which pull request takes each =="
d9=$(mktemp -d)
make_knowledge "$d9" 9 13
task=$(brief_for "$d9")
assert_eq 1 "$(printf '%s' "$task" | grep -c 'review-round cap is' | tr -d ' ')" \
    "the cap sentence appears once, in the auto-cycle step"
assert_contains "$task" "$CAP_SENTENCE_13_9" "the brief states the code cap (13) and the document cap (9) with the rule"
assert_contains "$task" "A finding that repeats one an earlier round raised, in a round past the document cap, ends the cycle: a headless run dispatches no further round, leaves the PR pr-open, and records the repeat on the issue thread." \
    "the auto-cycle step says what a headless run does with a repeat, by the document cap, not a round number"
case "$task" in
    *"9-round cap"*|*"5-round cap"*) _FAIL=$((_FAIL + 1)); echo "  FAIL: a superseded single-number cap is still in the brief" ;;
    *) _PASS=$((_PASS + 1)) ;;
esac

echo "== a policy file without review_rounds_code states the code fallback beside its document cap =="
dnocode=$(mktemp -d)
make_knowledge "$dnocode" 9
task=$(brief_for "$dnocode")
assert_contains "$task" "$(expected_cap_sentence "$DECIDED_CODE" 9)" \
    "a readable file with no code line states the decided code cap (11) and the document cap (9)"

echo "== the brief falls back to the FALLBACK constants when the policy is unreadable =="
dmissing=$(mktemp -d)
make_knowledge "$dmissing"   # no review_rounds_per_role arg -> no policy file at all
task=$(brief_for "$dmissing")
# The fallback sentence is a prefix of the full form, so it is matched with the step 6 text
# that follows it: the progress form continues "documents. A pull request", not "A finding".
assert_contains "$task" "$CAP_SENTENCE_FALLBACK A finding that repeats" "the brief states the code fallback and the document fallback (both from agent_policy)"
# The progress clause must be absent on its own.
case "$task" in
    *"runs rounds past"*) _FAIL=$((_FAIL + 1)); echo "  FAIL: the fallback brief states a progress clause past the document cap" ;;
    *) _PASS=$((_PASS + 1)) ;;
esac

# --- the two failure exits in the extracted block (reviewer, PR #745 round 1,
# PR comment 5938078083) -------------------------------------------------------
#
# Both stubs replace the sibling `builder-root` the extracted block resolves
# via `$(dirname "$_self_path")/builder-root`, the same sibling-lookup trick
# `brief_for` above uses to reach the real one. `make_knowledge`'s fixture
# cannot exercise either: it only controls what the real, always-succeeding
# `builder-root` reports once called, not a failure of the call itself.

echo "== bin/builder-root (data root) failing makes issue-drain exit 1, no brief =="
STUB_FAIL=$(mktemp -d)
cat > "$STUB_FAIL/builder-root" << 'EOF'
#!/bin/bash
echo "stub builder-root: forced failure" >&2
exit 1
EOF
chmod +x "$STUB_FAIL/builder-root"
out=$(_self_path="$STUB_FAIL/issue-drain" product=vivy /bin/bash "$EXTRACT" 2>&1)
rc=$?
assert_eq 1 "$rc" "issue-drain exits 1 when bin/builder-root (data root) fails"
assert_contains "$out" "issue-drain: cannot resolve the machine's data root (bin/builder-root)" \
    "and says why"
case "$out" in
    *"Task: Drain the in-box issue queue"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: a brief was produced despite bin/builder-root failing"
        ;;
    *) _PASS=$((_PASS + 1)) ;;
esac

echo "== the review-round cap read failing makes issue-drain exit 1, no brief =="
STUB_BAD_CAP=$(mktemp -d)
mkdir -p "$STUB_BAD_CAP/sub"
cat > "$STUB_BAD_CAP/sub/builder-root" << 'EOF'
#!/bin/bash
# The extracted block's only no-arg call is the data-root one; succeed with a
# fixed path so the python3 cap read below is what's under test.
echo "/nonexistent-data-root"
EOF
chmod +x "$STUB_BAD_CAP/sub/builder-root"
# _self_path's dirname is $STUB_BAD_CAP/sub, so the block's PYTHONPATH resolves
# to $STUB_BAD_CAP/lib/agent-platform — never created here, so `import
# agent_policy` fails and python3 exits non-zero, exercising the same cap-read
# failure branch a broken or unreadable agent_policy.py would.
out=$(_self_path="$STUB_BAD_CAP/sub/issue-drain" product=vivy /bin/bash "$EXTRACT" 2>&1)
rc=$?
assert_eq 1 "$rc" "issue-drain exits 1 when the review-round cap cannot be read"
assert_contains "$out" "issue-drain: cannot read the review-round cap from the policy file" \
    "and says why"
case "$out" in
    *"Task: Drain the in-box issue queue"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: a brief was produced despite the cap read failing"
        ;;
    *) _PASS=$((_PASS + 1)) ;;
esac

rm -rf "$d9" "$dnocode" "$dmissing" "$STUB_FAIL" "$STUB_BAD_CAP" "$(dirname "$EXTRACT")"
finish
