"""Collapsed session view (Deck WS1.5).

THE FEATURE THIS PINS. Tool calls and results collapse to one line; what a human
typed and what the model said render at length. The classification is a pure
function over the transcript's own record types, which is what lets this ship
without the conversation renderer's editorial vocabulary.

The tests that matter most are the ones about being WRONG: a tool result must
never render as if the human said it, an unknown record must surface rather than
vanish, and nothing may raise — the transcript format is Anthropic's.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"

# Rendering a document runs the md-serial renderer, which lives in the knowledge
# repo and executes under Node. Both are present on the mini and neither is on a
# hosted runner, so these tests are integration tests of a seam this repo only
# holds one side of.
#
# THEY ARE SKIPPED WHERE THEY CANNOT RUN, NOT WEAKENED SO THEY CAN. The
# alternative was vendoring a copy of the renderer here to test against, which
# would be a second renderer — the exact thing this change exists to remove.
#
# The logic this repo owns — the wikilink map's memo and cap, the Node probe,
# the timeout re-raise — is tested unconditionally below by monkeypatching, so a
# hosted run still covers everything written here.
_KNOWLEDGE_REPO_FOR_TESTS = Path.home() / "Convivy" / "knowledge"
_NODE_FOR_TESTS = any(
    os.path.isfile(p) and os.access(p, os.X_OK)
    for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")
)
needs_renderer = pytest.mark.skipif(
    not (_KNOWLEDGE_REPO_FOR_TESTS.is_dir() and _NODE_FOR_TESTS),
    reason=(
        "needs the md-serial renderer: the knowledge repo at "
        f"{_KNOWLEDGE_REPO_FOR_TESTS} and a node binary. Both exist on the mini; "
        "neither is on a hosted runner."
    ),
)

# The save-click handler's own logic — unlike the renderer above — needs only
# Node, not the knowledge repo: it is this repo's code, executed rather than
# string-matched so the tests pin what actually gets posted, not just what the
# generator emits.
_NODE_BIN = shutil.which("node") or next(
    (p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node")
     if os.path.isfile(p) and os.access(p, os.X_OK)),
    None,
)
needs_node = pytest.mark.skipif(_NODE_BIN is None, reason="needs a node binary")


def _run_save_click_raw(SV, editor_js: str, tmp_path: Path, *,
                         has_webkit: bool = True, prelude: str = "") -> dict:
    """Run the save-button click handler under Node against a stub `editor`
    and return everything observable: what got posted, and the
    'save-unavailable' banner's display style.

    Stubs `document.getElementById` (only 'save-btn' and 'save-unavailable'
    matter here) and, when `has_webkit` is true, `window.webkit.messageHandlers
    .deckEditor.postMessage`, then fires the one click the handler registers.
    `editor_js` is a JS object-literal source, e.g. a saveChecked() that
    throws or returns a particular roundTrip shape. `has_webkit=False` omits
    `window.webkit` entirely, for the click-time missing-handler branch.
    `prelude` is arbitrary JS run immediately before the click, e.g. to set up
    prototype pollution; `Object.prototype.ok`, `.source` and `.roundTrip` are
    always deleted afterward so one test's pollution can't leak into the next.
    """
    webkit_stub = (
        "global.window = { webkit: { messageHandlers: { deckEditor: {"
        "  postMessage: function(msg) { global.__posted.push(msg); }"
        "} } } };"
        if has_webkit else
        "global.window = {};"
    )
    script = "\n".join([
        "global.__posted = [];",
        webkit_stub,
        "var __elements = { 'save-unavailable': { style: {} } };",
        "var __saveBtnHandler = null;",
        "global.document = { getElementById: function(id) {",
        "  if (id === 'save-btn') {",
        "    return { addEventListener: function(evt, fn) {",
        "      if (evt === 'click') __saveBtnHandler = fn;",
        "    } };",
        "  }",
        "  if (!__elements[id]) __elements[id] = { style: {} };",
        "  return __elements[id];",
        "} };",
        f"global.editor = {editor_js};",
        SV._save_click_handler_js(),
        prelude,
        "__saveBtnHandler();",
        "delete Object.prototype.ok;",
        "delete Object.prototype.source;",
        "delete Object.prototype.roundTrip;",
        "console.log(JSON.stringify({",
        "  posted: global.__posted,",
        "  saveUnavailableDisplay: __elements['save-unavailable'].style.display,",
        "}));",
    ])
    script_path = tmp_path / "click.js"
    script_path.write_text(script)
    result = subprocess.run(
        [_NODE_BIN, str(script_path)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node script failed: {result.stderr}"
    return json.loads(result.stdout)


def _run_save_click(SV, editor_js: str, tmp_path: Path, **kwargs) -> list:
    """Like `_run_save_click_raw`, but returns only what got posted — the
    shape every pre-existing test here asserts against.
    """
    return _run_save_click_raw(SV, editor_js, tmp_path, **kwargs)["posted"]


@pytest.fixture
def SV():
    spec = importlib.util.spec_from_file_location("session_view", LIB / "session_view.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


# --- what renders full: intentional communication -----------------------------

def test_a_human_prompt_renders_full(SV):
    rows = SV.classify({"type": "user", "message": {"content": "do the thing"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("prompt", SV.FULL)]
    assert rows[0]["body"] == "do the thing"


def test_assistant_prose_renders_full(SV):
    rows = SV.classify(_assistant({"type": "text", "text": "here is what I found"}))
    assert [(r["kind"], r["display"]) for r in rows] == [("speech", SV.FULL)]


def test_assistant_string_content_renders_full(SV):
    """Content is sometimes a bare string rather than a block list."""
    rows = SV.classify({"type": "assistant", "message": {"content": "plain string"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("speech", SV.FULL)]


# --- what collapses -----------------------------------------------------------

def test_a_tool_call_collapses_with_its_identifying_argument(SV):
    rows = SV.classify(_assistant(
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y/thing.py"}}))
    assert rows[0]["display"] == SV.COLLAPSED
    assert rows[0]["label"] == "Edit(/x/y/thing.py)"


def test_a_tool_call_label_is_verbatim_never_paraphrased(SV):
    """THE SCOPE BOUNDARY. No summarization vocabulary lives here.

    A collapsed line names the tool and its argument exactly as the payload had
    them. Paraphrase is WS4.3's [J3] judgment; importing it would make this WS the
    thing it was scoped not to be.
    """
    rows = SV.classify(_assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status --short"}}))
    assert rows[0]["label"] == "Bash(git status --short)"


def test_thinking_collapses_but_is_present(SV):
    """Gate G10, the operator 2026-07-19: collapsed, not hidden."""
    rows = SV.classify(_assistant({"type": "thinking", "thinking": "considering"}))
    assert [(r["kind"], r["display"]) for r in rows] == [("thinking", SV.COLLAPSED)]
    assert rows[0]["body"] == "considering"


def test_a_tool_result_is_never_mistaken_for_a_human_prompt(SV):
    """THE DISTINCTION THE WHOLE FEATURE RESTS ON.

    A tool result arrives as `type: user`. Without the toolUseResult check, every
    result would render at full length as if the operator had typed it — which is the exact
    verbosity this WS exists to remove, made worse by attribution.
    """
    rows = SV.classify({"type": "user",
                        "message": {"content": "1234 files changed"},
                        "toolUseResult": {"stdout": "..."}})
    assert [(r["kind"], r["display"]) for r in rows] == [("tool_result", SV.COLLAPSED)]


# --- slash-command scaffolding does not render as the human -------------------

def test_a_slash_command_invocation_folds_to_a_chip(SV):
    """THE BUG THIS CLOSES. Running `/handoff-leave` records a user message whose
    content is `<command-name>/handoff-leave</command-name>` scaffolding, not the
    sentence a person typed. Rendered full as "you" it printed the command's
    internals into the pane; the operator hit this 2026-07-24. It folds to a chip naming
    the command instead, with the raw text kept one expand away."""
    rows = SV.classify({"type": "user", "message": {"content":
        "<command-message>handoff-leave</command-message>\n"
        "<command-name>/handoff-leave</command-name>"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("command", SV.COLLAPSED)]
    assert rows[0]["label"] == "/handoff-leave"


def test_an_injected_meta_message_folds_rather_than_rendering_as_you(SV):
    """A skill's body and a local-command caveat arrive as `isMeta` user records —
    Claude Code's own flag for a message it injected rather than one the person
    typed. The TUI does not show these as turns; neither should the pane. Folded,
    not dropped: the file's rule is to surface and collapse."""
    rows = SV.classify({"type": "user", "isMeta": True, "message": {"content":
        [{"type": "text", "text": "Base directory for this skill: …\n# handoff-leave"}]}})
    assert [(r["kind"], r["display"]) for r in rows] == [("meta", SV.COLLAPSED)]


def test_a_task_notification_is_not_attributed_to_you(SV):
    """THE PANE PUT WORDS IN THE OPERATOR'S MOUTH. A task notification is an ordinary user
    record — no `toolUseResult`, no `isMeta` — so it rendered as a full-width turn
    labelled "you", and the reader saw the output attributed to them, apparently writing XML. Measured on
    a live selector transcript: 19 harness records against 5 real prompts."""
    rows = SV.classify({"type": "user", "message": {"content":
        "<task-notification>\n<task-id>b5e6478q2</task-id>\n"
        "<status>completed</status>\n"
        "<summary>Background command \"Dispatch reviewer on A1a PR 302\" "
        "completed (exit code 0)</summary>\n</task-notification>"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("event", SV.COLLAPSED)]
    # The label carries the summary, so the folded row still says what happened
    # rather than making the reader expand it to find out.
    assert rows[0]["label"].startswith("task · Background command")


def test_a_stopped_agent_notice_is_not_attributed_to_you(SV):
    rows = SV.classify({"type": "user", "message": {"content":
        'Background agent "Build B1 recording cooldown" was stopped by the user.'}})
    assert [(r["kind"], r["display"]) for r in rows] == [("event", SV.COLLAPSED)]


def test_a_person_starting_a_sentence_with_background_agent_still_reads_as_a_prompt(SV):
    """The harness always NAMES the agent it stopped, so the quote is part of the
    match. Without it, asking "Background agent stopped working, what happened?"
    would fold the question as though the harness had written it."""
    rows = SV.classify({"type": "user", "message": {"content":
        "Background agent stopped working, what happened?"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("prompt", SV.FULL)]


def test_a_person_quoting_a_harness_line_still_reads_as_a_prompt(SV):
    """Matched only at the START of a record, the same rule `_slash_command`
    follows — otherwise asking about a notification would fold the question."""
    rows = SV.classify({"type": "user", "message": {"content":
        "why does <task-notification> render as me? Background agent lines too."}})
    assert [(r["kind"], r["display"]) for r in rows] == [("prompt", SV.FULL)]
    assert rows[0]["label"] == "you"


def test_an_event_label_stays_one_short_line(SV):
    long_summary = "word " * 60
    rows = SV.classify({"type": "user", "message": {"content":
        f"<task-notification><summary>{long_summary}</summary></task-notification>"}})
    label = rows[0]["label"]
    assert "\n" not in label and len(label) <= 90


def test_command_output_folds(SV):
    """`<local-command-stdout>Bye!</local-command-stdout>` is a command's output,
    not prose. It carries no command name of its own, so the chip is generic."""
    rows = SV.classify({"type": "user", "message": {"content":
        "<local-command-stdout>Bye!</local-command-stdout>"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("command", SV.COLLAPSED)]


def test_prose_that_merely_mentions_a_command_tag_still_renders_full(SV):
    """The guard against folding too much. A person asking about `<command-name>`
    in the middle of a sentence is talking, not invoking — recognition requires
    the content to LEAD with the tag, so this stays a full human turn."""
    rows = SV.classify({"type": "user", "message": {"content":
        "why does the <command-name> tag show up in my transcript?"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("prompt", SV.FULL)]


def test_an_ordinary_prompt_is_untouched_by_the_command_check(SV):
    """A message that starts with '<' but is not command scaffolding — an XML
    snippet, say — is not swallowed: it must contain a command tag to fold."""
    rows = SV.classify({"type": "user", "message": {"content": "<note>ship it</note>"}})
    assert [(r["kind"], r["display"]) for r in rows] == [("prompt", SV.FULL)]


# --- what hides ---------------------------------------------------------------

@pytest.mark.parametrize("rtype", sorted({
    "last-prompt", "attachment", "mode", "permission-mode", "bridge-session",
    "ai-title", "pr-link", "queue-operation", "file-history-snapshot",
    "file-history-delta", "system",
}))
def test_control_plane_records_are_hidden(SV, rtype):
    rows = SV.classify({"type": rtype})
    assert [r["display"] for r in rows] == [SV.HIDDEN]


# --- the released queued message ----------------------------------------------
#
# A message typed at a working session is written ONLY as a `queued_command`
# attachment — there is no `user` record for it anywhere in the file. Verified
# against a live 2.1.226 transcript on 2026-08-10, where the held row was
# withdrawn on release and nothing replaced it, so the message vanished from the
# conversation outright. These records are verbatim from that file.


def _queued(prompt, mode="prompt", origin={"kind": "human"}):
    att = {"type": "queued_command", "prompt": prompt, "commandMode": mode,
           "timestamp": "2026-08-10T13:45:31.836Z"}
    if origin is not None:
        att["origin"] = origin
    return {"type": "attachment", "attachment": att,
            "uuid": "05e0bce1-cf97-4305-ba30-64019a148927",
            "sessionId": "f41e1486-41e3-4507-901e-29daa7d3a9b6",
            "version": "2.1.226"}


def test_a_released_queued_message_becomes_an_ordinary_you_turn(SV):
    """The handoff the held row exists for. No badge, no "(queued)" label: once
    the agent has read it, it is a message like any other."""
    rows = SV.classify(_queued("This is the test"))
    assert [(r["kind"], r["display"], r["label"], r["body"]) for r in rows] == [
        ("prompt", SV.FULL, "you", "This is the test")]


def test_a_task_notification_attachment_stays_hidden(SV):
    """Most `queued_command` attachments in the corpus are these — the harness
    telling the agent a background task finished, 4,121 against 1,011 human ones
    at 07:30 MST on 2026-08-10. Drawing one as a turn labelled "you" is the
    misattribution `_harness_event` exists to prevent, on a surface the operator reads as
    their own words."""
    rec = _queued("<task-notification>agent finished</task-notification>",
                  mode="task-notification", origin=None)
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


def test_a_queued_command_survives_the_origin_field_being_dropped(SV):
    """`origin.kind` is required only when present, deliberately. Demanding it
    outright would re-hide every human message the day Anthropic drops the
    field — silent degradation, the failure mode this codebase has already
    shipped four times."""
    rows = SV.classify(_queued("still mine", origin=None))
    assert [(r["kind"], r["label"]) for r in rows] == [("prompt", "you")]


def test_a_queued_command_with_a_non_human_origin_is_refused(SV):
    """Present and explicitly not human is a claim, not an absence."""
    rec = _queued("injected", origin={"kind": "harness"})
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


@pytest.mark.parametrize("origin", ["human", ["human"], 42])
def test_a_queued_command_with_a_wrong_shaped_origin_is_refused(SV, origin):
    """PRESENT-BUT-UNREADABLE FAILS CLOSED, which an earlier version got wrong.

    Refusing only a dict with the wrong `kind` let a string or a list read as
    "field not there" and pass. Absent is a shape this code has seen and
    reasoned about; a shape it has not seen is exactly when it should not be
    attributing text to a person. Pins the strictness against
    `test_a_queued_command_survives_the_origin_field_being_dropped`, which pins
    the deliberate leniency for genuine absence."""
    rec = _queued("who wrote this?", origin=origin)
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


def test_a_queued_command_in_an_unknown_mode_is_refused(SV):
    """A mode this code has never seen is not one it should attribute to a
    person. Unlike `origin`, `commandMode` names what the record IS."""
    rec = _queued("who wrote this?", mode="somethingNew")
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


def test_harness_text_in_a_human_queued_command_is_still_caught(SV):
    """The envelope guards and the content guard fail independently, so both
    run. A record that passes `commandMode` and `origin` but carries harness
    text is folded the same way an ordinary user record carrying it would be."""
    rec = _queued("<task-notification>done</task-notification>")
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


def test_an_attachment_that_is_not_a_queued_command_stays_hidden(SV):
    """The fall-through is the common case and must not widen: only
    `queued_command` leaves the hidden set."""
    rec = {"type": "attachment",
           "attachment": {"type": "file", "path": "/tmp/x", "commandMode": "prompt"}}
    assert [r["display"] for r in SV.classify(rec)] == [SV.HIDDEN]


def test_a_queued_command_with_no_prompt_text_draws_nothing(SV):
    """Same rule the ordinary user branch applies to empty content."""
    assert [r["display"] for r in SV.classify(_queued("   "))] == [SV.HIDDEN]
    assert [r["display"] for r in SV.classify(_queued(None))] == [SV.HIDDEN]


# --- degradation: the format is Anthropic's ----------------------------------

def test_an_unknown_record_type_surfaces_collapsed_never_vanishes(SV):
    """A new record type is EXPECTED — the format is undocumented and changes.

    Dropping it silently would let the view diverge from reality without a signal.
    A reader seeing an unhelpful line is better off than one seeing nothing.
    """
    rows = SV.classify({"type": "some-future-thing"})
    assert [(r["kind"], r["display"]) for r in rows] == [("unknown", SV.COLLAPSED)]
    assert "some-future-thing" in rows[0]["label"]


def test_an_unknown_content_block_surfaces_collapsed(SV):
    rows = SV.classify(_assistant({"type": "future-block"}))
    assert [(r["kind"], r["display"]) for r in rows] == [("unknown", SV.COLLAPSED)]


@pytest.mark.parametrize("rec", [None, [], "a string", 42, {"type": None}])
def test_classify_never_raises_on_a_malformed_record(SV, rec):
    SV.classify(rec)


def test_an_unparseable_line_becomes_a_row_rather_than_an_exception(SV):
    rows = SV.rows_for(['not json at all', '{"type": "user", "message": {"content": "hi"}}'])
    assert rows[0]["kind"] == "unknown"
    assert rows[1]["kind"] == "prompt"


def test_a_non_dict_json_line_does_not_crash(SV):
    """cost_join has an uncaught AttributeError on exactly this shape.

    A JSON array parses fine and then fails on .get(). Documented in the transcript
    contract as a live crash path in another consumer; this one must not repeat it.
    """
    rows = SV.rows_for(['["assistant"]'])
    assert rows[0]["kind"] == "unknown"


def test_empty_messages_produce_no_row(SV):
    assert SV.classify({"type": "user", "message": {"content": "   "}}) == []
    assert SV.classify(_assistant({"type": "text", "text": ""})) == []


# --- ordering and end-to-end --------------------------------------------------

def test_rows_preserve_transcript_order(SV):
    lines = [json.dumps(r) for r in [
        {"type": "user", "message": {"content": "go"}},
        _assistant({"type": "thinking", "thinking": "hm"},
                   {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}),
        {"type": "user", "message": {"content": "out"}, "toolUseResult": {}},
        _assistant({"type": "text", "text": "done"}),
    ]]
    kinds = [r["kind"] for r in SV.rows_for(lines)]
    assert kinds == ["prompt", "thinking", "tool", "tool_result", "speech"]


def test_render_html_escapes_and_marks_up(SV):
    rows = SV.rows_for([json.dumps(
        {"type": "user", "message": {"content": "<script>alert(1)</script>"}})])
    out = SV.render_html(rows, "t")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_render_html_omits_hidden_rows(SV):
    rows = SV.rows_for([json.dumps({"type": "pr-link", "url": "http://x"})])
    out = SV.render_html(rows, "t")
    assert "pr-link" not in out
    assert "nothing to show yet" in out


def test_collapsed_rows_render_as_details_and_full_rows_do_not(SV):
    rows = SV.rows_for([
        json.dumps(_assistant({"type": "tool_use", "name": "Read",
                               "input": {"file_path": "a.py"}})),
        json.dumps({"type": "user", "message": {"content": "hello"}}),
    ])
    out = SV.render_html(rows, "t")
    # Read is on the allowlist: the argument becomes a deck-open anchor. Every
    # `<details>` also carries its row key, so the open row survives a reload.
    assert re.search(r"<details data-k=\"[^\"]+\"><summary>Read\(<a href=", out)
    assert "deck-open:?origin=path" in out
    assert ">a.py</a>)</summary>" in out
    assert "<div class=body><p>hello</p></div>" in out


def test_read_rows_on_a_missing_file_is_empty_not_an_error(SV, tmp_path):
    assert SV.read_rows(tmp_path / "nope.jsonl") == []


def test_identifying_arg_prefers_a_path_over_other_strings(SV):
    rows = SV.classify(_assistant({"type": "tool_use", "name": "Edit", "input": {
        "old_string": "aaa", "file_path": "/the/file.py", "new_string": "bbb"}}))
    assert rows[0]["label"] == "Edit(/the/file.py)"


def test_a_long_argument_is_truncated(SV):
    rows = SV.classify(_assistant({"type": "tool_use", "name": "Bash",
                                   "input": {"command": "x" * 500}}))
    assert len(rows[0]["label"]) < 200


def test_a_tool_call_with_no_string_argument_still_labels(SV):
    rows = SV.classify(_assistant({"type": "tool_use", "name": "TodoWrite",
                                   "input": {"items": [1, 2, 3]}}))
    assert rows[0]["label"] == "TodoWrite"


# --- session id validation (security review, round 1) -------------------------

def test_a_glob_metacharacter_id_returns_nothing(SV):
    """THE INJECTION THIS PINS.

    The id is interpolated into a glob PATTERN, not a path join, so `*` was code
    rather than data: transcript_path("*") matched */*.jsonl and returned somebody
    else's transcript. Traversal was never possible; reading an arbitrary session
    was.
    """
    for bad in ("*", "?", "[a-z]*", "**", "a*b"):
        assert SV.transcript_path(bad) is None, f"{bad!r} must not match"


def test_render_session_refuses_a_glob_id(SV):
    assert SV.render_session("*") is None


@pytest.mark.parametrize("bad", ["", "../x", "a/b", None, 42, "a b", "a.b"])
def test_only_the_session_id_alphabet_is_accepted(SV, bad):
    assert SV.transcript_path(bad) is None


def test_a_wellformed_id_still_resolves_shape(SV, monkeypatch, tmp_path):
    """Validation must not break the normal path — a real UUID still looks up."""
    proj = tmp_path / "-Users-operator-thing"
    proj.mkdir(parents=True)
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    (proj / f"{sid}.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(SV, "CLAUDE_PROJECTS", tmp_path)
    assert SV.transcript_path(sid) is not None


def test_an_explicit_null_tooluseresult_is_still_a_tool_result(SV):
    """`is not None` would read a JSON null as "no result" and attribute the row
    to the human. Membership is the precise test."""
    rows = SV.classify({"type": "user", "message": {"content": "out"},
                        "toolUseResult": None})
    assert [(r["kind"], r["display"]) for r in rows] == [("tool_result", SV.COLLAPSED)]


# --- the WS1.4 seam itself ----------------------------------------------------

def test_render_session_assembles_end_to_end(SV, monkeypatch, tmp_path):
    """The documented integration point had no coverage.

    Nothing here is subtle, which is exactly why a regression in the assembly
    (wrong title, wrong None path) would go unnoticed until fleetd wired it up.
    """
    proj = tmp_path / "-Users-operator-thing"
    proj.mkdir(parents=True)
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "hello there"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(SV, "CLAUDE_PROJECTS", tmp_path)

    page = SV.render_session(sid)

    assert page is not None
    assert sid[:8] in page                      # title
    assert "hello there" in page                # full turn
    # Read is on the allowlist: the argument becomes a deck-open anchor.
    assert "Read(<a " in page                   # collapsed with anchor
    assert "deck-open:?origin=path" in page


def test_render_session_returns_none_for_an_unknown_id(SV, monkeypatch, tmp_path):
    monkeypatch.setattr(SV, "CLAUDE_PROJECTS", tmp_path)
    assert SV.render_session("0199c7de-0000-4000-8000-000000000000") is None


# --- folding results into calls (the operator's feedback, 2026-07-19) ------------------

def _tool_use(tid, name="Bash", **inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp or {"command": "x"}}]}}


def _tool_result(tid, text, is_error=False):
    return {"type": "user", "toolUseResult": {"stdout": text},
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text,
                 **({"is_error": True} if is_error else {})}]}}


def test_a_result_body_is_no_longer_empty(SV):
    """THE BUG THIS PINS. Every expanded result rendered blank.

    _text_of collects only blocks of type `text`, but a result arrives as a
    `tool_result` block — so the body was always "".
    """
    rows = SV.rows_for([json.dumps(_tool_use("t1")), json.dumps(_tool_result("t1", "hello out"))])
    call = [r for r in rows if r["kind"] == "tool"][0]
    assert call["result"] == "hello out"


def test_a_result_folds_into_its_call_and_stops_being_its_own_row(SV):
    rows = SV.rows_for([json.dumps(_tool_use("t1")), json.dumps(_tool_result("t1", "out"))])
    assert [r["kind"] for r in rows] == ["tool"]


def test_results_fold_by_id_not_by_position(SV):
    """Concurrent calls resolve exactly — the result names the id it answers."""
    rows = SV.rows_for([
        json.dumps(_tool_use("a", name="Read", file_path="a.py")),
        json.dumps(_tool_use("b", name="Read", file_path="b.py")),
        json.dumps(_tool_result("b", "B-OUT")),
        json.dumps(_tool_result("a", "A-OUT")),
    ])
    got = {r["label"]: r.get("result") for r in rows if r["kind"] == "tool"}
    assert got == {"Read(a.py)": "A-OUT", "Read(b.py)": "B-OUT"}


def test_an_orphan_result_survives_as_its_own_row(SV):
    """A truncated transcript must not silently lose what came back."""
    rows = SV.rows_for([json.dumps(_tool_result("missing", "out"))])
    assert [r["kind"] for r in rows] == ["tool_result"]


def test_an_error_result_marks_its_call(SV):
    rows = SV.rows_for([json.dumps(_tool_use("t1")),
                        json.dumps(_tool_result("t1", "boom", is_error=True))])
    assert rows[0]["error"] is True
    assert "err" in SV.render_html(rows, "t")


def test_result_falls_back_to_tooluseresult_stdout(SV):
    """Some results carry no block content, only the structured form."""
    rec = {"type": "user", "toolUseResult": {"stdout": "from stdout"},
           "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
    rows = SV.rows_for([json.dumps(_tool_use("t1")), json.dumps(rec)])
    assert rows[0]["result"] == "from stdout"


def test_the_result_leads_the_expanded_block(SV):
    """Expanding a call is nearly always asking what came back."""
    rows = SV.rows_for([json.dumps(_tool_use("t1", name="Bash", command="ls")),
                        json.dumps(_tool_result("t1", "RESULTTEXT"))])
    out = SV.render_html(rows, "t")
    assert out.index("<pre class=res>") < out.index("<pre class=arg>")


# --- markdown -----------------------------------------------------------------

def test_markdown_renders_rather_than_showing_punctuation(SV):
    out = SV.render_markdown("**bold** and `code` and *em*")
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out
    assert "<em>em</em>" in out


def test_markdown_renders_lists_and_headings(SV):
    out = SV.render_markdown("## Title\n- one\n- two")
    assert "<h4>Title</h4>" in out
    assert out.count("<li>") == 2
    assert "<ul>" in out and "</ul>" in out


def test_markdown_renders_a_fenced_block_without_marking_it_up(SV):
    out = SV.render_markdown("```\n**not bold** here\n```")
    assert "<pre><code>" in out
    assert "<strong>" not in out


def test_markdown_escapes_before_it_marks_up(SV):
    """ORDER IS THE SECURITY PROPERTY.

    Escaping first makes any markup in the source inert; the patterns then only
    introduce tags we chose. Marking up first and escaping after would either
    destroy the tags or reopen XSS.
    """
    out = SV.render_markdown("<script>alert(1)</script> **b**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>b</strong>" in out


def test_markdown_link_only_accepts_http_schemes(SV):
    assert "<a href='https://x.test'>t</a>" in SV.render_markdown("[t](https://x.test)")
    out = SV.render_markdown("[t](javascript:alert(1))")
    assert "<a" not in out


def test_full_turns_render_markdown_collapsed_bodies_do_not(SV):
    rows = SV.rows_for([json.dumps({"type": "user", "message": {"content": "**hi**"}})])
    assert "<strong>hi</strong>" in SV.render_html(rows, "t")


@pytest.mark.parametrize("url", [
    "https://x.test/it's-here",   # -> &#x27; after escaping
    'https://x.test/say"hi',      # -> &quot; after escaping
])
def test_a_url_that_cannot_be_safely_quoted_stays_plain_text(SV, url):
    """THE SUBTLETY. Escaping runs FIRST, so a quote inside a URL arrives as the
    ENTITY (&#x27;/&quot;) — no literal quote survives for a character class to
    exclude, and the parser decodes the entity inside the attribute, closing the
    href early. The guard therefore matches the entity form, not the character.

    My first fix excluded the literal character and did nothing; the test caught it.
    """
    out = SV.render_markdown(f"[t]({url})")
    assert "<a" not in out


def test_a_query_string_still_linkifies(SV):
    """`&` is legitimate in URLs — the guard must reject escaped QUOTES only."""
    assert "<a href='https://x.test/a?b=1&amp;c=2'>" in SV.render_markdown(
        "[t](https://x.test/a?b=1&c=2)")


def test_a_normal_url_still_links(SV):
    assert "<a href='https://x.test/a-b_c'>t</a>" in SV.render_markdown("[t](https://x.test/a-b_c)")


def test_a_duplicate_tool_use_id_folds_to_the_first_call(SV):
    """Ids should be unique, but the format is undocumented. Last-wins would
    attach the result to the wrong call and leave the first blank."""
    rows = SV.rows_for([json.dumps(_tool_use("dup", name="Read", file_path="first.py")),
                        json.dumps(_tool_use("dup", name="Read", file_path="second.py")),
                        json.dumps(_tool_result("dup", "OUT"))])
    tools = [r for r in rows if r["kind"] == "tool"]
    assert tools[0]["label"] == "Read(first.py)"
    assert tools[0].get("result") == "OUT"


def test_an_empty_text_block_does_not_suppress_the_stdout_fallback(SV):
    """An empty text block counted as 'found content' and blanked the result even
    when stdout carried the answer."""
    rec = {"type": "user", "toolUseResult": {"stdout": "useful output"},
           "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                    "content": [{"type": "text", "text": ""}]}]}}
    rows = SV.rows_for([json.dumps(_tool_use("t1")), json.dumps(rec)])
    assert rows[0]["result"] == "useful output"


# MARK: - Appearance (the shell is the authority, not the page)


def test_no_theme_emits_no_html_tag_and_follows_the_system(SV):
    """A browser has no appearance authority, so the page keeps deciding there.
    That is the pre-existing behaviour and must survive."""
    page = SV.render_html([], "x")
    assert "<html" not in page
    assert "prefers-color-scheme: dark" in page


def test_a_resolved_theme_is_stamped_on_the_root_element(SV):
    assert '<html data-theme="dark">' in SV.render_html([], "x", theme="dark")
    assert '<html data-theme="light">' in SV.render_html([], "x", theme="light")


def test_the_stamp_is_not_overridden_by_the_system_preference(SV):
    """The whole point. A light-pinned Deck on a dark Mac must stay light, so
    the media query is scoped to the unstamped case."""
    page = SV.render_html([], "x", theme="light")
    assert ":root:not([data-theme])" in page
    assert "@media (prefers-color-scheme: dark) {\n  :root {" not in page


def test_an_unknown_theme_value_is_not_written_to_the_attribute(SV):
    """The value lands on the root element, so it is chosen from a closed set
    rather than passed through."""
    for bogus in ["' onload=alert(1)", "DARK", "", "auto", None]:
        assert "<html" not in SV.render_html([], "x", theme=bogus)


def test_the_page_carries_no_system_colour_keywords(SV):
    """Canvas/CanvasText are how the page used to self-decide. Their absence is
    what makes the shell the authority."""
    page = SV.render_html([], "x", theme="dark")
    assert "Canvas" not in page
    assert "CanvasText" not in page


# --- readability: paragraphs, and links that are links -------------------------
#
# All three of these came from the operator reading a real session in Deck rather than
# from a spec. The renderer was correct and hard to read.


def test_a_wrapped_paragraph_is_one_paragraph(SV):
    """The bug behind "the spacing is really big".

    Every source line used to become its own `<p>`, so a paragraph the model
    wrapped at terminal width arrived as four paragraphs carrying four
    margins. Joining them also lets the text reflow to the pane's width.
    """
    out = SV.render_markdown("A paragraph that\nwraps across\nthree lines.")
    assert out == "<p>A paragraph that wraps across three lines.</p>"


def test_a_blank_line_is_what_separates_paragraphs(SV):
    assert SV.render_markdown("One.\n\nTwo.") == "<p>One.</p>\n<p>Two.</p>"


def test_the_page_no_longer_asks_for_pre_wrap_prose(SV):
    """`pre-wrap` predates markdown rendering here. Left in place it turns the
    newline between two block tags into a visible break, on top of the margin
    those blocks already carry."""
    # Comments stripped: the stylesheet explains in prose why pre-wrap is gone,
    # and a bare substring check would match that explanation.
    page = re.sub(r"/\*.*?\*/", "", SV.render_html([], "x", theme="dark"), flags=re.S)
    assert "white-space: pre-wrap" not in page


@pytest.mark.parametrize("block", [
    "# A heading",
    "- a bullet",
    "> a quote",
    "```\nfenced\n```",
])
def test_a_block_ends_the_paragraph_before_it(SV, block):
    """Buffering prose lines must not swallow the block that follows them."""
    out = SV.render_markdown(f"Prose.\n{block}")
    assert out.startswith("<p>Prose.</p>")


def test_a_bare_url_becomes_a_link(SV):
    out = SV.render_markdown("See https://example.com/x for details.")
    assert "<a href='https://example.com/x'>https://example.com/x</a>" in out


def test_a_markdown_link_is_not_linkified_twice(SV):
    """The case that made the tag walk necessary: a regex sees `href='https://`
    and the text of a link as the same shape."""
    out = SV.render_markdown("A [labelled link](https://example.com/x) here.")
    assert out.count("<a ") == 1
    assert "<a href='https://example.com/x'>labelled link</a>" in out


def test_a_url_inside_code_stays_text(SV):
    """A code span is showing the URL, which is the point of the span."""
    out = SV.render_markdown("Run `curl https://example.com/x` now.")
    assert "<a " not in out
    assert "<code>curl https://example.com/x</code>" in out


# `;` is absent on purpose: it terminates an HTML entity, and trimming it split
# the `&amp;` of a query string. See the entity test below for the trade.
@pytest.mark.parametrize("trailing", [".", ",", ":", "!", "?", ")", "]"])
def test_sentence_punctuation_stays_out_of_the_href(SV, trailing):
    """A period after a URL belongs to the sentence. Swallowed into the href it
    produces a link that 404s."""
    out = SV.render_markdown(f"See https://example.com/x{trailing}")
    assert "<a href='https://example.com/x'>" in out
    assert f"</a>{trailing}" in out


@pytest.mark.parametrize("hostile", [
    "https://x.com/'onmouseover='alert(1)",
    'https://x.com/a"b',
])
def test_a_bare_url_bearing_a_quote_is_not_linked_at_all(SV, hostile):
    """Same judgement the markdown-link rule makes. The quote arrives as an
    entity, so the match stops before it — and a link built from that prefix
    would point somewhere the author did not write. Plain text is honest."""
    out = SV.render_markdown(hostile)
    assert "<a " not in out


def test_no_markup_survives_into_a_bare_link(SV):
    """`<` ends the URL, so the tag that follows is escaped text beside the
    link rather than markup inside it."""
    out = SV.render_markdown("https://x.com/<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_a_bare_url_keeps_its_whole_query_string(SV):
    """`&` arrives as `&amp;`. Stopping there would link the first half of the
    query — a link to somewhere the author did not write."""
    out = SV.render_markdown("See https://x.com/s?a=1&b=2 now.")
    assert "<a href='https://x.com/s?a=1&amp;b=2'>" in out


def test_only_http_schemes_are_linkified(SV):
    """Deck's own allowlist would refuse a `javascript:` URL at click time. It
    never becomes a link in the first place."""
    for scheme in ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"]:
        assert "<a " not in SV.render_markdown(scheme)


def test_the_newest_turn_is_first_in_the_dom_so_the_page_opens_on_it(SV):
    """Deck reloads the whole document whenever the conversation changes, and a
    fresh document starts at the beginning of the flow — so the turns are
    emitted REVERSED inside a `column-reverse` container. The flow therefore
    begins at the newest turn while the visual order still reads oldest-first.

    It was an `autofocus` anchor, and focusing an element in the pane stole the
    composer's first responder: the operator's keystrokes stopped landing in the message
    box and macOS beeped at every render. Verified in WebKit that nothing is
    focused now and the newest turn is what the page opens on.
    """
    rows = [{"kind": "prompt", "display": SV.FULL, "label": "operator", "body": "OLDEST"},
            {"kind": "text", "display": SV.FULL, "label": "claude", "body": "NEWEST"}]
    page = SV.render_html(rows, "x")

    assert "autofocus" not in page.split("<style>")[-1].split("</style>")[-1], \
        "no element may be focused; it steals the composer's first responder"
    assert "column-reverse" in page
    assert page.index("NEWEST") < page.index("OLDEST"), \
        "the newest turn must come first in the DOM for column-reverse to anchor on it"


def test_an_empty_session_has_no_turn_container(SV):
    """Nothing to scroll to, so no container and no anchoring."""
    assert "class=turns" not in SV.render_html([], "x")


def test_a_url_ending_in_an_ampersand_keeps_its_entity_whole(SV):
    """`;` is not sentence punctuation here. Trimming it split the `&amp;` the
    URL rule had just taken care to match as an atom — a malformed href and a
    stray semicolon in the prose."""
    out = SV.render_markdown("See https://x.com/api?token=abc& for more.")
    assert "<a href='https://x.com/api?token=abc&amp;'>" in out
    assert "</a>; " not in out


def test_a_stray_close_tag_cannot_reopen_linkification_inside_a_span(SV):
    """Unreachable from _inline_md's balanced output, which is why the counters
    are per-element: the invariant is written down rather than assumed."""
    out = SV._linkify_bare("<code>a</a> https://x.com/y </code>")
    assert "<a href=" not in out


# --- the pane's canvas and its reading face -----------------------------------


def test_deck_can_set_the_page_background(SV):
    """Deck sends it because only the shell knows what the terminal beside the
    pane is painted: the colour is per-session, user-remappable, and lives in
    Deck's theme catalog. The renderer cannot derive it."""
    page = SV.render_html([], "x", theme="dark", background="#2b1d12")
    assert "--bg:#2b1d12" in page


def test_the_background_override_beats_the_appearance_blocks(SV):
    """SOURCE ORDER IS NOT ENOUGH, and the first version of this test only
    checked that. The appearance rules are `:root[data-theme="dark"]` and
    `:root:not([data-theme])`, both specificity (0,2,0); a bare `:root` is
    (0,1,0) and loses to them however late it appears, because order decides
    ties and nothing else. The canvas applied in light mode and was silently
    discarded in dark — the mode Deck runs in — while this test passed."""
    page = SV.render_html([], "x", theme="dark", background="#2b1d12")
    assert "--bg:#2b1d12!important" in page
    assert page.index("--bg:#2b1d12") > page.index("color-scheme: dark")


@pytest.mark.parametrize("theme", ["dark", "light", None])
def test_the_canvas_applies_in_every_appearance(SV, theme):
    """The bug was mode-specific, so the guard is parametrised over all three
    paths: stamped dark, stamped light, and unstamped following the system."""
    page = SV.render_html([], "x", theme=theme, background="#2b1d12")
    assert "--bg:#2b1d12!important" in page


@pytest.mark.parametrize("hostile", [
    "red; } body { background: url(https://evil/x)",
    "#fff</style><script>alert(1)</script>",
    "javascript:alert(1)",
    "var(--ink)",
    "",
])
def test_only_a_hex_colour_reaches_the_stylesheet(SV, hostile):
    """The value lands inside a `<style>` block, where an unvalidated string
    could close the block and open a script tag."""
    page = SV.render_html([], "x", theme="dark", background=hostile)
    assert "--bg:" not in page.split("</style>")[-1]
    assert "<script>" not in page
    assert "evil" not in page


def test_no_background_leaves_the_shipped_palette_alone(SV):
    page = SV.render_html([], "x", theme="dark")
    assert page.count("<style>") == 1


def test_prose_is_serif_and_code_is_not(SV):
    """Only what a human reads at length. Tool calls and their output stay
    monospace — they are code, and the change of face is what tells the reader
    which is which without a label."""
    page = SV.render_html([], "x", theme="dark")
    assert "ui-serif" in page
    body_rule = [ln for ln in page.splitlines() if ln.startswith(".body { font-family")]
    assert body_rule, "prose has no serif rule"
    assert ".body code, .body pre, .body pre code { font-family: ui-monospace" in page


def test_a_collapsed_row_is_not_serif(SV):
    """`summary` carries the tool call's label and keeps its own monospace rule."""
    page = SV.render_html([], "x", theme="dark")
    summary = [ln for ln in page.splitlines() if ln.startswith("summary {")]
    assert summary and "ui-serif" not in "".join(summary)


# --- tables -------------------------------------------------------------------


def test_a_markdown_table_renders_as_a_table(SV):
    """The operator's report: a table that reads fine in the terminal arrived as "just a
    glob". It had no handling at all, so its rows fell through to the paragraph
    buffer and were joined with spaces into one line."""
    out = SV.render_markdown(
        "| | before | after |\n|---|---|---|\n| branches | 9 | 4 |")
    assert "<table>" in out
    assert "<th>before</th>" in out
    assert "<td>branches</td>" in out
    assert out.count("<tr>") == 2


def test_a_table_is_recognised_by_its_delimiter_not_by_pipes(SV):
    """A line with pipes is usually prose — `a | b` in a sentence, a shell
    pipeline — so the row under the header is what says a table was meant."""
    out = SV.render_markdown("the choice is a | b | c, roughly")
    assert "<table>" not in out
    assert "<p>" in out


def test_a_table_ends_at_the_first_line_without_pipes(SV):
    out = SV.render_markdown(
        "| a |\n|---|\n| 1 |\n\nprose after")
    assert "<table>" in out
    assert out.rstrip().endswith("<p>prose after</p>")


def test_prose_before_a_table_is_not_swallowed_into_it(SV):
    out = SV.render_markdown("Here is the summary.\n\n| a |\n|---|\n| 1 |")
    assert out.startswith("<p>Here is the summary.</p>")


def test_alignment_markers_are_honoured(SV):
    out = SV.render_markdown("| a | b | c |\n|:---|---:|:---:|\n| 1 | 2 | 3 |")
    assert "text-align:left" in out
    assert "text-align:right" in out
    assert "text-align:center" in out


def test_an_escaped_pipe_stays_inside_its_cell(SV):
    out = SV.render_markdown("| a | b |\n|---|---|\n| x \\| y | z |")
    assert "<td>x | y</td>" in out


def test_inline_markup_works_inside_a_cell(SV):
    out = SV.render_markdown("| a |\n|---|\n| `code` and **bold** |")
    assert "<code>code</code>" in out
    assert "<strong>bold</strong>" in out


def test_a_cell_cannot_inject_markup(SV):
    """Cells go through the same escape-then-markup path as every other body."""
    out = SV.render_markdown("| a |\n|---|\n| <script>alert(1)</script> |")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_a_table_inside_a_fence_is_left_alone(SV):
    """A fence is verbatim: a table drawn in a code block is being shown, not
    rendered."""
    out = SV.render_markdown("```\n| a |\n|---|\n| 1 |\n```")
    assert "<table>" not in out
    assert "<pre><code>" in out


def test_a_ragged_row_does_not_raise(SV):
    """Model output is not always well-formed, and the renderer's rule is that
    nothing raises."""
    out = SV.render_markdown("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |")
    assert "<table>" in out


@pytest.mark.parametrize("delim", ["---", "--", "----", ":---:", "- - -"])
def test_a_horizontal_rule_after_a_pipe_is_not_a_table(SV, delim):
    """THE FALSE POSITIVE. A delimiter without a pipe matched, so prose
    containing a pipe followed by a rule became a one-row table with an empty
    body — and the rule was swallowed. Every real GFM delimiter has a pipe."""
    out = SV.render_markdown(f"the choice is a | b\n{delim}\ncontinued")
    assert "<table>" not in out


@pytest.mark.parametrize("delim", ["|---|", "---|---", "|:---|---:|", "| --- |"])
def test_every_real_delimiter_form_still_makes_a_table(SV, delim):
    """The other half of that fix: requiring a pipe must not cost a legitimate
    table, including the single-column form."""
    columns = "| a | b |" if "|" in delim[1:-1] or delim.count("-") > 3 else "| a |"
    out = SV.render_markdown(f"{columns}\n{delim}\n| 1 | 2 |")
    assert "<table>" in out, delim


# --- prose line height ---------------------------------------------------------


@pytest.mark.parametrize("height,expected", [
    (1.65, "1.65"), (1.9, "1.90"), (1.2, "1.20"), (2.4, "2.40"),
])
def test_a_valid_line_height_opens_the_prose_leading(SV, height, expected):
    """A custom property rather than a rule, so the shipped 1.65 stays the
    fallback written beside the declaration it belongs to."""
    page = SV.render_html([], "x", theme="dark", line_height=height)
    assert f"--body-line-height:{expected};" in page


@pytest.mark.parametrize("bad", [1.0, 3.0, 0, -1, float("nan"), float("inf"), "tall", None])
def test_an_out_of_range_or_unusable_line_height_is_ignored(SV, bad):
    """Below ~1.2 descenders collide with the next line's ascenders; past ~2.4 a
    paragraph stops reading as one block. A bad value degrades to the shipped
    leading rather than an unreadable page — the rule `--scale` follows."""
    page = SV.render_html([], "x", theme="dark", line_height=bad)
    assert "--body-line-height:" not in page


def test_the_page_carries_its_own_default_leading(SV):
    """The fallback lives in the CSS, so a page rendered with no flag is
    unchanged from before the flag existed."""
    page = SV.render_html([], "x", theme="dark")
    assert "--body-line-height:" not in page
    assert "line-height: var(--body-line-height, 1.65)" in page


def test_line_height_is_independent_of_scale(SV):
    """Two controls, deliberately. `--scale` moves the whole document, prose and
    tool output together; this opens the leading of running prose only, because
    code wants its own tighter setting. One number would ruin one of them."""
    page = SV.render_html([], "x", theme="dark", scale=1.4, line_height=1.9)
    assert ":root{font-size:140%;}" in page
    assert "--body-line-height:1.90;" in page


# --- text scale ---------------------------------------------------------------


@pytest.mark.parametrize("scale,expected", [
    (1.0, "100%"), (1.4, "140%"), (0.5, "50%"), (3.0, "300%"),
])
def test_a_valid_scale_moves_the_root_font_size(SV, scale, expected):
    """The ROOT size, not each rule: every size on the page is already relative
    to it, so one number moves the document coherently and nothing has to be
    enumerated."""
    page = SV.render_html([], "x", theme="dark", scale=scale)
    assert f":root{{font-size:{expected};}}" in page


@pytest.mark.parametrize("bad", [4.0, 0.1, -1, 0, float("nan"), float("inf"), "big", None])
def test_an_out_of_range_or_unusable_scale_is_ignored(SV, bad):
    """The value lands in a CSS declaration and is computed by the caller from a
    font size, so a bad config must degrade to the shipped size rather than an
    unreadable page."""
    page = SV.render_html([], "x", theme="dark", scale=bad)
    assert ":root{font-size:" not in page


def test_no_scale_leaves_one_extra_stylesheet_at_most(SV):
    page = SV.render_html([], "x", theme="dark")
    assert ":root{font-size:" not in page


def test_every_font_size_is_relative_so_the_scale_reaches_all_of_it(SV):
    """`body` was an absolute 15px, so the empty-state line stayed put while
    everything around it resized — and the comment claiming 'every size here is
    relative' was false for whoever edited this next. Border widths and radii
    stay in px on purpose: a hairline is a hairline at any text size."""
    import re
    page = SV.render_html([], "x", theme="dark")
    css = page[page.index("<style>"):page.index("</style>")]
    absolute = re.findall(r"font(?:-size)?: *[^;]*?\d+px", css)
    assert not absolute, f"absolute font size(s) will not scale: {absolute}"


def test_the_reversed_dom_order_is_documented_as_a_trade(SV):
    """The clipboard follows DOM order, so a selection spanning several turns
    copies newest-first. That is accepted — a reversed paste is visible and
    fixable, a keyboard that stops working mid-sentence is not — but it must be
    written down where the next reader of this CSS will find it, rather than
    rediscovered as a bug.

    Matched on two words rather than a phrase: the comment is wrapped prose, and
    a phrase assertion breaks on a rewrap while the documentation is still there.
    """
    page = SV.render_html([], "x", theme="dark")
    css = page[page.index("<style>"):page.index("</style>")]
    assert "clipboard" in css and "WHAT THIS COSTS" in css, \
        "the copy-order trade must stay documented beside the rule that causes it"


# --- deck-open anchors (Deck phase 1) -----------------------------------------


@pytest.mark.parametrize("name,inp,expected_origin", [
    # _PATH_TOOLS
    ("Read",         {"file_path": "/a/b.py"},          "path"),
    ("Edit",         {"file_path": "/a/b.py",
                      "old_string": "x", "new_string": "y"}, "path"),
    ("Write",        {"file_path": "/a/b.py"},          "path"),
    ("NotebookEdit", {"notebook_path": "/a/b.ipynb"},   "path"),
    # _KB_TOOLS — short forms
    ("kb_get",       {"id": "deck/specs/foo"},           "kb"),
    ("kb_checkout",  {"id": "deck/specs/foo"},           "kb"),
    ("kb_create",    {"id": "deck/specs/foo"},           "kb"),
    ("kb_update",    {"id": "deck/specs/foo"},           "kb"),
    ("kb_propose",   {"id": "deck/specs/foo"},           "kb"),
    # _KB_TOOLS — prefixed forms (as the tool_use name appears in transcripts)
    ("mcp__kb__kb_get",      {"id": "deck/specs/foo"},  "kb"),
    ("mcp__kb__kb_checkout", {"id": "deck/specs/foo"},  "kb"),
    ("mcp__kb__kb_create",   {"id": "deck/specs/foo"},  "kb"),
    ("mcp__kb__kb_update",   {"id": "deck/specs/foo"},  "kb"),
    ("mcp__kb__kb_propose",  {"id": "deck/specs/foo"},  "kb"),
])
def test_allowlisted_tools_gain_a_deck_open_anchor(SV, name, inp, expected_origin):
    """Every tool on the allowlist emits a deck-open anchor with the right origin."""
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", "name": name,
                                               "input": inp}))])
    out = SV.render_html(rows, "t")
    assert f"deck-open:?origin={expected_origin}" in out
    assert "<a href=" in out


def test_a_tool_off_the_allowlist_has_no_anchor(SV):
    """Failing closed: Bash is not on the list and must render exactly as today."""
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", "name": "Bash",
                                               "input": {"command": "ls"}}))])
    out = SV.render_html(rows, "t")
    assert "deck-open:" not in out
    assert "Bash(ls)" in out


def test_anchor_href_carries_the_full_path_when_label_is_truncated(SV):
    """The label truncates at MAX_ARG; the href must carry the full path so Deck
    can locate the file unambiguously — spec subtlety 1."""
    import urllib.parse
    long_path = "/some/deeply/nested/" + "x" * 200 + ".py"
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", "name": "Read",
                                               "input": {"file_path": long_path}}))])
    out = SV.render_html(rows, "t")
    assert urllib.parse.quote(long_path, safe="") in out


def test_a_path_with_space_ampersand_and_quote_is_encoded(SV):
    """Spec requirement: test a path containing a space, an ampersand, and a quote.
    The ref is percent-encoded for URL and the attribute is HTML-escaped — getting
    either wrong turns the link into an injection point — spec subtlety 2."""
    import urllib.parse
    path = "/srv/convivy/my file & 'work'.py"
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", "name": "Read",
                                               "input": {"file_path": path}}))])
    out = SV.render_html(rows, "t")
    encoded = urllib.parse.quote(path, safe="")
    assert encoded in out          # percent-encoded ref present
    assert "%20" in out            # space encoded
    assert "%26" in out            # ampersand encoded
    assert "%27" in out            # single quote encoded
    assert "&amp;ref=" in out      # query separator HTML-escaped in the attribute


def test_malformed_tool_input_degrades_to_no_anchor_and_does_not_raise(SV):
    """A tool_use whose input lacks the expected field, or whose value is not a
    string, renders as today with no anchor. It must not raise and must not drop
    the row — spec degradation requirement."""
    cases = [
        # Read without file_path: identifies by a different field, no anchor.
        {"name": "Read", "input": {"command": "something"}},
        # Non-dict input.
        {"name": "Read", "input": None},
        # Non-string file_path value.
        {"name": "Read", "input": {"file_path": 42}},
        # KB tool without an id field.
        {"name": "kb_get", "input": {"query": "something"}},
    ]
    for case in cases:
        rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", **case}))])
        out = SV.render_html(rows, "t")
        assert "deck-open:" not in out, f"unexpected anchor for {case}"
        # The row must still appear (not dropped).
        assert "<details" in out, f"row was dropped for {case}"


def test_kb_anchor_encodes_slashes_in_the_id(SV):
    """A KB id like `deck/specs/foo` has slashes; they must be percent-encoded in
    the ref so Deck receives an unambiguous query value."""
    import urllib.parse
    kb_id = "deck/specs/opening-a-file-from-the-session"
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "mcp__kb__kb_get",
                                               "input": {"id": kb_id}}))])
    out = SV.render_html(rows, "t")
    assert urllib.parse.quote(kb_id, safe="") in out
    assert "deck-open:?origin=kb" in out


def test_anchor_text_and_href_derive_from_the_same_key(SV):
    """Regression: NotebookEdit({'pattern': '/some/*.py', 'notebook_path': '/foo.ipynb'}).

    _identifying_arg walks IDENTIFYING_ARG and finds 'pattern' before it reaches
    'notebook_path'. _tool_anchor walks _PATH_TOOL_KEYS (which skips 'pattern')
    and finds 'notebook_path'. Before the fix, tool_arg came from _identifying_arg
    ('/some/*.py') while tool_ref came from _tool_anchor ('/foo.ipynb'), so the
    visible anchor text named one file and the href opened another.
    """
    inp = {"pattern": "/some/*.py", "notebook_path": "/foo.ipynb"}
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "NotebookEdit",
                                               "input": inp}))])
    tool_rows = [r for r in rows if r.get("kind") == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["tool_arg"] == "/foo.ipynb"
    assert tool_rows[0]["tool_ref"] == "/foo.ipynb"
    out = SV.render_html(rows, "t")
    ref_encoded = urllib.parse.quote("/foo.ipynb", safe="")
    assert f"ref={ref_encoded}" in out


# --- list-valued identifying arguments (SendUserFile and the class) -----------
#
# SendUserFile's 'files' is a list of paths. The original fallback iterated
# tool_input.values() and took the first STRING, skipping the list — so it fell
# through to 'status: "normal"' and the filename never reached the label. Any
# tool with a list-valued primary argument has this class of bug.


def test_send_user_file_single_file_labels_the_path(SV):
    """The primary symptom: SendUserFile must show the filename, not 'normal'."""
    rows = SV.classify(_assistant({"type": "tool_use", "name": "SendUserFile",
                                   "input": {"files": ["/tmp/report.md"],
                                             "caption": "here it is",
                                             "status": "normal"}}))
    assert rows[0]["label"] == "SendUserFile(/tmp/report.md)"


def test_send_user_file_single_file_gains_a_deck_open_anchor(SV):
    """SendUserFile is on the anchor allowlist; a single-file call gets a link."""
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "SendUserFile",
                                               "input": {"files": ["/tmp/report.md"],
                                                         "status": "normal"}}))])
    out = SV.render_html(rows, "t")
    assert "deck-open:?origin=path" in out
    assert urllib.parse.quote("/tmp/report.md", safe="") in out


def test_send_user_file_multi_file_uses_first_path_for_label_and_anchor(SV):
    """A three-file call names the first path plus a count suffix; anchor is the first path only."""
    files = ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "SendUserFile",
                                               "input": {"files": files,
                                                         "status": "normal"}}))])
    tool_rows = [r for r in rows if r.get("kind") == "tool"]
    assert tool_rows[0]["label"] == "SendUserFile(/tmp/a.png +2)"
    # tool_arg carries the decorated label (what _summary_label renders as link text).
    # Previously this was full_ref[:MAX_ARG], stripping the +N and killing the feature.
    assert tool_rows[0]["tool_arg"] == "/tmp/a.png +2"
    # anchor ref is the bare path — count decoration lives in the label only
    assert tool_rows[0]["tool_ref"] == "/tmp/a.png"
    out = SV.render_html(rows, "t")
    assert "deck-open:?origin=path" in out
    assert urllib.parse.quote("/tmp/a.png", safe="") in out
    # The count must appear in the rendered anchor text, not just in the row dict.
    assert "+2" in out


def test_send_user_file_anchor_encodes_path_with_space_ampersand_quote(SV):
    """Percent-encode the ref, HTML-escape the attribute — spec requirement."""
    path = "/tmp/my file & 'work'.md"
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "SendUserFile",
                                               "input": {"files": [path]}}))])
    out = SV.render_html(rows, "t")
    encoded = urllib.parse.quote(path, safe="")
    assert encoded in out
    assert "%20" in out    # space
    assert "%26" in out    # ampersand
    assert "%27" in out    # single quote
    assert "&amp;ref=" in out   # query separator HTML-escaped in the attribute


def test_send_user_file_empty_list_degrades_gracefully(SV):
    """An empty files list must not raise and must not produce an anchor."""
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                               "name": "SendUserFile",
                                               "input": {"files": [],
                                                         "status": "normal"}}))])
    out = SV.render_html(rows, "t")
    assert "deck-open:" not in out
    assert "<details" in out    # row must still appear


def test_send_user_file_non_string_list_elements_degrade_gracefully(SV):
    """A list of dicts or ints must not raise and must not produce a spurious label."""
    for bad_files in ([{"path": "/tmp/f.png"}], [42, 99]):
        rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use",
                                                   "name": "SendUserFile",
                                                   "input": {"files": bad_files,
                                                             "status": "normal"}}))])
        out = SV.render_html(rows, "t")
        assert "deck-open:" not in out
        assert "<details" in out


def test_label_matches_anchor_when_caption_precedes_files(SV):
    """Regression: caption before files in insertion order must not become the label.

    {'caption': 'The Q3 report', 'files': ['/tmp/q3.md'], 'status': 'normal'} had
    the label 'The Q3 report' while the anchor pointed at /tmp/q3.md — text and
    href naming different things (#346 recurrence). _identifying_arg now derives the
    label from _tool_anchor's key-based resolution, so both always share one value.
    """
    rows = SV.classify(_assistant({"type": "tool_use", "name": "SendUserFile",
                                   "input": {"caption": "The Q3 report",
                                             "files": ["/tmp/q3.md"],
                                             "status": "normal"}}))
    assert rows[0]["label"] == "SendUserFile(/tmp/q3.md)"


def test_multi_file_count_reaches_rendered_anchor_text(SV):
    """Regression: the +N count must appear in the rendered HTML anchor text.

    Exact reproduce from the spec: caption before files, three files. classify()
    previously overwrote tool_arg with full_ref[:MAX_ARG], discarding the +N that
    _identifying_arg had computed. _summary_label renders tool_arg as the visible
    link text, so the count never reached the page while the row dict's 'label'
    field (tested separately) showed it fine — the existing tests passed while the
    feature was dead.
    """
    rec = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "SendUserFile", "id": "t1",
         "input": {"caption": "The Q3 report",
                   "files": ["/tmp/q3.md", "/tmp/b.png", "/tmp/c.png"],
                   "status": "normal"}}]}}
    rows = SV.rows_for([json.dumps(rec)])
    out = SV.render_html(rows, "t")
    # +2 must be visible inside the anchor, not just in the row dict.
    assert "+2" in out
    # href must point at the first file only.
    assert urllib.parse.quote("/tmp/q3.md", safe="") in out
    assert "deck-open:?origin=path" in out


def test_list_arg_never_falls_through_to_unrelated_scalar(SV):
    """THE CLASS INVARIANT. When the first non-IDENTIFYING_ARG value is a list,
    its first string element is taken — not a later unrelated scalar field.

    Tests the generic fallback path (tool name not on any anchor list), confirming
    the fallback loop handles the list case correctly regardless of anchor dispatch.
    """
    # Use an unknown tool name so the anchor branch does not fire.
    inp = {"files": ["/tmp/scratchpad/tracking.md"], "status": "normal"}
    result = SV._identifying_arg("UnknownTool", inp)
    assert result == "/tmp/scratchpad/tracking.md"
    assert result != "normal"


def test_list_arg_in_identifying_arg_key_position(SV):
    """A list under a named IDENTIFYING_ARG key also takes the first element.

    This exercises the first (named-key) loop of the generic fallback path.
    """
    # 'query' IS in IDENTIFYING_ARG; pass an unknown tool so anchor branch is skipped.
    inp = {"query": ["/path/a.txt", "/path/b.txt"]}
    result = SV._identifying_arg("UnknownTool", inp)
    assert result == "/path/a.txt"


def test_ask_user_question_returns_empty_label(SV):
    """AskUserQuestion has one field, 'questions', a list of dicts.

    _identifying_arg returns '' before and after this PR: there is no scalar
    fallback and _first_str returns None for a list of dicts. The row label is
    'AskUserQuestion' with no argument. dict-in-list handling is explicitly out of
    scope — a future fix would need to choose which sub-field (e.g. 'question',
    'text') identifies a row, which requires per-tool knowledge.
    """
    inp = {"questions": [{"question": "Which env?", "options": ["prod", "stage"]}]}
    result = SV._identifying_arg("AskUserQuestion", inp)
    assert result == ""


# --- text-block path linkification (Deck phase 1 extension) -------------------
#
# Phase 1 anchored only the argument of a folded tool-call row. The operator found that
# the paths a reader most wants to open are the ones written into a sentence —
# a user message, an assistant prose block, a Bash command string — and none
# of those were clickable. These tests cover the extension.


def test_an_existing_absolute_path_in_prose_becomes_a_deck_open_anchor(SV, tmp_path):
    """The primary case: an absolute path in a full prose row that actually exists."""
    target = tmp_path / "hello.py"
    target.write_text("x")
    rows = [{"kind": "prompt", "display": SV.FULL, "label": "you", "body": str(target)}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:?origin=path" in out
    assert urllib.parse.quote(str(target), safe="") in out


def test_a_nonexistent_path_in_prose_stays_plain_text(SV, tmp_path):
    """The existence check is the quieter, truer behaviour the operator chose over instant linking."""
    path = str(tmp_path / "does_not_exist.py")
    rows = [{"kind": "prompt", "display": SV.FULL, "label": "you", "body": path}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:" not in out


def test_a_tilde_path_is_expanded_before_the_existence_check(SV, monkeypatch, tmp_path):
    """A ~/... path fails Deck's absolute-path validation if emitted un-expanded.
    Expand first, then check, then emit the absolute path in the ref."""
    target = tmp_path / "file.py"
    target.write_text("x")
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(target) if p.startswith("~/") else p)
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": "~/file.py"}]
    out = SV.render_html(rows, "t")
    assert "deck-open:?origin=path" in out
    # The ref carries the expanded absolute path, not the tilde form.
    assert urllib.parse.quote(str(target), safe="") in out
    assert "~" not in out.split("ref=")[1].split("'")[0]


def test_a_path_in_a_code_span_becomes_a_clickable_anchor(SV, tmp_path):
    """Backticks are how prose names a path — the reader must be able to click it.

    The <code> element stays, so the reader still sees it as code (monospace,
    tinted background).  The deck-open anchor is inside the <code> span.
    """
    target = tmp_path / "real.py"
    target.write_text("x")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude",
             "body": f"`{target}`"}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:?origin=path" in out
    assert urllib.parse.quote(str(target), safe="") in out
    # <code> must survive — reader still sees it styled as code.
    assert "<code>" in out


def test_a_nonexistent_backticked_path_stays_plain_text(SV, tmp_path):
    """Existence check filters non-real paths even inside a code span."""
    path = str(tmp_path / "no_such_file.py")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude",
             "body": f"`{path}`"}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:?origin=path" not in out
    # The <code> element is still present — it is rendered as code, just not linked.
    assert "<code>" in out


def test_a_path_inside_tool_output_pre_is_not_linked(SV, tmp_path):
    """pre stays in _NO_PATH_LINKIFY — a path inside any <pre> stays plain text."""
    target = tmp_path / "real.py"
    target.write_text("x")
    raw = f"<pre class='arg'>{target}</pre>"
    result = SV._linkify_file_paths(raw, {}, str(tmp_path))
    assert "deck-open:" not in result


def test_a_path_inside_an_existing_anchor_gets_no_nested_anchor(SV, tmp_path):
    """a stays in _NO_PATH_LINKIFY — an anchor inside an anchor is invalid HTML."""
    target = tmp_path / "real.py"
    target.write_text("x")
    raw = f"<a href='something'>{target}</a>"
    result = SV._linkify_file_paths(raw, {}, str(tmp_path))
    # The outer <a> stays; no second <a> is added around the path text.
    assert result.count("<a ") == 1


def test_a_path_inside_a_fenced_block_is_not_linked(SV, tmp_path):
    """Fenced blocks are verbatim — a path shown in code is shown, not navigated."""
    target = tmp_path / "real.py"
    target.write_text("x")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude",
             "body": f"```\n{target}\n```"}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:?origin=path" not in out


def test_text_path_href_encodes_correctly_and_html_escapes_the_ampersand(SV, tmp_path):
    """The & in the query string is a named security requirement from the spec.

    deck-open:?origin=path&ref=<encoded> contains a literal & that must become
    &amp; inside the HTML attribute.  Both the percent-encoding and the HTML
    entity must be present.
    """
    target = tmp_path / "file.py"
    target.write_text("x")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": str(target)}]
    out = SV.render_html(rows, "t")
    assert "&amp;ref=" in out                                       # & HTML-escaped
    assert urllib.parse.quote(str(target), safe="") in out         # ref percent-encoded


def test_a_path_with_space_in_prose_does_not_create_a_partial_link(SV, tmp_path):
    """A space stops the path regex before the end of the token. The partial match
    (/tmp/my) won't exist, so no broken link to a different file is produced."""
    full = tmp_path / "my file.py"
    full.write_text("x")
    body = f"{tmp_path}/my file.py"
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": body}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    # The partial token (stopping at space) should not be linked.
    assert "deck-open:?origin=path" not in out


def test_a_relative_path_with_extension_resolves_against_cwd(SV, tmp_path):
    """A relative candidate like 'lib/session_view.py' is resolved against the cwd."""
    sub = tmp_path / "lib"
    sub.mkdir()
    (sub / "session_view.py").write_text("x")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude",
             "body": "lib/session_view.py"}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    assert "deck-open:?origin=path" in out
    expected_ref = urllib.parse.quote(str(tmp_path / "lib" / "session_view.py"), safe="")
    assert expected_ref in out


def test_a_relative_path_without_cwd_stays_plain_text(SV, tmp_path):
    """Without a cwd, relative candidates are not checked and not linked."""
    sub = tmp_path / "lib"
    sub.mkdir()
    (sub / "file.py").write_text("x")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude",
             "body": "lib/file.py"}]
    out = SV.render_html(rows, "t")    # no cwd
    assert "deck-open:?origin=path" not in out


def test_a_repeated_path_is_existence_checked_once(SV, tmp_path, monkeypatch):
    """Cache within one render: a path seen twenty times costs one stat."""
    target = tmp_path / "file.py"
    target.write_text("x")
    count = [0]
    real = os.path.lexists

    def counting(p):
        if p == str(target):
            count[0] += 1
        return real(p)

    monkeypatch.setattr(os.path, "lexists", counting)
    body = "\n\n".join([str(target)] * 5)
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": body}]
    SV.render_html(rows, "t")
    assert count[0] == 1


def test_a_broken_symlink_still_links(SV, tmp_path):
    """lexists returns True for broken symlinks; the spec requires this behaviour."""
    link = tmp_path / "broken.py"
    link.symlink_to(tmp_path / "target_does_not_exist.py")
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": str(link)}]
    out = SV.render_html(rows, "t")
    assert "deck-open:?origin=path" in out


def test_a_path_in_a_collapsed_row_is_not_prose_linked(SV, tmp_path):
    """Text linkification applies only to FULL prose rows.

    Collapsed rows render their label through _summary_label (which handles
    tool-call anchors) and their body in a <pre> block (which _linkify_file_paths
    skips). Only a path tool on the allowlist gets an anchor for a collapsed row.
    """
    target = tmp_path / "file.py"
    target.write_text("x")
    rows = [{"kind": "event", "display": SV.COLLAPSED,
             "label": str(target), "body": str(target)}]
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    # No tool_origin → no anchor from _summary_label either.
    assert "deck-open:?origin=path" not in out


def test_a_path_already_linked_via_tool_call_is_not_double_linked(SV, tmp_path):
    """A Read() call with an existing file produces one anchor from _summary_label.
    The body of the collapsed row is in <pre>, which _linkify_file_paths skips,
    so no second anchor appears."""
    target = tmp_path / "file.py"
    target.write_text("x")
    rows = SV.rows_for([json.dumps(_assistant({"type": "tool_use", "name": "Read",
                                               "input": {"file_path": str(target)}}))])
    out = SV.render_html(rows, "t", cwd=str(tmp_path))
    # Exactly one anchor: from the tool-call row, not a duplicate from text linking.
    assert out.count("deck-open:?origin=path") == 1


# --- render_file_html (Deck phase 1 — file viewer entry point) ----------------


@needs_renderer
def test_render_file_html_renders_markdown_content(SV, tmp_path):
    """Deck fetches rendered HTML for a Markdown file rather than running its own
    renderer, so the output here and a session message look alike."""
    f = tmp_path / "spec.md"
    f.write_text("# Title\n\n- one\n- two\n")
    page = SV.render_file_html(f)
    # The JS renderer emits the actual heading level (h1), not render_markdown's +2 offset.
    assert "<h1>Title</h1>" in page
    assert "<li>one</li>" in page
    assert "<!doctype html>" in page
    assert "<style>" in page            # self-contained, no external assets


@needs_renderer
def test_render_file_html_stamps_the_theme(SV, tmp_path):
    """Deck is the appearance authority; data-theme must arrive from --theme, not
    from the page self-deciding via the system preference."""
    f = tmp_path / "doc.md"
    f.write_text("hello")
    assert '<html data-theme="dark">' in SV.render_file_html(f, theme="dark")
    assert '<html data-theme="light">' in SV.render_file_html(f, theme="light")
    assert "<html" not in SV.render_file_html(f)    # no theme → no stamp


def test_render_file_html_raises_on_missing_file(SV, tmp_path):
    """Callers must get an OSError, not a silent empty page."""
    with pytest.raises(OSError):
        SV.render_file_html(tmp_path / "no_such_file.md")


@needs_renderer
def test_render_file_html_links_absolute_paths_in_the_document(SV, tmp_path):
    """Paths in the file's content are linkified the same way prose paths are."""
    sibling = tmp_path / "sibling.py"
    sibling.write_text("x")
    doc = tmp_path / "doc.md"
    doc.write_text(f"See {sibling} for details.")
    page = SV.render_file_html(doc)
    assert "deck-open:?origin=path" in page
    assert urllib.parse.quote(str(sibling), safe="") in page


@needs_renderer
def test_render_file_html_uses_the_files_parent_dir_as_cwd(SV, tmp_path):
    """A relative path in the file resolves against the file's directory."""
    sub = tmp_path / "lib"
    sub.mkdir()
    target = sub / "module.py"
    target.write_text("x")
    doc = tmp_path / "doc.md"
    doc.write_text("See lib/module.py for the implementation.")
    page = SV.render_file_html(doc)
    assert "deck-open:?origin=path" in page
    assert urllib.parse.quote(str(target), safe="") in page


# --- whitespace-only cwd does not link relative paths (reviewer finding) ------


def test_a_whitespace_only_cwd_does_not_link_relative_paths(SV):
    """THE BUG: bin/session-view computed cwd = raw_cwd.strip() if raw_cwd else None,
    which yields '' when tmux returns a whitespace-only #{pane_current_path}.  ''
    is not None, so _sub's guard ('if not cwd: return raw') did not fire, and
    relative paths were resolved via os.path.join('', rel) — which is just rel —
    then checked with lexists(rel) against session-view's OWN process cwd instead
    of the session's.  Fix: raw_cwd.strip() or None in the bin; the library guard
    is `not cwd` so it rejects '' at both levels.
    """
    import os
    # A relative path that DOES exist relative to the test process's cwd.
    # If the bug were present, lexists() against the process cwd would return True
    # and produce a spurious anchor.
    rel = "tests/test_session_view.py"
    assert os.path.lexists(rel), "sanity: expected this file to exist from repo root"
    rows = [{"kind": "speech", "display": SV.FULL, "label": "claude", "body": rel}]
    out = SV.render_html(rows, "t", cwd="")
    assert "deck-open:?origin=path" not in out


# --- render_file_page: the content entry point ---------------------------------

def test_render_file_page_and_html_agree_for_the_same_bytes(SV, monkeypatch, tmp_path):
    """The file entry point is the content one with a read in front of it.

    render_file_html now delegates, so this is the property that says the
    refactor changed no behaviour for the path callers that existed before it.
    """
    monkeypatch.setattr(SV, '_render_via_bridge', lambda source, wikilinks: f"<p>{source}</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    body = "A paragraph.\n"
    f = tmp_path / "doc.md"
    f.write_text(body)

    assert SV.render_file_html(f) == SV.render_file_page(body, name="doc.md", cwd=str(tmp_path))


def test_render_file_page_names_the_document_in_title_and_heading(SV, monkeypatch):
    """A document piped in on stdin has a caller-supplied name rather than a
    filename this function could derive itself."""
    monkeypatch.setattr(SV, '_render_via_bridge', lambda source, wikilinks: "<p>hi</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_file_page("hi", name="report.md")

    assert "<title>report.md</title>" in page
    assert "<h1>report.md</h1>" in page


def test_render_file_page_without_cwd_does_not_link_relative_paths(SV, monkeypatch):
    """Content piped in on stdin has no known directory, so relative-path
    linkification is disabled — the same `not cwd` guard _linkify_file_paths
    already applies for a whitespace-only tmux cwd.
    """
    monkeypatch.setattr(SV, '_render_via_bridge',
                        lambda source, wikilinks: "<p>See lib/module.py for details.</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_file_page("See lib/module.py for details.", name="doc.md")

    assert "deck-open:?origin=path" not in page


# --- render_kb_html (Deck KB viewer entry point) ---------------------------------
#
# KB docs arrive via the knowledge index (read-only SQLite) rather than a
# checked-out file.  kb_checkout acquires a 4-hour lease; viewing must never do
# that.  These tests exercise the contract without touching the live index.


def _make_kb_index(tmp_path: Path) -> Path:
    """A minimal knowledge index with one document, for tests that need it."""
    import sqlite3
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE docs "
        "(id TEXT PRIMARY KEY, title TEXT, kind TEXT, project TEXT, "
        "status TEXT, body TEXT)"
    )
    conn.execute(
        "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?)",
        (
            "deck/specs/opening-a-file",
            "Opening a file from the session",
            "spec",
            "deck",
            "draft",
            "# Opening a file\n\nA session names files constantly.\n",
        ),
    )
    conn.commit()
    conn.close()
    return db


@needs_renderer
def test_render_kb_html_renders_doc_title_and_markup(SV, monkeypatch, tmp_path):
    """A doc that exists renders with its title and markdown converted to HTML."""
    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    page = SV.render_kb_html("deck/specs/opening-a-file")

    assert page is not None
    assert "Opening a file from the session" in page   # title in <title> and heading
    # The JS renderer emits the actual heading level (h1), not render_markdown's +2 offset.
    assert "<h1>Opening a file</h1>" in page
    assert "<p>" in page                               # prose rendered
    assert "<!doctype html>" in page                   # self-contained page


def test_render_kb_html_missing_doc_returns_none(SV, monkeypatch, tmp_path):
    """A doc that does not exist returns None without emitting a page."""
    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    result = SV.render_kb_html("no/such/doc")

    assert result is None


@needs_renderer
def test_render_kb_html_stamps_theme(SV, monkeypatch, tmp_path):
    """data-theme is written from the --theme argument, not self-decided."""
    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    assert '<html data-theme="dark">' in SV.render_kb_html(
        "deck/specs/opening-a-file", theme="dark"
    )
    assert '<html data-theme="light">' in SV.render_kb_html(
        "deck/specs/opening-a-file", theme="light"
    )
    assert "<html" not in SV.render_kb_html("deck/specs/opening-a-file")


def test_render_kb_html_connection_is_readonly(SV, monkeypatch, tmp_path):
    """The index connection opens in read-only mode; writes must be refused."""
    import sqlite3

    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    # _kb_connect opens read-only via the URI mode=ro flag. Attempting a write
    # through that connection must raise OperationalError.
    conn = SV._kb_connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO docs VALUES ('x','x','x','x','x','x')")
    conn.close()


def test_render_kb_html_id_is_bound_parameter_not_interpolated(SV, monkeypatch, tmp_path):
    """SQL injection guard: the id must be a bound parameter, never string-formatted.

    An id containing SQL metacharacters should return None (no match), not raise
    and not corrupt the index.

    THIS RUNS EVERYWHERE, DELIBERATELY. A hostile id never reaches the renderer —
    _kb_fetch misses and render_kb_html returns None before the bridge — so the
    injection assertion needs no Node. The "table survived" half is asserted
    against the database directly rather than by rendering a real document, which
    is what used to drag this test behind `needs_renderer` and leave a security
    guarantee checked only on the mini. Raised by the round-2 security review.
    """
    import sqlite3

    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    hostile_id = "'; DROP TABLE docs; --"
    result = SV.render_kb_html(hostile_id)
    assert result is None

    # The table must still be intact — read it directly, so the assertion holds
    # on a runner with no renderer.
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT id FROM docs WHERE id = ?", ("deck/specs/opening-a-file",)
        ).fetchall()
    assert rows == [("deck/specs/opening-a-file",)]


def test_render_kb_html_missing_index_returns_none(SV, monkeypatch, tmp_path):
    """When the index file does not exist, None is returned without raising."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", tmp_path / "nonexistent.db")

    result = SV.render_kb_html("deck/specs/opening-a-file")

    assert result is None


def test_render_kb_html_wrong_schema_returns_none(SV, monkeypatch, tmp_path):
    """A SQLite file that exists but lacks the docs table returns None without raising.

    connect() succeeds on any valid SQLite file; the OperationalError surfaces at
    execute time ("no such table: docs").  This was the uncovered case in the
    original test.
    """
    import sqlite3

    db = tmp_path / "wrong_schema.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE other (id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    result = SV.render_kb_html("deck/specs/opening-a-file")

    assert result is None


def test_render_kb_html_corrupt_file_returns_none(SV, monkeypatch, tmp_path):
    """A file that exists but is not a valid SQLite database returns None without raising.

    sqlite3.DatabaseError ("file is not a database") surfaces at execute time after
    connect() accepts the path.  This was the uncovered case in the original test.
    """
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database\x00\xff")
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    result = SV.render_kb_html("deck/specs/opening-a-file")

    assert result is None


@needs_renderer
def test_render_kb_html_includes_metadata_line(SV, monkeypatch, tmp_path):
    """Kind, project, and status appear as a metadata line above the body."""
    db = _make_kb_index(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    page = SV.render_kb_html("deck/specs/opening-a-file")

    assert "spec" in page
    assert "deck" in page
    assert "draft" in page


# --- render_markdown escapes raw HTML (security property for WKWebView) --------


def test_render_markdown_escapes_raw_html(SV):
    """render_markdown escapes raw HTML rather than passing it through live.

    Deck loads this output into a WKWebView. If render_markdown ever stopped
    escaping — for example, a future Markdown engine swap — a session message
    or viewed file containing an <img onerror=...> beacon, an <iframe>, or a
    javascript: anchor could reach the WebKit renderer as live markup. This test
    pins the guarantee so that change fails loudly rather than silently in a
    production pane.

    The four payloads cover the classes of harm: script execution, event-handler
    beacons, embedded frames, and javascript: navigation.
    """
    cases = [
        ("<script>alert(1)</script>",                   "<script>", "&lt;script&gt;"),
        ("<img src=x onerror=alert(1)>",                "<img",     "&lt;img"),
        ("<iframe src=https://evil.example></iframe>",  "<iframe",  "&lt;iframe"),
        ("<a href='javascript:alert(1)'>click</a>",     "<a href",  "&lt;a"),
    ]
    for source, live, escaped in cases:
        out = SV.render_markdown(source)
        assert live not in out,    f"live tag survived render_markdown: {live!r}"
        assert escaped in out,     f"escaped form absent from output: {escaped!r}"


def test_render_markdown_escapes_html_inside_fenced_blocks(SV):
    """Fenced code content goes through its own html.escape(raw) call (session_view.py,
    the in_fence branch), separate from the prose-path escape applied to non-fence lines.
    Both paths are pinned here so a future refactor cannot regress either without a
    failing test — the two escapes look redundant but guard distinct code paths.

    Cases: <script> tag inside a fence, <img onerror=> inside a fence, the classic
    </code></pre>-breakout payload that would close the code block and inject arbitrary
    HTML in a naive renderer, and an unterminated fence at EOF (verifying the parser
    doesn't flush raw input when the closing fence is missing).
    """
    # <script> inside a fenced block
    out = SV.render_markdown("```\n<script>alert(1)</script>\n```")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out

    # <img onerror=> inside a fenced block
    out = SV.render_markdown("```\n<img src=x onerror=alert(1)>\n```")
    assert "<img" not in out
    assert "&lt;img" in out

    # </code></pre> breakout attempt: attacker tries to close the <pre><code> wrapper
    # and inject a sibling <script>
    breakout = "</code></pre><script>alert(1)</script><pre><code>"
    out = SV.render_markdown(f"```\n{breakout}\n```")
    assert "<script>" not in out
    assert "</code></pre><script>" not in out

    # Unterminated fence at EOF: lines after the opening ``` must still be escaped
    out = SV.render_markdown("```\n<script>alert(2)</script>")
    assert "<script>" not in out


# --- wikilink rendering (issue_01KZ5C5ED4Y2SD59GHCAF6NYYB) ---------------------
#
# [[target]] in prose must become a deck-open:?origin=kb anchor when the target
# resolves to a KB document.  Code spans and fenced blocks must be immune.


def _make_wikilink_kb(tmp_path: Path) -> Path:
    """KB index with documents for wikilink tests."""
    import sqlite3
    db = tmp_path / "wikilinks.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE docs "
        "(id TEXT PRIMARY KEY, title TEXT, kind TEXT, project TEXT, status TEXT, body TEXT)"
    )
    conn.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?)", [
        ("agent-platform/contracts/claude-hook-payloads", "Hook Payloads", "spec",
         "agent-platform", "active", ""),
        # slug 'deck-overview' resolves unambiguously to this id
        ("deck/overview/deck-overview", "Deck Overview", "spec", "deck", "active", ""),
        # two docs with the same basename — ambiguous slug
        ("project-a/post-merge", "Post Merge A", "runbook", "project-a", "active", ""),
        ("project-b/post-merge", "Post Merge B", "runbook", "project-b", "active", ""),
    ])
    conn.commit()
    conn.close()
    return db


def test_a_full_id_wikilink_becomes_a_deck_open_anchor(SV, monkeypatch, tmp_path):
    """[[agent-platform/contracts/claude-hook-payloads]] → deck-open:?origin=kb anchor."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    doc_id = "agent-platform/contracts/claude-hook-payloads"
    out = SV.render_markdown(f"See [[{doc_id}]] for details.")
    assert "<a href=" in out
    assert "deck-open:?origin=kb" in out
    assert urllib.parse.quote(doc_id, safe="") in out


def test_a_bare_slug_wikilink_resolves_and_becomes_an_anchor(SV, monkeypatch, tmp_path):
    """[[deck-overview]] resolves to id deck/overview/deck-overview via slug lookup."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    out = SV.render_markdown("See [[deck-overview]] here.")
    assert "deck-open:?origin=kb" in out
    assert urllib.parse.quote("deck/overview/deck-overview", safe="") in out


def test_an_unresolvable_wikilink_stays_literal_text(SV, monkeypatch, tmp_path):
    """A target that matches no KB document stays [[target]] in the rendered HTML."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    out = SV.render_markdown("See [[no-such-doc]] for details.")
    assert "[[no-such-doc]]" in out
    assert "deck-open:" not in out


def test_an_ambiguous_slug_stays_literal_text(SV, monkeypatch, tmp_path):
    """post-merge exists in two projects — ambiguous slug stays literal."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    out = SV.render_markdown("See [[post-merge]] for details.")
    assert "[[post-merge]]" in out
    assert "deck-open:" not in out


def test_bash_test_syntax_inside_fenced_block_is_untouched(SV):
    """[[ -n "$VAR" ]] in a fenced block is verbatim code — must not become an anchor.

    The corpus has 9 such occurrences, all in runbooks. Linkifying them would
    corrupt operational documentation agents follow step by step.
    """
    out = SV.render_markdown('Check:\n```\n[[ -n "$_DEPLOYED_SHA" ]]\n```')
    assert "<a " not in out
    assert "[[" in out    # brackets preserved


def test_wikilink_inside_inline_code_span_is_untouched(SV):
    """`[[:space:]]` in a code span stays plain text — must not be resolved as a wikilink.

    POSIX character classes look identical to wikilinks by shape.
    """
    out = SV.render_markdown("Use `[[:space:]]` in your regex.")
    assert "<a " not in out
    assert "[[:space:]]" in out


def test_ordinary_markdown_link_is_unaffected_by_wikilink_handling(SV, monkeypatch, tmp_path):
    """[text](url) still becomes an anchor; wikilink processing must not break it."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    out = SV.render_markdown("A [real link](https://x.com) and [[deck-overview]].")
    assert "<a href='https://x.com'>real link</a>" in out
    assert "deck-open:?origin=kb" in out


def test_wikilink_anchor_encodes_slashes_and_escapes_query_ampersand(SV, monkeypatch, tmp_path):
    """The KB id goes into the href as a URL-encoded ref; the & is HTML-escaped."""
    monkeypatch.setattr(SV, "KB_INDEX_PATH", _make_wikilink_kb(tmp_path))
    doc_id = "agent-platform/contracts/claude-hook-payloads"
    out = SV.render_markdown(f"[[{doc_id}]]")
    assert urllib.parse.quote(doc_id, safe="") in out    # slashes encoded
    assert "&amp;ref=" in out                            # query & HTML-escaped


@needs_renderer
def test_wikilink_in_a_kb_doc_body_renders_as_an_anchor(SV, monkeypatch, tmp_path):
    """render_kb_html (--render-kb) resolves wikilinks in the doc body."""
    import sqlite3
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE docs "
        "(id TEXT PRIMARY KEY, title TEXT, kind TEXT, project TEXT, status TEXT, body TEXT)"
    )
    conn.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?)", [
        ("deck/specs/session-pane", "Session Pane", "spec", "deck", "active",
         "See [[deck-overview]] for background."),
        ("deck/overview/deck-overview", "Deck Overview", "spec", "deck", "active", ""),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)

    page = SV.render_kb_html("deck/specs/session-pane")
    assert page is not None
    assert "deck-open:?origin=kb" in page
    assert urllib.parse.quote("deck/overview/deck-overview", safe="") in page


# --- JS renderer (md-serial bridge) tests ------------------------------------
#
# These tests exercise render_file_html and render_kb_html after they moved to
# the JavaScript renderer (md-serial + render.js via the Node bridge).
# They require Node.js and the knowledge repo to be available, which is true in
# this project's development environment.


@needs_renderer
def test_js_renderer_wrapped_list_item_is_one_li(SV, tmp_path):
    """THE LIVE DEFECT — render_markdown tore a wrapped list item into a one-item
    list plus an orphan paragraph outside it. md-serial parses the continuation
    correctly so the item and its continuation land in ONE <li>.
    """
    f = tmp_path / "doc.md"
    # The continuation line is indented 2 spaces — valid CommonMark list continuation.
    f.write_text(
        "- First line of a long list item\n"
        "  that wraps to the next source line\n"
        "- Second item\n"
    )
    page = SV.render_file_html(f)
    # Two items, two <li> tags — not three (old defect produced the first item split).
    assert page.count("<li>") == 2
    # No orphan paragraph carrying the continuation text outside the list.
    assert "<p>that wraps" not in page


@needs_renderer
def test_js_renderer_frontmatter_section(SV, tmp_path):
    """A file WITH frontmatter renders it as <section class="md-frontmatter">, and
    the raw YAML does NOT appear as body prose.

    This was the second visible defect: render_file_html passed the raw source
    (including the --- delimiters and field lines) to render_markdown, which turned
    them into a run-on paragraph with 'audience:' and 'authors:' as bullets.
    """
    f = tmp_path / "doc.md"
    f.write_text(
        "---\n"
        "id: test/doc\n"
        "title: My Doc\n"
        "audience: [team]\n"
        "---\n"
        "# Body heading\n\n"
        "Body text.\n"
    )
    page = SV.render_file_html(f)
    # Frontmatter renders as a typed header section.
    assert '<section class="md-frontmatter">' in page
    # The raw YAML must not appear as body prose.
    assert "<p>id: test/doc</p>" not in page
    assert "<p>title: My Doc</p>" not in page
    # The body content still renders.
    assert "<h1>Body heading</h1>" in page
    assert "<p>Body text.</p>" in page


@needs_renderer
def test_js_renderer_ordered_list_emits_ol(SV, tmp_path):
    """An ordered list in the source renders as <ol>, not <ul>.

    render_markdown always emitted <ul> regardless of the marker, because it
    only checked for the bullet regex ([-*+]) and handled ordered markers in the
    same branch. md-serial distinguishes them at parse time.
    """
    f = tmp_path / "doc.md"
    f.write_text("1. First\n2. Second\n3. Third\n")
    page = SV.render_file_html(f)
    assert "<ol" in page
    assert page.count("<li>") == 3


@needs_renderer
def test_js_renderer_nested_list(SV, tmp_path):
    """Nested lists render with the inner list inside the outer <li>, not flattened."""
    f = tmp_path / "doc.md"
    f.write_text(
        "- Item A\n"
        "  - Nested 1\n"
        "  - Nested 2\n"
        "- Item B\n"
    )
    page = SV.render_file_html(f)
    # At least one <ul> at the outer level and one for the nested list.
    assert page.count("<ul>") >= 2
    # Four list items in total (A, Nested 1, Nested 2, B).
    assert page.count("<li>") >= 4


@needs_renderer
def test_render_file_html_resolvable_wikilink_emits_deck_open_href(
        SV, monkeypatch, tmp_path):
    """A resolvable [[wikilink]] emits href="deck-open:?origin=kb&ref=<encoded-id>".

    The RESOLVED doc id (not the name in the source) must be in the href — that
    is the value Deck's WKNavigationDelegate keys off to open the correct document.
    """
    db = _make_wikilink_kb(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)
    f = tmp_path / "doc.md"
    f.write_text("See [[deck-overview]] for details.")
    page = SV.render_file_html(f)
    doc_id = "deck/overview/deck-overview"
    assert "deck-open:?origin=kb" in page
    # href must carry the RESOLVED doc id, percent-encoded.
    assert urllib.parse.quote(doc_id, safe="") in page
    # The raw [[...]] syntax must not appear in the rendered output.
    assert "[[deck-overview]]" not in page


@needs_renderer
def test_render_file_html_unresolvable_wikilink_stays_literal(
        SV, monkeypatch, tmp_path):
    """An unresolvable [[wikilink]] renders as literal [[name]] text with no anchor.

    Absent from the wikilink map → the renderer falls back to literal text.
    A caller that forgets to resolve a target gets visible-but-unclickable text
    rather than a broken link.
    """
    db = _make_wikilink_kb(tmp_path)
    monkeypatch.setattr(SV, "KB_INDEX_PATH", db)
    f = tmp_path / "doc.md"
    f.write_text("See [[no-such-doc]] here.")
    page = SV.render_file_html(f)
    assert "[[no-such-doc]]" in page
    assert "deck-open:?origin=kb" not in page


def test_node_missing_produces_named_error(SV, monkeypatch, tmp_path):
    """A missing Node.js surfaces as NodeMissingError — not a blank page and not
    a silent fallback to render_markdown. A silent fallback would reinstate the
    rendering divergence this bridge exists to remove, invisibly.
    """
    f = tmp_path / "doc.md"
    f.write_text("# Hello")

    def no_node():
        raise SV.NodeMissingError("Node.js not found")

    monkeypatch.setattr(SV, "_find_node", no_node)
    with pytest.raises(SV.NodeMissingError):
        SV.render_file_html(f)


def test_node_candidates_finds_an_opt_package_via_glob(SV, monkeypatch):
    """The Homebrew Apple Silicon install is found without the module naming
    "homebrew": a glob over /opt/<pkg>/bin/node, which also matches any other
    /opt-installed package manager (platform/decisions/0090)."""
    class _FakeOpt:
        def glob(self, pattern):
            assert pattern == "*/bin/node"
            return [Path("/opt/homebrew/bin/node")]
    monkeypatch.setattr(SV, "Path", lambda p: _FakeOpt() if p == "/opt" else Path(p))
    assert SV._node_candidates() == [
        "/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node",
    ]


def test_node_candidates_with_no_opt_packages(SV, monkeypatch):
    """A machine with nothing under /opt (or no /opt at all, e.g. Linux) still
    offers the two fixed POSIX locations — no exception, no crash."""
    class _FakeOpt:
        def glob(self, pattern):
            return []
    monkeypatch.setattr(SV, "Path", lambda p: _FakeOpt() if p == "/opt" else Path(p))
    assert SV._node_candidates() == ["/usr/local/bin/node", "/usr/bin/node"]


def test_find_node_prefers_the_env_override(SV, monkeypatch, tmp_path):
    fake = tmp_path / "node"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setenv(SV._NODE_ENV_VAR, str(fake))
    monkeypatch.setattr(SV, "_node_candidates", lambda: ["/should/not/be/used"])
    assert SV._find_node() == str(fake)


def test_find_node_falls_back_to_candidates_without_override(SV, monkeypatch, tmp_path):
    fake = tmp_path / "node"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.delenv(SV._NODE_ENV_VAR, raising=False)
    monkeypatch.setattr(SV, "_node_candidates", lambda: [str(fake)])
    assert SV._find_node() == str(fake)


def test_find_node_raises_when_no_candidate_exists(SV, monkeypatch):
    monkeypatch.delenv(SV._NODE_ENV_VAR, raising=False)
    monkeypatch.setattr(SV, "_node_candidates", lambda: ["/nonexistent-node-xyz"])
    with pytest.raises(SV.NodeMissingError):
        SV._find_node()


def test_bridge_timeout_becomes_runtime_error(SV, monkeypatch, tmp_path):
    """A runaway Node must reach the CLI as RuntimeError, which is what both
    _render_file and _render_kb catch.

    subprocess.TimeoutExpired derives from SubprocessError, NOT from RuntimeError
    or OSError, so an uncaught one sails past both handlers and lands in Deck's
    viewer as a Python traceback — the same surface the NodeMissingError catch
    was added to protect.
    """
    import subprocess

    f = tmp_path / "doc.md"
    f.write_text("# Hello")

    # Stub BOTH prerequisites so this runs anywhere: the timeout re-raise is
    # logic this repo owns and should not need the knowledge repo or a node
    # binary to be tested. Stubbing only the git read left a hidden node
    # dependency — _find_node runs first, and on a machine without node it
    # raises NodeMissingError, which IS a RuntimeError, so pytest.raises caught
    # it and the test failed on the `match` instead. The fake path is never
    # executed because subprocess.run is patched below.
    monkeypatch.setattr(SV, "_find_node", lambda: "/fake/node")
    monkeypatch.setattr(SV, "_read_js_module", lambda path: ("// stub", "0" * 40))

    # Only the bridge invocation may time out. `check_output` in _read_js_module
    # calls subprocess.run internally, so an unconditional fake would fire on the
    # git reads first — which is why they are stubbed above rather than matched
    # around here.
    def timed_out(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=SV._BRIDGE_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(RuntimeError, match="timed out"):
        SV.render_file_html(f)


def test_wikilink_map_queries_each_distinct_target_once(SV, monkeypatch):
    """Repetition must not multiply index queries.

    Only successful resolutions used to be remembered, so the same unresolvable
    target a thousand times cost a thousand queries — 245ms for 2000 repeats of
    one broken link, all of it before the bridge and so outside its timeout.
    """
    calls = []
    monkeypatch.setattr(SV, "_resolve_wikilink_target", lambda name: calls.append(name) or None)

    SV._wikilink_map("[[missing-doc]]\n" * 500 + "[[other-missing]]\n" * 500)

    assert calls == ["missing-doc", "other-missing"], (
        f"expected one query per distinct target, got {len(calls)}"
    )


def test_wikilink_map_caps_distinct_targets(SV, monkeypatch):
    """A hostile document cannot spend unbounded time in the resolve phase.

    This phase runs BEFORE the bridge, so _BRIDGE_TIMEOUT_S does not bound it:
    100k distinct targets measured 11.3s unbounded. Past the cap, targets are
    simply absent from the map and render as literal [[name]] text — the same
    thing an unresolvable target already does, so no new failure mode.
    """
    calls = []
    monkeypatch.setattr(SV, "_resolve_wikilink_target", lambda name: calls.append(name) or None)

    over = SV._WIKILINK_RESOLVE_CAP + 250
    SV._wikilink_map("".join(f"[[target-{i}]]\n" for i in range(over)))

    assert len(calls) == SV._WIKILINK_RESOLVE_CAP, (
        f"expected the scan to stop at {SV._WIKILINK_RESOLVE_CAP}, got {len(calls)}"
    )


def test_render_markdown_unchanged_for_session_transcript(SV):
    """render_markdown serves the session transcript — it must not have moved to
    the JavaScript renderer. The session view was written for and sized to this
    function; the JS renderer switch covers only --render-file and --render-kb.
    """
    out = SV.render_markdown("**bold** and `code` and *em*")
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out
    assert "<em>em</em>" in out
    # render_markdown's heading level offset (+2) must still be intact —
    # that offset is deliberate for the session view, and h1 in session prose
    # must not compete with the page's own h1. The JS renderer does NOT apply
    # this offset; this test pins the boundary.
    out2 = SV.render_markdown("# Heading")
    assert "<h3>Heading</h3>" in out2


# --- render_editor_html -------------------------------------------------------

def test_escape_for_script_no_lt_on_close_tag(SV):
    """A document containing </script> must produce a literal with no <.

    This is the primary injection vector: a document whose content closes the
    inline script block from inside the JSON string literal.
    """
    result = SV._escape_for_script("hello</script><script>alert(1)</script>")
    assert '<' not in result, f"< found in result: {result!r}"


def test_escape_for_script_no_lt_on_u2028(SV):
    """U+2028 (LINE SEPARATOR) must be absent from the literal.

    json.dumps with ensure_ascii=True (the default) escapes it; this test pins
    that ensure_ascii is not disabled.  U+2028 is a line terminator in JS and
    would break a string literal if left raw.
    """
    result = SV._escape_for_script("line separator")
    assert '<' not in result
    assert ' ' not in result, "U+2028 was not escaped by json.dumps"


def test_escape_for_script_no_lt_in_dict_keys(SV):
    """Wikilink map keys are document-derived and can contain <.

    The same treatment applied to the source string must apply to the whole
    JSON encoding of the map, including keys.
    """
    wm = {"<dangerous>": "deck/specs/thing", "normal": "deck/other/doc"}
    result = SV._escape_for_script(wm)
    assert '<' not in result, f"< found in dict result: {result!r}"


def test_flatten_js_modules_strips_export_and_import(SV):
    """The flattener drops import lines and strips the export keyword."""
    mod_a = "export function foo() { return 1; }\n"
    mod_b = "import { foo } from './a.js';\nexport function bar() { return foo(); }\n"
    result = SV._flatten_js_modules([mod_a, mod_b])
    assert 'function foo' in result
    assert 'function bar' in result
    for line in result.splitlines():
        assert not line.startswith('export '), f"export survived: {line!r}"
        assert not line.startswith('import '), f"import survived: {line!r}"


def test_flatten_js_modules_raises_on_duplicate_name(SV):
    """A duplicate top-level declaration must raise BundleError.

    Emitting a broken page that silently shadows an earlier definition is worse
    than failing at build time.
    """
    mod_a = "function helper() { return 1; }\n"
    mod_b = "function helper() { return 2; }\n"
    with pytest.raises(SV.BundleError, match="duplicate"):
        SV._flatten_js_modules([mod_a, mod_b])


def test_flatten_js_modules_raises_on_script_close_tag_in_comment(SV):
    """A module with '</script' in a JS comment must raise BundleError.

    The comment '// handle the </script> edge case' passes both the import/export
    guard and the duplicate-name guard, but plants a close tag inside the inline
    script block.  Guard 3 catches it at page-build time rather than letting it
    reach the page as a broken render and a confidentiality leak (the document
    source after the prematurely closed block becomes visible page text).
    """
    mod = "function foo() { return 1; }\n// handle the </script> edge case\n"
    with pytest.raises(SV.BundleError, match="script"):
        SV._flatten_js_modules([mod])


def test_flatten_js_modules_raises_on_export_no_space(SV):
    """'export{a, b}' (no space) must raise BundleError, not pass through silently.

    The strip pass only handles 'export ' (with a space); 'export{' survives
    unstripped and, without a word-boundary guard, was emitted verbatim — a syntax
    error in a non-module script context and a case the guard existed to catch.
    """
    mod = "export{a, b};\n"
    with pytest.raises(SV.BundleError, match="export"):
        SV._flatten_js_modules([mod])


def test_flatten_js_modules_raises_on_import_no_space(SV):
    """'import{foo}from...' (no space) must raise BundleError, not pass through silently.

    Same word-boundary gap as the export case: 'import{' has no trailing space so
    the strip-pass drop and the old guard both miss it.  The surviving import is a
    syntax error in any non-module context.
    """
    mod = "import{foo}from'./bar.js';\nfunction baz() {}\n"
    with pytest.raises(SV.BundleError, match="import"):
        SV._flatten_js_modules([mod])


def test_flatten_js_modules_refuses_every_export_default_form(SV):
    """A default export must raise, in all three of its forms.

    Stripping 'export default ' rescues only the NAMED function form. The
    anonymous form leaves 'function() {...}', an anonymous function declaration,
    which is a SyntaxError at statement position; the expression form leaves a
    bare '42' that silently drops the export. Two forms out of three therefore
    reach the page broken, which is the outcome this function exists to prevent.

    Flattening puts every module in one scope, where a default export has no
    meaning to preserve, so refusing is both correct and the only behaviour that
    holds for all three forms.
    """
    for mod in (
        "export default function foo() { return 1; }\n",
        "export default function() { return 1; }\n",
        "export default 42\n",
        # No space after 'default', and a tab instead of a space. A
        # space-suffixed test misses both, and they then fall through to the
        # 'export ' strip and emit 'default{...}' — the broken output this
        # branch exists to refuse. Same word-boundary gap the strip and Guard 1
        # close for named imports and exports.
        "export default{a}\nconst a = 1;\n",
        "export default\tfoo\nconst foo = 1;\n",
    ):
        with pytest.raises(SV.BundleError, match="default export"):
            SV._flatten_js_modules([mod])


def test_flatten_js_modules_does_not_mistake_a_named_default_binding(SV):
    """'export const defaultThing' is an ordinary named export, not a default one.

    The word-boundary match must not over-reach: 'default' followed by another
    word character is part of an identifier, so this strips like any named
    export rather than raising.
    """
    result = SV._flatten_js_modules(["export const defaultThing = 1\n"])
    assert "const defaultThing = 1" in result, f"unexpected: {result!r}"


def test_flatten_js_modules_keeps_named_exports(SV):
    """A named export still strips to a plain declaration and passes every guard."""
    result = SV._flatten_js_modules(["export function ok() { return 1; }\n"])
    assert "function ok" in result, f"'function ok' not in result: {result!r}"
    for line in result.splitlines():
        assert not SV._SURVIVING_MODULE_KW_RE.match(line), (
            f"export/import survived in: {line!r}"
        )


def test_render_editor_html_page_has_one_close_script_tag(SV, monkeypatch, tmp_path):
    """A document containing </script> must not add a second closing tag to the page.

    The editor page carries the document inside a <script type=module> block.
    Without escaping, a </script> in the content closes that block prematurely
    and the rest of the page becomes visible text.
    """
    f = tmp_path / "dangerous.md"
    f.write_text("# Title\n\nContent with </script><script>bad</script> in it.\n")

    monkeypatch.setattr(SV, '_read_js_module', lambda path_in_repo: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_editor_html(f)

    # Only the one true </script> closing the inline module should appear.
    assert page.count('</script>') == 1, (
        f"expected exactly 1 </script> in the page, found {page.count('</script>')}"
    )


# --- render_editor_page: the content entry point ------------------------------

def test_render_editor_page_and_html_agree_for_the_same_bytes(SV, monkeypatch, tmp_path):
    """The file entry point is the content one with a read in front of it.

    render_editor_html now delegates, so this is the property that says the
    refactor changed no behaviour for the path callers that existed before it.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    body = "---\nid: a/b/c\n---\n\n# Title\n\nA paragraph.\n"
    f = tmp_path / "doc.md"
    f.write_text(body)

    assert SV.render_editor_html(f) == SV.render_editor_page(body, name="doc.md")


def test_render_editor_page_names_the_document_in_title_and_heading(SV, monkeypatch):
    """A KB document has no filename, so the caller supplies the name.

    Its doc id goes here, which is why the name is a parameter rather than
    derived from a path that does not exist.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_editor_page("# Hi\n", name="deck/specs/session-pane")

    assert "<title>deck/specs/session-pane</title>" in page
    assert "<h1>deck/specs/session-pane</h1>" in page


def test_render_editor_page_escapes_a_name_that_carries_markup(SV, monkeypatch):
    """The name reaches two HTML contexts, and a doc id is caller-supplied."""
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_editor_page("# Hi\n", name="<script>alert(1)</script>")

    assert "<script>alert(1)</script>" not in page
    # Still exactly one closing tag: the inline module's own.
    assert page.count("</script>") == 1


def test_render_editor_page_inlines_the_source_it_was_given(SV, monkeypatch):
    """The bytes in the page are the bytes the caller holds the sha for.

    Deck checks a KB document out, gets content and a content-sha, and submits
    against that sha. If the page edited anything other than these exact bytes,
    the sha would be guarding a different document than the one on screen.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    body = "---\nid: a/b/c\nstatus: draft\n---\n\n# T\n\ntext with a \\ and a \" in it\n"
    page = SV.render_editor_page(body, name="a/b/c")

    literal = page.split("const SOURCE = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(literal) == body


def test_render_editor_page_binds_the_whole_editor_object(SV, monkeypatch):
    """mountEditor's return is kept whole, not destructured to `save` alone.

    `saveChecked` has to be reachable from the click handler; destructuring
    `{ save }` the way the page used to would silently fall back to the old
    unchecked save on every editor.js, checked or not.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})

    page = SV.render_editor_page("# Hi\n", name="a/b/c")

    assert "const editor = mountEditor(" in page
    assert "const { save } = mountEditor(" not in page


# --- the save-click verdict handler --------------------------------------------
#
# Deck refuses to sign whenever the posted roundTrip.ok is not true, so a
# missing or malformed verdict must never be posted as a pass. Each case below
# runs the actual handler under Node against a stub editor/document/window and
# inspects what it posted, rather than pattern-matching the generator's output.

@needs_node
def test_save_click_posts_ok_true_for_a_passing_round_trip(SV, tmp_path):
    editor_js = (
        "{ saveChecked: function() { return { source: 'S', "
        "roundTrip: { ok: true, failures: [] } }; } }"
    )
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert posted == [
        {"action": "save", "source": "S", "roundTrip": {"ok": True, "failures": []}},
    ]


@needs_node
def test_save_click_posts_ok_false_with_failures(SV, tmp_path):
    editor_js = (
        "{ saveChecked: function() { return { source: 'S', roundTrip: "
        "{ ok: false, failures: ['dropped a code span'] } }; } }"
    )
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert posted == [{
        "action": "save", "source": "S",
        "roundTrip": {"ok": False, "failures": ["dropped a code span"]},
    }]


@needs_node
def test_save_click_falls_back_to_save_when_save_checked_absent(SV, tmp_path):
    """A pre-#670 editor.js has no saveChecked; the page must still post,
    and must post ok: false rather than let a missing check read as a pass.
    """
    editor_js = "{ save: function() { return 'PLAIN SOURCE'; } }"
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert posted == [{
        "action": "save", "source": "PLAIN SOURCE",
        "roundTrip": {"ok": False, "failures": ["round-trip check did not run"]},
    }]


@needs_node
def test_save_click_posts_ok_false_when_save_checked_throws(SV, tmp_path):
    editor_js = "{ saveChecked: function() { throw new Error('boom'); } }"
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert len(posted) == 1
    assert posted[0]["action"] == "save"
    assert posted[0]["source"] == ""
    assert posted[0]["roundTrip"]["ok"] is False
    assert posted[0]["roundTrip"]["failures"] == ["boom"]


@needs_node
def test_save_click_posts_ok_false_when_save_throws(SV, tmp_path):
    """The fallback path (no saveChecked) must not post an unanswered click
    either, when the plain save() itself throws.
    """
    editor_js = "{ save: function() { throw new Error('save boom'); } }"
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert len(posted) == 1
    assert posted[0]["source"] == ""
    assert posted[0]["roundTrip"]["ok"] is False
    assert posted[0]["roundTrip"]["failures"] == ["save boom"]


@needs_node
def test_save_click_posts_ok_false_when_round_trip_is_missing(SV, tmp_path):
    """saveChecked() succeeding but returning no roundTrip at all must not
    read as a pass — the source it did produce is still posted.
    """
    editor_js = "{ saveChecked: function() { return { source: 'S' }; } }"
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert len(posted) == 1
    assert posted[0]["source"] == "S"
    assert posted[0]["roundTrip"]["ok"] is False
    assert posted[0]["roundTrip"]["failures"] != []


@needs_node
def test_save_click_posts_ok_false_for_a_truthy_non_true_ok(SV, tmp_path):
    """roundTrip.ok === 1 is not roundTrip.ok === true.

    A loose truthy check would post a pass for any non-zero, non-empty value;
    only the literal boolean true may sign as a pass.
    """
    editor_js = (
        "{ saveChecked: function() { return { source: 'S', "
        "roundTrip: { ok: 1, failures: [] } }; } }"
    )
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert posted[0]["roundTrip"]["ok"] is False


@needs_node
def test_save_click_posts_ok_false_for_a_non_string_source(SV, tmp_path):
    """A passing roundTrip must not survive a source that isn't a string.

    Deck signs and commits `source` verbatim whenever `roundTrip.ok === true`;
    a `saveChecked()` bug that returns a non-string `source` alongside a
    genuine `ok: true` must not post that combination, or Deck signs an empty
    document as a real edit.
    """
    editor_js = (
        "{ saveChecked: function() { return { source: undefined, "
        "roundTrip: { ok: true, failures: [] } }; } }"
    )
    posted = _run_save_click(SV, editor_js, tmp_path)
    assert posted == [{
        "action": "save", "source": "",
        "roundTrip": {
            "ok": False,
            "failures": ["saveChecked() returned a non-string source"],
        },
    }]


@needs_node
def test_save_click_posts_ok_false_for_a_polluted_prototype_ok(SV, tmp_path):
    """normaliseRoundTrip must read `ok` as roundTrip's own property.

    resolveWikilink, two functions above, already guards the WIKILINKS lookup
    with `hasOwnProperty` rather than a bare property read. normaliseRoundTrip
    needs the same guard: a polluted `Object.prototype.ok` must not stand in
    for a verdict `saveChecked()` never computed — an empty `{}` should read
    as no verdict at all.
    """
    editor_js = (
        "{ saveChecked: function() { return { source: 'S', roundTrip: {} }; } }"
    )
    posted = _run_save_click(
        SV, editor_js, tmp_path, prelude="Object.prototype.ok = true;",
    )
    assert posted[0]["roundTrip"]["ok"] is False


@needs_node
def test_save_click_posts_ok_false_for_a_polluted_prototype_source_and_round_trip(
    SV, tmp_path,
):
    """The click handler must read `source` and `roundTrip` off saveChecked()'s
    result as own properties too, not just `ok`/`failures` off `roundTrip`.

    A `saveChecked()` that returns `{}` has produced neither a source nor a
    verdict. A polluted `Object.prototype.source` (a string) and
    `Object.prototype.roundTrip` ({ ok: true, failures: [] }) must not stand
    in for either — `ownProp` must read both as own properties, matching
    normaliseRoundTrip's own guard on `ok`/`failures` one call further in.
    """
    editor_js = "{ saveChecked: function() { return {}; } }"
    posted = _run_save_click(
        SV, editor_js, tmp_path,
        prelude=(
            "Object.prototype.source = 'POLLUTED';"
            "Object.prototype.roundTrip = { ok: true, failures: [] };"
        ),
    )
    assert posted == [{
        "action": "save", "source": "",
        "roundTrip": {
            "ok": False,
            "failures": ["saveChecked() returned a non-string source"],
        },
    }]


@needs_node
def test_save_click_posts_ok_false_for_a_polluted_prototype_round_trip_alone(
    SV, tmp_path,
):
    """ownProp must read `roundTrip` off saveChecked()'s result as its own
    property, isolated from the `source` check one call further out.

    test_save_click_posts_ok_false_for_a_polluted_prototype_source_and_round_trip
    pollutes `source` too, so postSaveResult's non-string-source guard forces
    `ok: false` there regardless of what `roundTrip` read — reverting only the
    `roundTrip` half of that fix (back to `result && result.roundTrip`) still
    passes it. Here `source` is a real own string, so `ok: false` can only
    come from normaliseRoundTrip taking the "no round-trip verdict" path
    against the own (missing) `roundTrip`, not the polluted
    `Object.prototype.roundTrip = { ok: true, failures: [] }`.
    """
    editor_js = "{ saveChecked: function() { return { source: 'S' }; } }"
    posted = _run_save_click(
        SV, editor_js, tmp_path,
        prelude="Object.prototype.roundTrip = { ok: true, failures: [] };",
    )
    assert posted == [{
        "action": "save", "source": "S",
        "roundTrip": {
            "ok": False,
            "failures": ["saveChecked() returned no round-trip verdict"],
        },
    }]


@needs_node
def test_save_click_with_no_webkit_handler_shows_unavailable_and_posts_nothing(
    SV, tmp_path,
):
    """The click-time missing-handler guard must fire on its own.

    The page-load-time check and this click-time check are two separate reads
    of `window.webkit.messageHandlers.deckEditor`; only the page-load one had
    a test.
    """
    result = _run_save_click_raw(
        SV, "{ save: function() { return 'S'; } }", tmp_path, has_webkit=False,
    )
    assert result["posted"] == []
    assert result["saveUnavailableDisplay"] == "block"


# --- the --render-editor CLI branch -------------------------------------------

def test_render_editor_stdin_survives_invalid_utf8(session_view_cli, SV,
                                                   monkeypatch, capsys):
    """A malformed byte on stdin degrades; it does not raise.

    `sys.stdin.read()` decodes strictly, and UnicodeDecodeError is a ValueError
    rather than a RuntimeError — so the handler around the render call would not
    catch it and a traceback would reach stderr. Deck puts stderr into the
    viewer's error page, which means the reader sees the traceback along with
    the interpreter and script paths. Both reviewers on #369 found this
    independently.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    class _Stdin:
        buffer = io.BytesIO(b"# Title\n\nA \xff\xfe paragraph.\n")

    monkeypatch.setattr(session_view_cli.sys, 'stdin', _Stdin())

    assert session_view_cli._render_editor("-", None, "a/b/c") == 0

    out = capsys.readouterr().out
    assert "<h1>a/b/c</h1>" in out
    # The undecodable bytes became replacement characters rather than an
    # exception; the rest of the document is intact either way.
    assert "paragraph" in out


def test_render_editor_says_so_when_name_cannot_apply(session_view_cli, SV,
                                                      monkeypatch, capsys,
                                                      tmp_path):
    """`--name` beside a path is a no-op, and silence would hide that.

    The filename wins for a path argument. A caller who passed --name and got no
    complaint would reasonably believe the heading had been renamed.
    """
    monkeypatch.setattr(SV, '_read_js_module', lambda p: ("// stub\n", "a" * 40))
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    f = tmp_path / "real.md"
    f.write_text("# T\n")

    assert session_view_cli._render_editor(str(f), None, "ignored/doc/id") == 0

    captured = capsys.readouterr()
    assert "--name is ignored with a path argument" in captured.err
    assert "<h1>real.md</h1>" in captured.out
    assert "ignored/doc/id" not in captured.out


# --- the --render-file CLI branch ----------------------------------------------

def test_render_file_stdin_renders_content(session_view_cli, SV, monkeypatch, capsys):
    """`--render-file -` renders the document piped in on stdin, the way
    `--render-editor -` already does for a KB document with no path of its own.
    """
    monkeypatch.setattr(SV, '_render_via_bridge', lambda source, wikilinks: f"<p>{source}</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    class _Stdin:
        buffer = io.BytesIO(b"a paragraph\n")

    monkeypatch.setattr(session_view_cli.sys, 'stdin', _Stdin())

    assert session_view_cli._render_file("-", None, "real.md") == 0

    out = capsys.readouterr().out
    assert "<h1>real.md</h1>" in out
    assert "a paragraph" in out


def test_render_file_stdin_reports_an_oserror_without_a_traceback(
        session_view_cli, SV, monkeypatch, capsys):
    """An OSError from inside the render reaches stderr as one line, not a
    traceback.

    The bridge writes its own JS modules to a temp directory and execs node, so
    a disk-full write, a permissions failure, or a race where `_find_node`'s
    stat passes and the binary is gone by exec time all raise OSError from in
    here. Uncaught, that is the same interpreter-and-script-path disclosure the
    lenient decode exists to prevent, arriving by a different exception class —
    and Deck renders stderr into the viewer's error page, so the reader sees it.
    The path branch already catches OSError; this pins that the stdin branch
    agrees with it.
    """
    def _boom(source, wikilinks):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SV, '_render_via_bridge', _boom)
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    class _Stdin:
        buffer = io.BytesIO(b"a paragraph\n")

    monkeypatch.setattr(session_view_cli.sys, 'stdin', _Stdin())

    assert session_view_cli._render_file("-", None, "real.md") == 1

    captured = capsys.readouterr()
    assert "cannot render the document on stdin" in captured.err
    assert "No space left on device" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_render_file_stdin_survives_invalid_utf8(session_view_cli, SV, monkeypatch, capsys):
    """A malformed byte on stdin degrades; it does not raise.

    Mirrors --render-editor -'s reasoning: `sys.stdin.read()` decodes strictly,
    and UnicodeDecodeError is a ValueError rather than a RuntimeError, so the
    handler around the render call would not catch it and a traceback would
    reach stderr — which Deck's viewer pane shows to the reader verbatim.
    """
    monkeypatch.setattr(SV, '_render_via_bridge', lambda source, wikilinks: f"<p>{source}</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    class _Stdin:
        buffer = io.BytesIO(b"a \xff\xfe paragraph\n")

    monkeypatch.setattr(session_view_cli.sys, 'stdin', _Stdin())

    assert session_view_cli._render_file("-", None, "real.md") == 0

    out = capsys.readouterr().out
    assert "<h1>real.md</h1>" in out
    assert "paragraph" in out


def test_render_file_stdin_requires_name(session_view_cli, SV, monkeypatch, capsys):
    """Unlike --render-editor -, --render-file - has no fallback name to fall
    back on: Deck's viewer pane always holds the file's real name before it
    calls this, so a missing --name means the caller forgot to pass it.
    """
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    assert session_view_cli._render_file("-", None, None) == 1

    assert "--render-file - requires --name" in capsys.readouterr().err


def test_render_file_says_so_when_name_cannot_apply(session_view_cli, SV, monkeypatch,
                                                     capsys, tmp_path):
    """`--name` beside a path is a no-op, and silence would hide that."""
    monkeypatch.setattr(SV, '_render_via_bridge', lambda source, wikilinks: "<p>T</p>")
    monkeypatch.setattr(SV, '_wikilink_map', lambda s: {})
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    f = tmp_path / "real.md"
    f.write_text("T\n")

    assert session_view_cli._render_file(str(f), None, "ignored-name") == 0

    captured = capsys.readouterr()
    assert "--name is ignored with a path argument" in captured.err
    assert "<h1>real.md</h1>" in captured.out
    assert "ignored-name" not in captured.out


def test_render_file_relative_path_still_fails(session_view_cli, SV, monkeypatch, capsys):
    """A relative path is refused before any render is attempted — accepting
    '-' on stdin does not loosen the absolute-path requirement for a real path.
    """
    monkeypatch.setattr(session_view_cli, 'SV', SV)

    assert session_view_cli._render_file("relative/doc.md", None, None) == 1

    assert ("--render-file requires an absolute path or '-'"
            in capsys.readouterr().err)


# --- stable row keys: the reader's open rows survive a reload -----------------
#
# Deck reloads the whole document whenever the conversation changes, so an
# expanded `<details>` re-collapses unless something identifies it across the
# reload. These pin that identity. The failure they exist to stop is not "the
# row closed" — it is a key that MOVES, which reopens an unrelated row and lies
# about which call the reader was looking at.

def _line(rec) -> str:
    return json.dumps(rec)


def test_every_row_carries_a_key(SV):
    rows = SV.rows_for([
        _line({"type": "user", "uuid": "u-1", "message": {"content": "do the thing"}}),
        _line(dict(_assistant({"type": "thinking", "thinking": "considering"}), uuid="a-1")),
        "{ not json at all",
    ])
    assert all(r.get("key") for r in rows)


def test_a_key_is_the_record_uuid_and_the_block_index(SV):
    rows = SV.rows_for([_line(dict(_assistant(
        {"type": "text", "text": "found it"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ), uuid="a-1"))])
    assert [r["key"] for r in rows] == ["a-1.0", "a-1.1"]


def test_a_key_does_not_move_when_a_turn_is_appended(SV):
    """THE DEFECT THIS CLOSES. An index-based key would shift here.

    The document is emitted newest-first, so a new turn changes every row's
    position. A key that moved would re-open whichever row now sits where the
    reader's row used to be.
    """
    first = _line(dict(_assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}), uuid="a-1"))
    second = _line({"type": "user", "uuid": "u-2", "message": {"content": "next"}})
    before = {r["key"] for r in SV.rows_for([first])}
    after = {r["key"] for r in SV.rows_for([first, second])}
    assert before <= after


def test_folding_a_result_keeps_the_calls_key(SV):
    """A result folds into its call, and the surviving row keeps the call's key.

    Otherwise the one row a reader most wants open — a tool call with output —
    would change identity the moment its result arrived.
    """
    call = _line(dict(_assistant(
        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
         "input": {"command": "ls"}}), uuid="a-1"))
    result = _line({"type": "user", "uuid": "u-2", "toolUseResult": {"stdout": "x"},
                    "message": {"content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]}})
    rows = SV.rows_for([call, result])
    assert [r["key"] for r in rows] == ["a-1.0"]
    assert rows[0]["result"] == "x"


def test_keys_are_unique_even_when_a_uuid_repeats(SV):
    """Two rows sharing a key would open and close together."""
    dup = _line(dict(_assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}), uuid="same"))
    rows = SV.rows_for([dup, dup])
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == len(keys)


def test_a_row_without_a_uuid_gets_a_content_hash(SV):
    """Stable while the row's content is, which is all a keyless row can offer."""
    rows_a = SV.rows_for(["{ not json"])
    rows_b = SV.rows_for(["{ not json"])
    assert rows_a[0]["key"] == rows_b[0]["key"]
    assert rows_a[0]["key"].startswith("h")


def test_a_hostile_uuid_never_reaches_the_key(SV):
    """`uuid` arrives in a file this repo does not write.

    The key is interpolated into an HTML attribute and, in Deck, into a script
    the host evaluates against the page. So the shape is an allowlist and
    anything outside it falls back to the hash, which is hex by construction.
    """
    rows = SV.rows_for([_line({"type": "user", "uuid": '" onload="alert(1)',
                               "message": {"content": "hi"}})])
    assert rows[0]["key"].startswith("h")
    assert "onload" not in rows[0]["key"]


def test_the_page_carries_each_collapsed_rows_key(SV):
    rows = SV.rows_for([_line(dict(_assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}), uuid="a-1"))])
    out = SV.render_html(rows, "t")
    assert '<details data-k="a-1.0">' in out


def test_a_key_is_escaped_into_the_attribute(SV):
    """Belt and braces beside the charset allowlist: the attribute is escaped too.

    The key is overwritten directly, which is a state `rows_for` cannot produce —
    the allowlist above is what stops it, and `test_a_hostile_uuid_never_reaches_the_key`
    is that half. This one pins the second layer on its own, so a later change
    that widened the charset would still be caught here rather than shipping
    with only one defence and no test noticing.
    """
    rows = SV.rows_for([_line(dict(_assistant(
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}), uuid="a-1"))])
    rows[0]["key"] = 'x" onmouseover="boom'
    out = SV.render_html(rows, "t")
    assert 'onmouseover="boom' not in out
    assert "&quot;" in out


# --- CHANGE 1: data-k on FULL rows -------------------------------------------
#
# render_html previously emitted data-k only on <details> (collapsed rows).
# The append bridge must find and replace FULL rows (human prompts, assistant
# prose) by key too, so they now carry data-k on their .turn div.


def test_a_full_human_row_carries_data_k(SV):
    """THE CHANGE THIS PINS. A .turn div must carry data-k so the append bridge
    can find and replace it without a full page reload."""
    rows = SV.rows_for([_line(dict({"type": "user",
                                    "message": {"content": "hello"}}, uuid="u-1"))])
    out = SV.render_html(rows, "t")
    assert re.search(r"<div class='turn you' data-k=\"[^\"]+\">", out)


def test_a_full_assistant_row_carries_data_k(SV):
    rows = SV.rows_for([_line(dict(_assistant({"type": "text", "text": "hi"}), uuid="a-1"))])
    out = SV.render_html(rows, "t")
    assert re.search(r"<div class='turn said' data-k=\"[^\"]+\">", out)


def test_full_row_data_k_matches_the_rows_key(SV):
    """data-k on the div must carry the same key rows_for stamped."""
    rows = SV.rows_for([_line(dict({"type": "user",
                                    "message": {"content": "hi"}}, uuid="u-99"))])
    assert rows[0]["key"] == "u-99.0"
    out = SV.render_html(rows, "t")
    assert 'data-k="u-99.0"' in out


def test_full_row_data_k_is_escaped(SV):
    """Same escaping as collapsed rows: the attribute must be HTML-escaped."""
    rows = SV.rows_for([_line(dict({"type": "user",
                                    "message": {"content": "hi"}}, uuid="u-1"))])
    rows[0]["key"] = 'x" onload="alert(1)'
    out = SV.render_html(rows, "t")
    assert 'onload="alert(1)' not in out
    assert "&quot;" in out


# --- CHANGE 2: render_delta (the append bridge producer) ---------------------


def _make_transcript(tmp_path: Path, lines: list) -> Path:
    """Write a JSONL transcript to a temp file."""
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    return p


def test_render_delta_pure_append_returns_only_new_rows(SV, tmp_path):
    """After a pure append (no fold mutations), the delta contains only the new rows."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "go"}}
    reply = dict(_assistant({"type": "text", "text": "done"}), uuid="a-1")

    # Client has already rendered the prompt (1 line).
    p = _make_transcript(tmp_path, [prompt, reply])
    result = SV.render_delta(p, 1, "test")

    assert result["full"] is False
    assert result["lines"] == 2
    assert len(result["rows"]) == 1
    assert result["rows"][0]["key"] == "a-1.0"
    assert "done" in result["rows"][0]["html"]


def test_render_delta_fold_mutation_includes_tool_use_row(SV, tmp_path):
    """THE REWIND RULE. A tool_result lands after the cursor on line 1.  The
    tool_use it answers is on line 0 — behind the cursor — but the fold mutated
    it, so it must be included in the delta with the result in its body."""
    call_rec = dict(_assistant(
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}}), uuid="a-1")
    result_rec = {"type": "user", "uuid": "u-2",
                  "toolUseResult": {"stdout": "file.txt"},
                  "message": {"content": [
                      {"type": "tool_result", "tool_use_id": "t1",
                       "content": "file.txt"}]}}

    # Client has rendered the call (1 line). Result arrives on line 1.
    p = _make_transcript(tmp_path, [call_rec, result_rec])
    result = SV.render_delta(p, 1, "test")

    assert result["full"] is False
    assert len(result["rows"]) == 1
    assert result["rows"][0]["key"] == "a-1.0"
    assert "file.txt" in result["rows"][0]["html"]


def test_render_delta_cursor_past_eof_returns_full_true(SV, tmp_path):
    """A cursor past the end of the file signals a rotated or truncated transcript.
    The escape hatch fires and returns the whole page."""
    p = _make_transcript(tmp_path, [
        {"type": "user", "message": {"content": "hi"}}
    ])
    result = SV.render_delta(p, 999, "test")

    assert result["full"] is True
    assert "page" in result
    assert "hi" in result["page"]


def test_render_delta_cursor_at_eof_returns_empty_rows(SV, tmp_path):
    """Cursor at exactly the file length means the client is up to date."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "hi"}}
    p = _make_transcript(tmp_path, [prompt])
    first = SV.render_delta(p, 0, "test")
    assert first["full"] is False

    # Feed back the returned cursor.
    result = SV.render_delta(p, first["lines"], "test")
    assert result["full"] is False
    assert result["rows"] == []
    assert result["lines"] == first["lines"]


def test_render_delta_cursor_round_trips(SV, tmp_path):
    """Feeding back the returned 'lines' twice yields an empty rows list."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        dict(_assistant({"type": "text", "text": "done"}), uuid="a-1"),
    ]
    p = _make_transcript(tmp_path, recs)

    first = SV.render_delta(p, 0, "test")
    assert first["full"] is False
    cursor = first["lines"]

    second = SV.render_delta(p, cursor, "test")
    assert second["full"] is False
    assert second["rows"] == []

    third = SV.render_delta(p, cursor, "test")
    assert third["full"] is False
    assert third["rows"] == []


def test_render_delta_rows_carry_data_k(SV, tmp_path):
    """Every row in the delta carries key in its html, matching the key field."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "hello"}}
    p = _make_transcript(tmp_path, [prompt])
    result = SV.render_delta(p, 0, "test")

    assert result["full"] is False
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["key"] == "u-1.0"
    assert f'data-k="{row["key"]}"' in row["html"]


def test_render_delta_negative_cursor_treated_as_zero(SV, tmp_path):
    """A negative cursor must return the same rows as cursor=0.

    Deck never sends a negative cursor (it only echoes back a value the mini
    gave it), so this is latent rather than live — but the clamp pins the
    invariant so a hand-constructed call cannot return every row while appearing
    to honour a non-zero starting position.
    """
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "hi"}}
    reply = dict(_assistant({"type": "text", "text": "done"}), uuid="a-1")
    p = _make_transcript(tmp_path, [prompt, reply])

    result_neg = SV.render_delta(p, -5, "test")
    result_zero = SV.render_delta(p, 0, "test")

    assert result_neg["full"] is False
    assert result_zero["full"] is False
    assert [r["key"] for r in result_neg["rows"]] == [r["key"] for r in result_zero["rows"]]


def test_render_delta_result_not_yet_arrived_excludes_call(SV, tmp_path):
    """A tool call whose result has not yet arrived is included in the delta only
    if the call itself is after the cursor — not because of a future result."""
    call_rec = dict(_assistant(
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "ls"}}), uuid="a-1")
    # No result yet.
    p = _make_transcript(tmp_path, [call_rec])
    # Cursor at 0: call is new, must be included.
    result = SV.render_delta(p, 0, "test")
    assert len(result["rows"]) == 1

    # Cursor at 1: call was already seen, no result, not included.
    result2 = SV.render_delta(p, 1, "test")
    assert result2["rows"] == []


# --- CHANGE 3: transcript-lines meta (Deck append-bridge cursor) -------------


def test_transcript_lines_meta_matches_render_deltas_lines_count(
        session_view_cli, SV, monkeypatch, capsys, tmp_path):
    """THE LOAD-BEARING PROPERTY, checked through the CLI wiring Deck actually
    calls (`--render-tmux`), not against `_rows_for_lines` called a second
    time. Deck feeds the meta's number straight back as its next delta cursor,
    so `_render_tmux`'s own count-and-render step must stamp the SAME count
    render_delta reports for the SAME transcript.

    A prior version of this test called `SV._rows_for_lines` directly and fed
    its own total into `render_html`, which only proved the function agrees
    with itself — it never invoked `_render_tmux`, so a bug planted in its
    "total, rows = SV._rows_for_lines(...)" line went undetected by the full
    gate. Verified by hand: mutating that line to `total + 1` in `_render_tmux`
    makes this test fail; reverted after confirming that."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        dict(_assistant({"type": "text", "text": "done"}), uuid="a-1"),
    ]
    p = _make_transcript(tmp_path, recs)

    monkeypatch.setattr(session_view_cli, 'SV', SV)
    monkeypatch.setattr(session_view_cli.TT, 'transcript_for_tmux_session',
                        lambda name: p)
    monkeypatch.setattr(session_view_cli.TT, 'run',
                        lambda argv, stdin=None: str(tmp_path))

    assert session_view_cli._render_tmux("fake-session", None) == 0
    page = capsys.readouterr().out

    m = re.search(r'<meta name="transcript-lines" content="(\d+)">', page)
    assert m, "no transcript-lines meta in the rendered page"
    stamped = int(m.group(1))

    delta = SV.render_delta(p, 0, "fake-session")
    assert delta["full"] is False

    assert stamped == delta["lines"]


def test_transcript_lines_meta_is_absent_when_omitted(SV):
    """Every existing caller omits transcript_lines, and its output must stay
    byte for byte unchanged."""
    assert "transcript-lines" not in SV.render_html([], "x")


def test_transcript_lines_meta_is_present_for_an_empty_transcript(SV):
    """An empty conversation emits no `.turns` element at all, so the count
    has to live in the meta rather than there — otherwise a fresh pane could
    never establish a starting cursor of zero."""
    page = SV.render_html([], "x", transcript_lines=0)
    assert '<meta name="transcript-lines" content="0">' in page
    assert "class=turns" not in page


@pytest.mark.parametrize("bogus", ["3", 3.5, -1, True, [3], {}])
def test_transcript_lines_meta_refuses_non_int(SV, bogus):
    """Only a non-negative int reaches the attribute — the same posture as
    theme (closed set), background (_HEX_COLOUR), scale and line_height
    (range-checked coercions). Nothing else is a live caller today, but
    render_html is a public function and this is the invariant its own
    docstring states in prose; enforce it rather than trust the prose."""
    page = SV.render_html([], "x", transcript_lines=bogus)
    assert "transcript-lines" not in page


def test_the_full_true_envelope_carries_the_meta(SV, tmp_path):
    """The full:true escape hatch (cursor past EOF) must stamp the meta too:
    without it, a pane that fell back to a full page is cursorless again and
    goes straight back to the --delta-since 0 fetch this change exists to
    remove."""
    p = _make_transcript(tmp_path, [
        {"type": "user", "uuid": "u-1", "message": {"content": "hi"}}
    ])
    result = SV.render_delta(p, 999, "test")

    assert result["full"] is True
    assert '<meta name="transcript-lines" content="1">' in result["page"]


def test_the_unreadable_transcript_envelope_carries_the_meta(SV, tmp_path):
    """The OTHER full:true escape hatch — the transcript is unreadable
    (OSError) — must stamp the meta too, with the only count it can honestly
    report: 0, since no lines were read. Without it a pane hitting this branch
    stays cursorless, same failure mode as the cursor-past-EOF branch above."""
    p = tmp_path / "does-not-exist.jsonl"

    result = SV.render_delta(p, 0, "test")

    assert result["full"] is True
    assert '<meta name="transcript-lines" content="0">' in result["page"]


# --- held rows: the queue drawn as turns (input-robustness Slice D) -----------
#
# Slice C's spike found a native overlay could not match the page's own turn
# typography closely enough to avoid a visible seam at the release moment (six
# of six scale/appearance combinations failed). So the held message is drawn by
# the page itself, in the same markup a read "you" turn uses, plus one extra
# class. These pin the fold that makes that markup, not the fold itself —
# `fold_queue`'s own behaviour (enqueue/dequeue/popAll/remove) is pinned in
# `test_session_state.py`.

def test_a_queued_message_renders_as_a_held_turn(SV):
    rows = SV.held_rows_for([_line({
        "type": "queue-operation", "operation": "enqueue",
        "content": "also check the timeout branch",
    })])
    assert len(rows) == 1
    row = rows[0]
    assert row["held"] is True
    assert row["kind"] == "prompt"
    assert row["display"] == SV.FULL
    assert row["body"] == "also check the timeout branch"

    out = SV.render_html(rows, "t")
    assert "class='turn you held'" in out
    assert "also check the timeout branch" in out


def test_several_queued_messages_render_in_queue_order(SV):
    lines = [
        _line({"type": "queue-operation", "operation": "enqueue", "content": "first"}),
        _line({"type": "queue-operation", "operation": "enqueue", "content": "second"}),
    ]
    rows = SV.held_rows_for(lines)
    assert [r["body"] for r in rows] == ["first", "second"]


def test_a_non_user_enqueue_is_filtered_out(SV):
    """The queue carries more than typed messages — a `<task-notification>`
    blob from a background task appears in the corpus (the plan names this
    hazard by name) and must not draw as "your held message"."""
    lines = [
        _line({"type": "queue-operation", "operation": "enqueue",
               "content": "<task-notification><summary>build finished"
                          "</summary></task-notification>"}),
        _line({"type": "queue-operation", "operation": "enqueue", "content": "real message"}),
    ]
    rows = SV.held_rows_for(lines)
    assert [r["body"] for r in rows] == ["real message"]


def test_an_empty_queue_renders_nothing_extra(SV):
    lines = [_line({"type": "user", "message": {"content": "hi"}})]
    assert SV.held_rows_for(lines) == []


def test_an_unfoldable_queue_returns_none_not_positively_empty(SV):
    """`remove` names neither the index nor the content of the entry that
    left, so `fold_queue` refuses rather than guesses — and `held_rows_for`
    must not raise either, but it must also not collapse "unknown" into `[]`:
    `[]` is what `render_delta` reads as "queue is empty" and feeds straight
    into its removal list. `None` is the honest answer; callers with nothing
    to reconcile against (a full render) are the ones that get to treat it
    as `[]`, not this function itself."""
    lines = [_line({"type": "queue-operation", "operation": "remove"})]
    assert SV.held_rows_for(lines) is None


def test_held_rows_for_returns_none_when_session_state_is_unavailable(SV, monkeypatch):
    """`session_state` is an optional import (the picker_render pattern): a
    module unlinked in `~/.local/lib` must not take this page down. It costs
    the caller the held rows, but as `None` (unknown), not `[]` (positively
    empty) — the same three-way distinction as the ambiguous-fold case above,
    and for the identical reason: `render_delta` cannot tell "the module
    disappeared" from "the queue emptied" if both read as `[]`."""
    monkeypatch.setattr(SV, "_SS", None)
    lines = [_line({"type": "queue-operation", "operation": "enqueue", "content": "hi"})]
    assert SV.held_rows_for(lines) is None


def test_held_rows_for_recovers_once_an_ambiguous_remove_scrolls_out_of_the_tail(SV, monkeypatch):
    """`held_rows_for` now bounds its fold to the same tail window
    `session_state._read_held_queue` reads (`_tail_lines`, over `_SS.
    _HELD_TAIL_BYTES`), so an ambiguous `remove` desyncs the page's held rows
    only until it scrolls out of that window — matching the state field's own
    recovery, rather than going dark for the rest of the session the way an
    unbounded fold would (a transcript is append-only, so a `remove` seen once
    would otherwise be seen on every later full-file read forever)."""
    monkeypatch.setattr(SV._SS, "_HELD_TAIL_BYTES", 150)
    remove_line = _line({"type": "queue-operation", "operation": "remove"})
    padding_line = _line({"type": "user", "message": {"content": "x" * 50}})
    enqueue_line = _line({"type": "queue-operation", "operation": "enqueue",
                          "content": "still here"})

    # Small enough that the whole thing fits in the tail: `remove` is in
    # scope and the fold is genuinely ambiguous.
    assert SV.held_rows_for([remove_line, enqueue_line]) is None

    # One padding line is enough to push `remove` past the 150-byte cap,
    # leaving only the enqueue in the tail — the fold recovers.
    rows = SV.held_rows_for([remove_line, padding_line, enqueue_line])
    assert rows is not None
    assert [r["body"] for r in rows] == ["still here"]


def test_both_tail_readers_agree_on_a_record_that_straddles_the_boundary(SV, tmp_path, monkeypatch):
    """The page and the live bar must select the SAME records, not merely share
    a constant.

    `session_state._read_held_queue` seeks to `size - _HELD_TAIL_BYTES` in the
    raw file, so a record whose start lies before that offset arrives truncated
    and is dropped. `_tail_lines` used to round that same record UP to
    whole-line-included, and review reproduced the divergence: a 2000-byte
    enqueue followed by a 50-byte one under a 1000-byte cap gave the state field
    one message and the page two.

    The disagreement is worse than a content mismatch. An ambiguous `remove`
    visible to one reader and dropped by the other leaves one answering
    "unknown" while the other answers confidently — which is the distinction
    every other refusal in this system exists to protect.
    """
    monkeypatch.setattr(SV._SS, "_HELD_TAIL_BYTES", 1000)
    big = _line({"type": "queue-operation", "operation": "enqueue",
                 "content": "x" * 2000})
    small = _line({"type": "queue-operation", "operation": "enqueue",
                   "content": "small"})

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(big + "\n" + small + "\n", encoding="utf-8")

    from_state = SV._SS._read_held_queue(transcript)
    from_page = SV.held_rows_for([big, small])

    assert from_state is not None and from_page is not None
    assert from_state["messages"] == [r["body"] for r in from_page], (
        "the page and the state field selected different records at the tail "
        "boundary")
    # And specifically: the straddling record is dropped by BOTH, not kept by
    # one. Asserting equality alone would pass if a later change made both
    # round up together, which is the other way to be wrong.
    assert from_state["messages"] == ["small"]


def test_held_row_keys_never_collide_with_real_row_keys(SV):
    lines = [
        _line({"type": "user", "uuid": "u-1", "message": {"content": "hi"}}),
        _line(dict(_assistant({"type": "tool_use", "name": "Bash",
                                "input": {"command": "ls"}}), uuid="a-1")),
        _line({"type": "queue-operation", "operation": "enqueue", "content": "hi"}),
    ]
    real_keys = {r["key"] for r in SV.rows_for(lines)}
    held_keys = {r["key"] for r in SV.held_rows_for(lines)}
    assert held_keys
    assert real_keys.isdisjoint(held_keys)


def test_held_row_key_is_structurally_unreachable_from_row_key(SV):
    """`_row_key` only ever emits `<uid>.<index>` (uid validated against a
    charset with no `~`) or the `h<hex>` fallback, with `~n` appended only as a
    SUFFIX. So no real key can ever START with `~`, which is what makes a
    leading `~` collision-proof independent of transcript content — even a
    hostile uuid engineered to read like a held key falls back to the hash
    instead of being used verbatim, because `~` is outside `_ROW_KEY_OK`."""
    held_key = SV._held_row_key(0, "text")
    assert held_key.startswith("~")

    lines = [_line({"type": "user", "uuid": "~held.0.deadbeefdeadbeef",
                    "message": {"content": "hi"}})]
    real_key = SV.rows_for(lines)[0]["key"]
    assert not real_key.startswith("~")
    assert real_key != held_key


def test_held_css_uses_currentcolor_and_reduced_ink_not_a_new_hue(SV):
    """NO NEW COLOUR: presence, position and the .who label carry the held
    state; the rule must derive from currentColor, the same ink `.you .body`
    already draws with, never a new CSS variable or a literal colour."""
    css = SV.PAGE_CSS
    assert ".you.held .body" in css
    held_rule = css.split(".you.held .body", 1)[1].split("}", 1)[0]
    assert "currentColor" in held_rule
    assert "#" not in held_rule
    assert "dashed" in held_rule
    # Reduced ink relative to the read treatment's .95.
    assert "opacity: .55" in held_rule


def test_render_session_includes_a_held_row(SV, monkeypatch, tmp_path):
    """End to end through `read_rows`/`render_session`, not just the fold."""
    proj = tmp_path / "-Users-operator-thing"
    proj.mkdir(parents=True)
    sid = "0199c7de-1234-4abc-8def-0123456789ab"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "hello there"}}) + "\n"
        + json.dumps({"type": "queue-operation", "operation": "enqueue",
                      "content": "also check the timeout branch"}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(SV, "CLAUDE_PROJECTS", tmp_path)

    page = SV.render_session(sid)

    assert "hello there" in page
    assert "also check the timeout branch" in page
    assert "class='turn you held'" in page


def test_render_session_with_no_queue_activity_has_no_held_row(SV, monkeypatch, tmp_path):
    proj = tmp_path / "-Users-operator-thing"
    proj.mkdir(parents=True)
    sid = "0199c7de-1234-4abc-8def-0123456789ac"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "hello there"}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(SV, "CLAUDE_PROJECTS", tmp_path)

    page = SV.render_session(sid)

    assert "turn you held" not in page


# --- held rows in render_delta: the case the feature exists for --------------
#
# Deck applies a delta whenever a cursor already exists, which is whenever a
# reader has the pane open — the steady state, not the exception. The full-render
# tests above pin the fold; these pin that the SAME held rows reach the page on
# the path a working session actually takes, and that a released or drained
# queue entry is told to leave rather than left stranded on the page forever.


def _held_keys(rows: list) -> list:
    return [r["key"] for r in rows if r["key"].startswith("~held.")]


def test_render_delta_emits_a_newly_held_row(SV, tmp_path):
    """A message enqueued after the client's cursor must reach the page even
    though it produces no visible transcript row of its own — the gap this
    closes: a queue-operation line classifies HIDDEN and advances the cursor,
    but before this it emitted no delta row and no held row either."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "go"}}
    p = _make_transcript(tmp_path, [prompt])
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    assert _held_keys(first["rows"]) == []

    _make_transcript(tmp_path, [prompt, {
        "type": "queue-operation", "operation": "enqueue", "content": "hold me",
    }])
    second = SV.render_delta(p, cursor, "test")

    assert second["full"] is False
    assert second["removed"] == []
    held = _held_keys(second["rows"])
    assert len(held) == 1
    row = next(r for r in second["rows"] if r["key"] == held[0])
    assert "hold me" in row["html"]


def test_render_delta_removes_a_held_row_when_the_queue_drains(SV, tmp_path):
    """A held row the client is showing must be told to leave once it is no
    longer held — otherwise a released or deleted message waits on the page
    forever, which the plan calls worse than showing nothing."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ]
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    held = _held_keys(first["rows"])
    assert len(held) == 1
    held_key = held[0]

    _make_transcript(tmp_path, recs + [
        {"type": "queue-operation", "operation": "popAll"},
    ])
    second = SV.render_delta(p, cursor, "test")

    assert second["full"] is False
    assert second["removed"] == [held_key]
    assert _held_keys(second["rows"]) == []


def test_render_delta_adds_turn_rows_and_removes_a_held_row_together(SV, tmp_path):
    """The common real case: a queued message is released (becomes an ordinary
    turn) in the same poll that also carries the assistant's reply. One delta
    must both add the new rows and remove the now-stale held one."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ]
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    held_key = _held_keys(first["rows"])[0]

    _make_transcript(tmp_path, recs + [
        {"type": "queue-operation", "operation": "popAll"},
        {"type": "user", "uuid": "u-2", "message": {"content": "hold me"}},
        dict(_assistant({"type": "text", "text": "ok"}), uuid="a-1"),
    ])
    second = SV.render_delta(p, cursor, "test")

    assert second["removed"] == [held_key]
    keys = [r["key"] for r in second["rows"]]
    assert "u-2.0" in keys
    assert "a-1.0" in keys
    assert _held_keys(second["rows"]) == []


def test_render_delta_does_not_remove_a_held_row_that_only_aged_out(
        SV, monkeypatch, tmp_path):
    """SECURITY REVIEW'S SECOND FINDING, and the half `fold_queue` cannot cover.

    Its own refusal handles a record it cannot resolve. It cannot handle a
    record it never saw: an `enqueue` that scrolls past `_HELD_TAIL_BYTES` with
    no later operation on it leaves the fold short by one, with nothing to trip
    on. The fold over the client's shorter prefix still holds that message, so
    differencing the two windows names a still-queued message as removed and
    tells the client to erase it — the same vanishing this PR exists to fix,
    reached from the other side.

    The message here is enqueued and never released, so the honest answer is to
    say nothing about it. Asserting on `removed` rather than on the rows: the
    row legitimately drops out of a truncated fold, and what must not happen is
    the client being told to delete it."""
    recs = [{"type": "user", "uuid": "u-1", "message": {"content": "go"}},
            {"type": "queue-operation", "operation": "enqueue", "content": "still mine"}]
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    assert _held_keys(first["rows"])  # the row was sent to the client

    # More than the tail window of transcript arrives while it stays queued. A
    # small cap for the same reason as the sibling test above.
    monkeypatch.setattr(SV._SS, "_HELD_TAIL_BYTES", 40_000)
    filler = dict(_assistant({"type": "text", "text": "x" * 4000}), uuid="f-1")
    bulk = [filler] * ((SV._SS._HELD_TAIL_BYTES // 4000) + 4)
    _make_transcript(tmp_path, recs + bulk)

    second = SV.render_delta(p, cursor, "test")
    if second.get("full"):
        return  # the full-page hatch is also a correct answer here
    assert second["removed"] == []


def test_render_delta_clears_a_row_released_by_popall_in_a_truncated_window(
        SV, monkeypatch, tmp_path):
    """THE GHOST BOTH REVIEWERS FOUND, and the case gating on the window rather
    than on sync got wrong.

    A `popAll` release resolves cleanly, so the fold legitimately drops the
    message — but the transcript is over `_HELD_TAIL_BYTES`, so the coarse
    "did we see the whole session" flag said no and the removal was suppressed.
    The client kept a "you (queued)" row beside the delivered turn: the same
    message twice, persisting for the life of the pane.

    `popAll` empties the real queue, so the fold IS synced from that point even
    inside a truncated window, and the removal is safe. Pinned against
    `test_render_delta_does_not_remove_a_held_row_that_only_aged_out`, which
    pins the opposite direction — no `popAll`, so no sync, so no removal."""
    # A SMALL CAP RATHER THAN THE REAL ONE. What is under test is the behaviour
    # at a truncated window, not the constant's value, and sizing the fixture off
    # the real `_HELD_TAIL_BYTES` made these tests cost eight times more the day
    # it was raised to 8MB — three minutes of suite time for two cases.
    monkeypatch.setattr(SV._SS, "_HELD_TAIL_BYTES", 40_000)
    filler = dict(_assistant({"type": "text", "text": "x" * 4000}), uuid="f-1")
    # The bulk comes FIRST, so the file is over the cap while the queue records
    # themselves stay inside the tail. That is the decidable case: the fold can
    # see the release even though it cannot see the session's start.
    recs = ([{"type": "user", "uuid": "u-1", "message": {"content": "go"}}]
            + [filler] * ((SV._SS._HELD_TAIL_BYTES // 4000) + 4)
            + [{"type": "queue-operation", "operation": "enqueue", "content": "read me"}])
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    held_key = _held_keys(first["rows"])[0]

    _make_transcript(tmp_path, recs + [
        {"type": "queue-operation", "operation": "popAll", "content": "read me"},
    ])

    second = SV.render_delta(p, cursor, "test")
    if second.get("full"):
        return  # a full page also clears it correctly
    assert held_key in second["removed"]


def test_render_delta_marks_a_held_row_newest_and_an_ordinary_row_not(SV, tmp_path):
    """THE FLAG THAT HAD NO TEST. Review confirmed the field could be deleted
    outright with the whole suite still green — on the one change of the three
    that fixes a defect a reader actually saw.

    `newest` is what tells the client a held row belongs at the bottom wherever
    it currently sits. Without it the client replaces the row in place, new
    transcript rows insert in front of it, and the held message walks off the
    top of the viewport. Asserted on both sides: present and true on the held
    row, absent on the transcript row beside it in the same batch."""
    recs = [{"type": "user", "uuid": "u-1", "message": {"content": "go"}}]
    p = _make_transcript(tmp_path, recs)
    cursor = SV.render_delta(p, 0, "test")["lines"]

    _make_transcript(tmp_path, recs + [
        dict(_assistant({"type": "text", "text": "working"}), uuid="a-1"),
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ])
    delta = SV.render_delta(p, cursor, "test")

    by_key = {r["key"]: r for r in delta["rows"]}
    held_key = _held_keys(delta["rows"])[0]
    assert by_key[held_key]["newest"] is True
    assert "newest" not in by_key["a-1.0"]


def test_render_delta_converges_when_the_client_cursor_predates_an_add_and_drain(SV, tmp_path):
    """A client that has not polled in a while can have a cursor from before an
    entire enqueue-then-drain cycle, never having seen the held row at all. The
    reconstruction must still land on the current truth — nothing held, nothing
    to remove — rather than either fabricating a stale removal or leaking a
    phantom held row it never sent."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "go"}}
    p = _make_transcript(tmp_path, [prompt])
    before = SV.render_delta(p, 0, "test")
    cursor = before["lines"]
    assert _held_keys(before["rows"]) == []

    _make_transcript(tmp_path, [
        prompt,
        {"type": "queue-operation", "operation": "enqueue", "content": "ghost"},
        {"type": "queue-operation", "operation": "dequeue"},
    ])
    after = SV.render_delta(p, cursor, "test")

    assert after["removed"] == []
    assert _held_keys(after["rows"]) == []


def test_render_delta_full_escape_hatch_includes_held_rows(SV, tmp_path):
    """The cursor-past-EOF full-page fallback replaces the whole document, so
    it must draw current held rows itself rather than relying on a delta path
    it is bypassing — otherwise a rotated or truncated transcript would drop a
    still-queued message from the page until the next poll happens to differ."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ]
    p = _make_transcript(tmp_path, recs)
    result = SV.render_delta(p, 999, "test")

    assert result["full"] is True
    assert "hold me" in result["page"]
    assert "class='turn you held'" in result["page"]


def test_render_delta_escapes_to_full_page_when_session_state_vanishes_between_polls(
        SV, monkeypatch, tmp_path):
    """THE FAILURE THE REVIEW NAMED. Each `bin/session-view --delta-since`
    invocation is a fresh process, so `_SS` is re-evaluated on every poll: T1
    has it available and a held row reaches the client; before T2 `session_
    state.py` is unlinked mid-`install.sh` (#309's outage). A naive `_SS is
    None -> []` on the T2 side would make `held_before` and `held_now` both
    `[]`, so `removed` would be `[]` too — never naming the key the client is
    still showing, which would sit on screen reading "you (queued)" forever.
    The escape hatch must fire instead of computing that empty diff."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ]
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]
    assert _held_keys(first["rows"])  # a held row reached the client at T1

    monkeypatch.setattr(SV, "_SS", None)
    second = SV.render_delta(p, cursor, "test")

    assert second["full"] is True


def test_render_delta_escapes_to_full_page_when_session_state_vanishes_mid_call(
        SV, monkeypatch, tmp_path):
    """A narrower window than the poll-to-poll case above: `render_delta`
    itself calls `held_rows_for` twice per poll (`held_before`, then `held_
    now`), and `_SS` is re-read fresh each time. If it disappears BETWEEN
    those two calls within the SAME poll, `held_before` and `held_now` must
    not silently end up `[]` and `None` read as agreeing — the escape hatch
    must still fire rather than reconciling a known state against an
    unknown one."""
    recs = [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
        {"type": "queue-operation", "operation": "enqueue", "content": "hold me"},
    ]
    p = _make_transcript(tmp_path, recs)
    first = SV.render_delta(p, 0, "test")
    cursor = first["lines"]

    real_fold_queue = SV._SS._fold_queue
    calls = {"n": 0}

    def vanishing_fold_queue(records, **kwargs):
        calls["n"] += 1
        result = real_fold_queue(records, **kwargs)
        if calls["n"] == 1:
            # `held_before`'s call resolves normally; `_SS` disappears before
            # the `held_now` call that follows it in the same render_delta.
            monkeypatch.setattr(SV, "_SS", None)
        return result

    # Patched on `_fold_queue`, the internal the delta path calls — the public
    # `fold_queue` is a wrapper over it and patching that would never fire.
    monkeypatch.setattr(SV._SS, "_fold_queue", vanishing_fold_queue)

    second = SV.render_delta(p, cursor, "test")

    assert second["full"] is True
    # The escape hatch itself has an unknown current held state (`held_now`
    # is None once `_SS` is gone), so it honestly draws no held row rather
    # than resurrecting one from a state it can no longer confirm.
    assert "class='turn you held'" not in second["page"]


def test_render_delta_removed_is_always_a_list(SV, tmp_path):
    """`removed` is unconditionally present, `[]` rather than absent when
    there is nothing to remove — one shape for a routine no-op batch rather
    than two, even though the Deck side also tolerates the key being missing."""
    prompt = {"type": "user", "uuid": "u-1", "message": {"content": "hi"}}
    p = _make_transcript(tmp_path, [prompt])
    result = SV.render_delta(p, 0, "test")
    assert result["removed"] == []


def test_serve_unix_hint_names_the_running_user_and_the_tailnet_host(session_view_cli, tmp_path,
                                                                     monkeypatch, capsys):
    """The printed SSH forward is runnable as printed: it takes the user from
    `getpass.getuser()` at runtime, then the configured tailnet_host. The stubbed
    `getuser` returns "alice", so a hard-coded login name would fail. The stub
    server touches the socket path so `_serve_unix` can chmod it, and its
    serve_forever stops at once."""
    class _Srv:
        def __init__(self, addr, handler):
            Path(addr).touch()

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(session_view_cli, "ThreadingUnixHTTPServer", _Srv)
    monkeypatch.setattr(session_view_cli.signal, "signal", lambda *a: None)
    monkeypatch.setattr(session_view_cli.getpass, "getuser", lambda: "alice")
    sock = tmp_path / "session-view.sock"

    assert session_view_cli._serve_unix(sock) == 0

    out = capsys.readouterr().out
    assert (f"ssh -N -L {session_view_cli.DEFAULT_PORT}:{sock} "
            "alice@tailnet.example") in out
    # The stubbed getuser is the only login that may appear before an "@" —
    # a hardcoded real login would show up here as a second, un-stubbed one,
    # on any machine this runs on, not just one where it happens to match.
    assert {m.group(1) for m in re.finditer(r"(\S+)@", out)} == {"alice"}


def test_render_html_keeps_its_heading_by_default(SV):
    """`--render SESSION_ID`, `--list` and the served pages still show the
    session's name as a heading."""
    assert "<h1>x</h1>" in SV.render_html([], "x")


def test_render_tmux_leaves_out_the_heading_deck_already_shows(
        session_view_cli, SV, monkeypatch, capsys, tmp_path):
    """Deck's conversation pane sits under a status bar that names the session,
    so the page Deck asks for carries no `<h1>` (the operator, 2026-10-06). The
    `<title>` stays."""
    p = _make_transcript(tmp_path, [
        {"type": "user", "uuid": "u-1", "message": {"content": "go"}},
    ])
    monkeypatch.setattr(session_view_cli, 'SV', SV)
    monkeypatch.setattr(session_view_cli.TT, 'transcript_for_tmux_session',
                        lambda name: p)
    monkeypatch.setattr(session_view_cli.TT, 'run',
                        lambda argv, stdin=None: str(tmp_path))

    assert session_view_cli._render_tmux("deck", None) == 0
    page = capsys.readouterr().out
    assert "<h1>" not in page
    assert "<title>deck</title>" in page


def test_render_delta_full_page_fallbacks_leave_out_the_heading(SV, tmp_path):
    """A full page from the delta path replaces the pane Deck drew from
    `--render-tmux`, so it must match it: a heading here would reappear on
    every rotated transcript."""
    p = _make_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
    past_eof = SV.render_delta(p, 999, "deck")
    assert past_eof["full"] is True
    assert "<h1>" not in past_eof["page"]

    missing = SV.render_delta(tmp_path / "gone.jsonl", 0, "deck")
    assert missing["full"] is True
    assert "<h1>" not in missing["page"]


def test_render_delta_unreconstructable_held_rows_page_leaves_out_the_heading(
        SV, tmp_path, monkeypatch):
    """The third full-page fallback: with the queue state unknown, the held
    rows cannot be reconstructed, so `render_delta` sends the whole page. It
    replaces the pane `--render-tmux` drew and must carry no heading either."""
    monkeypatch.setattr(SV, "_SS", None)
    p = _make_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
    result = SV.render_delta(p, 0, "deck")
    assert result["full"] is True
    assert "<h1>" not in result["page"]
