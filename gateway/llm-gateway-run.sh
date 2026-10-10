#!/bin/bash
# Launch the LiteLLM gateway with provider credentials sourced from a secrets
# file rather than carried in the launchd plist (ARCH-342).
#
# Why this exists: the plist previously held six provider API keys as
# EnvironmentVariables. They were never committed (the plist is gitignored), but
# a plist is the wrong home for them — it is world-readable by default, it is
# copied to ~/Library/LaunchAgents on deploy, and `launchctl print` renders the
# values to anyone who can run it. Secrets belong in one 0600 file that nothing
# copies.
#
# bash 3.2 only (ARCH-082): no declare -A, no mapfile, no ${var,,}.
# Absolute paths only (ARCH-086): launchd gives this no useful PATH.

set -u

# NORMALISE TO THE LOW THREE OCTAL DIGITS AND DECIDE, in one function so a
# test can drive every case with a raw stat string rather than a real `stat`
# call. BSD's `%Lp` reports the low bits as three digits and drops
# setuid/setgid/sticky; GNU's `%a` reports FOUR digits when one is set — the
# low three digits are what both forms agree on and what actually governs
# read access (0600 and 2600 are equally private: a setuid, setgid or sticky
# bit on a data file grants no read access, and macOS has always accepted
# both, so normalising down preserves that behaviour rather than tightening
# it silently on one platform).
#
# Prints the normalised mode on stdout. Exits 0 if it is 600 or 400 (accept),
# 1 if it parsed to some other valid octal mode (refuse — no point retrying
# the other stat form), 2 if the text wasn't an octal mode at all (unparsable
# — the caller retries with the other form). The live path below always calls
# this with the output of the two hardcoded /usr/bin/stat invocations: nothing
# makes the stat binary itself caller-controlled, because a guard whose whole
# job is refusing a world-readable secrets file must not be pointable at a
# fake stat that prints 600.
_gateway_normalise_mode() { # <raw octal text from stat>
  local raw=$1 perms=""
  case "$raw" in
    ''|*[!0-7]*)             ;;                    # not an octal mode at all
    [0-7][0-7][0-7][0-7])     perms="${raw#?}" ;;   # four digits, drop the special bit
    *)                        perms="$raw" ;;       # one to three octal digits
  esac
  echo "$perms"
  case "$perms" in
    600|400) return 0 ;;
    "")      return 2 ;;
    *)       return 1 ;;
  esac
}

# VIRTUAL KEYS NEED A DATABASE (E17·1). LiteLLM keeps them in postgres, so /key/generate
# answers 500 "DB not connected" without one. Setup runs once per install, through
# `--provision-db` (install.sh calls it). It creates the role and database, gives the role
# a generated password, writes DATABASE_URL with that password into llm-gateway.env,
# generates the prisma client and applies LiteLLM's migrations. A start only checks: it
# exports DATABASE_URL when the URL logs in, the schema is present and the client is
# generated. Otherwise the gateway starts without the database, because model traffic
# needs none and a missing database must not take the gateway down.
#
# psql is LLM_GATEWAY_PSQL, else whatever PATH resolves. LLM_GATEWAY_DB_ROLE and
# LLM_GATEWAY_DB_NAME name the role and database (both default litellm). Provisioning
# writes them into SQL, so each must be a lowercase identifier; anything else is refused.
#
# THE PASSWORD NEVER REACHES ARGV OR OUTPUT. Logins pass it through PGPASSWORD. The admin
# SQL that sets it goes to psql on stdin from a 0600 temp file, and psql's output for that
# call is discarded, because a failed statement echoes its own text. Prisma's output is
# discarded for the same reason. The launcher reads the password from the URL as written
# and does not percent-decode it, so a password needs only unreserved characters.

_gateway_psql() {
  if [ -n "${LLM_GATEWAY_PSQL:-}" ]; then
    echo "$LLM_GATEWAY_PSQL"
  else
    command -v psql 2>/dev/null
  fi
}

_gateway_valid_identifier() { # <name>
  case "$1" in
    ''|[0-9]*|*[!a-z0-9_]*) return 1 ;;
    *) return 0 ;;
  esac
}

# SPLITS postgresql://[user[:password]@]host[:port]/db[?params]. Sets URL_SCHEME (through
# the ://), URL_USER, URL_PASSWORD and URL_REST (the host onward). Login passes the password
# through PGPASSWORD, so it never reaches argv.
_gateway_split_url() { # <database url>
  local userinfo
  URL_SCHEME="${1%%://*}://"
  URL_REST="${1#*://}"
  URL_USER=""
  URL_PASSWORD=""
  case "$URL_REST" in
    *@*)
      userinfo="${URL_REST%%@*}"
      URL_REST="${URL_REST#*@}"
      URL_USER="${userinfo%%:*}"
      case "$userinfo" in
        *:*) URL_PASSWORD="${userinfo#*:}" ;;
      esac
      ;;
  esac
}

# Runs one statement as the URL's login and prints its output. Connect and statement
# timeouts keep a hung postgres from hanging the start.
_gateway_sql() { # <psql> <database url> <sql>
  _gateway_split_url "$2"
  (
    if [ -n "$URL_PASSWORD" ]; then
      PGPASSWORD="$URL_PASSWORD"
      export PGPASSWORD
    fi
    PGCONNECT_TIMEOUT=5
    PGOPTIONS='-c statement_timeout=5000'
    export PGCONNECT_TIMEOUT PGOPTIONS
    "$1" -X -q -t -A -d "$URL_SCHEME${URL_USER:+$URL_USER@}$URL_REST" -c "$3" 2>/dev/null
  )
}

_gateway_login_ok() { # <psql> <database url>
  _gateway_sql "$1" "$2" 'select 1' >/dev/null
}

_LITELLM_TABLE_SQL="select count(*) from information_schema.tables where table_name = 'LiteLLM_VerificationToken'"

_gateway_schema_present() { # <psql> <database url>
  [ "$(_gateway_sql "$1" "$2" "$_LITELLM_TABLE_SQL")" = "1" ]
}

# The admin connection is the local superuser over the default socket, as before.
_gateway_admin() { # <psql> <sql file>
  PGCONNECT_TIMEOUT=5 "$1" -X -q -d postgres -v ON_ERROR_STOP=1 -f - < "$2" >/dev/null 2>&1
}

_gateway_new_password() {
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

# Rewrites llm-gateway.env with DATABASE_URL replaced. The new file is built in a 0600 temp
# file beside the old one and moved over it, so the secrets are never world-readable. No
# line is echoed. The value is single-quoted, because this file is sourced: an unquoted
# query string with a & would make bash run the rest of the URL as a command.
_gateway_write_database_url() { # <database url>
  local tmp line quoted
  tmp=$(mktemp "${ENV_FILE}.XXXXXX") || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      DATABASE_URL=*) ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$ENV_FILE" > "$tmp"
  quoted=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
  printf "DATABASE_URL='%s'\n" "$quoted" >> "$tmp"
  if ! mv "$tmp" "$ENV_FILE"; then
    rm -f "$tmp"
    return 1
  fi
}

# Runs a command with a time limit and discards its output. Bash 3.2 on macOS has no
# timeout(1), so a watchdog stops the command after $1 seconds. Job control gives the command
# and the watchdog each a process group of their own. A stop signals the whole group, so
# prisma's node child stops with the command. The watchdog's group is signalled before the
# watchdog is reaped, which ends its sleep. Job control also takes the command out of the
# launcher's group, so a Ctrl-C at the terminal no longer reaches it. The trap forwards INT,
# TERM and HUP to both groups and exits the shell it runs in. Call this from the launcher itself,
# never from a subshell: there the exit ends only the subshell, and the launcher reports 1.
_gateway_timed() { # <seconds> <command...>
  local secs="$1" pid watchdog rc
  shift
  set -m
  "$@" </dev/null >/dev/null 2>&1 &
  pid=$!
  ( sleep "$secs"; kill -TERM -- "-$pid" ) >/dev/null 2>&1 &
  watchdog=$!
  set +m
  trap "_gateway_timed_interrupt $pid $watchdog 130" INT
  trap "_gateway_timed_interrupt $pid $watchdog 143" TERM
  trap "_gateway_timed_interrupt $pid $watchdog 129" HUP
  wait "$pid"
  rc=$?
  trap - INT TERM HUP
  kill -TERM -- "-$watchdog" >/dev/null 2>&1
  wait "$watchdog" 2>/dev/null
  return $rc
}

# Stops the command's group and the watchdog's group, then exits the shell it runs in with the
# status the shell gives for the signal: 130 for INT, 143 for TERM, 129 for HUP. Called from the
# launcher, that exits the launcher with that status and prints nothing. The launcher never
# returns to _gateway_prisma_failed after an interrupt, so that function names only a step the
# watchdog stopped or one that failed.
_gateway_timed_interrupt() { # <command pid> <watchdog pid> <status>
  kill -TERM -- "-$1" >/dev/null 2>&1
  kill -TERM -- "-$2" >/dev/null 2>&1
  exit "$3"
}

# LiteLLM's database code imports the prisma package, and a client that was never
# generated fails there. A gateway started with DATABASE_URL in that state can crash-loop
# under KeepAlive and take every model down, so DATABASE_URL is exported only after this
# passes. The constructor call matches the one LiteLLM makes when it builds its database
# client.
_gateway_prisma_ready() { # <venv python>
  "$1" -c 'from prisma import Prisma; Prisma()' >/dev/null 2>&1
}

# The schema sits beside the migrations that LiteLLM applies, in litellm_proxy_extras.
_gateway_schema_path() { # <venv python>
  "$1" -c 'import os, litellm_proxy_extras as e; print(os.path.join(os.path.dirname(e.__file__), "schema.prisma"))' 2>/dev/null
}

PRISMA_TIMEOUT="${LLM_GATEWAY_PRISMA_TIMEOUT:-300}"

# Names a prisma step that did not finish. The watchdog stops a step with SIGTERM, which bash
# reports as exit status 143, and that is the only status named a timeout. Any other status is a
# plain failure. A step that another process stops with SIGTERM also reports 143, so it is named
# a timeout too.
_gateway_prisma_failed() { # <step> <exit status>
  if [ "$2" -eq 143 ]; then
    echo "llm-gateway: prisma $1 ran past ${PRISMA_TIMEOUT}s and was stopped; virtual keys stay unavailable" >&2
  else
    echo "llm-gateway: prisma $1 failed; virtual keys stay unavailable" >&2
  fi
}

# Generates the client and applies the migrations, each under PRISMA_TIMEOUT. The
# migrations run with DATABASE_URL in the environment, and not in argv. Used by
# --provision-db only, so the network fetch that prisma may need runs once, at install.
# Both prisma steps get the venv's bin first on PATH: the schema's generator is the
# prisma-client-py console script installed there, and the launcher's own PATH does not
# carry it. `/usr/bin/env` sets the PATH for the prisma process alone, and this shell keeps its own.
_gateway_prepare_schema() { # <venv python> <database url> <psql>
  local schema rc venvbin
  venvbin="${1%/*}"
  schema=$(_gateway_schema_path "$1") || schema=""
  if [ -z "$schema" ]; then
    echo "llm-gateway: cannot find litellm's schema.prisma; virtual keys stay unavailable" >&2
    return 1
  fi
  if ! _gateway_prisma_ready "$1"; then
    _gateway_timed "$PRISMA_TIMEOUT" /usr/bin/env PATH="$venvbin:$PATH" "$1" -m prisma generate --schema "$schema"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      _gateway_prisma_failed "generate" "$rc"
      return 1
    fi
    if ! _gateway_prisma_ready "$1"; then
      echo "llm-gateway: prisma generate ran but the client still does not load" >&2
      return 1
    fi
  fi
  # Exported for the migrations only, and unset after them. _gateway_timed runs in this shell so
  # that an INT, TERM or HUP to the launcher stops the migrations and exits the launcher with that
  # signal's status (130, 143 or 129).
  export DATABASE_URL="$2"
  _gateway_timed "$PRISMA_TIMEOUT" /usr/bin/env PATH="$venvbin:$PATH" "$1" -m prisma migrate deploy --schema "$schema"
  rc=$?
  unset DATABASE_URL
  if [ "$rc" -ne 0 ]; then
    _gateway_prisma_failed "migrate deploy" "$rc"
    return 1
  fi
  if ! _gateway_schema_present "$3" "$2"; then
    echo "llm-gateway: migrations ran but the LiteLLM schema is still missing" >&2
    return 1
  fi
}

# --provision-db. Creates the role, the database, the role's password and DATABASE_URL.
# A URL that already logs in is kept, with its password, so a rerun issues no new password.
_gateway_provision() { # <venv python>
  local psql role name pw sql url
  if [ -z "$DB_URL" ]; then
    echo "llm-gateway: DATABASE_URL is not set; add it to $ENV_FILE without a password, then rerun install.sh" >&2
    return 1
  fi
  psql=$(_gateway_psql)
  if [ -z "$psql" ]; then
    echo "llm-gateway: psql not found on PATH and LLM_GATEWAY_PSQL is unset; virtual keys stay unavailable" >&2
    return 1
  fi
  role="${LLM_GATEWAY_DB_ROLE:-litellm}"
  name="${LLM_GATEWAY_DB_NAME:-litellm}"
  if ! _gateway_valid_identifier "$role" || ! _gateway_valid_identifier "$name"; then
    echo "llm-gateway: LLM_GATEWAY_DB_ROLE and LLM_GATEWAY_DB_NAME must be lowercase identifiers" >&2
    return 1
  fi
  if _gateway_login_ok "$psql" "$DB_URL"; then
    url="$DB_URL"
  else
    pw=$(_gateway_new_password)
    sql=$(mktemp) || return 1
    # Each \gexec runs only when its guard finds nothing. The ALTER runs on every provision
    # that reaches it, so the role's password is always the one written to the URL.
    cat > "$sql" <<SQL
SELECT 'CREATE ROLE ${role} LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${role}')\gexec
ALTER ROLE ${role} LOGIN PASSWORD '${pw}';
SELECT 'CREATE DATABASE ${name} OWNER ${role}' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${name}')\gexec
SQL
    if ! _gateway_admin "$psql" "$sql"; then
      rm -f "$sql"
      echo "llm-gateway: could not create role $role and database $name as the local postgres superuser; output is withheld because it can carry the password" >&2
      return 1
    fi
    rm -f "$sql"
    _gateway_split_url "$DB_URL"
    url="$URL_SCHEME$role:$pw@$URL_REST"
    if ! _gateway_write_database_url "$url"; then
      echo "llm-gateway: could not write DATABASE_URL to $ENV_FILE" >&2
      return 1
    fi
  fi
  if ! _gateway_login_ok "$psql" "$url"; then
    echo "llm-gateway: DATABASE_URL does not connect after provisioning $name; check the role's password and pg_hba.conf" >&2
    return 1
  fi
  _gateway_prepare_schema "$1" "$url" "$psql"
}

# Retries the login until it succeeds or LLM_GATEWAY_LOGIN_WAIT seconds have passed. The
# interval is LLM_GATEWAY_LOGIN_INTERVAL seconds.
_gateway_wait_for_login() { # <psql> <database url>
  local deadline=$(( SECONDS + ${LLM_GATEWAY_LOGIN_WAIT:-60} ))
  until _gateway_login_ok "$1" "$2"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep "${LLM_GATEWAY_LOGIN_INTERVAL:-5}"
  done
}

# The start check. It creates nothing and fetches nothing: DATABASE_URL must log in, the
# schema must exist and the prisma client must be generated. install.sh does the creating.
# launchd starts this job and postgres at boot with no ordering between them, so a login
# refused at first is often a server that is not up yet. The login is retried for
# LLM_GATEWAY_LOGIN_WAIT seconds (default 60), every LLM_GATEWAY_LOGIN_INTERVAL seconds
# (default 5). The gate does not run again, so a start that gives up runs without the
# database until the gateway restarts.
_gateway_start_gate() { # <venv python> <database url>
  local psql
  psql=$(_gateway_psql)
  if [ -z "$psql" ]; then
    echo "llm-gateway: psql not found on PATH and LLM_GATEWAY_PSQL is unset" >&2
    return 1
  fi
  if ! _gateway_wait_for_login "$psql" "$2"; then
    echo "llm-gateway: DATABASE_URL does not connect; the login was refused or the server is down after ${LLM_GATEWAY_LOGIN_WAIT:-60}s" >&2
    echo "llm-gateway: if the role or its password changed, run $GATEWAY_LIB/llm-gateway-run.sh --provision-db; otherwise restart the gateway once postgres accepts logins" >&2
    return 1
  fi
  if ! _gateway_schema_present "$psql" "$2"; then
    echo "llm-gateway: the LiteLLM schema is missing from the database; run $GATEWAY_LIB/llm-gateway-run.sh --provision-db" >&2
    return 1
  fi
  if ! _gateway_prisma_ready "$1"; then
    echo "llm-gateway: the prisma client is missing or not generated; run $GATEWAY_LIB/llm-gateway-run.sh --provision-db" >&2
    return 1
  fi
}

# LiteLLM is started by its absolute path, so it needs no PATH entry of its own. The entries are
# system directories only: the venv's bin holds the prisma wheel's executable, and a node or npm
# directory is where a prisma CLI comes from.
GATEWAY_PATH=/usr/bin:/bin:/usr/sbin:/sbin

# Pins the PATH that LiteLLM inherits, then unsets the variables LiteLLM 1.86.2 reads to find or
# build a database URL, whether or not the start gate passed. Unsetting first means none of the
# unset names reaches LiteLLM from the launcher's environment. A LiteLLM upgrade needs this list
# re-read.
# The launcher's environment is the only source it covers: LiteLLM's load_dotenv() in DEV mode
# (litellm/proxy/proxy_cli.py:13, :28-30) fills any variable still unset from a .env that it
# finds by walking up from the package. No .env exists in any of those directories today. A
# .env placed there later would reopen the gap.
#
# DATABASE_SCHEMA is not unset. LiteLLM's view check reads it on the exposed path
# (litellm/proxy/utils.py:2981, a pg_views query in that schema, default public), and nothing
# else in the list reads it there. It cannot build a URL alone. The URL builder
# (litellm/proxy/utils.py:5955, :5970) needs DATABASE_HOST, DATABASE_USERNAME and DATABASE_NAME,
# and proxy_cli.py:906-913 calls it only when the config has no database_url and DATABASE_URL
# is unset. Those three are unset here on both paths, so DATABASE_SCHEMA appends to nothing.
#
# DATABASE_URL is exported only when the start gate passed and no prisma CLI resolves on the
# pinned PATH. LiteLLM runs `prisma migrate diff --from-url <DATABASE_URL>` whenever a CLI
# resolves there, and that puts the password on argv.
# Returns 1 when a CLI resolves. <gate passed: yes or no> <database url>
_gateway_expose_database() {
  PATH="$GATEWAY_PATH"
  export PATH
  unset DATABASE_URL DIRECT_URL DATABASE_URL_READ_REPLICA IAM_TOKEN_DB_AUTH \
    DATABASE_HOST DATABASE_PORT DATABASE_USER DATABASE_USERNAME DATABASE_PASSWORD \
    DATABASE_NAME \
    DATABASE_HOST_READ_REPLICA DATABASE_PORT_READ_REPLICA DATABASE_USER_READ_REPLICA \
    DATABASE_USERNAME_READ_REPLICA DATABASE_PASSWORD_READ_REPLICA DATABASE_NAME_READ_REPLICA \
    DATABASE_SCHEMA_READ_REPLICA
  [ "$1" = yes ] || return 0
  if command -v prisma >/dev/null 2>&1; then
    echo "llm-gateway: a prisma CLI resolves on PATH; LiteLLM would run prisma migrate diff with the database password on its command line, so every database variable is withheld and the gateway starts without the database" >&2
    return 1
  fi
  export DATABASE_URL="$2"
}

# Sourceable for _gateway_normalise_mode alone (the test suite does this)
# without running the launcher itself under a shell that isn't launchd.
if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then
  return 0
fi

# LLM_GATEWAY_CONFIG_DIR holds llm-gateway.env and litellm-keys.json; the mint and the
# key readers read the same setting. LLM_GATEWAY_ENV_FILE overrides the env file alone.
CONFIG_DIR="${LLM_GATEWAY_CONFIG_DIR:-$HOME/.config/convivy}"
ENV_FILE="${LLM_GATEWAY_ENV_FILE:-$CONFIG_DIR/llm-gateway.env}"
LITELLM="$HOME/.local/venv-llm-gateway/bin/litellm"
PY="${LITELLM%/*}/python"
# The same names install.sh uses for the copy it pins, so the messages name the installed file.
LOCAL_LIB="$HOME/.local/lib"
GATEWAY_LIB="$LOCAL_LIB/llm-gateway"
CONFIG="$GATEWAY_LIB/config.yaml"

# Fail loudly and specifically. A gateway that starts without credentials
# answers every request with an upstream auth error, which reads like a provider
# outage and costs an hour to trace back to a missing file.
if [ ! -f "$ENV_FILE" ]; then
  echo "llm-gateway: secrets file not found: $ENV_FILE" >&2
  echo "llm-gateway: refusing to start without provider credentials." >&2
  echo "llm-gateway: create it 0600 with KEY=value lines; see gateway/README" >&2
  exit 78   # EX_CONFIG
fi

# Refuse a world- or group-readable secrets file. This is the regression guard:
# the defect being fixed was a 0644 file holding six live API keys, and without
# this check the next person to create the file by hand reintroduces it.
# PORTABLE MODE READ, AND THE ANSWER IS VALIDATED RATHER THAN THE EXIT CODE.
#
# `stat -f '%Lp'` is BSD. On GNU coreutils `-f` is `--file-system`, so the same
# line parses as two operands: `%Lp`, which is no such file and whose error the
# redirect swallows, and the real file, whose multi-line FILESYSTEM DUMP goes to
# stdout. The `|| echo ""` fallback could not save it — `PERMS` held the dump,
# the `case` below fell through to the refusal branch, and a 0600 file that
# SATISFIES this check was refused, with a filesystem description printed where
# a mode should be. The message sent whoever debugged it at the secrets file
# rather than at `stat`. Measured in debian:bookworm-slim, not reasoned about
# (Shipwright, 2026-09-21).
#
# A `||` CHAIN CANNOT FIX THIS, which is why each form is tried and its OUTPUT
# is checked: the wrong form's failure mode is a wrong answer ON STDOUT AND a
# non-zero exit — `||` reacts to the exit and could fire a fallback, but the
# wrong answer is already captured inside the command substitution before
# `||` is evaluated, so the fallback's output only appends after it, never
# replaces it. An unreadable file leaves `PERMS` empty and still reaches the
# refusal branch, which is the safe direction for a guard whose whole job is
# refusing a world-readable secrets file.
#
# AND THIS IS WHY IT SURVIVED SO LONG: THE OLD LINE'S EXIT STATUS WAS 0 ON BOTH
# PLATFORMS — on BSD because `stat` succeeded, on GNU because `|| echo ""`
# rescued it. So the one cheap check, running the line and reading `$?`,
# returned the reassuring answer in the working case and the broken one alike.
# A check whose passing and failing states produce the same evidence is not a
# check. The only signal the old line left was `PERMS` itself: three bytes on
# BSD (`600`) against a multi-line dump on GNU. Measured here on real BSD
# `stat` and against a GNU-behaviour stub, and note that
# `|| echo ""` is invisible even in the content — `echo ""` contributes one
# newline and command substitution strips trailing newlines, so `PERMS` is
# byte-identical whether the fallback fired or not. The watchdog's `|| echo 0`
# differs on exactly that point: its 0 survives as a last line, which is what
# made the corruption visible there.
#
# SO IF YOU CHANGE A `stat` CALL IN THIS REPO, PICK A FALLBACK WHOSE FAILURE
# LEAVES A DIFFERENT MARK THAN ITS SUCCESS, and check the mark, not `$?`.
# SPELLED OUT RATHER THAN LOOPED OVER A FORMAT VARIABLE, on purpose. A loop
# like `for f in "-c %a" "-f %Lp"; do stat $f ...` relies on the shell word-
# splitting `$f` into two arguments, which bash and sh do and zsh does not —
# under zsh the whole string arrives as one argument and the format becomes
# " %Lp", whose output has a leading space and fails the check below. That cost
# me a confused half hour testing this fix in the wrong shell. Two calls need no
# splitting and behave the same everywhere.
# NORMALISED TO THE LOW THREE OCTAL DIGITS AND DECIDED by
# _gateway_normalise_mode, defined above: measured on this machine against a
# GNU-behaviour stub —
#
#     chmod 2600   BSD %Lp -> 600    GNU %a -> 2600
#     chmod 4600   BSD %Lp -> 600    GNU %a -> 4600
#
# — so without normalising, a setgid secrets file is ACCEPTED on macOS and
# REFUSED on Linux, the same file with two answers, which is the bug class
# above one layer up.
PERMS=""
_stat_out=$(/usr/bin/stat -c '%a' "$ENV_FILE" 2>/dev/null) || true
PERMS=$(_gateway_normalise_mode "$_stat_out")
_mode_rc=$?
if [ "$_mode_rc" -eq 2 ]; then
  _stat_out=$(/usr/bin/stat -f '%Lp' "$ENV_FILE" 2>/dev/null) || true
  PERMS=$(_gateway_normalise_mode "$_stat_out")
  _mode_rc=$?
fi
unset _stat_out
if [ "$_mode_rc" -ne 0 ]; then
  echo "llm-gateway: $ENV_FILE has mode ${PERMS:-unknown}; refusing to start." >&2
  echo "llm-gateway: run: chmod 600 $ENV_FILE" >&2
  exit 78
fi
unset _mode_rc

# Source it. `set -a` exports everything the file defines without each line
# needing its own export, which keeps the file a plain KEY=value list that other
# tooling can read.
set -a
# shellcheck source=/dev/null
if ! . "$ENV_FILE"; then
  set +a
  echo "llm-gateway: failed to source $ENV_FILE (syntax error?)" >&2
  echo "llm-gateway: refusing to start without provider credentials." >&2
  exit 78
fi
set +a

# Sourcing can succeed on a file that defines nothing — truncated by a failed
# edit, or emptied. Without this the gateway starts clean and then answers every
# request with an upstream auth error, which reads like a provider outage. Check
# one required key rather than all six: a partial file is a different failure
# from an empty one, and litellm reports a missing single provider clearly.
if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "llm-gateway: $ENV_FILE defines no LITELLM_MASTER_KEY." >&2
  echo "llm-gateway: refusing to start without provider credentials." >&2
  exit 78
fi

# The sourced file exported DATABASE_URL. Hold it aside and re-export it only after the
# start check passes (see _gateway_start_gate).
DB_URL="${DATABASE_URL:-}"
unset DATABASE_URL

# install.sh runs this once per install, so the role, password, schema and prisma client
# are in place before the gateway's first start.
if [ "${1:-}" = "--provision-db" ]; then
  _gateway_provision "$PY"
  exit $?
fi

DB_OK=no
if [ -n "$DB_URL" ]; then
  if _gateway_start_gate "$PY" "$DB_URL"; then
    DB_OK=yes
  else
    echo "llm-gateway: starting without the database; virtual keys stay unavailable until it connects and the gateway restarts" >&2
  fi
fi
_gateway_expose_database "$DB_OK" "$DB_URL"

# DISABLE_SCHEMA_UPDATE=true makes LiteLLM skip its `prisma migrate deploy` step. It does not
# stop LiteLLM's drift check: that check runs `prisma migrate diff --from-url <DATABASE_URL>`
# whenever a prisma CLI resolves on LiteLLM's PATH, and _gateway_expose_database withholds every
# database variable in that case. --provision-db applies the schema at install, so a LiteLLM
# upgrade needs --provision-db to run again.
export DISABLE_SCHEMA_UPDATE=true

exec "$LITELLM" --config "$CONFIG" --port 4000 --host 127.0.0.1
