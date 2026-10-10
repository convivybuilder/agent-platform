"""`sonnet-pre828`/`sonnet-post828`/`sonnet-fix832` — the review-replay eval
harness's arms for the reviewer/security-reviewer wording before and after
knowledge#828 (merged 2026-10-08, commit
7f9a8c2df20772611ac9b41e81b633c28a26620f), which rewrote both roster
definitions, and after knowledge#832 (merged, commit
9a57c465b4d1335d79375d944c7921945ed97a1a), which adds the paragraph that a
defect with a concrete way it goes wrong is a finding at its real severity
and that the occurrence bar covers only a proposed guard. Every other arm in
`bin/review-eval`'s STANDARD_ARMS resolves the Library's LIVE definition, so
it moved to each new wording the moment the knowledge PR merged; these three
arms pin one side each via `tools/review-eval/pre828-agents`,
`tools/review-eval/post828-agents` and `tools/review-eval/fix832-agents`, the
same `--agents-dir` mechanism `tools/review-eval/variant-agents` uses for its
`sonnet-variant-prompt` arm.

The three directories hold frozen snapshots of `convivy/knowledge`'s
`projects/agent-platform/agents/{reviewer,security-reviewer}.md` at a pinned
commit each (pre828-agents: c4ccad6433d3ce3a4488fc462c32642c4ecd94b7,
post828-agents: 7f9a8c2df20772611ac9b41e81b633c28a26620f, fix832-agents:
9a57c465b4d1335d79375d944c7921945ed97a1a). The sha256 pins below are what
make an accidental future edit to any of these files fail CI, instead of
silently drifting the arm away from the exact wording it exists to measure.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "agent-platform"))
import review_eval_replay as RER  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PRE828_DIR = REPO / "tools" / "review-eval" / "pre828-agents"
POST828_DIR = REPO / "tools" / "review-eval" / "post828-agents"
FIX832_DIR = REPO / "tools" / "review-eval" / "fix832-agents"

_EXPECTED_SHA256 = {
    PRE828_DIR / "reviewer.md":
        "bc194558f9dd8aad05064cc62e534d2d54563b983995ede35e86d9ac73480253",
    PRE828_DIR / "security-reviewer.md":
        "3d272ac1debc81ec1e681baa049413f8ff527518ce450ec944853ecd4f873dd7",
    POST828_DIR / "reviewer.md":
        "fe8baaa645607e5def0eff2971c469c1ca4a06ea60f7ce1220d1d531c31b746a",
    POST828_DIR / "security-reviewer.md":
        "94de032b6103e76411a9846ea7bfb19ea6c514212569451fb136c54bf8a8523a",
    FIX832_DIR / "reviewer.md":
        "7c5d746a1ff6ea1bdd9e0dfeef4e9173535d3e1893089c6fceb30aee0ed40c3c",
    FIX832_DIR / "security-reviewer.md":
        "560b3fcecf9d86fd6c49498d03bfdaa71a9a5d78e22fcbee6b9c366971d26874",
}


def _entry(tmp_path, role="reviewer"):
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    user_path = prompts / "user.md"
    user_path.write_text("synthetic brief text\n")
    return {"entry_id": f"agent-platform/1/{role}/aaaaaaaaaaaa", "repo": "agent-platform",
           "pr": "1", "role": role, "sha": "a" * 40, "kind": "confirmed",
           "user_prompt_path": str(user_path)}


def test_sonnet_pre828_and_sonnet_post828_are_known_arms(review_eval_cli):
    assert "sonnet-pre828" in review_eval_cli.STANDARD_ARMS
    assert "sonnet-post828" in review_eval_cli.STANDARD_ARMS


def test_sonnet_pre828_and_sonnet_post828_default_to_the_baseline_subset(review_eval_cli):
    assert "sonnet-pre828" in review_eval_cli.SUBSET_DEFAULT_KEYS
    assert "sonnet-post828" in review_eval_cli.SUBSET_DEFAULT_KEYS


def test_sonnet_pre828_and_sonnet_post828_share_the_sonnet_5_5_baselines_effort(review_eval_cli):
    baseline = review_eval_cli.STANDARD_ARMS["sonnet-5-5"]
    assert review_eval_cli.STANDARD_ARMS["sonnet-pre828"].effort == baseline.effort
    assert review_eval_cli.STANDARD_ARMS["sonnet-post828"].effort == baseline.effort
    assert review_eval_cli.STANDARD_ARMS["sonnet-pre828"].model == "sonnet"
    assert review_eval_cli.STANDARD_ARMS["sonnet-post828"].model == "sonnet"


def test_sonnet_pre828_arm_builds_argv_with_the_pre828_agents_dir(review_eval_cli, tmp_path):
    arm = review_eval_cli.STANDARD_ARMS["sonnet-pre828"]
    argv = RER.build_spawn_argv(_entry(tmp_path), arm)
    assert argv[argv.index("--agents-dir") + 1] == str(PRE828_DIR)


def test_sonnet_post828_arm_builds_argv_with_the_post828_agents_dir(review_eval_cli, tmp_path):
    arm = review_eval_cli.STANDARD_ARMS["sonnet-post828"]
    argv = RER.build_spawn_argv(_entry(tmp_path), arm)
    assert argv[argv.index("--agents-dir") + 1] == str(POST828_DIR)


def test_sonnet_pre828_and_sonnet_post828_resolve_to_different_directories(review_eval_cli):
    pre = review_eval_cli.STANDARD_ARMS["sonnet-pre828"].agents_dir
    post = review_eval_cli.STANDARD_ARMS["sonnet-post828"].agents_dir
    assert pre != post


def test_sonnet_fix832_is_a_known_arm(review_eval_cli):
    assert "sonnet-fix832" in review_eval_cli.STANDARD_ARMS


def test_sonnet_fix832_defaults_to_the_baseline_subset(review_eval_cli):
    assert "sonnet-fix832" in review_eval_cli.SUBSET_DEFAULT_KEYS


def test_sonnet_fix832_shares_the_sonnet_5_5_baselines_effort(review_eval_cli):
    baseline = review_eval_cli.STANDARD_ARMS["sonnet-5-5"]
    assert review_eval_cli.STANDARD_ARMS["sonnet-fix832"].effort == baseline.effort
    assert review_eval_cli.STANDARD_ARMS["sonnet-fix832"].model == "sonnet"


def test_sonnet_fix832_arm_builds_argv_with_the_fix832_agents_dir(review_eval_cli, tmp_path):
    arm = review_eval_cli.STANDARD_ARMS["sonnet-fix832"]
    argv = RER.build_spawn_argv(_entry(tmp_path), arm)
    assert argv[argv.index("--agents-dir") + 1] == str(FIX832_DIR)


def test_sonnet_fix832_resolves_to_a_directory_distinct_from_pre828_and_post828(review_eval_cli):
    pre = review_eval_cli.STANDARD_ARMS["sonnet-pre828"].agents_dir
    post = review_eval_cli.STANDARD_ARMS["sonnet-post828"].agents_dir
    fix = review_eval_cli.STANDARD_ARMS["sonnet-fix832"].agents_dir
    assert fix != pre
    assert fix != post


def test_both_pinned_directories_carry_reviewer_and_security_reviewer():
    for d in (PRE828_DIR, POST828_DIR, FIX832_DIR):
        assert (d / "reviewer.md").is_file()
        assert (d / "security-reviewer.md").is_file()


def test_pinned_files_match_their_recorded_sha256():
    for path, expected in _EXPECTED_SHA256.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, f"{path} has drifted from its pinned hash"
