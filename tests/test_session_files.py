"""`read_file`/`write_file` (lib/agent-platform/session_files.py) — the
containment that closes issue_01M0D8V6DTN96VSQXPVJ021D55: a symlink under an
allowed root that resolves outside it, checked on the wrong machine today
(Deck, on the laptop) rather than where the file is actually opened (the
mini). Every test here is either "a legitimate path inside the roots works"
or "something that looks contained but isn't gets refused" — the second
kind is the one that matters.
"""
from __future__ import annotations

import os
import threading
import types
from pathlib import Path

import pytest

import session_files as SF


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Point the two admitted roots at isolated tmp directories, and an
    `outside` directory that is under neither — the target every escape
    attempt in this file tries to reach."""
    home = tmp_path / "home"
    scratch = tmp_path / "scratchpad"
    outside = tmp_path / "outside"
    home.mkdir()
    scratch.mkdir()
    outside.mkdir()
    monkeypatch.setattr(SF, "_real_account_home", lambda: home)
    monkeypatch.setattr(SF, "_scratchpad_root", lambda: scratch)
    return types.SimpleNamespace(home=home, scratch=scratch, outside=outside)


# --- the golden path -----------------------------------------------------


def test_a_path_inside_home_is_written_then_read_back(roots):
    target = roots.home / "sub" / "dir" / "notes.md"
    target.parent.mkdir(parents=True)

    SF.write_file(str(target), b"hello world")

    assert SF.read_file(str(target)) == b"hello world"


def test_a_path_inside_the_scratchpad_is_written_then_read_back(roots):
    target = roots.scratch / "notes.md"

    SF.write_file(str(target), b"scratch content")

    assert SF.read_file(str(target)) == b"scratch content"


def test_a_file_directly_in_home_with_no_subdirectory_works(roots):
    target = roots.home / "notes.md"

    SF.write_file(str(target), b"root level")

    assert SF.read_file(str(target)) == b"root level"


# --- refusals: the shape a bad path can take ------------------------------


def test_a_path_outside_both_roots_is_refused_for_read(roots):
    outside_file = roots.outside / "secret.txt"
    outside_file.write_text("secret")

    with pytest.raises(SF.FileAccessError, match="outside the admitted roots"):
        SF.read_file(str(outside_file))


def test_a_path_outside_both_roots_is_refused_for_write(roots):
    target = roots.outside / "notes.md"

    with pytest.raises(SF.FileAccessError, match="outside the admitted roots"):
        SF.write_file(str(target), b"pwned")

    assert not target.exists()


def test_a_relative_path_is_refused(roots):
    with pytest.raises(SF.FileAccessError, match="must be absolute"):
        SF.read_file("relative/notes.md")


def test_a_dot_dot_segment_is_refused_even_though_it_stays_textually_inside(roots):
    """`home/../etc/passwd` never reaches the walk at all — refused on the
    string alone, before any fd is opened."""
    sneaky = str(roots.home / ".." / "etc" / "passwd")

    with pytest.raises(SF.FileAccessError, match=r"must not contain '\.\.'"):
        SF.read_file(sneaky)


def test_the_filesystem_root_alone_is_refused(roots):
    with pytest.raises(SF.FileAccessError, match="must name a file"):
        SF.read_file("/")


def test_a_root_directory_with_no_leaf_component_is_refused(roots):
    """The admitted root itself is a directory, not a file — asking for it
    by its own path names nothing this verb can open."""
    with pytest.raises(SF.FileAccessError):
        SF.read_file(str(roots.home))


def test_home_env_spoofing_does_not_move_the_boundary(tmp_path, monkeypatch):
    """`$HOME` is not the anchor — the passwd entry is. A caller (or an
    attacker controlling the invoking environment) setting `$HOME` to
    somewhere else must not admit paths under it, and must not lose access
    to the real passwd home either.
    """
    import pwd as real_pwd

    passwd_home = tmp_path / "passwd-home"
    spoofed_home = tmp_path / "spoofed-home"
    passwd_home.mkdir()
    spoofed_home.mkdir()

    fake_entry = types.SimpleNamespace(pw_dir=str(passwd_home))
    monkeypatch.setattr(real_pwd, "getpwuid", lambda uid: fake_entry)
    monkeypatch.setenv("HOME", str(spoofed_home))
    monkeypatch.setattr(SF, "_scratchpad_root", lambda: tmp_path / "no-scratchpad")

    spoofed_target = spoofed_home / "notes.md"
    with pytest.raises(SF.FileAccessError, match="outside the admitted roots"):
        SF.write_file(str(spoofed_target), b"x")
    assert not spoofed_target.exists()

    real_target = passwd_home / "notes.md"
    SF.write_file(str(real_target), b"real")
    assert SF.read_file(str(real_target)) == b"real"


# --- symlinks: the whole point ---------------------------------------------


def test_a_symlink_inside_home_pointing_outside_is_refused_for_read(roots):
    real_file = roots.outside / "secret.txt"
    real_file.write_text("secret")
    link = roots.home / "link.txt"
    link.symlink_to(real_file)

    with pytest.raises(SF.FileAccessError):
        SF.read_file(str(link))


def test_a_symlinked_parent_directory_is_refused_for_write(roots):
    """The defect this closes: Deck's stage-then-rename derives its staging
    directory from the target's PARENT, so a symlinked parent redirects both
    the staged write and the rename — an arbitrary-file-write, not merely a
    read. A symlink two levels up must be caught the same as one directly
    above the leaf.
    """
    linked_parent = roots.home / "linked"
    linked_parent.symlink_to(roots.outside)

    with pytest.raises(SF.FileAccessError):
        SF.write_file(str(linked_parent / "evil.txt"), b"pwned")

    assert list(roots.outside.iterdir()) == []

    nested_target = linked_parent / "sub" / "evil.txt"
    with pytest.raises(SF.FileAccessError):
        SF.write_file(str(nested_target), b"pwned")


def test_a_symlinked_grandparent_directory_is_refused_for_write(roots):
    """Same defect, one level deeper: the symlink is not the immediate
    parent, it is the parent's parent. `outside/sub` is created for real so
    that a walk that wrongly follows the symlink actually reaches and
    traverses the redirected directory instead of failing on a missing
    `sub` component for an unrelated reason — otherwise this test would
    pass even if the intermediate-directory `O_NOFOLLOW` were dropped."""
    linked = roots.home / "linked"
    linked.symlink_to(roots.outside)
    (roots.outside / "sub").mkdir()
    nested = linked / "sub" / "evil.txt"

    with pytest.raises(SF.FileAccessError):
        SF.write_file(str(nested), b"pwned")


def test_a_symlinked_parent_directory_is_refused_for_read(roots):
    real_file = roots.outside / "secret.txt"
    real_file.write_text("secret")
    linked_parent = roots.home / "linked"
    linked_parent.symlink_to(roots.outside)

    with pytest.raises(SF.FileAccessError):
        SF.read_file(str(linked_parent / "secret.txt"))


# --- write: atomicity and the replace-not-follow rule -----------------------


def test_write_is_atomic_no_reader_ever_sees_a_partial_file(roots, monkeypatch):
    """Simulated rather than raced: a write that fails partway through
    (disk full, permission pulled mid-write) must leave either the OLD
    content or nothing — never a half-written file — and must leave no
    staged temp file behind either.
    """
    target = roots.home / "notes.md"
    target.write_text("original")

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(SF.os, "fsync", boom)

    with pytest.raises(SF.FileAccessError, match="cannot write"):
        SF.write_file(str(target), b"new content")

    # The original file is untouched — the failed write never got as far as
    # the rename.
    assert target.read_text() == "original"
    # And no staged temp file was left behind in the directory.
    leftovers = [p for p in roots.home.iterdir() if p.name.startswith(".session-agent-write-")]
    assert leftovers == []


def test_a_symlink_at_the_write_target_is_replaced_not_written_through(roots):
    real_file = roots.outside / "secret.txt"
    real_file.write_text("secret")
    target = roots.home / "target.txt"
    target.symlink_to(real_file)

    SF.write_file(str(target), b"replaced")

    # The file the symlink used to point at is untouched...
    assert real_file.read_text() == "secret"
    # ...and the target is now a plain file holding the new content, not a
    # symlink any more.
    assert not target.is_symlink()
    assert target.read_bytes() == b"replaced"


def test_write_then_read_round_trips_binary_content(roots):
    target = roots.home / "blob.bin"
    payload = bytes(range(256)) * 4

    SF.write_file(str(target), payload)

    assert SF.read_file(str(target)) == payload


# --- read: refuses anything that is not a plain file ------------------------


def test_reading_a_directory_is_refused(roots):
    a_directory = roots.home / "a-directory"
    a_directory.mkdir()

    with pytest.raises(SF.FileAccessError, match="not a regular file"):
        SF.read_file(str(a_directory))


def test_reading_a_path_that_does_not_exist_is_refused(roots):
    with pytest.raises(SF.FileAccessError):
        SF.read_file(str(roots.home / "does-not-exist.txt"))


def test_reading_a_fifo_with_no_writer_refuses_promptly_instead_of_hanging(roots):
    """`open(2)` on a FIFO with no writer blocks inside the kernel until one
    shows up — without `O_NONBLOCK` on the leaf open, this would hang the
    calling process indefinitely rather than refusing, before the
    not-a-regular-file check ever ran. Run the call on a daemon thread with
    a bounded join so a regression here fails this test in a few seconds
    instead of hanging the whole suite."""
    fifo_path = roots.home / "pipe"
    os.mkfifo(fifo_path)

    result = {}

    def call():
        try:
            SF.read_file(str(fifo_path))
        except BaseException as e:  # noqa: BLE001 - capture anything, including a hang-free crash
            result["error"] = e

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "read_file blocked on a FIFO with no writer instead of refusing"
    assert isinstance(result.get("error"), SF.FileAccessError)
    assert "not a regular file" in str(result["error"])


# --- fail-closed on an unresolvable home -----------------------------------


def test_a_root_that_resolves_to_the_filesystem_root_is_never_trusted(monkeypatch):
    """A misconfigured or empty passwd home resolving to `/` must not become
    "everything is admitted" — every path has `/` as a prefix, so trusting
    it as a root makes the containment check vacuous. Reproduced against a
    real file outside `/tmp`/`/etc`/`/var` (all themselves symlinks on
    macOS, which would otherwise make this refuse for the wrong reason)."""
    monkeypatch.setattr(SF, "_real_account_home", lambda: Path("/"))
    monkeypatch.setattr(SF, "_scratchpad_root", lambda: Path("/nonexistent-scratch-root-xyz"))

    real_path = "/private/etc/hosts" if Path("/private/etc/hosts").exists() else None
    if real_path is None:
        pytest.skip("no fixed non-symlinked absolute path available to probe with")

    with pytest.raises(SF.FileAccessError):
        SF.read_file(real_path)


def test_an_unreadable_passwd_entry_refuses_everything(tmp_path, monkeypatch):
    """If the account's home cannot be determined at all, nothing is
    admitted — not "everything", not "just the scratchpad"."""
    def boom():
        raise KeyError("no such uid")

    monkeypatch.setattr(SF, "_real_account_home", boom)
    monkeypatch.setattr(SF, "_scratchpad_root", boom)

    with pytest.raises(SF.FileAccessError, match="cannot determine the admitted roots"):
        SF.read_file("/tmp/anything")
