"""`check-kb-guards` — the daily cross-repo KB-agreement runner (ARCH-436).

This tool exists because a check reported fine while nothing had been checked, so
it needs tests for exactly that: the two ways a run can LOOK clean and prove
nothing. `classify` is where that judgment lives, and `parse_counts` is what feeds
it — both pure, both tested here against real pytest output rather than a mock of
it, because the bug that shipped in the first version was a parse bug.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO / "bin" / "check-kb-guards"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_kb_guards", str(CHECK))
    spec = importlib.util.spec_from_loader("check_kb_guards", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def kb_guards():
    return _load()


# --- parse_counts ------------------------------------------------------------


def _suite(**attrs) -> str:
    body = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f'<?xml version="1.0"?><testsuites><testsuite {body}></testsuite></testsuites>'


def test_parse_counts_derives_passed(kb_guards):
    c = kb_guards.parse_counts(_suite(tests=7, failures=1, errors=0, skipped=2))
    assert c == {"tests": 7, "failed": 1, "error": 0, "skipped": 2, "passed": 4}


def test_parse_counts_handles_a_bare_testsuite_element(kb_guards):
    """Older pytest emits <testsuite> with no <testsuites> wrapper."""
    xml = '<?xml version="1.0"?><testsuite tests="3" failures="0" errors="0" skipped="0"/>'
    assert kb_guards.parse_counts(xml)["passed"] == 3


def test_parse_counts_on_unparseable_input_reports_nothing_ran(kb_guards):
    """A truncated report must read as an empty run, which `classify` calls a
    breach — never as a pass. Failing open here would restore the exact blindness
    this tool replaces."""
    c = kb_guards.parse_counts("<testsui")
    assert c["tests"] == 0 and c["passed"] == 0
    assert kb_guards.classify(0, c)[0] is False


def test_parse_counts_reads_a_real_pytest_report(kb_guards, tmp_path):
    """The regression test for the bug the first version shipped.

    It parsed pytest's TERMINAL summary. This repo's pyproject sets
    `addopts = "-q"`, so the tool's own `-q` made it `-qq`, at which pytest prints
    progress dots and no summary line at all — five passing tests parsed as zero of
    everything and were reported as "no tests were collected". Run real pytest and
    read the real report rather than a fixture string, because a fixture string is
    what the broken version would also have passed.
    """
    (tmp_path / "test_sample.py").write_text(textwrap.dedent("""
        import pytest

        def test_one(): pass
        def test_two(): pass

        @pytest.mark.skip(reason="deliberate")
        def test_three(): pass
    """), encoding="utf-8")
    report = tmp_path / "r.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-qq", "-p", "no:cacheprovider",
                    f"--junit-xml={report}", str(tmp_path)],
                   capture_output=True, text=True, check=False)
    c = kb_guards.parse_counts(report.read_text(encoding="utf-8"))
    assert c["passed"] == 2 and c["skipped"] == 1


# --- classify ----------------------------------------------------------------


def test_a_clean_run_is_ok(kb_guards):
    ok, reason = kb_guards.classify(0, {"tests": 5, "failed": 0, "error": 0,
                                        "skipped": 0, "passed": 5})
    assert ok and "5 passed" in reason


def test_an_empty_selection_is_a_breach(kb_guards):
    """The marker was renamed or the guards were deleted. A runner that reports OK
    on an empty selection is the blind green being replaced, not a fix for it."""
    ok, reason = kb_guards.classify(5, {"tests": 0, "failed": 0, "error": 0,
                                        "skipped": 0, "passed": 0})
    assert not ok and "no `needs_kb` tests were collected" in reason


def test_everything_skipped_is_a_breach(kb_guards):
    """pytest exits 0 when every test skips, so the exit code alone says "clean".
    It means the knowledge checkout went missing and the run proved nothing."""
    ok, reason = kb_guards.classify(0, {"tests": 4, "failed": 0, "error": 0,
                                        "skipped": 4, "passed": 0})
    assert not ok and "every `needs_kb` test skipped" in reason


def test_a_failure_is_named_as_a_failure_not_as_skips(kb_guards):
    """A run with both failures and skips must report the failures. Naming the
    skips would send the reader to look for a missing checkout that is present."""
    ok, reason = kb_guards.classify(1, {"tests": 4, "failed": 1, "error": 0,
                                        "skipped": 2, "passed": 1})
    assert not ok and "1 failed" in reason


def test_a_nonzero_exit_with_no_counted_failure_is_still_a_breach(kb_guards):
    """An internal pytest error can leave the counts clean and the exit code not.
    Trusting the counts alone would call that a pass."""
    ok, reason = kb_guards.classify(3, {"tests": 2, "failed": 0, "error": 0,
                                        "skipped": 0, "passed": 2})
    assert not ok and "exited 3" in reason


# --- run_repo ----------------------------------------------------------------


def test_a_missing_checkout_is_not_a_breach(kb_guards, tmp_path):
    """A repo that is not on this machine must read as absent, never as clean-or-
    broken. `check-cli-contracts` takes the same position for the same reason."""
    ok, reason, _, failing = kb_guards.run_repo("nope", {"python": sys.executable}, tmp_path)
    assert ok and "not found" in reason


def test_a_suite_that_cannot_start_is_a_breach(kb_guards, tmp_path):
    """pytest writing no report means it died before collection — a broken conftest
    or a missing interpreter. Reading that as zero tests would report "no guards"
    for what is really "the suite is broken"."""
    (tmp_path / "repo").mkdir()
    ok, reason, _, failing = kb_guards.run_repo(
        "repo", {"python": str(tmp_path / "no-such-python")}, tmp_path)
    assert not ok
    assert "could not run pytest" in reason or "no report" in reason


def test_run_repo_passes_the_knowledge_repo_it_was_told_to_check(kb_guards, tmp_path):
    """`--convivy-root` must reach the guards, or this tool checks one tree while
    they read another — a harness silently checking the wrong KB, which is the
    defect class it was built to catch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "conftest.py").write_text(textwrap.dedent("""
        import os
        import pytest

        def pytest_configure(config):
            config.addinivalue_line("markers", "needs_kb: x")
    """), encoding="utf-8")
    (repo / "test_env.py").write_text(textwrap.dedent(f"""
        import os
        import pytest

        @pytest.mark.needs_kb
        def test_sees_the_override():
            assert os.environ["KNOWLEDGE_REPO"] == {str(tmp_path / "knowledge")!r}
    """), encoding="utf-8")
    ok, reason, output, failing = kb_guards.run_repo("repo", {"python": sys.executable}, tmp_path)
    assert ok, f"{reason}\n{output}"


# --- the declared repo list --------------------------------------------------


def test_every_declared_repo_names_a_real_interpreter_path(kb_guards):
    """An absolute interpreter that does not exist makes the repo look broken every
    day the job runs.

    Checked ONLY for repos whose checkout is present, which is the same signal
    `run_repo` uses to decide whether a repo is checked at all — so this asserts
    exactly where the assertion means something. The first version asserted
    unconditionally and failed CI on `~/.local/venv-orient/bin/python`, a path that
    exists on the mini and on no runner: a host fact stated as a repo invariant.

    A bare name (resolved on PATH) is not checkable here and is left alone.
    """
    checked = 0
    for repo, cfg in kb_guards._REPOS.items():
        if not (kb_guards.CONVIVY_ROOT / repo).is_dir():
            continue
        checked += 1
        py = pathlib.Path(cfg["python"])
        if py.is_absolute():
            assert py.exists(), f"{repo}: no interpreter at {py}"
    if not checked:
        pytest.skip("no declared repo is checked out here; nothing to verify")


# --- _breach_body -------------------------------------------------------------


def test_breach_body_reproduce_line_names_the_passed_in_root(kb_guards, tmp_path):
    """The Reproduce line must name THIS machine's resolved root, not a literal
    ~/Convivy — a mini's configured root and an operator's chosen one can differ
    (company/decisions/0008)."""
    repo = next(iter(kb_guards._REPOS))
    body = kb_guards._breach_body(repo, "1 failed", "some output", tmp_path)
    assert f"cd {tmp_path}/{repo} &&" in body
    assert "~/Convivy" not in body


# --- a breach's identity is its failing tests, not their count -----------------


def _report_for(tmp_path, body: str) -> str:
    (tmp_path / "test_sample.py").write_text(textwrap.dedent(body), encoding="utf-8")
    report = tmp_path / "r.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-qq", "-p", "no:cacheprovider",
                    f"--junit-xml={report}", str(tmp_path / "test_sample.py")],
                   capture_output=True, text=True, check=False)
    return report.read_text(encoding="utf-8")


def test_failing_ids_names_failures_and_errors_from_a_real_report(kb_guards, tmp_path):
    junit = _report_for(tmp_path, """
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("fixture")

        def test_passes(): pass
        def test_fails(): assert False
        def test_errors(broken): pass
    """)
    assert kb_guards.failing_ids(junit) == ["test_sample::test_errors",
                                            "test_sample::test_fails"]


def test_a_different_failure_with_the_same_count_is_a_new_breach(kb_guards):
    """On 2026-09-16 the key was `kb-guards:<repo>:<N> failed, 0 errored`, so a new
    failure replacing an old one filed as `duplicate` of the old one's issue."""
    reason = "1 failed, 0 errored"
    old = kb_guards.breach_key("agent-platform", reason, ["t::test_known_kb_projects"])
    new = kb_guards.breach_key("agent-platform", reason, ["t::test_schema_constants"])
    assert old != new
    assert old == kb_guards.breach_key("agent-platform", reason, ["t::test_known_kb_projects"])


def test_a_breach_with_no_failing_test_keys_on_its_reason(kb_guards):
    empty = kb_guards.breach_key("r", "no `needs_kb` tests were collected", [])
    skipped = kb_guards.breach_key("r", "every `needs_kb` test skipped (3)", [])
    assert empty != skipped


def test_breach_body_lists_the_failing_tests_by_name(kb_guards, tmp_path):
    repo = next(iter(kb_guards._REPOS))
    body = kb_guards._breach_body(repo, "2 failed, 0 errored", "out", tmp_path,
                                  ["t::test_a", "t::test_b"])
    assert "- `t::test_a`" in body and "- `t::test_b`" in body


# --- the log is dated --------------------------------------------------------


def test_every_log_line_is_dated(kb_guards, capsys):
    kb_guards._log("[DONE] 1 repo(s) with breaches")
    out = capsys.readouterr().out
    assert __import__("re").match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[DONE\]", out)


# --- every monitor's breach is an alert signal under its key, never an item ----


@pytest.mark.parametrize("script,stream", [
    ("check-kb-guards", "kb-guards"),
    ("check-primary-checkouts", "primary-checkout"),
    ("check-serving-checkouts", "serving-checkout"),
    ("check-deploy-pinned-jobs", "deploy-pinned-jobs"),
    ("check-cli-contracts", "cli-contract"),
])
def test_the_monitor_logs_a_breach_as_an_alert_signal_under_its_key(script, stream, monkeypatch):
    """Each monitor logs its breach as one alert signal on its own stream, keyed on
    dedup_key, and creates no work item (platform/decisions/0073)."""
    loader = importlib.machinery.SourceFileLoader(script.replace("-", "_"), str(REPO / "bin" / script))
    spec = importlib.util.spec_from_loader(script.replace("-", "_"), loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    import signal_client
    import issue_client
    seen = {}
    monkeypatch.setattr(signal_client, "post_alert",
                        lambda s, body, **k: seen.update(stream=s, body=body, **k) or "created")
    assert not hasattr(issue_client, "post_issue"), "the item client has no create call to reach"
    res = mod._post_signal("agent-platform", "t", "b", "k")
    assert res == {"status": "created"}, script
    assert seen["stream"] == stream, script
    assert seen["dedup_key"] == "k", script
    assert seen["product"] == "agent-platform", script
    assert seen["body"].startswith("t\n\nb"), script


def test_a_backtick_in_a_test_id_cannot_close_its_code_span(kb_guards):
    block = kb_guards._failing_block(["t::test_thing[a`b]"])
    assert block == "Failing:\n- `t::test_thing[a'b]`\n\n"


def test_the_failing_list_is_bounded(kb_guards):
    ids = [f"t::test_{i:03d}" for i in range(kb_guards._FAILING_LISTED + 5)]
    block = kb_guards._failing_block(ids)
    assert block.count("\n- `") == kb_guards._FAILING_LISTED
    assert "- …and 5 more\n" in block


# --- the dedup_key is scoped to the UTC day ----------------------------------
# The signal store keeps every dedup_key for all time, so an unscoped key logs a
# breach that clears and returns never again. Each run reads the day once, and
# every key it writes carries it. Pinned here: two days give two keys, two runs
# on one day give one key, and present, absent, present on a later day logs again.

def _kb_keys(kb_guards, tmp_path, monkeypatch, *, day, breach):
    """Run main() with every repo's guard failing (breach) or passing; return the
    dedup_keys it logged. The guard run itself is stubbed; _post_signal too."""
    (tmp_path / "knowledge" / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(kb_guards, "_utc_today", lambda: day, raising=False)
    monkeypatch.setattr(kb_guards, "run_repo",
                        lambda repo, *a, **k: (not breach, "guard failed", "out", []))
    keys = []
    monkeypatch.setattr(kb_guards, "_post_signal",
                        lambda product, title, body, dedup_key, **k:
                        keys.append(dedup_key) or {"status": "stub"})
    kb_guards.main(["--convivy-root", str(tmp_path)])
    return keys


def test_kb_guards_key_is_scoped_to_the_utc_day(kb_guards, tmp_path, monkeypatch):
    day_one = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    day_two = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-02", breach=True)
    assert day_one and day_two
    assert day_one != day_two, "the same breach on two UTC days must log twice"
    assert day_one[0].endswith(":2030-01-01") and day_two[0].endswith(":2030-01-02")


def test_kb_guards_two_runs_on_one_day_give_one_key(kb_guards, tmp_path, monkeypatch):
    first = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    second = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    assert first == second and len(first) >= 1


def test_kb_guards_present_absent_present_logs_again_on_the_later_day(
        kb_guards, tmp_path, monkeypatch):
    present = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    absent = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-02", breach=False)
    back = _kb_keys(kb_guards, tmp_path, monkeypatch, day="2030-01-03", breach=True)
    assert absent == []
    assert back and not set(back) & set(present)
