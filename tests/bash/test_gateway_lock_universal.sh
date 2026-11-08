#!/bin/bash
# tests/bash/test_gateway_lock_universal.sh — gateway/requirements.lock.txt
# is resolved with `uv pip compile --universal` (this file's header says so),
# the property that keeps a marker-gated dependency in the lock no matter
# which interpreter or platform resolved it. A single-environment resolution
# omits one: redis==8.1.0 declares `async-timeout>=4.0.3; python_full_version
# < "3.11.3"`, true on Debian 12 bookworm's system CPython (3.11.2) and false
# under the CPython 3.11.16 this file used to be resolved with, so that
# resolution left async-timeout out and `pip install --require-hashes`
# refused the whole file on 3.11.2. This test does not hard-code
# async-timeout as the property to check — a marker-gated package the lock
# has today is not the thing that matters; --universal in the regeneration
# command is. It reads the file as text and makes no network call.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

GATEWAY_LOCK="$REPO_ROOT/gateway/requirements.lock.txt"

assert_eq "1" "$(test -r "$GATEWAY_LOCK" && echo 1 || echo 0)" \
    "gateway/requirements.lock.txt exists and is readable"

header=$(sed -n '1,40p' "$GATEWAY_LOCK" | grep '^#')
assert_contains "$header" "--universal" \
    "the lock's header documents a regeneration command that passes --universal"

# The header check above passes on a regeneration that dropped --universal
# but kept the old header text, bringing back the original bug (a
# marker-gated dependency true elsewhere than on the resolving machine goes
# missing). A --universal resolution is identifiable in the lock content
# itself: it keeps a marker-gated requirement as a top-level pin line of the
# form `name==version ; <marker>` (async-timeout==5.0.1 ; python_full_version
# < '3.11.3' is one today) rather than silently dropping it, so at least one
# such line existing is a property a single-environment regeneration cannot
# produce.
marker_entries=$(grep -E '^[A-Za-z0-9_.][A-Za-z0-9_.-]*(\[[^]]*\])?==[^[:space:];]+[[:space:]]*;[[:space:]]*[A-Za-z_]' "$GATEWAY_LOCK")
assert_eq "1" "$(test -n "$marker_entries" && echo 1 || echo 0)" \
    "the lock carries at least one environment-marked dependency (name==version ; <marker>)"

python3 - "$GATEWAY_LOCK" <<'EOF'
import re
import sys

path = sys.argv[1]
text = open(path).read()

unpinned = []  # top-level lines with no == pin
unhashed = []  # packages with an == pin but no --hash line before the next one

current_name = None
current_has_hash = False


def close_current():
    if current_name is not None and not current_has_hash:
        unhashed.append(current_name)


for raw_line in text.splitlines():
    if not raw_line or raw_line.startswith("#"):
        continue
    if raw_line.startswith("    --hash="):
        current_has_hash = True
        continue
    if raw_line.startswith(" ") or raw_line.startswith("\t"):
        continue  # a "via" comment continuation or similar indented line
    # A new top-level requirement line: "name==version" or
    # "name==version ; marker", so close out whatever package came before it.
    close_current()
    m = re.match(r'^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?==([^\s;\\]+)', raw_line)
    if not m:
        unpinned.append(raw_line)
        current_name = None
        current_has_hash = False
        continue
    current_name = m.group(1)
    current_has_hash = False
close_current()

if unpinned:
    sys.exit("line(s) without an == pin: " + " | ".join(unpinned[:5]))
if unhashed:
    sys.exit("package(s) with no --hash line: " + ", ".join(unhashed[:5]))
EOF
parse_rc=$?
assert_rc 0 "$parse_rc" \
    "every pinned package in gateway/requirements.lock.txt carries an == pin and at least one --hash line"

finish
