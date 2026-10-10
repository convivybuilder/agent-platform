#!/bin/bash
# scripts/ci/runner-isolation-probe.sh — CQ-417.
#
# Runs as a step of ci.yml's `bash` job, on the self-hosted macOS runner
# (the self-hosted runner on the Mac Studio, label `convivy-macos-bash:custom`),
# as the unprivileged launchd user ap-runner — the only way
# to exercise these checks as that user. Bash 3.2 (ARCH-082): no
# declare -A, no mapfile/readarray, no ${var,,}.
#
# Two things, in order:
#   a+b+c. Make one unauthenticated GET against every port CQ-417's work item
#          names (a FIXED list, below), on a route that serves data, so the
#          probe covers that list even on a host where enumeration sees
#          nothing. ALSO enumerate every TCP listener the host will show this
#          user (netstat and lsof, neither reliable alone on macOS — see
#          below) and GET "/" on any extra listener that enumeration finds
#          outside the fixed list. Every result reports ONLY port / route /
#          HTTP status / response byte count. A response body is NEVER
#          printed, saved, or uploaded: Selector's /status returns the
#          operator's Spotify name and listening history, and CI logs are not
#          a place user data may go. GET only, never POST/PUT/DELETE.
#   d.     Confirm ap-runner cannot reach the operator's own data: list
#          the operator's home directory, read the operator's `.claude`,
#          list /tmp/cc-socks, or connect to the local Postgres socket.
#   e.     Confirm the runner accounts are isolated from EACH OTHER, not only
#          from the operator: the job user's own home grants no group or
#          other permission, and no other runner's home (/Users/*-runner) is
#          readable or listable. Both runner homes were set up at mode 750,
#          group staff — every staff member, including the other runner
#          account, could read each one. Deck's own per-job credential check
#          caught this from its side and failed every Deck CI job
#          (convivy-deck#345) before the operator ran `chmod 700` on both
#          homes. The runner setup is manual steps, not code, so a reinstall
#          could reopen the gap with nobody watching; this probe is the
#          control that would catch it.
#
# EXIT STATUS REFLECTS ONLY (d) AND (e). The GET probes (a+b+c) are reported,
# not judged, by this script — it has no way to know which exposure is
# intended for a given service. Each line is read by the co and recorded on
# the CQ-417 work item as refused or open-by-design.

# -f (noglob): netstat's wildcard-bound rows print the literal address "*.PORT",
# and ports/routes below are iterated with plain unquoted word-splitting, so a
# stray "*" reaching one of those loops must stay a literal character rather
# than expand against this step's working directory.
set -uf
FAIL=0

echo "== runner isolation probe (CQ-417) =="
echo "host: $(hostname)  runner: ${RUNNER_NAME:-<unset>}  user: $(id -un)"
echo

# ---------------------------------------------------------------------------
# FIXED_PORTS: every port CQ-417's work item names for item 3, probed
# unconditionally below regardless of what enumeration sees. This is the fix
# for the round-1 defect: on this macOS version `netstat -anv -p tcp` run as
# ap-runner enumerated zero listeners while five of these ports were
# confirmed listening at the same moment by a different uid (issue comment,
# 2026-10-07) — enumeration is a visibility aid here, never the thing that
# decides what gets probed.
FIXED_PORTS="4000 8100 8099 8787 8000 5151 4100 8765 8300 8080 8081 20241 50406 8321 8322 8323 8324 8325 8326 8327 8328 8329 8330"

is_fixed_port() {
    case " $FIXED_PORTS " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# a. Enumerate every TCP listener this user can see, as an ADDITION to
#    FIXED_PORTS above, never a replacement for it.
#
# `lsof -iTCP -sTCP:LISTEN` run as a non-root user only sees sockets owned by
# that same user's own processes — macOS scopes lsof's cross-process fd view
# to root or a matching uid — so as ap-runner it reports almost nothing else
# on the box. `netstat` reads the kernel's protocol control block table
# directly and isn't scoped that way on most macOS versions, but on this
# runner's OS build it enumerated nothing at all for a non-root,
# non-interactive user even though real listeners existed — so neither tool
# is trusted alone; both run, and their results are unioned.
echo "-- TCP listeners (netstat -anv -p tcp + lsof -nP -iTCP -sTCP:LISTEN) --"
netstat_out=$(netstat -anv -p tcp 2>/dev/null)
# The row shape is "... Local-Address Foreign-Address LISTEN ...", so the
# local address is TWO fields before the literal state token "LISTEN", found
# by position rather than a fixed column number: -v adds columns (rexmt,
# pid, …) after the state whose exact count isn't worth pinning against the
# next time one is added upstream. The port is everything after the
# address's last '.' (netstat writes "host.port", including for IPv6).
netstat_ports=$(printf '%s\n' "$netstat_out" | awk '
    { for (i = 1; i <= NF; i++) {
          if ($i == "LISTEN") { n = split($(i-2), a, "."); print a[n]; break }
      }
    }')

lsof_out=$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null)
# lsof's NAME column is "addr:port" (or "*:port"), immediately followed by
# the literal "(LISTEN)" as the line's last field.
lsof_ports=$(printf '%s\n' "$lsof_out" | awk '
    /\(LISTEN\)$/ { n = split($(NF-1), a, ":"); print a[n] }')

ports=$(printf '%s\n%s\n' "$netstat_ports" "$lsof_ports" | grep -v '^$' | sort -un)

if [ -z "$ports" ]; then
    echo "  enumeration returned nothing (this tool cannot see other users' sockets here)"
else
    for p in $ports; do
        echo "  listening: $p"
    done
fi
echo

# ---------------------------------------------------------------------------
# route_for_port <port>: the GET route(s) (space-separated) to probe for a
# known/likely Convivy service on that port.
#
# Ports named explicitly in the CQ-417 work item use the route(s) it names
# (LiteLLM, Selector, cloudflared). Library and Orient use the real route
# this repo's own client code sends — lib/agent-platform/vault_plane.py and
# tests/test_vault_plane.py show the Library serves `/items`, Orient serves
# `/api/issues`. convivy-open's `/health` (tools/convivy-open/install-laptop.sh)
# is this repo's only other confirmed route, and covers :8765. Every other
# port below (Ask, CFO, convivy-services, the tennis app's 8321-8330,
# sous-chef, Docker, prisma) has no source in this repo to read a route
# from, so each is a GUESS, marked inline — `/health` where that convention
# is plausible elsewhere in this repo, else the service's own root.
route_for_port() {
    case "$1" in
        4000) echo "/v1/models /key/info" ;;  # LiteLLM, from the work item
        8100) echo "/items" ;;                 # Library, derived: vault_plane.py
        8099) echo "/api/issues" ;;            # Orient, derived: vault_plane.py
        8787) echo "/health" ;;                # Ask — GUESSED, no source in this repo
        8000) echo "/status" ;;                # Selector, from the work item
        5151) echo "/health" ;;                # CFO — GUESSED
        4100) echo "/health" ;;                # convivy-services — GUESSED
        8765) echo "/health" ;;                # convivy-open, derived: install-laptop.sh
        8300) echo "/health" ;;                # sous-chef — GUESSED
        8080|8081) echo "/version" ;;          # Docker — GUESSED (Engine API convention)
        20241) echo "/metrics" ;;              # cloudflared, from the work item
        50406) echo "/" ;;                     # prisma — GUESSED, no plausible non-root route found
        *)
            if [ "$1" -ge 8321 ] 2>/dev/null && [ "$1" -le 8330 ] 2>/dev/null; then
                echo "/health"                  # tennis app — GUESSED
            else
                echo "/"                        # unrecognised port — fallback
            fi
            ;;
    esac
}

# ---------------------------------------------------------------------------
# probe_one <port> <route>: one unauthenticated GET, reporting only
# port/route/status/bytes.
#
# -o /dev/null discards the body before anything in this script can see it;
# -w reports status/size from curl's own transfer accounting, never by
# reading the body back. No credentials, no headers, GET only (curl's
# default method, never -X/-d/-T), short timeouts so one dead service can't
# hang the step.
probe_one() {
    _p="$1"
    _route="$2"
    _result=$(curl -s -o /dev/null --max-time 5 --connect-timeout 2 \
        -w '%{http_code} %{size_download}' \
        "http://127.0.0.1:${_p}${_route}" 2>/dev/null)
    _rc=$?
    if [ "$_rc" -ne 0 ] || [ -z "$_result" ]; then
        echo "  port=$_p route=$_route status=refused(curl_rc=$_rc) bytes=0"
    else
        _status=${_result%% *}
        _bytes=${_result##* }
        echo "  port=$_p route=$_route status=$_status bytes=$_bytes"
    fi
}

# b+c. One unauthenticated GET per FIXED_PORTS entry on its real route,
# unconditionally — this is what makes item 3 run even when enumeration
# above sees nothing. Then one GET "/" on any extra listener enumeration
# found outside FIXED_PORTS.
echo "-- GET probes (status + byte count only; response body never shown) --"
echo "-- fixed ports (CQ-417 item 3), probed regardless of enumeration --"
for p in $FIXED_PORTS; do
    for route in $(route_for_port "$p"); do
        probe_one "$p" "$route"
    done
done

extra_ports=""
for p in $ports; do
    if ! is_fixed_port "$p"; then
        extra_ports="$extra_ports $p"
    fi
done
if [ -n "$extra_ports" ]; then
    echo "-- extra listeners found by enumeration, outside the fixed list (GET / only) --"
    for p in $extra_ports; do
        probe_one "$p" "/"
    done
fi
echo

# ---------------------------------------------------------------------------
# d. Isolation checks — the only checks this script's exit status reflects.
echo "-- isolation checks (must all refuse) --"

if [ -z "${OPERATOR_HOME:-}" ] || [ ! -d "$OPERATOR_HOME" ]; then
    echo "  FAIL: OPERATOR_HOME is unset or not a directory (set it to the operator's own home directory to run this check)"
    FAIL=1
else
    if ls "$OPERATOR_HOME" >/dev/null 2>&1; then
        echo "  FAIL: $OPERATOR_HOME is listable by ap-runner"
        FAIL=1
    else
        echo "  ok: $OPERATOR_HOME is not listable"
    fi

    if ls "$OPERATOR_HOME/.claude" >/dev/null 2>&1; then
        echo "  FAIL: $OPERATOR_HOME/.claude is readable by ap-runner"
        FAIL=1
    else
        echo "  ok: $OPERATOR_HOME/.claude is not readable"
    fi
fi

if ls /tmp/cc-socks >/dev/null 2>&1; then
    echo "  FAIL: /tmp/cc-socks is listable by ap-runner"
    FAIL=1
else
    echo "  ok: /tmp/cc-socks is not listable"
fi

# BSD nc's -U dials a unix-domain socket; -z probes without sending or
# reading any data (so this never reads a Postgres startup banner), -w caps
# how long a hung socket can hold the step.
if nc -zU -w 2 /tmp/.s.PGSQL.5432 >/dev/null 2>&1; then
    echo "  FAIL: connected to /tmp/.s.PGSQL.5432"
    FAIL=1
else
    echo "  ok: /tmp/.s.PGSQL.5432 refused or absent"
fi

# e. Runner-home isolation (CQ-417 item 5, added 2026-10-07 — see the header
# comment for why).
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    echo "  FAIL: \$HOME is unset or not a directory (HOME=${HOME:-<unset>})"
    FAIL=1
else
    home_mode=$(/usr/bin/stat -f '%Lp' "$HOME" 2>/dev/null)
    echo "  own home ($HOME) mode: ${home_mode:-<unreadable>}"
    case "$home_mode" in
        0|*00)
            echo "  ok: own home ($HOME) grants no group or other permission"
            ;;
        *)
            echo "  FAIL: own home ($HOME) mode $home_mode grants group or other permission"
            FAIL=1
            ;;
    esac
fi

# Enumerating /Users/*-runner needs real pathname expansion, which `set -uf`
# above (noglob, for the netstat/route loops) turns off; enable it only for
# this one expansion and restore it right after. With nullglob off (the
# bash 3.2 default) a pattern that matches nothing is left as the literal
# "RUNNER_HOMES_ROOT/*-runner" string rather than dropped, so each candidate
# is confirmed to exist with `[ -d ... ]` before anything treats it as a real
# path — a glob with no matches must neither fail nor pass some other path.
set +f
for _runner_home in "${RUNNER_HOMES_ROOT:-/Users}"/*-runner; do
    set -f
    [ -d "$_runner_home" ] || continue
    [ "$_runner_home" = "${HOME:-}" ] && continue
    if ls "$_runner_home" >/dev/null 2>&1; then
        echo "  FAIL: $_runner_home is readable by $(id -un)"
        FAIL=1
    else
        echo "  ok: $_runner_home is not readable"
    fi
    set +f
done
set -f

echo
if [ "$FAIL" -eq 0 ]; then
    echo "ISOLATION PROBE: PASS (the listener survey above is informational only)"
else
    echo "ISOLATION PROBE: FAIL — ap-runner is not isolated as expected"
fi
exit "$FAIL"
