"""The one parser and file classifier for the agent policy file.

The policy is `projects/platform/policy/agent-policy.yaml` in the adopter's
knowledge repository. Its format is Shipwright's (convivy/shipwright PR #61,
merge commit 6fe95376450388728e9bfb6b559dca259364c329), and the comments of
that file are the specification: this module implements them rule for rule.
Three readers build on it (the Library's kb merge, the review round cap in
agentctl, the concurrency limits in the registry) so that they cannot drift.

`load()` never returns a blank, a None limit or a permissive default, with one
exception: a readable file that has no `review_rounds_code` line loads that key
as `DECIDED_REVIEW_ROUNDS_CODE` (11), the code cap platform/decisions/0083 sets.
A missing file or any broken rule returns a `Fallback`, which permits no agent
merge and carries the numbers the file names for that case.

`classify()` applies the file's definitions of an instruction, a knowledge
document and a document to BOTH sides of a changed file. A file it cannot
classify is reported as unclassifiable, and every caller treats that as a
person's merge.

The module does no I/O except `policy_text_at_origin_main`.
"""
from __future__ import annotations

import re
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The knowledge repo's audience vocabulary, defined once in lib/knowledge/knowledge.py. Appended,
# not inserted, so that directory's builder_root.py cannot shadow lib/agent-platform's.
sys.path.append(str(Path(__file__).resolve().parent.parent / "knowledge"))
from knowledge import ALLOWED_AUDIENCE, NON_AGENT_AUDIENCE  # noqa: E402

# The agent roles an audience can name: the vocabulary less the people and groups. The vocabulary
# mirrors `ALLOWED_AUDIENCE` in the knowledge repo's scripts/validate.py, which the two files keep equal.
ALLOWED_AGENTS = ALLOWED_AUDIENCE - NON_AGENT_AUDIENCE

POLICY_PATH = "projects/platform/policy/agent-policy.yaml"
POLICY_DIR = "projects/platform/policy"

POLICY_NAME = "platform/agent-policy"
SCHEMA_VERSION = 1

# The numbers the shipped file's header names for a missing, unreadable or
# rule-breaking file. The code cap falls back to the document cap, so a broken file
# never gives a code pull request more rounds than a document one.
FALLBACK_REVIEW_ROUNDS_PER_ROLE = 5
FALLBACK_REVIEW_ROUNDS_CODE = FALLBACK_REVIEW_ROUNDS_PER_ROLE
# The code cap platform/decisions/0083 sets as amended 2026-10-05. `load` uses it for a
# readable file with no `review_rounds_code` line, which is not a broken file.
DECIDED_REVIEW_ROUNDS_CODE = 11
FALLBACK_MAX_CONCURRENT = 16
FALLBACK_MAX_WORKERS_PER_TEAM = 4
FALLBACK_MAX_LIVE_SUPERVISORS = 4

_BOOL_KEYS = (
    "agents_may_merge_knowledge",
    "agents_may_merge_knowledge_instructions",
    "agents_may_merge_documents",
)
_NUMBER_KEYS = (
    "review_rounds_per_role",
    "max_concurrent",
    "max_workers_per_team",
    "max_live_supervisors",
)
# A key the file may omit. `load` validates it like any number key and falls back
# to its FALLBACK_* constant when no line names it.
_OPTIONAL_NUMBER_KEYS = ("review_rounds_code",)
_REQUIRED_KEYS = ("policy", "schema_version") + _BOOL_KEYS + _NUMBER_KEYS
_KEYS = _REQUIRED_KEYS + _OPTIONAL_NUMBER_KEYS


@dataclass(frozen=True)
class Policy:
    """Every key the file defines, typed."""

    policy: str
    schema_version: int
    agents_may_merge_knowledge: bool
    agents_may_merge_knowledge_instructions: bool
    agents_may_merge_documents: bool
    review_rounds_per_role: int
    review_rounds_code: int
    max_concurrent: int
    max_workers_per_team: int
    max_live_supervisors: int


@dataclass(frozen=True)
class Fallback:
    """What a reader uses when the file is missing, unreadable or breaks a rule.

    It has the same fields as `Policy`, so a reader can read either the same
    way; every merge permission is False and every limit is a number the shipped
    file's header names for this case, except the code cap:
    FALLBACK_REVIEW_ROUNDS_CODE is 5 because a broken policy file is the state this
    module fails closed on, and the decision names no separate code fallback.
    `reason` says which rule failed, for the reader to print on stderr.
    """

    reason: str
    policy: str = POLICY_NAME
    schema_version: int = SCHEMA_VERSION
    agents_may_merge_knowledge: bool = False
    agents_may_merge_knowledge_instructions: bool = False
    agents_may_merge_documents: bool = False
    review_rounds_per_role: int = FALLBACK_REVIEW_ROUNDS_PER_ROLE
    review_rounds_code: int = FALLBACK_REVIEW_ROUNDS_CODE
    max_concurrent: int = FALLBACK_MAX_CONCURRENT
    max_workers_per_team: int = FALLBACK_MAX_WORKERS_PER_TEAM
    max_live_supervisors: int = FALLBACK_MAX_LIVE_SUPERVISORS


# A line is exactly `key: value` from the first column with nothing after the
# value. `[0-9]` and not `\d`, which would accept other scripts' digits.
_POLICY_LINE = re.compile(r"([a-z_]+): (\S+)")
_POLICY_NUMBER = re.compile(r"[1-9][0-9]{0,3}")


def load(origin_main_text: str | None) -> Policy | Fallback:
    """Parse the policy file's text as read at the knowledge repo's origin/main.

    `None` means the file is missing. The first rule a line breaks decides the
    reason, which names the line number and the rule.

    A readable file with no `review_rounds_code` line is not a fallback: it takes
    DECIDED_REVIEW_ROUNDS_CODE, because the decision sets that code cap and the file's
    other values stand. A broken file takes FALLBACK_REVIEW_ROUNDS_CODE instead.
    """
    if origin_main_text is None:
        return Fallback("the policy file is missing at origin/main")
    values: dict[str, object] = {}
    for number, line in enumerate(origin_main_text.split("\n"), start=1):
        if not line.strip(" \t") or line.startswith("#"):
            continue
        match = _POLICY_LINE.fullmatch(line)
        if not match:
            return Fallback(f"line {number} is not exactly `key: value` from the first column with nothing after the value: {line!r}")
        key, value = match.groups()
        if key not in _KEYS:
            return Fallback(f"line {number}: {key} is not a key of schema version {SCHEMA_VERSION}")
        if key in values:
            return Fallback(f"line {number}: {key} appears more than once")
        if key == "policy":
            if value != POLICY_NAME:
                return Fallback(f"line {number}: policy is {value!r}, not exactly {POLICY_NAME}")
            values[key] = value
        elif key == "schema_version":
            if value != str(SCHEMA_VERSION):
                return Fallback(f"line {number}: schema_version is {value!r}, not exactly {SCHEMA_VERSION}")
            values[key] = SCHEMA_VERSION
        elif key in _BOOL_KEYS:
            if value not in ("true", "false"):
                return Fallback(f"line {number}: {key} is {value!r}, not exactly true or false")
            values[key] = value == "true"
        else:
            if not _POLICY_NUMBER.fullmatch(value) or int(value) > 1000:
                return Fallback(f"line {number}: {key} is {value!r}, not a whole number from 1 to 1000 in digits with no leading zero")
            values[key] = int(value)
    missing = [key for key in _REQUIRED_KEYS if key not in values]
    if missing:
        return Fallback(f"the policy file has no line for {', '.join(missing)}")
    values.setdefault("review_rounds_code", DECIDED_REVIEW_ROUNDS_CODE)
    return Policy(**values)  # type: ignore[arg-type]


def review_round_cap_sentence(policy: Policy | Fallback) -> str:
    """The review-round cap as the drain briefs state it: both caps and which pull request takes each.

    Every brief that speaks of pull requests of both kinds reads this one sentence, so the
    wording and the rules (a pull request that touches code or an instruction file takes the
    code cap) live in one place. When the code cap exceeds the document cap, the sentence
    also states the progress condition for the rounds past the document cap: no finding in
    the latest round repeats one an earlier round raised (platform/decisions/0083). When the
    two caps are equal, as with the fallback, no round runs past the document cap, so the
    condition is not stated.
    """
    sentence = (f"The review-round cap is {policy.review_rounds_code} rounds for a pull request that changes code "
                f"or any file an agent is instructed to act on, and {policy.review_rounds_per_role} rounds for one "
                f"that changes only documents.")
    if policy.review_rounds_code > policy.review_rounds_per_role:
        sentence += (f" A pull request that changes code or any file an agent is instructed to act on runs rounds "
                     f"past {policy.review_rounds_per_role} only "
                     f"while no finding in the latest round repeats one an earlier round raised.")
    return sentence


def policy_text_at_origin_main(repo_path: str) -> str | None:
    """The policy file's text at `origin/main` of the repository at `repo_path`.

    Reads the committed blob, so a working-tree edit or a pull request's head
    never shows. Returns None when the file is absent there, and also when git
    or the decoding fails, since a reader treats every one of those as missing.

    The ref is fully qualified and resolved with `show-ref --verify`, which
    matches that exact name only. Git's own lookup of a bare `origin/main`
    tries refs/tags/ and refs/heads/ first, and a fully qualified name still
    falls through to `refs/tags/refs/remotes/origin/main` and its siblings when
    the real remote-tracking ref is absent (a remote that renamed its default
    branch, then `fetch --prune`), where a tag pushed to the remote would be
    read. A missing ref therefore returns None. The blob is then read by the
    resolved sha. Replace refs are off and every GIT_* variable is dropped, so
    a caller running inside a git hook (GIT_DIR set) reads `repo_path` and not
    the repository the hook runs in.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base = ["git", "--no-replace-objects", "-C", repo_path]
    try:
        ref = subprocess.run(
            [*base, "show-ref", "--verify", "--hash", "refs/remotes/origin/main"],
            capture_output=True,
            timeout=30,
            env=env,
        )
        sha = ref.stdout.decode("ascii", "replace").strip()
        if ref.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            return None
        done = subprocess.run(
            [*base, "show", f"{sha}:{POLICY_PATH}"],
            capture_output=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def touches_policy(path_before: str | None, path_after: str | None) -> bool:
    """True when either side is under projects/platform/policy/, in any letter case.

    No agent ever merges a pull request for which this is true. The path
    `projects/platform/policy` itself counts, since a file can replace the
    directory.
    """
    for path in (path_before, path_after):
        if path is None:
            continue
        folded = path.casefold()
        if folded == POLICY_DIR or folded.startswith(POLICY_DIR + "/"):
            return True
    return False


_INSTRUCTION_DIRS = frozenset({"agents", "instruction-blocks", "runbooks", "skills", "profiles"})
_INSTRUCTION_KINDS = frozenset({"agent", "skill", "runbook", "instruction-block"})
_INSTRUCTION_NAMES = frozenset({"claude.md", "claude.local.md", "agents.md"})
_REGULAR_MODES = frozenset({"100644", "100755"})

_FM_LINE = re.compile(r"([a-z0-9_-]+): ([^ ](?:.*[^ ])?)")
_FM_KIND = re.compile(r"[a-z0-9-]+")
_FM_AUDIENCE = re.compile(r"\[[a-z0-9-]+(?:, [a-z0-9-]+)*\]")


def _names(segment: str, name: str) -> bool:
    """True when `segment` is `name` in some ASCII letter case.

    Used where a match GRANTS a kind. Casefold would also match look-alikes
    such as U+017F (long s, which folds to s) and a Kelvin sign (folds to k),
    so a directory named `projectſ` would pass as `projects`.
    """
    return segment.isascii() and segment.lower() == name


def under_projects(path: str) -> bool:
    """True when `path`'s first segment names `projects` (ASCII letter case only)
    and at least one segment follows it.

    The one exported test of the `projects/` boundary, so every reader of it
    (`classify` here, `kb_merge._under_projects`) agrees on what `projects`
    matches.
    """
    first, _, rest = path.partition("/")
    return bool(rest) and _names(first, "projects")


@dataclass(frozen=True)
class Classification:
    """What the file's definitions say about one changed file, both sides judged.

    `unclassifiable` wins over everything: the other fields are then False and a
    caller treats the file as a person's merge. `instruction` is true when
    either side is one. `knowledge_document` and `document` are true only when
    every present side qualifies. `reasons` lists what decided it.
    """

    unclassifiable: bool
    instruction: bool
    knowledge_document: bool
    document: bool
    reasons: tuple[str, ...]


def _nonprintable(text: str, *, spaces_ok: bool = False) -> str | None:
    """The first character that is not printable (tab and newline are allowed), or None.
    With `spaces_ok`, a whitespace character (`str.isspace`) is allowed too."""
    return next((c for c in text
                 if not (c.isprintable() or c in "\t\n" or (spaces_ok and c.isspace()))), None)


def _instruction_reasons(path: str, text: str, mode: str) -> tuple[list[str], bool]:
    """Why this side is an instruction, and whether it has front matter at all."""
    reasons: list[str] = []
    parts = path.casefold().split("/")
    name = parts[-1]
    if _INSTRUCTION_DIRS.intersection(parts[:-1]):
        reasons.append(f"{path} sits under an agent-instruction directory")
    if mode not in _REGULAR_MODES:
        reasons.append(f"{path} is not a regular file (mode {mode})")
    if not path.rsplit("/", 1)[-1].lower().endswith(".md"):
        reasons.append(f"{path} does not end in .md")
    if name in _INSTRUCTION_NAMES:
        reasons.append(f"{path} is named {name}")
    bad = _nonprintable(text)
    if bad is not None:
        reasons.append(f"{path} holds the non-printable character {bad!r}")
    has_front_matter = text.startswith("---")
    if has_front_matter:
        keys, problem = _read_front_matter(text)
        if problem:
            reasons.append(f"{path} has front matter the rule cannot read: {problem}")
        else:
            kind = keys.get("kind")
            if kind in _INSTRUCTION_KINDS:
                reasons.append(f"{path} has kind {kind}")
            if kind == "decision" and keys.get("audience") != "[team]":
                reasons.append(f"{path} is a decision whose audience is not exactly [team]")
    return reasons, has_front_matter


def _front_matter_block(text: str) -> tuple[list[str] | None, str | None]:
    """The lines between the front matter's `---` delimiters, or None and the reason they do not open and close."""
    lines = text.split("\n")
    if lines[0] != "---":
        return None, "the opening line is not exactly ---"
    end = next((i for i in range(1, len(lines)) if lines[i].startswith("---")), None)
    if end is None:
        return None, "the front matter never closes"
    if lines[end] != "---":
        return None, "the closing line is not exactly ---"
    return lines[1:end], None


def _read_front_matter(text: str) -> tuple[dict[str, str], str | None]:
    """The front matter's keys and None, or no keys and the reason the rule cannot read it."""
    block, problem = _front_matter_block(text)
    if problem:
        return {}, problem
    keys: dict[str, str] = {}
    for number, line in enumerate(block, start=2):
        if "---" in line:
            return {}, f"line {number} contains ---"
        match = _FM_LINE.fullmatch(line)
        if not match:
            return {}, f"line {number} is not exactly `key: value`"
        key, value = match.groups()
        if key in keys:
            return {}, f"line {number} repeats {key}"
        if key == "kind" and not _FM_KIND.fullmatch(value):
            return {}, f"line {number}: kind has a value the rule cannot read"
        if key == "audience" and not _FM_AUDIENCE.fullmatch(value):
            return {}, f"line {number}: audience has a value the rule cannot read"
        keys[key] = value
    return keys, None


def classify(
    path_before: str | None,
    text_before: str | None,
    path_after: str | None,
    text_after: str | None,
    *,
    mode_before: str | None,
    mode_after: str | None,
) -> Classification:
    """Classify one changed file from both sides.

    A side is absent when its path is None (an addition has no before, a
    deletion no after; a rename has both). A present path with no text cannot
    be read, so the file is unclassifiable. `mode_*` is the git file mode of a
    side and is required for every present side; a present path whose mode is
    None is unclassifiable, since text alone cannot show a symlink. Any mode
    other than a regular file's (100644, 100755), such as a symlink's 120000,
    makes the side an instruction. The mode of an absent side is ignored.

    This does not apply the unconditional ban on `projects/platform/policy/`:
    a file there can come back as a knowledge document, so every reader must
    also call `touches_policy` on the same paths. A caller passes `text=None`
    for bytes it cannot decode strictly; a lossy decode yields U+FFFD, which is
    printable and would defeat the non-printable rule.

    Name comparisons: casefold where a match blocks (instruction directory
    names, CLAUDE.md / CLAUDE.local.md / AGENTS.md, `touches_policy`), since
    widening the fold only blocks more; ASCII lower() where a match permits
    (the `projects` and `docs` first segments, the `.md` ending). `kind` and
    `audience` values are compared exactly.
    """
    sides = [
        (path, text, mode)
        for path, text, mode in (
            (path_before, text_before, mode_before),
            (path_after, text_after, mode_after),
        )
        if path is not None
    ]
    if not sides:
        return Classification(True, False, False, False, ("the file has no path on either side",))
    for path, text, mode in sides:
        if not path:
            return Classification(True, False, False, False, ("a path is empty",))
        if mode is None:
            return Classification(True, False, False, False, (f"{path} has no file mode",))
        if text is None:
            return Classification(True, False, False, False, (f"{path} has no readable text",))
    reasons: list[str] = []
    fronts: list[bool] = []
    for path, text, mode in sides:
        side_reasons, has_front_matter = _instruction_reasons(path, text, mode)
        reasons.extend(side_reasons)
        fronts.append(has_front_matter)
    if reasons:
        return Classification(False, True, False, False, tuple(reasons))
    knowledge = all(under_projects(path) for path, _t, _m in sides)
    first = [path.split("/")[0] for path, _t, _m in sides]
    inside = [len(path.split("/")) > 1 for path, _t, _m in sides]
    document = all(_names(f, "docs") and i for f, i in zip(first, inside)) and not any(fronts)
    return Classification(False, False, knowledge, document, ())


# --- the review floor -------------------------------------------------------------
#
# The instruction block on taking a pull request through review defines the floor: a
# pull request that changes a file an agent is told to act on, or any file that is not
# a document, takes the code cap (`review_rounds_code`). The block on the review round
# cap says a member placed only by the tie-break ("when you cannot tell, it is in")
# takes the document cap, so the code cap needs a positive classification. `review_floor`
# returns that split, and `review_round_cap` applies it, so both gates read one rule.

@dataclass(frozen=True)
class ReviewFloor:
    """Whether a pull request is in the review floor, and what put it there.

    `floor` is True when a changed file is a floor member or its membership cannot be
    told. `positive` is True only when a rule names a member outright, the one reading
    that earns the code cap. `reason` names the file and the rule that decided it, for a
    refusal to print; it is empty when no file is in the floor.
    """

    floor: bool
    positive: bool
    reason: str


def _audience_split(audience: str | None) -> tuple[list[str], list[str]]:
    """The audience entries that name an agent role, and the entries that are in neither the role
    set nor the people and groups. An entry outside the vocabulary cannot be told, so the caller
    takes the tie-break whenever `unknown` is non-empty. `_read_front_matter` has already checked
    the value's shape, so it is a bracketed list of `a, b` entries."""
    if audience is None:
        return [], []
    entries = audience[1:-1].split(", ")
    roles = [e for e in entries if e in ALLOWED_AGENTS]
    unknown = [e for e in entries if e not in ALLOWED_AUDIENCE]
    return roles, unknown


def _raw_kind(text: str) -> str | None:
    """The floor kind on a raw `kind:` line, for front matter the reader cannot parse, or None.

    The reader refuses the whole block, so this reads one line of it, as `_raw_decision_audience`
    reads the `audience:` line. It needs exactly one `kind: ` line, and the value must be a kind
    the instruction block's floor names. Any other value is None, so the caller takes the tie-break.
    """
    block, _problem = _front_matter_block(text)
    if block is None:
        return None
    kinds = [line for line in block if line.startswith("kind:")]
    if len(kinds) != 1 or not kinds[0].startswith("kind: "):
        return None
    value = kinds[0][len("kind: "):]
    return value if value in _INSTRUCTION_KINDS else None


def _raw_decision_audience(path: str, text: str) -> tuple[list[str], list[str]]:
    """`_audience_split` of a decision's raw `audience:` line, for front matter the reader cannot parse.

    The reader refuses the whole block, so this reads one line of it. The side is a decision
    when its path is under `decisions/` or its block has exactly one `kind: decision` line. It
    needs exactly one `audience:` line, in the bracketed shape the reader accepts. Anything else
    names no role, so the caller takes the tie-break.
    """
    block, _problem = _front_matter_block(text)
    if block is None:
        return [], []
    kinds = [line for line in block if line.startswith("kind:")]
    in_decisions = any(_names(seg, "decisions") for seg in path.split("/")[:-1])
    if not (in_decisions or kinds == ["kind: decision"]):
        return [], []
    audiences = [line for line in block if line.startswith("audience:")]
    if len(audiences) != 1 or not audiences[0].startswith("audience: "):
        return [], []
    value = audiences[0][len("audience: "):]
    if not _FM_AUDIENCE.fullmatch(value):
        return [], []
    return _audience_split(value)


def _side_floor(path: str | None, text: str | None, mode: str | None) -> tuple[bool, str] | None:
    """`(positive, reason)` when this side is a floor member or cannot be told, else None.

    A side with no readable path, mode or text is a tie-break. The positive reasons are the ones
    the instruction block's floor names, checked in its order: an instruction directory, a file
    that is not markdown, a named instruction file, a `kind` in the floor (agent, skill, runbook or
    instruction-block), then a decision whose audience names an agent role. A non-regular mode and
    a non-printable character are not in that list, so each is a tie-break; a positive reason on the
    same side still wins. Front matter the rule cannot read is a tie-break too, except for the raw
    reads in `_unreadable_front_matter_floor`. An audience entry that is neither an agent role nor a
    person or group makes its decision a tie-break. Names are compared in ASCII case only, since a
    match here grants the code cap (see `_names`).
    """
    if not path or mode is None or text is None:
        return False, f"{path or 'a changed file'} cannot be read"
    parts = path.split("/")
    leaf = parts[-1]
    if any(_names(seg, d) for seg in parts[:-1] for d in _INSTRUCTION_DIRS):
        return True, f"{path} sits under an agent-instruction directory"
    if not leaf.lower().endswith(".md"):
        return True, f"{path} is not a markdown document"
    if any(_names(leaf, n) for n in _INSTRUCTION_NAMES):
        return True, f"{path} is named {leaf}"
    ties: list[str] = []
    if mode not in _REGULAR_MODES:
        ties.append(f"{path} is not a regular file (mode {mode})")
    # Whitespace is not a floor reason: three documents on origin/main hold U+00A0, and a match
    # here grants the code cap. classify still counts it, through `_instruction_reasons`.
    bad = _nonprintable(text, spaces_ok=True)
    if bad is not None:
        ties.append(f"{path} holds the non-printable character {bad!r}")
    keys: dict[str, str] = {}
    if text.startswith("---"):
        keys, problem = _read_front_matter(text)
        if problem:
            return _unreadable_front_matter_floor(path, text, problem, ties)
    kind = keys.get("kind")
    if kind in _INSTRUCTION_KINDS:
        return True, f"{path} has kind {kind}"
    if kind == "decision" or any(_names(seg, "decisions") for seg in parts[:-1]):
        roles, unknown = _audience_split(keys.get("audience"))
        if unknown:
            return False, (f"{path} is a decision whose audience names {', '.join(unknown)}, which is "
                           f"neither an agent role nor a person or group")
        if roles:
            return True, f"{path} is a decision whose audience names {', '.join(roles)}"
        return False, f"{path} is a decision whose audience names no agent role"
    return (False, ties[0]) if ties else None


def _unreadable_front_matter_floor(path: str, text: str, problem: str, ties: list[str]) -> tuple[bool, str]:
    """`_side_floor`'s verdict for front matter the reader refuses.

    A raw `kind:` line naming a floor kind is positive, and so is a decision whose raw `audience:`
    line names only agent roles. Anything else is a tie-break: the decision's unknown name, a
    non-regular mode or a non-printable character if one holds, or the unreadable front matter.
    """
    raw = _raw_kind(text)
    if raw is not None:
        return True, f"{path} has kind {raw} (front matter the rule cannot read: {problem})"
    roles, unknown = _raw_decision_audience(path, text)
    if unknown:
        return False, (f"{path} is a decision whose audience names {', '.join(unknown)}, which is neither "
                       f"an agent role nor a person or group (front matter the rule cannot read: {problem})")
    if roles:
        return True, (f"{path} is a decision whose audience names {', '.join(roles)} "
                      f"(front matter the rule cannot read: {problem})")
    return False, (ties[0] if ties else f"{path} has front matter the rule cannot read: {problem}")


def review_floor_of(changes) -> ReviewFloor:
    """The review floor for a pull request's changed files, read on both sides.

    `changes` is the list `kb_merge.changes_between` returns: objects with
    `path_before`, `text_before`, `mode_before`, `path_after`, `text_after` and
    `mode_after`, where an absent side has a None path. None means the changed files
    could not be read at all, which is a tie-break, so an unreadable pull request
    never takes the code cap.
    """
    if changes is None:
        return ReviewFloor(True, False, "the pull request's changed files could not be read")
    found: list[tuple[bool, str]] = []
    for c in changes:
        for path, text, mode in ((c.path_before, c.text_before, c.mode_before),
                                 (c.path_after, c.text_after, c.mode_after)):
            if path is None:
                continue
            got = _side_floor(path, text, mode)
            if got is not None:
                found.append(got)
    positive = [reason for pos, reason in found if pos]
    if positive:
        return ReviewFloor(True, True, positive[0])
    if found:
        return ReviewFloor(True, False, found[0][1])
    return ReviewFloor(False, False, "")


def review_round_cap(policy: Policy | Fallback | None, floor: ReviewFloor) -> tuple[int, str]:
    """The round cap a pull request takes, and the clause that says which cap and why.

    A positive floor takes `review_rounds_code`. Everything else, including a tie-break,
    takes `review_rounds_per_role`. With no policy read, both numbers are the fallback's.
    The clause is for a refusal to print, after the number it explains.
    """
    if policy is None:
        per_role, code = FALLBACK_REVIEW_ROUNDS_PER_ROLE, FALLBACK_REVIEW_ROUNDS_CODE
    else:
        per_role, code = policy.review_rounds_per_role, policy.review_rounds_code
    if floor.positive:
        return code, f"review_rounds_code ({code}) applies, because {floor.reason}"
    if floor.floor:
        return per_role, (f"review_rounds_per_role ({per_role}) applies, because {floor.reason}; "
                          f"that is in the floor only by the tie-break, which takes the document cap")
    return per_role, f"review_rounds_per_role ({per_role}) applies, because no changed file is in the review floor"
