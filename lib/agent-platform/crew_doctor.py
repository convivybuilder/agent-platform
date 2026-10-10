"""Is a registered crew actually USABLE, checked by calling each thing rather
than by reading the file that configures it?

WHY THIS EXISTS. The Signals crew was created on 2026-09-15 with a directory, a
GitHub repo, a tmux session and a Deck spec, and was reported done. A day later
it had no skills, `co -r` did nothing but run `/handoff-pickup`, it launched on
Sonnet, and no writing agent could be dispatched to it. The operator found every one of
those by USING it. Each was a registration site the `new-crew` skill never
touched. Placard (2026-09-05) and shipwright (2026-09-06) each missed a
different subset. Three crews in eleven days is a broken process, not three
slips (`issue_01M2NYBBY0FXCDHWWD8XBR9RR3`).

**EVERY CHECK CALLS THE THING. NONE OF THEM GREPS FOR A LITERAL**, and that is
the whole design. Shipwright's interim checker
(`payload/skills/new-crew/check-registration.py`) parses twelve sites with `ast`
— the right posture for files in another crew's repository, which must never be
executed here, and it is what this module keeps for the Library's trees. But
for the Bosun's own code it is the wrong question, and it answers wrongly in
both directions:

  - **False MISSING.** `selector` and `hollis` are absent from `_co_crew_dirs`
    and work perfectly, because they reach `co -r` through a dedicated branch
    instead. A checker reading the array reports two healthy crews broken.
  - **False PRESENT.** A name can sit in every list this repo keeps and the crew
    still fail, because what decides at runtime is a function, not a literal.
    `sig-` was in both prefix tuples before anything confirmed that
    `derive_purpose` actually returned a worktree for `sig-coder`.

So: `derive_purpose` is CALLED and its answer inspected, `co --resolve` is RUN,
`knowledge-ctx` is RUN in the crew's own directory, the Library is QUERIED over
HTTP, and the index is QUERIED. The one place this module reads rather than runs
is the knowledge repository's validators, which belong to the Library and are not
ours to execute.

FOUR STATES, NOT TWO. `PRESENT` and `MISSING` are not enough, and collapsing the
others into `MISSING` is how a health check sends its reader after the wrong
thing. The steward hit `unable to open database file` during the Library's WAL
switch this afternoon and read it as a permissions problem; a doctor reporting
that as a broken crew would have done the same to whoever ran it next. So a
site the checker could not evaluate says `UNKNOWN` and names the reason, and a
site that is deliberately not applicable to this crew says `N/A`. Both `MISSING`
and `UNKNOWN` exit non-zero — an unchecked site is not a passing one — but they
read differently and point somewhere different.

THE PROFILE SITE HAS THREE OUTCOMES OF ITS OWN: served (PRESENT), not served
(MISSING) and the Library unreachable (UNKNOWN). It reads only the Library:
`GET /profiles/{project}/{variant}` reads the Library's own `profiles` table,
whose only writer is `load_crew_registry.py profiles <root>`, run by hand. That
table is the source of truth, so a checkout file is not compared with it. A
machine with no knowledge checkout judges this site the same way as one with it.

**THE PROFILE SITE ASKS THE LIBRARY FOR EVERY CREW, AN ADOPTER'S INCLUDED.**
The site checks `<project>/<variant>` for the project `knowledge-ctx list` names.
`knowledge-ctx use` can render a different profile: a mapped project with no
Library rows, or an unmapped repo under the default fallback, renders
`platform/default`, which this site does not check. For an unmapped repo, `list`
exits 42 and the run stops at the project site, so this site is not reached. An
adopter's crew (`check_registry`'s `unit is None` branch) had a
tree-resolved mode that answered from the checkout instead; it is retired with
the local profile read (issue_01M3NW81FC33701HQE3WV7HFRE).

**A CREW'S PROFILE PROJECT IS NOT ITS CREW ID AND NOT ITS `kb_project`.**
`shipwright`'s `kb_project` is `company`, and its profile is
`projects/shipwright/profiles/daytime.yaml`. `bosun`'s home directory is
`agent-platform` and its profile is `agent-platform/daytime`. The mapping lives
in `projects.EXPLICIT_PROJECT_BY_NAME` (plus the `projects/<name>/` rule), keyed on the home directory's BASENAME, so
this module asks `knowledge-ctx` what the project is instead of guessing from a
registry field. Guessing `kb_project` here reported shipwright against the wrong
profile in an early draft.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import builder_root as BR
import projects
import worktrees as W
from primary_guard import safe_path

PRESENT = "PRESENT"
MISSING = "MISSING"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "N/A"

# A `MISSING` site is a broken crew; an `UNKNOWN` one is a check that could not
# run. Both are failures of the doctor's contract — "this crew works" was not
# established either way — so both exit non-zero. They are separate states
# because they send the reader to different places.
FAILING_STATES = (MISSING, UNKNOWN)

_TIMEOUT = 15
_PROBE_TASK = "crew doctor probe"
DEFAULT_INDEX_DB = Path.home() / ".knowledge-index" / "index.db"


@dataclass(frozen=True)
class Result:
    site: str
    owner: str
    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state not in FAILING_STATES

    def line(self) -> str:
        """The rendered line, with `detail` escaped ONCE, here.

        `detail` IS UNTRUSTED TEXT, and every field of it. It carries another
        process's stderr (`co`'s, `knowledge-ctx`'s), strings the Library serves,
        document ids out of the `profiles` table, and filesystem paths — and this
        line goes straight to a terminal, on a command meant to run unattended
        across every crew. That is the same sink `primary_guard.safe_path` was
        built for in agent-platform#608, over five review rounds, on the same day
        as this module: a path may carry ANSI escapes that erase the very line
        reporting the problem, or bidi controls that make it read as another
        path. `check_profile_served` puts the exception text of a failed Library
        call into its detail, so that text is not vetted by anything downstream
        of us.
        (security-reviewer, PR #609.)

        ESCAPED AT THE ONE CHOKE POINT rather than at each of the four producers,
        for the reason this PR already learned about `bin/co`'s fifteen launch
        sites: a rule applied per producer is a rule the next producer will not
        have. `site` and `owner` are code-controlled constants and are left
        alone, so an escape appearing in either is a bug in this file rather than
        in its input.

        **SO NOTHING IN THIS MODULE BUILDS A DETAIL WITH `!r`.** `repr()` escapes
        control bytes as a SIDE EFFECT of quoting, which makes it a second,
        undeclared escaping mechanism: it compounds with this one, rendering a
        `\\x1b` that `repr` already escaped as `\\\\x1b`, and — worse — it is the
        mechanism a later edit drops while tidying a format string, silently
        removing protection nobody knew was load-bearing. Where a value needs
        quoting so an empty or space-padded one is visible, the quotes are
        WRITTEN ('{value}'), a display choice with no security job.

        The first pass at this fixed only the one site a reviewer named and left
        six others, including the Library-served profile detail, which is the
        sharpest case of all (reviewer, PR #609 round 2). Fixing the named site
        rather than the family is what that round cost.
        `test_no_detail_is_built_with_repr` holds the whole file to it.
        """
        tail = f"  — {safe_path(self.detail)}" if self.detail else ""
        return f"{self.state:<7}  {self.site}  [{self.owner}]{tail}"


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT,
                          check=False, cwd=str(cwd) if cwd else None)


# --- the sites -------------------------------------------------------------

def check_registry(crew: str, *, knowledge_root: Path | str | None = None
                   ) -> tuple[Result, dict | None]:
    """The crew is a registered, active unit — OR one an adopter's own knowledge
    tree resolves with no registry row at all (company decision 0011). Either
    way a failure here makes the rest meaningless rather than merely failing,
    so this is what the rest of `diagnose` derives from.

    `unit` is `None` for BOTH a genuinely unknown name and an adopter's crew;
    they are told apart by `state` (`MISSING` vs `PRESENT`), not by `unit`,
    because an adopter's crew carries no unit to return.
    """
    unit = projects.get(crew)
    if unit is not None:
        life = unit.get("lifecycle")
        if life != "active":
            return Result("registry entry (config/projects.json)", "Bosun", MISSING,
                          f"lifecycle is '{life}', not 'active'"), unit
        return Result("registry entry (config/projects.json)", "Bosun", PRESENT,
                      unit.get("display") or crew), unit
    # No row in config/projects.json. `resolve_project`'s own fallback — a name
    # with a `projects/<name>/` directory in the knowledge tree resolves to
    # itself — is what gives an adopter's crew its crew-scoped instruction
    # blocks with no entry here, so a name that resolves that way is a real
    # crew rather than a broken one, and every later site in this module
    # diagnoses it the same way it diagnoses a registered one.
    if projects.resolve_project(crew, knowledge_root) == crew:
        return Result("registry entry (config/projects.json)", "Bosun", PRESENT,
                      f"projects/{crew}/ in the knowledge tree — no registry row "
                      f"by design"), None
    return Result("registry entry (config/projects.json)", "Bosun", MISSING,
                  f"no unit named '{crew}'"), None


def check_home(crew: str, unit: dict | None) -> tuple[Result, Path | None]:
    """`unit` is `None` for an adopter's crew — `check_registry` found no
    registry row for it — and that absence is this site's own signal to derive
    the home directory the way `agentctl spawn` already does for any repo it is
    handed, `worktrees.desired_path`'s primary-checkout branch, rather than
    through a `home_dir` field this crew's entry does not have.
    """
    if unit is None:
        try:
            home = W.desired_path(crew, None)
        except ValueError as exc:
            return Result("home directory", "Bosun", MISSING, str(exc)), None
    else:
        home = projects.home_path(crew)
        if home is None:
            return Result("home directory", "Bosun", MISSING, "no home_dir in the entry"), None
    if not home.is_dir():
        return Result("home directory", "Bosun", MISSING, f"{home} does not exist"), None
    return Result("home directory", "Bosun", PRESENT, str(home)), home


def check_dispatch(crew: str, unit: dict) -> Result:
    """Each declared repo resolves BACK to this crew.

    `crew_for_repo` is what the dispatcher calls, so this is the decision itself
    rather than the table behind it. A repo resolving to a different crew is
    worse than one resolving to nothing: work lands on someone else's board.
    """
    repos = unit.get("repos") or []
    if not repos:
        return Result("dispatch resolves (crew_for_repo)", "Bosun", NOT_APPLICABLE,
                      "the entry declares no repos")
    # THE REGISTRY KEY IS NOT THE CREW NAME, and conflating them is a false
    # finding rather than a missed one. `convivy-deck` is the project slug; its
    # `crew` field says `deck`, which is what `crew_for_repo` returns and what
    # the dispatcher uses. A draft compared against the key and reported a
    # healthy crew broken. Several projects MAY share one crew (ARCH-426), so
    # the crew name is the right target and the key is not.
    want = unit.get("crew") or crew
    wrong = []
    for repo in repos:
        got = projects.crew_for_repo(repo)
        if got != want:
            wrong.append(f"{repo} -> '{got}', expected '{want}'")
    if wrong:
        return Result("dispatch resolves (crew_for_repo)", "Bosun", MISSING,
                      "; ".join(wrong))
    return Result("dispatch resolves (crew_for_repo)", "Bosun", PRESENT,
                  f"{len(repos)} repo(s)")


def check_agent_prefix(crew: str, unit: dict, *, derive_purpose) -> Result:
    """A write-class agent for this crew gets a WORKTREE, not the primary.

    `derive_purpose` is the dispatcher's own decision function and it is pure —
    unlike `spawn --dry-run`, which creates a worktree, registers a run and
    writes a stub transcript, and would therefore dirty the machine every time a
    scheduled `--all` ran. `None` is the failure: it means the primary checkout,
    which is how an unregistered prefix leaves a coder committing into the crew's
    shared tree and then leaves it on a feature branch.
    """
    prefix = unit.get("agent_prefix")
    if not prefix:
        # Slice 2 puts `agent_prefix` in the registry. Until then a crew that
        # dispatches only bare roles is legitimately prefix-less, and saying
        # PRESENT here would be a check that cannot fail.
        return Result("agent prefix dispatches to a worktree", "Bosun", NOT_APPLICABLE,
                      "the entry declares no agent_prefix")
    bad = []
    for role in ("coder", "reviewer"):
        name = f"{prefix}{role}"
        purpose = derive_purpose(name, _PROBE_TASK, pr=None)
        if not purpose:
            bad.append(f"{name} -> the PRIMARY checkout")
    if bad:
        return Result("agent prefix dispatches to a worktree", "Bosun", MISSING,
                      "; ".join(bad))
    return Result("agent prefix dispatches to a worktree", "Bosun", PRESENT,
                  f"{prefix}coder, {prefix}reviewer")


def check_co_resolves(crew: str, home: Path, *, co_bin: Path) -> Result:
    """`co --resolve` agrees this directory is a crew it can launch.

    RUN, NOT PARSED, and this is the site that proves why. `_co_crew_dirs` does
    not name `selector` or `hollis`, which reach `co -r` by a dedicated branch
    and work — a checker reading the array calls two healthy crews broken. Asking
    `co` what it would do covers both paths and needs no exception list.
    """
    if not co_bin.exists():
        return Result("co -r resolves the crew", "Bosun", UNKNOWN, f"{co_bin} not found")
    try:
        cp = _run([str(co_bin), "--resolve"], cwd=home)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result("co -r resolves the crew", "Bosun", UNKNOWN, f"could not run co: {exc}")
    out = (cp.stdout or "").strip()

    # THE `crew=` LINE IS THE ONLY EVIDENCE, and a zero exit is not a substitute.
    # `co` forwards flags it does not recognise straight through to `claude`, so a
    # `co` without `--resolve` — the deployed one, while this branch is under
    # review — LAUNCHES CLAUDE with `--resolve` and returns whatever claude says.
    # That is how a first draft of this check reported every crew MISSING with the
    # detail "(Did you mean --resume?)", which is claude's error, not co's, and it
    # started a process per crew to produce it. Requiring the line means an older
    # `co` reads as UNKNOWN — a check that could not run — instead of as a broken
    # crew or, worse, a passing one.
    if not out.startswith("crew="):
        detail = (cp.stderr or out or "no output").strip().splitlines()
        last = detail[-1][:120] if detail else "no output"
        if "--resume" in last or "claude" in last.lower():
            return Result("co -r resolves the crew", "Bosun", UNKNOWN,
                          f"{co_bin} has no --resolve (it reached claude): {last}")
        return Result("co -r resolves the crew", "Bosun", MISSING, last)
    if cp.returncode != 0:
        return Result("co -r resolves the crew", "Bosun", MISSING,
                      (cp.stderr or out or "co refused").strip().splitlines()[-1][:160])
    return Result("co -r resolves the crew", "Bosun", PRESENT, out[:160])


def check_kb_admits(crew: str, unit: dict, *, allowed: set[str], id_pattern) -> Result:
    """A document id under this crew's KB slug passes the real validator.

    The pattern is COMPILED and MATCHED rather than searched as text, so a slug
    appearing in a comment does not count. This is the one family of sites read
    rather than run, because the authoritative copies live in the Library's trees
    and executing another crew's code to check our own is the trade this module
    declines.
    """
    slug = unit.get("kb_project")
    if unit.get("kb") == "local" or not slug:
        return Result("KB slug admits a document", "Library", NOT_APPLICABLE,
                      f"kb='{unit.get('kb')}'")
    if slug not in allowed:
        return Result("KB slug admits a document", "Library", MISSING,
                      f"'{slug}' is not in ALLOWED_PROJECTS")
    if not id_pattern.match(f"{slug}/decisions/0001-example"):
        return Result("KB slug admits a document", "Library", MISSING,
                      f"ID_PATTERN rejects '{slug}'")
    return Result("KB slug admits a document", "Library", PRESENT, slug)


def resolve_profile_project(home: Path, *, knowledge_ctx: Path) -> tuple[str | None, str]:
    """What project does `knowledge-ctx` think this directory is?

    ASKED RATHER THAN DERIVED, because the answer is not a registry field.
    `projects.resolve_project` keys on the home directory's basename, so shipwright (whose
    `kb_project` is `company`) resolves to `shipwright`, and bosun (home
    `agent-platform`) resolves to `agent-platform`. An early draft guessed
    `kb_project` and checked shipwright against the wrong profile.
    """
    if not knowledge_ctx.exists():
        return None, f"{knowledge_ctx} not found"
    try:
        cp = _run([str(knowledge_ctx), "list"], cwd=home)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run knowledge-ctx: {exc}"
    if cp.returncode != 0:
        return None, (cp.stderr or cp.stdout or "").strip().splitlines()[-1][:160] or "refused"
    m = re.search(r"profiles for (\S+):", cp.stdout or "")
    if not m:
        return None, "knowledge-ctx named no project"
    return m.group(1), ""


def _served_profile(project: str, variant: str, *, base_url: str, headers: dict,
                    opener: urllib.request.OpenerDirector | None = None
                    ) -> tuple[dict | None, str]:
    """`opener`, when given, replaces the process-wide default — the doctor's
    fallback-token path passes `issue_client.pinned_opener()` here so a
    broader-than-`roster.read` credential still goes only to the address it was
    paired with, with no proxy and no redirect (security-reviewer, PR #708
    round 2). `None` keeps today's behaviour (the global opener `vault_plane`
    installs) for every other caller."""
    url = f"{base_url}/profiles/{project}/{variant}"
    try:
        req = urllib.request.Request(url, headers=headers)
        opened = (opener.open(req, timeout=_TIMEOUT) if opener is not None
                  else urllib.request.urlopen(req, timeout=_TIMEOUT))
        with opened as resp:
            return json.loads(resp.read()), ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "404"
        return None, f"Library returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The Library being down is NOT this crew being broken. Keeping these
        # apart is the whole reason UNKNOWN exists as a state.
        return None, f"Library did not answer: {exc}"


def check_profile_served(project: str, *, variant: str,
                         base_url: str, headers: dict,
                         opener: urllib.request.OpenerDirector | None = None
                         ) -> tuple[Result, list[str]]:
    """The profile the Library SERVES, judged from the Library alone.

    Three outcomes, because collapsing them loses the distinction that matters:

      served        — PRESENT; the answer lists the documents a render resolves
      not served    — MISSING; a 404 for this name. `use` cannot load it and
                      exits 1, unless the default fallback renders
                      platform/default instead
      unreachable   — UNKNOWN; the Library did not answer, which says nothing
                      about the crew

    The checkout is not read. The Library's `profiles` table is written only by
    `load_crew_registry.py profiles <root>`, run by hand, so a checkout file says
    nothing about what is served. `knowledge-ctx use` loads the same route, so a
    404 here is a name `use` cannot load. The fallback can still render
    `platform/default` for that crew, so a MISSING here does not always mean a
    render fails.
    The tree-resolved mode, which answered from the checkout for an adopter's
    crew, is retired with the local profile read (issue_01M3NW81FC33701HQE3WV7HFRE).
    """
    site = f"profile served ({project}/{variant})"

    body, err = _served_profile(project, variant, base_url=base_url, headers=headers,
                                opener=opener)

    if body is None and err == "404":
        return Result(site, "Library", MISSING, "not served by the Library"), []
    if body is None:
        return Result(site, "Library", UNKNOWN, err), []

    doc_ids = [d.get("id") for d in (body.get("documents") or [])
               if isinstance(d, dict) and d.get("id")]
    return Result(site, "Library", PRESENT, f"{body.get('count')} docs"), doc_ids


def check_index_serves(doc_ids: list[str], *, index_db: Path) -> Result:
    """The index holds the documents this crew's profile RESOLVES TO.

    NOT `WHERE id LIKE '<project>/%'`, which is what a draft of this check asked
    and which measures the wrong thing entirely. A profile's documents are drawn
    from across projects — shipwright's daytime profile resolves 141 documents
    whose ids begin `agent-platform/`, `company/`, `platform/` — while the count
    of documents AUTHORED under the crew's own slug is 1 for a new crew and 0 for
    shipwright, whose knowledge lives under `company/`. That draft reported
    shipwright broken and signals nearly so, on a number that was never the
    question.

    The question is whether a session in this crew can actually read what its
    profile promises, so the served id list is checked against the index by
    membership. A missing document is named rather than counted: "3 of 141
    missing" sends someone to look, and naming them says where.
    """
    site = "index serves the profile's documents"
    if not doc_ids:
        return Result(site, "Bosun", MISSING, "the profile resolves no documents")
    if not index_db.exists():
        return Result(site, "Bosun", UNKNOWN, f"{index_db} not found")
    try:
        con = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True, timeout=10)
        try:
            have = {r[0] for r in con.execute("SELECT id FROM docs")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        # The index being locked or mid-rebuild is not a broken crew.
        return Result(site, "Bosun", UNKNOWN, str(exc))
    absent = [d for d in doc_ids if d not in have]
    if absent:
        return Result(site, "Bosun", MISSING,
                      f"{len(absent)} of {len(doc_ids)} not indexed: "
                      + ", ".join(absent[:3]) + (" …" if len(absent) > 3 else ""))
    return Result(site, "Bosun", PRESENT, f"all {len(doc_ids)} resolved documents")


def active_variant(home: Path, default: str = "daytime") -> str:
    """The variant this crew actually runs, read from its own marker.

    NOT A CONSTANT `daytime`. `ea` runs `platform/ea-daytime` and
    `convivy-commercial` runs `company/commercial`; a doctor hardcoding the
    default reported both broken for running the profile they are supposed to
    run. The crew's own marker is the authority on which variant is its, and
    `daytime` is only the fallback for a crew that has never individuated.
    """
    try:
        active = (home / ".claude" / ".active-profile").read_text().strip()
    except OSError:
        return default
    return active.split("/", 1)[1] if "/" in active else default


def check_crew_context(home: Path, project: str, variant: str) -> Result:
    """A session opening in this directory finds agents to read.

    THE SITE IS THE EFFECT, NOT THE MECHANISM, and this one took two tries to
    state. Signals' failure was that it had NO AGENTS — `co -r` did nothing but
    run a skill, because nothing had ever rendered them. But rendering is only
    one of the two legitimate ways a crew gets them: `convivy-deck` and `hollis`
    keep `.claude/agents/` COMMITTED in their own repositories and deliberately
    never individuate, which `bin/co`'s own deck branch says in as many words. A
    draft checked for the `.active-profile` marker and reported both broken,
    along with `svc` and `write-better`.

    So the question asked here is the one that matters to a session — are there
    agents — and the answer names which way this crew gets them, because an
    individuated crew that has silently stopped individuating looks identical to
    a committed one until someone says which it should be. Slice 2's registry
    field makes that decidable; until then, having agents is the check.
    """
    site = "a session finds agents here"
    agents_dir = home / ".claude" / "agents"
    agents = sorted(p.name for p in agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    marker = home / ".claude" / ".active-profile"
    try:
        active = marker.read_text().strip()
    except OSError:
        active = ""

    if not agents:
        return Result(site, "this crew", MISSING,
                      f"no {agents_dir}/*.md — run `knowledge-ctx use {variant} --force`")
    if not active:
        return Result(site, "this crew", PRESENT,
                      f"{len(agents)} committed in the repo, never individuated")
    want = f"{project}/{variant}"
    if active != want:
        return Result(site, "this crew", MISSING,
                      f"{len(agents)} agents, but the active profile is {active}, "
                      f"not the {want} this check resolved")
    return Result(site, "this crew", PRESENT, f"{len(agents)} agents from {active}")


# --- the whole run ---------------------------------------------------------

def _own_bin(name: str, base: Path) -> Path:
    """This checkout's `bin/<name>`, falling back to the machine's root copy.

    THE CHECKOUT FIRST, for the reason `agentctl._kb_indexer` gives: a branch
    must exercise its OWN tooling rather than whatever `install.sh` last
    deployed. It matters more here than usual — `co --resolve` does not exist on
    `main` while this branch is in review, so resolving to the deployed `co`
    would have every check in this module run against a binary that cannot
    answer, and `co` forwards an unknown flag to `claude` rather than refusing
    it.
    """
    # `parents[2]`, not `parent.parent`: this file is lib/agent-platform/<name>.py,
    # one level deeper than `bin/agentctl`, whose own checkout-first lookup uses
    # two. Off by one it silently resolved to the DEPLOYED bin and reported every
    # crew UNKNOWN — the failure looked like a finding about the crews.
    here = Path(__file__).resolve().parents[2] / "bin" / name
    return here if here.exists() else base / "agent-platform" / "bin" / name


def diagnose(crew: str, *, derive_purpose, allowed: set[str], id_pattern,
             variant: str = "daytime", root: Path | None = None,
             base_url: str = "", headers: dict | None = None,
             index_db: Path | None = None,
             opener: urllib.request.OpenerDirector | None = None) -> list[Result]:
    """Every site for one crew, in the order a reader should think about them.

    A failure at the registry or the home directory STOPS the run rather than
    cascading: reporting eight more MISSING lines for a crew that does not exist
    buries the one line that matters.
    """
    base = root or BR.root()
    out: list[Result] = []

    reg, unit = check_registry(crew, knowledge_root=base / "knowledge")
    out.append(reg)
    if not reg.ok:
        return out

    home_res, home = check_home(crew, unit)
    out.append(home_res)
    if home is None:
        return out

    # An adopter's crew (registry unit `None`, `reg` still `PRESENT`) carries
    # none of the fields the sites below read out of a registry entry. It gets
    # the same sites anyway, fed the one fact its absence from the registry
    # does not change: a repo of its own name is what dispatch and the central
    # KB both resolve it by (`crew_for_repo`'s and `resolve_project`'s own
    # identity fallback, company decision 0011).
    if unit is None:
        unit = {"repos": [crew], "crew": crew, "kb_project": crew}

    out.append(check_dispatch(crew, unit))
    out.append(check_agent_prefix(crew, unit, derive_purpose=derive_purpose))
    out.append(check_co_resolves(crew, home, co_bin=_own_bin("co", base)))
    out.append(check_kb_admits(crew, unit, allowed=allowed, id_pattern=id_pattern))

    # A `kb: local` crew keeps ALL its knowledge in its own repository and has no
    # central profile by declaration, so the remaining sites are not sites for it.
    # Reporting them MISSING would be the check disagreeing with the registry
    # about what this crew is — noise that trains a reader to skim the output,
    # which is how a real finding gets skipped. `kb: split` is NOT exempt: it
    # declares a central slice, so failing to resolve one is a real gap.
    if unit.get("kb") == "local":
        out.append(Result("central knowledge sites", "Bosun", NOT_APPLICABLE,
                          "kb='local' — all knowledge lives in the crew's own repo"))
        return out

    kctx = _own_bin("knowledge-ctx", base)
    project, why = resolve_profile_project(home, knowledge_ctx=kctx)
    if project is None:
        out.append(Result("knowledge-ctx resolves the crew's project", "Bosun", UNKNOWN, why))
        return out
    out.append(Result("knowledge-ctx resolves the crew's project", "Bosun", PRESENT, project))

    # The crew's OWN variant, not a constant — see `active_variant`. An explicit
    # `--variant` still wins, so a caller can ask about one a crew does not run.
    use = variant if variant != "daytime" else active_variant(home)

    resolved_index_db = index_db or DEFAULT_INDEX_DB
    prof_res, doc_ids = check_profile_served(
        project, variant=use,
        base_url=base_url, headers=headers or {}, opener=opener)
    out.append(prof_res)
    out.append(check_index_serves(doc_ids, index_db=resolved_index_db))
    out.append(check_crew_context(home, project, use))
    return out


def active_crews() -> list[str]:
    """Every active unit, which is what `--all` walks.

    Read from the registry rather than from a list of crews someone maintains,
    so a crew registered tomorrow is checked tomorrow without an edit here —
    the failure mode this whole epic exists to end.
    """
    return sorted(s for s, u in projects.units().items() if u.get("lifecycle") == "active")
