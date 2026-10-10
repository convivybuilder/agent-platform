#!/bin/bash
# PreToolUse(Bash) guard — refuses a pkill whose options follow its pattern.
#
# THE OCCURRENCE. On 2026-10-05 at 08:27:10 MST a dispatched coder ran
#   pkill -f "xctest -XCTest DeckTests" -u operator
# macOS's BSD pkill stops parsing options at its first non-option argument, so
# `-u` and `operator` became two more patterns. `operator` matches every command
# line containing the operator's home directory, and the command SIGTERMed tmux, every crew
# session, two coder runs, fourteen launchd jobs and Docker Desktop. Steward
# control: `pgrep -a -f zzz-no-such -u operator` matched 143 processes; with
# `-u operator` placed first it matched 2.
#
# THE PARSE RULE. The command is split into segments at unquoted ; & | newline
# and ( ) — the same boundary block-pr-merge.sh anchors on — and `$(...)` and
# backticks open a segment of their own. A segment whose command word (after
# sudo/env/exec/nohup/time/command/{/! wrappers and NAME=value assignments) is
# pkill, or a path ending in /pkill, is checked. In its arguments:
#   - `--` before the first pattern ends options; every later word is a pattern, `-` included.
#   - a word beginning with `-` that follows the first pattern is a refusal, `--` included.
#     A `--` after a pattern is itself a pattern, so the options after it are misordered.
#   - an option that takes a value (-u -U -g -G -t -P -F, or sudo/env's own)
#     consumes the next word as its value, unless the value is attached (-ujay).
#   - a signal (-9, -TERM, -SIGTERM) is an ordinary option.
# A quoted word is one argument, so `pkill -f "x -u y"` is allowed.
#
# SPELLINGS. The fast path removes quotes and backslashes before it looks for pkill, so
# p''kill, p\kill and "p"kill reach the tokenizer, which rebuilds each into pkill. The fast
# path and the `<<` test run on a copy with every backslash-newline pair removed, which is
# at least as broad as bash (bash keeps the pair inside single quotes). The tokenizer reads
# the original and skips the pair outside single quotes, except between `$` and `(` inside
# double quotes, which is a residual below.
#
# `<<`. A command that contains pkill and the two characters `<<` is refused whole. The guard
# does not parse heredoc bodies or herestrings, so it cannot tell a body from a command.
# Cost: this refuses an arithmetic shift such as $((1<<2)), and a commit message or PR body
# passed through a heredoc (git commit -m "$(cat <<'EOF' ... EOF)") that mentions pkill. Such
# text goes through a file instead: git commit -F <file>, or gh pr create --body-file <file>.
#
# SCOPE. The guard exists for one occurrence (above): an agent wrote pkill with its options
# after the pattern, in an ordinary command. It catches that accidental misordering. It is
# not a sandbox against an agent that deliberately spells pkill so the guard cannot read it.
# The residuals below are such spellings. None is decoded, and none has a gate of its own.
# pkill only. pgrep lists processes, and no killall occurrence has been seen.
#
# RESIDUALS. The guard does not evaluate variables. The contents of a command that runs its
# own argument are text to this guard. The list is not exhaustive. None of these shapes
# occurs in this repo (the only pkill text is in comments), and none is parsed:
#   - a command that runs its argument: bash, sh or zsh -c "...", find -exec pkill,
#     osascript, ssh, xargs pkill, eval, and text piped into a shell (echo "pkill ..." | bash)
#   - a wrapper outside the list above: nice, timeout, gtimeout, setsid, caffeinate,
#     stdbuf, ionice, doas, builtin, coproc
#   - env -S '...', whose quoted string is one word; sudo -R and -T values
#   - a pkill inside a function body: f() { pkill -f x -u y; }; f
#   - a name assembled at run time: $(echo pkill) -f x -u y
#   - brace expansion: p{k,}ill -f x -u y runs pkill, and the fast path sees no pkill
#   - ANSI-C quoting, which the tokenizer reads as plain single quotes: $'pkill' is allowed,
#     $'x\'' ends the quote early, and p$''kill, p$""kill, $'\x70kill' and any other $'...'
#     escape leave no pkill text after the fast path's quote removal
#   - locale quoting: $"pkill" -f x -u y runs pkill, and the tokenizer reads the word as $pkill
#   - a name in another case: PKILL -f x -u y. On an APFS volume /usr/bin/PKILL is the same file
#     as /usr/bin/pkill, so bash runs pkill here, and the fast path and base-name test compare
#     case-sensitively
#   - a backslash-newline between $ and ( inside double quotes: "$\<newline>(pkill -f x -u y)".
#     Bash removes the pair and runs the substitution; the tokenizer reads $ as literal. Unquoted,
#     the same text is refused.
#   - a script written with the Write tool and run later with bash
#
# FAIL BEHAVIOUR. Fail-open, as block-pr-merge.sh is: a missing jq, unparseable
# stdin or a missing tool_input.command allows the call. Only a parsed refusal denies.
# Bash 3.2 safe — runs under the mini's /bin/bash 3.2 (ARCH-082).

set -u

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$_HERE/../lib/safe-path.sh" 2>/dev/null || true

# Resolve jq from a fixed list of system directories (lib/safe-path.sh;
# never PATH). Fail open if absent.
JQ="$(safe_which jq 2>/dev/null || true)"
[ -z "$JQ" ] && exit 0   # can't parse → don't block anything

input="$(cat)"
cmd="$(printf '%s' "$input" | "$JQ" -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$cmd" ] && exit 0

# Fast path: almost every command has no pkill in it at all. The shell rebuilds p''kill,
# p\kill and "p"kill into pkill, so the test runs with quotes and backslashes removed.
# A backslash-newline pair is deleted by the shell before it reads words, so it is removed
# from a copy first (cmdn). The fast path and the `<<` test read the copy.
BSNL=$'\\\n'
cmdn=${cmd//"$BSNL"/}
t=${cmdn//\'/}; t=${t//\"/}; t=${t//\\/}
case "$t" in
    *pkill*) ;;
    *) exit 0 ;;
esac

NL=$'\n'
TAB=$'\t'
REFUSED=""

deny() {
    "$JQ" -n --arg r "$1" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0
}

deny_refusal() { # <misplaced option>
    deny "BLOCKED (pkill option order): '$1' comes after the pattern in this pkill command. macOS pkill stops reading options at its first non-option argument, so '$1' and the word after it are read as more patterns. A short word such as a user name then matches far more processes than intended. Put every option before the pattern, for example: pkill -u <user> -f '<pattern>'."
}

# is_assign <word>: a leading NAME=value environment assignment.
is_assign() {
    case "$1" in *=*) ;; *) return 1 ;; esac
    _pk_name="${1%%=*}"
    case "$_pk_name" in
        ''|[0-9]*|*[!A-Za-z0-9_]*) return 1 ;;
    esac
    return 0
}

# takes_value <wrapper> <option>: the option consumes the next word as its value.
takes_value() {
    case "$1:$2" in
        sudo:-[ugCDhprtU]|env:-[uCPS]|exec:-a) return 0 ;;
    esac
    return 1
}

# is_signal <option>: a signal option, which is an ordinary option (never a value taker).
is_signal() {
    case "$1" in
        -[0-9]|-[0-9][0-9]|-SIG*) return 0 ;;
        -HUP|-INT|-QUIT|-ILL|-TRAP|-ABRT|-FPE|-KILL|-BUS|-SEGV|-SYS|-PIPE|-ALRM|-TERM) return 0 ;;
        -URG|-STOP|-TSTP|-CONT|-CHLD|-TTIN|-TTOU|-IO|-XCPU|-XFSZ|-VTALRM|-PROF|-WINCH|-INFO|-USR1|-USR2) return 0 ;;
    esac
    return 1
}

# Tokenizer state is shared with the _pk_* helpers below through bash's dynamic
# scope: check_chunk declares word, inw, cur, nseg, nt, skip and depth as locals.
#   word/inw  the word being built, and whether one is in progress (an empty
#             quoted word is still a word)
#   cur       the segment the next word joins
#   skip      the next word is a redirect target and is dropped

_pk_emit() { # close the word in progress into the current segment
    if [ "$inw" -eq 1 ]; then
        if [ "$skip" -eq 1 ]; then
            skip=0
        else
            _PK_TSEG[nt]=$cur
            _PK_TVAL[nt]="$word"
            nt=$((nt+1))
        fi
    fi
    word=""; inw=0
}

_pk_sep() { # a command boundary: a new segment starts here
    _pk_emit
    skip=0
    nseg=$((nseg+1)); cur=$nseg
}

_pk_push() { # <kind>: open a frame, saving the enclosing segment, word and redirect skip
    depth=$((depth+1))
    _PK_K[depth]=$1
    _PK_SEG[depth]=$cur
    _PK_WORD[depth]="$word"
    _PK_INW[depth]=$inw
    _PK_SKIP[depth]=$skip
}

_pk_open() { # <p|b|q>: a $(...) or backtick (p, b) or a subshell (q) opens a new segment
    _pk_push "$1"
    word=""; inw=0
    # A pending redirect target belongs to the enclosing command, not to this segment.
    skip=0
    nseg=$((nseg+1)); cur=$nseg
}

_pk_pop() { # close the innermost frame and return to the enclosing segment
    case "${_PK_K[depth]}" in
        p|b)
            # A substitution is one word in its enclosing command, so the word resumes
            # with a placeholder where the substitution stood. After a redirect operator
            # that placeholder is the redirect target, so the saved skip is restored.
            cur=${_PK_SEG[depth]}; word="${_PK_WORD[depth]}X"; inw=1; skip=${_PK_SKIP[depth]} ;;
        *)
            # A subshell or <(...) / >(...) is not a redirect target, so skip stays clear.
            cur=${_PK_SEG[depth]}; word="${_PK_WORD[depth]}"; inw=${_PK_INW[depth]}; skip=0 ;;
    esac
    depth=$((depth-1))
}

# check_chunk <text>: tokenize one chunk into segments, then check every pkill segment.
# Returns 1 with REFUSED set to the misplaced option on a refusal.
check_chunk() {
    local s="$1" len i=0 c nx top word="" inw=0 cur=0 nseg=0 depth=0 nt=0 skip=0
    local sid t n k w o a j seen endopt skipval body blen q wrapper base A
    _PK_K=(n); _PK_SEG=(0); _PK_WORD=(""); _PK_INW=(0); _PK_SKIP=(0)
    _PK_TSEG=(); _PK_TVAL=()
    len=${#s}

    while [ "$i" -lt "$len" ]; do
        c="${s:$i:1}"
        nx="${s:$((i+1)):1}"
        top=${_PK_K[depth]}
        case "$top" in
            s)  # single quotes: literal until the closing quote
                if [ "$c" = "'" ]; then depth=$((depth-1)); else word="$word$c"; fi
                ;;
            d)  # double quotes: literal, except $( and backticks, which still run
                case "$c" in
                    '"') depth=$((depth-1)) ;;
                    '\')
                        case "$nx" in
                            "$NL") i=$((i+1)) ;;
                            '$'|'`'|'"'|'\') word="$word$nx"; i=$((i+1)) ;;
                            *) word="$word$c" ;;
                        esac ;;
                    '$')
                        if [ "$nx" = "(" ]; then _pk_open p; i=$((i+1)); else word="$word$c"; fi ;;
                    '`') _pk_open b ;;
                    *) word="$word$c" ;;
                esac
                ;;
            *)  # n (top level), p ($(...)), q (subshell), b (backticks)
                case "$c" in
                    "'") inw=1; _pk_push s ;;
                    '"') inw=1; _pk_push d ;;
                    '\')
                        if [ "$nx" = "$NL" ]; then i=$((i+1))
                        elif [ -n "$nx" ]; then word="$word$nx"; inw=1; i=$((i+1))
                        fi ;;
                    '$')
                        if [ "$nx" = "(" ]; then _pk_open p; i=$((i+1))
                        else word="$word$c"; inw=1; fi ;;
                    '`')
                        if [ "$top" = b ]; then _pk_emit; _pk_pop
                        else _pk_open b; fi ;;
                    '(') _pk_emit; _pk_open q ;;
                    ')')
                        case "$top" in
                            p|q) _pk_emit; _pk_pop ;;
                            b) word="$word$c"; inw=1 ;;
                            *) _pk_sep ;;
                        esac ;;
                    ';'|'|') _pk_sep ;;
                    '&')
                        if [ "$nx" = ">" ]; then
                            # &> is a redirect, so it does not separate commands
                            _pk_redirect_op
                        else _pk_sep; fi ;;
                    '<'|'>') _pk_redirect_op ;;
                    ' '|"$TAB") _pk_emit ;;
                    "$NL") _pk_sep ;;
                    '#')
                        if [ "$inw" -eq 0 ]; then
                            # a comment runs to the newline, which the loop then reads as a boundary
                            while [ "$i" -lt "$len" ] && [ "${s:$i:1}" != "$NL" ]; do i=$((i+1)); done
                            continue
                        fi
                        word="$word$c"; inw=1 ;;
                    *) word="$word$c"; inw=1 ;;
                esac
                ;;
        esac
        i=$((i+1))
    done
    _pk_emit

    sid=0
    while [ "$sid" -le "$nseg" ]; do
        A=(); n=0; t=0
        while [ "$t" -lt "$nt" ]; do
            if [ "${_PK_TSEG[t]}" -eq "$sid" ]; then A[n]="${_PK_TVAL[t]}"; n=$((n+1)); fi
            t=$((t+1))
        done
        sid=$((sid+1))
        [ "$n" -eq 0 ] && continue

        # Skip the wrappers and assignments that may precede the command word.
        k=0
        while [ "$k" -lt "$n" ]; do
            w="${A[k]}"
            if is_assign "$w"; then k=$((k+1)); continue; fi
            case "$w" in
                sudo|env|exec|nohup|time|command|'{'|'!'|if|then|else|elif|do|while|until)
                    wrapper="$w"; k=$((k+1))
                    while [ "$k" -lt "$n" ]; do
                        o="${A[k]}"
                        case "$o" in
                            --) k=$((k+1)); break ;;
                            -*) if takes_value "$wrapper" "$o"; then k=$((k+2)); else k=$((k+1)); fi ;;
                            *) if is_assign "$o"; then k=$((k+1)); else break; fi ;;
                        esac
                    done
                    continue ;;
            esac
            break
        done
        [ "$k" -lt "$n" ] || continue
        base="${A[k]##*/}"
        [ "$base" = pkill ] || continue

        # The pkill arguments, read the way its getopt reads them.
        seen=0; endopt=0; skipval=0; j=$((k+1))
        while [ "$j" -lt "$n" ]; do
            a="${A[j]}"; j=$((j+1))
            if [ "$skipval" -eq 1 ]; then skipval=0; continue; fi
            # After the first pattern, getopt has stopped reading options, so a dash-led word
            # is a misplaced option, `--` included. This check runs before `--` is read.
            if [ "$seen" -eq 1 ]; then
                case "$a" in -*) REFUSED="$a"; return 1 ;; esac
                continue
            fi
            if [ "$endopt" -eq 1 ]; then seen=1; continue; fi
            case "$a" in
                --) endopt=1; continue ;;
                -|[!-]*|'') seen=1; continue ;;
            esac
            is_signal "$a" && continue
            # Walk the cluster. A value option that ends the word takes the next word;
            # one with characters after it takes those characters as its value.
            body="${a#-}"; blen=${#body}; q=0
            while [ "$q" -lt "$blen" ]; do
                case "${body:$q:1}" in
                    [uUgGtPF])
                        if [ $((q+1)) -eq "$blen" ]; then skipval=1; fi
                        break ;;
                esac
                q=$((q+1))
            done
        done
    done
    return 0
}

# _pk_redirect_op: a redirect operator at the current position. Its target is dropped.
# A fd number before the operator (the 2 in 2>&1) is dropped with it.
_pk_redirect_op() {
    case "$word" in
        ''|*[!0-9]*) _pk_emit ;;
        *) word=""; inw=0 ;;
    esac
    skip=1
    # consume the rest of the operator (>&, &>, >|, <&); the loop advances i
    while [ "$i" -lt "$((len-1))" ]; do
        case "${s:$((i+1)):1}" in
            '&'|'>'|'|'|'<') i=$((i+1)) ;;
            *) break ;;
        esac
    done
}

case "$cmdn" in
    *'<<'*)
        # Heredoc and herestring bodies are not parsed, so the whole command is refused.
        deny "BLOCKED (pkill in a command that contains <<): this command contains the text pkill and the two characters <<. The guard does not parse heredoc bodies or herestrings, so it refuses the whole command. That includes an arithmetic shift such as \$((1<<2)), and a commit message or PR body passed through a heredoc. Pick the route that fits. If the command runs pkill, run that pkill as its own Bash command, with every option before the pattern, for example: pkill -u <user> -f '<pattern>'. If pkill is only text (a commit message, a PR body, a note), write the text to a file with your file-writing tool and pass the file: git commit -F <file>, or gh pr create --body-file <file>."
        ;;
    *)
        check_chunk "$cmd" || deny_refusal "$REFUSED"
        ;;
esac

exit 0
