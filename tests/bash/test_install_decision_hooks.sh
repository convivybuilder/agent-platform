#!/bin/bash
# install-decision-hooks installs the decision-record hooks into the substrate and
# knowledge checkouts under the machine's root (company/decisions/0008). Run end to
# end against a fake root named with --root-config: HOME does not move the root, so
# running the script bare would install into this machine's repos. BUILDER_ROOT
# keeps its log and seed state in the fake HOME too.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/install-decision-hooks"
MARKER='managed-by: agent-platform decision-record-notify'

setup() { # -> echoes fake HOME
    local home r
    home=$(mktemp -d)
    mkdir -p "$home/.local/bin" "$home/data"
    for r in substrate knowledge; do
        mkdir -p "$home/Convivy/$r"
        git -C "$home/Convivy/$r" init -q
    done
    cat > "$home/.local/bin/decision-record-notify" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$NOTIFY_LOG"
EOF
    chmod +x "$home/.local/bin/decision-record-notify"
    echo "$home"
}

run_hooks() { # <home> <root-config> -> sets RC, OUT
    OUT=$(HOME="$1" BUILDER_ROOT="$1/data" NOTIFY_LOG="$1/notify.log" \
          sh "$SCRIPT" --root-config="$2" 2>&1)
    RC=$?
}

installed() { # <hook path> -> 1 when it carries the managed marker
    [ -f "$1" ] && grep -q "$MARKER" "$1" && echo 1 || echo 0
}

echo "== the hooks land in the substrate and knowledge checkouts under the named root =="
H=$(setup)
run_hooks "$H" "$(co_root_config "$H")"
assert_rc 0 "$RC" "install-decision-hooks exits 0 against the named root"
assert_eq 1 "$(installed "$H/Convivy/substrate/.git/hooks/post-commit")" \
    "substrate's post-commit hook is installed"
assert_eq 1 "$(installed "$H/Convivy/knowledge/.git/hooks/post-commit")" \
    "knowledge's post-commit hook is installed"
assert_eq 1 "$(installed "$H/Convivy/knowledge/.git/hooks/post-merge")" \
    "knowledge's post-merge hook is installed"
assert_contains "$(cat "$H/notify.log")" "--source substrate --repo-root $H/Convivy/substrate --seed" \
    "substrate is seeded from the named root"
assert_contains "$(cat "$H/notify.log")" "--source knowledge --repo-root $H/Convivy/knowledge --seed" \
    "knowledge is seeded from the named root"
rm -rf "$H"

echo "== a root config that names no root is refused before anything is written =="
H=$(setup)
printf 'OTHER=1\n' > "$H/no-root.env"
run_hooks "$H" "$H/no-root.env"
assert_rc 1 "$RC" "install-decision-hooks exits 1 when the config names no root"
assert_contains "$OUT" "cannot resolve the machine's root" "and says why"
assert_eq "" "$(cat "$H/notify.log" 2>/dev/null)" "the notifier is never run"
rm -rf "$H"

finish
