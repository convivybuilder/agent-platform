"""Static guard: every `gh` subprocess call and every `_gh_pr_json`/
`_gh_api_json` call in `bin/agentctl` carries a minted `env` — never the
operator's ambient `gh` login (platform/decisions/0051: every `gh` call goes
through the Bosun; company/decisions/0027: the operator's own GitHub
credential is never on a machine that serves sessions).

The one exception is the Library's own merge paths (kb merge, docs merge),
routed separately and left untouched by this slice — `_EXCLUDED_FUNCTIONS`
names them exactly, and their whole subtree is never walked, so a plain `gh`
call made from WITHIN one of them is invisible to this test by design, not
by accident.

This is a syntactic check, not a runtime one: it asks whether the call site
PASSES an `env=` keyword whose value isn't the literal `None` (so either a
real variable or an attribute, never nothing and never a hardcoded null) —
not whether that value is actually a minted token at runtime. That is
exactly the shape every migrated call site in this file now has (`env=env`,
threaded down from a `with _gh_run_env(...) as env:` the command mints
once), and exactly the shape a forgotten migration lacks.
"""
import ast
import inspect


_EXCLUDED_FUNCTIONS = frozenset({
    "_kb_merge_read",
    "_kb_merge_rebuild",
    "_kb_merge_await_head_catchup",
    "_kb_merge_required_check_conclusions",
    "cmd_docs_merge",
    # `cmd_docs_merge`'s own body, split out only so `_merge_reads_as_app`'s
    # `with` block doesn't have to re-indent it; every call here still runs
    # inside that block, on the minted read token.
    "_docs_merge_decide",
})

_CHECKED_HELPER_NAMES = frozenset({"_gh_pr_json", "_gh_api_json"})


def _first_list_elt_is_gh(node) -> bool:
    """True when `node` is `["gh", ...]`, or an `if`/`else` expression between
    two such lists (`_mark_pr_ready`'s `["gh", "pr", "ready", ...] if repo else
    [...]`) — the only two shapes this file's `gh` argv literals take."""
    if isinstance(node, ast.List) and node.elts:
        first = node.elts[0]
        return isinstance(first, ast.Constant) and first.value == "gh"
    if isinstance(node, ast.IfExp):
        return _first_list_elt_is_gh(node.body) or _first_list_elt_is_gh(node.orelse)
    return False


def _is_gh_subprocess_call(node: ast.Call) -> bool:
    """`subprocess.run(["gh", ...], ...)`."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "run"
            and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
        return False
    return bool(node.args) and _first_list_elt_is_gh(node.args[0])


def _is_checked_helper_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id in _CHECKED_HELPER_NAMES


def _has_real_env_kwarg(node: ast.Call) -> bool:
    """True iff `node` carries an `env=` keyword whose value is not the
    literal `None` — omitting `env` and writing `env=None` are the same
    ambient-login outcome, so both fail this check the same way."""
    for kw in node.keywords:
        if kw.arg == "env":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


class _GhCallFinder(ast.NodeVisitor):
    """Every `gh` subprocess call and `_gh_pr_json`/`_gh_api_json` call OUTSIDE
    `_EXCLUDED_FUNCTIONS`'s own bodies. A named exclusion's subtree is never
    descended into (no `generic_visit` call for it), so a nested closure
    inside one is skipped too, by the same mechanism that skips the rest of
    its body."""

    def __init__(self):
        self.violations: list[ast.Call] = []

    def _visit_def(self, node):
        if node.name in _EXCLUDED_FUNCTIONS:
            return
        self.generic_visit(node)

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def

    def visit_Call(self, node: ast.Call):
        if (_is_gh_subprocess_call(node) or _is_checked_helper_call(node)) \
                and not _has_real_env_kwarg(node):
            self.violations.append(node)
        self.generic_visit(node)


def _find_violations(source: str) -> list[ast.Call]:
    finder = _GhCallFinder()
    finder.visit(ast.parse(source))
    return finder.violations


def test_every_gh_call_outside_the_library_paths_threads_a_minted_env(agentctl):
    """Fails on any `gh`/`_gh_pr_json`/`_gh_api_json` call in `bin/agentctl`,
    outside the five named Library functions, that omits `env=` or passes
    `env=None` — the shape a forgotten migration to `_gh_run_env` leaves
    behind."""
    source = inspect.getsource(agentctl)
    violations = _find_violations(source)
    assert not violations, (
        "the following call(s) in bin/agentctl pass no env= (or env=None) and "
        "would run `gh` on the operator's ambient login: "
        + ", ".join(f"line {node.lineno}" for node in violations))


def test_the_walker_catches_a_planted_ambient_gh_call():
    """Proves the walker above actually fires: a `subprocess.run(["gh", ...])`
    with no `env=` at module (non-excluded) scope must be flagged."""
    planted = (
        "import subprocess\n"
        "def cmd_not_excluded(args):\n"
        "    r = subprocess.run(['gh', 'pr', 'view', '1'], capture_output=True)\n"
        "    return r\n"
    )
    violations = _find_violations(planted)
    assert len(violations) == 1
    assert violations[0].lineno == 3


def test_the_walker_ignores_calls_inside_excluded_library_functions():
    """The same planted call, but inside a named excluded function, must be
    invisible — that is the kb/docs-merge carve-out this test exists to keep
    narrow and explicit."""
    planted = (
        "import subprocess\n"
        "def cmd_docs_merge(args):\n"
        "    r = subprocess.run(['gh', 'pr', 'view', '1'], capture_output=True)\n"
        "    return r\n"
    )
    assert _find_violations(planted) == []


def test_the_walker_accepts_env_equals_env_but_not_env_equals_none():
    passing = (
        "import subprocess\n"
        "def cmd_x(args):\n"
        "    with _gh_run_env(repo, 'read') as env:\n"
        "        subprocess.run(['gh', 'pr', 'view', '1'], env=env)\n"
    )
    failing = (
        "import subprocess\n"
        "def cmd_x(args):\n"
        "    subprocess.run(['gh', 'pr', 'view', '1'], env=None)\n"
    )
    assert _find_violations(passing) == []
    assert len(_find_violations(failing)) == 1
