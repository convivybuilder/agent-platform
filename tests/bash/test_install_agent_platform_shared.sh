#!/bin/bash
# Tests for bin/install-agent-platform-shared — the deploy-pin installer for
# the common bin/, lib/agent-platform/ and lib/knowledge/ set the Bosun's
# scheduled jobs run from (platform/decisions/0059;
# issue_01M19RRWSS9ETR09N9M2BJPA0Q). Generalizes agent-platform#468's
# install-run-register-api to the whole shared tree instead of one daemon's
# two files.
#
# Every run is hermetic: HOME points at a tmp dir, and the source repo is a
# from-scratch fixture (not this checkout) so the test controls exactly what
# "main" and a feature branch contain, independent of which branch this suite
# happens to be running from.
#
# The fixture's requirements.txt names real PyPI packages (watchdog, PyYAML)
# so the pip-install step this file also covers is exercised for real rather
# than mocked — this suite needs network access to PyPI. PIP_CACHE_DIR is
# pinned to one location shared across every scenario below (outside any
# per-scenario tmp $HOME) so repeated fresh installs in this file download
# each package once rather than once per scenario.
export PIP_CACHE_DIR="${TMPDIR:-/tmp}/agent-platform-shared-test-pip-cache"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-agent-platform-shared"

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# PORTABLE MTIME, GNU-first/BSD-second and OUTPUT-validated, same shape as
# bin/tailscale-watchdog's lock-age read and gateway/llm-gateway-run.sh's mode
# read. On GNU coreutils `-f` is `--file-system`, so `stat -f '%m' FILE` prints
# a filesystem dump to stdout and exits 1 rather than erroring cleanly — a bare
# `stat -f %m FILE 2>/dev/null || stat -c %Y FILE 2>/dev/null` runs BOTH forms
# under GNU (the second because the first's exit is 1) and concatenates the
# bogus dump with the real mtime instead of yielding just the mtime. Trying
# GNU's `-c '%Y'` first and validating the captured text is an integer, rather
# than trusting either exit code, is what the two call sites below share.
_venv_marker_mtime() { # <path>
    local out
    out=$(stat -c '%Y' "$1" 2>/dev/null) || true
    case "$out" in
        ''|*[!0-9]*) ;;
        *) echo "$out"; return ;;
    esac
    out=$(stat -f '%m' "$1" 2>/dev/null) || true
    case "$out" in
        ''|*[!0-9]*) ;;
        *) echo "$out"; return ;;
    esac
    echo ""
}

# Build a fixture repo with a bin/ tool, a lib/agent-platform/ module, a
# lib/knowledge/ set (all four files the real tree ships), and a
# requirements.txt naming the two packages knowledge-watcher needs — committed
# to a branch literally named "main" (never derived from whatever branch this
# suite is running under). Echoes the repo path.
_seed_repo() { # <path> <tool body>
    local repo=$1 tool_body=$2
    # eval/ is in the fixture because it is in the installer's required set:
    # bin/kb-eval resolves eval/kb-golden.jsonl against its OWN tree, so a deploy
    # without it raises FileNotFoundError at the next scheduled run. config/ is
    # NOT required — projects.py reads the registry from an absolute path into
    # the live checkout, deliberately, so no deploy carries a copy.
    mkdir -p "$repo/bin" "$repo/lib/agent-platform" "$repo/lib/knowledge" \
             "$repo/eval" "$repo/gateway" "$repo/tools/review-eval/variant-agents" \
             "$repo/tools/review-eval/pre828-agents" "$repo/tools/review-eval/post828-agents" \
             "$repo/tools/review-eval/fix832-agents"
    git -C "$repo" init -q
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    _git_ident "$repo"
    printf '%s\n' "$tool_body" > "$repo/bin/some-tool"
    chmod +x "$repo/bin/some-tool"
    echo "VALUE = 'v1'" > "$repo/lib/agent-platform/helper.py"
    echo "SCHEMA_MARKER" > "$repo/lib/knowledge/schema.sql"
    echo "INDEXER = 'v1'" > "$repo/lib/knowledge/indexer.py"
    echo "KNOWLEDGE = 'v1'" > "$repo/lib/knowledge/knowledge.py"
    echo "TELEMETRY = 'v1'" > "$repo/lib/knowledge/kb_telemetry.py"
    echo '{"q": "seed"}' > "$repo/eval/kb-golden.jsonl"
    # bin/review-eval (agent-platform#807) resolves both of these against its
    # own tree root, the same reason eval/kb-golden.jsonl is seeded above —
    # DEPLOY_PATHS now requires both to exist at the installed commit, or the
    # installer's preflight refuses before archiving anything.
    echo "model_list: []" > "$repo/gateway/config.yaml"
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/variant-agents/reviewer.md"
    # bin/review-eval's sonnet-pre828/sonnet-post828 arms (agent-platform#828)
    # resolve against these two directories the same way, so DEPLOY_PATHS
    # requires both to exist at the installed commit too.
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/pre828-agents/reviewer.md"
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/pre828-agents/security-reviewer.md"
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/post828-agents/reviewer.md"
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/post828-agents/security-reviewer.md"
    # bin/review-eval's sonnet-fix832 arm (agent-platform's #828-follow-up)
    # resolves against this directory the same way, so DEPLOY_PATHS requires
    # it to exist at the installed commit too.
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/fix832-agents/reviewer.md"
    printf '%s\n' "---" "model: sonnet" "---" "seed" \
        > "$repo/tools/review-eval/fix832-agents/security-reviewer.md"
    cat > "$repo/requirements.txt" <<'EOF'
watchdog==6.0.0
PyYAML==6.0.3
EOF
    git -C "$repo" add -A
    git -C "$repo" commit -qm seed
}

setup() { # -> echoes fake HOME, with $HOME/src-repo seeded on main
    local home
    home=$(mktemp -d)
    _seed_repo "$home/src-repo" "#!/usr/bin/env python3
print('v1')"
    echo "$home"
}

DEPLOY="/.local/deploy/agent-platform-shared"

# --- 1. a fresh install from main copies bin/ and lib/agent-platform/, pins
#        an interpreter, and records the deployed commit -----------------------
H=$(setup)
rc=0
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "a fresh install from main exits 0"

assert_eq "1" "$(test -f "$H$DEPLOY/bin/some-tool" && echo 1 || echo 0)" \
    "the tool script is deployed"
assert_eq "0" "$(test -L "$H$DEPLOY/bin/some-tool" && echo 1 || echo 0)" \
    "the deployed script is a real copy, not a symlink (rule 2)"
assert_contains "$(cat "$H$DEPLOY/bin/some-tool")" "print('v1')" \
    "the deployed script is main's committed content"
assert_eq "1" "$(test -f "$H$DEPLOY/lib/agent-platform/helper.py" && echo 1 || echo 0)" \
    "the lib module is deployed alongside bin/"
assert_eq "0" "$(test -L "$H$DEPLOY/lib/agent-platform/helper.py" && echo 1 || echo 0)" \
    "the deployed lib module is a real copy, not a symlink (rule 2)"

# Same reasoning as the registry assertion in the run-register-api suite: the
# fixture seeding these only proves the archive did not fail on them. Both are
# resolved by deployed code against its OWN tree — bin/kb-eval reads the golden
# set, lib/agent-platform/projects.py reads the registry — so a path silently
# dropped from DEPLOY_PATHS is a nightly job failing forever against a current
# DEPLOYED_COMMIT, which is the outage this suite now covers.
assert_eq "1" "$(test -f "$H$DEPLOY/eval/kb-golden.jsonl" && echo 1 || echo 0)" \
    "the KB golden set is deployed, not left behind in the checkout"
assert_eq "1" "$(test -x "$H$DEPLOY/venv/bin/python3" && echo 1 || echo 0)" \
    "a pinned interpreter is built inside the deploy area (rule 4)"

sha_main=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$(cat "$H$DEPLOY/DEPLOYED_COMMIT")" "$sha_main" \
    "DEPLOYED_COMMIT names the commit actually installed"
rm -rf "$H"

# --- 2. an UNCOMMITTED change to a tracked file is never what gets deployed
#        (rule 7 — the install reads the git ref, never the live checkout) -----
H=$(setup)
echo "print('DIRTY WORKING TREE — MUST NOT DEPLOY')" > "$H/src-repo/bin/some-tool"

HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "install still succeeds with a dirty working tree"

deployed=$(cat "$H$DEPLOY/bin/some-tool")
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

# --- 3. an untracked file anywhere in bin/ or lib/agent-platform/ never
#        reaches the deploy area (rule 7 — the credential-sweep concern the
#        ADR names by name) -----------------------------------------------------
H=$(setup)
echo "super-secret" > "$H/src-repo/lib/agent-platform/credentials.env"
echo "super-secret" > "$H/src-repo/bin/.scratch-token"

HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1

leaked=$(find "$H$DEPLOY" \( -name 'credentials.env' -o -name '.scratch-token' \) 2>/dev/null)
assert_eq "" "$leaked" \
    "an untracked file in the checkout must never land in the deploy area"
rm -rf "$H"

# --- 4. a non-main ref is refused without --allow-non-main, and the deploy
#        area is left untouched (rule 6) ----------------------------------------
H=$(setup)
git -C "$H/src-repo" checkout -qb feat/x
echo "print('v2-branch')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "branch change"
git -C "$H/src-repo" checkout -q main

# seed a prior main install so we can prove the refusal leaves it alone
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1

rc=0
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x >"$H/out.log" 2>&1 || rc=$?
assert_rc 1 "$rc" "installing a non-main ref without --allow-non-main is refused"
assert_contains "$(cat "$H/out.log")" "REFUSING" \
    "the refusal names itself"
assert_contains "$(cat "$H$DEPLOY/bin/some-tool")" "print('v1')" \
    "a refused install leaves the previously-deployed release untouched"

# --allow-non-main alone no longer reaches the PRODUCTION deploy area — see
# test 18 for why — so the override is exercised where it is now meant to be
# used, against a scratch --deploy-dir. What this test is about is unchanged:
# does the override actually deploy the named ref's content, and is the ref
# recorded. Only the target moved.
override_dir="$H/override-target"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$override_dir" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "--allow-non-main permits installing a non-main ref"
assert_contains "$(cat "$override_dir/bin/some-tool")" "print('v2-branch')" \
    "the override actually deploys the named ref's content"
assert_contains "$(cat "$override_dir/DEPLOYED_COMMIT")" "ref: feat/x" \
    "DEPLOYED_COMMIT records which ref was installed"
rm -rf "$H"

# --- 5. re-installing atomically replaces the prior release, with no leftover
#        staging directory ------------------------------------------------------
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
echo "print('v3')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "v3"

HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "re-installing over an existing deploy area succeeds"
assert_contains "$(cat "$H$DEPLOY/bin/some-tool")" "print('v3')" \
    "the re-install replaces the deployed content"
leftovers=$(find "$H/.local/deploy" -maxdepth 1 -name 'agent-platform-shared.*' 2>/dev/null)
assert_eq "" "$leftovers" \
    "no .new/.old staging directory survives a completed install"
rm -rf "$H"

# --- 6. a repeat install of the SAME commit is a cheap no-op (the idempotent
#        fast path bin/agent-platform-autopull depends on to run this every
#        ~10 minutes without rebuilding the venv each time) --------------------
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
venv_marker="$H$DEPLOY/venv/pyvenv.cfg"
before_mtime=$(_venv_marker_mtime "$venv_marker")

out=$(HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" 2>&1)
rc=$?
assert_rc 0 "$rc" "re-running against the same deployed commit exits 0"
assert_contains "$out" "Already deployed" \
    "a same-commit re-run says it did nothing rather than silently rebuilding"

after_mtime=$(_venv_marker_mtime "$venv_marker")
assert_eq "$before_mtime" "$after_mtime" \
    "the venv is not rebuilt when the target commit is already deployed"
rm -rf "$H"

# --- 7. --status answers what commit is deployed, with no side effects --------
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
before=$(find "$H$DEPLOY" | sort)

out=$(HOME="$H" sh "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
rc=$?
assert_rc 0 "$rc" "--status exits 0 once something is deployed"
sha_main=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$out" "$sha_main" "--status names the deployed commit"

after=$(find "$H$DEPLOY" | sort)
assert_eq "$before" "$after" "--status makes no change to the deploy area"
rm -rf "$H"

# --- 8. the --repo DEFAULT resolves from the machine's root, never from argv[0] ---
# The gap this closes: every scenario above passes --repo explicitly, so a
# regression reintroducing `$(dirname "$0")/..` resolution would pass all of
# them. That regression is not hypothetical — it is exactly the defect found
# in agent-platform#468's review, where install.sh symlinks the installer into
# ~/.local/bin and $0-derived resolution then lands on ~/.local rather than a
# checkout. Invoke through such a symlink, with NO --repo, and assert the
# default still reaches agent-platform under the machine's root, which HOME
# does not move (company/decisions/0008), so the fake root is named with
# --root-config.
H=$(setup)
mkdir -p "$H/Convivy" "$H/.local/bin"
cp -R "$H/src-repo" "$H/Convivy/agent-platform"
ln -sf "$INSTALLER" "$H/.local/bin/install-agent-platform-shared"

out=$(HOME="$H" sh "$H/.local/bin/install-agent-platform-shared" \
      --root-config="$(co_root_config "$H")" 2>&1)
rc=$?
assert_rc 0 "$rc" "invoked through a symlink with no --repo, the installer succeeds"

sha_default=$(git -C "$H/Convivy/agent-platform" rev-parse main)
status_out=$(HOME="$H" sh "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
assert_contains "$status_out" "$sha_default" \
    "the default --repo deployed agent-platform under the machine's root, not a path derived from argv[0]"

# A root config that names no root, with no --repo, is refused (exit 2) rather
# than answered with the machine's real checkout.
out=$(HOME="$H" sh "$INSTALLER" --root-config="$H/no-such.env" 2>&1); rc=$?
assert_rc 2 "$rc" "no --repo and a config naming no root -> refused"
assert_contains "$out" "cannot resolve the machine's root" "and it says why"

# And the negative half: nothing from ~/.local (where argv[0] lives) is treated
# as the repo. A $0-derived implementation would have failed above rather than
# reaching this, but assert the deploy area holds the fixture's own file so the
# pair distinguishes "worked" from "worked by accident".
assert_contains "$(cat "$H$DEPLOY/bin/some-tool" 2>&1)" "v1" \
    "the deployed tool came from the default checkout's committed content"
rm -rf "$H"

# --- 9: a failing archive aborts, leaves the live area intact, cleans up ---
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
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
good_sha=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$(cat "$H$DEPLOY/bin/some-tool" 2>&1)" "v1" "the good install deployed v1"

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

out=$(HOME="$H" sh "$INSTALLER" --repo="$empty" 2>&1)
rc=$?
assert_eq "1" "$([ "$rc" -ne 0 ] && echo 1 || echo 0)" \
    "a repo lacking the archived paths fails rather than deploying nothing quietly"

status_after=$(HOME="$H" sh "$INSTALLER" --status --root-config="$(co_root_config "$H")" 2>&1)
assert_contains "$status_after" "$good_sha" \
    "the previously deployed commit is still live after the failed install"

leftovers=$(find "$H/.local/deploy" -maxdepth 1 \( -name '*.new' -o -name '*.new.tar' \) 2>/dev/null)
assert_eq "" "$leftovers" \
    "the trap leaves no .new or .new.tar behind after a FAILED install"
rm -rf "$H"

# --- 10: SOURCE-LEVEL invariants a behavioural test cannot reach ------------
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

# --- 11: lib/knowledge/ lands in the deploy area, with schema.sql present --
# indexer.py resolves SCHEMA as Path(__file__).resolve().parent / "schema.sql",
# so shipping the three .py files without it would leave ensure_db() broken on
# a fresh index — this is the regression this scenario exists to catch.
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a fresh install with lib/knowledge in the fixture succeeds"

for f in schema.sql indexer.py knowledge.py kb_telemetry.py; do
    assert_eq "1" "$(test -f "$H$DEPLOY/lib/knowledge/$f" && echo 1 || echo 0)" \
        "lib/knowledge/$f is deployed alongside bin/ and lib/agent-platform/"
done
assert_contains "$(cat "$H$DEPLOY/lib/knowledge/schema.sql")" "SCHEMA_MARKER" \
    "lib/knowledge/schema.sql is main's committed content, not a stub"
rm -rf "$H"

# --- 12: the deploy venv can actually import watchdog and PyYAML -----------
# The installer used to build a deliberately bare venv; knowledge-watcher
# imports `watchdog` directly and lib/knowledge's modules import `yaml`
# (PyYAML), so a deploy-pinned knowledge-watcher needs both importable from
# THIS venv, not merely present on some other interpreter's path.
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a fresh install that pip-installs requirements.txt succeeds"

import_out=$("$H$DEPLOY/venv/bin/python3" -c 'import watchdog, yaml; print("IMPORT-OK")' 2>&1)
assert_contains "$import_out" "IMPORT-OK" \
    "watchdog and PyYAML (as the yaml module) import cleanly from the deployed venv's own interpreter"
rm -rf "$H"

# --- 13: no venv/bin shebang names the .new staging path, and pip actually
#         runs post-swap (the shebang-rewrite regression from real console
#         scripts pip installs, not merely the pre-existing bundled pip) -----
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a fresh install with a real pip step succeeds"

offenders=""
for f in "$H$DEPLOY"/venv/bin/*; do
    [ -L "$f" ] && continue
    [ -f "$f" ] || continue
    case "$(head -1 "$f" 2>/dev/null)" in
        *".new"*) offenders="$offenders $(basename "$f")" ;;
    esac
done
assert_eq "" "$offenders" \
    "no venv/bin console-script shebang still names the \$DEPLOY_DIR.new staging path"

pip_out=$("$H$DEPLOY/venv/bin/pip" list 2>&1)
pip_rc=$?
assert_rc 0 "$pip_rc" "venv/bin/pip actually runs post-swap — its shebang resolves to a real interpreter"
assert_contains "$pip_out" "watchdog" \
    "pip list (invoked through the repointed shebang) shows the packages requirements.txt named"
rm -rf "$H"

# --- 14: the idempotent fast path still exits before ANY pip work ----------
# Proven the strong way: break venv/bin/pip after the first install so it
# fails if invoked at all, then re-run against the same commit. If the fast
# path ever moved to after the pip-install line, this re-run would fail on
# the broken pip; it must instead never reach that line.
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >/dev/null 2>&1
printf '#!/bin/sh\nexit 1\n' > "$H$DEPLOY/venv/bin/pip"
chmod +x "$H$DEPLOY/venv/bin/pip"

out=$(HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" 2>&1)
rc=$?
assert_rc 0 "$rc" "a same-commit re-run succeeds even though pip would fail if it were invoked"
assert_contains "$out" "Already deployed" \
    "the fast path took over rather than reaching the (now-broken) pip step"
rm -rf "$H"

# --- 15: the staging path survives NOWHERE under venv/bin, not merely on
#         line 1. Test 13 above scans `head -1`, which is what a shebang-only
#         rewrite fixes — so it passes while activate/activate.csh/activate.fish
#         still set VIRTUAL_ENV to the vanished $DEPLOY_DIR.new. A test that
#         only looks where the fix looked cannot catch the fix being too narrow,
#         which is how those three files were missed. This one reads whole
#         files, and separately proves VIRTUAL_ENV names a directory that is
#         really there.
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a fresh install succeeds"

whole_file_offenders=""
for f in "$H$DEPLOY"/venv/bin/*; do
    [ -L "$f" ] && continue
    [ -f "$f" ] || continue
    grep -Iq . "$f" 2>/dev/null || continue
    if grep -qF '.new/venv' "$f" 2>/dev/null; then
        whole_file_offenders="$whole_file_offenders $(basename "$f")"
    fi
done
assert_eq "" "$whole_file_offenders" \
    "no venv/bin file names the staging path ANYWHERE in it, not just on line 1"

# Strip surrounding quotes. Some venv versions write VIRTUAL_ENV=/path and others
# write VIRTUAL_ENV="/path"; taking the raw field left the quote characters IN the
# string, so `[ -d ]` tested a path that could never exist. Green locally, red in
# CI on a different Python — the assertion was about the environment's quoting
# style rather than about the thing it claimed to check.
venv_env=$(grep -h '^VIRTUAL_ENV=' "$H$DEPLOY/venv/bin/activate" | head -1 | cut -d= -f2-)
venv_env=$(printf '%s' "$venv_env" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")
assert_eq "1" "$([ -d "$venv_env" ] && echo 1 || echo 0)" \
    "activate's VIRTUAL_ENV names a directory that exists ($venv_env)"
rm -rf "$H"

# --- 16: a post-swap failure must leave the PREVIOUS release recoverable.
#         Before the venv/bin repoint existed, nothing after the swap could
#         fail, so deleting $OLD on the line after `mv` was safe. The repoint
#         CAN fail — that is why it carries a post-condition assert — and if
#         $OLD were still deleted first, the assert would fire with the deploy
#         area live, partially rewritten, and no way back. The rollback must
#         therefore outlive every post-swap step.
#
#         Injected for real rather than asserted textually: a copy of the
#         installer with `exit 1` planted immediately after the swap stands in
#         for any post-swap failure, so this tests the ORDERING property (is a
#         previous release still on disk when a post-swap step dies?) rather
#         than one specific way of dying.
H=$(setup)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/first.log" 2>&1
assert_rc 0 "$?" "the first install succeeds, so there is a previous release to protect"

# Advance the fixture so the second install does real work rather than taking
# the already-deployed fast path.
echo "# moved" >> "$H/src-repo/bin/demo-tool"
git -C "$H/src-repo" add -A
git -C "$H/src-repo" commit -q -m "advance"

# The failure is planted at the START OF THE REPOINT STEP, not right after the
# `mv`. Anchoring on the `mv` cannot distinguish the two orderings: the bad
# version's `rm -rf "$OLD"` sits on the line AFTER the mv, so an `exit 1` there
# short-circuits it too and the rollback survives either way. The repoint step
# is the first point that is after the deletion in the bad ordering and before
# it in the good one, which is what makes this test able to fail.
sabotaged="$H/installer-that-dies-during-the-repoint"
awk '{ if ($0 ~ /^say "Repointing venv\/bin staging paths/) print "exit 1"; print }' \
    "$INSTALLER" > "$sabotaged"
grep -q '^exit 1$' "$sabotaged" \
    || { echo "  FAIL: could not plant the post-swap failure — anchor not found"; _FAIL=$((_FAIL+1)); }

HOME="$H" sh "$sabotaged" --repo="$H/src-repo" >"$H/second.log" 2>&1
sab_rc=$?
assert_rc 1 "$sab_rc" "the sabotaged install fails after the swap, as planted"

survivors=$(find "$H$(dirname "$DEPLOY")" -maxdepth 1 -name "$(basename "$DEPLOY").old.*" 2>/dev/null | wc -l | tr -d ' ')
assert_eq "1" "$survivors" \
    "the previous release survives a post-swap failure, so the operator can roll back"
rm -rf "$H"

# --- 17: the installer accepts the option form its real caller uses.
#         bin/agent-platform-autopull's refresh_deploy_area() is the only
#         automatic refresher of the deploy area all 31 deploy-pinned jobs run
#         from. It called `--repo "$REPO"` while the installer parsed only
#         `--repo=<path>`, so every cycle exited 2 on "unknown option: --repo".
#         144 consecutive failures and zero successes, logged as "ACTION NEEDED"
#         into ~/.local/state/agent-platform/autopull.log — a file nobody reads —
#         so the deploy area was only ever refreshed by hand while every plist
#         comment claimed autopull did it.
#
#         Both forms are asserted, because fixing only the caller would leave the
#         next caller to rediscover this, and fixing only the installer would let
#         the caller drift again.
H=$(setup)
for form in "--repo=$H/src-repo" "--repo $H/src-repo"; do
    # shellcheck disable=SC2086
    HOME="$H" sh "$INSTALLER" $form --status >/dev/null 2>&1
    rc=$?
    # --status on a never-installed area exits 1 by design; 2 is the parse failure.
    assert_eq "0" "$([ "$rc" -ne 2 ] && echo 0 || echo 1)" \
        "the installer parses '$form' rather than rejecting it as an unknown option"
done

# The real caller's invocation, read out of the caller rather than restated here,
# so this fails if either side drifts.
caller_form=$(grep -oE '"\$installer" --repo=?"?\$REPO"?' "$REPO_ROOT/bin/agent-platform-autopull" | head -1)
assert_eq "1" "$([ -n "$caller_form" ] && echo 1 || echo 0)" \
    "found refresh_deploy_area's invocation in bin/agent-platform-autopull"
HOME="$H" sh "$INSTALLER" "--repo=$H/src-repo" >"$H/real.log" 2>&1
assert_rc 0 "$?" "a full install through the caller's own option form succeeds"
rm -rf "$H"

# --- 18: --deploy-dir, and the guard that keeps a branch out of production.
#         On 2026-09-01 a worker ran this installer against its own worktree with
#         --allow-non-main to verify its work. That is the use ADR-0059 rule 6
#         sanctions — but the target was the production deploy area, so every
#         deploy-pinned launchd job served unmerged branch code until a human
#         noticed. Verification and deploy-to-production were the same action.
#         --deploy-dir gives verification its own target; the guard stops the
#         production outcome being reached by leaving a flag off, without banning
#         it outright, since an operator who names the production path has said
#         what they mean.
H=$(setup)
# The fixture must actually HAVE the non-main ref. Without this the installer
# exits 1 on "does not resolve to a commit" — which is also 1, so the refusal
# assertion below would pass for entirely the wrong reason. That is exactly what
# the first version of this test did.
git -C "$H/src-repo" checkout -qb feat/x
echo "print('v2-branch')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "branch change"
git -C "$H/src-repo" checkout -q main

# a) the incident's exact command is refused, and exit 1 not 2 (a refusal, not a
#    parse error — the distinction matters because 2 is what the caller bug did)
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main >"$H/refuse.log" 2>&1
assert_rc 1 "$?" "a non-main ref into the DEFAULT deploy dir is refused"
assert_contains "$(cat "$H/refuse.log")" "REFUSING to install non-main ref" \
    "the refusal says what it refused"
assert_contains "$(cat "$H/refuse.log")" "--deploy-dir" \
    "the refusal names the option that makes verification possible"
# The suggested path must not be predictable and world-writable. It used to say
# /tmp/verify-$REF, which is guessable, and /tmp is shared — so any other local
# process could pre-create a marker (a FIFO, say) at the path this script itself
# told the operator to use. mktemp -d is unguessable and owned by the caller.
assert_eq "0" "$(grep -c 'deploy-dir=/tmp/' "$H/refuse.log" || true)" \
    "the refusal does not recommend a predictable world-writable path"
assert_contains "$(cat "$H/refuse.log")" "mktemp -d" \
    "the refusal recommends a caller-owned temporary directory instead"
assert_eq "0" "$(test -d "$H$DEPLOY" && echo 1 || echo 0)" \
    "the refused install created no production deploy area at all"

# b) verification into a scratch dir works, and leaves production alone
scratch="$H/scratch-verify"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$scratch" >"$H/scratch.log" 2>&1
assert_rc 0 "$?" "a non-main ref installs into a scratch --deploy-dir"
assert_eq "1" "$(test -f "$scratch/DEPLOYED_COMMIT" && echo 1 || echo 0)" \
    "the scratch deploy area was actually built"
assert_eq "0" "$(test -d "$H$DEPLOY" && echo 1 || echo 0)" \
    "scratch verification never touches the production deploy area"

# c) a scratch verification must NOT print the production warning. A warning that
#    fires on the safe path is how an operator learns to ignore the real one.
assert_eq "0" "$(grep -c 'PRODUCTION IS NOW SERVING' "$H/scratch.log" || true)" \
    "no false production warning on a scratch --deploy-dir install"

# d) naming the production path explicitly still works — the deliberate
#    emergency — and DOES warn
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H$DEPLOY" >"$H/explicit.log" 2>&1
assert_rc 0 "$?" "naming the production path explicitly is still permitted"
assert_contains "$(cat "$H/explicit.log")" "PRODUCTION IS NOW SERVING" \
    "a branch deliberately put into production says so, loudly"

# e) the ordinary main install is unaffected by any of this
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" >"$H/main.log" 2>&1
assert_rc 0 "$?" "a plain main install into production still succeeds"
assert_eq "0" "$(grep -c 'PRODUCTION IS NOW SERVING' "$H/main.log" || true)" \
    "a main install prints no production warning"
rm -rf "$H"

# --- 19: a differently-spelled production path is still production.
#         Both guards compared paths as literal strings, so any spelling that was
#         not byte-identical to $DEFAULT_DEPLOY_DIR silently read as "not
#         production" — a branch into the real deploy area with no warning at all,
#         which is the exact incident --deploy-dir exists to prevent. A trailing
#         slash is the ordinary case, not a contrived one: shell tab-completion
#         appends one, so an operator completing the production path out of this
#         script's own usage text produces it (reviewer run-9fea0503).
#
#         The trailing slash also broke the install outright, which is the worse
#         half and was found only by running it: "$DEPLOY_DIR.new" became
#         `<target>/.new`, a subdirectory INSIDE the target rather than a sibling,
#         and the swap died on `mv <target>/ <target>/.old.$$` — after the pip
#         step had already run. Canonicalising the target fixes both.
H=$(setup)
git -C "$H/src-repo" checkout -qb feat/x
echo "print('v2-branch')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "branch change"
git -C "$H/src-repo" checkout -q main

# a) trailing slash: must install cleanly AND be recognised as production
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H$DEPLOY/" >"$H/slash.log" 2>&1
assert_rc 0 "$?" "a trailing-slash deploy dir installs cleanly rather than dying on the swap"
assert_contains "$(cat "$H/slash.log")" "PRODUCTION IS NOW SERVING" \
    "a trailing-slash spelling of the production path is recognised as production"
assert_eq "0" "$(test -e "$H$DEPLOY/.new" && echo 1 || echo 0)" \
    "no staging directory is left INSIDE the target"

# b) a symlink pointing at production is production
ln -sfn "$H$DEPLOY" "$H/sneaky-link"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H/sneaky-link" >"$H/link.log" 2>&1
assert_rc 0 "$?" "a symlinked deploy dir installs"
assert_contains "$(cat "$H/link.log")" "PRODUCTION IS NOW SERVING" \
    "a symlink resolving to the production path is recognised as production"

# c) a genuinely different directory must still NOT warn — the guard has to
#    distinguish, not just fire on everything
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H/genuinely-elsewhere" >"$H/else.log" 2>&1
assert_rc 0 "$?" "a genuinely scratch deploy dir installs"
assert_eq "0" "$(grep -c 'PRODUCTION IS NOW SERVING' "$H/else.log" || true)" \
    "a genuinely different directory is not mistaken for production"
rm -rf "$H"

# --- 20: --deploy-dir must not destroy a directory this script did not create.
#         The swap does `mv "$DEPLOY_DIR" "$OLD"` and later `rm -rf "$OLD"`, so
#         whatever is at the path is renamed aside and then deleted. That was
#         safe while the path was a hardcoded constant; --deploy-dir made it
#         operator-supplied, and a mistyped or pasted value pointed at a checkout
#         would take uncommitted work with it, in one command, with no
#         confirmation (security-reviewer run-628a8cae). DEPLOYED_COMMIT is the
#         provenance marker because this script is the only thing that writes it.
H=$(setup)

victim="$H/precious"
mkdir -p "$victim/nested"
echo "uncommitted work" > "$victim/important.txt"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$victim" >"$H/victim.log" 2>&1
assert_rc 1 "$?" "installing over a populated non-deploy directory is refused"
assert_contains "$(cat "$H/victim.log")" "REFUSING to install over" \
    "the refusal says what it refused"
assert_eq "uncommitted work" "$(cat "$victim/important.txt" 2>/dev/null)" \
    "the refused directory is left completely untouched"

mkdir -p "$H/empty-target"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$H/empty-target" >/dev/null 2>&1
assert_rc 0 "$?" "an existing but EMPTY directory is a fine target"

HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$H/empty-target" >/dev/null 2>&1
assert_rc 0 "$?" "a prior deploy area (carrying DEPLOYED_COMMIT) can be replaced"

notdir="$H/a-file"
echo x > "$notdir"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$notdir" >"$H/notdir.log" 2>&1
assert_rc 1 "$?" "a deploy dir that exists and is not a directory is refused"
assert_eq "x" "$(cat "$notdir")" "that file is left untouched"

# A value-taking option must not swallow a following flag as its value. Not
# reachable by untrusted input today — every caller is first-party — but this
# parser feeds mv and rm -rf targets, so a value starting with '-' is a mistake
# worth catching at the door rather than downstream.
HOME="$H" sh "$INSTALLER" --repo --allow-non-main >"$H/flag.log" 2>&1
assert_rc 2 "$?" "--repo followed by a flag is a usage error, not a repo named --allow-non-main"
assert_contains "$(cat "$H/flag.log")" "got the flag" \
    "the error says the value looked like a flag"
rm -rf "$H"

# --- 21: the two gaps both reviewers found at round 3 (run-9fea0503,
#         run-628a8cae), neither blocking, both worth closing.
H=$(setup)
git -C "$H/src-repo" checkout -qb feat/x
echo "print('v2-branch')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "branch change"
git -C "$H/src-repo" checkout -q main

# a) a FOREIGN DEPLOYED_COMMIT must not open the door. The guard used to test
#    only that a file of that name existed, so a directory of real content plus
#    an empty file called DEPLOYED_COMMIT was accepted and destroyed. This is an
#    accident filter, not a security boundary — anyone who can create a file
#    there can write the right bytes — but the mistake it catches is real.
foreign="$H/looks-like-a-deploy-area"
mkdir -p "$foreign"
echo "irreplaceable" > "$foreign/work.txt"
: > "$foreign/DEPLOYED_COMMIT"          # right name, no content
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$foreign" >"$H/foreign.log" 2>&1
assert_rc 1 "$?" "an empty DEPLOYED_COMMIT does not make a foreign directory installable"
assert_eq "irreplaceable" "$(cat "$foreign/work.txt" 2>/dev/null)" \
    "the foreign directory's content survives"

echo "commit: not-a-sha" > "$foreign/DEPLOYED_COMMIT"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$foreign" >"$H/foreign2.log" 2>&1
assert_rc 1 "$?" "a malformed commit line does not make it installable either"
assert_eq "irreplaceable" "$(cat "$foreign/work.txt" 2>/dev/null)" \
    "the foreign directory's content still survives"

# b) the production warning must fire on the IDEMPOTENT FAST PATH. Re-running the
#    same command is the natural way to check what production is serving, and it
#    used to exit silently on "Already deployed" — telling the operator nothing at
#    exactly the moment they asked.
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H$DEPLOY" >"$H/fp1.log" 2>&1
assert_rc 0 "$?" "the first non-main install into production succeeds"
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H$DEPLOY" >"$H/fp2.log" 2>&1
assert_rc 0 "$?" "the re-run takes the idempotent fast path"
assert_contains "$(cat "$H/fp2.log")" "Already deployed" \
    "the re-run really did take the fast path rather than reinstalling"
assert_contains "$(cat "$H/fp2.log")" "PRODUCTION IS NOW SERVING" \
    "the fast path still says production is on a branch"

# c) and the fast path must NOT warn for a scratch target
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H/fp-scratch" >/dev/null 2>&1
HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --ref=feat/x --allow-non-main \
    --deploy-dir="$H/fp-scratch" >"$H/fp3.log" 2>&1
assert_contains "$(cat "$H/fp3.log")" "Already deployed" \
    "the scratch re-run also took the fast path"
assert_eq "0" "$(grep -c 'PRODUCTION IS NOW SERVING' "$H/fp3.log" || true)" \
    "no false production warning on a scratch fast path"
rm -rf "$H"

# --- 22: the marker check must never HANG, and must fail closed on every file
#         type that is not a readable regular file.
#
#         Reading an operator-supplied path with grep is the new risk introduced
#         by validating the marker's CONTENT rather than its mere existence. A
#         marker that is a FIFO makes grep block forever waiting for a writer,
#         and bin/agent-platform-autopull invokes this script every ~10 minutes —
#         so a hang wedges the job that keeps the deploy area current, which is
#         worse than anything the marker check exists to prevent. Found by
#         creating each file type and running it, not by reading the code.
#
#         Every case here is bounded by `timeout`: a test for a hang that itself
#         hangs reports nothing.
H=$(setup)

# Bound a command's runtime WITHOUT GNU `timeout`. macOS ships no `timeout` —
# it arrives with homebrew coreutils, as `gtimeout` symlinked to `timeout`, so a
# developer machine with coreutils installed has it and a stock macOS CI runner
# does not. Using it here passed locally and failed in CI with exit 127,
# "command not found", on every case below. The suite must not depend on a tool
# the target may lack, least of all in the tests that exist to prove the
# installer cannot hang.
#
# Returns the command's own status, or 137 (128+SIGKILL) when the bound fired.
# SIGNAL THE PROCESS GROUP, NOT THE PID. `kill -9 <pid>` reaches only the direct
# child. If the thing actually blocked is a grandchild — which is the exact shape
# this helper exists to bound, a `grep` blocked on a FIFO inside the installer —
# killing the top-level pid orphans the blocked process rather than freeing it,
# and the "bound" silently does not bound (reviewer round 6). `set -m` puts each
# background job in its own process group, and `kill -9 -$pid` signals that whole
# group. The watchdog gets the same treatment on the fast path, so its `sleep`
# does not linger as an orphan for the length of the bound.
#
# Job control is enabled only inside this function and restored on the way out,
# because leaving it on changes how the rest of the suite's background work
# reports.
#
# ONE RACE IS LEFT AND IS NOT CLOSED HERE, deliberately, AT BOTH KILL SITES.
# `wait` reaps the child, freeing its pid for reuse, and only then is the watchdog
# stopped — so a watchdog firing inside the microseconds between those two steps
# could signal a recycled pid (security-reviewer round 6). The same holds for the
# second kill, the one that stops the watchdog itself: its pid is equally
# reusable, and the reasoning below applies to it unchanged. Neither call probes
# first, and that is the point — a probe would move the window, not remove it
# (reviewer round 7). Narrowing it with a `kill -0` probe
# first would not close it, since the same window sits between the probe and the
# signal, and writing it as though it did would be the exact overclaim this suite
# keeps catching elsewhere. Reaching it needs a command finishing within
# microseconds of a 30s or 120s bound, which nothing here can steer; this helper
# runs only in tests and never touches the deploy area. Recorded rather than
# papered over, so anyone lifting this into non-test code knows what they are
# taking.
#
# Returns the command's own status, or 137 (128+SIGKILL) when the bound fired.
_run_bounded() { # <seconds> <command...>
    _rb_secs=$1
    shift
    case "$-" in *m*) _rb_had_m=1 ;; *) _rb_had_m=0 ;; esac
    set -m
    "$@" &
    _rb_pid=$!
    ( sleep "$_rb_secs"; kill -9 -"$_rb_pid" 2>/dev/null || kill -9 "$_rb_pid" 2>/dev/null ) >/dev/null 2>&1 &
    _rb_watch=$!
    wait "$_rb_pid" 2>/dev/null
    _rb_rc=$?
    kill -9 -"$_rb_watch" 2>/dev/null || kill -9 "$_rb_watch" 2>/dev/null
    wait "$_rb_watch" 2>/dev/null
    [ "$_rb_had_m" -eq 1 ] || set +m
    return "$_rb_rc"
}

_marker_case() { # <label> <setup-shell> <expect-victim-intact>
    _mc_dir="$H/marker-$1"
    mkdir -p "$_mc_dir"
    echo "victim" > "$_mc_dir/work.txt"
    eval "$2"
    _run_bounded 30 env HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" \
        --deploy-dir="$_mc_dir" >"$H/marker-$1.log" 2>&1
    _mc_rc=$?
    assert_eq "0" "$([ "$_mc_rc" -eq 137 ] && echo 1 || echo 0)" \
        "a DEPLOYED_COMMIT that is a $1 does not hang the installer"
    assert_rc 1 "$_mc_rc" "a DEPLOYED_COMMIT that is a $1 is refused"
    assert_eq "victim" "$(cat "$_mc_dir/work.txt" 2>/dev/null)" \
        "the directory holding a $1 marker is left untouched"
}

_marker_case "fifo"       'mkfifo "$_mc_dir/DEPLOYED_COMMIT"'
_marker_case "directory"  'mkdir -p "$_mc_dir/DEPLOYED_COMMIT"'
_marker_case "unreadable" 'echo x > "$_mc_dir/DEPLOYED_COMMIT"; chmod 000 "$_mc_dir/DEPLOYED_COMMIT"'

# and the positive control: a well-formed marker still identifies a replaceable
# deploy area, so the guard distinguishes rather than refusing everything
ok="$H/marker-ok"
mkdir -p "$ok"
printf 'ref: main\ncommit: %040d\n' 0 > "$ok/DEPLOYED_COMMIT"
_run_bounded 120 env HOME="$H" sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$ok" >/dev/null 2>&1
assert_rc 0 "$?" "a well-formed marker still identifies a replaceable deploy area"
chmod -R u+rwX "$H" 2>/dev/null || true
rm -rf "$H"

# --- 23: _run_bounded's group-kill path, tested directly.
#
#         The three marker cases above all resolve fast, because the installer fix
#         they guard already stops it blocking on the marker. So the bound never
#         fires there and `kill -9 -"$pid"` never runs: in the passing suite, the
#         safety mechanism is dead code. If a future edit broke `set -m` or the
#         group-kill outright, nothing here would notice until an installer
#         regression reintroduced a real hang — and then the helper meant to catch
#         it would be the thing that had quietly stopped working
#         (security-reviewer round 7).
#
#         Its verification was a manual one-off quoted in a commit message, which
#         is not a test. This exercises the helper against exactly the shape it
#         exists for: a GRANDCHILD blocked on a FIFO. Killing the top-level pid
#         alone would orphan that grandchild and leave it blocked forever, so
#         asserting the grandchild is GONE is what distinguishes a working group
#         kill from a bound that only looks like one.
H=$(setup)
fifo_dir="$H/bounded-helper"
mkdir -p "$fifo_dir"
mkfifo "$fifo_dir/blocker"

# THE TRAILING `; true` IS LOAD-BEARING. `sh -c '<one command>'` EXECS that
# command in place, so outer and inner shells share a pid and there is no
# grandchild at all — a pid-only kill reaches it, and the test passes whether or
# not the group kill works. That is what the first version of this test did:
# both mutations (dropping the group form, dropping `set -m`) went green against
# it. A second statement forces the outer shell to fork rather than exec, which
# is what makes the process tree deep enough to measure anything.
_run_bounded 3 sh -c "sh -c 'echo \$\$ > \"$fifo_dir/gpid\"; exec grep -q nomatch \"$fifo_dir/blocker\"' ; true"
bounded_rc=$?
assert_eq "137" "$bounded_rc" \
    "a command whose grandchild blocks is killed by the bound, not left running"

# The grandchild recorded its own pid before blocking. If the group kill worked it
# is gone; if only the top-level pid was signalled it is still sitting on the FIFO.
sleep 1
gpid=$(cat "$fifo_dir/gpid" 2>/dev/null)
assert_eq "1" "$([ -n "$gpid" ] && echo 1 || echo 0)" \
    "the grandchild recorded its pid, so this test is measuring something"
# A BARE `kill -0 "$gpid"` WOULD TRUST A PID THAT MAY HAVE BEEN REUSED. The pid is
# read from a file a second or so after the process it names was killed, so the
# kernel may already have handed that number to something else. That would fail
# this assertion for a reason unrelated to what it tests — a flake in the safe
# direction — and would make the cleanup below signal a process this test never
# created (security-reviewer round 8). Blast radius is small (a single-job runner,
# so the only candidates are this suite's own short-lived children) but the check
# costs one `ps`.
#
# Matching the command name is what distinguishes "our grandchild is still
# blocked" from "this pid now belongs to something else". A pid that exists but
# is no longer our grep/sh counts as GONE, which is the correct reading: the
# grandchild we were watching is not running any more.
_still_our_grandchild() { # <pid>
    [ -n "$1" ] || return 1
    kill -0 "$1" 2>/dev/null || return 1
    case "$(ps -o comm= -p "$1" 2>/dev/null)" in
        *grep*|*/sh|sh) return 0 ;;
        *) return 1 ;;
    esac
}

assert_eq "0" "$(_still_our_grandchild "$gpid" && echo 1 || echo 0)" \
    "the blocked GRANDCHILD was killed too, not orphaned by a pid-only kill"
# Same identity check before signalling, so a recycled pid is never the target.
_still_our_grandchild "$gpid" && kill -9 "$gpid" 2>/dev/null

# And the bound must not fire on something that simply finishes.
_run_bounded 30 true
assert_rc 0 "$?" "a fast success returns its own status, not the bound's"
_run_bounded 30 sh -c 'exit 3'
assert_rc 3 "$?" "a fast failure returns its own status, so a hang stays distinguishable"
rm -rf "$H"

# --- a commit missing a required runtime path is REFUSED, by name -----------
# The deploy area is a COPY: a path it does not carry is absent at the next
# scheduled run, not fetched from the checkout. That is how the KB golden-set
# eval failed silently every night from 2026-08-31 while DEPLOYED_COMMIT stayed
# current. Refusing here is the only place the operator can still see it.
H=$(setup)
git -C "$H/src-repo" rm -q -r --cached eval >/dev/null
rm -rf "$H/src-repo/eval"
git -C "$H/src-repo" commit -qm "drop eval/"

out=$(HOME="$H" "$INSTALLER" --repo "$H/src-repo" --deploy-dir "$H$DEPLOY" 2>&1)
rc=$?

assert_rc 1 "$rc" "an install whose commit lacks a required runtime path refuses"
assert_contains "$out" "eval is missing" \
    "the refusal NAMES the missing path rather than failing as a git error"
assert_contains "$out" "COPY" \
    "the refusal says why the path mattered, so the reader can act on it"
assert_eq "0" "$(test -d "$H$DEPLOY/bin" && echo 1 || echo 0)" \
    "a refused install deploys nothing at all"
rm -rf "$H"

# --- 24: a refresh that changes lib/knowledge/ restarts the knowledge-watcher,
#         checked against the pid; one that does not, does not; a first install
#         cannot tell and restarts. A launchctl stub on PATH records argv and
#         hands back a new pid after every kickstart, so the assertions are on
#         what the installer asked launchd for and what it said about the answer
#         (issue_01M1WXDWRX620XB65F0AMAZRGJ: the watcher enforced a stale
#         ALLOWED_PROJECTS for days after this script had deployed the new one). --
# The stub's `print` reports an `arguments = { }` block (python3 then the
# knowledge-watcher script, matching launchd/com.convivy.knowledge-watcher.plist's
# real ProgramArguments shape) with both entries inside
# $LAUNCHCTL_STUB_DEPLOY_DIR (the caller sets this to the deploy area the
# installer under test will compute), because the fixed installer now walks
# every argv entry to see whether the loaded job's SCRIPT runs from this
# deploy area, not merely whether DEPLOY_DIR equals the sandboxed HOME's
# default — see scenario 25 below for the case where it does not match.
_stub_launchctl() { # <home>
    mkdir -p "$1/stubbin"
    cat > "$1/stubbin/launchctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    # grep -c prints 0 AND exits 1 on no match, so `|| echo 0` would make
    # two lines; take the count as printed.
    n=$(grep -c '^kickstart' "$LAUNCHCTL_LOG" 2>/dev/null); n=${n:-0}
    printf '\targuments = {\n\t\t%s/venv/bin/python3\n\t\t%s/bin/knowledge-watcher\n\t}\n\tstate = running\n\tpid = %s\n' \
        "$LAUNCHCTL_STUB_DEPLOY_DIR" "$LAUNCHCTL_STUB_DEPLOY_DIR" $((548 + n))
    ;;
esac
exit 0
STUB
    chmod +x "$1/stubbin/launchctl"
}

# H_REAL, NOT $H, IS WHAT DEPLOY_DIR_CANON RESOLVES TO. mktemp -d hands back a
# path through /var, which on macOS is itself a symlink to /private/var; the
# installer's canonical_path() resolves that (pwd -P) when it builds
# DEPLOY_DIR_CANON, so a stub reporting a `program =` line built from the raw
# $H would never match even the "should restart" case.
H=$(setup)
H_REAL=$(cd "$H" && pwd -P)
_stub_launchctl "$H"
: > "$H/launchctl.log"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_contains "$(cat "$H/launchctl.log")" "kickstart -k gui/$(id -u)/com.convivy.knowledge-watcher" \
    "first install: no previous commit, so the watcher is restarted (cannot tell)"

: > "$H/launchctl.log"
echo "print('v2')" > "$H/src-repo/bin/some-tool"
git -C "$H/src-repo" commit -qam "bin only"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_eq "0" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "a refresh that leaves lib/knowledge alone does not restart the watcher"
assert_contains "$(cat "$H/out.log")" "not restarted" \
    "and says so"

: > "$H/launchctl.log"
echo "# admitted a project" >> "$H/src-repo/lib/knowledge/knowledge.py"
git -C "$H/src-repo" commit -qam "lib/knowledge change"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_eq "1" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "a refresh that changes lib/knowledge restarts the watcher once"
assert_contains "$(cat "$H/out.log")" "restarted, pid 548 -> 549" \
    "the restart is reported against the pid, before and after"
assert_eq "0" "$(grep -c 'needs no launchd step' "$H/out.log")" \
    "the old 'needs no launchd step' line is gone"

# The pid not changing is reported as a failed restart, with the command to run.
cat > "$H/stubbin/launchctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
[ "$1" = print ] && printf '\targuments = {\n\t\t%s/venv/bin/python3\n\t\t%s/bin/knowledge-watcher\n\t}\n\tstate = running\n\tpid = 548\n' \
    "$LAUNCHCTL_STUB_DEPLOY_DIR" "$LAUNCHCTL_STUB_DEPLOY_DIR"
exit 0
STUB
: > "$H/launchctl.log"
echo "# another admission" >> "$H/src-repo/lib/knowledge/knowledge.py"
git -C "$H/src-repo" commit -qam "lib/knowledge change 2"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a failed restart does not fail the deploy"
assert_contains "$(cat "$H/out.log")" "did not produce a new pid" \
    "a pid that did not change is reported as a failed restart"
assert_contains "$(cat "$H/out.log")" "Restart by hand: launchctl kickstart -k" \
    "with the command to run"
# The previous commit is read with the marker's own anchored shape: a marker
# whose first `commit:` line is a git option does not reach git as one.
_stub_launchctl "$H"
: > "$H/launchctl.log"
marker="$H$DEPLOY/DEPLOYED_COMMIT"
real_line=$(grep -E '^commit: [0-9a-f]{40}$' "$marker")
{ echo "commit: --output=$H/pwned"; echo "ref: main"; echo "$real_line"; } > "$marker"
echo "# admission after a crafted marker" >> "$H/src-repo/lib/knowledge/knowledge.py"
git -C "$H/src-repo" commit -qam "lib/knowledge change after crafted marker"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_eq "0" "$(test -e "$H/pwned" && echo 1 || echo 0)" \
    "a dash-leading first commit line never reaches git as an option"
assert_eq "1" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "the real commit line is the one used, so the lib/knowledge change still restarts"

# A verification run against a scratch --deploy-dir must not kick the real
# service, even with lib/knowledge changed and no previous marker there.
_stub_launchctl "$H"
: > "$H/launchctl.log"
echo "# yet another admission" >> "$H/src-repo/lib/knowledge/knowledge.py"
git -C "$H/src-repo" commit -qam "lib/knowledge change 3"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" --deploy-dir="$H/verify" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "a scratch --deploy-dir install succeeds"
assert_eq "0" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "a scratch --deploy-dir never restarts the production watcher"
assert_contains "$(cat "$H/out.log")" "not the production deploy area" \
    "and says why"
rm -rf "$H"

# --- 25: THE DEFECT ITSELF (measured 2026-09-13). DEPLOY_DIR_CANON equalling
#         DEFAULT_DEPLOY_DIR_CANON is a fact about the sandboxed $HOME, not
#         about what launchd actually has loaded — `gui/$(id -u)/<label>`
#         addresses the real per-user domain no matter what HOME this process
#         has. Before this scenario's fix, a sandboxed run whose stub reported
#         a loaded job running from ANYWHERE (including this test's own
#         previous run, or — unstubbed — the real production watcher) was
#         kickstarted the moment lib/knowledge changed, because the installer
#         only ever compared two HOME-derived strings to each other. This
#         proves the installer now also checks WHAT THE LOADED JOB RUNS,
#         refusing to restart a job whose program sits outside DEPLOY_DIR.
H=$(setup)
H_REAL=$(cd "$H" && pwd -P)
mkdir -p "$H/elsewhere/venv/bin"
_stub_launchctl "$H"
: > "$H/launchctl.log"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_contains "$(cat "$H/launchctl.log")" "kickstart" \
    "setup: the first install (job running from THIS deploy area) restarts, establishing a baseline"

: > "$H/launchctl.log"
echo "# admission against a foreign program path" >> "$H/src-repo/lib/knowledge/knowledge.py"
git -C "$H/src-repo" commit -qam "lib/knowledge change with a foreign loaded job"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL/elsewhere" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_eq "0" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "code changed, but the LOADED job runs from a different tree — no kickstart is recorded"
assert_contains "$(cat "$H/out.log")" "not restarted" \
    "and the installer says so"
assert_contains "$(cat "$H/out.log")" "$H_REAL/elsewhere" \
    "naming the path the loaded job actually runs from"
rm -rf "$H"

# --- 26: the not-loaded branch (reviewer, PR #565 round 2). Every stub above
#         always reports `print` as loaded, so nothing exercised the `|| true`
#         around `print_out=$(launchctl print ...) || true` — the fix that
#         keeps a label launchd reports as not loaded from aborting the whole
#         install under `set -eu`. This stub's `print` returns nothing and
#         exits 1, exactly the shape real launchctl gives for a label that
#         isn't loaded (and the one tests/bash/lib.sh's own default stub also
#         gives, which is what let this branch go unexercised even though
#         every OTHER scenario in this file overrides it). A fresh install has
#         no previous commit, so "changed" is unconditionally true and this
#         reaches the print check on the very first run. --
H=$(setup)
mkdir -p "$H/stubbin"
cat > "$H/stubbin/launchctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print) exit 1 ;;
esac
exit 0
STUB
chmod +x "$H/stubbin/launchctl"
: > "$H/launchctl.log"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" \
    "a not-loaded watcher label does not abort the install under set -eu"
assert_contains "$(cat "$H/out.log")" "is not loaded — nothing to restart" \
    "and the installer reports the job as not loaded"
assert_eq "0" "$(grep -c '^kickstart' "$H/launchctl.log")" \
    "no kickstart is recorded against a job that was never loaded"
rm -rf "$H"

# --- 27: CQ-428 fallback — the watcher is still loaded under its
#         pre-migration label, exactly the shape of a machine that has not
#         yet run `install.sh --migrate-labels`. The legacy label is an
#         arbitrary stand-in here, never the real pre-rename one: the
#         installer finds it by suffix match against bare `launchctl list`
#         (`legacy_knowledge_watcher_label`), never by hardcoding it. Without
#         the fallback, the new label alone reads as not-loaded and a real
#         code change never reaches the live watcher until someone migrates
#         by hand.
H=$(setup)
H_REAL=$(cd "$H" && pwd -P)
mkdir -p "$H/stubbin"
cat > "$H/stubbin/launchctl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  list)
    if [ -z "$2" ]; then
      printf 'PID\tStatus\tLabel\n123\t0\tcom.example-legacy.knowledge-watcher\n'
      exit 0
    fi
    exit 1
    ;;
  print)
    case "$2" in
      */com.example-legacy.knowledge-watcher)
        n=$(grep -c '^kickstart' "$LAUNCHCTL_LOG" 2>/dev/null); n=${n:-0}
        printf '\targuments = {\n\t\t%s/venv/bin/python3\n\t\t%s/bin/knowledge-watcher\n\t}\n\tstate = running\n\tpid = %s\n' \
            "$LAUNCHCTL_STUB_DEPLOY_DIR" "$LAUNCHCTL_STUB_DEPLOY_DIR" $((548 + n))
        ;;
      *) exit 1 ;;
    esac
    ;;
esac
exit 0
STUB
chmod +x "$H/stubbin/launchctl"
: > "$H/launchctl.log"
HOME="$H" PATH="$H/stubbin:$PATH" LAUNCHCTL_LOG="$H/launchctl.log" \
    LAUNCHCTL_STUB_DEPLOY_DIR="$H_REAL$DEPLOY" \
    sh "$INSTALLER" --repo="$H/src-repo" >"$H/out.log" 2>&1
assert_contains "$(cat "$H/launchctl.log")" "kickstart -k gui/$(id -u)/com.example-legacy.knowledge-watcher" \
    "a watcher still loaded under its pre-migration label is kickstarted BY that label"
assert_eq "0" "$(grep -c 'com.convivy.knowledge-watcher is not loaded' "$H/out.log")" \
    "the fallback means this is not reported as not-loaded"
rm -rf "$H"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
