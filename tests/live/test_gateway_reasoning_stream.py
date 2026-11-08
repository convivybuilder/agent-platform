"""Live check: the gateway's LiteLLM opens a `thinking` content block for a
reasoning stream from DeepSeek- and Moonshot-shaped chunks (gateway/requirements.txt,
the 1.86.2 -> 1.91.5 upgrade).

THE DEFECT (LiteLLM 1.86.2). The Anthropic `/v1/messages` streaming adapter
(litellm/llms/anthropic/experimental_pass_through/adapters/) always opens content
block 0 as `{"type": "text"}` (streaming_iterator.py). Its content-block
classifier, `_translate_streaming_openai_chunk_to_anthropic_content_block`
(transformation.py), returns `"thinking"` only for a delta carrying
`thinking_blocks`, the field Anthropic- and Bedrock-shaped providers send reasoning
in. DeepSeek and Moonshot send reasoning in `delta.reasoning_content` instead,
which the classifier does not check, so it falls through to `"text"` and no
`thinking` block ever opens. The delta translator
(`_translate_streaming_openai_chunk_to_anthropic`) turns `reasoning_content` into
`thinking_delta` events regardless, so those events land inside the text block
opened for block 0. Claude Code's Anthropic client rejects that shape ("Stream
event unreadable") and falls back to a non-streamed resend of the same turn after
1-2s. The provider bills both requests; LiteLLM's spend callback, which only fires
once a stream completes, logs just the resend. A capture of a short deepseek run
showed 2-4 of every 6 turns with reasoning sent twice; turns without reasoning
streamed cleanly.

THE FIX (LiteLLM 1.91.0, carried into the 1.91.5 floor gateway/requirements.txt
pins). The classifier gains
`elif isinstance(choice, StreamingChoices) and getattr(choice.delta,
"reasoning_content", None): return "thinking", ...`, so a `reasoning_content`-only
delta now opens a real `thinking` content block before any `thinking_delta` is
emitted into it.

WHAT THIS TEST DRIVES. `AnthropicStreamWrapper`
(litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py)
is the class the `/v1/messages` streaming path wraps every OpenAI-shaped
completion stream in. This test feeds it two synthetic `ModelResponseStream`
sequences, built from the captured failing stream with filenames and text
replaced by synthetic equivalents: one with `reasoning_content` chunks
(DeepSeek/Moonshot shape), one without. It drives those sequences with a small
script, run under the gateway venv's OWN interpreter (`GATEWAY_PYTHON`) rather
than this repo's venv, because the adapter lives in the gateway's isolated
`litellm` install. gateway/requirements.txt is not read by the repo's pytest (see
its header comment), and this repo's venv does not have `litellm` installed at
all. The script prints every emitted Anthropic SSE event as one JSON line; this
test asserts on that output.

Skipped by default. It runs real code from the gateway venv's installed LiteLLM,
not this repo's own dependencies, and whether it passes depends entirely on which
LiteLLM version is installed there. The 1.86.2 -> 1.91.5 upgrade this PR makes to
gateway/requirements.txt does not reach the already-provisioned venv; a human runs
`pip install -r gateway/requirements.txt` in it separately. Run by hand:

    AGENTCTL_LIVE_GATEWAY_STREAM=1 .venv/bin/python -m pytest \\
        tests/live/test_gateway_reasoning_stream.py -s

Set GATEWAY_PYTHON to point at a different interpreter; it defaults to
$HOME/.local/venv-llm-gateway/bin/python, this deployment's gateway venv.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTCTL_LIVE_GATEWAY_STREAM") != "1",
    reason="live gateway LiteLLM stream check: set AGENTCTL_LIVE_GATEWAY_STREAM=1 "
           "to run it under the gateway venv's own interpreter",
)

GATEWAY_PYTHON = os.environ.get(
    "GATEWAY_PYTHON", str(Path.home() / ".local" / "venv-llm-gateway" / "bin" / "python")
)

# Builds one of the two synthetic ModelResponseStream sequences, feeds it through
# litellm's own AnthropicStreamWrapper, and prints each emitted event as one JSON
# line on stdout. Runs under GATEWAY_PYTHON; litellm lives only in the gateway
# venv, not this repo's. `-I` (isolated mode, passed by the caller) keeps it from
# picking up an unrelated script of the same name from a cwd or script directory
# on sys.path.
_DRIVER_SCRIPT = '''
import json
import sys

from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    Delta,
    Function,
    ModelResponseStream,
    StreamingChoices,
    Usage,
)
from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
    AnthropicStreamWrapper,
)

MODEL = "deepseek/deepseek-flash"


def _chunk(delta, finish_reason=None, usage=None):
    kwargs = {}
    if usage is not None:
        kwargs["usage"] = usage
    return ModelResponseStream(
        id="synthetic-1", created=1, model=MODEL,
        choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
        **kwargs,
    )


def _tool_call_delta():
    return Delta(tool_calls=[
        ChatCompletionDeltaToolCall(
            id="call_1", type="function", index=0,
            function=Function(name="Read", arguments=\'{"file_path": "a.txt"}\'),
        )
    ])


def reasoning_chunks():
    """Rebuilt from a captured failing deepseek stream: empty lead-in, three
    reasoning_content deltas, a text delta, a tool call, then finish+usage."""
    return [
        _chunk(Delta(content="")),
        _chunk(Delta(reasoning_content="The")),
        _chunk(Delta(reasoning_content=" user")),
        _chunk(Delta(reasoning_content=" wants")),
        _chunk(Delta(content="I will read the file.")),
        _chunk(_tool_call_delta()),
        _chunk(Delta(), finish_reason="tool_calls",
               usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)),
    ]


def control_chunks():
    """The same stream with the reasoning_content chunks removed."""
    return [
        _chunk(Delta(content="")),
        _chunk(Delta(content="I will read the file.")),
        _chunk(_tool_call_delta()),
        _chunk(Delta(), finish_reason="tool_calls",
               usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)),
    ]


def main():
    which = sys.argv[1]
    chunks = reasoning_chunks() if which == "reasoning" else control_chunks()
    wrapper = AnthropicStreamWrapper(completion_stream=iter(chunks), model=MODEL)
    for event in wrapper:
        sys.stdout.write(json.dumps(event, default=str) + "\\n")


if __name__ == "__main__":
    main()
'''


def _run_driver(tmp_path: Path, which: str) -> list[dict]:
    script = tmp_path / "gateway_reasoning_stream_driver.py"
    script.write_text(_DRIVER_SCRIPT)
    proc = subprocess.run(
        [GATEWAY_PYTHON, "-I", str(script), which],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, (
        f"driver script failed under {GATEWAY_PYTHON!r} (set GATEWAY_PYTHON to "
        f"override):\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"driver script produced no output; stderr:\n{proc.stderr}"
    return [json.loads(line) for line in lines]


def _assert_well_formed_blocks(events: list[dict]) -> None:
    """Content blocks are opened and closed one at a time: a content_block_start
    must not occur while another block's content_block_stop is still pending, and
    every content_block_stop must close the block that is actually open."""
    open_index = None
    for event in events:
        if event["type"] == "content_block_start":
            assert open_index is None, (
                f"content_block_start at index {event['index']} arrived while "
                f"block {open_index} was still open: {events}"
            )
            open_index = event["index"]
        elif event["type"] == "content_block_stop":
            assert open_index == event["index"], (
                f"content_block_stop at index {event['index']} does not match "
                f"the open block {open_index}: {events}"
            )
            open_index = None
    assert open_index is None, f"stream ended with block {open_index} still open: {events}"


def test_reasoning_stream_opens_a_thinking_block(tmp_path):
    events = _run_driver(tmp_path, "reasoning")
    _assert_well_formed_blocks(events)

    block_type_by_index: dict[int, str] = {}
    thinking_deltas_seen = 0
    for event in events:
        if event["type"] == "content_block_start":
            block_type_by_index[event["index"]] = event["content_block"]["type"]
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "thinking_delta":
            thinking_deltas_seen += 1
            index = event["index"]
            # The defect: 1.86.2's classifier never returns "thinking" for a
            # reasoning_content-only delta, so streaming_iterator.py never opens a
            # thinking block and this thinking_delta lands in whatever block 0
            # opened as (always "text"). The fix must have opened a "thinking"
            # block, at this exact index, before this delta was emitted.
            assert block_type_by_index.get(index) == "thinking", (
                f"thinking_delta at index {index} arrived in a block that opened "
                f"as {block_type_by_index.get(index)!r}, not 'thinking': {events}"
            )
    assert thinking_deltas_seen > 0, f"no thinking_delta events were emitted at all: {events}"
    assert "thinking" in block_type_by_index.values(), (
        f"no content_block_start ever opened a 'thinking' block: {events}"
    )


def test_control_stream_without_reasoning_has_no_thinking_block(tmp_path):
    events = _run_driver(tmp_path, "control")
    _assert_well_formed_blocks(events)

    block_types = {
        event["content_block"]["type"]
        for event in events if event["type"] == "content_block_start"
    }
    assert block_types == {"text", "tool_use"}, (
        f"control stream (no reasoning_content) opened unexpected block types "
        f"{block_types}: {events}"
    )
    for event in events:
        if event["type"] == "content_block_delta":
            assert event["delta"]["type"] != "thinking_delta", (
                f"control stream (no reasoning_content) emitted a thinking_delta: {events}"
            )
