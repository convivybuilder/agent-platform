#!/bin/bash
# Tests for the "registry unreadable -> exit 1" path this PR gave
# bin/arch-review and bin/bug-sweep: each
# reads its valid-<product> set from lib/agent-platform/projects.py
# (projects.shared_coverage_products()) before doing anything else, and each
# guards that read with `&& [[ -n "$shared_coverage_products" ]] || { ... exit
# 1; }`. Until now nothing exercised the failing half of that guard — only
# the real, always-readable registry ever ran through it.
#
# PROJECTS_JSON is projects.py's own override seam (_registry_path(),
# checked before the module's own tree and before builder_root.root()), so
# pointing it at a path that does not exist makes `import projects` raise
# inside the one-liner these scripts shell out to, without needing a fake
# HOME or a crafted registry file.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

BOGUS_REGISTRY="$(mktemp -d)/does-not-exist.json"

check_fails_closed() { # <script-name> [<extra-arg> ...]
    local script="$1"; shift
    local out rc
    out=$(PROJECTS_JSON="$BOGUS_REGISTRY" BUILDER_ROOT="$(mktemp -d)" \
        /bin/bash "$REPO_ROOT/bin/$script" "$@" 2>&1)
    rc=$?
    assert_rc 1 "$rc" "$script exits 1 when the registry is unreadable"
    assert_contains "$out" "$script: cannot read the shared-coverage product list from the registry" \
        "$script says why"
}

check_fails_closed arch-review acme
check_fails_closed bug-sweep acme

finish
