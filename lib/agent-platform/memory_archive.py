"""memory_archive — move each co's cold `reference` and `project` memories into
the Archives, and never a `feedback` memory (`platform/decisions/0089`).

Every co session loads its own `~/.claude/projects/<dir>/memory/MEMORY.md`
index in full at startup, and the index only grows. This module is the
decision logic for the scheduled job (`bin/memory-archive`) that keeps that
index bounded: a `reference` or `project` memory that has sat unread for 30
days is written to the Archives, recalled to prove the write landed, and only
then deleted locally along with its `MEMORY.md` line.

A `feedback` memory is never a candidate. A session follows most rules from
the one-line hook in the index without ever opening the file, so the "no Read
in any transcript" signal reads a rule in constant use as cold — the ADR's own
measurement found "quote `agentctl --task` strings" and "label PR URLs as
in-review" both in the cold `feedback` set the same day each was applied from
its index line alone.

WHICH COS. `projects.units()` names every registered project; `projects.
home_path(slug)` gives its home directory. A co's memory folder lives at
`~/.claude/projects/<sanitized home>/memory/`, where "sanitized" is Claude
Code's own convention (every `/` and `.` in the absolute path becomes `-`,
confirmed against this machine's real `~/.claude/projects` entries — see
`bin/transcript-capture`'s `_sanitize`, duplicated here rather than imported
since that file is a `bin/` entry point, not a library). A `~/.claude/projects`
directory that matches no registered crew's home is a scratchpad, a pytest
temp dir, the deploy area, or a worktree, and is never opened.

EXCLUSION IS A NAMED LIST FROM THE OPERATOR CONFIG FILE
(`lib/agent-platform/operator_config.py`'s `excluded_crews` key), NOT THE
REGISTRY'S `private` FLAG: one crew in the real list carries no `private:
true` at all — transcript-capture leaked it that way on 2026-09-12 — and
is not even registered, so a flag a co never gets is not a fence around it.
Another crew is excluded beyond the registry's own black boxes because one
of its memories records measured shares of the operator's own listening,
and `platform/decisions/0006` keeps user-owned data out of the Archives
altogether — the same clause `platform/decisions/0089` invokes for its own
"keep user-owned data out" requirement. Two more are excluded for the same
reason: the first dry run listed one of them's own plan for the operator's
home, and the steward ruled those two crews personal by default and never
to be archived. This job reads no memory content to classify it as user
data; the exclusion list is the whole mechanism — see
`config/operator.example.json` for the key's shape (synthetic values only).

FAIL CLOSED. A missing, unreadable, malformed, or empty `excluded_crews`
list means `run()` archives nothing at all and says so on stderr, rather
than treating the gap as "nothing is excluded" — the inversion that would
turn a config problem into exactly the leak this list exists to prevent.

A subcrew of an excluded crew — a slug shaped `<excluded>--<anything>` — is
excluded with its parent. Exclusion is decided on the REGISTRY SLUG, never on
the `~/.claude/projects` directory name: every such directory name starts
with the sanitized form of `$HOME` (this machine's own home directory), so a
substring test on an excluded crew's slug would exclude every crew on the
machine if that slug were also a substring of $HOME.

COLD, per the ADR: (a) the file's mtime is 30 or more days old, AND (b) the
literal frontmatter line `name: <name>` appears in none of that co's `*.jsonl`
transcripts, subagent transcripts in nested directories included. The name
line, not the filename: every session's transcript carries the whole
MEMORY.md index, so a filename appears in nearly every transcript regardless
of use; the name line inside a memory's own body appears only when something
actually opened the file (a Read, a cat, an injection). Matched with a
right-side boundary so `name: foo` does not match inside `name: foo-bar` —
a JSONL line escapes an embedded newline to the two characters backslash-n, so a
genuine occurrence of `name: foo-bar`'s own line is followed by that escape
or by the closing quote of the JSON string, and `name: foo` followed by `-`
is neither.

THE MOVE, per memory: POST to the Archives (`/api/signals`, via `vault_plane`
— the module that decides which service holds the vaults, same as `bin/
transcript-capture` and `signal_client`), then a RECALL GATE (a search for the
memory's own name must return the written id, and that id's stored content
must equal the file's bytes exactly) before anything local is touched. A miss
at any step — a non-2xx, a timeout, an exception, a search that does not
include the id, a blob that does not match — leaves the file and its
`MEMORY.md` line exactly as they were; nothing here raises out of a single
memory's move to stop the rest.

Once the gate passes, the `MEMORY.md` line is removed BEFORE the file is
unlinked. If the index edit itself fails, nothing has been deleted, so the
memory is reported as kept. If the index edit succeeds and the unlink then
fails, the file is left on disk with no index line — a state the next run
heals on its own: the file is still cold, the write's `dedup_key` returns the
same row, the index edit is a no-op, and the unlink is retried.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import operator_config
import projects
import signal_client
import vault_plane

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML is a pinned requirement
    yaml = None


def _load_excluded_crews() -> "frozenset[str] | None":
    """The `excluded_crews` list from the operator config file, or `None`
    when the file is missing, unreadable, malformed, or the list is empty —
    see the module docstring's FAIL CLOSED paragraph. Read fresh on every
    call rather than cached at import time, so `run()`'s own fail-closed
    check always sees the current file."""
    try:
        crews = operator_config.get("excluded_crews")
    except operator_config.OperatorConfigError:
        return None
    if not isinstance(crews, list) or not crews:
        return None
    return frozenset(str(c) for c in crews)

# A memory is cold once its file has not changed in this many days — the ADR's
# own bound, chosen because 30 days is as far back as local transcripts reach
# (the steward's oldest, measured 2026-09-29, was dated 2026-08-31).
COLD_DAYS = 30

# Only these two memory `type`s are ever candidates. A `feedback` memory is
# never selected, cold or not — see the module docstring.
ARCHIVABLE_TYPES = frozenset({"reference", "project"})

# `POST /signals`'s own body cap for a job like this one (`bin/transcript-
# capture`'s `_MAX_CONTENT_BYTES` is the analogous limit for `content`; the
# brief for this job caps `body` specifically).
MAX_BODY_CHARS = 200_000

DEFAULT_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

_HTTP_TIMEOUT = 30


def is_excluded_slug(slug: str, excluded: "frozenset[str]") -> bool:
    """Whether *slug* is in *excluded*, or a subcrew of one of them
    (`<excluded>--<anything>`). Decided on the slug alone — never on a
    `~/.claude/projects` directory name, which always contains this
    machine's own home directory and so could contain any excluded crew's
    slug as a substring. Decides purely on *excluded*, the set it is given;
    it never reads the operator config itself. `run()` loads that once and
    passes the result down, so a config that fails to load (see
    `_load_excluded_crews`) never reaches this function at all."""
    if slug in excluded:
        return True
    parent, sep, _rest = slug.partition("--")
    return bool(sep) and parent in excluded


def _sanitize_dirname(path: Path) -> str:
    """A directory Path -> the `~/.claude/projects` naming convention: every
    '/' and '.' becomes '-'. Duplicated from `bin/transcript-capture`'s
    `_sanitize` rather than imported — that file is a `bin/` entry point, not
    a library other code is meant to import."""
    return str(path).replace("/", "-").replace(".", "-")


def crew_homes(excluded: "frozenset[str]", *, units: Callable[[], dict] | None = None,
              home_path: Callable[[str], "Path | None"] | None = None
              ) -> dict[str, str]:
    """`{sanitized-dirname: slug}` for every registered, non-excluded crew
    with a home directory, deciding exclusion against *excluded* (see
    `is_excluded_slug`). A `~/.claude/projects` entry not in this map is
    either unregistered (a scratchpad, a worktree, the deploy area) or an
    excluded crew's own home, and either way is never opened."""
    units_fn = units or projects.units
    home_path_fn = home_path or projects.home_path
    out: dict[str, str] = {}
    for slug in units_fn():
        if is_excluded_slug(slug, excluded):
            continue
        try:
            home = home_path_fn(slug)
        except Exception:  # noqa: BLE001 — a malformed entry must not stop the walk
            continue
        if home is None:
            continue
        out[_sanitize_dirname(home)] = slug
    return out


# --- frontmatter -------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def _split_frontmatter(text: str) -> "tuple[dict, str] | None":
    """`(frontmatter, body-without-frontmatter)`, or `None` when *text* has no
    parseable YAML frontmatter block."""
    if yaml is None:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data, text[m.end():].lstrip("\n")


def _memory_type(frontmatter: dict) -> "str | None":
    """The memory's `type`: `metadata.type` (the current shape) or a
    top-level `type` (older files) — see this module's docstring."""
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("type"), str):
        return metadata["type"]
    t = frontmatter.get("type")
    return t if isinstance(t, str) else None


def read_candidate(path: Path) -> "dict | None":
    """One memory file -> a candidate record, or `None` when it is not one:
    `MEMORY.md` itself, unreadable, not valid UTF-8, unparseable frontmatter,
    missing a `name`, or a `type` outside `ARCHIVABLE_TYPES` (a `feedback`
    memory, most commonly)."""
    if path.name == "MEMORY.md":
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    split = _split_frontmatter(text)
    if split is None:
        return None
    frontmatter, body = split
    name = frontmatter.get("name")
    mtype = _memory_type(frontmatter)
    if not isinstance(name, str) or not name or mtype not in ARCHIVABLE_TYPES:
        return None
    description = frontmatter.get("description")
    return {
        "path": path,
        "name": name,
        "type": mtype,
        "description": description if isinstance(description, str) else "",
        "raw": raw,
        "body": body,
    }


def candidates_in(memory_dir: Path) -> list[dict]:
    """Every archivable candidate in *memory_dir*, in filename order."""
    out = []
    for path in sorted(memory_dir.glob("*.md")):
        c = read_candidate(path)
        if c is not None:
            out.append(c)
    return out


def is_mtime_cold(path: Path, now: float, *, days: int = COLD_DAYS) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) >= days * 86400


# --- the read signal: a name line in a transcript -----------------------------


def _name_line_occurs(line: str, name: str) -> bool:
    """Whether the literal text `name: <name>` occurs in *line* with a real
    right-hand boundary: immediately followed by the two characters `\\n`
    (a JSONL-escaped newline) or by `"` (the closing quote of a JSON string).
    Without this, `name: foo` would match inside `name: foo-bar`'s own line —
    a real occurrence of a DIFFERENT memory's name line, not evidence this
    one's body ever reached a transcript."""
    marker = f"name: {name}"
    start = 0
    while True:
        i = line.find(marker, start)
        if i == -1:
            return False
        after = line[i + len(marker):]
        if after.startswith("\\n") or after.startswith('"'):
            return True
        start = i + 1


def find_read_names(project_dir: Path, candidate_names: "set[str]") -> "set[str]":
    """The subset of *candidate_names* whose frontmatter `name:` line appears
    in any `*.jsonl` file anywhere under *project_dir* (subagent transcripts
    in nested directories included). Streams each file rather than loading it
    whole; a cheap substring pre-filter (`"name: " in line`) keeps the common
    case — a line with no name field at all — from paying the per-candidate
    check, mirroring `limit_resume._five_hour_samples`'s own pre-filter."""
    if not candidate_names:
        return set()
    remaining = set(candidate_names)
    found: set[str] = set()
    for jsonl_path in sorted(project_dir.rglob("*.jsonl")):
        if not remaining:
            break
        try:
            with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "name: " not in line:
                        continue
                    for name in list(remaining):
                        if _name_line_occurs(line, name):
                            found.add(name)
                            remaining.discard(name)
                    if not remaining:
                        break
        except OSError:
            continue
    return found


def cold_candidates(memory_dir: Path, project_dir: Path, now: float) -> list[dict]:
    """Every candidate in *memory_dir* that is cold by BOTH conditions."""
    all_candidates = candidates_in(memory_dir)
    mtime_cold = [c for c in all_candidates if is_mtime_cold(c["path"], now)]
    read = find_read_names(project_dir, {c["name"] for c in mtime_cold})
    return [c for c in mtime_cold if c["name"] not in read]


# --- MEMORY.md editing --------------------------------------------------------


def _memory_md_marker(filename: str) -> str:
    return f"]({filename})"


def preview_memory_md_line(memory_md_path: Path, filename: str) -> "str | None":
    """The exact `MEMORY.md` line that links to *filename*, read-only, or
    `None` if there is none. Used by `--dry-run` to preview a removal without
    writing anything."""
    marker = _memory_md_marker(filename)
    try:
        text = memory_md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if marker in line:
            return line
    return None


def remove_memory_md_line(memory_md_path: Path, filename: str) -> bool:
    """Remove every line in `MEMORY.md` that links to *filename*
    (`](<filename>)`), leaving every other line byte-identical, including its
    own line ending. Re-reads the file immediately before editing (per the
    ADR: another writer may have touched it since), and writes it back
    through a temp file plus rename in the same directory — the same atomic
    pattern `limit_resume.save_state` uses. Returns whether anything was
    removed."""
    marker = _memory_md_marker(filename)
    try:
        raw = memory_md_path.read_bytes()
    except OSError:
        return False
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if marker not in ln]
    if len(kept) == len(lines):
        return False
    new_text = "".join(kept)
    tmp_path = memory_md_path.with_name(f".{memory_md_path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(new_text.encode("utf-8"))
    os.replace(tmp_path, memory_md_path)
    return True


# --- the write + recall gate ---------------------------------------------------


def build_payload(slug: str, product: str, candidate: dict) -> dict:
    """The `POST /signals` payload for one candidate. `body` is a clipped,
    human-readable synthesis (name, description, body without frontmatter);
    `content` is the whole file exactly as read, frontmatter included — never
    the same string, so a reader of `body` sees a summary while the Archives
    still hold the byte-exact original."""
    body_text = f"{candidate['name']}\n{candidate['description']}\n\n{candidate['body']}"
    if len(body_text) > MAX_BODY_CHARS:
        body_text = body_text[:MAX_BODY_CHARS]
    digest = hashlib.sha256(candidate["raw"]).hexdigest()
    return {
        "source": "memory-archive",
        "kind": "memory",
        "actor": slug,
        "product": product,
        "dedup_key": f"memory:{slug}:{candidate['name']}:{digest}",
        "body": body_text,
        "content": candidate["raw"].decode("utf-8"),
        "meta": {
            "co": slug,
            "memory_type": candidate["type"],
            "name": candidate["name"],
            "path": candidate["path"].name,
        },
    }


def move_one(slug: str, product: str, candidate: dict, memory_md_path: Path, *,
            poster: Callable[[dict], "str | None"],
            searcher: Callable[[str], "list[dict] | None"],
            blob_getter: Callable[[str], "bytes | None"]) -> "tuple[str, str]":
    """Write, recall-gate, remove the `MEMORY.md` line, then delete the file
    — in that order, so a failure after the index edit never leaves an index
    line pointing at a deleted file. Returns `(status, reason)`; `reason` is
    empty on `"moved"` and names the miss or failure otherwise:

    - `"moved"`: the write, recall gate, index edit and unlink all succeeded.
    - `"kept"`: the write, recall gate, or index edit missed or raised. The
      file and its `MEMORY.md` line are both untouched.
    - `"orphaned"`: the index line was removed but the unlink then raised.
      The file is still on disk with no index line; this heals on the next
      run, which finds the file still cold, gets the same row back from the
      write's `dedup_key`, makes a no-op index edit, and retries the unlink.

    Never raises: any exception from a step is caught and reported the same
    as a miss, so one bad memory cannot stop the run."""
    try:
        payload = build_payload(slug, product, candidate)
        posted_id = poster(payload)
        if not posted_id:
            return "kept", "the Archives write did not return an id"
        results = searcher(candidate["name"])
        if not results or not any(
                isinstance(r, dict) and r.get("id") == posted_id for r in results):
            return "kept", f"a search for '{candidate['name']}' did not include {posted_id}"
        blob = blob_getter(posted_id)
        if blob != candidate["raw"]:
            return "kept", f"the recalled blob for {posted_id} did not match the file's bytes"
    except Exception as exc:  # noqa: BLE001 — a miss, not a run-ending failure
        return "kept", f"{type(exc).__name__}: {exc}"

    try:
        remove_memory_md_line(memory_md_path, candidate["path"].name)
    except Exception as exc:  # noqa: BLE001 — nothing has been deleted yet;
        # report as kept, since the file is still intact.
        return "kept", f"written and recalled, but the index edit failed: {exc}"

    try:
        candidate["path"].unlink()
    except Exception as exc:  # noqa: BLE001 — the index line is already gone;
        # this is neither "kept" (the index no longer names the file) nor
        # "moved" (the file is still on disk) — see the docstring's healing
        # note.
        return "orphaned", f"the index line was removed, but the file delete failed: {exc}"
    return "moved", ""


# --- default I/O: the real Archives calls -------------------------------------


def _default_poster(payload: dict) -> "str | None":
    tok = signal_client.token()
    if not tok:
        return None
    try:
        req = urllib.request.Request(
            vault_plane.url("/api/signals"),
            data=json.dumps(payload).encode("utf-8"),
            headers=vault_plane.headers(token=tok, json_body=True),
            method="POST")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status not in (200, 201):
                return None
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body.get("id") if isinstance(body, dict) else None


def _default_searcher(name: str) -> "list[dict] | None":
    tok = signal_client.token()
    if not tok:
        return None
    try:
        url = vault_plane.url("/api/signals/search") + "?" + urllib.parse.urlencode(
            {"q": name, "limit": 10})
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    rows = body.get("signals")
    return rows if isinstance(rows, list) else None


def _default_blob_getter(signal_id: str) -> "bytes | None":
    """The recalled signal's stored bytes, straight off `GET /api/signals/
    {id}/blob` — no JSON parse, no re-encode. `GET /api/signals/{id}` (no
    `/blob`) has no `content` key at all; the blob route is what actually
    carries the bytes the store holds (`platform/contracts/library-protocol`
    §6: `content` is stored unchanged as UTF-8 bytes)."""
    tok = signal_client.token()
    if not tok:
        return None
    try:
        url = vault_plane.url(
            f"/api/signals/{urllib.parse.quote(signal_id, safe='')}/blob")
        req = urllib.request.Request(url, headers=vault_plane.headers(token=tok))
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError):
        return None


# --- the whole run -------------------------------------------------------------


def _product_for(slug: str) -> str:
    """The `product` field this job stamps on a signal. Not a commercial
    product (ARCH-426's separate dimension) — the Signals/Archives schema's
    `product` column is a generic scope label (a Workshop issue may be scoped
    `commercial`, an Archives signal `company`), and every other poster in
    this repo (`bin/transcript-capture`, `signal_client`) fills it with the
    project/scope slug itself. This job does the same: the crew's own
    registry slug."""
    return slug


def process_crew(slug: str, memory_dir: Path, project_dir: Path, *, now: float,
                 dry_run: bool,
                 poster: Callable[[dict], "str | None"],
                 searcher: Callable[[str], "list[dict] | None"],
                 blob_getter: Callable[[str], "bytes | None"],
                 log: Callable[[str], None]) -> dict:
    """Process one co's memory folder. Returns a per-co tally: `{"candidates":
    n, "cold": n, "moved": n, "kept_on_miss": n, "orphaned": n}` — `orphaned`
    counts a `move_one` outcome where the index line was removed but the
    file's own delete then failed (see `move_one`'s docstring)."""
    all_candidates = candidates_in(memory_dir)
    cold = cold_candidates(memory_dir, project_dir, now)
    tally = {"candidates": len(all_candidates), "cold": len(cold), "moved": 0,
             "kept_on_miss": 0, "orphaned": 0}
    memory_md_path = memory_dir / "MEMORY.md"

    if dry_run:
        for c in cold:
            line = preview_memory_md_line(memory_md_path, c["path"].name)
            log(f"[dry-run] {slug}: would move {c['path']} "
                f"(MEMORY.md line: {line!r})")
        return tally

    product = _product_for(slug)
    for c in cold:
        status, reason = move_one(slug, product, c, memory_md_path,
                                  poster=poster, searcher=searcher,
                                  blob_getter=blob_getter)
        if status == "moved":
            tally["moved"] += 1
            log(f"{slug}: moved {c['name']}")
        elif status == "orphaned":
            tally["orphaned"] += 1
            log(f"{slug}: orphaned {c['name']} ({reason})")
        else:
            tally["kept_on_miss"] += 1
            log(f"{slug}: kept {c['name']} ({reason})")
    return tally


def run(*, now: "float | None" = None,
       claude_projects_dir: "Path | None" = None,
       units: Callable[[], dict] | None = None,
       home_path: Callable[[str], "Path | None"] | None = None,
       poster: Callable[[dict], "str | None"] | None = None,
       searcher: Callable[[str], "list[dict] | None"] | None = None,
       blob_getter: Callable[[str], "bytes | None"] | None = None,
       dry_run: bool = False,
       log: Callable[[str], None] = print) -> dict:
    """Walk every registered, non-excluded crew's memory folder and move its
    cold `reference`/`project` memories into the Archives. Returns `{slug:
    tally}` for every crew that has a memory folder. `--dry-run` (`dry_run=
    True`) computes candidates and cold status and logs what it would do,
    making no network call and writing nothing.

    FAIL CLOSED: if the operator config's `excluded_crews` list cannot be
    read (missing file, unreadable, malformed, or empty), this archives
    nothing at all — never falling through to "nothing is excluded" — and
    says so on stderr before returning an empty summary."""
    excluded = _load_excluded_crews()
    if excluded is None:
        print(f"memory-archive: excluded_crews unavailable from "
              f"{operator_config.config_path()}; archiving nothing", file=sys.stderr)
        return {}

    now = time.time() if now is None else now
    claude_projects_dir = claude_projects_dir or DEFAULT_CLAUDE_PROJECTS
    poster = poster or _default_poster
    searcher = searcher or _default_searcher
    blob_getter = blob_getter or _default_blob_getter

    dirname_to_slug = crew_homes(excluded, units=units, home_path=home_path)

    summary: dict[str, dict] = {}
    if not claude_projects_dir.is_dir():
        return summary

    for project_dir in sorted(p for p in claude_projects_dir.iterdir() if p.is_dir()):
        slug = dirname_to_slug.get(project_dir.name)
        if slug is None:
            continue
        memory_dir = project_dir / "memory"
        if not memory_dir.is_dir():
            continue
        tally = process_crew(slug, memory_dir, project_dir, now=now, dry_run=dry_run,
                             poster=poster, searcher=searcher, blob_getter=blob_getter,
                             log=log)
        summary[slug] = tally
        log(f"memory-archive: {slug}: candidates={tally['candidates']} "
            f"cold={tally['cold']} moved={tally['moved']} "
            f"kept_on_miss={tally['kept_on_miss']} orphaned={tally['orphaned']}")
    return summary
