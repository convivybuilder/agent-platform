"""`agentctl crew roster <crew> <agent> [<agent> ...]` — add a crew's agent
names to the Library's audience roster (`PUT /roster/{name}`), the table
`knowledge.py`'s admission check reads at `GET /roster` before it will admit
a document whose `audience` names that agent.

A STANDALONE COMMAND, NOT A STEP OF `crew create` OR `crew register`, because
neither knows a new crew's agent names at the moment it runs:

  - `cmd_crew_create` builds its `CrewSpec` with no `agent_prefix` at all, and
    `crew create`'s own argparser takes no flag for one either (bin/agentctl,
    `sp_crew_create`) — there is nothing to derive a name from.
  - `crew register`'s `--agent-prefix` is, by its own help text, "recorded
    here only" (bin/agentctl, `sp_crew_reg`); it still "needs a hand edit to
    bin/agentctl's `_INTERNAL_APP_PREFIXES` and review_protocol's copy"
    before dispatch acts on it, and nothing in `crew_registration.build_row`
    expands a prefix into role names.
  - `crew_create.default_profile`'s proposed profile selects EXISTING shared
    roles (`audience_contains: [reviewer, security-reviewer, scout]`); it
    creates no new agent document and names no new agent.
  - the registered row itself names no agent file: `CrewSpec`/`build_row`
    have no such field.
  - a fixed `<prefix>-<role>` guess over "Builder's seed" roles (`coder`,
    `reviewer`, `security-reviewer`, `planning-orchestrator`— the four files
    under shipwright's `payload/knowledge-seed/projects/platform/agents/`)
    does not hold for a real crew either: the roster rows already recorded
    for acme (5 roles, including `architect`/`planner`, which are not seed
    roles), globex (3, no `reviewer`), widget (3, no `planner`) and orchard (6,
    including `design-critic`) each pick a different set. Reproducing the
    seed's four would be a guess that the recorded history already
    contradicts.

So this is the callable the brief asks for instead: given names already
decided elsewhere (by the `new-crew` skill's "register the crew's agent
prefix" step, once the prefix and its roles are chosen and both dispatch
tuples are hand-edited, or by whoever later proposes the crew's first agent
documents), add each to the roster. It is reachable the same way regardless
of whether the crew was made with `agentctl crew create` or with the
`new-crew` skill's `agentctl crew register` — neither path is wired to call
it automatically, for the reason above, so one command serves both.

CREDENTIAL. The write is gated on the Library's `roster.write`
(library/app.py's `add_roster_name`, checked in `crew_registry._require_
write`), granted only to a `kind: orchestrator` identity
(library/identity.py: `ROLE_CAPS["orchestrator"] = ROLE_CAPS["agent"] |
frozenset({"roster.write"})`). Every credential this repo resolves today —
`SC.token()` (the `agent-platform` bearer, kind `agent`) and the doctor's own
fallback file `library-token` (kind `agent` or `system`) —
is explicitly `kind: agent`/`system`, and identity.py's own comment on the
`orchestrator` role says "minting an actual credential of this kind for a co
session is Bosun-side work" and "Nothing names it there yet". So no
credential this repo can present carries `roster.write`, and this module
does not invent one: it sends the request with whatever credential
`_doctor_auth()` resolves (the same one `crew doctor` already uses for
`roster.read`), and treats the 403 the Library sends back — it names the
missing capability in its body (`refusals.forbidden`: `"this identity lacks
'roster.write'"`) BEFORE it looks at the name at all — as GRANT_MISSING
rather than a per-name refusal, since every name would fail it identically.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT = 10

ADDED = "added"
ALREADY = "already on the roster"
REFUSED = "refused"
GRANT_MISSING = "grant missing"


def push_roster_name(name: str, *, base_url: str, headers: dict,
                     opener: urllib.request.OpenerDirector | None = None,
                     timeout: float = _TIMEOUT) -> tuple[str, str]:
    """One `PUT /roster/{name}`. Returns `(status, detail)`:

      - `(ADDED, "")` or `(ALREADY, "")` — success either way, since the
        route upserts (`crew_registry.roster_add`'s own docstring: "adding a
        name already present is not an error"). Told apart by comparing the
        stored row's `created_at` and `updated_at`, which `roster_add` sets
        equal on a first insert and apart on a refresh.
      - `(GRANT_MISSING, detail)` — the Library's 403 names `roster.write`
        itself; the credential sent holds no capability this call needs, so
        no other name would fare better in the same run.
      - `(REFUSED, detail)` — any other refusal (a malformed name, the
        Library unreachable, a 5xx), naming `detail` as the Library gave it.

    `opener`, when given, replaces the process-wide default — the same
    reason `crew_doctor._served_profile` takes one: a fallback credential
    broader than this call needs must still go only to the address it was
    paired with, with no proxy and no redirect.
    """
    url = f"{base_url}/roster/{urllib.parse.quote(name, safe='')}"
    req = urllib.request.Request(url, method="PUT", headers=headers, data=b"")
    try:
        opened = (opener.open(req, timeout=timeout) if opener is not None
                 else urllib.request.urlopen(req, timeout=timeout))
        with opened as resp:
            row = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        detail = (body.get("error") if isinstance(body, dict) else None) or f"HTTP {exc.code}"
        if exc.code == 403 and "roster.write" in detail:
            return GRANT_MISSING, detail
        return REFUSED, detail
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return REFUSED, str(exc)
    status = ADDED if row.get("created_at") == row.get("updated_at") else ALREADY
    return status, ""
