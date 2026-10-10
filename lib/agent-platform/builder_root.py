"""The one root everything Builder owns on this machine lives under.

company/decisions/0008 (the operator, 2026-09-11): crews, the knowledge base, the
platform's checkouts and the run records all live under one root per machine.
That root is `CREWS_ROOT` in ~/.config/convivy/session-agent.env, read as data on
every call. This module is the single reader; every path the platform builds
under the root is meant to come from `root()`, and a CI check counts the
constructions that still do not (tests/test_builder_root.py).

The logic here is the session agent's own, moved rather than rewritten: it was
`session_lifecycle._creatable_root` and its helpers, which already refused a
root too wide to act as a floor. `session_lifecycle` keeps its names as aliases.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


def real_account_home() -> Path:
    """The account's home from the passwd database, not `$HOME`.

    `Path.home()` reads `$HOME`, which whatever invoked this process carries: an
    ssh session, launchd, cron, or a command line with `HOME=...` prepended. A
    different `$HOME` would read a different config file and move this module's
    idea of what counts as too wide, so every home-derived value here comes from
    the system's own record of the account instead.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def fallback_for(home: Path) -> Path:
    """The compiled-in root for an account whose home is `home`, used when nothing
    is configured: ~/builder (company/decisions/0008), now that every reader goes
    through `root()`. A machine with an existing root is pinned by `CREWS_ROOT`
    and does not reach this; the mini's deployed `builder-root --root`, run under
    `env -i HOME=$HOME`, prints its configured root from the config file."""
    return home / "builder"


def config_for(home: Path) -> Path:
    """The per-machine config file for an account whose home is `home`."""
    return home / ".config" / "convivy" / "session-agent.env"


FALLBACK = fallback_for(real_account_home())

# The per-machine config naming the root, read fresh on every call rather than
# cached or sourced. `session-agent` runs over ssh non-interactively, which does
# not load a login shell's rc files, so an environment variable would silently
# never arrive; a file read at call time has no such gap.
CONFIG = config_for(real_account_home())

# The only key CONFIG may set. Parsed as DATA, never sourced or executed: the
# file is operator-editable, and a bad line in it must never do more than leave
# the setting unset.
KEY = "CREWS_ROOT"


def configured_raw(config: Path | None = None) -> str | None:
    """The raw `CREWS_ROOT` value from `config` (default CONFIG), or None when
    nothing is set.

    An absent file, an absent key, and a line this cannot parse all read the
    same. Any other key in the file is ignored. Surrounding quotes are
    tolerated, for a hand-edited value.
    """
    try:
        text = (CONFIG if config is None else config).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def expanded(raw: str, home: Path | None = None) -> Path:
    """`raw` with a leading `~` expanded against the PASSWD home.

    `Path.expanduser()` resolves `~` from `$HOME` when it is set, which would leave
    one environment-influenced input after `real_account_home` closed the others:
    an ordinary `CREWS_ROOT=~` would resolve against whatever `$HOME` the
    invoking process carried. Only `~` and `~/...` are handled here; `~user` goes
    to `expanduser()`, which reads it from passwd.
    """
    home = real_account_home() if home is None else home
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw).expanduser()


def root(config: Path | None = None, fallback: Path | None = None,
         home: Path | None = None) -> Path:
    """The machine's root: the configured `CREWS_ROOT`, or FALLBACK.

    `config`, `fallback` and `home` default to this module's CONFIG, FALLBACK and
    the passwd home. A caller passes its own only so tests that point those at a
    temporary directory keep working; `session_lifecycle` does exactly that.

    FAILS SAFE, NEVER OPEN. A configured value that is relative, empty,
    unparseable, or names something that is not a directory falls back to
    FALLBACK rather than being trusted or raising. So does a value too WIDE to
    act as a root at all: the filesystem root, the account's home, or any
    ancestor of home, or a symlink resolving to one. Each of those passes every
    other check while making any containment test against it vacuous.

    A NON-HOME ROOT IS ACCEPTED AS GIVEN, AND THAT IS A LIMIT, NOT AN
    OVERSIGHT. `CREWS_ROOT=/tmp`, `/var`, `/Volumes`, or any other wide but
    non-home directory passes every check here: it is not the filesystem root,
    not the account's home, and not an ancestor of home. This bounds only the
    degenerate case and the home-relative ones; it does not attempt a list of
    conventionally dangerous roots, because no such list can be complete or stay
    correct across machines. A typo'd `/tmp` meaning `/tmp/crews-scratch` is
    accepted the same as a deliberate one.

    A narrow root that is simply wrong, such as a stale path left by a
    half-finished move, is NOT detected here; nothing in a single path can tell
    it from a right one. How a reader confirms the root before trusting what it
    reads under it is a separate step (company/decisions/0008, "What this does
    not settle").

    Returned unresolved, as the caller wrote it, so a message quoting it shows the
    value actually in force.
    """
    fallback = FALLBACK if fallback is None else fallback
    home = real_account_home() if home is None else home
    raw = configured_raw(config)
    if not raw:
        return fallback
    try:
        candidate = expanded(raw, home)
        if not candidate.is_absolute():
            return fallback
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError is an embedded NUL (Path.resolve() on POSIX); RuntimeError is
        # a symlink loop. Either is a value this machine cannot make sense of.
        return fallback
    if resolved.exists() and not resolved.is_dir():
        return fallback
    home = home.resolve()
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        return fallback
    return candidate


# THE DATA ROOT (company/decisions/0008, amended 2026-09-11). `root()` is a TRUST
# ANCHOR: the session agent's create-dir floor is checked against it, so it reads
# only the passwd home and the machine's config, never the environment. The
# platform's DATA (run records now; the knowledge base and checkouts as they move)
# resolves through `data_root()`, which also honours one explicit variable,
# BUILDER_ROOT. That is narrower than what already redirected those paths: every
# one of them was built from `Path.home()`, which follows `$HOME`. Tests point
# BUILDER_ROOT at a scratch directory where they used to repoint HOME.
#
# NOTHING THAT IS A TRUST ANCHOR MAY READ data_root(). tests/test_builder_root.py
# pins that root() and the create-dir floor ignore BUILDER_ROOT.
DATA_ENV = "BUILDER_ROOT"


def data_root() -> Path:
    """Where the platform keeps its data: BUILDER_ROOT when set and non-empty,
    taken as given, else `root()`.

    The shell launchers resolve through the same rule, as
    `${BUILDER_ROOT:-$(bin/builder-root)}`, and `bin/builder-root` prints this, so
    a launcher and a Python reader cannot disagree about where the data is.
    """
    override = os.environ.get(DATA_ENV, "")
    return Path(override) if override else root()


def data_root_label() -> str:
    """Which root `data_root()` answers from right now, for a report line."""
    return f"data_root() from {DATA_ENV}" if os.environ.get(DATA_ENV, "") else "data_root() from root()"


def runs_root() -> Path:
    """The run-records directory, `<data root>/agent-runs`.

    Every run-record path is built here, so this is where the data root's marker is
    reported (report-only; see `report_unconfirmed`)."""
    data = data_root()
    report_unconfirmed(data_root_label(), data)
    return data / "agent-runs"


# THE ROOT MARKER (company/decisions/0008, "What this does not settle"). The
# installer writes <root>/.builder-root holding the root's own resolved absolute
# path and a newline, nothing else. A tree that was moved or copied carries a
# marker naming its OLD path, and a root nobody installed has none, so comparing
# the marker against the CONFIGURED root, resolved, is what tells the real root
# from a stale one. Format agreed with Shipwright's bin/install-payload.
MARKER = ".builder-root"


def marker_problem(root: Path) -> str | None:
    """Why `root` is not confirmed as this machine's installed root, or None.

    `root` should be what `root()` returned. Its resolved form must be exactly the
    marker's one line. The marker is checked with `is_file()` before it is opened:
    a marker that is a FIFO would otherwise block the read forever, and every
    knowledge read with it.
    """
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return f"{root} cannot be resolved ({exc})"
    marker = resolved / MARKER
    if not marker.is_file():
        return f"{marker} is missing or not a regular file"
    try:
        text = marker.read_text()
    except OSError as exc:
        return f"{marker} is unreadable ({exc})"
    if text != f"{resolved}\n":
        return f"{marker} names {text!r}, not the configured root {str(resolved)!r}"
    return None


# REPORT-ONLY, and one line per root per process (company/decisions/0008). The
# enforcement change will refuse a root its marker does not confirm; before that,
# a day of these lines has to show which processes it would refuse. So the line
# names the root that was checked, how it was chosen and its resolved path, the
# three things a reader of the log needs to tell a test's scratch root from a
# stale root on a real machine.
_REPORTED: set[str] = set()


def report_unconfirmed(which: str, root: Path) -> str | None:
    """Why `root`, the root named `which`, is not confirmed by its marker, or None.

    The first problem for each `which` is printed to stderr. It never raises and
    never refuses, because it runs where run-record paths are built, including at
    import time, and a report must not take its caller down.
    """
    try:
        problem = marker_problem(root)
    except Exception as exc:  # noqa: BLE001 - a report must not raise
        problem = f"the marker check failed ({exc!r})"
    if problem and which not in _REPORTED:
        _REPORTED.add(which)
        try:
            shown = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            shown = root
        # The path and the problem are printed as repr(), so a BUILDER_ROOT (or a
        # marker path) holding a newline or an escape sequence cannot forge a
        # second line in the log this report exists to have read
        # (security-reviewer, #551 round 1).
        print(f"builder_root: {which} = {str(shown)!r} not confirmed (report-only): "
              f"{problem!r}", file=sys.stderr)
    return problem

