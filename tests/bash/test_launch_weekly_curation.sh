#!/bin/bash
# Tests for bin/launch-weekly-curation's sibling resolution — it shells out to
# worktrees-cleanup as stage 0 of the pipeline (ARCH-126 reap). That call used
# to hardcode "$HOME/.local/bin/worktrees-cleanup", the interactive symlink
# farm install.sh phase 2 points into whichever branch this checkout happens
# to be on. This PR repointed com.convivy.weekly-curation.plist to invoke a
# deploy-pinned real COPY of this script instead (platform/decisions/0059
# rule 1), so an unattended Sunday firing of that copy reached back into the
# checkout's current branch through the hardcoded path — exactly the hazard
# the deploy pin exists to remove.
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
# Every stage past stage 0 is short-circuited: run_agent's "$AGENTCTL" spawn
# call is guarded by an if/then/else, so a stub agentctl that exits 1 lets the
# rest of the pipeline complete harmlessly without spawning anything real.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/launch-weekly-curation"

_write_stub() { # <path> <marker-tag> <exit-code>
    cat > "$1" <<EOF
#!/bin/bash
echo "$2" >> "\$MARKER_FILE"
exit $3
EOF
    chmod +x "$1"
}

# fake agentctl: exits 1 immediately, so every run_agent call in stages 1-3
# takes the "failed" branch and the pipeline finishes without spawning
# anything real.
_write_agentctl_stub() { # <home>
    mkdir -p "$1/.local/bin"
    printf '#!/bin/bash\nexit 1\n' > "$1/.local/bin/agentctl"
    chmod +x "$1/.local/bin/agentctl"
}

# --- 1. a DEPLOYED REAL COPY resolves worktrees-cleanup from its OWN dir,
#        not $HOME/.local/bin -----------------------------------------------
H=$(mktemp -d)
mkdir -p "$H/Convivy/agent-runs" "$H/deploy/bin"
_write_agentctl_stub "$H"
cp "$SCRIPT" "$H/deploy/bin/launch-weekly-curation"
chmod +x "$H/deploy/bin/launch-weekly-curation"
_write_stub "$H/deploy/bin/worktrees-cleanup" "DEPLOY_STUB_INVOKED" 0

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/deploy/bin/launch-weekly-curation" >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "the pipeline still exits 0 with agentctl stubbed out"
assert_eq "1" "$(test -f "$MARKER" && echo 1 || echo 0)" \
    "the deployed copy's own sibling worktrees-cleanup ran"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "DEPLOY_STUB_INVOKED" \
    "a deployed real copy resolves worktrees-cleanup from its OWN deploy-area directory"
rm -rf "$H"

# --- 2. the ~/.local/bin SYMLINK into the checkout (rule 5) still resolves
#        worktrees-cleanup from the CHECKOUT, never the old hardcoded
#        $HOME/.local/bin/worktrees-cleanup ----------------------------------
H=$(mktemp -d)
mkdir -p "$H/Convivy/agent-runs" "$H/checkout/bin" "$H/.local/bin"
_write_agentctl_stub "$H"
cp "$SCRIPT" "$H/checkout/bin/launch-weekly-curation"
chmod +x "$H/checkout/bin/launch-weekly-curation"
_write_stub "$H/checkout/bin/worktrees-cleanup" "CHECKOUT_STUB_INVOKED" 0
ln -sf "$H/checkout/bin/launch-weekly-curation" "$H/.local/bin/launch-weekly-curation"
# Decoy at the OLD hardcoded location — a regression to
# "$HOME/.local/bin/worktrees-cleanup" would invoke THIS instead.
_write_stub "$H/.local/bin/worktrees-cleanup" "WRONG_OLD_HARDCODED_HOME_LOCAL_BIN_INVOKED" 0

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" bash "$H/.local/bin/launch-weekly-curation" >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "the pipeline still exits 0 invoked through the symlink"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "CHECKOUT_STUB_INVOKED" \
    "invoked through the ~/.local/bin symlink, worktrees-cleanup resolves to the CHECKOUT's sibling"
case "$(cat "$MARKER" 2>/dev/null)" in
    *WRONG_OLD_HARDCODED*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the old hardcoded \$HOME/.local/bin/worktrees-cleanup decoy ran"
        ;;
    *)
        _PASS=$((_PASS + 1))
        ;;
esac
rm -rf "$H"

finish
