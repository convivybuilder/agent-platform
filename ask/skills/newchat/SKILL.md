---
name: newchat
description: Start a new, separate ask chat — a fresh parallel conversation like opening a new chat on Claude.ai. Spawns its own remote-control session (reachable from the phone) with its own transcript, leaving the current chat untouched. Optionally name it with a topic, e.g. /newchat groceries.
---

# `/newchat` — open a new parallel ask chat

The operator wants a **fresh, separate conversation** on the ask surface — the equivalent of clicking "New chat" in Claude.ai. This spawns a brand-new `claude --remote-control` session in `~/Convivy/ask/`, which:

- appears as its **own session** in the phone's Code list (its own remote-control registration), so the operator can switch between chats, and
- writes its **own transcript** `~/.claude/projects/<ask-slug>/<uuid>.jsonl` — one process = one conversation = one clean KB RawItem for the PA adapter (ADR-0027). This is why a new *process* per chat (not `/clear` in one session) is the model: it guarantees a clean per-conversation boundary.

The current chat is **left running and untouched** — `/newchat` only adds a sibling.

## Naming (so the phone list stays navigable)

Every ask chat's slug starts with `ask-new-session` (ARCH-238) so it reads as an ask chat in the phone's Code list, with the topic appended when there is one:

- `/newchat <topic>` → name the session `ask-new-session-<topic>` (slugify the topic: lowercase, spaces/punctuation → `-`). E.g. `/newchat trip to japan` → `ask-new-session-trip-to-japan`.
- `/newchat` with no arg → name it `ask-new-session` (the bridge appends a unique suffix automatically). Prefer to ask the operator for a one-word topic if it's easy, so the list reads well.

## Procedure

1. **Resolve the tmux session + the chat name.**
   ```bash
   SESS=$(tmux display-message -p '#{session_name}')   # the ask session (usually "ask")
   ```
   Build `NAME` from the topic arg: lowercase, non-alphanumeric → `-`, collapse repeats, strip leading/trailing `-`, then prefix `ask-new-session-`. No arg → `NAME=ask-new-session`. Keep the topic short.

2. **Spawn the new chat** as a new window in the ask tmux session, running `co` in the ask dir. `-d` so the current chat's window stays foreground; the env prefix names the new remote-control session; `|| exec zsh` keeps a failed spawn diagnosable.
   ```bash
   tmux new-window -a -d -t "$SESS:" -n "$NAME" -c "$HOME/Convivy/ask" \
     -e CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX="$NAME" \
     "zsh -lc 'co || exec zsh'"
   ```

3. **Verify it registered.** `co` launches `claude` in ~10s. Do NOT check `pane_current_command` — it races (reads `zsh` for several seconds during startup even on success; learned in ARCH-231). Poll the new window's pane for the remote-control banner instead:
   ```bash
   WIN=$(tmux list-windows -t "$SESS" -F '#{window_index} #{window_name}' | awk -v n="$NAME" '$2==n{print $1; exit}')
   for i in $(seq 1 15); do
     out=$(tmux capture-pane -t "$SESS:$WIN" -p 2>/dev/null)
     echo "$out" | grep -qE 'remote-control is active|◷ ctx|ctx [0-9]+K' && { echo UP; break; }
     echo "$out" | grep -qiE 'command not found|no such|error' && { echo FAILED; break; }
     sleep 2
   done
   ```

4. **Hand off to the operator.** On `UP`:
   > New chat **`ask-new-session[-<topic>]`** is live — open it from the Code list in the app. This chat keeps running.

   On failure: the current chat is untouched — say so, report, and dump the new pane (`tmux capture-pane -t "$SESS:$WIN" -p | tail -30`) for the cause.

## Invariants

- **Never disturb the current chat.** `/newchat` only spawns a sibling window; it never exits, clears, or restyles the session it runs in.
- **One new chat per invocation.**
- **Always name it** (prefix `ask-new-session`), or the phone's session list becomes unnavigable.
- This is the multi-chat surface's only spawn path — for cleanup, the operator closes a chat from the phone (`/exit`) or its tmux window; if dead windows accumulate, a future `/chats` helper can list + reap them (not built yet).
