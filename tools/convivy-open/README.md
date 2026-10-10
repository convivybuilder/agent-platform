# convivy-open

Click a file link in Slack → it opens on **whichever machine you click from**,
resolved against that machine's copy of Convivy. Same link, per-machine path.

- **mini**  → resolves against the mini's own resolved root (company/decisions/0008)
- **laptop** → resolves against that root's SMB-mounted mirror under `/Volumes` (see `resolve-open-base.sh`)

This works because the link points at `http://localhost:8765`, and `localhost`
is always the machine doing the clicking. A tiny launchd-managed HTTP server on
each machine receives the click and runs `open <file>` locally.

## Link format the bot should post

Instead of a bare path like
`<root>/agent-runs/2026-05-22/run-50d95758/dep-audit-report.md`,
post a Slack mrkdwn link to the **path relative to the Convivy root**:

    <http://localhost:8765/open?p=agent-runs/2026-05-22/run-50d95758/dep-audit-report.md|dep-audit-report.md>

The `<url|label>` form is always clickable in Slack (bare URLs and `file://`
are not reliably linkified — that's why we use an explicit localhost link).

**Line numbers.** Append `&line=N` to deep-link to a line:

    <http://localhost:8765/open?p=substrate/decisions.md&line=1131|decisions.md:1131>

The server hands a line request to `typora-goto`, which opens the doc in Typora,
then launches **`~/Applications/ConvivyTyporaGoto.app`** (built by
`install-laptop.sh` via `osacompile`) to drive Find (⌘F) to the line's text
snippet — Typora has no line API. The Find-drive lives in an app bundle, not the
launchd daemon, on purpose: **Accessibility** can't be granted reliably to a
daemon's `python3`/`osascript`, but an app has a stable TCC identity and macOS
**prompts to allow it** on first use (or add `ConvivyTyporaGoto.app` under System
Settings → Privacy & Security → Accessibility). Without a line, or on a machine
without Typora (the mini), it falls back to plain `open`. The agent platform's
`slack.py` emits `&line=N` automatically when a path is written with a trailing
`:N` (e.g. `app/config.py:21`).

### Drop-in for the bot

**Live implementation:** `agent-platform/lib/agent-platform/slack.py` (`file_link`,
`linkify_paths`, `linkify_urls`, `linkify`). As of ARCH-045 the agent platform applies
this automatically at the transport layer — `slack.send()` / `slack.send_as_bot()` run
every outgoing message's text fields through `linkify()`, which wraps Convivy-absolute
paths into convivy-open links **and** wraps bare http(s) URLs in `<...>` (Slack doesn't
reliably auto-linkify bare URLs in block mrkdwn). Callers and agents need do nothing —
emit absolute paths (per ARCH-043) and bare URLs as normal; the bot makes them clickable.

The canonical `file_link` (kept in sync with slack.py):

```python
from urllib.parse import quote

import builder_root

CONVIVY_ROOT = str(builder_root.root()) + "/"

def file_link(abs_path: str, label: str | None = None) -> str:
    """Turn an absolute mini path into a clickable, machine-portable Slack link."""
    rel = abs_path[len(CONVIVY_ROOT):] if abs_path.startswith(CONVIVY_ROOT) else abs_path.lstrip("/")
    url = "http://localhost:8765/open?p=" + quote(rel)
    return f"<{url}|{label or abs_path.rsplit('/', 1)[-1]}>"
```

## Terminal fallback (`convivy-open` CLI)

Clicking can't reach a remote shell, and a headless mini has no GUI for `open`.
So inside an SSH/tmux session, use the companion CLI on the same path the bot
posted — it resolves against *this* machine and acts in the terminal:

    convivy-open <ref>        # print the resolved absolute path (default)
    convivy-open -e <ref>     # open in $EDITOR
    convivy-open -d <ref>     # print its directory
    convivy-open -l <ref>     # page with $PAGER
    convivy-open -o <ref>     # GUI open (same as clicking)

`<ref>` can be a relative path, an absolute mini/laptop path, or the full
`http://localhost:8765/open?p=...` link — paste whatever you have.

Optional shell helpers (adds `cve`/`cvd`/`cvl`) — in `~/.zshrc`:

    source ~/.local/bin/convivy-open.zsh

Then `cvd <ref>` cd's you into the file's directory, `cve <ref>` edits it.

The HTTP server also no longer claims "Opened" when there's no GUI session —
it returns the resolved path and the `convivy-open` command instead.

## Cmd-click in iTerm (Semantic History)

Clicking the Slack `localhost:8765` link (served by `convivy-open-server`) is the
**guaranteed** path. Cmd-clicking a bare absolute path in an iTerm session is a
separate convenience layer: iTerm's **Semantic History** runs **`~/bin/open-md-link`**
on the clicked path. `open-md-link` is the cmd-click handler — it does the
`/Users`→`/Volumes` translation itself, opens `.md` in Typora, and drives Typora's
Find to the clicked line number. (It is **not** `convivy-open`: `convivy-open` is the
CLI + the `:8765` Slack-link server; `open-md-link` is the richer iTerm click handler.
An earlier rev wrongly wired `convivy-open` into Semantic History — see substrate
ARCH-154.)

This is **not a daemon** — it is an iTerm profile setting plus the `~/bin/open-md-link`
script. Two things must be true on the laptop:

1. **`~/bin/open-md-link` exists** on the laptop (and Typora + an Accessibility grant
   for the line-jump). It is self-contained — no env var needed.
2. **The iTerm profile carries the Semantic History command.** It ships in the
   `agent-platform/iterm/co-*.json` DynamicProfiles (deployed laptop-side by
   the operator's own ssh-alias installer):

   ```json
   "Semantic History": {
     "action": "raw command",
     "text": "$HOME/bin/open-md-link \\1 \\2"
   }
   ```

   `"raw command"` is iTerm's **"Always run command"** — it runs even when the
   clicked path does not exist on the laptop. That is required here: the mini's
   `<root>/…` paths are **not** present on the laptop's own disk
   (only under the `/Volumes` mount), so the ordinary `"command"` action would not
   fire. `\1` is the clicked path, `\2` the line number; `open-md-link` maps
   `/Users`→`/Volumes` and opens in Typora.

   **Manual fallback** (if the profile didn't take): iTerm Settings → Profiles →
   [your profile] → Advanced → Semantic History → **Always run command** →
   `$HOME/bin/open-md-link \1 \2`.

A reset that breaks cmd-click is almost always #2 reverting (an iTerm/profile reset),
or a redeploy overwriting the command. Set Semantic History back to `open-md-link`
and reopen iTerm. `open-md-link` is version-controlled here (`open-md-link`, ARCH-155)
and `install-laptop.sh` deploys it to `~/bin/open-md-link`.

## Endpoints

- `GET /open?p=<relative-path>` — resolve under the base, then `open` it.
- `GET /health` — returns `ok`.

Safety: binds to `127.0.0.1` only; refuses paths that escape the Convivy root.

## Install

- **mini**: already installed via `~/Library/LaunchAgents/com.convivy.openserver.plist`.
- **laptop**: run `bash <the root's SMB-mounted mirror>/agent-platform/tools/convivy-open/install-laptop.sh`.

## Manage

    launchctl unload ~/Library/LaunchAgents/com.convivy.openserver.plist   # stop
    launchctl load   ~/Library/LaunchAgents/com.convivy.openserver.plist   # start
    tail -f /tmp/convivy-open-server.log                                   # logs
