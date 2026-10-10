# LiteLLM gateway

The gateway is a long-running daemon that serves `127.0.0.1:4000` and fronts the
non-Anthropic providers. This deployment runs it as the launchd job
`com.convivy.llm-gateway`, and its agents route `co -m deepseek` and the `scout` agent's
DeepSeek lane through it.

## Configuration directory

`LLM_GATEWAY_CONFIG_DIR` names the directory that holds `llm-gateway.env` and
`litellm-keys.json`. It defaults to `~/.config/convivy`. The launcher, the mint, and both
key readers (`lib/agent-platform/gateway-vkey` and `bin/agentctl`) read this one setting,
so exporting it moves all of them together. `LLM_GATEWAY_ENV_FILE` overrides only the env
file.

## Credentials

Provider keys and the database URL live in `llm-gateway.env` in that directory, mode
0600, one `KEY=value` line each. This is this deployment's example:

```
DEEPSEEK_API_KEY=...
DATABASE_URL=postgresql://litellm@127.0.0.1:5432/litellm
LITELLM_MASTER_KEY=...
MOONSHOT_API_KEY=...
OPENAI_API_KEY=...
XIAOMI_MIMO_API_KEY=...
ZAI_API_KEY=...
```

Write `DATABASE_URL` without a password. `install.sh` adds the password when it provisions
the database. Quote any value that contains `&`, because the file is sourced.

`llm-gateway-run.sh` sources that file and execs litellm. It exits 78 (`EX_CONFIG`)
with a named reason if the file is missing or its mode is looser than 0600.

## Database and virtual keys

LiteLLM keeps virtual keys in postgres. Without a database, `POST /key/generate`
answers 500 `DB not connected`. Setup is one step, run by `install.sh`, and it takes four
inputs.

1. **Install postgres and make `psql` findable.** The launcher uses `LLM_GATEWAY_PSQL`
   if it is set, and otherwise the first `psql` on `PATH`. launchd gives the job only the
   system `PATH`, so a postgres installed elsewhere needs `LLM_GATEWAY_PSQL` set in
   `llm-gateway.env`. This deployment's postgres is Homebrew `postgresql@15`, so its
   `llm-gateway.env` sets `LLM_GATEWAY_PSQL` to that install's `psql` binary (resolve the
   prefix with `brew --prefix postgresql@15`).
2. **Install the prisma client package into the gateway venv.** `gateway/requirements.txt`
   pins `prisma==0.11.0`. install.sh does not create the venv or install into it, so run
   `pip install -r gateway/requirements.txt` in the venv that holds litellm. Provisioning
   generates the client from that package, so install it before `install.sh` runs.
3. **Name the role and database, and write the URL without a password.**
   `LLM_GATEWAY_DB_ROLE` and `LLM_GATEWAY_DB_NAME` default to `litellm`. Each must be a
   lowercase identifier, because provisioning writes them into SQL. Set the host, port and
   database name in `DATABASE_URL`, and leave out the password, as the example above does.
   Provisioning replaces the user and password with the role and a generated password. It
   keeps the rest of the URL, so the database name in the URL must match
   `LLM_GATEWAY_DB_NAME`.
4. **Run `install.sh`.** It runs `$HOME/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db`, which:
   - creates the role and database if they are missing, and sets the role's password;
   - writes `DATABASE_URL`, with that password, into `llm-gateway.env`, mode 0600;
   - generates the prisma client if it is missing;
   - applies LiteLLM's migrations, which create the schema.

   Each prisma step has a time limit, `LLM_GATEWAY_PRISMA_TIMEOUT`, default 300 seconds.
   Provisioning is idempotent. A `DATABASE_URL` that already logs in is kept with its
   password, so a rerun issues no new password. If provisioning fails, install.sh warns,
   and the gateway serves without the database. Its messages go to stderr. prisma's output
   is discarded, because it can carry the password.

The launcher exports `DATABASE_URL` to LiteLLM only when three checks pass. The URL must log
in, the LiteLLM schema must exist, and the prisma client must be generated. A start creates
nothing, sets no password, generates nothing and applies no migrations. The login is retried
for `LLM_GATEWAY_LOGIN_WAIT` seconds (default 60), every `LLM_GATEWAY_LOGIN_INTERVAL` seconds
(default 5), because launchd starts the gateway and postgres at boot with no ordering between
them. When a check still fails, the gateway starts without the database and logs why. Fix the
cause, run `$HOME/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db` if the role, its password or the schema is at
fault, and restart with `launchctl kickstart`. This ordering avoids a crash loop under launchd
KeepAlive.

Three things about the password:

- The password is in the URL, which sits in `llm-gateway.env`, the one 0600 file. The
  launcher gives it to psql through `PGPASSWORD`, so it never appears in an argv.
- The launcher reads a password as written and does not percent-decode it. Provisioning
  generates hex. Keep any password you write yourself to unreserved characters.
- A password protects the role only when postgres asks for one. `local all all trust` in
  `pg_hba.conf` lets any local OS account log in as any role, this one included. That is
  host configuration, and this setup does not change it.

While the login retry runs, the gateway does not listen on port 4000. On a slow boot that is up
to about 65 seconds: the retry window is 60 seconds, and one attempt can take up to 5 more.
Consumers that start in that window get connection refused.

A start runs no prisma migration. With `DISABLE_SCHEMA_UPDATE=true`, LiteLLM skips its
`prisma migrate deploy` step. It still runs `prisma migrate diff --from-url <DATABASE_URL>` when a
prisma CLI resolves on its PATH, and that command puts the password on argv. The launcher sets
LiteLLM's PATH to `/usr/bin:/bin:/usr/sbin:/sbin`, which keeps a prisma CLI outside those
directories from resolving. On every start the launcher also unsets each database variable
LiteLLM reads to find or build a URL, in the launcher's environment: `DATABASE_URL`,
`DIRECT_URL`, the `DATABASE_*` builder fields and the read-replica fields. `DATABASE_SCHEMA` is the
exception. LiteLLM reads it for its view check on the exposed path, and it builds no URL alone, so
the launcher leaves it as the env file sets it. A prisma CLI that still resolves therefore gets no
URL, and when the start check fails LiteLLM gets none either, so the gateway starts without the
database.
The schema therefore comes from `--provision-db` alone.
The start check looks for the `LiteLLM_VerificationToken` table only, so it does not notice a
schema that lacks a later LiteLLM migration.

**Upgrading LiteLLM:** after `pip install` changes the litellm version in the gateway venv,
run `$HOME/.local/lib/llm-gateway/llm-gateway-run.sh --provision-db` before the gateway restarts. install.sh does not change
the venv, but its own run calls `--provision-db` too, so a reinstall also covers the upgrade.

## Consumers and the mint

Each consumer holds its own key. `gateway/consumers.json` declares them as a JSON list.
Each item names a consumer tag, which the key is minted with as `metadata.consumer`, and
the entry in `litellm-keys.json` that the consumer's reader looks up:

```json
[
  {"consumer": "agentctl", "entry": "litellm-agentctl-virtual-key"},
  {"consumer": "co", "entry": "litellm-co-virtual-key"},
  {"consumer": "opencode", "entry": "litellm-opencode-virtual-key"}
]
```

The list above is this deployment's example. To use another list, edit the declaration,
or point `LLM_GATEWAY_CONSUMERS_FILE` at another file. Consumer names must be unique,
and so must entry names. The mint refuses a malformed declaration before it sends any
request.

Run the mint once the proxy is up with its database connected. `/health/readiness` reports
the database state.

```
bin/gateway-mint-keys                  # gateway at http://127.0.0.1:4000
bin/gateway-mint-keys --base-url URL   # any other gateway
```

The mint calls `/key/generate` once per declared consumer and writes each key into its
`{"account": ..., "key": ...}` entry in `litellm-keys.json`, mode 0600. It refuses a key
whose `key` field hashes the same as the master key or another entry's, and then writes
nothing. It prints each consumer with a sha256 prefix of its key, never the key itself.
Re-running it mints a new key per consumer and does not revoke the old ones.

**Why the file and not the plist (ARCH-342).** Provider keys used to sit in the plist's
`EnvironmentVariables`. A plist is world-readable by default, `install.sh` copies templates
over the live one, and `launchctl print` renders every value to anyone who can run it. One
0600 file that nothing copies is the right home.

To rotate a key, edit the file and restart the daemon. Nothing else reads it.

## Deploying a change

The runtime is pinned: `install.sh` phase 3 *copies* `gateway/` into
`~/.local/lib/llm-gateway/` rather than symlinking, so a feature branch checked out in the
repo can never become production on the next crash-restart (ARCH-116).

```bash
git -C ~/Convivy/agent-platform checkout main && git -C ~/Convivy/agent-platform pull
bash ~/Convivy/agent-platform/install.sh
launchctl kickstart -k "gui/$(id -u)/com.convivy.llm-gateway"
```

A plist change needs `bootout` and `bootstrap` instead, because `kickstart -k` reuses the
old job definition (ARCH-104).

## Checking it

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/health/liveliness
curl -sS http://127.0.0.1:4000/health/readiness    # reports the database state
launchctl print "gui/$(id -u)/com.convivy.llm-gateway" | grep -E 'state|pid'
```

Logs for this deployment: `$HOME/Convivy/agent-runs/llm-gateway.{out,err}.log`.
