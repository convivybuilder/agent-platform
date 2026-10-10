#!/bin/bash
# tools/build-reviewer-lock.sh — regenerate requirements-reviewer.lock.txt.
#
# Why a separate lock rather than hashes in requirements.txt itself
# (platform/decisions/0090-builder-runs-as-two-dedicated-accounts-and-github-holds-the-gate):
# root installs the REVIEWER venv with `pip --require-hashes` from a commit it
# confirms is on agent-platform's main, so every byte it installs for a
# reviewer or security-reviewer session is pinned by hash. requirements.txt
# stays the unhashed top-level set for every OTHER install (knowledge-watcher,
# notion-migrate, the dev/test venv); this lock covers only what the reviewer
# tooling actually imports (see the header this script writes).
#
# PINS TO WHAT THIS REPO'S OWN LOCK ALREADY SAYS, NEVER TO WHATEVER PyPI
# RESOLVES TO TODAY OR TO A VENV ON THIS MACHINE: the constraint file below is
# built from the committed requirements-reviewer.lock.txt itself (when one
# exists), so a re-run reproduces exactly the versions already reviewed and
# merged to main. Nothing writable outside this repo (a developer's machine,
# an agents-account venv) can steer which transitive version lands in the
# regenerated lock — only an edit to the three top-level pins below, which
# goes through the same review this script's own output does. Upgrading a
# pin is a deliberate, separate decision, same discipline as requirements.txt
# (ARCH-156).
#
# Usage: tools/build-reviewer-lock.sh
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/.." && pwd)
OUT="$REPO_ROOT/requirements-reviewer.lock.txt"

command -v python3.11 >/dev/null 2>&1 || {
    echo "build-reviewer-lock.sh: python3.11 not found on PATH" >&2
    exit 1
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

python3.11 -m venv "$WORK/build-env"
"$WORK/build-env/bin/python" -m pip install --quiet --upgrade pip pip-tools

# The reviewer tooling's own import surface — see requirements-reviewer.lock.txt's
# header for the derivation. Edit THESE lines (and the header) if that surface
# changes; everything else here is mechanical.
cat > "$WORK/reviewer.in" <<'EOF'
mcp==1.28.1
PyYAML==6.0.3
cryptography==50.0.1
EOF

# Constrain the TRANSITIVE packages to the versions the EXISTING lock already
# pins, so a regeneration reproduces the reviewed tree rather than
# re-resolving against whatever PyPI serves today. The three top-level
# packages above are excluded from the constraint (by name, case-
# insensitively) so a deliberate version bump in reviewer.in isn't fought by
# the old lock's now-stale pin for that same package. On the very first
# generation, before $OUT exists, there is nothing to constrain against and
# pip-compile resolves fresh from reviewer.in alone.
sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p' "$WORK/reviewer.in" | tr 'A-Z' 'a-z' > "$WORK/top-level-names.txt"
if [ -r "$OUT" ]; then
    # Package lines in $OUT may carry an extras marker (e.g. "pyjwt[crypto]==2.15.0");
    # pip constraint files reject extras syntax, and a version pin applies to the
    # package regardless of which extra pulled it in, so the bracket is stripped.
    grep -E '^[A-Za-z0-9._-]+(\[[A-Za-z0-9,._-]*\])?==' "$OUT" \
        | sed 's/ \\$//; s/\[[^]]*\]//' \
        | while IFS= read -r line; do
            pkg_lower=$(echo "$line" | sed -n 's/^\([A-Za-z0-9._-]*\)==.*/\1/p' | tr 'A-Z' 'a-z')
            if grep -Fxq "$pkg_lower" "$WORK/top-level-names.txt"; then
                continue
            fi
            printf '%s\n' "$line"
        done > "$WORK/existing-lock-constraint.txt"
else
    : > "$WORK/existing-lock-constraint.txt"
fi

# Run from $WORK with relative filenames so pip-compile's "# via -c
# existing-lock-constraint.txt" annotation names a fixed string this script
# can strip, rather than this run's own tmp path.
(cd "$WORK" && "$WORK/build-env/bin/pip-compile" --generate-hashes --allow-unsafe \
    --constraint existing-lock-constraint.txt \
    --output-file reviewer.lock.txt \
    reviewer.in)

# pip-compile's own header (every unindented "#" line at the top, naming the
# two temp files above by path) is replaced by the one this repo ships below.
# The per-package "# via -c existing-lock-constraint.txt" annotation is
# dropped too — the constraint file did its job already, and it names a temp
# path that does not exist once this script exits; every OTHER "# via
# <package>" line is kept.
grep -v '^#' "$WORK/reviewer.lock.txt" \
    | grep -v '^    #   -c existing-lock-constraint\.txt$' \
    > "$WORK/reviewer.lock.body.txt"

cat > "$OUT" <<HEADER
# requirements-reviewer.lock.txt — hash-pinned lock for the reviewer tooling's
# venv (platform/decisions/0090-builder-runs-as-two-dedicated-accounts-and-
# github-holds-the-gate): root installs the reviewer/security-reviewer venv
# with \`pip install --require-hashes -r requirements-reviewer.lock.txt\`, so
# every byte it installs is pinned by hash. Regenerate with:
#   tools/build-reviewer-lock.sh
#
# Reviewer tooling's import surface, derived from the review path:
#   mcp          bin/mcp-kb-server and bin/mcp-ops-server — the two MCP
#                servers wired into every session (bin/co), reviewers
#                included (\`from mcp.server.fastmcp import FastMCP\`).
#   PyYAML       \`agentctl kb sync\` / \`kb propose\` / \`kb apply\`, each its
#                own \`venv_bootstrap.ensure(requires=("yaml", ...))\` call —
#                reached through mcp-kb-server's kb_checkout/kb_propose/
#                kb_update/kb_create tools, which shell into \`agentctl kb\`.
#   cryptography \`agentctl kb apply\` (kb_update/kb_create's target
#                subcommand) calls \`venv_bootstrap.ensure(requires=("yaml",
#                "cryptography"))\` unconditionally, before it even reads
#                which provenance was asked for.
# agentctl's own review commands (review-status, spawn, wait, result, status,
# pr-open) are stdlib-only and need nothing from this lock.
# NOT included, though requirements.txt carries them for other installs:
# watchdog (knowledge-watcher only) and requests (notion-migrate only) — ask
# lib/agent-platform/venv_bootstrap.py's module docstring who needs what if
# this surface is ever in question.
#
# Transitive versions are pinned to what THIS FILE already said before the
# regeneration that produced it (tools/build-reviewer-lock.sh constrains
# pip-compile to its own prior output); only mcp, PyYAML and cryptography can
# move, and only by editing the pins in that script. Transitive hashes below
# are pip-compile's (sha256 wheel + sdist for every platform PyPI publishes),
# not hand-computed.
HEADER
cat "$WORK/reviewer.lock.body.txt" >> "$OUT"

echo "wrote $OUT"
