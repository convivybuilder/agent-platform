"""notify — best-effort phone-push egress for urgent platform alerts (ARCH-269).

A thin POST helper that pushes the genuinely-urgent, currently-Slack-only
platform events to the operator's phone via ntfy, on a SEPARATE topic from PA's under the
SAME convention (PA ADR-0034): counts and dollars only, never content — with one
deliberate exception. `tcc_prompt_watch.push_alert` sends the requesting
process name and the protected item name (e.g. "Documents Folder access
requested by tmux"), because the operator has to know what he's being asked before he
can answer the TCC prompt over Screen Sharing; a counts-only alert would give
him nothing to act on.

This is the egress only. Detection/enforcement logic stays put — the substrate
budget cap and PA's app cost guard remain peers with zero cross-dependency
(ARCH-249). Tiering is the caller's job: push only the genuinely urgent (a
budget HARD-cap crossing); routine soft-cap crossings and the cost digests
stay in `#agent-cost`.

Credential resolution mirrors PA's push-cred — Keychain first, then a chmod-600
file — format `ntfy:<topic>` or `pushover:<app_token>/<user_token>`. With no
credential the push degrades silently; the Slack alert behind it is the durable
signal. Set AGENT_PLATFORM_PUSH_DRY_RUN=1 to route the push to stderr with no POST
(the test guard — never let a test fire a real push).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

_LOG = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "AGENT_PLATFORM_PUSH_CRED"
CRED_FILE = Path.home() / ".config" / "agent-platform" / "push-cred"
ENV_DRY_RUN = "AGENT_PLATFORM_PUSH_DRY_RUN"
SOURCE = "agent-platform"
TIMEOUT = 5


def _is_dry_run() -> bool:
    return os.environ.get(ENV_DRY_RUN, "").strip() not in ("", "0", "false", "False")


def _resolve_cred() -> str | None:
    """The push credential — Keychain first, then the chmod-600 file, else None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-a", os.environ.get("USER", ""), "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        if CRED_FILE.exists():
            v = CRED_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
    except OSError:
        pass
    return None


def _build_payload(kind: str, message: str, fields: dict) -> dict:
    """Counts-and-dollars payload — no content ever crosses the wire."""
    payload = {"kind": kind, "source": SOURCE, "message": message}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


def push(kind: str, message: str, **fields) -> bool:
    """Best-effort phone push. Returns True only on a confirmed 2xx send (or
    dry-run), else False.

    ntfy shows the body of a POST to a topic URL verbatim, so `message` goes
    over the wire as plain text — never as the JSON `payload` (ntfy parses JSON
    only when posted to the root URL with a `topic` field, which this does not
    use). `payload`, with its counts/dollars `fields`, is built only for the
    dry-run line; it never crosses the wire.

    Never raises — a push failure must not break the caller (the Slack alert
    behind it is the durable signal). `fields` carry counts/dollars only; do not
    pass content (subject, body, text, names) — that constraint is on `fields`,
    which only ever feeds the dry-run log line. `message` itself is
    caller-controlled free text sent over the wire verbatim; most callers keep
    it to counts/dollars per PA ADR-0034, but `tcc_prompt_watch.push_alert` is
    a deliberate exception (see the module docstring).
    """
    payload = _build_payload(kind, message, fields)
    cred = None
    try:
        if _is_dry_run():
            sys.stderr.write(f"[notify dry-run] {json.dumps(payload)}\n")
            return True
        cred = _resolve_cred()
        if not cred:
            _LOG.debug("notify.push: no credential configured; skipping (kind=%s)", kind)
            return False
        import requests  # lazy — not installed in all environments  # noqa: PLC0415
        if cred.startswith("ntfy:"):
            topic = cred[len("ntfy:"):]
            resp = requests.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                headers={"Title": kind, "Priority": "high", "Tags": "warning"},
                timeout=TIMEOUT,
            )
        elif cred.startswith("pushover:"):
            app_token, _, user_token = cred[len("pushover:"):].partition("/")
            resp = requests.post(
                "https://api.pushover.net/1/messages.json",
                json={"token": app_token, "user": user_token, "message": message},
                timeout=TIMEOUT,
            )
        else:
            _LOG.warning("notify.push: unknown credential format (expected ntfy:/pushover:)")
            return False
        if resp.status_code // 100 != 2:
            _LOG.warning("notify.push: non-2xx response %s (kind=%s)", resp.status_code, kind)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort egress; never raise into the caller
        # No exc_info/str(exc) here: requests/urllib3 embed the request URL
        # (https://ntfy.sh/<topic>) in ConnectionError/Timeout messages, and
        # the topic is the push credential — logging it defeats Keychain
        # storage the moment a transient network failure hits an always-on
        # daemon (this repo's err.log is mode 644).
        backend = ("ntfy" if cred and cred.startswith("ntfy:")
                   else "pushover" if cred and cred.startswith("pushover:")
                   else "unknown")
        _LOG.warning(
            "notify.push failed (kind=%s, backend=%s): %s "
            "(message redacted — may embed the push credential)",
            kind, backend, type(exc).__name__,
        )
        return False
