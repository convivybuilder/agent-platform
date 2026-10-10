"""session_files — `read-file` and `write-file`, contained at the point of open.

WHY THIS EXISTS. Deck opens any file on the mini today by composing the ssh
command itself: a read is `ssh <target> cat <path>`, a write is
`ssh <target> "cat > <staged> && mv -f <staged> <path>"`. Neither goes through
a Bosun program, and nothing on this side checks anything — Deck's own
containment check (`DeckOpenRef.validatePath`) runs on the LAPTOP, against a
path that is then opened on the MINI. A symlink placed under an allowed root
on the mini, pointing outside it, passes the laptop's string check and
resolves wherever it actually points when the ssh command runs — the laptop
is checking a question the mini answers. This module REPLACES that
unrestricted composition with two contained verbs; it does not add a
capability Deck did not already have. See
`issue_01M0D8V6DTN96VSQXPVJ021D55` for the measurement and the three call
sites this closes, including `WorkingCopyStore.writeBack`, whose stage-then-
rename is an arbitrary-file-write once a parent directory is a symlink.

THE ONE PROPERTY EVERYTHING ELSE SERVES. The check and the open are not two
steps a caller could observe between — there is no verb that validates a path
and hands back a verdict for the caller to act on later, because that verdict
would describe a filesystem state that can change before the caller's own
open runs. Every function below resolves and opens as one operation: symlinks
are refused by the SAME `open(2)` call that opens the next path component,
never by a `stat()` beforehand. `O_NOFOLLOW` and `O_EXCL` are what make a
refusal and an open the same syscall; nothing here stats a path and later
trusts what it saw.

CONTAINMENT WALKS ONE COMPONENT AT A TIME, NOT ONE STRING. A path is admitted
only if it resolves inside the invoking account's home directory or its
Claude scratchpad (`/tmp/claude-<uid>/…`, the shape `transcript-capture`
already observes and excludes by name — see `bin/transcript-capture`'s own
header). Resolving the CANDIDATE path once with a library call and comparing
strings — the approach `DeckOpenRef.validatePath` took — only catches a
symlink that already existed at check time; it says nothing about the
component the actual open() will walk through a moment later, and macOS's
`NSString.standardizingPath` does not even resolve symlinks at all, which is
the whole of what went wrong there. `os.open(dir_fd=…, O_NOFOLLOW)` on each
directory component between the root and the leaf is a single kernel call
that checks and opens the SAME entry — there is no window between "is this a
real directory" and "descend into it" for a symlink to be swapped into. A
single multi-component open (`os.open(dir_fd=root_fd, "a/b/c")`) does NOT
have this property: `O_NOFOLLOW` there guards only the final component, and
the kernel still follows a symlink sitting at an INTERMEDIATE one while
resolving the rest of the string — exactly the gap that let a symlinked
PARENT directory redirect Deck's staged write and its rename both.

FAILS CLOSED, ALWAYS. An unresolvable path, a passwd entry that cannot be
read, a component that is a symlink, a `..` segment, a path outside both
admitted roots — every one of these refuses rather than degrading or
guessing. There is no partial credit here: a path this module cannot fully
account for is a path it does not open.

THE HOME ANCHOR IS THE PASSWD DATABASE, NEVER `$HOME`. `session-agent` runs
over ssh non-interactively, where `$HOME` is whatever the invoking process
set — the same reasoning `session_lifecycle._real_account_home` documents at
length for `--create-dir`'s floor. Duplicated here as a three-line function
rather than imported, so this module carries none of `session_lifecycle`'s
tmux-specific dependencies; the reasoning, not the code, is what has to stay
in sync, and it is written down in exactly one place — read
`session_lifecycle.py` before changing either copy.
"""
from __future__ import annotations

import os
import pwd
import secrets
import stat
from pathlib import Path


class FileAccessError(Exception):
    """A path was refused: outside the admitted roots, unresolvable, or
    otherwise not safe to open. Every refusal here is fail-closed."""


def _real_account_home() -> Path:
    """The account's home directory from the passwd database, not `$HOME`.
    See `session_lifecycle._real_account_home` for the full reasoning."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _scratchpad_root() -> Path:
    """Claude Code's own scratch working-directory root for this account.
    Observed shape: `/tmp/claude-<uid>/<origin-path>-scratchpad-<name>` (see
    `bin/transcript-capture`'s header and `tests/test_transcript_capture.py`).
    Keyed off the same passwd uid as the home anchor, never `$HOME`/`$TMPDIR`,
    for the same reason: neither is trustworthy in a non-interactive ssh
    invocation."""
    return Path(f"/tmp/claude-{os.getuid()}")


def _admitted_roots() -> list[Path]:
    """The roots a path may resolve under: each of `_real_account_home` and
    `_scratchpad_root`, in BOTH the literal form a caller conventionally
    writes and its `os.path.realpath` form — `/tmp` is itself a symlink to
    `/private/tmp` on macOS, so a scratchpad path arriving as
    `/tmp/claude-<uid>/…` (the form Claude Code itself uses) would otherwise
    fail to match a root pre-resolved to `/private/tmp/claude-<uid>`. This is
    not a containment weakening: `/tmp` -> `/private/tmp` is a trusted system
    symlink nobody but the OS installer sets, and whichever variant a path
    matches, the walk in `_open_parent_dir` still opens every component from
    there down with `O_NOFOLLOW` — the variant only decides where the walk
    starts, never what it is allowed to pass through.

    FAILS CLOSED PER ROOT: a root whose passwd entry cannot be read, isn't
    absolute, or resolves to `/` (a misconfigured or empty passwd home) is
    dropped rather than trusted — the last of these matters because every
    path has `/` as a prefix, so trusting it would make the containment
    check vacuous, the same failure mode `session_lifecycle._creatable_root`
    guards against for `CREWS_ROOT`.
    """
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate_fn in (_real_account_home, _scratchpad_root):
        try:
            candidate = candidate_fn()
        except (KeyError, OSError):
            continue
        variants = [candidate]
        try:
            resolved = Path(os.path.realpath(candidate, strict=False))
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and resolved != candidate:
            variants.append(resolved)
        for variant in variants:
            if not variant.is_absolute() or len(variant.parts) <= 1:
                continue
            key = str(variant)
            if key in seen:
                continue
            seen.add(key)
            roots.append(variant)
    return roots


def _admitted_components(raw_path: str) -> tuple[Path, list[str]]:
    """The admitted root `raw_path` is nominally under, and the path
    components between that root and the leaf.

    This never resolves or stats anything — `_open_parent_dir` is the only
    place a symlink is actually checked, and it checks by opening, not by
    looking first. What this function refuses is refusable from the string
    alone: a relative path, a `..` segment (which would let the fd-relative
    walk below climb back out through a directory's own parent link, with no
    symlink involved at all), or a path that, component-by-component, does
    not start with either admitted root.
    """
    if not raw_path or not raw_path.startswith("/"):
        raise FileAccessError(f"path must be absolute: {raw_path!r}")
    parts = [p for p in raw_path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileAccessError(f"path must not contain '..': {raw_path!r}")
    if not parts:
        raise FileAccessError(f"path must name a file: {raw_path!r}")

    roots = _admitted_roots()
    if not roots:
        raise FileAccessError(
            "cannot determine the admitted roots (home, Claude scratchpad)")

    for root in roots:
        root_parts = [p for p in str(root).split("/") if p]
        if len(parts) > len(root_parts) and parts[:len(root_parts)] == root_parts:
            return root, parts[len(root_parts):]
    raise FileAccessError(
        f"path is outside the admitted roots (home, Claude scratchpad): {raw_path!r}")


def _open_parent_dir(root: Path, components: list[str]) -> int:
    """Open the leaf's actual parent directory, walking every component
    between `root` and the leaf ONE OPEN CALL AT A TIME, each with
    `O_NOFOLLOW`, chained by file descriptor rather than by name. See the
    module docstring for why a single multi-component open cannot do this —
    `O_NOFOLLOW` there guards only the final component, not the ones the
    kernel still has to resolve through on the way there.

    The caller owns the returned fd and must close it. Every fd opened along
    the way that is not the one returned is closed before returning, and
    everything opened so far is closed if the walk fails partway through.
    """
    try:
        fd = os.open(str(root), os.O_DIRECTORY | os.O_RDONLY)
    except (OSError, ValueError) as e:
        raise FileAccessError(f"cannot open root {root}: {e}") from e
    try:
        for part in components[:-1]:
            try:
                next_fd = os.open(
                    part, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY, dir_fd=fd)
            except (OSError, ValueError) as e:
                raise FileAccessError(f"cannot open {part!r}: {e}") from e
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(path: str) -> bytes:
    """Read a file's exact bytes, admitting only a path that walks — every
    component, including the leaf — inside the account's home or its Claude
    scratchpad with no symlink anywhere in the chain. `O_NOFOLLOW` on the
    leaf refuses a symlink there too, deliberately: this verb never writes,
    so there is no "replace, don't follow" case to carve out the way there is
    for `write_file`, and refusing is the fail-closed default for a chain
    this module cannot otherwise account for.

    `O_NONBLOCK` on the leaf open keeps a FIFO with no writer from hanging
    this call inside the kernel before the not-a-regular-file check below
    ever runs — without it, `open(2)` on a FIFO blocks until a writer shows
    up, and a caller over ssh has no way to interrupt that. It is a no-op
    for a regular file, which is the only thing this verb ever actually
    returns.
    """
    root, components = _admitted_components(path)
    parent_fd = _open_parent_dir(root, components)
    try:
        leaf = components[-1]
        try:
            fd = os.open(
                leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except (OSError, ValueError) as e:
            raise FileAccessError(f"cannot open {path!r}: {e}") from e
        close_needed = True
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FileAccessError(f"not a regular file: {path!r}")
            f = os.fdopen(fd, "rb")
            close_needed = False  # `f` now owns `fd`
        finally:
            if close_needed:
                os.close(fd)
        with f:
            try:
                return f.read()
            except OSError as e:
                raise FileAccessError(f"cannot read {path!r}: {e}") from e
    finally:
        os.close(parent_fd)


def write_file(path: str, content: bytes) -> None:
    """Write `content` to `path` atomically: stage inside the leaf's real
    parent directory, then rename over the target. `rename(2)` replaces
    whatever sits at the target — a regular file, nothing, or a symlink —
    without ever opening through it, so a symlink AT the leaf is replaced
    rather than followed, matching Deck's existing `mv -f` behaviour, and no
    reader ever observes a half-written file.

    Containment is enforced on the PARENT, walked the same fd-relative way
    `read_file` walks the whole path — a symlinked parent directory would
    otherwise redirect both the staged write and the rename, which is
    exactly how Deck's own write is exposed today (see the module
    docstring). The staged name is random and opened with `O_CREAT|O_EXCL`,
    so even a predicted or pre-existing name at that path — file or symlink
    — is refused rather than written through or created underneath it.
    """
    root, components = _admitted_components(path)
    parent_fd = _open_parent_dir(root, components)
    try:
        leaf = components[-1]
        tmp_name = f".session-agent-write-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(
                tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=parent_fd)
        except (OSError, ValueError) as e:
            raise FileAccessError(f"cannot stage write for {path!r}: {e}") from e

        staged = True
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            staged = False
        except OSError as e:
            raise FileAccessError(f"cannot write {path!r}: {e}") from e
        finally:
            if staged:
                try:
                    os.unlink(tmp_name, dir_fd=parent_fd)
                except OSError:
                    pass
    finally:
        os.close(parent_fd)
