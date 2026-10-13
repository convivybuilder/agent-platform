#!/bin/bash
# tests/bash/test_git_object_redirect_wrapper_refuses_to_recurse.sh —
# OUTAGE, 2026-10-12. At 20:28 MST the user's process count hit 10,417, 9,256 of
# them `/bin/bash /tmp/gitwrap-*/git -C <scratch> rev-parse ...`, each the parent of
# the next. `AGENTCTL_REAL_GIT` had ended up pointing at a copy of the wrapper itself
# (test_git_object_redirect_wrapper_scopes_to_own_repo.sh's old `command -v git`,
# resolved from inside a dispatched run where the wrapper is first on PATH) — so the
# wrapper's probe, a command substitution, forked a child that ran the same script
# again with the same self-referential env still in it, which forked the next child
# the same way, without bound. `dispatch_env._GIT_WRAPPER_SCRIPT` now refuses, before
# any exec or probe, when `$AGENTCTL_REAL_GIT` resolves to a copy of the wrapper
# (itself, by `$0`, or a different copy, by its marker line) instead of recursing.
#
# SAFETY. This test must never let a BROKEN wrapper actually recurse — that is the
# fork bomb above. Every case below is bounded two ways:
#   1. A background-job-plus-watchdog bound on wall clock (no dependency on GNU
#      `timeout`, which macOS does not ship — see test_symlink_hop_cap.sh's
#      `_bounded_run`, duplicated here for the same reason that file states it).
#   2. Case 1 (AGENTCTL_REAL_GIT = this wrapper's own $0) additionally runs under a
#      lowered `ulimit -u`: the fix's $0 comparison needs zero forks to catch this
#      case, so the lowered limit never blocks the passing run, and is pure
#      defense-in-depth if a regression ever reaches the probe below it (this
#      process, and every ambient one under this user, already sits well above any
#      sane cap, so lowering it denies even the FIRST fork a regression would make).
#      Case 2 (a SECOND wrapper copy, caught only by the marker line) is NOT run
#      under a lowered ulimit: the marker check itself forks once (`grep`), which a
#      low cap would deny even on a correct, passing run — measured: ulimit -u 32
#      turned a correct refusal into a spurious `fork: Resource temporarily
#      unavailable` here. Case 2 is bounded instead by pointing `AGENTCTL_REAL_GIT`
#      at a COUNTING SHIM rather than a second real wrapper copy: the shim carries
#      the wrapper's marker line (so the check under test treats it as "a wrapper
#      copy", exactly like a second run's real one would), but if that check ever
#      fails to refuse and the shim actually gets exec'd, the shim increments a
#      counter file and hard-exits once it passes 3 invocations. The shim's own
#      recursive step is `exec`, never a forking command substitution, so even a
#      fully regressed fix can add at most the wrapper's own single probe fork plus
#      the shim's few in-place `exec`s — nowhere near process exhaustion — before
#      the shim's cap ends it.
#
# THE PLANT CHECK FOR THIS TEST WAS SKIPPED. Running it against the unfixed wrapper
# (reverting dispatch_env.py's refusal) is exactly the fork bomb this test exists to
# prevent, so there is no "run it red first" step here. Red-ness is instead shown by
# the shim in case 2 once it hits its own 3-invocation cap (see that case's
# assertions below) and by the two independent manual checks in the PR body (the
# fix, run without any guard, exits 127 with no forking at all; run inside this
# session's own OLD, pre-fix wrapper environment with no `_find_real_git` fix in the
# cross-repo test, this exact recursion hung the sandboxed shell until the harness's
# own timeout killed it — the live reproduction of the incident this file guards).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

WRAPPER_DIR="$SCRATCH/wrapper"
mkdir -p "$WRAPPER_DIR"
MARKER=$(PYTHONPATH="$REPO_ROOT/lib/agent-platform" /usr/bin/python3 -c '
import dispatch_env
import sys
sys.stdout.write(dispatch_env._GIT_WRAPPER_MARKER)
')
PYTHONPATH="$REPO_ROOT/lib/agent-platform" /usr/bin/python3 -c '
import dispatch_env
import sys
sys.stdout.write(dispatch_env._GIT_WRAPPER_SCRIPT)
' > "$WRAPPER_DIR/git"
chmod +x "$WRAPPER_DIR/git"

A="$SCRATCH/A"
PRIVATE="$SCRATCH/private-A"
mkdir -p "$A" "$PRIVATE"
git init -q -b main "$A"
git -C "$A" -c user.name=t -c user.email=t@t commit -q --allow-empty -m a
OWN_COMMON_DIR=$(cd "$A" && git rev-parse --path-format=absolute --git-common-dir)

# Run "$@" (a function name, no args) in the background, bounded to <secs> wall clock.
# Echoes the function's exit code. No dependency on GNU coreutils' `timeout`, which
# macOS does not ship — same pattern, and the same reason for it, as
# test_symlink_hop_cap.sh's `_bounded_run`, duplicated here rather than shared because
# neither file depends on the other.
_bounded_run() { # <secs> <outfile> <funcname>
    local secs=$1 outfile=$2 funcname=$3
    "$funcname" >"$outfile" 2>&1 &
    local pid=$!
    ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    local watchdog=$!
    wait "$pid" 2>/dev/null
    local rc=$?
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    echo "$rc"
}

# Case 1: AGENTCTL_REAL_GIT names the wrapper's OWN file — the exact incident shape
# (a "real git" resolution that lands back on this process's own wrapper). The $0
# self-comparison must refuse this before the probe ever runs, so this case cannot
# fork even once when the fix works; `ulimit -u` here is the defense-in-depth against
# a regression that moves the check later, not a bound this case is expected to hit.
run_case1() {
    (
        ulimit -u 32 2>/dev/null
        # `exec` here, deliberately, not a plain command: `exec` replaces this
        # subshell's own process image (execve, no fork) rather than forking a new
        # one, so the lowered ulimit above can never block THIS, the one invocation
        # the test actually needs — confirmed separately that `exec` succeeds under
        # ulimit -u as low as 1. Anything the wrapper itself forks after this point
        # (the probe, on a regression) is what the lowered ulimit is there to deny.
        AGENTCTL_REAL_GIT="$WRAPPER_DIR/git" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
          GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
          exec /bin/bash "$WRAPPER_DIR/git" -C "$A" cat-file -t HEAD
    )
}
OUT1="$SCRATCH/case1.out"
rc1=$(_bounded_run 10 "$OUT1" run_case1)
out1=$(cat "$OUT1")
assert_rc "127" "$rc1" "AGENTCTL_REAL_GIT pointing at the wrapper's own file exits 127, not 0 and not a watchdog kill"
assert_contains "$out1" "AGENTCTL_REAL_GIT" "the refusal names AGENTCTL_REAL_GIT in its error"
assert_contains "$out1" "copy of this wrapper" "the refusal says why: real_git resolved to a wrapper copy"

# Case 2: AGENTCTL_REAL_GIT names a SECOND copy of the wrapper in a different
# directory (a different run's /tmp/gitwrap-*/git). The $0 comparison alone does not
# catch this — only the marker line does. Pointed at a counting shim rather than a
# literal second wrapper copy; see the file header for why.
COPY_DIR="$SCRATCH/wrapper-copy"
mkdir -p "$COPY_DIR"
COUNTER="$SCRATCH/shim-invocations"
: > "$COUNTER"
cat > "$COPY_DIR/git" <<SHIM
#!/bin/bash
# the marker line below is only here so the detection UNDER TEST reads this file as a
# wrapper copy — it is not a real copy of the wrapper logic.
# $MARKER
echo x >> "$COUNTER"
count=\$(wc -l < "$COUNTER")
if [ "\$count" -gt 3 ]; then
    echo "shim: invocation cap exceeded -- the fix under test did not refuse" >&2
    exit 1
fi
exec "\$AGENTCTL_REAL_GIT" "\$@"
SHIM
chmod +x "$COPY_DIR/git"

run_case2() {
    (
        # No lowered ulimit here — see the file header for why (the marker check's
        # own `grep` needs to fork once on the correct, passing path). The shim's
        # own invocation cap is this case's bound instead.
        AGENTCTL_REAL_GIT="$COPY_DIR/git" AGENTCTL_OWN_GIT_COMMON_DIR="$OWN_COMMON_DIR" \
          GIT_OBJECT_DIRECTORY="$PRIVATE" GIT_ALTERNATE_OBJECT_DIRECTORIES="$A/.git/objects" \
          exec /bin/bash "$WRAPPER_DIR/git" -C "$A" cat-file -t HEAD
    )
}
OUT2="$SCRATCH/case2.out"
rc2=$(_bounded_run 10 "$OUT2" run_case2)
out2=$(cat "$OUT2")
assert_rc "127" "$rc2" "AGENTCTL_REAL_GIT pointing at a second wrapper copy (by marker) exits 127"
assert_contains "$out2" "AGENTCTL_REAL_GIT" "the refusal names AGENTCTL_REAL_GIT in its error"
invocations=$(wc -l < "$COUNTER" | tr -d ' ')
assert_eq "0" "$invocations" "the shim was never invoked -- the wrapper refused before ever calling through to AGENTCTL_REAL_GIT"

finish
