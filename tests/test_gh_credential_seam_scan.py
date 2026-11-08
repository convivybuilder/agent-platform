"""Every `gh` subprocess call in `bin/` and `lib/agent-platform/` threads a
minted token's environment (company/decisions/0027: the operator's own `gh`
login is never on a machine that serves sessions).

`bin/agentctl` is excluded — a separate run moves its own `gh` calls onto the
install's GitHub App, and this scan is not that run's gate. `scripts/ci/`
is excluded too: it runs inside GitHub Actions on Actions' own token, not
this install's App.

The scan is syntactic, not semantic: it flags a `subprocess.run` call whose
first argument looks like a `gh` CLI invocation — a list/tuple starting
with the literal `"gh"`, or with a variable (a resolved binary path, such as
`queue-sync`'s `GH_BIN` or `worktrees.py`'s local `gh`) followed by one of
the subcommands this codebase actually calls (`pr`, `run`, `search`, `api`)
— and that call carries no `env=` keyword at all. A call that threads `env=`
some other variable (including one that can be `None` at runtime, like
`model-scorecard`'s `_gh_json`/`had_follow_up_bug` for `gh search`, which a
repo-scoped token cannot drive) passes: the seam this scan guards is that
every call site was wired for a minted credential, not that every call
always has one at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
LIB_DIR = ROOT / "lib" / "agent-platform"

# Every `gh` subprocess call site that carries no `env=` keyword, by design —
# not a credential bug. Empty: every real call this move touched threads
# `env=` at the call site itself (see the module docstring's `_gh_json` note).
ALLOWED_UNMOVED: frozenset[tuple[str, int]] = frozenset()

EXCLUDED_BIN_SCRIPTS = frozenset({"agentctl"})

# The gh subcommands this codebase actually invokes, for recognizing a
# variable-held binary path (`GH_BIN`, a local `gh`) as a `gh` call rather
# than some other tool's argv.
_GH_SUBCOMMAND_MARKERS = frozenset({"pr", "run", "search", "api"})


def _is_gh_argv(node: ast.expr) -> bool:
    """True when `node` — a `subprocess.run` call's first positional arg —
    looks like a `gh` CLI argv."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    if isinstance(first, ast.Constant) and first.value == "gh":
        return True
    if isinstance(first, ast.Name) and len(node.elts) >= 2:
        second = node.elts[1]
        return isinstance(second, ast.Constant) and second.value in _GH_SUBCOMMAND_MARKERS
    return False


def _is_subprocess_run(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "run"
    if isinstance(func, ast.Name):
        return func.id == "run"
    return False


def scan_source(source: str, *, filename: str) -> list[int]:
    """Every line number of a `gh` subprocess call in `source` that carries
    no `env=` keyword."""
    tree = ast.parse(source, filename=filename)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_run(node) or not node.args:
            continue
        if not _is_gh_argv(node.args[0]):
            continue
        if any(kw.arg == "env" for kw in node.keywords):
            continue
        out.append(node.lineno)
    return out


def _candidate_files():
    for path in sorted(BIN_DIR.iterdir()):
        if not path.is_file() or path.name in EXCLUDED_BIN_SCRIPTS:
            continue
        yield path
    for path in sorted(LIB_DIR.glob("*.py")):
        yield path


def _scan_tree() -> list[tuple[str, int]]:
    out = []
    for path in _candidate_files():
        try:
            source = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        # bin/ holds bash/zsh scripts alongside Python ones (this repo's own
        # ARCH-082 convention: check the shebang, never assume); only a
        # Python source can be parsed as one.
        first_line = source.split("\n", 1)[0]
        if path.suffix == "" and "python" not in first_line:
            continue
        if "subprocess" not in source:
            continue
        for lineno in scan_source(source, filename=str(path)):
            out.append((str(path.relative_to(ROOT)), lineno))
    return out


def test_every_gh_subprocess_call_in_bin_and_lib_threads_a_minted_env():
    hits = _scan_tree()
    unexpected = sorted(h for h in hits if h not in ALLOWED_UNMOVED)
    assert not unexpected, (
        "gh subprocess call(s) with no minted env= (company/decisions/0027): "
        + ", ".join(f"{f}:{n}" for f, n in unexpected))


# --- the scan itself, pinned against synthetic source rather than the real tree ---

def test_scan_source_flags_a_bare_gh_call_with_no_env():
    src = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['gh', 'pr', 'list'], capture_output=True)\n"
    )
    assert scan_source(src, filename="<test>") == [3]


def test_scan_source_flags_a_variable_held_gh_binary_too():
    src = (
        "import subprocess\n"
        "GH_BIN = '/usr/local/bin/gh'\n"
        "def f():\n"
        "    subprocess.run([GH_BIN, 'pr', 'view', '1'], capture_output=True)\n"
    )
    assert scan_source(src, filename="<test>") == [4]


def test_scan_source_passes_a_call_that_threads_env():
    src = (
        "import subprocess\n"
        "def f(env):\n"
        "    subprocess.run(['gh', 'pr', 'list'], capture_output=True, env=env)\n"
    )
    assert scan_source(src, filename="<test>") == []


def test_scan_source_ignores_an_unrelated_binary():
    src = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['git', 'status'], capture_output=True)\n"
    )
    assert scan_source(src, filename="<test>") == []


def test_scan_source_matches_the_starred_args_shape_every_scorecard_uses():
    """`_gh_json(['gh', *args], ..., env=env)` — the shared shape in
    dependabot-review/security-scorecard/warrant-scorecard/model-scorecard/
    velocity-report — carries a starred expression as the second list
    element, so the subcommand-marker branch never applies; the literal
    `"gh"` first element alone is enough."""
    src = (
        "import subprocess\n"
        "def f(args, env):\n"
        "    subprocess.run(['gh', *args], capture_output=True, env=env)\n"
    )
    assert scan_source(src, filename="<test>") == []
