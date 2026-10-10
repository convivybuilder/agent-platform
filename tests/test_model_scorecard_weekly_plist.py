"""com.convivy.model-scorecard-weekly is a live, loaded launchd job whose definition
existed nowhere but ~/Library/LaunchAgents on this machine (issue_01M19RRWSS9ETR09N9M2BJPA0Q).

48b8a7a repointed the 26 plists that DID exist in this repo at the shared deploy
area and correctly noted that a second model-scorecard plist wasn't among them.
It was live and running, reading `~/.local/bin/model-scorecard` straight into the
checkout, exactly what the deploy-pin migration exists to stop — a plist living in
no repository at all, one step past ADR-0059 rule 8's "a plist living in a
checkout". A reprovision of this machine would have lost the job's definition
silently.

This pins the recovered file: it must exist, keep the live schedule/arguments/log
paths, and read the deploy area rather than ~/.local/bin.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLIST = REPO / "launchd" / "com.convivy.model-scorecard-weekly.plist"
DEPLOY_AREA = "__HOME__/.local/deploy/agent-platform-shared"


def _load():
    assert PLIST.exists(), (
        f"{PLIST} is missing. com.convivy.model-scorecard-weekly is loaded and running "
        f"on this machine but its definition is not committed anywhere — recover it "
        f"from ~/Library/LaunchAgents/com.convivy.model-scorecard-weekly.plist."
    )
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_exists_and_parses():
    _load()


def test_program_arguments_read_the_deploy_area_not_the_checkout_symlink():
    data = _load()
    args = data["ProgramArguments"]
    assert args[0] == f"{DEPLOY_AREA}/venv/bin/python3", (
        "must exec the deploy area's pinned interpreter (rule 4), not launchd's "
        "PATH or a shebang"
    )
    assert args[1] == f"{DEPLOY_AREA}/bin/model-scorecard", (
        "must read the deploy-area copy (rules 2, 6, 7), not "
        "~/.local/bin/model-scorecard — that symlink still resolves into the "
        "working checkout, which is exactly what this migration removes from the "
        "unattended firing path (rule 1)"
    )
    assert "/.local/bin/model-scorecard" not in " ".join(args)


def test_preserves_the_live_rollup_arguments():
    data = _load()
    args = data["ProgramArguments"][2:]
    assert args == ["--days", "14", "--rollup", "--post", "--judge"], (
        "this job's flags (--rollup --post --judge, the Monday digest) differ from "
        "com.convivy.model-scorecard's daily --days 14 scan; recovering it must not "
        "collapse the two into duplicates of each other"
    )


def test_preserves_the_live_monday_schedule():
    data = _load()
    assert data["StartCalendarInterval"] == {"Weekday": 1, "Hour": 8, "Minute": 0}


def test_preserves_the_live_log_paths():
    data = _load()
    assert data["StandardOutPath"] == (
        "__HOME__/Convivy/agent-runs/model-scorecard-weekly.out.log"
    )
    assert data["StandardErrorPath"] == (
        "__HOME__/Convivy/agent-runs/model-scorecard-weekly.err.log"
    )


def test_working_directory_is_the_deploy_area():
    data = _load()
    assert data.get("WorkingDirectory") == DEPLOY_AREA


def _curated_activation_line(install_sh: str) -> str:
    # The curated block (distinct from the earlier `for label in $new_plists`
    # announcement loop) is the one that names actual job labels.
    return next(
        line for line in install_sh.splitlines()
        if line.strip().startswith("for label in com.")
    )


def test_install_sh_activation_list_names_the_recovered_job():
    install_sh = (REPO / "install.sh").read_text()
    activation_line = _curated_activation_line(install_sh)
    assert "com.convivy.model-scorecard-weekly" in activation_line, (
        "the curated activation block must cover the recovered job like the rest"
    )


def test_install_sh_activation_list_drops_the_nonexistent_pa_job():
    install_sh = (REPO / "install.sh").read_text()
    activation_line = _curated_activation_line(install_sh)
    assert "com.convivy.model-scorecard-pa" not in activation_line, (
        "com.convivy.model-scorecard-pa is neither a file in launchd/ nor a job loaded "
        "on this machine — a name in the activation list that resolves to nothing"
    )
    assert not (REPO / "launchd" / "com.convivy.model-scorecard-pa.plist").exists()
