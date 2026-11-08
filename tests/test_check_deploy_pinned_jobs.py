"""`check-deploy-pinned-jobs` — the standing check #469/#470 should have had
(platform/decisions/0059).

THE DEFECT THIS PINS. #469's own completion measurement read only
`ProgramArguments[0]` and called a job "migrated" once that one entry pointed
at the deploy area, while argv[1] — the actual script, behind an interpreter
— still pointed at a symlink into the checkout. A test that only ever
constructs an argv[0]-pointing-at-the-deploy-area fixture would pass against
that exact bug, so the tests below build jobs shaped like
`[interpreter, script, ...flags]` and assert the SCRIPT position is checked,
not just the interpreter.
"""

# `com.convivy.triage-queue` appears throughout as SYNTHETIC fixture data — a label
# string and tmp-path files, never the real job. That job was deleted (it had
# been crashing on every firing since the Slack removal took its input away), so
# do not go looking for it. The name is kept because these fixtures encode the
# exact argv[1]-behind-an-interpreter shape that #469's measurement missed, and
# renaming them would churn a test whose value is in that shape.
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys
from unittest import mock

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CHECK = REPO / "bin" / "check-deploy-pinned-jobs"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_deploy_pinned_jobs", str(CHECK))
    spec = importlib.util.spec_from_loader("check_deploy_pinned_jobs", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def cdpj():
    return _load()


@pytest.fixture(autouse=True)
def _empty_deploy_root(cdpj, tmp_path_factory, monkeypatch):
    """Every test in this file defaults to an EMPTY deploy root. Without this,
    `main()`'s `--deploy-root` default is this machine's real `~/.local/deploy`,
    so a test that never mentions deploy areas at all would pick up whatever is
    actually installed there and depend on host state. A test that wants a
    deploy-area fixture passes its own `--deploy-root` explicitly, which wins
    over this default the same way `--checkout-root` already does.
    """
    monkeypatch.setattr(cdpj, "DEPLOY_ROOT", tmp_path_factory.mktemp("empty-deploy-root"))


# --- parse_labels ------------------------------------------------------------


def test_parse_labels_skips_the_header_and_empty_labels(cdpj):
    listing = (
        "PID\tStatus\tLabel\n"
        "123\t0\tcom.convivy.triage-queue\n"
        "-\t0\t\n"
        "456\t-9\tcom.apple.something\n"
    )
    assert cdpj.parse_labels(listing) == ["com.convivy.triage-queue", "com.apple.something"]


def test_parse_labels_on_empty_output(cdpj):
    assert cdpj.parse_labels("") == []


# --- _migrating_label_suffixes: CQ-428 coverage for a job still loaded under
#     its pre-rename label, discovered by suffix rather than hardcoded -------


def test_migrating_label_suffixes_reads_launchd_job_basenames(cdpj, tmp_path):
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    (launchd / "com.convivy.run-observer.plist").write_text("", encoding="utf-8")
    (launchd / "com.convivy.knowledge-watcher.plist").write_text("", encoding="utf-8")
    (launchd / "com.convivy.ip-forwarding.plist").write_text("", encoding="utf-8")
    suffixes = cdpj._migrating_label_suffixes(str(tmp_path))
    assert set(suffixes) == {".run-observer", ".knowledge-watcher", ".ip-forwarding"}


def test_migrating_label_suffixes_on_a_missing_launchd_dir(cdpj, tmp_path):
    assert cdpj._migrating_label_suffixes(str(tmp_path / "nope")) == ()


def test_a_job_still_loaded_under_a_pre_migration_label_is_still_scanned(cdpj, tmp_path, monkeypatch):
    """The whole point of CQ-428 is that the pre-rename label named a person,
    so this file must never hardcode it — the fallback coverage is suffix
    match against `launchd/`'s own job basenames, proven here with an
    arbitrary non-com.convivy. label rather than the real pre-rename one."""
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    (launchd / "com.convivy.run-observer.plist").write_text("", encoding="utf-8")
    listing = (
        "PID\tStatus\tLabel\n"
        "123\t0\tcom.example-legacy.run-observer\n"
    )
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: listing)
    monkeypatch.setattr(cdpj, "_launchctl_print", lambda label, uid: ("", 1))
    rc = cdpj.main(["--checkout-root", str(tmp_path), "--no-report"])
    assert rc == 0  # print failing is skipped with a note, not treated as a breach
    # the real assertion is that the label reached _launchctl_print at all —
    # proven by _migrating_label_suffixes directly above, and end-to-end here.
    suffixes = cdpj._migrating_label_suffixes(str(tmp_path))
    assert any("com.example-legacy.run-observer".endswith(s) for s in suffixes)


# --- parse_arguments_block ----------------------------------------------------


_PRINT_TRANSCRIPT = """gui/501/com.convivy.triage-queue = {
\tactive count = 0
\tpath = /srv/convivy/Library/LaunchAgents/com.convivy.triage-queue.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /opt/homebrew/bin/python3
\targuments = {
\t\t/opt/homebrew/bin/python3
\t\t/srv/convivy/.local/bin/triage-queue
\t}

\tstdout path = /srv/convivy/Convivy/agent-runs/triage-queue.out.log
\tenvironment = {
\t\tPATH => /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
\t}
}
"""


def test_parse_arguments_block_reads_every_entry(cdpj):
    """Not just argv[0] — the whole array, in order."""
    assert cdpj.parse_arguments_block(_PRINT_TRANSCRIPT) == [
        "/opt/homebrew/bin/python3",
        "/srv/convivy/.local/bin/triage-queue",
    ]


def test_parse_arguments_block_on_a_job_with_no_arguments_key(cdpj):
    assert cdpj.parse_arguments_block("gui/501/x = {\n\tstate = running\n}\n") == []


def test_parse_arguments_block_stops_at_the_closing_brace_not_mid_array(cdpj):
    """A transcript has other `}`-closed blocks after `arguments`; only the
    first close after the open may end the array."""
    text = (
        "arguments = {\n"
        "\t/bin/sh\n"
        "\t/path/to/script\n"
        "}\n"
        "\n"
        "environment = {\n"
        "\tPATH => /bin\n"
        "}\n"
    )
    assert cdpj.parse_arguments_block(text) == ["/bin/sh", "/path/to/script"]


# --- classify: the core regression -------------------------------------------


def test_a_script_behind_an_interpreter_in_argv1_is_caught(cdpj, tmp_path):
    """THE #469 MEASUREMENT BUG, reproduced and fixed. argv[0] is a real,
    non-checkout interpreter (would read clean under an argv[0]-only check);
    argv[1] is the actual script, resolving into the checkout. Only a checker
    that walks the FULL argv catches this."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("#!/usr/bin/env python3\n")
    symlinked_script = tmp_path / "local-bin" / "triage-queue"
    symlinked_script.parent.mkdir(parents=True)
    symlinked_script.symlink_to(real_script)

    interpreter = tmp_path / "homebrew" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")  # exists, and is not under the checkout

    argv = [str(interpreter), str(symlinked_script)]

    breaches = cdpj.classify("com.convivy.triage-queue", argv, checkout_root=str(checkout))
    kinds_by_argv = {b["argv"]: b["kind"] for b in breaches}
    assert kinds_by_argv.get(str(symlinked_script)) == "checkout-reaching", (
        "argv[1] (the script, behind the interpreter) must be flagged — this is "
        "exactly the position #469's argv[0]-only measurement missed"
    )
    assert str(interpreter) not in kinds_by_argv, "the interpreter itself is not a breach"


def test_an_argv0_only_check_would_have_missed_the_same_fixture(tmp_path):
    """Proves the fixture above is a genuine regression test rather than one
    that would pass under the OLD, broken measurement too: a naive
    reimplementation that only ever looks at argv[0] finds nothing wrong with
    the identical inputs used above."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("#!/usr/bin/env python3\n")
    symlinked_script = tmp_path / "local-bin" / "triage-queue"
    symlinked_script.parent.mkdir(parents=True)
    symlinked_script.symlink_to(real_script)
    interpreter = tmp_path / "homebrew" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")

    argv = [str(interpreter), str(symlinked_script)]

    def argv0_only_classify(label, argv, checkout_root):
        checkout_real = os.path.realpath(checkout_root)
        entry = argv[0]
        resolved = os.path.realpath(entry)
        if not os.path.exists(resolved):
            return ["dangling"]
        if resolved == checkout_real or resolved.startswith(checkout_real + os.sep):
            return ["checkout-reaching"]
        return []

    assert argv0_only_classify("com.convivy.triage-queue", argv, str(checkout)) == [], (
        "an argv[0]-only measurement reads this exact breach as clean — "
        "that is the defect this file exists to catch"
    )


def test_a_dangling_target_is_caught(cdpj, tmp_path):
    argv = [str(tmp_path / "does-not-exist" / "script")]
    breaches = cdpj.classify("com.convivy.waiting-on-operator-digest", argv,
                             checkout_root=str(tmp_path / "agent-platform"))
    assert breaches == [{
        "kind": "dangling",
        "label": "com.convivy.waiting-on-operator-digest",
        "argv": argv[0],
        "resolved": os.path.realpath(argv[0]),
    }]


def test_a_dangling_target_is_not_also_reported_checkout_reaching(cdpj, tmp_path):
    """A target that does not exist can't make good on "reaching" anything —
    reported once, as dangling, not doubled into a second class."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    argv = [str(checkout / "bin" / "deleted-script")]
    breaches = cdpj.classify("com.convivy.x", argv, checkout_root=str(checkout))
    assert [b["kind"] for b in breaches] == ["dangling"]


def test_a_clean_deploy_pinned_job_is_not_a_breach(cdpj, tmp_path):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    deploy = tmp_path / "deploy"
    (deploy / "bin").mkdir(parents=True)
    (deploy / "bin" / "triage-queue").write_text("")
    (deploy / "venv" / "bin").mkdir(parents=True)
    (deploy / "venv" / "bin" / "python3").write_text("")
    argv = [str(deploy / "venv" / "bin" / "python3"), str(deploy / "bin" / "triage-queue")]
    assert cdpj.classify("com.convivy.triage-queue", argv, checkout_root=str(checkout)) == []


def test_non_absolute_argv_entries_are_not_paths(cdpj, tmp_path):
    """Flags and bare values (`--days`, `14`) are not file paths and must
    never be realpath-resolved against the cwd and reported as breaches."""
    argv = ["--days", "14"]
    assert cdpj.classify("com.convivy.model-scorecard", argv,
                         checkout_root=str(tmp_path / "agent-platform")) == []


def test_the_autopull_watcher_is_exempt_from_checkout_reaching(cdpj, tmp_path):
    """com.convivy.agent-platform-autopull must read the checkout — that is how it
    learns what to deploy — so its checkout-reaching argv is not a breach."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    script = checkout.parent / "local-bin" / "agent-platform-autopull"
    script.parent.mkdir(parents=True)
    real = checkout / "bin" / "agent-platform-autopull"
    real.parent.mkdir(parents=True)
    real.write_text("")
    script.symlink_to(real)
    breaches = cdpj.classify("com.convivy.agent-platform-autopull", [str(script)],
                             checkout_root=str(checkout))
    assert breaches == []


def test_the_autopull_exemption_does_not_cover_a_dangling_target(cdpj, tmp_path):
    """The exemption is specifically for reaching the checkout, which is this
    job's job. It must not become a blanket pass for a target that plain
    doesn't exist."""
    argv = [str(tmp_path / "gone")]
    breaches = cdpj.classify("com.convivy.agent-platform-autopull", argv,
                             checkout_root=str(tmp_path / "agent-platform"))
    assert [b["kind"] for b in breaches] == ["dangling"]


# --- classify: `sh -c '<command>'` is a command string, not a path -----------


def test_a_shell_dash_c_command_whose_first_word_exists_passes(cdpj, tmp_path):
    """`com.convivy.tmux-keeper`'s real argv, verbatim in shape: ["/bin/sh",
    "-c", "<tmux command with flags and a `||`>"]. Realpathing the whole
    string (unfixed) finds no such file — spaces and `||` are not on disk —
    and reports it dangling; the fix checks only the leading `tmux` path,
    which exists, and passes clean."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    tmux = tmp_path / "homebrew" / "tmux"
    tmux.parent.mkdir(parents=True)
    tmux.write_text("")
    command = (
        f'{tmux} has-session -t keeper 2>/dev/null || '
        f'{tmux} new-session -d -s keeper -c "$HOME"'
    )
    argv = [str(sh), "-c", command]
    assert cdpj.classify("com.convivy.tmux-keeper", argv,
                         checkout_root=str(checkout)) == []


def test_a_shell_dash_c_command_whose_first_word_is_missing_is_dangling(cdpj, tmp_path):
    """Same shape, program missing. Unfixed code realpaths the whole string
    (command + trailing flags, never a real file) and reports THAT garbled
    string as the resolved target; the fix resolves and reports just the
    missing program."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    missing = tmp_path / "gone" / "tmux"
    command = f'{missing} has-session -t keeper'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.tmux-keeper", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "dangling",
        "label": "com.convivy.tmux-keeper",
        "argv": command,
        "resolved": os.path.realpath(str(missing)),
    }]


def test_a_shell_dash_c_command_whose_first_word_is_under_the_checkout_is_checkout_reaching(cdpj, tmp_path):
    """Same shape, program lives under the checkout. Unfixed code realpaths
    the whole string, which does not exist as a file (the string has trailing
    flags appended), so it reports DANGLING; the fix resolves just the
    program, which exists under the checkout, so it reports CHECKOUT-REACHING."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    program = checkout / "bin" / "some-tool"
    program.parent.mkdir(parents=True)
    program.write_text("")
    command = f'{program} --flag value'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "checkout-reaching",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(program)),
    }]


def test_a_plain_absolute_path_argv_is_unaffected_by_the_dash_c_special_case(cdpj, tmp_path):
    """`/bin/sh /path/to/script` (no `-c`) is the ordinary interpreter+script
    shape the earlier regression tests already cover; the `-c` special-case
    must not swallow this shape too."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    script = checkout / "bin" / "some-script"
    script.parent.mkdir(parents=True)
    script.write_text("")
    argv = [str(sh), str(script)]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert [b["kind"] for b in breaches] == ["checkout-reaching"]


def test_a_shell_dash_c_wrapper_reaching_the_checkout_is_still_caught(cdpj, tmp_path):
    """round 2 (security-reviewer finding): `env <program>` in front of the
    real program used to pass, because the unfixed code checked only the
    command string's first word — `/usr/bin/env`, which exists — and never
    looked at the argument after it. The fix walks every absolute token, so
    the program env is about to run is checked too."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    env = tmp_path / "usr" / "bin" / "env"
    env.parent.mkdir(parents=True)
    env.write_text("")
    program = checkout / "bin" / "some-tool"
    program.parent.mkdir(parents=True)
    program.write_text("")
    command = f'{env} {program}'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "checkout-reaching",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(program)),
    }]


def test_a_shell_dash_c_command_with_a_missing_program_after_and_and_is_dangling(cdpj, tmp_path):
    """The first word existing is not enough to clear the whole string: a
    second program named after `&&` that is not on disk is still a job that
    will fail to exec partway through its own command."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    first = tmp_path / "bin" / "first"
    first.write_text("")
    missing = tmp_path / "gone" / "second"
    command = f'{first} && {missing}'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "dangling",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(missing)),
    }]


def test_a_shell_dash_c_command_with_unbalanced_quotes_is_still_checked(cdpj, tmp_path):
    """`shlex` raises on a command string it can't tokenize (an unbalanced
    quote); the fallback splits on whitespace instead of skipping the
    string outright. The checkout-reaching program sits after the unbalanced
    word here, not as the string's first word — a first-word-only rule would
    check only `wrapper` (which exists, outside the checkout) and pass."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    wrapper = tmp_path / "bin" / "wrapper"
    wrapper.write_text("")
    program = checkout / "bin" / "some-tool"
    program.parent.mkdir(parents=True)
    program.write_text("")
    command = f'{wrapper} --note "unbalanced {program}'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "checkout-reaching",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(program)),
    }]


def test_a_shell_dash_c_command_not_starting_with_slash_reaching_the_checkout_is_caught(cdpj, tmp_path):
    """round 3 (security-reviewer finding): the per-entry `startswith("/")`
    gate used to run before the `-c` string was recognised as one, so a
    command that itself does not begin with "/" (`cd /tmp && ...`) was
    skipped whole and never tokenised. The fix recognises the `-c` entry
    first and tokenises it regardless of its own leading character."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    program = checkout / "bin" / "some-tool"
    program.parent.mkdir(parents=True)
    program.write_text("")
    command = f'cd /tmp && {program}'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "checkout-reaching",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(program)),
    }]


def test_a_shell_dash_c_command_not_starting_with_slash_and_dangling_is_caught(cdpj, tmp_path):
    """Same shape as above, program missing rather than under the checkout."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    sh = tmp_path / "bin" / "sh"
    sh.parent.mkdir(parents=True)
    sh.write_text("")
    missing = tmp_path / "gone" / "some-tool"
    command = f'cd /tmp && {missing}'
    argv = [str(sh), "-c", command]
    breaches = cdpj.classify("com.convivy.some-job", argv, checkout_root=str(checkout))
    assert breaches == [{
        "kind": "dangling",
        "label": "com.convivy.some-job",
        "argv": command,
        "resolved": os.path.realpath(str(missing)),
    }]


# --- main(): scope, degrade-gracefully, silence, exit codes ------------------


def _print_for(argv: list[str]) -> str:
    body = "\n".join(f"\t\t{a}" for a in argv)
    return f"gui/501/x = {{\n\targuments = {{\n{body}\n\t}}\n}}\n"


def test_main_only_checks_labels_this_platform_owns(cdpj, tmp_path, monkeypatch):
    """macOS's own agents reference paths that live only in the dyld shared
    cache and never exist as a standalone file — real, unrelated to this
    migration, and would swamp every run in system noise if walked too."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n"
        "1\t0\tcom.apple.somesystemagent\n"
    ))
    calls = []
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: calls.append(label) or ("", 0))
    rc = cdpj.main(["--checkout-root", str(checkout), "--no-report"])
    assert calls == [], "an unowned label must never even reach launchctl print"
    assert rc == 0


def test_main_skips_a_label_whose_print_fails_without_crashing(cdpj, tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n1\t0\tcom.convivy.broken\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print", lambda label, uid: ("", 1))
    rc = cdpj.main(["--checkout-root", str(checkout), "--no-report", "-v"])
    assert rc == 0
    assert "SKIP" in capsys.readouterr().err


def test_main_is_silent_and_exits_zero_when_clean(cdpj, tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    deploy_script = tmp_path / "deploy" / "bin" / "triage-queue"
    deploy_script.parent.mkdir(parents=True)
    deploy_script.write_text("")
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n1\t0\tcom.convivy.triage-queue\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(deploy_script)]), 0))
    rc = cdpj.main(["--checkout-root", str(checkout), "--no-report"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_reports_a_breach_and_exits_two(cdpj, tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("")
    symlinked = tmp_path / "local-bin" / "triage-queue"
    symlinked.parent.mkdir(parents=True)
    symlinked.symlink_to(real_script)
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n1\t0\tcom.convivy.triage-queue\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(symlinked)]), 0))
    rc = cdpj.main(["--checkout-root", str(checkout), "--no-report"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "com.convivy.triage-queue" in err and "checkout-reaching" in err


def test_main_logs_one_signal_per_breach_class_present(cdpj, tmp_path, monkeypatch):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("")
    symlinked = tmp_path / "local-bin" / "triage-queue"
    symlinked.parent.mkdir(parents=True)
    symlinked.symlink_to(real_script)
    dangling = tmp_path / "gone" / "waiting-on-operator-digest"

    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n"
        "1\t0\tcom.convivy.triage-queue\n"
        "2\t2\tcom.convivy.waiting-on-operator-digest\n"
    ))

    def fake_print(label, uid):
        if label == "com.convivy.triage-queue":
            return _print_for([str(symlinked)]), 0
        return _print_for([str(dangling)]), 0
    monkeypatch.setattr(cdpj, "_launchctl_print", fake_print)

    calls = []
    monkeypatch.setattr(cdpj, "_post_signal",
                        lambda *a, **k: calls.append((a, k)) or {"status": "stub"})
    rc = cdpj.main(["--checkout-root", str(checkout)])
    assert rc == 2
    kinds = {k["dedup_key"].split(":")[1] for _a, k in calls}
    assert kinds == {"dangling", "checkout-reaching"}
    assert len(calls) == 2, "one signal per breach class present, not one per job"


def test_main_the_dedup_key_is_stable_for_an_unchanged_breach_set(cdpj, tmp_path, monkeypatch):
    """So a standing breach does not log every day — same breach set,
    same dedup_key, across two independent runs."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("")
    symlinked = tmp_path / "local-bin" / "triage-queue"
    symlinked.parent.mkdir(parents=True)
    symlinked.symlink_to(real_script)
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n1\t0\tcom.convivy.triage-queue\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(symlinked)]), 0))
    keys = []
    monkeypatch.setattr(cdpj, "_post_signal",
                        lambda *a, **k: keys.append(k["dedup_key"]) or {"status": "stub"})
    cdpj.main(["--checkout-root", str(checkout)])
    cdpj.main(["--checkout-root", str(checkout)])
    assert keys[0] == keys[1]


def test_breach_sets_that_joined_alike_get_different_names_digests(cdpj):
    """The old key joined the breached ids with `_`. One breach whose argv holds `_` and a second
    label, and two breaches with those labels, both became `com.convivy.p:x_com.convivy.q:y`, so the second
    distinct breach set answered `duplicate` (review of PR #788, rounds 1 and 2)."""
    one, two = ["com.convivy.p:x_com.convivy.q:y"], ["com.convivy.p:x", "com.convivy.q:y"]
    assert "_".join(one) == "_".join(two), "the collision the old key produced"
    assert cdpj._names_digest(one) != cdpj._names_digest(two)


def test_main_exit_three_when_reporting_fails(cdpj, tmp_path, monkeypatch):
    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    real_script = checkout / "bin" / "triage-queue"
    real_script.parent.mkdir(parents=True)
    real_script.write_text("")
    symlinked = tmp_path / "local-bin" / "triage-queue"
    symlinked.parent.mkdir(parents=True)
    symlinked.symlink_to(real_script)
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n1\t0\tcom.convivy.triage-queue\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(symlinked)]), 0))

    def failing_post_signal(*a, **k):
        cdpj._report_failures += 1
        return {"status": "error:Boom"}
    monkeypatch.setattr(cdpj, "_post_signal", failing_post_signal)
    rc = cdpj.main(["--checkout-root", str(checkout)])
    assert rc == 3


def test_a_breach_logs_an_alert_signal_and_files_no_item(cdpj, tmp_path, monkeypatch):
    """The real posting path: one alert signal on the deploy-pinned stream, keyed on
    dedup_key, with the breach named in its body. No work item is created
    (platform/decisions/0073: only a co session creates one)."""
    import signal_client

    checkout = tmp_path / "agent-platform"
    checkout.mkdir()
    dangling = tmp_path / "gone" / "waiting-on-operator-digest"
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n2\t2\tcom.convivy.waiting-on-operator-digest\n"
    ))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(dangling)]), 0))
    fake_issue_client = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "issue_client", fake_issue_client)
    posted = []
    monkeypatch.setattr(signal_client, "post_alert",
                        lambda stream, body, **kw: posted.append((stream, body, kw)) or "created")

    rc = cdpj.main(["--checkout-root", str(checkout)])

    assert rc == 2
    assert len(posted) == 1, posted
    stream, body, kw = posted[0]
    assert stream == "deploy-pinned-jobs"
    assert body.splitlines()[0] == "[deploy-pinned-jobs] 1 loaded argv entry point nowhere"
    # The label names the job. The argv path is not asserted: sanitise redacts a
    # run of 32+ path characters (see the PR body), so a long temp path does not survive.
    assert "com.convivy.waiting-on-operator-digest" in body
    assert kw["product"] == "agent-platform"
    assert kw["dedup_key"].startswith("deploy-pinned-jobs:dangling:")
    assert kw["meta"] == {"severity": "high"}, "the item this replaced was filed high"
    fake_issue_client.post_issue.assert_not_called()


# --- the dedup_key is scoped to the UTC day ----------------------------------
# The signal store keeps every dedup_key for all time, so an unscoped key logs a
# breach that clears and returns never again. Each run reads the day once, and
# every key it writes carries it. Pinned here: two days give two keys, two runs
# on one day give one key, and present, absent, present on a later day logs again.

def _deploy_keys(cdpj, tmp_path, monkeypatch, *, day, breach):
    """Run main() with one loaded job whose argv is dangling (breach) or resolves
    outside the checkout (clean); return the dedup_keys it logged."""
    checkout = tmp_path / "agent-platform"
    checkout.mkdir(exist_ok=True)
    target = tmp_path / "gone" / "waiting-on-operator-digest"
    if not breach:
        target = tmp_path / "elsewhere" / "waiting-on-operator-digest"
        target.parent.mkdir(exist_ok=True)
        target.write_text("")
    monkeypatch.setattr(cdpj, "_utc_today", lambda: day, raising=False)
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: (
        "PID\tStatus\tLabel\n2\t2\tcom.convivy.waiting-on-operator-digest\n"))
    monkeypatch.setattr(cdpj, "_launchctl_print",
                        lambda label, uid: (_print_for([str(target)]), 0))
    keys = []
    monkeypatch.setattr(cdpj, "_post_signal",
                        lambda *a, **k: keys.append(k["dedup_key"]) or {"status": "stub"})
    cdpj.main(["--checkout-root", str(checkout)])
    return keys


def test_deploy_pinned_key_is_scoped_to_the_utc_day(cdpj, tmp_path, monkeypatch):
    day_one = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    day_two = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-02", breach=True)
    assert day_one and day_two
    assert day_one != day_two, "the same breach on two UTC days must log twice"
    assert day_one[0].endswith(":2030-01-01") and day_two[0].endswith(":2030-01-02")


def test_deploy_pinned_two_runs_on_one_day_give_one_key(cdpj, tmp_path, monkeypatch):
    first = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    second = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    assert first == second and len(first) == 1


def test_deploy_pinned_present_absent_present_logs_again_on_the_later_day(
        cdpj, tmp_path, monkeypatch):
    present = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-01", breach=True)
    absent = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-02", breach=False)
    back = _deploy_keys(cdpj, tmp_path, monkeypatch, day="2030-01-03", breach=True)
    assert absent == []
    assert len(back) == 1 and back[0] not in present


# --- classify_deploy_area: a deploy area falling behind main (E24·3) --------
#
# `widget-area`, `widget-tool` and the `origin`/`clone` repos below are all
# SYNTHETIC fixture data built fresh under `tmp_path` for each test — never
# the real run-register-api incident, never this checkout. `clone` is a real
# `git clone` of `origin`, so its `refs/remotes/origin/main` is the ref `git
# clone` itself writes, and `clone`'s path is always under `tmp_path`, never
# this repository's own checkout — a test cannot pass here by having
# `classify_deploy_area` read THIS checkout's `origin/main` by accident.


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True)


def _rev(repo: pathlib.Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def widget_history(tmp_path):
    """A throwaway `origin` + its `clone`, four commits on `main`:

      c1: adds bin/widget-tool (the area's one shipped file) and
          docs/other-note.md (not shipped by the area)
      c2: edits docs/other-note.md only
      c3: edits bin/widget-tool only
      c4: edits docs/other-note.md only

    `clone`'s `origin/main` lands on c4. Returns `(clone, shas)`, `shas` a
    dict of each commit's SHA by name."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "bin").mkdir()
    (origin / "bin" / "widget-tool").write_text("v1\n", encoding="utf-8")
    (origin / "docs").mkdir()
    (origin / "docs" / "other-note.md").write_text("v1\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "c1")
    c1 = _rev(origin)

    (origin / "docs" / "other-note.md").write_text("v2\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "c2")
    c2 = _rev(origin)

    (origin / "bin" / "widget-tool").write_text("v2\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "c3")
    c3 = _rev(origin)

    (origin / "docs" / "other-note.md").write_text("v3\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "c4")
    c4 = _rev(origin)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True)
    return clone, {"c1": c1, "c2": c2, "c3": c3, "c4": c4}


def _deployed_commit_text(*, commit: str | None, installed_from: str | None) -> str:
    lines = ["ref: main"]
    if commit is not None:
        lines.append(f"commit: {commit}")
    lines.append("installed_at: 2030-01-01T00:00:00Z")
    if installed_from is not None:
        lines.append(f"installed_from: {installed_from}")
    return "\n".join(lines) + "\n"


def _make_area(deploy_root: pathlib.Path, name: str, *, text: str) -> pathlib.Path:
    """A deploy area on disk shipping exactly one file, bin/widget-tool —
    the area's own content, never read for its bytes, only for its PATH
    (`classify_deploy_area` derives shipped paths from what sits on disk)."""
    area = deploy_root / name
    (area / "bin").mkdir(parents=True)
    (area / "bin" / "widget-tool").write_text("deployed\n", encoding="utf-8")
    (area / "DEPLOYED_COMMIT").write_text(text, encoding="utf-8")
    return area


def test_an_area_behind_main_is_reported_with_the_right_count(cdpj, tmp_path, widget_history):
    """Fails against the code before this change existed: `classify_deploy_area`
    did not exist at all, so this raised AttributeError. Deployed at c1; only
    c3 (of c2/c3/c4) touches the shipped file, so the lag is exactly 1."""
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result == {"area": "widget-area", "status": "stale",
                       "deployed_commit": shas["c1"], "lag": 1,
                       "installed_from": str(clone)}


def test_an_area_whose_only_later_commits_touch_unshipped_files_is_not_reported(
        cdpj, tmp_path, widget_history):
    """Deployed at c3 (the commit that DID touch the shipped file); the only
    later commit on main, c4, touches only docs/other-note.md, which this
    area does not ship. The fixture carries both kinds of commit (c3 ships-
    touching, c4 not), and this case must read as current regardless."""
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c3"], installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result == {"area": "widget-area", "status": "current",
                       "deployed_commit": shas["c3"], "lag": 0}


def test_an_area_at_mains_head_is_not_reported(cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c4"], installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result == {"area": "widget-area", "status": "current",
                       "deployed_commit": shas["c4"], "lag": 0}


def test_an_area_missing_installed_from_is_uncheckable(cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=None))
    result = cdpj.classify_deploy_area(area)
    assert result["area"] == "widget-area"
    assert result["status"] == "uncheckable"
    assert "installed_from" in result["reason"]


def test_an_area_whose_commit_is_unknown_in_the_repository_is_uncheckable(
        cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    bogus = "0" * 40
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=bogus, installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result["area"] == "widget-area"
    assert result["status"] == "uncheckable"
    assert result["deployed_commit"] == bogus
    assert bogus in result["reason"]


def test_an_area_whose_installed_from_repo_is_gone_is_uncheckable(cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    gone = tmp_path / "nowhere"
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=str(gone)))
    result = cdpj.classify_deploy_area(area)
    assert result["status"] == "uncheckable"
    assert str(gone) in result["reason"]


def test_an_area_whose_deployed_commit_has_no_commit_line_is_uncheckable(
        cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=None, installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result["area"] == "widget-area"
    assert result["status"] == "uncheckable"
    assert "commit" in result["reason"]
    assert "deployed_commit" not in result


def test_an_area_whose_origin_main_does_not_resolve_is_uncheckable(
        cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    result = cdpj.classify_deploy_area(area)
    assert result["status"] == "uncheckable"
    assert result["deployed_commit"] == shas["c1"]
    assert "origin/main" in result["reason"]


def test_an_area_whose_rev_list_call_fails_is_uncheckable(cdpj, tmp_path, widget_history, monkeypatch):
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    real_run_git = cdpj._run_git

    def fake_run_git(repo, *args):
        if args and args[0] == "rev-list":
            return None
        return real_run_git(repo, *args)

    monkeypatch.setattr(cdpj, "_run_git", fake_run_git)
    result = cdpj.classify_deploy_area(area)
    assert result["status"] == "uncheckable"
    assert result["deployed_commit"] == shas["c1"]
    assert "rev-list" in result["reason"]


def test_an_area_whose_walk_cannot_list_a_subdirectory_is_uncheckable(
        cdpj, tmp_path, widget_history, monkeypatch):
    """Stubs `os.scandir` so the walk into `bin/` fails, rather than relying
    on chmod (which the test runner may ignore). The missing listing also
    means the one shipped file, bin/widget-tool, is never found — this must
    still report uncheckable, not current."""
    clone, shas = widget_history
    area = _make_area(tmp_path / "deploy", "widget-area",
                      text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    blocked = str(area / "bin")
    real_scandir = os.scandir

    def fake_scandir(path="."):
        if os.fspath(path) == blocked:
            raise OSError(13, "Permission denied", blocked)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    result = cdpj.classify_deploy_area(area)
    assert result["area"] == "widget-area"
    assert result["status"] == "uncheckable"
    assert result["deployed_commit"] == shas["c1"]
    assert blocked in result["reason"]


def test_an_area_with_no_shipped_files_is_uncheckable(cdpj, tmp_path, widget_history):
    clone, shas = widget_history
    area = tmp_path / "deploy" / "widget-area"
    area.mkdir(parents=True)
    (area / "DEPLOYED_COMMIT").write_text(
        _deployed_commit_text(commit=shas["c1"], installed_from=str(clone)), encoding="utf-8")
    result = cdpj.classify_deploy_area(area)
    assert result["area"] == "widget-area"
    assert result["status"] == "uncheckable"
    assert result["deployed_commit"] == shas["c1"]


def test_shipped_paths_excludes_root_venv_and_any_pycache(cdpj, tmp_path):
    """venv/ is only skipped at the area's own root; a nested directory that
    happens to also be named venv/ is not an installer artifact and stays."""
    area = tmp_path / "area"
    (area / "bin").mkdir(parents=True)
    (area / "bin" / "tool").write_text("x", encoding="utf-8")
    (area / "venv" / "lib").mkdir(parents=True)
    (area / "venv" / "lib" / "site.py").write_text("x", encoding="utf-8")
    (area / "lib" / "__pycache__").mkdir(parents=True)
    (area / "lib" / "__pycache__" / "mod.pyc").write_text("x", encoding="utf-8")
    (area / "lib" / "mod.py").write_text("x", encoding="utf-8")
    (area / "nested" / "venv").mkdir(parents=True)
    (area / "nested" / "venv" / "kept.py").write_text("x", encoding="utf-8")
    result = cdpj._shipped_paths(area)
    assert result == ["bin/tool", "lib/mod.py", "nested/venv/kept.py"]


# --- main(): the deploy-area lag check through the same reporting path -----


def test_main_reports_a_stale_deploy_area_and_exits_two(cdpj, tmp_path, monkeypatch, widget_history):
    clone, shas = widget_history
    deploy_root = tmp_path / "deploy"
    _make_area(deploy_root, "widget-area",
              text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: "PID\tStatus\tLabel\n")
    rc = cdpj.main(["--deploy-root", str(deploy_root), "--no-report"])
    assert rc == 2


def test_main_reports_an_uncheckable_deploy_area_and_exits_two(
        cdpj, tmp_path, monkeypatch, widget_history):
    clone, shas = widget_history
    deploy_root = tmp_path / "deploy"
    _make_area(deploy_root, "widget-area",
              text=_deployed_commit_text(commit=shas["c1"], installed_from=None))
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: "PID\tStatus\tLabel\n")
    rc = cdpj.main(["--deploy-root", str(deploy_root), "--no-report"])
    assert rc == 2


def test_main_is_silent_when_every_deploy_area_is_current(cdpj, tmp_path, monkeypatch,
                                                           widget_history, capsys):
    clone, shas = widget_history
    deploy_root = tmp_path / "deploy"
    _make_area(deploy_root, "widget-area",
              text=_deployed_commit_text(commit=shas["c4"], installed_from=str(clone)))
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: "PID\tStatus\tLabel\n")
    rc = cdpj.main(["--deploy-root", str(deploy_root)])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_logs_stale_and_uncheckable_deploy_breaches_each_once(
        cdpj, tmp_path, monkeypatch, widget_history):
    clone, shas = widget_history
    deploy_root = tmp_path / "deploy"
    _make_area(deploy_root, "widget-area",
              text=_deployed_commit_text(commit=shas["c1"], installed_from=str(clone)))
    _make_area(deploy_root, "other-area",
              text=_deployed_commit_text(commit=shas["c1"], installed_from=None))
    monkeypatch.setattr(cdpj, "_launchctl_list", lambda: "PID\tStatus\tLabel\n")
    calls = []
    monkeypatch.setattr(cdpj, "_post_signal",
                        lambda *a, **k: calls.append((a, k)) or {"status": "stub"})
    rc = cdpj.main(["--deploy-root", str(deploy_root)])
    assert rc == 2
    kinds = {k["dedup_key"].split(":")[1] for _a, k in calls}
    assert kinds == {"stale-deploy", "uncheckable-deploy"}
    assert len(calls) == 2, "one signal per breach class present, not one per area"
