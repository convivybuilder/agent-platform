#!/bin/bash
# install.sh copies a new launchd plist into LaunchAgents and NOTHING loads it.
# Only `launchctl bootstrap` does, and the activation loop install.sh prints at
# the end names a hardcoded list of labels that has not kept up with `launchd/` —
# it covers 16 of 48. So a job added since that list was last edited is installed,
# never mentioned again, and never runs. Nothing errors.
#
# Measured on the live machine 2026-08-27: the installer copied
# com.convivy.run-observer.plist and mentioned "run-observer" ZERO times in its whole
# output, having just merged as a nightly sweep in #451/#454.
#
# The fix names plists that did not exist before this run — deliberately NOT every
# plist on disk, because several live ones are installed and intentionally not
# loaded (the overnight fleet), and a derived loop would silently re-enable them.
#
# Naming is ALL this does. Earlier rounds also printed a paste-ready `launchctl
# bootstrap <plist>` command per newly-named job, gated on a same-UID freshness
# signal meant to withhold that command on a machine where "new" means "every
# plist on disk" — including jobs deliberately not on by default. Four rounds of
# review each found a different way to forge that signal, because every candidate
# for it was itself a same-UID file, and any same-UID file can be planted or can
# already exist for an unrelated reason. There is no such signal that can't be
# forged, so the fix removes the thing being gated instead: no code path here
# ever prints a per-job bootstrap command, so there is nothing left for a forged
# signal to unlock. A from-scratch install and an incremental one now behave the
# same way — name what's new, point at the curated activation block below.
#
# Hermetic: HOME points at a tmp dir, so LaunchAgents is the fake HOME's. The repo
# is CLONED rather than used in place because install.sh's link-farm guard refuses
# to run from a linked worktree (ARCH: the 2026-07-30 farm incident), and a clone
# is a primary checkout of itself.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

setup() { # -> echoes fake HOME; clones the repo into it
    local home
    home=$(mktemp -d)
    mkdir -p "$home/Library/LaunchAgents"
    git clone -q --local "$REPO_ROOT" "$home/repo" 2>/dev/null
    cp "$REPO_ROOT/install.sh" "$home/repo/install.sh"
    echo "$home"
}

# --- a plist that already exists is not announced; a new one is, and named ---
# --- without ever printing a per-job bootstrap command ------------------------
#
# ONE RUN COVERS BOTH DIRECTIONS, which is the point: pre-seed every plist, remove
# exactly one, and the output must name that one and none of the others. A test
# that only checked the positive would pass on an installer that announced
# everything every time, which is the same as announcing nothing.
home=$(setup)
for src in "$REPO_ROOT"/launchd/*.plist; do
    cp "$src" "$home/Library/LaunchAgents/$(basename "$src")"
done
rm -f "$home/Library/LaunchAgents/com.convivy.run-observer.plist"

out=$(HOME="$home" bash "$home/repo/install.sh" 2>&1)

assert_contains "$out" "NEW launchd job(s) installed for the first time" \
    "a plist that did not exist before must be announced as new"
assert_contains "$out" "com.convivy.run-observer" \
    "and named, so the human knows which job is not running yet"
assert_contains "$out" "curated activation block" \
    "and pointed at the curated block rather than handed a ready command"

# NO PER-JOB BOOTSTRAP COMMAND — for the new job or any other. This is the
# artifact four rounds of review kept reaching through a forged freshness
# signal: a paste-ready command for a job that may be deliberately not on by
# default. The fix removes the artifact itself, between the new-job notice and
# the always-printed curated block, so this must hold with no signal left to
# defeat.
notice=$(printf '%s\n' "$out" | sed -n '/NEW launchd job/,/Install complete/p')
bootstrap_lines=$(printf '%s\n' "$notice" | grep -c "launchctl bootstrap")
assert_eq "0" "$bootstrap_lines" \
    "no per-job bootstrap command may be printed for a newly-named plist"

# EXACTLY ONE, counted, not "a specific other one is absent". The first version
# grepped for `com.convivy.agent-summary` and would have passed on a defect that
# named some other wrong subset — a negative assertion that only rules out one of
# 43 possibilities rules out almost nothing.
listed=$(printf '%s\n' "$notice" | grep -c "com\.")
assert_eq "1" "$listed" \
    "exactly one plist was new, so exactly one may be announced — otherwise every
     run names everything and the notice stops meaning anything"

rm -rf "$home"

# --- nothing new at all: the notice must not appear --------------------------
home=$(setup)
for src in "$REPO_ROOT"/launchd/*.plist; do
    cp "$src" "$home/Library/LaunchAgents/$(basename "$src")"
done
out=$(HOME="$home" bash "$home/repo/install.sh" 2>&1)
count=$(printf '%s\n' "$out" | grep -c "NEW launchd job(s)")
assert_eq "0" "$count" \
    "an install that added no plists must say nothing about new jobs"
rm -rf "$home"

# --- a from-scratch install (every plist reads as new) must not hand out -----
# --- bootstrap lines either — the case the removed freshness signal existed --
# --- to gate -------------------------------------------------------------------
#
# With an empty LaunchAgents, every plist on disk reads as new, overnight fleet
# included. There is now only one code path — the one exercised above — so this
# is the same guarantee at the size where the since-removed freshness signal
# used to matter, and where three of its four defeats were found.
home=$(setup)
out=$(HOME="$home" bash "$home/repo/install.sh" 2>&1)

assert_contains "$out" "NEW launchd job(s) installed for the first time" \
    "a from-scratch install still names what it copied"

overnight=$(printf '%s\n' "$out" \
            | grep -c "bootstrap.*com\.convivy\.overnight-orchestrator\.plist")
assert_eq "0" "$overnight" \
    "a fresh install must NOT hand out a ready bootstrap line for a job that is
     deliberately not on by default"

assert_contains "$out" "curated activation block" \
    "and must point at the curated block rather than listing bootstrap commands"
rm -rf "$home"

finish
