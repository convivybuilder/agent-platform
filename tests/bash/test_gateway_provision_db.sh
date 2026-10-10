#!/bin/bash
# Behavioral test for gateway/llm-gateway-run.sh --provision-db (E17·1): install-time setup
# of the gateway's role, password, database, DATABASE_URL, prisma client and LiteLLM schema.
# A stub psql stands in for the admin connection and for logins, and a stub venv python
# stands in for prisma. The stub enforces the role's password: a login succeeds only with
# the password the last ALTER ROLE set. HOME is a sandbox, so the real venv and the real
# llm-gateway.env are never reached. No live postgres is touched.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

LAUNCHER="$REPO_ROOT/gateway/llm-gateway-run.sh"
SANDBOX=$(mktemp -d) || { echo "  FATAL: mktemp -d failed"; exit 1; }
trap 'rm -rf "$SANDBOX"' EXIT
unset LLM_GATEWAY_PSQL LLM_GATEWAY_DB_ROLE LLM_GATEWAY_DB_NAME LLM_GATEWAY_PRISMA_TIMEOUT

VENV="$SANDBOX/userhome/.local/venv-llm-gateway/bin"
mkdir -p "$VENV" "$SANDBOX/userhome/.local/lib/llm-gateway"
ENV_FILE="$SANDBOX/llm-gateway.env"
BASE_URL='postgresql://litellm@127.0.0.1:5432/litellm'

# psql: the admin call (-d postgres) logs its SQL, sets the role's password from any ALTER
# ROLE it is given, and creates the server when PSQL_ADMIN_CREATES=yes. A login succeeds
# once the server exists and PGPASSWORD matches the role's password. The schema count is 1
# once SCHEMA_STATE exists. Every call logs its argv, so a test can check it for secrets.
cat > "$SANDBOX/psql" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PSQL_LOG"
case " $* " in
    *" -d postgres "*)
        echo "ADMIN" >> "$PSQL_LOG"
        sql=$(cat)
        printf '%s\n' "$sql" >> "$PSQL_LOG"
        if printf '%s\n' "$sql" | grep -q 'ALTER ROLE'; then
            printf '%s\n' "$sql" | sed -n "s/.*PASSWORD '\([^']*\)'.*/\1/p" > "$ROLE_PW"
        fi
        if [ "${PSQL_ADMIN_CREATES:-no}" = "yes" ]; then : > "$PSQL_STATE"; fi
        exit 0
        ;;
    *)
        echo "CONNECT" >> "$PSQL_LOG"
        echo "ENV PGPASSWORD=${PGPASSWORD:-<unset>}" >> "$PSQL_LOG"
        [ -e "$PSQL_STATE" ] || exit 2
        expected=$(cat "$ROLE_PW" 2>/dev/null || true)
        if [ "${PGPASSWORD:-}" != "$expected" ]; then
            echo "password authentication failed" >&2
            exit 2
        fi
        case " $* " in
            *"information_schema"*) if [ -e "$SCHEMA_STATE" ]; then echo 1; else echo 0; fi ;;
            *) echo 1 ;;
        esac
        exit 0
        ;;
esac
STUB
chmod +x "$SANDBOX/psql"

# The venv python. The prisma probe passes once PRISMA_STATE exists. Generate creates it when
# PRISMA_GENERATES=yes, and sleeps when GEN_HANG=yes. Migrate deploy, per MIGRATES: yes creates
# the schema and succeeds; skip succeeds and creates nothing; anything else fails. MIGRATE_HANG=n
# makes it record its pid in MIGRATE_PID and sleep n seconds first. Its argv goes to its own
# log, so a test can check it for secrets.
cat > "$VENV/python" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PY_ARGV_LOG"
case "$*" in
    *"print(os.path.join"*)
        echo "/nonexistent/litellm_proxy_extras/schema.prisma"
        exit 0
        ;;
    *"from prisma import Prisma"*)
        echo "PROBE" >> "$PY_LOG"
        [ -e "$PRISMA_STATE" ] && exit 0
        exit 1
        ;;
    *"-m prisma generate"*)
        echo "GENERATE" >> "$PY_LOG"
        echo "GENERATE PATH=$PATH" >> "$PY_LOG"
        if [ "${GEN_HANG:-no}" = "yes" ]; then sleep 30; fi
        if [ "${GEN_EXITS_ZERO:-no}" = "yes" ]; then exit 0; fi
        if [ "${PRISMA_GENERATES:-no}" = "yes" ]; then : > "$PRISMA_STATE"; exit 0; fi
        exit 1
        ;;
    *"-m prisma migrate deploy"*)
        echo "MIGRATE DATABASE_URL=${DATABASE_URL:-<unset>}" >> "$PY_LOG"
        echo "MIGRATE PATH=$PATH" >> "$PY_LOG"
        if [ -n "${MIGRATE_HANG:-}" ]; then echo $$ > "$MIGRATE_PID"; sleep "$MIGRATE_HANG"; fi
        case "${MIGRATES:-no}" in
            yes) : > "$SCHEMA_STATE"; exit 0 ;;
            skip) exit 0 ;;
            *) exit 1 ;;
        esac
        ;;
    *)
        echo "UNEXPECTED $*" >> "$PY_LOG"
        exit 2
        ;;
esac
STUB
chmod +x "$VENV/python"

fresh_env() {
    printf '%s\n' \
        "DATABASE_URL=$BASE_URL" \
        'LITELLM_MASTER_KEY=test-only-not-a-real-key' > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}

# reset <server exists> <schema exists>: a clean sandbox for one scenario. With no role
# password set, a passwordless login is what the stub accepts.
reset() {
    export PSQL_LOG="$SANDBOX/psql.log" PY_LOG="$SANDBOX/py.log" PY_ARGV_LOG="$SANDBOX/py-argv.log"
    export PSQL_STATE="$SANDBOX/server" ROLE_PW="$SANDBOX/role-pw" SCHEMA_STATE="$SANDBOX/schema"
    export PRISMA_STATE="$SANDBOX/prisma-ready"
    rm -f "$PSQL_LOG" "$PY_LOG" "$PY_ARGV_LOG" "$PSQL_STATE" "$ROLE_PW" "$SCHEMA_STATE" "$PRISMA_STATE"
    [ "$1" = "yes" ] && : > "$PSQL_STATE"
    [ "$2" = "yes" ] && : > "$SCHEMA_STATE"
    fresh_env
}

# run_provision <admin creates the server>: runs --provision-db once. Sets RC, ERR, OUT,
# LOG (psql), PY_CALLS and PY_ARGV. MIGRATES and PRISMA_GENERATES default to yes, and a
# caller may set them for one call.
run_provision() {
    RC=0
    env HOME="$SANDBOX/userhome" LLM_GATEWAY_ENV_FILE="$ENV_FILE" LLM_GATEWAY_PSQL="$SANDBOX/psql" \
        PSQL_ADMIN_CREATES="$1" PRISMA_GENERATES="${PRISMA_GENERATES:-yes}" MIGRATES="${MIGRATES:-yes}" \
        /bin/bash "$LAUNCHER" --provision-db >"$SANDBOX/out" 2>"$SANDBOX/err" || RC=$?
    ERR=$(cat "$SANDBOX/err")
    OUT=$(cat "$SANDBOX/out")
    LOG=$(cat "$PSQL_LOG" 2>/dev/null || true)
    PY_CALLS=$(cat "$PY_LOG" 2>/dev/null || true)
    PY_ARGV=$(cat "$PY_ARGV_LOG" 2>/dev/null || true)
}

echo "== a URL that already logs in is kept: no admin connection, and no new password =="
reset yes no
run_provision yes
assert_rc 0 "$RC" "reachable DATABASE_URL: --provision-db exits 0"
admin_calls=$(printf '%s\n' "$LOG" | grep -c '^ADMIN' || true)
assert_eq "0" "$admin_calls" "reachable DATABASE_URL: no admin connection"
assert_eq "$BASE_URL" "$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)" "the working URL is written back unchanged"
assert_contains "$PY_CALLS" "MIGRATE" "the schema is applied even when the URL was already working"
assert_contains "$PY_CALLS" "GENERATE" "the client is generated at install, when it is missing"

echo "== an unreachable database is created, and its role gets a password the URL carries =="
reset no no
run_provision yes
assert_rc 0 "$RC" "after provisioning the URL logs in: exits 0"
assert_contains "$LOG" "ADMIN" "unreachable DATABASE_URL: admin connection made"
assert_contains "$LOG" "CREATE ROLE litellm LOGIN" "admin SQL creates the litellm role"
assert_contains "$LOG" "CREATE DATABASE litellm OWNER litellm" "admin SQL creates the litellm database"
assert_contains "$LOG" "\\gexec" "admin SQL is guarded, so a rerun creates nothing"
pw=$(cat "$SANDBOX/role-pw")
assert_eq "48" "${#pw}" "the role's password is 48 hex characters"
assert_eq "'postgresql://litellm:$pw@127.0.0.1:5432/litellm'" "$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)" "DATABASE_URL carries the role's password, single-quoted"
assert_eq "postgresql://litellm:$pw@127.0.0.1:5432/litellm" "$(. "$ENV_FILE"; printf '%s' "$DATABASE_URL")" "sourcing llm-gateway.env yields the URL with its password"
assert_eq "-rw-------" "$(ls -l "$ENV_FILE" | cut -c1-10)" "llm-gateway.env stays mode 0600"
assert_contains "$PY_CALLS" "MIGRATE DATABASE_URL=postgresql://litellm:$pw@" "migrations run with DATABASE_URL in the environment"

echo "== the password reaches no argv and no output =="
assert_eq "0" "$(printf '%s\n' "$LOG" | grep '^ARGV' | grep -c "$pw" || true)" "no psql argv carries the password"
assert_eq "0" "$(grep -c "$pw" "$SANDBOX/py-argv.log" || true)" "no python argv carries the password"
assert_eq "0" "$(printf '%s\n%s\n' "$OUT" "$ERR" | grep -c "$pw" || true)" "the password is not in the launcher's output"
login_env=$(printf '%s\n' "$LOG" | grep '^ENV PGPASSWORD=' | tail -n 1)
assert_eq "ENV PGPASSWORD=$pw" "$login_env" "the login is given the password through PGPASSWORD"

echo "== re-provisioning keeps a working URL, and issues no new password =="
before=$(cat "$ENV_FILE")
: > "$PSQL_LOG"
run_provision yes
assert_rc 0 "$RC" "a rerun exits 0"
assert_eq "$before" "$(cat "$ENV_FILE")" "a rerun leaves llm-gateway.env byte-identical"
admin_calls=$(printf '%s\n' "$LOG" | grep -c '^ADMIN' || true)
assert_eq "0" "$admin_calls" "a rerun makes no admin connection"

echo "== prisma runs with the venv's bin first on PATH, where prisma-client-py is installed =="
reset no no
run_provision yes
assert_rc 0 "$RC" "control: provisioning exits 0, so both prisma steps ran"
assert_contains "$PY_CALLS" "GENERATE PATH=$VENV:" "the generate step's PATH starts with the venv's bin"
assert_contains "$PY_CALLS" "MIGRATE PATH=$VENV:" "the migrate deploy step's PATH starts with the venv's bin"

echo "== a query string with & survives the rewrite, because the env file is sourced =="
reset no no
printf '%s\n' \
    "DATABASE_URL='postgresql://litellm@127.0.0.1:5432/litellm?sslmode=disable&application_name=gw'" \
    'LITELLM_MASTER_KEY=test-only-not-a-real-key' > "$ENV_FILE"
run_provision yes
assert_rc 0 "$RC" "a URL with a query string: --provision-db exits 0"
pw=$(cat "$SANDBOX/role-pw")
assert_eq "postgresql://litellm:$pw@127.0.0.1:5432/litellm?sslmode=disable&application_name=gw" "$(. "$ENV_FILE"; printf '%s' "$DATABASE_URL")" "the sourced URL keeps its query string whole"
assert_eq "test-only-not-a-real-key" "$(. "$ENV_FILE"; printf '%s' "$LITELLM_MASTER_KEY")" "the other lines of the env file survive the rewrite"

echo "== a database that still does not connect is reported, and not fatal =="
reset no no
run_provision no
assert_rc 1 "$RC" "provisioning fails to connect: --provision-db exits 1 (install.sh warns)"
assert_contains "$ERR" "does not connect after provisioning" "failure names the cause"

echo "== a generation that runs past its limit is stopped and reported =="
reset yes no
start=$SECONDS
LLM_GATEWAY_PRISMA_TIMEOUT=1 GEN_HANG=yes PRISMA_GENERATES=no run_provision yes
elapsed=$((SECONDS - start))
assert_rc 1 "$RC" "a generation past the limit: --provision-db exits 1"
assert_contains "$ERR" "ran past 1s" "the timeout is named"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the watchdog ends the generation instead of waiting it out"

echo "== a generation that exits 0 without a loadable client is reported =="
reset yes no
GEN_EXITS_ZERO=yes PRISMA_GENERATES=no run_provision yes
assert_rc 1 "$RC" "a generate that succeeds but leaves no client: --provision-db exits 1"
assert_contains "$ERR" "still does not load" "the client that still does not load is named"

echo "== a migration that leaves no schema is reported =="
reset yes no
MIGRATES=skip run_provision yes
assert_rc 1 "$RC" "migrations that create nothing: --provision-db exits 1"
assert_contains "$ERR" "schema is still missing" "the missing schema is named"

# A normal start must go on to exec litellm after a failed provisioning. The sandboxed
# HOME has no litellm, so the exec fails and bash names the litellm path it tried. That
# message is the proof the start reached exec; the exit code varies by bash build.
echo "== a normal start proceeds to exec when the database is down =="
reset no no
env HOME="$SANDBOX/userhome" LLM_GATEWAY_ENV_FILE="$ENV_FILE" LLM_GATEWAY_PSQL="$SANDBOX/psql" \
    LLM_GATEWAY_LOGIN_WAIT=1 LLM_GATEWAY_LOGIN_INTERVAL=1 \
    /bin/bash "$LAUNCHER" >/dev/null 2>"$SANDBOX/err" || true
ERR=$(cat "$SANDBOX/err")
assert_contains "$ERR" "does not connect" "the start reports the database"
assert_contains "$ERR" "venv-llm-gateway/bin/litellm" "the start goes on to exec litellm"
assert_eq "" "$(cat "$PY_LOG" 2>/dev/null || true)" "a start provisions nothing: no generate, no migrate"

# psql comes from LLM_GATEWAY_PSQL, else PATH. Nothing is hardcoded to a Homebrew path, so
# with neither set, the launcher reports the missing psql instead of reaching for one.
unset LLM_GATEWAY_PSQL LLM_GATEWAY_DB_ROLE LLM_GATEWAY_DB_NAME
mkdir -p "$SANDBOX/pathbin" "$SANDBOX/empty"
cp "$SANDBOX/psql" "$SANDBOX/pathbin/psql"

echo "== psql is taken from PATH when LLM_GATEWAY_PSQL is unset =="
reset yes yes
RC=0
env HOME="$SANDBOX/userhome" PATH="$SANDBOX/pathbin:/usr/bin:/bin" LLM_GATEWAY_ENV_FILE="$ENV_FILE" \
    /bin/bash "$LAUNCHER" --provision-db >/dev/null 2>"$SANDBOX/err" || RC=$?
LOG=$(cat "$PSQL_LOG" 2>/dev/null || true)
assert_contains "$LOG" "CONNECT" "the psql found on PATH is the one that connects"

echo "== a missing psql is reported, and not fatal =="
reset no no
RC=0
env HOME="$SANDBOX/userhome" PATH="$SANDBOX/empty" LLM_GATEWAY_ENV_FILE="$ENV_FILE" \
    /bin/bash "$LAUNCHER" --provision-db >/dev/null 2>"$SANDBOX/err" || RC=$?
ERR=$(cat "$SANDBOX/err")
assert_rc 1 "$RC" "no psql on PATH: --provision-db exits 1, not a crash"
assert_contains "$ERR" "psql not found on PATH" "the missing psql is named"
assert_eq "" "$(cat "$PSQL_LOG" 2>/dev/null || true)" "with no psql, nothing connects"

echo "== the role and database names come from the settings =="
reset no no
export LLM_GATEWAY_DB_ROLE=gw_keys LLM_GATEWAY_DB_NAME=gwkeys
run_provision yes
unset LLM_GATEWAY_DB_ROLE LLM_GATEWAY_DB_NAME
assert_rc 0 "$RC" "custom names: provisioning exits 0"
assert_contains "$LOG" "CREATE ROLE gw_keys LOGIN" "the configured role is created"
assert_contains "$LOG" "CREATE DATABASE gwkeys OWNER gw_keys" "the configured database is created"
assert_contains "$(grep '^DATABASE_URL=' "$ENV_FILE")" "postgresql://gw_keys:" "the URL logs in as the configured role"

echo "== a name that is not a plain identifier is refused before any SQL runs =="
reset no no
export LLM_GATEWAY_DB_NAME='gw;drop'
run_provision yes
unset LLM_GATEWAY_DB_NAME
admin_calls=$(printf '%s\n' "$LOG" | grep -c '^ADMIN' || true)
assert_rc 1 "$RC" "a bad name: --provision-db exits 1"
assert_eq "0" "$admin_calls" "a bad name: no admin connection"
assert_contains "$ERR" "lowercase identifiers" "a bad name is named as the cause"

# _gateway_timed runs in a subshell that sources the launcher, so the launcher's `set -u`
# and job table stay out of this shell. Each case uses a sleep length no other process here
# uses, so a leftover sleep is found by its exact command line.
sleeps_named() { # <seconds>
  ps -axo command= | grep -c "^sleep $1\$" || true
}

echo "== the process query finds a sleep that is running (control for the cases below) =="
sleep 316 &
control=$!
sleep 1
assert_eq "1" "$(sleeps_named 316)" "the query sees a sleep this test started"
kill "$control" 2>/dev/null
wait "$control" 2>/dev/null

# The command must outlast the watchdog's fork. A command that ends in milliseconds can stop
# the watchdog before its sleep exists, which hides a leaked sleep from this check.
echo "== a command that finishes before its limit leaves no watchdog behind =="
( . "$LAUNCHER"; _gateway_timed 317 sleep 1 ) >/dev/null 2>&1
RC=$?
sleep 1
assert_rc 0 "$RC" "a command that finishes early: the timed call exits 0"
assert_eq "0" "$(sleeps_named 317)" "the watchdog's sleep is gone once the command has finished"

echo "== a timeout stops the command's children with it =="
child_pid_file="$SANDBOX/child.pid"
start=$SECONDS
( . "$LAUNCHER"; _gateway_timed 1 /bin/bash -c "sleep 319 & echo \$! > '$child_pid_file'; wait" ) >/dev/null 2>&1
RC=$?
elapsed=$((SECONDS - start))
sleep 1
assert_eq "yes" "$([ -s "$child_pid_file" ] && echo yes || echo no)" "the timed command started its child before the limit"
# A PID of 0 would make kill signal this shell's own process group, so only a real PID is probed
# and killed.
child=$(cat "$child_pid_file" 2>/dev/null || echo 0)
alive=no
if [ "$child" -gt 0 ] && kill -0 "$child" 2>/dev/null; then alive=yes; fi
assert_rc 143 "$RC" "a timed-out command exits 143, the SIGTERM status"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the limit ends the command, rather than the command finishing itself"
assert_eq "no" "$alive" "the child the command started is stopped with it"
if [ "$child" -gt 0 ]; then kill "$child" 2>/dev/null; fi

# No controlling terminal: perl's setsid starts a session with none, and stdin is /dev/null.
# The command records its own pid and pgid, so the test sees whether job control gave it a group
# of its own without a terminal. If it did not, the timeout would signal nothing.
cat > "$SANDBOX/notty-cmd.sh" <<'SH'
#!/bin/bash
echo $$ > "$1"
ps -o pgid= -p $$ | tr -d ' ' > "$2"
sleep 320 & echo $! > "$3"
wait
SH
cat > "$SANDBOX/notty-driver.sh" <<'SH'
#!/bin/bash
( . "$1"; tty > "$2" 2>&1; _gateway_timed 1 /bin/bash "$6" "$3" "$4" "$5" )
SH

echo "== with no controlling terminal and stdin from /dev/null, a timeout still stops the command =="
start=$SECONDS
perl -MPOSIX -e 'POSIX::setsid() or die "setsid: $!"; exec @ARGV' /bin/bash "$SANDBOX/notty-driver.sh" \
    "$LAUNCHER" "$SANDBOX/notty.tty" "$SANDBOX/notty.pid" "$SANDBOX/notty.pgid" "$SANDBOX/notty-child.pid" \
    "$SANDBOX/notty-cmd.sh" </dev/null >/dev/null 2>&1
RC=$?
elapsed=$((SECONDS - start))
sleep 1
assert_eq "not a tty" "$(cat "$SANDBOX/notty.tty")" "the timed call runs with no controlling terminal"
assert_eq "$(cat "$SANDBOX/notty.pid")" "$(cat "$SANDBOX/notty.pgid")" "job control gives the command a process group of its own, with no terminal"
assert_rc 143 "$RC" "a timed-out command exits 143 with no terminal"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "with no terminal, the limit ends the command within its bound"
child=$(cat "$SANDBOX/notty-child.pid" 2>/dev/null || echo 0)
alive=no
if [ "$child" -gt 0 ] && kill -0 "$child" 2>/dev/null; then alive=yes; fi
assert_eq "no" "$alive" "with no terminal, the child the command started is stopped with it"
if [ "$child" -gt 0 ]; then kill "$child" 2>/dev/null; fi

# Ctrl-C at the terminal reaches the whole foreground group. The timed call's command is in a
# group of its own, so it gets no signal from that; the launcher must forward the INT. The
# driver is its own group with default INT, which is what a foreground job has.
cat > "$SANDBOX/int-cmd.sh" <<'SH'
#!/bin/bash
sleep 321 & echo $! > "$1"
wait
SH
cat > "$SANDBOX/int-driver.sh" <<'SH'
#!/bin/bash
( . "$1"; _gateway_timed 60 /bin/bash "$2" "$3" )
SH

echo "== an INT sent to the process group that runs a timed command stops the command and its children =="
perl -e 'setpgrp(0, 0); $SIG{INT} = "DEFAULT"; exec @ARGV' /bin/bash "$SANDBOX/int-driver.sh" \
    "$LAUNCHER" "$SANDBOX/int-cmd.sh" "$SANDBOX/int-child.pid" </dev/null >/dev/null 2>&1 &
drv=$!
sleep 1
start=$SECONDS
kill -INT -- "-$drv" 2>/dev/null
wait "$drv"
RC=$?
elapsed=$((SECONDS - start))
sleep 1
assert_rc 130 "$RC" "an INT stops the timed call with the interrupt status"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the INT ends the call rather than the 60 s limit"
child=$(cat "$SANDBOX/int-child.pid" 2>/dev/null || echo 0)
alive=no
if [ "$child" -gt 0 ] && kill -0 "$child" 2>/dev/null; then alive=yes; fi
assert_eq "no" "$alive" "an INT stops the command's child too"
if [ "$child" -gt 0 ]; then kill "$child" 2>/dev/null; fi

# A signal to the launcher's own pid alone, not a group, must stop migrate deploy and exit the
# launcher with that signal's status: 130 for INT, 143 for TERM, 129 for HUP. The group signals
# above also reach the migration, so they prove nothing about this. The launcher starts with INT,
# TERM and HUP at their defaults, as launchd starts it: a background job in a non-interactive
# shell can start with INT ignored, and a trap cannot catch a signal that was ignored on entry.
# Each signal gets its own sleep length, so a leftover is found by its exact command line.
migrate_signalled_alone() { # <signal> <sleep seconds>: sets RC, elapsed, MIG_STARTED, MIG_ALIVE, MIG_SLEEPS
    local lpid i mpid
    reset yes no
    rm -f "$SANDBOX/migrate.pid"
    env HOME="$SANDBOX/userhome" LLM_GATEWAY_ENV_FILE="$ENV_FILE" LLM_GATEWAY_PSQL="$SANDBOX/psql" \
        PSQL_ADMIN_CREATES=no PRISMA_GENERATES=yes MIGRATES=yes MIGRATE_HANG="$2" \
        MIGRATE_PID="$SANDBOX/migrate.pid" LLM_GATEWAY_PRISMA_TIMEOUT=60 \
        perl -e '$SIG{INT} = "DEFAULT"; $SIG{TERM} = "DEFAULT"; $SIG{HUP} = "DEFAULT"; exec @ARGV' /bin/bash "$LAUNCHER" --provision-db \
        >/dev/null 2>"$SANDBOX/err" &
    lpid=$!
    i=0
    while [ ! -s "$SANDBOX/migrate.pid" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
    MIG_STARTED=$([ -s "$SANDBOX/migrate.pid" ] && echo yes || echo no)
    sleep 1
    start=$SECONDS
    kill -"$1" "$lpid" 2>/dev/null
    wait "$lpid"
    RC=$?
    elapsed=$((SECONDS - start))
    sleep 1
    mpid=$(cat "$SANDBOX/migrate.pid" 2>/dev/null || echo 0)
    MIG_ALIVE=no
    if [ "$mpid" -gt 0 ] && kill -0 "$mpid" 2>/dev/null; then MIG_ALIVE=yes; fi
    MIG_SLEEPS=$(sleeps_named "$2")
    # The migrate stub leads its own process group, so this also stops a sleep it left behind.
    if [ "$mpid" -gt 0 ]; then kill -TERM -- "-$mpid" 2>/dev/null; fi
}

echo "== a TERM to the launcher's pid alone stops migrate deploy and exits the launcher with 143 =="
migrate_signalled_alone TERM 330
assert_eq "yes" "$MIG_STARTED" "control: migrate deploy had started when the TERM was sent"
assert_rc 143 "$RC" "a TERM to the launcher exits it with the SIGTERM status"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the TERM ends the launcher within its bound, not at the 60 s limit"
assert_eq "no" "$MIG_ALIVE" "the TERM stops the migrate deploy process"
assert_eq "0" "$MIG_SLEEPS" "the TERM stops migrate deploy's sleep too"

echo "== an INT to the launcher's pid alone stops migrate deploy and exits the launcher with 130 =="
migrate_signalled_alone INT 331
assert_eq "yes" "$MIG_STARTED" "control: migrate deploy had started when the INT was sent"
assert_rc 130 "$RC" "an INT to the launcher exits it with the SIGINT status"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the INT ends the launcher within its bound, not at the 60 s limit"
assert_eq "no" "$MIG_ALIVE" "the INT stops the migrate deploy process"
assert_eq "0" "$MIG_SLEEPS" "the INT stops migrate deploy's sleep too"

echo "== a HUP to the launcher's pid alone stops migrate deploy and exits the launcher with 129 =="
migrate_signalled_alone HUP 332
assert_eq "yes" "$MIG_STARTED" "control: migrate deploy had started when the HUP was sent"
assert_rc 129 "$RC" "a HUP to the launcher exits it with the SIGHUP status"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the HUP ends the launcher within its bound, not at the 60 s limit"
assert_eq "no" "$MIG_ALIVE" "the HUP stops the migrate deploy process"
assert_eq "0" "$MIG_SLEEPS" "the HUP stops migrate deploy's sleep too"

finish
