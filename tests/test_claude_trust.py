"""claude_trust — the read-modify-write into Claude Code's own `~/.claude.json`
that pre-trusts a directory `session_lifecycle.start --create-dir` just made.

THE TEST THAT MATTERS MOST is the one about not touching what it cannot read:
`~/.claude.json` is live config for every Claude Code session on the box, and
a version of this that guessed at a shape it could not parse would risk
clobbering it — worse than the trust prompt this exists to avoid.
"""
from __future__ import annotations

import json
import os
import stat
import time

import claude_trust as CT


def _read(home):
    return json.loads((home / CT.CONFIG_FILENAME).read_text())


# MARK: - No file yet


def test_no_config_file_creates_one_with_the_entry(tmp_path):
    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    data = _read(tmp_path)
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}


# MARK: - An existing file


def test_existing_file_no_entry_for_the_path_adds_it_others_untouched(tmp_path):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({
        "numStartups": 42,
        "projects": {
            "/srv/convivy/other": {"hasTrustDialogAccepted": True,
                                          "lastSessionId": "abc123"},
        },
    }))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    data = _read(tmp_path)
    assert data["numStartups"] == 42
    assert data["projects"]["/srv/convivy/other"] == {
        "hasTrustDialogAccepted": True, "lastSessionId": "abc123"}
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}


def test_existing_entry_with_other_keys_keeps_them(tmp_path):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {
        "/srv/convivy/nyh": {"lastSessionId": "abc123", "allowedTools": ["Bash"]},
    }}))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    entry = _read(tmp_path)["projects"]["/srv/convivy/nyh"]
    assert entry == {"lastSessionId": "abc123", "allowedTools": ["Bash"],
                     "hasTrustDialogAccepted": True}


def test_the_config_files_mode_survives_the_write(tmp_path):
    """`~/.claude.json` is live config for every Claude Code session on the
    box, some of it credential-adjacent (the shared `mcpServers` block). The
    temp file `write_text` creates is fresh, at the umask default, and
    `os.replace` carries that mode across verbatim — so a restrictively
    permissioned file (0600 is common for this kind of config) would
    silently come back more permissive after every successful write unless
    the original mode is explicitly carried across."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    config.chmod(0o600)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_a_fresh_files_mode_is_0600_with_no_prior_file_to_preserve(tmp_path):
    """No prior file means no prior mode to carry across — this is
    credential-adjacent config this process is creating, so it gets 0600
    rather than falling through to whatever the umask happens to allow."""
    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    config = tmp_path / CT.CONFIG_FILENAME
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_the_temp_file_is_created_at_the_target_mode_before_any_write(
        tmp_path, monkeypatch):
    """THE WINDOW THIS CLOSES. The old code created the temp file fresh via
    `write_text` — at the umask default — and only chmodded it down to match
    the real file AFTER the full JSON body, including the shared
    `mcpServers` block, had already landed in it: a restrictively
    permissioned (0600) config's entire contents sat in a world-readable
    temp file for that gap. `os.open` must be given the intended mode
    directly, so the file never exists at a wider mode than its final one at
    any point content is present."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    config.chmod(0o600)

    real_open = os.open
    opened = {}
    tmp_marker = f".tmp.{os.getpid()}"

    def spying_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if str(path).endswith(tmp_marker):
            opened["mode_at_creation"] = stat.S_IMODE(os.fstat(fd).st_mode)
            opened["flags"] = flags
            opened["size_at_creation"] = os.fstat(fd).st_size
        return fd

    monkeypatch.setattr(CT.os, "open", spying_open)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert opened, "the PID-scoped temp path was never opened"
    assert opened["mode_at_creation"] == 0o600
    assert opened["flags"] & os.O_EXCL
    assert opened["size_at_creation"] == 0, \
        "the file must be created empty, at its final mode, before any content is written"


def test_a_stale_temp_file_is_cleaned_up_and_the_write_succeeds(tmp_path):
    """The temp path is scoped to this process's own PID
    (`<config>.tmp.<pid>`), so a leftover found there can only be THIS SAME
    PID's own prior write — a previous invocation that crashed or was
    killed before reaching `os.replace`, whose leftover survived until the
    OS cycled PIDs back around to this one. It is never a live concurrent
    writer (this call already holds `LOCK_EX`) and, because the name is
    PID-scoped rather than the bare shared `.tmp` a peer might also use, it
    is never a peer's in-flight file either. Finding one must not
    permanently disable pre-trust on the box: it's cleared and the write
    retried once, still under the lock, and still created at the target
    mode."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    config.chmod(0o600)
    tmp = tmp_path / f"{CT.CONFIG_FILENAME}.tmp.{os.getpid()}"
    tmp.write_text("stale leftover from this same PID's crashed prior write")

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    data = _read(tmp_path)
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_a_second_stale_temp_file_still_fails_soft(tmp_path, monkeypatch):
    """The self-healing retry is a one-shot: if the PID-scoped temp path is
    occupied again immediately after being cleared, this must still fail
    soft like any other write failure rather than looping or raising."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    tmp_marker = f".tmp.{os.getpid()}"
    tmp = tmp_path / f"{CT.CONFIG_FILENAME}{tmp_marker}"
    tmp.write_text("stale leftover")

    real_open = os.open

    def always_conflicting_open(path, flags, mode=0o777):
        if str(path).endswith(tmp_marker) and (flags & os.O_EXCL):
            raise FileExistsError(17, "File exists")
        return real_open(path, flags, mode)

    monkeypatch.setattr(CT.os, "open", always_conflicting_open)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "could not write" in result["reason"]
    assert _read(tmp_path) == {"projects": {}}


def test_a_bare_named_peer_temp_file_is_left_untouched(tmp_path):
    """THE COLLISION THIS PID-SCOPING CLOSES. The bare `<config>.tmp` name
    is the plain convention Claude Code's own atomic-write implementation
    could plausibly use for the same file, and this module's own docstring
    already concedes it holds no lock a live `co` session participates in.
    Before this process's own temp path was PID-scoped, a file found at
    that bare name was indistinguishable from this process's own stale
    leftover and would have been unlinked by the self-heal below — deleting
    a peer's in-flight write. Scoping this process's own path to its PID
    means it never looks at, let alone unlinks, the bare name at all."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    peer_tmp = tmp_path / f"{CT.CONFIG_FILENAME}.tmp"
    peer_tmp.write_text("a peer's in-flight write, not this process's own")

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert peer_tmp.exists(), "a peer's temp file must never be unlinked"
    assert peer_tmp.read_text() == "a peer's in-flight write, not this process's own"
    data = _read(tmp_path)
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}


# MARK: - Sweeping stale <config>.tmp.<pid> litter


def test_an_aged_tmp_leftover_is_removed(tmp_path):
    """A leftover from a crashed prior invocation, old enough that no write
    under the current lock could still be using it, is cleared on the next
    call — regardless of which PID wrote it."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    stale = tmp_path / f"{CT.CONFIG_FILENAME}.tmp.99999"
    stale.write_text("a full plaintext copy of a prior config, including secrets")
    old = time.time() - CT.STALE_TMP_AGE_SECONDS - 1
    os.utime(stale, (old, old))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert not stale.exists()


def test_a_fresh_tmp_leftover_is_not_removed(tmp_path):
    """A leftover younger than the sweep threshold survives until a later
    run — it may still belong to a write that's mid-flight elsewhere, and
    the sweep only runs once a leftover is old enough to be certainly
    dead."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    fresh = tmp_path / f"{CT.CONFIG_FILENAME}.tmp.99999"
    fresh.write_text("recent, not yet stale")

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert fresh.exists()
    assert fresh.read_text() == "recent, not yet stale"


def test_the_bare_tmp_name_is_never_swept(tmp_path):
    """The bare `<config>.tmp` name is precisely the name a peer (a live
    `co` session, Claude Code's own atomic-write implementation) might use —
    the sweep must never touch it, aged or not."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    peer_tmp = tmp_path / f"{CT.CONFIG_FILENAME}.tmp"
    peer_tmp.write_text("a peer's in-flight write")
    old = time.time() - CT.STALE_TMP_AGE_SECONDS - 1
    os.utime(peer_tmp, (old, old))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert peer_tmp.exists()
    assert peer_tmp.read_text() == "a peer's in-flight write"


def test_a_non_digit_tmp_suffix_is_never_swept(tmp_path):
    """A suffix that isn't all digits isn't this module's own naming
    convention (`<config>.tmp.<pid>`), so it isn't this module's litter to
    clear, aged or not."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    foreign = tmp_path / f"{CT.CONFIG_FILENAME}.tmp.backup"
    foreign.write_text("not ours")
    old = time.time() - CT.STALE_TMP_AGE_SECONDS - 1
    os.utime(foreign, (old, old))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    assert foreign.exists()
    assert foreign.read_text() == "not ours"


def test_a_sweep_that_raises_still_lets_pre_trust_succeed(tmp_path, monkeypatch):
    """Sweeping is housekeeping, not the job — a failure in it must not
    surface past this function's own "FAILS SOFT, NEVER RAISES"
    guarantee."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))
    stale = tmp_path / f"{CT.CONFIG_FILENAME}.tmp.99999"
    stale.write_text("stale")
    old = time.time() - CT.STALE_TMP_AGE_SECONDS - 1
    os.utime(stale, (old, old))

    def boom(self):
        raise OSError("permission denied listing directory")
    monkeypatch.setattr(CT.Path, "iterdir", boom)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": None}
    data = _read(tmp_path)
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}


# MARK: - The mode lookup cannot crash the "never raises" guarantee


def test_config_deleted_right_before_the_stat_call_does_not_raise(tmp_path, monkeypatch):
    """THE RACE THIS CLOSES. The old code was `config_path.exists()`
    followed by a separate `os.stat(config_path)` call outside every
    try/except in the function — unlike the read, the open, and the write,
    which all catch `OSError`. A config deleted in the window between those
    two calls (the same kind of concurrent modification this module's
    docstring already says it cannot close the window against — a live `co`
    session rewriting `~/.claude.json` on its own schedule) made the second,
    unguarded `os.stat` raise `FileNotFoundError` right past this function's
    own "FAILS SOFT, NEVER RAISES" guarantee.

    This deletes on the SECOND `os.stat` call for `config_path`, not the
    first, so it lands exactly in that old two-call window: mutating the fix
    back to `config_path.exists() and os.stat(config_path)` makes this test
    raise (`.exists()` is itself the first call and succeeds, then the
    explicit second call hits the deleted file, unguarded). Against the
    fixed single stat-and-catch, there is no second call left to land in —
    which is the point: the window is gone, not just individually
    survived."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))

    real_stat = os.stat
    calls_for_config = {"n": 0}

    def deletes_on_second_stat(path, *a, **k):
        if str(path) == str(config):
            calls_for_config["n"] += 1
            if calls_for_config["n"] == 2:
                config.unlink()
        return real_stat(path, *a, **k)

    monkeypatch.setattr(CT.os, "stat", deletes_on_second_stat)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is True
    assert result["reason"] is None
    data = _read(tmp_path)
    assert data["projects"]["/srv/convivy/nyh"] == {"hasTrustDialogAccepted": True}


def test_a_stat_failure_other_than_missing_file_fails_soft(tmp_path, monkeypatch):
    """`os.stat` failing for a reason other than "the file is gone" (a
    permissions error, say) must degrade the same as every other `OSError`
    in this function, not surface past the mode lookup."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {}}))

    real_stat = os.stat

    def boom(path, *a, **k):
        if str(path) == str(config):
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(CT.os, "stat", boom)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "could not write" in result["reason"]
    assert _read(tmp_path) == {"projects": {}}


def test_an_already_trusted_entry_is_a_noop(tmp_path, monkeypatch):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {
        "/srv/convivy/nyh": {"hasTrustDialogAccepted": True, "lastSessionId": "abc123"},
    }}))
    before = config.read_text()

    def _no_write(*a, **k):
        raise AssertionError("must not rewrite an already-trusted entry")
    monkeypatch.setattr(CT.os, "replace", _no_write)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result == {"trusted": True, "reason": "already trusted"}
    assert config.read_text() == before


# MARK: - Fails soft, never raises, never guesses at what it cannot parse


def test_unreadable_file_fails_soft_and_is_untouched(tmp_path):
    # A directory in place of the file makes `read_text()` raise `OSError`
    # (specifically `IsADirectoryError`) — portable across platforms and
    # permission models, unlike a chmod-based simulation.
    (tmp_path / CT.CONFIG_FILENAME).mkdir()

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "could not read" in result["reason"]
    assert (tmp_path / CT.CONFIG_FILENAME).is_dir()


def test_malformed_json_fails_soft_and_is_untouched(tmp_path):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text("{not json")

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "not valid JSON" in result["reason"]
    assert config.read_text() == "{not json"


def test_non_object_json_fails_soft(tmp_path):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text("[]")

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "not a JSON object" in result["reason"]
    assert config.read_text() == "[]"


def test_a_non_object_projects_key_fails_soft(tmp_path):
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": "nope"}))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "projects" in result["reason"]
    assert _read(tmp_path) == {"projects": "nope"}


def test_a_non_object_project_entry_fails_soft(tmp_path):
    """The same 'this process does not know the shape of data it cannot
    parse' philosophy already honored for a non-object top-level file and a
    non-object `projects` container, extended to a single stray entry: a
    string where `projects[directory]` should be an object must not be
    silently discarded and replaced."""
    config = tmp_path / CT.CONFIG_FILENAME
    config.write_text(json.dumps({"projects": {
        "/srv/convivy/nyh": "nope",
    }}))

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "/srv/convivy/nyh" in result["reason"]
    assert _read(tmp_path) == {"projects": {"/srv/convivy/nyh": "nope"}}


def test_a_write_failure_fails_soft(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(CT.os, "replace", _boom)

    result = CT.pre_trust_created_directory(tmp_path, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert "could not write" in result["reason"]


def test_an_unwritable_home_fails_soft(tmp_path):
    """No config file, and the lock file itself cannot be created — the
    earliest possible failure point, degrading the same as every later one."""
    home = tmp_path / "does-not-exist"  # never created

    result = CT.pre_trust_created_directory(home, "/srv/convivy/nyh")

    assert result["trusted"] is False
    assert result["reason"]
