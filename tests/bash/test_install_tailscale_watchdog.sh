#!/bin/bash
# sbin/install-tailscale-watchdog.sh refuses to install from anything but the
# PRIMARY checkout (same guard as lib/link-farm.sh, after the 2026-07-30
# incident). Only the refusal path is exercised here: it exits before any
# launchctl call, so it is safe to run for real. The fix line it prints must
# name the root resolved from the checkout's own sibling bin/builder-root
# (company/decisions/0008), never a literal $HOME/Convivy.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/sbin/install-tailscale-watchdog.sh"

# A non-git copy: $REPO/bin/tailscale-watchdog must exist so the guard reaches
# is_primary_checkout rather than refusing earlier for a different reason.
setup() { # -> echoes fake checkout dir
    local repo
    repo=$(mktemp -d)
    mkdir -p "$repo/bin" "$repo/launchd"
    echo '#!/bin/sh' > "$repo/bin/tailscale-watchdog"
    echo "$repo"
}

echo "== a non-git copy is refused, naming the root from its own builder-root =="
R=$(setup)
cat > "$R/bin/builder-root" <<'EOF'
#!/bin/sh
echo "/some/other/root"
EOF
chmod +x "$R/bin/builder-root"
# The installer resolves REPO from ITS OWN location ($(dirname "$0")/..), so it
# must be copied under $R/sbin to exercise a $REPO other than this checkout.
mkdir -p "$R/sbin"
cp "$INSTALLER" "$R/sbin/install-tailscale-watchdog.sh"
out=$(HOME=$(mktemp -d) bash "$R/sbin/install-tailscale-watchdog.sh" 2>&1)
rc=$?
assert_rc 2 "$rc" "a non-git copy is refused"
assert_contains "$out" "REFUSING to install" "and says so"
assert_contains "$out" "/some/other/root/agent-platform" \
    "the fix line names the root resolved from the checkout's own builder-root"
assert_eq "0" "$(printf '%s' "$out" | grep -c '\$HOME/Convivy\|~/Convivy')" \
    "no literal ~/Convivy or \$HOME/Convivy leaks into the fix line"
rm -rf "$R"

echo "== with no resolvable root, the fallback names no wrong path =="
R=$(setup)
mkdir -p "$R/sbin"
cp "$INSTALLER" "$R/sbin/install-tailscale-watchdog.sh"
# No bin/builder-root at all: the root hint is empty, so the fallback fires.
out=$(HOME=$(mktemp -d) bash "$R/sbin/install-tailscale-watchdog.sh" 2>&1)
rc=$?
assert_rc 2 "$rc" "still refused with no builder-root sibling"
assert_contains "$out" "its root cannot be" "falls back to naming no path rather than a wrong one"
rm -rf "$R"

finish
