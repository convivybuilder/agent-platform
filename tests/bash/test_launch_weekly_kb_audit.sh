#!/bin/bash
# tests/bash/test_launch_weekly_kb_audit.sh — the two reports posted to the
# plane keep their line breaks (reviewer, PR #803 round 2).
#
# signal_client.sanitise() collapses every newline to a space. Both
# render()'s write-history report and render_access()'s access report are
# deliberately multi-line and meant to be skimmed; a single sanitise() call
# over the whole string flattens each one into one unreadable line. The fix
# sanitises each line and rejoins with real newlines, same as
# bin/agent-platform-autopull already does for the same reason.
#
# The real signal_client no-ops without a delivery token and never exposes
# what it was called with, so a fake stands in. Its sanitise() keeps the two
# properties that matter here — strip a control character, collapse an
# embedded newline within a single string — so feeding it the WHOLE
# multi-line report (the pre-fix shape) collapses to one line exactly as the
# real bug did, and this test catches a regression back to that shape.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

SCRIPT="$REPO_ROOT/bin/launch-weekly-kb-audit"

H=$(mktemp -d)
mkdir -p "$H/bin" "$H/lib/agent-platform" "$H/agent-runs"
cp "$SCRIPT" "$H/bin/launch-weekly-kb-audit"
chmod +x "$H/bin/launch-weekly-kb-audit"

# Stub agentctl: the write-history report is two lines, the second carrying a
# control character (BEL, \x07) a real report could never carry on purpose
# but sanitise() must still strip.
cat > "$H/bin/agentctl" <<'EOF'
#!/bin/bash
if [ "$1 $2" != "kb audit" ]; then
    echo "agentctl stub: only 'kb audit' is stubbed, got: $*" >&2
    exit 99
fi
printf 'FIRST-LINE-OF-WRITE-REPORT\nSECOND-LINE-WITH-\a-BELL\n'
exit 0
EOF
chmod +x "$H/bin/agentctl"

# Stub kb_audit.py: the access report is one clean line, exit 0.
cat > "$H/lib/agent-platform/kb_audit.py" <<'EOF'
import sys
print("ACCESS-REPORT-CLEAN-LINE")
sys.exit(0)
EOF

# Fake signal_client: real one no-ops without AGENT_SIGNAL_TOKEN and never
# says what it was posted with, so this captures the `combined` body instead.
cat > "$H/lib/agent-platform/signal_client.py" <<'EOF'
import os
import re

_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_NEWLINE_RE = re.compile(r"[\n\r\t]+")


def sanitise(s, *, max_len=None):
    s = _CONTROL_RE.sub("", s)
    s = _NEWLINE_RE.sub(" ", s)
    return s.strip()


def _capture(stream, body, **kw):
    with open(os.environ["COMBINED_MARKER"], "w") as f:
        f.write(body)
    return "stub-posted"


post_report = _capture
post_alert = _capture
EOF

MARKER="$H/combined.txt"
BUILDER_ROOT="$H" COMBINED_MARKER="$MARKER" /bin/zsh "$H/bin/launch-weekly-kb-audit" \
    >/dev/null 2>"$H/stderr.log"
rc=$?

echo "== a clean write-history read and a clean access read exit 0 and post =="
assert_rc 0 "$rc" "both reads clean"
assert_eq "1" "$(test -f "$MARKER" && echo 1 || echo 0)" \
    "the fake signal_client captured what was posted"
combined=$(cat "$MARKER" 2>/dev/null)

echo "== the write-history report's two lines stay two lines =="
case "$combined" in
    *"FIRST-LINE-OF-WRITE-REPORT"$'\n'"SECOND-LINE-WITH"*)
        _PASS=$((_PASS + 1)) ;;
    *)
        _FAIL=$((_FAIL + 1))
        echo "  FAIL: the two lines must stay joined by a real newline, not collapsed to one line"
        echo "        got: [$combined]"
        ;;
esac

echo "== a control character inside a line is still stripped =="
assert_eq "0" "$(printf '%s' "$combined" | grep -c $'\a')" \
    "the BEL inside the second line was stripped"

echo "== the access report's line reached the posted body =="
assert_contains "$combined" "ACCESS-REPORT-CLEAN-LINE" "the access report survived into combined"

rm -rf "$H"
finish
