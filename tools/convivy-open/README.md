# convivy-open

Resolve a Convivy file reference against *this* machine's own copy, from a
terminal or from an iTerm cmd-click. There is no server and no daemon: each
tool below resolves the path locally and acts on it.

- **mini**  → resolves against the mini's own resolved root (company/decisions/0008)
- **laptop** → resolves against that root's SMB-mounted mirror under `/Volumes` (see `resolve-open-base.sh`)

An earlier revision served a clickable `http://localhost:8765/open?p=...` link
through a launchd-managed `convivy-open-server`. Nothing posts that link
anymore, so the server has been retired along with its laptop install step;
`open-md-link` (below) opens a file directly, and the `convivy-open` CLI
needs no server to resolve a path.

## Terminal fallback (`convivy-open` CLI)

Inside an SSH/tmux session, use the CLI on a Convivy-relative or absolute
path — it resolves against *this* machine and acts in the terminal:

    convivy-open <ref>        # print the resolved absolute path (default)
    convivy-open -e <ref>     # open in $EDITOR
    convivy-open -d <ref>     # print its directory
    convivy-open -l <ref>     # page with $PAGER
    convivy-open -o <ref>     # GUI open

`<ref>` can be a relative path or an absolute mini/laptop path — paste
whatever you have.

Optional shell helpers (adds `cve`/`cvd`/`cvl`) — in `~/.zshrc`:

    source ~/.local/bin/convivy-open.zsh

Then `cvd <ref>` cd's you into the file's directory, `cve <ref>` edits it.

## Cmd-click in iTerm (Semantic History)

Cmd-clicking a bare absolute path in an iTerm session runs iTerm's
**Semantic History**, which calls **`~/bin/open-md-link`**. `open-md-link` is
the cmd-click handler — it does the `/Users`→`/Volumes` translation itself,
opens `.md` in Typora, and drives Typora's Find to the clicked line number.
(It is **not** `convivy-open`: `convivy-open` is the terminal CLI;
`open-md-link` is the iTerm click handler. An earlier rev wrongly wired
`convivy-open` into Semantic History — see substrate ARCH-154.)

This is **not** a daemon — it is an iTerm profile setting plus the
`~/bin/open-md-link` script. Two things must be true on the laptop:

1. **`~/bin/open-md-link` exists** on the laptop (and Typora + an Accessibility
   grant for the line-jump). It is self-contained — no env var needed.
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

## Install

- **mini**: runs the scripts in this directory directly; there is no install step.
- **laptop**: run `bash <the root's SMB-mounted mirror>/agent-platform/tools/convivy-open/install-laptop.sh`.
  It installs the `convivy-open` CLI, the `cve`/`cvd`/`cvl` shell helpers, and
  `open-md-link`, which drives the line-jump itself.

A laptop installed before this change can remove the now-unused
`~/Applications/ConvivyTyporaGoto.app` and `~/.local/bin/typora-goto` by hand.
