"""The agent policy parser and file classifier (lib/agent-platform/agent_policy.py).

The fixture is the policy file as merged in convivy/shipwright PR #61, commit
6fe95376450388728e9bfb6b559dca259364c329, byte for byte. Its comments are the
specification the module implements.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import agent_policy as ap

SHIPPED = Path(__file__).parent / "fixtures" / "agent-policy-6fe9537.yaml"
SHIPPED_SHA256 = "2a3172fe4d641baac927fa75274e1fc53df8a8dc5dade643e957adb92ef5d58d"


def shipped() -> str:
    return SHIPPED.read_text(encoding="utf-8")


def test_the_fixture_is_the_file_as_merged():
    assert hashlib.sha256(SHIPPED.read_bytes()).hexdigest() == SHIPPED_SHA256


def test_the_shipped_file_parses_to_its_shipped_values():
    assert ap.load(shipped()) == ap.Policy(
        policy="platform/agent-policy",
        schema_version=1,
        agents_may_merge_knowledge=False,
        agents_may_merge_knowledge_instructions=False,
        agents_may_merge_documents=False,
        review_rounds_per_role=5,
        review_rounds_code=11,
        max_concurrent=16,
        max_workers_per_team=4,
        max_live_supervisors=4,
    )


def test_the_shipped_file_has_no_review_rounds_code_line_so_the_code_cap_is_the_decided_eleven():
    """A readable file with no code line is not broken: the decision's code cap applies."""
    assert "review_rounds_code" not in shipped()
    got = ap.load(shipped())
    assert isinstance(got, ap.Policy)
    assert (got.review_rounds_per_role, got.review_rounds_code) == (5, ap.DECIDED_REVIEW_ROUNDS_CODE) == (5, 11)


def test_review_rounds_code_is_read_from_the_file_when_it_names_it():
    text = swap("review_rounds_per_role: 5\n", "review_rounds_per_role: 9\nreview_rounds_code: 13\n")
    got = ap.load(text)
    assert isinstance(got, ap.Policy)
    assert (got.review_rounds_per_role, got.review_rounds_code) == (9, 13)


def test_values_are_typed_and_read_from_the_file_not_defaulted():
    text = shipped().replace("agents_may_merge_documents: false", "agents_may_merge_documents: true").replace(
        "max_concurrent: 16", "max_concurrent: 1000"
    )
    got = ap.load(text)
    assert isinstance(got, ap.Policy)
    assert got.agents_may_merge_documents is True
    assert got.max_concurrent == 1000


# ---- Fallback -------------------------------------------------------------


def test_fallback_carries_the_values_the_file_names_for_it():
    fb = ap.load(None)
    assert isinstance(fb, ap.Fallback)
    assert "missing" in fb.reason
    assert (fb.agents_may_merge_knowledge, fb.agents_may_merge_knowledge_instructions, fb.agents_may_merge_documents) == (
        False,
        False,
        False,
    )
    assert (fb.review_rounds_per_role, fb.review_rounds_code, fb.max_concurrent, fb.max_workers_per_team,
            fb.max_live_supervisors) == (5, 5, 16, 4, 4)


def test_the_module_constants_match_the_fallback_the_fixture_names():
    assert "5 review rounds per\n# role, and 16, 4 and 4 for the concurrency limits" in shipped()
    assert (ap.FALLBACK_REVIEW_ROUNDS_PER_ROLE, ap.FALLBACK_MAX_CONCURRENT, ap.FALLBACK_MAX_WORKERS_PER_TEAM,
            ap.FALLBACK_MAX_LIVE_SUPERVISORS) == (5, 16, 4, 4)
    assert ap.FALLBACK_REVIEW_ROUNDS_CODE == ap.FALLBACK_REVIEW_ROUNDS_PER_ROLE == 5
    assert ap.DECIDED_REVIEW_ROUNDS_CODE == 11


def swap(old: str, new: str) -> str:
    text = shipped()
    assert old in text
    return text.replace(old, new, 1)


LINE_RULE_VIOLATIONS = [
    ("quoted policy name", swap("policy: platform/agent-policy", 'policy: "platform/agent-policy"'), "line 1"),
    ("wrong policy name", swap("policy: platform/agent-policy", "policy: platform/other"), "line 1"),
    ("schema version 2", swap("schema_version: 1", "schema_version: 2"), "schema_version"),
    ("schema version 01", swap("schema_version: 1", "schema_version: 01"), "schema_version"),
    ("indented key", swap("\nmax_concurrent: 16", "\n  max_concurrent: 16"), "first column"),
    ("trailing comment", swap("max_concurrent: 16", "max_concurrent: 16 # cap"), "nothing after the value"),
    ("trailing space", swap("max_concurrent: 16", "max_concurrent: 16 "), "nothing after the value"),
    ("no space after colon", swap("max_concurrent: 16", "max_concurrent:16"), "key: value"),
    ("two spaces after colon", swap("max_concurrent: 16", "max_concurrent:  16"), "key: value"),
    ("quoted number", swap("max_concurrent: 16", 'max_concurrent: "16"'), "1 to 1000"),
    ("carriage return", shipped().replace("\n", "\r\n"), "key: value"),
    ("capitalised boolean", swap("agents_may_merge_documents: false", "agents_may_merge_documents: False"), "true or false"),
    ("yes for a boolean", swap("agents_may_merge_documents: false", "agents_may_merge_documents: yes"), "true or false"),
    ("boolean as a number", swap("agents_may_merge_documents: false", "agents_may_merge_documents: 1"), "true or false"),
    ("number zero", swap("max_concurrent: 16", "max_concurrent: 0"), "1 to 1000"),
    ("number 1001", swap("max_concurrent: 16", "max_concurrent: 1001"), "1 to 1000"),
    ("leading zero", swap("max_concurrent: 16", "max_concurrent: 016"), "leading zero"),
    ("negative number", swap("max_concurrent: 16", "max_concurrent: -4"), "1 to 1000"),
    ("decimal number", swap("max_concurrent: 16", "max_concurrent: 4.0"), "1 to 1000"),
    ("word number", swap("max_concurrent: 16", "max_concurrent: sixteen"), "1 to 1000"),
    ("non-ascii digits", swap("max_concurrent: 16", "max_concurrent: ١٦"), "1 to 1000"),
    ("blank value", swap("max_concurrent: 16", "max_concurrent:"), "key: value"),
    ("duplicate key", shipped() + "max_concurrent: 16\n", "more than once"),
    ("extra key", shipped() + "max_bosses: 3\n", "not a key"),
    ("nested key", shipped() + "  nested: 3\n", "first column"),
    ("uppercase key", swap("max_concurrent: 16", "Max_concurrent: 16"), "key: value"),
    ("missing key", swap("max_live_supervisors: 4\n", ""), "max_live_supervisors"),
    ("empty file", "", "policy"),
    ("a line that is not a setting", shipped() + "just some words\n", "key: value"),
]


@pytest.mark.parametrize("label,text,rule", LINE_RULE_VIOLATIONS, ids=[v[0] for v in LINE_RULE_VIOLATIONS])
def test_a_line_rule_violation_falls_back_naming_the_rule(label, text, rule):
    got = ap.load(text)
    assert isinstance(got, ap.Fallback), label
    assert rule in got.reason, got.reason
    assert (got.review_rounds_per_role, got.review_rounds_code, got.max_concurrent, got.max_workers_per_team,
            got.max_live_supervisors) == (5, 5, 16, 4, 4)
    assert not (got.agents_may_merge_knowledge or got.agents_may_merge_knowledge_instructions or got.agents_may_merge_documents)


def test_a_violation_names_the_line_number():
    got = ap.load(swap("max_concurrent: 16", "max_concurrent: 0"))
    assert isinstance(got, ap.Fallback)
    line = shipped().split("\n").index("max_concurrent: 16") + 1
    assert f"line {line}:" in got.reason


def test_a_changed_value_is_not_hidden_by_a_fallback_shaped_default():
    got = ap.load(swap("review_rounds_per_role: 5", "review_rounds_per_role: 7"))
    assert isinstance(got, ap.Policy) and got.review_rounds_per_role == 7


CODE_CAP_LINE_VIOLATIONS = [
    ("zero", "review_rounds_code: 0", "1 to 1000"),
    ("number 1001", "review_rounds_code: 1001", "1 to 1000"),
    ("leading zero", "review_rounds_code: 011", "leading zero"),
    ("quoted number", 'review_rounds_code: "11"', "1 to 1000"),
    ("word number", "review_rounds_code: eleven", "1 to 1000"),
    ("blank value", "review_rounds_code:", "key: value"),
    ("trailing comment", "review_rounds_code: 11 # code", "nothing after the value"),
]


@pytest.mark.parametrize("label,line,rule", CODE_CAP_LINE_VIOLATIONS, ids=[v[0] for v in CODE_CAP_LINE_VIOLATIONS])
def test_review_rounds_code_is_validated_like_the_other_number_keys(label, line, rule):
    got = ap.load(swap("review_rounds_per_role: 5\n", f"review_rounds_per_role: 5\n{line}\n"))
    assert isinstance(got, ap.Fallback), label
    assert rule in got.reason and "review_rounds_code" in got.reason, got.reason
    assert got.review_rounds_code == ap.FALLBACK_REVIEW_ROUNDS_CODE


def test_review_rounds_code_given_twice_is_a_violation():
    got = ap.load(swap("review_rounds_per_role: 5\n", "review_rounds_per_role: 5\nreview_rounds_code: 11\nreview_rounds_code: 13\n"))
    assert isinstance(got, ap.Fallback) and "review_rounds_code appears more than once" in got.reason


def test_making_review_rounds_code_optional_does_not_make_the_other_keys_optional():
    got = ap.load(swap("policy: platform/agent-policy\n", ""))
    assert isinstance(got, ap.Fallback) and "has no line for policy" in got.reason


def test_review_round_cap_sentence_names_both_caps_and_which_pull_requests_take_each():
    got = ap.load(swap("review_rounds_per_role: 5\n", "review_rounds_per_role: 9\nreview_rounds_code: 13\n"))
    assert ap.review_round_cap_sentence(got) == (
        "The review-round cap is 13 rounds for a pull request that changes code or any file an agent is "
        "instructed to act on, and 9 rounds for one that changes only documents. A pull request that "
        "changes code or any file an agent is instructed to act on runs rounds past 9 only while no finding "
        "in the latest round repeats one an earlier round raised."
    )


def test_review_round_cap_sentence_for_the_fallback_states_the_fallback_caps_without_the_progress_condition():
    # Both caps are 5 here, so no round runs past the document cap. A progress clause would
    # read as permission for rounds the fallback does not grant.
    assert ap.review_round_cap_sentence(ap.load(None)) == (
        "The review-round cap is 5 rounds for a pull request that changes code or any file an agent is "
        "instructed to act on, and 5 rounds for one that changes only documents."
    )


def test_review_round_cap_sentence_for_the_decided_code_cap_states_eleven_and_seven_with_the_progress_condition():
    got = ap.load(swap("review_rounds_per_role: 5\n", "review_rounds_per_role: 7\n"))
    assert ap.review_round_cap_sentence(got) == (
        "The review-round cap is 11 rounds for a pull request that changes code or any file an agent is "
        "instructed to act on, and 7 rounds for one that changes only documents. A pull request that "
        "changes code or any file an agent is instructed to act on runs rounds past 7 only while no finding "
        "in the latest round repeats one an earlier round raised."
    )


def test_blank_lines_and_comments_are_ignored_anywhere():
    text = "\n# a comment\n   \n" + shipped() + "\n\n# more\n"
    assert isinstance(ap.load(text), ap.Policy)


# ---- classification: the cases every reader must test ----------------------

DOC = "projects/x/notes.md"


def fm(*lines: str) -> str:
    return "\n".join(["---", *lines, "---", "body", ""])


def cl(path_before, text_before, path_after, text_after, *, mode_before="100644", mode_after="100644"):
    """`ap.classify` with regular-file modes unless a test says otherwise; the modes have no default in the module."""
    return ap.classify(path_before, text_before, path_after, text_after, mode_before=mode_before, mode_after=mode_after)


def sides(path: str, text: str):
    """The same file as an addition, a deletion, an edit with a benign other side, and its reverse."""
    benign = "body\n"
    yield cl(None, None, path, text)
    yield cl(path, text, None, None)
    yield cl(path, benign, path, text)
    yield cl(path, text, path, benign)


def assert_instruction(path: str, text: str):
    for got in sides(path, text):
        assert got.instruction and not got.unclassifiable, got
        assert not got.knowledge_document and not got.document
        assert got.reasons


def test_a_plain_document_is_a_knowledge_document():
    got = cl(None, None, DOC, "body\n")
    assert got == ap.Classification(False, False, True, False, ())


@pytest.mark.parametrize("kind", ["agent", "skill", "runbook", "instruction-block"])
def test_case_instruction_kind(kind):
    assert_instruction(DOC, fm(f"kind: {kind}"))


@pytest.mark.parametrize(
    "value",
    ["'agent'", '"agent"', "!!str agent", "&a agent", "*a", "agent #x"],
    ids=["single-quoted", "double-quoted", "tagged", "anchor", "alias", "trailing-comment"],
)
def test_case_yaml_spellings_of_a_kind(value):
    assert_instruction(DOC, fm(f"kind: {value}"))


def test_case_kind_agent_with_dashes_after_it():
    assert_instruction(DOC, fm("kind: agent---"))


def test_case_any_line_containing_dashes_between_the_delimiters():
    assert_instruction(DOC, fm("kind: note", "title: a --- b"))


@pytest.mark.parametrize("line", ["kind : agent", "kind:  agent"], ids=["space-before-colon", "two-spaces"])
def test_case_kind_spacing(line):
    assert_instruction(DOC, fm(line))


def test_case_duplicate_kind():
    assert_instruction(DOC, fm("kind: note", "kind: agent"))


def test_case_duplicate_kind_whose_last_value_is_harmless():
    assert_instruction(DOC, fm("kind: agent", "kind: note"))
    assert_instruction(DOC, fm("title: a", "title: b"))


@pytest.mark.parametrize("line", ["Kind: agent", '"kind": agent'], ids=["capital-key", "quoted-key"])
def test_case_kind_key_spelling(line):
    assert_instruction(DOC, fm(line))


NON_PRINTABLE = ["\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\r", "\ufeff"]
NON_PRINTABLE_IDS = ["vertical-tab", "form-feed", "u0085", "u2028", "u2029", "carriage-return", "byte-order-mark"]


@pytest.mark.parametrize("char", NON_PRINTABLE, ids=NON_PRINTABLE_IDS)
def test_case_a_line_break_character_inside_a_free_key_value(char):
    # `title` accepts any value, so only the printable rule can catch this.
    assert_instruction(DOC, fm(f"title: a{char}kind: agent"))


@pytest.mark.parametrize("char", NON_PRINTABLE, ids=NON_PRINTABLE_IDS)
def test_case_a_line_break_character_in_a_file_with_no_front_matter(char):
    assert_instruction(DOC, f"body a{char}b\n")


def test_case_carriage_return_anywhere():
    assert_instruction(DOC, "body\r\nmore\n")
    assert_instruction(DOC, fm("kind: note").replace("\n", "\r\n"))


def test_case_byte_order_mark():
    assert_instruction(DOC, "﻿" + fm("kind: note"))


@pytest.mark.parametrize(
    "text",
    [
        "--- \nkind: note\n---\nbody\n",
        "---\nkind: note\n--- \nbody\n",
        "---\nkind: note\nbody with no closing line\n",
    ],
    ids=["opener-trailing-space", "closer-trailing-space", "never-closes"],
)
def test_case_inexact_delimiters(text):
    assert_instruction(DOC, text)


@pytest.mark.parametrize(
    "audience",
    ["audience: [team, reviewer]", "audience: [Team]", "audience: team"],
)
def test_case_decision_audience_other_than_team(audience):
    assert_instruction(DOC, fm("kind: decision", audience))


def test_case_decision_with_no_audience():
    assert_instruction(DOC, fm("kind: decision"))


def test_a_decision_for_team_only_is_not_an_instruction():
    got = cl(None, None, DOC, fm("kind: decision", "audience: [team]"))
    assert not got.instruction and got.knowledge_document


def test_audience_counts_as_team_only_on_an_exact_line():
    for line in ("audience: [ team ]", "audience: [team] ", "audience: [team,]", "audience: [team]#x"):
        assert_instruction(DOC, fm("kind: decision", line))


@pytest.mark.parametrize("name", ["claude.md", "CLAUDE.md", "Claude.MD", "CLAUDE.local.md", "claude.LOCAL.md", "AGENTS.md", "agents.md"])
def test_case_named_instruction_files(name):
    assert_instruction(f"projects/x/{name}", "body\n")
    assert_instruction(f"docs/{name}", "body\n")


@pytest.mark.parametrize("path", ["Agents/a.md", "RUNBOOKS/a.md", "projects/x/agents/a.md", "docs/Skills/a.md",
                                  "projects/x/Instruction-Blocks/a.md", "projects/x/PROFILES/a.md"])
def test_case_a_file_under_an_instruction_directory(path):
    assert_instruction(path, "body\n")


def test_case_a_symlink():
    for got in (
        cl(None, None, DOC, "target.md", mode_after="120000"),
        cl(DOC, "target.md", None, None, mode_before="120000"),
        cl(DOC, "body\n", DOC, "target.md", mode_before="100644", mode_after="120000"),
    ):
        assert got.instruction and not got.unclassifiable


def test_a_present_side_with_no_mode_is_unclassifiable():
    for got in (
        cl(None, None, DOC, "target.md", mode_after=None),
        cl(DOC, "target.md", None, None, mode_before=None),
        cl(DOC, "body\n", DOC, "body\n", mode_before=None),
        cl(DOC, "body\n", DOC, "body\n", mode_after=None),
    ):
        assert got.unclassifiable and not got.instruction and not got.knowledge_document and not got.document


def test_the_mode_of_an_absent_side_is_ignored():
    assert cl(None, None, DOC, "body\n", mode_before=None).knowledge_document
    assert cl(DOC, "body\n", None, None, mode_after=None).knowledge_document


def test_the_modes_have_no_default():
    with pytest.raises(TypeError):
        ap.classify(None, None, DOC, "body\n")


def test_a_submodule_is_not_a_regular_file():
    assert cl(None, None, DOC, "x", mode_after="160000").instruction


def test_an_executable_regular_file_is_still_regular():
    assert cl(None, None, DOC, "body\n", mode_after="100755").knowledge_document


@pytest.mark.parametrize("path", ["projects/x/data.yaml", "projects/x/notes.txt", "projects/x/notes", "docs/logo.png"])
def test_a_file_not_ending_in_md_is_an_instruction(path):
    assert_instruction(path, "body\n")


@pytest.mark.parametrize(
    "lines",
    [
        ["kind: note", "title: one", "  continued on a second line"],
        ["kind: note", "", "title: one"],
        ["kind: note", "title: one ", ],
        ["kind: note", "title:one"],
        ["kind: note", "key with spaces: one"],
    ],
    ids=["multi-line-value", "blank-line-inside", "trailing-space-value", "no-space-after-colon", "spaced-key"],
)
def test_case_front_matter_the_rule_cannot_read(lines):
    assert_instruction(DOC, fm(*lines))


def test_case_a_file_with_front_matter_is_not_a_document():
    for got in sides("docs/a.md", fm("kind: note")):
        assert not got.document and not got.instruction


def test_case_a_docs_directory_that_is_not_at_the_top():
    for path in ("sub/docs/a.md", "projects/docs/a.md", "docs.md"):
        assert not cl(None, None, path, "body\n").document


def test_a_docs_file_without_front_matter_is_a_document():
    assert cl(None, None, "docs/a.md", "body\n").document
    assert cl(None, None, "DOCS/deep/er/a.md", "body\n").document


def test_a_horizontal_rule_opening_the_file_is_front_matter():
    got = cl(None, None, "docs/a.md", "---\nbody\n")
    assert got.instruction  # `---\nbody` cannot be read as key: value


def test_free_keys_may_carry_any_value():
    got = cl(None, None, DOC, fm("kind: note", "shipped_in: 'v1' # ok", "title: Hello, world"))
    assert got.knowledge_document and not got.instruction


# ---- both sides: addition, deletion, edit, rename --------------------------


def test_an_addition_is_judged_on_its_only_side():
    assert cl(None, None, DOC, "body\n").knowledge_document
    assert cl(None, None, "agents/a.md", "body\n").instruction


def test_a_deletion_is_judged_on_its_only_side():
    assert cl(DOC, "body\n", None, None).knowledge_document
    assert cl("agents/a.md", "body\n", None, None).instruction
    assert cl("docs/a.md", fm("kind: agent"), None, None).instruction


def test_an_edit_that_turns_a_note_into_an_agent_is_an_instruction():
    got = cl(DOC, "body\n", DOC, fm("kind: agent"))
    assert got.instruction


def test_an_edit_that_turns_an_agent_into_a_note_is_still_an_instruction():
    got = cl(DOC, fm("kind: agent"), DOC, "body\n")
    assert got.instruction


def test_a_clean_edit_is_a_knowledge_document():
    assert cl(DOC, "one\n", DOC, "two\n").knowledge_document


def test_a_rename_from_docs_into_agents_is_an_instruction():
    got = cl("docs/a.md", "body\n", "agents/a.md", "body\n")
    assert got.instruction and not got.document


def test_a_rename_from_agents_into_docs_is_an_instruction():
    got = cl("agents/a.md", "body\n", "docs/a.md", "body\n")
    assert got.instruction and not got.document


def test_a_rename_between_docs_files_is_a_document():
    assert cl("docs/a.md", "body\n", "docs/b.md", "body\n").document


def test_a_rename_from_docs_to_projects_is_neither_document_nor_knowledge():
    got = cl("docs/a.md", "body\n", "projects/a.md", "body\n")
    assert got == ap.Classification(False, False, False, False, ())


def test_a_rename_from_projects_to_the_repository_root_is_not_knowledge():
    assert not cl(DOC, "body\n", "top.md", "body\n").knowledge_document


def test_a_rename_to_a_named_instruction_file_is_an_instruction():
    assert cl(DOC, "body\n", "projects/x/claude.md", "body\n").instruction


def test_a_knowledge_path_in_another_letter_case_is_knowledge():
    assert cl(None, None, "Projects/X/a.md", "body\n").knowledge_document


@pytest.mark.parametrize("path", ["projectſ/a.md", "PROJECTſ/a.md", "projeCTs\u212a/a.md", "\u0131projects/a.md", "projects\u0131/a.md"])
def test_a_look_alike_of_projects_is_not_a_knowledge_document(path):
    got = cl(None, None, path, "body\n")
    assert not got.knowledge_document and not got.document and not got.instruction


@pytest.mark.parametrize("path", ["docſ/a.md", "doc\u017f/a.md", "\u0131docs/a.md", "docs\u212a/a.md", "d\u0131cs/a.md"])
def test_a_look_alike_of_docs_is_not_a_document(path):
    got = cl(None, None, path, "body\n")
    assert not got.knowledge_document and not got.document and not got.instruction


def test_the_ascii_case_variants_of_docs_and_projects_still_count():
    assert cl(None, None, "PROJECTS/x/a.md", "body\n").knowledge_document
    assert cl(None, None, "Docs/a.md", "body\n").document


def test_classify_alone_does_not_ban_the_policy_directory():
    path = "projects/platform/policy/README.md"
    assert cl(None, None, path, "body\n").knowledge_document
    assert ap.touches_policy(None, path)


# ---- under_projects -----------------------------------------------------


_UNDER_PROJECTS_CASES = (
    ("projects/x", True),
    ("PROJECTS/x", True),
    ("projects", False),
    ("projectſ/x", False),
    ("docs/x", False),
)


@pytest.mark.parametrize("path,expected", _UNDER_PROJECTS_CASES)
def test_under_projects(path, expected):
    assert ap.under_projects(path) is expected


def test_classify_and_kb_merge_under_projects_agree():
    import kb_merge as km

    for path, expected in _UNDER_PROJECTS_CASES:
        assert ap.under_projects(path) is expected
        assert km._under_projects(path) is expected


# ---- unclassifiable ---------------------------------------------------------


def test_no_path_on_either_side_is_unclassifiable():
    got = cl(None, None, None, None)
    assert got.unclassifiable and not got.instruction and not got.knowledge_document and not got.document


def test_a_present_path_with_no_text_is_unclassifiable():
    for got in (
        cl(None, None, DOC, None),
        cl(DOC, None, None, None),
        cl(DOC, "body\n", DOC, None),
    ):
        assert got.unclassifiable and not got.knowledge_document and not got.document


def test_an_empty_path_is_unclassifiable():
    assert cl(None, None, "", "body\n").unclassifiable


# ---- touches_policy ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "projects/platform/policy/agent-policy.yaml",
        "projects/Platform/Policy/agent-policy.yaml",
        "PROJECTS/PLATFORM/POLICY/x.md",
        "projects/platform/policy/sub/dir/x",
        "projects/platform/policy",
    ],
)
def test_touches_policy_in_any_letter_case_on_either_side(path):
    assert ap.touches_policy(path, None)
    assert ap.touches_policy(None, path)
    assert ap.touches_policy("docs/a.md", path)
    assert ap.touches_policy(path, "docs/a.md")


@pytest.mark.parametrize(
    "path",
    ["projects/platform/policy-notes.md", "projects/platform/policies/a.md", "projects/other/policy/a.md",
     "docs/projects/platform/policy/a.md", "projects/platform/a.md"],
)
def test_touches_policy_is_false_elsewhere(path):
    assert not ap.touches_policy(path, path)


def test_touches_policy_with_no_paths_is_false():
    assert not ap.touches_policy(None, None)


def test_a_rename_out_of_the_policy_directory_touches_it():
    assert ap.touches_policy("projects/platform/policy/a.yaml", "docs/a.yaml")


# ---- the git helper ---------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A clone with origin/main carrying the shipped policy."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    target = origin / ap.POLICY_PATH
    target.parent.mkdir(parents=True)
    target.write_text(shipped(), encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "policy")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    return clone


def test_the_helper_reads_the_file_at_origin_main(repo):
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()
    assert isinstance(ap.load(ap.policy_text_at_origin_main(str(repo))), ap.Policy)


def test_the_helper_ignores_a_working_tree_edit(repo):
    (repo / ap.POLICY_PATH).write_text("agents_may_merge_documents: true\n", encoding="utf-8")
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()


def test_the_helper_ignores_a_committed_local_branch_change(repo):
    git(repo, "checkout", "-b", "pr-head")
    (repo / ap.POLICY_PATH).write_text(shipped().replace("documents: false", "documents: true"), encoding="utf-8")
    git(repo, "commit", "-am", "widen")
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()


def test_the_helper_returns_none_when_origin_main_has_no_policy(repo):
    git(repo, "checkout", "-b", "other", "origin/main")
    git(repo, "rm", "-q", ap.POLICY_PATH)
    git(repo, "commit", "-m", "remove")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert ap.policy_text_at_origin_main(str(repo)) is None
    assert isinstance(ap.load(None), ap.Fallback)


def test_the_helper_returns_none_for_a_path_that_is_not_a_repository(tmp_path):
    assert ap.policy_text_at_origin_main(str(tmp_path / "nowhere")) is None
    assert ap.policy_text_at_origin_main(str(tmp_path)) is None


def _widen(repo: Path, branch: str) -> str:
    git(repo, "checkout", "-q", "-b", branch, "refs/remotes/origin/main")
    (repo / ap.POLICY_PATH).write_text(shipped().replace("documents: false", "documents: true"), encoding="utf-8")
    git(repo, "commit", "-qam", "widen")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    git(repo, "checkout", "-q", "main")
    return sha


@pytest.mark.parametrize("plant", ["tag", "branch", "both"])
def test_the_helper_ignores_a_tag_or_branch_named_origin_main(repo, plant):
    widened = _widen(repo, "widened")
    if plant in ("tag", "both"):
        git(repo, "tag", "origin/main", widened)
    if plant in ("branch", "both"):
        git(repo, "branch", "origin/main", widened)
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()


def _drop_remote_ref(repo: Path) -> str:
    widened = _widen(repo, "widened")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    return widened


@pytest.mark.parametrize(
    "plant",
    [
        ("tag", "refs/tags/refs/remotes/origin/main"),
        ("tag", "refs/tags/remotes/origin/main"),
        ("branch", "refs/heads/refs/remotes/origin/main"),
        ("ref", "refs/refs/remotes/origin/main"),
        ("ref", "refs/remotes/refs/remotes/origin/main"),
    ],
)
def test_a_missing_remote_tracking_ref_is_missing_not_a_planted_ref(repo, plant):
    widened = _drop_remote_ref(repo)
    kind, name = plant
    if kind == "tag":
        git(repo, "tag", name.removeprefix("refs/tags/"), widened)
    else:
        git(repo, "update-ref", name, widened)
    assert ap.policy_text_at_origin_main(str(repo)) is None


def test_a_deleted_remote_tracking_ref_alone_is_none(repo):
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert ap.policy_text_at_origin_main(str(repo)) is None


def test_the_helper_ignores_a_replace_ref(repo):
    widened = _widen(repo, "widened")
    real = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"refs/remotes/origin/main:{ap.POLICY_PATH}"], check=True, capture_output=True, text=True
    ).stdout.strip()
    fake = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{widened}:{ap.POLICY_PATH}"], check=True, capture_output=True, text=True
    ).stdout.strip()
    git(repo, "replace", real, fake)
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()


def test_the_helper_ignores_a_git_dir_inherited_from_the_caller(repo, tmp_path, monkeypatch):
    # A caller inside a git hook has GIT_DIR set to the repository the hook runs in.
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main")
    (other / "f").write_text("x", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-m", "x")
    subprocess.run(["git", "-C", str(other), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert ap.policy_text_at_origin_main(str(repo)) == shipped()


def test_the_helper_returns_none_for_a_blob_that_is_not_utf8(repo):
    (repo / ap.POLICY_PATH).write_bytes(b"\xff\xfe")
    git(repo, "commit", "-qam", "binary")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert ap.policy_text_at_origin_main(str(repo)) is None


def test_the_helper_returns_none_when_git_times_out(repo, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(ap.subprocess, "run", slow)
    assert ap.policy_text_at_origin_main(str(repo)) is None


# --- the review floor (instruction block on taking a pull request through review) ---


@dataclass(frozen=True)
class _Change:
    """The six fields `kb_merge.Change` carries, which `review_floor_of` reads."""
    path_before: str | None = None
    mode_before: str | None = None
    text_before: str | None = None
    path_after: str | None = None
    mode_after: str | None = None
    text_after: str | None = None


def _added(path, text):
    return _Change(path_after=path, mode_after="100644", text_after=text)


def _deleted(path, text):
    return _Change(path_before=path, mode_before="100644", text_before=text)


def _modified(path, before, after):
    return _Change(path_before=path, mode_before="100644", text_before=before,
                   path_after=path, mode_after="100644", text_after=after)


AGENT = "---\nkind: agent\ntitle: t\n---\nbody\n"
RUNBOOK = "---\nkind: runbook\ntitle: t\n---\nbody\n"
REFERENCE = "---\nkind: reference\ntitle: t\n---\nbody\n"
DECISION_CODER = "---\nkind: decision\naudience: [team, coder]\ntitle: t\n---\nbody\n"
# The operator marker, not a literal name: a decision naming it must read as a known
# person, not an unknown entry, and the marker is per-operator config (CQ·428), read
# from the same mirror `agent_policy` imports (`NON_AGENT_AUDIENCE`, less the three
# fixed group names `consultant`/`team`/`contractor`), the same derivation
# `tests/test_knowledge.py` uses.
_OPERATOR_AUDIENCE_MEMBER = next(iter(ap.NON_AGENT_AUDIENCE - {"consultant", "team", "contractor"}))
DECISION_TEAM = f"---\nkind: decision\naudience: [team, {_OPERATOR_AUDIENCE_MEMBER}]\ntitle: t\n---\nbody\n"
DECISION_CONTRACTOR = "---\nkind: decision\naudience: [team, contractor]\ntitle: t\n---\nbody\n"
DECISION_CONSULTANT = "---\nkind: decision\naudience: [consultant]\ntitle: t\n---\nbody\n"
DECISION_CONSULTANT_OPERATOR = (f"---\nkind: decision\naudience: [consultant, "
                                f"{_OPERATOR_AUDIENCE_MEMBER}]\ntitle: t\n---\nbody\n")
DECISION_CONTRACTOR_CODER = "---\nkind: decision\naudience: [contractor, coder]\ntitle: t\n---\nbody\n"
# A list value makes the reader refuse the whole block, so only the raw `audience:` line is readable.
UNREADABLE_DECISION_CODER = "---\nkind: decision\naudience: [team, coder]\napplies_to:\n  - bin/x\n---\nbody\n"
UNREADABLE_DECISION_TEAM_CONTRACTOR = "---\nkind: decision\naudience: [team, contractor]\napplies_to:\n  - bin/x\n---\nbody\n"
UNREADABLE_RUNBOOK = "---\nkind: runbook\napplies_to:\n  - bin/x\n---\nbody\n"
UNREADABLE_NOTE = "---\nkind: note\napplies_to:\n  - bin/x\n---\nbody\n"
# `zebra` is in neither the agent roles nor the people and groups, so the decision is a tie-break.
DECISION_TEAM_ZEBRA = "---\nkind: decision\naudience: [team, zebra]\ntitle: t\n---\nbody\n"
DECISION_CODER_ZEBRA = "---\nkind: decision\naudience: [coder, zebra]\ntitle: t\n---\nbody\n"


@pytest.mark.parametrize("change, positive, fragment", [
    (_added("projects/x/new.md", AGENT), True, "has kind agent"),
    (_deleted("projects/x/rb.md", RUNBOOK), True, "has kind runbook"),
    (_Change(path_before="projects/x/skills/s.md", mode_before="100644", text_before="plain\n",
             path_after="projects/x/s.md", mode_after="100644", text_after="plain\n"),
     True, "sits under an agent-instruction directory"),
    (_modified("projects/x/rb.md", RUNBOOK, REFERENCE), True, "has kind runbook"),
    (_added("projects/platform/decisions/0001-x.md", DECISION_CODER), True,
     "is a decision whose audience names coder"),
    (_added("projects/platform/decisions/0006-w.md", DECISION_CONTRACTOR_CODER), True,
     "is a decision whose audience names coder"),
    (_added("projects/x/tool.py", "print(1)\n"), True, "is not a markdown document"),
    (_added("projects/x/Skills/s.md", "plain\n"), True, "sits under an agent-instruction directory"),
    (_Change(path_after="projects/x/link.md", mode_after="120000", text_after="plain.md"), False,
     "is not a regular file"),
    (_added("projects/platform/decisions/0002-y.md", DECISION_TEAM), False,
     "is a decision whose audience names no agent role"),
    (_added("projects/platform/decisions/0003-v.md", DECISION_CONTRACTOR), False,
     "is a decision whose audience names no agent role"),
    (_added("projects/platform/decisions/0004-u.md", DECISION_CONSULTANT), False,
     "is a decision whose audience names no agent role"),
    (_added("projects/platform/decisions/0005-t.md", DECISION_CONSULTANT_OPERATOR), False,
     "is a decision whose audience names no agent role"),
    (_added("projects/x/new.md", "hi\n"), False, ""),
    (_added("projects/x/fm.md", "---\nkind: reference\napplies_to:\n  - bin/x\n---\nbody\n"), False,
     "has front matter the rule cannot read"),
    # Unreadable front matter is a tie-break, except that a decision's raw `audience:` line still
    # counts. The reader cannot parse the block, so this case reads that one line directly.
    (_added("projects/platform/decisions/0008-q.md", UNREADABLE_DECISION_CODER), True,
     "is a decision whose audience names coder"),
    (_added("projects/platform/decisions/0009-p.md", UNREADABLE_DECISION_TEAM_CONTRACTOR), False,
     "has front matter the rule cannot read"),
    (_added("projects/x/CLAUDE.md", "plain\n"), True, "is named"),
    (_added("projects/x/sub/CLAUDE.local.md", "plain\n"), True, "is named"),
    (_added("projects/x/AGENTS.md", "plain\n"), True, "is named"),
    (_added("projects/x/np.md", "hi\x07there\n"), False, "holds the non-printable character"),
    # KNOWN UNDER-CAP, not correct behaviour. On APFS (this machine's volume) `ſ` resolves to `s`,
    # so `projects/x/ſkills/s.md` is a real file in `skills/`. The floor compares names in ASCII
    # case only, as `_names` requires for a grant, so it reads these as plain documents and gives
    # them the document cap (7) where the code cap (11) applies. `classify` casefolds, so both
    # still count as instructions and merge admission is unaffected. Pinned here so a change to
    # the comparison shows up; the expected values change only with the comparison.
    (_added("projects/x/ſkills/s.md", "plain\n"), False, ""),
    (_added("projects/platform/deciſions/0007-s.md", "plain\n"), False, ""),
    (_Change(path_before="projects/x/bin.md", mode_before="100644", text_before=None,
             path_after="projects/x/bin.md", mode_after="100644", text_after="hi\n"),
     False, "cannot be read"),
    # Whitespace is not a floor reason, so these take the document cap. A zero-width space is
    # not whitespace, but the instruction block's floor does not list it either, so it is a
    # tie-break and takes the document cap too.
    (_added("projects/x/zw.md", "hi​there\n"), False, "holds the non-printable character"),
    (_added("projects/x/nbsp.md", "hi there\n"), False, ""),
    (_added("projects/x/crlf.md", "hi\r\nthere\r\n"), False, ""),
    # Unreadable front matter: a raw `kind:` line naming a floor kind is positive; any other is a tie-break.
    (_added("projects/x/rb.md", UNREADABLE_RUNBOOK), True, "has kind runbook"),
    (_added("projects/x/note.md", UNREADABLE_NOTE), False, "has front matter the rule cannot read"),
    # An audience entry that is neither an agent role nor a person or group makes the decision a tie-break.
    (_added("projects/platform/decisions/0010-z.md", DECISION_TEAM_ZEBRA), False,
     "neither an agent role nor a person or group"),
    (_added("projects/platform/decisions/0011-r.md", DECISION_CODER_ZEBRA), False,
     "neither an agent role nor a person or group"),
    # A tie-break reason does not stop the checks after it: a runbook holding a zero-width space is positive.
    (_added("projects/x/rb-zw.md", "---\nkind: runbook\ntitle: t\n---\nhi​there\n"), True,
     "has kind runbook"),
], ids=["added-agent", "deleted-runbook", "rename-out-of-skills", "kind-runbook-to-reference",
        "decision-naming-a-role", "decision-contractor-and-coder", "source-file",
        "casefolded-skills-directory", "symlink", "decision-team-only",
        "decision-contractor-team", "decision-consultant-alone", "decision-consultant-and-operator",
        "plain-document", "unreadable-front-matter", "unreadable-decision-naming-a-role",
        "unreadable-decision-team-and-contractor", "named-claude-md", "named-claude-local-md",
        "named-agents-md", "non-printable-character", "known-under-cap-long-s-skills-directory",
        "known-under-cap-long-s-decisions-directory", "unreadable-base-side",
        "zero-width-character", "no-break-space-is-whitespace", "carriage-return-is-whitespace",
        "unreadable-runbook-kind", "unreadable-note-kind", "decision-team-and-unknown-name",
        "decision-role-and-unknown-name", "runbook-beside-a-zero-width-tie-break"])
def test_the_review_floor_places_each_case_by_the_rule_it_names(change, positive, fragment):
    got = ap.review_floor_of([change])
    assert got.positive is positive, got
    assert got.floor is (positive or fragment != ""), got
    assert fragment in got.reason, got


@pytest.mark.parametrize("change", [
    _added("projects/x/skills/s.md", "plain\n"),
    _added("projects/x/tool.py", "print(1)\n"),
    _added("projects/x/CLAUDE.md", "plain\n"),
    _added("projects/x/new.md", AGENT),
    _added("projects/platform/decisions/0001-x.md", DECISION_CODER),
], ids=["instruction-directory", "not-markdown", "named-instruction-file", "kind-agent",
        "decision-naming-a-role"])
def test_every_classify_instruction_reason_is_a_positive_floor_member(change):
    """classify's instruction reasons and the floor agree, so a file an agent is told to act on
    never takes the document cap. The reasons the instruction block's floor does not list are
    deliberately not positive in the floor, and the table above covers them, not this test:

    - a non-regular mode (a symlink) and a non-printable character that is not whitespace. The
      block does not list either, so each is a tie-break for the cap. classify keeps both as
      instruction reasons for merge admission.
    - unreadable front matter, which is a tie-break for the cap, except a raw `kind:` line naming
      a floor kind and a decision whose raw `audience:` line names a role. The reader refuses the
      block, so the floor reads those lines directly. classify keeps the reason for admission.
    - a decision naming no agent role, or naming an entry that is neither a role nor a person or
      group. A tie-break for the same reason.
    - a non-printable character that is whitespace (`str.isspace`, such as U+00A0 or CR). classify
      keeps it as an instruction reason, and the floor does not count it (see the test below).
    """
    got = ap.classify(None, None, change.path_after, change.text_after,
                      mode_before=None, mode_after=change.mode_after)
    assert got.instruction, got
    assert ap.review_floor_of([change]).positive


def test_unreadable_front_matter_is_an_instruction_to_classify_but_only_a_tie_break_for_the_cap():
    """The deliberate exception to the parity test above: classify keeps unreadable front matter
    as an instruction reason for merge admission, and the floor gives it the document cap."""
    change = _added("projects/x/fm.md", "---\nkind: reference\napplies_to:\n  - bin/x\n---\nbody\n")
    got = ap.classify(None, None, change.path_after, change.text_after,
                      mode_before=None, mode_after=change.mode_after)
    assert got.instruction, got
    assert ap.review_floor_of([change]) == ap.ReviewFloor(
        True, False, "projects/x/fm.md has front matter the rule cannot read: line 3 is not exactly `key: value`")


def test_whitespace_is_non_printable_to_classify_but_not_a_floor_reason():
    """The second exception to the parity test: classify keeps a whitespace character that is not
    printable (U+00A0, CR) as an instruction reason for merge admission, and the floor does not
    count it, so the file takes the document cap. Three documents on origin/main hold U+00A0."""
    for text in ("hi there\n", "hi\r\nthere\r\n"):
        change = _added("projects/x/ws.md", text)
        got = ap.classify(None, None, change.path_after, change.text_after,
                          mode_before=None, mode_after=change.mode_after)
        assert got.instruction, got
        assert ap.review_floor_of([change]) == ap.ReviewFloor(False, False, "")


@pytest.mark.parametrize("change", [
    _Change(path_after="projects/x/link.md", mode_after="120000", text_after="plain.md"),
    _added("projects/x/zw.md", "hi​there\n"),
], ids=["symlink", "zero-width-character"])
def test_a_floor_tie_break_reason_is_still_an_instruction_to_classify(change):
    """The two reasons the block's floor does not list leave merge admission as it was: classify
    still calls the file an instruction. Only the cap changes, and it takes the document cap."""
    got = ap.classify(None, None, change.path_after, change.text_after,
                      mode_before=None, mode_after=change.mode_after)
    assert got.instruction, got
    floor = ap.review_floor_of([change])
    assert (floor.floor, floor.positive) == (True, False), floor
    assert change.path_after in floor.reason, floor


def test_a_positive_member_beside_a_tie_break_is_positive():
    tie = _Change(path_after="projects/x/bin.md", mode_after="100644", text_after=None)
    got = ap.review_floor_of([tie, _added("projects/x/new.md", AGENT)])
    assert (got.floor, got.positive) == (True, True) and "has kind agent" in got.reason, got


def test_changes_that_could_not_be_read_at_all_are_a_tie_break_not_the_code_cap():
    got = ap.review_floor_of(None)
    assert (got.floor, got.positive) == (True, False), got


def test_no_changes_is_a_document_pr():
    assert ap.review_floor_of([]) == ap.ReviewFloor(False, False, "")


def _policy_with(per_role, code):
    return replace(ap.load(shipped()), review_rounds_per_role=per_role, review_rounds_code=code)


def test_a_positive_floor_takes_the_code_cap_and_says_why():
    cap, why = ap.review_round_cap(_policy_with(9, 13),
                                   ap.ReviewFloor(True, True, "x.md has kind agent"))
    assert cap == 13 and why.startswith("review_rounds_code (13) applies, because x.md has kind agent")


@pytest.mark.parametrize("floor, expect", [
    (ap.ReviewFloor(True, False, "x.md cannot be read"), "tie-break"),
    (ap.ReviewFloor(False, False, ""), "no changed file is in the review floor"),
])
def test_a_tie_break_or_document_takes_the_document_cap(floor, expect):
    cap, why = ap.review_round_cap(_policy_with(9, 13), floor)
    assert cap == 9 and why.startswith("review_rounds_per_role (9) applies") and expect in why, why


def test_no_policy_takes_the_fallbacks_for_both_caps():
    assert ap.review_round_cap(None, ap.ReviewFloor(True, True, "x"))[0] == ap.FALLBACK_REVIEW_ROUNDS_CODE == 5
    assert ap.review_round_cap(None, ap.ReviewFloor(False, False, ""))[0] == 5
