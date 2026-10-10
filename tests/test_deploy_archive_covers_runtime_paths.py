"""The deploy is a COPY, so a path it does not enumerate is silently absent.

`bin/install-agent-platform-shared` builds the shared tree with an explicit
`git archive ... -- <paths>` list. Anything a deployed program resolves against
its OWN tree therefore has to appear in that list, or it is missing at runtime
with no fallback to the checkout.

That failed for five days: `bin/kb-eval` reads its golden set from
`Path(__file__).resolve().parents[1] / "eval" / "kb-golden.jsonl"`, `eval` was
not in the archive list, and every nightly run raised FileNotFoundError against
a deploy whose DEPLOYED_COMMIT was current.

This holds the list to what the code actually resolves, rather than to whoever
last remembered to update it.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "bin" / "install-agent-platform-shared"
DEPLOYED_TREES = ("bin", "lib/agent-platform", "lib/knowledge")

# ROOT EXPRESSIONS. `.parents[N]` is what the two real instances use;
# `.parent.parent` is what MOST of this codebase uses (farm_check.py,
# check-kb-guards, check-cli-contracts, agentctl, agent-cost-report, 15+ sites),
# and an earlier version of this file recognised only the first — so the
# commonest idiom in the repo was invisible to the check meant to cover it.
_ROOT_EXPR = r"""Path\(__file__\)\.resolve\(\)(?:\.parents\[\d+\]|(?:\.parent)+)"""

# Assignment to a root, then `ROOT / "name"` elsewhere — the two-statement form
# `bin/kb-eval` uses. The variable may be any case: a lowercase local is as
# idiomatic as a module-level constant, and requiring SCREAMING_SNAKE made
# `repo = Path(__file__).resolve().parents[1]` invisible.
_ROOT_ASSIGN = re.compile(
    rf"""^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{_ROOT_EXPR}""", re.MULTILINE)
_INLINE = re.compile(rf"""{_ROOT_EXPR}\s*/\s*["']([A-Za-z0-9_.-]+)["']""")

# WHAT THIS DELIBERATELY DOES NOT CATCH, so the docstring above is not read as a
# stronger guarantee than the regexes give. None of these is used by either real
# instance, so nothing is broken today, but a future one written this way slips
# past silently — which is the failure mode this whole file exists to prevent,
# and it would be dishonest to leave the limit undocumented:
#
#   f"{REPO}/eval/x"                     an f-string join
#   REPO.joinpath("eval", "x")           joinpath rather than /
#   os.path.join(REPO, "eval", "x")      the os.path idiom
#   _t = Path(__file__).resolve()        a root captured across two hops
#   REPO = _t.parents[2]
#   dirname "$0"/../eval                 shell scripts in bin/ — Python only
#
# The honest version of the guarantee: this holds the archive list against the
# two Path-operator forms, in Python, resolved in one or two statements.


def _strip_comments(text: str) -> str:
    """Comments are stripped before scanning because the FIRST version of this
    file matched the pattern inside `install-agent-platform-shared`'s own
    explanatory comment and attributed `eval` to the installer. It still went red
    when `eval` was dropped, which is exactly what made it look correct."""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split(" # ")[0])
    return "\n".join(out)


def tree_relative_dirs(text: str) -> set[str]:
    """Directory names this source resolves against its own tree root."""
    code = _strip_comments(text)
    names = set(_INLINE.findall(code))
    for var in set(_ROOT_ASSIGN.findall(code)):
        names |= set(re.findall(
            rf"""\b{re.escape(var)}\s*/\s*["']([A-Za-z0-9_.-]+)["']""", code))
    return names


_DEPLOY_PATHS = re.compile(r"""^DEPLOY_PATHS="([^"]*)"\s*$""", re.MULTILINE)


def archived_paths():
    """The pathspecs the installer archives into the deploy tree.

    Read from the `DEPLOY_PATHS=` assignment, which the installer word-splits
    into both its preflight check and its `git archive` line. Parsing the
    assignment rather than the archive line is what makes this one source: an
    earlier version scanned for the first line containing `archive` and `-- `,
    which a later `say`-style echo of the command could have out-raced by
    appearing first in file order, leaving the guard keyed off a stale copy.
    """
    m = _DEPLOY_PATHS.search(INSTALLER.read_text())
    assert m, "no `DEPLOY_PATHS=\"...\"` assignment in the installer"
    return m.group(1).split()


# DELIBERATELY ABSENT FROM EVERY DEPLOY, with the reason, because "not archived"
# is otherwise indistinguishable from "forgotten" — which is the whole defect
# this file exists to catch. An entry here is a claim that the reading code
# copes with the path being missing; adding one without that being true reopens
# the hole quietly, so each carries its argument rather than a name alone.
ABSENT_ON_PURPOSE = {
    "config": (
        "lib/agent-platform/projects.py reads config/projects.json from its own "
        "tree, and MUST NOT find a copy there. It is the privacy registry: a "
        "deployed copy parses fine while being as old as the last install, so a "
        "project marked private afterwards would read as PUBLIC to run-register-"
        "api's /runs and /sessions. Its resolution falls through to the "
        "operator's checkout precisely because this is absent (operator, 2026-09-05)."
    ),
    "etc": (
        "bin/pin-claude only WRITES etc/claude-pin.yaml (platform/decisions/0090), "
        "creating the directory with mkdir(parents=True, exist_ok=True) if it is "
        "missing, and it only ever runs from the operator's own checkout — nothing "
        "schedules it and no launchd job reads its output. A deployed copy would "
        "also be the wrong value to check an install against: the pin has to reflect "
        "what is committed on main right now, not whatever was current at the last "
        "deploy, or a newer pin on main could be bypassed by a stale deployed one."
    ),
}


def _covered(name: str, archived) -> bool:
    """Whether the archive carries `name`, whole or in part.

    `lib` is the case that forces this: scripts add `parent.parent / "lib"` to
    sys.path, while the archive takes `lib/agent-platform` and `lib/knowledge`
    rather than the whole tree. A plain membership test called that missing.

    THE LIMIT THIS ACCEPTS: the regexes above capture only the FIRST path
    component, so a script reading `REPO / "lib" / "something-unarchived"` looks
    identical here to one reading `REPO / "lib" / "agent-platform"`. Catching
    that needs the whole path, not its head — worth doing if a real instance
    ever appears, and not worth guessing at before one does.
    """
    return any(a == name or a.startswith(name + "/") for a in archived)


def test_the_archive_list_was_found_and_is_not_empty():
    """Guards the parse itself. If the installer's line moves or is reworded,
    every other assertion here would pass vacuously against an empty list —
    which is the failure mode this whole file exists to catch, one level up."""
    paths = archived_paths()
    assert len(paths) >= 4
    assert "bin" in paths


def test_the_pinned_review_eval_agent_dirs_are_in_the_archive_list():
    """bin/review-eval's `sonnet-pre828`/`sonnet-post828`/`sonnet-fix832` arms
    resolve `tools/review-eval/pre828-agents`, `tools/review-eval/post828-agents`
    and `tools/review-eval/fix832-agents` the same way `sonnet-variant-prompt`
    resolves `.../variant-agents` — deployed-program-relative, so each needs
    its own entry in DEPLOY_PATHS. The generic `tree_relative_dirs` scan above
    only recognizes the directory name's FIRST path component (`tools`),
    which the already-present `tools/review-eval/variant-agents` entry
    satisfies by prefix regardless of whether these three are listed — so
    that scan cannot catch any of them being dropped. This pins them
    explicitly instead."""
    archived = set(archived_paths())
    assert "tools/review-eval/pre828-agents" in archived
    assert "tools/review-eval/post828-agents" in archived
    assert "tools/review-eval/fix832-agents" in archived


def test_every_tree_relative_path_a_deployed_program_reads_is_deployed():
    """The invariant. A directory named relative to a deployed script's own root
    must be in the archive, or that script is broken the moment it runs from the
    deploy rather than the checkout."""
    archived = set(archived_paths())
    missing = {}
    for tree in DEPLOYED_TREES:
        for src in sorted((REPO / tree).rglob("*")):
            if not src.is_file() or src.is_symlink():
                continue
            try:
                text = src.read_text(errors="ignore")
            except OSError:
                continue
            for name in tree_relative_dirs(text):
                if name in ABSENT_ON_PURPOSE:
                    continue
                if (REPO / name).is_dir() and not _covered(name, archived):
                    missing.setdefault(name, []).append(
                        str(src.relative_to(REPO)))
    assert not missing, (
        "these repo directories are read by deployed code but not archived into "
        f"the deploy: {missing}")


def test_every_deliberate_absence_names_a_reason():
    """An exception with no argument is how this check gets hollowed out. A name
    can be added to `ABSENT_ON_PURPOSE` in a moment; requiring it to carry why
    makes the next person state a case rather than silence a failure."""
    for name, reason in ABSENT_ON_PURPOSE.items():
        assert len(reason) > 80, f"{name} is excluded without a real reason"
        assert (REPO / name).exists(), (
            f"{name} is excluded but does not exist — a stale exception hides "
            f"nothing and will outlive whatever it was for")
