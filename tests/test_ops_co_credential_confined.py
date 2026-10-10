"""The co-only credential's file name is written in exactly four tracked files.

This is a placement rule, not a secrecy rule. The path is not a secret: any run
can read `bin/co`, which names it, and `bin/mcp-ops-server`, which reads it. The
dispatch sandbox (`lib/agent-platform/run_sandbox.py`, knowledge#793 step 3) has landed
and denies a dispatched run read access to the whole `.orient-control` directory the
file lives in, so its read denial protects the file now rather than only once it lands;
`tests/live/test_dispatched_run_confinement.py` is the live test for that denial, and
both files name the credential for that reason. The rule keeps the name in the files
that need it, so a reviewer sees every place a new reference would appear.

The match is the literal bytes of a file and the characters of its path. A file
that builds the name from parts (`"ops-co" "-token"`) passes, and this test does
not try to stop that. The test file holds the literal so the scan can look for it,
so it is exempt too. Tests that exercise the credential use neutral file names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEEDLE = "ops-co-token"
# bin/co and bin/mcp-ops-server name the credential; lib/agent-platform/run_sandbox.py
# denies a dispatched run read access to the directory it lives in, and
# tests/live/test_dispatched_run_confinement.py is the live test for that denial. This
# file holds the literal so the scan can look for it. Nothing else is exempt.
ALLOWED = frozenset({
    "bin/co",
    "bin/mcp-ops-server",
    "lib/agent-platform/run_sandbox.py",
    "tests/live/test_dispatched_run_confinement.py",
    "tests/test_ops_co_credential_confined.py",
})


def _tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def _offenders(root: Path, rels: list[str]) -> list[str]:
    """The files in `rels` outside ALLOWED whose path or bytes contain NEEDLE."""
    hits = []
    for rel in rels:
        if rel in ALLOWED:
            continue
        if NEEDLE in rel:
            hits.append(rel)
            continue
        path = root / rel
        # A tracked path absent from the checkout (a deleted file the index has
        # not caught up with) has no bytes to scan.
        if path.is_file() and NEEDLE.encode() in path.read_bytes():
            hits.append(rel)
    return hits


def test_the_co_credential_name_appears_only_in_bin_co_and_the_ops_server():
    assert _offenders(ROOT, _tracked_files(ROOT)) == []


def test_the_scan_flags_a_planted_file_and_its_path(tmp_path):
    (tmp_path / "docs.md").write_text("the token lives in the ops-co-token file\n")
    (tmp_path / "notes" / "ops-co-token.txt").parent.mkdir()
    (tmp_path / "notes" / "ops-co-token.txt").write_text("nothing here\n")
    assert _offenders(tmp_path, ["docs.md", "notes/ops-co-token.txt"]) == [
        "docs.md", "notes/ops-co-token.txt"]


def test_the_scan_passes_a_clean_file_and_exempts_the_two_allowed_paths(tmp_path):
    (tmp_path / "clean.md").write_text("no credential named here\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "co").write_text("ops-co-token\n")
    assert _offenders(tmp_path, ["clean.md", "bin/co"]) == []
