#!/bin/bash
# `co -r` and `co --resolve` resolve a crew by the same rule plain `co` uses,
# including a Builder adopter's own crew (issue_01M3NW7WSXXCYW57HWSGQ76VYH).
#
# THE BUG, measured by Shipwright on a clean install (crew `tidytags`,
# individuated from platform/default, no config/projects.json row at all):
# `co -r`'s product-name gate accepted only six hardcoded repo basenames and
# refused every adopter's crew outright, even one plain `co` launches with no
# complaint; `co --resolve`'s answer lives entirely in `_apply_co_style`,
# which the generic product tail never called for anything outside that same
# six-name list, so it read a launchable adopter crew as "no crew resolves".
#
# THE FIX asks `projects.project_for_repo` / `projects.resolve_project`
# directly — the same admission `agentctl crew doctor`'s `check_registry` was
# fixed to make (company decision 0011) — rather than a second hardcoded
# list. `project_for_repo` is checked FIRST and its own regression is pinned
# below: a repo config/projects.json already claims for a registered project
# (`convivy-services`, crew `svc`) must stay refused here exactly as before,
# because `co` deliberately has no generic reindividuation branch for it —
# `_co_reindividuate --force` would rmtree its git-tracked hand-written agents
# (bin/co's own comment beside `_co_profiled_dirs`). A first version of this
# fix used `resolve_project` alone and reindividuated straight over that
# directory.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.
#
# ALSO COVERS the security-reviewer REQUEST_CHANGES finding on this file's own
# `*)` styling arm (PR #709, https://github.com/convivy/agent-platform/pull/709
# #issuecomment-5885939909): that arm passed $style_product — a raw,
# unvalidated git-toplevel basename on the plain-launch path, since
# `_co_is_an_adopter_crew` only runs under `-r` — straight into
# `_apply_co_style`, which writes it unescaped into an OSC 1337 `SetProfile`
# terminal sequence (`profile_name="co-$style_id"`, bin/co ~507, sunk at
# ~590/~612). A basename holding a raw control byte (BEL, ESC — the
# filesystem permits any byte but `/` and NUL) closes that sequence early and
# lets the rest of the name reach the terminal as its own escape sequence.
# THE FIX gates the `*)` arm on the same charset `projects._NAME_OK` already
# enforces before calling `_apply_co_style` at all, so an unsafe name never
# reaches ANY of that function's sinks (profile_name, label, title,
# status-left, rename-window, and the `--resolve` `print -r` line alike) —
# see below for the case the previous commit's own fixture (`nope`, no
# control bytes) could not have caught.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A raw BEL (0x07) inside a directory basename — ANSI-C quoting so the byte is
# literal, not the two-character notation `\a`.
EVIL_NAME=$'evil\ainjected'

assert_not_contains() { # <haystack> <needle> <msg>
    case "$1" in
        *"$2"*)
            _FAIL=$((_FAIL + 1))
            echo "  FAIL: $3"
            echo "        found: [$2]"
            echo "        in:    [$1]"
            ;;
        *) _PASS=$((_PASS + 1)) ;;
    esac
}

# A fake HOME with a real, git-trackable ~/Convivy/knowledge (`co -r` pulls it
# for real via the stubbed `agentctl kb sync`; a fake checkout with no real
# remote would abort the pull before the gate under test ever ran) carrying
# `projects/tidytags/` — the one thing that makes `tidytags` an adopter's
# crew with no registry row at all.
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    write_kb_sync_stub "$home/stubbin"

    git init -q --bare "$home/kb-remote.git"
    git init -q "$home/Convivy/knowledge"
    mkdir -p "$home/Convivy/knowledge/projects/tidytags/profiles"
    : > "$home/Convivy/knowledge/projects/tidytags/profiles/daytime.yaml"
    (
        cd "$home/Convivy/knowledge" || exit 1
        git config user.email t@example.com
        git config user.name Test
        git add -A
        git commit -qm seed
        git branch -M main
        git remote add origin "$home/kb-remote.git"
        git push -q -u origin main
    ) >/dev/null 2>&1

    # tidytags: an adopter's crew, already individuated (agent files present,
    # the same state Shipwright measured it in), resolved only through
    # projects/tidytags/ above — no config/projects.json row anywhere.
    mkdir -p "$home/Convivy/tidytags/.claude/agents"
    : > "$home/Convivy/tidytags/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/tidytags" && git init -q . )

    # The control: nothing individuated, no registry row, no projects/nope/ in
    # the knowledge tree either — a directory no crew resolves at all.
    mkdir -p "$home/Convivy/nope"
    ( cd "$home/Convivy/nope" && git init -q . )

    # The regression this fix must not open: a repo THIS checkout's own
    # config/projects.json already claims for a registered project (`svc`),
    # with no branch of its own in bin/co and no adopter fallback either —
    # `co -r` must keep refusing it rather than reindividuating over its
    # hand-written agents. Never individuated here on purpose: the real
    # convivy-services carries git-tracked svc-* agents from the start.
    mkdir -p "$home/Convivy/convivy-services"
    ( cd "$home/Convivy/convivy-services" && git init -q . )

    # THE INJECTION FIXTURE: a directory basename carrying a raw BEL byte
    # (0x07 — the filesystem permits any byte but `/` and NUL; a real
    # instance of this is `git worktree add $'\e]1337;File=...\a'` or an
    # agent's own `os.mkdir` run by shell-having code). Already
    # individuated, exactly like tidytags, so it reaches the `*)` styling
    # arm on a BARE launch with no `-r`/`--resolve` at all — the only path
    # `_co_is_an_adopter_crew`'s validation never runs.
    mkdir -p "$home/Convivy/$EVIL_NAME/.claude/agents"
    : > "$home/Convivy/$EVIL_NAME/.claude/agents/planning-orchestrator.md"
    ( cd "$home/Convivy/$EVIL_NAME" && git init -q . )

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "$CLAUDE_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/claude"
    # Records that it was asked to re-individuate, which is the half of the
    # refresh a launch alone would not prove.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$KX_CALL_LOG"
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"

    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, CLAUDE_ARGS, KX_ARGS, ERR, OUT
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log kx_log err_log out_log
    call_log=$(mktemp); kx_log=$(mktemp); err_log=$(mktemp); out_log=$(mktemp)
    # TMUX/TMUX_PANE FORCED EMPTY, not merely left unset: this test runner's
    # own process may itself be inside a real tmux pane (every co session is),
    # and an inherited $TMUX_PANE would point `_apply_co_style`'s tmux calls
    # at that REAL pane — a bash test must never touch a real tmux server
    # (never-xtrace-co / a-dispatched-run-inherits-my-panes-tmux). Forcing
    # TMUX="" makes `_apply_co_style` take its non-tmux branch deterministically,
    # which is also the branch the OSC-injection assertions below exercise.
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" KX_CALL_LOG="$kx_log" TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home")" "$@" \
      >"$out_log" 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); KX_ARGS=$(cat "$kx_log")
    ERR=$(cat "$err_log"); OUT=$(cat "$out_log")
    rm -f "$call_log" "$kx_log" "$err_log" "$out_log"
}

H=$(setup)

echo "== co --resolve names an adopter's already-individuated crew =="
run_co "$H" Convivy/tidytags --resolve
assert_rc 0 "$RC" "tidytags: co --resolve exits 0"
assert_contains "$OUT" "crew=tidytags" "tidytags: co --resolve names the crew"

echo "== co --resolve still refuses a directory no crew resolves =="
# Nothing individuated in `nope`, so this reaches the pre-existing "is not
# individuated" refusal rather than `_co_exec_claude`'s "no crew resolves" —
# both are a refusal with no `crew=` line, which is the only thing
# `agentctl crew doctor`'s `check_co_resolves` reads.
run_co "$H" Convivy/nope --resolve
assert_rc 1 "$RC" "nope: co --resolve exits 1"
assert_not_contains "$OUT" "crew=" "nope: co --resolve prints no crew= line"

echo "== co -r proceeds past validation for the same adopter's crew =="
run_co "$H" Convivy/tidytags -r
assert_rc 0 "$RC" "tidytags: co -r exits 0 rather than rejecting the cwd"
assert_not_contains "$ERR" "not a recognized product repo" \
    "tidytags: co -r no longer rejects an adopter's crew"
assert_contains "$KX_ARGS" "use daytime --force" \
    "tidytags: co -r re-individuates via knowledge-ctx use"
assert_contains "$CLAUDE_ARGS" "--model" "tidytags: co -r reaches the launch"

echo "== co -r still refuses a directory no crew resolves =="
run_co "$H" Convivy/nope -r
assert_rc 1 "$RC" "nope: co -r exits 1"
assert_contains "$ERR" "not a recognized product repo" "nope: co -r says why"
assert_not_contains "$KX_ARGS" "use" "nope: co -r never reaches knowledge-ctx use"

echo "== a repo the registry already claims for another project stays refused =="
# THE REGRESSION THIS FIX MUST NOT OPEN. `convivy-services` is not individuated
# and has no branch of its own in bin/co; a check keyed on resolve_project
# alone would admit it (it IS in projects.py's EXPLICIT_PROJECT_BY_NAME) and
# `--force`-reindividuate over a directory whose real counterpart carries
# hand-written, git-tracked agents. project_for_repo must catch it first.
run_co "$H" Convivy/convivy-services -r
assert_rc 1 "$RC" "convivy-services: co -r exits 1 rather than reindividuating it"
assert_contains "$ERR" "not a recognized product repo" \
    "convivy-services: co -r says why"
assert_not_contains "$KX_ARGS" "use" \
    "convivy-services: co -r never reaches knowledge-ctx use"

echo "== bare co (no -r, no --resolve) still styles a valid adopter name =="
# THE PASSING CASE: gating the `*)` arm must not cost a valid name its style.
# tidytags is already a fixture above; this is the first assertion in this
# file that exercises a BARE launch (no flag reroutes it before the styling
# case, unlike -r/--resolve), so it is also the first to see this arm's OSC
# output at all.
run_co "$H" Convivy/tidytags
assert_rc 0 "$RC" "tidytags: bare co launches"
assert_contains "$OUT" "SetProfile=co-tidytags" "tidytags: bare co still styles a valid name"

echo "== bare co never lets an unvalidated basename reach the OSC 1337 sink =="
# THE FIX UNDER TEST. Before it, this arm called
# _apply_co_style "$style_product" ... with $style_product raw off
# `basename "$repo_root"` — no `-r`, so `_co_is_an_adopter_crew` never ran —
# and `_apply_co_style` wrote it unescaped into `printf '\e]1337;SetProfile=%s\a'
# "$profile_name"` (profile_name="co-$style_id"). A basename carrying a raw
# BEL closed that sequence early and let the rest of the name reach the
# terminal as its own bytes. FAILS ON dcd86875: the unfixed `*)` arm calls
# _apply_co_style unconditionally, so $OUT there contains "evil".
run_co "$H" "Convivy/$EVIL_NAME"
assert_rc 0 "$RC" "evil name: bare co still launches (unstyled)"
assert_not_contains "$OUT" "evil" \
    "evil name: no trace of the unvalidated basename reaches stdout"
assert_not_contains "$OUT" "SetProfile=" \
    "evil name: the OSC 1337 sink is never reached for an invalid name"

echo "== --resolve does not claim a crew resolves for the same unvalidated name =="
# THE LESSER INSTANCE the security review also named: _apply_co_style's
# `co_resolve` early return does `print -r -- "crew=$style_id ..."` before any
# printf sink, so gating only the printf calls (rather than the call to
# _apply_co_style itself) would still leak the raw name here and still
# mis-report an invalid name as a resolved crew. Gating the call itself
# closes both at once.
run_co "$H" "Convivy/$EVIL_NAME" --resolve
assert_rc 1 "$RC" "evil name: co --resolve exits 1"
assert_not_contains "$OUT" "crew=" "evil name: co --resolve does not report it resolved"
assert_not_contains "$OUT" "evil" "evil name: co --resolve prints none of the raw basename"

rm -rf "$H"
finish
