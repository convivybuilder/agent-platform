#!/bin/bash
# install.sh --migrate-labels (CQ-428) moves each gui-domain job from its
# pre-rename personal label onto com.convivy.<job>, bootstrapping the new
# label before touching the old one. reviewer finding, PR #841 round 1: the
# final step ran `launchctl bootout "gui/$uid/$old_label" 2>/dev/null || true`
# and then unconditionally removed the old plist and reported success — a
# bootout that didn't actually take (KeepAlive respawn, or any other failure
# `|| true` swallows) left the old job loaded, headless (its plist gone, no
# record left to retry from), while install.sh exited 0. The fix confirms
# `launchctl print` on the old label fails before touching its plist, keeps
# the plist and reports the job NOT migrated otherwise, and exits non-zero
# overall so a human (or a cron re-run) notices.
#
# Hermetic: a fixture install.sh (MIGRATED_JOBS trimmed to two synthetic job
# names, never the real pre-rename personal namespace this script's own
# comment refuses to hardcode) + a fixture launchd/ dir, run against a
# stateful launchctl stub (not the lib.sh default, which only refuses) that
# tracks which labels are "loaded" across the run in a plain file, so print/
# list/bootstrap/bootout behave consistently with each other and with what
# the script just did. Every call is logged for ordering assertions.

set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

UID_N=$(id -u)

JOB="widget-sync"
JOB2="gizmo-sync"
OLD_LABEL="com.fixtureuser.$JOB"
NEW_LABEL="com.convivy.$JOB"
OLD_LABEL2="com.fixtureuser.$JOB2"
NEW_LABEL2="com.convivy.$JOB2"

FIXTURE=$(mktemp -d)
cp "$REPO_ROOT/install.sh" "$FIXTURE/install.sh"
# Trim MIGRATED_JOBS to the two synthetic names above so --only validates
# against them and the fixture launchd/ dir only needs two templates.
sed -i.bak "s/^MIGRATED_JOBS=.*/MIGRATED_JOBS=\"$JOB $JOB2\"/" "$FIXTURE/install.sh"
rm -f "$FIXTURE/install.sh.bak"
mkdir -p "$FIXTURE/launchd"
write_plist_template() { # <label> <dest>
    cat > "$2" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>__HOME__/bin/true</string></array>
</dict>
</plist>
PLIST
}
write_plist_template "$NEW_LABEL" "$FIXTURE/launchd/$NEW_LABEL.plist"
write_plist_template "$NEW_LABEL2" "$FIXTURE/launchd/$NEW_LABEL2.plist"

setup() { # -> echoes fresh fake HOME with an empty LaunchAgents
    local home
    home=$(mktemp -d)
    mkdir -p "$home/Library/LaunchAgents"
    echo "$home"
}

# make_stub <stubdir> [loaded-label ...]: a launchctl stub that tracks loaded
# labels in a file across subprocess calls, so a bootstrap this run performs
# is visible to the SAME run's later print/bootout calls. Controlled via
# three more files in the same dir: refuse_bootout (bootout fails, label
# stays loaded — the exact masked-by-`|| true` case), refuse_bootstrap
# (bootstrap itself fails), silent_bootstrap (bootstrap exits 0 but never
# actually registers the label — the "print afterward fails" case).
make_stub() {
    local dir=$1
    shift
    mkdir -p "$dir"
    : > "$dir/state"
    : > "$dir/log"
    : > "$dir/refuse_bootout"
    : > "$dir/refuse_bootstrap"
    : > "$dir/silent_bootstrap"
    local lbl
    for lbl in "$@"; do printf '%s\n' "$lbl" >> "$dir/state"; done
    cat > "$dir/launchctl" <<'STUB'
#!/bin/bash
STUBDIR="$(cd "$(dirname "$0")" && pwd)"
printf '%s\n' "$*" >> "$STUBDIR/log"
state="$STUBDIR/state"
case "$1" in
    print)
        label="${2##*/}"
        grep -qxF "$label" "$state" 2>/dev/null && exit 0
        exit 1
        ;;
    list)
        echo "header-line-ignored-by-callers-NR-gt-1-check"
        n=1
        while IFS= read -r lbl; do
            [ -z "$lbl" ] && continue
            printf '%s\t0\t%s\n' "$n" "$lbl"
            n=$((n + 1))
        done < "$state"
        exit 0
        ;;
    bootstrap)
        plist="$3"
        label=$(basename "$plist" .plist)
        if grep -qxF "$label" "$STUBDIR/refuse_bootstrap" 2>/dev/null; then
            echo "stub: bootstrap of $label refused" >&2
            exit 1
        fi
        if grep -qxF "$label" "$STUBDIR/silent_bootstrap" 2>/dev/null; then
            exit 0
        fi
        printf '%s\n' "$label" >> "$state"
        exit 0
        ;;
    bootout)
        label="${2##*/}"
        if grep -qxF "$label" "$STUBDIR/refuse_bootout" 2>/dev/null; then
            echo "stub: bootout of $label refused" >&2
            exit 1
        fi
        grep -vxF "$label" "$state" > "$state.tmp" 2>/dev/null || : > "$state.tmp"
        mv "$state.tmp" "$state"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
STUB
    chmod +x "$dir/launchctl"
}

run_migrate() { # <home> <stubdir> [install.sh args...] -> sets OUT, RC
    local home=$1 stub=$2
    shift 2
    OUT=$(HOME="$home" PATH="$stub:$PATH" bash "$FIXTURE/install.sh" --migrate-labels "$@" 2>&1)
    RC=$?
}

first_line() { grep -n -F -- "$2" "$1" 2>/dev/null | head -1 | cut -d: -f1; } # <log> <needle>
last_line()  { grep -n -F -- "$2" "$1" 2>/dev/null | tail -1 | cut -d: -f1; } # <log> <needle>
count_matching() { grep -c -F -- "$2" "$1" 2>/dev/null || true; } # <log> <needle>

# --- only the old label loaded: bootstrap -> confirm -> bootout -> confirm ---
echo "== only the old label loaded =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 0 "$RC" "a clean migration exits 0"
bootstrap_line=$(first_line "$S/log" "bootstrap gui/$UID_N $H/Library/LaunchAgents/$NEW_LABEL.plist")
bootout_line=$(first_line "$S/log" "bootout gui/$UID_N/$OLD_LABEL")
confirm_old_line=$(last_line "$S/log" "print gui/$UID_N/$OLD_LABEL")
new_print_count=$(count_matching "$S/log" "print gui/$UID_N/$NEW_LABEL")
assert_eq "1" "$([ -n "$bootstrap_line" ] && [ -n "$bootout_line" ] && [ "$bootstrap_line" -lt "$bootout_line" ] && echo 1 || echo 0)" \
    "bootstrap(new) happens before bootout(old)"
assert_eq "1" "$([ -n "$confirm_old_line" ] && [ -n "$bootout_line" ] && [ "$confirm_old_line" -gt "$bootout_line" ] && echo 1 || echo 0)" \
    "print(old), confirming it is gone, happens after bootout(old)"
assert_eq "2" "$new_print_count" "print(new) runs twice: once to decide, once to confirm after bootstrap"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/.pre-migration-backup/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist is backed up before removal"
assert_eq "0" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist file is gone"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$NEW_LABEL.plist" ] && echo 1 || echo 0)" \
    "the new plist is installed"
rm -rf "$H" "$S"

# --- new already loaded AND old still loaded (an interrupted run resumes) ---
echo "== new already loaded, old still loaded (interrupted run) =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
S=$(mktemp -d)
make_stub "$S" "$NEW_LABEL" "$OLD_LABEL"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 0 "$RC" "a resumed migration still exits 0"
assert_eq "0" "$(count_matching "$S/log" "bootstrap ")" \
    "no bootstrap call when the new label is already loaded"
assert_eq "1" "$(count_matching "$S/log" "bootout gui/$UID_N/$OLD_LABEL")" \
    "bootout(old) still runs exactly once"
assert_eq "0" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist is cleaned up on resume"
rm -rf "$H" "$S"

# --- neither label loaded: nothing happens ----------------------------------
echo "== neither label loaded =="
H=$(setup)
S=$(mktemp -d)
make_stub "$S"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 0 "$RC" "nothing loaded is not a failure"
assert_eq "0" "$(count_matching "$S/log" "bootstrap ")" "no bootstrap when neither label is loaded"
assert_eq "0" "$(count_matching "$S/log" "bootout ")" "no bootout when neither label is loaded"
assert_eq "0" "$(find "$H/Library/LaunchAgents" -type f | wc -l | tr -d ' ')" \
    "no file is written under LaunchAgents"
rm -rf "$H" "$S"

# --- bootout fails: old plist kept, job reported NOT migrated, other jobs --
# --- in the same run still migrate, overall exit is non-zero ---------------
echo "== bootout fails (stub refuses); other jobs in the run still migrate =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
cp "$FIXTURE/launchd/$NEW_LABEL2.plist" "$H/Library/LaunchAgents/$OLD_LABEL2.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL" "$OLD_LABEL2"
printf '%s\n' "$OLD_LABEL" >> "$S/refuse_bootout"
run_migrate "$H" "$S"
assert_rc 1 "$RC" "a refused bootout fails the overall run"
assert_contains "$OUT" "still loaded after bootout" "the failure names itself"
assert_contains "$OUT" "NOT migrated" "and reports the job as not migrated"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist is kept when bootout fails to actually take"
assert_eq "0" "$([ -f "$H/Library/LaunchAgents/.pre-migration-backup/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "no backup is made for a job whose old label never actually went away"
assert_eq "0" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL2.plist" ] && echo 1 || echo 0)" \
    "job2, whose bootout was not refused, still migrated in the same run"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$NEW_LABEL2.plist" ] && echo 1 || echo 0)" \
    "job2's new plist is installed"
rm -rf "$H" "$S"

# --- bootstrap(new) itself fails: old label untouched, old plist kept ------
echo "== bootstrap(new) fails =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL"
printf '%s\n' "$NEW_LABEL" >> "$S/refuse_bootstrap"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 1 "$RC" "a failed bootstrap fails the run"
assert_contains "$OUT" "bootstrap of $NEW_LABEL FAILED" "names the failure"
assert_eq "0" "$(count_matching "$S/log" "bootout ")" "bootout never runs when bootstrap itself failed"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist is untouched"
rm -rf "$H" "$S"

# --- bootstrap(new) exits 0 but print(new) fails afterward -----------------
echo "== bootstrap(new) succeeds but print(new) fails afterward =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL"
printf '%s\n' "$NEW_LABEL" >> "$S/silent_bootstrap"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 1 "$RC" "a bootstrap that silently no-ops fails the run"
assert_contains "$OUT" "did not come up after bootstrap" "names the failure"
assert_eq "0" "$(count_matching "$S/log" "bootout ")" "bootout never runs when the new label never actually came up"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "the old plist is untouched"
rm -rf "$H" "$S"

# --- --dry-run: read-only, no file under HOME changes -----------------------
echo "== --dry-run =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL"
before=$(find "$H" -type f | sort)
run_migrate "$H" "$S" --dry-run --only "$JOB"
assert_rc 0 "$RC" "dry-run exits 0"
after=$(find "$H" -type f | sort)
assert_eq "$before" "$after" "dry-run writes no file under the tmp HOME"
other_verbs=$(awk '{print $1}' "$S/log" | sort -u | grep -vE '^(print|list)$')
assert_eq "" "$other_verbs" "the log holds only print/list calls under dry-run"
rm -rf "$H" "$S"

# --- --only <job> touches only that job -------------------------------------
echo "== --only touches only the named job =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
cp "$FIXTURE/launchd/$NEW_LABEL2.plist" "$H/Library/LaunchAgents/$OLD_LABEL2.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL" "$OLD_LABEL2"
run_migrate "$H" "$S" --only "$JOB"
assert_rc 0 "$RC" "a scoped run exits 0"
assert_eq "0" "$(count_matching "$S/log" "$JOB2")" "nothing in the log names job2 at all"
assert_eq "1" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL2.plist" ] && echo 1 || echo 0)" \
    "job2's old plist is untouched"
assert_eq "0" "$([ -f "$H/Library/LaunchAgents/$OLD_LABEL.plist" ] && echo 1 || echo 0)" \
    "job1's old plist IS migrated"
rm -rf "$H" "$S"

# --- rollback lines print before any change, naming each new->old pair -----
echo "== rollback lines are printed up front, before any change =="
H=$(setup)
cp "$FIXTURE/launchd/$NEW_LABEL.plist" "$H/Library/LaunchAgents/$OLD_LABEL.plist"
cp "$FIXTURE/launchd/$NEW_LABEL2.plist" "$H/Library/LaunchAgents/$OLD_LABEL2.plist"
S=$(mktemp -d)
make_stub "$S" "$OLD_LABEL" "$OLD_LABEL2"
run_migrate "$H" "$S"
assert_rc 0 "$RC" "both jobs migrate cleanly"
assert_contains "$OUT" "$NEW_LABEL -> $OLD_LABEL" "the rollback section names job1's new->old pair"
assert_contains "$OUT" "$NEW_LABEL2 -> $OLD_LABEL2" "the rollback section names job2's new->old pair"
mapping_line=$(printf '%s\n' "$OUT" | grep -n "Reverse mapping" | head -1 | cut -d: -f1)
first_action_line=$(printf '%s\n' "$OUT" | grep -n "bootstrapping\|booted out and removed" | head -1 | cut -d: -f1)
assert_eq "1" "$([ -n "$mapping_line" ] && [ -n "$first_action_line" ] && [ "$mapping_line" -lt "$first_action_line" ] && echo 1 || echo 0)" \
    "the rollback mapping prints before any migration action starts"
rm -rf "$H" "$S"

rm -rf "$FIXTURE"
finish
