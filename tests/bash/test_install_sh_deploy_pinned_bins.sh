#!/bin/bash
# tests/bash/test_install_sh_deploy_pinned_bins.sh — install.sh's phase-2 bin/
# symlink loop must never create ~/.local/bin/<name> for a DEPLOY_PINNED_BINS
# daemon (platform/decisions/0059 rules 1, 2): run-register-api and
# bosun-server each ship their own deploy-pinned installer
# (bin/install-run-register-api, bin/install-bosun-server) and are read from
# their own ~/.local/deploy/<name>/ area, never from a symlink into this
# checkout — a routine re-install recreating that symlink is the stale trap
# a plist edited by hand later could get re-pointed at. #891 round 1 found
# bosun-server missing from the list entirely, so a routine install.sh run
# would have (re)created exactly that symlink.
#
# Hermetic and fast on purpose: no git, no clone, no venv/pip over the
# network, unlike tests/bash/test_install_new_plists.sh's full end-to-end
# run. $REPO here is a from-scratch directory holding only the bin/ fixture
# install.sh's phase-2 bin/ loop reads — nothing else is needed to exercise
# it, and the assertions below are all about $LOCAL_BIN, written before that
# loop ends. install.sh is then allowed to die on its NEXT line
# (lib/link-farm.sh does not exist in this tiny fixture) without touching
# anything these assertions depend on.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

# A stub knowledge venv: phase 1 skips `python3.11 -m venv` (the dir already
# exists) and its two `pip install` calls hit this no-op instead of the
# network — the same seam test_install_bosun_server.sh's make_stub_venv_bin
# uses for its own installer.
_seed_stub_knowledge_venv() { # <home>
    mkdir -p "$1/.local/lib/knowledge/.venv/bin"
    printf '#!/bin/sh\nexit 0\n' > "$1/.local/lib/knowledge/.venv/bin/pip"
    chmod +x "$1/.local/lib/knowledge/.venv/bin/pip"
    printf '#!/bin/sh\necho stub-python3\n' > "$1/.local/lib/knowledge/.venv/bin/python3"
    chmod +x "$1/.local/lib/knowledge/.venv/bin/python3"
}

# A from-scratch $HOME/repo with only what phase 2's bin/ loop reads: one
# normal tool (agentctl, standing in for any interactive command) and the
# two deploy-pinned daemon binaries.
setup() { # -> echoes fake HOME, with $HOME/repo seeded and $HOME/.local/bin ready
    local home
    home=$(mktemp -d)
    mkdir -p "$home/repo/bin" "$home/.local/bin"
    cp "$REPO_ROOT/install.sh" "$home/repo/install.sh"
    printf '#!/bin/sh\necho agentctl\n' > "$home/repo/bin/agentctl"
    printf '#!/bin/sh\necho run-register-api\n' > "$home/repo/bin/run-register-api"
    printf '#!/bin/sh\necho bosun-server\n' > "$home/repo/bin/bosun-server"
    chmod +x "$home/repo/bin/agentctl" "$home/repo/bin/run-register-api" "$home/repo/bin/bosun-server"
    _seed_stub_knowledge_venv "$home"
    echo "$home"
}

# Runs install.sh from the fixture repo. The run is expected to die partway
# through phase 2 (this fixture has no lib/link-farm.sh for install.sh's next
# line to exec) — that failure lands strictly AFTER the bin/ loop this suite
# is about, so it is deliberately ignored here rather than asserted on.
run_install() { # <home>
    HOME="$1" bash "$1/repo/install.sh" >"$1/out.log" 2>&1 || true
}

# --- 1. a from-scratch install creates NOTHING at ~/.local/bin for either
#        deploy-pinned daemon, while a normal tool is symlinked as usual -----
H=$(setup)
run_install "$H"

assert_eq "0" "$(test -e "$H/.local/bin/run-register-api" && echo 1 || echo 0)" \
    "a fresh install creates no ~/.local/bin/run-register-api at all"
assert_eq "0" "$(test -e "$H/.local/bin/bosun-server" && echo 1 || echo 0)" \
    "a fresh install creates no ~/.local/bin/bosun-server at all (#891 round 1: this was missing)"
assert_eq "1" "$(test -L "$H/.local/bin/agentctl" && echo 1 || echo 0)" \
    "a normal tool is still symlinked, so the loop itself is not broken"
assert_eq "$H/repo/bin/agentctl" "$(readlink "$H/.local/bin/agentctl")" \
    "...and it points at the checkout, as every interactive bin does (rule 5)"
rm -rf "$H"

# --- 2. a STALE symlink left by a prior install (before either daemon was
#        carved out, or on a machine installed before this fix) is REMOVED,
#        not recreated -------------------------------------------------------
H=$(setup)
ln -sfn "$H/repo/bin/run-register-api" "$H/.local/bin/run-register-api"
ln -sfn "$H/repo/bin/bosun-server" "$H/.local/bin/bosun-server"
run_install "$H"

assert_eq "0" "$(test -e "$H/.local/bin/run-register-api" && echo 1 || echo 0)" \
    "a stale run-register-api symlink into the checkout is removed, not refreshed"
assert_eq "0" "$(test -e "$H/.local/bin/bosun-server" && echo 1 || echo 0)" \
    "a stale bosun-server symlink into the checkout is removed, not refreshed"
assert_contains "$(cat "$H/out.log")" "Removing stale symlink" \
    "and the removal is announced (platform/decisions/0059 rule 1)"
rm -rf "$H"

# --- 3. a symlink pointing somewhere OTHER than this checkout (an operator's
#        own, or a different deploy area) is left alone -----------------------
H=$(setup)
elsewhere=$(mktemp -d)
printf '#!/bin/sh\necho elsewhere\n' > "$elsewhere/bosun-server"
chmod +x "$elsewhere/bosun-server"
ln -sfn "$elsewhere/bosun-server" "$H/.local/bin/bosun-server"
run_install "$H"

assert_eq "1" "$(test -L "$H/.local/bin/bosun-server" && echo 1 || echo 0)" \
    "a symlink pointing outside the checkout survives — this loop only removes ITS OWN stale case"
assert_eq "$elsewhere/bosun-server" "$(readlink "$H/.local/bin/bosun-server")" \
    "...and its target is unchanged"
rm -rf "$H" "$elsewhere"

finish
