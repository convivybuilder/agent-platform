"""Guards for issue_01M33KK04A9B1A2MC710CNF583: a bin/ script that resolves its
platform library only from the installed ~/.local/lib/agent-platform farm
cannot run from a Builder deploy area, which places bin/ and lib/agent-platform/
together but never runs install.sh and so has no farm at all.
`bin/mcp-ops-server` was the worst case: a bare `import vault_plane` followed
the farm-only insert, so with no farm the server died at import and an
adopter's sessions had no ops tools at all.

The fix (bin/knowledge-watcher's shape, copied to all 18 sites the issue
inventoried) tries the checkout's own lib/agent-platform first — gated on a
module that must be there, so a wrong guess finds nothing rather than
importing a stale copy — and falls back to the installed farm second.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "bin"

# The two spellings every one of the original 18 sites used
# (issue_01M33KK04A9B1A2MC710CNF583's inventory: `Path.home() / ".local" /
# "lib" / "agent-platform"` and `os.path.expanduser('~/.local/lib/agent-platform')`),
# as a direct, unconditional argument to `sys.path.insert`. A comment or
# docstring that merely mentions the path text does not match either pattern.
_FARM_INSERT_PATTERNS = (
    re.compile(
        r'sys\.path\.insert\(\s*0\s*,\s*str\(\s*(?:pathlib\.)?Path\.home\(\)\s*/\s*'
        r'"\.local"\s*/\s*"lib"\s*/\s*"agent-platform"\s*\)\s*\)'
    ),
    re.compile(
        r"sys\.path\.insert\(\s*0\s*,\s*os\.path\.expanduser\(\s*"
        r"['\"]~/\.local/lib/agent-platform['\"]\s*\)\s*\)"
    ),
)


def _extract_resolution_loop(script_path: Path) -> str:
    """Pull the `for _libdir in (...): ... break` block verbatim out of a
    bin/ script, so this test tracks the real source rather than a
    hand-copied twin that could silently drift from it."""
    source = script_path.read_text()
    match = re.search(r"^for _libdir in \(.*?\n(?:.*\n)*?    +break\n", source, re.MULTILINE)
    assert match, f"no gated lib-resolution loop found in {script_path}"
    return match.group(0)


def test_mcp_ops_server_resolves_its_own_checkout_lib_with_no_farm(tmp_path, monkeypatch):
    """`bin/mcp-ops-server`'s farm-resolution loop must find THIS checkout's
    own lib/agent-platform with `HOME` pointed at an empty directory — no
    `.local/lib/agent-platform` farm exists there at all, the state of a
    Builder deploy area.

    Tests the resolution statement in isolation (extracted from the real
    file and exec'd against a throwaway `sys`-alike), not the whole server:
    the real, unstubbed server also does `import mcp`, which is not on
    sys.path outside its own venv, so running the whole file would die on
    `ModuleNotFoundError: No module named 'mcp'` before ever reaching this
    resolution — a fact about the interpreter, not about farm resolution,
    and not what this test checks. Loading the module through the
    project's own `mcp_ops_server` fixture would not isolate this either:
    conftest.py puts this checkout's lib/agent-platform on the REAL
    `sys.path` for the whole suite (so every other lib import resolves to
    this checkout, not a stale deploy), which would let even the
    old, unconditional-farm-only insert import `vault_plane` successfully
    by accident, off that pre-seeded path rather than off its own
    resolution.
    """
    script = BIN_DIR / "mcp-ops-server"
    loop_src = _extract_resolution_loop(script)

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))

    class _FakeSys:
        def __init__(self):
            self.path = []

    fake_sys = _FakeSys()
    ns = {"sys": fake_sys, "Path": Path, "__file__": str(script)}
    exec(compile(loop_src, str(script), "exec"), ns)

    expected = script.resolve().parent.parent / "lib" / "agent-platform"
    assert fake_sys.path == [str(expected)], (
        f"expected the loop to insert only {expected}, got {fake_sys.path}"
    )
    assert (expected / "vault_plane.py").is_file()


def test_no_bin_file_reintroduces_an_unconditional_farm_insert():
    """Nothing under bin/ may go back to inserting the ~/.local/lib/agent-platform
    farm unconditionally (in either spelling) with no checkout-relative
    fallback — that is exactly the defect issue_01M33KK04A9B1A2MC710CNF583
    fixed, and it is easy to reintroduce by copying an old call site rather
    than bin/knowledge-watcher's gated shape.
    """
    offenders = []
    for path in sorted(BIN_DIR.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        if any(pattern.search(text) for pattern in _FARM_INSERT_PATTERNS):
            offenders.append(path.name)
    assert not offenders, (
        "unconditional sys.path.insert of the ~/.local/lib/agent-platform farm, "
        f"with no checkout-relative fallback, in: {offenders} — copy "
        "bin/knowledge-watcher's gated shape instead "
        "(issue_01M33KK04A9B1A2MC710CNF583)"
    )
