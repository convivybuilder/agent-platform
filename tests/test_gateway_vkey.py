"""`lib/agent-platform/gateway-vkey` — one key reader for all three consumers.

An SSH-originated session cannot read the login keychain AT ALL, and every co
session on this machine arrives over SSH or mosh, so the file is the only path
from where `co` and `oc` actually run. `agentctl` learned this in PR #485; this
is the same rule for the two launchers the operator runs by hand.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

VKEY = Path(__file__).resolve().parent.parent / "lib" / "agent-platform" / "gateway-vkey"

# PREFER dash WHERE IT EXISTS, because that is what the CI runner's /bin/sh IS.
# macOS's /bin/sh is bash in posix mode and accepts bashisms like `[[`, so a
# first cut passed on darwin and failed on the runner with "[[: not found".
# Running the suite under dash locally is what makes this check as wide as CI's.
_SH = shutil.which("dash") or "sh"


def _run(home: Path, service: str, *, keychain: bool = False):
    """Run the helper with a STUBBED `security` unless a test asks otherwise.

    WHY STUB IT. Two of these tests assert the Keychain fallback FAILS. They
    passed here only because this session is SSH-locked, so the real `security`
    returns exit 36 — they were measuring the ambient session type, not the code.
    From a console Terminal on this machine the real binary would succeed and
    return the real key, flipping both to failures; on Linux CI it passes for a
    third unrelated reason, that `security` does not exist. Three environments,
    three different reasons, none of them the behaviour under test (reviewer,
    PR #488).

    `keychain=True` puts a stub that SUCCEEDS on PATH, so the fallback's success
    path is exercised deliberately rather than by accident of where it runs.
    """
    stub_dir = home / "_stub_bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "security"
    if keychain:
        stub.write_text("#!/bin/sh\nprintf 'sk-from-keychain\\n'\n")
    else:
        stub.write_text("#!/bin/sh\nexit 44\n")     # errSecItemNotFound
    stub.chmod(0o755)
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{stub_dir}:/bin:/usr/bin:/usr/local/bin:/opt/homebrew/bin"}
    # A developer's own override must not move the default directory these tests assume.
    env.pop("LLM_GATEWAY_CONFIG_DIR", None)
    return subprocess.run([_SH, str(VKEY), service], capture_output=True,
                          text=True, timeout=30, env=env)


def _write_keys(home: Path, payload, mode=0o600):
    d = home / ".config" / "convivy"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "litellm-keys.json"
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    f.chmod(mode)
    return f


def test_the_file_is_read_before_the_keychain(tmp_path):
    """The whole point: from an SSH session the keychain is unreachable, so the
    file has to answer."""
    _write_keys(tmp_path, {"litellm-co-virtual-key": {"account": "co", "key": "sk-from-file"}})
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "sk-from-file"


def test_a_world_readable_file_is_an_ERROR_not_a_fallback(tmp_path):
    """Falling back would surface later as a confusing Keychain failure and, on a
    machine where Keychain works, would hide a leaking credential file
    indefinitely."""
    _write_keys(tmp_path, {"litellm-co-virtual-key": {"key": "sk-leaky"}}, mode=0o644)
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert cp.returncode != 0
    assert "readable beyond its owner" in cp.stderr
    assert "chmod 600" in cp.stderr


def test_malformed_JSON_is_an_ERROR_not_a_fallback(tmp_path):
    _write_keys(tmp_path, "{not json")
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert cp.returncode != 0
    assert "not valid JSON" in cp.stderr


def test_an_unreplaced_placeholder_is_named(tmp_path):
    """The template ships PASTE_ placeholders; sending one as a bearer token
    produces a gateway auth error that says nothing about the real cause."""
    _write_keys(tmp_path, {"litellm-co-virtual-key": {"key": "PASTE_CO_KEY_HERE"}})
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert cp.returncode != 0
    assert "placeholder" in cp.stderr


def test_a_missing_ENTRY_falls_through_quietly(tmp_path):
    """Absent from the file is 'not adopted for this consumer yet' — a safe state
    to fall back from, and it must not print the internal sentinel."""
    _write_keys(tmp_path, {"litellm-agentctl-virtual-key": {"key": "sk-other"}})
    cp = _run(tmp_path, "litellm-co-virtual-key")
    # Keychain is unreachable in a test env, so this fails — but on the FALLBACK
    # message, never on the sentinel.
    assert "__ABSENT__" not in cp.stderr and "__ABSENT__" not in cp.stdout
    assert "Keychain refused it" in cp.stderr


def test_no_file_at_all_falls_through_quietly(tmp_path):
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert "__ABSENT__" not in cp.stderr
    assert cp.returncode != 0


@pytest.mark.parametrize("script,service", [
    ("co", "litellm-co-virtual-key"),
    ("oc", "litellm-opencode-virtual-key"),
])
def test_the_launchers_call_the_shared_reader(script, service):
    """Both launchers must go through one reader rather than each growing its own
    keychain call — the drift that left agentctl fixed and these two broken."""
    src = (Path(__file__).resolve().parent.parent / "bin" / script).read_text()
    assert "gateway-vkey" in src and service in src
    assert f"security find-generic-password -w -s {service}" not in src, (
        f"bin/{script} still reads the keychain directly")


def test_the_keychain_fallback_is_used_when_the_file_has_no_entry(tmp_path):
    """The fallback's SUCCESS path, exercised deliberately with a stub rather
    than left to whether the machine happens to have a reachable Keychain."""
    _write_keys(tmp_path, {"litellm-agentctl-virtual-key": {"key": "sk-other"}})
    cp = _run(tmp_path, "litellm-co-virtual-key", keychain=True)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "sk-from-keychain"


def test_the_final_error_names_the_FILE_SHAPE_not_just_a_read_command(tmp_path):
    """Whoever hits this path is at the moment they most need help. The first
    version said "add the key there" and then printed a Keychain READ — a command
    that adds nothing, anywhere, and never named the JSON shape (reviewer)."""
    cp = _run(tmp_path, "litellm-co-virtual-key")
    assert cp.returncode != 0
    err = cp.stderr
    assert "litellm-keys.json" in err
    assert '"key"' in err, "the message must show the JSON shape to write"
    assert "chmod 600" in err
