"""No file in this repository creates a work item (platform/decisions/0073).

Only a co session creates an item. A monitor, a script or a scheduled job logs a signal through
`signal_client`. This guard is a literal scan of every tracked text file, whatever its extension.
It fails on any line that contains an item-creating identifier (`post_issue`, `post_bug`), an item
route (`/api/issues`, `/api/items`, `/api/bugs`, and the Library's bare `/items`, which
`vault_plane.translate` rewrites `/api/issues` to) or an item-creating tool name (`issue_create`,
`bug_create`). It reads text, not calls, so an import alias, a wrapper, a lowercase `requests.post`,
an implicit-POST `urllib` request and a tool name held in a string all match on the literal.

The literal scan also matches every read of an item route, and a comment that names one. Those lines
are listed in ALLOWED, each with its reason. A listed line is matched exactly, so a change to it
stops the allowance and fails the scan until someone reads it again.

`test_the_guard_finds_each_planted_shape` plants each shape the review named in a scratch tree, one
per directory and one in a non-`.py` file, and asserts the scan finds every one, so a passing run on
the real tree cannot be a scanner that finds nothing.

Every tracked file is scanned, a binary one included. A NUL byte does not stop bash from running a
script, so a file that holds one is read as text with undecodable bytes replaced.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TERMS = ("post_issue", "post_bug", "/api/issues", "/api/items", "/api/bugs",
         "issue_create", "bug_create")

# The Library's bare item route. A `/` or a path character after it is a per-item route
# (`/items/{id}`), which is a read or a per-item action, so the match needs a boundary.
BARE_ITEMS = re.compile(r"/items(?![\w/-])")


def _matches(text: str) -> bool:
    return any(t in text for t in TERMS) or BARE_ITEMS.search(text) is not None


EXCLUDED_DIRS = {
    "tests/": "the guard's own fixtures and the tests that drive item clients through fakes; a "
              "test is not a script or library module",
}
EXCLUDED_FILES = {
    "bin/mcp-ops-server": "the co's own tool surface: its issue_create and bug_create are the "
                          "calls a co makes",
}

# (file, stripped line) -> reason. Every entry is a read, a comment, prose or a deny list.
ALLOWED: dict[tuple[str, str], str] = {
    ("bin/agentctl", '"mcp__ops__issue_create",'):
        "_FILING_DENY: the tool names a dispatched session is denied; a deny list, not a call",
    ("bin/agentctl", '"mcp__ops__bug_create",'):
        "_FILING_DENY: the tool names a dispatched session is denied; a deny list, not a call",
    ("bin/agentctl", "# cannot reach this field. `issue_create`'s `topics` only seeds a NEW item"):
        "comment",
    ("bin/co", "# `issue_create` and `kb_create` in reach (nothing leaked — the directory was private"):
        "comment",
    ("bin/create-lab-app", "work items and files them (`issue_create`). **Every other session files nothing:**"):
        "prose the script writes into a bead for a person or session to act on; the script calls nothing",
    ("bin/issue-pr-reconcile", "loopback `/api/issues` returns the stored `refs` dict (`{url, pr, repo}`), while"):
        "comment",
    ("bin/issues-waiting", 'url = vault_plane.url(f"/api/issues?{urllib.parse.urlencode(params)}")'):
        "GET read: a urllib request with no body",
    ("lib/agent-platform/issue_client.py", "orient's /api/bugs path aliases — and will be removed after all callers migrate."):
        "docstring",
    ("lib/agent-platform/issue_client.py", 'return f"/api/issues/{urllib.parse.quote(issue_id, safe=\'\')}/{action}"'):
        "route builder for per-item actions (checkout, release, comment); the creation route takes no item id",
    ("lib/agent-platform/issue_client.py", 'url = vault_plane.url("/api/issues?state=open")'):
        "GET read: a urllib request with no body",
    ("lib/agent-platform/issue_client.py", 'f"/api/issues?state={urllib.parse.quote(state, safe=\'\')}&offset={offset}")'):
        "GET read: a urllib request with no body",
    ("lib/agent-platform/issue_client.py", 'url = vault_plane.url("/api/issues?state=claimable")'):
        "GET read: a urllib request with no body",
    ("lib/agent-platform/issue_client.py", 'url = vault_plane.url(f"/api/items/{urllib.parse.quote(item_id, safe=\'\')}")'):
        "GET read: a urllib request with no body, for a per-item route (/items/{id})",
    ("lib/agent-platform/issue_client.py", 'vault_plane.url("/api/issues?state=open") + ('):
        "GET read: a urllib request with no body",
    ("lib/agent-platform/cost_join.py",
     "`issue_create` now refuses bug/improvement/task, so `type` empties out as"):
        "docstring",
    ("lib/agent-platform/tool_policy.py", "# /api/issues, `issue_get` calls GET /api/issues/{id} — so they belong on the read"):
        "comment",
    ("lib/agent-platform/vault_plane.py", "# would get wrong: `/api/issues` looks like it should become `/issues`, which the"):
        "comment",
    ("lib/agent-platform/vault_plane.py", '_ORIENT_ITEMS = "/api/issues"'):
        "the URL rewrite's route constant. A POST built from this name is not visible to a literal "
        "scan; the Library refuses item creation server-side",
    ("lib/agent-platform/vault_plane.py", '_LIBRARY_ITEMS = "/items"'):
        "the rewrite's target constant, the Library's bare item route. A POST built from this name "
        "is not visible to a literal scan; the Library refuses item creation server-side",
    ("bin/agentctl", "# `/revision` and `POST /items` refuse `model_choice` by name. So a non-null"):
        "comment: the Library refuses a model_choice write on the bare item route by name",
    ("lib/agent-platform/vault_plane.py", "rewritten, so `/api/issues?state=open` keeps its filters."):
        "comment",
    ("scripts/ci/runner-isolation-probe.sh",
     "# tests/test_vault_plane.py show the Library serves `/items`, Orient serves"):
        "comment",
    ("scripts/ci/runner-isolation-probe.sh",
     "# `/api/issues`. convivy-open's `/health` (tools/convivy-open/install-laptop.sh)"):
        "comment",
    ("scripts/ci/runner-isolation-probe.sh", '8100) echo "/items" ;;                 # Library, derived: vault_plane.py'):
        "GET probe route: the CI isolation probe requests it unauthenticated and discards the body; creates nothing",
    ("scripts/ci/runner-isolation-probe.sh", '8099) echo "/api/issues" ;;            # Orient, derived: vault_plane.py'):
        "GET probe route: the CI isolation probe requests it unauthenticated and discards the body; creates nothing",
    ("tools/review-eval/variant-agents/reviewer.md",
     "Your review is about the diff. If you notice something outside it (a flaky-looking test in "
     "a file you opened for context, dead code in an adjacent module, a misleading docstring near "
     "the change site), do not chase it: no investigation, no verification pass, no write-up. You "
     "file nothing into the queue; the filing tools are denied on the dispatch path and `post_bug` "
     "is deleted (platform/decisions/0073). Do not route around that with `curl` or any other "
     "client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    # The same roster prose, frozen at two commits each by tools/review-eval/{pre,post}828-agents
    # (agent-platform#828's eval arms) — identical reasoning, not a call.
    ("tools/review-eval/pre828-agents/reviewer.md",
     "Your review is about the diff. If you notice something outside it (a flaky-looking test in "
     "a file you opened for context, dead code in an adjacent module, a misleading docstring near "
     "the change site), do not chase it: no investigation, no verification pass, no write-up. You "
     "file nothing into the queue; the filing tools are denied on the dispatch path and `post_bug` "
     "is deleted (platform/decisions/0073). Do not route around that with `curl` or any other "
     "client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    ("tools/review-eval/pre828-agents/security-reviewer.md",
     "Your review is about the diff. If you notice something outside it (a weak auth check in an "
     "adjacent endpoint, a logging statement in a sibling module that may leak PII, an unrelated "
     "unsafe deserialization), do not chase it: no investigation, no verification pass, no "
     "write-up. You file nothing into the queue; the filing tools are denied on the dispatch path "
     "and `post_bug` is deleted (platform/decisions/0073). Do not route around that with `curl` or "
     "any other client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    ("tools/review-eval/post828-agents/reviewer.md",
     "Your review is about the diff. If you notice something outside it (a flaky-looking test in "
     "a file you opened for context, dead code in an adjacent module, a misleading docstring near "
     "the change site), do not chase it: no investigation, no verification pass, no write-up. You "
     "file nothing into the queue; the filing tools are denied on the dispatch path and `post_bug` "
     "is deleted (platform/decisions/0073). Do not route around that with `curl` or any other "
     "client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    ("tools/review-eval/post828-agents/security-reviewer.md",
     "Your review is about the diff. If you notice something outside it (a weak auth check in an "
     "adjacent endpoint, a logging statement in a sibling module that may leak PII, an unrelated "
     "unsafe deserialization), do not chase it: no investigation, no verification pass, no "
     "write-up. You file nothing into the queue; the filing tools are denied on the dispatch path "
     "and `post_bug` is deleted (platform/decisions/0073). Do not route around that with `curl` or "
     "any other client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    # The same roster prose, frozen by tools/review-eval/fix832-agents
    # (agent-platform's #828-follow-up eval arm) — identical reasoning, not a call.
    ("tools/review-eval/fix832-agents/reviewer.md",
     "Your review is about the diff. If you notice something outside it (a flaky-looking test in "
     "a file you opened for context, dead code in an adjacent module, a misleading docstring near "
     "the change site), do not chase it: no investigation, no verification pass, no write-up. You "
     "file nothing into the queue; the filing tools are denied on the dispatch path and `post_bug` "
     "is deleted (platform/decisions/0073). Do not route around that with `curl` or any other "
     "client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
    ("tools/review-eval/fix832-agents/security-reviewer.md",
     "Your review is about the diff. If you notice something outside it (a weak auth check in an "
     "adjacent endpoint, a logging statement in a sibling module that may leak PII, an unrelated "
     "unsafe deserialization), do not chase it: no investigation, no verification pass, no "
     "write-up. You file nothing into the queue; the filing tools are denied on the dispatch path "
     "and `post_bug` is deleted (platform/decisions/0073). Do not route around that with `curl` or "
     "any other client."):
        "prose: the roster's own instruction that filing is denied/deleted, not a call",
}


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def _scanned_lines(root: Path, rels: list[str]):
    """Yield (rel, line number, stripped line) for every line of every file the scan reads.

    A file with a NUL byte is read too. Bash runs such a script, and a NUL that splits a command
    word hides the word from a scan that skipped the file; undecodable bytes are replaced."""
    for rel in rels:
        if rel in EXCLUDED_FILES or rel.startswith(tuple(EXCLUDED_DIRS)):
            continue
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            yield rel, i, line.strip()


def find_item_creation(root: Path, rels: list[str]) -> list[str]:
    """Every `file:line: text` in `rels` under `root` that matches a term and is not in ALLOWED."""
    return [f"{rel}:{i}: {text}" for rel, i, text in _scanned_lines(root, rels)
            if _matches(text) and (rel, text) not in ALLOWED]


def test_no_tracked_file_creates_an_item():
    found = find_item_creation(REPO, tracked_files(REPO))
    assert found == [], (
        "a file names an item-creating call, route or tool. Log a signal with "
        "signal_client.post_alert instead (platform/decisions/0073). A read or a comment that "
        "must stay goes in ALLOWED with its reason:\n  " + "\n  ".join(found))


def test_every_allowed_line_still_exists():
    """An entry whose line has changed or gone is an allowance nobody reads, so it fails."""
    live = {(rel, text) for rel, _, text in _scanned_lines(REPO, tracked_files(REPO))
            if _matches(text)}
    stale = sorted(key for key in ALLOWED if key not in live)
    assert stale == [], f"ALLOWED names lines that no longer exist: {stale}"


def test_the_guard_finds_each_planted_shape(tmp_path):
    """Each shape the review named, planted in its own directory, is found. The fifth is a shell
    script, so the scan does not depend on the extension."""
    shapes = {
        # an import alias, then a call through the alias
        "bin/planted-alias": "from issue_client import post_issue as file_it\nfile_it('p', 't')\n",
        # the route in a private wrapper's argument
        "lib/agent-platform/planted-post.py": "req = _post('/api/issues', payload, tok)\n",
        # a lowercase requests.post
        "tools/convivy-open/planted-requests.py":
            "requests.post(vault_plane.url('/api/issues'), json=body)\n",
        # a urllib Request with a body, an implicit POST
        "lib/knowledge/planted-urllib.py":
            "req = urllib.request.Request(vault_plane.url('/api/items'), data=body)\n",
        # a tool name held in a string, in a non-.py file
        "lib/planted-tool-name.sh": 'call_tool "issue_create" "$payload"\n',
        # the Library's bare item route, the form vault_plane.translate rewrites /api/issues to
        "lib/agent-platform/planted-library-items.py":
            "req = urllib.request.Request(vault_plane.fixed_library_url('/items'), data=body)\n",
    }
    for rel, text in shapes.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)

    found = find_item_creation(tmp_path, list(shapes))

    assert sorted(f.split(":")[0] for f in found) == sorted(shapes)


def test_a_nul_byte_does_not_hide_an_item_creation_call(tmp_path):
    """Bash runs a script that holds a NUL byte, so a call on a line after one must still be found."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "nul-script").write_bytes(b"#!/bin/bash\necho ok\x00\npost_issue 'p' 't'\n")
    assert find_item_creation(tmp_path, ["bin/nul-script"]) == [
        "bin/nul-script:3: post_issue 'p' 't'"]


def test_a_new_read_on_an_item_route_is_flagged_until_it_is_listed(tmp_path):
    """The allowance is per line, not per term: a read the repo has not listed fails the scan."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "reader").write_text('items = _get("/api/items", {"state": "open"})\n')
    assert find_item_creation(tmp_path, ["bin/reader"]) == [
        'bin/reader:1: items = _get("/api/items", {"state": "open"})']


def test_a_comment_naming_a_create_call_is_flagged_until_it_is_listed(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "note.py").write_text("# do not call post_issue here\n")
    assert find_item_creation(tmp_path, ["lib/note.py"]) == [
        "lib/note.py:1: # do not call post_issue here"]


def test_a_bare_items_route_is_flagged_but_a_per_item_route_is_not(tmp_path):
    """`/items` alone is the creation route. `/items/{id}` is a read of one item and is not a match."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shape.py").write_text(
        "a = vault_plane.url('/items')\n"
        "b = fixed_library_url(f\"/items/{issue_id}\")\n")
    assert find_item_creation(tmp_path, ["lib/shape.py"]) == [
        "lib/shape.py:1: a = vault_plane.url('/items')"]


def test_the_exclusions_are_not_scanned(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.py").write_text("post_issue('p', 't')\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "mcp-ops-server").write_text("def issue_create(): ...\n")
    assert find_item_creation(tmp_path, ["tests/fixture.py", "bin/mcp-ops-server"]) == []
