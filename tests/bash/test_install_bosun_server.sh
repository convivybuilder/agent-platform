#!/bin/bash
# Tests for bin/install-bosun-server — the deploy-pin installer for the
# com.convivy.bosun-server launchd job (platform/decisions/0059, E12 stage
# one). Mirrors tests/bash/test_install_run_register_api.sh's fixture shape:
# every run is hermetic, HOME points at a tmp dir, and the source repo is a
# from-scratch fixture (not this checkout).
#
# `python3.11 -m venv` + `pip install` is skipped via --skip-venv/
# --venv-bin-dir, the same test seam install-individuator's own suite uses
# for `swift build` (--skip-build/--build-dir) — no test here needs network
# egress to PyPI. `launchctl` is tests/bash/lib.sh's own global stub, which
# refuses bootstrap/bootout loudly; the installer treats that as non-fatal
# (same as install-individuator), so these tests never touch real launchd.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

INSTALLER="$REPO_ROOT/bin/install-bosun-server"
PLIST_NAME="com.convivy.bosun-server.plist"

_git_ident() { # <repo>
    git -C "$1" config user.email t@t
    git -C "$1" config user.name t
    git -C "$1" config commit.gpgsign false
}

# A stub python3: never actually invoked (nothing in these tests starts the
# daemon), just an executable file at venv/bin/python3 for the installer to
# copy in place of a real `python3.11 -m venv` build.
make_stub_venv_bin() { # -> echoes a dir containing a stub python3
    local dir
    dir=$(mktemp -d)
    printf '#!/bin/sh\nexit 0\n' > "$dir/python3"
    chmod +x "$dir/python3"
    echo "$dir"
}

# Build a fixture repo with exactly the files the installer needs, committed
# to a branch literally named "main". The plist is the REAL template from
# this checkout, not a hand-copy, so a change to the real file is what this
# suite exercises.
_seed_repo() { # <path> -> commits on main
    local repo=$1
    mkdir -p "$repo/bin" "$repo/lib/agent-platform" "$repo/launchd"
    git -C "$repo" init -q
    git -C "$repo" symbolic-ref HEAD refs/heads/main
    _git_ident "$repo"
    printf '#!/usr/bin/env python3\nprint("v1")\n' > "$repo/bin/bosun-server"
    chmod +x "$repo/bin/bosun-server"
    for dep in bosun_registration.py bosun_authz.py builder_root.py vault_plane.py; do
        echo "# $dep" > "$repo/lib/agent-platform/$dep"
    done
    printf 'cryptography==50.0.1\n' > "$repo/requirements.txt"
    cp "$REPO_ROOT/launchd/$PLIST_NAME" "$repo/launchd/$PLIST_NAME"
    git -C "$repo" add -A
    git -C "$repo" commit -qm seed
}

setup() { # -> echoes fake HOME, with $HOME/src-repo seeded on main
    local home
    home=$(mktemp -d)
    _seed_repo "$home/src-repo"
    echo "$home"
}

DEPLOY="/.local/deploy/bosun-server"

# --- 0. the TEMPLATE in this checkout carries KeepAlive but none of the
#        three certificate/host keys (ARCH-342: they are operator-chosen at
#        install time, so they live in ~/.config/convivy/bosun-server.env,
#        never in a plist install.sh copies over the live one). Checked
#        directly against the real repo file, with no installer run. -------
tmpl=$(cat "$REPO_ROOT/launchd/$PLIST_NAME")
case "$tmpl" in
    *"<key>BOSUN_TLS_CERT</key>"*|*"<key>BOSUN_TLS_KEY</key>"*|*"<key>BOSUN_REGISTRATION_HOST</key>"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the template carries a certificate/key/host key (ARCH-342)" ;;
    *) _PASS=$((_PASS + 1));;
esac
assert_contains "$tmpl" "<key>KeepAlive</key>" "the template carries KeepAlive"
case "$tmpl" in
    *"<key>KeepAlive</key>"*"<true/>"*) _PASS=$((_PASS + 1));;
    *) _FAIL=$((_FAIL + 1)); echo "  FAIL: KeepAlive is not set to true";;
esac
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$REPO_ROOT/launchd/$PLIST_NAME" >/dev/null 2>&1
    assert_rc 0 "$?" "the template is well-formed XML (plutil)"
fi
# plutil -lint is lenient about "--" inside an XML comment (reads OK), but
# that sequence is forbidden anywhere in an XML comment, so xml.parsers.expat
# (what plistlib and launchd's own parser both use) rejects it. Run
# unconditionally, not only when plutil is absent, so this catches the class
# on the self-hosted macOS bash runner too, where plutil's leniency would
# otherwise mask it (#891: "--help" inside a comment passed plutil -lint
# clean while tests/test_scheduled_dispatch_origin.py's plistlib load failed).
python3 -c "import plistlib, sys; plistlib.load(open(sys.argv[1], 'rb'))" \
    "$REPO_ROOT/launchd/$PLIST_NAME" >/dev/null 2>&1
assert_rc 0 "$?" "the template is well-formed XML (plistlib's strict XML parser)"

# --- 1. the installer refuses without --tls-cert/--tls-key, naming why,
#        before doing any work (no --repo even needed to reach the refusal) -
out=$(HOME=/nonexistent "$INSTALLER" --repo=/nonexistent 2>&1); rc=$?
assert_rc 2 "$rc" "no --tls-cert/--tls-key at all: refused"
assert_contains "$out" "--tls-cert and --tls-key are both required" "and says why"

out=$(HOME=/nonexistent "$INSTALLER" --repo=/nonexistent --tls-cert=/tmp/cert.pem 2>&1); rc=$?
assert_rc 2 "$rc" "--tls-cert alone, missing --tls-key: still refused"

out=$(HOME=/nonexistent "$INSTALLER" --repo=/nonexistent --tls-key=/tmp/key.pem 2>&1); rc=$?
assert_rc 2 "$rc" "--tls-key alone, missing --tls-cert: still refused"

# --- 2. a fresh install deploys the code, pins a (stubbed) interpreter,
#        records the deployed commit, and renders the plist with the given
#        certificate/key and the DEFAULT (loopback) registration host -------
H=$(setup)
venv_bin=$(make_stub_venv_bin)
rc=0
HOME="$H" "$INSTALLER" --repo="$H/src-repo" --skip-venv --venv-bin-dir="$venv_bin" \
    --tls-cert=/etc/bosun/tls/server.crt --tls-key=/etc/bosun/tls/server.key \
    >"$H/out.log" 2>&1 || rc=$?
assert_rc 0 "$rc" "a fresh install exits 0"

assert_eq "1" "$(test -f "$H$DEPLOY/bin/bosun-server" && echo 1 || echo 0)" \
    "the daemon script is deployed"
assert_eq "0" "$(test -L "$H$DEPLOY/bin/bosun-server" && echo 1 || echo 0)" \
    "the deployed script is a real copy, not a symlink (rule 2)"
assert_contains "$(cat "$H$DEPLOY/bin/bosun-server")" "print(\"v1\")" \
    "the deployed script is main's committed content"
assert_eq "1" "$(test -x "$H$DEPLOY/venv/bin/python3" && echo 1 || echo 0)" \
    "an interpreter is staged inside the deploy area (rule 4)"

# bin/bosun-server's whole import closure must travel with it (the installer's
# own `git archive` list, above) — a deploy area missing one of these four
# fails to import at process start, not at review time.
for dep in bosun_registration.py bosun_authz.py builder_root.py vault_plane.py; do
    assert_eq "1" "$(test -f "$H$DEPLOY/lib/agent-platform/$dep" && echo 1 || echo 0)" \
        "$dep is deployed alongside the daemon script"
    assert_eq "0" "$(test -L "$H$DEPLOY/lib/agent-platform/$dep" && echo 1 || echo 0)" \
        "$dep is a real copy, not a symlink (rule 2)"
done

sha_main=$(git -C "$H/src-repo" rev-parse main)
assert_contains "$(cat "$H$DEPLOY/DEPLOYED_COMMIT")" "$sha_main" \
    "DEPLOYED_COMMIT names the commit actually installed"

plist_live="$H/Library/LaunchAgents/$PLIST_NAME"
assert_eq "1" "$(test -f "$plist_live" && echo 1 || echo 0)" \
    "the plist is copied into LaunchAgents"
assert_eq "0" "$(test -L "$plist_live" && echo 1 || echo 0)" \
    "the copied plist is a real file, not a symlink (rule 8)"

installed_plist=$(cat "$plist_live")
case "$installed_plist" in
    *"__HOME__"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: installed plist still carries a placeholder: not fully rendered" ;;
    *) _PASS=$((_PASS + 1));;
esac
assert_contains "$installed_plist" "$H/.local/deploy/bosun-server/venv/bin/python3" \
    "__HOME__ is rendered to this fake HOME"
case "$installed_plist" in
    *"<key>BOSUN_TLS_CERT</key>"*|*"<key>BOSUN_TLS_KEY</key>"*|*"<key>BOSUN_REGISTRATION_HOST</key>"*)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the installed plist carries a certificate/key/host key (ARCH-342)" ;;
    *) _PASS=$((_PASS + 1));;
esac

# --- the certificate, key and host land in the config file instead, mode
#     0600, never in the plist. ---------------------------------------------
env_file="$H/.config/convivy/bosun-server.env"
assert_eq "1" "$(test -f "$env_file" && echo 1 || echo 0)" \
    "the config file is written"
assert_eq "-rw-------" "$(ls -l "$env_file" | cut -c1-10)" \
    "the config file is mode 0600"
env_contents=$(cat "$env_file")
assert_contains "$env_contents" "BOSUN_TLS_CERT=/etc/bosun/tls/server.crt" \
    "--tls-cert reaches the config file"
assert_contains "$env_contents" "BOSUN_TLS_KEY=/etc/bosun/tls/server.key" \
    "--tls-key reaches the config file"
assert_contains "$env_contents" "BOSUN_REGISTRATION_HOST=127.0.0.1" \
    "--registration-host defaults to loopback, not the module's own 0.0.0.0 default"
rm -rf "$H" "$venv_bin"

# --- 3. --registration-host overrides the default -------------------------
H=$(setup)
venv_bin=$(make_stub_venv_bin)
HOME="$H" "$INSTALLER" --repo="$H/src-repo" --skip-venv --venv-bin-dir="$venv_bin" \
    --tls-cert=/etc/bosun/tls/server.crt --tls-key=/etc/bosun/tls/server.key \
    --registration-host=0.0.0.0 \
    >"$H/out.log" 2>&1
rc=$?
assert_rc 0 "$rc" "install with an explicit --registration-host exits 0"
assert_contains "$(cat "$H/.config/convivy/bosun-server.env")" "BOSUN_REGISTRATION_HOST=0.0.0.0" \
    "--registration-host overrides the loopback default"
rm -rf "$H" "$venv_bin"

# --- 4. a drifted live plist is never overwritten without --force, and IS
#        overwritten with --force (ARCH-342 idiom, mirrors install.sh phase 4
#        and install-run-register-api) ---------------------------------------
H=$(setup)
venv_bin=$(make_stub_venv_bin)
HOME="$H" "$INSTALLER" --repo="$H/src-repo" --skip-venv --venv-bin-dir="$venv_bin" \
    --tls-cert=/etc/bosun/tls/server.crt --tls-key=/etc/bosun/tls/server.key \
    >/dev/null 2>&1

plist_live="$H/Library/LaunchAgents/$PLIST_NAME"
echo "DRIFTED-BY-HAND" > "$plist_live"

out=$(HOME="$H" "$INSTALLER" --repo="$H/src-repo" --skip-venv --venv-bin-dir="$venv_bin" \
    --tls-cert=/etc/bosun/tls/server.crt --tls-key=/etc/bosun/tls/server.key \
    2>&1)
rc=$?
assert_rc 0 "$rc" "install still succeeds when the live plist has drifted"
assert_contains "$out" "NOT overwritten" "and it says the drifted plist was not overwritten"
assert_eq "DRIFTED-BY-HAND" "$(cat "$plist_live")" \
    "the drifted live plist survives a re-install without --force"

out=$(HOME="$H" "$INSTALLER" --repo="$H/src-repo" --skip-venv --venv-bin-dir="$venv_bin" \
    --tls-cert=/etc/bosun/tls/server.crt --tls-key=/etc/bosun/tls/server.key \
    --force 2>&1)
rc=$?
assert_rc 0 "$rc" "install with --force exits 0"
assert_contains "$(cat "$plist_live")" "<key>KeepAlive</key>" \
    "--force overwrites the drifted plist with the rendered template"
rm -rf "$H" "$venv_bin"

echo "  $_PASS passed, $_FAIL failed"
[ "$_FAIL" -eq 0 ]
