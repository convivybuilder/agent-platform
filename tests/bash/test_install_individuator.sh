#!/bin/bash
# bin/install-individuator's own gates: it refuses without root, it never
# calls `individuator keygen`/`self-test`/`fingerprint` (the daemon creates
# and self-tests its own key now — 2026-10-15), it waits for and reads the
# daemon's own self-test line, and its flags reach both the config it
# writes and the `individuator` binary's `csr` call.
#
# Nothing here touches a real root path, a real machine key, or real
# launchd: --install-dir/--config-path/--launch-daemons-dir/--key-dir/
# --socket-dir/--daemon-log redirect every write into this test's own tmp
# dirs, --skip-build with a stubbed `individuator`/`individuator-daemon`
# pair stands in for `swift build` and the real key, `sudo` is stubbed to
# exec its argv directly (with a faked `id -u` so the script's own root
# check passes), and `launchctl` is tests/bash/lib.sh's own global stub,
# which refuses `bootstrap`/`bootout` loudly — so the stub daemon this
# installs never actually runs, and never writes a real self-test line;
# tests that need one pre-write it to --daemon-log themselves.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-individuator"

# A stub individuator binary: every subcommand just logs its full argv to
# $STUB_LOG, so a test can assert both HOW OFTEN and WITH WHAT ARGUMENTS
# this script called it. Never special-cases any subcommand — unlike the
# retired Secure Enclave key's keygen stub, the daemon (never this binary)
# is what creates a key now, so there is nothing here for this script to
# fake creating.
make_stub_bin() { # <dir>
    mkdir -p "$1"
    cat > "$1/individuator" <<'STUB'
#!/bin/bash
echo "$*" >> "$STUB_LOG"
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

echo "== happy path: the daemon's own self-test line is read, and arguments reach both the config and the csr call =="
repo=$(make_repo)
stub_dir=$(mktemp -d)
make_stub_bin "$stub_dir"
sudo_dir=$(make_sudo_stub)
key_dir=$(mktemp -d)/keys
install_dir=$(mktemp -d)/opt
config_path=$(mktemp -d)/config.json
launchd_dir=$(mktemp -d)/LaunchDaemons
socket_dir=$(mktemp -d)/individuator-daemon
daemon_log=$(mktemp -d)/individuator.out.log
STUB_LOG=$(mktemp)
export STUB_LOG

# A --root-ca source the test controls directly: an operator-writable path
# (standing in for the real one, typically under the operator's own home),
# so a test can tell the written config apart from a config that still
# names this path as-is (the defect) rather than the root-owned copy.
root_ca_dir=$(mktemp -d)
root_ca_src="$root_ca_dir/library-root.pem"
echo "fixture library root CA (synthetic; not a real certificate) v1" > "$root_ca_src"
root_ca_copy="$(dirname "$config_path")/root-ca.pem"

# `launchctl bootstrap` is stubbed to refuse (tests/bash/lib.sh), so the
# stub daemon never actually runs and never writes its own self-test line
# — this stands in for it already being there from an earlier real start,
# so every run below finds it on the very first check, with no waiting.
fake_self_test_line="individuator-daemon: machine key self-test passed (created), effective uid 0, public key fingerprint deadbeef"
echo "$fake_self_test_line" > "$daemon_log"

run_installer() {
    PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
        --repo="$repo" --skip-build --build-dir="$stub_dir" \
        --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
        --root-ca="$root_ca_src" --operator-uid=501 \
        --machine-name=test-machine-1 \
        --key-dir="$key_dir" --install-dir="$install_dir" \
        --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
        --socket-dir="$socket_dir" --daemon-log="$daemon_log" 2>&1
}

out1=$(run_installer); rc1=$?
assert_rc 0 "$rc1" "the first run installs cleanly as root"
assert_contains "$out1" "$fake_self_test_line" \
    "the daemon's own self-test line is read and printed"

out2=$(run_installer); rc2=$?
assert_rc 0 "$rc2" "a second run installs cleanly too"
assert_contains "$out2" "Root CA unchanged" \
    "a second run with the same --root-ca source leaves the copy as-is and says so"

for subcommand in keygen self-test fingerprint; do
    assert_eq "0" "$(grep -c "^$subcommand" "$STUB_LOG")" \
        "individuator $subcommand is never called — the daemon creates and self-tests its own key"
done

assert_contains "$(cat "$config_path")" '"bosun_host": "bosun-test"' \
    "--bosun-host reaches the written config"
assert_contains "$(cat "$config_path")" '"bosun_address": "127.0.0.1:8710"' \
    "--bosun-address reaches the written config"
assert_contains "$(cat "$config_path")" "\"root_ca_path\": \"$root_ca_copy\"" \
    "root_ca_path in the config names the root-owned copy, not --root-ca's own path"
assert_contains "$(cat "$config_path")" '"operator_uid": 501' \
    "--operator-uid reaches the written config"

case "$(cat "$config_path")" in
    *"$root_ca_src"*) root_ca_src_leaked="yes" ;;
    *) root_ca_src_leaked="no" ;;
esac
assert_eq "no" "$root_ca_src_leaked" \
    "the config never names the operator-writable --root-ca path directly"

assert_eq "present" "$([ -f "$root_ca_copy" ] && echo present || echo missing)" \
    "--root-ca was copied beside the config"
assert_contains "$(cat "$root_ca_copy" 2>/dev/null)" "v1" \
    "and the copy's content matches the source"
assert_eq "644" "$(stat -f '%Lp' "$root_ca_copy")" \
    "the copy is mode 0644"

assert_contains "$(cat "$STUB_LOG")" "csr --machine-name test-machine-1 --key-dir $key_dir" \
    "--machine-name and --key-dir reach the csr call"

present() { [ -f "$1" ] && echo "present" || echo "missing"; }
assert_eq "present" "$(present "$install_dir/individuator")" "the individuator binary was installed"
assert_eq "present" "$(present "$install_dir/individuator-daemon")" "the daemon binary was installed"
assert_eq "present" "$(present "$install_dir/DEPLOYED_COMMIT")" "a DEPLOYED_COMMIT record was written"
assert_contains "$(cat "$install_dir/DEPLOYED_COMMIT" 2>/dev/null)" "individuator_sha256" "and it records the code hash"

assert_eq "present" "$([ -d "$socket_dir" ] && echo present || echo missing)" \
    "--socket-dir was created for the daemon's Unix socket"
assert_eq "755" "$(stat -f '%Lp' "$socket_dir")" \
    "the socket directory is mode 0755, matching DaemonSocketDirectory.mode"

echo "== a changed --root-ca source is recopied, and the run says so =="
echo "fixture library root CA (synthetic; not a real certificate) v2" > "$root_ca_src"
out3=$(run_installer); rc3=$?
assert_rc 0 "$rc3" "a third run, with a changed --root-ca source, installs cleanly"
assert_contains "$out3" "Root CA copied" \
    "a changed --root-ca source is recopied and the run says so"
assert_contains "$(cat "$root_ca_copy" 2>/dev/null)" "v2" \
    "the copy picks up the changed source"

echo "== no self-test line ever appears: the run still installs, and warns rather than hanging =="
missing_log=$(mktemp -d)/individuator.out.log
out4=$(PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
    --repo="$repo" --skip-build --build-dir="$stub_dir" \
    --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca="$root_ca_src" --operator-uid=501 \
    --machine-name=test-machine-1 \
    --key-dir="$key_dir" --install-dir="$install_dir" \
    --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
    --socket-dir="$socket_dir" --daemon-log="$missing_log" \
    --self-test-wait-seconds=1 2>&1)
rc4=$?
assert_rc 0 "$rc4" "the run still installs cleanly when no self-test line ever appears"
assert_contains "$out4" "no self-test line from the daemon" \
    "and it warns, rather than ever hanging or failing the whole install"

rm -rf "$repo" "$stub_dir" "$sudo_dir" "$(dirname "$key_dir")" "$(dirname "$install_dir")" \
    "$(dirname "$config_path")" "$(dirname "$launchd_dir")" "$(dirname "$socket_dir")" \
    "$(dirname "$daemon_log")" "$(dirname "$missing_log")" "$root_ca_dir" "$STUB_LOG"

finish
