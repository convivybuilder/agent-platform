"""PR-2 routing helper tests (Phase C, ARCH-102).

Pins the three load-bearing safety properties:
  1. Anthropic models never get the gateway env (byte-identical path unchanged).
  2. Gateway aliases route correctly (ANTHROPIC_BASE_URL + virtual key injected).
  3. Unknown literals fail closed to Anthropic-direct (the critical safety test).
"""
from __future__ import annotations

import yaml
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATEWAY_CONFIG = REPO / "gateway" / "config.yaml"


def test_is_gateway_model_anthropic_returns_false(agentctl):
    """Every known Anthropic model ID returns False — they never route to the proxy."""
    anthropic_ids = [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        agentctl.model_id("opus"),
        agentctl.model_id("sonnet"),
        agentctl.model_id("haiku"),
        agentctl.DEFAULT_MODEL,
    ]
    for mid in anthropic_ids:
        assert agentctl.is_gateway_model(mid) is False, (
            f"is_gateway_model({mid!r}) should be False (Anthropic model)"
        )


def test_is_gateway_model_gateway_aliases_return_true(agentctl):
    """Every gateway alias value (resolved through model_id) returns True.

    Includes the ARCH-201 trial aliases mimo (openai/ prefix) and glm (the new
    zai/ prefix). glm's presence is load-bearing: it proves GATEWAY_MODEL_PREFIXES
    was extended with "zai/", without which glm fails closed to Anthropic-direct
    and never reaches the gateway."""
    for alias in ("gpt5", "deepseek", "kimi", "mimo", "glm"):
        resolved = agentctl.model_id(alias)
        assert agentctl.is_gateway_model(resolved) is True, (
            f"is_gateway_model({resolved!r}) should be True (alias={alias!r})"
        )


def test_is_gateway_model_unknown_literal_fails_closed_to_direct(agentctl):
    """Unknown literal returns False — fail-closed to Anthropic-direct.

    This is the load-bearing safety test. An agent with an unrecognised model
    alias must never silently route to a proxy that can't serve it.
    """
    unknowns = [
        "some-random-model-id",
        "gpt-4o",           # not in our gateway aliases
        "claude-future-9",
        "anthropic/claude-sonnet",  # a provider-prefixed Anthropic model we don't serve
        "",
    ]
    for mid in unknowns:
        assert agentctl.is_gateway_model(mid) is False, (
            f"is_gateway_model({mid!r}) should be False (unknown literal, fail-closed)"
        )


def test_gateway_env_anthropic_is_byte_identical(agentctl):
    """gateway_env for an Anthropic model returns base_env unchanged.

    Dict-equality: no extra keys, no modified values. This is the
    'Anthropic path unchanged' guarantee pinned mechanically.
    """
    base = {"FOO": "bar", "ANTHROPIC_API_KEY": "real-key"}
    result = agentctl.gateway_env(
        "claude-opus-4-7", base,
        virtual_key="vk", gateway_url="http://127.0.0.1:4000",
    )
    assert result == base
    assert set(result.keys()) == set(base.keys())


def test_gateway_env_gateway_model_adds_base_url_and_key(agentctl):
    """gateway_env for a gateway model injects ANTHROPIC_BASE_URL and virtual key."""
    base = {"FOO": "bar", "OTHER": "value"}
    result = agentctl.gateway_env(
        "openai/gpt-5", base,
        virtual_key="sk-virt-key-123",
        gateway_url="http://127.0.0.1:4000",
    )
    assert result["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert result["ANTHROPIC_API_KEY"] == "sk-virt-key-123"
    assert result["FOO"] == "bar"
    assert result["OTHER"] == "value"


def test_gateway_env_does_not_mutate_base_env(agentctl):
    """gateway_env must not modify the base_env dict in place."""
    base = {"X": "1"}
    original_keys = set(base.keys())
    agentctl.gateway_env(
        "deepseek/deepseek-v4-flash", base,
        virtual_key="vk", gateway_url="http://127.0.0.1:4000",
    )
    assert set(base.keys()) == original_keys


@pytest.mark.skipif(
    not GATEWAY_CONFIG.exists(),
    reason="gateway/config.yaml not present (PR-1 not yet merged); skipped"
)
def test_gateway_aliases_match_yaml_model_list(agentctl):
    """MODEL_ALIASES gateway entries must be in lockstep with gateway/config.yaml.

    Each gateway alias key must appear as a model_name in the YAML, and the alias
    value must match the corresponding litellm_params.model. A coder editing one
    without the other fails this gate. Skipped when PR-1 is not yet merged.
    """
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    yaml_models = {
        entry["model_name"]: entry["litellm_params"]["model"]
        for entry in config.get("model_list", [])
    }
    gateway_aliases = {
        k: v for k, v in agentctl.MODEL_ALIASES.items()
        if agentctl.is_gateway_model(v)
    }
    assert gateway_aliases, "No gateway aliases found in MODEL_ALIASES"
    for alias_key, alias_value in gateway_aliases.items():
        assert alias_key in yaml_models, (
            f"MODEL_ALIASES key {alias_key!r} has no matching model_name in "
            f"gateway/config.yaml model_list"
        )
        assert yaml_models[alias_key] == alias_value, (
            f"MODEL_ALIASES[{alias_key!r}] = {alias_value!r} but "
            f"config.yaml model_name={alias_key!r} has litellm_params.model="
            f"{yaml_models[alias_key]!r} — update them in sync"
        )


def test_gateway_url_returns_loopback(agentctl):
    assert agentctl._gateway_url() == "http://127.0.0.1:4000"


# --- x-agentctl-task header sanitization (gateway rejects non-ASCII headers) ---

def test_header_task_label_strips_non_ascii(agentctl):
    """Em dash / emoji / accents are dropped — the proxy 400s on non-ASCII headers."""
    label = agentctl._header_task_label("fix bug — the café crashed 🔥")
    assert label == "fix bug  the caf crashed "
    assert all(" " <= c <= "~" for c in label)


def test_header_task_label_collapses_newlines_and_truncates(agentctl):
    assert agentctl._header_task_label("line1\nline2\rline3") == "line1 line2 line3"
    assert len(agentctl._header_task_label("x" * 200)) == 80


def test_header_task_label_handles_none_and_empty(agentctl):
    assert agentctl._header_task_label(None) == ""
    assert agentctl._header_task_label("") == ""
