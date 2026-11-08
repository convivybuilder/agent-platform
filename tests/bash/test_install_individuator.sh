#!/bin/bash
# bin/install-individuator's own gates: it refuses without root, it never
# calls `individuator keygen`/`self-test`/`fingerprint` (the daemon creates
# and self-tests its own key now — 2026-10-15), it waits for and reads the
# daemon's own self-test line, its flags reach both the config it writes
# and the `individuator` binary's `csr` call, and a repeatable --account
# writes the admitted-accounts list that a later run with no --account at
# all leaves unchanged (E12·2; platform/decisions/0099 item 8).
#
# Nothing here touches a real root path, a real machine key, or real
# launchd: --install-dir/--config-path/--launch-daemons-dir/--key-dir/
# --socket-dir/--daemon-log redirect every write into this test's own tmp
# dirs, --skip-build with a stubbed `individuator`/`individuator-daemon`
# pair stands in for `swift build` and the real key, `sudo` is stubbed to
# exec its argv directly (with a faked `id` so the script's own root check
# passes and `--account`'s two synthetic test accounts, alice/bob, resolve
# to fixed synthetic uids without a real privilege change), and `launchctl`
# is tests/bash/lib.sh's own global stub, which refuses `bootstrap`/
# `bootout` loudly — so the stub daemon this installs never actually runs,
# and never writes a real self-test line; tests that need one pre-write it
# to --daemon-log themselves.

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

# make_repo_at <dir> — the same fixture as make_repo, but created in place
# rather than at a random mktemp name, so a test can put it at a path whose
# LAST COMPONENT is literally "agent-platform" (what install-individuator's
# own REPO resolution always appends to a resolved root).
make_repo_at() { # <dir>
    mkdir -p "$1"
    git init -q "$1"
    mkdir -p "$1/launchd"
    cp "$REPO_ROOT/launchd/com.convivy.individuator.plist" "$1/launchd/"
    git -C "$1" -c user.email=t@example.com -c user.name=t add -A
    git -C "$1" -c user.email=t@example.com -c user.name=t commit -q -m init
    git -C "$1" branch -M main >/dev/null 2>&1 || true
}

# A `sudo` that execs its argv directly (never elevating anything for
# real), after prepending a dir whose `id` fakes `id -u` (bare, no name) as
# 0 — so the script's own "must be root" check passes without this test
# ever running as real root — and fakes `id -u <name>` for exactly the two
# synthetic test accounts install-individuator's own --account resolution
# calls it for, so two admitted accounts can be exercised without a real
# privilege change (the same idiom PeerUIDGateTests uses for a uid it
# cannot otherwise simulate). `launchctl` and every other command the
# script calls stays whatever tests/bash/lib.sh already put on PATH
# (lib.sh's own stub, sourced above, refuses `bootstrap`/`bootout` loudly —
# the installer treats that as non-fatal, so this still exercises the full
# script).
make_sudo_stub() { # -> echoes a dir to prepend to PATH
    local sudo_dir id_dir
    sudo_dir=$(mktemp -d)
    id_dir=$(mktemp -d)
    cat > "$id_dir/id" <<'EOF'
#!/bin/bash
if [ "$1" = "-u" ] && [ "$#" -eq 1 ]; then
    echo 0
elif [ "$1" = "-u" ]; then
    case "$2" in
        alice) echo 9001 ;;
        bob) echo 9002 ;;
        # Round-1 revise fixtures (PR 1110): probe-gamma and probe-delta
        # share a uid on purpose, to exercise "two names, one uid" without
        # ever touching a real account.
        probe-alpha) echo 9101 ;;
        probe-beta) echo 9102 ;;
        probe-gamma) echo 9103 ;;
        probe-delta) echo 9103 ;;
        # Resolves the test-8 JSON-injection fixture to a uid on purpose,
        # so that fixture exercises the account database actually
        # resolving the crafted name rather than failing at this stub for
        # an unrelated reason (security-reviewer comment 6578327476).
        'x","account":"y') echo 9104 ;;
        *) exit 1 ;;
    esac
else
    exec /usr/bin/id "$@"
fi
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
    --root-ca=/tmp/does-not-matter.pem --account=alice 2>&1)
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

# run_installer [account ...] — zero or more --account=<name> flags, one
# per argument, so a test can assert both "repeating --account admits more
# than one account" and "omitting it keeps whatever is already there".
run_installer() {
    local account_args=""
    for acct in "$@"; do
        account_args="$account_args --account=$acct"
    done
    PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
        --repo="$repo" --skip-build --build-dir="$stub_dir" \
        --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
        --root-ca="$root_ca_src" $account_args \
        --machine-name=test-machine-1 \
        --key-dir="$key_dir" --install-dir="$install_dir" \
        --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
        --socket-dir="$socket_dir" --daemon-log="$daemon_log" 2>&1
}

# run_installer_no_bosun <extra-arg> [...] — the same fixtures as
# run_installer, but with no --bosun-host/--bosun-address/--root-ca of its
# own, for a later run against an already-written config that updates (or
# is refused before updating) only the admitted-accounts half of it.
run_installer_no_bosun() {
    PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
        --repo="$repo" --skip-build --build-dir="$stub_dir" \
        --machine-name=test-machine-1 \
        --key-dir="$key_dir" --install-dir="$install_dir" \
        --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
        --socket-dir="$socket_dir" --daemon-log="$daemon_log" "$@" 2>&1
}

out1=$(run_installer alice bob); rc1=$?
assert_rc 0 "$rc1" "the first run installs cleanly as root, with two admitted accounts"
assert_contains "$out1" "$fake_self_test_line" \
    "the daemon's own self-test line is read and printed"

out2=$(run_installer); rc2=$?
assert_rc 0 "$rc2" "a second run, with no --account at all, installs cleanly too"
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
assert_contains "$(cat "$config_path")" '{"uid": 9001, "account": "alice"}' \
    "the first --account reaches the written config, resolved to its uid"
assert_contains "$(cat "$config_path")" '{"uid": 9002, "account": "bob"}' \
    "the second --account reaches the written config too"

case "$(cat "$config_path")" in
    *'library_address'*) library_address_present="yes" ;;
    *) library_address_present="no" ;;
esac
assert_eq "no" "$library_address_present" \
    "an install that never names --library-address carries no library_address key at all"

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
assert_contains "$(cat "$config_path")" '{"uid": 9001, "account": "alice"}' \
    "a third run with no --account at all still keeps the first admitted account"
assert_contains "$(cat "$config_path")" '{"uid": 9002, "account": "bob"}' \
    "...and the second"

echo "== no self-test line ever appears: the run still installs, and warns rather than hanging =="
missing_log=$(mktemp -d)/individuator.out.log
out4=$(PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
    --repo="$repo" --skip-build --build-dir="$stub_dir" \
    --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca="$root_ca_src" \
    --machine-name=test-machine-1 \
    --key-dir="$key_dir" --install-dir="$install_dir" \
    --config-path="$config_path" --launch-daemons-dir="$launchd_dir" \
    --socket-dir="$socket_dir" --daemon-log="$missing_log" \
    --self-test-wait-seconds=1 2>&1)
rc4=$?
assert_rc 0 "$rc4" "the run still installs cleanly when no self-test line ever appears"
assert_contains "$out4" "no self-test line from the daemon" \
    "and it warns, rather than ever hanging or failing the whole install"

# --- round-1 revise fixtures (PR 1110): a repeated --account, an invalid
# name, a name that cannot resolve, and --account alone against an
# already-written config. config_before is the config as test 3/4 above
# left it (bosun-test / alice, bob), so every refusal below can assert the
# file is byte-for-byte untouched. -------------------------------------------
config_before=$(cat "$config_path")

echo "== a duplicate --account (the same name twice) is refused, and nothing is written =="
out5=$(run_installer_no_bosun --account=probe-alpha --account=probe-alpha); rc5=$?
assert_rc 2 "$rc5" "a repeated --account name is refused"
assert_contains "$out5" "duplicate --account: probe-alpha" "and it names the duplicate"
assert_eq "$config_before" "$(cat "$config_path")" "the config is untouched"

echo "== two different --account names that resolve to the same uid are refused, and nothing is written =="
out6=$(run_installer_no_bosun --account=probe-gamma --account=probe-delta); rc6=$?
assert_rc 2 "$rc6" "two names resolving to one uid are refused"
assert_contains "$out6" "probe-delta resolves to uid 9103, already admitted by another --account" \
    "and it names the duplicate uid and the account that already claimed it"
assert_eq "$config_before" "$(cat "$config_path")" "the config is untouched"

echo "== an --account name that fails the strict character class is refused before any lookup or write =="
out7=$(run_installer_no_bosun --account='probe-alpha"injected'); rc7=$?
assert_rc 2 "$rc7" "a name outside the allowed character class is refused"
assert_contains "$out7" 'invalid --account name: probe-alpha"injected' "and it says why"
assert_eq "$config_before" "$(cat "$config_path")" "the config is untouched"

echo "== an --account name built to close its JSON string and inject a second entry cannot add one =="
# The id stub resolves this exact name to uid 9104 (unlike probe-missing),
# so this fixture exercises the account database actually resolving the
# crafted name rather than failing at the stub for an unrelated reason.
# --bosun-host is given (via run_installer, not run_installer_no_bosun)
# because against 0c7788743 the account loop — and the hand-built JSON
# concatenation it feeds — only ran when --bosun-host was also given
# (round-1 finding #3, same file); only on that path does the resolved
# name reach the old concatenation and the "y" substring below actually
# appear in the config. Here the character-class check refuses the name
# before id -u is ever called, regardless of --bosun-host.
out8=$(run_installer 'x","account":"y'); rc8=$?
assert_rc 2 "$rc8" "the crafted name is refused by the same character-class check, never reaching the JSON"
assert_eq "$config_before" "$(cat "$config_path")" "the config is untouched"
case "$(cat "$config_path")" in
    *'"y"'*|*'uid": 0'*) injected="yes" ;;
    *) injected="no" ;;
esac
assert_eq "no" "$injected" "no attacker-named entry, and no uid-0 entry, reached the config"

echo "== an --account name that does not resolve to any account is refused, and nothing is written =="
out9=$(run_installer_no_bosun --account=probe-missing); rc9=$?
assert_rc 2 "$rc9" "a name id -u cannot resolve is refused"
assert_contains "$out9" "no such account: probe-missing" "and it says why"
assert_eq "$config_before" "$(cat "$config_path")" "the config is untouched"

echo "== --account alone, with no --bosun-host, updates the admitted list of an existing config and keeps the Bosun fields =="
out10=$(run_installer_no_bosun --account=probe-beta); rc10=$?
assert_rc 0 "$rc10" "--account alone against an existing config installs cleanly"
assert_contains "$out10" "Config written" "and it writes the config rather than reporting it unchanged"
assert_contains "$(cat "$config_path")" '{"uid": 9102, "account": "probe-beta"}' \
    "the lone --account reaches the written config, resolved to its uid"
assert_contains "$(cat "$config_path")" '"bosun_host": "bosun-test"' \
    "bosun_host is kept from the existing config, unchanged"
assert_contains "$(cat "$config_path")" '"bosun_address": "127.0.0.1:8710"' \
    "...and bosun_address"
assert_contains "$(cat "$config_path")" "\"root_ca_path\": \"$root_ca_copy\"" \
    "...and root_ca_path"

echo "== neither --bosun-host nor --account given leaves the config unchanged =="
config_before_noop=$(cat "$config_path")
out11=$(run_installer_no_bosun); rc11=$?
assert_rc 0 "$rc11" "a run with neither flag still installs cleanly"
assert_contains "$out11" "Config unchanged" "and it says the config is unchanged"
assert_eq "$config_before_noop" "$(cat "$config_path")" "and the config is untouched"

echo "== --library-address alone, with no --bosun-host, writes only that field and keeps everything else =="
out13=$(run_installer_no_bosun --library-address=127.0.0.1:7777); rc13=$?
assert_rc 0 "$rc13" "--library-address alone against an existing config installs cleanly"
assert_contains "$out13" "Config written" "and it writes the config rather than reporting it unchanged"
assert_contains "$(cat "$config_path")" '"library_address": "127.0.0.1:7777"' \
    "--library-address reaches the written config"
assert_contains "$(cat "$config_path")" '"bosun_host": "bosun-test"' \
    "bosun_host is kept from the existing config, unchanged"
assert_contains "$(cat "$config_path")" '{"uid": 9102, "account": "probe-beta"}' \
    "...and the admitted accounts already there"

echo "== a second --library-address replaces the first, and the daemon's own serving test covers the rest =="
out14=$(run_installer_no_bosun --library-address=127.0.0.1:8888); rc14=$?
assert_rc 0 "$rc14" "a changed --library-address installs cleanly"
assert_contains "$(cat "$config_path")" '"library_address": "127.0.0.1:8888"' \
    "the new --library-address replaces the old one"
case "$(cat "$config_path")" in
    *"127.0.0.1:7777"*) old_value_present="yes" ;;
    *) old_value_present="no" ;;
esac
assert_eq "no" "$old_value_present" "and the old value is gone, not merely appended"

echo "== a run with neither flag given leaves an already-configured library_address untouched =="
config_before_library_noop=$(cat "$config_path")
out15=$(run_installer_no_bosun); rc15=$?
assert_rc 0 "$rc15" "a run with no flags at all still installs cleanly"
assert_eq "$config_before_library_noop" "$(cat "$config_path")" \
    "the config, library_address included, is byte-for-byte untouched"

echo "== --account alone, with no config at --config-path yet, is refused by the first-install gate before any account lookup =="
fresh_config_path=$(mktemp -d)/config.json
out12=$(PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
    --repo="$repo" --skip-build --build-dir="$stub_dir" \
    --account=probe-beta \
    --machine-name=test-machine-1 \
    --key-dir="$key_dir" --install-dir="$install_dir" \
    --config-path="$fresh_config_path" --launch-daemons-dir="$launchd_dir" \
    --socket-dir="$socket_dir" --daemon-log="$daemon_log" 2>&1)
rc12=$?
assert_rc 2 "$rc12" "--account alone on a fresh machine is refused"
assert_contains "$out12" "--bosun-host is required on a first install" \
    "with the first-install reason, not an account error"
assert_eq "missing" "$([ -f "$fresh_config_path" ] && echo present || echo missing)" \
    "and no config file is written"

# --- REPO resolution under sudo with no --repo (the 2026-11-02 production
# occurrence: `sudo install-individuator` on its own resolved
# /var/root/builder/agent-platform and refused it as "not a git checkout",
# because bin/builder-root --root reads the PASSWD home of the CALLING
# process's own uid — root's, under sudo — never the invoking operator's).
# Both cases below must fail against f107c1cb0 for the stated reason: that
# commit calls plain `builder-root --root` unconditionally, never consulting
# SUDO_USER at all, so it neither resolves SUDO_USER's own checkout nor
# refuses when SUDO_USER is absent. ----------------------------------------

echo "== under sudo with no --repo: REPO resolves against SUDO_USER's own home (the system account database), never the invoking shell's HOME =="
sudo_user_name="installtestop"
sudo_user_home=$(mktemp -d)
sudo_user_repo="$sudo_user_home/Convivy/agent-platform"
make_repo_at "$sudo_user_repo"
mkdir -p "$sudo_user_home/.config/convivy"
printf 'CREWS_ROOT=%s\n' "$sudo_user_home/Convivy" > "$sudo_user_home/.config/convivy/session-agent.env"
sudo_user_sha=$(git -C "$sudo_user_repo" rev-parse main)

# Standing in for sudo's own HOME=/var/root under a real sudo invocation —
# never consulted by the fix (the system account database, not $HOME, names
# the invoking operator's home), set here so a regression that read $HOME
# instead would resolve here and be caught by the assertion below.
fake_root_home=$(mktemp -d)

# A `dscacheutil` stub resolving exactly $sudo_user_name's record, in the
# real `dscacheutil -q user -a name <name>` output shape. The stub's own
# synthetic home lives under a mktemp dir, not /Users.
dscl_dir=$(mktemp -d)
cat > "$dscl_dir/dscacheutil" <<EOF
#!/bin/bash
if [ "\$1" = "-q" ] && [ "\$2" = "user" ] && [ "\$3" = "-a" ] && [ "\$4" = "name" ] && [ "\$5" = "$sudo_user_name" ]; then
    echo "name: $sudo_user_name"
    echo "dir: $sudo_user_home"
else
    exit 1
fi
EOF
chmod +x "$dscl_dir/dscacheutil"

no_repo_install_dir=$(mktemp -d)/opt
no_repo_config_path=$(mktemp -d)/config.json
no_repo_launchd_dir=$(mktemp -d)/LaunchDaemons
no_repo_socket_dir=$(mktemp -d)/individuator-daemon
no_repo_key_dir=$(mktemp -d)/keys

out_sudo_user=$(SUDO_USER="$sudo_user_name" HOME="$fake_root_home" \
    PATH="$dscl_dir:$sudo_dir:$PATH" sudo "$INSTALLER" \
    --skip-build --build-dir="$stub_dir" \
    --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca="$root_ca_src" --account=alice \
    --machine-name=test-machine-1 \
    --key-dir="$no_repo_key_dir" --install-dir="$no_repo_install_dir" \
    --config-path="$no_repo_config_path" --launch-daemons-dir="$no_repo_launchd_dir" \
    --socket-dir="$no_repo_socket_dir" --daemon-log="$daemon_log" 2>&1)
rc_sudo_user=$?
assert_rc 0 "$rc_sudo_user" "with no --repo, SUDO_USER's own checkout resolves and installs cleanly"
[ "$rc_sudo_user" -ne 0 ] && echo "$out_sudo_user" >&2
assert_contains "$(cat "$no_repo_install_dir/DEPLOYED_COMMIT" 2>/dev/null)" "installed_from: $sudo_user_repo" \
    "REPO resolved to SUDO_USER's own checkout, by its dscacheutil-looked-up home, not the invoking shell's HOME"
assert_contains "$(cat "$no_repo_install_dir/DEPLOYED_COMMIT" 2>/dev/null)" "commit: $sudo_user_sha" \
    "...and built from that checkout's own commit"
case "$(cat "$no_repo_install_dir/DEPLOYED_COMMIT" 2>/dev/null)" in
    *"$fake_root_home"*) resolved_under_fake_root="yes" ;;
    *) resolved_under_fake_root="no" ;;
esac
assert_eq "no" "$resolved_under_fake_root" "REPO never resolved under the fake root HOME"

rm -rf "$sudo_user_home" "$fake_root_home" "$dscl_dir" "$(dirname "$no_repo_install_dir")" \
    "$(dirname "$no_repo_config_path")" "$(dirname "$no_repo_launchd_dir")" \
    "$(dirname "$no_repo_socket_dir")" "$(dirname "$no_repo_key_dir")"

echo "== under sudo with no --repo and no SUDO_USER: refused, naming what was missing, rather than silently resolving some other account's or root's own home =="
fake_root_home2=$(mktemp -d)
unset_sudo_user_install_dir=$(mktemp -d)/opt
unset_sudo_user_config_path=$(mktemp -d)/config.json
unset_sudo_user_launchd_dir=$(mktemp -d)/LaunchDaemons
unset_sudo_user_socket_dir=$(mktemp -d)/individuator-daemon
unset_sudo_user_key_dir=$(mktemp -d)/keys

out_no_sudo_user=$(HOME="$fake_root_home2" PATH="$sudo_dir:$PATH" env -u SUDO_USER sudo "$INSTALLER" \
    --skip-build --build-dir="$stub_dir" \
    --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca="$root_ca_src" --account=alice \
    --machine-name=test-machine-1 \
    --key-dir="$unset_sudo_user_key_dir" --install-dir="$unset_sudo_user_install_dir" \
    --config-path="$unset_sudo_user_config_path" --launch-daemons-dir="$unset_sudo_user_launchd_dir" \
    --socket-dir="$unset_sudo_user_socket_dir" --daemon-log="$daemon_log" 2>&1)
rc_no_sudo_user=$?
assert_rc 2 "$rc_no_sudo_user" "with no --repo and no SUDO_USER, the install refuses rather than resolving some other account's or root's own home"
assert_contains "$out_no_sudo_user" "no SUDO_USER" "and it names what was missing"
assert_contains "$out_no_sudo_user" "pass --repo" "and says how to work around it"
assert_eq "missing" "$([ -f "$unset_sudo_user_install_dir/DEPLOYED_COMMIT" ] && echo present || echo missing)" \
    "and nothing was installed"

rm -rf "$fake_root_home2" "$(dirname "$unset_sudo_user_install_dir")" \
    "$(dirname "$unset_sudo_user_config_path")" "$(dirname "$unset_sudo_user_launchd_dir")" \
    "$(dirname "$unset_sudo_user_socket_dir")" "$(dirname "$unset_sudo_user_key_dir")"

echo "== under sudo with no --repo and SUDO_USER=root: refused, rather than resolving root's own checkout as the invoking operator's =="
fake_root_home3=$(mktemp -d)
sudo_user_root_install_dir=$(mktemp -d)/opt
sudo_user_root_config_path=$(mktemp -d)/config.json
sudo_user_root_launchd_dir=$(mktemp -d)/LaunchDaemons
sudo_user_root_socket_dir=$(mktemp -d)/individuator-daemon
sudo_user_root_key_dir=$(mktemp -d)/keys

out_sudo_user_root=$(SUDO_USER=root HOME="$fake_root_home3" PATH="$sudo_dir:$PATH" sudo "$INSTALLER" \
    --skip-build --build-dir="$stub_dir" \
    --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
    --root-ca="$root_ca_src" --account=alice \
    --machine-name=test-machine-1 \
    --key-dir="$sudo_user_root_key_dir" --install-dir="$sudo_user_root_install_dir" \
    --config-path="$sudo_user_root_config_path" --launch-daemons-dir="$sudo_user_root_launchd_dir" \
    --socket-dir="$sudo_user_root_socket_dir" --daemon-log="$daemon_log" 2>&1)
rc_sudo_user_root=$?
assert_rc 2 "$rc_sudo_user_root" "with no --repo and SUDO_USER=root, the install refuses rather than resolving root's own checkout"
assert_contains "$out_sudo_user_root" "SUDO_USER=root" "and it names what was refused"
assert_contains "$out_sudo_user_root" "pass --repo" "and says how to work around it"
assert_eq "missing" "$([ -f "$sudo_user_root_install_dir/DEPLOYED_COMMIT" ] && echo present || echo missing)" \
    "and nothing was installed"

rm -rf "$fake_root_home3" "$(dirname "$sudo_user_root_install_dir")" \
    "$(dirname "$sudo_user_root_config_path")" "$(dirname "$sudo_user_root_launchd_dir")" \
    "$(dirname "$sudo_user_root_socket_dir")" "$(dirname "$sudo_user_root_key_dir")"

# --- round-1 revise fixtures (PR 1119): passing --config alone (f33f3d9e6)
# moves which FILE is read but not whose home `~` expansion, the default
# root and the "too wide" guard resolve against — each still fell back to
# the CALLING (root) process's own home. The fix threads --home through too.
# Every case below shares one fixture: the SUDO_USER's own checkout lives at
# $tilde_user_home/builder/agent-platform (the compiled-in default root,
# company/decisions/0008), so a value that falls back and a value that
# resolves it directly land at the SAME repo, and one assertion shape covers
# both. Each case must fail against f33f3d9e6 for the reason named. --------
echo "== under sudo with no --repo: a configured CREWS_ROOT and an unconfigured one each resolve under SUDO_USER's home, never the fake root home =="
tilde_user_name="installtildeop"
tilde_user_home=$(mktemp -d)
tilde_user_repo="$tilde_user_home/builder/agent-platform"
make_repo_at "$tilde_user_repo"
tilde_user_sha=$(git -C "$tilde_user_repo" rev-parse main)
mkdir -p "$tilde_user_home/.config/convivy"

tilde_dscl_dir=$(mktemp -d)
cat > "$tilde_dscl_dir/dscacheutil" <<EOF
#!/bin/bash
if [ "\$1" = "-q" ] && [ "\$2" = "user" ] && [ "\$3" = "-a" ] && [ "\$4" = "name" ] && [ "\$5" = "$tilde_user_name" ]; then
    echo "name: $tilde_user_name"
    echo "dir: $tilde_user_home"
else
    exit 1
fi
EOF
chmod +x "$tilde_dscl_dir/dscacheutil"

# run_tilde_case <session-agent.env content> -> sets TILDE_RC, TILDE_OUT,
# TILDE_DEPLOYED (DEPLOYED_COMMIT's contents) and TILDE_FAKE_HOME (the
# per-case fake /var/root stand-in, for the "never resolved there" assertion),
# against a FRESH fake root HOME and install dir each call.
run_tilde_case() {
    local config_content="$1"
    local idir cpath ldir sdir kdir fake_home
    idir=$(mktemp -d)/opt
    cpath=$(mktemp -d)/config.json
    ldir=$(mktemp -d)/LaunchDaemons
    sdir=$(mktemp -d)/individuator-daemon
    kdir=$(mktemp -d)/keys
    fake_home=$(mktemp -d)
    printf '%s' "$config_content" > "$tilde_user_home/.config/convivy/session-agent.env"
    TILDE_OUT=$(SUDO_USER="$tilde_user_name" HOME="$fake_home" \
        PATH="$tilde_dscl_dir:$sudo_dir:$PATH" sudo "$INSTALLER" \
        --skip-build --build-dir="$stub_dir" \
        --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
        --root-ca="$root_ca_src" --account=alice \
        --machine-name=test-machine-1 \
        --key-dir="$kdir" --install-dir="$idir" \
        --config-path="$cpath" --launch-daemons-dir="$ldir" \
        --socket-dir="$sdir" --daemon-log="$daemon_log" 2>&1)
    TILDE_RC=$?
    TILDE_DEPLOYED=$(cat "$idir/DEPLOYED_COMMIT" 2>/dev/null)
    TILDE_FAKE_HOME="$fake_home"
    rm -rf "$fake_home" "$(dirname "$idir")" "$(dirname "$cpath")" "$(dirname "$ldir")" \
        "$(dirname "$sdir")" "$(dirname "$kdir")"
}

# assert_tilde_resolved <label> — TILDE_RC/TILDE_OUT/TILDE_DEPLOYED/TILDE_FAKE_HOME
# from the run just made: installs cleanly, resolves to the SUDO_USER's own
# checkout, and never under that run's fake root home.
assert_tilde_resolved() {
    local label="$1"
    assert_rc 0 "$TILDE_RC" "$label: installs cleanly"
    [ "$TILDE_RC" -ne 0 ] && echo "$TILDE_OUT" >&2
    assert_contains "$TILDE_DEPLOYED" "installed_from: $tilde_user_repo" \
        "$label: REPO resolved under the SUDO_USER's own home"
    assert_contains "$TILDE_DEPLOYED" "commit: $tilde_user_sha" "$label: ...and that checkout's own commit"
    case "$TILDE_DEPLOYED" in
        *"$TILDE_FAKE_HOME"*) resolved_under_fake="yes" ;;
        *) resolved_under_fake="no" ;;
    esac
    assert_eq "no" "$resolved_under_fake" "$label: never resolved under the fake root home"
}

run_tilde_case "CREWS_ROOT=~/builder
"
assert_tilde_resolved "CREWS_ROOT=~/builder"

run_tilde_case "CREWS_ROOT=~
"
assert_tilde_resolved "CREWS_ROOT=~ (too wide: the account's own home)"

run_tilde_case ""
assert_tilde_resolved "no CREWS_ROOT line at all (falls back to the compiled-in default)"

run_tilde_case "CREWS_ROOT=$tilde_user_home
"
assert_tilde_resolved "CREWS_ROOT=\$SUDO_USER_HOME, written out in full rather than as ~ (the too-wide guard itself, not just ~ expansion)"

rm -rf "$tilde_user_home" "$tilde_dscl_dir"

echo "== under sudo with no --repo: SUDO_USER values outside the allowed character class are refused before dscacheutil is ever called =="
charclass_dscl_dir=$(mktemp -d)
cat > "$charclass_dscl_dir/dscacheutil" <<'EOF'
#!/bin/bash
echo "dscacheutil must not be called for an invalid SUDO_USER" >&2
exit 1
EOF
chmod +x "$charclass_dscl_dir/dscacheutil"

for bad_sudo_user in "a/slash" "a space" "-leading-dash" "shell;metachar"; do
    cc_fake_home=$(mktemp -d)
    cc_install_dir=$(mktemp -d)/opt
    cc_config_path=$(mktemp -d)/config.json
    cc_launchd_dir=$(mktemp -d)/LaunchDaemons
    cc_socket_dir=$(mktemp -d)/individuator-daemon
    cc_key_dir=$(mktemp -d)/keys
    out_cc=$(SUDO_USER="$bad_sudo_user" HOME="$cc_fake_home" \
        PATH="$charclass_dscl_dir:$sudo_dir:$PATH" sudo "$INSTALLER" \
        --skip-build --build-dir="$stub_dir" \
        --bosun-host=bosun-test --bosun-address=127.0.0.1:8710 \
        --root-ca="$root_ca_src" --account=alice \
        --machine-name=test-machine-1 \
        --key-dir="$cc_key_dir" --install-dir="$cc_install_dir" \
        --config-path="$cc_config_path" --launch-daemons-dir="$cc_launchd_dir" \
        --socket-dir="$cc_socket_dir" --daemon-log="$daemon_log" 2>&1)
    rc_cc=$?
    assert_rc 2 "$rc_cc" "SUDO_USER=$bad_sudo_user is refused"
    assert_contains "$out_cc" "SUDO_USER is not a valid account name" "and it names why"
    case "$out_cc" in
        *"dscacheutil must not be called"*) dscacheutil_ran="yes" ;;
        *) dscacheutil_ran="no" ;;
    esac
    assert_eq "no" "$dscacheutil_ran" "and the account-database lookup never ran"
    assert_eq "missing" "$([ -f "$cc_install_dir/DEPLOYED_COMMIT" ] && echo present || echo missing)" \
        "and nothing was installed"
    rm -rf "$cc_fake_home" "$(dirname "$cc_install_dir")" "$(dirname "$cc_config_path")" \
        "$(dirname "$cc_launchd_dir")" "$(dirname "$cc_socket_dir")" "$(dirname "$cc_key_dir")"
done
rm -rf "$charclass_dscl_dir"

rm -rf "$repo" "$stub_dir" "$sudo_dir" "$(dirname "$key_dir")" "$(dirname "$install_dir")" \
    "$(dirname "$config_path")" "$(dirname "$launchd_dir")" "$(dirname "$socket_dir")" \
    "$(dirname "$daemon_log")" "$(dirname "$missing_log")" "$(dirname "$fresh_config_path")" \
    "$root_ca_dir" "$STUB_LOG"

finish
