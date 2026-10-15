"""The `kb` subcommands' argparse wiring, driven through the REAL parser.

Every other kb test hands its command a `Namespace` built by hand, which cannot
catch the one failure mode argparse owns: a `dest` that does not match what the
function reads. That mismatch passes every existing test and raises
`AttributeError` the first time a person runs the command — the shape where a
green suite is actively misleading. Found as a coverage gap by round-3 review
of #247; the wiring was correct, the proof was missing.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def parsed(agentctl, monkeypatch):
    """Drive `agentctl.main()` and capture the Namespace the command receives,
    without executing the command itself."""
    seen: dict = {}

    def capture(args):
        seen.clear()
        seen.update(vars(args))
        return 0

    monkeypatch.setattr(agentctl, "cmd_kb_apply", capture)
    monkeypatch.setattr(agentctl, "cmd_kb_propose", capture)
    monkeypatch.setattr(agentctl, "cmd_kb_checkout", capture)
    monkeypatch.setattr(agentctl, "cmd_kb_release", capture)
    monkeypatch.setattr(agentctl, "cmd_kb_leases", capture)
    monkeypatch.setattr(agentctl, "cmd_kb_sync", capture)

    def run(argv):
        seen.clear()
        agentctl.main(argv)
        return dict(seen)
    return run


# --- the dests the functions actually read -----------------------------------


def test_apply_exposes_content_and_edits_in_both_forms(parsed):
    ns = parsed(["kb", "apply", "projects/x/a.md", "--base-sha", "s",
                 "--provenance", "steward", "--content-file", "/tmp/body.md"])
    assert ns["content_file"] == "/tmp/body.md"
    assert ns["content"] is None and ns["edits"] is None and ns["edits_file"] is None

    ns = parsed(["kb", "apply", "projects/x/a.md", "--base-sha", "s",
                 "--provenance", "steward", "--content", "literal body"])
    assert ns["content"] == "literal body" and ns["content_file"] is None


def test_propose_exposes_the_same_four_flags(parsed):
    """propose and apply must agree, since they share `_kb_read_change` — a dest
    correct in one and wrong in the other would only break the other command."""
    ns = parsed(["kb", "propose", "projects/x/a.md", "--title", "T",
                 "--edits-file", "/tmp/edits.json"])
    assert ns["edits_file"] == "/tmp/edits.json"
    assert ns["content_file"] is None and ns["edits"] is None


@pytest.mark.parametrize("cmd", ["apply", "propose"])
def test_every_dest_read_by_the_command_exists_on_the_namespace(parsed, cmd):
    """The direct guard against a dest mismatch: whatever the command reads must
    be present, or it is an AttributeError at runtime rather than a test failure."""
    argv = (["kb", "apply", "projects/x/a.md", "--base-sha", "s", "--provenance", "steward"]
            if cmd == "apply" else ["kb", "propose", "projects/x/a.md", "--title", "T"])
    ns = parsed(argv + ["--content", "body"])
    for dest in ("doc", "content", "content_file", "edits", "edits_file", "holder",
                 "allow_overwrite", "dry_run"):
        assert dest in ns, f"kb {cmd} is missing `{dest}`"


def test_apply_carries_its_own_extra_dests(parsed):
    ns = parsed(["kb", "apply", "projects/x/a.md", "--base-sha", "s",
                 "--provenance", "steward", "--content", "b"])
    for dest in ("base_sha", "provenance", "message", "no_push", "no_sync"):
        assert dest in ns


def test_propose_carries_its_own_extra_dests(parsed):
    ns = parsed(["kb", "propose", "projects/x/a.md", "--title", "T", "--content", "b"])
    for dest in ("title", "rationale", "base_sha", "delete", "also"):
        assert dest in ns
    assert ns["delete"] is False and ns["also"] == []


def test_an_option_after_the_end_of_options_is_a_path_not_a_holder(parsed):
    """The MCP server passes every caller value after `--`. This pins that the
    real parser honours it: an injected `--holder` becomes a path the cage then
    refuses to resolve, not the identity every lease check compares (security
    review of PR 537)."""
    ns = parsed(["kb", "propose", "--title", "T", "--delete", "--",
                 "projects/x/a.md", "--holder", "session:victim"])
    assert ns["holder"] is None
    assert ns["doc"] == "projects/x/a.md"
    assert ns["also"] == ["--holder", "session:victim"]
    ns = parsed(["kb", "checkout", "--", "--holder=session:victim"])
    assert ns["holder"] is None and ns["doc"] == "--holder=session:victim"


def test_a_delete_proposal_takes_several_paths(parsed):
    ns = parsed(["kb", "propose", "projects/x/a.md", "projects/x/profiles/daytime.yaml",
                 "--delete", "--title", "T"])
    assert ns["doc"] == "projects/x/a.md"
    assert ns["also"] == ["projects/x/profiles/daytime.yaml"]
    assert ns["delete"] is True


# --- the other kb verbs ------------------------------------------------------


def test_checkout_release_and_leases_parse(parsed):
    assert parsed(["kb", "checkout", "agent-platform/agents/architect"])["doc"]
    assert parsed(["kb", "release", "agent-platform/agents/architect"])["doc"]
    assert parsed(["kb", "leases", "--json"])["json"] is True


def test_checkout_sha_only_parses_and_defaults_false(parsed):
    assert parsed(["kb", "checkout", "agent-platform/agents/architect"])["sha_only"] is False
    ns = parsed(["kb", "checkout", "--sha-only", "agent-platform/agents/architect"])
    assert ns["sha_only"] is True


def test_sync_flags_parse(parsed):
    ns = parsed(["kb", "sync", "--verify-only", "--no-heal"])
    assert ns["verify_only"] is True and ns["no_heal"] is True


# --- required arguments ------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["kb", "apply", "projects/x/a.md", "--provenance", "steward"],   # no --base-sha
    ["kb", "apply", "projects/x/a.md", "--base-sha", "s"],           # no --provenance
    ["kb", "propose", "projects/x/a.md"],                            # no --title
])
def test_missing_required_arguments_exit_nonzero(parsed, argv):
    """The precondition and the provenance are not optional; argparse is what
    enforces that, so it is worth pinning here rather than assuming."""
    with pytest.raises(SystemExit) as e:
        parsed(argv)
    assert e.value.code != 0


def _kb_apply_parser(agentctl, monkeypatch):
    """The real `kb apply` subparser, reached by capturing the parser `main()` builds."""
    import argparse
    found = {}
    real = argparse.ArgumentParser.parse_args

    def spy(self, *a, **k):
        found.setdefault("root", self)
        return real(self, *a, **k)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    monkeypatch.setattr(agentctl, "cmd_kb_apply", lambda args: 0)
    agentctl.main(["kb", "apply", "projects/x/a.md", "--base-sha", "s",
                   "--provenance", "person", "--content", "b"])
    node = found["root"]
    for name in ("kb", "apply"):
        sub = next(a for a in node._actions if isinstance(a, argparse._SubParsersAction))
        node = sub.choices[name]
    return node


def test_apply_accepts_person_and_no_option_takes_an_assertion(agentctl, monkeypatch, parsed):
    """The assertion travels on stdin only: argv shows in `ps`, and a value flag
    would put a live bearer credential there. Asserted over the parser's own
    option strings, since a negative parse ('--access-assertion is rejected')
    passes vacuously for any name nobody thought of."""
    ns = parsed(["kb", "apply", "projects/x/a.md", "--base-sha", "s",
                 "--provenance", "person", "--content", "b"])
    assert ns["provenance"] == "person"
    options = sorted(o for a in _kb_apply_parser(agentctl, monkeypatch)._actions
                     for o in a.option_strings)
    assert options, "no option strings read; the walk found the wrong parser"
    assert "--provenance" in options
    banned = ("assert", "jwt", "token", "access", "cred", "cookie", "bearer")
    assert [o for o in options if any(b in o.lower() for b in banned)] == []
