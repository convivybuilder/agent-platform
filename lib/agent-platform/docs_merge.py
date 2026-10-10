"""`agentctl docs merge` — an agent merge of a reviewed documentation PR, once
the adopter's policy permits it (issue_01M3SGQ4QTQ8FB0DN0X9G54ZXP, the epic
"What an adopter tunes ships into their Reference Room",
issue_01M3CDNGTQ8RET5SMQC3XSM8YJ, specification approved by the operator 2026-09-25).

`agent_policy.py` has parsed `agents_may_merge_documents` since it was added,
and nothing read it: `kb_merge.py`'s `agentctl kb merge` covers only Library
`kb/` PRs in the knowledge repository. This module is the reader for that key,
and it merges in ANY repository of the install a reviewed PR is open in — not
just the knowledge repository — once every condition below holds. On
Convivy's own machine the policy key is false, so this changes nothing here
until an adopter sets it.

A merge is refused unless ALL of these hold at the moment it is made, and
`refusals()` names every one that fails, not just the first:

- the agent policy (`agent_policy`), read at the freshly fetched
  `refs/remotes/origin/main` of the knowledge checkout — never the PR's own
  head — sets `agents_may_merge_documents` to true;
- `agentctl review-status` is PASS on the PR's CURRENT head, computed
  in-process through `_review_status_decision` (the function `agentctl
  review-status` itself is a thin printer over, since the PR's own
  changelog), never by running `review-status` as a subprocess and parsing
  its text — unlike `kb_merge.cmd_kb_merge`, which subprocesses it because
  its caller (`bin/agentctl`) already pays that cost for every other kb
  command. `docs merge`'s caller has the decision function at hand, so it
  reads the structured result directly;
- no review role ran more rounds than the policy's `review_rounds_per_role`,
  counted from the same stamps `review-status` just authenticated
  (`kb_merge.rounds_by_role`, reused rather than re-implemented — a second
  counter risks drifting from the one `kb merge` already keeps in step with
  the cap the policy defines);
- every changed file, on both sides of the diff (`kb_merge.Change`,
  `kb_merge.changes_between` — the same git-diff-based reader, generalized
  from the knowledge checkout to any repository of the install, since paths,
  modes and blob bytes matter and the GitHub files API gives neither the
  modes nor a rename's two sides), classifies as `agent_policy.classify`'s
  `document` kind on every present side: inside a top-level `docs/`
  directory, no front matter, not an instruction. A file that cannot be
  classified refuses, naming it, same as `kb merge`;
- the PR is open, targets `main`, its head is not on a fork
  (`isCrossRepository`) and has not moved between the file check and the
  review check; the merge call itself pins the head it checked, so GitHub
  refuses if a push lands between the last check and the merge (same pattern
  `kb_merge.merge_pr` uses). The fork check matters more here than in
  `kb_merge.refusals`, where it is largely redundant with the author-identity
  check: this module has no author-identity check at all (see below), so
  origin is the only thing telling an internal branch from an external fork;
- the repository named by `--repo` is one this install's configuration
  (`load_config()`, below) names as documents-mergeable.

**THE CREDENTIAL IS ITS OWN CONFIGURATION FILE, NOT `library-app.json`.**
`kb_merge.LibraryConfig` is shaped for one repository and one PR-author
identity check (`bot_login`/`bot_user_id`, since `agentctl kb propose` is the
only opener of a Library PR) — neither fits here. `docs merge` acts across
however many repositories the adopter names, on a PR opened by anyone whose
review passed, so `DocsMergeConfig` (below) holds a `repos` LIST instead of
one `repo`, and carries no author check. Folding this into
`library-app.json` would give every reader of that file — including
anything dispatched with only the Library's own narrow purpose in mind — a
config shape that also grants cross-repository merge rights, which is
exactly the kind of widening platform/decisions/0056's "the conditions are
the control, not the credential" argues against concentrating. A second file,
read fresh on every call exactly as `kb_merge.load_config` reads
`library-app.json` (never cached, no Convivy value as a fallback — an
adopter's install either configures this or `docs merge` refuses outright),
keeps the two exceptions — "the Library merges its own kb/ PRs" and
"agents may merge documentation once reviewed" — independently revocable.

**THE INSTALLATION IS CHECKED LIVE, THE SAME NARROWING `kb_merge.
knowledge_installation` APPLIES, GENERALIZED FROM ONE REPOSITORY TO THE
CONFIGURED SET.** `installation_for` refuses unless the App's one
installation is owned by the configured owner and reaches EXACTLY
`config.repos` — no more, no less — so a widened installation (the App
added to a repository nobody put in `docs-merge-app.json`) stops the merge
by name rather than silently reaching further than the adopter intended.
Each individual merge call then mints a token scoped to the ONE repository
it is about to act on (`merge_token`), narrower still than the
installation's full configured reach — least privilege per call, the same
argument `kb_merge.installation_token` makes for the Library's single-repo
token.

Reading the knowledge repository's policy and the target repository's
changed files use two different credentials, on purpose. The policy read
goes through the SAME path `agentctl kb merge` already uses
(`bin/agentctl`'s `_kb_library_git`, read-only) — the operator is retiring his
personal credentials from the machine, and the knowledge repository's reads
have already moved off them. The target repository's diff is read with
whatever `git` already has configured for that repository's own primary
checkout (ambient credentials) — that migration has not reached the
product/platform repositories yet, and `docs merge` does not get ahead of it
here; only the merge call itself (the write that actually lands the PR)
goes through this module's own App credential, never a personal one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import agent_policy as AP
import builder_root as BR
import github_app as GA
import kb_merge as KM
from github_app import Unreadable

BASE = "main"

CONFIG_PATH = BR.real_account_home() / ".config" / "convivy" / "docs-merge-app.json"


@dataclass(frozen=True)
class DocsMergeConfig:
    """This install's documents-merge identity, as `docs-merge-app.json` holds
    it. `owner` is derived from `repos` rather than stored a second time."""
    repos: tuple[str, ...]
    owner: str
    app_id: str
    key_path: Path


def _require_str(data: dict, field_name: str, path: Path) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise Unreadable(f"{path}: {field_name!r} must be a non-empty string, got {value!r}")
    return value


def load_config(path: Path | None = None) -> DocsMergeConfig:
    """This install's documents-merge identity, read fresh from `path`
    (default `CONFIG_PATH`) on every call — never cached, so an edit reaches
    the very next `docs merge` with no restart, the same contract
    `kb_merge.load_config` holds for `library-app.json`.

    `repos` must be a non-empty JSON list of non-empty `owner/name` strings,
    all sharing one owner — a GitHub App installation is scoped to one
    account, so a config naming more than one owner can never be satisfied by
    a single installation and is refused outright rather than accepted and
    left to fail later at the installation check. `owner` is derived from it.
    `app_id` must be a non-empty string. `key_path` is optional and defaults
    to `docs-merge-app.pem` beside `path`.

    A missing file, unreadable JSON, a document that is not a JSON object, a
    missing field or a field of the wrong shape all refuse, naming `path` and
    the field — never falling back to any value of Convivy's own.
    """
    p = CONFIG_PATH if path is None else path
    try:
        raw = p.read_text()
    except FileNotFoundError:
        raise Unreadable(f"{p} does not exist") from None
    except OSError as e:
        raise Unreadable(f"{p} cannot be read: {e}") from None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise Unreadable(f"{p} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise Unreadable(f"{p} does not hold a JSON object")
    repos_raw = data.get("repos")
    if (not isinstance(repos_raw, list) or not repos_raw
            or not all(isinstance(r, str) and r for r in repos_raw)):
        raise Unreadable(f"{p}: 'repos' must be a non-empty list of non-empty strings, "
                         f"got {repos_raw!r}")
    repos = tuple(repos_raw)
    owners = set()
    for r in repos:
        if r.count("/") != 1 or not all(r.split("/")):
            raise Unreadable(f"{p}: 'repos' entries must be 'owner/name', got {r!r}")
        owners.add(r.split("/", 1)[0])
    if len(owners) != 1:
        raise Unreadable(f"{p}: 'repos' names more than one owner ({sorted(owners)!r}); "
                         f"a GitHub App installation is scoped to one account")
    owner = next(iter(owners))
    app_id = _require_str(data, "app_id", p)
    raw_key_path = data.get("key_path")
    if raw_key_path is None:
        key_path = p.parent / "docs-merge-app.pem"
    elif not isinstance(raw_key_path, str) or not raw_key_path:
        raise Unreadable(f"{p}: 'key_path' must be a non-empty string, got {raw_key_path!r}")
    else:
        key_path = Path(raw_key_path)
    return DocsMergeConfig(repos=repos, owner=owner, app_id=app_id, key_path=key_path)


@dataclass
class Facts:
    """Everything `refusals()` reads, gathered by the caller from GitHub, from
    `_review_status_decision` and from git — never from anything the PR
    itself supplies. Mirrors `kb_merge.Facts`'s shape.

    `head_at_diff` is the head the changed-file check read; `head_at_review`
    is the head `_review_status_decision` read. They are read separately (the
    file check first, the review decision after) so a push landing between
    the two is visible as a mismatch rather than silently judging one head's
    files against another head's review.
    """
    repo: str
    number: int
    state: str
    head_at_diff: str
    head_at_review: str
    base_ref: str
    cross_repo: bool
    review_ok: bool
    review_reasons: list = field(default_factory=list)
    stamps: list = field(default_factory=list)
    policy: "AP.Policy | AP.Fallback | None" = None
    changes: "list[KM.Change] | None" = None
    git_problem: str | None = None
    read_error: str | None = None


def refusals(f: Facts, config: DocsMergeConfig | None,
            config_error: str | None = None) -> list[str]:
    """Why this PR may not be merged as a reviewed documentation PR. Empty
    means admit.

    Every condition is checked and reported, rather than stopping at the
    first, so a refusal tells the caller everything to fix at once. Unlike
    `kb_merge.refusals`, `config` is never loaded lazily here: the caller
    (`cmd_docs_merge`) always resolves it once and passes the definitive
    outcome — `config` and `config_error` are mutually exclusive, and a
    missing/malformed config is reported as ONE of potentially several
    simultaneous refusals rather than a reason to stop gathering the rest.
    """
    out: list[str] = []
    if config_error:
        out.append(f"the merge identity could not be read: {config_error}")
    if config is not None:
        allowed = {r.lower() for r in config.repos}
        if (f.repo or "").lower() not in allowed:
            out.append(f"{f.repo} is not one of this install's documents-mergeable "
                       f"repositories ({', '.join(sorted(config.repos))})")
    if f.read_error:
        out.append(f"the PR could not be read: {f.read_error}")
    if (f.state or "").upper() != "OPEN":
        out.append(f"the PR is {f.state or 'in an unknown state'}, not open")
    if f.base_ref and f.base_ref != BASE:
        out.append(f"the PR targets {f.base_ref!r}, not {BASE!r}")
    if f.cross_repo:
        out.append("the PR comes from a fork")
    if f.head_at_diff and f.head_at_review and f.head_at_diff != f.head_at_review:
        out.append(f"the head moved from {f.head_at_diff[:12]} to "
                   f"{f.head_at_review[:12]} between the file check and the review check")

    policy = f.policy
    if isinstance(policy, AP.Fallback):
        out.append(f"the agent policy fell back, so no agent merge is permitted: "
                   f"{policy.reason}")
    elif policy is None:
        out.append("the agent policy was not read")
    elif not policy.agents_may_merge_documents:
        out.append("the policy sets agents_may_merge_documents to false")

    if f.git_problem:
        out.append(f"the repository's changed files could not be read: {f.git_problem}")
    elif f.changes is None:
        out.append("the PR's changed files could not be read")
    else:
        for c in f.changes:
            got = AP.classify(c.path_before, c.text_before, c.path_after, c.text_after,
                              mode_before=c.mode_before, mode_after=c.mode_after)
            if got.unclassifiable:
                out.append(f"{c.label} cannot be classified ({'; '.join(got.reasons)}), "
                           f"so a person merges it")
            elif got.instruction:
                out.append(f"{c.label} is an instruction ({'; '.join(got.reasons)}): "
                           f"agents merge only documents")
            elif not got.document:
                out.append(f"{c.label} is not a document: agents merge only a file "
                           f"inside a top-level docs/ directory with no front matter")

    if not f.review_ok:
        if f.review_reasons:
            out.extend(f"review-status: {r}" for r in f.review_reasons)
        else:
            out.append("`agentctl review-status` is not PASS")

    # Only the document cap is read here, and that is the whole cap. Each file the review
    # floor places positively is refused above: `classify` calls it an instruction (under an
    # instruction directory, a named instruction file, a non-markdown name, a kind in the
    # floor, or a decision naming a role), or it is not a document because it has front
    # matter (a decision under `decisions/` of another kind). So `review_rounds_code` cannot
    # apply here. A tie-break member takes the document cap either way, and `classify` also
    # refuses the tie-break reasons (a non-regular mode, a non-printable character, unreadable
    # front matter), so those are refused here too.
    cap = (f.policy.review_rounds_per_role if isinstance(f.policy, AP.Policy)
           else AP.FALLBACK_REVIEW_ROUNDS_PER_ROLE)
    try:
        for role, n in KM.rounds_by_role(f.stamps, roles=KM.REVIEW_ROLES).items():
            if n > cap:
                out.append(f"{role} ran {n} rounds, past the cap of {cap}: "
                           f"reaching the cap is the operator's call")
    except Unreadable as e:
        out.append(f"the round count cannot be read: {e}")

    return out


# --- what git says about the change (generalized to any repository) --------


def valid_repo_shape(repo: str) -> bool:
    """`repo` reads as a bare `owner/name` GitHub slug. `repo_root()` builds a
    filesystem path from the segment after the slash alone
    (`repo.rsplit("/", 1)[-1]`), so a `name` of `.` or `..` would walk that
    path out of the worktrees root — refused here, before `repo_root()` or
    the config-membership check ever run, and before any checkout is
    touched."""
    if repo.count("/") != 1:
        return False
    owner, name = repo.split("/", 1)
    return bool(owner) and bool(name) and name not in (".", "..")


def repo_root(repo: str) -> Path:
    """The primary checkout of `repo` (`owner/name`) on this machine
    (`worktrees.py`'s layout: `<root>/<name>/`)."""
    return BR.root() / repo.rsplit("/", 1)[-1]


def pr_ref(number: int) -> str:
    """Where a PR's head is fetched to for the diff, and dropped from again
    once read — outside refs/heads and refs/tags, as `kb_merge.pr_ref` keeps
    its own fetches."""
    return f"refs/docs-merge/pr-{number}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """`git -C root args`, bytes out. Same hygiene as `kb_merge._git`: replace
    refs off, every GIT_* variable dropped, so a caller inside a git hook
    reads `root` and not the repository the hook runs in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                              capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unreadable(f"git {args[0]} did not run: {e}") from None


def fetch_pr_and_main(root: Path, number: int) -> str | None:
    """Fetch `root`'s own `origin` (ambient checkout credentials — see the
    module docstring for why this differs from the policy read) for
    `origin/main` and PR `number`'s head. None on success, else why."""
    done = _git(root, "fetch", "--quiet", "origin",
               f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}")
    if done.returncode != 0:
        return f"could not fetch origin/{BASE} ({done.stderr.decode('utf-8', 'replace').strip()})"
    done = _git(root, "fetch", "--quiet", "origin",
               f"+refs/pull/{number}/head:{pr_ref(number)}")
    if done.returncode != 0:
        return f"could not fetch the PR head ({done.stderr.decode('utf-8', 'replace').strip()})"
    return None


def _exact_ref_sha(root: Path, ref: str) -> str:
    done = _git(root, "show-ref", "--verify", "--hash", ref)
    sha = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Unreadable(f"{ref} does not exist in {root}")
    return sha


def drop_pr_ref(root: Path, number: int) -> None:
    """Drop the fetched PR ref once read; objects wait for git's own gc —
    same as `cmd_kb_merge` does for the knowledge checkout."""
    _git(root, "update-ref", "-d", pr_ref(number))


def read_changes(root: Path, number: int, head: str) -> "list[KM.Change]":
    """Every file `head` changes against `root`'s `origin/main`, or
    `Unreadable`. The caller has just fetched both into `root` via
    `fetch_pr_and_main`. The fetched PR head must equal `head` — the head the
    caller checked — so a push between the fetch and this read refuses rather
    than silently diffing a different commit."""
    fetched = _exact_ref_sha(root, pr_ref(number))
    if fetched != head:
        raise Unreadable(f"the fetched PR head is {fetched[:12]}, not the {head[:12]} "
                         f"that was checked")
    base = _exact_ref_sha(root, f"refs/remotes/origin/{BASE}")
    return KM.changes_between(root, base, head)


def read_policy(knowledge_root: Path) -> "AP.Policy | AP.Fallback":
    """The agent policy at `knowledge_root`'s freshly fetched `origin/main` —
    the caller fetches it first (`agentctl kb merge`'s own read path,
    `_kb_library_git`, reused for this read too)."""
    return AP.load(AP.policy_text_at_origin_main(str(knowledge_root)))


# --- the credential ---------------------------------------------------------
#
# `key_problem`, `app_jwt`, `installation_for` and `merge_token` read the
# config (`load_config`) when called, never as a default bound at definition —
# see `kb_merge.py`'s identical note on why, just above its own copies.


def key_problem(path: Path | None = None) -> str | None:
    """Why the App key cannot be used, or None. See `github_app.key_problem`."""
    return GA.key_problem(load_config().key_path if path is None else path,
                          label="the documents-merge App")


def app_jwt(key_path: Path | None = None, app_id: str | None = None,
            now: int | None = None) -> str:
    """A GitHub App JWT for the configured documents-merge App."""
    if key_path is None or app_id is None:
        cfg = load_config()
    kp = cfg.key_path if key_path is None else key_path
    aid = cfg.app_id if app_id is None else app_id
    return GA.app_jwt(kp, aid, now=now)


def installation_for(jwt: str, config: DocsMergeConfig | None = None,
                     opener=urllib.request.urlopen) -> str:
    """The App's one installation, checked to reach EXACTLY `config.repos` and
    nothing else — see the module docstring for why this is checked live on
    every call rather than trusted from the config file alone."""
    cfg = config or load_config()
    inst = GA.single_installation(jwt, cfg.owner, opener=opener)
    inst_id = str(inst.get("id") or "")
    if not inst_id:
        raise Unreadable("the installation carries no id")
    listing_token = GA.installation_token(jwt, inst_id, opener=opener)
    try:
        code, repos = GA.request("GET", f"{GA.API}/installation/repositories?per_page=100",
                                 listing_token, None, opener=opener)
    finally:
        GA.revoke_installation_token(listing_token, opener=opener)
    if code != 200:
        raise Unreadable(f"the installation's repositories could not be read ({code})")
    names = sorted(r.get("full_name", "?") for r in repos.get("repositories") or [])
    wanted = sorted(cfg.repos)
    if repos.get("total_count") != len(wanted) or names != wanted:
        raise Unreadable(f"repo list: the installation reaches {names or 'nothing'} "
                         f"(total {repos.get('total_count')}), not exactly {wanted!r}")
    return inst_id


def merge_token(jwt: str, installation_id: str, repo: str,
                opener=urllib.request.urlopen) -> str:
    """A token for one merge call, scoped to `repo` alone — narrower than the
    installation's full configured reach, minted fresh per invocation. Same
    argument as `kb_merge.installation_token` makes for the Library's
    single-repo token."""
    return GA.installation_token(
        jwt, installation_id, repositories=[repo.rsplit("/", 1)[-1]],
        permissions={"contents": "write", "pull_requests": "write"}, opener=opener)


def merge_pr(number: int, head_sha: str, token: str, repo: str,
            opener=urllib.request.urlopen) -> tuple[bool, str]:
    """Merge with a merge commit. `sha` makes GitHub refuse the merge if the
    head is no longer the one checked."""
    code, payload = GA.request(
        "PUT", f"{GA.API}/repos/{repo}/pulls/{number}/merge", token,
        {"sha": head_sha, "merge_method": "merge"}, opener=opener)
    if code == 200 and payload.get("merged"):
        return True, f"merged as {payload.get('sha', '?')[:12]}"
    return False, f"GitHub refused the merge ({code}): {payload.get('message', 'no message')}"
