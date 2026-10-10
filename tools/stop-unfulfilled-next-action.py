#!/usr/bin/env python3.11
"""Stop hook: refuse to end a turn that PROMISES a next action instead of taking it.

WHY THIS IS A MECHANISM AND NOT A SENTENCE. The rule already exists, verbatim
and emphatic, in ~/.claude/CLAUDE.md: "End a turn only for a reason you can
name" / "'Next I'll X' is legitimate only if a tool call follows in the same
turn" (the operator, 2026-08-23). On 2026-09-03 a single session broke it roughly
fifteen times while that text was loaded in its context the whole time. Adding
another sentence to a file that already says this would have produced a
sixteenth. So the rule gets a check that fires instead.

WHAT IT DOES. On Stop, read the LAST assistant message's text. If a sentence in
it is an unconditional first-person commitment to do something next, block the
stop and hand back the sentence. Blocking makes the model continue, which is
the whole point: the action was one tool call away.

WHAT IT DELIBERATELY DOES NOT FIRE ON, because these are the LEGITIMATE reasons
a turn ends and a false positive here trains the reader to ignore the check:

  - blocked on the operator      "tell me which and I'll do it", "unless you redirect"
  - blocked on something started "I'll publish once the coder lands"
  - a question put to the human  anything addressed to "you"

Both are decided per SENTENCE, not per message, so a message that legitimately
ends on "waiting for X" is not rescued by an unconditional promise elsewhere in
it — and a real promise is not excused by an unrelated conditional nearby.

FAILS OPEN, ON PURPOSE. Any error — no transcript, unreadable JSON, anything —
exits 0 and allows the stop. A hook that wedges a session is worse than the
habit it corrects.

NEVER BLOCKS THE SAME MESSAGE TWICE. The blocked text's hash is recorded per
session; a second Stop on identical text is allowed through. Without this, a
turn that genuinely must end while stating a next step would loop forever, and
an inescapable check is one someone disables rather than obeys.
"""
import hashlib
import json
import os
import re
import sys

STATE_DIR = os.path.expanduser("~/.claude/.stop-next-action")

# An unconditional, first-person, near-future commitment.
PROMISE = re.compile(
    r"""(?:^|\s)(?:
          next(?:\s+up)?\s*,?\s+i['’]?ll\b
        | then\s+i['’]?ll\b
        | i['’]?ll\s+(?:now|go\s+ahead\s+and)\b
        | i['’]?m\s+going\s+to\b
        | i\s+will\s+now\b
        | let\s+me\s+(?:now\s+)?(?:go\s+)?(?:ahead\s+and\s+)?
      )""",
    re.IGNORECASE | re.VERBOSE,
)

# The sentence is waiting on something, so ending the turn is correct.
DEPENDENT = re.compile(
    r"\b(?:when|once|after|if|unless|pending|until|while|as soon as|"
    r"waiting|awaiting|blocked)\b",
    re.IGNORECASE,
)

# The sentence hands a decision to the operator, so ending the turn is correct.
OPERATOR = re.compile(
    r"(?:\byou\b|\byour\b|\btell me\b|\blet me know\b|\bsay the word\b|"
    r"\bunless you\b|\bwant me to\b|\byours\b)",
    re.IGNORECASE,
)


def last_assistant_text(path):
    """The text of the final assistant message, or '' if there isn't one."""
    last = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "assistant":
                last = rec
    if last is None:
        return ""
    content = (last.get("message") or {}).get("content") or []
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(parts)


def offending_sentence(text):
    """The first unconditional promise sentence, or None."""
    # Split on sentence enders and newlines; a bullet is its own sentence.
    for raw in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = raw.strip().lstrip("-*# ").strip()
        if not s or not PROMISE.search(s):
            continue
        if DEPENDENT.search(s) or OPERATOR.search(s):
            continue
        return s
    return None


def already_blocked(session_id, text):
    """True if this exact text was blocked before; records it either way."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")[:80]
    marker = os.path.join(STATE_DIR, f"{safe}-{digest}")
    if os.path.exists(marker):
        return True
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(marker, "w") as fh:
        fh.write("")
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return 0
    try:
        text = last_assistant_text(path)
        sentence = offending_sentence(text)
        if not sentence:
            return 0
        if already_blocked(payload.get("session_id", ""), text):
            return 0
    except Exception:
        return 0

    print(json.dumps({
        "decision": "block",
        "reason": (
            "This turn ends on a stated next action instead of taking it: "
            f"\"{sentence[:200]}\". That action is one tool call away — take it "
            "now, in this turn. A turn ends only for a reason you can name: the "
            "work is done, you are blocked on the operator, you are blocked on "
            "something external you already started, or you are out of context. "
            "If one of those applies, say which and rephrase so the sentence "
            "names the blocker rather than promising."
        ),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
