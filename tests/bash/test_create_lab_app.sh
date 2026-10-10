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

# The scaffolded README and CLAUDE.md must not tell a new app that registration
# in LOCAL_AGENT_REPOS alone keeps its KB local. That was true until 2026-08-25,
# when REPO_LOCAL_DOCS was split out of it; since then the crew half and the
# KB-write half are separate tuples and neither implies the other.
#
# This is asserted because the claim is BOILERPLATE ASSERTED AS FACT, baked into
# each new app's own charter at creation time, where nothing prompts a second
# look — unlike a checklist line an operator reads before acting. It survived two
# earlier fixes to the checklist sites and was found only by a reviewer asked to
# sweep for a third instance.
README="$FAKE_HOME/Convivy/testapp/README.md"
CLAUDEMD="$FAKE_HOME/Convivy/testapp/CLAUDE.md"

for f in "$README" "$CLAUDEMD"; do
    [ -f "$f" ]; assert_rc 0 "$?" "scaffold wrote $(basename "$f")"
    grep -q 'REPO_LOCAL_DOCS' "$f"
    assert_rc 0 "$?" "$(basename "$f") names REPO_LOCAL_DOCS as the KB-local half"
    # Require the two tuples to appear WITH their properties, rather than only
    # requiring the name to appear somewhere. A mention of REPO_LOCAL_DOCS as an
    # unconnected aside satisfies the check above while leaving the false claim
    # intact next to it.
    # Order-independent: the tuple and its property must share a sentence, in
    # either direction. The generated text puts the name in parentheses after the
    # property in one file and before it in the other, and pinning one order
    # would fail on prose that is correct.
    grep -qE '(KB stays local[^.]*REPO_LOCAL_DOCS|REPO_LOCAL_DOCS[^.]*(KB|scribe))' "$f"
    assert_rc 0 "$?" "$(basename "$f") ties the KB-local property to REPO_LOCAL_DOCS"
    grep -qE '(dispatchable[^.]*LOCAL_AGENT_REPOS|LOCAL_AGENT_REPOS[^.]*(dispatch|crew|resolv))' "$f"
    assert_rc 0 "$?" "$(basename "$f") ties the crew property to LOCAL_AGENT_REPOS"

    # WHAT THIS CANNOT DO, stated so nobody reads a green run as proof. These are
    # assertions about PROSE, so there is no behaviour to exercise and no
    # behavioural test to write instead — unlike the scribe gate, where a
    # differential test replaced exactly this kind of string matching.
    #
    # A security reviewer defeated the first version of this check by rewording
    # the sentence to preserve the false meaning while dodging the pattern, and
    # the same is possible here with more effort. That is the wrong bar: the
    # realistic failure is someone editing the boilerplate and reintroducing the
    # old claim, or the text drifting as the tuples change again — not an author
    # working to evade a grep. This catches those. It does not catch a
    # deliberately reworded falsehood, and no regex over prose will.
done

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
