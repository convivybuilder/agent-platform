"""Tests for `operator_config`'s own branches no caller test reaches: a
config file whose JSON is not an object, and `config_path()`'s default
location when `$CONVIVY_OPERATOR_CONFIG` is unset.
"""
from __future__ import annotations

import json

import pytest

import operator_config


def test_get_raises_when_the_file_is_not_a_json_object(tmp_path, monkeypatch):
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(["not", "an", "object"]))
    monkeypatch.setenv(operator_config.ENV_VAR, str(path))
    with pytest.raises(operator_config.OperatorConfigError, match="must be a JSON object"):
        operator_config.get("excluded_crews")


def test_config_path_falls_back_to_the_default_under_home_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(operator_config.ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert operator_config.config_path() == tmp_path / ".config" / "convivy" / "operator.json"
