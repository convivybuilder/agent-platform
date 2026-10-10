"""Shared query/individuation telemetry for the knowledge layer (ARCH-121).

Two producers append JSON lines to sibling logs under ~/.knowledge-index/:
  - mcp-kb-server → kb-queries.jsonl       (live kb_search/kb_get/kb_list_topics)
  - knowledge-ctx → kb-materializations.jsonl  (profile individuation coverage)

The convivy-orient /kb dashboard and the eval harness read these to measure how
effectively the index serves agents. Telemetry is best-effort: append() never
raises, because logging must not break retrieval or individuation. Both bins
import this module defensively, so a not-yet-installed copy degrades to
no-telemetry rather than crashing.

The watcher is the sole writer of index.db, so query logs live in flat files,
not the index.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INDEX_DIR = Path.home() / ".knowledge-index"
QUERY_LOG = INDEX_DIR / "kb-queries.jsonl"
INDIVIDUATE_LOG = INDEX_DIR / "kb-materializations.jsonl"
MATERIALIZE_LOG = INDIVIDUATE_LOG  # pre-0088 name, kept for outside importers


def caller_env() -> dict[str, str]:
    """Correlation fields identifying the calling agent run. agentctl sets these
    when it spawns an orchestrated run (bin/agentctl); an interactive co session
    sets none, and that absence is itself the live-vs-orchestrated signal."""
    return {
        k: v
        for k, v in {
            "run_id": os.environ.get("AGENTCTL_RUN_ID"),
            "agent": os.environ.get("AGENTCTL_AGENT_NAME"),
            "repo": os.environ.get("AGENTCTL_REPO"),
        }.items()
        if v
    }


def append(log_path: Path, record: dict[str, Any]) -> None:
    """Append one telemetry line, stamped with a UTC timestamp and caller fields.

    Never raises — a failed append (disk full, race, unwritable dir) silently
    drops the record. Caller-supplied keys win over the stamped `ts`/caller keys
    only if they collide, which they should not.
    """
    try:
        line = {"ts": datetime.now(timezone.utc).isoformat(), **caller_env(), **record}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass
