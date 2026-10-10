"""com.convivy.model-scorecard-hollis is a launchd job whose definition existed
nowhere but ~/Library/LaunchAgents on this machine — the same gap
com.convivy.model-scorecard-weekly closed for the Monday rollup, found this time
by this PR's own audit of what #469/#470 left behind.

This pins the recovered file, deploy-pinned from the start (unlike
model-scorecard-weekly, which was recovered once and repointed later), and
pins that it stays UNLOADED: recovering the definition into git must not be
read as also loading the job.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLIST = REPO / "launchd" / "com.convivy.model-scorecard-hollis.plist"
DEPLOY_AREA = "__HOME__/.local/deploy/agent-platform-shared"


def _load():
    assert PLIST.exists(), f"{PLIST} is missing"
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_plist_exists_and_parses():
    _load()


def test_program_arguments_read_the_deploy_area_not_the_checkout_symlink():
    data = _load()
    args = data["ProgramArguments"]
    assert args[0] == f"{DEPLOY_AREA}/venv/bin/python3"
    assert args[1] == f"{DEPLOY_AREA}/bin/model-scorecard"
    assert "/.local/bin/model-scorecard" not in " ".join(args)


def test_preserves_the_live_repo_scorecard_arguments():
    data = _load()
    args = data["ProgramArguments"][2:]
    assert args == ["--repo-scorecard", "hollis", "--days", "14"], (
        "this job's flags (the per-repo hollis scorecard) differ from both "
        "com.convivy.model-scorecard's daily scan and model-scorecard-weekly's "
        "rollup; recovering it must not collapse it into either"
    )


def test_preserves_the_live_schedule():
    data = _load()
    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 35}


def test_run_at_load_is_false():
    data = _load()
    assert data["RunAtLoad"] is False


def test_preserves_the_live_log_paths():
    data = _load()
    assert data["StandardOutPath"] == (
        "__HOME__/Convivy/agent-runs/model-scorecard-hollis.out.log"
    )
    assert data["StandardErrorPath"] == (
        "__HOME__/Convivy/agent-runs/model-scorecard-hollis.err.log"
    )


def test_working_directory_is_the_deploy_area():
    data = _load()
    assert data.get("WorkingDirectory") == DEPLOY_AREA


def test_install_sh_does_not_add_this_job_to_the_activation_list():
    """The job is currently NOT loaded on the machine, and committing this
    definition must not silently start loading it — that is a separate,
    deliberate step. install.sh's curated from-scratch activation block must
    not name it."""
    install_sh = (REPO / "install.sh").read_text()
    activation_line = next(
        line for line in install_sh.splitlines()
        if line.strip().startswith("for label in com.")
    )
    assert "com.convivy.model-scorecard-hollis" not in activation_line
