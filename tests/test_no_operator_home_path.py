"""No code file under bin/, lib/ or tools/ names a macOS user's home directory.

Shipwright's install rehearsal blocks on a `/Users/<name>` path in a code file,
comments and docstrings included. A comment that spells the path therefore
blocks packaging as surely as code does, so this scan covers comments too.

Docs (`.md`) and launchd job declarations (`.plist`) under those directories name
the path and do not block the rehearsal, so they are not scanned.

The pattern below is built from string pieces rather than written as one
literal, so this file does not itself carry anyone's home path.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED = ("bin/", "lib/", "tools/")
NOT_CODE_SUFFIXES = (".md", ".plist")

# Matches any macOS home directory path, e.g. "/Users/<name>", without this
# file ever holding an operator's real path as one literal. No trailing "/"
# is required: the match stops at the next "/", quote or whitespace, or at
# the end of the line, so "/Users/<name>." and "(/Users/<name>)" are caught the
# same as "/Users/<name>/.local".
_HOME_DIR_RE = re.compile("/" + "Users" + r"/[^/\s\"']+")

# Every /Users/<name> hit already in the tree, pinned by file plus the exact
# matched text rather than a line number, so a slice that clears one in any
# merge order neither breaks this test nor has to touch it: a pinned hit
# going away is fine, a new, unpinned one is not. Each is a generic
# placeholder example, never the operator's real path — agent-platform#828
# scoped bin/ and lib/ against Shipwright's publish-engine and reached that
# same read for every one of these except check-primary-checkouts, read here
# directly: a template "<name>" token, not an account.
_KNOWN_HOME_PATH_HITS = {
    "bin/check-primary-checkouts": ("/Users/<name>`.",),
    "bin/review-launch": ("/" + "Users" + "/reviewers",),
    "lib/agent-platform/bosun_authz.py": ("/" + "Users" + "/foo",),
    "lib/agent-platform/projects.py": ("/" + "Users" + "/root`.",),
    "lib/agent-platform/shell_families.py": ("/" + "Users" + "/x",),
    "lib/agent-platform/venv_bootstrap.py": ("/Users/<name>",),
}


def _code_files():
    """Tracked files under SCANNED that are code. `git ls-files` (not a walk) so a
    local `__pycache__` or `.venv` never counts; the index is what ships."""
    out = subprocess.run(
        ["git", "ls-files", "--", *SCANNED], cwd=REPO,
        capture_output=True, text=True, check=True).stdout.split()
    return [REPO / p for p in out if not p.endswith(NOT_CODE_SUFFIXES)]


def _home_path_hits(paths):
    """Every line naming a macOS home directory, as (label, line_no, line,
    matched_text) tuples."""
    hits = []
    for path in paths:
        text = path.read_bytes().decode("utf-8", "replace")
        label = (path.relative_to(REPO) if REPO in path.parents else path).as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            m = _HOME_DIR_RE.search(line)
            if m:
                hits.append((label, n, line.strip(), m.group()))
    return hits


def _lines_naming_a_home_path(paths):
    return [f"{label}:{n}: {line}" for label, n, line, _ in _home_path_hits(paths)]


def test_no_code_file_names_a_users_home_directory():
    unpinned = [
        f"{label}:{n}: {line}"
        for label, n, line, matched in _home_path_hits(_code_files())
        if matched not in _KNOWN_HOME_PATH_HITS.get(label, ())
    ]
    assert not unpinned, (
        "a macOS user's home directory (/Users/<name>) is in a code file "
        "under bin/, lib/ or tools/ (comments included), and it is not one "
        "of the pinned pre-existing examples in _KNOWN_HOME_PATH_HITS. Name "
        "the path relatively, or say 'the mini's home directory':\n"
        + "\n".join(unpinned))


def test_the_scan_sees_code_files_and_a_home_path(tmp_path):
    # Positive control: the scan must find a planted occurrence, and must see
    # real tracked code, or a clean result proves nothing. The planted path
    # uses a synthetic username, built the same way as the pattern above, so
    # this file still carries no one's real home path.
    synthetic_home = "/" + "Users" + "/" + "synthetic-user" + "/.local/run"
    planted = tmp_path / "planted"
    planted.write_text(f"# see {synthetic_home}\nx = 1\n")
    hits = _lines_naming_a_home_path([planted])
    assert len(hits) == 1 and hits[0].endswith(f":1: # see {synthetic_home}")
    scanned = {p.relative_to(REPO).as_posix() for p in _code_files()}
    assert "bin/session-view" in scanned
    assert "lib/agent-platform/registry.py" in scanned
    # Pins the suffix filter: a .sh file is code and must be scanned, a .md doc must not.
    assert "tools/convivy-open/install-laptop.sh" in scanned
    assert "tools/convivy-open/README.md" not in scanned
