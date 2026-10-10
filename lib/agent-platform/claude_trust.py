"""claude_trust — pre-trust a directory `session_lifecycle.start` just
created, so `co` does not stop on Claude Code's workspace-trust prompt the
moment a brand-new crew's session is typed into.

WHAT THIS IS COUPLED TO, STATED PLAINLY. Claude Code added an interactive
startup check — "Is this a project you created or one you trust?" — with no
CLI flag, env var, or settings key to pre-answer it (confirmed empirically:
the dialog still appears under `--permission-mode bypassPermissions`). The
only way an answer is remembered is `hasTrustDialogAccepted: true` on that
project's own entry in `~/.claude.json`. THAT KEY IS UNDOCUMENTED, UNSUPPORTED
INTERNAL STATE of a product that ships independently of agent-platform, not a
published API — this module is a coupling to it, not a use of it. If a future
Claude Code release renames the key, moves it, or changes what it means, this
module quietly stops doing anything useful: a freshly created crew goes back
to being born on the trust prompt, exactly as it was before this existed.
NOTHING BECOMES UNSAFE either way — the worst case on either side of that
break is the same manual "type 1, Enter" a human was always able to do.

SCOPED TO A DIRECTORY THE CALLER JUST CREATED. Call this only for a directory
`session_lifecycle.start`'s own `--create-dir` path actually made with
`mkdir`. A directory Deck is merely adopting was not created by this call and
the user may never have looked inside it, so it must keep prompting — do not
widen this to cover it.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path

CONFIG_FILENAME = ".claude.json"
LOCK_FILENAME = ".claude.json.trust.lock"
TRUST_KEY = "hasTrustDialogAccepted"
STALE_TMP_AGE_SECONDS = 60 * 60


def _sweep_stale_temp_files(config_path: Path) -> None:
    """Best-effort cleanup of this module's own `<config>.tmp.<pid>` litter
    left behind by a prior invocation that crashed between creating its temp
    file and `os.replace`-ing it into place. Call only under the same
    `LOCK_EX` `pre_trust_created_directory` already holds, before it creates
    its own temp file, so nothing here can race that write.

    NEVER RAISES — housekeeping, not the job; see the function's own
    "FAILS SOFT, NEVER RAISES" guarantee, which this shares.

    NAME-SCOPED, NOT JUST PID-SCOPED. Only `<config>.tmp.<digits>` is a
    candidate. The bare `<config>.tmp` is left alone on principle — that is
    precisely the name a peer (a live `co` session, Claude Code's own
    atomic-write implementation) might use, and this module exists to never
    touch that file. A suffix that is not all digits is not this module's
    naming convention either, so it is left alone too.

    AGE FROM MTIME, NOT FOLLOWING A SYMLINK. `lstat` (not `stat`) so a
    symlink planted at a matching name is judged, and later removed, as
    itself — never through to whatever it points at. `Path.unlink` on a
    symlink removes the link, not its target, so that property holds
    end-to-end.
    """
    try:
        prefix = f"{config_path.name}.tmp."
        now = time.time()
        for entry in config_path.parent.iterdir():
            suffix = entry.name[len(prefix):] if entry.name.startswith(prefix) else None
            if not suffix or not suffix.isdigit():
                continue
            try:
                age = now - entry.lstat().st_mtime
            except OSError:
                continue
            if age < STALE_TMP_AGE_SECONDS:
                continue
            try:
                entry.unlink()
            except OSError:
                pass
    except OSError:
        pass


def pre_trust_created_directory(home: Path, directory: str) -> dict:
    """Set `hasTrustDialogAccepted: true` for `directory` in
    `home/.claude.json`. Returns `{"trusted": bool, "reason": str | None}` and
    never raises.

    READ-MODIFY-WRITE, PRESERVING EVERYTHING ELSE. The file is live config for
    every Claude Code session on the box, including per-project stats this
    must not disturb — an existing project entry keeps every other key it
    carries, and every other project's entry is untouched.

    NEVER LOWERS TRUST. An entry already `hasTrustDialogAccepted: true` is
    left exactly as it was (`reason` reports it, `trusted` is still `True`).

    THE WRITE IS ATOMIC, THE READ-MODIFY-WRITE IS NOT RACE-FREE AGAINST
    CLAUDE CODE ITSELF. An `fcntl.flock` on a sibling lock file (the same
    pattern `registry.py` uses for its own active-runs file) serialises this
    against another `session-agent start --create-dir` racing the same
    directory — that closes the only concurrent writer this process controls.
    It does NOT, and cannot, close the window against a live `co` session on
    this machine reading and rewriting `~/.claude.json` on its own schedule:
    there is no lock Claude Code itself participates in, so a write landing
    between this function's read and its rename can still be overwritten or
    lost. What the atomic rename (temp file + `os.replace`) guarantees is
    narrower and still worth having: a concurrent reader of the file never
    observes a half-written one.

    FAILS SOFT, NEVER RAISES. A missing config file is treated as an empty
    one and a fresh file is written — the common case (a first-ever
    `--create-dir` on a machine with no prior Claude Code config) is exactly
    the case this exists to fix, so it is worth writing into, not skipping.
    An UNREADABLE, UNPARSEABLE, or otherwise malformed file is left
    untouched and reported rather than guessed at or overwritten: this
    process does not know the shape of data it cannot parse, and clobbering
    an existing file `co` itself is actively using would be a worse outcome
    than the trust prompt this is meant to avoid. A file that cannot be
    written (permissions, a full disk, a missing parent) degrades the same
    way. In every failing case the caller — `session_lifecycle.start` — is
    expected to proceed with the session anyway: a session that starts with a
    trust prompt is a UX regression, not a reason to fail `start`.
    """
    config_path = home / CONFIG_FILENAME
    lock_path = home / LOCK_FILENAME

    try:
        lock_path.touch(exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError as e:
        return {"trusted": False, "reason": f"could not open lock file: {e}"}

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _sweep_stale_temp_files(config_path)
        try:
            raw = config_path.read_text()
        except FileNotFoundError:
            raw = ""
        except OSError as e:
            return {"trusted": False, "reason": f"could not read {config_path}: {e}"}

        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                return {"trusted": False,
                        "reason": f"{config_path} is not valid JSON: {e}"}
            if not isinstance(data, dict):
                return {"trusted": False,
                        "reason": f"{config_path} is not a JSON object"}
        else:
            data = {}

        projects = data.get("projects")
        if projects is None:
            projects = {}
            data["projects"] = projects
        elif not isinstance(projects, dict):
            return {"trusted": False,
                    "reason": f'{config_path}\'s "projects" is not an object'}

        entry = projects.get(directory)
        if entry is not None and not isinstance(entry, dict):
            return {"trusted": False,
                    "reason": f"{config_path}'s entry for {directory!r} is not an object"}
        if entry is None:
            entry = {}
        if entry.get(TRUST_KEY) is True:
            return {"trusted": True, "reason": "already trusted"}
        entry[TRUST_KEY] = True
        projects[directory] = entry

        tmp_path = config_path.parent / f"{config_path.name}.tmp.{os.getpid()}"
        # PID-SCOPED, NOT THE BARE `<name>.tmp` A NAIVE ATOMIC WRITE WOULD
        # USE. This module's own docstring above already concedes it cannot
        # lock out a live `co` session rewriting `~/.claude.json` on its own
        # schedule — and Claude Code's own atomic-write implementation
        # (closed-source, unverified) could plausibly reach for that same
        # bare `.tmp` convention for the same file. Scoping the name to this
        # process's own PID makes it a path nothing else on the box would
        # plausibly choose, so the leftover self-heal below can only ever
        # find and clear this exact process's own prior litter — never a
        # peer's in-flight temp file.
        #
        # LITTER, BOUNDED BY AGE RATHER THAN LEFT UNCLEANED. If this process
        # crashes between creating `tmp_path` and the `os.replace` below, the
        # leftover `<config>.tmp.<pid>` file stays on disk — a full plaintext
        # copy of `~/.claude.json`, including the shared `mcpServers` block,
        # which can carry bearer tokens or API keys. `_sweep_stale_temp_files`
        # above clears any `<config>.tmp.<digits>` file older than
        # `STALE_TMP_AGE_SECONDS`, regardless of which PID wrote it, the next
        # time ANY invocation of this function runs — so a leftover does not
        # survive indefinitely, only until it's old enough to be certainly
        # dead and a later `--create-dir` happens to run. A leftover younger
        # than the threshold is left in place until then, deliberately: this
        # call's own write is mid-flight under the same lock the sweep runs
        # under, and a short grace window costs nothing a bare shared `.tmp`
        # name wouldn't already cost.
        #
        # The mode is set BEFORE any content reaches the temp file, not after:
        # `write_text` would create it fresh at the umask default, and content
        # written at that mode is exposed — briefly, but in full, including the
        # shared `mcpServers` block — for however long it takes to `chmod` it
        # down afterward. A restrictively-permissioned config (0600 is common
        # for credential-adjacent CLI config) deserves that mode from its very
        # first byte, not from the moment someone remembers to narrow it.
        # `os.open` with `O_CREAT | O_EXCL` both creates at the target mode
        # atomically and keeps the guarantee `write_text` gave for free: a
        # pre-existing temp file is never silently overwritten.
        #
        # STAT-AND-CATCH, NOT CHECK-THEN-STAT: a separate `.exists()` before
        # `os.stat()` is a TOCTOU window — a config deleted between the two
        # (a live `co` session rewriting it, the exact concurrent case this
        # module's docstring already says it cannot close the window
        # against) raised an unguarded `FileNotFoundError` right past this
        # function's own "FAILS SOFT, NEVER RAISES" guarantee. A single
        # `os.stat()` call can't be caught between two halves of itself, and
        # `FileNotFoundError` from it means exactly what `.exists()`
        # returning `False` meant: no prior file, no prior mode to
        # preserve — 0600, the same default a brand-new file gets.
        try:
            target_mode = stat.S_IMODE(os.stat(config_path).st_mode)
        except FileNotFoundError:
            target_mode = 0o600
        except OSError as e:
            return {"trusted": False, "reason": f"could not write {config_path}: {e}"}

        try:
            tmp_fd = os.open(str(tmp_path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, target_mode)
        except FileExistsError:
            # With `tmp_path` scoped to this process's own PID, nothing else
            # on the box would plausibly have created it — so a leftover
            # found here can only be THIS SAME PID's own prior write, from a
            # previous invocation that crashed or was killed before reaching
            # `os.replace`, and whose leftover survived until the OS cycled
            # PIDs back around to this one. That's narrow, but real, not
            # hypothetical, and it's safe to clear and retry once, still
            # under the lock. A second `FileExistsError` fails soft like any
            # other write failure.
            try:
                tmp_path.unlink()
                tmp_fd = os.open(str(tmp_path),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, target_mode)
            except OSError as e:
                return {"trusted": False, "reason": f"could not write {config_path}: {e}"}
        except OSError as e:
            return {"trusted": False, "reason": f"could not write {config_path}: {e}"}
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(json.dumps(data, indent=2) + "\n")
            os.replace(tmp_path, config_path)
        except OSError as e:
            return {"trusted": False, "reason": f"could not write {config_path}: {e}"}

        return {"trusted": True, "reason": None}
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
