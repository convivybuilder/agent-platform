#!/bin/bash
# bin/launch-weekly-consulting's task prompt names the machine's resolved root
# (company/decisions/0008) for the substrate/knowledge paths it tells the
# consultant agent to read and write — not a literal ~/Convivy, which would be
# wrong on a machine whose root is configured elsewhere.
#
# root() is a trust anchor (lib/agent-platform/builder_root.py) that
# deliberately ignores $HOME, reading the account's real passwd entry instead —
# so faking HOME cannot isolate ROOT's value here the way other bash tests fake
# a root (this script has no --root-config test hook, unlike the installers
# that need one). This checks the source directly: TASK is built from $ROOT
# and $LOG_DIR, not a literal path.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/launch-weekly-consulting"
task_line=$(grep '^TASK=' "$SCRIPT")

echo "== the task prompt is built from the resolved root, not a literal path =="
assert_contains "$task_line" '$ROOT/substrate/decisions.md' \
    "the substrate/decisions.md reference is built from \$ROOT"
assert_contains "$task_line" '$LOG_DIR' \
    "the agent-runs reference is built from \$LOG_DIR"
assert_contains "$task_line" '$ROOT/knowledge/projects/agent-platform/briefs/' \
    "the knowledge-briefs write target is built from \$ROOT"
assert_eq "0" "$(printf '%s' "$task_line" | grep -cE '/Users/[a-z]+/Convivy|~/Convivy')" \
    "no literal ~/Convivy remains in the task prompt"

grep -q '^ROOT="\$("\$(dirname "\$_self_path")/builder-root" --root)" || exit 1$' "$SCRIPT"
assert_rc 0 "$?" "ROOT comes from builder-root --root (the crews/knowledge anchor), and fails closed"

echo "== AGENTCTL resolves beside this script, not through \$HOME/.local/bin =="
agentctl_line=$(grep '^AGENTCTL=' "$SCRIPT")
assert_contains "$agentctl_line" 'dirname "$_self_path"' \
    "AGENTCTL is resolved relative to this script's own real path"
assert_eq "0" "$(printf '%s' "$agentctl_line" | grep -c '\.local/bin')" \
    "AGENTCTL no longer names the \$HOME/.local/bin symlink farm (ADR-0059 rule 1)"

finish
