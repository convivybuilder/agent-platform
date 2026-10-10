#!/bin/bash
# install.sh's final "Still TODO on a fresh mini" block names paths under the
# machine's root (company/decisions/0008) — the knowledge clone target and the
# two symlink commands. It must name whatever this machine's root actually is,
# not a literal ~/Convivy, so the checklist works when pasted on a mini whose
# root was configured somewhere else.
#
# root() is a trust anchor (lib/agent-platform/builder_root.py) that deliberately
# ignores $HOME, reading the account's real passwd entry instead — so faking HOME
# and running install.sh end to end cannot isolate its value the way other
# bash tests fake a root. This checks the source directly: the "Still TODO"
# lines are built from $ROOT, which is `builder-root --root`, not a literal
# path, and no hardcoded account path remains in that section.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

section=$(awk '/^# The machine.s root/,/^EOF$/' "$REPO_ROOT/install.sh")

echo "== the fresh-mini checklist is built from the resolved root, not a literal path =="
assert_contains "$section" 'ROOT="$("$LOCAL_BIN/builder-root" --root)"' \
    "ROOT comes from builder-root --root (the crews/knowledge/checkouts anchor, not data_root())"
assert_contains "$section" 'Clone convivy/knowledge to $ROOT/knowledge' \
    "the knowledge-clone line is built from \$ROOT"
assert_contains "$section" 'ln -sfn $ROOT/substrate/machine-global-CLAUDE.md' \
    "the CLAUDE.md symlink line is built from \$ROOT"
assert_contains "$section" 'ln -sfn $ROOT/write-better/write-better.md' \
    "the write-better symlink line is built from \$ROOT"
assert_eq "0" "$(printf '%s\n' "$section" | grep -cE '/Users/[a-z]+/Convivy|~/Convivy')" \
    "no literal account path remains in the still-TODO section"

finish
