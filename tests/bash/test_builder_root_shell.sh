#!/bin/bash
# The shell side of builder_root.data_root(). Every launcher resolves its run
# records as ${BUILDER_ROOT:-$(<its real bin/>/builder-root)}, and run.sh and
# lib.sh set BUILDER_ROOT for every other test, so the fallback branch, and the
# one symlink hop it depends on, run nowhere else. These run it with BUILDER_ROOT
# unset and write nothing (review, PR 538: install-decision-hooks lacked the hop).
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

want=$(PYTHONPATH="$REPO_ROOT/lib/agent-platform" python3 -c 'import builder_root; print(builder_root.root())')

echo "== builder-root prints root() when BUILDER_ROOT is unset, and the variable when set =="
assert_eq "$want" "$(env -u BUILDER_ROOT "$REPO_ROOT/bin/builder-root")" "falls back to root()"
assert_eq "/tmp/a-data-root" "$(BUILDER_ROOT=/tmp/a-data-root "$REPO_ROOT/bin/builder-root")" \
    "BUILDER_ROOT is taken as given"

echo "== --root prints root(), ignores BUILDER_ROOT, and takes an override only from --config =="
# root() is a trust anchor (co's black-box matching answers from it), so the one
# way a test moves it is an explicit --config on the command line (steward,
# 2026-09-11). An inherited variable must not reach it.
assert_eq "$want" "$(BUILDER_ROOT=/tmp/a-data-root "$REPO_ROOT/bin/builder-root" --root)" \
    "--root ignores BUILDER_ROOT"
cfgdir=$(mktemp -d); mkdir -p "$cfgdir/crews"
printf 'CREWS_ROOT=%s\n' "$cfgdir/crews" > "$cfgdir/session-agent.env"
assert_eq "$cfgdir/crews" "$("$REPO_ROOT/bin/builder-root" --root --config "$cfgdir/session-agent.env")" \
    "--root --config names the root a test wants"
"$REPO_ROOT/bin/builder-root" --config "$cfgdir/session-agent.env" >/dev/null 2>&1; rc=$?
assert_rc 2 "$rc" "--config without --root is refused"

echo "== --root --config refuses a config that names no root, rather than printing the real one =="
# root() answers each of these with the machine's real root instead of raising,
# so without the refusal a test with a broken config would read and write the
# real tree (agent-platform#545). Nothing may be printed: a caller that ignored
# the exit code would otherwise still get a path.
printf 'CREWS_ROOT=\n' > "$cfgdir/empty.env"
printf 'CREWS_ROOT=relative/crews\n' > "$cfgdir/relative.env"
printf 'CREWS_ROOT=/\n' > "$cfgdir/slash.env"
printf 'SOMETHING_ELSE=1\n' > "$cfgdir/no-key.env"
for c in "$cfgdir/no-such.env" "$cfgdir/empty.env" "$cfgdir/relative.env" \
         "$cfgdir/slash.env" "$cfgdir/no-key.env"; do
    out=$("$REPO_ROOT/bin/builder-root" --root --config "$c" 2>/dev/null); rc=$?
    assert_rc 2 "$rc" "$(basename "$c"): refused"
    assert_eq "" "$out" "$(basename "$c"): prints no path"
done
rm -rf "$cfgdir"

farm=$(mktemp -d)

echo "== a launcher reached through a ~/.local/bin-style link finds its real bin/ =="
# mark-fresh resolves the data root first, then refuses an unknown product with
# exit 2 before writing anything. Reaching that refusal means the hop found
# bin/builder-root; a missed hop exits 1 at the resolution line instead.
ln -s "$REPO_ROOT/bin/mark-fresh" "$farm/mark-fresh"
out=$(env -u BUILDER_ROOT /bin/bash "$farm/mark-fresh" notaproduct bug-fix 2>&1); rc=$?
assert_rc 2 "$rc" "mark-fresh resolved its data root through the link"
assert_contains "$out" "invalid product" "and reached its own refusal"

echo "== install-decision-hooks' /bin/sh hop, run from a link =="
# Its real resolution lines, run under sh with \$0 set to a link, since running the
# script itself would install hooks into the machine's repos.
ln -s "$REPO_ROOT/bin/install-decision-hooks" "$farm/install-decision-hooks"
block=$(sed -n '/^_self_path="\$0"/,/^DATA_ROOT=/p' "$REPO_ROOT/bin/install-decision-hooks")
assert_eq 2 "$(printf '%s\n' "$block" | grep -c .)" "the two resolution lines are found"
got=$(env -u BUILDER_ROOT sh -c "$block
echo \"\$DATA_ROOT\"" "$farm/install-decision-hooks")
assert_eq "$want" "$got" "install-decision-hooks resolves the root through its link"

rm -rf "$farm"
finish
