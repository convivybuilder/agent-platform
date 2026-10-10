"""Which service holds the vaults, and the sweep that keeps the answer in one place.

**The test that matters most here is the sweep**, not the translations. Step 7 of
the cutover repoints every client at the Library, and its failure is silent by
construction: Orient's copy of the moved tables survives until Stage C, so a
client nobody repointed gets a `200` and writes where nobody reads. The
specification named seven files and a survey found twelve, because five reach the
base URL by importing another module's constant rather than reading the
environment. A list is the wrong artifact for that; a sweep that fails CI is the
right one.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))

import vault_plane  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LIBRARY = "http://127.0.0.1:8100"


@pytest.fixture
def on_library(monkeypatch):
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", LIBRARY)
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", True)


@pytest.fixture
def on_orient(monkeypatch):
    """The address stays set, because it always is — Orient's `library_client`
    has pointed at the Library since the read rewire. Only the switch is off,
    which is the state this merges in."""
    monkeypatch.setattr(vault_plane, "LIBRARY_BASE_URL", LIBRARY)
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)


# --- the default, which is now what makes an unconfigured writer SAFE -----------

def test_explicit_opt_out_still_reaches_orient(on_orient):
    """The rollback must still work exactly as the pre-cutover default did:
    Orient's address, Orient's path shape, and no protocol header. Inverting a
    default must not remove the way back, and this is the test that says so.

    `0` is the one spelling that gets here, and `false` raises rather than being
    read as the same intent — so a rollback either works or says why, and never
    quietly does nothing.
    """
    assert not vault_plane.on_library()
    assert vault_plane.url("/api/issues?state=open") == (
        f"{vault_plane.ORIENT_BASE_URL}/api/issues?state=open")
    assert vault_plane.headers("tok") == {"Authorization": "Bearer tok"}


# The env spellings, tested against the parse itself rather than around an import.
# THE OLD TEST FOR THIS COULD NOT HAVE CAUGHT AN INVERTED DEFAULT. It was named
# `test_unset_means_orient_and_changes_nothing` and asserted through the `on_orient`
# fixture, which monkeypatches the constant — so it passed whatever the environment
# parse did, and the parse is the entire behaviour. `_plane_from_env` takes the
# environment as an argument precisely so this can be checked directly.
@pytest.mark.parametrize("env,expect_library", [
    ({}, True),                            # unset — the deployed value on 75 of 83 jobs
    ({"VAULTS_ON_LIBRARY": "1"}, True),    # the explicit on, and the value all 8 set jobs use
    ({"VAULTS_ON_LIBRARY": "0"}, False),   # the rollback
    ({"VAULTS_ON_LIBRARY": " 1 "}, True),  # .strip() is load-bearing for a padded plist value
    ({"VAULTS_ON_LIBRARY": " 0 "}, False),
])
def test_the_environment_decides_the_plane(env, expect_library):
    assert vault_plane._plane_from_env(env) is expect_library


@pytest.mark.parametrize("value", [
    "true", "false", "yes", "no", "off", "on",  # the plausible words, in both directions
    "", "   ",                                  # a templated deploy that emitted nothing
    "2", "1 0", "Library", "orient", "TRUE",
])
def test_an_unrecognised_value_raises_rather_than_picking_a_store(value):
    """The whole point of the parse, and the one behaviour worth arguing about.

    Two weaker rules were considered and each fails silently in one direction. A
    strict `== "1"` sends `true` to Orient — the store nobody reads — and that is
    the defect this change exists to close, reintroduced through a typo. An
    accept-set of falsey words narrows the silent-rollback path without closing it,
    and teaches a generalization it does not honour: an operator who learns that
    `false` works writes `disabled` next time and is silently on the Library.

    Raising is the only rule loud in BOTH directions. The empty cases are the ones
    `security-reviewer` found on PR #406: a templated deploy emitting an empty value
    rather than omitting the key. Under `== "1"` that silently reverted a job to the
    superseded store; here it stops the process and says so.
    """
    with pytest.raises(RuntimeError, match="VAULTS_ON_LIBRARY"):
        vault_plane._plane_from_env({"VAULTS_ON_LIBRARY": value})


def test_no_deployed_value_would_trip_the_refusal():
    """Raising is only acceptable because nothing deployed hits it.

    Verified 2026-08-18 across 83 installed launchd jobs: the variable is `1` on 8
    and absent on the other 75, with no shell startup file and no git hook setting
    it. Those are the only two shapes in the fleet and both are accepted here, so
    this lands refusing nothing that exists.
    """
    assert vault_plane._plane_from_env({"VAULTS_ON_LIBRARY": "1"}) is True
    assert vault_plane._plane_from_env({}) is True


def test_an_unconfigured_writer_would_reach_the_library(on_library):
    """The regression this inversion exists to prevent, stated end to end.

    A git hook or a launchd job with nothing exporting the variable is the case
    that stranded 9 work items and 8 signals in Orient's superseded copy between
    the cutover and 2026-08-18. `_plane_from_env({})` is that process's answer;
    these are the requests it then makes.
    """
    assert vault_plane._plane_from_env({}) is True
    assert vault_plane.url("/api/issues") == f"{vault_plane.LIBRARY_BASE_URL}/items"
    assert vault_plane.headers("tok")[vault_plane.PROTOCOL_HEADER] == \
        vault_plane.PROTOCOL_VERSION


def test_orient_paths_are_untouched(on_orient):
    for path in ("/api/issues", "/api/signals/search", "/api/courses/course_1",
                 "/api/decisions/dec_1/decide", "/api/leases"):
        assert vault_plane.translate(path) == path


# --- the mapping ----------------------------------------------------------------

def test_issues_become_items(on_library):
    """**The one part of the mapping that is not mechanical, and the part a
    hand-edited client gets wrong.** `/api/issues` looks like it should become
    `/issues`, and the Library serves no such route — it would 404 every filing,
    every claim and every resolve."""
    assert vault_plane.translate("/api/issues") == "/items"
    assert vault_plane.translate("/api/issues/iss_1") == "/items/iss_1"
    assert vault_plane.translate("/api/issues/iss_1/claim") == "/items/iss_1/claim"


def test_every_other_resource_just_loses_the_prefix(on_library):
    assert vault_plane.translate("/api/signals") == "/signals"
    assert vault_plane.translate("/api/signals/search") == "/signals/search"
    assert vault_plane.translate("/api/courses/course_1") == "/courses/course_1"
    assert vault_plane.translate("/api/decisions/dec_1/decide") == "/decisions/dec_1/decide"
    assert vault_plane.translate("/api/leases") == "/leases"
    assert vault_plane.translate("/api/metrics/librarian") == "/metrics/librarian"


def test_a_query_string_survives(on_library):
    """A filter dropped in translation is not an error — it is a wider answer,
    which reads as a working call returning more rows than it should."""
    assert vault_plane.translate("/api/issues?state=claimable") == "/items?state=claimable"
    assert vault_plane.translate(
        "/api/issues?state=open&offset=200") == "/items?state=open&offset=200"


def test_a_path_that_merely_starts_with_issues_is_not_rewritten(on_library):
    """`/api/issues-waiting` is not `/api/issues`, and a prefix match that did not
    check the boundary would rewrite it into a route that does not exist."""
    assert vault_plane.translate("/api/issuesomething") == "/issuesomething"


def test_the_protocol_header_is_added_only_on_the_library(on_library):
    """The Library refuses a request that declares no version, so a client that
    reached it without this would fail on every call rather than on the
    interesting ones."""
    sent = vault_plane.headers("tok", json_body=True)
    assert sent[vault_plane.PROTOCOL_HEADER] == vault_plane.PROTOCOL_VERSION
    assert sent["Authorization"] == "Bearer tok"
    assert sent["Content-Type"] == "application/json"


def test_no_token_means_no_authorization_header(on_library):
    """`signal_client` no-ops when it holds no token, so `headers()` has to
    tolerate one being absent rather than sending the string "None"."""
    assert "Authorization" not in vault_plane.headers(None)


def test_the_plane_can_name_itself(on_library, monkeypatch):
    """A client that wants to say where it was pointed asks for words rather
    than interpolating the address — the one shape that reads as innocent and
    composes into a request one edit later."""
    assert vault_plane.describe() == f"the Library at {LIBRARY}"
    monkeypatch.setattr(vault_plane, "VAULTS_ON_LIBRARY", False)
    assert vault_plane.describe() == f"Orient at {vault_plane.ORIENT_BASE_URL}"


def test_the_url_uses_the_configured_plane(on_library):
    assert vault_plane.url("/api/issues") == f"{LIBRARY}/items"
    assert vault_plane.base_url() == LIBRARY


# --- the sweep -------------------------------------------------------------------

# **WHAT THIS SWEEP IS, stated because two rounds of review were spent finding
# out.** It catches the NATURAL mistake — a client author writing the obvious
# thing without knowing `vault_plane` exists — and it is not an adversarial
# control. It matches names and call shapes, so an author determined to route
# around it can, through a helper, a dict of endpoints, a `getattr`, or a
# renamed alias whose name says nothing. Full data-flow tracking is a different
# tool and is not worth building for a rule whose violators are honest.
#
# What it does have to catch is every shape a reasonable author would reach for
# unprompted, which is what `_PLANTED` pins: each one was written by a reviewer
# attacking a previous version of this file, not imagined here.

# Every file allowed to name the base URL or the switch. `vault_plane` is the
# module itself; this file feeds deliberate offenders to the detector below.
#
# A two-item literal inside a tracked file, so it cannot grow silently — an
# addition shows up in the diff that adds it.
_SWEEP_EXEMPT = {
    "lib/agent-platform/vault_plane.py",
    "tests/test_vault_plane.py",
}

# The variables that decide where a client writes. Naming one outside
# `vault_plane` is how a client acquires its own opinion.
_PLANE_VARIABLES = {"ORIENT_BASE_URL", "LIBRARY_BASE_URL", "VAULTS_ON_LIBRARY"}

# The accessors that hand back a bare address. `base_url()` sits next to `url()`
# in the same module and carries no warning, so composing with it is an ordinary
# thing to write and produces the exact bug the module exists to prevent: an
# Orient-shaped path sent to the Library, which 404s (reviewer, round 2).
_PLANE_ACCESSORS = {"base_url"}


def _plane_aliases(tree) -> set[str]:
    """Every name in this file that refers to `vault_plane`.

    **Because `import vault_plane as vp` defeated the whole check** — the first
    version hardcoded the literal module name, so an ordinary alias reproduced
    the sibling repo's shipped bug byte for byte and the sweep said nothing
    (security-reviewer, round 2).
    """
    names = {"vault_plane"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "vault_plane" and alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "vault_plane":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _env_name_constants(tree) -> set[str]:
    """Names bound to one of the plane variables as a string constant.

    **Because storing the variable's NAME in a constant first is this repo's own
    idiom** — `budget.py` and `session_view.py` both do it — and it evaded the
    check entirely (security-reviewer, round 3). `_ORIENT_ENV = "ORIENT_BASE_URL"`
    then `os.environ.get(_ORIENT_ENV)` is a client resolving the plane for
    itself, written the way the surrounding code is written.
    """
    named = set()
    for node in ast.walk(tree):
        # `_ENV = "..."` and `_ENV: str = "..."`. The annotated form is a
        # different node type and was missed by the first cut of this
        # (reviewer, round 4) — a module constant carrying a type annotation is
        # ordinary, and the check has to see both spellings of one statement.
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        value = getattr(node, "value", None)
        if not targets or not isinstance(value, ast.Constant):
            continue
        if value.value in _PLANE_VARIABLES:
            named.update(t.id for t in targets if isinstance(t, ast.Name))
    return named


def _reads_a_plane_variable(node, named=frozenset()) -> bool:
    """`os.getenv("ORIENT_BASE_URL")`, `os.environ.get(...)`, `os.environ[...]`,
    and the same three reached through a bare `environ`.

    **Matched on the SHAPE rather than on one spelling.** The first version was a
    line regex for `os.environ.get(`, and a reviewer defeated it in a minute with
    `os.getenv` — an idiom at least as common, which a future client author would
    reach for without knowing this sweep exists (round 1).
    """
    def names_a_plane_variable(expr) -> bool:
        if isinstance(expr, ast.Constant):
            return expr.value in _PLANE_VARIABLES
        return isinstance(expr, ast.Name) and expr.id in named

    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in {"getenv", "get"} and node.args:
            return names_a_plane_variable(node.args[0])
    if isinstance(node, ast.Subscript):
        target = getattr(node.value, "attr", None) or getattr(node.value, "id", None)
        return target == "environ" and names_a_plane_variable(node.slice)
    return False


def _yields_a_base_url(node, aliases: set[str]) -> bool:
    """An expression handing back a bare address: a `*BASE_URL` name, or a call
    to one of the plane's accessors.

    **The name is trusted, not the value.** An alias renamed to something that
    does not end in `BASE_URL` — `endpoint = vault_plane.ORIENT_BASE_URL` — is
    invisible here, and chasing that needs data-flow rather than more AST
    cleverness. Documented rather than silent (reviewer, round 2).
    """
    name = getattr(node, "attr", None) or getattr(node, "id", None)
    if name and name.endswith("BASE_URL"):
        return True
    if isinstance(node, ast.Call):
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called in _PLANE_ACCESSORS:
            owner = getattr(getattr(node.func, "value", None), "id", None)
            return owner in aliases or owner is None
    return False


def _tainted_names(tree, aliases: set[str]) -> set[str]:
    """Names assigned a base address directly — `url = BASE_URL`.

    **Because `url += path` produces no `BinOp` at all**, and that two-line form
    is the house style two clients in this repo already use to extend a URL
    (reviewer, round 2). One assignment deep is as far as this goes: it is a
    taint set, not data-flow analysis, and the sweep's job is the natural
    mistake rather than the determined one.
    """
    tainted = set()
    for node in ast.walk(tree):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        value = getattr(node, "value", None)
        if targets and value is not None and _yields_a_base_url(value, aliases):
            tainted.update(t.id for t in targets if isinstance(t, ast.Name))
    return tainted


def _composes_a_base_url(node, aliases: set[str], tainted=frozenset()) -> bool:
    """A base URL joined to something else. That composition is the moment a
    client decides which service its request reaches.

    Every shape here was written by a reviewer against an earlier version:
    `+` and `%` and `.format` (round 1), `urljoin` and `+=` (round 2). A bare
    assignment is not a composition and is left alone; a message that wants the
    address calls the accessor and does not join a path to it.
    """
    def holds_a_base(*subtrees) -> bool:
        return any(_yields_a_base_url(sub, aliases)
                   or getattr(sub, "id", None) in tainted
                   for tree in subtrees for sub in ast.walk(tree))

    if isinstance(node, ast.JoinedStr):
        # **AN f-STRING IS A COMPOSITION ONLY IF IT JOINS SOMETHING TO THE
        # ADDRESS.** `f"unreachable at {vault_plane.base_url()}"` names the
        # address in a message and routes nothing, which `mcp-ops-server` does
        # today and should keep doing; `f"{BASE_URL}{path}"` and
        # `f"{BASE_URL}/api/issues"` build a request. The difference is a second
        # interpolation, or literal text carrying a path.
        if not holds_a_base(*node.values):
            return False
        interpolations = sum(1 for part in node.values
                             if isinstance(part, ast.FormattedValue))
        literal_path = any(isinstance(part, ast.Constant)
                           and isinstance(part.value, str) and "/" in part.value
                           for part in node.values)
        return interpolations > 1 or literal_path
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return holds_a_base(node.left, node.right)
    # `url = BASE_URL` then `url += path` produces no BinOp at all, and it is the
    # house style two clients in this repo already use to extend a URL.
    if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
        return holds_a_base(node.target, node.value)
    if isinstance(node, ast.Call):
        called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if called in {"format", "urljoin", "join"}:
            return holds_a_base(*node.args)
    return False


def _routed_url_without_routed_headers(node, aliases: set[str]) -> bool:
    """A request whose URL came from `vault_plane` and whose headers are a
    hand-built dict.

    **The invariant both reviewers had to find by reading.** `parked_sweep._do_park`
    in the sibling repo took its URL from `vault_plane.url()` and built its
    headers by hand, so it reached the right route and was refused for declaring
    no protocol version — a park failing on every run while unpark kept working,
    logged as a warning, never fatal (convivy/convivy-orient#175). The URL rule
    could not see it: the URL was right.

    **Only a DICT LITERAL with no `headers()` call inside it is flagged.** A
    headers value that is a name, or a dict spreading `vault_plane.headers(...)`
    into a larger one, is left alone — both are ordinary refactors of correct
    code, and a detector that flags those gets adopted by being disabled
    (reviewer, round 2).
    """
    if not isinstance(node, ast.Call):
        return False
    supplied = list(node.args) + [kw.value for kw in node.keywords]
    from_plane = any(
        isinstance(sub, ast.Call)
        and getattr(sub.func, "attr", None) == "url"
        and getattr(getattr(sub.func, "value", None), "id", None) in aliases
        for arg in supplied for sub in ast.walk(arg))
    if not from_plane:
        return False
    for candidate in supplied:
        if not isinstance(candidate, ast.Dict):
            continue
        keys = {k.value for k in candidate.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not keys & {"Authorization", "Content-Type"}:
            continue
        routed = any(isinstance(sub, ast.Call)
                     and getattr(sub.func, "attr", None) == "headers"
                     for sub in ast.walk(candidate))
        if not routed:
            return True
    return False


def offenders_in(source: str, label: str) -> list[str]:
    """Every place in `source` that decides a plane for itself."""
    found = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found            # not Python this interpreter parses; not a client
    aliases = _plane_aliases(tree)
    tainted = _tainted_names(tree, aliases)
    named = _env_name_constants(tree)
    for node in ast.walk(tree):
        why = ("resolves a plane variable" if _reads_a_plane_variable(node, named)
               else "composes a URL from a base address"
               if _composes_a_base_url(node, aliases, tainted)
               else "takes its URL from vault_plane and its headers by hand"
               if _routed_url_without_routed_headers(node, aliases) else None)
        if why:
            found.append(f"{label}:{getattr(node, 'lineno', '?')}: {why}")
    return found


def source_files() -> list[Path]:
    """Every tracked Python file, including the `bin/` scripts that carry no `.py`
    suffix. Four of the twelve clients live there, and a sweep that globbed
    `*.py` would have reported the tree clean while missing them — the same blind
    spot that made the specification's list wrong."""
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout.split()
    found = []
    for name in tracked:
        path = REPO / name
        if name in _SWEEP_EXEMPT or not path.is_file():
            continue
        if path.suffix == ".py":
            found.append(path)
            continue
        try:
            with path.open("rb") as fh:
                first = fh.readline()
        except OSError:
            continue
        if first.startswith(b"#!") and b"python" in first:
            found.append(path)
    return found


# **THE POSITIVE CONTROLS, and the reason they are not optional.** A sweep that
# has only ever run against a repo it finds clean cannot tell "the detector works"
# from "the detector matches nothing" — which is what a reviewer proved by
# planting `os.getenv` and a `+` concatenation and watching both tests pass.
# Every shape below was written by a reviewer attacking a version of this file.
_PLANTED = [
    ('BASE = os.getenv("ORIENT_BASE_URL", "http://x")', "os.getenv"),
    ('BASE = os.environ.get("ORIENT_BASE_URL", "http://x")', "os.environ.get"),
    ('BASE = os.environ["LIBRARY_BASE_URL"]', "os.environ[...]"),
    ('from os import environ\nBASE = environ.get("VAULTS_ON_LIBRARY")',
     "a bare environ"),
    ('url = BASE_URL + path', "concatenation"),
    ('url = f"{BASE_URL}{path}"', "an f-string"),
    ('url = f"{sc.BASE_URL}/api/issues"', "an f-string on another module"),
    ('url = "{}{}".format(BASE_URL, path)', ".format"),
    ('url = "%s%s" % (BASE_URL, path)', "percent formatting"),
    ('url = urllib.parse.urljoin(BASE_URL, path)', "urljoin"),
    ('_ORIENT_ENV = "ORIENT_BASE_URL"\nBASE = os.environ.get(_ORIENT_ENV)',
     "the variable name held in a constant"),
    ('_ENV = "LIBRARY_BASE_URL"\nBASE = os.getenv(_ENV, "http://x")',
     "the same, through getenv"),
    ('_ENV: str = "ORIENT_BASE_URL"\nBASE = os.environ.get(_ENV)',
     "the same, annotated"),
    ('url: str = BASE_URL\nurl += path', "an annotated tainted name"),
    ('_ENV = "VAULTS_ON_LIBRARY"\nON = os.environ[_ENV]',
     "the same, through a subscript"),
    ('url = BASE_URL\nurl += path', "augmented assignment"),
    ('import vault_plane\nurl = vault_plane.base_url() + path',
     "composing with the accessor"),
    ('import vault_plane\nurl = f"{vault_plane.base_url()}{path}"',
     "an f-string on the accessor"),
    ('import vault_plane\n'
     'req = Request(vault_plane.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "a routed URL with hand-built headers"),
    ('import vault_plane as vp\n'
     'req = Request(vp.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "the same, through a module alias"),
    ('import vault_plane\n'
     'req = Request(url=vault_plane.url("/api/issues"),\n'
     '              headers={"Authorization": f"Bearer {tok}"})',
     "the same, with the URL passed by keyword"),
    ('import vault_plane\n'
     'req = Request(vault_plane.url("/api/issues"), data,\n'
     '              {"Authorization": f"Bearer {tok}"})',
     "the same, with headers passed positionally"),
]

_ALLOWED = [
    'BASE_URL = vault_plane.ORIENT_BASE_URL',
    'url = vault_plane.url("/api/issues")',
    'msg = f"unreachable — {vault_plane.describe()}"',
    'headers = vault_plane.headers(token=tok, json_body=True)',
    'import vault_plane\n'
    'req = Request(vault_plane.url("/api/issues"),\n'
    '              headers=vault_plane.headers(token=tok))',
    'import vault_plane\n'
    'hdrs = vault_plane.headers(token=tok)\n'
    'req = Request(vault_plane.url("/api/issues"), headers=hdrs)',
    'import vault_plane\n'
    'req = Request(vault_plane.url("/api/issues"),\n'
    '              headers={**vault_plane.headers(token=tok), "X-Extra": "1"})',
]


@pytest.mark.parametrize("snippet,shape", _PLANTED,
                         ids=[shape for _, shape in _PLANTED])
def test_the_sweep_flags_a_planted_offender(snippet, shape):
    assert offenders_in(snippet, "planted"), (
        f"the sweep does not catch {shape}, so a client written that way would "
        f"keep writing to Orient after the cutover with nothing to say so")


@pytest.mark.parametrize("snippet", _ALLOWED)
def test_the_sweep_does_not_flag_the_approved_shapes(snippet):
    """The other half of a positive control: a detector that flags everything is
    as useless as one that flags nothing, and it gets adopted by being disabled.
    The last two are ordinary refactors of correct code — pulling a keyword into
    a variable, spreading the routed headers into a larger dict — which an
    earlier version flagged (reviewer, round 2)."""
    assert not offenders_in(snippet, "allowed"), snippet


def test_nothing_outside_vault_plane_decides_a_plane_for_itself():
    """**The count becomes a check.** A client that resolves the base URL or
    joins it to a path decides for itself which service holds the vaults, and at
    the cutover that decision is made once, silently, in whichever file nobody
    remembered.

    This fails on the file rather than on the deploy, which is the only place the
    failure is cheap.
    """
    offenders = []
    for path in source_files():
        offenders += offenders_in(path.read_text(errors="replace"),
                                  str(path.relative_to(REPO)))
    assert not offenders, (
        "these decide which service holds the vaults for themselves; call "
        "vault_plane.url(path) and vault_plane.headers(...) instead, so the "
        "write cutover repoints them:\n  " + "\n  ".join(offenders))


# --- the Library's own routes, which the vault switch does not move -----------

def test_library_routes_stay_on_the_library_after_a_rollback_to_orient(on_orient):
    """`/bundles` has no Orient counterpart, so `url()` (which follows the
    vault switch) would send it to a service that does not serve it. The
    protocol header is required by the Library and is sent regardless."""
    assert vault_plane.library_url("/bundles") == f"{LIBRARY}/bundles"
    assert vault_plane.url("/bundles") != vault_plane.library_url("/bundles")
    sent = vault_plane.library_headers("tok", json_body=True)
    assert sent == {"Authorization": "Bearer tok", "Content-Type": "application/json",
                    vault_plane.PROTOCOL_HEADER: vault_plane.PROTOCOL_VERSION}
    assert vault_plane.PROTOCOL_HEADER not in vault_plane.headers("tok")


def test_library_routes_on_the_library_plane_match_the_vault_ones(on_library):
    assert vault_plane.library_url("/bundles") == vault_plane.url("/bundles")
    assert vault_plane.library_headers("tok") == vault_plane.headers("tok")


# --- the address, which carries a bearer token ----------------------------------

def test_a_remote_plain_http_library_is_refused():
    """**The write path is held to the read path's bar** (reviewer,
    convivy/convivy-orient#175). Orient's `library_client` refuses at import
    unless the address is loopback or https, because it sends a bearer token;
    every client here sends one too. Without this, one address would sit at two
    safety bars, and the lower one would be the fleet's writes.
    """
    with pytest.raises(RuntimeError) as exc:
        vault_plane._refuse_cleartext("http://library.example.com")
    assert "cleartext" in str(exc.value)


def test_loopback_and_https_are_allowed():
    for allowed in ("http://127.0.0.1:8100", "http://localhost:8100",
                    "https://library.example.com"):
        assert vault_plane._refuse_cleartext(allowed) == allowed


# --- the redirect, which is how the import-time check gets bypassed -------------

def _redirect_to(target: str, origin: str = "https://library.example.com/items"):
    """Ask the installed handler to follow a redirect from `origin` to `target`."""
    import urllib.request
    handler = vault_plane._NoDowngrade()
    return handler.redirect_request(urllib.request.Request(origin), None, 302,
                                    "Found", {}, target)


def test_a_redirect_down_to_cleartext_is_refused():
    """**`_refuse_cleartext` checks the configured address once, at import, and a
    redirect is how that check is bypassed at run time** (security-reviewer,
    convivy/agent-platform#400).

    `urlopen`'s default opener follows 30x and copies every header except
    `Content-Length` and `Content-Type` onto the new request, with no scheme or
    host check — so an https Library answering `Location: http://anywhere`
    resends `Authorization: Bearer <token>` in the clear. Reaching it needs
    control of the Library origin or whatever fronts it, which is exactly the
    actor the guard names.
    """
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:
        _redirect_to("http://attacker.example.com/items")
    assert "cleartext" in str(exc.value)


def test_a_redirect_to_another_host_is_refused_even_over_https():
    """**Refusing only cleartext was not enough** (security-reviewer, round 3).
    `Location: https://attacker.example.com` carried the bearer token intact —
    exfiltration with the transport doing its job. The same actor reaches both,
    so the host has to match as well as the scheme."""
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:
        _redirect_to("https://attacker.example.com/items")
    assert "leaves the origin" in str(exc.value)


def test_a_redirect_to_another_port_on_the_same_host_is_refused():
    """**Comparing the host alone let the token move between loopback services**
    (security-reviewer, round 4). `LIBRARY_BASE_URL` defaults to
    `http://127.0.0.1:8100` and this box runs several services on loopback ports,
    so a host-only check follows `Location: http://127.0.0.1:9999` and hands the
    bearer token to whatever is listening. "Where the request was sent" means the
    origin, which includes the port."""
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _redirect_to("http://127.0.0.1:9999/collect", "http://127.0.0.1:8100/items")
    with pytest.raises(urllib.error.HTTPError):
        _redirect_to("https://library.example.com:8443/items")


def test_the_scheme_default_port_is_the_same_origin():
    """`https://host` and `https://host:443` are one origin, not two — a guard
    that called them different would refuse an ordinary redirect."""
    assert _redirect_to("https://library.example.com:443/items/") is not None


def test_a_same_host_redirect_is_followed():
    """The other half: a guard that refused every redirect would be discovered
    as an outage rather than as a control. A trailing slash or a moved path on
    the host the request was already sent to is ordinary."""
    assert _redirect_to("https://library.example.com/items/") is not None


def test_the_guard_is_the_process_default():
    """Installed rather than offered. Fourteen call sites reach the stdlib
    directly, and a control that depends on all fourteen adopting a new name is
    one the fifteenth will miss."""
    import urllib.request
    installed = urllib.request._opener
    assert installed is not None, "vault_plane did not install its opener"
    assert any(isinstance(h, vault_plane._NoDowngrade) for h in installed.handlers)
