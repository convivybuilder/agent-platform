#!/bin/bash
# Tests for bin/agent-platform-autopull: the job that keeps the DEPLOYED platform
# current, since ~/.local/bin symlinks into this repo's working tree (ARCH-229)
# and a merge to origin/main deploys nothing on its own.
#
# The REFUSALS are the load-bearing part, not the happy path. This job
# fast-forwards the checkout that IS production, so a guard that fails open can
# clobber uncommitted work or yank files out from under a running orchestrator.
# Each guard is therefore driven against a real git repo rather than asserted.
#
# The script hardcodes $HOME/Convivy/agent-platform, so a hermetic test points
# HOME at a tmp dir holding a fake origin + checkout — the same technique
# test_knowledge_main_guard.sh uses for the same reason.
#
# Run under /bin/bash 3.2 like every test here; the script under test is zsh
# (excluded from the ARCH-082 contract by lint.sh's shebang filter), and bash
# invoking it is exactly how launchd will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

AUTOPULL="$REPO_ROOT/bin/agent-platform-autopull"

# Build a fake HOME with an origin and a checkout of it. Echoes the fake HOME.
#
# The bare origin's HEAD is set to `main` EXPLICITLY, and that line is the whole
# reason this fixture is written the way it is. `git init --bare` points HEAD at
# init.defaultBranch, which is `master` on the GitHub macOS runner and `main`
# locally. Push `main` to such a repo and its HEAD names a ref that does not
# exist — harmless until something clones it, at which point git says "remote
# HEAD refers to nonexistent ref, unable to checkout" and produces an EMPTY
# working tree.
#
# The first version of this file skipped that line. It passed locally and failed
# on CI with six confident assertion failures about the script under test, none
# of which were about the script: advance_origin's clone came back empty, so
# origin never advanced. A fixture that breaks quietly reports the wrong culprit.
#
# Same reason the checkout's branch is forced rather than assumed.
setup() { # -> echoes fake HOME
    local home origin repo
    home=$(mktemp -d)
    origin="$home/origin.git"
    repo="$home/Convivy/agent-platform"

    git init -q --bare "$origin"
    git -C "$origin" symbolic-ref HEAD refs/heads/main

    mkdir -p "$repo"
    git init -q "$repo"
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    _git_ident "$repo"
    mkdir -p "$repo/bin" "$repo/launchd"
    echo v1 > "$repo/bin/thing"
    git -C "$repo" add -A
    git -C "$repo" commit -qm seed
    git -C "$repo" remote add origin "$origin"
    git -C "$repo" push -q -u origin main || {
        echo "  FIXTURE BROKEN: seed push failed; every assertion below is meaningless" >&2
        exit 1
    }
    echo "$home"
}

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# Add a commit to origin so the checkout falls behind. Extra args are paths to
# create, letting a test stage a new bin or a changed plist.
#
# Fails loudly rather than returning quietly: a fixture that cannot advance
# origin makes "did not move HEAD" pass for the wrong reason, which is how the
# first version of this file reported six confident failures about the script
# when the script was never the problem.
advance_origin() { # <home> [path...]
    local home=$1; shift
    local push
    push=$(mktemp -d)
    git clone -q "$home/origin.git" "$push/w" || {
        echo "  FIXTURE BROKEN: cannot clone origin" >&2; exit 1; }
    _git_ident "$push/w"
    local p
    if [ "$#" -eq 0 ]; then
        echo more >> "$push/w/bin/thing"
    else
        for p in "$@"; do
            mkdir -p "$(dirname "$push/w/$p")"
            echo x > "$push/w/$p"
        done
    fi
    git -C "$push/w" add -A
    git -C "$push/w" commit -qm "advance"
    git -C "$push/w" push -q origin main || {
        echo "  FIXTURE BROKEN: cannot push to origin" >&2; exit 1; }
    rm -rf "$push"
}

run_autopull() { # <home> -> echoes rc, leaves log at $home/.local/state/...
    HOME="$1" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$1")" >/dev/null 2>&1
    echo $?
}

logfile() { echo "$1/.local/state/agent-platform/autopull.log"; }
log_of()  { cat "$(logfile "$1")" 2>/dev/null; }
head_of() { git -C "$1/Convivy/agent-platform" rev-parse HEAD; }

# --- 1. already current: silent, and writes nothing ---------------------------
# A quiet cycle must stay quiet. This job runs every 10 minutes; a line per cycle
# would bury the events that matter under ~144 no-ops a day.
H=$(setup)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "already current exits 0"
assert_eq "" "$(log_of "$H")" "already current writes nothing to the log"
rm -rf "$H"

# --- 2. clean fast-forward actually moves the checkout ------------------------
H=$(setup)
before=$(head_of "$H")
advance_origin "$H"
rc=$(run_autopull "$H")
after=$(head_of "$H")
assert_rc 0 "$rc" "clean fast-forward exits 0"
if [ "$before" != "$after" ]; then _PASS=$((_PASS + 1)); else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: clean fast-forward did not move HEAD"; fi
case "$(log_of "$H")" in
    *"now live"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: fast-forward did not log what became live";;
esac
rm -rf "$H"

# --- 3. dirty tree: refuses, and does NOT pull --------------------------------
# The refusal is the point, but so is exit 1 — a silent skip here looks exactly
# like a healthy quiet cycle, which is the state this job exists to end.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
echo "uncommitted work" >> "$H/Convivy/agent-platform/bin/thing"
rc=$(run_autopull "$H")
assert_rc 1 "$rc" "dirty tree exits non-zero (loud, not a silent skip)"
assert_eq "$before" "$(head_of "$H")" "dirty tree does not move HEAD"
case "$(log_of "$H")" in
    *REFUSING*dirty*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: dirty tree did not log a refusal";;
esac
rm -rf "$H"

# --- 4. non-main branch: refuses (the ARCH-229 direction) ---------------------
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
git -C "$H/Convivy/agent-platform" checkout -q -b feature/x
rc=$(run_autopull "$H")
assert_rc 1 "$rc" "non-main branch exits non-zero"
assert_eq "$before" "$(head_of "$H")" "non-main branch does not move HEAD"
rm -rf "$H"

# --- 5. diverged history: refuses rather than forcing -------------------------
H=$(setup)
advance_origin "$H"
echo local > "$H/Convivy/agent-platform/bin/localonly"
git -C "$H/Convivy/agent-platform" add -A
git -C "$H/Convivy/agent-platform" commit -qm "local-only"
before=$(head_of "$H")
rc=$(run_autopull "$H")
assert_rc 1 "$rc" "diverged checkout exits non-zero"
assert_eq "$before" "$(head_of "$H")" "diverged checkout does not move HEAD"
rm -rf "$H"

# --- 6. a live run holding the checkout defers, and does not pull -------------
# planning-orchestrator runs in the PRIMARY checkout, so fast-forwarding under it
# changes files mid-run. Unlike the refusals this resolves on its own, so it
# exits 0 and retries next cycle. A fake agentctl on the fake HOME's PATH
# reports a run whose worktree is the checkout.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/agentctl" <<EOF
#!/bin/bash
case "\$1" in
  list)   echo "run-deadbeef  planning-orchestrator  pid=1  1m  agent-platform  — Task: x";;
  status) echo "worktree:     $H/Convivy/agent-platform";;
esac
EOF
chmod +x "$H/.local/bin/agentctl"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a held checkout defers with exit 0 (it resolves on its own)"
assert_eq "$before" "$(head_of "$H")" "a held checkout does not move HEAD"
case "$(log_of "$H")" in
    *deferring*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: held checkout did not log the deferral";;
esac
rm -rf "$H"

# --- 7. a run in some OTHER worktree does not block the pull ------------------
# The guard must key on the worktree PATH, not on "any run is alive". Workers run
# in their own worktrees constantly; blocking on those would mean never pulling.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/agentctl" <<EOF
#!/bin/bash
case "\$1" in
  list)   echo "run-deadbeef  coder  pid=1  1m  vivy  — Task: x";;
  status) echo "worktree:     $H/Convivy/worktrees/vivy.coder-something";;
esac
EOF
chmod +x "$H/.local/bin/agentctl"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a run in another worktree exits 0"
if [ "$before" != "$(head_of "$H")" ]; then _PASS=$((_PASS + 1)); else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: a run in an unrelated worktree wrongly blocked the pull"; fi
rm -rf "$H"

# --- 7b. a listed run whose worktree cannot be read counts as HELD ------------
# The uncertain answer must be the safe one. Both reviewers on PR #288 found this
# failing OPEN: `wt` came back empty, the equality test quietly failed, the loop
# fell through and the pull proceeded — under a live orchestrator, and against
# the function's own comment claiming it failed HELD.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/agentctl" <<'EOF'
#!/bin/bash
case "$1" in
  list)   echo "run-deadbeef  planning-orchestrator  pid=1  1m  agent-platform  — Task: x";;
  status) echo "agent:        planning-orchestrator";;   # no worktree: line
esac
EOF
chmod +x "$H/.local/bin/agentctl"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "an unreadable worktree defers with exit 0"
assert_eq "$before" "$(head_of "$H")" "an unreadable worktree does NOT pull (fails HELD, not open)"
# And it must say so. A silent fail-HELD would freeze deploys forever while every
# cycle looked like an ordinary quiet deferral.
case "$(log_of "$H")" in
    *"cannot resolve a worktree"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: unreadable worktree deferred silently";;
esac
rm -rf "$H"

# --- 7c. a failing `agentctl list` counts as HELD -----------------------------
# A transient list failure is indistinguishable from "nothing is running", so it
# cannot be read as the latter. Exit 0 with no rows still means nothing running —
# that is a real answer and test 7 covers the pull proceeding.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/agentctl" <<'EOF'
#!/bin/bash
case "$1" in
  list)   echo "socket error" >&2; exit 3;;
  status) exit 3;;
esac
EOF
chmod +x "$H/.local/bin/agentctl"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a failing agentctl list defers with exit 0"
assert_eq "$before" "$(head_of "$H")" "a failing agentctl list does NOT pull (fails HELD)"
case "$(log_of "$H")" in
    *"cannot enumerate runs"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: a failing agentctl list deferred silently";;
esac
rm -rf "$H"

# --- 7d. the holder is found even when it is not the FIRST run listed ---------
# Every other run-detection test uses a single run, so none of them would notice
# the list being truncated to its first row. Raised on PR #288 review. The
# described mechanism (unquoted expansion collapsing newlines) does not fire
# under default zsh, which leaves SH_WORD_SPLIT off — but the coverage gap is
# real either way, and this pins the behaviour rather than the option setting.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/agentctl" <<EOF
#!/bin/bash
case "\$1" in
  list)
    echo "run-aaa1  coder                  pid=1  1m  vivy  — Task: x"
    echo "run-bbb2  reviewer               pid=2  1m  vivy  — Task: x"
    echo "run-ccc3  planning-orchestrator  pid=3  1m  agent-platform  — Task: x"
    ;;
  status)
    case "\$2" in
      run-ccc3) echo "worktree:     $H/Convivy/agent-platform";;
      *)        echo "worktree:     $H/Convivy/worktrees/vivy.something";;
    esac
    ;;
esac
EOF
chmod +x "$H/.local/bin/agentctl"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a holder listed third still defers with exit 0"
assert_eq "$before" "$(head_of "$H")" "a holder listed third is still found (list is not truncated to row 1)"
rm -rf "$H"

# --- 8. a pull that adds a bin or changes a plist says so ---------------------
# Symlinks cover files that already exist; a NEW bin needs install.sh, and a
# plist is a COPY (ARCH-116) needing bootout+bootstrap (ARCH-104). Landing either
# silently is how a fix looks deployed while being inert.
H=$(setup)
advance_origin "$H" "bin/brandnew" "launchd/com.convivy.thing.plist"
run_autopull "$H" >/dev/null
out=$(log_of "$H")
case "$out" in
    *"ACTION NEEDED"*install.sh*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: a new bin did not produce an install.sh action";;
esac
case "$out" in
    *"ACTION NEEDED"*plists*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: a changed plist did not produce a bootout/bootstrap action";;
esac
rm -rf "$H"

# --- 9. unreachable origin is a silent no-op ----------------------------------
# A sleeping or off-network laptop must not write a line every 10 minutes.
H=$(setup)
mv "$H/origin.git" "$H/origin.git.away"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "unreachable origin exits 0"
assert_eq "" "$(log_of "$H")" "unreachable origin writes nothing"
rm -rf "$H"

# --- 9b. a LOCAL git error during fetch is LOUD, not a quiet no-op ------------
# The 2026-10-07 incident: a dispatched run's own, not-yet-imported commit left
# refs/heads/<branch> (shared across every worktree of this checkout) pointing
# at an object that only the run's own private object store held. `git fetch`
# failed with "bad object", and the blanket quiet-on-any-failure path above
# would otherwise swallow this exactly like an asleep laptop. Reproduced here
# the same way agent-platform's own run_sandbox tests do: a linked worktree
# whose commit lands only in a redirected GIT_OBJECT_DIRECTORY.
H=$(setup)
REPO="$H/Convivy/agent-platform"
git -C "$REPO" worktree add -q -b agent/foo "$H/wt"
GITCOMMON=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)
LINKEDGIT=$(git -C "$H/wt" rev-parse --path-format=absolute --git-dir)
mkdir -p "$LINKEDGIT/agentctl-private-objects"
GIT_OBJECT_DIRECTORY="$LINKEDGIT/agentctl-private-objects" \
    GIT_ALTERNATE_OBJECT_DIRECTORIES="$GITCOMMON/objects" \
    git -C "$H/wt" commit -q --allow-empty -m "private, unimported"
SIGNAL_MARKER="$H/.signal-recorded-local-fetch"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "\$@" >> $SIGNAL_MARKER
cat >> $SIGNAL_MARKER
EOF
chmod +x "$H/.local/bin/python3"
before=$(head_of "$H")
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "local fetch error (bad object) exits 1, not a quiet no-op"
assert_eq "$before" "$(head_of "$H")" "local fetch error does not move HEAD"
assert_eq "1" "$(log_of "$H" | grep -qi "LOCAL git error" && echo 1 || echo 0)" \
    "local fetch error logs a line naming the reason"
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" \
    "local fetch error invokes the signal function"
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *fetch-local-error*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: signal does not carry the fetch-local-error reason";;
esac
rm -rf "$H"

# --- 9c. a genuinely unreachable origin still stays a quiet no-op, even with a
# fake python3 on PATH ready to record a signal — the fix to 9b must not loosen 9 ---
H=$(setup)
mv "$H/origin.git" "$H/origin.git.away"
SIGNAL_MARKER="$H/.signal-should-not-exist-offline"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "\$@" >> $SIGNAL_MARKER
cat >> $SIGNAL_MARKER
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "unreachable origin still exits 0 with the local-error check in place"
assert_eq "" "$(log_of "$H")" "unreachable origin still writes nothing"
assert_eq "0" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" \
    "unreachable origin still does not invoke the signal function"
rm -rf "$H"

# --- 10. the plist points at the installed bin, not the working tree ----------
# The plist must exec ~/.local/bin (itself a symlink install.sh manages), the
# same indirection every other job uses.
PLIST="$REPO_ROOT/launchd/com.convivy.agent-platform-autopull.plist"
if [ -f "$PLIST" ]; then _PASS=$((_PASS + 1)); else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: launchd/com.convivy.agent-platform-autopull.plist is missing"; fi
# Templated: install.sh renders __HOME__ to the installing user's $HOME.
case "$(cat "$PLIST" 2>/dev/null)" in
    *"__HOME__/.local/bin/agent-platform-autopull"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: plist does not exec the installed bin path";;
esac

# --- 11. a non-zero exit emits a signal to the operating plane ------------------
# The autopull now writes a signal on non-zero exit via signal_client. Replace
# python3 with a marker script that records what would be posted.
H=$(setup)
advance_origin "$H"
git -C "$H/Convivy/agent-platform" checkout -q -b stray
SIGNAL_MARKER="$H/.signal-recorded"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
# builder-root runs under python3 too (autopull resolves the machine's root
# through it), so hand it to the real interpreter and record only the signal.
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "\$@" >> $SIGNAL_MARKER
cat >> $SIGNAL_MARKER
EOF
chmod +x "$H/.local/bin/python3"
# Extend PATH so the fake python3 is found first, but real system tools
# (git, date, mkdir) are still available.
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "non-main branch still exits 1 when signal is written"
# A signal was attempted — the marker file exists and mentions the reason+branch.
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" "signal fcn was invoked"
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *signal_client*|*post_structured*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: signal call did not reference signal_client";;
esac
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"agent-platform-autopull-failure-{reason}-{branch}-{_dt.date.today().isoformat()}"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: signal dedup_key includes reason, branch, and date component";;
esac
# commits_behind must carry the real count, not 0
behind_field=$(awk 'NR==1{print $3}' "$SIGNAL_MARKER")
assert_eq "1" "$behind_field" "commits_behind is 1 (not 0) on not-on-main path"
rm -rf "$H"

# --- 12. signal write failure does not change the exit code ---------------------
# A missing or crashing python3.11 must not mask the original non-zero exit.
H=$(setup)
advance_origin "$H"
git -C "$H/Convivy/agent-platform" checkout -q -b stray2
# Standard tools are still available via $PATH; only python3.11 is removed.
rc=$(HOME="$H" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "signal failure does not mask non-zero exit"
# An unresolvable root also exits 1, so show the root resolved and this exit is the path's own.
assert_eq "no" "$(log_of "$H" | grep -q "cannot resolve the machine's root" && echo yes || echo no)" \
    "stray branch: the root resolved, so the exit is the stray path's own"
rm -rf "$H"

# --- 12b. a failed signal on a dirty tree also preserves exit code --------------
H=$(setup)
advance_origin "$H"
echo "dirty" >> "$H/Convivy/agent-platform/bin/thing"
rc=$(HOME="$H" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "dirty tree + signal failure still exits 1"
assert_eq "no" "$(log_of "$H" | grep -q "cannot resolve the machine's root" && echo yes || echo no)" \
    "dirty tree: the root resolved, so the exit is the dirty path's own"
rm -rf "$H"

# --- 12c. diverged checkout also preserves exit code ----------------------------
H=$(setup)
advance_origin "$H"
echo local > "$H/Convivy/agent-platform/bin/localonly"
git -C "$H/Convivy/agent-platform" add -A
git -C "$H/Convivy/agent-platform" commit -qm "local-only"
rc=$(HOME="$H" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "diverged + signal failure still exits 1"
assert_eq "no" "$(log_of "$H" | grep -q "cannot resolve the machine's root" && echo yes || echo no)" \
    "diverged: the root resolved, so the exit is the diverged path's own"
rm -rf "$H"

# --- 12d. a happy path stays quiet and exits 0 even with python3 present --------
# The signal function must not fire on exit 0, so a healthy cycle is unchanged.
H=$(setup)
mkdir -p "$H/.local/bin"
SIGNAL_MARKER="$H/.signal-should-not-exist"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
# builder-root runs under python3 too (autopull resolves the machine's root
# through it), so hand it to the real interpreter; anything else is the signal.
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "FAIL: signal function was called on exit 0" > $SIGNAL_MARKER
exit 1
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "happy path exits 0 even with python3 on PATH"
assert_eq "0" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" "signal not called on exit 0"
rm -rf "$H"

# --- 12e. diverged path emits signal (was crashing on nounset) -----------------
# Finding 2 was a use-before-declare of `branch` in the diverged guard, which
# triggered a nounset error and skipped the signal entirely. A signal must be
# emitted on the diverged path, and the branch field must be present.
H=$(setup)
advance_origin "$H"
echo local > "$H/Convivy/agent-platform/bin/localonly"
git -C "$H/Convivy/agent-platform" add -A
git -C "$H/Convivy/agent-platform" commit -qm "local-only"
SIGNAL_MARKER="$H/.signal-diverged"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
# builder-root runs under python3 too (autopull resolves the machine's root
# through it), so hand it to the real interpreter and record only the signal.
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "\$@" >> $SIGNAL_MARKER
cat >> $SIGNAL_MARKER
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "diverged exits 1 when signal is written"
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" "diverged path invokes signal function"
case "$(awk 'NR==1{print $3}' "$SIGNAL_MARKER" 2>/dev/null)" in
    diverged) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: diverged path reason is 'diverged'";;
esac
# The behind field (3rd arg) is empty for diverged — the signal omits
# commits_behind because the count is semantically ambiguous.
case "$(awk 'NR==1{print $2}' "$SIGNAL_MARKER" 2>/dev/null)" in
    main) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: diverged path branch field is correct";;
esac
rm -rf "$H"

# --- 12f. dirty path emits signal with real commits_behind ---------------------
# Both reviewers found that the dirty path also left commits_behind as 0. The
# count must carry the real number of commits behind origin/main.
H=$(setup)
advance_origin "$H"
echo "dirty" >> "$H/Convivy/agent-platform/bin/thing"
SIGNAL_MARKER="$H/.signal-dirty"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
# builder-root runs under python3 too (autopull resolves the machine's root
# through it), so hand it to the real interpreter and record only the signal.
case "\$1" in */builder-root) exec /usr/bin/python3 "\$@" ;; esac
echo "\$@" >> $SIGNAL_MARKER
cat >> $SIGNAL_MARKER
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "dirty tree exits 1 when signal is written"
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" "dirty path invokes signal function"
# commits_behind must carry the real count, not 0
behind_field=$(awk 'NR==1{print $3}' "$SIGNAL_MARKER")
assert_eq "1" "$behind_field" "commits_behind is 1 (not 0) on dirty path"
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"post_structured"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: dirty path signal did not reference signal_client";;
esac
rm -rf "$H"

# --- 13. dead/wrong farm drift raises an operational alert ---------------------
# The deployed farm is what every launchd job imports; dead/wrong links mean the
# machine runs code from a non-primary checkout — the 2026-07-30 incident shape.
# The check now runs on cycles where NOTHING is pulled (the incident sat for days
# because nothing watched the farm), and drift that cannot auto-fix posts an
# alert instead of a log line nobody reads. The farm_check.py here is a stub that
# emits drift; its own detection is covered by tests/test_farm_check.py.
H=$(setup)
SIGNAL_MARKER="$H/.signal-farm-drift"
mkdir -p "$H/.local/bin" "$H/Convivy/agent-platform/lib/agent-platform"
cat > "$H/Convivy/agent-platform/lib/agent-platform/farm_check.py" <<'EOF'
print("WRONG TARGET: issue_client.py expected /Convivy/agent-platform/lib/agent-platform/issue_client.py got /Convivy/worktrees/agent-platform.bug-something/lib/agent-platform/issue_client.py")
raise SystemExit(1)
EOF
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
if [ "\$1" = "-" ]; then
    echo "\$@" >> $SIGNAL_MARKER
    cat >> $SIGNAL_MARKER
    exit 0
fi
exec /usr/bin/python3 "\$@"
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "farm drift is alerted but does not fail the cycle (the pull is fine)"
case "$(log_of "$H")" in
    *"ACTION NEEDED"*"WRONG TARGET"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: farm drift not logged as ACTION NEEDED with the detail";;
esac
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" "farm drift invokes the alert function"
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"post_alert"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: alert does not reference signal_client.post_alert";;
esac
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"WRONG TARGET"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: alert body does not carry the drift detail";;
esac
# The fix message names THIS machine's resolved root (company/decisions/0008),
# passed as the alert's second positional arg, not a hardcoded ~/Convivy.
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"$H/Convivy/agent-platform"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: alert does not carry the resolved root for the fix message";;
esac
rm -rf "$H"

# --- 13b. a DEAD link does not block auto-linking a NEW module -----------------
# THE CASE THAT WAS NEVER COVERED, AND IT WEDGED THE REAL FARM FOR THREE DAYS.
# The gate used to require new AND no-dead AND no-wrong. techdebt.py was deleted
# from lib/ on 2026-09-15 and its farm symlink stayed behind, so from that moment
# crew_doctor.py, github_app.py and shell_families.py could not link. This
# function logged ACTION NEEDED 1,645 times naming the same four files.
#
# Dead and wrong are different risks: a wrong target means the farm resolves into
# another checkout (test 13 above), while a dead link is inert. So a dead link
# must no longer block the link — AND must still be reported and alerted, or
# auto-linking the new modules would make the remaining drift quiet.
H=$(setup)
SIGNAL_MARKER="$H/.signal-farm-dead"
LINKER_MARKER="$H/.linker-ran"
mkdir -p "$H/.local/bin" "$H/Convivy/agent-platform/lib/agent-platform"
cat > "$H/Convivy/agent-platform/lib/agent-platform/farm_check.py" <<'EOF'
print("NEW MODULE: brand_new.py")
print("DEAD LINK: techdebt.py → (target missing)")
raise SystemExit(1)
EOF
cat > "$H/Convivy/agent-platform/lib/link-farm.sh" <<EOF
#!/usr/bin/env bash
echo ran >> $LINKER_MARKER
EOF
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
if [ "\$1" = "-" ]; then
    echo "\$@" >> $SIGNAL_MARKER
    cat >> $SIGNAL_MARKER
    exit 0
fi
exec /usr/bin/python3 "\$@"
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "new module beside a dead link exits 0"
assert_eq "1" "$(test -f "$LINKER_MARKER" && echo 1 || echo 0)" \
    "a dead link does NOT block link-farm.sh from linking the new module"
case "$(log_of "$H")" in
    *"auto-linked new lib module"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: the new module was not reported as auto-linked";;
esac
# The dead link still needs a human, so it must still be loud.
case "$(log_of "$H")" in
    *"ACTION NEEDED"*"DEAD LINK"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: the remaining dead link went quiet after the auto-link";;
esac
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" \
    "a remaining dead link still raises the alert"
rm -rf "$H"

# --- 14. the quiet path refreshes the Bosun's shared deploy area via
#         install-agent-platform-shared, called with --repo pointed at the
#         working tree explicitly (platform/decisions/0059;
#         issue_01M19RRWSS9ETR09N9M2BJPA0Q) -----------------------------------
_seed_installer_stub() { # <repo> <marker-path>
    local repo=$1 marker=$2
    mkdir -p "$repo/bin"
    cat > "$repo/bin/install-agent-platform-shared" <<EOF
#!/bin/sh
echo "installer called: \$*" >> "$marker"
mkdir -p "\$HOME/.local/deploy/agent-platform-shared"
echo "commit: stub" > "\$HOME/.local/deploy/agent-platform-shared/DEPLOYED_COMMIT"
echo "Deployed: stub"
exit 0
EOF
    chmod +x "$repo/bin/install-agent-platform-shared"
    git -C "$repo" add -A
    git -C "$repo" commit -qm "add installer stub"
    git -C "$repo" push -q origin main
}

H=$(setup)
MARKER="$H/.installer-called"
_seed_installer_stub "$H/Convivy/agent-platform" "$MARKER"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "quiet path with an installer present still exits 0"
assert_eq "1" "$(test -f "$MARKER" && echo 1 || echo 0)" \
    "the quiet path invokes install-agent-platform-shared"
# THIS ASSERTION WAS TRUE AND THE CALL WAS BROKEN, FOR A DAY AND 144 CYCLES.
# The installer here is a STUB that records its argv, so this checks how the call
# is SPELLED and never that the real installer accepts it — and the real one
# parsed only `--repo=<path>`, exiting 2 on "unknown option: --repo" every time.
# A test that replaces the component whose contract is in question cannot see the
# contract break. The spelling assertion is kept, because the caller drifting is
# also worth catching, but the thing that actually holds this now is test 17 in
# tests/bash/test_install_agent_platform_shared.sh, which runs the REAL installer
# with both option forms.
assert_contains "$(cat "$MARKER")" "--repo=$H/Convivy/agent-platform" \
    "the installer is called with --repo pointed at the working tree explicitly"
assert_eq "1" "$(test -f "$H/.local/deploy/agent-platform-shared/DEPLOYED_COMMIT" && echo 1 || echo 0)" \
    "the deploy area the installer wrote exists where refresh_deploy_area expects it"
rm -rf "$H"

# --- 15. a fixture with NO installer present is unaffected — the call is a
#         no-op, not a failure, so every existing test above (whose fixture
#         never seeds an installer) stays a valid quiet-path exercise ---------
H=$(setup)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "quiet path with no installer present still exits 0"
assert_eq "0" "$(test -d "$H/.local/deploy" && echo 1 || echo 0)" \
    "no deploy area is created when install-agent-platform-shared does not exist"
rm -rf "$H"

# --- 16. a FAILED deploy refresh does not fail the pull's own exit code, and
#         alerts instead (same shape as farm drift) ---------------------------
H=$(setup)
mkdir -p "$H/Convivy/agent-platform/bin"
cat > "$H/Convivy/agent-platform/bin/install-agent-platform-shared" <<'EOF'
#!/bin/sh
echo "install-agent-platform-shared: deliberately broken for this test" >&2
exit 1
EOF
chmod +x "$H/Convivy/agent-platform/bin/install-agent-platform-shared"
git -C "$H/Convivy/agent-platform" add -A
git -C "$H/Convivy/agent-platform" commit -qm "add broken installer"
git -C "$H/Convivy/agent-platform" push -q origin main

SIGNAL_MARKER="$H/.signal-deploy-refresh-failure"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/python3" <<EOF
#!/bin/bash
if [ "\$1" = "-" ]; then
    echo "\$@" >> $SIGNAL_MARKER
    cat >> $SIGNAL_MARKER
    exit 0
fi
exec /usr/bin/python3 "\$@"
EOF
chmod +x "$H/.local/bin/python3"
rc=$(HOME="$H" PATH="$H/.local/bin:$PATH" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$H")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "a failed deploy-area refresh does not fail the cycle (the pull itself is fine)"
case "$(log_of "$H")" in
    *"ACTION NEEDED"*"deploy area refresh failed"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: refresh failure not logged as ACTION NEEDED";;
esac
assert_eq "1" "$(test -f "$SIGNAL_MARKER" && echo 1 || echo 0)" \
    "a failed refresh invokes the alert function"
case "$(cat "$SIGNAL_MARKER" 2>/dev/null)" in
    *"post_alert"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: alert does not reference signal_client.post_alert";;
esac
rm -rf "$H"

# --- 17. THE SAFETY PROPERTY: the watcher never reads the deploy area it
#         writes. A poisoned copy of this very script sitting in the deploy
#         area must not affect the watcher's own execution — launchd always
#         invokes it through ~/.local/bin (the interactive symlink into the
#         tree), never through ~/.local/deploy/agent-platform-shared -------
H=$(setup)
mkdir -p "$H/.local/deploy/agent-platform-shared/bin"
cat > "$H/.local/deploy/agent-platform-shared/bin/agent-platform-autopull" <<'EOF'
#!/bin/zsh
exit 99
EOF
chmod +x "$H/.local/deploy/agent-platform-shared/bin/agent-platform-autopull"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" \
    "a poisoned copy in the deploy area does not affect the watcher's own execution"
rm -rf "$H"

# --- 18. the signal heredocs' library resolution prefers the checkout over the
#         installed farm (issue_01M33KK04A9B1A2MC710CNF583) -------------------
# Before the fix, the three signal_failure/alert_farm_drift/alert_deploy_refresh_failure
# heredocs did `sys.path.insert(0, os.path.expanduser('~/.local/lib/agent-platform'))`,
# reading whatever $HOME the process runs under — every fixture in THIS file
# has no farm under its fake $HOME at all, so `import signal_client` always
# raised ModuleNotFoundError inside the heredoc, silently swallowed by the
# `2>/dev/null || true` wrapping every call site. That swallowing means no
# assertion above would ever notice the import failing, so this test checks
# the resolution directly instead: it extracts the real `_AUTOPULL_BIN`/
# `_LIB_DIR` block verbatim from the script (so it tracks the source rather
# than a hand-copied twin) and evaluates it standalone, proving it resolves
# to THIS checkout's own lib/agent-platform — where signal_client.py
# actually lives — regardless of $HOME, and falls back to the installed farm
# only when the checkout candidate lacks the module.
_lib_dir_snippet() {
    sed -n '/^_AUTOPULL_BIN=/,/_LIB_DIR=\$HOME\/\.local\/lib\/agent-platform/p' "$AUTOPULL"
}
snippet=$(_lib_dir_snippet)
case "$snippet" in
    *_AUTOPULL_BIN=*_LIB_DIR=*'$HOME/.local/lib/agent-platform'*) ;;
    *) echo "  FIXTURE BROKEN: could not extract the _LIB_DIR block from $AUTOPULL" >&2; exit 1;;
esac

resolved=$(zsh -c "$snippet"$'\nprint -r -- "$_LIB_DIR"' "$AUTOPULL")
assert_eq "$REPO_ROOT/lib/agent-platform/signal_client.py" "$resolved/signal_client.py" \
    "the real script resolves to this checkout's own signal_client.py, not any \$HOME farm"

# Fallback: a script whose OWN checkout has no lib/agent-platform at all (a
# copied/partial deployment) falls back to the installed farm under its HOME,
# same as bin/knowledge-watcher's shape.
FAKE=$(mktemp -d)
mkdir -p "$FAKE/bin"
fallback=$(HOME="$FAKE" zsh -c "$snippet"$'\nprint -r -- "$_LIB_DIR"' "$FAKE/bin/agent-platform-autopull")
assert_eq "$FAKE/.local/lib/agent-platform" "$fallback" \
    "with no checkout lib dir, _LIB_DIR falls back to the farm under HOME"
rm -rf "$FAKE"

# --- the machine's root decides which checkout is kept current, not HOME -------
# company/decisions/0008: autopull finds its checkout through the builder-root
# beside it, which HOME does not move, so every run here names the root with
# --root-config. In this case the checkout exists only under that root, and HOME
# holds nothing but the log directory autopull creates.
R=$(setup); H=$(mktemp -d)
before=$(head_of "$R")
advance_origin "$R"
rc=$(HOME="$H" /bin/zsh "$AUTOPULL" --root-config "$(co_root_config "$R")" >/dev/null 2>&1; echo $?)
assert_rc 0 "$rc" "configured root: the fast-forward exits 0"
assert_eq "yes" "$([ "$before" != "$(head_of "$R")" ] && echo yes || echo no)" \
    "configured root: the checkout under the root is the one that moved"
assert_eq "no" "$([ -e "$H/Convivy" ] && echo yes || echo no)" "and HOME is not consulted"
rm -rf "$R" "$H"

# --- a root config that names no root: nothing is pulled, and the log says why ---
# builder-root refuses it rather than printing the real root. An unresolvable root
# keeps nothing current, so the cycle fails loudly instead of guessing a checkout.
H=$(setup)
advance_origin "$H"
before=$(head_of "$H")
rc=$(HOME="$H" /bin/zsh "$AUTOPULL" --root-config "$H/no-such.env" >/dev/null 2>&1; echo $?)
assert_rc 1 "$rc" "no root: the cycle exits non-zero"
assert_eq "$before" "$(head_of "$H")" "no root: nothing is pulled"
case "$(log_of "$H")" in
    *"cannot resolve the machine's root"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: no root: the log says why";;
esac
rm -rf "$H"

# --- the real remote-tracking ref is present: a tag or branch named origin/main is not main
# This is the case that steered the script before this change. It ran
# `git rev-parse origin/main`, and git's search rules read a tag (then a local
# branch) named `origin/main` before refs/remotes/origin/main, so a decoy on a
# descendant of HEAD was fast-forwarded into the live tree. The decoys here are
# planted in the checkout on a forged commit; origin then advances to the real one.
H=$(setup)
repo="$H/Convivy/agent-platform"
git -C "$repo" checkout -q -b forge
echo forged > "$repo/bin/thing"
git -C "$repo" commit -qam forged
forged=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" checkout -q main
git -C "$repo" branch -q -D forge
git -C "$repo" tag origin/main "$forged"
git -C "$repo" branch origin/main "$forged"
advance_origin "$H"
rc=$(run_autopull "$H")
real=$(git -C "$repo" show-ref --verify --hash refs/remotes/origin/main)
assert_rc 0 "$rc" "decoy tag and branch named origin/main: the cycle exits 0"
assert_eq "$real" "$(head_of "$H")" "decoy tag and branch named origin/main: lands on the real main sha"
if [ "$(head_of "$H")" != "$forged" ]; then _PASS=$((_PASS + 1)); else
    _FAIL=$((_FAIL + 1)); echo "  FAIL: decoy tag and branch named origin/main: fast-forwarded to the forged commit"; fi
assert_eq "0" "$(grep -c forged "$repo/bin/thing")" "decoy tag and branch named origin/main: the forged file content is not in the tree"
rm -rf "$H"

# --- the real remote-tracking ref is ABSENT: a same-named tag is not main -----------
# The remote renamed its default branch to trunk and the checkout's origin/main
# ref is gone (as after `fetch --prune`). A tag pushed to the remote as
# `refs/remotes/origin/main` is auto-followed by the fetch. The script must refuse
# and say why. rc 1 and an unchanged HEAD also held before this change (the short
# name `origin/main` matched nothing once the ref was gone), so those two
# assertions do not tell the versions apart; the log line naming the missing ref
# does, and the present-ref case above is the one that fails on the old script.
H=$(setup)
advance_origin "$H"
git -C "$H/origin.git" tag refs/remotes/origin/main main
git -C "$H/origin.git" branch -m main trunk
git -C "$H/origin.git" symbolic-ref HEAD refs/heads/trunk
git -C "$H/Convivy/agent-platform" update-ref -d refs/remotes/origin/main
before=$(head_of "$H")
rc=$(run_autopull "$H")
assert_rc 1 "$rc" "missing origin/main ref: the cycle exits non-zero"
assert_eq "$before" "$(head_of "$H")" "missing origin/main ref: a same-named tag is not merged"
assert_eq "yes" "$(git -C "$H/Convivy/agent-platform" rev-parse -q --verify refs/tags/refs/remotes/origin/main >/dev/null && echo yes || echo no)" \
    "missing origin/main ref: the decoy tag did arrive, so the refusal is what held"
case "$(log_of "$H")" in
    *"refs/remotes/origin/main does not exist"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: missing origin/main ref: the log names the missing ref";;
esac
rm -rf "$H"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
