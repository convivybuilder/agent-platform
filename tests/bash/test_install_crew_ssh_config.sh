#!/bin/bash
# install-crew-ssh-config.sh runs on the LAPTOP and writes ssh aliases, mosh
# launchers and Deck's rail, all naming directories the crew machine opens. So
# the root in them is that machine's, read over ssh from its deploy copy of
# builder-root (company/decisions/0008), never this laptop's layout or a
# literal path. The crew-host/standby-host/ssh-user/crew-list/style content is
# never a literal here either: it comes from a synthetic operator config and a
# synthetic rendered hosts.conf/mosh-sessions.conf, standing in for what
# bin/render-ssh-config writes on the crew machine (CQ-603). These run it end
# to end under a fake HOME with stub `hostname` and `ssh`, so nothing real is
# touched and no network is used. Invented crew names only ("widget") — no
# registered name.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/tools/install-crew-ssh-config.sh"
FAKE_ROOT="/tmp/fakeroot/builder"
CREW_HOST_NAME="crew.example"
STANDBY_HOST_NAME="standby.example"

# setup <crew-host-answer> [crew-host-rc] [laptop-tailnet-host] -> echoes fake HOME.
# A realistic answer (one with an account-home shape, >=3 path segments — true
# of $FAKE_ROOT, never of the refusal-case answers below) also seeds the
# rendered ssh/mosh fixture (CQ-603) at the mounted-volume path this laptop
# reads over the SMB mount CONVIVY_MINI_VOLUME_ROOT (set by run_installer,
# below) stands in for — mirroring what bin/render-ssh-config would have
# written on the crew machine. laptop_host, when given, is this laptop's OWN
# operator-config crew host, used to bootstrap the first ssh call; it
# defaults to CREW_HOST_NAME (the ordinary case: both machines agree).
setup() {
    local home crew_host_rc laptop_host user mount
    crew_host_rc=${2:-0}
    laptop_host=${3-$CREW_HOST_NAME}
    home=$(mktemp -d)
    mkdir -p "$home/stubbin"
    printf '#!/bin/sh\necho laptop\n' > "$home/stubbin/hostname"
    cat > "$home/stubbin/ssh" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$home/ssh.log"
printf '%s\n' '$1'
exit $crew_host_rc
EOF
    chmod +x "$home/stubbin/hostname" "$home/stubbin/ssh"
    mkdir -p "$home/.config/convivy"
    cat > "$home/.config/convivy/operator.json" <<EOF
{"tailnet_host": "$laptop_host"}
EOF
    case "$1" in
      /*/*/*)
        user=$(printf '%s\n' "$1" | cut -d/ -f3)
        mount="$home/mnt/$user/.local/state/agent-platform/co-style"
        mkdir -p "$mount/deck" "$mount/iterm" "$mount/ssh"
        printf '{"appearance": "system"}' > "$mount/deck/theme.json"
        cat > "$mount/ssh/hosts.conf" <<'HOSTS'
# Managed by agent-platform tools/install-crew-ssh-config.sh —
Host mini widget standby crew.example standby.example
  User op
  IdentityFile ~/.ssh/id_ed25519_mini
  IdentitiesOnly yes
  ServerAliveInterval 60
  ServerAliveCountMax 3

Host mini
  HostName crew.example

Host widget
  HostName crew.example
  RequestTTY yes
  RemoteCommand printf '\033]1337;SetProfile=co-widget\a'; ~/.local/bin/session-agent ensure-server && (tmux attach -d -t widget || tmux new -s widget -c @MINI_ROOT@/widget)
Host standby
  HostName standby.example
HOSTS
        cat > "$mount/ssh/mosh-sessions.conf" <<'MOSH'
widget|co-widget|widget|@MINI_ROOT@/widget
MOSH
        ;;
    esac
    echo "$home"
}

# setup_with_standby <crew-answer> <standby-answer> [standby-rc] -> echoes fake
# HOME. Builds on setup() (crew-host query always answers <crew-answer>, rc 0)
# and additionally seeds ssh/standby-host and ssh/standby-sessions.conf under
# the mounted fixture — what bin/render-ssh-config would have written when a
# standby_alias/standby_sessions pair is configured on the crew machine (CQ-603)
# — and gives the stub ssh script a second branch: a query naming the standby
# host (its own `-o HostName=<host>` argument) answers with <standby-answer>
# and exits <standby-rc>, independently of the crew-host query.
setup_with_standby() {
    local home standby_answer standby_rc user mount
    standby_answer=$2
    standby_rc=${3:-0}
    home=$(setup "$1" 0)
    user=$(printf '%s\n' "$1" | cut -d/ -f3)
    mount="$home/mnt/$user/.local/state/agent-platform/co-style"
    printf '%s\n' "$STANDBY_HOST_NAME" > "$mount/ssh/standby-host"
    cat > "$mount/ssh/standby-sessions.conf" <<'STANDBYSESS'
Host standby-widget
  HostName standby.example
  RequestTTY yes
  RemoteCommand printf '\033]1337;SetProfile=co-widget\a'; ~/.local/bin/session-agent ensure-server && (tmux attach -d -t widget || tmux new -s widget -c @STANDBY_ROOT@/widget)
STANDBYSESS
    cat > "$home/stubbin/ssh" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$home/ssh.log"
case "\$*" in
  *"HostName=$STANDBY_HOST_NAME"*)
    printf '%s\n' '$standby_answer'
    exit $standby_rc
    ;;
  *)
    printf '%s\n' '$1'
    exit 0
    ;;
esac
EOF
    chmod +x "$home/stubbin/ssh"
    echo "$home"
}

run_installer() { # <home> -> sets RC, OUT
    # CONVIVY_OPERATOR_CONFIG is cleared, overriding lib.sh's own export of it
    # for the rest of this bash suite (a shared fixture naming a different
    # host), so this script's python3 reads reach the per-test operator.json
    # written under $1 instead.
    OUT=$(cd "$1" && HOME="$1" PATH="$1/stubbin:$PATH" CONVIVY_MINI_VOLUME_ROOT="$1/mnt" \
        CONVIVY_OPERATOR_CONFIG="" bash "$SCRIPT" 2>&1)
    RC=$?
}

echo "== the laptop installs the rendered ssh blocks, substituting its own live root =="
H=$(setup "$FAKE_ROOT" 0)
run_installer "$H"
assert_rc 0 "$RC" "the installer exits 0 when the crew machine answers"
managed="$H/.ssh/convivy-mini-tmux.conf"
assert_contains "$(cat "$H/ssh.log")" "builder-root --root" \
    "the root is read from the crew machine's builder-root"
assert_contains "$(cat "$H/ssh.log")" "HostName=$CREW_HOST_NAME" \
    "the root is read from the configured crew host"
assert_contains "$(cat "$managed")" "tmux new -s widget -c $FAKE_ROOT/widget" \
    "the widget alias opens under the crew machine's root"
assert_eq 0 "$(grep -c '@MINI_ROOT@' "$managed")" "no placeholder survives into the ssh config"
assert_eq 0 "$(grep -c "$HOME" "$managed")" "no literal account path is written"
mosh="$H/.convivy-mosh-sessions.zsh"
assert_contains "$(cat "$mosh")" "-c $FAKE_ROOT/widget" "the mosh launcher opens under the crew machine's root"
assert_eq 0 "$(grep -c '@MINI_ROOT@' "$mosh")" "no placeholder survives into the mosh launchers"
deck="$H/.config/convivy/deck/theme.json"
assert_eq 0 "$(grep -c '@MINI_ROOT@' "$deck")" "no placeholder reaches Deck's rail"
rm -rf "$H"

echo "== the rendered standby alias installs unchanged =="
H=$(setup "$FAKE_ROOT" 0)
run_installer "$H"
managed="$H/.ssh/convivy-mini-tmux.conf"
assert_contains "$(cat "$managed")" "Host standby" "the standby alias is installed as rendered"
assert_contains "$(cat "$managed")" "HostName $STANDBY_HOST_NAME" "the standby alias names the rendered standby host"
rm -rf "$H"

echo "== a crew machine that cannot answer is refused before anything is written =="
H=$(setup "" 255)
run_installer "$H"
assert_rc 1 "$RC" "the installer exits 1"
assert_contains "$OUT" "Cannot read the root on $CREW_HOST_NAME" "and says why"
assert_eq "0" "$([ -e "$H/.ssh/convivy-mini-tmux.conf" ] && echo 1 || echo 0)" \
    "no ssh config is written"
assert_eq "0" "$([ -e "$H/.convivy-mosh-sessions.zsh" ] && echo 1 || echo 0)" \
    "no mosh launchers are written"
rm -rf "$H"

echo "== no local crew-host config at all refuses before any ssh call is made =="
H=$(mktemp -d)
mkdir -p "$H/stubbin"
printf '#!/bin/sh\necho laptop\n' > "$H/stubbin/hostname"
cat > "$H/stubbin/ssh" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$H/ssh.log"
exit 1
EOF
chmod +x "$H/stubbin/hostname" "$H/stubbin/ssh"
run_installer "$H"
assert_rc 1 "$RC" "the installer exits 1 with no operator config at all"
assert_contains "$OUT" "ERROR" "and names the operator-config read failure"
assert_eq "0" "$([ -e "$H/ssh.log" ] && echo 1 || echo 0)" "no ssh call is made"
rm -rf "$H"

echo "== a laptop configured with no crew host value refuses before any ssh call is made =="
H=$(setup "$FAKE_ROOT" 0 "")
run_installer "$H"
assert_rc 1 "$RC" "the installer exits 1"
assert_contains "$OUT" "No crew host configured" "and says why"
assert_eq "0" "$([ -e "$H/ssh.log" ] && echo 1 || echo 0)" "no ssh call is made"
rm -rf "$H"

# The guard refuses the configured crew host's own short name, never a literal
# server name — so it generalizes to whatever host this install's own
# operator config names.
echo "== a machine whose hostname matches the configured crew host is refused =="
H=$(setup "$FAKE_ROOT" 0)
printf '#!/bin/sh\necho %s\n' "$CREW_HOST_NAME" | sed 's/\..*//' > "$H/stubbin/hostname"
chmod +x "$H/stubbin/hostname"
run_installer "$H"
assert_rc 1 "$RC" "the installer exits 1"
assert_contains "$OUT" "Refusing to run" "and says why"
assert_eq "0" "$([ -e "$H/ssh.log" ] && echo 1 || echo 0)" "no ssh call is made"
rm -rf "$H"

# Nothing rendered yet (a crew machine that has not run install.sh's ssh-render
# phase, or one whose own operator config is missing a crew host or ssh user)
# costs only the ssh/mosh content — the rest of the install still proceeds,
# same shape as a missing mount.
echo "== nothing rendered yet leaves a header-only managed file and no mosh launchers =="
H=$(mktemp -d)
mkdir -p "$H/stubbin"
printf '#!/bin/sh\necho laptop\n' > "$H/stubbin/hostname"
cat > "$H/stubbin/ssh" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$H/ssh.log"
printf '%s\n' '$FAKE_ROOT'
exit 0
EOF
chmod +x "$H/stubbin/hostname" "$H/stubbin/ssh"
mkdir -p "$H/.config/convivy"
printf '{"tailnet_host": "%s"}\n' "$CREW_HOST_NAME" > "$H/.config/convivy/operator.json"
run_installer "$H"
assert_rc 0 "$RC" "the installer still exits 0"
managed="$H/.ssh/convivy-mini-tmux.conf"
assert_contains "$(cat "$managed")" "nothing rendered yet" "the managed file says nothing was rendered"
mosh="$H/.convivy-mosh-sessions.zsh"
assert_eq "0" "$(grep -c '^[a-zA-Z_][a-zA-Z0-9_-]*() {' "$mosh")" "no mosh launcher function is written"
rm -rf "$H"

# Every ssh alias and mosh launcher the render provides runs `session-agent
# ensure-server` before `tmux attach || tmux new`, exactly as rendered. Each
# generated command runs under sh with stub session-agent and tmux, so the
# order and the stop are observed, not read off the text. The stub
# session-agent sits at $HOME/.local/bin, where the ~ in the command resolves
# under the fake HOME.
echo "== the installed alias and mosh launcher check the server before attach or create =="
H=$(setup "$FAKE_ROOT" 0)
run_installer "$H"
managed="$H/.ssh/convivy-mini-tmux.conf"
mosh="$H/.convivy-mosh-sessions.zsh"
mkdir -p "$H/.local/bin"
cat > "$H/.local/bin/session-agent" <<EOF
#!/bin/sh
echo "session-agent \$*" >> "$H/cmd.log"
exit \${ENSURE_RC:-0}
EOF
cat > "$H/stubbin/tmux" <<EOF
#!/bin/sh
echo "tmux \$*" >> "$H/cmd.log"
[ "\$1" = attach ] && exit 1
exit 0
EOF
chmod +x "$H/.local/bin/session-agent" "$H/stubbin/tmux"

ssh_cmd=$(sed -n 's/^  RemoteCommand //p' "$managed" | head -n1)
mosh_cmd=$(sed -n "s/^  mosh $CREW_HOST_NAME -- sh -c '\(.*\)'\$/\1/p" "$mosh" | head -n1)
assert_contains "$ssh_cmd" "session-agent ensure-server && (tmux attach" \
    "the rendered RemoteCommand runs ensure-server before attach or create"
assert_contains "$mosh_cmd" "session-agent ensure-server && (tmux attach" \
    "the mosh launcher runs ensure-server before attach or create"

: > "$H/cmd.log"
(cd "$H" && HOME="$H" PATH="$H/stubbin:$PATH" ENSURE_RC=1 sh -c "$ssh_cmd") >/dev/null 2>&1
assert_eq "session-agent ensure-server" "$(cat "$H/cmd.log")" \
    "ssh: a refused check stops before any tmux call"
: > "$H/cmd.log"
(cd "$H" && HOME="$H" PATH="$H/stubbin:$PATH" ENSURE_RC=0 sh -c "$ssh_cmd") >/dev/null 2>&1
assert_eq "session-agent ensure-server" "$(head -n 1 "$H/cmd.log")" \
    "ssh: the check is the first thing run"
assert_contains "$(cat "$H/cmd.log")" "tmux new -s widget -c " "ssh: a passing check reaches the create"
rm -rf "$H"

# The co-styling files install.sh renders on the crew machine (CQ-590) install
# the same way the old committed iterm/co-*.json and deck/theme.json entries
# did: profiles copied atomically (temp + rename, never a symlink), the Deck
# seed merged key-by-key.
echo "== the laptop installs the rendered iTerm profile and Deck theme seed =="
H=$(setup "$FAKE_ROOT" 0)
user=$(printf '%s\n' "$FAKE_ROOT" | cut -d/ -f3)
mount="$H/mnt/$user/.local/state/agent-platform/co-style"
cat > "$mount/iterm/co-widget.json" <<'EOF'
{"Profiles": [{"Name": "co-widget", "Guid": "co-widget", "Badge Text": "widget"}]}
EOF
cat > "$mount/deck/theme.json" <<'EOF'
{"appearance": "system", "sessions": {"widget": "indigo"}, "railNames": {"widget": "Widget Co"}}
EOF
# Pre-seed the laptop's own DynamicProfiles dir with stale content, to prove
# the install replaces it (the script's own atomic-install comment) rather
# than leaving it beside a new file.
mkdir -p "$H/Library/Application Support/iTerm2/DynamicProfiles"
echo "stale" > "$H/Library/Application Support/iTerm2/DynamicProfiles/co-widget.json"
run_installer "$H"
assert_rc 0 "$RC" "the installer exits 0"
installed_profile="$H/Library/Application Support/iTerm2/DynamicProfiles/co-widget.json"
assert_contains "$(cat "$installed_profile")" '"Badge Text": "widget"' \
    "the rendered profile replaces the stale one"
assert_eq 0 "$(find "$H/Library/Application Support/iTerm2" -name '.co-install.*' | wc -l | tr -d ' ')" \
    "no atomic-install temp file is left behind"
deck="$H/.config/convivy/deck/theme.json"
assert_contains "$(cat "$deck")" '"widget": "indigo"' \
    "the rendered Deck session theme reaches the installed config"
rm -rf "$H"

# The standby session block runs in the STANDBY machine's own root, read live
# over ssh exactly as the crew root is (CQ-603) — never the crew machine's.
FAKE_STANDBY_ROOT="/tmp/fakeroot/standby"

# The guard refuses the rendered standby host's own short name too — read out
# of the mounted ssh/standby-host file, same as install-crew-ssh-config.sh
# reads it, never a literal server name — so a laptop whose crew-host query
# succeeds first still gets refused before the managed files are written.
echo "== a machine whose hostname matches the rendered standby host is refused =="
H=$(setup_with_standby "$FAKE_ROOT" "$FAKE_STANDBY_ROOT")
printf '#!/bin/sh\necho %s\n' "$STANDBY_HOST_NAME" | sed 's/\..*//' > "$H/stubbin/hostname"
chmod +x "$H/stubbin/hostname"
run_installer "$H"
assert_rc 1 "$RC" "the installer exits 1"
assert_contains "$OUT" "Refusing to run" "and says why"
assert_eq "0" "$([ -e "$H/.ssh/convivy-mini-tmux.conf" ] && echo 1 || echo 0)" \
    "no ssh config is written"
assert_eq "0" "$([ -e "$H/.convivy-mosh-sessions.zsh" ] && echo 1 || echo 0)" \
    "no mosh launchers are written"
rm -rf "$H"

echo "== the standby session block installs under the standby machine's own root =="
H=$(setup_with_standby "$FAKE_ROOT" "$FAKE_STANDBY_ROOT")
run_installer "$H"
assert_rc 0 "$RC" "the installer exits 0 when both roots answer"
managed="$H/.ssh/convivy-mini-tmux.conf"
assert_contains "$(cat "$H/ssh.log")" "HostName=$STANDBY_HOST_NAME" \
    "the standby root is read from the configured standby host"
assert_contains "$(cat "$managed")" "Host standby-widget" "the standby session block is installed"
assert_contains "$(cat "$managed")" "tmux new -s widget -c $FAKE_STANDBY_ROOT/widget" \
    "the standby session alias opens under the STANDBY machine's root, not the crew machine's"
assert_eq 0 "$(grep -c '@STANDBY_ROOT@' "$managed")" "no standby placeholder survives into the ssh config"
assert_contains "$(cat "$managed")" "tmux new -s widget -c $FAKE_ROOT/widget" \
    "the ordinary widget alias is still installed, under the crew machine's root"
rm -rf "$H"

# Break the code: if a failed standby root read aborted the whole install
# instead of skipping only the standby block, this would fail (RC != 0, or
# the crew alias/mosh launchers would be missing).
echo "== a standby root that cannot be read skips only the standby session block =="
H=$(setup_with_standby "$FAKE_ROOT" "" 255)
run_installer "$H"
assert_rc 0 "$RC" "the installer still exits 0 — the standby machine being down does not stop the install"
managed="$H/.ssh/convivy-mini-tmux.conf"
assert_contains "$OUT" "Skipping the standby session block" "and says why, on stderr"
assert_eq 0 "$(grep -c 'standby-widget' "$managed")" "no standby session block is installed"
assert_eq 0 "$(grep -c '@STANDBY_ROOT@' "$managed")" "no unsubstituted standby placeholder is left behind"
assert_contains "$(cat "$managed")" "tmux new -s widget -c $FAKE_ROOT/widget" \
    "the ordinary widget alias still installs"
mosh="$H/.convivy-mosh-sessions.zsh"
assert_contains "$(cat "$mosh")" "-c $FAKE_ROOT/widget" "the mosh launcher still installs"
rm -rf "$H"

finish
