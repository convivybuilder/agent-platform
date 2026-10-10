"""What a Claude Code session in a given directory can be told to do.

WHY THIS EXISTS. Deck's composer accepts a slash command, and until now the
only way to find out whether one had loaded was to type it and see. A command
that silently is not there looks exactly like a command that is there, right up
to the moment it does nothing — so the reader guesses. This lists what is
actually on disk for a session, which is the same thing Claude Code reads.

WHAT IT DOES NOT DO. It does not ask the running session, scrape its screen, or
type into it. Claude Code loads these from files at startup; reading the same
files answers the question without touching the session at all. The cost of
that choice is honest and worth stating: a file added after the session started
appears here before the session has actually loaded it.

Nor does it list Claude Code's built-in commands (`/help`, `/clear`). Those
cannot fail to load, so they are not what anybody is checking.

WHAT COUNTS AS INVOCABLE, measured on the mini 2026-08-13 rather than assumed.
Two shapes, two scopes:

  * a command is a markdown file — `commands/write-better.md` is `/write-better`
  * a skill is a directory holding SKILL.md — `skills/handoff-leave/` is
    `/handoff-leave`, and its frontmatter carries a description worth showing

and each comes from either the user's `~/.claude` or the project's
`<cwd>/.claude`. In practice the weight is entirely on skills: across
agent-platform, vivy, sous-chef and knowledge there were 19 to 24 skills each
and zero project commands, with two user commands total. A version of this that
only read `commands/` would have reported almost nothing and looked broken.
"""
from __future__ import annotations

import os
from pathlib import Path

import builder_root

# A GLOB MATCH IS A NAME, AND A NAME CAN BE A SYMLINK — the sentence
# `tmux_transcript.contained_transcript` already carries. `rglob("*.md")`
# matches a symlink, `is_file()` is true through it, and `read_text()` returns
# the TARGET's bytes. A project from somewhere the operator does not fully control could
# ship `.claude/commands/note.md` pointing at `~/.ssh/id_rsa`, and the menu
# would read it and send any `description:`-shaped line back over ssh. Found by
# security-reviewer.
#
# BUT DROPPING SUCH AN ENTRY WAS THE WRONG FIX, and the operator's own machine is the
# proof: both of his user-wide commands are symlinks into
# `<root>/write-better/`, so the first version of this containment check
# removed the only two commands he has from the menu. The reviewers verified
# the hostile case thoroughly and neither of us checked the legitimate one
# against real data.
#
# What leaks is the target's CONTENT, not the fact of the name. Claude Code
# follows the symlink and loads the command, so the menu reporting it is simply
# true — and the name is the answer to "did my command load", which the
# description only decorates. So an out-of-tree match is LISTED WITHOUT BEING
# READ: no bytes from the target leave the mini, and the menu stops lying about
# what the session can do.
#
# KNOWN AND ACCEPTED, as `contained_transcript` records: this canonicalises a
# name rather than holding a descriptor, so a file swapped between the check and
# the read would be followed. The window is sub-millisecond and the attacker
# already needs write access to that directory.
#
# TWO SECOND TRUSTED ROOTS, FOR SKILLS ONLY: a Builder install places every
# skill as a link OUT of `base` on purpose, and two different Shipwright
# installers do the linking. `bin/install-payload` resolves
# `~/.claude/skills/new-crew` to `~/.local/deploy/builder-payload/skills/new-crew`
# (see `_builder_deploy_skills_root`); `bin/link-commands` separately resolves
# skills such as `write-better` and `talk-better` to
# `~/.local/deploy/write-better/skills/<name>` (see
# `_write_better_deploy_skills_root`). Either left unadmitted means the check
# above alone leaves that installer's skills on such a machine "listed without
# being read", the same false-MISSING failure the reviewers found for the operator's
# symlinked commands, just triggered by an install layout instead of a
# personal one. Both targets are trustworthy for the same reason `base` is:
# nothing but this platform's own installers writes there, so admitting them
# is not the blanket loosening dropping the containment check outright would
# be — a link resolving anywhere else still reports "listed without being
# read", exactly as before this change.
def _readable(path: Path, base: Path, *extra_roots: Path) -> bool:
    """Whether this module is willing to open `path` and report what is in it.

    A name found under `base` is always worth listing. Only its CONTENT is
    gated, and only on where the name really points: inside `base`, or inside
    one of `extra_roots` (passed only for a skill, never a command — see
    `_skills_in`).
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in (base, *extra_roots):
        try:
            if resolved.is_relative_to(root.resolve()):
                return True
        except OSError:
            continue
    return False


def _builder_deploy_skills_root() -> Path:
    """One of two places outside `base` a SKILL's content may also be read
    from: `<account home>/.local/deploy/builder-payload/skills`, the area
    `bin/install-payload` (convivy/shipwright) populates. The other is
    `_write_better_deploy_skills_root`, populated by a different installer in
    the same repo.

    THE NAME AND LAYOUT ARE SHIPWRIGHT'S, NOT GUESSED. `bin/install-payload`
    stages a skill's real content at `<deploy>/skills/<name>` and links
    `~/.claude/skills/<name>` to it — the same `~/.local/deploy/<name>/` shape
    this repo's own `install-agent-platform-shared` and
    `install-run-register-api` use for their own payloads, with
    `builder-payload` as Shipwright's name for its own and `skills` as the one
    subdirectory it actually populates: nothing else `install-payload` links a
    skill from, so a narrower root — this one, not the whole
    `~/.local/deploy/` shared by every installer, and not even the whole
    `builder-payload` payload — is enough to read every skill install-payload
    places without trusting more than that.

    RESOLVED FROM THE PASSWD HOME, NEVER `$HOME` OR AN ENVIRONMENT VARIABLE.
    Every other trust anchor in this family reads `builder_root.
    real_account_home()` rather than `$HOME` for the same reason
    (company/decisions/0008): a non-interactive invocation carries whatever
    `$HOME` its caller set. `install-payload` itself accepts
    `BUILDER_DEPLOY_DIR` to redirect where IT writes, for verifying a branch
    before it ships — an operator override this containment check must not
    follow, since anything able to set that variable for session-agent's own
    process could otherwise widen what gets read.
    """
    return (builder_root.real_account_home() / ".local" / "deploy"
            / "builder-payload" / "skills")


def _write_better_deploy_skills_root() -> Path:
    """The other place outside `base` a SKILL's content may also be read
    from: `<account home>/.local/deploy/write-better/skills`, the area
    `bin/link-commands` (convivy/shipwright) populates for skills such as
    `write-better` and `talk-better`.

    THE NAME AND LAYOUT ARE SHIPWRIGHT'S, NOT GUESSED. `bin/link-commands`
    resolves a skill's source with `skill_source_for(name, deploy_root)`,
    defined as `deploy_root / SKILLS_REPOSITORY / "skills" / name` where
    `SKILLS_REPOSITORY = "write-better"`, and links `~/.claude/skills/<name>`
    to it. `write-better/skills` is the one subdirectory that installer
    populates, so — as with `_builder_deploy_skills_root` — a root that narrow
    is enough to read every skill it places without trusting more than that.

    RESOLVED FROM THE PASSWD HOME, NEVER `$HOME` OR AN ENVIRONMENT VARIABLE,
    for the same reason given in `_builder_deploy_skills_root`.
    """
    return (builder_root.real_account_home() / ".local" / "deploy"
            / "write-better" / "skills")


# A CAP ON THE PER-FILE WORK, which is what it actually bounds and what the
# earlier comment here overstated. The walk itself is not bounded: `sorted()`
# materialises every match before the slice can take any, so a huge tree is
# still fully enumerated — measured by the reviewer at ~1.6s for 300k files,
# which is slow rather than hanging. What the cap prevents is opening, reading
# and YAML-parsing all of them. The real ceiling on this machine is 24 skills.
_MAX_ENTRIES = 500

# Long enough for every description shipping today, and the cut is on a word
# boundary so the next one over is elided rather than mangled.
_MAX_DESCRIPTION = 400

# A command in a subdirectory is namespaced by it — `commands/kb/audit.md` is
# `/kb:audit` — so the name is the path below `commands/`, not the basename.
_NAMESPACE_SEPARATOR = ":"


def _frontmatter(text: str) -> dict:
    """The leading `---` YAML block as a mapping, or {}.

    A REAL YAML PARSE, and the hand-rolled reader this replaces is why. It
    handled `key: value` with indented continuations and claimed to return
    nothing for shapes it did not recognise. It did not: YAML's double-quoted
    folded scalar — which three of this repo's own skills use, because their
    descriptions contain an em dash or an embedded quote — came back as
    garbled text carrying literal backslashes and unresolved `\u2014` escapes.
    Garbled text in a menu is worse than a missing description. Found by
    reviewer, against the real files rather than in the abstract.

    The argument against a dependency was wrong on the facts: `bin/knowledge-ctx`
    already imports yaml, and every interpreter on the mini has it. The import
    is guarded and lazy anyway, because `session-agent state` is what Deck reads
    on every poll and a missing module must not take that down for the sake of
    a menu.
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:  # noqa: BLE001 - malformed frontmatter is not fatal
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _text_field(front: dict, field: str) -> str | None:
    """A frontmatter value as a string fit to hand a renderer.

    SANITISED, like every other disk-derived string this platform sends
    somewhere it will be displayed. An untrusted project's own SKILL.md can put
    any bytes in `description`, including terminal escapes, and this module was
    the one place in the family piping such text at a UI without the pass the
    rest use. Found by security-reviewer.

    TRUNCATED ON A WORD BOUNDARY, because the cap is not hypothetical. An
    earlier version capped at 300 and called that generous; the reviewer found
    `endpoint-family-defense-coverage`, shipping in two repos today, whose
    345-character description was cut mid-word inside "router". A menu item
    ending in "…a rout…" reads as corruption rather than as elision, and the
    next long description is somebody writing a good one.
    """
    value = front.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        import signal_client
        cleaned = signal_client.sanitise(value)
    except ImportError:
        # Better a collapsed line than no description; the escape stripping is
        # the part that matters and a menu is not a shell.
        cleaned = " ".join(value.split())
    return _ellipsised(cleaned, _MAX_DESCRIPTION) or None


def _ellipsised(text: str, limit: int) -> str:
    """`text` cut at the last word boundary that fits, with an ellipsis.

    Falls back to a hard cut for text with no space in range, which a
    description in a language this does not space-separate would be.
    """
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit // 2 else head).rstrip(" ,;:—-") + "…"


def _commands_in(root: Path, scope: str) -> list[dict]:
    """Every `*.md` under `root/commands`, named the way Claude Code names it."""
    base = root / "commands"
    if not base.is_dir():
        return []
    out = []
    try:
        matches = sorted(base.rglob("*.md"))
    except OSError:
        return []
    for path in matches[:_MAX_ENTRIES]:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        relative = path.relative_to(base).with_suffix("")
        name = _NAMESPACE_SEPARATOR.join(relative.parts)
        readable = _readable(path, base)
        front = {}
        if readable:
            try:
                front = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                front = {}
        out.append({
            "name": name,
            "kind": "command",
            "scope": scope,
            "description": _text_field(front, "description"),
            "path": str(path),
            # Present only when the content was deliberately not read, so a
            # reader wondering why this one row has no description gets an
            # answer instead of another silent gap.
            **({} if readable else {"content_unread": True}),
        })
    return out


def _skills_in(root: Path, scope: str) -> list[dict]:
    """Every directory under `skills/` holding a SKILL.md.

    The directory name is the invocation, and the frontmatter's `name` is
    checked against it: a skill whose frontmatter disagrees with its directory
    is a real misconfiguration, and reporting the directory — which is what
    gets invoked — is more useful than reporting what the file wishes it were.
    """
    base = root / "skills"
    if not base.is_dir():
        return []
    out = []
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    for entry in children[:_MAX_ENTRIES]:
        manifest = entry / "SKILL.md"
        # A DIRECTORY WITH NO SEARCH PERMISSION RAISES HERE, not at the read —
        # `is_file()` needs to stat through the parent. The unreadable-FILE case
        # was handled and tested and this one was not, so a single chmod'd skill
        # directory took the whole listing down. Found by reviewer.
        try:
            if not manifest.is_file():
                continue
        except OSError:
            continue
        readable = _readable(manifest, base, _builder_deploy_skills_root(),
                              _write_better_deploy_skills_root())
        front = {}
        if readable:
            try:
                front = _frontmatter(manifest.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                front = {}
        declared = _text_field(front, "name")
        out.append({
            "name": entry.name,
            "kind": "skill",
            "scope": scope,
            "description": _text_field(front, "description"),
            "path": str(manifest),
            # Only present when it disagrees, so a consumer can surface the
            # mismatch without every healthy row carrying a null field.
            **({"declared_name": declared}
               if declared and declared != entry.name else {}),
            **({} if readable else {"content_unread": True}),
        })
    return out


def loaded_commands(cwd: str | None, home: str | None = None) -> dict:
    """Everything invocable by name in a session working in `cwd`.

    `cwd` may be None — a session whose directory could not be read still has
    the user's own commands, and reporting those beats reporting nothing.
    Sources are listed so a consumer can say WHERE it looked, which is the
    difference between "you have no project commands" and "I could not tell".
    """
    home_dir = Path(home if home is not None else os.path.expanduser("~"))
    roots: list[tuple[Path, str]] = [(home_dir / ".claude", "user")]
    if cwd:
        roots.append((Path(cwd) / ".claude", "project"))

    entries: list[dict] = []
    sources = []
    for root, scope in roots:
        sources.append({"scope": scope, "path": str(root), "exists": root.is_dir()})
        entries.extend(_commands_in(root, scope))
        entries.extend(_skills_in(root, scope))

    # PROJECT BEFORE USER, and by name within each. A reader scanning for
    # whether THIS repo's commands loaded should not have to look past a
    # machine-wide list to find them.
    entries.sort(key=lambda e: (e["scope"] != "project", e["name"]))
    return {"cwd": cwd, "sources": sources, "commands": entries}
