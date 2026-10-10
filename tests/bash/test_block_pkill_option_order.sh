#!/bin/bash
# Tests for tools/block-pkill-option-order.sh — the PreToolUse(Bash) guard that
# refuses a pkill whose options follow its pattern (the 2026-10-05 08:27 occurrence).
# Black-box: feed crafted tool-input JSON on stdin, assert the hook's decision.
# No test runs pkill or pgrep; the guard only parses text.
#
# The hook fails OPEN without jq, so deny-assertions would spuriously pass as allow;
# skip the suite (still exit 0) when jq is absent rather than emit false greens.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

HOOK="$REPO_ROOT/tools/block-pkill-option-order.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq absent — hook fails open, decisions not exercised"
    finish
    exit 0
fi

# decision <command> -> echoes "deny" or "allow".
decision() {
    local out dec
    out=$(jq -n --arg c "$1" '{tool_input:{command:$c}}' | /bin/bash "$HOOK" 2>/dev/null)
    dec=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null)
    [ -z "$dec" ] && dec=allow
    echo "$dec"
}

# reason <command> -> echoes the deny reason, or nothing when allowed.
reason() {
    local out
    out=$(jq -n --arg c "$1" '{tool_input:{command:$c}}' | /bin/bash "$HOOK" 2>/dev/null)
    printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null
}

# --- the 2026-10-05 08:27 occurrence ---
assert_eq deny  "$(decision 'pkill -f "xctest -XCTest DeckTests" -u operator')" \
    "the 08:27 command (-u after the pattern) is refused"
assert_eq allow "$(decision 'pkill -u operator -f "xctest -XCTest DeckTests"')" \
    "the same command with -u before the pattern is allowed"

# --- allowed shapes ---
assert_eq allow "$(decision 'pkill -f pattern')"                  "pkill -f pattern is allowed"
assert_eq allow "$(decision 'pkill -9 -f pattern')"               "a signal before the pattern is allowed"
assert_eq allow "$(decision 'pkill -u operator -f pattern')"           "a value option before the pattern is allowed"
assert_eq allow "$(decision 'pkill -SIGTERM -f pattern')"         "a named signal before the pattern is allowed"
assert_eq allow "$(decision 'pkill -fu operator pattern')"             "a cluster ending in a value option takes the next word"
assert_eq allow "$(decision 'pkill -ujay -f pattern')"            "an attached value (-ujay) takes no next word"
assert_eq allow "$(decision 'pkill -P 1 -f pattern')"             "-P consumes its value before the pattern"
assert_eq allow "$(decision 'pkill -f -u operator pattern')"           "a value option after a flag but before the pattern is allowed"
assert_eq allow "$(decision 'pkill -- -weird-pattern')"           "after --, a dash-led word is a pattern"
assert_eq allow "$(decision 'pkill -f -- -u')"                    "a -- before the first pattern makes -u a pattern"
assert_eq allow "$(decision 'pkill -f -- -x')"                    "a -- before the first pattern makes a dash-led pattern"
assert_eq allow "$(decision 'pkill -u operator -f -- pat')"            "a -- after the options and before the pattern is allowed"
assert_eq allow "$(decision 'pkill -f "pat -u x"')"               "a quoted pattern containing ' -u ' is one argument"
assert_eq allow "$(decision 'pkill -f "a;b"')"                    "a quoted ; inside the pattern is not a separator"

# --- refused shapes ---
assert_eq deny  "$(decision 'pkill -f pattern -9')"               "a signal after the pattern is refused"
assert_eq deny  "$(decision 'pkill -f pattern -TERM')"            "a named signal after the pattern is refused"
assert_eq deny  "$(decision 'pkill -f pattern -ujay')"            "an attached value option after the pattern is refused"
assert_eq deny  "$(decision 'pkill -f "a;b" -u y')"               "a -u after a quoted ; pattern is refused"
assert_eq deny  "$(decision 'pkill -s x -f y')"                   "-s is not a pkill option: x is the pattern, so -f after it is refused"
assert_eq deny  "$(decision 'pkill -f pattern -- -u')"            "a -- after the pattern is a pattern, so -u after it is refused"
assert_eq deny  "$(decision 'pkill -f pat -- -u operator')"            "a -- after the pattern makes the options after it misordered"
assert_eq deny  "$(decision 'pkill -f pat --')"                   "a -- after the pattern is itself refused"
assert_eq deny  "$(decision 'pkill -- -x -u y')"                  "after a -- that precedes the first pattern, -u is still refused"
assert_eq deny  "$(decision 'pkill -- -x --')"                    "a second -- after a dash-led first pattern is refused"

# --- misplaced option behind each segment boundary and wrapper ---
assert_eq deny  "$(decision 'true && pkill -f x -u y')"           "pkill after && is caught"
assert_eq deny  "$(decision 'echo hi; pkill -f x -u y')"          "pkill after ; is caught"
assert_eq deny  "$(decision 'echo hi || pkill -f x -u y')"        "pkill after || is caught"
assert_eq deny  "$(decision 'true | pkill -f x -u y')"            "pkill after | is caught"
assert_eq deny  "$(decision 'true
pkill -f x -u y')"                                                "pkill on a new line is caught"
assert_eq deny  "$(decision 'sudo pkill -f x -u y')"              "pkill after sudo is caught"
assert_eq deny  "$(decision 'sudo -u root pkill -f x -u y')"      "pkill after sudo and its own option is caught"
assert_eq deny  "$(decision 'env X=1 pkill -f x -u y')"           "pkill after env and an assignment is caught"
assert_eq deny  "$(decision 'X=1 pkill -f x -u y')"               "pkill after a bare assignment is caught"
assert_eq deny  "$(decision 'exec pkill -f x -u y')"              "pkill after exec is caught"
assert_eq deny  "$(decision 'nohup pkill -f x -u y')"             "pkill after nohup is caught"
assert_eq deny  "$(decision 'time pkill -f x -u y')"              "pkill after time is caught"
assert_eq deny  "$(decision 'command pkill -f x -u y')"           "pkill after command is caught"
assert_eq deny  "$(decision 'env -P /usr/bin pkill -f x -u y')"  "pkill after env -P (a value option) is caught"
assert_eq deny  "$(decision '{ pkill -f x -u y; }')"              "pkill inside a brace group is caught"
assert_eq deny  "$(decision 'echo $(pkill -f x -u y)')"           "pkill inside \$(...) is caught"
assert_eq deny  "$(decision 'echo `pkill -f x -u y`')"            "pkill inside backticks is caught"
assert_eq deny  "$(decision 'echo "$(pkill -f x -u y)"')"         "pkill inside \$(...) in double quotes is caught"
assert_eq deny  "$(decision '/usr/bin/pkill -f x -u y')"          "/usr/bin/pkill is caught"
assert_eq deny  "$(decision 'pkill -f "$(cat f)" -u j')"          "a substitution inside the pattern does not split the command"
assert_eq deny  "$(decision 'echo x > out.txt && pkill -f x -u y')" "pkill after a redirect and && is caught"
assert_eq deny  "$(decision 'pkill -f x 2>&1 -u y')"              "a fd redirect does not hide a misplaced -u"

# --- a redirect before a substitution or subshell does not hide the pkill ---
# The redirect operator sets skip (its target is dropped). An opener after it must not
# inherit that skip, or the first word inside the opener is dropped as the target.
assert_eq deny  "$(decision 'echo x > $(pkill -f a -u b)')"       "pkill in \$(...) after a redirect is caught"
assert_eq deny  "$(decision 'echo x >$(pkill -f a -u b)')"        "pkill in \$(...) after an attached redirect is caught"
assert_eq deny  "$(decision 'echo x > `pkill -f a -u b`')"        "pkill in backticks after a redirect is caught"
assert_eq deny  "$(decision 'cat <(pkill -f a -u b)')"            "pkill in <(...) is caught"
assert_eq deny  "$(decision 'cat >(pkill -f a -u b)')"            "pkill in >(...) is caught"
assert_eq deny  "$(decision 'echo x > (pkill -f a -u b)')"        "pkill in a subshell after a redirect is caught (bash rejects this form; the guard still checks it)"
assert_eq deny  "$(decision '>f$(true) pkill -f x -u y')"         "a substitution in a redirect target does not hide the command after it"
assert_eq allow "$(decision 'cat <(echo pkill -f a -u b)')"       "pkill as an echo argument inside <(...) after a redirect is allowed"

# --- spellings the shell rebuilds into pkill: the fast path must not skip them ---
assert_eq deny  "$(decision "p''kill -f x -u y")"                "p''kill (an empty quote inside the name) is caught"
assert_eq deny  "$(decision 'p\kill -f x -u y')"                 "p\\kill (an escaped letter inside the name) is caught"
assert_eq deny  "$(decision '"p"kill -f x -u y')"                "\"p\"kill (a quoted letter inside the name) is caught"

# --- a backslash-newline is deleted by the shell before it reads words ---
# The fast path and the << test read a copy with each pair removed; the tokenizer keeps the original.
assert_eq deny  "$(decision 'pk\
ill -f x -u y')"                                                 "pk, a backslash-newline, then ill is caught (the shell runs pkill)"
assert_eq deny  "$(decision 'p\
kill -f x -u y')"                                                "p, a backslash-newline, then kill is caught (the shell runs pkill)"
assert_eq deny  "$(decision $'cat <\\\n<E\nit\'s\nE\npkill -f x -u y')" \
    "a heredoc whose << is split by a backslash-newline is refused (the shell reads it as <<E)"
assert_eq deny  "$(decision 'echo $((1<<2)); pkill -u operator -f x')" \
    "an arithmetic shift makes the whole command a << refusal, as the header says"

# --- a backslash-newline inside double quotes is deleted by the shell too ---
assert_eq deny  "$(decision '"pk\
ill" -f x -u y')"                                                "pk, a backslash-newline, then ill inside double quotes is caught (the shell runs pkill)"
assert_eq deny  "$(decision 'pkill -f x "\
-u" operator')"                                                       "a backslash-newline inside double quotes before -u is caught (the shell reads -u as an option)"

# --- allowed: mentions and non-pkill commands ---
assert_eq allow "$(decision 'echo "pkill -f x -u y"')"            "pkill mentioned inside a quoted echo is allowed"
assert_eq allow "$(decision 'echo pkill -f x -u y')"              "pkill as an echo argument is allowed"
assert_eq allow "$(decision 'git commit -m "fix pkill -f x -u y"')" "pkill mentioned in a commit message is allowed"
assert_eq allow "$(decision 'grep pkill -f x -u y file')"         "pkill as a grep argument is allowed"
assert_eq allow "$(decision 'ls -la /tmp')"                       "a non-pkill command is allowed"
assert_eq allow "$(decision 'echo hi # pkill -f x -u y')"         "pkill in a shell comment is allowed"
assert_eq allow "$(decision 'echo "pkill -f a -u b"; ls')"        "a quoted mention before a separator is allowed"

# --- heredocs and herestrings: a command with pkill and << is refused whole ---
# The guard does not parse bodies, so each shape that opens one is refused, not tracked.
assert_eq deny  "$(decision "$(printf "cat <<'EOF' > n.txt\ndon't\nEOF\npkill -f x -u y")")" \
    "a heredoc with an apostrophe in its body and a misplaced pkill after the terminator is refused"
assert_eq deny  "$(decision $'cat <<\x5cEOF\nit\'s\nEOF\npkill -f x -u y')" \
    "a backslash-quoted heredoc delimiter (<<\\EOF) with a misplaced pkill is refused"
assert_eq deny  "$(decision $'cat <<$\'EOF\'\nit\'s\nEOF\npkill -f x -u y')" \
    "an ANSI-C quoted heredoc delimiter (<<\$'EOF') with a misplaced pkill is refused"
assert_eq deny  "$(decision $'cat <<A <<B\nB\nit\'s\nA\nB\npkill -f x -u y')" \
    "two heredocs on one line with a misplaced pkill are refused"
assert_eq deny  "$(decision 'bash <<EOF
echo "
" ; pkill -f x -u y
EOF')" \
    "a quote opened on one heredoc body line does not hide a pkill on the next"
assert_eq deny  "$(decision 'python3 -c "
x = 1 << n
print(x)
"; pkill -f a -u b')" \
    "a << inside a python3 -c string (arithmetic) does not hide a misplaced pkill"
assert_eq deny  "$(decision 'echo "a <<b
"; pkill -f a -u b')" \
    "a << inside an unclosed quote does not hide a misplaced pkill"
assert_eq deny  "$(decision 'bash <<< "pkill -f x -u y"')"       "a herestring carrying a misplaced pkill is refused"
assert_eq allow "$(decision "$(printf "cat <<'EOF'\ndon't\nEOF")")" \
    "a heredoc command with no pkill anywhere is allowed"

r=$(reason "$(printf 'cat <<EOF\npkill -f x -u y\nEOF')")
assert_contains "$r" "contains <<" "the << refusal says the command contains <<"
assert_contains "$r" "own Bash command" "the << refusal tells the agent to run the pkill as its own Bash command"
assert_contains "$r" "-F" "the << refusal names git commit -F for text that is only a commit message"
assert_contains "$r" "--body-file" "the << refusal names gh pr create --body-file for text that is only a PR body"
assert_eq 0 "$(printf '%s' "$r" | grep -c 'script file')" "the << refusal does not send the agent to a script file, which the guard does not check"

# --- the refusal names the option and says what to do ---
r=$(reason 'pkill -f x -u y')
assert_contains "$r" "'-u'"          "the reason names the misplaced option"
assert_contains "$r" "macOS pkill"   "the reason explains the macOS parsing rule"
assert_contains "$r" "every option before the pattern" "the reason says to put every option before the pattern"

# --- fail-open: input the guard cannot parse is allowed, exit 0 ---
out=$(printf '%s' '{}' | /bin/bash "$HOOK" 2>/dev/null); rc=$?
assert_rc 0 "$rc" "a payload with no tool_input.command exits 0"
assert_eq "" "$out" "a payload with no tool_input.command prints nothing"
printf '%s' 'not json' | /bin/bash "$HOOK" >/dev/null 2>&1; rc=$?
assert_rc 0 "$rc" "unparseable stdin exits 0"

finish
