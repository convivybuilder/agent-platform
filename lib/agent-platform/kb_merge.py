"""The Library's merge of its own knowledge PRs (platform/decisions/0078).

The operator, 2026-09-12: the Library merges a knowledge PR it opened, through its own
tools, once the review cycle has passed, and he leaves that loop. It covers the
repository configured for this installation (`library-app.json`, below) and
nothing else. Every other repository keeps the rule that only the operator merges.

**THE REPOSITORY AND THE APP ARE THIS INSTALLATION'S OWN, NEVER A CONVIVY
DEFAULT IN CODE** (issue_01M3SGPX7AMVR2SKREVPWEPKN0). `load_config()` reads
`~/.config/convivy/library-app.json` (`CONFIG_PATH`, under the real account
home, never `$HOME`, like `KEY_PATH` used to be) fresh on every call: `repo`
(`owner/name`, `owner` derived rather than held twice), `app_id`, `bot_login`,
`bot_user_id`, and an optional `key_path` (defaulting to `library-app.pem`
beside the file). A missing file, unreadable JSON, a missing field or a field
of the wrong shape refuses the merge, naming the file and the field — this
module holds no Convivy value to fall back to, so an adopter's install either
has its own `library-app.json` or `kb merge` refuses outright. On this
machine, that file has to hold Convivy's own values (see the PR that adds
this note for the deploy step) for the Library to keep merging
`convivy/knowledge` PRs the way it does today.

**THE CONDITIONS ARE THE CONTROL, NOT THE CREDENTIAL** (platform/decisions/0056).
`refusals()` decides from facts this module reads itself, never from what a
caller reports, and it answers the same whoever asks. The App credential that
performs the merge is an identity for the history, and on this machine it sits
where every process running as the operator can read it, as the PAT and the SSH
key already do (0051, step 6). So nothing here is safer for holding it; the
merge is admitted only because every condition below holds.

**THE REVIEW CONDITIONS ARE ONLY AS STRONG AS THE STAMPS BEHIND THE GATE.**
`review-status` authenticates each review stamp against its dispatch run's
`meta.json` (ARCH-193), and that store is writable by every process running as
the operator, as 0056 records (issue_01M0R0BFE0CFHJM6XR5NG13PF4, OS-level
isolation, not built). So a forged run record can produce a PASS. That gap is
0056's, not new here, but this module is the first caller that turns a PASS
into a merge with no human after it (security-reviewer, PR #546).

It adds no path while agents still authenticate as the operator's personal account, the
org-admin login 0051 describes: anything able to forge a run record can
already run `gh pr merge` on a knowledge PR itself. **It becomes a new path
when that login comes off the machine (0051, step 6) and agents hold only the
App's rights. Run-record isolation has to land before then.** The same holds
for a head whose ancestry is forged with `git merge -s ours origin/main`
rather than an honest merge; pushing it to a `kb/` branch needs that same
operator-level reach today, but once agents push only through the App's
scoped push token (`contents: write`, no merge rights), pushing the forgery
no longer implies being able to merge directly, and this gate would need its
own check for it before that transition too.

A merge is refused unless ALL of these hold at the moment it is made:

- the PR is in the configured repository, open, based on `main`, and not from a
  fork;
- its author, as GitHub's pulls endpoint reports it, is the configured Library
  App's bot account (`bot_login`, `bot_user_id`, type Bot), read from
  `library-app.json`. A branch name, title, body or label never counts, since an
  agent can write each of those; the branch must also start with `kb/`, the prefix
  only `agentctl kb propose` builds, but that only narrows and never admits;
- the agent policy file (`agent_policy`), read at the freshly fetched
  `refs/remotes/origin/main` of the knowledge checkout, loaded and not a
  `Fallback`. A PR's own head never supplies the policy that gates its merge;
- no changed file, on either side of a rename, is under `projects/platform/policy/`,
  whatever the policy says;
- every changed file is classifiable, read from git (paths, modes and blobs of the
  PR head against `origin/main`) and never from the files API, which gives no modes;
  every one is a knowledge document under `projects/`; and the policy permits it:
  `agents_may_merge_knowledge_instructions` when any file is an instruction,
  `agents_may_merge_knowledge` otherwise;
- the PR head contains the freshly fetched `origin/main`
  (`git merge-base --is-ancestor`), so the required checks the gate read ran on a
  tree that already held everything on `main`. knowledge#699 merged on a PASS
  recorded against a main a day older than the one it landed on, and main then
  failed `validate` for a duplicate decision number (issue_01M3XNHABVJ4RZJPQAQ13A46DW).
  A git failure in that check, or the check never running at all, refuses too,
  never admits;
- every commit carries a `KB-Write:` trailer, the stamp the Library's write
  tools put on each commit (the same regex `kb_audit` reads off `main`);
- `agentctl review-status` is PASS, and the head it judged is still the head —
  OR, for each required role whose latest stamp is not a current APPROVE, THAT
  SAME latest stamp is itself an older APPROVE that carries to the current
  head under the three conditions `carry_blocked` checks (platform/decisions/0078,
  "What the Library may merge" amendment, 2026-10-09): same changed paths,
  identical blob and mode at every one, and an identical main-side original
  for every one. A role's latest stamp reading REQUEST_CHANGES never carries,
  whatever an earlier APPROVE from the same role would have satisfied — there
  is no fallback to a stamp the role itself superseded. `review-status` itself
  is unchanged and still reports FAIL in that case; carrying is a decision
  this module makes on its own stamps and git reads, never a change to what
  `review-status` reports for this repository or any other. Only an APPROVE
  carries, and a carried stamp is not a review run, so it adds no round to
  the count below;
- no review role has run more rounds than its cap, counted from the stamps (see
  `rounds_by_role`), and a count that cannot be read refuses. The cap is the policy's
  `review_rounds_code` for a pull request in the review floor, and its
  `review_rounds_per_role` for any other (`agent_policy.review_round_cap`). A changed
  file the floor places only by the tie-break takes the document cap;
- every `Merge-after: <owner>/<repo>#<n>` line in the body names a PR that has
  MERGED. knowledge#404 and #405 had to wait for #408 on 2026-09-12, and a merge
  on PASS alone would have landed #404 while `validate.py` still disagreed.

The merge itself pins the head it checked (`sha` on the merge call), so GitHub
refuses it if a push lands between the check and the merge.

**THE MERGE IS MADE AS THE CONFIGURED LIBRARY APP, AN APP INSTALLED ON THE
CONFIGURED REPOSITORY ALONE** (the operator, 2026-09-12). Being installed nowhere else is
what keeps the exception narrow: the org ruleset lets this App update `main`,
and the installation is what stops it reaching any other repository's. So the
installation is not stored here but checked on every run
(`knowledge_installation`): exactly one, owned by the configured owner, and
seeing exactly the configured repository. A widened installation stops the
merge with a refusal naming which of the three moved.

The JWT, the installation-token mint and the raw authenticated request are the
shared GitHub App primitives in `github_app.py` (also used by
`bin/pr-state-capture`, which authenticates as a different App for a read-only
capture); this module keeps only what is specific to the knowledge merge —
which installation counts as "the" one, and the merge call itself.

**EVERY WRITE THE LIBRARY MAKES TO KNOWLEDGE RUNS AS THE SAME APP** (`push_credential`,
`push_and_pr_credentials`, `push_env`, `gh_env`, `commit_env`): `agentctl kb apply` pushes
main, `agentctl kb propose` pushes a `kb/` branch and opens or edits its PR, and both
commit as the configured bot account. They authenticated as the operator's personal account (ssh for git,
`gh`'s stored login for the PR) and committed as the operator until they were moved here. The
two commands' fetches use the same URL and token, so retiring the operator's ssh key breaks
neither. The two paths that only read (`kb sync`, `kb checkout`) fetch with
`read_credential`, `contents: read`, so a bug there cannot push. All of it shares this
module's installation check, so the credential cannot reach a repository other than the
configured one, and a command that cannot obtain it refuses.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import agent_policy as AP
import builder_root as BR
import github_app as GA
import review_protocol as RP
from github_app import Unreadable

BASE = "main"
BRANCH_PREFIX = "kb/"
REVIEW_ROLES = ("reviewer", "security-reviewer")

# The type GitHub reports for a GitHub App's own user account. Not adopter
# configuration — every GitHub App's bot user carries this, whichever App it is.
BOT_TYPE = "Bot"

# Where this installation's identity lives: the repository the Library may
# merge into and the GitHub App that does it. Read fresh on every call by
# `load_config()` (below), from the real account home never `$HOME`, as every
# trust anchor is (company/decisions/0008) — never cached, and never defaulted
# to Convivy's own values in code.
CONFIG_PATH = BR.real_account_home() / ".config" / "convivy" / "library-app.json"

# GitHub lists at most 250 commits for a pull request. Past that the trailer
# check cannot see the whole change, so it refuses rather than pass on part.
MAX_PR_COMMITS = 250

MERGE_AFTER_RE = re.compile(
    r"^Merge-after:\s*(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<num>\d+)\s*$",
    re.MULTILINE)


@dataclass(frozen=True)
class LibraryConfig:
    """This installation's identity, as `library-app.json` holds it. `owner` is
    derived from `repo` rather than stored a second time in the file."""
    repo: str
    owner: str
    app_id: str
    bot_login: str
    bot_user_id: int
    key_path: Path


def _require_str(data: dict, field_name: str, path: Path) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise Unreadable(f"{path}: {field_name!r} must be a non-empty string, got {value!r}")
    return value


def load_config(path: Path | None = None) -> LibraryConfig:
    """This installation's identity, read fresh from `path` (default
    `CONFIG_PATH`) on every call — never cached, so an edit reaches the very
    next `kb merge`/`kb apply`/`kb propose` with no restart, the same contract
    `access_assertion.config` and `rollover_policy.load_policy` hold for their
    own config files.

    `repo` must be `owner/name`; `owner` is derived from it. `app_id`,
    `bot_login` must be non-empty strings and `bot_user_id` an integer.
    `key_path` is optional and defaults to `library-app.pem` beside `path`.

    A missing file, unreadable JSON, a document that is not a JSON object, a
    missing field or a field of the wrong shape all refuse, naming `path` and
    the field — never falling back to any value of Convivy's own. An adopter's
    install has no correct default to fall back to, and Convivy's own values
    live in this same file on Convivy's machine (see the module docstring).
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
    repo = _require_str(data, "repo", p)
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise Unreadable(f"{p}: 'repo' must be 'owner/name', got {repo!r}")
    owner = repo.split("/", 1)[0]
    app_id = _require_str(data, "app_id", p)
    bot_login = _require_str(data, "bot_login", p)
    bot_user_id = data.get("bot_user_id")
    if isinstance(bot_user_id, bool) or not isinstance(bot_user_id, int):
        raise Unreadable(f"{p}: 'bot_user_id' must be an integer, got {bot_user_id!r}")
    raw_key_path = data.get("key_path")
    if raw_key_path is None:
        key_path = p.parent / "library-app.pem"
    elif not isinstance(raw_key_path, str) or not raw_key_path:
        raise Unreadable(f"{p}: 'key_path' must be a non-empty string, got {raw_key_path!r}")
    else:
        key_path = Path(raw_key_path)
    return LibraryConfig(repo=repo, owner=owner, app_id=app_id, bot_login=bot_login,
                         bot_user_id=bot_user_id, key_path=key_path)


def trailer_re():
    # The one regex, shared with kb_audit and the knowledge repo's CI check.
    import kb_audit  # noqa: PLC0415 — lazy, like the other kb helpers
    return kb_audit.TRAILER_RE


def merge_after(body: str | None) -> list[tuple[str, int]]:
    """Every companion a PR body names, in order, without duplicates."""
    out: list[tuple[str, int]] = []
    for m in MERGE_AFTER_RE.finditer(body or ""):
        key = (m.group("repo"), int(m.group("num")))
        if key not in out:
            out.append(key)
    return out


def rounds_by_role(stamps, roles=REVIEW_ROLES) -> dict[str, int]:
    """How many review rounds each role ran, counted as distinct run-ids.

    0078 counts distinct reviewed commits, and that undercounts: a re-review at
    an unchanged head, which answering a finding without a code change produces,
    is a round that names the same commit (agent-platform#542's security review
    ran two rounds at one head). Every round is a separate dispatch run, so
    distinct run-ids count what the cap is about and are never fewer than
    distinct commits. `stamps` must already be authenticated, so a run-id names a
    real review run. A stamp with no run-id cannot be placed in a round, and
    raises rather than being skipped, since skipping it would undercount.

    A role's internal-app variant (`platform-reviewer`) counts as that role,
    through the same `_review_base` the gate uses. The gate accepts such a stamp
    as the role's APPROVE, so matching the bare name here would let a PR pass
    `review-status` while its rounds read as zero (reviewer, PR #546).
    """
    runs: dict[str, set[str]] = {r: set() for r in roles}
    for s in stamps:
        role = RP._review_base(s.agent)
        if role not in runs:
            continue
        if not s.run_id:
            raise Unreadable(f"a {s.agent} stamp carries no run-id, so its round "
                             f"cannot be counted")
        runs[role].add(s.run_id)
    return {r: len(v) for r, v in runs.items()}


def missing_trailers(commits: list[dict]) -> list[str]:
    """The shas of commits whose message carries no `KB-Write:` trailer."""
    rx = trailer_re()
    return [c.get("sha", "?") for c in commits
            if not rx.search(((c.get("commit") or {}).get("message")) or "")]


@dataclass(frozen=True)
class Change:
    """One changed file as git reports it, both sides. A side is absent (path,
    mode and text all None) for an addition's before and a deletion's after. A
    side's text is None when its bytes are not strict UTF-8 or are a submodule."""
    path_before: str | None
    mode_before: str | None
    text_before: str | None
    path_after: str | None
    mode_after: str | None
    text_after: str | None

    @property
    def label(self) -> str:
        if self.path_before is not None and self.path_after not in (None, self.path_before):
            return f"{self.path_before} -> {self.path_after}"
        return self.path_after if self.path_after is not None else self.path_before


@dataclass
class Facts:
    """Everything the decision reads, gathered by the caller from GitHub and from
    git. `author` is the pulls endpoint's `{login, id, type}` for the PR's `user`.
    `policy`, `changes`, `base_sha` and `head_contains_base` come from the
    freshly fetched knowledge checkout (`read_git_facts`), or `git_problem`
    says why they could not be read.

    `review_ok`/`review_reasons` are the review gate's own decision (ARCH-184,
    `_review_status_decision` — the same function `agentctl review-status`
    itself prints), computed by the caller BEFORE a carried approval
    (platform/decisions/0078) is considered: `review_ok` true, or `review_ok`
    false with `review_reasons` empty, both read as "not PASS"; a false
    `review_ok` with reasons reports each one, prefixed. The caller sets
    `review_ok` true when every required role's APPROVE is current or
    carries — `refusals()` itself never reads git and knows nothing about
    carrying; that decision is made before `Facts` is built. `gate_head` is
    the PR's head as this command's OWN first read saw it, kept separately
    from `head_sha` (the head the review gate's own, later read confirmed) so
    a push landing between the two is caught as a moved head."""
    repo: str
    number: int
    state: str
    head_ref: str
    base_ref: str
    cross_repo: bool
    head_sha: str
    body: str
    commits: list[dict] | None
    stamps: list = field(default_factory=list)
    review_ok: bool = False
    review_reasons: list = field(default_factory=list)
    gate_head: str = ""
    companions: dict = field(default_factory=dict)  # (repo, num) -> state or None
    author: dict | None = None
    policy: AP.Policy | AP.Fallback | None = None
    changes: list[Change] | None = None
    git_problem: str | None = None
    base_sha: str | None = None
    head_contains_base: bool | None = None
    # `read_stamp_changes`: each stamp's (pinned sha, `reviewed-commit` text) -> the `StampHead`
    # it names. A stamp with no entry reads as an unreadable head, so the cap never reads a head
    # it did not classify.
    stamp_changes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StampHead:
    """The commit a stamp's authenticated run pinned, and that commit's changed files against
    base. `commit` is None when the run pinned no full commit id, so the stamp names no reviewed
    commit. `changes` is None when the commit's changed files cannot be read, which includes a
    commit the knowledge checkout does not hold."""
    commit: str | None
    changes: list[Change] | None


def _under_projects(path: str | None) -> bool:
    # `classify` marks an instruction as not a knowledge document without saying
    # where it sits, and a knowledge instruction must still be under projects/.
    # The test is `agent_policy`'s own, so the two cannot disagree on what
    # `projects` matches (ASCII letter case only).
    if path is None:
        return True
    return AP.under_projects(path)


def _policy_refusals(f: Facts) -> list[str]:
    """The refusals that read the policy file and the PR's changed files."""
    out: list[str] = []
    policy = f.policy
    if isinstance(policy, AP.Fallback):
        out.append(f"the agent policy fell back, so no agent merge is permitted: "
                   f"{policy.reason}")
    elif policy is None:
        out.append("the agent policy was not read")
    if f.changes is None:
        out.append("the PR's changed files could not be read")
        return out
    instruction = False
    for c in f.changes:
        if AP.touches_policy(c.path_before, c.path_after):
            out.append(f"{c.label} is under {AP.POLICY_DIR}/, and no agent merges a "
                       f"change there, whatever the policy says")
        got = AP.classify(c.path_before, c.text_before, c.path_after, c.text_after,
                          mode_before=c.mode_before, mode_after=c.mode_after)
        if got.unclassifiable:
            out.append(f"{c.label} cannot be classified ({'; '.join(got.reasons)}), "
                       f"so a person merges it")
        elif got.instruction:
            if not (_under_projects(c.path_before) and _under_projects(c.path_after)):
                out.append(f"{c.label} is an instruction outside projects/: the Library "
                           f"merges only knowledge documents")
            instruction = True
        elif not got.knowledge_document:
            out.append(f"{c.label} is not a knowledge document under projects/: the "
                       f"Library merges only knowledge documents")
    if isinstance(policy, AP.Policy):
        key = ("agents_may_merge_knowledge_instructions" if instruction
               else "agents_may_merge_knowledge")
        if not getattr(policy, key):
            out.append(f"the policy sets {key} to false"
                       + (", and a changed file is an instruction" if instruction else ""))
    return out


def _stamp_floor(text: str, head: StampHead | None) -> tuple[str, AP.ReviewFloor]:
    """The clause that names a stamped head, and the floor that head's changed files put it in.

    A stamp whose run pinned no full commit names no reviewed commit, and the clause says so by
    the text the stamp carries. A pinned commit whose changed files cannot be read is a tie-break
    as well, and the clause says the files could not be read, which is the only case where that
    wording applies.
    """
    if head is None:
        return f"reviewed-commit {text!r}", AP.ReviewFloor(
            True, False, f"the changed files at reviewed-commit {text!r} could not be read")
    if head.commit is None:
        return f"reviewed-commit {text!r}", AP.ReviewFloor(
            True, False, f"the stamp's reviewed-commit {text!r} names no reviewed commit: its "
                         f"run pinned no full commit id")
    label = f"head {head.commit[:12]}"
    if head.changes is None:
        return label, AP.ReviewFloor(
            True, False, f"the changed files at {label} could not be read")
    return label, AP.review_floor_of(head.changes)


def _round_cap(f: Facts) -> tuple[int, str]:
    """The round cap for this PR: the lowest cap at the current head and at every head a stamp names.

    A later commit can lower the cap and never raise it, so a head that was documents-only,
    or a tie-break, keeps the document cap for the whole PR (platform/decisions/0083, amended
    2026-10-05). A stamp's head is the full commit its authenticated run pinned, never the text
    the stamp carries: `authenticate` accepts that text as a prefix of the pin, so the text can
    be short and still name the pin. A run that pinned no full commit id, and a pinned commit
    whose changed files cannot be read, are tie-breaks and take the document cap. When an
    earlier head sets the cap, the clause names that head's short sha, and the file that would
    have raised the cap at the current head.
    """
    current = AP.review_floor_of(f.changes)
    cap, why = AP.review_round_cap(f.policy, current)
    lowered_at = None
    # Every stamp is read, including one the reader never saw: a missing entry is None.
    for key in dict.fromkeys((s.pinned_sha, s.reviewed_commit) for s in f.stamps):
        head = f.stamp_changes.get(key)
        if head is not None and head.commit is not None and head.commit == f.head_sha:
            continue
        label, floor = _stamp_floor(key[1], head)
        head_cap, head_why = AP.review_round_cap(f.policy, floor)
        if head_cap < cap:
            cap, why, lowered_at = head_cap, f"at {label}, {head_why}", label
    if lowered_at is not None and current.positive:
        why += f"; head {f.head_sha[:12]} would raise it, because {current.reason}"
    return cap, why


def refusals(f: Facts, config: LibraryConfig | None = None) -> list[str]:
    """Why this PR may not be merged by the Library. Empty means admit.

    Every condition is checked and reported, rather than stopping at the first,
    so a refusal tells the Library everything it has to fix. `config` defaults
    to `load_config()`; when that refuses, the two checks that read it (the
    repository and the author) are reported as one refusal naming why, and
    every condition that does not depend on it is still checked.
    """
    out: list[str] = []
    if config is None:
        try:
            config = load_config()
        except Unreadable as e:
            out.append(f"the Library config could not be read: {e}")
            config = None
    if config is not None:
        # GitHub names repositories case-insensitively, so `Convivy/Knowledge` is
        # the same repository. Merging always targets the configured repository
        # itself, never `f.repo`.
        if (f.repo or "").lower() != config.repo.lower():
            out.append(f"{f.repo} is not {config.repo}: the Library merges only "
                       f"knowledge PRs")
    if (f.state or "").upper() != "OPEN":
        out.append(f"the PR is {f.state or 'in an unknown state'}, not open")
    if f.base_ref != BASE:
        out.append(f"the PR targets {f.base_ref!r}, not {BASE!r}")
    if f.cross_repo:
        out.append("the PR comes from a fork")
    if config is not None:
        author = f.author or {}
        if (author.get("login"), author.get("id"), author.get("type")) != (
                config.bot_login, config.bot_user_id, BOT_TYPE):
            out.append(f"the PR was opened by {author.get('login')!r}, not by "
                       f"{config.bot_login}, the account `agentctl kb propose` "
                       f"opens the Library's PRs as")
    if not (f.head_ref or "").startswith(BRANCH_PREFIX):
        out.append(f"the branch {f.head_ref!r} does not start with {BRANCH_PREFIX!r}, "
                   f"so `agentctl kb propose` did not open it")
    if f.git_problem:
        out.append(f"the knowledge checkout could not be read: {f.git_problem}")
    else:
        out.extend(_policy_refusals(f))
        if f.head_contains_base is None:
            out.append(f"the branch's ancestry against {BASE!r} was never read")
        elif f.head_contains_base is False:
            out.append(
                f"the branch does not contain {BASE!r}: {(f.base_sha or '?')[:12]} is not "
                f"an ancestor of {f.head_sha[:12]}, so the branch needs {BASE} merged in "
                f"and its required checks re-run on the new head")
    if f.commits is None:
        out.append("the PR's commits could not be read")
    elif len(f.commits) >= MAX_PR_COMMITS:
        out.append(f"the PR lists {len(f.commits)} commits, GitHub's ceiling, so the "
                   f"trailer check cannot see all of them")
    else:
        missing = missing_trailers(f.commits)
        if missing:
            out.append("commits without a KB-Write trailer: "
                       + ", ".join(s[:12] for s in missing))
    if not f.review_ok:
        if f.review_reasons:
            out.extend(f"`agentctl review-status`: {r}" for r in f.review_reasons)
        else:
            out.append("`agentctl review-status` is not PASS")
    elif f.gate_head != f.head_sha:
        out.append(f"the head moved from {f.gate_head[:12]} to {f.head_sha[:12]} "
                   f"while the review gate ran")
    # A policy that was not read is the file's own fallback, which names 5.
    cap, why_cap = _round_cap(f)
    try:
        for role, n in rounds_by_role(f.stamps).items():
            if n > cap:
                out.append(f"{role} ran {n} rounds, past the cap of {cap}: {why_cap}; "
                           f"reaching the cap is the operator's call")
    except Unreadable as e:
        out.append(f"the round count cannot be read: {e}")
    for key in merge_after(f.body):
        state = f.companions.get(key)
        repo, num = key
        if state is None:
            out.append(f"the companion {repo}#{num} could not be read")
        elif state.upper() != "MERGED":
            out.append(f"the companion {repo}#{num} is {state}, not merged")
    return out


# --- what git says about the change -----------------------------------------------

ORIGIN_MAIN = f"refs/remotes/origin/{BASE}"


def pr_ref(number: int) -> str:
    """Where the PR's head is fetched to in the knowledge checkout, and dropped
    from again once read. Outside refs/heads and refs/tags, so nothing that lists
    branches or tags sees it."""
    return f"refs/kb-merge/pr-{number}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """`git -C root args`, bytes out. Replace refs are off; every GIT_* variable
    is dropped, as in `agent_policy.policy_text_at_origin_main`, so a caller inside
    a git hook reads `root` and not the repository the hook runs in; and grafts
    are neutralised. `.git/info/grafts` rewrites a commit's recorded parents the
    same way `refs/replace` does, and `--no-replace-objects` does not cover it. A
    `-c core.graftFile=/dev/null` override does not take effect on the git this
    machine runs (2.54.0, Apple Git-157, checked live): the default
    `info/grafts` path is still read regardless. `GIT_GRAFT_FILE` (undocumented,
    but honoured by that same build) is pinned to `os.devnull` instead, which does
    neutralise it, checked the same way."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_GRAFT_FILE"] = os.devnull
    try:
        return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args],
                              capture_output=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unreadable(f"git {args[0]} did not run: {e}") from None


def _exact_ref_sha(root: Path, ref: str) -> str:
    """The commit `ref` names, matching that exact ref only. A bare or short name
    resolves a tag or branch of that name first (`agent_policy` records how that
    lets a pushed tag stand in for `origin/main`), so every ref here is fully
    qualified and resolved with `show-ref --verify`."""
    done = _git(root, "show-ref", "--verify", "--hash", ref)
    sha = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Unreadable(f"{ref} does not exist in {root}")
    return sha


def _blob(root: Path, mode: str, sha: str) -> str | None:
    """A side's text, or None when it is not strict UTF-8 or is a submodule
    (`classify` is told None for bytes it cannot decode, since a lossy decode
    yields U+FFFD, which is printable and would defeat its non-printable rule)."""
    if mode == "160000":
        return None
    done = _git(root, "cat-file", "blob", sha)
    if done.returncode != 0:
        raise Unreadable(f"blob {sha[:12]} could not be read: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is reachable from `descendant` (`git merge-base
    --is-ancestor`), or `Unreadable`. Exit 1 is git's own answer for "not an
    ancestor", never a failure; any other non-zero exit is unreadable, so a git
    problem in this check refuses rather than being read as either answer."""
    done = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if done.returncode == 0:
        return True
    if done.returncode == 1:
        return False
    raise Unreadable(f"git merge-base --is-ancestor {ancestor[:12]} {descendant[:12]} "
                     f"failed ({done.returncode}): "
                     f"{done.stderr.decode('utf-8', 'replace').strip()}")


def changes_between(root: Path, base: str, head: str) -> list[Change]:
    """Every file `head` changes against `base`, as git reports it (rename-aware,
    from the merge base, as a merge would land it), both sides, or `Unreadable`.

    Paths and modes come from the raw diff and the bytes from `cat-file`. The
    GitHub files API gives neither the modes nor a rename's two sides. A path that
    is not strict UTF-8 refuses the whole PR."""
    done = _git(root, "diff", "--raw", "-z", "-M", "--no-abbrev", "--no-ext-diff",
                f"{base}...{head}")
    if done.returncode != 0:
        raise Unreadable(f"git diff {base[:12]}...{head[:12]} failed: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    fields = done.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    out: list[Change] = []
    i = 0
    while i < len(fields):
        meta = fields[i].decode("ascii", "replace").split(" ")
        if len(meta) != 5 or not meta[0].startswith(":"):
            raise Unreadable(f"git diff output not understood: {fields[i][:80]!r}")
        mode_a, mode_b, sha_a, sha_b, status = meta[0][1:], meta[1], meta[2], meta[3], meta[4]
        if status[0] not in "AMDTRC":
            raise Unreadable(f"git diff reports the status {status!r}")
        width = 2 if status[0] in "RC" else 1
        raw = fields[i + 1:i + 1 + width]
        if len(raw) != width:
            raise Unreadable("git diff output ended inside a file")
        i += 1 + width
        try:
            paths = [p.decode("utf-8") for p in raw]
        except UnicodeDecodeError:
            raise Unreadable(f"a changed path is not UTF-8: {raw[0][:80]!r}") from None
        path_a, path_b = paths[0], paths[-1]
        before = None if mode_a == "000000" else (path_a, mode_a, _blob(root, mode_a, sha_a))
        after = None if mode_b == "000000" else (path_b, mode_b, _blob(root, mode_b, sha_b))
        out.append(Change(*(before or (None, None, None)), *(after or (None, None, None))))
    return out


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def read_stamp_changes(root: Path, base: str, stamps) -> dict[tuple[str, str], StampHead]:
    """What each stamp names, keyed by its (pinned sha, `reviewed-commit` text). See `StampHead`.

    The commit is the run's pin, and only a full id is taken: a pin that is shorter, or is not
    lower-case hex, names no commit. The pin is never resolved against the checkout, so no stamp
    text, and no short pin, can name a commit in the object store. A full pin's changed files are
    read with `changes_between(base, pin)`; a pin the checkout does not hold reads as unreadable.
    Each stamp is read alone, so one bad stamp does not refuse the whole PR.
    """
    out: dict[tuple[str, str], StampHead] = {}
    for s in stamps:
        key = (s.pinned_sha, s.reviewed_commit)
        if key in out:
            continue
        commit = s.pinned_sha if _FULL_SHA_RE.fullmatch(s.pinned_sha) else None
        changes = None
        if commit is not None:
            try:
                changes = changes_between(root, base, commit)
            except Unreadable:
                # A diff git could not read reads as an unreadable head: a tie-break at the
                # document cap.
                changes = None
        out[key] = StampHead(commit, changes)
    return out


def read_git_facts(root: Path, number: int, head: str):
    """`(policy, changes, base_sha, head_contains_base)` read from the knowledge
    checkout at `root`, or `Unreadable`.

    The caller has just fetched `refs/remotes/origin/main` and `pr_ref(number)`.
    The policy is `agent_policy`'s reading of `origin/main` alone, so no PR's head
    supplies the permission that gates its own merge. The fetched PR head must be
    the head the review gate judged, so a push between the two refuses.
    `head_contains_base` is whether `head` has `base_sha` as an ancestor, so a PR
    whose required checks ran before `main` moved past them is caught here rather
    than admitted on a stale PASS (issue_01M3XNHABVJ4RZJPQAQ13A46DW)."""
    fetched = _exact_ref_sha(root, pr_ref(number))
    if fetched != head:
        raise Unreadable(f"the fetched PR head is {fetched[:12]}, not the {head[:12]} "
                         f"the review gate judged")
    base = _exact_ref_sha(root, ORIGIN_MAIN)
    head_contains_base = _is_ancestor(root, base, head)
    policy = AP.load(AP.policy_text_at_origin_main(str(root)))
    return policy, changes_between(root, base, head), base, head_contains_base


# --- carrying an earlier APPROVE onto a rebuilt head -----------------------------
#
# platform/decisions/0078, the amendment under "What the Library may merge"
# (2026-10-09): a rebuild through `kb propose` is a new commit with no stamps,
# so every merge that moves `main` leaves every other open `kb/` PR needing a
# fresh review round even when the rebuild changed nothing a reviewer judged.
# A role's LATEST authenticated stamp, an APPROVE from an earlier commit S of
# the SAME PR, carries to the current head H when S and H change the same
# files, each one identically, against an identical main-side original — read
# entirely from git object hashes, never from file contents off disk or
# GitHub's files API, and never from the PR's narration. A latest stamp that
# is a REQUEST_CHANGES never carries, and there is no fallback to an earlier
# stamp the role itself superseded. `review-status` itself is unchanged by
# any of this; the carry is a decision this module makes on its own, after
# `review-status` has already said FAIL.


def _commit_exists(root: Path, sha: str) -> bool:
    """Whether `sha` is already a commit object in `root`'s object store. Never
    raises: this is the first check `carry_blocked` makes, specifically so a
    rebuild's earlier, force-pushed-away commit — which GitHub stops serving
    and which this module never fetches on its own — is reported by name
    rather than surfacing as an unexplained `git merge-base` failure."""
    return _git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _merge_base(root: Path, a: str, b: str) -> str:
    """`git merge-base a b`, or `Unreadable` when either commit is missing from
    `root`'s object store or the two share no common history."""
    done = _git(root, "merge-base", a, b)
    sha = done.stdout.decode("ascii", "replace").strip()
    if done.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Unreadable(f"git merge-base {a[:12]} {b[:12]} failed: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return sha


def _changed_path_set(root: Path, base: str, head: str) -> frozenset[str]:
    """The paths `git diff --name-only` reports changed between `base` and
    `head` taken directly (not a three-dot diff — the caller passes a merge
    base as `base` precisely when that is what is meant), with rename
    detection off: a rename is an add and a delete here, so the two sides'
    path sets are compared without git collapsing one into the other first.
    `Unreadable` on a git failure or a path that is not valid UTF-8."""
    done = _git(root, "diff", "--name-only", "-z", "--no-renames", base, head)
    if done.returncode != 0:
        raise Unreadable(f"git diff --name-only {base[:12]} {head[:12]} failed: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    raw = done.stdout.split(b"\0")
    if raw and raw[-1] == b"":
        raw.pop()
    out: list[str] = []
    for p in raw:
        try:
            out.append(p.decode("utf-8"))
        except UnicodeDecodeError:
            raise Unreadable(f"a changed path is not UTF-8: {p[:80]!r}") from None
    return frozenset(out)


def _entry_at(root: Path, commit: str, path: str) -> "tuple[str, str] | None":
    """`(mode, blob-sha)` for `path` as git tracks it at `commit`, or `None`
    when `path` is not tracked there at all — the caller tells "absent" apart
    from "present but different" this way, which `carry_blocked`'s third
    condition needs (an added file has no main-side original on either side).
    `commit` is assumed to already exist (the caller establishes that first);
    any other git failure, or git reporting more than one entry for the one
    path asked for, is `Unreadable`."""
    done = _git(root, "ls-tree", "-z", "-r", "--full-tree", commit, "--", path)
    if done.returncode != 0:
        raise Unreadable(f"git ls-tree {commit[:12]}:{path} failed: "
                         f"{done.stderr.decode('utf-8', 'replace').strip()}")
    entries = [e for e in done.stdout.split(b"\0") if e]
    if not entries:
        return None
    if len(entries) > 1:
        raise Unreadable(f"git ls-tree {commit[:12]}:{path} returned more than one entry")
    meta, _, raw_path = entries[0].partition(b"\t")
    try:
        path_str = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise Unreadable(f"the path at {commit[:12]} is not UTF-8") from None
    if path_str != path:
        raise Unreadable(f"git ls-tree {commit[:12]}:{path} returned {path_str!r}, "
                         f"not {path!r}")
    fields = meta.decode("ascii", "replace").split(" ")
    if len(fields) != 3:
        raise Unreadable(f"git ls-tree output not understood: {meta!r}")
    mode, _obj_type, sha = fields
    return mode, sha


def carry_blocked(root: Path, stamp_commit: str, head: str, base: str) -> "str | None":
    """Why an APPROVE stamped at `stamp_commit` (S, an earlier commit of the
    same PR) does not carry to the PR's current head `head` (H) — the
    platform/decisions/0078 amendment under "What the Library may merge" — or
    `None` iff it does.

    `base` is `origin/main`'s current sha, already fetched fresh by the
    caller (`read_git_facts`'s own `base_sha`). Both merge bases below are
    taken against this one ref rather than against whatever `main` was at the
    time each commit was reviewed: `main` only ever moves forward (the
    ancestry condition this module checks elsewhere), so an earlier main tip
    is always an ancestor of today's, and `git merge-base` against today's
    ref already resolves to the exact tip each commit forked from — S's
    merge base is unaffected by every later move of `base`.

    Three conditions, each returning its own reason naming the mismatch, in
    the order the amendment states them:
      1. S and H change the same set of paths, each against its own merge base.
      2. Every one of those paths is byte- and mode-identical between S and H.
      3. Every one of those paths' main-side original — the blob at the merge
         base — is identical between S's merge base and H's; absent on both
         sides is fine (the path was added fresh by both reviews), but a
         mismatch means `main` itself changed the file between the two.

    `stamp_commit` must already be a commit object in `root`. A rebuild force-
    pushes the branch away, so GitHub stops serving S the instant a later push
    lands, and this never fetches it on its own. That, any merge base that
    cannot be computed, and any other git failure along the way, all raise
    `Unreadable` rather than returning a reason — matching `_is_ancestor`'s
    handling: a git read failing here must refuse the merge outright, never
    be read as "does not carry" and let the caller fall through to another
    candidate stamp.
    """
    if not _commit_exists(root, stamp_commit):
        raise Unreadable(f"{stamp_commit[:12]} is not in {root}'s object store")
    mb_s = _merge_base(root, stamp_commit, base)
    mb_h = _merge_base(root, head, base)
    paths_s = _changed_path_set(root, mb_s, stamp_commit)
    paths_h = _changed_path_set(root, mb_h, head)
    if paths_s != paths_h:
        only_s, only_h = sorted(paths_s - paths_h), sorted(paths_h - paths_s)
        return (f"{stamp_commit[:12]} and {head[:12]} change different paths against "
                f"their own merge bases (only at {stamp_commit[:12]}: {only_s or 'none'}; "
                f"only at {head[:12]}: {only_h or 'none'})")
    for path in sorted(paths_h):
        entry_h, entry_s = _entry_at(root, head, path), _entry_at(root, stamp_commit, path)
        if entry_h != entry_s:
            return (f"{path} differs between {stamp_commit[:12]} ({entry_s}) and "
                    f"{head[:12]} ({entry_h})")
    for path in sorted(paths_h):
        before_h = _entry_at(root, mb_h, path)
        before_s = _entry_at(root, mb_s, path)
        blob_h = before_h[1] if before_h else None
        blob_s = before_s[1] if before_s else None
        if blob_h != blob_s:
            return (f"the main-side original of {path} differs between {mb_s[:12]} "
                    f"(blob {blob_s}) and {mb_h[:12]} (blob {blob_h})")
    return None


def carry_for_role(root: Path, candidates: list, head: str, base: str) -> "tuple[str | None, str]":
    """Whether the role's LATEST authenticated stamp carries to `head`, as
    `(commit, "")`, or `(None, reason)` when it does not.

    `candidates` is every authenticated stamp for one role, in the same
    chronological order (oldest first) `stamps` arrives in from GitHub —
    the same order `review_protocol.evaluate_gate` relies on to pick "the
    last stamp per agent wins" for the gate itself. The last entry of
    `candidates` is this role's current verdict by that same rule, and only
    it is ever considered: a REQUEST_CHANGES that is the role's latest stamp
    never carries, whatever an EARLIER APPROVE would have satisfied in
    `carry_blocked` — carrying from a stamp the role itself superseded is
    the bug agent-platform#840's round 1 found (reviewer, security-reviewer):
    approve S, request changes at a later S2, rebuild back to S's bytes —
    the earlier "try every APPROVE, oldest skipped" form admitted that over
    a live REQUEST_CHANGES. There is no fallback to an older stamp, approved
    or not. Raises `Unreadable` when the latest stamp's conditions cannot be
    read (`carry_blocked`'s own handling) — a git failure refuses outright.
    """
    if not candidates:
        return None, "no stamp from this role to carry"
    latest = candidates[-1]
    if latest.verdict != "APPROVE":
        return None, f"the latest stamp from this role is {latest.verdict}, not APPROVE"
    reason = carry_blocked(root, latest.reviewed_commit, head, base)
    if reason is None:
        return latest.reviewed_commit, ""
    return None, reason


# --- the credential -------------------------------------------------------------


# `key_problem`, `app_jwt` and `push_credential` read the config (`load_config`,
# so `KEY_PATH` before it) when called, never as a default bound at definition: a
# default is fixed at import, so a test that repoints `CONFIG_PATH` (conftest
# does, for every test) would not reach it, and a caller passing no argument
# would still read the real key.


def key_problem(path: Path | None = None) -> str | None:
    """Why the App key cannot be used, or None. See `github_app.key_problem`."""
    return GA.key_problem(load_config().key_path if path is None else path)


def app_jwt(key_path: Path | None = None, app_id: str | None = None,
            now: int | None = None) -> str:
    """A GitHub App JWT for the configured Library App. See `github_app.app_jwt`."""
    if key_path is None or app_id is None:
        cfg = load_config()
    kp = cfg.key_path if key_path is None else key_path
    aid = cfg.app_id if app_id is None else app_id
    return GA.app_jwt(kp, aid, now=now)


def knowledge_installation(jwt: str, owner: str | None = None, repo: str | None = None,
                           opener=urllib.request.urlopen) -> str:
    """The App's one installation, checked to reach the configured repository and
    nothing else. `owner`/`repo` default to the configured values.

    Three conditions, each refused by name so a widened installation is seen for
    what it is rather than as a token error: exactly one installation ("count"),
    owned by `owner` ("owner", both via `github_app.single_installation`), and
    seeing exactly `repo` ("repo list", checked here — it is specific to this
    merge's guarantee, not shared with any other caller). The repository list is
    read with an UNSCOPED token, because a token scoped to `repo` could only ever
    see `repo` and so could not show what else the installation reaches. That
    token is used for the listing alone, and revoked the moment it has listed.
    """
    if owner is None or repo is None:
        cfg = load_config()
    owner = cfg.owner if owner is None else owner
    repo = cfg.repo if repo is None else repo
    inst = GA.single_installation(jwt, owner, opener=opener)
    inst_id = str(inst.get("id") or "")
    if not inst_id:
        raise Unreadable("the installation carries no id")
    listing_token = GA.installation_token(jwt, inst_id, opener=opener)
    try:
        code, repos = GA.request("GET", f"{GA.API}/installation/repositories?per_page=100",
                                 listing_token, None, opener=opener)
    finally:
        # Every permission the installation holds, for a token used once. Revoked
        # as soon as it has listed, because the mint rate is per `kb sync` tick and
        # per `kb checkout` now, no longer per merge or push.
        GA.revoke_installation_token(listing_token, opener=opener)
    if code != 200:
        raise Unreadable(f"the installation's repositories could not be read ({code})")
    names = sorted(r.get("full_name", "?") for r in repos.get("repositories") or [])
    if repos.get("total_count") != 1 or names != [repo]:
        raise Unreadable(f"repo list: the installation reaches {names or 'nothing'} "
                         f"(total {repos.get('total_count')}), not exactly [{repo!r}]")
    return inst_id


def installation_token(jwt: str, installation_id: str, repo: str | None = None,
                       opener=urllib.request.urlopen) -> str:
    """An installation token scoped to the configured repository alone, with only
    the two permissions a merge needs. It expires within the hour."""
    if repo is None:
        repo = load_config().repo
    return GA.installation_token(
        jwt, installation_id, repositories=[repo.split("/")[1]],
        permissions={"contents": "write", "pull_requests": "write"}, opener=opener)


def merge_pr(number: int, head_sha: str, token: str, repo: str | None = None,
             opener=urllib.request.urlopen) -> tuple[bool, str]:
    """Merge with a merge commit, as knowledge PRs have always landed, so every
    commit's KB-Write trailer reaches `main` for kb_audit to read. `sha` makes
    GitHub refuse the merge if the head is no longer the one checked."""
    if repo is None:
        repo = load_config().repo
    code, payload = GA.request(
        "PUT", f"{GA.API}/repos/{repo}/pulls/{number}/merge", token,
        {"sha": head_sha, "merge_method": "merge"}, opener=opener)
    if code == 200 and payload.get("merged"):
        return True, f"merged as {payload.get('sha', '?')[:12]}"
    return False, f"GitHub refused the merge ({code}): {payload.get('message', 'no message')}"


# --- pushing as the App -----------------------------------------------------------


def push_url(repo: str | None = None) -> str:
    """The https URL a push or fetch as the App names, instead of `origin`. An
    installation token authenticates over https; the configured repository's own
    remote is typically ssh, and can never be fallen back to (which would
    authenticate as the operator's personal login and look like success)."""
    return f"https://github.com/{load_config().repo if repo is None else repo}.git"


# `git fetch <url> main` would leave `origin/main` where it was, and every reader
# of the cage compares against that ref. So a fetch over `push_url()` names the
# destination too, and force-updates it as the default `origin` refspec does.
MAIN_REFSPEC = f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}"


def push_token(jwt: str, installation_id: str, repo: str | None = None,
              opener=urllib.request.urlopen) -> str:
    """An installation token for pushing: the configured repository alone,
    `contents: write` only.

    A push needs no `pull_requests` permission, so it does not carry it: a bug
    that tried to open or merge a PR with this token is refused by GitHub even if
    it slips past review. `installation_token` keeps both permissions for the
    merge, which needs them."""
    if repo is None:
        repo = load_config().repo
    return GA.installation_token(
        jwt, installation_id, repositories=[repo.split("/")[1]],
        permissions={"contents": "write"}, opener=opener)


def pr_token(jwt: str, installation_id: str, repo: str | None = None,
            opener=urllib.request.urlopen) -> str:
    """An installation token for the `gh` calls that open, find or edit a PR:
    the configured repository alone, `pull_requests: write` and `contents: read`.

    Not `installation_token`'s shape. `contents: write` from this App can push
    `main` without review (it is on the `update` ruleset's bypass list), and this
    token sits in `gh`'s environment for three calls that write no file. Verified
    live on 2026-09-18: `gh pr list`, `pr create`, `pr edit`, `pr view` and
    `pr close` all succeed on this pair, and a push with it is refused (403)."""
    if repo is None:
        repo = load_config().repo
    return GA.installation_token(
        jwt, installation_id, repositories=[repo.split("/")[1]],
        permissions={"pull_requests": "write", "contents": "read"}, opener=opener)


def read_token(jwt: str, installation_id: str, repo: str | None = None,
              opener=urllib.request.urlopen) -> str:
    """An installation token for a caller that only fetches: the configured
    repository alone, `contents: read`. A bug in `kb sync` or `kb checkout` that
    tried to push is refused by GitHub (403, checked live on 2026-09-18) even if
    review missed it."""
    if repo is None:
        repo = load_config().repo
    return GA.installation_token(
        jwt, installation_id, repositories=[repo.split("/")[1]],
        permissions={"contents": "read"}, opener=opener)


class Unreachable(Unreadable):
    """GitHub could not be reached at all, as against being reached and refusing.

    A caller for which offline is not a failure (`kb sync`, whose timer will retry)
    tells the two apart. Every other caller catches `Unreadable` and refuses."""


def _mint_tokens(minters, key_path: Path | None, opener) -> list[str]:
    """One token per minter, from one Library config read, one key, one JWT and
    one installation check.

    The stages are the merge's own, in order: this installation's own config
    ("Library config" — the repository, the App id and the bot identity
    `load_config` reads; see its docstring for what refuses it), the App key
    ("App key"), the JWT ("App JWT"), the installation check that stops this
    credential reaching any other repository ("installation", carrying
    `knowledge_installation`'s own count/owner/repo-list label), and the mint
    ("token mint"). A GitHub outage is named too. There is no fallback: a caller
    that gets `Unreadable` must refuse, never act with a personal credential or
    unauthenticated.

    `key_path` defaults to the configured key at call time (see the note above
    `key_problem`), so the test suite can repoint `CONFIG_PATH` and have every
    un-stubbed caller refuse rather than mint a real token.
    """
    try:
        try:
            config = load_config()
        except Unreadable as e:
            raise Unreadable(f"Library config: {e}") from None
        key_path = config.key_path if key_path is None else key_path
        problem = key_problem(key_path)
        if problem:
            raise Unreadable(f"App key: {problem}")
        try:
            jwt = app_jwt(key_path, config.app_id)
        except Unreadable as e:
            raise Unreadable(f"App JWT: {e}") from None
        try:
            inst_id = knowledge_installation(jwt, config.owner, config.repo, opener=opener)
        except Unreadable as e:
            raise Unreadable(f"installation: {e}") from None
        try:
            return [mint(jwt, inst_id, config.repo, opener=opener) for mint in minters]
        except Unreadable as e:
            raise Unreadable(f"token mint: {e}") from None
    except OSError as e:
        raise Unreachable(f"GitHub could not be reached: {e}") from None


def read_credential(key_path: Path | None = None, opener=urllib.request.urlopen) -> str:
    """A read-only token for the configured repository, or `Unreadable` naming
    the stage that failed. The stages are those of `_mint_tokens`."""
    return _mint_tokens([read_token], key_path, opener)[0]


def push_credential(key_path: Path | None = None, opener=urllib.request.urlopen) -> str:
    """A push token for the configured repository, or `Unreadable` naming the
    stage that failed. The stages are those of `_mint_tokens`."""
    return _mint_tokens([push_token], key_path, opener)[0]


def push_and_pr_credentials(key_path: Path | None = None,
                            opener=urllib.request.urlopen) -> tuple[str, str]:
    """`(push token, PR token)`, from one installation check, or `Unreadable`.

    `kb propose` pushes a branch and then opens or edits its PR, and a push token
    is refused a PR by design (`push_token`). The PR token (`pr_token`) is minted
    beside it and used for the `gh` calls alone; the push keeps `contents: write`
    and the PR token gets only `contents: read`. Both are minted before either is
    used, so a refusal happens before any remote is touched."""
    push, pr = _mint_tokens([push_token, pr_token], key_path, opener)
    return push, pr


def push_env(token: str, base: dict | None = None, url: str | None = None) -> dict:
    """The environment for one git invocation that pushes to `url` (default
    `push_url()`) as the App.

    **WHERE THE TOKEN LANDS.** It travels in `GIT_CONFIG_*` environment variables,
    which set an `http.<url>.extraHeader` for that one process. It is not in argv
    (`ps` shows every user's arguments on macOS, and git can echo a URL into an
    error), not in the URL, and not in any file: `GIT_CONFIG_*` never writes
    `.git/config`, and nothing is `git remote set-url`ed. **The exposure accepted**
    is that the process environment is readable by processes running as the same
    user (`ps eww`). Every process on this machine runs as the operator, and the
    App key sits at mode 600 in that same account (see the module docstring), so a
    reader able to see this environment could mint a token from the key itself;
    the environment adds no reach. The header is scoped to `url`, so git sends it
    nowhere else.

    **THE HTTPS CREDENTIAL HELPER IS CLOSED OFF, NOT MERELY UNUSED.** This machine
    sets `credential.https://github.com.helper` to `gh auth git-credential`, so an
    https push that lacked a working token would authenticate as the operator's personal account
    with nothing under ~/.ssh implicated. An empty `credential.helper` resets
    every configured helper (the last entry wins, and this one is last), askpass
    is emptied and prompts disabled, so a rejected token fails rather than asking
    anything for another. `GIT_ALLOW_PROTOCOL` limits the invocation to the URL's
    scheme, so an `insteadOf` rewrite of https to ssh in any gitconfig is refused
    rather than followed.
    """
    url = push_url() if url is None else url
    env = dict(os.environ if base is None else base)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    config = [("credential.helper", ""),
              (f"http.{url}.extraHeader", f"Authorization: Basic {basic}")]
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_ALLOW_PROTOCOL"] = urllib.parse.urlparse(url).scheme
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    return env


def gh_env(token: str, base: dict | None = None) -> dict:
    """The environment for one `gh` invocation as the App.

    `GH_TOKEN` outranks `GITHUB_TOKEN` and the stored login in
    `~/.config/gh/hosts.yml`, so with it set `gh` never reads that file for a
    credential. It sits in the process environment, which carries the exposure
    `push_env` accepts. Prompts are disabled so a rejected token fails rather
    than asking for another."""
    env = dict(os.environ if base is None else base)
    env["GH_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def commit_name(config: LibraryConfig | None = None) -> str:
    """The identity the Library's own merge commits already carry in knowledge's
    log (`git log --format='%an <%ae>'`, for example knowledge#557): the
    configured App's bot user."""
    return (config or load_config()).bot_login


def commit_email(config: LibraryConfig | None = None) -> str:
    """The address alongside `commit_name` — GitHub's noreply form for the
    configured App's bot user."""
    cfg = config or load_config()
    return f"{cfg.bot_user_id}+{cfg.bot_login}@users.noreply.github.com"


def commit_env(base: dict | None = None, config: LibraryConfig | None = None) -> dict:
    """The environment for one `git commit` made as the App, author AND committer.

    Set per invocation and never in a gitconfig: `~/.gitconfig` is the operator's
    and governs their own commits in every repository. `GIT_AUTHOR_*` and
    `GIT_COMMITTER_*` outrank `-c user.name`, so an inherited value cannot leave
    either field a person. The requesting session stays on the commit as the
    `holder` of its `KB-Write:` trailer."""
    cfg = config or load_config()
    name, email = commit_name(cfg), commit_email(cfg)
    env = dict(os.environ if base is None else base)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = name
        env[f"GIT_{role}_EMAIL"] = email
    return env


def scrub(text: str, token: str) -> str:
    """`text` with the token, and its Basic-auth encoding, removed.

    The token is never in argv or a URL, so git has no reason to print it. This is
    the backstop for the two callers that print git's stderr on failure, and it
    holds whatever a git trace setting inherited from the environment would emit."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    for secret in (basic, token):
        if secret:
            text = text.replace(secret, "***")
    return text
