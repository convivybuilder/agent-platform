#!/usr/bin/env bash
# install.sh — restore or refresh the agent platform on this mini.
#
# Idempotent. Safe to re-run. Two phases:
#
#   1. venv + deps    — creates ~/.local/lib/knowledge/.venv if missing,
#                        installs/upgrades requirements.txt.
#   2. symlinks       — replaces ~/.local/bin/<binary> and
#                        ~/.local/lib/{agent-platform,knowledge}/* with symlinks
#                        back into this repo, so future edits land here and
#                        propagate live without a separate sync step.
#   3. gateway pin    — COPIES (not symlinks) gateway/*.py + config.yaml into
#                        ~/.local/lib/llm-gateway/. The LiteLLM proxy is a
#                        long-running KeepAlive daemon; symlinking it to the repo
#                        (like the re-invoked bins in phase 2) would make whatever
#                        branch is checked out go live on the next restart — incl.
#                        an unattended crash-restart. A real copy pins the daemon
#                        to installed-from-main code. Re-run this install + restart
#                        the gateway to deploy a gateway change (ARCH-116).
#   4. launchd plists — RENDERS (not symlinks, not raw copies) each
#                        launchd/*.plist template into ~/Library/LaunchAgents/,
#                        substituting the installing user's $HOME for the
#                        __HOME__ placeholder the templates carry in git.
#                        Symlinks here are unreliable with `launchctl
#                        bootstrap`; render + reload is the safe idiom. Re-run
#                        `launchctl bootout gui/$(id -u) <label>` + `bootstrap`
#                        after install to pick up plist changes.
#
# `install.sh --migrate-labels [--dry-run] [--only <job>]` is a separate mode
# (it does not run phases 1-4b): for each gui-domain job this platform ships
# under a com.convivy.* label today, it looks for that SAME job still loaded
# under whatever label it used before the CQ-428 rename (discovered from
# `launchctl list`, never hardcoded — the pre-rename label named a person,
# and this script's own source must not carry it either), bootstraps the
# com.convivy.* label, confirms it came up, and only then boots out and
# removes the old one. --dry-run prints the mapping and writes nothing. See
# the MIGRATE_LABELS block below for the full contract.
#
# Does NOT:
#   - Provision Slack webhooks in macOS Keychain (see
#     agent-platform/runbooks/agent-slack-webhook-setup in the knowledge repo).
#   - Register the mcp-kb-server with Claude Code (`claude mcp add`).
#   - Initialize ~/agent-runs/, ~/.knowledge-index/, or the knowledge repo.
#     Those have their own bootstrap steps documented in dev-setup/.
#
# On a brand-new mini, the full restore sequence is (company/decisions/0008):
# <root> is ~/builder, the default when nothing is configured yet, or wherever
# the operator has chosen to put it (CREWS_ROOT in
# ~/.config/convivy/session-agent.env; see lib/agent-platform/builder_root.py).
#   1. Clone convivy/knowledge to <root>/knowledge
#   2. Clone convivy/agent-platform to <root>/agent-platform
#   3. bash <root>/agent-platform/install.sh
#   4. claude mcp add kb -- ~/.local/bin/mcp-kb-server
#   5. Provision Slack webhooks per the runbook
#   6. launchctl bootstrap each of the three launchd jobs

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LOCAL_BIN="$HOME/.local/bin"
LOCAL_LIB="$HOME/.local/lib"
KNOWLEDGE_VENV="$LOCAL_LIB/knowledge/.venv"
GATEWAY_LIB="$LOCAL_LIB/llm-gateway"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

say() { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }

# --- argument parsing --------------------------------------------------------
# bash 3.2 (ARCH-082): no declare -A, mapfile, or ${var,,} anywhere below.

PLIST_FORCE=0
MIGRATE_LABELS=0
MIGRATE_DRY_RUN=0
MIGRATE_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) PLIST_FORCE=1 ;;
    --migrate-labels) MIGRATE_LABELS=1 ;;
    --dry-run) MIGRATE_DRY_RUN=1 ;;
    --only)
      shift
      MIGRATE_ONLY="${1:-}"
      if [ -z "$MIGRATE_ONLY" ]; then
        echo "install.sh: --only requires a job name" >&2
        exit 1
      fi
      ;;
    *) warn "Unknown argument: $1" ;;
  esac
  shift
done

# --- label migration: discover each job's pre-CQ-428 label, never hardcode it
# The 40 gui-domain jobs this platform ships under com.convivy.* today were
# renamed from a personal namespace (CQ-428, so Builder's publish-engine
# stops refusing a plist naming a person) — the pre-rename label is exactly
# the string that migration exists to remove, so it is never written as a
# literal here either; it is discovered at runtime from what `launchctl
# list` actually reports loaded. SYSTEM_DAEMON_PLISTS (phase 4, below) were
# already com.convivy.* and are not part of this list.
MIGRATED_JOBS="agent-activity-report agent-cost-report-refresh agent-cost-report agent-platform-autopull agent-postgres agent-summary arch129-remeasure changelog-drain check-deploy-pinned-jobs ci1-heartbeat cli-contract-check dependabot-review hook-log-prune issue-pr-reconcile kb-guards-check knowledge-autopull knowledge-watcher limit-resume memory-archive metric-alerts model-scorecard-hollis model-scorecard-weekly model-scorecard orchestrator-tokens-record pr-state-capture primary-checkout-check run-observer run-register-api run-retention security-scorecard serving-checkout-check session-follow-watchdog tcc-prompt-watchdog transcript-capture velocity-report vivy-main-suite warrant-scorecard weekly-consulting weekly-kb-audit worktrees-cleanup"

# Renders launchd/<name>.plist ($1) to a destination path ($2), substituting
# $HOME for the __HOME__ placeholder the template carries in git.
render_plist() {
  src="$1"
  dest="$2"
  tmp="$(mktemp "${TMPDIR:-/tmp}/launchd-plist.XXXXXX")"
  sed "s|__HOME__|$HOME|g" "$src" > "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$dest"
}

# The label this job ($1, a bare basename like "run-observer") is loaded
# under OTHER than its current com.convivy.* label ($2), if any — found by
# suffix match (".$job") against every label `launchctl list` reports
# loaded, skipping the current label itself. Prints nothing (and the caller
# treats that as "no pre-migration label currently loaded") when none
# matches. Pure awk, no bash-4 constructs (ARCH-082).
legacy_label_for() {
  job="$1"
  new_label="$2"
  launchctl list 2>/dev/null | awk -F'\t' -v new="$new_label" -v suffix=".$job" '
    NR > 1 && $3 != "" && $3 != new {
      n = length($3); s = length(suffix)
      if (n >= s && substr($3, n - s + 1) == suffix) { print $3; exit }
    }'
}

# Migrates one job ($1, e.g. "run-observer") onto com.convivy.$1 from
# whatever label `legacy_label_for` finds it loaded under today. Idempotent
# and resumable: safe to re-run after any interruption (ARCH-082-style bash
# 3.2, no associative arrays).
#   - new already loaded        -> skip bootstrap, go straight to booting out old.
#   - old loaded, new not       -> bootstrap new, confirm, then boot out + remove old.
#   - neither loaded/no old found -> do nothing (this job is intentionally unloaded,
#                                     or has no pre-migration label to migrate from).
# Never pkill/kill/launchctl unload|load — bootstrap/bootout only.
migrate_one_job() {
  job="$1"
  new_label="com.convivy.$job"
  new_plist="$LAUNCH_AGENTS/$new_label.plist"
  src_template="$REPO/launchd/$new_label.plist"
  uid="$(id -u)"

  old_label="$(legacy_label_for "$job" "$new_label")"
  new_loaded=1
  launchctl print "gui/$uid/$new_label" >/dev/null 2>&1 && new_loaded=0

  if [ "$new_loaded" -eq 0 ]; then
    if [ -z "$old_label" ]; then
      say "  $job: $new_label already loaded, no pre-migration label found — nothing to do"
      return 0
    fi
    say "  $job: $new_label already loaded — booting out its pre-migration label"
  elif [ -n "$old_label" ]; then
    say "  $job: bootstrapping $new_label"
    render_plist "$src_template" "$new_plist"
    if ! launchctl bootstrap "gui/$uid" "$new_plist"; then
      warn "  $job: bootstrap of $new_label FAILED — leaving its pre-migration label loaded, not booting it out"
      return 1
    fi
    if ! launchctl print "gui/$uid/$new_label" >/dev/null 2>&1; then
      warn "  $job: $new_label did not come up after bootstrap — leaving its pre-migration label loaded"
      return 1
    fi
    say "  $job: $new_label confirmed running"
  else
    say "  $job: neither label loaded — leaving unloaded, nothing to do"
    return 0
  fi

  # bootout's own exit status is not trusted: a KeepAlive job can report success
  # while still respawning, or fail for a reason that leaves it loaded either
  # way. The only thing that may remove the old plist is a CONFIRMED absence —
  # `print` on the old label failing after bootout. Skip that confirmation and
  # a bootout that didn't actually take leaves the old job running headless
  # (its plist gone, no record to retry from) while install.sh reports success.
  # On a failed confirmation, the old plist is kept so a re-run's
  # "new already loaded" branch above finds the old label still loaded and
  # retries the bootout from there.
  old_plist="$LAUNCH_AGENTS/$old_label.plist"
  launchctl bootout "gui/$uid/$old_label" 2>/dev/null || true
  if launchctl print "gui/$uid/$old_label" >/dev/null 2>&1; then
    warn "  $job: $old_label still loaded after bootout — leaving its plist in place, NOT migrated (re-run to retry)"
    return 1
  fi

  # Back up rather than bare-delete: the printed rollback command stays valid
  # even after this step removes the old plist from LaunchAgents. Only reached
  # once the bootout above is confirmed.
  if [ -f "$old_plist" ]; then
    mkdir -p "$LAUNCH_AGENTS/.pre-migration-backup"
    cp "$old_plist" "$LAUNCH_AGENTS/.pre-migration-backup/$old_label.plist"
  fi
  rm -f "$old_plist"
  say "  $job: pre-migration label booted out and removed"
}

if [ "$MIGRATE_LABELS" -eq 1 ]; then
  if [ -n "$MIGRATE_ONLY" ]; then
    case " $MIGRATED_JOBS " in
      *" $MIGRATE_ONLY "*) jobs_to_process="$MIGRATE_ONLY" ;;
      *)
        echo "install.sh --migrate-labels: unknown job '$MIGRATE_ONLY'" >&2
        exit 1
        ;;
    esac
  else
    jobs_to_process="$MIGRATED_JOBS"
  fi

  uid="$(id -u)"
  say "Reverse mapping (new -> pre-migration label, as currently loaded) and rollback commands — run these if the migration needs to be undone:"
  for job in $jobs_to_process; do
    new_label="com.convivy.$job"
    old_label="$(legacy_label_for "$job" "$new_label")"
    if [ -n "$old_label" ]; then
      say "  $new_label -> $old_label"
      say "    rollback: launchctl bootstrap gui/$uid $LAUNCH_AGENTS/.pre-migration-backup/$old_label.plist && launchctl bootout gui/$uid/$new_label"
    else
      say "  $new_label -> (no pre-migration label currently loaded)"
    fi
  done
  say ""

  if [ "$MIGRATE_DRY_RUN" -eq 1 ]; then
    say "DRY RUN — no file written, no launchctl bootstrap/bootout call (print is read-only)"
    for job in $jobs_to_process; do
      new_label="com.convivy.$job"
      old_label="$(legacy_label_for "$job" "$new_label")"
      if [ -n "$old_label" ]; then
        say "  $old_label -> $new_label"
        say "    destination: $LAUNCH_AGENTS/$new_label.plist"
        say "    $old_label currently: loaded"
      else
        say "  $new_label: no pre-migration label currently loaded"
      fi
    done
    exit 0
  fi

  migrate_failed=0
  for job in $jobs_to_process; do
    migrate_one_job "$job" || migrate_failed=1
  done
  if [ "$migrate_failed" -ne 0 ]; then
    warn "One or more jobs failed to migrate; re-run 'install.sh --migrate-labels' to retry (resumable, idempotent)."
    exit 1
  fi
  say "Label migration complete."
  exit 0
fi

# --- phase 1: venv + deps ---------------------------------------------------

say "Phase 1: venv + Python deps"
mkdir -p "$LOCAL_LIB/knowledge"
if [[ ! -d "$KNOWLEDGE_VENV" ]]; then
  say "Creating $KNOWLEDGE_VENV (python3.11)"
  python3.11 -m venv "$KNOWLEDGE_VENV"
fi
"$KNOWLEDGE_VENV/bin/pip" install --quiet --upgrade pip
"$KNOWLEDGE_VENV/bin/pip" install --quiet -r "$REPO/requirements.txt"
say "venv ready: $("$KNOWLEDGE_VENV/bin/python3" --version)"

# --- phase 2: symlinks ------------------------------------------------------

say "Phase 2: symlinks for bin/ and lib/"
mkdir -p "$LOCAL_BIN" "$LOCAL_LIB/agent-platform" "$LOCAL_LIB/knowledge"

# bin/
#
# DEPLOY-PINNED daemon binaries are carved out of this loop (platform/decisions/
# 0059 rules 1, 2). Each one's actual production entry point is its own deploy
# area (bin/install-run-register-api / bin/install-bosun-server, agent-platform#468) —
# its plist names a venv inside ~/.local/deploy/<name>/, never
# ~/.local/bin/<name>. Nothing legitimate types a daemon's name at a prompt the
# way it does agent-cost-report or model-scorecard, so unlike THOSE (which stay
# symlinked here on purpose — ADR-0059 rule 5, a person re-invoking one while
# working on it is still testing), a symlink at ~/.local/bin/<name> serves no
# interactive use and is only a stale trap: a plist edited by hand later could
# be re-pointed at the easy, wrong path. The gateway has this same carve-out in
# phase 3 below, for daemon code that was never in bin/ to begin with; this is
# that carve-out for the daemon binaries that ARE.
DEPLOY_PINNED_BINS="run-register-api bosun-server"
for src in "$REPO"/bin/*; do
  name="$(basename "$src")"
  case " $DEPLOY_PINNED_BINS " in
    *" $name "*)
      # A prior install may have left $LOCAL_BIN/$name symlinked into this
      # checkout before it was carved out above (agent-platform#468 landed
      # after run-register-api's symlink already existed on this machine,
      # dated Aug 20). Remove ONLY that stale case: a symlink whose target is
      # inside $REPO. A real file or a symlink pointing elsewhere is left
      # alone — this loop still creates nothing for a DEPLOY_PINNED_BINS name.
      dst="$LOCAL_BIN/$name"
      if [[ -L "$dst" ]]; then
        case "$(readlink "$dst")" in
          "$REPO"/*)
            warn "Removing stale symlink $dst → checkout (deploy-pinned, platform/decisions/0059 rule 1)"
            rm -f "$dst"
            ;;
        esac
      fi
      continue
      ;;
  esac
  dst="$LOCAL_BIN/$name"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    continue  # already correct
  fi
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    warn "Backing up existing file $dst → $dst.preinstall.$(date +%s)"
    mv "$dst" "$dst.preinstall.$(date +%s)"
  fi
  ln -sfn "$src" "$dst"
done

# lib/agent-platform/ — delegated to the shared farm-linker so the
# autopull can also link new modules without reimplementing the loop.
say "  lib/agent-platform/:"
bash "$REPO/lib/link-farm.sh" "$REPO" "$LOCAL_LIB" --prune

# lib/knowledge/  (knowledge.py + schema.sql; preserve the .venv subdir)
for src in "$REPO"/lib/knowledge/*; do
  name="$(basename "$src")"
  dst="$LOCAL_LIB/knowledge/$name"
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    continue
  fi
  if [[ -e "$dst" && ! -L "$dst" ]]; then
    warn "Backing up existing file $dst → $dst.preinstall.$(date +%s)"
    mv "$dst" "$dst.preinstall.$(date +%s)"
  fi
  ln -sfn "$src" "$dst"
done

say "Symlinks in place. Verify: ls -la $LOCAL_BIN | grep -- '->'"

# --- phase 3: gateway pinned copy -------------------------------------------
# Unlike the bins/lib above (symlinked to the repo so edits propagate live), the
# LiteLLM gateway is a long-running KeepAlive daemon. If it imported straight
# from the checkout, whatever branch is checked out would become production on
# the next restart — including an unattended crash-restart. Copy the runtime
# files to a pinned dir the plist points at, so only installed-from-main code is
# ever live. cp (not ln -s) is the whole point here. (ARCH-116)

say "Phase 3: gateway pinned copy → $GATEWAY_LIB"
mkdir -p "$GATEWAY_LIB"
# builder_root.py rides along because spend_callback.py imports it as a sibling to
# find the run-records root, and this dir is the gateway's only PYTHONPATH entry
# (security review, PR 538). It is stdlib-only, and pinned from main like the rest.
for src in "$REPO"/gateway/*.py "$REPO"/gateway/config.yaml "$REPO"/gateway/*.sh \
           "$REPO"/lib/agent-platform/builder_root.py; do
  [[ -e "$src" ]] || continue  # no-match globs expand literally under set -u
  cp "$src" "$GATEWAY_LIB/$(basename "$src")"
done
# The launcher is what the plist execs, so it needs the bit. Pinned like the rest
# (ARCH-116): the plist must never exec a script out of the repo working tree.
chmod +x "$GATEWAY_LIB"/*.sh 2>/dev/null || true
say "Gateway pinned: $(ls "$GATEWAY_LIB" | tr '\n' ' ')"
# Set up the gateway's postgres role, password, database, prisma client and schema (E17·1)
# so virtual keys can be minted. Non-fatal: the gateway starts without the database and
# serves model traffic. A start only checks, so this is the one place that creates anything.
if ! bash "$GATEWAY_LIB/llm-gateway-run.sh" --provision-db; then
  warn "gateway database not provisioned; virtual keys stay unavailable (see gateway/README.md)"
fi

# --- phase 4: launchd plists ------------------------------------------------

say "Phase 4: launchd plists (render templates, not symlink)"
mkdir -p "$LAUNCH_AGENTS"
# System-domain LaunchDaemons live in launchd/ for versioning but install into
# /Library/LaunchDaemons via their own sudo installer — NOT the user gui domain.
# Skip them here so a stray inert copy doesn't land in ~/Library/LaunchAgents
# (a latent hazard if someone later bootstraps it gui-side).
SYSTEM_DAEMON_PLISTS="com.convivy.ip-forwarding.plist com.convivy.sshd-watchdog.plist com.convivy.individuator.plist"
# This is the only record of which launchd/ plists are system-domain. A new one
# added here without a matching entry lands in LaunchAgents as a stray inert
# copy (already true before this file's new-job notice existed) and now also
# gets offered a gui-domain bootstrap line by that notice. There is no second
# list to cross-check this one against — the gap closes only by remembering to
# update it when a system-domain plist is added.
# NEVER silently overwrite a live plist that differs from its template (ARCH-342).
#
# This phase renders over the operator's live launchd config. On 2026-07-13 the live
# com.convivy.overnight-orchestrator.plist carried four env flags that existed nowhere in
# git — the sous-chef phase-split canary and the digest assembler, both set by hand
# with PlistBuddy. Running this installer would have reverted them: fragment mode off,
# the consolidated digest re-broken, silently, with an exit code of 0. The flags have
# since moved to ~/.config/convivy/overnight.env so they cannot drift again — but the
# installer must not be a loaded gun for the NEXT thing someone tunes live.
#
# So: a live plist that differs from what this install would render is REFUSED, with
# the diff, and the run continues. --force overwrites (the "I know, my template is
# right" path). A missing live plist is a normal fresh install and is rendered without
# ceremony. PLIST_FORCE is set from --force in the argument-parsing block above.
plist_drift=0
# A PLIST RENDERED FOR THE FIRST TIME IS NOT A JOB THAT RUNS. Rendering puts it in
# LaunchAgents; only `launchctl bootstrap` loads it, and nothing here does that.
# The activation loop printed at the end names a hardcoded list of labels that has
# not kept up — it covers 16 of the 48 plists in `launchd/` — so a job added since
# that list was last edited is installed, is never mentioned again, and never runs.
# Nothing errors and nothing looks wrong.
#
# `com.convivy.run-observer` is the instance that surfaced this: merged in #451/#454 as
# a nightly sweep, rendered by the loop below, absent from the printed list, and
# therefore inert on every machine that installed it.
#
# The fix below only NAMES which plists this run rendered for the first time — it
# is deliberately NOT a derived-from-disk list of everything installed, because
# several live plists are installed and intentionally NOT loaded (the overnight
# fleet among them), and a derived loop would silently re-enable them.
#
# Naming is all this does. Earlier rounds also printed a paste-ready `launchctl
# bootstrap` command per named job, gated on a same-UID freshness signal meant to
# withhold that command on a machine where "new" means "every plist on disk,"
# including jobs that are deliberately not on by default. Four rounds each hardened
# that signal and each time a different forgeable input defeated it — because
# every candidate signal was itself a same-UID file, and any same-UID file can be
# planted or can already exist for an unrelated reason. There is no such signal
# that can't be forged, so the fix removes the thing being gated instead: no code
# path here ever prints a per-job bootstrap command, so naming a plist — on a
# from-scratch machine or an established one — carries no more than its name and
# a pointer to the curated activation block below.
#
# One gap left open on purpose: a plist a human DELETED from LaunchAgents to
# disable a job also reads as new. The sanctioned disable here is `launchctl
# bootout`, which leaves the file in place, and rendering already silently
# restores a deleted one today — but this now additionally names it. Advisory
# only, nothing executes.
new_plists=""
for src in "$REPO"/launchd/*.plist; do
  name="$(basename "$src")"
  case " $SYSTEM_DAEMON_PLISTS " in
    *" $name "*) continue ;;  # system daemon — installed by its own sudo script
  esac
  dst="$LAUNCH_AGENTS/$name"
  rendered_tmp="$(mktemp "${TMPDIR:-/tmp}/launchd-plist.XXXXXX")"
  sed "s|__HOME__|$HOME|g" "$src" > "$rendered_tmp"
  if [ -f "$dst" ] && ! diff -q "$rendered_tmp" "$dst" >/dev/null 2>&1 && [ "$PLIST_FORCE" -eq 0 ]; then
    plist_drift=$((plist_drift + 1))
    say "  SKIPPED (live differs from rendered template): $name"
    diff -u "$dst" "$rendered_tmp" | sed 's/^/      /' || true
    rm -f "$rendered_tmp"
    continue
  fi
  [ -f "$dst" ] || new_plists="$new_plists ${name%.plist}"
  chmod 644 "$rendered_tmp"
  mv "$rendered_tmp" "$dst"
done
if [ "$plist_drift" -gt 0 ]; then
  say ""
  say "  $plist_drift live plist(s) differ from their template and were NOT overwritten."
  say "  Reconcile them, or re-run with --force to replace the live copies."
  say "  Tunable overnight flags belong in ~/.config/convivy/overnight.env, not a plist"
  say "  (launchd/overnight.env.example) — that is what keeps this from drifting."
fi

# --- phase 4b: decision-record → #*-explanations hooks (ARCH-170 piece B) ----
# Wire the git hooks that post committed ARCH entries / ADRs to #*-explanations.
# Idempotent + seed-once (re-runs leave tracking state alone). Needs the bin
# symlinks from phase 2, so it runs after them. Best-effort — never fail install.

say "Phase 4b: decision-record hooks"
bash "$REPO/bin/install-decision-hooks" || say "  decision-record hooks skipped (non-fatal)"
sh "$REPO/bin/install-knowledge-main-guard" || say "  knowledge-main guard skipped (non-fatal)"
sh "$REPO/bin/install-agent-platform-primary-guard" || say "  agent-platform primary guard skipped (non-fatal)"
sh "$REPO/bin/install-rollover-policy" || say "  rollover policy seed skipped (non-fatal)"

# One inbox per PROJECT, not per directory (ARCH-426). Best-effort like the rest
# of this phase: a blocked inbox holding real notes exits 1 on purpose, and that
# is a thing to merge by hand rather than a reason to fail the install.
say "Phase 4c: project inbox links"
python3 "$REPO/bin/link-project-inboxes" || say "  some project inboxes need attention (non-fatal) — re-run bin/link-project-inboxes after merging them"

# --- phase 5: iTerm2 DynamicProfiles — DELIBERATELY NOT HERE ----------------
# Per-product co styling (ARCH-077) needs the DynamicProfiles JSON installed
# where iTerm2 actually runs — the operator's LAPTOP — not the mini. This install.sh
# runs on the mini, which is headless (no iTerm2.app), so deploying profiles
# here is a no-op that only litters the mini's filesystem. That was the
# ARCH-077 bug (ARCH-078): the prior session symlinked profiles into the
# mini's iTerm support dir, iTerm never saw them, and SetProfile silently
# no-opped. The co wrapper (which runs on the mini) still emits the OSC-1337
# SetProfile escape — it travels up the ssh/tmux pipe to the laptop's iTerm.
# The profiles themselves are deployed laptop-side by:
#     the operator's own ssh-alias installer, run on the LAPTOP

# NEWLY INSTALLED PLISTS, NAMED SEPARATELY AND FIRST. These have never been
# bootstrapped on this machine — they were copied into LaunchAgents moments ago and
# will not run until they are. The generic list below is a hardcoded subset that
# has not kept up with `launchd/`, which is how a merged nightly job can be
# installed and inert with nothing reporting it.
#
# Naming is all this does — see the phase-4 comment for why no per-job
# `launchctl bootstrap` command is ever printed here, on a from-scratch machine
# or an established one. Starting a job is what the curated activation block
# below is for; it deliberately omits jobs that are not on by default.
if [ -n "$new_plists" ]; then
  say ""
  say "  NEW launchd job(s) installed for the first time. They are NOT running yet:"
  for label in $new_plists; do say "    $label"; done
  say ""
  say "  Use the curated activation block below rather than starting them all — it"
  say "  deliberately omits jobs that are not on by default. Read what a job does"
  say "  before adding it there; one that posts, spawns or writes starts doing so"
  say "  on its own schedule from that moment."
fi

# The machine's root (company/decisions/0008), for the "Still TODO" paths below.
# --root, not data_root() (the default): this is the crews/knowledge/checkouts
# anchor, which never moves on BUILDER_ROOT. builder-root was just symlinked
# into LOCAL_BIN by phase 2, above.
ROOT="$("$LOCAL_BIN/builder-root" --root)"

cat <<EOF

Install complete. To activate launchd jobs (idempotent — re-bootstrap is safe):

  for label in com.convivy.knowledge-watcher com.convivy.agent-summary com.convivy.knowledge-autopull com.convivy.model-scorecard com.convivy.model-scorecard-weekly com.convivy.security-scorecard com.convivy.warrant-scorecard com.convivy.metric-alerts com.convivy.agent-cost-report com.convivy.agent-cost-report-refresh com.convivy.velocity-report com.convivy.sshd-watchdog-notify com.convivy.appkit.autoupdate; do
    launchctl bootout "gui/\$(id -u)/\$label" 2>/dev/null || true
    launchctl bootstrap "gui/\$(id -u)" "$LAUNCH_AGENTS/\$label.plist"
  done

The appkit autoupdate plist CHANGED in ARCH-441 — it used to invoke /usr/bin/git
directly and now runs bin/appkit-autoupdate, which asserts the checkout is on main
first. The drift check refuses to overwrite an existing live plist, so on a machine
that already has the old one, deploying it takes:
  bash $REPO/install.sh --force
  launchctl bootout "gui/\$(id -u)/com.convivy.appkit.autoupdate" 2>/dev/null || true
  launchctl bootstrap "gui/\$(id -u)" "$LAUNCH_AGENTS/com.convivy.appkit.autoupdate.plist"

To deploy a GATEWAY change (after merging it to main):
  git -C $REPO checkout main && git -C $REPO pull   # get the reviewed code
  bash $REPO/install.sh                             # phase 3 refreshes $GATEWAY_LIB
  launchctl kickstart -k "gui/\$(id -u)/com.convivy.llm-gateway"   # re-import pinned code
  # (first switch onto the pinned paths needs bootout+bootstrap, not kickstart,
  #  because PYTHONPATH/--config changed; code-only refreshes after that = kickstart)

Still TODO on a fresh mini:
  - claude mcp add kb -- $LOCAL_BIN/mcp-kb-server
  - claude mcp add ops -- $LOCAL_BIN/mcp-ops-server   # signal memory (ARCH-349);
    # needs an agent-class token minted into convivy-orient api-tokens.json and
    # written to ~/.orient-control/ops-mcp-token (0600) — see the server docstring
  - Provision Slack webhooks (see knowledge repo: agent-platform/runbooks/agent-slack-webhook-setup)
  - Clone convivy/knowledge to $ROOT/knowledge (the watcher needs it)
  - Gateway credentials: create ~/.config/convivy/llm-gateway.env, mode 0600, with
    one KEY=value line per provider (OPENAI/DEEPSEEK/MOONSHOT/XIAOMI_MIMO/ZAI) plus
    LITELLM_MASTER_KEY. See gateway/README.md. The launcher refuses to start if the
    file is missing or looser than 0600 (ARCH-342) — keys never go in the plist.
  - Gateway live plist: cp launchd/com.convivy.llm-gateway.plist.example to
    launchd/com.convivy.llm-gateway.plist and bootstrap it. No secrets to fill in.
    Paths already point at $GATEWAY_LIB (run install.sh first so it exists).
  - Link the machine-global Claude config — universal cross-co standing directions
    (Write Better + Talk Better standards, PR-URL labeling, ARCH-108) reach every
    co, incl. KB-independent ones, only through ~/.claude/CLAUDE.md. Canonical copy
    lives in substrate; symlink:
      ln -sfn $ROOT/substrate/machine-global-CLAUDE.md ~/.claude/CLAUDE.md
  - Install the Write Better + Talk Better cleanup-pass commands so /write-better
    and /talk-better work in every co session (the always-on standards above are
    applied without a command; these are the on-demand retrofit passes):
      mkdir -p ~/.claude/commands
      ln -sfn $ROOT/write-better/write-better.md ~/.claude/commands/write-better.md
      ln -sfn $ROOT/write-better/talk-better.md  ~/.claude/commands/talk-better.md
EOF
