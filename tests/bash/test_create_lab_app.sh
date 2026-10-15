#!/bin/bash
# Tests for bin/create-lab-app — the handoff-skill scaffolding (ARCH-266
# follow-up): every lab product must get /handoff-leave + /handoff-pickup so a
# co session can span days. Runs the real scaffold under a fake $HOME so it
# writes into a temp dir, never the real ~/Convivy.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CLA="$REPO_ROOT/bin/create-lab-app"

# --- dry-run lists both handoff skills (no fs writes) ----------------------
out=$(/bin/bash "$CLA" testapp --prefix tt --dry-run 2>&1)
echo "$out" | grep -q '.claude/skills/handoff-leave/SKILL.md'
assert_rc 0 "$?" "dry-run lists handoff-leave skill"
echo "$out" | grep -q '.claude/skills/handoff-pickup/SKILL.md'
assert_rc 0 "$?" "dry-run lists handoff-pickup skill"

# --- real scaffold under a fake HOME and root -----------------------------
# The app lands under the machine's root, which HOME no longer moves, so the
# fake root is named explicitly with --root-config (company/decisions/0008).
FAKE_HOME=$(mktemp -d)
HOME="$FAKE_HOME" /bin/bash "$CLA" testapp --prefix tt \
    --root-config "$(co_root_config "$FAKE_HOME")" >/dev/null 2>&1
assert_rc 0 "$?" "real scaffold exits 0"

# --- the root decides where the app lands, not HOME ------------------------
ELSEWHERE_HOME=$(mktemp -d)
ROOT_HOME=$(mktemp -d)
HOME="$ELSEWHERE_HOME" /bin/bash "$CLA" rootapp --prefix ra \
    --root-config "$(co_root_config "$ROOT_HOME")" >/dev/null 2>&1
assert_rc 0 "$?" "scaffold with a root elsewhere exits 0"
[ -d "$ROOT_HOME/Convivy/rootapp" ]; assert_rc 0 "$?" "the app lands under the configured root"
[ ! -e "$ELSEWHERE_HOME/Convivy/rootapp" ]; assert_rc 0 "$?" "and not under HOME"
rm -rf "$ELSEWHERE_HOME" "$ROOT_HOME"

# --- a root config that names no root is refused before anything is made ---
# builder-root exits 2 for it rather than printing the real root, so the scaffold
# stops at the resolution line. The message is specific, so an older script that
# refuses the flag as unknown does not pass for the wrong reason.
NOROOT_HOME=$(mktemp -d)
out=$(HOME="$NOROOT_HOME" /bin/bash "$CLA" norootapp --prefix na \
    --root-config "$NOROOT_HOME/no-such.env" 2>&1)
assert_rc 1 "$?" "a config naming no root exits 1"
assert_contains "$out" "cannot resolve the machine's root" "and says why"
rm -rf "$NOROOT_HOME"

LEAVE="$FAKE_HOME/Convivy/testapp/.claude/skills/handoff-leave/SKILL.md"
PICK="$FAKE_HOME/Convivy/testapp/.claude/skills/handoff-pickup/SKILL.md"

[ -f "$LEAVE" ]; assert_rc 0 "$?" "handoff-leave SKILL.md written"
[ -f "$PICK" ];  assert_rc 0 "$?" "handoff-pickup SKILL.md written"

grep -q '^name: handoff-leave' "$LEAVE"
assert_rc 0 "$?" "handoff-leave frontmatter name intact"
grep -q '^name: handoff-pickup' "$PICK"
assert_rc 0 "$?" "handoff-pickup frontmatter name intact"

# The quoted heredoc must preserve runtime shell literally (NOT interpolate it
# to empty at scaffold time) — the skill resolves <product> when the operator runs it.
grep -q 'git rev-parse --show-toplevel' "$LEAVE"
assert_rc 0 "$?" "handoff-leave preserves literal \$(git rev-parse ...)"
grep -q 'git rev-parse --show-toplevel' "$PICK"
assert_rc 0 "$?" "handoff-pickup preserves literal \$(git rev-parse ...)"

# ARCH-043 absolute-path guidance must survive into the scaffolded skill.
grep -q 'ARCH-043' "$LEAVE"
assert_rc 0 "$?" "handoff-leave carries the ARCH-043 path convention"

# The scaffolded README, CLAUDE.md and post-creation checklist must not tell a
# new app to rely on REPO_LOCAL_DOCS, or on 'scribe' refusing writes against it:
# #578 (b23cf33e, 2026-09-14) deleted both the tuple and the `scribe verify`/
# `scribe apply` commands that read it from bin/agentctl, so a line generated
# against them names a variable and a refusal that no longer exist.
#
# This is asserted because the claim is BOILERPLATE ASSERTED AS FACT, baked into
# each new app's own charter at creation time, where nothing prompts a second
# look — unlike a checklist line an operator reads before acting.
README="$FAKE_HOME/Convivy/testapp/README.md"
CLAUDEMD="$FAKE_HOME/Convivy/testapp/CLAUDE.md"
ORCH="$FAKE_HOME/Convivy/testapp/.claude/agents/tt-overnight-orchestrator.md"
ADR="$FAKE_HOME/Convivy/testapp/docs/decisions/0001-testapp-creation.md"

for f in "$README" "$CLAUDEMD" "$ORCH" "$ADR"; do
    [ -f "$f" ]; assert_rc 0 "$?" "scaffold wrote $(basename "$f")"
    grep -q 'REPO_LOCAL_DOCS' "$f"
    assert_rc 1 "$?" "$(basename "$f") no longer names the removed REPO_LOCAL_DOCS"
    grep -qE '\bscribe\b' "$f"
    assert_rc 1 "$?" "$(basename "$f") no longer claims scribe refuses anything"
    grep -qE '(dispatchable[^.]*LOCAL_AGENT_REPOS|LOCAL_AGENT_REPOS[^.]*dispatchable)' "$f"
    assert_rc 1 "$?" "$(basename "$f") no longer claims the crew is dispatchable via LOCAL_AGENT_REPOS"
    grep -qF 'Library-served index' "$f"
    assert_rc 0 "$?" "$(basename "$f") states agentctl spawn resolves the crew through the shared Library-served index"
    grep -qE 'LOCAL_AGENT_REPOS.*reserved' "$f"
    assert_rc 0 "$?" "$(basename "$f") says LOCAL_AGENT_REPOS is reserved for the privacy crews, not this app"

    # WHAT THIS CANNOT DO, stated so nobody reads a green run as proof. These are
    # assertions about PROSE, so there is no behaviour to exercise and no
    # behavioural test to write instead. A security reviewer defeated an earlier
    # version of this check (when it asserted a claim's PRESENCE) by rewording the
    # sentence to preserve a false meaning while dodging the pattern; asserting
    # ABSENCE instead does not have that gap in the same way, but still does not
    # catch a reworded claim that keeps the same meaning without the word
    # 'REPO_LOCAL_DOCS' or 'scribe' in it, and no regex over prose will.
done

# The post-creation checklist is printed to stdout, never written to a file, so
# it needs its own dry-run capture rather than the loop over README/CLAUDE.md
# ($out from the top of this script is NOT reused here — it is reassigned below
# for the no-root-config case by the time this runs). The checklist's `cat`
# block runs unconditionally, before the DRY_RUN branch that gates file content,
# so a dry-run capture still carries it.
checklist=$(/bin/bash "$CLA" testapp --prefix tt --dry-run 2>&1)
echo "$checklist" | grep -q 'REPO_LOCAL_DOCS'
assert_rc 1 "$?" "dry-run checklist no longer tells the operator to add the app to REPO_LOCAL_DOCS"
echo "$checklist" | grep -q 'BOTH tuples'
assert_rc 1 "$?" "dry-run checklist no longer tells the operator to add the app to two tuples"
echo "$checklist" | grep -q 'to LOCAL_AGENT_REPOS'
assert_rc 1 "$?" "dry-run checklist no longer tells the operator to add the app to LOCAL_AGENT_REPOS"
echo "$checklist" | grep -q '_INTERNAL_APP_PREFIXES'
assert_rc 0 "$?" "dry-run checklist tells the operator to register the crew prefix in _INTERNAL_APP_PREFIXES"

# The scaffolded CLAUDE.md review cycle step and the overnight-orchestrator's
# auto-cycle step must both name the current posting command (`agentctl
# review-post`), not `gh pr comment`, and must name the review-round caps
# without baking a number into a file this tool writes once at creation and
# never touches again — a written-in number drifts the moment the policy's
# `review_rounds_per_role` or `review_rounds_code` next changes (the document cap
# has changed twice: platform/decisions/0083 raised it 3 -> 5, and the operator raised it
# again 5 -> 7 on 2026-10-01, sig_01M3W9600TWE8SVKQNXX6G282A; the code cap is new,
# 2026-10-05). Every app scaffolded after this PR would otherwise ship stale
# onboarding text, unnoticed the same way the CLAUDE.md hit was on the first round.
ORCH="$FAKE_HOME/Convivy/testapp/.claude/agents/tt-overnight-orchestrator.md"
[ -f "$ORCH" ]; assert_rc 0 "$?" "overnight-orchestrator agent file written"

# The cap wording is hand-copied into this file's two generated steps and into
# docs/lab-product-template.md, so each copy is pinned to the same literal text. A key-name
# grep passed while the wording drifted. Both strings are the prose the three copies carry.
CAP_DEFAULT='(for a pull request that changes code or any file an agent is instructed to act on, the cap is `review_rounds_code` in the platform'\''s agent policy file, or eleven rounds when that file has no `review_rounds_code` line; for one that changes only documents, the cap is `review_rounds_per_role` in that file)'
CAP_PROGRESS='A pull request that changes code or any file an agent is instructed to act on runs rounds past the document cap only while no finding in the latest round repeats one an earlier round raised'

for f in "$CLAUDEMD" "$ORCH"; do
    grep -q 'review-round cap' "$f"
    assert_rc 0 "$?" "$(basename "$f") names the review-round cap"
    grep -q 'review_rounds_per_role' "$f" && grep -q 'review_rounds_code' "$f" && grep -q 'changes only documents' "$f"
    assert_rc 0 "$?" "$(basename "$f") points at both review_rounds_code and review_rounds_per_role, and says which applies to which pull request"
    grep -qF -- "$CAP_DEFAULT" "$f"
    assert_rc 0 "$?" "$(basename "$f") states eleven rounds as the code cap when the policy file has no review_rounds_code line"
    grep -qF -- "$CAP_PROGRESS" "$f"
    assert_rc 0 "$?" "$(basename "$f") states the progress clause in the wording the template carries"
    grep -q 'agentctl review-post' "$f"
    assert_rc 0 "$?" "$(basename "$f") tells reviewers to post with agentctl review-post"

    grep -qE '[0-9]+-round cap' "$f"
    assert_rc 1 "$?" "$(basename "$f") does not bake a round-cap number into onboarding text written once at creation"
    grep -qE '3-cycle cap|cap at 3 cycles|3-cap' "$f"
    assert_rc 1 "$?" "$(basename "$f") no longer names the superseded three-cycle cap"
    grep -q 'gh pr comment' "$f"
    assert_rc 1 "$?" "$(basename "$f") no longer tells reviewers to post with gh pr comment"
done

# docs/lab-product-template.md is the conformance checklist a lab app is
# verified against post-scaffold. create-lab-app's own scaffolded CLAUDE.md
# sends the reader to it BY PATH (`bin/create-lab-app:319`), not by copying or
# rendering it, so it is a static file in this repo rather than scaffold
# output — the loop above cannot reach it, and it needs its own check against
# $REPO_ROOT rather than $FAKE_HOME.
TEMPLATE="$REPO_ROOT/docs/lab-product-template.md"
[ -f "$TEMPLATE" ]; assert_rc 0 "$?" "docs/lab-product-template.md exists"
grep -q 'review_rounds_per_role' "$TEMPLATE" && grep -q 'review_rounds_code' "$TEMPLATE" && grep -q 'changes only documents' "$TEMPLATE"
assert_rc 0 "$?" "lab-product-template.md points at both review_rounds_code and review_rounds_per_role, and says which applies to which pull request"
grep -qF -- "$CAP_DEFAULT" "$TEMPLATE"
assert_rc 0 "$?" "lab-product-template.md states eleven rounds as the code cap when the policy file has no review_rounds_code line"
grep -qF -- "$CAP_PROGRESS" "$TEMPLATE"
assert_rc 0 "$?" "lab-product-template.md states the progress clause in the wording the generated steps carry"
grep -qE '[0-9]+-round cap' "$TEMPLATE"
assert_rc 1 "$?" "lab-product-template.md does not bake a round-cap number into the conformance checklist"

# The write-bead stub is loaded by every agent in a scaffolded repo, dispatched runs
# included (platform/decisions/0073: a dispatched run files nothing). Both checks read
# the generated file, not the heredoc in the source.
WB="$FAKE_HOME/Convivy/testapp/.claude/skills/write-bead/SKILL.md"
[ -f "$WB" ]; assert_rc 0 "$?" "write-bead SKILL.md written"

# A filing instruction is one unit of text: a numbered or bulleted item, running to the
# next marker or blank line. List items have no blank line between them, so the unit
# boundary is the marker. A unit that names issue_create must also say that a co session
# files and a dispatched run does not; otherwise it is an unconditional instruction.
UNCOND_AWK='
    /^[0-9]+\. |^- |^$/ { if (buf ~ /issue_create/ && (buf !~ /co session/ || buf !~ /dispatched run/)) print buf; buf="" }
    { buf = buf " " $0 }
    END { if (buf ~ /issue_create/ && (buf !~ /co session/ || buf !~ /dispatched run/)) print buf }
'
UNCOND=$(awk "$UNCOND_AWK" "$WB")
[ -z "$UNCOND" ]; assert_rc 0 "$?" "write-bead stub has no unconditional issue_create instruction"
# Positive control: the same check must flag an unconditional filing line, or the empty
# result above proves nothing.
POS=$(printf '4. **File every entry** (`issue_create`).\n' | awk "$UNCOND_AWK")
[ -n "$POS" ]; assert_rc 0 "$?" "the filing check flags an unconditional issue_create line (positive control)"

# The method moved from the deprecated knowledge-base copy to the Shipwright payload
# (platform/skills/write-bead is status: deprecated), so the stub must name the payload.
grep -q 'platform/skills/write-bead' "$WB"
assert_rc 1 "$?" "write-bead stub names no deprecated kb_get target"
grep -q 'payload/skills/write-bead/SKILL.md' "$WB"
assert_rc 0 "$?" "write-bead stub names the Shipwright payload source"

rm -rf "$FAKE_HOME"
finish
