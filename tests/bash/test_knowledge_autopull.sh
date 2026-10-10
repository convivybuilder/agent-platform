#!/bin/bash
# Tests for bin/knowledge-autopull, the 10-minute launchd wrapper over
# `agentctl kb sync`.
#
# The contract under test is the log's signal-to-noise. `kb sync` says what it
# verified on every run, which is what an operator at a keyboard wants; the
# timer passes `--quiet` so a healthy up-to-date cycle writes NOTHING to
# autopull.log, and anything `kb sync` still says (a fast-forward, an invalid
# doc, a heal) is kept verbatim, with exit 2 and any other non-zero exit named
# after it. A fake `agentctl` beside a copy of the script records its argv and
# plays back a scripted output + exit code, so these are behavioural
# assertions about what reaches the log, not a grep of the source.
#
# The script resolves `agentctl` as a sibling of its OWN real path (deploy rule
# 1), so the fake must sit next to a copy of the script rather than on PATH.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

# Fake HOME holding a copy of the script, its fake sibling agentctl, and the
# log directory the script appends into. Echoes the fake HOME.
setup() { # <fake-output> <fake-rc> [precreate-index=yes] -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/bin"
    [ "${3:-yes}" = "yes" ] && mkdir -p "$home/.knowledge-index"
    cp "$REPO_ROOT/bin/knowledge-autopull" "$home/bin/knowledge-autopull"
    printf '%s' "$1" > "$home/fake_out"
    printf '%s' "$2" > "$home/fake_rc"
    cat > "$home/bin/agentctl" <<'FAKE'
#!/bin/bash
here=$(cd "$(dirname "$0")/.." && pwd)
printf '%s\n' "$*" >> "$here/argv"
out=$(cat "$here/fake_out")
[ -n "$out" ] && printf '%s\n' "$out"
exit "$(cat "$here/fake_rc")"
FAKE
    chmod +x "$home/bin/agentctl" "$home/bin/knowledge-autopull"
    echo "$home"
}

run_autopull() { # <fake HOME> -> echoes rc
    HOME="$1" /bin/zsh "$1/bin/knowledge-autopull" >/dev/null 2>&1
    echo $?
}

log_of() { cat "$1/.knowledge-index/autopull.log" 2>/dev/null; }

# --- 1. a healthy up-to-date cycle writes nothing --------------------------------
# `kb sync --quiet` prints nothing when the checkout is current and the index
# verifies, and the wrapper must add nothing of its own: 144 "up to date"
# lines a day is the log nobody reads.
H=$(setup "" 0)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a healthy cycle exits 0"
assert_contains "$(cat "$H/argv")" "kb sync --quiet" "the timer passes --quiet"
assert_eq "" "$(log_of "$H")" "a healthy up-to-date cycle writes nothing to the log"
rm -rf "$H"

# --- 2. what kb sync does say is kept verbatim ----------------------------------
H=$(setup "kb sync: 0123456ab -> 89abcdef0 — 3 indexed, 0 removed, 0 invalid" 0)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a fast-forward cycle exits 0"
assert_contains "$(log_of "$H")" "3 indexed" "a fast-forward's summary reaches the log"
rm -rf "$H"

# --- 3. exit 2 is named as an inconsistent index --------------------------------
H=$(setup "kb sync: VERIFY FAILED — 1 missing, 0 extra, 0 stale" 2)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "the wrapper itself exits 0 so launchd does not back off"
assert_contains "$(log_of "$H")" "VERIFY FAILED" "the failure line is kept"
assert_contains "$(log_of "$H")" "INDEX INCONSISTENT" "exit 2 is named after it"
rm -rf "$H"

# --- 4. any other non-zero exit is named with its code ---------------------------
H=$(setup "kb sync: fast-forward REFUSED" 1)
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a refused fast-forward still exits 0 from the wrapper"
assert_contains "$(log_of "$H")" "kb sync failed (exit 1)" "a non-zero exit is named"
rm -rf "$H"

# --- 5. a fresh account with no ~/.knowledge-index yet still logs -------------
# issue_01M3J7Q5ZFCWZMD9FWSNVKPKDH: `log()` appends to $LOG with no parent-dir
# check, which on a fresh account silently loses every line (zsh's `>>` refuses
# a missing parent directory) rather than crashing loudly.
H=$(setup "kb sync: 0123456ab -> 89abcdef0 — 3 indexed, 0 removed, 0 invalid" 0 no)
assert_eq "no" "$([ -e "$H/.knowledge-index" ] && echo yes || echo no)" \
    "fixture sanity: no .knowledge-index exists yet"
rc=$(run_autopull "$H")
assert_rc 0 "$rc" "a fresh account still exits 0"
assert_eq "yes" "$([ -d "$H/.knowledge-index" ] && echo yes || echo no)" \
    "the log directory is created"
assert_contains "$(log_of "$H")" "3 indexed" "the log line survives on a fresh account"
rm -rf "$H"

finish
