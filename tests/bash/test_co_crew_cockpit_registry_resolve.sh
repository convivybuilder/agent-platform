#!/bin/bash
# `co --resolve` for a crew-cockpit crew APPENDED FROM THE REGISTRY
# (`crew_cockpit: true`, issue_01M3YN6857H5J541G73Y9P3KA1): bin/co's one
# crew-cockpit branch matches it by running `projects.cockpit_gate(slug)`
# against the repo root and basename, then styles it with
# `projects.co_style(slug)` — no dedicated branch or hardcoded crew name of
# its own.
#
# A matched crew with no configured `co_accent` falls back to the directory
# basename for both style_id and label, and `--resolve` reports that
# fallback as `crew=<basename> label=<basename>` rather than "no crew
# resolves" — the answer `agentctl crew doctor <crew>` needs to read such a
# crew as present rather than MISSING. `--resolve` answers before the
# branch's knowledge pull, name-set or styling run (`_co_cockpit_refresh`,
# `_co_refresh_if_stale`, `_co_set_name`, `_apply_co_style`), which the
# second case below checks directly by asserting no call reaches
# `knowledge-ctx` at all when the workspace is stale.
#
# `co` is zsh (excluded from the ARCH-082 bash-3.2 contract by lint.sh's
# shebang filter); bash invoking it here is exactly how a login shell will.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

CO="$REPO_ROOT/bin/co"

# A fake HOME with one registry-appended crew-cockpit crew (synthetic name,
# matched only through the registry's `crew_cockpit: true` row, not any
# name bin/co's crew-cockpit branch special-cases), plus a second synthetic
# crew with its own configured style (`styled-crew`) as the control that
# must resolve to its registry row's `co_style_id`/`co_label` rather than
# the basename fallback.
setup() { # -> echoes fake HOME
    local home
    home=$(mktemp -d)
    mkdir -p "$home/stubbin" "$home/Convivy/example-crew" "$home/Convivy/styled-crew"
    ( cd "$home/Convivy/example-crew" && git init -q . )
    ( cd "$home/Convivy/styled-crew" && git init -q . )

    # A registry naming exactly one synthetic, active, `crew_cockpit: true`
    # crew — the seam bin/co's crew-cockpit block already reads
    # (`PROJECTS_REGISTRY`, honoured via `co --root-config`), so this never
    # touches the operator's own checked-in config/projects.json. The
    # second crew carries the style fields `co_style` reads (`co_accent`,
    # `co_style_id`, `co_label`, `co_fg`), shaped like a real styled row in
    # config/projects.json, with a style_id and label that differ from its
    # own basename so configured styling is distinguishable from the
    # basename fallback.
    cat > "$home/registry.json" <<'JSON'
{
  "example-crew": {
    "class": "project", "display": "Example Crew", "owner": "convivy",
    "home_dir": "<root>/example-crew", "dirs": [], "crew": "example-crew",
    "lifecycle": "active", "products": [], "repos": ["example-crew"],
    "aliases": [], "kb": "central", "crew_cockpit": true
  },
  "styled-crew": {
    "class": "project", "display": "Styled Crew", "owner": "convivy",
    "home_dir": "<root>/styled-crew", "dirs": [], "crew": "styled-crew",
    "lifecycle": "active", "products": [], "repos": ["styled-crew"],
    "aliases": [], "kb": "central", "crew_cockpit": true,
    "co_accent": "#c19a6b", "co_style_id": "styled", "co_label": "Styled Crew",
    "co_fg": "#33240f"
  }
}
JSON

    cat > "$home/stubbin/claude" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" > "${CLAUDE_CALL_LOG:-/dev/null}"
exit 0
EOF
    chmod +x "$home/stubbin/claude"

    # Records every call and answers `check` as STALE (exit 1) — the staleness
    # case below asserts `--resolve` never reaches this at all.
    cat > "$home/stubbin/knowledge-ctx" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${KX_CALL_LOG:-/dev/null}"
[ "$1" = check ] && exit 1
exit 0
EOF
    chmod +x "$home/stubbin/knowledge-ctx"
    # Belt and suspenders for the same case: if the refresh ran despite the
    # fix, `_co_pull_knowledge` calls `agentctl kb sync` next. Stubbed so a
    # regression fails this test's assertion rather than reaching the real
    # agentctl or network.
    write_kb_sync_stub "$home/stubbin"

    echo "$home"
}

# run_co <home> <dir-relative-to-home> [co-args...] -> RC, OUT, ERR, KX_ARGS
run_co() {
    local home=$1 rel=$2; shift 2
    local out_log err_log kx_log
    out_log=$(mktemp); err_log=$(mktemp); kx_log=$(mktemp)
    ( cd "$home/$rel" && HOME="$home" PATH="$home/stubbin:$PATH" \
      KX_CALL_LOG="$kx_log" TMUX="" TMUX_PANE="" \
      /bin/zsh "$CO" --root-config "$(co_root_config "$home" "$home/registry.json")" "$@" \
      >"$out_log" 2>"$err_log" )
    RC=$?
    OUT=$(cat "$out_log"); ERR=$(cat "$err_log"); KX_ARGS=$(cat "$kx_log")
    rm -f "$out_log" "$err_log" "$kx_log"
}

H=$(setup)

echo "== a registry-appended crew-cockpit crew resolves, not just launches =="
run_co "$H" Convivy/example-crew --resolve
assert_rc 0 "$RC" "example-crew: co --resolve exits 0"
assert_contains "$OUT" "crew=example-crew label=example-crew dir=" \
    "example-crew: co --resolve reports its basename as crew and label"

echo "== --resolve in a stale crew-cockpit workspace pulls no knowledge =="
# `.claude/.active-profile` present is what makes `_co_refresh_if_stale` look
# at `knowledge-ctx check` at all; the stub above answers it STALE (exit 1),
# which is exactly the condition that used to trigger a pull-and-reindividuate.
mkdir -p "$H/Convivy/example-crew/.claude"
echo daytime > "$H/Convivy/example-crew/.claude/.active-profile"
run_co "$H" Convivy/example-crew --resolve
assert_rc 0 "$RC" "example-crew (stale): co --resolve still exits 0"
assert_contains "$OUT" "crew=example-crew label=example-crew dir=" \
    "example-crew (stale): co --resolve still names the crew"
assert_eq "" "$KX_ARGS" \
    "example-crew (stale): co --resolve calls knowledge-ctx not at all"

echo "== a crew with configured styling resolves to it, not the basename fallback =="
run_co "$H" Convivy/styled-crew --resolve
assert_rc 0 "$RC" "styled-crew: co --resolve exits 0"
assert_contains "$OUT" "crew=styled label=Styled Crew dir=" \
    "styled-crew: co --resolve reports its configured style_id and label"

rm -rf "$H"
finish
