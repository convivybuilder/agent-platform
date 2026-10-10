#!/bin/bash
# Tests for the rollover-successor fallback-shell leak (agent-platform PR #574
# round 2). `co_rollover.spawn_successor` used to set CO_ROLLOVER_SUCCESSOR=1
# via tmux `-e`, which lands in the environment of the WRAPPER shell itself
# (`zsh -lc 'co -r ... || exec zsh'`), not just the `co` command it runs. When
# `co -r` fails, `|| exec zsh` replaces the wrapper with an interactive shell
# that still carries the marker inherited from that `-e`, so a by-hand `co -r`
# retry from that pane would still skip knowledge-ctx's forced
# re-individuate — exactly the guard-race `_co_reindividuate` exists to avoid
# only for the ACTUAL successor launch, not for a human retrying by hand.
#
# The fix scopes the marker to the `co` command alone, as a shell prefix
# assignment (`CO_ROLLOVER_SUCCESSOR=1 co -r ... || exec zsh`): bash/zsh give a
# prefix assignment's binding only to the command it prefixes, so the fallback
# branch of `||` never sees it.
#
# No tmux involved — this runs the exact shell string shape directly under
# `zsh -lc`, with stub `co` and a stub that records its own environment.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
. "$HERE/lib.sh"

WORK=$(mktemp -d)

# Stands in for `co -r ...`: always fails, as a real launch failure would.
cat > "$WORK/co-that-fails" <<'EOF'
#!/bin/zsh
exit 1
EOF
chmod +x "$WORK/co-that-fails"

# Stands in for the `|| exec zsh` fallback: records whether the marker
# reached IT, rather than actually exec'ing an interactive shell.
ENV_LOG="$WORK/env.log"
cat > "$WORK/record-env" <<EOF
#!/bin/zsh
if [[ -n \${CO_ROLLOVER_SUCCESSOR+set} ]]; then
    echo "CO_ROLLOVER_SUCCESSOR=\${CO_ROLLOVER_SUCCESSOR}" > "$ENV_LOG"
else
    : > "$ENV_LOG"
fi
EOF
chmod +x "$WORK/record-env"

shell_cmd="CO_ROLLOVER_SUCCESSOR=1 $WORK/co-that-fails || $WORK/record-env"

echo "== the fallback shell string scopes the marker to the co command only =="

/bin/zsh -lc "$shell_cmd"
RC=$?
assert_rc 0 "$RC" "the shell string itself exits 0 (the fallback ran and recorded)"

ENV_CONTENT=$(cat "$ENV_LOG" 2>/dev/null)
assert_eq "" "$ENV_CONTENT" \
    "the || exec zsh fallback does not see CO_ROLLOVER_SUCCESSOR (marker was scoped to the failed co command)"

rm -rf "$WORK"
finish
