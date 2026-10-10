"""tcc_prompt_watch — TCC permission-prompt alert (issue_01M3Q1VHWARM4NKYRH44D0MYX5).

Fixtures below are real `tccd` lines, read from macstudio's own unified log
(`/usr/bin/log show --predicate 'process == "tccd"'` and `log stream --style
ndjson` for the JSON envelope shape) — not synthesized from Apple's
documentation, which does not cover this internal wording. Each fixture's
docstring/comment cites the exact timestamp it was read at.

THE ONE EXCEPTION: `LINE_INDIRECT_TMUX_QUOTED_BY_ISSUE` below reproduces the
`... iCloud Drive by tmux` wording the issue quotes for 2026-09-28 23:13 MST.
That exact line could not be found in macstudio's current unified log — checked
with `log show` over the full day and a narrow window around 23:13, with and
without --info/--debug; the only `Prompting for access` line anywhere in the
retained log is the 2026-09-29 09:24:23 python3.14 one used below. It is kept
as a test case because the parser must handle "by tmux" as well as "by
python3.14", but it is built from the issue's quoted wording, not an
independently observed log line.
"""
from __future__ import annotations

import json

import tcc_prompt_watch as w


def _ndjson(event_message: str, timestamp: str = "2026-09-29 09:24:23.657285-0700") -> str:
    """A minimal but real-shaped `log stream --style ndjson` line — same key
    set `log stream --style ndjson --predicate 'process == "tccd"'` emits,
    trimmed to what this module reads (`eventMessage`) plus enough context to
    look like the real thing."""
    return json.dumps({
        "timestamp": timestamp,
        "eventMessage": event_message,
        "processImagePath": "/System/Library/PrivateFrameworks/TCC.framework/Support/tccd",
        "subsystem": "com.apple.TCC",
        "category": "access",
        "processID": 429,
    })


# --- real lines, `log show --predicate 'process == "tccd"'` -------------------

# 2026-09-29 09:24:23.657285-0700, confirmed present (positive control for the
# "Prompting for access" family — this is the ONLY such line found anywhere in
# macstudio's retained log for the 2026-09-28..09-29 range searched).
LINE_INDIRECT_ICLOUD_PYTHON = (
    "Prompting for access to indirect object iCloud Drive by python3.14"
)

# Built from the issue's quoted wording for 2026-09-28 23:13 MST — see module
# docstring: this exact line was searched for and not found in the retained log.
LINE_INDIRECT_TMUX_QUOTED_BY_ISSUE = (
    "Prompting for access to indirect object iCloud Drive by tmux"
)

# 2026-09-29 09:14:45.229649-0700, confirmed present — the Documents-folder gap
# the issue named (2026-09-28's Documents prompt did not show under "Prompting
# for access").
LINE_AUTHREQ_DOCUMENTS = (
    "AUTHREQ_PROMPTING: msgID=170.124, service=kTCCServiceSystemPolicyDocumentsFolder, "
    "subject=Sub:{/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/bin/python3.14}Resp:{TCCDProcess: identifier="
    "python3-55554944764cf091575036e79c674908301b2094, pid=95525, auid=501, euid=501, "
    "responsible_path=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/bin/python3.14, "
    "binary_path=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/Resources/Python.app/Contents/MacOS/Python},"
)

# 2026-09-29 09:14:41.279735-0700, confirmed present — the Photos gap the issue named.
LINE_AUTHREQ_PHOTOS = (
    "AUTHREQ_PROMPTING: msgID=170.107, service=kTCCServicePhotos, "
    "subject=Sub:{/opt/homebrew/Cellar/tmux/3.7c/bin/tmux}Resp:{TCCDProcess: identifier="
    "tmux-55554944c42e6d898c003e91bcaf5080491b0274, pid=84946, auid=501, euid=501, "
    "responsible_path=/opt/homebrew/Cellar/tmux/3.7c/bin/tmux, "
    "binary_path=/srv/convivy/.local/share/claude/versions/2.1.284},"
)

# 2026-09-29 02:41:58.801604-0700, confirmed present.
LINE_AUTHREQ_DESKTOP = (
    "AUTHREQ_PROMPTING: msgID=170.784, service=kTCCServiceSystemPolicyDesktopFolder, "
    "subject=Sub:{/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/bin/python3.14}Resp:{TCCDProcess: identifier="
    "python3-55554944764cf091575036e79c674908301b2094, pid=80070, auid=501, euid=501, "
    "responsible_path=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/bin/python3.14, "
    "binary_path=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
    "Versions/3.14/Resources/Python.app/Contents/MacOS/Python},"
)

# 2026-09-29 09:14:43.946988-0700, confirmed present.
LINE_AUTHREQ_NETWORK_VOLUMES = (
    "AUTHREQ_PROMPTING: msgID=170.110, service=kTCCServiceSystemPolicyNetworkVolumes, "
    "subject=Sub:{/opt/homebrew/Cellar/tmux/3.7c/bin/tmux}Resp:{TCCDProcess: identifier="
    "tmux-55554944c42e6d898c003e91bcaf5080491b0274, pid=84946, auid=501, euid=501, "
    "responsible_path=/opt/homebrew/Cellar/tmux/3.7c/bin/tmux, "
    "binary_path=/srv/convivy/.local/share/claude/versions/2.1.284},"
)

# A non-prompt line from the same window, to prove the parser doesn't fire on
# ordinary tccd chatter that merely mentions a service or a process.
LINE_GRANT_NOT_A_PROMPT = (
    "Granting TCCDProcess: identifier=com.apple.icloudwebd, pid=45533, auid=501, "
    "euid=501, binary_path=/usr/libexec/icloudwebd access to kTCCServicePhotos "
    "via entitlement 'com.apple.private.tcc.allow'"
)


# --- parse_prompt: the indirect-object family ---------------------------------


def test_parses_the_confirmed_icloud_drive_python_line():
    assert w.parse_prompt(LINE_INDIRECT_ICLOUD_PYTHON) == ("python3.14", "iCloud Drive")


def test_parses_the_issue_quoted_tmux_line():
    assert w.parse_prompt(LINE_INDIRECT_TMUX_QUOTED_BY_ISSUE) == ("tmux", "iCloud Drive")


# --- parse_prompt: the AUTHREQ_PROMPTING family, the Documents/Photos gap -----


def test_parses_authreq_prompting_documents_folder():
    assert w.parse_prompt(LINE_AUTHREQ_DOCUMENTS) == ("python3.14", "System Policy Documents Folder")


def test_parses_authreq_prompting_photos():
    assert w.parse_prompt(LINE_AUTHREQ_PHOTOS) == ("tmux", "Photos")


def test_parses_authreq_prompting_desktop_folder():
    assert w.parse_prompt(LINE_AUTHREQ_DESKTOP) == ("python3.14", "System Policy Desktop Folder")


def test_parses_authreq_prompting_network_volumes():
    assert w.parse_prompt(LINE_AUTHREQ_NETWORK_VOLUMES) == ("tmux", "System Policy Network Volumes")


def test_ignores_a_grant_line_that_is_not_a_prompt():
    assert w.parse_prompt(LINE_GRANT_NOT_A_PROMPT) is None


def test_ignores_unrelated_text():
    assert w.parse_prompt("tccd: some other line entirely") is None


# --- extract_event_message: the ndjson envelope --------------------------------


def test_extract_event_message_reads_the_field():
    line = _ndjson(LINE_INDIRECT_ICLOUD_PYTHON)
    assert w.extract_event_message(line) == LINE_INDIRECT_ICLOUD_PYTHON


def test_extract_event_message_ignores_the_stream_startup_banner():
    assert w.extract_event_message('Filtering the log data using "process == \\"tccd\\""') is None


def test_extract_event_message_ignores_blank_lines():
    assert w.extract_event_message("") is None
    assert w.extract_event_message("   \n") is None


def test_extract_event_message_ignores_malformed_json():
    assert w.extract_event_message("{not json") is None


def test_extract_event_message_handles_a_missing_field():
    assert w.extract_event_message(json.dumps({"timestamp": "x"})) is None


# --- process_line: envelope + parse + dedup + push, end to end -----------------


class _Pusher:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind: str, message: str, **fields) -> bool:
        self.calls.append((kind, message))
        return True


def test_process_line_pushes_on_a_real_prompt():
    pusher = _Pusher()
    deduper = w.Deduper()
    pushed = w.process_line(_ndjson(LINE_AUTHREQ_PHOTOS), deduper, now=0.0, pusher=pusher)
    assert pushed is True
    assert len(pusher.calls) == 1
    kind, message = pusher.calls[0]
    assert kind == "tcc-prompt"
    assert "Photos" in message
    assert "tmux" in message


def test_process_line_does_not_push_on_ordinary_chatter():
    pusher = _Pusher()
    deduper = w.Deduper()
    pushed = w.process_line(_ndjson(LINE_GRANT_NOT_A_PROMPT), deduper, now=0.0, pusher=pusher)
    assert pushed is False
    assert pusher.calls == []


def test_process_line_does_not_push_on_a_non_json_line():
    pusher = _Pusher()
    deduper = w.Deduper()
    pushed = w.process_line("Filtering the log data...", deduper, now=0.0, pusher=pusher)
    assert pushed is False
    assert pusher.calls == []


# --- Deduper: one push per (process, item) per window ---------------------------


def test_deduper_suppresses_a_repeat_within_the_window():
    d = w.Deduper(window_seconds=300.0)
    assert d.should_push("tmux", "iCloud Drive", now=1000.0) is True
    assert d.should_push("tmux", "iCloud Drive", now=1010.0) is False  # 10s later, same prompt
    assert d.should_push("tmux", "iCloud Drive", now=1200.0) is False  # 200s later, still inside


def test_deduper_allows_after_the_window_elapses():
    d = w.Deduper(window_seconds=300.0)
    assert d.should_push("tmux", "iCloud Drive", now=1000.0) is True
    assert d.should_push("tmux", "iCloud Drive", now=1301.0) is True  # 301s later, window has passed


def test_deduper_treats_different_pairs_independently():
    d = w.Deduper(window_seconds=300.0)
    assert d.should_push("tmux", "iCloud Drive", now=1000.0) is True
    assert d.should_push("python3.14", "iCloud Drive", now=1000.0) is True
    assert d.should_push("tmux", "Photos", now=1000.0) is True


def test_process_line_end_to_end_dedup_suppresses_the_second_push():
    pusher = _Pusher()
    deduper = w.Deduper(window_seconds=300.0)
    line = _ndjson(LINE_AUTHREQ_PHOTOS)
    assert w.process_line(line, deduper, now=0.0, pusher=pusher) is True
    assert w.process_line(line, deduper, now=5.0, pusher=pusher) is False
    assert len(pusher.calls) == 1


# --- run: restarts the stream on exit -------------------------------------------


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.pid = 12345

    def wait(self):
        return self.returncode


def test_run_restarts_the_stream_when_it_exits():
    """Each `popen()` call gets one prompt line, then its fake stdout ends (as
    a real `log stream` process exiting would). `run` must spawn again rather
    than returning — that's the whole point of a watchdog for a stream that
    can die."""
    calls: list[int] = []

    def fake_popen(cmd, **kwargs):
        calls.append(1)
        return _FakeProc([_ndjson(LINE_AUTHREQ_PHOTOS)])

    sleeps: list[float] = []
    pusher = _Pusher()

    w.run(popen=fake_popen, sleep=sleeps.append, pusher=pusher, max_restarts=3)

    assert len(calls) == 3
    # No sleep after the run's final (capped) attempt — nothing left to wait for.
    assert len(sleeps) == 2
    assert all(s == w.RESTART_DELAY_SECONDS for s in sleeps)


def test_run_dedups_across_a_stream_restart():
    """The Deduper `run` builds lives for the whole call, not per subprocess —
    a `log stream` hiccup that triggers a restart must not re-alert for a
    prompt already pushed moments earlier. Each restart here replays the exact
    same line; only the first should produce a push."""
    def fake_popen(cmd, **kwargs):
        return _FakeProc([_ndjson(LINE_AUTHREQ_PHOTOS)])

    pusher = _Pusher()
    w.run(popen=fake_popen, sleep=lambda s: None, pusher=pusher, max_restarts=3)

    assert len(pusher.calls) == 1


def test_run_survives_popen_failing_to_start():
    attempts: list[int] = []

    def flaky_popen(cmd, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no such file")
        return _FakeProc([])

    sleeps: list[float] = []
    w.run(popen=flaky_popen, sleep=sleeps.append, pusher=_Pusher(), max_restarts=2)

    assert len(attempts) == 2
    assert len(sleeps) == 1  # no sleep after the final (capped) attempt


def test_run_does_not_crash_on_a_bad_line():
    """One malformed or unexpectedly-shaped line must not take down the whole
    stream loop — the next line still gets a chance."""
    good = _ndjson(LINE_AUTHREQ_PHOTOS)
    bad = "{" + "not json"

    def fake_popen(cmd, **kwargs):
        return _FakeProc([bad, good])

    pusher = _Pusher()
    w.run(popen=fake_popen, sleep=lambda s: None, pusher=pusher, max_restarts=1)

    assert len(pusher.calls) == 1


# --- push_alert / notify.push wiring --------------------------------------------


def test_push_alert_names_process_and_item_in_the_message():
    pusher = _Pusher()
    ok = w.push_alert("tmux", "iCloud Drive", pusher=pusher)
    assert ok is True
    kind, message = pusher.calls[0]
    assert kind == "tcc-prompt"
    assert "tmux" in message
    assert "iCloud Drive" in message
    assert "Screen Sharing" in message


def test_push_alert_reports_false_on_a_failed_push():
    def failing_pusher(kind, message, **fields):
        return False

    assert w.push_alert("tmux", "iCloud Drive", pusher=failing_pusher) is False
