"""Derive the documents a dispatch run has fetched, and the knowledge and
signal searches it has run, live, from its own `tool_calls.jsonl` — the
FETCHED half of Deck's two-sided document list for a run clicked in its
Dispatched panel (issue_01M3BFGKYNHFN29R43PAZXRRHV). Fetches are served as
`docs_fetched` rows; searches are served as `searches` rows, one per successful
search. Each string field is bounded in the reader; its control-character bound
covers C0 control characters and DEL only (`_CONTROL_CHARS`).

THE OTHER HALF — what a run was GIVEN at dispatch (agents, skills,
instruction blocks, profile documents) — is recorded separately, at dispatch
time, into the run directory (issue_01M3BFGE9QE8339CK5RNVP5XQC). This module
never writes anything; it only derives from what `telemetry.append_tool_call`
has already appended, which is why it works for a run that is still running
and has not finalized `docs_consulted.json` yet.

NOT THE LIBRARY'S READ RECORD. Phase 5's bundle-miss and bundle-waste
measures need the Library to capture reads per library card; what this
module derives is this ONE run's own tool-call log, read for display in
Deck, and must not become the input to those measures.

Entries share the `id` / `kind` field names with the given-set half so Deck
can line the two lists up, even though the two halves choose their own
`kind` vocabularies independently (this module's: "knowledge", "signal") —
the given-set half's `kind` is the kb document's own frontmatter kind
(`decision`, `runbook`, and so on), a different, finer vocabulary. The same
document fetched mid-run shows a different `kind` string in each list; line
the two lists up on `id` alone, never on `(id, kind)`.

EVERY ENTRY HERE IS SELF-REPORTED. `tool_calls.jsonl` is written inside the
run's own writable directory, by the run it describes — an entry means
"this run's own tool-call log says it fetched this document," not an
attestation from anything outside that run. See `_looks_like_a_miss`'s
docstring for the one piece of this that is NOT self-reported: whether a
recorded call actually returned a document, which this module checks
against the call's own `result_summary`, not against the run's say-so.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Iterable

# tool_name -> (kind, the input field holding the document's id) for a tool
# call that FETCHES one document in full, by id.
#
# mcp__kb__kb_search and mcp__ops__signal_search are deliberately absent: a
# search returns snippets of several documents that matched a query, and
# recording every hit as "fetched" would claim this run read the full content
# of documents it only saw a snippet of. Only a call that names one document
# by id and gets its full content back counts — kb_get and kb_checkout both
# do (kb_checkout's docstring: "read it back"), and so does signal_get.
# The searches are listed separately, in `_SEARCH_TOOLS`.
_FETCH_TOOLS: dict[str, tuple[str, str]] = {
    "mcp__kb__kb_get": ("knowledge", "id"),
    "mcp__kb__kb_checkout": ("knowledge", "id"),
    "mcp__ops__signal_get": ("signal", "signal_id"),
}

# tool_name -> the `tool` a search row reports, for a call that searches the
# knowledge base or the signal stream. The query is read from the call's
# `input["query"]`, the field both tools record it under in tool_calls.jsonl
# (`mcp__kb__kb_search`'s `query` parameter; `mcp__ops__signal_search`'s
# `query` parameter).
_SEARCH_TOOLS: dict[str, str] = {
    "mcp__kb__kb_search": "kb_search",
    "mcp__ops__signal_search": "signal_search",
}

# A real id is a short, printable token (a kb id's shape is
# `project/kind/slug`; `kb_checkout` also admits a repo-relative path; a
# signal id is a short slug like "sig-42") — nowhere near this bound. 1024
# matches `session_crews.MAX_PATH`, this codebase's existing bound for a
# path-shaped field reaching a comparable boundary, so a run stuffing a
# multi-kilobyte or C0-control-or-DEL id into its own writable
# tool_calls.jsonl can't make it into this daemon's JSON response. This is a
# generic shape bound, not a validity check — an id that is the right shape
# but names nothing real still serves (`kb_get`/`signal_get` would have
# returned nothing for it, which `_looks_like_a_miss` below catches
# separately).
_MAX_ID_LEN = 1024
# The C0 control characters (U+0000 to U+001F) and DEL (U+007F). C1 controls
# (U+0080 to U+009F) are not matched; json.dumps escapes them on the wire.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# A search query is free text the agent typed, so it is bounded the same way
# an id is, with two differences. An over-long query is truncated to this many
# characters rather than skipped, because the search still happened and its
# opening words are what a reader needs. A C0 control character or DEL is
# replaced with a space rather than skipped, for the same reason: dropping the
# row would hide a real search, and a query is display text rather than a key.
# The cap bounds each query. It does not bound the response, which is limited
# only by the 64 MiB read in `_read_tool_calls_bounded`. The longest query
# observed in the agent-runs run logs is 127 characters (measured 2026-10-05),
# so no real query is truncated.
_MAX_QUERY_LEN = 512

# A timestamp is the ISO-8601 string `telemetry.now_iso` writes, 20 characters
# long; a fractional second and a numeric offset keep an ISO-8601 timestamp
# under 40. 64 leaves headroom above that. A run writes its own
# tool_calls.jsonl, so a timestamp it chooses can be any length or hold a C0
# control character or DEL. Such a timestamp is served as null, and its row is
# kept.
_MAX_TIMESTAMP_LEN = 64

# tool_calls.jsonl lives inside a run's own writable directory
# (telemetry.append_tool_call appends to it for the run's entire life) — the
# same run-writable-directory threat review_protocol._read_meta_bounded's
# docstring describes for meta.json, except this file grows without bound
# for as long as the run keeps calling tools rather than being capped at a
# few tens of KB. Capped at run-observer's own transcript.jsonl bound (64
# MiB, the largest run-writable log this platform already reads unattended)
# so an honest long run is never truncated, while a run that deliberately
# grows this file cannot make a GET for it stall this KeepAlive daemon.
_MAX_TOOL_CALLS_BYTES = 64 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


def _open_regular_file(path: Path) -> "int | None":
    """A read fd for `path`, or None unless it is (and stays) a plain regular
    file — the same O_NOFOLLOW + O_NONBLOCK + fstat double-check
    `review_protocol._open_meta_nofollow` applies to meta.json, for the same
    reason: a symlink to /dev/zero reads forever, and a FIFO with no writer
    blocks the open itself."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _read_tool_calls_bounded(path: Path) -> bytes:
    """`path`'s bytes, up to `_MAX_TOOL_CALLS_BYTES`, read in fixed chunks —
    never the whole file in one `read()` call, which a single-line,
    no-newline file would otherwise force regardless of the cap (the same
    reasoning `run-observer._transcript_lines` gives for its own chunked
    read of transcript.jsonl). Returns `b""` for a missing, non-regular, or
    unreadable file, never raising."""
    fd = _open_regular_file(path)
    if fd is None:
        return b""
    chunks: list[bytes] = []
    read = 0
    try:
        with os.fdopen(fd, "rb") as f:
            while read < _MAX_TOOL_CALLS_BYTES:
                chunk = f.read(min(_READ_CHUNK, _MAX_TOOL_CALLS_BYTES - read))
                if not chunk:
                    break
                chunks.append(chunk)
                read += len(chunk)
    except OSError:
        return b""
    return b"".join(chunks)


def _iter_tool_call_lines(raw: bytes) -> Iterable[dict]:
    """Yield each parsed JSON object from `raw`'s lines, skipping any line
    that fails to decode or parse, for any reason, or that does not decode
    to a dict (a line that parses clean to a list, a number, or `null` names
    no tool call and is skipped the same as a malformed one).

    THE LAST LINE IS TOLERATED PARTIAL BY CONSTRUCTION, not specially cased.
    `telemetry.append_tool_call` opens tool_calls.jsonl O_APPEND and writes
    one line per call while the run is still live, so a read racing that
    append can land between the open and the trailing newline and see a
    truncated final line. This reader has no way to distinguish "mid-write"
    from "genuinely malformed" and does not need to: both answers are the
    same — drop the one line, keep the rest.

    CAUGHT BROADLY, not narrowed to `json.JSONDecodeError`: this file lives
    inside the run's own writable directory (the same threat
    `review_protocol._read_meta_bounded` names for meta.json, whose
    docstring this mirrors), so a line is adversarial input, not merely
    occasionally malformed. A deeply nested line (`"[" * 2000 + "]" * 2000`)
    raises `RecursionError`; a line with an invalid UTF-8 byte sequence
    raises `UnicodeDecodeError` (`json.loads` decodes `bytes` itself before
    parsing). Neither is a `JSONDecodeError`, and either escaped this
    function, through `fetched_and_searched()` and `_docs_fetched_payload`,
    into `Handler.do_GET`, which has no wrapping `try`/`except` — a crash
    with no response, for any caller polling this run (security-reviewer,
    round 1 of agent-platform#742)."""
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            yield entry


def _looks_like_a_miss(tool_name: str, result_summary: "object") -> bool:
    """True when `result_summary` — the text `bin/agentctl`'s
    `_stream_transcript` recorded for a `_FETCH_TOOLS` call — is the shape that
    tool returns for an id that names no document, NOT a tool-level exception.

    `is_error` CANNOT TELL THESE APART: `bin/agentctl`'s `_stream_transcript`
    copies it straight from the Claude Code transcript's `tool_result.is_error`,
    which mirrors MCP's own `isError` — set `True` only from the server's
    `except`
    clause around a raised exception (`mcp.server.lowlevel.server`), never
    from a tool's own return value. None of the three misses below raise:
    they are each tool's ordinary, successful answer to "no such document."
    `result_summary` is the only field left that can tell them apart
    (reviewer + security-reviewer, round 1 of agent-platform#742).

    THE THREE SHAPES, each read from the server that produces it:

    - `kb_get` returns Python `None` for an unindexed id (`bin/mcp-kb-server`'s
      `kb_get`). FastMCP's `_convert_to_content` maps `None` to an
      EMPTY content-block list (`result is None: return []`,
      `mcp.server.fastmcp.utilities.func_metadata`), so `bin/agentctl`'s
      `"\n".join(...)` over that empty list is the empty string. A found
      doc is always a non-empty dict, which never serializes to "" — empty
      is unambiguous, and needs no parsing to recognize.

    - `kb_checkout` always returns a dict, so the empty-string check above
      does not apply to it. `bin/mcp-kb-server`'s `_run_kb` builds
      `{"ok": ..., "exit_code": ..., "stdout": ..., "stderr": ...}`, and
      `kb_checkout` either adds the checkout's own fields on success or
      returns that dict as-is on refusal (lease conflict, policy refusal,
      stale base, and so on). A refusal is a miss when its `"ok"` is false.
      `bin/agentctl` cuts `result_summary` at 500 characters, so a refusal
      over 500 characters does not parse and is read as a fetch. There is no
      prefix match on `"ok"`.

    - `signal_get` always returns a JSON string (`bin/mcp-ops-server`'s
      `signal_get`). `_call`'s error replies always carry an `"error"`
      key; `_shape_item`'s success shape never has one. Same truncation
      reasoning as `kb_checkout`: a parse failure here is read as a fetch,
      which includes an error reply over 500 characters (see
      `_is_in_band_error`).

    `json.loads` IS WRAPPED BROADLY, not narrowed to `JSONDecodeError`, for
    the same reason `_iter_tool_call_lines` is: `result_summary` is a string
    this run fully controls, and a crafted deeply-nested value here would
    otherwise raise `RecursionError` out of this function the same way it
    would out of the outer line parse.

    A NEW FAILURE SHAPE NOT LISTED ABOVE — a `_FETCH_TOOLS` member gaining a
    new refusal code `kb_checkout`/`signal_get` doesn't already encode as
    `ok: false` / an `error` key, or a fourth tool added to `_FETCH_TOOLS`
    with its own miss shape — is NOT caught here and reads as a fetch. This
    function recognizes the three shapes traced above; it does not infer
    "probably a miss" from `result_summary` text in general."""
    if tool_name == "mcp__kb__kb_get":
        return result_summary == ""
    if tool_name not in ("mcp__kb__kb_checkout", "mcp__ops__signal_get"):
        return False
    if tool_name == "mcp__kb__kb_checkout":
        parsed = _summary_dict(result_summary)
        return parsed is not None and parsed.get("ok") is False
    return _is_in_band_error(result_summary)


def _summary_dict(result_summary: "object") -> "dict | None":
    """The JSON object `result_summary` holds, or None when it is not a
    non-empty string, does not parse (a summary `bin/agentctl` cut at 500
    characters does not), or parses to something other than a dict."""
    if not isinstance(result_summary, str) or not result_summary:
        return None
    try:
        parsed = json.loads(result_summary)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_in_band_error(result_summary: "object") -> bool:
    """True when `result_summary` is a JSON object with an `"error"` key: a
    tool that returned a failure as an ordinary result, where the harness's
    `is_error` flag stays false.

    `bin/mcp-ops-server`'s `signal_search` does this for a negative `offset`
    (`json.dumps({"error": "offset must be non-negative"})`) and for any
    control-plane failure its `_call` reports. `bin/mcp-kb-server`'s
    `kb_search` never does: it returns a list on success, and a failure
    raises, which `is_error` records. So on the search branch this test fires
    only for `signal_search`. `_looks_like_a_miss` also uses it for
    `signal_get`, which returns the same shape for the same failures.

    A summary over 500 characters does not parse, because `bin/agentctl` cuts
    `result_summary` at 500 characters at write time (`summary[:500]`). Such a
    summary reads as not an error, so an error reply longer than 500
    characters is served as a search. This is a known gap. No recorded case of
    a truncated error is named, so this test does not add a prefix match."""
    parsed = _summary_dict(result_summary)
    return parsed is not None and "error" in parsed


def _bound_query(query: str) -> str:
    """`query` truncated to `_MAX_QUERY_LEN` characters, with each C0 control
    character or DEL replaced by a space. Truncation runs first, so the served
    string is bounded regardless of how many such characters it held."""
    return _CONTROL_CHARS.sub(" ", query[:_MAX_QUERY_LEN])


def _bound_timestamp(timestamp: "object") -> "str | None":
    """`timestamp` itself when it is a string of at most `_MAX_TIMESTAMP_LEN`
    characters with no C0 control character or DEL; None otherwise. Both the
    search row's `searched_at` and the fetch row's `fetched_at` go through
    this."""
    if (not isinstance(timestamp, str) or len(timestamp) > _MAX_TIMESTAMP_LEN
            or _CONTROL_CHARS.search(timestamp)):
        return None
    return timestamp


def fetched_and_searched(run_dir: Path) -> tuple[list[dict], list[dict]]:
    """This run's fetched documents and its searches, both from one read of
    its own `tool_calls.jsonl`, as `(docs_fetched, searches)`. Each list is in
    the order the tool calls happened.

    `docs_fetched` is `[{"id", "kind", "fetched_at"}, ...]`, from the
    `_FETCH_TOOLS` calls. `searches` is `[{"tool", "query", "searched_at"},
    ...]`, one row per successful `_SEARCH_TOOLS` call, with `tool` the value
    that table maps the call to. The two lists never share a row: a search
    adds nothing to `docs_fetched`, because its results are snippets, not
    fetched documents.

    Safe to call against a run that is still live and still appending to that
    file; safe to call repeatedly as it grows, since this always re-reads from
    the start rather than tracking an offset (the register is read fresh per
    request, no cache — the same "read live, don't cache" contract
    `bin/run-register-api`'s module docstring states for `/runs`).

    A call whose recorded `input` is missing or not a dict, or whose id
    field is missing, not a non-empty string, longer than `_MAX_ID_LEN`, or
    holds a C0 control character or DEL, is skipped rather than served with a
    placeholder or oversized id: a malformed or redacted record names no
    document, and this is a KeepAlive daemon response, not a place for a
    run-controlled string of unbounded size or shape to land. A search call
    whose `input` is not a dict is skipped the same way.

    A SEARCH WITH NO USABLE QUERY IS STILL A SEARCH. `query` is `None` when the
    input has no `query` key or the value is not a string, which is how a
    filters-only `kb_search` shows up. A string query is served through
    `_bound_query`: truncated to `_MAX_QUERY_LEN`, with C0 control characters
    and DEL replaced by spaces. The row is never skipped for its query, and see
    `_MAX_QUERY_LEN` for why.

    A TIMESTAMP IS NULLED, NOT SKIPPED. `searched_at` and `fetched_at` both go
    through `_bound_timestamp`: a timestamp that is not a string, is longer
    than `_MAX_TIMESTAMP_LEN`, or holds a C0 control character or DEL is served
    as None, and the row is kept, because the call still happened.

    An ERRORED call is absent from both lists, since a call that raised
    returned nothing. "Errored" means `is_error` true, or, for a search, a
    result that is an in-band `"error"` object (see `_is_in_band_error`).

    A CALL THAT DID NOT RETURN A DOCUMENT IS NOT A FETCH, even when its
    `tool_name`/`input` shape-match a `_FETCH_TOOLS` entry — see
    `_looks_like_a_miss` for the three ordinary (non-exception) ways
    `kb_get`, `kb_checkout`, and `signal_get` answer "no such document," and
    why `is_error` alone does not catch any of them. A search that found
    nothing is still a search, so `searches` does not filter on results."""
    raw = _read_tool_calls_bounded(run_dir / "tool_calls.jsonl")
    docs: list[dict] = []
    searches: list[dict] = []
    for entry in _iter_tool_call_lines(raw):
        if entry.get("is_error"):
            continue
        tool_name = entry.get("tool_name")
        if not isinstance(tool_name, str):
            continue
        timestamp = _bound_timestamp(entry.get("timestamp"))
        if tool_name in _SEARCH_TOOLS:
            if _is_in_band_error(entry.get("result_summary")):
                continue
            tool_input = entry.get("input")
            if not isinstance(tool_input, dict):
                continue
            query = tool_input.get("query")
            searches.append({
                "tool": _SEARCH_TOOLS[tool_name],
                "query": _bound_query(query) if isinstance(query, str) else None,
                "searched_at": timestamp,
            })
            continue
        if tool_name not in _FETCH_TOOLS:
            continue
        kind, id_field = _FETCH_TOOLS[tool_name]
        tool_input = entry.get("input")
        if not isinstance(tool_input, dict):
            continue
        doc_id = tool_input.get(id_field)
        if (not isinstance(doc_id, str) or not doc_id
                or len(doc_id) > _MAX_ID_LEN or _CONTROL_CHARS.search(doc_id)):
            continue
        if _looks_like_a_miss(tool_name, entry.get("result_summary")):
            continue
        docs.append({
            "id": doc_id,
            "kind": kind,
            "fetched_at": timestamp,
        })
    return docs, searches
