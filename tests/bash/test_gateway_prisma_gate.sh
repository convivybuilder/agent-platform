#!/bin/bash
# Behavioral test for the start check in gateway/llm-gateway-run.sh (E17·1): the launcher
# exports DATABASE_URL to litellm only once the URL logs in, the LiteLLM schema is present
# and the prisma client is generated. A start creates nothing and generates nothing; that
# is --provision-db's job, tested in test_gateway_provision_db.sh. Stubs stand in for psql,
# the venv python and litellm, and the config dir comes from LLM_GATEWAY_CONFIG_DIR. No live
# postgres, no real venv and no real llm-gateway.env is touched; HOME is a sandbox.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

LAUNCHER="$REPO_ROOT/gateway/llm-gateway-run.sh"
SANDBOX=$(mktemp -d) || { echo "  FATAL: mktemp -d failed"; exit 1; }
trap 'rm -rf "$SANDBOX"' EXIT
unset LLM_GATEWAY_PSQL LLM_GATEWAY_DB_ROLE LLM_GATEWAY_DB_NAME LLM_GATEWAY_CONFIG_DIR LLM_GATEWAY_ENV_FILE
# The login retry is bounded by these. The defaults, 60 s and 5 s, would make each refused
# login in this file take a minute. The retry case sets its own bound.
export LLM_GATEWAY_LOGIN_WAIT=2 LLM_GATEWAY_LOGIN_INTERVAL=1

CFG="$SANDBOX/cfg"
VENV="$SANDBOX/userhome/.local/venv-llm-gateway/bin"
mkdir -p "$CFG" "$VENV" "$SANDBOX/userhome/.local/lib/llm-gateway"

printf '%s\n' \
    'DATABASE_URL=postgresql://litellm@127.0.0.1:5432/litellm' \
    'LITELLM_MASTER_KEY=test-only-not-a-real-key' > "$CFG/llm-gateway.env"
chmod 600 "$CFG/llm-gateway.env"

# The prisma wheel installs a `prisma` executable into the venv's bin. Head put that bin
# on LiteLLM's PATH, which made LiteLLM find this one; the launcher must not.
printf '#!/bin/bash\nexit 0\n' > "$VENV/prisma"
chmod +x "$VENV/prisma"

# A logging prisma in a directory the launcher INHERITS, as a host with a global npm prisma
# would have it. Every run below starts with this directory at the front of PATH. Each
# invocation is logged with its argv and DATABASE_URL, so a test can see whether any prisma
# CLI ran and whether the URL reached it. A run that reaches it leaves a line in the log.
FAKE_BIN="$SANDBOX/fakebin"
FAKE_PRISMA_LOG="$SANDBOX/fake-prisma.log"
export FAKE_PRISMA_LOG
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/prisma" <<'STUB'
#!/bin/bash
{
    echo "INVOKED $*"
    echo "DATABASE_URL=${DATABASE_URL:-<unset>}"
} >> "$FAKE_PRISMA_LOG"
exit 0
STUB
chmod +x "$FAKE_BIN/prisma"
rm -f "$FAKE_PRISMA_LOG"

# psql: every connection logs its argv and the PGPASSWORD it was given. A login succeeds
# once $PSQL_STATE exists, and is refused when LOGIN_REFUSED=yes. With LOGIN_FAILURES=n, the
# first n connections are refused and later ones are not. Every connection counts, including
# the schema query, so $LOGIN_TRIES is the total. The schema query answers 1 once $SCHEMA_STATE
# exists. An admin call (-d postgres) must never happen at start.
cat > "$SANDBOX/psql" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PSQL_LOG"
case " $* " in
    *" -d postgres "*)
        echo "ADMIN" >> "$PSQL_LOG"
        cat > /dev/null
        exit 0
        ;;
    *)
        echo "ENV PGPASSWORD=${PGPASSWORD:-<unset>}" >> "$PSQL_LOG"
        if [ -n "${LOGIN_FAILURES:-}" ]; then
            tries=$(cat "$LOGIN_TRIES" 2>/dev/null || echo 0)
            echo $((tries + 1)) > "$LOGIN_TRIES"
            if [ "$tries" -lt "$LOGIN_FAILURES" ]; then
                echo "could not connect to server" >&2
                exit 2
            fi
        fi
        if [ "${LOGIN_REFUSED:-no}" = "yes" ]; then
            echo "password authentication failed" >&2
            exit 2
        fi
        [ -e "$PSQL_STATE" ] || exit 2
        case " $* " in
            *"information_schema"*) if [ -e "$SCHEMA_STATE" ]; then echo 1; else echo 0; fi ;;
            *) echo 1 ;;
        esac
        exit 0
        ;;
esac
STUB
chmod +x "$SANDBOX/psql"

# The venv python: answers the prisma probe and records any generate. It must never be
# asked to generate at start, so GENERATE in the log is a failure.
cat > "$VENV/python" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PY_ARGV_LOG"
case "$*" in
    *"from prisma import Prisma"*)
        echo "PROBE" >> "$PY_LOG"
        [ -e "$PRISMA_STATE" ] && exit 0
        exit 1
        ;;
    *"-m prisma generate"*)
        echo "GENERATE" >> "$PY_LOG"
        if [ "${PRISMA_GENERATES:-no}" = "yes" ]; then : > "$PRISMA_STATE"; exit 0; fi
        exit 1
        ;;
    *)
        echo "UNEXPECTED $*" >> "$PY_LOG"
        exit 2
        ;;
esac
STUB
chmod +x "$VENV/python"

# Every variable LiteLLM 1.86.2 reads to find or build a database URL. Read from
# litellm/proxy/proxy_cli.py, litellm/proxy/utils.py, litellm/proxy/db/prisma_client.py,
# litellm/proxy/db/db_url_settings.py and litellm_proxy_extras/utils.py. SPEND_LOGS_URL is left
# out: it is an HTTP base URL for spend logs, not a database. A LiteLLM upgrade re-reads the list.
LITELLM_DB_VARS="DATABASE_URL DIRECT_URL DATABASE_URL_READ_REPLICA IAM_TOKEN_DB_AUTH
    DATABASE_HOST DATABASE_PORT DATABASE_USER DATABASE_USERNAME DATABASE_PASSWORD
    DATABASE_NAME DATABASE_SCHEMA DATABASE_HOST_READ_REPLICA DATABASE_PORT_READ_REPLICA
    DATABASE_USER_READ_REPLICA DATABASE_USERNAME_READ_REPLICA DATABASE_PASSWORD_READ_REPLICA
    DATABASE_NAME_READ_REPLICA DATABASE_SCHEMA_READ_REPLICA"
export LITELLM_DB_VARS
# DATABASE_SCHEMA is the one name the launcher leaves set: LiteLLM's view check reads it on the
# exposed path, and it builds no URL alone. The withheld-variable cases below run over this list.
LITELLM_DB_VARS_WITHHELD=""
for v in $LITELLM_DB_VARS; do
    [ "$v" = DATABASE_SCHEMA ] || LITELLM_DB_VARS_WITHHELD="$LITELLM_DB_VARS_WITHHELD $v"
done

# litellm: records what the environment hands it, what a PATH lookup of prisma finds, and the
# PATH itself. Then it does what LiteLLM does with DISABLE_SCHEMA_UPDATE=true and a prisma CLI
# on its PATH: it runs `prisma migrate diff --from-url <DATABASE_URL>`, which is the argv leak.
# Each database variable it was handed is recorded as SET <name>.
cat > "$VENV/litellm" <<'STUB'
#!/bin/bash
{
    echo "DATABASE_URL=${DATABASE_URL:-<unset>}"
    echo "DATABASE_SCHEMA=${DATABASE_SCHEMA:-<unset>}"
    echo "MASTER=${LITELLM_MASTER_KEY:-<unset>}"
    echo "DISABLE_SCHEMA_UPDATE=${DISABLE_SCHEMA_UPDATE:-<unset>}"
    echo "PRISMA=$(command -v prisma || echo none)"
    echo "LITELLM_PATH=$PATH"
    for v in $LITELLM_DB_VARS; do
        env | grep -q "^$v=" && echo "SET $v"
    done
} > "$SEEN"
if [ "${DISABLE_SCHEMA_UPDATE:-false}" = "true" ] && command -v prisma >/dev/null 2>&1; then
    prisma migrate diff --from-url "${DATABASE_URL:-}" --to-schema-datamodel ./schema.prisma --script >/dev/null 2>&1
fi
exit 0
STUB
chmod +x "$VENV/litellm"

# run_gateway <database up> <prisma client ready> <schema present> <generate would succeed>,
# each yes or no. LOGIN_REFUSED, when set in the caller, makes the login fail. Sets RC, ERR,
# SEEN_TXT, PY_CALLS, PSQL_CALLS and FAKE_TXT.
run_gateway() {
    export PSQL_LOG="$SANDBOX/psql.log" PY_LOG="$SANDBOX/py.log" PY_ARGV_LOG="$SANDBOX/py-argv.log" SEEN="$SANDBOX/seen"
    export PSQL_STATE="$SANDBOX/db-up" PRISMA_STATE="$SANDBOX/prisma-ready" SCHEMA_STATE="$SANDBOX/schema-present"
    export LOGIN_TRIES="$SANDBOX/login-tries"
    rm -f "$PSQL_LOG" "$PY_LOG" "$PY_ARGV_LOG" "$SEEN" "$PSQL_STATE" "$PRISMA_STATE" "$SCHEMA_STATE" "$LOGIN_TRIES"
    [ "$1" = "yes" ] && : > "$PSQL_STATE"
    [ "$2" = "yes" ] && : > "$PRISMA_STATE"
    [ "$3" = "yes" ] && : > "$SCHEMA_STATE"
    export PRISMA_GENERATES="$4"
    RC=0
    # PATH is the PATH the launcher INHERITS: the fake prisma in front of the system directories.
    # The launcher must pin it out of reach before LiteLLM starts. The fake log is not cleared
    # here, so it holds every run's invocations, and the final assertion checks all of them.
    env HOME="$SANDBOX/userhome" LLM_GATEWAY_CONFIG_DIR="$CFG" LLM_GATEWAY_PSQL="$SANDBOX/psql" \
        PATH="$FAKE_BIN:/usr/bin:/bin" /bin/bash "$LAUNCHER" >/dev/null 2>"$SANDBOX/err" || RC=$?
    ERR=$(cat "$SANDBOX/err")
    SEEN_TXT=$(cat "$SEEN" 2>/dev/null || true)
    PY_CALLS=$(cat "$PY_LOG" 2>/dev/null || true)
    PSQL_CALLS=$(cat "$PSQL_LOG" 2>/dev/null || true)
    FAKE_TXT=$(cat "$FAKE_PRISMA_LOG" 2>/dev/null || true)
}

echo "== a database that logs in, with a schema and a ready client: litellm receives DATABASE_URL =="
assert_contains "$(env PATH="$FAKE_BIN:/usr/bin:/bin" /bin/bash -c 'command -v prisma')" "$FAKE_BIN/prisma" "control: the logging prisma resolves on the inherited PATH"
run_gateway yes yes yes no
assert_rc 0 "$RC" "the start execs litellm"
assert_contains "$SEEN_TXT" "DATABASE_URL=postgresql://" "DATABASE_URL reaches litellm"
assert_contains "$SEEN_TXT" "MASTER=test-only-not-a-real-key" "LLM_GATEWAY_CONFIG_DIR supplies the env file"
assert_eq "PROBE" "$PY_CALLS" "the client is probed, and nothing is generated"
assert_contains "$SEEN_TXT" "PRISMA=none" "prisma is not on litellm's PATH, so litellm skips its schema step"
assert_contains "$SEEN_TXT" "DISABLE_SCHEMA_UPDATE=true" "DISABLE_SCHEMA_UPDATE=true reaches litellm"
assert_eq "LITELLM_PATH=/usr/bin:/bin:/usr/sbin:/sbin" "$(printf '%s\n' "$SEEN_TXT" | grep '^LITELLM_PATH=')" "litellm inherits only the pinned system directories, with no venv bin, node or npm"
assert_eq "" "$FAKE_TXT" "the logging prisma on the inherited PATH is never invoked, so DATABASE_URL never reaches it"

echo "== a client that is not generated: nothing is generated at start, DATABASE_URL is withheld =="
run_gateway yes no yes yes
assert_rc 0 "$RC" "the start still execs litellm"
assert_contains "$SEEN_TXT" "DATABASE_URL=<unset>" "DATABASE_URL is withheld from litellm"
assert_contains "$SEEN_TXT" "MASTER=test-only-not-a-real-key" "litellm still gets its credentials"
assert_eq "PROBE" "$PY_CALLS" "the start probes the client and never generates it, even when generation would work"
assert_contains "$ERR" "$SANDBOX/userhome/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db" "the start names the installed script's path for the narrower recovery, --provision-db"
assert_contains "$ERR" "starting without the database" "the start reports why"

echo "== a database with no LiteLLM schema: DATABASE_URL is withheld before the client is probed =="
run_gateway yes yes no no
assert_contains "$SEEN_TXT" "DATABASE_URL=<unset>" "DATABASE_URL is withheld without the schema"
assert_contains "$ERR" "schema is missing" "the missing schema is named"
assert_contains "$ERR" "$SANDBOX/userhome/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db" "the missing schema names the installed script's path"
assert_eq "" "$PY_CALLS" "no prisma probe runs before the schema check passes"

echo "== a database that refuses the login: DATABASE_URL is withheld, and the start creates nothing =="
LOGIN_REFUSED=yes run_gateway yes yes yes no
assert_rc 0 "$RC" "a refused login still lets litellm start"
assert_contains "$SEEN_TXT" "DATABASE_URL=<unset>" "DATABASE_URL is withheld after a refused login"
assert_contains "$ERR" "does not connect" "the refused login is reported"
assert_contains "$ERR" "$SANDBOX/userhome/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db" "the refused login names the installed script's path"
assert_eq "0" "$(printf '%s\n' "$ERR" | grep -c 'after provisioning' || true)" "the start does not say after provisioning: it provisions nothing"
assert_eq "" "$(printf '%s\n' "$PSQL_CALLS" | grep '^ADMIN' || true)" "a start makes no admin connection"
assert_eq "" "$PY_CALLS" "no prisma probe runs after a refused login"

echo "== a database that does not connect: the client is not probed, and DATABASE_URL is withheld =="
run_gateway no no yes yes
assert_contains "$SEEN_TXT" "DATABASE_URL=<unset>" "DATABASE_URL is withheld without a database"
assert_eq "" "$PY_CALLS" "no prisma probe or generate runs without a database"
assert_contains "$ERR" "does not connect" "the database failure is reported"

echo "== a login refused twice and then accepted: the start retries, and the database is used =="
LOGIN_FAILURES=2 LLM_GATEWAY_LOGIN_WAIT=5 run_gateway yes yes yes no
assert_rc 0 "$RC" "a login that succeeds on the third try: the start execs litellm"
assert_contains "$SEEN_TXT" "DATABASE_URL=postgresql://" "DATABASE_URL reaches litellm once a retried login succeeds"
assert_eq "yes" "$([ "$(cat "$LOGIN_TRIES")" -ge 3 ] && echo yes || echo no)" "the login was retried: at least three connections were made"
assert_eq "" "$(printf '%s\n' "$ERR" | grep 'starting without' || true)" "a start that retried into a login does not report running without the database"

echo "== a login that never succeeds is retried for the bound, then the start goes on without the database =="
start=$SECONDS
LOGIN_FAILURES=1000 LLM_GATEWAY_LOGIN_WAIT=2 LLM_GATEWAY_LOGIN_INTERVAL=1 run_gateway yes yes yes no
elapsed=$((SECONDS - start))
assert_rc 0 "$RC" "a login that never succeeds: the start still execs litellm"
assert_contains "$SEEN_TXT" "DATABASE_URL=<unset>" "DATABASE_URL is withheld after the bound"
assert_contains "$ERR" "starting without the database" "the start that gave up says so"
# The bound is whole seconds and the interval is one second, so the count depends on where the
# first attempt falls inside a second. Two is the minimum: the refusal, and one retry.
assert_eq "yes" "$([ "$(cat "$LOGIN_TRIES")" -ge 2 ] && echo yes || echo no)" "the login was retried before giving up"
assert_eq "fast" "$([ "$elapsed" -lt 15 ] && echo fast || echo slow)" "the retry stops at the bound instead of running on"

echo "== a password in the URL travels in PGPASSWORD, and not in any argv =="
printf '%s\n' \
    'DATABASE_URL=postgresql://litellm:s3cret-test-pw@127.0.0.1:5432/litellm' \
    'LITELLM_MASTER_KEY=test-only-not-a-real-key' > "$CFG/llm-gateway.env"
chmod 600 "$CFG/llm-gateway.env"
run_gateway yes yes yes no
assert_contains "$SEEN_TXT" "DATABASE_URL=postgresql://litellm:s3cret-test-pw@" "litellm receives the URL with its password"
assert_contains "$PSQL_CALLS" "ENV PGPASSWORD=s3cret-test-pw" "the login is given the password through PGPASSWORD"
assert_eq "0" "$(printf '%s\n' "$PSQL_CALLS" | grep '^ARGV' | grep -c 's3cret' || true)" "no psql argv carries the password"
assert_eq "0" "$(cat "$SANDBOX/py-argv.log" 2>/dev/null | grep -c 's3cret' || true)" "no python argv carries the password"

echo "== every database variable LiteLLM reads is withheld, whether or not the start check passed =="
{
    printf '%s\n' 'DATABASE_URL=postgresql://litellm@127.0.0.1:5432/litellm'
    for v in $LITELLM_DB_VARS; do
        [ "$v" = DATABASE_URL ] || printf '%s=%s\n' "$v" 'not-a-real-value'
    done
    printf '%s\n' 'LITELLM_MASTER_KEY=test-only-not-a-real-key'
} > "$CFG/llm-gateway.env"
chmod 600 "$CFG/llm-gateway.env"
run_gateway no no yes yes
assert_rc 0 "$RC" "a failed start check still execs litellm, with every database variable in the env file"
for v in $LITELLM_DB_VARS_WITHHELD; do
    assert_eq "" "$(printf '%s\n' "$SEEN_TXT" | grep "^SET $v\$" || true)" "$v is withheld when the start check fails"
done
run_gateway yes yes yes no
assert_rc 0 "$RC" "a passing start check execs litellm, with every database variable in the env file"
assert_contains "$SEEN_TXT" "SET DATABASE_URL" "control: litellm sees DATABASE_URL when the start check passes"
for v in $LITELLM_DB_VARS_WITHHELD; do
    [ "$v" = DATABASE_URL ] && continue
    assert_eq "" "$(printf '%s\n' "$SEEN_TXT" | grep "^SET $v\$" || true)" "$v is withheld when the start check passes"
done

echo "== DATABASE_SCHEMA reaches litellm when the database is exposed: LiteLLM's view check reads it =="
printf '%s\n' \
    'DATABASE_URL=postgresql://litellm@127.0.0.1:5432/litellm' \
    'DATABASE_SCHEMA=gw_schema' \
    'LITELLM_MASTER_KEY=test-only-not-a-real-key' > "$CFG/llm-gateway.env"
chmod 600 "$CFG/llm-gateway.env"
run_gateway yes yes yes no
assert_rc 0 "$RC" "a database exposed with DATABASE_SCHEMA set: the start execs litellm"
assert_contains "$SEEN_TXT" "DATABASE_URL=postgresql://" "control: DATABASE_URL reaches litellm on the same run"
assert_eq "DATABASE_SCHEMA=gw_schema" "$(printf '%s\n' "$SEEN_TXT" | grep '^DATABASE_SCHEMA=' || true)" "DATABASE_SCHEMA reaches litellm with the value it was given"

# The pin and the withholding, run by sourcing the launcher, which returns before its main body.
# GATEWAY_PATH is the pinned PATH; a case overrides it to put the logging prisma back in reach
# after the pin, the one state the default PATH cannot produce.
EXPOSE_URL='postgresql://litellm@127.0.0.1:5432/litellm'
expose() { # <gate passed> <GATEWAY_PATH>: prints the PATH and DATABASE_URL the launcher leaves
    ( . "$LAUNCHER"; GATEWAY_PATH="$2"; _gateway_expose_database "$1" "$EXPOSE_URL" 2>"$SANDBOX/expose.err"; echo "PATH=$PATH DB=${DATABASE_URL:-<unset>}" )
}
# database_vars_set: prints each database variable LiteLLM reads that the current environment
# holds, one per line. Run in a subshell that has exported some of them.
database_vars_set() {
    for v in $LITELLM_DB_VARS; do env | grep -q "^$v=" && echo "$v"; done
}

echo "== a pinned PATH with no prisma CLI exports DATABASE_URL (control for the withheld case) =="
EXPOSED=$(expose yes /usr/bin:/bin)
assert_contains "$EXPOSED" "DB=postgresql://litellm@" "with no prisma CLI on the pinned PATH, DATABASE_URL is exported"

echo "== a prisma CLI that still resolves after the pin withholds DATABASE_URL =="
EXPOSED=$(expose yes "$FAKE_BIN:/usr/bin:/bin")
assert_eq "PATH=$FAKE_BIN:/usr/bin:/bin DB=<unset>" "$EXPOSED" "with the logging prisma reachable after the pin, DATABASE_URL is withheld"
assert_contains "$(cat "$SANDBOX/expose.err")" "prisma CLI resolves on PATH" "the withholding is reported, and names the cause"
assert_contains "$(cat "$SANDBOX/expose.err")" "database password on its command line" "the warning says the password would reach a command line"

# Every name is exported before the withholding runs, so the subshell below starts with each one set.
export_all_db_vars() {
    for v in $LITELLM_DB_VARS; do export "$v=not-a-real-value"; done
}

echo "== control: every database variable is set before the withholding runs =="
BEFORE=$( export_all_db_vars; database_vars_set )
assert_eq "$(for v in $LITELLM_DB_VARS; do echo "$v"; done)" "$BEFORE" "control: each database variable is set when the withholding starts"

echo "== a prisma CLI reachable after the pin withholds every database variable, DATABASE_URL included =="
LEFT=$( export_all_db_vars; . "$LAUNCHER"; GATEWAY_PATH="$FAKE_BIN:/usr/bin:/bin"; _gateway_expose_database yes "$EXPOSE_URL" 2>/dev/null; database_vars_set )
assert_eq "DATABASE_SCHEMA" "$LEFT" "with the logging prisma reachable after the pin, only DATABASE_SCHEMA is left for litellm, and it builds no URL alone"

echo "== with no prisma CLI on the pinned PATH, DATABASE_URL and DATABASE_SCHEMA are the database variables exported =="
LEFT=$( export_all_db_vars; . "$LAUNCHER"; _gateway_expose_database yes "$EXPOSE_URL" 2>/dev/null; database_vars_set )
assert_eq "$(printf 'DATABASE_URL\nDATABASE_SCHEMA')" "$LEFT" "with no prisma CLI on the pinned PATH, DATABASE_URL and DATABASE_SCHEMA reach litellm and nothing else"

echo "== a start whose gate failed still pins PATH, and exports nothing =="
EXPOSED=$(expose no "$FAKE_BIN:/usr/bin:/bin")
assert_eq "PATH=$FAKE_BIN:/usr/bin:/bin DB=<unset>" "$EXPOSED" "a failed gate leaves DATABASE_URL unset and PATH pinned"

echo "== across every run above, the logging prisma was never invoked =="
assert_eq "" "$(cat "$FAKE_PRISMA_LOG" 2>/dev/null || true)" "no run invoked the prisma on the inherited PATH, so no argv or environment carried DATABASE_URL"

finish
