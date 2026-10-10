"""Read the KB's write history back — the compensating control for `in-session`.

ARCH-408 grants `in-session` a direct write to knowledge/main. It rests on the
writing agent's own attestation that the operator agreed to the change — unverifiable at
write time, and accepted on the condition that the writes get read back in
batch. This is that read-back. Without it the provenance has detection in
principle and nobody looking in practice.

Two other provenances once wrote directly too: `verifier-pass`, on a
kb-sync-verifier PASS, and `generated`, on a path allowlist. `generated` came
off the direct path in platform/decisions/0081 (2026-09-13), alongside `keel`
and `steward`. `verifier-pass` took two separate changes to close, not one:
0081 removed `kb apply --provenance verifier-pass`, but `scribe apply
--verifier-run` was a SECOND, separate direct-write path — this module's
read-back does not gate it through `kb_writes.policy_for` at all, since it
never called that function. It made 14 verifier-backed writes between
2026-08-01 and 2026-08-19, then sat unused but still live and callable straight
through 0081 landing on 2026-09-13, until this cage's own round-2 revision
refused it at the input on 2026-09-14. As of that revision `verifier-pass` is
off both direct-write paths, `kb apply` and `scribe apply`. Commits already on
main under any of the four are read back here exactly as they were the day
they landed, judged by the policy in force when each was made rather than by
today's. `kb_writes.GENERATED_PATH_RE` stays live for that reason: this module
still uses it to check a historical `generated` claim against the paths that
provenance was ever allowed to touch.

It answers two questions over a window:

  1. WHAT WAS WRITTEN ON ATTESTATION — every `in-session` commit, so a human can
     skim what agents claimed he agreed to.
  2. WHAT ARRIVED BY NEITHER SANCTIONED PATH — the invariant is that every commit
     on main comes through the cage or as a PR merge. A commit that is neither is
     a bypass, and nothing else in the platform would notice it.

The second is the one that pays for the tool. On the first real run it found six
in a week: two from the briefing job before it was routed through the cage, and
four docs commits pushed straight to main from a branch — which the ARCH-395
pre-commit hook cannot catch, because it blocks committing ON main rather than
pushing TO it.

CLASSIFICATION IS DELIBERATELY GENEROUS ABOUT WHAT COUNTS AS SANCTIONED. The
legacy `scribe apply` cage predates the `KB-Write:` trailer and signs its commits
in prose; counting those as bypasses would have reported 33 false positives in
the first week and taught everyone to ignore the report. An audit that cries wolf
is worse than none, because it converts a real signal into noise someone learns
to skip.

`audit_access`/`render_access`, below, answer a DIFFERENT question from
`audit`/`render` above: not who wrote to `knowledge/main`, but who COULD —
collaborators, teams, the repo's default workflow-token permissions, any
workflow file granting itself `contents: write`, and every GitHub App installed
on the `convivy` org (issue_01KZQA9FK7F6GZS4E4NCQRH0YB, split from the write-audit
issue because two of its controls were verified once on 2026-08-11 and nothing
has watched them since). The expected state is a literal snapshot from that
verification (comments 298 and 307 on the issue), not a live derivation — a
value here goes stale the moment the operator changes a setting on purpose, and the only
way to tell "changed on purpose" from "drifted" is a human reading the diff this
module reports, not a rule this module could apply on its own.

BRANCH PROTECTION IS DELIBERATELY LEFT OUT, pending issue_01KZQA8A1SEEWSNYV5H8N0EQW7.
Adding a half-built check for it here would be worse than the gap: a reader who
sees `audit_access` run and pass would reasonably assume branch protection was
covered.

INSTALLATION REPOSITORY SCOPE IS CHECKED BY FIELD, NOT BY ASKING GITHUB WHICH
REPOSITORIES A `selected` INSTALLATION ACTUALLY REACHES. Resolving that live
needs either that installation's own private key (this platform holds its own
App's, and the Library's, but not render's, Cloudflare's, or the CI-runner
App's) or a user access token issued through that specific App's own OAuth
flow — the operator's personal token is neither and was refused with
`GET /user/installations/{id}/repositories` → 403 (verified live, 2026-10-07).
So this checks `repository_selection` and `permissions` against the recorded
baseline and nothing more: a `selected` installation that is re-granted
`convivy/knowledge` while its `repository_selection` field stays `"selected"`
would not be caught. That is a real gap in what this check can see, not a
guard this module could add — no credential exists yet to build the check
that would close it.
"""
from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import yaml

import kb_writes
import builder_root as BR
import git_refs
import operator_config

KNOWLEDGE = BR.root() / "knowledge"

# --- who could write: expected state, verified live (issue_01KZQA9FK7F6GZS4E4NCQRH0YB,
# comments 298 and 307, 2026-08-11) ------------------------------------------


def _expected_collaborators() -> "frozenset[tuple[str, str]] | None":
    """The one collaborator GitHub should ever report on convivy/knowledge:
    `(operator_config.get("github_login"), "admin")`. `None` when the operator
    config has no `github_login` — `audit_access` treats that as unreadable,
    never as a clean read, so a missing login is never mistaken for an
    actually-matching audit."""
    try:
        login = operator_config.get("github_login")
    except operator_config.OperatorConfigError:
        return None
    return frozenset({(login, "admin")})

# convivy/knowledge's own default-workflow-token grant. `can_approve_pull_request_reviews`
# was flipped `true` -> `false` on 2026-08-11 in the same decision: it had let a
# workflow approve a pull request even without `contents: write`.
EXPECTED_WORKFLOW_PERMISSIONS = {
    "default_workflow_permissions": "read",
    "can_approve_pull_request_reviews": False,
}

# Every GitHub App installed on the `convivy` org that can reach, or is recorded
# as NOT reaching, `convivy/knowledge` — keyed by `app_slug`. `permissions` only
# carries the fields this checklist actually watches (`contents`, `workflows`);
# comparing GitHub's full permissions dict would flag every unrelated permission
# GitHub adds to an App over time as if it were a KB-relevant drift.
# `repository_selection` is omitted for `claude-design-import` because comment
# 307 never recorded it — this module asserts only what was actually verified.
EXPECTED_INSTALLATIONS = {
    "render": {"repository_selection": "all", "suspended": False,
               "permissions": {"contents": "read", "workflows": "write"}},
    "claude-design-import": {"suspended": False,
                             "permissions": {"contents": "read"}},
    # Does not reach convivy/knowledge (verified 2026-08-11) — see the module
    # docstring's note on why that fact is not re-derived live every run.
    "cloudflare-workers-and-pages": {"repository_selection": "selected", "suspended": False,
                                     "permissions": {"contents": "write"}},
    "claude": {"repository_selection": "all", "suspended": True,
              "permissions": {"contents": "write"}},
}

# The machine-readable trailer the ARCH-408 cage writes.
TRAILER_RE = re.compile(
    r"^KB-Write:\s+(?P<verb>\S+)\s+(?P<ref>\S+)\s+(?P<provenance>\S+)\s+(?P<holder>\S+)\s*$",
    re.MULTILINE)

# The second line a `person` write carries, naming the email the cage verified.
# Only the cage can produce it, so a `person` claim without one is false on its face.
# Any line that starts like one is counted, well-formed or not, so a second line
# cannot hide behind a valid first. The value must be a conservative email shape
# spanning the whole line: it is printed into the report, so it may not carry ESC,
# other control or bidi characters.
PERSON_LINE_RE = re.compile(r"^KB-Person:[^\n]*$", re.MULTILINE)
PERSON_RE = re.compile(
    r"^KB-Person:[ \t]+(?P<person>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})[ \t]*$")

# The legacy `scribe apply` cage's prose signature, and the provenance it states.
SCRIBE_RE = re.compile(r"Applied via `agentctl scribe apply`")
SCRIBE_PROV_RE = re.compile(r"Provenance:\s*(?P<prov>[^.]+)\.")

# Provenances that write main directly, which is what the weekly read-back exists
# to surface. `in-session` rests on an agent's own attestation; `person` rests on
# a verified Cloudflare Access assertion, and is listed so the read-back sees every
# direct write to main.
ATTESTED = ("in-session", "person")


def _git(*a, root: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root or KNOWLEDGE), *a],
                          capture_output=True, text=True, check=False)


def _normalize_scribe_provenance(prose: str) -> str:
    """Map the legacy cage's prose provenance onto the ARCH-408 vocabulary.

    One vocabulary in the report, whichever cage wrote the commit — otherwise a
    reader has to know which era a commit came from to know whether it was
    attested, which is exactly the thing the report is meant to make obvious.
    """
    p = prose.lower()
    if "verifier" in p:
        return "verifier-pass"
    if "in-session" in p or "agreed in session" in p:
        return "in-session"
    return "scribe-unknown"


def corroborate(info: dict, changed: list[str]) -> dict:
    """Check a commit's CLAIM against what it actually changed.

    The trailer is prose. Nothing binds it to the commit, so a writer with push
    access can forge one — and the dangerous forgery is not a fake `in-session`,
    which still surfaces where a human skims it, but a fake NON-attested
    provenance like `verifier-pass`, which lands in neither list and only bumps a
    count. That is a bypass made invisible by claiming to be sanctioned
    (security review, #248).

    Five checks the forger cannot talk past, because each compares the claim to
    the commit's actual contents rather than to itself:

    1. A ROSTER OR PROFILE PATH IN A NON-MERGE COMMIT IS A BYPASS, whatever the
       trailer says. Those paths are PR-only under every provenance, so arriving
       any other way is definitionally unsanctioned. This is the strongest check:
       it is an invariant, not a heuristic.
    2. A SIGNED COMMIT CHANGES EXACTLY ONE FILE. Both cages stage a single path,
       so a multi-file commit carrying either signature could not have been
       written by one. Without this, naming a legitimate doc while smuggling a
       second file alongside it passes every other check.
    3. `generated` MUST NAME A REGISTERED GENERATOR PATH. The provenance is
       bounded by path at write time; a claim outside that bound is false.
    4. THE CLAIMED REF MUST BE A FILE THE COMMIT ACTUALLY TOUCHED. A trailer
       naming some innocuous doc on a commit that changed something else is
       forged on its face.
    5. A `person` CLAIM MUST CARRY A `KB-Person:` LINE. The cage cannot write the
       provenance without the line naming the verified email, so a claim with no
       line was typed by hand.

    An empty commit carrying a signature is also rejected: it wrote nothing, so
    no content bypassed anything, but the signature is still a false claim.

    A claim that fails any of these is reported as `unsigned` with the reason, so
    it lands in the bypass list rather than dissolving into a count.
    """
    if info["path"] == "pr":
        return info

    if not changed:
        # An empty commit carrying a cage signature wrote nothing, so no content
        # bypass occurred — but the signature is still false, and a claim that is
        # false about nothing is still a claim the audit should not accept
        # (security review, #248).
        if info["path"] in ("cage", "scribe"):
            return {**info, "path": "unsigned",
                    "why": "carries a cage signature but changed no files"}
        return info

    docs = [c for c in changed if c.endswith(".md") or "/profiles/" in c]

    # 1. PR-only paths, whatever was claimed.
    #
    # Delegated to `kb_writes.is_pr_only` rather than re-stating the rule, because
    # the copy that used to live here had drifted from it in two ways and both
    # mattered. It named `projects/shared/agents/` literally, so the step-5
    # re-file would have taken the roster out of the AUDIT while leaving it in the
    # cage — the two disagreeing about what is protected is worse than either
    # being wrong. And it compared case-SENSITIVELY, which is the exact bug
    # `is_pr_only` carries a docstring about: macOS is case-insensitive, so
    # `PROJECTS/SHARED/AGENTS/reviewer.md` opens the same inode and a
    # case-sensitive `startswith` waved it through.
    protected = [c for c in changed if kb_writes.is_pr_only(c)]
    if protected:
        return {**info, "path": "unsigned",
                "why": f"touches PR-only path {protected[0]} outside a PR"}

    # The single-file invariant covers BOTH cages, so it runs before the
    # cage-only checks. Each stages exactly one path (`git add <target>` in a
    # worktree reset to origin/main), so a multi-file commit carrying either
    # signature could not have been written by one. Verified across 60 days of
    # the real corpus: every cage and scribe commit touches exactly one file.
    if info["path"] in ("cage", "scribe") and len(changed) > 1:
        return {**info, "path": "unsigned",
                "why": f"a cage commit stages one file, but this changed "
                       f"{len(changed)} (including {changed[1]})"}

    if info["path"] != "cage":
        return info

    # The fifth check: the cage writes `person` and `KB-Person:` together, never one alone.
    if info.get("provenance") == "person" and not info.get("person"):
        return {**info, "path": "unsigned",
                "why": "claims `person` but carries no `KB-Person:` line"}

    # 2. `generated` is bounded by path; a claim outside the bound is false.
    #
    # The BOUND ITSELF comes from the cage, for the same reason the PR-only check
    # above does. This had its own hardcoded copy naming convivy and engineering,
    # so widening the cage for step 5's re-file would have left the audit flagging
    # every legitimate brief written to the new location as `unsigned` — and this
    # module's own docstring names that failure mode: an audit that cries wolf is
    # worse than none, because it converts a real signal into noise someone learns
    # to skip (security-reviewer, PR #341).
    #
    # The cage's pattern is STRICTER than the copy it replaces — `[a-z0-9][a-z0-9.-]*`
    # against `[^/]+` for the filename. That is the right direction: a filename the
    # cage would have refused did not reach main through the cage, so an audit that
    # accepted it was calling a bypass sanctioned. Every brief in the corpus is
    # lowercase-and-dashes, so nothing already written is newly flagged.
    if info.get("provenance") == "generated":
        bad = [c for c in docs if not kb_writes.GENERATED_PATH_RE.match(c)]
        if bad:
            return {**info, "path": "unsigned",
                    "why": f"claims `generated` but changed {bad[0]}, "
                           f"which no registered generator owns"}

    # 3. The claimed ref must be something the commit actually touched.
    ref = info.get("ref")
    if ref and ref not in changed:
        return {**info, "path": "unsigned",
                "why": f"trailer names {ref}, which this commit did not change"}
    return info


def classify(subject: str, body: str, *, is_merge: bool) -> dict:
    """One commit -> {path, provenance, ref, holder}.

    `path` is how it reached main: "pr" (a merge), "cage" (ARCH-408 trailer),
    "scribe" (the legacy cage), or "unsigned" — the bypass.
    """
    if is_merge:
        return {"path": "pr", "provenance": None, "ref": None, "holder": None}
    m = TRAILER_RE.search(body or "")
    if m:
        person = None
        if m.group("provenance") == "person":
            found = PERSON_LINE_RE.findall(body or "")
            p = PERSON_RE.match(found[0]) if len(found) == 1 else None
            person = p.group("person") if p else None
        return {"path": "cage", "provenance": m.group("provenance"),
                "ref": m.group("ref"), "holder": m.group("holder"),
                "person": person}
    if SCRIBE_RE.search(body or ""):
        prov = SCRIBE_PROV_RE.search(body or "")
        return {"path": "scribe",
                "provenance": _normalize_scribe_provenance(prov.group("prov") if prov else ""),
                "ref": None, "holder": None}
    return {"path": "unsigned", "provenance": None, "ref": None, "holder": None}


def audit(since: str = "7.days", ref: str = "origin/main", root: Path | None = None) -> dict:
    """Walk main's first-parent history and classify how each commit arrived.

    FIRST-PARENT is the load-bearing choice. It walks the mainline only, so a
    merged PR counts once as its merge commit rather than dragging in every
    commit from the branch — those were reviewed as part of the PR, and counting
    them individually would swamp the report with work that already had eyes on
    it.
    """
    root = root or KNOWLEDGE
    fmt = "%H%x1f%an%x1f%aI%x1f%P%x1f%s%x1f%b%x1e"
    # `origin/main` is resolved to the exact remote-tracking ref's sha (git_refs):
    # a same-named tag or branch, or a tag standing in for an absent ref, would
    # otherwise hide unsigned commits from the audit. `ref` stays as given for messages.
    rev = ref
    if ref == "origin/main":
        rev = git_refs.origin_main_sha(root) or ""
        if not rev:
            return {"error": f"could not read {ref}: {git_refs.ORIGIN_MAIN_REF} does not exist in {root}",
                    "commits": [], "attested": [], "unsigned": [], "counts": {}}
    r = _git("log", "--first-parent", f"--since={since}", f"--format={fmt}", rev, root=root)
    if r.returncode != 0:
        return {"error": f"could not read {ref}: {r.stderr.strip()}",
                "commits": [], "attested": [], "unsigned": [], "counts": {}}

    commits = []
    for rec in r.stdout.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split("\x1f")
        if len(parts) < 6:
            continue
        sha, author, when, parents, subject, body = parts[:6]
        is_merge = len(parents.split()) > 1
        info = classify(subject, body, is_merge=is_merge)
        if not is_merge:
            # What the commit ACTUALLY changed, to check its claim against.
            names = _git("show", "--name-only", "--format=", sha, root=root)
            changed = [ln for ln in names.stdout.splitlines() if ln.strip()]
            info = corroborate(info, changed)
        commits.append({"sha": sha[:9], "author": author, "date": when[:10],
                        "subject": subject, **info})

    counts: dict = {}
    for c in commits:
        counts[c["path"]] = counts.get(c["path"], 0) + 1

    return {
        "since": since,
        "commits": commits,
        # What agents claimed the operator agreed to.
        "attested": [c for c in commits if c["provenance"] in ATTESTED],
        # Neither cage nor PR: the invariant says this set should be empty.
        "unsigned": [c for c in commits if c["path"] == "unsigned"],
        "counts": counts,
    }


def render(result: dict) -> str:
    """A report meant to be skimmed, with the thing that needs action first."""
    if result.get("error"):
        return f"kb audit: {result['error']}"

    counts = result["counts"]
    lines = [f"KB write audit — last {result['since']}",
             f"  {counts.get('pr', 0)} via PR · {counts.get('cage', 0)} via the cage · "
             f"{counts.get('scribe', 0)} via legacy scribe · "
             f"{counts.get('unsigned', 0)} unsigned"]

    unsigned = result["unsigned"]
    if unsigned:
        lines.append("")
        lines.append(f"BYPASSED BOTH SANCTIONED PATHS ({len(unsigned)}) — neither a cage "
                     f"write nor a PR merge:")
        for c in unsigned:
            why = f"  — {c['why']}" if c.get("why") else ""
            lines.append(f"  {c['sha']}  {c['date']}  {c['author']}  {c['subject'][:70]}{why}")
        lines.append("  Each of these reached main without review or provenance. The "
                     "ARCH-395 hook does not catch them: it blocks committing ON main, "
                     "not pushing TO it from a branch.")

    attested = result["attested"]
    lines.append("")
    if attested:
        lines.append(f"WRITTEN ON ATTESTATION ({len(attested)}) — these commits claim "
                     f"an agent's word or a person's verified edit; skim for "
                     f"anything you do not recognise:")
        for c in attested:
            who = f"  [{c['holder']}]" if c.get("holder") else ""
            if c.get("provenance") == "person" and c.get("person"):
                who += f" person={c['person']}"
            lines.append(f"  {c['sha']}  {c['date']}  {c['subject'][:80]}{who}")
    else:
        lines.append("WRITTEN ON ATTESTATION (0) — nothing to review.")

    return "\n".join(lines)


# --- who could write -----------------------------------------------------


def _workflow_grants_contents_write(doc: dict) -> bool:
    """True when *doc* (a parsed workflow file) declares, at the workflow level
    or any job's level, a `permissions:` block that grants `contents: write` —
    either directly, or via the `write-all` shorthand, which grants every scope
    including `contents`."""
    def grants(perms) -> bool:
        if perms == "write-all":
            return True
        return isinstance(perms, dict) and perms.get("contents") == "write"

    if grants(doc.get("permissions")):
        return True
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and grants(job.get("permissions")):
                return True
    return False


def _flatten(value) -> str:
    """Collapse every line break embedded in *value* — anything taken
    from a GitHub response, such as a file name or a `yaml.safe_load` error
    quoting a workflow file's own bytes — to a single space, so it cannot
    read in a rendered report as a line `render_access` wrote itself. Joins
    on `str.splitlines()` rather than a hand-listed character class, so it
    collapses exactly the set `bin/launch-weekly-kb-audit`'s
    `splitlines()`-based sanitising would otherwise split on, which runs too
    late to flatten it itself (security-reviewer, PR #803 round 2)."""
    return " ".join(str(value).splitlines())


def audit_access(get, *, org: str = "convivy", repo: str = "convivy/knowledge") -> dict:
    """Who COULD write to *repo*, per GitHub's live settings, against the
    recorded baseline above. `get(path) -> (status_code, payload)` is injected
    so this never makes a network call itself — the live GitHub client lives in
    `main()`, below, and tests supply a fake.

    Returns `{"findings": [...], "unreadable": [...]}`. A surface GitHub did not
    answer with 200 lands in `unreadable`, never silently folded into "nothing
    found" — the exact blind spot this check exists to not repeat (see the
    module docstring). `findings` carries every drift from the recorded
    baseline; an entry with `critical: True` is an un-suspension, the one
    `render_access` sorts to the top, per the issue that asked for this check.

    `get` can raise `urllib.error.URLError` (`github_app.request` catches only
    `HTTPError`, so a DNS failure or a refused connection escapes it) or a bare
    `TimeoutError` (`github_app.request`'s own `try` wraps `urlopen` and
    `resp.read()` together, but a timeout during `resp.read()` — the connection
    succeeds and the response is merely slow to finish arriving — raises
    `TimeoutError`, which is not a `URLError`). Both are caught here, as the
    common base `OSError`, per call, and folded into that call's surface as
    `unreadable`, same as a non-200 response, so a network blip or a slow read
    reads as "this surface could not be read" rather than crashing `main()`
    with Python's default exit 1, a code the job's exit-code table does not
    reserve for this (reviewer, PR #803 round 1; security-reviewer, round 2).
    """
    findings: list[dict] = []
    unreadable: list[dict] = []

    def safe_get(path: str):
        try:
            return get(path)
        except OSError as e:
            # `code` becomes a descriptive string rather than a status int, so
            # it still reads correctly through every "returned {code}" message
            # below without each one needing its own network-error branch.
            # `OSError`, not `urllib.error.URLError`, so a bare `TimeoutError`
            # raised from `resp.read()` lands here too, not just one raised
            # from `urlopen` itself.
            return f"network error: {e}", None

    expected_collaborators = _expected_collaborators()
    if expected_collaborators is None:
        unreadable.append({"surface": "collaborators",
                           "why": f"operator config at {operator_config.config_path()} "
                                  f"has no github_login, so the expected collaborator "
                                  f"is unconfigured"})
    else:
        code, payload = safe_get(f"/repos/{repo}/collaborators")
        if code != 200 or not isinstance(payload, list):
            unreadable.append({"surface": "collaborators",
                               "why": f"GET /repos/{repo}/collaborators returned {code}"})
        else:
            got = {(c.get("login"), c.get("role_name")) for c in payload if isinstance(c, dict)}
            if got != expected_collaborators:
                findings.append({"surface": "collaborators",
                                 "why": f"collaborators are {sorted(got)}, expected "
                                        f"{sorted(expected_collaborators)}"})

    code, payload = safe_get(f"/repos/{repo}/teams")
    if code != 200 or not isinstance(payload, list):
        unreadable.append({"surface": "teams",
                           "why": f"GET /repos/{repo}/teams returned {code}"})
    elif payload:
        findings.append({"surface": "teams",
                         "why": f"{len(payload)} team(s) have repository access, expected "
                                f"none: {[t.get('slug') for t in payload if isinstance(t, dict)]}"})

    code, payload = safe_get(f"/repos/{repo}/actions/permissions/workflow")
    if code != 200 or not isinstance(payload, dict):
        unreadable.append({"surface": "workflow permissions",
                           "why": f"GET /repos/{repo}/actions/permissions/workflow "
                                  f"returned {code}"})
    else:
        for key, expected in EXPECTED_WORKFLOW_PERMISSIONS.items():
            if payload.get(key) != expected:
                findings.append({"surface": "workflow permissions",
                                 "why": f"{key} is {payload.get(key)!r}, expected {expected!r}"})

    code, payload = safe_get(f"/repos/{repo}")
    default_branch = payload.get("default_branch") if code == 200 and isinstance(payload, dict) else None
    if code != 200 or not default_branch:
        unreadable.append({"surface": "workflow files",
                           "why": f"GET /repos/{repo} returned {code}; could not "
                                  f"resolve the default branch"})
    else:
        code, payload = safe_get(f"/repos/{repo}/contents/.github/workflows?ref={default_branch}")
        if code == 404:
            pass  # no workflows directory at all; nothing to check
        elif code != 200 or not isinstance(payload, list):
            unreadable.append({"surface": "workflow files",
                               "why": f"GET /repos/{repo}/contents/.github/workflows "
                                      f"returned {code}"})
        else:
            for entry in payload:
                name = entry.get("name") or "" if isinstance(entry, dict) else ""
                if not (name.endswith(".yml") or name.endswith(".yaml")):
                    continue
                fcode, fpayload = safe_get(f"/repos/{repo}/contents/.github/workflows/{name}"
                                      f"?ref={default_branch}")
                if fcode != 200 or not isinstance(fpayload, dict) or "content" not in fpayload:
                    unreadable.append({"surface": "workflow files",
                                       "why": f"could not read {_flatten(name)} ({fcode})"})
                    continue
                try:
                    text = base64.b64decode(fpayload["content"]).decode("utf-8", "replace")
                    doc = yaml.safe_load(text)
                except Exception as e:
                    # `e` is a `yaml.safe_load` error quoting the workflow
                    # file's own bytes — content from convivy/knowledge, not
                    # from this job — and PyYAML's own multi-line error text
                    # must not survive into the report as extra lines
                    # (security-reviewer, PR #803 round 2).
                    unreadable.append({"surface": "workflow files",
                                       "why": f"could not parse {_flatten(name)}: {_flatten(e)}"})
                    continue
                if isinstance(doc, dict) and _workflow_grants_contents_write(doc):
                    findings.append({"surface": "workflow files",
                                     "why": f"{_flatten(name)} declares a permissions block "
                                            f"granting contents: write"})

    code, payload = safe_get(f"/orgs/{org}/installations?per_page=100")
    installations = payload.get("installations") if code == 200 and isinstance(payload, dict) else None
    if code != 200 or not isinstance(installations, list):
        unreadable.append({"surface": "installations",
                           "why": f"GET /orgs/{org}/installations returned {code}"})
    elif isinstance(payload.get("total_count"), int) and payload["total_count"] > len(installations):
        unreadable.append({"surface": "installations",
                           "why": f"the org reports {payload['total_count']} installations but "
                                  f"this page returned {len(installations)}; refusing to treat "
                                  f"an unread page as absence"})
    else:
        live_by_slug = {inst.get("app_slug"): inst for inst in installations
                        if isinstance(inst, dict) and inst.get("app_slug")}
        for slug, inst in live_by_slug.items():
            expected = EXPECTED_INSTALLATIONS.get(slug)
            if expected is None:
                findings.append({"surface": "installations",
                                 "why": f"new installation {slug!r} (id {inst.get('id')}), "
                                        f"not in the recorded baseline"})
                continue
            if "repository_selection" in expected and \
                    inst.get("repository_selection") != expected["repository_selection"]:
                findings.append({"surface": "installations",
                                 "why": f"{_flatten(slug)}: repository_selection is "
                                        f"{inst.get('repository_selection')!r}, expected "
                                        f"{expected['repository_selection']!r}"})
            for perm_key, perm_val in expected.get("permissions", {}).items():
                live_val = (inst.get("permissions") or {}).get(perm_key)
                if live_val != perm_val:
                    findings.append({"surface": "installations",
                                     "why": f"{_flatten(slug)}: permissions.{perm_key} is "
                                            f"{live_val!r}, expected {perm_val!r}"})
            live_suspended = bool(inst.get("suspended_at"))
            if live_suspended != expected["suspended"]:
                findings.append({"surface": "installations",
                                 "why": f"{_flatten(slug)}: suspended_at is "
                                        f"{inst.get('suspended_at')!r}, expected "
                                        f"{'suspended' if expected['suspended'] else 'active'}",
                                 "critical": expected["suspended"] and not live_suspended})
        for slug in EXPECTED_INSTALLATIONS:
            if slug not in live_by_slug:
                findings.append({"surface": "installations",
                                 "why": f"{slug}: no longer appears in the installations "
                                        f"list (expected, recorded baseline)"})

    return {"findings": findings, "unreadable": unreadable}


def render_access(result: dict) -> str:
    """A report meant to be skimmed, with an un-suspension — the issue's own
    "probably the most important one this check can raise" — sorted first."""
    findings = sorted(result["findings"], key=lambda f: not f.get("critical"))
    unreadable = result["unreadable"]

    lines = ["KB access audit — who could write to convivy/knowledge, per GitHub's "
             "live settings"]

    if unreadable:
        lines.append("")
        lines.append(f"COULD NOT READ ({len(unreadable)}) — never counted as a pass:")
        for u in unreadable:
            lines.append(f"  {u['surface']}: {u['why']}")

    if findings:
        lines.append("")
        lines.append(f"DRIFT FROM THE RECORDED EXPECTATION ({len(findings)}):")
        for f in findings:
            lead = "UN-SUSPENDED — " if f.get("critical") else ""
            lines.append(f"  {lead}{f['surface']}: {f['why']}")

    if not unreadable and not findings:
        lines.append("")
        lines.append("Every surface read as expected; nothing to review.")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """The live CLI entry point `bin/launch-weekly-kb-audit` runs as its own
    `python3.11 kb_audit.py` process, separate from `audit`/`render` above,
    which agentctl's `kb audit` calls and which never touch the network.

    Authenticates as the convivy-bosun GitHub App (github_app.py) — the
    org-wide read credential this platform already mints scoped tokens from
    elsewhere (bin/pr-state-capture), never `gh`, which rides
    the operator's personal OAuth token and whose reach this job has no
    business holding.

    Exit 0: every surface read clean. 2: at least one surface could not be
    read. 4: every surface read, and at least one carries a drift from the
    recorded baseline. 2 takes priority over 4 when both apply, because an
    unreadable surface is the one thing this exit code must never let read as
    healthy.
    """
    import github_app as GA  # noqa: PLC0415 — only the live entry point needs it

    key_path = BR.real_account_home() / ".config" / "convivy" / "bosun-app.pem"
    app_id = "4666673"
    owner = "convivy"
    repo = "convivy/knowledge"

    problem = GA.key_problem(key_path, label="the convivy-bosun App")
    if problem:
        print(f"kb access audit: {problem}")
        return 2
    try:
        jwt = GA.app_jwt(key_path, app_id)
        inst = GA.single_installation(jwt, owner)
        inst_id = str(inst.get("id") or "")
        if not inst_id:
            raise GA.Unreadable("the installation carries no id")
        # Scoped to convivy/knowledge alone, as kb_merge.py already does for
        # this same App (lib/agent-platform/kb_merge.py:666-668) — every read
        # below is against that one repo, so a token with org-wide reach would
        # cost nothing to avoid and widen the blast radius of any future leak
        # (reviewer + security-reviewer, PR #803 round 1).
        token = GA.installation_token(jwt, inst_id,
                                      repositories=[repo.split("/")[1]],
                                      permissions={"contents": "read", "metadata": "read"})
    except GA.Unreadable as e:
        print(f"kb access audit: {e}")
        return 2

    def get(path: str):
        return GA.request("GET", f"{GA.API}{path}", token, None)

    result = audit_access(get, repo=repo)
    print(render_access(result))
    if result["unreadable"]:
        return 2
    return 4 if result["findings"] else 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
