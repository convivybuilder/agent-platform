"""ssh_config_render — pure rendering of the per-crew ssh host blocks and the
mosh launcher table from the registry's own `crew` and `home_dir` fields
(CQ-603 / E36 slice 6), the ssh/mosh counterpart to `co_style_render.py`
(CQ-590 / E36 slice 5) it ships beside and is read alongside.

`bin/render-ssh-config` is the I/O wrapper, called from install.sh phase 5 on
the crew machine: it reads the registry and the operator's own crew host,
standby host and ssh user (`lib/agent-platform/operator_config.py`),
validates each unit's `crew`, `home_dir` and style id the way
`bin/render-co-style` validates a unit's own `co_style_id`/`co_iterm_guid` —
`projects.load()` runs neither load path through `projects.validate_registry`,
so a malformed value reaches here unchecked — and writes what this module
returns into the generated co-style directory.
`tools/install-crew-ssh-config.sh`, on the LAPTOP, installs it.

A UNIT'S SSH ALIAS IS ITS OWN SESSION NAME — its `tmux_session` field when
present, else its `crew` field — never its registry slug and never bare
`crew` once `tmux_session` is set: mirrors `co_style_render.deck_key_for`,
since both Deck and ssh have to match the crew's live tmux session, not
whatever key the registry happens to file it under (a unit whose own slug
names no live tmux session at all can still have one under its `crew`
field).

A UNIT'S TMUX SESSION NAME IS ITS OWN `tmux_session` FIELD WHEN PRESENT,
ELSE ITS `crew` FIELD. The two usually agree, but not always: a unit's
`crew` is the registry's own name for the dev crew, while the live tmux
session a crew's `co` wrapper attaches is sometimes a different name set up
before this registry field existed, or a second unit sharing that same
`crew` but running a session of its own. The alias follows the session,
not `crew`, so two units sharing a `crew` get two distinct aliases instead
of one shadowing the other, and the alias always names the session it
actually attaches.

THE STANDBY MACHINE gets its own aliases from this module too, named
`<standby_alias>-<session>` (`standby_alias` an operator config key, never
a literal here): a plain-shell alias for the standby machine itself, plus
one block per unit named in `standby_sessions` (another operator config
key, a list of SESSION names — `tmux_session` when a unit sets one, else
`crew` — matched the same way `bin/render-ssh-config` dedupes
`_valid_entries`, so a `standby_sessions` entry can name a unit whose
`crew` runs more than one session) whose session also runs there —
reachable if the primary crew machine is down.
Each such block runs in the STANDBY machine's own root, carried by a second
placeholder, `@STANDBY_ROOT@`, parallel to `@MINI_ROOT@` below but
substituted by the laptop installer from what it reads live over ssh from
the STANDBY machine, not the crew machine.

A UNIT'S WORKING DIRECTORY IS THE RELATIVE PART OF ITS OWN `home_dir` (stored
`<root>/<rest>`) — `rest` alone, never resolved against any particular
machine's root. `@MINI_ROOT@` stays a literal placeholder in every rendered
block, substituted by the laptop installer at install time from what IT
reads live over ssh from the crew machine — never baked in here, since a
value baked in at render time on the crew machine would carry no liveness
check at all when the laptop installs it later, possibly long afterward.
"""
from __future__ import annotations

from typing import Any, Iterator

_ROOT_PREFIX = "<root>/"


def relative_home_dir(unit: dict[str, Any]) -> str | None:
    """The `<rest>` component of this unit's own `home_dir` — only the
    canonical `<root>/<rest>` stored form is read (the legacy pre-2026-09-12
    stored form `projects._root_relative` still accepts is not, since every
    registry has long since moved past it); `None` when `home_dir` is
    missing, not a string, not in that form, or empty after the prefix."""
    raw = unit.get("home_dir")
    if not isinstance(raw, str) or not raw.startswith(_ROOT_PREFIX):
        return None
    rest = raw[len(_ROOT_PREFIX):]
    return rest or None


def sessions(registry: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(slug, unit) for every active unit carrying a `crew` session name —
    the domain filter: lifecycle and "does this unit have a live session at
    all". `bin/render-ssh-config` additionally runs each pair's `crew`,
    `home_dir` and style id through a name/path shape check before calling
    the render functions below; this generator does not check shape itself,
    the same split `co_style_render._styled_units` draws against its own
    wrapper's `_invalid_fields`."""
    for slug, unit in registry.items():
        if not isinstance(slug, str) or slug.startswith("_") or not isinstance(unit, dict):
            continue
        if unit.get("lifecycle") not in (None, "active"):
            continue
        if not unit.get("crew"):
            continue
        yield slug, unit


def host_block(alias: str, *, host: str, style_id: str, relative_dir: str,
                session: str | None = None) -> str:
    """One `Host <alias>` stanza: emit the SetProfile escape for `style_id`,
    then `session-agent ensure-server` before attach-or-create, in
    `@MINI_ROOT@/<relative_dir>` on `host`. `alias` is the laptop-facing
    name — a unit's own session name, module docstring — and also the tmux
    session to attach/create when `session` is omitted, since the caller
    always passes the two in agreement."""
    session = session or alias
    return (
        f"Host {alias}\n"
        f"  HostName {host}\n"
        f"  RequestTTY yes\n"
        f"  RemoteCommand printf '\\033]1337;SetProfile=co-{style_id}\\a'; "
        f"~/.local/bin/session-agent ensure-server && "
        f"(tmux attach -d -t {session} || tmux new -s {session} -c @MINI_ROOT@/{relative_dir})\n"
    )


def render_host_blocks(entries: list[dict[str, str]], *, crew_host: str, ssh_user: str,
                        standby_host: str | None = None, standby_alias: str | None = None,
                        standby_entries: list[dict[str, str]] | None = None) -> str:
    """The full managed ssh config text: a shared `Host` line naming every
    unit's own alias (session name, module docstring) in `entries` plus the
    generic `mini` plain shell (and, when both `standby_alias` and
    `standby_host` are set, `standby_alias` itself plus
    `<standby_alias>-<session>` for every row of `standby_entries`) carrying
    `User`/`IdentityFile`/keepalive settings common to all of them; `Host
    mini` itself; one block per `entries` row, on `crew_host`; and a plain
    shell block named `standby_alias` on `standby_host` when both are set.

    The per-session standby blocks themselves (`<standby_alias>-<session>`)
    are NOT rendered here — see `render_standby_session_blocks` — because they
    run in the standby machine's own root, read live at install time, same
    as the crew blocks run in the crew machine's root; this function only
    reserves their names on the shared `Host` line so the common settings
    apply to them too.

    `entries` and `standby_entries` rows are `{"crew": ..., "dir": ...,
    "style_id": ..., "session": ...}`, already shape-validated and
    de-duplicated by the caller — this function assumes they're safe to
    splice into ssh config text and a remote shell command verbatim.
    Likewise assumes `crew_host` and `ssh_user` are both already known; the
    caller decides whether to call this at all when either is missing,
    rather than this function guessing or rendering a partial config with no
    destination or login name. Never renders anything standby-shaped unless
    BOTH `standby_alias` and `standby_host` are given — a name guessed here
    (e.g. a hardcoded "standby") is exactly what the caller's own operator
    config exists to avoid."""
    names = [e.get("session") or e["crew"] for e in entries]
    shared = ["mini", *names]
    render_standby = bool(standby_alias and standby_host)
    if render_standby:
        shared.append(standby_alias)
        shared.extend(f"{standby_alias}-{e.get('session') or e['crew']}"
                      for e in (standby_entries or []))
    shared.append(crew_host)
    if render_standby:
        shared.append(standby_host)

    lines = [
        "# Managed by agent-platform tools/install-crew-ssh-config.sh —",
        "# do not hand-edit; re-run to update. One tmux session (and iTerm",
        "# window) per crew the project registry names, rendered by",
        "# bin/render-ssh-config on the crew machine (CQ-603). Plain `ssh",
        "# mini` opens a normal shell on the crew machine, with no tmux.",
        "#",
        "# Each crew alias emits its iTerm2 SetProfile escape (OSC 1337) before",
        "# attaching tmux, so the window gets its per-crew styling on every",
        "# (re)connect, and runs `session-agent ensure-server` before attach-or-",
        "# create, so a cold session-agent never forks a tmux server from this",
        "# login's own sshd session.",
        "",
        f"Host {' '.join(shared)}",
        f"  User {ssh_user}",
        "  IdentityFile ~/.ssh/id_ed25519_mini",
        "  IdentitiesOnly yes",
        "  ServerAliveInterval 60",
        "  ServerAliveCountMax 3",
        "",
        "Host mini",
        f"  HostName {crew_host}",
        "",
    ]
    for e in entries:
        lines.append(host_block(e.get("session") or e["crew"], host=crew_host,
                                 style_id=e["style_id"], relative_dir=e["dir"]))
    if render_standby:
        lines.append(f"Host {standby_alias}")
        lines.append(f"  HostName {standby_host}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_standby_session_blocks(standby_alias: str | None, entries: list[dict[str, str]],
                                   standby_host: str | None) -> str:
    """One `Host <standby_alias>-<session>` stanza per row of `entries`, each
    attaching/creating that row's own tmux session in `@STANDBY_ROOT@/<dir>`
    on `standby_host` — the standby machine's own root, substituted by the
    laptop installer the same way `@MINI_ROOT@` is, from a live read of the
    STANDBY machine rather than the crew machine (module docstring). Empty
    when `standby_alias`, `standby_host` or `entries` is falsy — the caller
    decides whether a standby block belongs on the shared `Host` line at
    all; this function renders nothing without all three."""
    if not (standby_alias and standby_host and entries):
        return ""
    lines = []
    for e in entries:
        session = e.get("session") or e["crew"]
        lines.append(
            f"Host {standby_alias}-{session}\n"
            f"  HostName {standby_host}\n"
            f"  RequestTTY yes\n"
            f"  RemoteCommand printf '\\033]1337;SetProfile=co-{e['style_id']}\\a'; "
            f"~/.local/bin/session-agent ensure-server && "
            f"(tmux attach -d -t {session} || tmux new -s {session} -c @STANDBY_ROOT@/{e['dir']})\n"
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def render_mosh_sessions(entries: list[dict[str, str]]) -> str:
    """The pipe-delimited `alias|iterm-profile|tmux-session|workdir` table
    `tools/install-crew-ssh-config.sh` builds its mosh launcher functions
    from. `@MINI_ROOT@` stays a literal placeholder, substituted by the
    laptop installer the same way the ssh host blocks are. The first and
    third fields are both each row's own session name (`session`, falling
    back to `crew`), mirroring `host_block` above — the alias and the tmux
    session it attaches are always the same name."""
    lines = []
    for e in entries:
        alias = e.get("session") or e["crew"]
        lines.append(f"{alias}|co-{e['style_id']}|{alias}|@MINI_ROOT@/{e['dir']}")
    return "".join(f"{line}\n" for line in lines)
