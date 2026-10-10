#!/bin/bash
# SessionStart guard — keep ~/.claude/settings.json pinned to the ARCH-134 model.
#
# THE HAZARD (substrate ARCH-134 / ARCH-304): Claude Code's `/model <x>` command
# switches the running session AND silently saves <x> as `"model"` in
# ~/.claude/settings.json ("saved as your default for new sessions"). A casual
# per-session switch — e.g. `/model fable` to try Fable 5 with the operator in the loop —
# therefore un-pins the machine-wide default, and every later bare `claude`
# launch inherits it. Fable 5 at $10/$50 per Mtok burned $100+ in a day on
# 2026-06-11 exactly this way (an unpinned default), which is why the pin exists.
# It happened again on 2026-07-02 via `/model fable`; the steward caught it by
# hand, and this hook is the durable version of that catch.
#
# FIRE CONDITION: settings.json's `"model"` is absent or differs from the pin.
# A missing key is also a fire — with no `model` key, bare `claude` falls back
# to the CLI's built-in default, which is Fable 5. The one legitimate state is
# silent: `"model"` equals the pin.
#
# ACTION: restore the pin in settings.json (atomic write), then tell the session
# what happened via hookSpecificOutput.additionalContext. Restore rather than
# warn-only: rewriting the default is side-effect-free for every RUNNING session
# (a session's model is resolved at launch and held in session state — this file
# is only read at startup), so there is nothing a human needs to weigh, and a
# warn-only guard leaves bare-`claude` exposure open until someone acts. The
# hook cannot change the model of the session it fires in — if that session was
# itself launched off the un-pinned default, the injected context says so and
# the fix is to relaunch. To change the machine default deliberately, edit PIN
# below (it's the ARCH-134 decision made executable).
#
# WHY A HOOK: `/model` writes the file mid-session with no hookable event, so
# the earliest durable checkpoint is the next SessionStart — which fires on
# every launch path (co, raw claude, FleetView, agentctl workers). The window
# between the `/model` save and the next session start stays open by design;
# every scripted `claude` caller on this machine (co, agentctl, model-scorecard)
# passes an explicit --model and never reads this default, so the residual
# exposure is a human bare-`claude` launch in that window.
#
# Needs python3 for safe JSON editing (guaranteed on macOS at /usr/bin/python3);
# if it's somehow missing, fall back to warn-only — never let the guard fail a
# session. Always exit 0. Bash 3.2 safe (ARCH-082).
#
# Lives in the agent-platform repo (versioned + reviewable); referenced by
# absolute path from ~/.claude/settings.json, which is user config and not
# under git (like block-pr-merge.sh and check-anthropic-billing-lane.sh —
# re-add this hook when provisioning a new box).

set -u

PIN='claude-opus-5-5[1m]'
SETTINGS="$HOME/.claude/settings.json"

# No settings file → nothing to guard (a fresh box; provisioning creates it).
[ -f "$SETTINGS" ] || exit 0

PYTHON3=$(command -v python3 || true)
[ -n "$PYTHON3" ] || PYTHON3=/usr/bin/python3

if [ ! -x "$PYTHON3" ]; then
  printf '%s\n' "⚠️  model-pin-guard: python3 not found — cannot verify the ARCH-134 model pin in $SETTINGS. Check it by hand." >&2
  exit 0
fi

"$PYTHON3" - "$SETTINGS" "$PIN" <<'PYEOF'
import json, os, sys, tempfile

settings_path, pin = sys.argv[1], sys.argv[2]

try:
    with open(settings_path) as f:
        data = json.load(f)
except (OSError, ValueError):
    # Unreadable/invalid JSON: warn, don't touch. A rewrite here could clobber
    # whatever a human is mid-way through fixing.
    print(
        f"⚠️  model-pin-guard: {settings_path} is unreadable or not valid JSON — "
        f"cannot verify the ARCH-134 model pin. Check it by hand.",
        file=sys.stderr,
    )
    sys.exit(0)

current = data.get("model")
if current == pin:
    sys.exit(0)  # the one legitimate state — silent

data["model"] = pin

# Atomic replace: concurrent SessionStarts (an overnight fan-out) may all run
# this; each writes the same pinned content, and os.replace keeps the file
# whole under any interleaving.
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(settings_path), suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)
except OSError:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    print(
        f"⚠️  model-pin-guard: found model={current!r} but could not rewrite "
        f"{settings_path} — restore the ARCH-134 pin ({pin}) by hand.",
        file=sys.stderr,
    )
    sys.exit(0)

was = repr(current) if current is not None else "absent (falls back to the CLI built-in default, Fable 5)"

# Live banner (best-effort; stderr).
print(
    f"⚠️  MODEL-PIN RESTORED (ARCH-134): ~/.claude/settings.json model was {was}; "
    f"restored to {pin}. If that came from a deliberate /model switch, the running "
    f"session it was made in keeps its model — only the saved default was reset.",
    file=sys.stderr,
)

# Inject into the assistant's context so it surfaces to the operator on the first turn.
ctx = (
    f"MODEL-PIN GUARD (ARCH-134/ARCH-304): at session start, ~/.claude/settings.json "
    f"had model {was} instead of the pinned {pin}; this hook restored the pin. "
    f"That usually means a recent /model command saved itself as the machine default. "
    f"Surface this to the user. Note: this hook cannot change the CURRENT session's "
    f"model — if this session was launched bare (plain `claude`, no --model) it may "
    f"itself be running the un-pinned model; suggest checking /status and relaunching "
    f"via co if so. Do not treat the restore as an error; it is the guard working."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }
}))
PYEOF

exit 0
