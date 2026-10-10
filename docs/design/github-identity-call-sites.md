# Call sites that assume the operator's GitHub identity — audit for the identity cutover

Work item `issue_01M1749DS3KT1C9K5VSE8X3J36`, under epic `issue_01M0G22P0N6QV6J0VPEBRG8JV4`
("Agents get their own GitHub identity"). Investigation only — nothing here changes
authentication. Findings below, not fixes.

## The design changed under this item; read this before the enumeration

The epic body's original step 1 ("`agentctl` injects a GitHub App installation token into each
dispatched run's environment") was superseded on 2026-08-20, before this audit started, by
`platform/decisions/0051`: **every `gh` call goes through the Bosun, and no worker process holds a
credential at all.** Routing removes the credential from the worker; injecting a token would only
have swapped one credential for another. That decision also states plainly that the routing layer
does not exist yet — `agentctl` has no `gh` subcommand today, and where the seam sits (a
subcommand, a `PATH` wrapper, something else) is an open question.

So "would this call site work under an injected non-operator credential" has no single mechanism to
test against yet. What's checkable now, and what this document answers per site, is narrower and
still useful: which credential the call authenticates with today, whether that call goes through
`gh` (interceptable by a routing layer, whatever shape it takes) or through raw `git` (which a
`gh`-wrapping layer never sees at all — ADR-0051's SSH-key finding, confirmed again below), and
what permission the operation needs, checked against the App that already exists
(`convivy-bosun`, installation `155330565`: `contents: write`, `pull_requests: write`,
`actions: read`, `checks: read`, `metadata: read`). That permission set is real and API-verified
regardless of whether the eventual mechanism injects it or a Bosun layer holds it and calls on the
site's behalf — the operations below either fit inside it or they don't.

**Updated 2026-09-12: a second App now writes to `knowledge`.** `convivy-library` (App `4923626`,
installation `161212648`) is installed on `convivy/knowledge` alone, with `contents: write`,
`pull_requests: write` and `metadata: read`. Since agent-platform#547, `agentctl kb merge` merges
the Library's knowledge PRs as that App. The `update` rule on `main` in `knowledge`,
`agent-platform` and `substrate` now comes from org ruleset `23086738` (`gh api
repos/convivy/<repo>/rules/branches/main`), and ruleset `21119402` keeps `deletion` and
`non_fast_forward`. Read through each repo's own rulesets endpoint at 2026-09-12 21:53 UTC, which
needs no `admin:org`, the bypass list of `23086738` is the same on all three:

```
gh api repos/convivy/<repo>/rulesets/23086738 \
  --jq '[.bypass_actors[] | "\(.actor_type):\(.actor_id // "-"):\(.bypass_mode)"]'
["OrganizationAdmin:-:exempt","Integration:4923626:always"]
```

`convivy-bosun` (App `4666673`) is on neither entry, so its permission set above cannot move any of
those `main` branches; no probe has tried it. `convivy-library` is on the list for all three repos
but installed only on `knowledge`, where it merged knowledge#412 at 2026-09-12 20:56 UTC with merge
commit `ae73809` authored by `convivy-library[bot]`.

**Updated 2026-09-18: the Library's two knowledge pushes now run as `convivy-library`.** Sites 1j
(`kb propose` pushing a `kb/` branch) and 1l (`kb apply` pushing `main`) used the SSH remote, so
they authenticated as the operator. Each now mints an installation token for the App, scoped to
`convivy/knowledge` with `contents: write` alone (no `pull_requests`), and pushes to the https URL
in place of `origin`. A missing key, an unsignable JWT, a widened installation or a refused mint
refuses the command with exit 2; nothing falls back to the ssh remote.

**Updated 2026-09-18 (second change): the rest of both commands now runs as `convivy-library` too.**
Four things still ran as a person after the pushes moved, and none of them runs as one now:

- **The three `gh` calls in `kb propose` (site 1k)** (`gh pr list --head`, `gh pr edit`, `gh pr create`)
  ran on `gh`'s stored login, so the branch arrived as `convivy-library` and the pull request was
  authored by the operator. Each now runs with `GH_TOKEN` set to an App installation token in its
  own environment (`kb_merge.gh_env`), and the PR is authored by `app/convivy-library`. The token is
  `kb_merge.pr_token`'s, `pull_requests: write` plus `contents: read`, minted beside the push token
  from one installation check (`kb_merge.push_and_pr_credentials`). It is not the merge's
  `installation_token` shape: `contents: write` from this App can push `main` without review (the App
  is on the `update` ruleset's bypass list), and that token would sit in `gh`'s environment for three
  calls that write no file. The push token keeps `contents: write` alone, and `kb apply`, `kb sync`
  and `kb checkout` never ask for the PR token.
- **Four `git fetch origin main` calls ran over the ssh remote as the operator.** They are reads, so none was a
  security exposure, but retiring the ssh key would have broken `kb apply` and `kb propose` at the
  first one, before either reached the App identity. All four now fetch from
  `https://github.com/convivy/knowledge.git` as the App into `refs/remotes/origin/main`
  (`_KbLibrary.fetch_main`): `_scribe_worktree` (`bin/agentctl:7553`, the first network call of every
  `kb apply` and `kb propose`), `_kb_blob_at_origin` (`:8057`, reached from `kb checkout`),
  `kb sync` (`:8233`), and the fetch after the `kb apply` push (`:9320`). The `merge --ff-only
  origin/main` beside the last one is local and needed no change. The two that only read, `kb sync`
  and `kb checkout`, fetch with `kb_merge.read_token` (`contents: read`), so a bug in either that
  tried to push is refused by GitHub (403); `kb apply` and `kb propose` fetch with the push token they
  already hold.
- **Both commands committed as `the operator <operator@example.com>`,** read from `~/.gitconfig`, so every
  document the Library wrote was permanently authored by a person. Both commits now set
  `GIT_AUTHOR_*` and `GIT_COMMITTER_*` for that one `git` invocation (`kb_merge.commit_env`) to
  `convivy-library[bot] <328494887+convivy-library[bot]@users.noreply.github.com>`, the identity the
  Library's merge commits already carry in knowledge's log. `~/.gitconfig` is untouched. The person is
  not recorded in the author field; the requesting session is the `holder` in the commit's
  `KB-Write:` trailer and the `Proposed by` line of the PR body, as before.
- **`kb sync` is in scope, and it now says when the identity is missing.** It is a read-only fetch, so
  it is the one path that gains a credential dependency it did not have to have. It is included
  because it runs on a ten-minute timer, and left on ssh it would read a retired key as "offline",
  exit 0 and let the index go stale with nothing in the log. A missing or refused identity now exits 1
  and names the stage; an unreachable GitHub is still "offline" and exits 0. A 5xx or 429 from
  `api.github.com` takes the same refusal branch, because `github_app.request` returns any HTTP error
  as a status and only a socket-level failure is "unreachable"; the next tick retries it. `--verify-only`
  fetches nothing and asks for no identity.
- **`kb checkout` no longer reports a failed read as a missing document.** It used to map a refused
  identity and a failed fetch to `exists: false`, empty content and exit 0, after taking a four-hour
  lease, so a broken key blocked every other session's checkout of documents that were there. It now
  exits 1, prints the reason, releases a lease taken for that call (one the holder already had from an
  earlier checkout stays) and prints no payload. A document that is not on `origin/main` is still
  `exists: false`, because that is how `kb_create` reserves a path.
- **The unscoped listing token is now revoked.** `kb_merge.knowledge_installation` mints a token with
  every permission the installation holds to list its repositories, and never revoked it. It lives in
  process memory and expires within the hour, so that was small, but this change moved the check from
  "per merge or push" to every `kb sync` tick and every `kb checkout`. It now calls
  `DELETE /installation/token` straight after the listing (`github_app.revoke_installation_token`), also
  when the listing fails; a failed revoke does not fail the check.

**The identity is now obtained before the lock and the first fetch,** because that fetch is made as
the App. In `kb apply` it used to be obtained after validation and before the commit, so a refusal
left nothing to reset; it is now earlier, before `_scribe_worktree` takes the lock, fetches or builds
the cage, so a refusal leaves no lock, no cage state and no remote touched. The cost is that
`--no-push` and `--dry-run` need the identity too, for the fetch alone, and that a stale-base or
no-op `kb apply` now makes the mint's GitHub calls before it learns so. `kb checkout` mints once per
call, for the same reason, and `kb sync` once per tick.

**Verified against GitHub on 2026-09-18, with `~/.config/gh/hosts.yml`, `~/.ssh/github_ed25519` and
`~/.gitconfig` all unavailable** (the process ran with `HOME` pointed at an empty directory; the App
key is read from the passwd home, so it was still found). With a real `convivy-library` installation
token: `gh pr list --head` (empty for an absent branch, and a positive control that returned an
existing PR in the same run), `gh pr create --draft`, `gh pr edit --body`, `gh pr view` and
`gh pr close --delete-branch` all succeeded against a throwaway `kb/probe-…` branch (knowledge#565,
closed and its branch deleted). The PR's author was `app/convivy-library`, its body read back as
edited, its commit's author and committer were both `convivy-library[bot]`, and the branch push was accepted.

**Verified again on 2026-09-18 with the two narrower tokens**, in the same conditions (empty `HOME`,
throwaway `kb/probe-…` branch, knowledge#566, closed and its branch deleted). A `pull_requests: write`
plus `contents: read` token ran `gh pr list --head` (absent, then present), `gh pr create --draft`,
`gh pr edit --body`, `gh pr view` and `gh pr close`, and the PR's author was `app/convivy-library`. Both it
and a `contents: read` token were refused a push (`remote: Write access to repository not granted`,
HTTP 403), which is the control showing the narrowing is real; the `contents: read` token fetched
`origin/main` by URL and refspec. `DELETE /installation/token` on the listing token answered 204, and
using that token afterwards answered 401. The branch was deleted with the push token, because deleting
a ref needs `contents: write`, which the PR token no longer has (`kb propose` deletes only its local
branch, so no call of it needs that).

The App can push `main` because it is on the bypass list of ruleset `23086738`, not because of its
token scope. I read that from the API on 2026-09-18 (a bypass list is mutable state, so re-run the
check before relying on it): `gh api /repos/convivy/knowledge/rulesets/23086738` returns
`bypass_actors` of `OrganizationAdmin` (`exempt`) and `Integration` `4923626` (`always`), and
4923626 is `kb_merge.APP_ID`. That is an inference from the bypass list; no direct push by the App
has been observed, because knowledge#412 was a PR merge, which exercises the merge path only. The
first live `kb apply` is the confirmation. Ruleset `21119402` bypasses `OrganizationAdmin` alone,
so the App is subject to its `deletion` and `non_fast_forward` rules. A `kb/` branch carries no
rules (`gh api /repos/convivy/knowledge/rules/branches/kb%2Fexample` returns `[]`), so site 1j's
branch push, including its `--force-with-lease`, never depended on a bypass.

## Two credentials, confirmed again on this repo

`agent-platform`'s and `knowledge`'s remotes are both SSH (`git@github.com:convivy/...`), not
HTTPS:

```
$ git remote -v
origin  git@github.com:convivy/agent-platform.git (fetch)
origin  git@github.com:convivy/agent-platform.git (push)
```

Every `git push` call site below therefore authenticates with `~/.ssh/github_ed25519` (no
passphrase), not with the PAT in `~/.config/gh/hosts.yml`. Every `gh <subcommand>` call site
authenticates with the PAT, via `gh`'s own stored credential, independent of the repo's git
remote scheme. A routing layer that only wraps the `gh` binary — the shape ADR-0051's open
questions lean toward — sees none of the `git push` sites. This is not a new finding; it restates
ADR-0051's own SSH-credential section against this repo's actual remotes, because the call sites
below split cleanly along it and the split changes what "confirm this site" means for each one.

---

## 1. Inside `bin/agentctl` — run by the orchestrating session (interactive `co`, or the
   overnight orchestrator), never by a dispatched worker directly

These are `agentctl` subcommands. The process running them is whichever session typed
`agentctl publish` / `pr-open` / `review-post` / etc. — today that's always a session
authenticating as the operator, whether that's the operator's own `co`, a crew's `co`, or the
autonomous overnight orchestrator (itself a `claude -p` process launched by launchd as
the operator).

| # | Site | Authenticates as today | `gh` or raw `git`? | Scope needed |
|---|------|------------------------|---------------------|--------------|
| 1a | `cmd_publish`, `git push origin HEAD:<branch>` — `bin/agentctl:9614` | SSH key | raw `git` | N/A to the App; a `gh`-wrapping layer never sees this call. Needs its own closure per ADR-0051 step 6 (restrict/rotate the key, or move the remote to HTTPS so the layer sees the traffic). |
| 1b | `cmd_publish`, `gh pr view --json headRefName` (only when `--pr` is passed) — `bin/agentctl:9527-9530` | PAT | `gh` | `pull_requests: read` — inside the App's `pull_requests: write`. |
| 1c | `cmd_pr_open`, `gh pr list --head … --state open` / `gh pr view branch --json state,url` — `bin/agentctl:9755` and `:9772` | PAT | `gh` | `pull_requests: read` — covered. |
| 1d | `cmd_pr_open`, `gh pr create --title … --body-file … --base … --head …` — `bin/agentctl:9813-9814` | PAT | `gh` | `pull_requests: write` — covered. |
| 1e | `cmd_review_post`, `gh api repos/{repo}/commits/{sha}` (SHA normalization, only when a caller passes a short SHA) — `bin/agentctl:10569-10572` | PAT | `gh api` | `contents: read` — covered under `contents: write`. |
| 1f | `cmd_review_post`, `gh pr comment --body-file` — `bin/agentctl:10717-10720` | PAT | `gh` | `pull_requests: write` — covered. **This is the literal `agentctl review-post` stamping site the epic names first**, and the one whose failure "looks like a clean finish" per the epic. |
| 1g | `_mark_pr_ready` (fires automatically at the tail of a completing `review-post`, and directly via `cmd_pr_ready`), `gh pr ready` — `bin/agentctl:10951-10953` | PAT | `gh` | `pull_requests: write` — covered. |
| 1h | `cmd_review_status`, `_gh_pr_json` reading the `statusCheckRollup` field — the authoritative check-rollup verdict — `bin/agentctl:9835-9842`, called once from `cmd_review_status:11013` and again, post-settle, from `_approve_blocked_by_ci:10302,10449` | PAT | `gh` (`gh pr view --json ...`) | `checks: read` + `pull_requests: read` — both covered. **This is the epic's "`gh pr checks` polling"**: no literal `gh pr checks` subcommand runs anywhere in `bin/agentctl`, and no `gh api graphql` call exists either (`grep -c graphql bin/agentctl` → 0) — the verdict comes from this `gh pr view --json` read. The code's own comments (`:10166`, `:10182`, `:10220`) describe it as GraphQL-backed, which is `gh`'s own internal mechanism for resolving `statusCheckRollup`, not a literal `gh api graphql` invocation in this file. A separate REST pair, `_gh_api_json`/`_ci_settled_rest` (`bin/agentctl:10162-10213`: `gh api repos/{repo}/commits/{sha}/check-runs` and `.../status`), runs only inside `_approve_blocked_by_ci` (called from `cmd_review_post:10661`, gating an APPROVE stamp before it can be written) via `_await_ci_settle`, and decides only whether CI has stopped moving — never the pass/fail verdict, which always comes from the `statusCheckRollup` read above. |
| 1i | `cmd_review_status`, `gh api repos/{repo}/actions/workflows` inside `_repo_has_ci_workflows` (`bin/agentctl:9858-9878`, call at `:9870`) and `gh api --paginate` workflow runs inside `_workflow_path_map` (`bin/agentctl:9921-9954`, call at `:9942`) — two separate functions, not one | PAT | `gh api` | `actions: read` — covered. |
| 1j | `cmd_kb_propose`, `git push` to a proposal branch on `convivy/knowledge` (and the `ls-remote` tip read before it) — `bin/agentctl:8869-8879` | `convivy-library` App installation token, since 2026-09-18 (was SSH key) | raw `git` over https | The token carries `contents: write` alone and travels in `GIT_CONFIG_*` for that one process (`kb_merge.push_env`), so it is still invisible to a `gh`-wrapping layer, but it is the App's identity rather than the operator's. No fallback: a missing key or refused mint refuses the command (exit 2). The `git fetch` in `_scribe_worktree` (`bin/agentctl:7553`, called at `:8697`) now runs as the App over the same URL, and the identity is obtained before it, at `:8692`, so removing the ssh key no longer breaks `kb propose`. The branch's commit is authored and committed as `convivy-library[bot]` (`bin/agentctl:8854`). |
| 1k | `cmd_kb_propose`, `gh pr list` / `gh pr edit` / `gh pr create --repo convivy/knowledge` — `bin/agentctl:8904-8927` | `convivy-library` App installation token, since 2026-09-18 (was PAT) | `gh` | `pull_requests: write` on `convivy/knowledge`. The token is `kb_merge.pr_token`'s (`pull_requests: write` plus `contents: read`), not the merge's wider one, passed as `GH_TOKEN` in each call's own environment. `convivy-library`'s installation is `repository_selection: selected` with `convivy/knowledge` as its one repository, checked on every mint (`kb_merge.knowledge_installation`); it is `convivy-bosun` that is installed org-wide (`gh api /orgs/convivy/installations`, read 2026-09-18). All three calls were run against GitHub with a real token of exactly that shape (dated note in the opening section). |
| 1l | `cmd_kb_apply`, direct-to-main push, `library(kroot, "push", "--quiet", KM.PUSH_URL, "HEAD:main")` against `convivy/knowledge` — `bin/agentctl:9300` | `convivy-library` App installation token, since 2026-09-18 (was SSH key) | raw `git` over https | Pushed as the App through `kb_merge.PUSH_URL`, on the same `contents: write`-only token as 1j. Both fetches of this command, the one in `_scribe_worktree` (`bin/agentctl:7553`, called at `:9146`) and the one after this push (`:9320`), now run as the App over the same URL, and the identity is obtained before the first, at `:9140`, so removing the ssh key no longer breaks `kb apply`. The commit is authored and committed as `convivy-library[bot]` (`:9292`). **Worth flagging on its own**: this is a direct push to `main`, not a PR. Three rules are effective on `main` of `convivy/knowledge`: `update` from ruleset `23086738` ("Only the operator updates main"), and `deletion` and `non_fast_forward` from ruleset `21119402`. The bypass list of `23086738` has two separate entries, `OrganizationAdmin` (`exempt`) and the `convivy-library` integration, App `4923626` (`always`), so pushing `main` as the App is permitted by name on the strength of that entry; no direct push by the App has been observed (knowledge#412 was a PR merge), and the first live `kb apply` is the confirmation. Until 2026-09-18 this push authenticated as the operator under the `OrganizationAdmin` entry, so the ruleset constrained nothing here (ADR-0051). The bypass list of `21119402` holds `OrganizationAdmin` alone, so the App is subject to its `deletion` and `non_fast_forward` rules. All of this was read from the ruleset API on 2026-09-18 (dated note in the opening section). |
| 1m | `_head_advanced_beyond_pin`, `gh api repos/{slug}/compare/{pin}...{head}` — `bin/agentctl:10844-10865`, under the merge gate's `# --- stamped-review merge gate (ARCH-184 phase 2) ---` header (`:9825`), called from the stale-replay disambiguation inside `_authenticated_stamps:10901`, itself called from `_mark_pr_ready` (1g) and `cmd_review_status` (1h) | PAT | `gh api` | `contents: read` — covered under `contents: write`. Runs inside the merge-gate path itself, one of the four categories this document searches. |
| 1n | `_gh_repo_slug`, `gh repo view --json nameWithOwner` — `bin/agentctl:9827-9832` | PAT | `gh` | `metadata: read` — covered. |

**Branch pushes** (the epic's third named item) are 1a and 1j — both raw `git`, both outside
anything a `gh`-wrapping Bosun layer sees. 1a goes over SSH; 1j, since 2026-09-18, goes over https
as the `convivy-library` App (dated note in the opening section).

**PR creation** (the epic's second named item) is 1d and 1k — both `gh pr create`. 1d is PAT-based
today and fits inside the App's `pull_requests: write`; 1k has run as `convivy-library` since
2026-09-18.

## 2. Dispatched review-class agents' own raw `gh` grants — the literal `gh pr checks`
   invocation, run from inside the agent's own Bash tool call, not through `agentctl`

`lib/agent-platform/tool_policy.py:294-336`, `review_tools()`, granted to `reviewer` and
`security-reviewer` (and, per the module's `ENFORCEABLE_AGENTS` list at line 72, also `planner`,
`platform-planner`, `kb-sync-verifier`, `design-critic`, `scout` — the same grant function, so the
same call sites apply to all seven roles wherever their prompts use them):

```
"Bash(gh pr checks:*)",
"Bash(gh pr view:*)",
"Bash(gh pr diff:*)",
"Bash(gh run view:*)",
"Bash(gh pr comment:*)",
```

This is where the **literal** `gh pr checks` subcommand runs — a dispatched reviewer typing it
directly, authenticating with whatever `gh` resolves to in that process's environment (today: the
PAT, inherited ambient). Scope needed: `pull_requests: read`, `checks: read`, `actions: read` (for
`gh run view`) — all covered by the App's grant — plus `pull_requests: write` for the
`gh pr comment` in this same list, which is how a reviewer posts its own prose comment
independent of `agentctl review-post`'s stamped one.

**The module's own comment at lines 297-311 already names the gap this item exists to close**:
these grants are not repo-scoped — Bash permission rules match command *text*, and a flag's value
can't be pinned independently of its position, so `Bash(gh pr view --repo convivy/vivy:*)` doesn't
match `gh pr view 129 --repo convivy/vivy`, the form agents actually write. The comment states the
fix in the same words this epic is pursuing: "stop handing reviewers raw `gh` and route PR reads
through `agentctl` wrappers that take the repo from the run's own `AGENTCTL_REPO`." That's the
Bosun-routing direction, already identified from inside this exact file before this audit ran.

## 3. Dispatched build-class agents (`coder`, `bug-fixer`, `refactorer`, …) — no enforced
   allow-list, so no enumerable "call site" in the usual sense

`tool_policy.py:359-369`: any agent not in `ENFORCEABLE_AGENTS` gets `enforced=False` on the
dispatch path. The reasoning recorded in the module itself: a build-class agent needs arbitrary
shell (`pytest`, `venv` activation, `make`, `psql`), so an allow-list permissive enough to be
useful is equivalent to full Bash — the deny-list (the ARCH-103 `block-pr-merge.sh` hook) is the
*only* control. That hook denies `gh pr merge` and `git push … main` shapes (with the known
wrapper-word and `||` bypasses the epic already documents); it says nothing about `git push` to a
non-`main` branch, `gh pr comment`, `gh issue comment`, or any other `gh`/`git` verb.

There's no fixed list of call sites here because there's no fixed list of commands — a coder can
type any `gh`/`git` invocation, and every one of them authenticates with whatever's ambient in
that process (PAT via `gh`, SSH key via `git push`, same split as above). **This is the largest
single gap for the cutover**, not because any specific command is wrong today, but because
whatever mechanism eventually removes the credential from a worker has to remove it from a process
with unrestricted shell, not intercept a fixed set of tool grants. A routing layer that only wraps
`gh` on `PATH` is bypassed the moment a build-class agent's shell calls `/opt/homebrew/bin/gh`
directly or shells past the wrapper (`bash -c`, a script file, `$(which gh)`) — the same
bypass shape ADR-0051 already names for `block-pr-merge.sh` itself, one layer over.

## 4. The self-hosted Deck CI runner

`~/Library/LaunchAgents/com.convivy.deck-ci-runner.plist` — a `LaunchAgent` (not a
`LaunchDaemon`), no `UserName` key, so it runs as the logged-in GUI user: the operator, confirmed
by its `EnvironmentVariables` block setting `HOME` to the operator's home directory and by the on-disk credential
files at `~/.local/share/actions-runner-deck/.credentials*` being owned by the operator. It runs
GitHub's own runner binary (`run.sh`), registered against `convivy/convivy-deck`, and executes
whatever `convivy-deck/.github/workflows/ci.yml` specifies.

That workflow makes exactly one `gh` call, and it's already isolated: `ci.yml:56-65`, "Repo must
still be private", uses `GH_TOKEN: ${{ github.token }}` — the ephemeral, run-scoped token GitHub
Actions itself provides, not the operator's PAT. Nothing else in the workflow invokes `gh` or
`git push`.

**The exposure isn't in what the workflow calls — it's in what the runner process can reach.**
`swift build`, `./Scripts/make-app.sh`, and every other step execute as uid 501, with the same
filesystem access to `~/.config/gh/hosts.yml` and `~/.ssh/github_ed25519` as a dispatched agent
run has. `ci.yml:44-55` already documents an adjacent version of this ("a self-hosted runner on a
public repo executes fork-PR code as the machine owner") and gates on the repo staying private,
which bounds *who* can submit code the runner executes but does nothing about *what that code can
read once it runs*. Nothing here changes under the App/Bosun model unless the runner's own OS-level
credential exposure is addressed separately — this call site's fix is the account-boundary work
(`issue_01M17V9XB22KEFMRDA3ZKGGXWE`), not a `gh`-routing layer, since the runner never goes through
`agentctl` at all.

## 5. Launchd cron jobs calling `gh` directly as the operator, outside any dispatched-run or
   interactive-session frame

Each of these is a `LaunchAgent` running the deploy area's pinned `python3` directly (no
`agentctl spawn`, no `co`), so there's no run-id, no dispatch registry entry, and no tool-policy
enforcement of any kind — just a script running as the operator with `gh` on `PATH` and
`~/.config/gh/hosts.yml` readable.

| Script | launchd job | `gh` usage | Read or write? |
|---|---|---|---|
| `bin/queue-sync` | (was `com.convivy.issue-queue-drain.plist`; plist deleted 2026-09-10, program kept) | `gh pr view --json …` (`queue-sync:119`, `resolve()`) | read |
| `bin/queue-sync` | (same) | `git push origin HEAD` straight to substrate `main` (`queue-sync:390`) | **write, raw `git`, SSH key.** The in-code comment at `queue-sync:337-343` says this is deliberate: "queue-sync does the push itself rather than handing the agent a `git push`" specifically *because* the ARCH-103 hook would deny an agent-typed one — so this site exists as a documented, sanctioned bypass of the very control this epic is superseding. The org ruleset covers this repo's default branch too, but today this push also authenticates as the operator, who is on the ruleset's bypass list, so the ruleset provides zero constraint on it right now either (ADR-0051) — the hook is not this site's only undocumented gap. Whatever replaces the hook needs an explicit answer for this site, not just silence. |
| `lib/agent-platform/worktrees.py:pr_merged()` | `com.convivy.worktrees-cleanup.plist` | `gh pr list --head … --state merged` | read |
| `bin/velocity-report` | `com.convivy.velocity-report.plist` | `gh pr list`, `gh api` (reporting) | read |
| `bin/security-scorecard` | `com.convivy.security-scorecard.plist` | `gh api`/`gh pr list` (reporting) | read |
| `bin/warrant-scorecard` | `com.convivy.warrant-scorecard.plist` | `gh api`/`gh pr list` (reporting) | read |
| `bin/model-scorecard` | `com.convivy.model-scorecard.plist`, `.model-scorecard-weekly.plist`, `.model-scorecard-hollis.plist` | `gh pr list`, `gh pr view` (reporting) | read |
| `bin/dependabot-review` | `com.convivy.dependabot-review.plist` | `gh pr list --repo convivy/<repo>` | read |

`lib/agent-platform/techdebt.py` (`gh_aging`, `_issue_body`) and `com.convivy.tech-debt-harvest.plist`
were removed 2026-09-15 (tech-debt.json's only consumer, Orient's `/tech-debt` dashboard, was
removed first). That was the App's already-verified permission set's one clean-cut case: the
App's grant is `contents: write, pull_requests: write, actions: read, checks: read,
metadata: read` — no `issues` permission at all — and `gh issue list`/`gh issue view` need
`issues: read` at minimum, which a token minted from this installation would fail outright.

**`bin/bug-sweep`'s embedded task prompt is now the only remaining `gh issue` call site with an
unmet permission** (`bug-sweep:55,57`, not itself a subprocess call — it launches an interactive
`bug-investigator` co session, so it's really an interactive-session call site): `gh issue
list --label …`, applying the `claude-mirrored` label, and `gh issue comment` for triage
diagnoses all need `issues: write`. This is not something to guess past — it's a permission the
App does not have today, checked against the API record in the epic's own comment thread, and it
needs adding (or this call site needs to keep a separate path) before it can move off the PAT.

## 6. The interactive session's own credential

**Updated 2026-09-19: `co -r`'s knowledge refresh no longer runs as the operator.** `_co_pull_knowledge`
in `bin/co` used to run three things on a personal credential. A `git ls-remote origin`
probe and a `git fetch origin main:refs/remotes/origin/main` both went over the knowledge remote's
SSH URL with `~/.ssh/github_ed25519`, so retiring that key would have stopped every new session
refreshing knowledge. When the probe failed against an HTTPS remote, `gh auth login -h github.com -p
https -w` opened an interactive login as the operator and reinstalled the PAT into `~/.config/gh/hosts.yml`;
that branch was dead for `agent-platform` and `knowledge`, whose remotes are both SSH (§2 above).

The function now runs `agentctl kb sync`, which fetches over https as `convivy-library` (the `kb
sync` fetch that moved on 2026-09-18, above), so the App token never reaches `co`'s zsh. The probe
and both auth branches are gone. `bin/co` has no `gh`, `git fetch`, `git pull`, `git ls-remote` or
`ssh` command left outside comments. A refused App identity exits 1 and `co -r` aborts, naming the
stage. That covers a missing key, a refused mint, a suspended installation and a 5xx or 429 from
`api.github.com`, all of which `kb sync` raises as `Unreadable`. An unreachable GitHub (a
socket-level failure minting the token, before any fetch runs) exits 0, and so does any nonzero
`git fetch`, including a token GitHub minted and then rejected; `co` continues with a warning that
says the fetch failed and points at `kb sync`'s line above it, which says what failed and includes
git's stderr when a fetch ran. `kb sync` reports both under one "fetch failed (likely offline)"
line, so `co` names no cause.

ADR-0051 does not leave the shape of an interactive session's credential open. Its decision section
states "This applies to interactive sessions as well as dispatched runs. A crew's `co` reaches
GitHub the same way its workers do," and its sequencing step 4 is only "Move interactive sessions
onto it [the Bosun layer]" — the same no-locally-held-credential destination as a dispatched worker,
not a PAT/App-token/mediated-shape choice left for later. The refresh path above moved ahead of that
sequence because the ssh key is being retired. What step 4 still covers is the ambient credential of
the session's own `gh` and `git`, and it changes last (step 4 follows dispatched runs being confirmed
on the layer in step 3).

---

## What I could not confirm by reading, and the command that would settle each

- **Whether `gh pr comment` (site 1f/2) and `gh pr create` (1d) actually succeed against a
  minted `convivy-bosun` installation token, as opposed to merely fitting inside the App's
  documented permission set on paper.** The epic's own comment thread (id 672) already says this
  is unverified for the merge path specifically and must be proven before any build — the same
  caution applies to every `gh` call site named above, not only `gh pr merge`. Settle it with:
  `gh api -X POST /app/installations/155330565/access_tokens` (needs the App's JWT, not a call I
  can make here) followed by `GH_TOKEN=<minted> gh pr comment <throwaway-pr> --body test` and
  `GH_TOKEN=<minted> gh pr create --title test --body test --head <throwaway-branch>` against a
  disposable PR — read-write, so I did not run it.
- **Whether the org ruleset would meaningfully constrain queue-sync's push (5) once it moves off
  the operator.** The KB direct-to-main push (1l) is answered: it moved to `convivy-library` on
  2026-09-18 and the App is on the `update` rule's bypass list (row 1l and the dated note in the
  opening section). Queue-sync still authenticates as the one identity on the `OrganizationAdmin`
  bypass entry, so the ruleset constrains that push right now. What's open is the post-cutover
  question: whether the identity it moves to falls outside the bypass entries, or whether an entry
  follows it there. The `update` rule is ruleset `23086738`, whose bypass list, read in the dated
  notes at the top of this document, holds `OrganizationAdmin` and the `convivy-library`
  integration. For `knowledge`, `agent-platform` and `substrate`, an identity queue-sync moves to is
  refused unless it is one of those two or an entry is added for it. Settle it by reading the bypass
  list after the identity is decided: `gh api repos/convivy/substrate/rulesets/23086738`.
- **Whether any other product repo's default-branch remote is HTTPS rather than SSH**, which
  would put its pushes inside a `gh`-wrapping layer's reach where `agent-platform`'s and
  `knowledge`'s are not. Settle it with `git -C ~/Convivy/<repo> remote -v` per repo — ADR-0051
  already found 14 of 20 checkouts on SSH but did not enumerate which; I did not re-run that
  sweep here since it's identity-agnostic infrastructure state, not a call site.

## Not enumerated further

`bin/velocity-report`, `security-scorecard`, `warrant-scorecard`, `model-scorecard`, and
`dependabot-review`'s individual `gh` invocations are all read-only reporting queries
(`gh pr list`, `gh api`) with no write surface and no merge-gate role — grouped under §5 by
mechanism (launchd-as-the-operator) rather than itemized line-by-line, since the fix for all five is
the same one line: whatever they authenticate with next, it needs `pull_requests: read` and
nothing else. Full line references are in the grep output this audit is built from, reproducible
with `grep -rn '"gh"' bin/ lib/agent-platform/` from the repo root.
