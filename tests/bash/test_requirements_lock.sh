#!/bin/bash
# tests/bash/test_requirements_lock.sh — requirements.lock.txt and
# gateway/requirements.lock.txt are the hash-pinned locks install.sh (main
# venv, ~/.local/lib/knowledge/.venv) and the gateway README's manual `pip
# install` (gateway venv, ~/.local/venv-llm-gateway — ARCH-102 isolation
# means install.sh never touches it) install from. A lock that looks right
# but that `pip --require-hashes` won't actually install from, or that would
# accept a package whose bytes don't match the pin, defeats the one property
# the hash pin exists for — so this builds REAL venvs from scratch and REAL
# installs, same as tests/bash/test_reviewer_lock.sh, rather than asserting
# on the lock files' text alone.
#
# `pip --require-hashes` refuses the WHOLE file if even one requirement in it
# has no hash at all, before installing anything — so tests 1 and 2 below
# (a real install of each unmodified lock succeeding) already prove every
# line in both lock files carries a --hash, with no separate text-parsing
# check needed.
#
# Needs network access to PyPI, same as test_reviewer_lock.sh and
# test_install_agent_platform_shared.sh; PIP_CACHE_DIR is pinned to the same
# location those use so repeated runs (and this file's four venv builds)
# download each package once.
export PIP_CACHE_DIR="${TMPDIR:-/tmp}/agent-platform-shared-test-pip-cache"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

MAIN_LOCK="$REPO_ROOT/requirements.lock.txt"
GATEWAY_LOCK="$REPO_ROOT/gateway/requirements.lock.txt"
INSTALL_SH="$REPO_ROOT/install.sh"
GATEWAY_README="$REPO_ROOT/gateway/README.md"

command -v python3.11 >/dev/null 2>&1 || {
    echo "  SKIPPED: python3.11 not on PATH — cannot exercise either venv build"
    finish
    exit $?
}

assert_eq "1" "$(test -r "$MAIN_LOCK" && echo 1 || echo 0)" \
    "requirements.lock.txt exists and is readable"
assert_eq "1" "$(test -r "$GATEWAY_LOCK" && echo 1 || echo 0)" \
    "gateway/requirements.lock.txt exists and is readable"

# --- 0: install.sh installs the main venv with --require-hashes from the
#        lock, and the gateway README documents the same for the gateway
#        venv (which install.sh never touches — ARCH-102 isolation) --------
phase1_line=$(grep -E 'KNOWLEDGE_VENV/bin/pip" install .*requirements' "$INSTALL_SH")
assert_contains "$phase1_line" "--require-hashes" \
    "install.sh's phase 1 pip install uses --require-hashes"
assert_contains "$phase1_line" "--only-binary :all:" \
    "install.sh's phase 1 pip install uses --only-binary :all: (installs from wheels only; never builds an sdist)"
assert_contains "$phase1_line" "requirements.lock.txt" \
    "install.sh's phase 1 pip install reads requirements.lock.txt, not the unhashed requirements.txt"

readme_text=$(cat "$GATEWAY_README")
assert_contains "$readme_text" "pip install --require-hashes --only-binary :all: -r gateway/requirements.lock.txt" \
    "gateway/README.md documents installing the gateway venv with --require-hashes --only-binary :all: from gateway/requirements.lock.txt"

# install.sh runs no `pip install` line without --require-hashes (an unhashed
# install run immediately before a hashed one would still write unpinned bytes
# into the same venv, lock file notwithstanding), and no such line without
# --only-binary :all: either (a pin-installing line that can fall back to an
# sdist build pulls its build backend from the index, unchecked by
# --require-hashes)
unhashed_pip_lines=$(grep -n 'pip["'"'"']\? install' "$INSTALL_SH" | grep -v -- '--require-hashes')
assert_eq "" "$unhashed_pip_lines" \
    "install.sh has no pip install line lacking --require-hashes"
wheel_only_missing_lines=$(grep -n 'pip["'"'"']\? install' "$INSTALL_SH" | grep -- '--require-hashes' | grep -v -- '--only-binary :all:')
assert_eq "" "$wheel_only_missing_lines" \
    "install.sh has no --require-hashes pip install line lacking --only-binary :all:"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- 1: a fresh venv installs the real main lock wheel-only --------------
python3.11 -m venv "$WORK/main-good" >/dev/null 2>"$WORK/main-good-venv.log"
install_out=$("$WORK/main-good/bin/python" -m pip install --quiet --require-hashes --only-binary :all: -r "$MAIN_LOCK" 2>&1)
install_rc=$?
assert_rc 0 "$install_rc" "pip install --require-hashes --only-binary :all: -r requirements.lock.txt succeeds into a from-scratch venv (every pin resolves to a wheel; no sdist build needed)"
[ "$install_rc" -eq 0 ] || echo "  (pip output: $install_out)"

# --- 2: a fresh venv installs the real gateway lock wheel-only -----------
python3.11 -m venv "$WORK/gateway-good" >/dev/null 2>"$WORK/gateway-good-venv.log"
gw_install_out=$("$WORK/gateway-good/bin/python" -m pip install --quiet --require-hashes --only-binary :all: -r "$GATEWAY_LOCK" 2>&1)
gw_install_rc=$?
assert_rc 0 "$gw_install_rc" "pip install --require-hashes --only-binary :all: -r gateway/requirements.lock.txt succeeds into a from-scratch venv (every pin resolves to a wheel; no sdist build needed)"
[ "$gw_install_rc" -eq 0 ] || echo "  (pip output: $gw_install_out)"

# pip freeze matches the gateway lock's own pins exactly (normalized: pip and
# pip-compile don't agree on case or underscore-vs-hyphen spelling). The
# gateway lock is resolved --universal (every supported Python and
# platform), so a pin can carry an environment marker that is false right
# here — `packaging`, already in the venv as part of the real install
# above, is what decides which pins that marker excludes from this
# comparison, rather than this test guessing at marker syntax itself.
gw_freeze=$("$WORK/gateway-good/bin/python" -m pip freeze 2>/dev/null | sed 's/_/-/g' | tr 'A-Z' 'a-z' | sort)
gw_lock_pins=$("$WORK/gateway-good/bin/python" - "$GATEWAY_LOCK" <<'EOF' | sort
import re
import sys
from packaging.markers import Marker

text = open(sys.argv[1]).read()
names = []
for line in text.splitlines():
    if not line or line.startswith("#") or line.startswith((" ", "\t")):
        continue
    m = re.match(
        r'^([A-Za-z0-9._-]+)(\[[A-Za-z0-9,._-]*\])?==([A-Za-z0-9.+!-]+)'
        r'(?:\s*;\s*(.+?))?\s*\\?$',
        line,
    )
    if not m:
        continue
    name, _extra, version, marker = m.groups()
    if marker and not Marker(marker).evaluate():
        continue
    names.append(name.replace("_", "-").lower() + "==" + version)
print("\n".join(names))
EOF
)
assert_eq "$gw_lock_pins" "$gw_freeze" \
    "pip freeze in the fresh gateway venv matches gateway/requirements.lock.txt's pins whose environment marker (if any) is true here"

# litellm and prisma — the two packages gateway/requirements.txt names
# directly — land at the exact versions it pins
assert_contains "$gw_freeze" "litellm==1.91.5" \
    "the gateway venv installs litellm==1.91.5"
assert_contains "$gw_freeze" "prisma==0.11.0" \
    "the gateway venv installs prisma==0.11.0"

# --- 3/4 helper: corrupt every --hash line for ONE package in a lock file --
#     (flip every hash, not just the first — pip accepts any one listed hash
#     matching, so tampering only the first leaves the stanza installable via
#     its second hash)
tamper_one() { # <src lock> <dst lock> <package name, lowercase as the lock spells it>
    python3 - "$1" "$2" "$3" <<'EOF'
import re
import sys

src, dst, pkg = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src).read()

m = re.search(
    r'\n' + re.escape(pkg) + r'(\[[^\]]*\])?==[^\n]*(?:\n    --hash=sha256:[0-9a-f]+ \\)*\n    --hash=sha256:[0-9a-f]+\n',
    text,
)
if not m:
    sys.exit(f"could not find a {pkg} stanza to tamper with")
stanza = m.group(0)


def flip(match):
    h = match.group(1)
    bad = ('0' if h[0] != '0' else '1') + h[1:]
    return '--hash=sha256:' + bad


tampered = re.sub(r'--hash=sha256:([0-9a-f]{64})', flip, stanza)
assert tampered != stanza, "tampering produced no change"
open(dst, 'w').write(text[: m.start()] + tampered + text[m.end() :])
EOF
}

# --- 3: a main lock with one package's hashes corrupted is REFUSED --------
tamper_one "$MAIN_LOCK" "$WORK/main-tampered.lock.txt" "pyyaml"
tamper_rc=$?
assert_rc 0 "$tamper_rc" "the tampering script found and corrupted the pyyaml stanza in requirements.lock.txt"

python3.11 -m venv "$WORK/main-bad" >/dev/null 2>"$WORK/main-bad-venv.log"
tampered_out=$("$WORK/main-bad/bin/python" -m pip install --quiet --require-hashes -r "$WORK/main-tampered.lock.txt" 2>&1)
tampered_rc=$?
assert_eq "nonzero" "$([ "$tampered_rc" -ne 0 ] && echo nonzero || echo "$tampered_rc")" \
    "pip install --require-hashes REFUSES requirements.lock.txt with one package's hashes corrupted (rc=$tampered_rc)"
assert_contains "$tampered_out" "DO NOT MATCH" \
    "the refusal names a hash mismatch, not some unrelated failure"
"$WORK/main-bad/bin/python" -c "import yaml" 2>/dev/null
assert_rc 1 "$?" \
    "yaml is NOT importable from the venv the tampered install was attempted into"

# --- 4: same proof for the gateway lock, on prisma --------------------------
tamper_one "$GATEWAY_LOCK" "$WORK/gateway-tampered.lock.txt" "prisma"
gw_tamper_rc=$?
assert_rc 0 "$gw_tamper_rc" "the tampering script found and corrupted the prisma stanza in gateway/requirements.lock.txt"

python3.11 -m venv "$WORK/gateway-bad" >/dev/null 2>"$WORK/gateway-bad-venv.log"
gw_tampered_out=$("$WORK/gateway-bad/bin/python" -m pip install --quiet --require-hashes -r "$WORK/gateway-tampered.lock.txt" 2>&1)
gw_tampered_rc=$?
assert_eq "nonzero" "$([ "$gw_tampered_rc" -ne 0 ] && echo nonzero || echo "$gw_tampered_rc")" \
    "pip install --require-hashes REFUSES gateway/requirements.lock.txt with one package's hashes corrupted (rc=$gw_tampered_rc)"
assert_contains "$gw_tampered_out" "DO NOT MATCH" \
    "the refusal names a hash mismatch, not some unrelated failure"
"$WORK/gateway-bad/bin/python" -c "import prisma" 2>/dev/null
assert_rc 1 "$?" \
    "prisma is NOT importable from the venv the tampered install was attempted into"

# --- 5: a lock whose WHEEL hash is corrupted but whose SDIST hash is intact
#        is refused by --only-binary :all:, and installs anyway (building the
#        sdist) without it — proving the fallback --only-binary :all: closes
#        is live, not just asserted away.
#
# click==8.5.0 pins both a wheel and an sdist hash in requirements.lock.txt.
# Confirmed which is which by downloading each independently and hashing it
# (network, done once while writing this test, not run by the test itself):
#   pip download --no-deps --only-binary :all: click==8.5.0
#     -> click-8.5.0-py3-none-any.whl, sha256 255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360
#   pip download --no-deps --no-binary :all:   click==8.5.0
#     -> click-8.5.0.tar.gz,            sha256 ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34
# Both match requirements.lock.txt's two --hash lines for click, in that order.
tamper_wheel_hash_only() { # <src lock> <dst lock> <package> <wheel hash to corrupt>
    python3 - "$1" "$2" "$3" "$4" <<'EOF'
import re
import sys

src, dst, pkg, wheel_hash = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
text = open(src).read()

m = re.search(
    r'\n' + re.escape(pkg) + r'(\[[^\]]*\])?==[^\n]*(?:\n    --hash=sha256:[0-9a-f]+ \\)*\n    --hash=sha256:[0-9a-f]+\n',
    text,
)
if not m:
    sys.exit(f"could not find a {pkg} stanza to tamper with")
stanza = m.group(0)
if wheel_hash not in stanza:
    sys.exit(f"wheel hash {wheel_hash} not found in the {pkg} stanza")

bad = ('0' if wheel_hash[0] != '0' else '1') + wheel_hash[1:]
tampered = stanza.replace('sha256:' + wheel_hash, 'sha256:' + bad)
assert tampered != stanza, "tampering produced no change"
open(dst, 'w').write(text[: m.start()] + tampered + text[m.end() :])
EOF
}

CLICK_WHEEL_HASH="255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360"
tamper_wheel_hash_only "$MAIN_LOCK" "$WORK/main-wheel-tampered.lock.txt" "click" "$CLICK_WHEEL_HASH"
wheel_tamper_rc=$?
assert_rc 0 "$wheel_tamper_rc" "the tampering script found click's stanza in requirements.lock.txt and corrupted only its wheel hash"

# --only-binary :all: refuses the corrupted wheel outright; it never
# considers the sdist, so the sdist's still-valid hash doesn't save it.
python3.11 -m venv "$WORK/main-wheel-bad" >/dev/null 2>"$WORK/main-wheel-bad-venv.log"
wheel_bad_out=$("$WORK/main-wheel-bad/bin/python" -m pip install --quiet --require-hashes --only-binary :all: -r "$WORK/main-wheel-tampered.lock.txt" 2>&1)
wheel_bad_rc=$?
assert_eq "nonzero" "$([ "$wheel_bad_rc" -ne 0 ] && echo nonzero || echo "$wheel_bad_rc")" \
    "pip install --require-hashes --only-binary :all: REFUSES a lock with click's wheel hash corrupted, even with its sdist hash intact (rc=$wheel_bad_rc)"
assert_contains "$wheel_bad_out" "DO NOT MATCH" \
    "the refusal names a hash mismatch, not some unrelated failure"
"$WORK/main-wheel-bad/bin/python" -c "import click" 2>/dev/null
assert_rc 1 "$?" \
    "click is NOT importable from the venv the --only-binary :all: install was attempted into"

# The same tampered lock installs anyway without --only-binary :all: — pip
# falls back to the pinned sdist, whose hash is untouched, and builds it,
# installing its build backend from the index unchecked by --require-hashes.
# This is the fallback --only-binary :all: exists to close; it is shown live
# here rather than only asserted.
python3.11 -m venv "$WORK/main-wheel-fallback" >/dev/null 2>"$WORK/main-wheel-fallback-venv.log"
wheel_fallback_out=$("$WORK/main-wheel-fallback/bin/python" -m pip install --quiet --require-hashes -r "$WORK/main-wheel-tampered.lock.txt" 2>&1)
wheel_fallback_rc=$?
assert_rc 0 "$wheel_fallback_rc" \
    "the same click-wheel-tampered lock installs anyway with --require-hashes alone (no --only-binary :all:) — the sdist fallback is live"
"$WORK/main-wheel-fallback/bin/python" -c "import click" 2>/dev/null
assert_rc 0 "$?" \
    "click IS importable from that venv, built from its sdist"

finish
