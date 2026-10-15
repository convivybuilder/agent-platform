#!/bin/bash
# tests/bash/check_install_individuator_stage.sh — runs bin/install-individuator's
# REAL staging step (the same `git archive` file list a real build uses, never a
# copy of it) against this checkout's own HEAD, then runs --build-only, which
# calls the installer's own build_release_binaries function — the same
# `swift build -c release --product <p>` calls (one per installed product) the
# real install path runs — and asserts both binaries come out executable.
#
# This is deliberately the real build, not a cheaper stand-in: an earlier
# version of this check ran `swift package describe`, which never compiles
# anything, so it kept passing while bin/install-individuator's real build
# command built the whole package (including the test targets) and failed
# outright — `@testable import IndividuatorCore` cannot resolve against a
# release build of IndividuatorCore. See bin/install-individuator's
# build_release_binaries for that failure and why building by --product avoids
# it.
#
# tests/bash/test_install_individuator.sh always runs with --skip-build, which
# skips the archive-and-build step entirely, so until this file existed nothing
# exercised the staged tree: the manifest declares IndividuatorCoreTests and
# IndividuatorNetworkTests (individuator/Package.swift), and SwiftPM refuses the
# package outright when their target directory is missing from what was staged,
# even for a release build that never compiles a test target.
#
# Deliberately NOT named test_*.sh: tests/bash/run.sh globs that pattern and runs
# every match on the bash job's runner, which has no reason to carry `swift`.
# This check needs a real `swift` and belongs in ci.yml's `swift` job
# (macos-latest) instead — see that job's own step for how it's wired in.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

command -v swift >/dev/null 2>&1 || {
    echo "FAIL: swift is required on PATH to run this check (it belongs in a job that has it)" >&2
    exit 1
}

INSTALLER="$REPO_ROOT/bin/install-individuator"

# Same sudo/id stub as test_install_individuator.sh's make_sudo_stub: execs its
# argv directly (never elevating anything for real), with a faked `id -u` so the
# installer's own "must be root" check passes without this test running as root.
sudo_dir=$(mktemp -d)
id_dir=$(mktemp -d)
cat > "$id_dir/id" <<'EOF'
#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi
EOF
chmod +x "$id_dir/id"
cat > "$sudo_dir/sudo" <<EOF
#!/bin/bash
PATH="$id_dir:\$PATH" exec "\$@"
EOF
chmod +x "$sudo_dir/sudo"

# Every write the installer could reach before --build-only exits is redirected
# into this test's own tmp dirs — never /var/db, /usr/local, /etc or launchd.
install_dir=$(mktemp -d)/opt
config_path=$(mktemp -d)/config.json
launchd_dir=$(mktemp -d)/LaunchDaemons
key_dir=$(mktemp -d)/keys
socket_dir=$(mktemp -d)/individuator-daemon

SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)

echo "== the installer's real staging step and real build (--build-only) produce both binaries =="
out=$(PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
    --repo="$REPO_ROOT" --ref="$SHA" --allow-non-main --build-only \
    --bosun-host=stage-check --bosun-address=127.0.0.1:8710 \
    --root-ca=/dev/null --operator-uid=501 \
    --key-dir="$key_dir" --install-dir="$install_dir" \
    --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
    --socket-dir="$socket_dir" 2>&1)
rc=$?
assert_rc 0 "$rc" "the staged tree (this checkout's own archive file list) builds both installed products with the real swift build command"
[ "$rc" -ne 0 ] && echo "$out" >&2

rm -rf "$sudo_dir" "$id_dir" "$(dirname "$install_dir")" "$(dirname "$config_path")" \
    "$(dirname "$launchd_dir")" "$(dirname "$key_dir")" "$(dirname "$socket_dir")"

finish
