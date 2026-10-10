"""What GENERIC role is this agent — `sel-reviewer` is a `reviewer`?

WHY ANYTHING NEEDS TO ASK. Skills and documents declare `audience`, and that
vocabulary names generic roles: `coder` appears on 22 skills, `reviewer` on 22,
`platform-coder` on none. An audience filter applied literally hands a
`platform-coder` zero skills. Instruction blocks hit the same gap:
`binds_agents: [reviewer]` has to reach `sel-reviewer` and `deck-reviewer`
without naming each one.

THE ROLE IS WHAT THE AGENT'S DOCUMENT DECLARES. Every `kind: agent` document
carries a `role:` field, required by the knowledge repo's validator, and the
indexer keeps it in the `frontmatter_extra` column of `docs`. `role_for` reads
it from that index, the same source `knowledge-ctx` resolves agents and
instruction blocks from, so there is one record of which agent holds which role.

A ROLE IS NOT DERIVABLE FROM A NAME, which is why the earlier prefix-stripping
version was deleted rather than kept as a fallback. `wb-reviewer` checks the
writing guide's internal consistency with Read, Grep and Glob and nothing else;
stripping the registered `wb-` prefix returned `reviewer` with full confidence,
which would have bound a read-only guide checker to a job that needs `Bash` and
a diff. It declares `writing-reviewer`. A derivation that fires wrongly is worse
than one that returns nothing, so an agent with no document, or a document with
no `role`, gets `None`, which means "bind this one by name". No caller may
substitute a guess.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


def default_index_path() -> Path:
    """The index `lib/knowledge/indexer.py` writes and `knowledge-ctx` reads,
    honouring `KB_INDEX_DB` the same way both do. Read at call time so a test
    that repoints the environment is not defeated by an import-time binding."""
    return Path(os.environ.get(
        "KB_INDEX_DB", str(Path.home() / ".knowledge-index" / "index.db")))


class AgentRoleError(Exception):
    """The agent documents cannot yield one role for this name: a matched
    document does not parse, or same-named documents declare different roles.
    Raised, never returned as None, because None means "bind by name", which no
    `binds_agents` selector expresses, so it would withhold every such block
    from the agent with nothing saying so."""


def _doc_id_from_ref(source_ref: str) -> str:
    """`projects/selector/agents/sel-reviewer.md` -> `selector/agents/sel-reviewer`."""
    ref = source_ref[:-3] if source_ref.endswith(".md") else source_ref
    return ref[len("projects/"):] if ref.startswith("projects/") else ref


def role_for(agent_id: str, *, db_path: Path | str | None = None) -> str | None:
    """The `role` declared on this agent's document, or None.

    Accepts a full document id (`selector/agents/sel-reviewer`) or a bare agent
    name (`sel-reviewer`), because callers have both: an assembler reading the
    corpus knows the id, a dispatch knows the name.

    DOCUMENTS ARE SELECTED AND MATCHED BY `source_ref`, THE PATH THE FILE SITS
    AT, NEVER BY `id`. The write guard (`kb_writes.PR_ONLY_SEGMENTS`) protects
    `/agents/` by path, and `id` is a frontmatter field the author writes and
    nothing ties to the location. A file at an unguarded path such as
    `projects/platform/decisions/0099-x.md` can carry `kind: agent` and a real
    agent's `id`; the indexer upserts on `id`, so it would replace that agent's
    row, and an `id`-keyed `role_for` would return the forged role. The requested name is
    compared with the name derived from the path, so a forged document at a
    non-agents path is never a candidate. `knowledge-ctx`'s
    `fetch_instruction_blocks` keys the same way (PR #616, round 2).

    None when no agent document sits at that path or name, or when the document
    declares no `role`. Nothing here derives a role from the name.

    Raises AgentRoleError when a matched document's `frontmatter_extra` is not a
    JSON object (naming its `source_ref`; the schema already rejects malformed
    JSON), and when a bare name matches documents that declare different roles
    (naming them). An index that is missing or
    unreadable RAISES too: returning None there would read as "bind by name" for
    every agent at once, so a role-scoped instruction block would silently bind
    nothing.
    """
    path = Path(db_path) if db_path is not None else default_index_path()
    if not path.exists():
        raise FileNotFoundError(
            f"cannot read agent roles: the knowledge index is missing at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT source_ref, frontmatter_extra FROM docs "
            "WHERE kind = 'agent' AND source_ref LIKE '%/agents/%'").fetchall()
    finally:
        conn.close()

    by_id = "/" in agent_id
    matched = []
    for source_ref, raw in rows:
        doc_id = _doc_id_from_ref(source_ref)
        if (doc_id if by_id else doc_id.rsplit("/", 1)[-1]) == agent_id:
            matched.append((source_ref, raw))

    declared: dict[str, str | None] = {}
    for source_ref, raw in matched:
        fm = json.loads(raw or "{}")
        if not isinstance(fm, dict):
            raise AgentRoleError(
                f"agent document {source_ref!r} has a frontmatter_extra that is "
                f"not a JSON object, cannot read its role: {raw!r}")
        role = fm.get("role")
        declared[source_ref] = role if isinstance(role, str) and role else None

    if len(set(declared.values())) > 1:
        raise AgentRoleError(
            f"agent name {agent_id!r} matches documents declaring different roles "
            f"({', '.join(f'{r} -> {v!r}' for r, v in sorted(declared.items()))}); "
            f"a bare name cannot say which was meant")
    return next(iter(declared.values()), None)
