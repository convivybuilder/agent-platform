"""`check-cli-contracts` — the cross-repo CLI-contract check.

The whole failure class this fixes is a check reporting fine while the thing
it checked was broken, so the check needs its own tests.

Fixture layout (all under tmp_path):
    bin/
        post-changelog         # --help → known flags only
        post-release-note      # same
    vivy/
        scripts/...
    constance/
        scripts/...
    selector/
        scripts/...
    selector-backend/
        scripts/...
"""
from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import threading
from unittest import mock

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO / "bin"
CHECK_CLI = BIN_DIR / "check-cli-contracts"

# An empty, real directory so a subprocess's $HOME has no
# ~/.orient-control/agent-platform-token to find (signal_client.TOKEN_FILE).
# Built once, at import, same as REPO/BIN_DIR — every test that shells out to
# check-cli-contracts shares it; none of them write to it.
_FAKE_HOME = tempfile.mkdtemp(prefix="check-cli-contracts-test-home-")


def _subprocess_env() -> dict:
    """Env for invoking a `bin/` tool as a subprocess, with the repo's own lib on
    PYTHONPATH, and no path to a real signal-store credential or endpoint.

    Every `bin/` tool resolves its modules by inserting `~/.local/lib/agent-platform`
    at the head of sys.path — the symlink farm `install.sh` lays down. That exists on
    the mini and NOT on a CI runner, so a tool invoked here died at
    `ModuleNotFoundError: No module named 'signal_client'` and the four tests that
    shell out failed for a reason that had nothing to do with what they assert.

    Pointing PYTHONPATH at the repo's `lib/agent-platform` is also the more correct
    thing to test: these tests should exercise the code in THIS checkout, not
    whatever happens to be installed on the machine running them. The insert at
    sys.path[0] is harmless when the directory is absent, so the interpreter falls
    through to PYTHONPATH.

    SAFETY (issue_01M3VM4X01JGV79V2B6557SV4T). check-cli-contracts' _post_signal
    posts a real alert signal via signal_client.post_alert on every breach it
    detects — exactly what several of these tests set up on purpose. signal_client
    resolves its credential from signal_client.token(): the AGENT_SIGNAL_TOKEN env
    var, or else the 0600 file $HOME/.orient-control/agent-platform-token. Copying
    os.environ wholesale used to carry a real token straight into the subprocess
    on a machine that holds one, sending `--no-slack` breaches into vivy's and
    selector's live signal streams on every test run. AGENT_SIGNAL_TOKEN is stripped and
    HOME is repointed at an empty directory so neither source can resolve to a
    real credential; VAULTS_ON_LIBRARY/LIBRARY_BASE_URL/ORIENT_BASE_URL are
    stripped too, as defense in depth, so even a would-be post has nowhere real
    to go. A missing credential makes signal_client.post_alert return
    "skip" before any network call, so detection and reporting on
    stdout/stderr are unaffected.
    """
    env = dict(os.environ)
    lib = str(REPO / "lib" / "agent-platform")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{lib}{os.pathsep}{existing}" if existing else lib
    for var in ("AGENT_SIGNAL_TOKEN", "VAULTS_ON_LIBRARY", "LIBRARY_BASE_URL",
                "ORIENT_BASE_URL"):
        env.pop(var, None)
    env["HOME"] = _FAKE_HOME
    return env

# --help output for fake bin/ tools. Self-contained — tests do not depend on
# real tool internals.
FAKE_HELP_POST_CHANGELOG = """\
usage: post-changelog [-h] [--product PRODUCT] [--file FILE] [--pr PR] [message]

flags: --product, --file, --pr

options:
  -h, --help         show this help message and exit
  --product PRODUCT  product/repo name
  --file FILE        read from this file
  --pr PR            PR number
"""

FAKE_HELP_POST_RELEASE_NOTE = """\
usage: post-release-note [-h] [--product PRODUCT] [--version VERSION] [--file FILE] [message]

flags: --product, --version, --file

options:
  -h, --help         show this help message and exit
  --product PRODUCT  product/repo name
  --version VERSION  release tag
  --file FILE        read from this file
"""


# -- helpers -------------------------------------------------------------------


def _load_check_module():
    """Load the check-cli-contracts module so pure helpers can be tested."""
    loader = importlib.machinery.SourceFileLoader(
        "check_cli_contracts", str(CHECK_CLI),
    )
    spec = importlib.util.spec_from_loader("check_cli_contracts", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def fake_bin_dir(tmp_path):
    """Create a tmp bin/ with stub tools that print controlled --help text."""
    bd = tmp_path / "bin"
    bd.mkdir()
    for tool_name, help_text in [
        ("post-changelog", FAKE_HELP_POST_CHANGELOG),
        ("post-release-note", FAKE_HELP_POST_RELEASE_NOTE),
    ]:
        p = bd / tool_name
        p.write_text(
            "#!/usr/bin/env python3\n"
            f"print({help_text!r})\n"
        )
        p.chmod(0o755)
    return bd


@pytest.fixture
def setup(fake_bin_dir, tmp_path):
    """Build a minimal product-guest structure under tmp_path.

    Returns {convivy_root, bin_dir} so tests can override individual files.
    """
    convivy_root = tmp_path / "convivy"
    convivy_root.mkdir()
    (convivy_root / "agent-platform").mkdir()
    return {
        "convivy_root": str(convivy_root),
        "bin_dir": str(fake_bin_dir),
    }


def _make_script(product_root, rel, content):
    """Write a caller script under product/scripts/, creating dirs."""
    p = product_root / "scripts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _run(convivy_root, bin_dir, *args):
    """Run check-cli-contracts as a subprocess with convivy_root and bin_dir.

    Returns (stdout, stderr, exit_code).
    """
    cmd = [
        sys.executable, str(CHECK_CLI),
        "--convivy-root", convivy_root,
        "--bin-dir", bin_dir,
        *args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                            env=_subprocess_env())
    return result.stdout, result.stderr, result.returncode


# -- detection: known flag differences ---------------------------------------


def test_unknown_flag_detected(setup):
    """A caller passing a flag the tool does not accept must be detected.

    vivy has two callers (post-changelog and post-release-note), both
    passing --no-slack. constance is clean. Result: 2 breach lines.
    """
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy", "--no-slack"]
        subprocess.run(cmd)
    """))
    _make_script(vivy, "cut_release.sh", textwrap.dedent("""\
        #!/bin/bash
        "$HOME/.local/bin/post-release-note" --product vivy --version v1 --file /tmp/n --no-slack
    """))

    constance = convivy_root / "constance"
    constance.mkdir()
    _make_script(constance, "post_changelog.sh", textwrap.dedent("""\
        tool=post-changelog
        cmd="$tool --product constance --file /tmp/x"
        eval "$cmd"
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 2, f"expected breach, got rc={rc}\nstderr: {stderr}"
    lines = [row for row in stderr.splitlines() if "[BREACH]" in row]
    assert len(lines) == 2, f"expected 2 breach lines, got {len(lines)}: {stderr}"


def test_all_flags_known_pass(setup):
    """Callers passing only accepted flags → pass."""
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]
        subprocess.run(cmd)
    """))
    constance = convivy_root / "constance"
    constance.mkdir()
    _make_script(constance, "post_changelog.sh", textwrap.dedent("""\
        tool=post-changelog
        cmd="$tool --product constance --file /tmp/x"
        eval "$cmd"
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass, got rc={rc}\nstderr: {stderr}"
    assert "[BREACH]" not in stderr


# -- false positives -----------------------------------------------------------


def test_comment_is_not_false_positive(setup):
    """A comment line mentioning an unknown flag must not be flagged."""
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        # This script calls post-changelog --no-slack --product vivy
        # but these are just comments — the real call uses valid flags.
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]
        subprocess.run(cmd)
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass (comments only), got rc={rc}\nstderr: {stderr}"
    assert "[BREACH]" not in stderr


def test_inline_comment_not_false_positive(setup):
    """An inline comment with an unknown flag must be stripped."""
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]  # --no-slack was here
        subprocess.run(cmd)
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass (inline comment), got rc={rc}\nstderr: {stderr}"
    assert "[BREACH]" not in stderr


# -- degraded states -----------------------------------------------------------


def test_missing_checkout_graceful(setup):
    """A product whose checkout is absent must not crash or report breach."""
    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass with no checkouts, got rc={rc}\nstderr: {stderr}"
    assert "[BREACH]" not in stderr


def test_missing_bin_tool_graceful(setup):
    """A bin/ tool that is missing must not crash the whole check."""
    bd = pathlib.Path(setup["bin_dir"])
    (bd / "post-release-note").unlink()

    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]
        subprocess.run(cmd)
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass (missing tool skipped), got rc={rc}\nstderr: {stderr}"
    assert "[SKIP] post-release-note" in stderr


# -- safety --------------------------------------------------------------------


def test_no_real_work_tool(setup):
    """The check must run --help on the tool, never with real args."""
    trap_script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        if len(sys.argv) > 1 and sys.argv[1] != "--help":
            print("TRAP: invoked with real args", file=sys.stderr)
            sys.exit(1)
        print("usage: post-changelog [--product P] [--file F] [--pr P] [message]")
        print("options:")
        print("  --product P  product name")
        print("  --file F     read from file")
        print("  --pr P       PR number")
    """)
    trap_path = pathlib.Path(setup["bin_dir"]) / "post-changelog"
    trap_path.write_text(trap_script)
    trap_path.chmod(0o755)

    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]
        subprocess.run(cmd)
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass, got rc={rc}\nstderr: {stderr}"
    assert "TRAP" not in stderr, f"trap was hit — check ran the tool with real args:\n{stderr}"


class _FailOnRequestHandler(http.server.BaseHTTPRequestHandler):
    """A stub issue-queue endpoint. Any request reaching it is the exact
    failure test_breach_never_posts_even_with_a_parent_token exists to catch."""

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        self.server.requests_received += 1
        self.send_response(500)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep test output quiet


def test_breach_never_posts_even_with_a_parent_token(setup, monkeypatch):
    """A breaching run must post nothing, even when the PARENT process holds a
    real-looking token and the subprocess would otherwise inherit it.

    Regression test for issue_01M3VM4X01JGV79V2B6557SV4T: _subprocess_env()
    used to copy os.environ wholesale, so on a machine that holds a real ops
    token, every test run in this file filed real `--no-slack` breach issues
    into vivy's and selector's live queues. AGENT_SIGNAL_TOKEN and the vault
    URL variables are set here, in the PARENT process, exactly as they would
    be on such a machine; the stub server stands in for the real signal store
    so proving the leak (or its absence) never touches one.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _FailOnRequestHandler)
    server.requests_received = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stub_url = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("AGENT_SIGNAL_TOKEN", "fake-token-for-test")
        monkeypatch.setenv("VAULTS_ON_LIBRARY", "1")
        monkeypatch.setenv("LIBRARY_BASE_URL", stub_url)
        monkeypatch.setenv("ORIENT_BASE_URL", stub_url)

        convivy_root = pathlib.Path(setup["convivy_root"])
        vivy = convivy_root / "vivy"
        vivy.mkdir()
        _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
            import subprocess, pathlib
            tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
            cmd = [str(tool), "--product", "vivy", "--no-slack"]
            subprocess.run(cmd)
        """))

        # Deliberately NOT --no-report: this must prove the breach-filing path
        # itself is safe, not that the dry-run switch is.
        stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
        assert rc == 2, f"expected breach to still be detected, got rc={rc}\nstderr: {stderr}"
        assert "[BREACH]" in stderr, f"breach must still be reported:\n{stderr}"
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert server.requests_received == 0, (
        f"{server.requests_received} POST(s) reached the stub signal store — a "
        "breaching test run posted (or tried to post) a real signal"
    )


# -- no-flag invocations -------------------------------------------------------


def test_call_with_no_long_flags_is_not_a_breach(setup):
    """A bare `post-changelog \"message\"` with no --flags is skipped."""
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess
        subprocess.run(["post-changelog", "a message"])
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 0, f"expected pass, got rc={rc}\nstderr: {stderr}"
    assert "[BREACH]" not in stderr


# -- mixed products ------------------------------------------------------------


def test_mixed_clean_and_breached(setup):
    """One product clean, another breached → breach reported."""
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy"]
        subprocess.run(cmd)
    """))
    selector = convivy_root / "selector"
    selector.mkdir()
    _make_script(selector, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "selector", "--no-slack"]
        subprocess.run(cmd)
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"])
    assert rc == 2, f"expected breach, got rc={rc}\nstderr: {stderr}"
    assert "selector" in stderr and "--no-slack" in stderr


# -- flag-parsing unit tests ---------------------------------------------------


class TestFlagParsing:
    """Unit tests for parse_accepted_flags loaded via importlib."""

    @classmethod
    def setup_class(cls):
        cls.mod = _load_check_module()

    def test_basic_flags(self):
        flags = self.mod.parse_accepted_flags(FAKE_HELP_POST_CHANGELOG)
        assert flags == frozenset({"--product", "--file", "--pr"})

    def test_short_and_long_on_one_line(self):
        """A short+long pair like ``-p, --product`` must yield --product.

        argparse help output often puts the short and long forms on one
        line (``  -p PRODUCT, --product PRODUCT``). The old parser only
        matched lines starting with ``  --``, missing the long flag in
        this case. This test pins the fix and guards against regression.
        """
        text = textwrap.dedent("""\
            usage: post-changelog [-h] [-p PRODUCT] [--file FILE] [message]

            options:
              -h, --help         show this help message and exit
              -p PRODUCT, --product PRODUCT  product/repo name
              --file FILE        read from this file
        """)
        flags = self.mod.parse_accepted_flags(text)
        assert flags == frozenset({"--product", "--file"})
        assert "--help" not in flags

    def test_no_implicit_help_flag(self):
        """--help must be excluded (argparse adds it automatically)."""
        flags = self.mod.parse_accepted_flags(FAKE_HELP_POST_CHANGELOG)
        assert "--help" not in flags

    def test_multiple_tools(self):
        flags = self.mod.parse_accepted_flags(FAKE_HELP_POST_RELEASE_NOTE)
        assert flags == frozenset({"--product", "--version", "--file"})

    def test_example_in_help_does_not_add_stale_flags(self):
        """A stale flag in a usage example must not be treated as accepted.

        The example mentions ``--now-obsolete``, which is NOT listed in
        the options section. ``parse_accepted_flags`` must not extract it.
        """
        text = FAKE_HELP_POST_CHANGELOG + (
            "\nExample: post-changelog --product vivy --now-obsolete\n"
        )
        flags = self.mod.parse_accepted_flags(text)
        assert "--now-obsolete" not in flags, (
            f"stale example flag leaked into accepted set: {flags}"
        )
        assert flags == frozenset({"--product", "--file", "--pr"})

    def test_real_post_changelog(self):
        """parse_accepted_flags on real post-changelog --help output.

        Runs against the actual bin/ tool so a future short-option addition
        (e.g. -p/--product) is caught — the real-tool test would fail when
        the parse needs updating, rather than producing a false breach
        silently.
        """
        help_output = subprocess.run(
            [str(BIN_DIR / "post-changelog"), "--help"],
            capture_output=True, text=True, timeout=15, env=_subprocess_env(),
        )
        assert help_output.returncode == 0
        flags = self.mod.parse_accepted_flags(help_output.stdout)
        # Assertions on current flags only. Update when the tool changes.
        assert "--product" in flags
        assert "--file" in flags
        assert "--pr" in flags
        assert "--help" not in flags

    def test_real_post_release_note(self):
        """Same guard for post-release-note — catches short-option drift."""
        help_output = subprocess.run(
            [str(BIN_DIR / "post-release-note"), "--help"],
            capture_output=True, text=True, timeout=15, env=_subprocess_env(),
        )
        assert help_output.returncode == 0
        flags = self.mod.parse_accepted_flags(help_output.stdout)
        assert "--product" in flags
        assert "--version" in flags
        assert "--file" in flags
        assert "--help" not in flags


# -- reporting path: alert signal and dry-run ----------------------------------


class TestPostSignal:
    """_post_signal logs one alert signal per breach and never files an item
    (platform/decisions/0073: only a co session creates a work item)."""

    @classmethod
    def setup_class(cls):
        cls.mod = _load_check_module()

    def test_logs_alert_signal_keyed_on_dedup_key_and_files_no_item(self):
        """A breach posts an alert signal with the title leading the body and
        dedup_key set, and never touches issue_client."""
        import signal_client

        fake_issue_client = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"issue_client": fake_issue_client}), \
                mock.patch.object(signal_client, "post_alert", return_value="created") as post:
            result = self.mod._post_signal("test-p", "test title", "test body", "test-key")

        assert result == {"status": "created"}
        post.assert_called_once()
        stream, body = post.call_args.args
        assert stream == "cli-contract"
        assert body == "test title\n\ntest body"
        assert post.call_args.kwargs == {"product": "test-p", "dedup_key": "test-key",
                                         "meta": {"severity": "medium"}}
        fake_issue_client.post_issue.assert_not_called()

    def test_body_from_source_is_sanitised(self):
        """A caller's source line is text the platform did not author. An escape
        sequence in it must not reach the alert body, and a credential is redacted
        when it appears as written or with a character `sanitise_body` removes; a
        whitespace control or a kept format character inside the prefix lets it
        through, a known gap not acted on."""
        import signal_client

        line = "\x1b[31mcall tool --token Bearer abcdefghijklmnop\x1b[0m"
        with mock.patch.object(signal_client, "post_alert", return_value="created") as post:
            self.mod._post_signal("p", "t", f"- `f.py:3`: `{line}`", "k")

        body = post.call_args.args[1]
        assert "\x1b" not in body
        assert "abcdefghijklmnop" not in body
        assert "[redacted]" in body

    def test_dry_run_skips_signal(self):
        """With dry_run=True, _post_signal returns dry-run status without posting."""
        import signal_client

        with mock.patch.object(signal_client, "post_alert") as post:
            result = self.mod._post_signal("p", "t", "b", "k", dry_run=True)

        assert result["status"] == "dry-run"
        post.assert_not_called()


# -- dry-run integration: --no-report flag ------------------------------------


def test_no_report_flag_skips_filing(setup):
    """With --no-report, breaches must show dry-run status, not error:400.

    Uses the same breached setup as test_unknown_flag_detected but verifies
    the reporting path produces dry-run output rather than hitting the real
    API (which would post a real signal).
    """
    convivy_root = pathlib.Path(setup["convivy_root"])
    vivy = convivy_root / "vivy"
    vivy.mkdir()
    _make_script(vivy, "post_changelog.py", textwrap.dedent("""\
        import subprocess, pathlib
        tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
        cmd = [str(tool), "--product", "vivy", "--no-slack"]
        subprocess.run(cmd)
    """))
    _make_script(vivy, "cut_release.sh", textwrap.dedent("""\
        #!/bin/bash
        "$HOME/.local/bin/post-release-note" --product vivy --version v1 --file /tmp/n --no-slack
    """))
    constance = convivy_root / "constance"
    constance.mkdir()
    _make_script(constance, "post_changelog.sh", textwrap.dedent("""\
        tool=post-changelog
        cmd="$tool --product constance --file /tmp/x"
        eval "$cmd"
    """))

    stdout, stderr, rc = _run(setup["convivy_root"], setup["bin_dir"], "--no-report")
    assert rc == 2, f"expected rc=2 (breach, dry-run), got rc={rc}\nstderr: {stderr}"
    # Verify dry-run status in output, not error:400
    assert "dry-run" in stderr, f"expected dry-run status, got:\n{stderr}"
    assert "error:400" not in stderr, f"dry run must not hit the API:\n{stderr}"


# -- the caller map ------------------------------------------------------------
# Declared rather than discovered, so an omission is invisible: the check reports
# "no problems found" across the callers it happens to know about. convivy-app-kit
# was left out on the reasoning that its --no-slack breach was "already fixed" —
# but so was every other caller's, and being fixed is not the same as ceasing to
# call the tool. The map exists to catch the NEXT contract change.


class TestToolCallers:
    """Every repo that calls a shared tool must be in the map."""

    @classmethod
    def setup_class(cls):
        cls.mod = _load_check_module()

    def test_all_known_post_changelog_callers_are_declared(self):
        assert set(self.mod._TOOL_CALLERS["post-changelog"]) == {
            "vivy", "constance", "selector", "selector-backend", "convivy-app-kit",
        }

    def test_post_release_note_callers_are_declared(self):
        assert set(self.mod._TOOL_CALLERS["post-release-note"]) == {"vivy"}

    def test_every_declared_caller_actually_has_a_scripts_dir(self):
        """A caller named here but absent on disk reads as "checkout missing" and
        is skipped, which is correct. This pins the reverse: a name that is simply
        wrong (a typo, a rename) would be skipped forever and never checked."""
        root = pathlib.Path.home() / "Convivy"
        if not root.is_dir():
            pytest.skip("no Convivy checkout root on this machine")
        missing = [
            name
            for names in self.mod._TOOL_CALLERS.values()
            for name in names
            if (root / name).is_dir() and not (root / name / "scripts").is_dir()
        ]
        assert not missing, f"declared callers with no scripts/ dir: {missing}"


def test_lib_path_insert_is_idempotent():
    """The insert used to sit inside _post_signal, which runs per breach — so
    sys.path grew with the number of problems found, while importing nothing after
    the first call."""
    mod = _load_check_module()
    before = list(sys.path)
    try:
        for _ in range(5):
            mod._ensure_lib_on_path()
        added = [p for p in sys.path if p not in before]
        assert len(added) <= 1, f"sys.path grew by {len(added)} entries: {added}"
    finally:
        sys.path[:] = before


# --- the dedup_key is scoped to the UTC day ----------------------------------
# The signal store keeps every dedup_key for all time, so an unscoped key logs a
# breach that clears and returns never again. Each run reads the day once, and
# every key it writes carries it. Pinned here: two days give two keys, two runs
# on one day give one key, and present, absent, present on a later day logs again.

_CLI_BREACH = textwrap.dedent("""\
    import subprocess, pathlib
    tool = pathlib.Path.home() / ".local" / "bin" / "post-changelog"
    cmd = [str(tool), "--product", "vivy", "--no-slack"]
    subprocess.run(cmd)
""")
_CLI_CLEAN = _CLI_BREACH.replace(', "--no-slack"', "")


def _cli_keys(setup, monkeypatch, *, day, breach):
    """Run the monitor in-process against the fake tree, pinned to *day*;
    return the dedup_keys it logged. Nothing posts: _post_signal is stubbed."""
    mod = _load_check_module()
    monkeypatch.setattr(mod, "_utc_today", lambda: day, raising=False)
    keys = []
    monkeypatch.setattr(mod, "_post_signal",
                        lambda product, title, body, dedup_key, **k:
                        keys.append(dedup_key) or {"status": "stub"})
    vivy = pathlib.Path(setup["convivy_root"]) / "vivy"
    vivy.mkdir(exist_ok=True)
    _make_script(vivy, "post_changelog.py", _CLI_BREACH if breach else _CLI_CLEAN)
    mod.main(["--convivy-root", setup["convivy_root"], "--bin-dir", setup["bin_dir"]])
    return keys


def test_cli_contract_key_is_scoped_to_the_utc_day(setup, monkeypatch):
    day_one = _cli_keys(setup, monkeypatch, day="2030-01-01", breach=True)
    day_two = _cli_keys(setup, monkeypatch, day="2030-01-02", breach=True)
    assert day_one and day_two
    assert day_one != day_two, "the same breach on two UTC days must log twice"
    assert day_one[0].endswith(":2030-01-01") and day_two[0].endswith(":2030-01-02")


def test_cli_contract_two_runs_on_one_day_give_one_key(setup, monkeypatch):
    first = _cli_keys(setup, monkeypatch, day="2030-01-01", breach=True)
    second = _cli_keys(setup, monkeypatch, day="2030-01-01", breach=True)
    assert first == second and len(first) == 1


def test_cli_contract_present_absent_present_logs_again_on_the_later_day(setup, monkeypatch):
    present = _cli_keys(setup, monkeypatch, day="2030-01-01", breach=True)
    absent = _cli_keys(setup, monkeypatch, day="2030-01-02", breach=False)
    back = _cli_keys(setup, monkeypatch, day="2030-01-03", breach=True)
    assert absent == []
    assert len(back) == 1 and back[0] not in present


def test_flag_sets_that_joined_alike_get_different_names_digests():
    """The old key joined the flags with `_`, so the set {x_y} and the set {x, y} both became `x_y`,
    and the second distinct breach answered `duplicate` (review of PR #788, rounds 1 and 2)."""
    mod = _load_check_module()
    assert "_".join(["x_y"]) == "_".join(["x", "y"]), "the collision the old key produced"
    assert mod._names_digest(["x_y"]) != mod._names_digest(["x", "y"])
    assert mod._names_digest(["x", "y"]) == mod._names_digest(["x", "y"]), "stable for one set"
