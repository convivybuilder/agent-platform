"""tcc_prompt_watch — push a phone alert when macOS raises a TCC permission
prompt on this machine (issue_01M3Q1VHWARM4NKYRH44D0MYX5).

WHY. Since 2026-09-27, at least five crew-session freezes: a background process
blocks in `openat` on a macOS privacy-consent (TCC) dialog nobody is at the
screen to answer, because the operator works remotely. Nothing watched for the dialog
appearing, so a freeze sat unanswered for hours (2026-09-28 23:13 MST to
2026-09-29 ~09:10).

TWO tccd MESSAGE FAMILIES CARRY A REAL PROMPT, found by reading the macstudio's own
unified log (`/usr/bin/log show --predicate 'process == "tccd"'`), not from
Apple documentation, which does not cover this internal wording:

- `Prompting for access to indirect object <item> by <process>` — the
  FileProvider-backed flow (`TCCAccessRequestIndirect`). Confirmed present:
  "...iCloud Drive by python3.14" at 2026-09-29 09:24:23.657285-0700.
- `AUTHREQ_PROMPTING: msgID=<id>, service=<kTCCService...>, subject=Sub:{...}
  Resp:{...}` — the direct-request flow (`TCCAccessRequest`). Confirmed
  present for kTCCServiceSystemPolicyDocumentsFolder (09:14:45.229649-0700),
  kTCCServicePhotos (09:14:41.279735-0700), kTCCServiceSystemPolicyDesktopFolder
  (02:41:58.801604-0700) and kTCCServiceSystemPolicyNetworkVolumes
  (09:14:43.946988-0700) — none of which the first pattern alone catches. This
  is the gap the issue named for the 2026-09-28 Documents and Photos prompts.
  No literal kTCCServiceMediaLibrary (Music) line was found in the retained
  log to confirm against directly; AUTHREQ_PROMPTING is the generic pre-prompt
  line the same code path emits for every service in this family, so it is
  expected to cover Music the same way — unconfirmed by a seen occurrence.

THE EXACT LINE THE ISSUE QUOTES FOR 2026-09-28 23:13 MST — `tccd ... Prompting
for access to indirect object iCloud Drive by tmux` — COULD NOT BE FOUND in
the macstudio's current unified log, checked with `log show` over the full day and a
narrow window around 23:13, both with and without --info/--debug. The only
`Prompting for access` line anywhere in the retained log is the 09:24:23
python3.14 one above. The predicate below is validated against that line and
the four AUTHREQ_PROMPTING lines instead — `log show --last 3d` runs to roughly
500K tccd lines, so the specific 23:13 window is a plausible casualty of
retention under that volume, not a sign the predicate is wrong.

STREAMING, NOT POLLING. `/usr/bin/log stream --style ndjson` (always the full
path — `log` is a zsh builtin) emits one JSON object per log line on stdout,
each carrying `eventMessage`. `parse_prompt` reads that field; nothing here
reads Apple's private `formatString` machinery, so it is unaffected by the
`<private>` redaction seen elsewhere in this log (kTCCService names and process
paths were never marked private in the confirmed lines above).

DEDUP. The two message families never fire for the same request (different
code paths, no correlating ID visible in either message alone), so there is
no risk of one prompt producing two pushes across families. Within one family,
`Deduper` suppresses a repeat push for the same (process, item) pair inside a
short window — the same real prompt logged again before the operator has answered it
must not re-alert every few seconds.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

import notify

LOG_STREAM_CMD = (
    "/usr/bin/log", "stream", "--style", "ndjson", "--predicate",
    'process == "tccd" AND '
    '(eventMessage CONTAINS "Prompting for access" '
    'OR eventMessage CONTAINS "AUTHREQ_PROMPTING")',
)

DEDUP_WINDOW_SECONDS = 300.0  # 5 minutes: one push per (process, item) in this window
RESTART_DELAY_SECONDS = 5.0

_INDIRECT_RE = re.compile(
    r'Prompting for access to indirect object (?P<item>.+) by (?P<process>\S+)\s*$'
)
_AUTHREQ_PROMPTING_RE = re.compile(
    r'AUTHREQ_PROMPTING: msgID=\S+, service=(?P<service>kTCCService\w+), '
    r'subject=Sub:\{(?P<subject>[^}]*)\}Resp:\{(?P<resp>[^}]*)\}'
)
_RESPONSIBLE_PATH_RE = re.compile(r'responsible_path=([^,}]+)')
_BINARY_PATH_RE = re.compile(r'binary_path=([^,}]+)')


def _basename(path: str) -> str:
    path = path.strip()
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _humanize_service(service: str) -> str:
    """`kTCCServiceSystemPolicyDocumentsFolder` -> `"System Policy Documents
    Folder"`. General on purpose (split camelCase after stripping the
    `kTCCService` prefix) rather than a hand-maintained name table, so a service
    kind this module has never seen still reads as words, not a constant."""
    name = service[len("kTCCService"):] if service.startswith("kTCCService") else service
    spaced = re.sub(r'(?<!^)(?=[A-Z])', ' ', name).strip()
    return spaced or service


def parse_prompt(event_message: str) -> tuple[str, str] | None:
    """(process, item) out of one tccd `eventMessage` that raises a real TCC
    prompt, or None if this line is not one of the two known prompt-raising
    message families (see module docstring)."""
    m = _INDIRECT_RE.search(event_message)
    if m:
        return _basename(m.group("process")), m.group("item").strip()

    m = _AUTHREQ_PROMPTING_RE.search(event_message)
    if m:
        item = _humanize_service(m.group("service"))
        resp = m.group("resp")
        path_m = _RESPONSIBLE_PATH_RE.search(resp) or _BINARY_PATH_RE.search(resp)
        if path_m:
            process = _basename(path_m.group(1))
        else:
            subject = m.group("subject").strip()
            process = _basename(subject) if subject else "unknown"
        return process, item

    return None


def extract_event_message(ndjson_line: str) -> str | None:
    """The `eventMessage` field out of one raw `log stream --style ndjson`
    line, or None for a blank line, a non-JSON line (the stream's own startup
    banner), or a JSON object with no `eventMessage`."""
    line = ndjson_line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    msg = event.get("eventMessage")
    return msg if isinstance(msg, str) and msg else None


class Deduper:
    """Suppresses a repeat push for the same (process, item) inside a window.

    Keyed on content, not a tccd msgID — the indirect-object message carries no
    ID of its own, so a per-request ID cannot be the shared key across both
    message families. The tradeoff: two genuinely different prompts for the
    same process+item within the window collapse into one push. Given the
    failure this exists to catch (a session frozen for hours until someone
    notices), one push naming the right process and item is what matters; a
    second push for the identical pair while the first is still unanswered adds
    nothing the operator can act on differently.
    """

    def __init__(self, window_seconds: float = DEDUP_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._seen: dict[tuple[str, str], float] = {}

    def should_push(self, process: str, item: str, now: float) -> bool:
        key = (process, item)
        last = self._seen.get(key)
        self._seen[key] = now
        return last is None or (now - last) >= self._window


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sys.stderr.write(f"[{ts}] tcc-prompt-watchdog: {msg}\n")
    sys.stderr.flush()


def push_alert(process: str, item: str, pusher: Callable[..., bool] = notify.push) -> bool:
    """One best-effort phone push naming the process and the protected item.
    `pusher` never raises (`notify.push`'s own contract); this still logs the
    outcome to the watchdog's own log rather than trusting silence."""
    message = (
        f"{item} access requested by {process} — crew sessions may be frozen "
        f"until you answer this over Screen Sharing"
    )
    ok = pusher("tcc-prompt", message)
    if ok:
        _log(f"pushed: {item} by {process}")
    else:
        _log(f"push failed or not sent: {item} by {process}")
    return ok


def process_line(
    ndjson_line: str,
    deduper: Deduper,
    now: float | None = None,
    pusher: Callable[..., bool] = notify.push,
) -> bool:
    """Parse one raw ndjson line and push if it is a new-within-the-window
    prompt. Returns whether a push was attempted (not whether it succeeded)."""
    event_message = extract_event_message(ndjson_line)
    if event_message is None:
        return False
    parsed = parse_prompt(event_message)
    if parsed is None:
        return False
    process, item = parsed
    if not deduper.should_push(process, item, now if now is not None else time.monotonic()):
        return False
    push_alert(process, item, pusher=pusher)
    return True


def run(
    cmd: tuple[str, ...] = LOG_STREAM_CMD,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    deduper: Deduper | None = None,
    pusher: Callable[..., bool] = notify.push,
    max_restarts: int | None = None,
) -> None:
    """Run `log stream` forever, pushing on each matching prompt line.
    Restarts the subprocess whenever it exits (a crash, a log daemon restart,
    `log stream` itself exiting) rather than letting the watchdog go silent.

    `max_restarts` is None in production (run forever, matching the launchd
    KeepAlive contract this is deployed under); a test passes a small int to
    make the loop terminate instead of hanging on stdout forever.
    """
    if deduper is None:
        deduper = Deduper()
    restarts = 0
    while True:
        try:
            proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as exc:
            _log(f"failed to start log stream: {exc}; retrying in {RESTART_DELAY_SECONDS}s")
        else:
            _log(f"log stream started (pid={getattr(proc, 'pid', '?')})")
            try:
                for line in proc.stdout:
                    try:
                        process_line(line, deduper, pusher=pusher)
                    except Exception as exc:  # noqa: BLE001 — one bad line must not kill the stream
                        _log(f"error handling a log line (ignored): {exc}")
            finally:
                try:
                    proc.stdout.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
                proc.wait()
            _log(f"log stream exited (code={proc.returncode}); "
                 f"restarting in {RESTART_DELAY_SECONDS}s")

        restarts += 1
        if max_restarts is not None and restarts >= max_restarts:
            return
        sleep(RESTART_DELAY_SECONDS)


def main() -> int:
    _log("starting")
    try:
        run()
    except KeyboardInterrupt:
        pass
    return 0
