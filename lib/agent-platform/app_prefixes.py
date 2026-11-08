"""The one place internal-app crew prefixes live (ARCH-397).

bin/agentctl's `_INTERNAL_APP_PREFIXES` and tool_policy.py's prefix-stripping used to be
two independent lists naming the same crews, and they drifted: tool_policy's own regex
never learned several of the newer crews' prefixes, so a crew-prefixed reviewer fell
through to the build-class branch — enforced=False, an empty allow-list,
`autoAllowBashIfSandboxed: false` — while agentctl's own `_is_review_agent` correctly
called it review-class. Two internal-app crews' reviewers both ended errored on exactly
this gap, having posted no verdict, surfacing only because a human went looking for the
missing comment. One tuple, read by both `bin/agentctl` and `tool_policy.py`, is what
keeps the two from disagreeing on which names are review-class again (Phase 1).

Phase 2 (ARCH-397, issue_01M3YN6857H5J541G73Y9P3KA1) collapses the tuple itself into the
project registry: `internal_app_prefixes()` below returns `projects.all_agent_prefixes()`,
built from each unit's own `agent_prefix` field in the project registry, rather than a
literal this module and `review_protocol.py` each kept pinned equal to bin/agentctl's copy
by a test. A crew registered with its own `agent_prefix` is covered the moment that
registry edit lands, with no change to this module, `review_protocol.py` or bin/agentctl.

It is a FUNCTION, called at use, rather than a module-level constant bound at import:
review_protocol.py, tool_policy.py and bin/agentctl all import this module, and a module
constant here would read the registry once, at whichever of those three happened to import
first, with no chance to recover for the rest of the process. Called at use, a transient
registry problem at process start does not have to outlive it.

A missing, unreadable or malformed registry — a local file that is absent or broken JSON,
an unreachable or refusing Library, or any unit's `agent_prefix` entry that fails
`projects.agent_prefixes()`'s shape check — degrades this
to the empty tuple rather than raising: an import crash here would take `bin/agentctl`
itself down (every spawn dies at argparse, not just the one feature that needed the
registry), and the empty tuple is the
fail-CLOSED reading either caller needs — dispatch recognises no prefixed agent (so a crew
name it cannot resolve refuses rather than silently reads as a bare role), and review
authentication counts no stamp as a prefixed reviewer's. Never the other direction: this
must never read as every name being prefixed.
"""

from __future__ import annotations

import sys

import projects


def internal_app_prefixes() -> tuple[str, ...]:
    """Every registered crew's agent prefix (see module docstring). Returns `()`,
    never raises, when the registry cannot be read — a local file that is
    missing or malformed (`OSError`/`ValueError`), or an unreachable or
    refusing Library (`projects.RegistryUnavailable`, CQ·563) — OR fails
    `projects.agent_prefixes()`'s shape check on any entry: a crash here
    would propagate out of `strip_app_prefix` and every review-authenticity
    check that calls it, so one malformed row degrades the whole process to
    the fail-closed empty tuple rather than taking it down."""
    try:
        return projects.all_agent_prefixes()
    except (OSError, ValueError, projects.RegistryUnavailable) as exc:
        print(f"app_prefixes: could not read agent prefixes: {exc}",
              file=sys.stderr)
        return ()


def strip_app_prefix(agent_name: str) -> str:
    """`deck-reviewer` -> `reviewer`. `orient-coder` -> `coder`. Returns the name
    unchanged when it carries none of `internal_app_prefixes()` — including when
    the registry itself can't be read."""
    for p in internal_app_prefixes():
        if agent_name.startswith(p):
            return agent_name[len(p):]
    return agent_name
