#!/bin/bash
# Round 5 (security review of PR#985): bin/check-primary-checkouts read its
# `_LIVE_CHECKOUTS` table at module import — `projects.live_checkout_readers()`
# — uncaught. Both plists that run it leave PROJECTS_JSON unset, so a Library
# outage or a missing registry token raises projects.RegistryUnavailable there
# every time. Uncaught, that killed `--assert` before it could return
# EXIT_ASSERT_BREACH (4) — the refusal code bin/primary-checkout-autoupdate
# already treats as "do not pull" — so primary-checkout-autoupdate read the
# crash as "the check could not run" and pulled anyway, unchecked. It also
# killed the 15-minute monitor before main() could log anything, so the
# outage itself was invisible. Tests 1-3 use PROJECTS_JSON pointed at an
# unreadable path, the same pattern tests/bash/test_registry_unreadable.sh
# uses for the four bash siblings this PR already made fail closed.
#
# Round 6 (both reviewers of round 5): the registry can be READABLE JSON and
# still fail INTERPRETATION — projects.load() (which live_checkout_readers()
# calls) runs _validate_kb_project_targets() over the whole registry document
# and raises a plain ValueError for a bad kb_project_by_repo/kb_scope_exceptions
# entry anywhere in it, and a live_checkout_readers row missing "reads" raises
# KeyError. Round 5's `except` clause caught only OSError, json.JSONDecodeError
# and projects.RegistryUnavailable, so either of those escaped uncaught the
# same way. Test 4 below exercises both, with made-up registries.
#
# CQ-602 made primary-checkout-autoupdate read its OWN repo list from the
# registry too (`primary_checkout_autoupdate_repos`), so section 3 and 4c's
# "the branch check cannot read the registry" scenario now fails one step
# earlier than it used to: the script's own repo-list read fails before it
# ever reaches check-primary-checkouts or looks for a checkout directory. No
# checkout or registry unit is needed to prove it, so those sections need
# neither — the proof that matters is still the same one: it never falls
# through to `git pull`.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CHECK="$REPO_ROOT/bin/check-primary-checkouts"
BOGUS_REGISTRY="$(mktemp -d)/does-not-exist.json"

# --- 1. --assert refuses (exit 4) rather than crashing -----------------------
out=$(PROJECTS_JSON="$BOGUS_REGISTRY" HOME="$(mktemp -d)" \
    "$CHECK" --assert foundry-kit --actor northwind-robot 2>&1)
rc=$?
assert_rc 4 "$rc" \
    "check-primary-checkouts --assert exits 4 (the refusal code primary-checkout-autoupdate treats as 'do not pull') when the registry is unreadable, not some other crash code"
assert_contains "$out" "REFUSING" \
    "--assert reports a refusal rather than a bare traceback"
assert_contains "$out" "registry" \
    "--assert names the registry as the cause"
assert_contains "$out" "northwind-robot" \
    "--assert names the actor that was about to act, same as an ordinary off-main refusal"

# --- 2. the sweep (monitor) mode reports and refuses to sweep, rather than
#        reading zero live checkouts as "all clear" -------------------------
out=$(PROJECTS_JSON="$BOGUS_REGISTRY" HOME="$(mktemp -d)" \
    "$CHECK" --convivy-root "$(mktemp -d)" --no-report 2>&1)
rc=$?
assert_rc 5 "$rc" \
    "the sweep exits a distinct nonzero code when the registry is unreadable, never 0"
assert_contains "$out" "REGISTRY-UNAVAILABLE" \
    "the sweep logs that it attempted to report the outage through the breach alert path"
assert_contains "$out" "registry" \
    "the sweep names the registry as the cause"

# --- 3. primary-checkout-autoupdate: REFUSES to pull (never reaches the
#        branch check at all) when IT cannot read its own repo list from the
#        registry ------------------------------------------------------------
# Before CQ-602 this ran the UNCHANGED script against a real checkout to
# exercise check-primary-checkouts's fail-closed behaviour, read through the
# script's exit-code contract. Now the script's OWN registry read
# (primary_checkout_autoupdate_repos()) fails first, so no checkout and no
# registry unit are needed to prove the thing that matters: it never falls
# through to a pull. check-primary-checkouts is still symlinked in, inert,
# to mirror the real deploy-area layout.
H=$(mktemp -d)
mkdir -p "$H/deploy/bin"
cp "$REPO_ROOT/bin/primary-checkout-autoupdate" "$H/deploy/bin/primary-checkout-autoupdate"
chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
ln -s "$CHECK" "$H/deploy/bin/check-primary-checkouts"
# So the script's OWN registry read (PYTHONPATH=<sibling>/lib/agent-platform)
# reaches the real projects.py and genuinely fails on $BOGUS_REGISTRY/
# $BROKEN_REGISTRY, rather than on a missing module.
mkdir -p "$H/deploy/lib"
ln -sfn "$REPO_ROOT/lib/agent-platform" "$H/deploy/lib/agent-platform"
cat > "$H/deploy/bin/builder-root" <<EOF
#!/bin/bash
[ "\${1:-}" = "--root" ] || exit 2
echo "$H/Convivy"
EOF
chmod +x "$H/deploy/bin/builder-root"

rc=0
HOME="$H" PROJECTS_JSON="$BOGUS_REGISTRY" bash "$H/deploy/bin/primary-checkout-autoupdate" \
    >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" \
    "primary-checkout-autoupdate exits 1 (a harness error, not the branch-check's exit 4) when it cannot read its own repo list from the registry"
assert_contains "$(cat "$H/out.log")" "cannot read the primary-checkout-autoupdate repo list from the registry" \
    "primary-checkout-autoupdate names the cause rather than falling through to git pull"
case "$(cat "$H/out.log")" in
    *"primary-checkout-autoupdate: pulling"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: primary-checkout-autoupdate attempted a pull despite the unreadable registry"
        ;;
    *)
        _PASS=$((_PASS + 1))
        ;;
esac
rm -rf "$H"

# --- 4. a registry that is READABLE JSON but fails interpretation refuses the
#        same way an unreadable one does (round 6, both reviewers of round 5):
#        the `except` clause round 5 wrote caught only OSError,
#        json.JSONDecodeError and projects.RegistryUnavailable, so a plain
#        ValueError from projects._validate_kb_project_targets (run over the
#        WHOLE registry document by projects.load(), which
#        live_checkout_readers() calls) or a KeyError from a
#        live_checkout_readers row missing "reads" escaped it, crashed the
#        import, and reached primary-checkout-autoupdate as "the check could not run" —
#        exactly the hazard round 5 closed for a merely-unreadable file. Made
#        up unit and repo names throughout, naming no real Convivy crew.
INVALID_KB_TARGET_REGISTRY="$(mktemp -d)/invalid-kb-target.json"
cat > "$INVALID_KB_TARGET_REGISTRY" <<'JSON'
{
  "northwind-robotics": {
    "class": "project", "kb": "local",
    "kb_project_by_repo": {
      "northwind-repo": {
        "kb_project": "no-such-kb-project-anywhere",
        "reason": "round 6 synthetic registry: an undeclared kb_project target"
      }
    }
  }
}
JSON

MISSING_READS_FIELD_REGISTRY="$(mktemp -d)/missing-reads.json"
cat > "$MISSING_READS_FIELD_REGISTRY" <<'JSON'
{
  "driftwood-collective": {
    "class": "project", "kb": "local",
    "live_checkout_readers": {
      "driftwood-repo": {
        "adr": "ADR-0000 (made up, not a real decision)"
      }
    }
  }
}
JSON

for BROKEN_REGISTRY in "$INVALID_KB_TARGET_REGISTRY" "$MISSING_READS_FIELD_REGISTRY"; do
    # --- 4a. --assert refuses (exit 4) rather than crashing -----------------
    out=$(PROJECTS_JSON="$BROKEN_REGISTRY" HOME="$(mktemp -d)" \
        "$CHECK" --assert foundry-kit --actor northwind-robot 2>&1)
    rc=$?
    assert_rc 4 "$rc" \
        "check-primary-checkouts --assert exits 4 for a readable-but-uninterpretable registry ($BROKEN_REGISTRY), not some other crash code"
    assert_contains "$out" "REFUSING" \
        "--assert reports a refusal rather than a bare traceback ($BROKEN_REGISTRY)"
    assert_contains "$out" "registry" \
        "--assert names the registry as the cause ($BROKEN_REGISTRY)"
    assert_contains "$out" "northwind-robot" \
        "--assert names the actor that was about to act, same as an ordinary off-main refusal ($BROKEN_REGISTRY)"

    # --- 4b. the sweep reports and refuses to sweep, rather than reading zero
    #         live checkouts as "all clear" -----------------------------------
    out=$(PROJECTS_JSON="$BROKEN_REGISTRY" HOME="$(mktemp -d)" \
        "$CHECK" --convivy-root "$(mktemp -d)" --no-report 2>&1)
    rc=$?
    assert_rc 5 "$rc" \
        "the sweep exits a distinct nonzero code for a readable-but-uninterpretable registry ($BROKEN_REGISTRY), never 0"
    assert_contains "$out" "REGISTRY-UNAVAILABLE" \
        "the sweep logs that it attempted to report the outage through the breach alert path ($BROKEN_REGISTRY)"
    assert_contains "$out" "registry" \
        "the sweep names the registry as the cause ($BROKEN_REGISTRY)"
done

# --- 4c. primary-checkout-autoupdate: REFUSES to pull (never reaches the
#         branch check) when IT cannot interpret the registry ----------------
# Only against $INVALID_KB_TARGET_REGISTRY, not both broken registries in the
# 4a/4b loop above: that loop exercises check-primary-checkouts's own
# `live_checkout_readers()`, which both broken registries defeat (one at
# `_validate_kb_project_targets`, run over the whole document; the other at
# the `row["reads"]` KeyError `live_checkout_readers()` itself raises).
# primary-checkout-autoupdate's own repo-list read
# (`primary_checkout_autoupdate_repos()`) never calls `live_checkout_readers()`
# and never touches a `"reads"` field, so $MISSING_READS_FIELD_REGISTRY does
# not defeat it — it reads a registry with no unit opted in and correctly,
# quietly does nothing, same as section 5 of
# tests/bash/test_primary_checkout_autoupdate.sh. $INVALID_KB_TARGET_REGISTRY
# does defeat it, since `_validate_kb_project_targets` runs over the whole
# document on every `load()` regardless of which field a caller wants.
H=$(mktemp -d)
mkdir -p "$H/deploy/bin"
cp "$REPO_ROOT/bin/primary-checkout-autoupdate" "$H/deploy/bin/primary-checkout-autoupdate"
chmod +x "$H/deploy/bin/primary-checkout-autoupdate"
ln -s "$CHECK" "$H/deploy/bin/check-primary-checkouts"
mkdir -p "$H/deploy/lib"
ln -sfn "$REPO_ROOT/lib/agent-platform" "$H/deploy/lib/agent-platform"
cat > "$H/deploy/bin/builder-root" <<EOF
#!/bin/bash
[ "\${1:-}" = "--root" ] || exit 2
echo "$H/Convivy"
EOF
chmod +x "$H/deploy/bin/builder-root"

rc=0
HOME="$H" PROJECTS_JSON="$INVALID_KB_TARGET_REGISTRY" bash "$H/deploy/bin/primary-checkout-autoupdate" \
    >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" \
    "primary-checkout-autoupdate exits 1 rather than pulling when it cannot interpret the registry for its own repo list"
assert_contains "$(cat "$H/out.log")" "cannot read the primary-checkout-autoupdate repo list from the registry" \
    "primary-checkout-autoupdate names the cause rather than falling through to git pull"
case "$(cat "$H/out.log")" in
    *"primary-checkout-autoupdate: pulling"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: primary-checkout-autoupdate attempted a pull despite the uninterpretable registry"
        ;;
    *)
        _PASS=$((_PASS + 1))
        ;;
esac
rm -rf "$H"

finish
