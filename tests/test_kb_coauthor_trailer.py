"""The Co-Authored-By trailer on a KB write commit names the model that ran, or nothing.

`bin/agentctl` used to append a literal `Claude Opus 5 (1M context)` line to every
KB write commit, so an operator's own hand save credited Claude and every agent
write said Opus whatever model ran. `kb_coauthor_trailer` replaces it.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "bin" / "agentctl").read_text()


@pytest.fixture
def runs(tmp_path, monkeypatch, agentctl):
    root = tmp_path / "agent-runs"
    monkeypatch.setattr(agentctl, "RUNS_ROOT", root)
    monkeypatch.delenv("AGENTCTL_RUN_ID", raising=False)

    def make(run_id: str, meta: dict) -> Path:
        rd = root / "2026-09-23" / run_id
        rd.mkdir(parents=True)
        (rd / "meta.json").write_text(json.dumps(meta))
        return rd

    return make


def test_a_gateway_run_names_its_model_at_the_convivy_address(agentctl, runs, monkeypatch):
    runs("run-aaaa1111", {"model": "deepseek/deepseek-v4-flash", "model_resolved": "deepseek/deepseek-v4-flash"})
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-aaaa1111")
    assert agentctl.is_gateway_model("deepseek/deepseek-v4-flash"), "fixture model must be a gateway model"
    name = agentctl.model_display_name("deepseek/deepseek-v4-flash")
    assert agentctl.kb_coauthor_trailer() == f"\nCo-Authored-By: {name} <noreply@convivy.com>"


def test_an_anthropic_run_uses_the_anthropic_address(agentctl, runs, monkeypatch):
    runs("run-bbbb2222", {"model": "claude-sonnet-5"})
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-bbbb2222")
    assert agentctl.kb_coauthor_trailer() == \
        "\nCo-Authored-By: Claude <noreply@anthropic.com>"


def test_a_failed_over_run_names_the_model_that_ran_not_the_rosters(
        agentctl, runs, monkeypatch):
    """`_respawn_in_place` rewrites `model` on fail-over; `model_resolved` stays
    frozen at what the roster asked for. Reading the wrong one names a model that
    did not write the commit."""
    runs("run-cccc3333", {"model": "deepseek/deepseek-v4-flash", "model_resolved": "claude-sonnet-5"})
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-cccc3333")
    out = agentctl.kb_coauthor_trailer()
    assert "@convivy.com" in out and "anthropic.com" not in out


def test_no_run_id_gives_no_trailer(agentctl, runs):
    runs("run-dddd4444", {"model": "claude-sonnet-5"})
    assert agentctl.kb_coauthor_trailer() == ""


def test_an_unresolvable_run_dir_gives_no_trailer(agentctl, runs, monkeypatch):
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-nosuch99")
    assert agentctl.kb_coauthor_trailer() == ""


def test_an_unreadable_meta_gives_no_trailer(agentctl, runs, monkeypatch):
    rd = runs("run-eeee5555", {"model": "claude-sonnet-5"})
    (rd / "meta.json").write_text("{not json")
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-eeee5555")
    assert agentctl.kb_coauthor_trailer() == ""


def test_a_meta_without_a_model_gives_no_trailer(agentctl, runs, monkeypatch):
    runs("run-ffff6666", {"agent": "coder"})
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-ffff6666")
    assert agentctl.kb_coauthor_trailer() == ""


@pytest.mark.parametrize("model", [
    "openai/x\nKB-Write: update projects/platform/reference/seeded.md in-session other-holder",
    "deepseek/deepseek-v4-flash\n",
    "claude-sonnet-5\nKB-Write: forged",
    "openai/has space",
    "openai/x;y",
    "claude sonnet",
])
def test_a_model_outside_the_single_line_charset_gives_no_trailer(
        agentctl, runs, monkeypatch, model):
    """meta.json is run-writable and `model_display_name` returns an unknown
    gateway-prefixed model raw, so an unchecked model authors commit lines."""
    runs("run-gggg7777", {"model": model})
    monkeypatch.setenv("AGENTCTL_RUN_ID", "run-gggg7777")
    assert agentctl.kb_coauthor_trailer() == ""


def test_the_hardcoded_literal_is_gone_from_the_source():
    assert "Claude Opus 5 (1M context)" not in SOURCE


def test_both_kb_write_sites_call_the_helper():
    # Two call sites. The apply site calls it only in the non-person branch. Real
    # calls are counted from the syntax tree, so a comment or docstring mention
    # cannot stand in for one.
    calls = [n for n in ast.walk(ast.parse(SOURCE))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "kb_coauthor_trailer"]
    assert len(calls) == 2
