#!/bin/bash
# tests/bash/test_safe_path.sh — lib/safe-path.sh's `safe_which`, the shared
# bash-side resolver (platform/decisions/0090's Handoff: a credentialed or
# guard-deciding tool is resolved from a fixed, ordered list of system
# directories, never from $PATH or $HOME). See tests/test_safe_path.py for
# the Python sibling and tests/test_shared_safe_path_dirs.py for the
# cross-language list-equality check. Run under /bin/bash (3.2.57), per
# ARCH-082.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"
# shellcheck source=/dev/null
. "$REPO_ROOT/lib/safe-path.sh"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

_plant() { # <dir> <name>
    mkdir -p "$1"
    printf '#!/bin/sh\necho planted\n' > "$1/$2"
    chmod +x "$1/$2"
}

# --- the fixed list, on this machine today -----------------------------------
#
# No literal path here: a Homebrew install sits at /opt/homebrew/bin on
# Apple Silicon, /usr/local/bin on Intel, and the GitHub Actions macOS
# runner puts `gh` and `npx` somewhere else again (round 13 — the macOS
# runner first ran this suite once the PR left draft, and both literals
# were wrong there). The expectation is computed the same way
# `safe_which` resolves: the first `_safe_path_system_dirs` entry holding
# an executable regular file of that name.

_expected_in_system_dirs() { # <name> -> path on stdout + rc 0, or rc 1
    local name="$1" dirs dir saved_ifs="$IFS"
    dirs="$(_safe_path_system_dirs)"
    IFS=:
    for dir in $dirs; do
        IFS="$saved_ifs"
        [ -n "$dir" ] || continue
        if [ -f "$dir/$name" ] && [ -x "$dir/$name" ]; then
            printf '%s\n' "$dir/$name"
            return 0
        fi
        IFS=:
    done
    IFS="$saved_ifs"
    return 1
}

for _tool in git jq gh npx; do
    if expected=$(_expected_in_system_dirs "$_tool"); then
        out=$(safe_which "$_tool")
        assert_eq "$expected" "$out" "$_tool resolves from the fixed list"
    else
        echo "  SKIP: $_tool is not on any _safe_path_system_dirs() entry on this machine"
    fi
done

out=$(safe_which definitely-not-a-real-binary-xyz); rc=$?
assert_eq "1" "$rc" "a name nothing provides: exit 1"
assert_eq "" "$out" "a name nothing provides: prints nothing"

# --- the plant: four shapes an agent's own environment fully controls -------
#
# THE PLANT: a dispatched agent can create a directory under /tmp (world-
# writable, sticky), a directory literally named `worktrees` (bare, nothing
# below it — the exact shape round 3 found the old bash pattern missed), a
# directory under its own $HOME, or point a relative PATH entry at its own
# worktree cwd — and set PATH/HOME/cwd to put any of them ahead of a real
# tool. None of PATH, HOME or cwd is ever read by `safe_which`'s default (no
# second-argument) resolution, so planting in any of them must leave the
# answer unchanged — the real tool if one exists on the fixed list, nothing
# otherwise — never the plant.

tmp_plant="/tmp/safe-path-test-plant-$$"
bare_worktrees_plant="$WORK/elsewhere/worktrees"
under_home_plant="$WORK/fake-home/.local/bin"
relative_plant="$WORK/relwork/x"

for shape_dir in "$tmp_plant" "$bare_worktrees_plant" "$under_home_plant" "$relative_plant"; do
    _plant "$shape_dir" definitely-not-a-real-binary-xyz
    _plant "$shape_dir" jq
done

_check_plant_not_chosen() { # <label> <plant_dir> <home> <path> <cwd-or->
    local label="$1" plant_dir="$2" home="$3" path="$4" cwd="$5" out rc
    if [ "$cwd" != "-" ]; then
        out=$(cd "$cwd" && HOME="$home" PATH="$path" safe_which definitely-not-a-real-binary-xyz)
    else
        out=$(HOME="$home" PATH="$path" safe_which definitely-not-a-real-binary-xyz)
    fi
    rc=$?
    assert_eq "1" "$rc" "$label: a name nothing real provides still exits 1"
    assert_eq "" "$out" "$label: a plant in $plant_dir is not chosen over nothing"

    if [ "$cwd" != "-" ]; then
        out=$(cd "$cwd" && HOME="$home" PATH="$path" safe_which jq)
    else
        out=$(HOME="$home" PATH="$path" safe_which jq)
    fi
    assert_eq "/usr/bin/jq" "$out" "$label: a plant in $plant_dir does not shadow the real jq"
}

_check_plant_not_chosen "/tmp plant" "$tmp_plant" "$WORK/fake-home" \
    "$tmp_plant:$PATH" -
_check_plant_not_chosen "bare worktrees-dir plant" "$bare_worktrees_plant" "$WORK/fake-home" \
    "$bare_worktrees_plant:$PATH" -
_check_plant_not_chosen "under-HOME plant" "$under_home_plant" "$WORK/fake-home" \
    "$under_home_plant:$PATH" -
_check_plant_not_chosen "relative-entry plant" "$relative_plant" "$WORK/fake-home" \
    "x:$PATH" "$WORK/relwork"

rm -rf "$tmp_plant"

# --- the second argument: a test-only override --------------------------------

first="$WORK/override-first"
second="$WORK/override-second"
_plant "$first" gh
_plant "$second" gh
out=$(safe_which gh "$first:$second")
assert_eq "$first/gh" "$out" "override order: first dir wins"
out=$(safe_which gh "$second:$first")
assert_eq "$second/gh" "$out" "override order: honours the order given"

empty="$WORK/override-empty"
mkdir -p "$empty"
out=$(safe_which gh "$empty"); rc=$?
assert_eq "1" "$rc" "override with nothing matching: exit 1"
assert_eq "" "$out" "override with nothing matching: prints nothing"

# A fixture left on disk must not leak into the default (no-override) call.
out=$(safe_which gh)
if [ "$out" = "$first/gh" ]; then
    _FAIL=$((_FAIL + 1))
    echo "  FAIL: the default resolution picked up an override fixture left on disk"
else
    _PASS=$((_PASS + 1))
fi

# --- safe_which leaves the caller's IFS and PWD unchanged ---------------------
#
# Every `IFS=:` is restored before every `return`, and nothing ever `cd`s — so
# calling safe_which directly (not inside a `$(...)` subshell, where a changed
# IFS/PWD would be invisible to the caller anyway) must not leak either one.

_before_ifs="$IFS"
_before_pwd="$PWD"
safe_which gh >/dev/null
assert_eq "$(printf '%s' "$_before_ifs" | od -An -tx1)" \
          "$(printf '%s' "$IFS" | od -An -tx1)" \
          "safe_which leaves the caller's IFS unchanged"
assert_eq "$_before_pwd" "$PWD" "safe_which leaves the caller's PWD unchanged"

# Same check after a call that does NOT find anything — the not-found path
# returns through the same loop-exit code as the found path.
safe_which definitely-not-a-real-binary-xyz >/dev/null
assert_eq "$(printf '%s' "$_before_ifs" | od -An -tx1)" \
          "$(printf '%s' "$IFS" | od -An -tx1)" \
          "safe_which (not found) leaves the caller's IFS unchanged"
assert_eq "$_before_pwd" "$PWD" "safe_which (not found) leaves the caller's PWD unchanged"

# --- _safe_path_sort_colon_list is locale-independent --------------------------
#
# A bare `sort` follows LC_ALL/LC_COLLATE, so it can disagree with Python's
# always-ordinal `sorted()` on the same input. `/opt` holds exactly one entry
# on this machine today, so the full `_safe_path_system_dirs` glob never
# exercises a multi-entry sort either way — this calls the sort helper
# directly with crafted names, under a non-C locale, to exercise what a
# second `/opt/*/bin` entry would. See tests/test_shared_safe_path_dirs.py
# for the Python-side comparison confirming both languages agree.

_locale_names="/example/Zebra/bin:/example/apple/bin:/example/_under/bin"
_locale_expected="/example/Zebra/bin:/example/_under/bin:/example/apple/bin:"   # ordinal (C-locale) order
if locale -a 2>/dev/null | grep -qi '^en_US\.UTF-8$'; then
    out=$(LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 _safe_path_sort_colon_list "$_locale_names")
    assert_eq "$_locale_expected" "$out" \
        "_safe_path_sort_colon_list is ordinal (matches Python's sorted()) under en_US.UTF-8, not locale-dependent"
else
    echo "  SKIP: en_US.UTF-8 not installed on this machine — locale-sort check skipped"
fi
out=$(_safe_path_sort_colon_list "$_locale_names")
assert_eq "$_locale_expected" "$out" \
    "_safe_path_sort_colon_list is ordinal under the default locale too"

finish
