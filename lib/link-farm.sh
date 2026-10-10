#!/usr/bin/env bash
# link-farm.sh — sync a per-file symlink farm under ~/.local/lib/agent-platform/
#                back into THIS checkout, one module at a time.
#
# Invoked as a subprocess by install.sh (phase 2) and called directly by agent-platform-autopull
# when a pull adds new lib modules. Keeps the farm-layout logic in one place:
# two independent implementations of the loop below would drift apart, which is
# how a new module stays unlinked until the next full install.
#
# USAGE
#   link-farm.sh <checkout_root> [<farm_dir>] [--allow-non-primary] [--prune]
#
# The checkout root is where the repo lives (<root>/agent-platform or
# whatever the caller resolves). The farm target defaults to
# $HOME/.local/lib/agent-platform/; a second arg overrides it (used in tests).
#
# --prune removes farm symlinks that point at a module no longer in the
# checkout (deleted lib/agent-platform/*.py). OFF BY DEFAULT — the link loop
# above only ever ADDS, and pruning is the one thing three callers must not
# all do. install.sh passes --prune (operator-run, deliberate). The autopull
# cron tick and farm_check.py's fix_new_modules do NOT pass it and must not:
# clearing a dead link is not part of the unattended job, because the
# unattended tick must not start DELETING from the machine's deployed
# library, where today it only ever adds (the operator, 2026-09-17 and 2026-09-21).
# See bin/agent-platform-autopull and lib/agent-platform/farm_check.py for
# the alert this leaves in place and why.
#
# REFUSES to build the farm from anything but the PRIMARY checkout unless
# --allow-non-primary is passed. The farm is the machine's deployed platform
# library — every launchd job imports from it — so a farm linked from a linked
# worktree makes unmerged branch code live everywhere. That is the 2026-07-30
# incident: install.sh had been run inside a bug-fixer's worktree, and 38 of 40
# farm links came to point at that still-open PR's checkout. install.sh derives
# its checkout root from wherever it was invoked, so this guard is what stops a
# dispatch run inside a worktree from re-pointing the deployed library at the
# branch it is testing. The primary checkout is where the git-dir IS the common
# git-dir (same definition the ARCH-396 primary guard uses); a non-git copy or
# anything unverifiable is refused too, because an unverifiable checkout must
# not repoint the deployed library.
#
# Idempotent. Safe to run when nothing is new: an already-correct link is
# skipped, a missing or wrong-target link is fixed, and a real file (not a
# symlink) is backed up with a .preinstall.<ts> suffix, matching install.sh's
# behaviour.
#
# Logs to stdout (one line per new/changed link). Terse on purpose — the caller
# wraps it in its own context (install.sh's "Phase 2" banner, autopull's log).
#
# Does NOT touch bin/, lib/knowledge/, gateway/, or launchd/ — only the
# agent-platform lib farm. Those are install.sh's full-phase responsibility.

set -euo pipefail

ALLOW_NON_PRIMARY=0
PRUNE=0
POSITIONAL=()
for _arg in "$@"; do
  case "$_arg" in
    --allow-non-primary) ALLOW_NON_PRIMARY=1 ;;
    --prune) PRUNE=1 ;;
    --*) echo "link-farm.sh: unknown option: $_arg" >&2; exit 2 ;;
    *) POSITIONAL+=("$_arg") ;;
  esac
done

REPO="${POSITIONAL[0]:-}"
if [[ -z $REPO ]]; then
  echo "usage: link-farm.sh <checkout_root> [<farm_dir>] [--allow-non-primary] [--prune]" >&2
  exit 2
fi

# --allow-non-primary AND --prune TOGETHER ARE REFUSED, and the refusal is here
# rather than inside the prune block so it fires before anything is written.
# --allow-non-primary waives the primary-checkout guard below; --prune lets this
# script DELETE from the deployed library. Together they let a checkout that is
# not the primary one delete entries by a rule that keys only on the $REPO it
# was handed, with no notion of whether that checkout is the one the machine
# serves from. A worktree whose branch deleted a module the primary still ships
# would prune that module's link and leave the deployed library with NO link at
# all for it.
#
# That is strictly worse than what the same misuse could do before --prune
# existed, which is the reason this is refused rather than documented: the
# 2026-07-30 incident (install.sh run inside a bug-fixer's worktree, 38 of 40
# farm links repointed at a feature branch) left every link WRONG, and a wrong
# link still resolves to code. A missing one does not, and nothing puts it back
# until someone reinstalls from the primary checkout.
#
# Refused rather than made to re-check the guard, because the two flags state
# contradictory intents — "I am not the primary checkout" and "delete from the
# machine's deployed library" — and silently honouring one of them would pick
# for the caller.
if [[ $PRUNE -eq 1 && $ALLOW_NON_PRIMARY -eq 1 ]]; then
  echo "link-farm.sh: REFUSING --prune together with --allow-non-primary." >&2
  echo "  --allow-non-primary waives the primary-checkout guard; --prune deletes" >&2
  echo "  from the machine's deployed library. A non-primary checkout pruning that" >&2
  echo "  library can leave a module with no link at all, which is worse than the" >&2
  echo "  wrong link the guard already exists to prevent. Run --prune from the" >&2
  echo "  primary checkout, or link without it." >&2
  exit 2
fi
LOCAL_LIB="${POSITIONAL[1]:-$HOME/.local/lib}"
FARM="$LOCAL_LIB/agent-platform"

[[ -d $REPO/lib/agent-platform ]] || {
  echo "link-farm.sh: $REPO/lib/agent-platform/ not found — nothing to link" >&2
  exit 1
}

# True only when $REPO is the PRIMARY checkout of its repo: the git-dir IS the
# common git-dir, and $REPO is that checkout's toplevel. A linked worktree's
# git-dir is .git/worktrees/<name> while its common-dir points at the primary's
# .git — the same split the ARCH-396 primary guard keys on. --path-format=absolute
# keeps both paths independent of the caller's cwd (git would otherwise print the
# primary's git-dir relative to it), so the equality holds wherever this script is
# invoked. The caller's GIT_DIR / GIT_COMMON_DIR / GIT_WORK_TREE would override
# where git looks and what it reports, enough for all three checks to agree on a
# non-primary checkout, so the guard scrubs them and reads what git resolves for
# itself. Fails closed: a non-git copy, a subdirectory of a repo, a git older
# than --path-format (2.31), or any path git cannot verify returns 1, and the
# caller refuses rather than link an unverifiable checkout.
is_primary_checkout() {
  local git_dir common_dir toplevel
  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE
  git_dir=$(git -C "$REPO" rev-parse --path-format=absolute --git-dir 2>/dev/null) || return 1
  common_dir=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  toplevel=$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null) || return 1
  [[ "$git_dir" == "$common_dir" && "$(cd "$REPO" && pwd -P)" == "$toplevel" ]]
}

if [[ $ALLOW_NON_PRIMARY -eq 0 ]] && ! is_primary_checkout; then
  # The primary's path, for the fix line below — read from $REPO's own sibling
  # bin/builder-root (company/decisions/0008) rather than a fixed HOME-derived
  # guess: $REPO need not be a valid checkout at all here, so this is
  # best-effort and falls back to naming no path rather than a wrong one.
  _root_hint=""
  if [[ -x "$REPO/bin/builder-root" ]]; then
    _root_hint="$("$REPO/bin/builder-root" --root 2>/dev/null || true)"
  fi
  echo "link-farm.sh: REFUSING to build the lib farm at $FARM from $REPO." >&2
  echo "  $REPO is not the PRIMARY checkout of agent-platform — a linked worktree," >&2
  echo "  a non-git copy, or an unverifiable checkout. The farm is the deployed" >&2
  echo "  platform library; every launchd job imports from it. A farm linked from" >&2
  echo "  anything but the primary checkout makes unmerged or untracked code live" >&2
  echo "  to the whole machine (the 2026-07-30 incident: 38 of 40 links pointed at" >&2
  echo "  a feature-branch worktree). Run install.sh from" >&2
  if [[ -n $_root_hint ]]; then
    echo "  $_root_hint/agent-platform," >&2
  else
    echo "  the primary checkout of agent-platform (its root cannot be resolved from here)," >&2
  fi
  echo "  or pass --allow-non-primary to link from this checkout anyway." >&2
  exit 1
fi

mkdir -p "$FARM"

linked=0
for src in "$REPO"/lib/agent-platform/*; do
  name="$(basename "$src")"
  dst="$FARM/$name"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    continue  # already correct
  fi
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    bkpdst="$dst.preinstall.$(date +%s)"
    while [[ -e "$bkpdst" ]]; do
      bkpdst="${bkpdst}.0"
    done
    echo "  backing up $dst → $bkpdst" >&2
    mv "$dst" "$bkpdst"
  fi
  ln -sfn "$src" "$dst"
  echo "  linked $name"
  (( linked++ )) || true
done

if (( linked == 0 )); then
  # silent — nothing was new or wrong
  :
fi

# --prune only, and only after the primary-checkout guard above already ran —
# it sits after that check in this same top-to-bottom script, so a refused
# checkout exits before ever reaching here. A stale entry is removed only when
# ALL FOUR hold, fail closed on any doubt:
#   1. it is a SYMLINK, never a regular file — the farm's .preinstall.<ts>
#      backups are real files and must survive untouched.
#   2. its (unresolved) link text falls under $REPO/lib/agent-platform/ — an
#      operator's own link to something else is not ours to delete. Compared
#      as literal text against the prefix, not resolved: the target is about
#      to be checked for existence, so resolving a path that may not exist is
#      exactly the wrong tool here.
#   3. the target does not exist.
if [[ $PRUNE -eq 1 ]]; then
  for dst in "$FARM"/*; do
    [[ -e "$dst" || -L "$dst" ]] || continue  # no-match glob expands literally
    [[ -L "$dst" ]] || continue               # never a regular file
    target="$(readlink "$dst")"
    case "$target" in
      "$REPO/lib/agent-platform/"*) ;;
      *) continue ;;                          # not ours
    esac
    [[ -e "$dst" ]] && continue               # target exists — not stale
    name="$(basename "$dst")"
    echo "  pruned $name (target missing)"
    rm -f "$dst"
  done
fi
