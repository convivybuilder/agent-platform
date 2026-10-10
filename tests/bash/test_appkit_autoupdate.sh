#!/bin/bash
# Tests for bin/appkit-autoupdate's sibling resolution — it execs
# check-primary-checkouts as the safety gate deciding whether the hourly
# `git pull` into convivy-app-kit is safe (exit 4 is the only refusal). That
# call used to hardcode CHECK="$HOME/.local/bin/check-primary-checkouts", the
# interactive symlink farm install.sh phase 2 points into whichever branch
# this checkout happens to be on. This PR repointed
# com.convivy.appkit.autoupdate.plist to invoke a deploy-pinned real COPY of
# this script instead (platform/decisions/0059 rule 1), so an unattended
# hourly firing of that copy had its safety gate answered by whatever branch
# is checked out in agent-platform — exactly the hazard the deploy pin
# exists to remove.
#
# The fix resolves the sibling relative to the script's OWN location,
# following symlinks (bash equivalent of bin/check-cli-contracts's
# pathlib.Path(__file__).resolve()). Two callers must get two answers:
#   - a deployed real copy reaches its own deploy-area sibling
#   - the ~/.local/bin symlink into the checkout (rule 5, for the person
#     working on it interactively) still reaches the checkout's sibling
# A regression back to the hardcoded path would pass EITHER direction alone
# (both stubs coincidentally live under a directory reachable from $HOME in
# these fixtures) — pinning both, plus a decoy at the old hardcoded location,
# is what catches it.
#
# The stub check always exits 4 (REFUSED) so the script stops right after
# proving which check ran, never reaching the real `git pull`.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/appkit-autoupdate"

_write_stub() { # <path> <marker-tag>
    cat > "$1" <<EOF
#!/bin/bash
echo "$2" >> "\$MARKER_FILE"
exit 4
EOF
    chmod +x "$1"
}

# The script finds the app kit under the machine's root, printed by the
# builder-root beside it (company/decisions/0008). Each copy here gets a stub
# sibling printing the fake root, which is the same sibling resolution this file
# pins for check-primary-checkouts.
_write_root_stub() { # <bin-dir> <root>
    cat > "$1/builder-root" <<EOF
#!/bin/bash
[ "\${1:-}" = "--root" ] || exit 2
echo "$2"
EOF
    chmod +x "$1/builder-root"
}

# --- 1. a DEPLOYED REAL COPY resolves check-primary-checkouts from its OWN
#        dir, not $HOME/.local/bin -------------------------------------------
H=$(mktemp -d)
mkdir -p "$H/Convivy/convivy-app-kit/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/appkit-autoupdate"
chmod +x "$H/deploy/bin/appkit-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "DEPLOY_STUB_INVOKED"
_write_root_stub "$H/deploy/bin" "$H/Convivy"

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/deploy/bin/appkit-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 4 "$rc" "REFUSED (exit 4) from the stub check, before any git pull runs"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "DEPLOY_STUB_INVOKED" \
    "a deployed real copy resolves check-primary-checkouts from its OWN deploy-area directory"
assert_contains "$(cat "$H/out.log")" "REFUSED" \
    "the script reports the refusal rather than falling through to git pull"
rm -rf "$H"

# --- 2. the ~/.local/bin SYMLINK into the checkout (rule 5) still resolves
#        check-primary-checkouts from the CHECKOUT, never the old hardcoded
#        $HOME/.local/bin/check-primary-checkouts ----------------------------
H=$(mktemp -d)
mkdir -p "$H/Convivy/convivy-app-kit/.git" "$H/checkout/bin" "$H/.local/bin"
cp "$SCRIPT" "$H/checkout/bin/appkit-autoupdate"
chmod +x "$H/checkout/bin/appkit-autoupdate"
_write_stub "$H/checkout/bin/check-primary-checkouts" "CHECKOUT_STUB_INVOKED"
_write_root_stub "$H/checkout/bin" "$H/Convivy"
ln -sf "$H/checkout/bin/appkit-autoupdate" "$H/.local/bin/appkit-autoupdate"
# Decoy at the OLD hardcoded location — a regression to
# "$HOME/.local/bin/check-primary-checkouts" would invoke THIS instead.
_write_stub "$H/.local/bin/check-primary-checkouts" "WRONG_OLD_HARDCODED_HOME_LOCAL_BIN_INVOKED"

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/.local/bin/appkit-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 4 "$rc" "REFUSED (exit 4) invoked through the symlink"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "CHECKOUT_STUB_INVOKED" \
    "invoked through the ~/.local/bin symlink, check-primary-checkouts resolves to the CHECKOUT's sibling"
case "$(cat "$MARKER" 2>/dev/null)" in
    *WRONG_OLD_HARDCODED*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the old hardcoded \$HOME/.local/bin/check-primary-checkouts decoy ran"
        ;;
    *)
        _PASS=$((_PASS + 1))
        ;;
esac
rm -rf "$H"

# --- 3. the machine's root decides where the app kit is, not HOME --------------
# The checkout exists only under the root the sibling builder-root prints. HOME
# holds no Convivy at all, so a script still reading HOME would say "nothing to
# pull" and exit 0 without ever running the check.
H=$(mktemp -d); R=$(mktemp -d)
mkdir -p "$R/Convivy/convivy-app-kit/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/appkit-autoupdate"; chmod +x "$H/deploy/bin/appkit-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "ROOT_STUB_INVOKED"
_write_root_stub "$H/deploy/bin" "$R/Convivy"
MARKER="$H/marker.log"; rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/deploy/bin/appkit-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 4 "$rc" "the checkout under the configured root is found and checked"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "ROOT_STUB_INVOKED" \
    "the check ran against the app kit under the root builder-root printed"
rm -rf "$H" "$R"

# --- 4. an unresolvable root fails loudly, before the check or any pull -------
# A checkout under HOME is a decoy: a script still reading HOME would find it and
# run the check.
H=$(mktemp -d)
mkdir -p "$H/Convivy/convivy-app-kit/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/appkit-autoupdate"; chmod +x "$H/deploy/bin/appkit-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "CHECK_RAN_WITHOUT_A_ROOT"
printf '#!/bin/bash\nexit 1\n' > "$H/deploy/bin/builder-root"; chmod +x "$H/deploy/bin/builder-root"
MARKER="$H/marker.log"; rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/deploy/bin/appkit-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" "an unresolvable root exits 1"
assert_contains "$(cat "$H/out.log")" "cannot resolve the machine's root" "and says why"
assert_eq "" "$(cat "$MARKER" 2>/dev/null)" "and the check never ran"
rm -rf "$H"

finish
