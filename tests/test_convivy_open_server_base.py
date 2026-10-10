"""tools/convivy-open/convivy-open-server — CONVIVY_OPEN_BASE from the
environment must be taken as its first line only, and the server must refuse
to start when the resulting BASE is not an absolute path.

Without this, a multi-line or relative value that reached the plist's
EnvironmentVariables (install-laptop.sh interpolates a config-file value that
resolve-open-base.sh does not always validate — see
test_resolve_open_base.sh and test_install_laptop_plist_escaping.sh) would be
used whole: os.path.join(BASE, rel) with a multi-line BASE, or a relative one,
changes what `_send`/`os.path.realpath` confine requests to. security-reviewer
finding, PR #561 round 1.

Each test loads a fresh copy of the module (module-level code computes BASE
at import time from the environment), matching tests/test_outbound_guard.py's
pattern for a hyphenated, extension-less script."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "tools" / "convivy-open" / "convivy-open-server"


def _load(name="convivy_open_server_under_test"):
    loader = importlib.machinery.SourceFileLoader(name, str(SERVER))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_accepts_a_normal_absolute_single_line_base(monkeypatch):
    monkeypatch.setenv("CONVIVY_OPEN_BASE", "/Volumes/exampleuser/builder")
    mod = _load()
    assert mod.BASE == "/Volumes/exampleuser/builder"


def test_takes_only_the_first_line_of_an_lf_joined_value(monkeypatch):
    monkeypatch.setenv(
        "CONVIVY_OPEN_BASE", "/Volumes/exampleuser/builder\n/Volumes/exampleuser/pwned")
    mod = _load()
    assert mod.BASE == "/Volumes/exampleuser/builder"


def test_takes_only_the_first_line_of_a_cr_joined_value(monkeypatch):
    monkeypatch.setenv(
        "CONVIVY_OPEN_BASE", "/Volumes/exampleuser/builder\r/Volumes/exampleuser/pwned")
    mod = _load()
    assert mod.BASE == "/Volumes/exampleuser/builder"


def test_refuses_to_start_with_a_relative_base(monkeypatch):
    monkeypatch.setenv("CONVIVY_OPEN_BASE", "not/an/absolute/path")
    with pytest.raises(SystemExit) as exc:
        _load()
    assert exc.value.code == 1


def test_refuses_to_start_when_the_first_line_of_a_multiline_value_is_relative(monkeypatch):
    # The newline check lives upstream (resolve-open-base.sh); this asserts the
    # server's OWN guard does not depend on that upstream check ever running —
    # a relative first line is refused even though a later line looks absolute.
    monkeypatch.setenv("CONVIVY_OPEN_BASE", "relative\n/Volumes/exampleuser/builder")
    with pytest.raises(SystemExit) as exc:
        _load()
    assert exc.value.code == 1
