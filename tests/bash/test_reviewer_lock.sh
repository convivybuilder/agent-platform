#!/bin/bash
# Tests for requirements-reviewer.lock.txt — the hash-pinned lock root installs
# the reviewer/security-reviewer venv from (platform/decisions/0090-builder-
# runs-as-two-dedicated-accounts-and-github-holds-the-gate). A lock that looks
# right but that `pip --require-hashes` won't actually install from, or that
# would accept a package whose bytes don't match the pin, defeats the one
# property the hash pin exists for — so this builds a REAL venv from scratch
# and a REAL install, twice (once honest, once with one hash flipped), rather
# than asserting on the lock file's text.
#
# Needs network access to PyPI, same as tests/bash/test_install_agent_platform_shared.sh;
# PIP_CACHE_DIR is pinned to one location so repeated runs of this file (and
# that one) download each package once.
export PIP_CACHE_DIR="${TMPDIR:-/tmp}/agent-platform-shared-test-pip-cache"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

LOCK="$REPO_ROOT/requirements-reviewer.lock.txt"

command -v python3.11 >/dev/null 2>&1 || {
    echo "  SKIPPED: python3.11 not on PATH — cannot exercise the reviewer venv build"
    finish
    exit $?
}

assert_eq "1" "$(test -r "$LOCK" && echo 1 || echo 0)" \
    "requirements-reviewer.lock.txt exists and is readable"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- 0: the build script's top-level pins agree with requirements.txt ------
#        (code review drift note on #733) — the lock's three packages
#        (mcp, PyYAML, cryptography) are also unhashed top-level pins in
#        requirements.txt for every OTHER install; a pin moved in one file
#        and not the other is exactly the drift that defeats "regenerate
#        and it reproduces the reviewed tree".
BUILD_SCRIPT="$REPO_ROOT/tools/build-reviewer-lock.sh"
REQUIREMENTS="$REPO_ROOT/requirements.txt"
awk '/<<.?EOF.?$/{p=1;next} p&&/^EOF$/{p=0;next} p' "$BUILD_SCRIPT" | sort > "$WORK/script-pins.txt"
for pkg in mcp PyYAML cryptography; do
    script_pin=$(grep -E "^${pkg}==" "$WORK/script-pins.txt")
    req_pin=$(grep -E "^${pkg}==" "$REQUIREMENTS")
    assert_eq "$req_pin" "$script_pin" \
        "tools/build-reviewer-lock.sh's $pkg pin matches requirements.txt's"
done

# --- 1: a fresh venv installs the real lock with --require-hashes ----------
python3.11 -m venv "$WORK/good" >/dev/null 2>"$WORK/good-venv.log"
install_out=$("$WORK/good/bin/python" -m pip install --quiet --require-hashes -r "$LOCK" 2>&1)
install_rc=$?
assert_rc 0 "$install_rc" "pip install --require-hashes -r requirements-reviewer.lock.txt succeeds into a from-scratch venv"
[ "$install_rc" -eq 0 ] || echo "  (pip output: $install_out)"

# --- 2: the tooling's entry modules import from THAT venv's own interpreter -
import_out=$("$WORK/good/bin/python" -c '
import mcp
from mcp.server.fastmcp import FastMCP
import yaml
from cryptography import x509
print("IMPORT-OK")
' 2>&1)
import_rc=$?
assert_rc 0 "$import_rc" "mcp, mcp.server.fastmcp.FastMCP, yaml and cryptography.x509 import cleanly from the locked venv"
assert_contains "$import_out" "IMPORT-OK" \
    "the import script actually ran to its last line, not just exited 0"

# --- 3: a lock with one package's hashes corrupted is REFUSED, not silently -
#        accepted via a different platform's hash still being correct in the
#        same stanza (round 1 of this test: flipping only the FIRST hash on a
#        multi-wheel package like PyYAML installed clean anyway, because pip
#        accepts any one listed hash matching — flip every hash line for the
#        package instead).
python3 - "$LOCK" "$WORK/tampered.lock.txt" <<'EOF'
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()

m = re.search(r'\npyyaml==[^\n]*(?:\n    --hash=sha256:[0-9a-f]+ \\)*\n    --hash=sha256:[0-9a-f]+\n', text)
if not m:
    sys.exit("could not find a pyyaml stanza to tamper with")
stanza = m.group(0)


def flip(match):
    h = match.group(1)
    bad = ('0' if h[0] != '0' else '1') + h[1:]
    return '--hash=sha256:' + bad


tampered = re.sub(r'--hash=sha256:([0-9a-f]{64})', flip, stanza)
assert tampered != stanza, "tampering produced no change"
open(dst, 'w').write(text[:m.start()] + tampered + text[m.end():])
EOF
tamper_rc=$?
assert_rc 0 "$tamper_rc" "the tampering script found and corrupted the pyyaml stanza"

python3.11 -m venv "$WORK/bad" >/dev/null 2>"$WORK/bad-venv.log"
tampered_out=$("$WORK/bad/bin/python" -m pip install --quiet --require-hashes -r "$WORK/tampered.lock.txt" 2>&1)
tampered_rc=$?
assert_eq "nonzero" "$([ "$tampered_rc" -ne 0 ] && echo nonzero || echo "$tampered_rc")" \
    "pip install --require-hashes REFUSES a lock with one package's hashes corrupted (rc=$tampered_rc)"
assert_contains "$tampered_out" "DO NOT MATCH" \
    "the refusal names a hash mismatch, not some unrelated failure"
"$WORK/bad/bin/python" -c "import yaml" 2>/dev/null
assert_rc 1 "$?" \
    "yaml is NOT importable from the venv the tampered install was attempted into"

finish
