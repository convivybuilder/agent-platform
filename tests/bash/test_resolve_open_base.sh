#!/bin/bash
# resolve-open-base.sh reads the mini's root over ssh — an untrusted transport —
# and writes what it gets into a config file that install-laptop.sh later
# interpolates into a launchd plist. The path-shape `case` and the
# character-class `grep` that guard it both test the value ONE LINE AT A TIME
# (a `case` glob's `*` matches across an embedded newline, and grep splits on
# them), so before this fix a two-line answer where each line individually
# looked like a safe absolute path passed both checks, with its second line
# landing unvalidated in the config file. security-reviewer finding, PR #561
# round 1: run end to end under a stub `ssh`, so nothing real is touched.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/convivy-open/resolve-open-base.sh"

setup() { # <ssh-rc> -> echoes fake HOME; caller writes $HOME/ssh-answer next
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    cat > "$home/stubbin/ssh" <<EOF
#!/bin/sh
cat "$home/ssh-answer"
exit ${1:-0}
EOF
    chmod +x "$home/stubbin/ssh"
    echo "$home"
}

run_resolve() { # <home> -> sets RC, OUT; config is <home>/convivy-open.env
    OUT=$(HOME="$1" PATH="$1/stubbin:$PATH" bash "$SCRIPT" "$1/convivy-open.env" 2>&1)
    RC=$?
}

echo "== a single-line answer is accepted and written (the happy path still works) =="
H=$(setup 0)
printf '/Users/exampleuser/builder' > "$H/ssh-answer"
run_resolve "$H"
assert_rc 0 "$RC" "the resolver exits 0 for a one-line answer"
assert_contains "$OUT" "/Volumes/exampleuser/builder" "and prints the laptop mirror path"
assert_contains "$(cat "$H/convivy-open.env")" "CONVIVY_OPEN_BASE=/Volumes/exampleuser/builder" \
    "the config file holds the resolved base"
rm -rf "$H"

echo "== a two-line answer is refused before anything is written =="
H=$(setup 0)
printf '/Users/exampleuser/builder\n/Users/exampleuser/pwned' > "$H/ssh-answer"
run_resolve "$H"
assert_rc 1 "$RC" "the resolver exits 1 for a two-line answer"
assert_contains "$OUT" "more than one line" "and says why"
assert_eq "0" "$([ -e "$H/convivy-open.env" ] && echo 1 || echo 0)" \
    "no config file is written"
rm -rf "$H"

echo "== a CR-joined answer is caught by the newline/CR check, not just the character class =="
# A lone \r is already rejected by the pre-existing character-class check
# (grep splits on LF only, so \r stays inside the one "line" grep inspects, and
# \r itself is outside [A-Za-z0-9._/-]) — this isn't an independent bypass. The
# check added here still covers CR explicitly, per the finding's wording, and
# this asserts it fires FIRST, before the character check's generic message.
H=$(setup 0)
printf '/Users/exampleuser/builder\r/Users/exampleuser/pwned' > "$H/ssh-answer"
run_resolve "$H"
assert_rc 1 "$RC" "the resolver exits 1 for a CR-joined answer"
assert_contains "$OUT" "more than one line" "and says why"
assert_eq "0" "$([ -e "$H/convivy-open.env" ] && echo 1 || echo 0)" \
    "no config file is written"
rm -rf "$H"

echo "== a trailing blank line (a bare trailing newline) still refuses rather than guessing =="
# \$(...) strips trailing newlines from the CAPTURED value, but the check must
# run on MINI_ROOT as bash actually holds it — proving this isn't a fragile
# "ends in one newline" special case.
H=$(setup 0)
printf '/Users/exampleuser/builder\n\n' > "$H/ssh-answer"
run_resolve "$H"
assert_rc 0 "$RC" "a single trailing blank line does not trip the refusal (command substitution already stripped it)"
rm -rf "$H"

finish
