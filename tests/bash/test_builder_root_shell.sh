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

echo "== --root with no --home behaves exactly as it did before --home existed (round-1 revise, PR 1119) =="
# Pinning test: adding --home must not change the no-option, --config-only, or
# BUILDER_ROOT-still-ignored behaviour above for a caller that never passes it.
assert_eq "$want" "$("$REPO_ROOT/bin/builder-root" --root)" "--root alone is unchanged"
assert_eq "$cfgdir/crews" "$("$REPO_ROOT/bin/builder-root" --root --config "$cfgdir/session-agent.env")" \
    "--root --config alone is unchanged"

echo "== --root --home resolves against a DIFFERENT account's home than the calling process's own =="
# install-individuator's SUDO_USER fix (round-1 revise, PR 1119): --config alone
# moves which file is read, but '~' expansion, the default root and the "too
# wide" guard still resolved against the CALLING process's own passwd home.
# --home carries all three against a given account's home instead.
homedir=$(mktemp -d)/someone
mkdir -p "$homedir/.config/convivy" "$homedir/builder"
assert_eq "$homedir/builder" "$("$REPO_ROOT/bin/builder-root" --root --home "$homedir")" \
    "--home alone: no CREWS_ROOT configured for that account falls back to ITS OWN default root"
printf 'CREWS_ROOT=~/builder\n' > "$homedir/.config/convivy/session-agent.env"
assert_eq "$homedir/builder" "$("$REPO_ROOT/bin/builder-root" --root --home "$homedir")" \
    "--home alone: ~ in that account's own config expands against ITS OWN home"
printf 'CREWS_ROOT=~\n' > "$homedir/.config/convivy/session-agent.env"
assert_eq "$homedir/builder" "$("$REPO_ROOT/bin/builder-root" --root --home "$homedir")" \
    "--home alone: CREWS_ROOT=~ is too wide against THAT account's home and falls back"
"$REPO_ROOT/bin/builder-root" --home "$homedir" >/dev/null 2>&1; rc=$?
assert_rc 2 "$rc" "--home without --root is refused"
rm -rf "$(dirname "$homedir")"

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
