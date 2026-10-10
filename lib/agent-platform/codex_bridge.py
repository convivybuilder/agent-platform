"""Install and verify the Codex hook bridge in a CODEX_HOME.

The bridge is one entry in `$CODEX_HOME/hooks.json`: a PreToolUse hook with no
matcher that runs tools/codex-hook-shim.py, which in turn runs block-pr-merge.sh
and outbound-guard.py (see the shim's docstring for why no matcher). `agentctl`
refuses to dispatch a Codex run unless `verify()` returns no problems.

THREE THINGS A HOOK MUST BE BEFORE IT BINDS A RUN, and what this module can see
of each:

  present   hooks.json holds the matcher-free shim entry, and the shim and both
            guards exist. Checked directly.
  unmodified  hooks.json, the shim and both guards hash to what install() wrote
            into the manifest. Checked directly. A file edited after install is
            the case Codex's own `modified` status exists for, and passing
            `--dangerously-bypass-hook-trust` would run it anyway, which is why
            that flag is not how this lane establishes trust.
  trusted   Codex keeps `[hooks.state."<key>"] trusted_hash` in config.toml and
            does not run an untrusted hook. Checked only as far as a state entry
            for OUR hooks.json exists with a hash and is not disabled. WE DO NOT
            KNOW HOW CODEX COMPUTES THAT HASH OR FORMS THAT KEY (the binary's
            HookStateToml names `enabled` and `trusted_hash` and nothing more), so
            this cannot detect a hash Codex would call stale. install() cannot
            create trust either: someone reviews the hook in Codex (`/hooks`),
            which writes the entry. Until they do, verify() reports it untrusted
            and the dispatch is refused, which is the intended state of a fresh
            install. The first Codex run (slice 2) is what shows whether this
            reading of the key is right.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import builder_root as BR

SHIM = "codex-hook-shim.py"
GUARDS = ("block-pr-merge.sh", "block-pkill-option-order.sh", "outbound-guard.py")
SHAPE = "codex_tool_shape.py"  # imported by the shim and by outbound-guard
MANIFEST = "agent-platform-hook-bridge.json"
_PYTHON = "/usr/bin/python3"  # the interpreter outbound-guard already runs under
TIMEOUT_S = 30


def install_hint() -> str:
    """The command that writes hooks.json and the manifest into the default CODEX_HOME.
    It does not create trust: that takes a review in Codex (`/hooks`)."""
    lib = Path(__file__).resolve().parent
    return (f"install them with: PYTHONPATH={lib} python3 -c "
            "'import codex_bridge as b; b.install(b.codex_home())', then review the hook in Codex (/hooks)")


def default_tools_dir() -> Path:
    """The OPERATOR'S checkout's tools/, the one ~/.claude/settings.json points
    its own hooks at. Not this module's tree: both deploys are COPIES that carry
    no tools/ (bin/install-agent-platform-shared archives an explicit list), so
    a deployed agentctl would find nothing there and refuse every Codex dispatch.
    Same resolution projects.py uses for config/projects.json, for the same reason."""
    return BR.root() / "agent-platform" / "tools"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _bridge_files(home: Path, tools: Path) -> dict[str, Path]:
    return {"hooks.json": home / "hooks.json",
            **{name: tools / name for name in (SHIM, SHAPE, *GUARDS)}}


def shim_command(tools: Path) -> str:
    return f"{_PYTHON} {tools / SHIM}"


def hooks_document(tools: Path) -> dict:
    """The hooks.json body. No `matcher` key, on purpose."""
    return {"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": shim_command(tools), "timeout": TIMEOUT_S}]}]}}


def install(home: Path, tools: Path | None = None) -> None:
    """Write hooks.json and the manifest of what was written. Refuses to replace
    a hooks.json that is not ours: other hooks in it would be lost."""
    tools = tools or default_tools_dir()
    target = home / "hooks.json"
    if target.exists() and _digest(target) != _manifest(home).get("sha256", {}).get("hooks.json"):
        raise FileExistsError(f"{target} exists and was not written by this bridge; "
                              f"merge the bridge entry into it by hand")
    home.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(hooks_document(tools), indent=2) + "\n")
    files = _bridge_files(home, tools)
    (home / MANIFEST).write_text(json.dumps({
        "tools_dir": str(tools),
        "sha256": {name: _digest(path) for name, path in files.items()},
    }, indent=2) + "\n")


def _manifest(home: Path) -> dict:
    try:
        data = json.loads((home / MANIFEST).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_bridge_entry(entry: object, tools: Path) -> bool:
    if not isinstance(entry, dict) or entry.get("matcher") not in (None, ""):
        return False
    return any(isinstance(h, dict) and h.get("command") == shim_command(tools)
               for h in entry.get("hooks") or [])


def verify(home: Path, tools: Path | None = None) -> list[str]:
    """Why this CODEX_HOME's guards do not bind a run. Empty means they do."""
    tools = tools or default_tools_dir()
    problems = []
    try:
        doc = json.loads((home / "hooks.json").read_text())
    except (OSError, ValueError):
        return [f"the guards are absent: {home / 'hooks.json'} is missing or unreadable; "
                f"{install_hint()}"]
    entries = ((doc.get("hooks") or {}).get("PreToolUse") or []) if isinstance(doc, dict) else []
    if not any(_is_bridge_entry(e, tools) for e in entries):
        problems.append("the guards are absent: hooks.json has no matcher-free PreToolUse "
                        f"entry running {tools / SHIM}")

    recorded = _manifest(home).get("sha256")
    if not isinstance(recorded, dict):
        problems.append(f"the guards are unverified: {home / MANIFEST} is missing, so nothing "
                        f"says what was installed; {install_hint()}")
        recorded = {}
    for name, path in _bridge_files(home, tools).items():
        now = _digest(path)
        if now is None:
            problems.append(f"the guards are absent: {path} is missing")
        elif name in recorded and now != recorded[name]:
            problems.append(f"the guards are modified: {path} changed since install")
        elif name not in recorded and recorded:
            problems.append(f"the guards are unverified: {name} is not in the manifest")

    if not _trusted(home):
        problems.append(f"the guards are untrusted: {home / 'config.toml'} has no enabled "
                        "[hooks.state] entry with a trusted_hash for this hooks.json; review "
                        "the hook in Codex (/hooks) before dispatching")
    return problems


def _trusted(home: Path) -> bool:
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError):
        return False
    state = (config.get("hooks") or {}).get("state")
    if not isinstance(state, dict):
        return False
    ours = str(home / "hooks.json")
    return any(ours in key and isinstance(v, dict) and v.get("trusted_hash")
               and v.get("enabled", True) is not False for key, v in state.items())
