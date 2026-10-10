#!/bin/bash
# tests/bash/lint.sh — guard the bash layer against the ARCH-082 class.
# Run under /bin/bash (3.2) in CI. Two checks over every bash-shebang script in
# bin/, tools/, sbin/, gateway/, and lib/ (gateway added 2026-07-19 with the
# gateway launcher, lib/ added with link-farm.sh's primary-checkout guard — a
# bash script outside the searched dirs is a script CI never parses, which is
# exactly what the launcher was for one round):
# (1) it parses under 3.2 (`/bin/bash -n`); (2) it contains no bash-4+
# construct that 3.2.57 can't run. zsh scripts (co, knowledge-autopull) are
# excluded by the shebang filter — they're out of the 3.2 contract.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
fail=0

# `/bin/sh` counts, because on this machine /bin/sh IS the same bash 3.2.57
# binary — a script declaring it is under the identical contract and was simply
# invisible to this check. Five scripts joined the scan when this widened, the
# tailscale watchdog among them, and a launchd plist invokes that one through
# /bin/sh explicitly.
# `headless/` joins the list with the URL handler that lives there. Widening the
# shebang regex without widening the directories was half a fix: it picked up the
# watchdog in bin/ and left headless/install-headless-browser unparsed by the
# very change that was meant to cover it, which is the hazard the note above
# describes, reproduced in one round.
#
# `scripts/ci/` joined 2026-10-07 (CQ-417) with the self-hosted runner's
# isolation probe — a bash-shebang script that runs ONLY on the mac CI
# runner's own 3.2.57, so it is under the exact contract this guard exists
# to check. ci-tree-skip-decide, already in that directory, has a python
# shebang and the regex below skips it as it always has.
scripts=$(grep -rlE '^#!.*(bash|/bin/sh)' "$REPO/bin" "$REPO/tools" "$REPO/sbin" "$REPO/gateway" "$REPO/lib" "$REPO/headless" "$REPO/scripts/ci" 2>/dev/null)

echo "== syntax (/bin/bash -n) =="
for s in $scripts; do
    if /bin/bash -n "$s"; then
        echo "  ok: $(basename "$s")"
    else
        echo "  SYNTAX FAIL: $s"
        fail=1
    fi
done

echo "== bash-4 construct guard =="
# declare -A / mapfile / readarray hard-fail under 3.2 (declare -A is the exact
# ARCH-082 crash). ${var^^}/${var,,} case-modification is also 4+. Full-line
# comments are stripped first so the ARCH-082 explanatory comments (which quote
# `declare -A`) don't trip the guard.
construct_re='declare[[:space:]]+-A|(^|[^[:alnum:]_])(mapfile|readarray)([^[:alnum:]_]|$)|\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|,,|\^|,)'
guard_hit=0
for s in $scripts; do
    hits=$(grep -vE '^[[:space:]]*#' "$s" | grep -nE "$construct_re")
    if [ -n "$hits" ]; then
        echo "  $(basename "$s") (comment-stripped line nums):"
        echo "$hits" | sed 's/^/    /'
        guard_hit=1
    fi
done
if [ "$guard_hit" -eq 1 ]; then
    echo "  BASH-4 CONSTRUCT FOUND above — will abort under /bin/bash 3.2 (ARCH-082 class)"
    fail=1
else
    echo "  clean — no bash-4 constructs"
fi

if [ "$fail" -eq 0 ]; then
    echo "BASH LINT PASSED"
else
    echo "BASH LINT FAILED"
fi
exit "$fail"
