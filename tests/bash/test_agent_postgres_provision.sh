#!/bin/bash
# Behavioral test for bin/agent-postgres-provision (round-1 revise on PR
# #817). Stub initdb/pg_ctl/psql stand in for the real binaries —
# no live Postgres is ever started, initialized or connected to. HOME is a
# sandbox, so the real ~/.local and ~/.orient-control are never reached.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/agent-postgres-provision"
SANDBOX=$(mktemp -d) || { echo "  FATAL: mktemp -d failed"; exit 1; }
trap 'rm -rf "$SANDBOX"' EXIT

INITDB_LOG="$SANDBOX/initdb.log"
PG_CTL_LOG="$SANDBOX/pg_ctl.log"
PSQL_LOG="$SANDBOX/psql.log"
export INITDB_LOG PG_CTL_LOG PSQL_LOG

# initdb: creates PG_VERSION plus initdb's own (soon-overwritten) postgresql.conf,
# and logs its own argv — enough to assert it ran, and ran only once. Its
# pg_hba.conf mirrors what real initdb writes for the given --auth-local
# value (trust, or a password method), and it records whether --pwfile's
# target already existed and was non-empty when initdb ran — the fix for
# the round-2 bootstrap-window finding depends on the password being on
# disk before initdb, not after.
cat > "$SANDBOX/initdb" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$INITDB_LOG"
datadir=""
pwfile=""
authlocal=""
prev=""
for a in "$@"; do
    case "$a" in
        --pwfile=*) pwfile="${a#--pwfile=}" ;;
        --auth-local=*) authlocal="${a#--auth-local=}" ;;
    esac
    [ "$prev" = "-D" ] && datadir="$a"
    prev="$a"
done
if [ -n "$pwfile" ] && [ -s "$pwfile" ]; then
    echo "PWFILE_PRESENT_AT_INITDB=1" >> "$INITDB_LOG"
else
    echo "PWFILE_PRESENT_AT_INITDB=0" >> "$INITDB_LOG"
fi
mkdir -p "$datadir"
echo "15" > "$datadir/PG_VERSION"
echo "local   all             all                                     ${authlocal:-trust}" > "$datadir/pg_hba.conf"
echo "# initdb default, to be overwritten" > "$datadir/postgresql.conf"
exit 0
STUB
chmod +x "$SANDBOX/initdb"

# pg_ctl: start/stop are no-ops beyond logging — nothing here ever has a real
# postmaster behind it. A "start" also snapshots pg_hba.conf as it stands at
# that moment, between markers, so a test can inspect exactly what the
# cluster would have been started with — which, on a fresh initdb, is still
# initdb's own pg_hba.conf, not the final one _write_pg_hba_conf writes only
# after the cluster stops again.
cat > "$SANDBOX/pg_ctl" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PG_CTL_LOG"
case " $* " in
    *" start "*)
        datadir=""
        prev=""
        for a in "$@"; do
            [ "$prev" = "-D" ] && datadir="$a"
            prev="$a"
        done
        echo "PG_HBA_AT_START_BEGIN" >> "$PG_CTL_LOG"
        cat "$datadir/pg_hba.conf" >> "$PG_CTL_LOG" 2>/dev/null
        echo "PG_HBA_AT_START_END" >> "$PG_CTL_LOG"
        ;;
esac
exit 0
STUB
chmod +x "$SANDBOX/pg_ctl"

# psql: the admin call (-f -, reading the role/password SQL from stdin) logs
# it and succeeds. The role-attributes query (-c "SELECT rolsuper, ...")
# answers per PSQL_ATTRS_MODE: safe (every column f, the default), or one
# of superuser/createrole/replication/bypassrls/exec/read/write to flip
# exactly that column to t, or missing (empty result, no such role) or
# unreachable (no server up). The COPY ... TO PROGRAM call answers per
# PSQL_VERIFY_MODE: refuse (permission denied, the safe case, default),
# succeed (the unsafe case --verify must catch) or unreachable (no server
# up).
cat > "$SANDBOX/psql" <<'STUB'
#!/bin/bash
echo "ARGV $*" >> "$PSQL_LOG"
case " $* " in
    *" -f - "*)
        sql=$(cat)
        printf '%s\n' "$sql" >> "$PSQL_LOG"
        echo "ADMIN_SQL_RECEIVED" >> "$PSQL_LOG"
        exit 0
        ;;
    *"rolsuper"*)
        case "${PSQL_ATTRS_MODE:-safe}" in
            safe)        echo "f|f|f|f|f|f|f" ;;
            superuser)   echo "t|f|f|f|f|f|f" ;;
            createrole)  echo "f|t|f|f|f|f|f" ;;
            replication) echo "f|f|t|f|f|f|f" ;;
            bypassrls)   echo "f|f|f|t|f|f|f" ;;
            exec)        echo "f|f|f|f|t|f|f" ;;
            read)        echo "f|f|f|f|f|t|f" ;;
            write)       echo "f|f|f|f|f|f|t" ;;
            missing)     : ;;
            unreachable)
                echo "psql: error: could not connect to server: No such file or directory" >&2
                exit 2
                ;;
        esac
        exit 0
        ;;
    *"COPY"*)
        case "${PSQL_VERIFY_MODE:-refuse}" in
            succeed)
                echo 1
                exit 0
                ;;
            unreachable)
                echo "psql: error: could not connect to server: No such file or directory" >&2
                exit 2
                ;;
            *)
                echo "ERROR:  permission denied to COPY to or from an external program" >&2
                exit 1
                ;;
        esac
        ;;
    *)
        echo "UNEXPECTED psql invocation: $*" >&2
        exit 99
        ;;
esac
STUB
chmod +x "$SANDBOX/psql"

HOME_DIR="$SANDBOX/home"
DATA_DIR="$HOME_DIR/.local/state/agent-platform/postgres/data"
SOCK_DIR="$HOME_DIR/.local/state/agent-platform/postgres/sockets"
PWFILE="$HOME_DIR/.orient-control/agent-postgres-superuser-password"

run_provision() {
    RC=0
    env HOME="$HOME_DIR" \
        AGENT_POSTGRES_INITDB="$SANDBOX/initdb" \
        AGENT_POSTGRES_PG_CTL="$SANDBOX/pg_ctl" \
        AGENT_POSTGRES_PSQL="$SANDBOX/psql" \
        /bin/bash "$SCRIPT" >"$SANDBOX/out" 2>"$SANDBOX/err" || RC=$?
    OUT=$(cat "$SANDBOX/out")
    ERR=$(cat "$SANDBOX/err")
}

echo "== a fresh run initializes the cluster, writes its config, and provisions the role =="
rm -rf "$HOME_DIR"; : > "$INITDB_LOG"; : > "$PG_CTL_LOG"; : > "$PSQL_LOG"
run_provision
assert_rc 0 "$RC" "a fresh run exits 0: $ERR"
assert_eq "1" "$(grep -c '^ARGV' "$INITDB_LOG")" "initdb ran exactly once"
assert_eq "15" "$(cat "$DATA_DIR/PG_VERSION")" "initdb's own stub ran against the real data dir"

echo "== no bootstrap window: initdb itself never trusts anyone, on the granted socket =="
initdb_argv=$(grep '^ARGV' "$INITDB_LOG")
assert_contains "$initdb_argv" "--auth-local=scram-sha-256" "initdb's own auth-local method requires a password"
assert_eq "0" "$(printf '%s\n' "$initdb_argv" | grep -c -- "--auth-local=trust" || true)" "initdb is never told to trust local connections"
assert_contains "$initdb_argv" "--pwfile=$PWFILE" "initdb is handed the admin's password file by path"
assert_eq "0" "$(printf '%s\n' "$initdb_argv" | grep -c -- "--auth-local=reject" || true)" "the chosen method is scram-sha-256, not reject (the admin role still needs to connect)"
assert_contains "$(cat "$INITDB_LOG")" "PWFILE_PRESENT_AT_INITDB=1" "the password file exists and is non-empty before initdb ever runs"
pw1_check=$(cat "$PWFILE")
assert_eq "0" "$(grep -c "$pw1_check" "$INITDB_LOG" || true)" "the password itself never reaches initdb's argv"
pg_hba_at_start=$(sed -n '/PG_HBA_AT_START_BEGIN/,/PG_HBA_AT_START_END/p' "$PG_CTL_LOG" | sed '1d;$d')
assert_eq "0" "$(printf '%s\n' "$pg_hba_at_start" | grep -ciE 'trust' || true)" \
    "the pg_hba.conf the cluster is actually started with (initdb's own, on a fresh provision) trusts nobody"

echo "== postgresql.conf disables TCP and points at the dedicated socket dir =="
conf=$(cat "$DATA_DIR/postgresql.conf")
assert_contains "$conf" "listen_addresses = ''" "no TCP listener"
assert_contains "$conf" "unix_socket_directories = '$SOCK_DIR'" "socket dir is the dedicated one"
assert_contains "$conf" "port = 5433" "a distinct port from the main cluster's 5432"
assert_eq "0" "$(printf '%s\n' "$conf" | grep -c "listen_addresses = '\*'" || true)" "TCP is never opened wide"

echo "== pg_hba.conf trusts only agent, and rejects everyone else on this socket =="
hba=$(cat "$DATA_DIR/pg_hba.conf")
body=$(printf '%s\n' "$hba" | grep -vE '^[[:space:]]*#')
trust_lines=$(printf '%s\n' "$body" | grep -cE 'trust[[:space:]]*$')
assert_eq "1" "$trust_lines" "exactly one trust line in the managed pg_hba.conf"
assert_eq "1" "$(printf '%s\n' "$body" | grep -cE '^local[[:space:]]+all[[:space:]]+agent[[:space:]]+trust[[:space:]]*$')" \
    "agent is the one trusted role"
assert_eq "1" "$(printf '%s\n' "$body" | grep -cE '^local[[:space:]]+all[[:space:]]+agent_admin[[:space:]]+scram-sha-256[[:space:]]*$')" \
    "the bootstrap superuser needs a password, not trust"
assert_eq "1" "$(printf '%s\n' "$body" | grep -cE '^local[[:space:]]+all[[:space:]]+all[[:space:]]+reject[[:space:]]*$')" \
    "everything else on this socket is rejected"

echo "== the role SQL grants nothing it should not =="
sql=$(cat "$PSQL_LOG")
assert_contains "$sql" "NOSUPERUSER" "agent is never made a superuser"
assert_contains "$sql" "NOCREATEROLE" "agent cannot create roles"
assert_contains "$sql" "NOREPLICATION" "agent has no replication privilege"
assert_contains "$sql" "NOBYPASSRLS" "agent cannot bypass row security"
assert_contains "$sql" "REVOKE pg_execute_server_program FROM agent" "no server-program execution"
assert_contains "$sql" "REVOKE pg_read_server_files FROM agent" "no server-side file reads"
assert_contains "$sql" "REVOKE pg_write_server_files FROM agent" "no server-side file writes"
super_count=$(printf '%s' "$sql" | grep -o 'SUPERUSER' | wc -l | tr -d ' ')
nosuper_count=$(printf '%s' "$sql" | grep -o 'NOSUPERUSER' | wc -l | tr -d ' ')
assert_eq "$nosuper_count" "$super_count" "every SUPERUSER occurrence is part of NOSUPERUSER — none is a bare grant"
assert_eq "0" "$(printf '%s\n' "$sql" | grep -c 'GRANT pg_execute_server_program\|GRANT pg_read_server_files\|GRANT pg_write_server_files' || true)" "none of the three predefined roles is ever granted"

echo "== the password file is created once, under the denied directory, mode 0600 =="
assert_eq "1" "$([ -f "$PWFILE" ] && echo 1 || echo 0)" "the password file exists"
assert_eq "-rw-------" "$(ls -l "$PWFILE" | cut -c1-10)" "the password file is mode 0600"
pw1=$(cat "$PWFILE")
assert_eq "0" "$(printf '%s\n%s\n' "$OUT" "$ERR" | grep -c "$pw1" || true)" "the password never reaches stdout or stderr"

echo "== a rerun is idempotent: initdb does not run again, and the password is reused =="
: > "$INITDB_LOG"; : > "$PSQL_LOG"
run_provision
assert_rc 0 "$RC" "a rerun exits 0: $ERR"
assert_eq "0" "$(grep -c '^ARGV' "$INITDB_LOG")" "a rerun does not re-initdb"
pw2=$(cat "$PWFILE")
assert_eq "$pw1" "$pw2" "the password file is not regenerated on a rerun"
assert_contains "$(cat "$PSQL_LOG")" "ALTER ROLE agent_admin WITH PASSWORD '$pw1'" "the same password is reapplied"

echo "== --verify: refused COPY ... TO PROGRAM is a pass (exit 0) =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_VERIFY_MODE=refuse \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 0 "$RC" "a refused COPY TO PROGRAM passes --verify"

echo "== --verify: a successful COPY ... TO PROGRAM fails loudly (exit 1) =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_VERIFY_MODE=succeed \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "a successful COPY TO PROGRAM fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "FAIL" "the failure is named, not silent"

echo "== --verify: an unreachable cluster is a distinct operational exit (2) =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_VERIFY_MODE=unreachable \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 2 "$RC" "an unreachable cluster is reported separately from a refused COPY"

echo "== --verify: a superuser agent role fails, before the COPY probe even runs =="
RC=0
: > "$PSQL_LOG"
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=superuser \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "a superuser agent role fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "SUPERUSER" "the failure names the attribute"
assert_eq "0" "$(grep -c 'COPY' "$PSQL_LOG" || true)" "the COPY probe never runs once the attribute check already failed"

echo "== --verify: CREATEROLE fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=createrole \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "CREATEROLE fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "CREATEROLE" "the failure names the attribute"

echo "== --verify: REPLICATION fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=replication \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "REPLICATION fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "REPLICATION" "the failure names the attribute"

echo "== --verify: BYPASSRLS fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=bypassrls \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "BYPASSRLS fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "BYPASSRLS" "the failure names the attribute"

echo "== --verify: holding pg_execute_server_program (even by inheritance) fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=exec \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "pg_execute_server_program fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "pg_execute_server_program" "the failure names the role"

echo "== --verify: holding pg_read_server_files (even by inheritance) fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=read \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "pg_read_server_files fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "pg_read_server_files" "the failure names the role"

echo "== --verify: holding pg_write_server_files (even by inheritance) fails =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=write \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "pg_write_server_files fails --verify"
assert_contains "$(cat "$SANDBOX/verr")" "pg_write_server_files" "the failure names the role"

echo "== --verify: no such role fails closed rather than passing on empty evidence =="
RC=0
env HOME="$HOME_DIR" AGENT_POSTGRES_PSQL="$SANDBOX/psql" PSQL_ATTRS_MODE=missing \
    /bin/bash "$SCRIPT" --verify >"$SANDBOX/vout" 2>"$SANDBOX/verr" || RC=$?
assert_rc 1 "$RC" "an empty role-attributes result fails --verify rather than passing it"

echo "== an unknown option is refused =="
RC=0
env HOME="$HOME_DIR" /bin/bash "$SCRIPT" --bogus >"$SANDBOX/out2" 2>"$SANDBOX/err2" || RC=$?
assert_rc 2 "$RC" "an unknown option exits 2"

finish
