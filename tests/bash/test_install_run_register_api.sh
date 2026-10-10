#!/bin/bash
# Tests for bin/install-run-register-api — the deploy-pin installer for the
# com.convivy.run-register-api daemon (platform/decisions/0059). The daemon used to
# run through ~/.local/bin/run-register-api, a symlink into this checkout, so
# whatever branch was checked out here became what the daemon served on its
# next restart. This installer replaces that with a real copy, built from a
# git REF rather than the working tree, in its own deploy area.
#
# Every run is hermetic: HOME points at a tmp dir, and the source repo is a
# from-scratch fixture (not this checkout) so the test controls exactly what
# "main" and a feature branch contain, independent of which branch this suite
# happens to be running from.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-run-register-api"

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# Build a fixture repo with exactly the files the installer needs, committed
# to a branch literally named "main" (never derived from whatever branch this
# suite is running under). Echoes the repo path.
_seed_repo() { # <path> <run-register-api body> <registry body>
    local repo=$1 rr_body=$2 reg_body=$3
    mkdir -p "$repo/bin" "$repo/lib/agent-platform" "$repo/launchd"
    git -C "$repo" init -q
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    _git_ident "$repo"
    printf '%s\n' "$rr_body" > "$repo/bin/run-register-api"
    chmod +x "$repo/bin/run-register-api"
    printf '%s\n' "$reg_body" > "$repo/lib/agent-platform/registry.py"
    echo "# telemetry" > "$repo/lib/agent-platform/telemetry.py"
    # The /sessions route (bin/run-register-api) pulls in session_state.py and
    # everything IT imports, transitively — see the installer's own archive
    # list. Stubbed here too: `git archive` fails outright on a pathspec that
    # matches nothing, so every one of these has to exist and be committed or
    # the fixture repo can't be archived at all, not just "imports wrong".
    for dep in private_crew.py projects.py session_lifecycle.py session_state.py \
               session_input.py rollover_policy.py claude_trust.py tmux_transcript.py \
               signal_client.py vault_plane.py builder_root.py review_protocol.py agent_policy.py \
               fetched_documents.py crew_key.py slate.py; do
        echo "# $dep" > "$repo/lib/agent-platform/$dep"
    done
    cat > "$repo/launchd/com.convivy.run-register-api.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.convivy.run-register-api</string>
    <key>ProgramArguments</key>
    <array>
        <string>__HOME__/.local/deploy/run-register-api/venv/bin/python3</string>
        <string>__HOME__/.local/deploy/run-register-api/bin/run-register-api</string>
    </array>
</dict>
</plist>
PLIST
    git -C "$repo" add -A
    git -C "$repo" commit -qm seed
}

setup() { # -> echoes fake HOME, with $HOME/src-repo seeded on main
    local home
    home=$(mktemp -d)
    _seed_repo "$home/src-repo" "#!/usr/bin/env python3
print('v1')" "# registry v1"
    echo "$home"
}

DEPLOY="/.local/deploy/run-register-api"

# --- 1. a fresh install from main copies code, pins an interpreter, and
#        records the deployed commit --------------------------------------------
H=$(setup)
rc=0
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "a fresh install from main exits 0"

assert_eq "1" "$(test -f "$H$DEPLOY/bin/run-register-api" && echo 1 || echo 0)" \
    "the daemon script is deployed"
assert_eq "0" "$(test -L "$H$DEPLOY/bin/run-register-api" && echo 1 || echo 0)" \
    "the deployed script is a real copy, not a symlink (rule 2)"
assert_contains "$(cat "$H$DEPLOY/bin/run-register-api")" "print('v1')" \
    "the deployed script is main's committed content"
assert_eq "1" "$(test -x "$H$DEPLOY/venv/bin/python3" && echo 1 || echo 0)" \
    "a pinned interpreter is built inside the deploy area (rule 4)"

sha_main=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$(cat "$H$DEPLOY/DEPLOYED_COMMIT")" "$sha_main" \
    "DEPLOYED_COMMIT names the commit actually installed"

assert_eq "1" "$(test -f "$H/Library/LaunchAgents/com.convivy.run-register-api.plist" && echo 1 || echo 0)" \
    "the plist is copied into LaunchAgents"
assert_eq "0" "$(test -L "$H/Library/LaunchAgents/com.convivy.run-register-api.plist" && echo 1 || echo 0)" \
    "the copied plist is a real file, not a symlink (rule 8)"

installed_plist=$(cat "$H/Library/LaunchAgents/com.convivy.run-register-api.plist")
case "$installed_plist" in
    *"__HOME__"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: installed plist still carries the __HOME__ placeholder (CQ-428): not rendered" ;;
    *) _PASS=$((_PASS + 1));;
esac
assert_contains "$installed_plist" "$H/.local/deploy/run-register-api/venv/bin/python3" \
    "the __HOME__ placeholder is rendered to this fake HOME, same as install.sh phase 4"
rm -rf "$H"

# --- 2. an UNCOMMITTED change to a tracked file is never what gets deployed
#        (rule 7 — the install reads the git ref, never the live checkout) -----
H=$(setup)
echo "print('DIRTY WORKING TREE — MUST NOT DEPLOY')" > "$H/src-repo/bin/run-register-api"

HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "install still succeeds with a dirty working tree"

deployed=$(cat "$H$DEPLOY/bin/run-register-api")
case "$deployed" in
    *"DIRTY WORKING TREE"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the uncommitted working-tree edit was deployed — git archive was bypassed"
        ;;
    *)
        _PASS=$((_PASS + 1))
        ;;
esac
assert_contains "$deployed" "print('v1')" \
    "the deployed content is main's last COMMIT, not the dirty working copy"
rm -rf "$H"

# --- 3. an untracked file anywhere in the tree never reaches the deploy area
#        (rule 7 — the credential-sweep concern the ADR names by name) ---------
H=$(setup)
echo "super-secret" > "$H/src-repo/lib/agent-platform/credentials.env"

HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1

leaked=$(find "$H$DEPLOY" -name 'credentials.env' 2>/dev/null)
assert_eq "" "$leaked" \
    "an untracked file in the checkout must never land in the deploy area"
rm -rf "$H"

# --- 4. a non-main ref is refused without --allow-non-main, and the deploy
#        area is left untouched (rule 6) ----------------------------------------
H=$(setup)
git -C "$H/src-repo" checkout -qb feat/x
echo "print('v2-branch')" > "$H/src-repo/bin/run-register-api"
git -C "$H/src-repo" commit -qam "branch change"
git -C "$H/src-repo" checkout -q main

# seed a prior main install so we can prove the refusal leaves it alone
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1

rc=0
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" --ref=feat/x >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" "installing a non-main ref without --allow-non-main is refused"
assert_contains "$(cat "$H/out.log")" "REFUSING" \
    "the refusal names itself"
assert_contains "$(cat "$H$DEPLOY/bin/run-register-api")" "print('v1')" \
    "a refused install leaves the previously-deployed release untouched"

HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "--allow-non-main permits installing a non-main ref"
assert_contains "$(cat "$H$DEPLOY/bin/run-register-api")" "print('v2-branch')" \
    "the override actually deploys the named ref's content"
assert_contains "$(cat "$H$DEPLOY/DEPLOYED_COMMIT")" "ref: feat/x" \
    "DEPLOYED_COMMIT records which ref was installed"
rm -rf "$H"

# --- 5. re-installing atomically replaces the prior release, with no leftover
#        staging directory ------------------------------------------------------
H=$(setup)
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
echo "print('v3')" > "$H/src-repo/bin/run-register-api"
git -C "$H/src-repo" commit -qam "v3"

HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "re-installing over an existing deploy area succeeds"
assert_contains "$(cat "$H$DEPLOY/bin/run-register-api")" "print('v3')" \
    "the re-install replaces the deployed content"
leftovers=$(find "$H/.local/deploy" -maxdepth 1 -name 'run-register-api.*' 2>/dev/null)
assert_eq "" "$leftovers" \
    "no .new/.old staging directory survives a completed install"
rm -rf "$H"

# --- 6. --status answers what commit is deployed, with no side effects --------
H=$(setup)
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
before=$(find "$H$DEPLOY" | sort)

out=$(HOME="$H" bash "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
rc=$?
assert_rc 0 "$rc" "--status exits 0 once something is deployed"
sha_main=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$out" "$sha_main" "--status names the deployed commit"

after=$(find "$H$DEPLOY" | sort)
assert_eq "$before" "$after" "--status makes no change to the deploy area"
rm -rf "$H"

# --- 7. the --repo DEFAULT resolves from the machine's root, never from argv[0] ---
# (The root is named with --root-config below: HOME does not move it,
# company/decisions/0008.)
# The gap this closes, and it is the same defect twice: #468's review found
# argv[0]-derived resolution broken in this exact file, the fix landed only in
# the sibling install-agent-platform-shared, and this script was cherry-picked
# into that PR still carrying it. install.sh symlinks this installer into
# ~/.local/bin (it is NOT in DEPLOY_PINNED_BINS, which names only the daemon
# binary), and rule 5 exists so an operator can invoke it by name — through
# that symlink `dirname "$0"/..` lands on ~/.local rather than a checkout.
# Every other scenario here passes --repo explicitly, so nothing caught it.
H=$(setup)
mkdir -p "$H/Convivy" "$H/.local/bin"
cp -R "$H/src-repo" "$H/Convivy/agent-platform"
ln -sf "$INSTALLER" "$H/.local/bin/install-run-register-api"

out=$(HOME="$H" bash "$H/.local/bin/install-run-register-api" \
      --root-config="$(co_root_config "$H")" 2>&1)
rc=$?
assert_rc 0 "$rc" "invoked through a symlink with no --repo, the installer succeeds"

sha_default=$(git -C "$H/Convivy/agent-platform" rev-parse main)
status_out=$(HOME="$H" bash "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
assert_contains "$status_out" "$sha_default" \
    "the default --repo deployed agent-platform under the machine's root, not a path from argv[0]"

assert_contains "$(cat "$H$DEPLOY/bin/run-register-api" 2>&1)" "v1" \
    "the daemon binary came from the default checkout's committed content"

# A root config that names no root, with no --repo, is refused (exit 2) rather
# than answered with the machine's real checkout.
out=$(HOME="$H" bash "$INSTALLER" --root-config="$H/no-such.env" 2>&1); rc=$?
assert_rc 2 "$rc" "no --repo and a config naming no root -> refused"
assert_contains "$out" "cannot resolve the machine's root" "and it says why"
rm -rf "$H"

# --- 8: a failing archive aborts, leaves the live area intact, cleans up ---
# Covers this round's two behavioural changes, neither of which had a test:
#   (a) `git archive` is its own command now, not piped into tar, so `set -e`
#       catches ITS failure. Piped, a failing archive was invisible — tar exits
#       0 on empty input and only a LATER command failing caught it, which is
#       fail-closed by accident rather than by design.
#   (b) the EXIT/INT/TERM trap removes staging, so a failed install leaves
#       nothing beside the live area.
# Asserted as a DELTA: install successfully first, then fail, then prove the
# GOOD deploy survived. Asserting only "the bad install failed" would pass
# against an implementation that never deploys anything at all.
H=$(setup)
HOME="$H" bash "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
good_sha=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$(cat "$H$DEPLOY/bin/run-register-api" 2>&1)" "v1" "the good install deployed v1"

# A VALID repo on main that simply lacks the paths the installer archives.
empty="$H/empty-repo"
mkdir -p "$empty"
git -C "$empty" init -q
git -C "$empty" symbolic-ref HEAD refs/heads/main
git -C "$empty" config user.email t@t
git -C "$empty" config user.name t
echo unrelated > "$empty/README"
git -C "$empty" add -A
git -C "$empty" commit -qm seed

out=$(HOME="$H" bash "$INSTALLER" --repo="$empty" 2>&1)
rc=$?
assert_eq "1" "$([ "$rc" -ne 0 ] && echo 1 || echo 0)" \
    "a repo lacking the archived paths fails rather than deploying nothing quietly"

status_after=$(HOME="$H" bash "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
assert_contains "$status_after" "$good_sha" \
    "the previously deployed commit is still live after the failed install"

leftovers=$(find "$H/.local/deploy" -maxdepth 1 \( -name '*.new' -o -name '*.new.tar' \) 2>/dev/null)
assert_eq "" "$leftovers" \
    "the trap leaves no .new or .new.tar behind after a FAILED install"
rm -rf "$H"

# --- 9: SOURCE-LEVEL invariants a behavioural test cannot reach ------------
# Both properties below are real and neither is black-box testable in this
# script, so they are pinned against the source rather than pretended to be
# covered. Written down because the alternative was a test that passes whether
# or not the property holds — measured, not assumed: reintroducing each defect
# left every behavioural scenario in this file green.
#
#   (a) THE ARCHIVE IS NOT PIPED INTO TAR. /bin/sh has no pipefail, so in
#       `git archive | tar -x` a failing archive is invisible: tar exits 0 on
#       empty input. Today a LATER command happens to fail on the missing
#       files, so the script still aborts — which is why no outcome-based test
#       can tell the two forms apart. That accident is exactly what the fix
#       removes, and an archive failure paired with a later command that
#       happened to succeed would not be caught at all.
#
#       The check must catch a pipe into tar ANYWHERE on the line, not just a
#       continuation line starting with `|` — a single-line
#       `git archive ... | tar -x ...` scores 0 against a start-anchored
#       regex and passed here with the defect present (round 4). Comment
#       lines are stripped first: this file's own prose above quotes
#       `git archive | tar -x` verbatim, which a naive anywhere-on-line scan
#       over the SOURCE would also flag as a false positive if it ever
#       reused that phrasing in a comment.
#
#   (b) THE TRAP IS DISARMED BEFORE THE SWAP. Between `mv $DEPLOY_DIR $OLD` and
#       `mv $STAGE $DEPLOY_DIR` the live path does not exist and $STAGE IS the
#       release. A cleanup trap firing there deletes the only thing that can
#       complete the swap forward. Catching that behaviourally needs a signal
#       delivered inside a two-instruction window.
#
#       Disarmed-once-and-before-the-swap is not enough: nothing forbade a
#       RE-ARM between the disarm and the swap, which puts the destructive
#       trap right back inside the window it was disarmed to protect (round
#       4). The check below scans every line strictly between the disarm and
#       the swap for a `trap` re-arm.
#
#       Three rounds tried to recognise where a shell command begins by
#       matching separators and keywords, and each round's regex was evaded
#       by a form one syntax level lower than the one before: start-anchored
#       missed `rm -rf "$OLD"; trap ...` (round 5); adding separators and
#       keywords still missed a `case "$x" in y) trap ... ;; esac` arm, where
#       `)` legally starts a command with no separator or keyword at all
#       (round 8, found independently by both reviewers). A regex will never
#       be a shell parser, so this stopped trying to recognise commands and
#       instead asserts a stronger, syntax-independent property: the six-line
#       disarm-to-swap window below has zero legitimate reason to contain the
#       word "trap" at all, in any position. The check below forbids the bare
#       token, comment lines stripped first (same false-positive reason as
#       (a): this file's own prose says "trap" repeatedly) and word-bounded
#       so "bootstrap" does not trip it. Do not narrow this back to a
#       separator or keyword pattern; that is the exact mechanism that has
#       now been defeated three times.
src=$(cat "$INSTALLER")

assert_eq "0" "$(printf '%s\n' "$src" | grep -vE '^[[:space:]]*#' | grep -cE '\|[[:space:]]*tar\b')" \
    "(a) git archive is never piped into tar — set -e must catch the archive itself"

assert_eq "1" "$(printf '%s' "$src" | grep -c 'trap - EXIT INT TERM')" \
    "(b) the staging trap is disarmed before the swap"

disarm_line=$(printf '%s\n' "$src" | grep -n 'trap - EXIT INT TERM' | head -1 | cut -d: -f1)
swap_line=$(printf '%s\n' "$src" | grep -n 'mv "\$STAGE" "\$DEPLOY_DIR"' | head -1 | cut -d: -f1)
assert_eq "1" "$([ "$disarm_line" -lt "$swap_line" ] && echo 1 || echo 0)" \
    "(b) the disarm comes BEFORE the swap, not after it"

rearm_count=$(printf '%s\n' "$src" | sed -n "$((disarm_line + 1)),$((swap_line - 1))p" | grep -vE '^[[:space:]]*#' | grep -cE '\btrap\b')
assert_eq "0" "$rearm_count" \
    "(b) no trap re-arm between the disarm and the swap"

# --- 10: the installer's git-archive module list covers the transitive
#         import closure reachable from bin/run-register-api -----------------
# ARCH note (2026-09-12): the builder_root migration added `import builder_root
# as BR` to registry.py, telemetry.py, projects.py, session_lifecycle.py and
# session_state.py, and this installer's hard-coded archive list was never
# updated — main crashed at startup with `ModuleNotFoundError: No module named
# builder_root`, raised from projects.py:36. The behavioural scenarios above
# only prove the ARCHIVED files import cleanly; none of them can catch a
# module the source tree needs that the list never named. This computes the
# closure independently — via ast, over the REAL repo tree, never by importing
# the modules (which would run their top-level code) — and diffs it against
# the installer's own list, so the list going stale again fails here first.
closure_stderr=$(mktemp)
closure_json=$(python3 - "$REPO_ROOT" <<'PYEOF' 2>"$closure_stderr"
import ast
import json
import re
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
entry = repo_root / "bin" / "run-register-api"
lib_dir = repo_root / "lib" / "agent-platform"
installer = repo_root / "bin" / "install-run-register-api"

local_modules = {p.stem for p in lib_dir.glob("*.py")}


class _RequiredImportCollector(ast.NodeVisitor):
    """Collects `import`/`from ... import` names, skipping any import inside
    a `try`/`except` — this codebase's documented pattern (session_state.py,
    session_input.py) for a module that must degrade rather than take the
    caller down if it isn't installed yet (the picker_render outage,
    2026-07-29: a per-file symlink farm leaves a new module unlinked until
    install.sh next runs). Those are optional by design, not a dependency the
    installer has to archive."""

    def __init__(self):
        self.names = set()

    def visit_Try(self, node):
        pass  # do not descend — imports guarded by try/except are optional

    def visit_Import(self, node):
        for alias in node.names:
            self.names.add(alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        if node.module:
            self.names.add(node.module.split(".")[0])


def local_imports(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    collector = _RequiredImportCollector()
    collector.visit(tree)
    return collector.names & local_modules


closure = set()
frontier = local_imports(entry)
while frontier:
    mod = frontier.pop()
    if mod in closure:
        continue
    closure.add(mod)
    frontier |= local_imports(lib_dir / f"{mod}.py") - closure

archived = set(re.findall(r"lib/agent-platform/([A-Za-z0-9_]+)\.py", installer.read_text()))
missing = sorted(closure - archived)
print(json.dumps({"missing": missing, "closure": sorted(closure)}))
PYEOF
)
closure_rc=$?
assert_eq "0" "$closure_rc" \
    "the import-closure python3 step exits 0 (a crash here must not read as 'no missing modules')"
if [ "$closure_rc" -ne 0 ]; then
    echo "  closure step stderr:"
    sed 's/^/    /' "$closure_stderr"
fi
rm -f "$closure_stderr"

extract_stderr=$(mktemp)
extraction=$(printf '%s' "$closure_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(" ".join(data["missing"]))
print(len(data["closure"]))
print("yes" if "projects" in data["closure"] else "no")
' 2>"$extract_stderr")
extract_rc=$?
assert_eq "0" "$extract_rc" \
    "the missing-modules extraction python3 step exits 0 (a crash here must not read as 'no missing modules')"
if [ "$extract_rc" -ne 0 ]; then
    echo "  extraction step stderr:"
    sed 's/^/    /' "$extract_stderr"
fi
rm -f "$extract_stderr"

missing=$(printf '%s\n' "$extraction" | sed -n '1p')
closure_size=$(printf '%s\n' "$extraction" | sed -n '2p')
closure_has_projects=$(printf '%s\n' "$extraction" | sed -n '3p')

# A crashed step above produces an empty $closure_json/$extraction, which would
# make $missing empty too — the exact "both sides go blank" failure mode this
# scenario exists to catch. Assert the closure is non-empty and contains a
# module the service certainly imports, so that shape can never read as a pass.
assert_eq "1" "$([ "$closure_size" -gt 0 ] 2>/dev/null && echo 1 || echo 0)" \
    "the computed import closure is non-empty"
assert_eq "yes" "$closure_has_projects" \
    "the computed import closure contains 'projects', which bin/run-register-api certainly imports"

assert_eq "" "$missing" \
    "every module in bin/run-register-api's transitive import closure is in the installer's archive list"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
