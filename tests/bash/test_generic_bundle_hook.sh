#!/bin/bash
# Tests for tools/generic-bundle-context.sh, the SessionStart hook that delivers
# `knowledge-ctx bundle --generic` where no render exists.
#
# `knowledge-ctx` is a stub named through KNOWLEDGE_CTX; HOME is a temp dir, so
# nothing here reads or writes the real ~/.claude. The failure cases assert the
# contract that a broken bundle never blocks a session: exit 0, empty stdout,
# exactly one stderr line.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

HOOK="$REPO_ROOT/tools/generic-bundle-context.sh"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME"

stub() { # <name> <body...> -> path of an executable stub
    printf '#!/bin/bash\n%s\n' "$2" > "$T/$1"; chmod +x "$T/$1"; echo "$T/$1"
}
GOOD=$(stub good 'printf "<!-- instruction-block: a -->\nRule \"quoted\" \\\\ back\\\\slash\n"')
FAILING=$(stub failing 'echo "index unreadable" >&2; exit 1')
EMPTY=$(stub empty 'exit 0')
WS="$T/ws"; mkdir -p "$WS"

run_hook() { # <cwd> <knowledge-ctx stub>
    OUT=$(cd "$1" && KNOWLEDGE_CTX="$2" /bin/bash "$HOOK" 2>"$T/err"); RC=$?
    ERR=$(cat "$T/err")
}

echo "== no manifest: valid SessionStart JSON carrying the bundle =="
run_hook "$WS" "$GOOD"
assert_rc 0 "$RC" "emit: exit 0"
PARSED=$(printf '%s' "$OUT" | python3 -c '
import json, sys
d = json.load(sys.stdin)["hookSpecificOutput"]
print(d["hookEventName"]); print(d["additionalContext"])')
assert_eq "SessionStart" "$(printf '%s\n' "$PARSED" | sed -n 1p)" "emit: hookEventName"
assert_contains "$PARSED" 'Rule "quoted" \ back\slash' "emit: quotes and backslashes survive the JSON round trip"
assert_eq "" "$ERR" "emit: nothing on stderr"

echo "== manifest in cwd: silent =="
mkdir -p "$T/r1/.claude"; : > "$T/r1/.claude/.profile-documents.json"
run_hook "$T/r1" "$GOOD"
assert_rc 0 "$RC" "rendered: exit 0"
assert_eq "" "$OUT" "rendered: no output"

echo "== hand-written .claude with no manifest: still emits =="
mkdir -p "$T/r2/.claude/agents"
run_hook "$T/r2" "$GOOD"
assert_contains "$OUT" '"additionalContext"' "hand-written tree: bundle emitted"

echo "== failing knowledge-ctx: exit 0, no stdout, one stderr line =="
run_hook "$WS" "$FAILING"
assert_rc 0 "$RC" "failing: exit 0"
assert_eq "" "$OUT" "failing: nothing on stdout"
assert_eq "1" "$(printf '%s\n' "$ERR" | grep -c .)" "failing: exactly one stderr line"
assert_contains "$ERR" "index unreadable" "failing: the one line names knowledge-ctx's own last stderr line"

echo "== multi-line stderr: only the last line, still one line =="
NOISY=$(stub noisy 'echo "first" >&2; echo "Traceback (most recent call last):" >&2; echo "last cause" >&2; exit 3')
run_hook "$WS" "$NOISY"
assert_rc 0 "$RC" "noisy: exit 0"
assert_eq "" "$OUT" "noisy: nothing on stdout"
assert_eq "1" "$(printf '%s\n' "$ERR" | grep -c .)" "noisy: exactly one stderr line"
assert_contains "$ERR" "last cause" "noisy: last stderr line included"

echo "== knowledge-ctx printing nothing: same =="
run_hook "$WS" "$EMPTY"
assert_rc 0 "$RC" "empty: exit 0"
assert_eq "" "$OUT" "empty: nothing on stdout"
assert_eq "1" "$(printf '%s\n' "$ERR" | grep -c .)" "empty: exactly one stderr line"

echo "== knowledge-ctx absent: same =="
run_hook "$WS" "$T/does-not-exist"
assert_rc 0 "$RC" "absent: exit 0"
assert_eq "" "$OUT" "absent: nothing on stdout"
assert_eq "1" "$(printf '%s\n' "$ERR" | grep -c .)" "absent: exactly one stderr line"

echo "== a part name is passed through as --part =="
ARGS=$(stub args 'printf "%s|" "$@"')
run_hook_part() { # <cwd> <stub> <part>
    OUT=$(cd "$1" && KNOWLEDGE_CTX="$2" /bin/bash "$HOOK" "$3" 2>"$T/err"); RC=$?
    ERR=$(cat "$T/err")
}
run_hook_part "$WS" "$ARGS" write-better
assert_rc 0 "$RC" "part: exit 0"
assert_contains "$OUT" 'bundle|--generic|--part|write-better|' "part: knowledge-ctx got --generic --part write-better"
run_hook "$WS" "$ARGS"
assert_contains "$OUT" 'bundle|--generic|' "no part: knowledge-ctx got no --part"
assert_eq "0" "$(printf '%s' "$OUT" | grep -c -- '--part')" "no part: --part is not passed"

echo "== a size refusal exits 1 with no stdout and one stderr line =="
TOOBIG=$(stub toobig 'echo "knowledge-ctx: generic bundle part '"'"'blocks'"'"' is 9500 bytes, over the 9000-byte cap; refusing to print it." >&2; exit 1')
run_hook_part "$WS" "$TOOBIG" blocks
assert_rc 1 "$RC" "size refusal: exit 1"
assert_eq "" "$OUT" "size refusal: nothing on stdout"
assert_eq "1" "$(printf '%s\n' "$ERR" | grep -c .)" "size refusal: exactly one stderr line"
assert_contains "$ERR" "9500 bytes, over the 9000-byte cap" "size refusal: the line names size and cap"

echo "== any other part failure still exits 0 =="
run_hook_part "$WS" "$FAILING" blocks
assert_rc 0 "$RC" "other failure with a part: exit 0"
assert_eq "" "$OUT" "other failure with a part: nothing on stdout"

finish
