"""`agentctl models` (platform/contracts/agentctl-models).

Deck's Item pane took free text for a model id; the Library stored it and
`agentctl spawn --issue` refused it at dispatch. The fix is a command that
lists exactly the models a dispatch accepts, built from the same two tables
`_model_known` reads (MODEL_ALIASES, _MODEL_TIERS), so the picker and the
dispatch check cannot disagree about a listed name (issue_01M3N0F35X7QQKMZKKHQ6AR1RG).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTCTL = REPO / "bin" / "agentctl"


def _models_args(*, as_json=False):
    return argparse.Namespace(json=as_json)


def test_every_listed_model_passes_model_known(agentctl):
    models = agentctl._list_models()
    assert models, "the contract's table is empty"
    for entry in models:
        assert agentctl._model_known(entry["model"]), entry


def test_every_alias_and_tier_id_appears_exactly_once(agentctl):
    models = agentctl._list_models()
    listed = [m["model"] for m in models]
    assert len(listed) == len(set(listed)), f"a model appears twice: {listed}"
    expected = set(agentctl.MODEL_ALIASES.values()) | set(agentctl._MODEL_TIERS)
    assert set(listed) == expected


def test_order_is_aliases_then_unaliased_tiers(agentctl):
    """MODEL_ALIASES entries first, in dict order; then _MODEL_TIERS ids no
    alias maps to, in _MODEL_TIERS order, alias null."""
    models = agentctl._list_models()
    aliased_models = set(agentctl.MODEL_ALIASES.values())
    expected_order = list(agentctl.MODEL_ALIASES.values()) + [
        m for m in agentctl._MODEL_TIERS if m not in aliased_models
    ]
    assert [m["model"] for m in models] == expected_order
    for entry, model in zip(models, expected_order):
        if model in agentctl.MODEL_ALIASES.values():
            # the FIRST alias mapping to this model, per dict order
            expected_alias = next(
                a for a, mid in agentctl.MODEL_ALIASES.items() if mid == model
            )
            assert entry["alias"] == expected_alias
        else:
            assert entry["alias"] is None


def test_provider_matches_is_gateway_model(agentctl):
    for entry in agentctl._list_models():
        want = "gateway" if agentctl.is_gateway_model(entry["model"]) else "anthropic"
        assert entry["provider"] == want, entry


def test_pins_a_literal_entry_so_equivalence_checks_cant_all_pass_on_a_bad_list(agentctl):
    """The equivalence checks above would still pass a `_list_models` that
    built its rows from some other table agreeing on membership and order;
    pin one literal row from the contract's own example so that can't happen."""
    models = agentctl._list_models()
    by_alias = {m["alias"]: m for m in models}
    assert by_alias["opus"] == {
        "alias": "opus", "model": "claude-opus-5-5", "provider": "anthropic",
    }
    assert by_alias["fable"] == {
        "alias": "fable", "model": "claude-fable-5-1", "provider": "anthropic",
    }
    by_model = {m["model"]: m for m in models}
    assert by_model["claude-opus-5"] == {
        "alias": None, "model": "claude-opus-5", "provider": "anthropic",
    }


def test_model_known_still_accepts_the_pre_repoint_ids(agentctl):
    """The 2026-09-28 opus/fable repoint keeps a work item or script naming the
    old id dispatchable: `claude-opus-5` through `_MODEL_TIERS` (still a real
    fail-over tier), `claude-fable-5` through `_RETIRED_MODEL_IDS`."""
    assert agentctl._model_known("claude-opus-5")
    assert agentctl._model_known("claude-fable-5")


def test_cmd_models_json_output(agentctl, capsys):
    rc = agentctl.cmd_models(_models_args(as_json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.endswith("\n")
    payload = json.loads(out)
    assert payload["schema_version"] == 1
    assert payload["models"] == agentctl._list_models()


def test_cmd_models_table_output_is_not_json(agentctl, capsys):
    rc = agentctl.cmd_models(_models_args(as_json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-opus-5" in out
    try:
        json.loads(out)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("table output parsed as JSON; --json path leaked")


def test_main_models_json_prints_valid_json_and_exits_0(agentctl, capsys):
    rc = agentctl.main(["models", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert any(m["alias"] == "opus" for m in payload["models"])


def test_subprocess_models_json_prints_valid_json_and_exits_0():
    """The real CLI entry point, as a subprocess — not the in-process fixture
    — so an argparse wiring mistake (subcommand never registered, wrong dest)
    can't hide behind the fixture's own import machinery."""
    cp = subprocess.run(
        [sys.executable, str(AGENTCTL), "models", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["schema_version"] == 1
    assert payload["models"]

