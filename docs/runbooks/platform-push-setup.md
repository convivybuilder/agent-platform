# Runbook — provision the platform's phone-push topic (ARCH-269)

`lib/agent-platform/notify.py` pushes the genuinely-urgent platform alerts (a budget HARD-cap crossing today; overnight-orchestrator death and the full-monty 401 as fast-follows) to the operator's phone via ntfy. It reuses the egress PA already installed (ADR-0034) on a **separate topic under the same convention** — counts and dollars only, never content. Until a credential is provisioned the push degrades silently and the Slack alert in `#agent-cost` is the signal; this runbook provisions it.

The topic is the only secret, so it's a long unguessable string, and it stays off git and on the mini — exactly like PA's `PA_PUSH_CRED`.

## Steps

1. `phone` — in the **ntfy** app (already installed for PA), "Subscribe to topic" → a new long random topic, distinct from PA's, e.g. `platform-alert-3d9f1a7c4e22`. Keeping it separate from PA's topic is the point (ARCH-269): the platform stream mutes/routes independently of PA's metered-key stream.

2. `mini` — store the credential (Keychain preferred; `_resolve_cred` tries Keychain, then `~/.config/agent-platform/push-cred`):
   ```
   security add-generic-password -U -a "$USER" -s AGENT_PLATFORM_PUSH_CRED -w "ntfy:platform-alert-3d9f1a7c4e22"
   ```
   File fallback:
   ```
   mkdir -p ~/.config/agent-platform && umask 177
   printf '%s' "ntfy:platform-alert-3d9f1a7c4e22" > ~/.config/agent-platform/push-cred
   ```

3. `mini` → `phone` — fire one real push through the live path and watch the phone:
   ```
   AGENT_PLATFORM_PUSH_DRY_RUN= python3.11 -c "import sys; sys.path.insert(0,'$HOME/.local/lib/agent-platform'); import notify; print('pushed:', notify.push('test', 'platform push runbook test', spent_usd=0))"
   ```
   Expect `pushed: True` and a notification within a few seconds. (Pass `pushover:<app>/<user>` instead of `ntfy:<topic>` to use Pushover — `notify.py` speaks both, same as PA.)

## Notes

- **Tiering (ARCH-269):** only genuinely-urgent events push. The budget **soft** cap stays Slack-only; routine cost digests stay in `#agent-cost`. The push is best-effort — a failed POST logs a WARNING and never raises, and the Slack alert behind it is the durable record.
- **No content ever crosses the wire** — the payload is `kind`, `source`, `message`, and numeric counts/dollars only (asserted in `tests/test_notify.py`).
- **Logic stays peer-separate** from PA's cost guard (ARCH-249): this shares only the egress topic + convention, never the breaker logic.
