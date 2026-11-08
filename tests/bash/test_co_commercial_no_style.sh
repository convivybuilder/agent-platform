#!/bin/bash
# A `*-commercial` workspace whose base product carries no `co_accent`
# (issue_01M625CZQ0ZCC9T6SQC7GT6XB5).
#
# THE BUG. The generic `*-commercial` branch (bin/co's `$(basename
# "$repo_root") == *-commercial` arm) calls `_co_apply_product_style
# "$commercial_product"` with no guard. That helper returns 1
# (`[[ -n $style ]] || return 1`) when `projects.co_style()` finds no
# `co_accent` for the base product — which is exactly the operator's own
# registry today for every real `*-commercial` directory's base product.
# `co` runs under `set -eu` (no `set +e` anywhere in between), so the bare,
# unguarded call ends the whole launch: `claude` is never exec'd. The
# branch's OTHER caller of the same helper (the generic daytime fallback,
# near the end of this file) already guards it with `if ! _co_apply_product_style
# ...; then ...; fi`.
#
# A SYNTHETIC REGISTRY, not the operator's own checked-in config/projects.json:
# `acme` names a product with no `co_accent` of its own, the same shape the
# operator's real commercial base products carry, without borrowing one of
# their real names. `acme-styled` is the control: a product that DOES carry
# a `co_accent`, proving the fix does not touch the already-working styled
# path.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

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

setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"

    mkdir -p "$home/Convivy/acme-commercial/.claude/agents"
    : > "$home/Convivy/acme-commercial/.claude/agents/product-manager.md"
    ( cd "$home/Convivy/acme-commercial" && git init -q . )

    mkdir -p "$home/Convivy/acme-styled-commercial/.claude/agents"
    : > "$home/Convivy/acme-styled-commercial/.claude/agents/product-manager.md"
    ( cd "$home/Convivy/acme-styled-commercial" && git init -q . )

    cat > "$home/registry.json" <<'JSON'
{
  "acme": {
    "class": "product", "display": "Acme", "owner": "convivy",
    "home_dir": "<root>/acme", "dirs": [], "crew": "acme",
    "lifecycle": "active", "products": ["acme"], "repos": ["acme"],
    "aliases": [], "kb": "local"
  },
  "acme-styled": {
    "class": "product", "display": "Acme Styled", "owner": "convivy",
    "home_dir": "<root>/acme-styled", "dirs": [], "crew": "acme-styled",
    "lifecycle": "active", "products": ["acme-styled"], "repos": ["acme-styled"],
    "aliases": [], "kb": "local",
    "co_accent": "#112233", "co_fg": "#445566"
  }
}
JSON

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "${CLAUDE_CALL_LOG:-/dev/null}"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, CLAUDE_ARGS, OUT, ERR
run_co() {
    local home=$1 rel=$2; shift 2
    local call_log out_log err_log
    call_log=$(mktemp); out_log=$(mktemp); err_log=$(mktemp)
    # TMUX/TMUX_PANE forced empty (test_co_resolve_adopter_crew.sh's own
    # reasoning): a bash test must never touch a real tmux server, and this
    # makes `_apply_co_style` — on the control case that still calls it —
    # take its deterministic non-tmux OSC branch.
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      CLAUDE_CALL_LOG="$call_log" TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$home/registry.json")" \
      "$@" >"$out_log" 2>"$err_log" )
    RC=$?
    CLAUDE_ARGS=$(cat "$call_log"); OUT=$(cat "$out_log"); ERR=$(cat "$err_log")
    rm -f "$call_log" "$out_log" "$err_log"
}

H=$(setup)

echo "== a commercial workspace for a product with no co_accent still launches =="
run_co "$H" Convivy/acme-commercial
assert_rc 0 "$RC" "acme-commercial: co exits 0"
assert_contains "$CLAUDE_ARGS" "--agent" \
    "acme-commercial: claude was exec'd (launch reached past styling)"
assert_contains "$CLAUDE_ARGS" "product-manager" \
    "acme-commercial: claude was exec'd with the branch's default agent"
assert_not_contains "$OUT" "SetProfile=co-" \
    "acme-commercial: no OSC profile was set — it launched without a style"

echo "== the control: a commercial workspace for a styled product keeps its style =="
run_co "$H" Convivy/acme-styled-commercial
assert_rc 0 "$RC" "acme-styled-commercial: co exits 0"
assert_contains "$OUT" "SetProfile=co-acme-styled" \
    "acme-styled-commercial: the fix left the already-working styled path alone"

rm -rf "$H"
finish
