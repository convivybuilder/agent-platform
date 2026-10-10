"""Tests for bin/knowledge-watcher on a fresh account (issue_01M3J7Q5ZFCWZMD9FWSNVKPKDH).

The bug: `LOG = Path.home() / ".knowledge-index" / "watcher.log"` fed straight into
`logging.basicConfig` at import time, so a fresh account with no ~/.knowledge-index
crashed with `FileNotFoundError` before the daemon did anything at all.

The script is loaded fresh per test rather than through conftest's session-scoped
`_load_cli` — its log setup runs at import time from `$HOME`, so a cached copy
would carry over whichever HOME the first test set.

`_bootstrap()` is the seam: schema + full scan + the missing-checkout guard,
split out of `main()` so a test can drive startup without also starting
`Observer`'s real watch loop (an actual filesystem watch with no end condition).
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import logging
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

DOC = """---
id: {project}/references/{slug}
kind: reference
project: {project}
title: A doc
status: current
---

Body.
"""


@contextlib.contextmanager
def _root_logger_cleared():
    """pytest's own log-capture plugin re-attaches a handler to the root logger
    for the whole "call" phase of every test — after any fixture's own setup
    runs, so clearing it in a fixture is undone before the test body starts.
    `logging.basicConfig` no-ops once the root logger already has a handler,
    so without this the script's own FileHandler for `LOG` would never
    actually be created and the file would never get written. Wrap the
    module load + `_bootstrap()` call in this, then let pytest's handler back
    in so `caplog` keeps working for every other test.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        yield
    finally:
        for h in root.handlers:
            h.close()
        root.handlers[:] = saved


def _stub_watchdog():
    """`watchdog` is a runtime dep (requirements.txt) the test venv does not
    install (requirements-dev.txt) — same reason `mcp-kb-server`'s fixture
    stubs `mcp.server.fastmcp` in conftest.py. Only `FileSystemEventHandler`
    (a base class) and `Observer` (never called by `_bootstrap`) are used at
    module scope, so a minimal stand-in is enough to import the script.
    """
    if "watchdog.events" in sys.modules and "watchdog.observers" in sys.modules:
        return
    events = types.ModuleType("watchdog.events")
    observers = types.ModuleType("watchdog.observers")

    class FileSystemEventHandler:
        pass

    class Observer:
        def schedule(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, *a, **k):
            pass

    events.FileSystemEventHandler = FileSystemEventHandler
    observers.Observer = Observer
    sys.modules.setdefault("watchdog", types.ModuleType("watchdog"))
    sys.modules["watchdog.events"] = events
    sys.modules["watchdog.observers"] = observers


def _load_knowledge_watcher():
    """A fresh module object each call — see module docstring for why this
    cannot be the cached, session-scoped `_load_cli` conftest uses for other
    hyphenated `bin/` CLIs."""
    _stub_watchdog()
    path = REPO / "bin" / "knowledge-watcher"
    loader = importlib.machinery.SourceFileLoader("knowledge_watcher_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_a_fresh_home_gets_past_logging_setup_and_writes_the_log(tmp_path, monkeypatch):
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        log_dir = fake_home / ".knowledge-index"
        assert log_dir.is_dir(), \
            "the log directory must exist by the time the module has imported"

        code = mod._bootstrap()

    assert code is None, "a real checkout bootstraps cleanly and falls through to the watch loop"
    log_path = log_dir / "watcher.log"
    assert log_path.is_file()
    assert "full scan" in log_path.read_text()


def test_a_missing_knowledge_checkout_exits_with_a_message_naming_the_path(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    missing = tmp_path / "kb-root" / "knowledge"  # deliberately never created

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()
        monkeypatch.setattr(mod, "KNOWLEDGE", missing)

        code = mod._bootstrap()

    assert code == 1
    assert str(missing) in capsys.readouterr().err

    log_path = fake_home / ".knowledge-index" / "watcher.log"
    assert log_path.is_file(), "logging setup itself must still have succeeded"


def test_a_document_in_a_newly_created_subdirectory_is_indexed_from_the_directorys_own_event(
    tmp_path, monkeypatch
):
    """issue_01M3V1DEPGD1HQX2C2V2G0DEF8: the observer can drop the file-created
    event for a document that lands in a directory created moments before it, so
    `on_created` must recover by walking the directory when it is handed the
    directory's own create event -- a nested document (a level deeper than the
    new directory itself) must be picked up too.
    """
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        new_dir = kb_root / "projects" / "adopter-app" / "field-notes"
        new_dir.mkdir(parents=True)
        (new_dir / "doc-one.md").write_text(DOC.format(project="adopter-app", slug="doc-one"))
        nested = new_dir / "nested"
        nested.mkdir()
        (nested / "doc-two.md").write_text(DOC.format(project="adopter-app", slug="doc-two"))

        event = types.SimpleNamespace(is_directory=True, src_path=str(new_dir))
        mod.Handler().on_created(event)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'"
            ).fetchall()}
        finally:
            conn.close()

    assert "projects/adopter-app/field-notes/doc-one.md" in refs
    assert "projects/adopter-app/field-notes/nested/doc-two.md" in refs


def test_a_directory_event_reads_the_roster_once_so_every_file_in_it_gets_one_answer(
        tmp_path, monkeypatch):
    """`index_new_directory` takes one roster read for the whole event. The first read
    succeeds and then the cache bound passes, so a read per file would fail on the second
    file and defer it. One read admits both."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        new_dir = kb_root / "projects" / "platform" / "reference" / "fresh"
        new_dir.mkdir(parents=True)
        (new_dir / "alpha.md").write_text(MOVE_DOC.format(body="One."))
        (new_dir / "beta.md").write_text(MOVE_DOC.replace("alpha", "beta").format(body="Two."))

        now = [0.0]
        reads = []

        def fetch():
            reads.append(1)
            if len(reads) == 1:
                now[0] += knowledge.ROSTER_TTL_S + 1
                return frozenset({"architect"})
            raise knowledge.RosterUnavailable("connection refused")

        monkeypatch.setattr(knowledge, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(knowledge, "_fetch_roster", fetch)
        monkeypatch.setattr(knowledge, "_roster_cache", None)
        indexer.ensure_db()

        mod.index_new_directory(new_dir)

        assert len(reads) == 1, reads
        assert _file_refs(indexer) == [
            "projects/platform/reference/fresh/alpha.md",
            "projects/platform/reference/fresh/beta.md",
        ]


def test_a_directory_moved_into_the_tree_is_indexed_from_its_own_moved_event(
    tmp_path, monkeypatch
):
    """The same gap as the create case: a directory moved into the watched tree
    arrives with its files already inside, so `on_moved` must walk it too when
    the event names a directory rather than a file.
    """
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        # Built outside KNOWLEDGE, as a move-in would land it: already full of
        # docs by the time the directory's own moved event arrives.
        staged = tmp_path / "staged" / "field-notes"
        staged.mkdir(parents=True)
        (staged / "doc-one.md").write_text(DOC.format(project="adopter-app", slug="doc-one"))

        dest = kb_root / "projects" / "adopter-app" / "field-notes"
        dest.parent.mkdir(parents=True)
        staged.rename(dest)

        event = types.SimpleNamespace(
            is_directory=True, src_path=str(staged), dest_path=str(dest)
        )
        mod.Handler().on_moved(event)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'"
            ).fetchall()}
        finally:
            conn.close()

    assert "projects/adopter-app/field-notes/doc-one.md" in refs


def test_a_directory_event_under_dot_git_is_a_no_op(tmp_path, monkeypatch):
    """The watched tree includes the knowledge checkout's own `.git`, so
    routine git housekeeping (creating `.git/objects/xx`, moving refs around)
    fires a directory-created/moved event under `.git`. That must never walk:
    reviewer (run-f656afba)."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        calls = []
        monkeypatch.setattr(mod, "index_new_directory", lambda d: calls.append(d))

        git_subdir = kb_root / ".git" / "objects" / "ab"
        git_subdir.mkdir(parents=True)
        created = types.SimpleNamespace(is_directory=True, src_path=str(git_subdir))
        mod.Handler().on_created(created)

        moved = types.SimpleNamespace(
            is_directory=True,
            src_path=str(kb_root / ".git" / "objects" / "tmp-stage"),
            dest_path=str(git_subdir / "renamed"),
        )
        mod.Handler().on_moved(moved)

    assert calls == [], "a directory event under .git must never trigger a walk"


def test_a_doc_in_a_projects_own_nested_git_directory_is_indexed_like_the_other_two_paths(
        tmp_path, monkeypatch):
    """A newly created directory can itself contain a nested `.git` (a
    vendored repo under a project) — unlike the knowledge checkout's OWN
    top-level `.git` (the no-op guard above), a document inside a NESTED
    `.git` is indexed by a file event and by `full_scan_and_reconcile`'s
    plain `rglob`, since neither of those prunes by name at all. An earlier
    version of this walk pruned every `.git`-named directory at any depth,
    not only the top-level one, so such a document landed in exactly the
    window this PR exists to close whenever it arrived through its
    directory's own create/move event, while the other two paths indexed it
    fine — reviewer (PR #737 round 4, run-93318ce1). The walk must agree
    with the other two: prune only the knowledge checkout's own top-level
    `.git`."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        new_dir = kb_root / "projects" / "adopter-app" / "field-notes"
        new_dir.mkdir(parents=True)
        (new_dir / "doc-one.md").write_text(DOC.format(project="adopter-app", slug="doc-one"))
        dotdir = new_dir / ".git"
        dotdir.mkdir()
        (dotdir / "doc-two.md").write_text(DOC.format(project="adopter-app", slug="doc-two"))

        event = types.SimpleNamespace(is_directory=True, src_path=str(new_dir))
        mod.Handler().on_created(event)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'"
            ).fetchall()}
        finally:
            conn.close()

    assert "projects/adopter-app/field-notes/doc-one.md" in refs
    assert "projects/adopter-app/field-notes/.git/doc-two.md" in refs, \
        "a document inside a project's own nested .git must be indexed, matching " \
        "a file event and full_scan_and_reconcile's plain rglob"


def test_a_non_git_dot_directory_inside_a_new_directory_is_indexed(tmp_path, monkeypatch):
    """The walk prunes only `.git`, not every dot-prefixed name, so a doc
    under a non-`.git` dot-directory (an editor's `.obsidian`) is indexed by
    this walk the same way `full_scan_and_reconcile`'s plain `rglob` and a
    file event's `handle_path` already index it — a document's indexed state
    must not depend on which of the three paths reaches it first: reviewer
    (run-f6a202ce)."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        new_dir = kb_root / "projects" / "adopter-app" / "field-notes"
        new_dir.mkdir(parents=True)
        dotdir = new_dir / ".obsidian"
        dotdir.mkdir()
        (dotdir / "doc-one.md").write_text(DOC.format(project="adopter-app", slug="doc-one"))

        event = types.SimpleNamespace(is_directory=True, src_path=str(new_dir))
        mod.Handler().on_created(event)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'"
            ).fetchall()}
        finally:
            conn.close()

    assert "projects/adopter-app/field-notes/.obsidian/doc-one.md" in refs


def test_a_subdirectory_swapped_for_a_symlink_mid_walk_yields_nothing_from_outside(
        tmp_path, monkeypatch):
    """security-reviewer (PR #737 round 5): the walk's old `is_dir()` then
    `iterdir()` pattern checked a subdirectory's type and then re-resolved
    its path string to recurse, two separate steps a writer could land a
    symlink swap between — a writer that wins that race gets this walk to
    LIST and YIELD paths from a directory tree outside the knowledge root,
    which is unbounded (can stall the single-threaded watchdog dispatcher on
    a large external tree) and names external filenames, even though
    `_open_doc_file` independently refuses to ever READ any of them (its own
    ancestor-swap protection, round 4, doesn't depend on how this walk
    reached the path). So this test exercises `_walk_pruning_git_dir`
    directly, the one layer the fix changes, rather than the full
    create-event-to-index-row pipeline, which would pass even on the
    unfixed walk purely on `_open_doc_file`'s unrelated protection.

    Monkeypatch `_lstat_for_walk` — the walk's own classification call, and
    the only thing a test outside a real race can hook — to perform exactly
    that swap as a side effect: call the real lstat (while the subdirectory
    is still genuine) to get the classification the walk would have seen,
    THEN replace the subdirectory with a symlink to a directory outside the
    knowledge root, THEN return the pre-swap result, landing the race in the
    one window `_walk_fd`'s subsequent `O_NOFOLLOW` open exists to close."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "secret.md").write_text(DOC.format(project="adopter-app", slug="secret"))

        new_dir = kb_root / "projects" / "adopter-app" / "field-notes"
        new_dir.mkdir(parents=True)
        (new_dir / "doc-one.md").write_text(DOC.format(project="adopter-app", slug="doc-one"))
        nested = new_dir / "nested"
        nested.mkdir()
        (nested / "doc-two.md").write_text(DOC.format(project="adopter-app", slug="doc-two"))

        swapped = {"done": False}
        real_lstat_for_walk = mod._lstat_for_walk

        def _swap_nested_for_a_symlink_then_report_its_old_stat(name, dir_fd):
            st = real_lstat_for_walk(name, dir_fd)
            if name == "nested" and not swapped["done"]:
                swapped["done"] = True
                shutil.rmtree(nested)
                nested.symlink_to(outside)
            return st

        monkeypatch.setattr(mod, "_lstat_for_walk",
                            _swap_nested_for_a_symlink_then_report_its_old_stat)

        yielded = {str(p.relative_to(new_dir)) for p in
                  mod._walk_pruning_git_dir(new_dir, kb_root)}

        assert swapped["done"], "the race must actually land for this test to prove anything"
        assert nested.is_symlink(), "the swap itself must have taken"

    assert "doc-one.md" in yielded, \
        "the sibling file outside the swapped subdirectory must still be yielded normally"
    assert "nested" in yielded, \
        "the swapped name itself is still yielded as a path, same as any symlinked directory"
    assert not any("secret" in p for p in yielded), \
        f"nothing from outside the knowledge root may be listed or yielded: {yielded}"
    assert "nested/doc-two.md" not in yielded, \
        "the pre-swap nested doc is gone (its directory was replaced) and must not reappear"


def test_the_walk_refuses_a_symlinked_file_inside_a_new_directory(tmp_path, monkeypatch):
    """security-reviewer (run-8f2c6d62): a symlink inside a just-created
    directory must not be dereferenced and indexed by this walk."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects").mkdir(parents=True)

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        code = mod._bootstrap()
        assert code is None

        outside = tmp_path / "outside.md"
        outside.write_text(DOC.format(project="adopter-app", slug="evil"))

        new_dir = kb_root / "projects" / "adopter-app" / "field-notes"
        new_dir.mkdir(parents=True)
        (new_dir / "evil.md").symlink_to(outside)

        event = types.SimpleNamespace(is_directory=True, src_path=str(new_dir))
        mod.Handler().on_created(event)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'"
            ).fetchall()}
        finally:
            conn.close()

    assert "projects/adopter-app/field-notes/evil.md" not in refs


def test_an_event_during_a_roster_outage_logs_a_deferral_and_keeps_the_row(
    tmp_path, monkeypatch
):
    """The per-file event path (`handle_path` -> `index_one`): a changed audience
    document during a roster outage is logged as deferred with its row kept, and is
    never logged as invalid."""
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects" / "platform" / "reference").mkdir(parents=True)
    doc = kb_root / "projects" / "platform" / "reference" / "alpha.md"
    rel = "projects/platform/reference/alpha.md"
    audience_doc = ("---\nid: platform/reference/alpha\nkind: reference\nproject: platform\n"
                    "title: A doc\nstatus: current\naudience: [architect]\n"
                    "last_reviewed: 2026-07-24\nauthors: [operator]\n---\n\n{body}\n")

    with _root_logger_cleared():
        mod = _load_knowledge_watcher()

        import indexer
        import knowledge
        monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
        monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")

        doc.write_text(audience_doc.format(body="First."))
        mod.handle_path(doc)

        def unreachable():
            raise knowledge.RosterUnavailable("connection refused")

        monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
        monkeypatch.setattr(knowledge, "_roster_cache", None)
        doc.write_text(audience_doc.format(body="Second."))
        mod.handle_path(doc)

        conn = sqlite3.connect(str(indexer.DB))
        try:
            refs = {row[0] for row in conn.execute(
                "SELECT source_ref FROM docs WHERE source = 'files'").fetchall()}
        finally:
            conn.close()

    log_text = (fake_home / ".knowledge-index" / "watcher.log").read_text()
    assert rel in refs, "the outage must keep the indexed row"
    assert f"deferred {rel}: connection refused (row kept)" in log_text, log_text
    assert "invalid doc" not in log_text, log_text


MOVE_DOC = ("---\nid: platform/reference/alpha\nkind: reference\nproject: platform\n"
            "title: A doc\nstatus: current\naudience: [architect]\n"
            "last_reviewed: 2026-07-24\nauthors: [operator]\n---\n\n{body}\n")
MOVE_ALPHA = "projects/platform/reference/alpha.md"
MOVE_BETA = "projects/platform/reference/beta.md"


def _move_setup(tmp_path, monkeypatch):
    fake_home = tmp_path / "fresh-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    kb_root = tmp_path / "kb-root" / "knowledge"
    (kb_root / "projects" / "platform" / "reference").mkdir(parents=True)
    mod = _load_knowledge_watcher()
    import indexer
    import knowledge
    monkeypatch.setattr(mod, "KNOWLEDGE", kb_root)
    monkeypatch.setattr(indexer, "KNOWLEDGE", kb_root)
    monkeypatch.setattr(indexer, "PROJECTS", kb_root / "projects")
    return mod, indexer, knowledge, kb_root


def _file_refs(indexer) -> list[str]:
    conn = sqlite3.connect(str(indexer.DB))
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT source_ref FROM docs WHERE source = 'files'").fetchall())
    finally:
        conn.close()


def _go_unreadable(monkeypatch, knowledge):
    original = knowledge._fetch_roster

    def unreachable():
        raise knowledge.RosterUnavailable("connection refused")

    monkeypatch.setattr(knowledge, "_fetch_roster", unreachable)
    monkeypatch.setattr(knowledge, "_roster_cache", None)
    return original


def _go_readable(monkeypatch, knowledge, original):
    monkeypatch.setattr(knowledge, "_fetch_roster", original)
    monkeypatch.setattr(knowledge, "_roster_cache", None)


def test_a_rename_during_a_roster_outage_keeps_the_row_until_the_roster_reads(
        tmp_path, monkeypatch):
    """The per-file move path (`Handler.on_moved`). A document renamed while the roster
    is unreadable is deferred under its new path, and the row under its old path stays.
    The first readable event leaves one row, at the new path."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        beta = kb_root / MOVE_BETA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)
        assert _file_refs(indexer) == [MOVE_ALPHA]

        alpha.rename(beta)
        original = _go_unreadable(monkeypatch, knowledge)
        mod.Handler().on_moved(types.SimpleNamespace(
            is_directory=False, src_path=str(alpha), dest_path=str(beta)))
        assert _file_refs(indexer) == [MOVE_ALPHA], "the old path's row stays during the outage"

        _go_readable(monkeypatch, knowledge, original)
        mod.handle_path(beta)
        assert _file_refs(indexer) == [MOVE_BETA], "the first readable event leaves one row"


def test_a_removal_then_a_creation_during_a_roster_outage_keeps_the_row(
        tmp_path, monkeypatch):
    """The same move arriving as two events, a removal and then a creation. The removal
    cannot yet be told from a move, so the event path keeps the row while the roster is
    unreadable, and the creation is deferred. The first readable event leaves one row,
    at the new path."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        beta = kb_root / MOVE_BETA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)
        assert _file_refs(indexer) == [MOVE_ALPHA]

        alpha.rename(beta)
        original = _go_unreadable(monkeypatch, knowledge)
        mod.Handler().on_deleted(types.SimpleNamespace(is_directory=False, src_path=str(alpha)))
        mod.Handler().on_created(types.SimpleNamespace(is_directory=False, src_path=str(beta)))
        assert _file_refs(indexer) == [MOVE_ALPHA], "the removal must not drop the row"

        _go_readable(monkeypatch, knowledge, original)
        mod.Handler().on_created(types.SimpleNamespace(is_directory=False, src_path=str(beta)))
        assert _file_refs(indexer) == [MOVE_BETA], "the first readable event leaves one row"


def test_a_move_to_a_refused_destination_during_a_roster_outage_keeps_the_old_row(
        tmp_path, monkeypatch):
    """A rename whose destination fails a check the roster does not decide (no title).
    The destination's refusal removes only a row under its own path, and the old path is
    not refused, so the old path's row stays until the roster reads. The full scan keeps
    the same row. The first readable scan removes it."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        beta = kb_root / MOVE_BETA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)
        assert _file_refs(indexer) == [MOVE_ALPHA]

        alpha.rename(beta)
        beta.write_text(MOVE_DOC.format(body="First.").replace("title: A doc\n", ""))
        original = _go_unreadable(monkeypatch, knowledge)
        # Positive control: the destination's refusal is not the roster's.
        errs = indexer.document_errors(beta.read_text(), MOVE_BETA)
        assert errs and not any("roster" in e for e in errs), errs

        mod.Handler().on_moved(types.SimpleNamespace(
            is_directory=False, src_path=str(alpha), dest_path=str(beta)))
        assert _file_refs(indexer) == [MOVE_ALPHA], "the old path's row stays during the outage"

        _go_readable(monkeypatch, knowledge, original)
        indexer.full_scan_and_reconcile()
        assert _file_refs(indexer) == [], "the first readable scan removes the old row"


@pytest.mark.parametrize("event", ["on_created", "on_modified", "on_deleted"])
def test_a_create_change_or_removal_on_a_vanished_path_keeps_the_row_during_a_roster_outage(
        tmp_path, monkeypatch, event):
    """`on_created`, `on_modified` and `on_deleted` on a path that has gone decide the row
    alike: nothing refuses the path, so the outage keeps its row."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)
        assert _file_refs(indexer) == [MOVE_ALPHA]

        alpha.unlink()
        _go_unreadable(monkeypatch, knowledge)
        getattr(mod.Handler(), event)(types.SimpleNamespace(
            is_directory=False, src_path=str(alpha)))
        assert _file_refs(indexer) == [MOVE_ALPHA], f"{event} must keep the row during the outage"


@pytest.mark.parametrize("event", ["on_created", "on_modified"])
def test_a_refused_document_at_its_own_path_keeps_its_row_during_a_roster_outage(
        tmp_path, monkeypatch, event):
    """A document edited to fail a check the roster does not decide, under its own id. The
    event logs a refusal, not a deferral or an invalid doc, and the row stays."""
    fake_home = tmp_path / "fresh-home"
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)
        assert _file_refs(indexer) == [MOVE_ALPHA]

        alpha.write_text(MOVE_DOC.format(body="Edit.").replace("status: current", "status: bogus"))
        _go_unreadable(monkeypatch, knowledge)
        getattr(mod.Handler(), event)(types.SimpleNamespace(
            is_directory=False, src_path=str(alpha)))
        assert _file_refs(indexer) == [MOVE_ALPHA], f"{event} must keep the refused row"

    log_text = (fake_home / ".knowledge-index" / "watcher.log").read_text()
    assert f"refused {MOVE_ALPHA}: invalid status: bogus (row kept)" in log_text, log_text
    assert "deferred " not in log_text and "invalid doc" not in log_text, log_text


def test_the_startup_scan_logs_a_refusal_during_an_outage_with_the_same_text_as_an_event(
        tmp_path, monkeypatch):
    """The startup scan and a per-file event print one reason for one refusal: the index's
    own text, with no suffix, under the REFUSED label, and the row kept."""
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        alpha.write_text(MOVE_DOC.format(body="First."))
        mod.handle_path(alpha)

        alpha.write_text(MOVE_DOC.format(body="Edit.").replace("status: current", "status: bogus"))
        _go_unreadable(monkeypatch, knowledge)
        mod.full_scan_and_reconcile()
        assert _file_refs(indexer) == [MOVE_ALPHA], "the startup scan keeps the refused row"

    log_text = (tmp_path / "fresh-home" / ".knowledge-index" / "watcher.log").read_text()
    assert f"refused {MOVE_ALPHA}: invalid status: bogus (row kept)" in log_text, log_text
    assert "not admitted" not in log_text, log_text


def test_a_move_onto_an_indexed_path_refused_under_its_own_id_keeps_both_rows_during_an_outage(
        tmp_path, monkeypatch):
    """The move's destination holds another document's row, and that document is now refused
    under its own id. Its refusal keeps its row, and so does the source's, because its file
    is gone. The first readable scan removes both: the source's file is gone, and the
    destination is refused."""
    other = MOVE_DOC.replace("id: platform/reference/alpha", "id: platform/reference/beta-doc")
    with _root_logger_cleared():
        mod, indexer, knowledge, kb_root = _move_setup(tmp_path, monkeypatch)
        alpha = kb_root / MOVE_ALPHA
        beta = kb_root / MOVE_BETA
        alpha.write_text(MOVE_DOC.format(body="First."))
        beta.write_text(other.format(body="First."))
        mod.handle_path(alpha)
        mod.handle_path(beta)
        assert _file_refs(indexer) == [MOVE_ALPHA, MOVE_BETA]

        alpha.unlink()
        beta.write_text(other.format(body="Second.").replace("status: current", "status: bogus"))
        original = _go_unreadable(monkeypatch, knowledge)
        mod.Handler().on_moved(types.SimpleNamespace(
            is_directory=False, src_path=str(alpha), dest_path=str(beta)))
        assert _file_refs(indexer) == [MOVE_ALPHA, MOVE_BETA], "both rows stay during the outage"

        _go_readable(monkeypatch, knowledge, original)
        indexer.full_scan_and_reconcile()
        assert _file_refs(indexer) == [], "the first readable scan removes both rows"
