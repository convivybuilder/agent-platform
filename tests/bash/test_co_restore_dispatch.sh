#!/bin/bash
# bin/co's `restore` hand-off (CQ·392). The logic is tested in
# tests/test_co_restore.py; this checks the zsh side: that `co restore` reaches
# lib/agent-platform/co_restore.py with its arguments, and that it does so before
# any launch branch, so no `claude` runs and no session-agent ensure-server runs
# during a dry run.
#
# A dry run reads the pane list through tmux and starts nothing. The private socket
# named here has no server, and tmux is a stub on PATH, so the module reads "no
# server" and nothing here can reach the operator's server. TMUX is removed and
# TMUX_TMPDIR points at a temp directory as well.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

home=$(mktemp -d)
stubbin="$home/stubbin"
mkdir -p "$stubbin" "$home/Convivy/vivy" "$home/projects" "$home/tmux"
claude_log="$home/claude.log"
ensure_log="$home/ensure.log"

# Stubs that record a call. Neither is expected to run in a dry run.
printf '#!/bin/sh\necho claude >> "%s"\n' "$claude_log" > "$stubbin/claude"
printf '#!/bin/sh\necho ensure >> "%s"\n' "$ensure_log" > "$stubbin/session-agent"
chmod +x "$stubbin/claude" "$stubbin/session-agent"

# A stub tmux, because co_restore.py runs `tmux list-panes` even for a dry run. A
# crew host always has tmux, but this test cannot count on one being on PATH: the
# bash job's runner did not have it (CI run 37388588307 failed with exit 1 and no
# output, the uncaught FileNotFoundError for 'tmux'). The stub answers as tmux does
# for a socket with no server, so the module reads no live panes.
printf '#!/bin/sh\necho "no server running on $*" >&2\nexit 1\n' > "$stubbin/tmux"
chmod +x "$stubbin/tmux"

# One interactive transcript under the vivy directory, written now. Its project
# slug is the cwd with every non-alphanumeric turned into `-`, as Claude Code does.
session_id="0a1b2c3d-0000-4000-8000-000000000001"
vivy="$home/Convivy/vivy"
slug=$(printf '%s' "$vivy" | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$home/projects/$slug"
printf '{"type":"user","entrypoint":"cli","cwd":"%s","message":"private text"}\n' "$vivy" \
    > "$home/projects/$slug/$session_id.jsonl"

out="$home/out.txt"
err="$home/err.txt"
( cd "$home" && env -u TMUX -u TMUX_PANE \
    HOME="$home" BUILDER_ROOT="$home/data" PATH="$stubbin:$PATH" TMUX_TMPDIR="$home/tmux" \
    /bin/zsh "$CO" --root-config "$(co_root_config "$home")" \
    restore --dry-run --since 1d --projects-dir "$home/projects" \
    --tmux-socket "cr-dispatch-$$" >"$out" 2>"$err" )
rc=$?

# A non-zero exit leaves stderr as the only record of its cause, so show it in the log.
[ "$rc" -eq 0 ] || { echo "    co restore --dry-run stderr:"; sed 's/^/      /' "$err"; }
assert_rc 0 "$rc" "co restore --dry-run exits 0"
# No co-* record exists under BUILDER_ROOT, so the transcript is planned and skipped
# as unnamed. That line is the proof the module ran and read the transcript.
assert_contains "$(cat "$out")" "session $session_id: no tmux session name on record" \
    "the restore plan reached co_restore.py and read the interactive transcript"
assert_contains "$(cat "$out")" "dry run: nothing started" "the dry run says it started nothing"
if [ -e "$claude_log" ]; then
    assert_eq "no claude" "claude ran" "a restore never reaches a launch branch"
else
    assert_eq "no claude" "no claude" "a restore never reaches a launch branch"
fi
if [ -e "$ensure_log" ]; then
    assert_eq "no ensure-server" "ensure-server ran" "a dry run never starts the server"
else
    assert_eq "no ensure-server" "no ensure-server" "a dry run never starts the server"
fi

# A window is required, and the module's refusal is what the operator sees.
( cd "$home" && env -u TMUX -u TMUX_PANE HOME="$home" PATH="$stubbin:$PATH" \
    TMUX_TMPDIR="$home/tmux" /bin/zsh "$CO" --root-config "$(co_root_config "$home")" \
    restore --projects-dir "$home/projects" >"$out" 2>"$err" )
assert_rc 2 "$?" "co restore without --since or --from is refused by its own parser"
assert_contains "$(cat "$err")" "--since" "the refusal names the window options"

rm -rf "$home"
finish
