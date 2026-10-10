"""The inbox linker's contract (ARCH-426): unify a project's inboxes by symlink,
and never discard a note doing it.

The refusal is the load-bearing half. A real `inbox/` holding notes is data, and
the failure mode this guards against is silent — replacing the directory with a
link would leave the co reading the right place and the notes gone, with nothing
in the output saying so.
"""
from pathlib import Path

import pytest


@pytest.fixture
def pair(tmp_path):
    """A primary inbox and a second directory whose inbox should point at it."""
    primary = tmp_path / "primary"
    second = tmp_path / "second"
    (primary / "inbox").mkdir(parents=True)
    second.mkdir()
    link = second / "inbox"
    target = Path("../primary/inbox")
    return link, target, primary / "inbox"


def test_an_absent_inbox_is_created_as_a_link(link_project_inboxes, pair):
    link, target, primary_inbox = pair
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert ok and msg.startswith("linked")
    assert link.is_symlink()
    assert link.resolve() == primary_inbox.resolve()


def test_an_empty_inbox_directory_is_replaced(link_project_inboxes, pair):
    """Nothing to lose, so the linker acts rather than asking."""
    link, target, primary_inbox = pair
    link.mkdir()
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert ok and msg.startswith("linked")
    assert link.is_symlink() and link.resolve() == primary_inbox.resolve()


def test_an_inbox_holding_notes_is_REFUSED_and_left_untouched(link_project_inboxes, pair):
    """The contract that matters. Merging is the operator's call; discarding is
    never the linker's."""
    link, target, _ = pair
    link.mkdir()
    (link / "note.md").write_text("a real note")
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert not ok
    assert "BLOCKED" in msg and "merge them into" in msg
    assert link.is_dir() and not link.is_symlink()
    assert (link / "note.md").read_text() == "a real note"


def test_a_handled_subdirectory_counts_as_content(link_project_inboxes, pair):
    """Both svc repos have an `inbox/handled/` with history in it and an empty
    top level, so "empty apart from handled/" is the live shape — and it is
    content, not emptiness."""
    link, target, _ = pair
    link.mkdir()
    (link / "handled").mkdir()
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert not ok and "BLOCKED" in msg
    assert (link / "handled").is_dir()


def test_content_landing_mid_link_blocks_rather_than_crashing(link_project_inboxes,
                                                              pair, monkeypatch):
    """Between the emptiness check and the rmdir, another process can drop a note
    in. rmdir(2) refuses a non-empty directory unconditionally, so nothing is lost
    — but the OSError must become a BLOCKED line rather than ending the run in a
    traceback (security-reviewer, PR #337)."""
    link, target, _ = pair
    link.mkdir()
    real_rmdir = Path.rmdir

    def _racy(self):
        (self / "arrived-late.md").write_text("dropped mid-link")
        return real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", _racy)
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert not ok and "BLOCKED" in msg and "gained content" in msg
    assert link.is_dir() and not link.is_symlink()
    assert (link / "arrived-late.md").read_text() == "dropped mid-link"


def test_a_correct_link_is_left_alone_and_reported_ok(link_project_inboxes, pair):
    """Idempotence: install.sh runs this every time."""
    link, target, _ = pair
    link.symlink_to(target)
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert ok and msg.startswith("ok")


def test_a_link_pointing_elsewhere_is_repointed(link_project_inboxes, pair, tmp_path):
    """Replacing a symlink destroys no content, only a pointer, so this one the
    linker may fix itself."""
    link, target, primary_inbox = pair
    (tmp_path / "elsewhere").mkdir()
    link.symlink_to(Path("../elsewhere"))
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert ok and msg.startswith("repointed")
    assert link.resolve() == primary_inbox.resolve()


def test_a_file_where_the_inbox_belongs_is_refused(link_project_inboxes, pair):
    link, target, _ = pair
    link.write_text("not a directory")
    ok, msg = link_project_inboxes.link_one(link, target, check=False)
    assert not ok and "BLOCKED" in msg
    assert link.read_text() == "not a directory"


@pytest.mark.parametrize("setup, expect", [
    (lambda link: None, "MISSING"),
    (lambda link: link.mkdir(), "MISSING"),
    (lambda link: link.symlink_to(Path("../elsewhere")), "WRONG"),
])
def test_check_mode_reports_drift_and_changes_nothing(link_project_inboxes, pair,
                                                      setup, expect):
    link, target, _ = pair
    setup(link)
    before = link.is_symlink(), link.exists()
    ok, msg = link_project_inboxes.link_one(link, target, check=True)
    assert not ok and expect in msg
    assert (link.is_symlink(), link.exists()) == before


def test_check_mode_passes_a_correct_link(link_project_inboxes, pair):
    link, target, _ = pair
    link.symlink_to(target)
    ok, msg = link_project_inboxes.link_one(link, target, check=True)
    assert ok and msg.startswith("ok")


def test_the_link_survives_the_base_directory_moving(link_project_inboxes, pair,
                                                     tmp_path):
    """ARCH-429 makes the base directory configurable. A relative target means the
    whole tree can move and every link still resolves — which an absolute target
    would not."""
    link, target, _ = pair
    link_project_inboxes.link_one(link, target, check=False)
    moved = tmp_path / "moved-base"
    tmp_path.joinpath("primary").rename(tmp_path / "_tmp_primary")
    moved.mkdir()
    (tmp_path / "_tmp_primary").rename(moved / "primary")
    (tmp_path / "second").rename(moved / "second")
    relinked = moved / "second" / "inbox"
    assert relinked.is_symlink()
    assert relinked.resolve() == (moved / "primary" / "inbox").resolve()
