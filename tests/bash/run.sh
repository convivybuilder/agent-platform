#!/bin/bash
# tests/bash/run.sh — run every bash test under /bin/bash (3.2 on macOS + the
# mini). MUST be invoked as `/bin/bash tests/bash/run.sh` in CI: Homebrew/Linux
# bash is 5.x and would not exercise the 3.2 constraints (ARCH-082).
#
# Every test runs under an ops-write guard, the bash suite's `_no_live_ops_writes`:
# OVERNIGHT_OPS_GUARD is exported to a fresh file, truncated before each test, and
# checked after. A test whose exercised code path attempts a plane write through a
# shadowed client (see mocks/) with no OVERNIGHT_MOCK_LOG leaves a record in the
# guard file and FAILS — nothing reaches the plane, and a silently-captured write
# is impossible, because capture itself is the failure. This is the structural
# answer to the 2026-07-29 alerts that a suite emitting to production trains its
# reader to ignore.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fail=0

# A dispatched run's own environment carries dispatch_env's GIT_OBJECT_DIRECTORY /
# GIT_ALTERNATE_OBJECT_DIRECTORIES, redirecting git at the run's private object store
# (agent-platform#793). Inherited by every test file below, this redirected a test's
# `git init --bare` into that private store, leaving the new repo with no objects/
# directory (issue_01M4CWH0XFCV5CYMZVNDYSMP9C). Unset before any test runs, mirroring
# the filter run_sandbox.import_private_objects already applies in Python.
unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_DIR

echo "bash under test: $(/bin/bash --version | head -1)"
case "$(/bin/bash --version | head -1)" in
    *"version 3.2"*) echo "  (3.2 — faithful to the mini)";;
    *) echo "  WARNING: /bin/bash is not 3.2 — the 3.2 hazard class is NOT being exercised";;
esac

# SKIP_INSTALLER_TESTS=true (set only by ci.yml's bash-tests step, from the
# `changes` job's installer_tests output) skips exactly the four heavy
# installer/deploy test files — the ones whose venv-building, pip-installing
# scenarios made the macOS job's cost, per issue_01M2F1XW1MJ7A30EK88NY4XRHD —
# when the PR's changed files match none of tests/bash/installer-test-inputs.txt.
# Unset, which is every local run and every push to main, this changes nothing:
# every test file below still runs. tests/bash/test_installer_test_inputs_coverage.sh
# is deliberately NOT in this list, so it always runs and keeps the pattern
# file honest against what these four files actually reference.
_INSTALLER_TEST_FILES="test_install_agent_platform_shared.sh test_install_run_register_api.sh test_install_new_plists.sh test_agent_platform_autopull.sh"

GUARD=$(mktemp) || { echo "  FATAL: mktemp failed — cannot arm the ops-write guard; aborting"; exit 1; }
export OVERNIGHT_OPS_GUARD="$GUARD"

# NO TEST WRITES THE LIVE RUN RECORDS. The launchers resolve their run-record root
# through builder_root.data_root(): BUILDER_ROOT when set, else the machine's real
# root, which ignores HOME. So a test that sandboxes HOME must also set
# BUILDER_ROOT, and this throwaway default means one that forgets writes here, and
# fails its own assertions, rather than writing into the machine's agent-runs.
BUILDER_ROOT=$(mktemp -d) || { echo "  FATAL: mktemp -d failed — cannot isolate run records; aborting"; exit 1; }
export BUILDER_ROOT
trap 'rm -f "$GUARD"; rm -rf "$BUILDER_ROOT"' EXIT

for t in "$HERE"/test_*.sh; do
    tname=$(basename "$t")
    echo "== $tname =="
    case " $_INSTALLER_TEST_FILES " in
        *" $tname "*)
            if [ "${SKIP_INSTALLER_TESTS:-}" = "true" ]; then
                echo "  SKIPPED: installer_tests=false — this PR's changed files match no pattern in tests/bash/installer-test-inputs.txt. Full suite still runs on every push to main."
                continue
            fi
            ;;
    esac
    : > "$GUARD"   # attribute any unacknowledged write to the test about to run
    if /bin/bash "$t"; then :; else fail=1; fi
    if [ -s "$GUARD" ]; then
        echo "  FAIL: unacknowledged operating-plane write intercepted (nothing was sent):"
        cat "$GUARD"
        fail=1
    fi
done

if [ "$fail" -eq 0 ]; then
    echo "ALL BASH TESTS PASSED"
else
    echo "BASH TESTS FAILED"
fi
exit "$fail"
