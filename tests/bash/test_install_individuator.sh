#!/bin/bash
# bin/install-individuator's own gates: it refuses without root, it never
# regenerates a key that is already present, and its flags reach both the
# config it writes and the `individuator` binary it calls.
#
# Nothing here touches a real root path, a real Secure Enclave key, or real
# launchd: --install-dir/--config-path/--launch-daemons-dir/--key-dir/
# --socket-dir redirect every write into this test's own tmp dirs,
# --skip-build with a stubbed `individuator`/`individuator-daemon` pair
# stands in for `swift build` and the real key, `sudo` is stubbed to exec
# its argv directly (with a faked `id -u` so the script's own root check
# passes), and `launchctl` is tests/bash/lib.sh's own global stub.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-individuator"

# A stub individuator binary. `keygen` writes a marker key file (so a test
# can tell whether it was actually asked to make one); every subcommand,
# keygen included, logs its full argv to $STUB_LOG first, so a test can
# assert both HOW OFTEN and WITH WHAT ARGUMENTS this script called it.
make_stub_bin() { # <dir>
    mkdir -p "$1"
    cat > "$1/individuator" <<'STUB'
#!/bin/bash
echo "$*" >> "$STUB_LOG"
if [ "$1" = "keygen" ]; then
    key_dir=""
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --key-dir) key_dir="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    mkdir -p "$key_dir"
    echo "stub-key" > "$key_dir/machine.key"
fi
exit 0
STUB
    chmod +x "$1/individuator"
    # Only ever copied by the installer, never executed by it.
    cp "$1/individuator" "$1/individuator-daemon"
}

# A scratch git repo carrying just the one file install-individuator reads
# with `git show "$SHA:launchd/..."` — not the real individuator/ source
# tree, since --skip-build never asks it to build anything.
make_repo() { # -> echoes repo dir
    local dir
    dir=$(mktemp -d)
    git init -q "$dir"
    mkdir -p "$dir/launchd"
    cp "$REPO_ROOT/launchd/com.convivy.individuator.plist" "$dir/launchd/"
    git -C "$dir" -c user.email=t@example.com -c user.name=t add -A
    git -C "$dir" -c user.email=t@example.com -c user.name=t commit -q -m init
    git -C "$dir" branch -M main >/dev/null 2>&1 || true
    echo "$dir"
}

# A `sudo` that execs its argv directly (never elevating anything for
# real), after prepending a dir whose `id` fakes `id -u` as 0 — so the
# script's own "must be root" check passes without this test ever running
# as real root. `launchctl` and every other command the script calls stays
# whatever tests/bash/lib.sh already put on PATH (lib.sh's own stub, sourced
# above, refuses `bootstrap`/`bootout` loudly — the installer treats that as
# non-fatal, so this still exercises the full script).
make_sudo_stub() { # -> echoes a dir to prepend to PATH
    local sudo_dir id_dir
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
    echo "$sudo_dir"
}

echo "== refuses without root =="
out=$("$INSTALLER" --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca=/tmp/does-not-matter.pem --operator-uid=501 2>&1)
rc=$?
assert_rc 1 "$rc" "install-individuator must refuse when not running as root"
assert_contains "$out" "must be run as root" "and say why"

echo "== happy path: keygen runs once, is never regenerated, and arguments reach both the config and the binary =="
repo=$(make_repo)
stub_dir=$(mktemp -d)
make_stub_bin "$stub_dir"
sudo_dir=$(make_sudo_stub)
key_dir=$(mktemp -d)/keys
install_dir=$(mktemp -d)/opt
config_path=$(mktemp -d)/config.json
launchd_dir=$(mktemp -d)/LaunchDaemons
socket_dir=$(mktemp -d)/individuator-daemon
STUB_LOG=$(mktemp)
export STUB_LOG

run_installer() {
    PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
        --repo="$repo" --skip-build --build-dir="$stub_dir" \
        --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
        --root-ca=/tmp/library-root.pem --operator-uid=501 \
        --machine-name=test-machine-1 \
        --key-dir="$key_dir" --install-dir="$install_dir" \
        --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
        --socket-dir="$socket_dir" 2>&1
}

out1=$(run_installer); rc1=$?
assert_rc 0 "$rc1" "the first run installs cleanly as root"
assert_contains "$(cat "$key_dir/machine.key" 2>/dev/null)" "stub-key" \
    "the first run generates the key"

out2=$(run_installer); rc2=$?
assert_rc 0 "$rc2" "a second run installs cleanly too"
assert_contains "$out2" "already present" \
    "a second run announces the existing key rather than silently skipping it"

keygen_calls=$(grep -c '^keygen' "$STUB_LOG")
assert_eq "1" "$keygen_calls" \
    "individuator keygen is called exactly once across two installer runs — the second run must not overwrite the key"

assert_contains "$(cat "$config_path")" '"bosun_host": "bosun-test"' \
    "--bosun-host reaches the written config"
assert_contains "$(cat "$config_path")" '"bosun_address": "127.0.0.1:8710"' \
    "--bosun-address reaches the written config"
assert_contains "$(cat "$config_path")" '"root_ca_path": "/tmp/library-root.pem"' \
    "--root-ca reaches the written config"
assert_contains "$(cat "$config_path")" '"operator_uid": 501' \
    "--operator-uid reaches the written config"

assert_contains "$(cat "$STUB_LOG")" "self-test --key-dir $key_dir" \
    "--key-dir reaches the self-test call"
assert_contains "$(cat "$STUB_LOG")" "csr --machine-name test-machine-1 --key-dir $key_dir" \
    "--machine-name and --key-dir reach the csr call"
assert_contains "$(cat "$STUB_LOG")" "fingerprint --key-dir $key_dir" \
    "--key-dir reaches the fingerprint call"

present() { [ -f "$1" ] && echo "present" || echo "missing"; }
assert_eq "present" "$(present "$install_dir/individuator")" "the individuator binary was installed"
assert_eq "present" "$(present "$install_dir/individuator-daemon")" "the daemon binary was installed"
assert_eq "present" "$(present "$install_dir/DEPLOYED_COMMIT")" "a DEPLOYED_COMMIT record was written"
assert_contains "$(cat "$install_dir/DEPLOYED_COMMIT" 2>/dev/null)" "individuator_sha256" "and it records the code hash"

assert_eq "present" "$([ -d "$socket_dir" ] && echo present || echo missing)" \
    "--socket-dir was created for the daemon's Unix socket"
assert_eq "755" "$(stat -f '%Lp' "$socket_dir")" \
    "the socket directory is mode 0755, matching DaemonSocketDirectory.mode"

rm -rf "$repo" "$stub_dir" "$sudo_dir" "$(dirname "$key_dir")" "$(dirname "$install_dir")" \
    "$(dirname "$config_path")" "$(dirname "$launchd_dir")" "$(dirname "$socket_dir")" "$STUB_LOG"

finish
