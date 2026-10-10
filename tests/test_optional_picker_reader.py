"""The picker reader is optional, and `session_state` must survive its absence.

WHY THIS FILE EXISTS. `picker_render` landed in #303 as a hard module-scope import,
and within minutes of the merge every session in Deck read `unknown` with "cannot
reach the session". `~/.local/lib` is a per-file symlink farm built by
`install.sh`, so a new module is unlinked until that runs, while the checkout the
links point into is current the moment someone pulls. In that window
`import picker_render` raised, `session-agent` could not start at all, and an
optional enhancement had taken out the whole state channel.

The reader's own contract already said what should have happened: a capture that
failed, a version it has not been checked against, a layout that matched nothing
are all one answer, the field is absent. An unimportable module is that same case.

So there are two tests here and they are not the same test. One pins that the
module IMPORTS without the reader present, which is what the outage was. The other
pins that a state assembled without a reader still carries its gate, which is what
the outage cost. A fix could pass either alone and still leave Deck blind.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "agent-platform"


@pytest.fixture
def SS():
    spec = importlib.util.spec_from_file_location("session_state", LIB / "session_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated_slate_home(tmp_path, monkeypatch):
    """`session_state()` now resolves `asks_open`/`asks_answered` through
    `slate.ask_counts` unconditionally (CQ-358, platform/decisions/0091), and
    `slate.state_root()` re-reads `Path.home()` on every call rather than
    being a patchable module-level constant. Without this, the
    `SS.session_state("deck")` calls below would create
    `~/.local/state/convivy/slate/deck.json` on whatever machine runs this
    suite."""
    monkeypatch.setenv("HOME", str(tmp_path))


def _import_with_reader_refused(raising: str):
    """Import `session_state` in a subprocess where the reader raises `raising`.

    A subprocess, because the failure being reproduced happens at import time and
    cannot be provoked by patching a module that has already loaded.
    """
    program = f"""
import importlib.abc, importlib.util, sys
from pathlib import Path

# The repo's lib on the path, so the sibling modules resolve from the tree under
# test. Without this the subprocess finds them only through `~/.local/lib`, which
# exists on the operator's machines and on no CI runner — the first version of this test
# passed locally and failed in CI for exactly that reason.
sys.path.insert(0, {str(LIB)!r})

class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "picker_render":
            raise {raising}
        return None

sys.meta_path.insert(0, Refuse())
spec = importlib.util.spec_from_file_location(
    "session_state", {str(LIB / "session_state.py")!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.PR is None, "the reader should be absent, not half-present"
print("imported")
"""
    return subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)


@pytest.mark.parametrize("raising, why", [
    ('ImportError("no such module, as an unlinked one behaves")',
     "the outage: a new module the symlink farm has never seen"),
    ('RuntimeError("a half-installed module failing in its own imports")',
     "the reason the catch is Exception and not ImportError"),
    ('SyntaxError("a module broken by an edit")',
     "not an ImportError subclass, and identically fatal at module scope"),
])
def test_session_state_imports_with_no_picker_reader(raising, why):
    """THE OUTAGE ITSELF. A blocked import must not stop the module loading.

    PARAMETRIZED BECAUSE THE CATCH IS BROAD AND SAID SO WITHOUT PROVING IT. The
    first version tested only the missing-module case while the code's comment
    defended `except Exception` over `except ImportError` on the strength of the
    other two. A comment is not a test.
    """
    done = _import_with_reader_refused(raising)
    assert done.returncode == 0, (
        f"session_state did not import — {why}:\n{done.stderr}")
    assert "imported" in done.stdout


def test_the_swallowed_import_failure_is_still_reported():
    """Silent to the caller, loud in the log.

    Absence is a legitimate answer, so it must not raise. But a `picker_render`
    broken by a future edit would otherwise disable the feature with nothing
    anywhere saying why. On stderr, because stdout is a JSON contract.
    """
    done = _import_with_reader_refused('RuntimeError("broken on purpose")')
    assert done.returncode == 0
    assert "picker_render unavailable" in done.stderr
    assert "broken on purpose" in done.stderr, "the reason should survive to the log"
    assert "picker_render" not in done.stdout, "stdout is a JSON contract"


def test_a_preloaded_stale_signal_client_is_evicted_not_used():
    """The sys.path insert cannot override a signal_client already in sys.modules.

    If a sibling imported the deployed farm's copy before session_state loads,
    the `from signal_client import sanitise` would take that possibly-stale copy
    and raise (a pre-sanitise farm has no `sanitise`). The eviction guard removes
    the cached copy so the from-import loads THIS tree's. Not a production path
    today, but the seam is one future module-scope import away — and a
    silently-stale module would be worse than an ImportError.
    """
    program = f"""
import importlib.util, sys, types

# A stale farm copy, pre-imported ahead of session_state — no sanitise.
fake = types.ModuleType("signal_client")
sys.modules["signal_client"] = fake

spec = importlib.util.spec_from_file_location(
    "session_state", {str(LIB / "session_state.py")!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import signal_client
assert signal_client is not fake, "the preloaded stale copy was used, not evicted"
assert callable(signal_client.sanitise), "sanitise should resolve from this tree"
print("imported")
"""
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, f"session_state did not import:\n{done.stderr}"
    assert "imported" in done.stdout


def _blocked_on_a_question(SS, monkeypatch, tmp_path):
    """Stub the world down to one session blocked on an asking-class gate.

    That shape is what reaches the reader: a full read, `blocked`, `kind:
    question`. Anything else skips the block and would pass these tests without
    exercising them.
    """
    monkeypatch.setattr(SS.TT, "claude_pane", lambda n: ("%1", 4242))
    monkeypatch.setattr(SS.TT, "meta_for_pid", lambda pid, days=3: {
        "session_id": "aaaa", "cwd": "/x", "transcript_path": str(tmp_path / "t.jsonl")})
    monkeypatch.setattr(SS, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(SS, "state_from_events", lambda events: {
        "state": "blocked",
        "gate": {"kind": "question", "tool": "AskUserQuestion",
                 "questions": [{"question": "Which?", "options": []}]},
    })
    log = tmp_path / "hooks.jsonl"
    log.write_text('{"hook_event_name": "Notification"}\n', encoding="utf-8")
    monkeypatch.setattr(SS, "_hook_logs", lambda sid, cwd=None, days=3: [log])


def test_a_gate_survives_a_missing_reader(SS, monkeypatch, tmp_path):
    """WHAT THE OUTAGE COST. The gate is what Deck renders, so it must not depend
    on the scrape that decorates it.

    `rendered` absent is the documented degradation. A state that cannot be
    produced at all is a pane with nothing to draw, which is what the operator saw.
    """
    _blocked_on_a_question(SS, monkeypatch, tmp_path)
    monkeypatch.setattr(SS, "PR", None)

    got = SS.session_state("deck")
    assert got["state"] == "blocked"
    assert got["gate"]["kind"] == "question"
    assert "rendered" not in got["gate"], "no reader means no rendered block"


def test_the_reader_is_still_consulted_when_it_is_present(SS, monkeypatch, tmp_path):
    """The guard must not have turned the feature off for everyone.

    A degradation test passes just as well against code that never reads the
    picker at all, so the working path needs its own assertion. This is the test
    that fails if `PR is not None` is ever mistyped into something always false.
    """
    _blocked_on_a_question(SS, monkeypatch, tmp_path)

    class Fake:
        calls: list[str] = []

        @staticmethod
        def read(name):
            Fake.calls.append(name)
            return {"questionIndex": 1, "claudeVersion": "2.1.220"}

    monkeypatch.setattr(SS, "PR", Fake)

    got = SS.session_state("deck")
    assert Fake.calls == ["deck"], "the reader was not consulted"
    assert got["gate"]["rendered"]["questionIndex"] == 1


def test_a_reader_that_refuses_says_so_in_the_state(SS, monkeypatch, tmp_path):
    """WHY THE FIELD IS MISSING, which is the half that was absent until now.

    `rendered` was gone for ten days because the version constant had gone stale,
    and the degradation was correct and silent in exactly the way it is when a
    session has no picker up. A consumer could not tell "nothing to show" from "I
    cannot read this picker", so nobody did, and the feature sat dark until
    someone went looking. `renderedDark` is the difference.
    """
    _blocked_on_a_question(SS, monkeypatch, tmp_path)

    class Dark:
        @staticmethod
        def read_detail(name):
            return None, {"reason": "markers-moved", "paneVersion": "2.9.0",
                          "floor": "2.1.220"}

    monkeypatch.setattr(SS, "PR", Dark)

    got = SS.session_state("deck")
    assert "rendered" not in got["gate"]
    assert got["gate"]["renderedDark"]["reason"] == "markers-moved"
    assert got["gate"]["renderedDark"]["paneVersion"] == "2.9.0"


def test_a_reader_with_no_picker_to_read_is_not_reported_dark(SS, monkeypatch, tmp_path):
    """Both keys absent is the ordinary answer for a question already answered.

    Marking that as darkness would put a "cannot read this picker" marker on the
    pane every time a gate resolved between the hook read and the capture.
    """
    _blocked_on_a_question(SS, monkeypatch, tmp_path)

    class Quiet:
        @staticmethod
        def read_detail(name):
            return None, None

    monkeypatch.setattr(SS, "PR", Quiet)

    got = SS.session_state("deck")
    assert "rendered" not in got["gate"]
    assert "renderedDark" not in got["gate"]
