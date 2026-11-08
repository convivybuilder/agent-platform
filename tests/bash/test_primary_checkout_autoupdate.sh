#!/bin/bash
# Tests for bin/primary-checkout-autoupdate's sibling resolution — it execs
# check-primary-checkouts as the safety gate deciding whether the hourly
# `git pull` into a registry-opted-in repo is safe (exit 4 is the only
# refusal). That call used to hardcode CHECK="$HOME/.local/bin/check-primary-checkouts",
# the interactive symlink farm install.sh phase 2 points into whichever branch
# this checkout happens to be on. This PR repointed
# com.convivy.primary-checkout-autoupdate.plist to invoke a deploy-pinned real
# COPY of this script instead (platform/decisions/0059 rule 1), so an unattended
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
#
# CQ-602 made the job read which repo(s) it fast-forwards from the project
# registry (`primary_checkout_autoupdate_repos`) instead of naming one — every
# fixture below carries an invented unit and repo name, naming no real
# Convivy crew or repository.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/primary-checkout-autoupdate"
TEST_REPO="wharf-utils"

_write_stub() { # <path> <marker-tag>
    cat > "$1" <<EOF
#!/bin/bash
echo "$2" >> "\$MARKER_FILE"
exit 4
EOF
    chmod +x "$1"
}

# A stub that logs "ASSERT <repo>" for every --assert call (so a test can
# confirm which repo name each call carried, in order) and refuses — exit
# 4 — only the one repo named <refused-repo> at fixture-build time; it
# allows every other repo (exit 0), for a scenario driving the job's loop
# with more than one opted-in repo.
_write_two_repo_assert_stub() { # <path> <refused-repo>
    cat > "$1" <<EOF
#!/bin/bash
echo "ASSERT \$2" >> "\$MARKER_FILE"
if [ "\$2" = "$2" ]; then
    exit 4
else
    exit 0
fi
EOF
    chmod +x "$1"
}

# The script finds its target checkout under the machine's root, printed by the
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

# The script reads lib/agent-platform/projects.py as a sibling of its OWN bin/
# directory (the deploy area's own layout). Symlinked rather than copied — the
# real module and its own deps (builder_root, vault_plane) stay a single
# source of truth.
_link_lib() { # <bin-dir>
    mkdir -p "$(dirname "$1")/lib"
    ln -sfn "$REPO_ROOT/lib/agent-platform" "$(dirname "$1")/lib/agent-platform"
}

# A registry declaring exactly one opted-in repo ($TEST_REPO) via the
# `primary_checkout_autoupdate_repos` field the script reads instead of a
# hard-coded name.
_write_autoupdate_registry() { # <path>
    cat > "$1" <<JSON
{
  "wharf-test-unit": {
    "class": "project", "display": "Wharf Test Unit", "owner": "test-owner",
    "home_dir": "<root>/$TEST_REPO", "dirs": [],
    "repos": ["$TEST_REPO"], "aliases": [],
    "crew": "wharf-test-unit", "lifecycle": "active", "products": [],
    "kb": "local",
    "primary_checkout_autoupdate_repos": ["$TEST_REPO"]
  }
}
JSON
}

# --- 1. a DEPLOYED REAL COPY resolves check-primary-checkouts from its OWN
#        dir, not $HOME/.local/bin -------------------------------------------
H=$(mktemp -d)
mkdir -p "$H/Convivy/$TEST_REPO/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/primary-checkout-autoupdate"
chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "DEPLOY_STUB_INVOKED"
_write_root_stub "$H/deploy/bin" "$H/Convivy"
_link_lib "$H/deploy/bin"
REGISTRY="$H/registry.json"
_write_autoupdate_registry "$REGISTRY"

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$REGISTRY" \
    bash "$H/deploy/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
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
mkdir -p "$H/Convivy/$TEST_REPO/.git" "$H/checkout/bin" "$H/.local/bin"
cp "$SCRIPT" "$H/checkout/bin/primary-checkout-autoupdate"
chmod +x "$H/checkout/bin/primary-checkout-autoupdate"
_write_stub "$H/checkout/bin/check-primary-checkouts" "CHECKOUT_STUB_INVOKED"
_write_root_stub "$H/checkout/bin" "$H/Convivy"
_link_lib "$H/checkout/bin"
ln -sf "$H/checkout/bin/primary-checkout-autoupdate" "$H/.local/bin/primary-checkout-autoupdate"
# Decoy at the OLD hardcoded location — a regression to
# "$HOME/.local/bin/check-primary-checkouts" would invoke THIS instead.
_write_stub "$H/.local/bin/check-primary-checkouts" "WRONG_OLD_HARDCODED_HOME_LOCAL_BIN_INVOKED"
REGISTRY="$H/registry.json"
_write_autoupdate_registry "$REGISTRY"

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$REGISTRY" \
    bash "$H/.local/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
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

# --- 3. the machine's root decides where the checkout is, not HOME -----------
# The checkout exists only under the root the sibling builder-root prints. HOME
# holds no Convivy at all, so a script still reading HOME would say "nothing to
# pull" and exit 0 without ever running the check.
H=$(mktemp -d); R=$(mktemp -d)
mkdir -p "$R/Convivy/$TEST_REPO/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/primary-checkout-autoupdate"; chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "ROOT_STUB_INVOKED"
_write_root_stub "$H/deploy/bin" "$R/Convivy"
_link_lib "$H/deploy/bin"
REGISTRY="$H/registry.json"
_write_autoupdate_registry "$REGISTRY"
MARKER="$H/marker.log"; rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$REGISTRY" \
    bash "$H/deploy/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 4 "$rc" "the checkout under the configured root is found and checked"
assert_contains "$(cat "$MARKER" 2>/dev/null)" "ROOT_STUB_INVOKED" \
    "the check ran against the checkout under the root builder-root printed"
rm -rf "$H" "$R"

# --- 4. an unresolvable root fails loudly, before the check or any pull -------
# A checkout under HOME is a decoy: a script still reading HOME would find it and
# run the check.
H=$(mktemp -d)
mkdir -p "$H/Convivy/$TEST_REPO/.git" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/primary-checkout-autoupdate"; chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "CHECK_RAN_WITHOUT_A_ROOT"
printf '#!/bin/bash\nexit 1\n' > "$H/deploy/bin/builder-root"; chmod +x "$H/deploy/bin/builder-root"
REGISTRY="$H/registry.json"
_write_autoupdate_registry "$REGISTRY"
MARKER="$H/marker.log"; rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$REGISTRY" \
    bash "$H/deploy/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" "an unresolvable root exits 1"
assert_contains "$(cat "$H/out.log")" "cannot resolve the machine's root" "and says why"
assert_eq "" "$(cat "$MARKER" 2>/dev/null)" "and the check never ran"
rm -rf "$H"

# --- 5. no repo opted in: nothing to pull, exit 0 ----------------------------
# A registry that declares no `primary_checkout_autoupdate_repos` anywhere is a
# legitimate, quiet "nothing to do" — distinct from an unreadable registry
# (test_check_primary_checkouts_registry_unavailable.sh covers that failure).
H=$(mktemp -d)
mkdir -p "$H/Convivy" "$H/deploy/bin"
cp "$SCRIPT" "$H/deploy/bin/primary-checkout-autoupdate"; chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
_write_stub "$H/deploy/bin/check-primary-checkouts" "CHECK_SHOULD_NOT_RUN"
_write_root_stub "$H/deploy/bin" "$H/Convivy"
_link_lib "$H/deploy/bin"
EMPTY_REGISTRY="$H/empty-registry.json"
printf '{}' > "$EMPTY_REGISTRY"
MARKER="$H/marker.log"; rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$EMPTY_REGISTRY" \
    bash "$H/deploy/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "no repo opted in exits 0"
assert_contains "$(cat "$H/out.log")" "nothing to pull" "and says so"
assert_eq "" "$(cat "$MARKER" 2>/dev/null)" "and the check never ran"
rm -rf "$H"

# --- 6. TWO opted-in repos: the first's refusal does not stop the loop, and
#        each repo's own --assert names THAT repo, not the first or a fixed
#        one (round 1 — no scenario before this drove the loop with more than
#        one opted-in repo, so neither claim in the PR body was tested) -----
H=$(mktemp -d)
REPO_A="anchor-depot"
REPO_B="bollard-relay"
mkdir -p "$H/Convivy/$REPO_A/.git" "$H/deploy/bin"
# A REAL repo for $REPO_B (not just a `.git` dir), so its `git pull --ff-only`
# exits 1 ("no tracking information") rather than 128 ("fatal: not a git
# repository") — 128 would itself become the loop's reported "worst" and mask
# whether the refusal on $REPO_A (exit 4) is what the job actually reports.
git init -q -b main "$H/Convivy/$REPO_B"
cp "$SCRIPT" "$H/deploy/bin/primary-checkout-autoupdate"
chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
_write_two_repo_assert_stub "$H/deploy/bin/check-primary-checkouts" "$REPO_A"
_write_root_stub "$H/deploy/bin" "$H/Convivy"
_link_lib "$H/deploy/bin"
REGISTRY="$H/registry.json"
cat > "$REGISTRY" <<JSON
{
  "anchor-test-unit": {
    "class": "project", "display": "Anchor Test Unit", "owner": "test-owner",
    "home_dir": "<root>/$REPO_A", "dirs": [],
    "repos": ["$REPO_A"], "aliases": [],
    "crew": "anchor-test-unit", "lifecycle": "active", "products": [],
    "kb": "local",
    "primary_checkout_autoupdate_repos": ["$REPO_A"]
  },
  "bollard-test-unit": {
    "class": "project", "display": "Bollard Test Unit", "owner": "test-owner",
    "home_dir": "<root>/$REPO_B", "dirs": [],
    "repos": ["$REPO_B"], "aliases": [],
    "crew": "bollard-test-unit", "lifecycle": "active", "products": [],
    "kb": "local",
    "primary_checkout_autoupdate_repos": ["$REPO_B"]
  }
}
JSON

MARKER="$H/marker.log"
rc=0
HOME="$H" MARKER_FILE="$MARKER" PROJECTS_JSON="$REGISTRY" \
    bash "$H/deploy/bin/primary-checkout-autoupdate" >"$H/out.log" 2>&1 || rc=$?
assert_rc 4 "$rc" "the job exits 4 — the worst across repos — even though the refusal landed on the FIRST repo"
assert_contains "$(cat "$H/out.log")" "pulling $H/Convivy/$REPO_B" \
    "the second repo is still pulled after the first is refused — the loop does not stop"
case "$(cat "$MARKER" 2>/dev/null)" in
    *"ASSERT $REPO_A"*"ASSERT $REPO_B"*)
        _PASS=$((_PASS + 1))
        ;;
    *)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: each repo's --assert names that same repo, in order (ASSERT $REPO_A then ASSERT $REPO_B)"
        echo "        marker log: $(cat "$MARKER" 2>/dev/null)"
        ;;
esac
rm -rf "$H"

finish
