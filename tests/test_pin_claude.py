"""`bin/pin-claude` — the reviewers' Claude binary pin (platform/decisions/0090).

root installs the reviewers' Claude binary only if its sha256 matches the pin
this command writes, so the command's own correctness is the gate: a pin
written without the hashes actually having matched Anthropic's manifest would
make that install-time check meaningless. These tests exercise `pin()` end to
end against a fake HTTP transport (no real network), and specifically prove
the three guarantees the spec calls out:

* a checksum mismatch on any one platform refuses the whole pin, writes
  nothing, and leaves an existing pin file byte-for-byte unchanged;
* the "binary" is never executed, only hashed — proved with a stand-in that is
  itself a script which would leave a marker file behind if run;
* every scratch file written while hashing is deleted, whether verification
  passed or failed.
"""
from __future__ import annotations

import email
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

PLATFORMS = ("darwin-arm64", "linux-x64", "linux-arm64")
VERSION = "9.9.9"  # not a real release — these tests never touch the network


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _stand_in_binary(marker_path) -> bytes:
    """A 'binary' that, if ever executed, proves it by writing `marker_path`.
    pin-claude must never reach that — it only reads these bytes and hashes
    them."""
    return f"#!/bin/sh\necho ran >> {marker_path}\n".encode()


def _manifest_for(binaries: dict) -> dict:
    return {"platforms": {platform: {"checksum": hashlib.sha256(data).hexdigest()}
                          for platform, data in binaries.items()}}


def _fake_urlopen(manifest: dict, binaries: dict):
    def _urlopen(url, timeout=None):
        if url.endswith("/manifest.json"):
            return _FakeResponse(json.dumps(manifest).encode("utf-8"))
        for platform, data in binaries.items():
            if url.endswith(f"/{platform}/claude"):
                return _FakeResponse(data)
        raise AssertionError(f"unexpected URL in test: {url}")
    return _urlopen


def _binaries(marker_path) -> dict:
    return {platform: _stand_in_binary(marker_path) for platform in PLATFORMS}


@pytest.fixture(autouse=True)
def _restore_process_opener():
    """`main()` installs `_build_opener()`'s chain as the process-wide
    default, the same `urllib.request.install_opener` surface
    lib/agent-platform/vault_plane.py uses for its own redirect guard
    (test_vault_plane.py::test_the_guard_is_the_process_default). Several
    tests below call `main()` directly; without this, whichever one runs
    last in the process leaves pin-claude's opener installed for every test
    that runs after it, anywhere in the session — the same failure this
    file's `test_loading_pin_claude_does_not_install_an_opener` exists to
    catch at import time, moved to run time instead."""
    before = urllib.request._opener
    yield
    urllib.request.install_opener(before)


# ---- version validation -----------------------------------------------

def test_validate_version_accepts_plain_release_versions(pin_claude):
    assert pin_claude.validate_version("2.1.286") == "2.1.286"
    assert pin_claude.validate_version("0.0.1") == "0.0.1"


@pytest.mark.parametrize("bad", [
    "latest", "stable", "v2.1.286", "2.1", "2.1.286 ; rm -rf /",
    "2.1.286-beta.1",  # the suffix this regex used to accept (security review on #734, point 3)
    "2.1.286-/../evil",
    "2.1.286-?evil=1",
    "2.1.286\n",  # trailing newline — `$` allows it, `\Z` doesn't (security review on #734, point 3)
    "2.1.286\nrm -rf /",
])
def test_validate_version_rejects_moving_targets_and_junk(pin_claude, bad):
    with pytest.raises(pin_claude.PinError):
        pin_claude.validate_version(bad)


def test_malformed_version_is_refused_before_any_fetch(pin_claude, monkeypatch):
    """A version string with URL-reshaping characters in it must never reach
    urlopen — `validate_version` has to refuse it first (security review on
    #734, point 3: the old regex's open-ended prerelease suffix let
    '2.1.286-/../evil' and similar through to fetch_manifest's constructed URL)."""
    def _urlopen(url, timeout=None):
        raise AssertionError(
            f"pin-claude fetched {url!r} for a version that should have been refused first")

    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _urlopen)

    with pytest.raises(pin_claude.PinError):
        pin_claude.pin("2.1.286-/../evil")


# ---- happy path ----------------------------------------------------------

def test_happy_path_writes_pin_for_all_three_platforms(pin_claude, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    pin_path = tmp_path / "claude-pin.yaml"
    monkeypatch.setattr(pin_claude, "PIN_PATH", pin_path)

    outcome = pin_claude.pin(VERSION)

    assert outcome["version"] == VERSION
    for platform in PLATFORMS:
        assert outcome["results"][platform]["ok"] is True
        assert outcome["results"][platform]["sha256"] == hashlib.sha256(binaries[platform]).hexdigest()

    assert pin_path.exists()
    doc = yaml.safe_load(pin_path.read_text())
    assert doc["claude"]["version"] == VERSION
    for platform in PLATFORMS:
        assert doc["claude"]["sha256"][platform] == hashlib.sha256(binaries[platform]).hexdigest()

    assert not marker.exists(), "pin-claude executed a downloaded 'binary' instead of only hashing it"


def test_main_cli_prints_hashes_and_exits_zero(pin_claude, monkeypatch, tmp_path, capsys):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    monkeypatch.setattr(pin_claude, "PIN_PATH", tmp_path / "claude-pin.yaml")

    rc = pin_claude.main([VERSION])

    assert rc == 0
    out = capsys.readouterr().out
    for platform in PLATFORMS:
        assert hashlib.sha256(binaries[platform]).hexdigest() in out


# ---- a mismatch refuses the whole pin -------------------------------------

def test_mismatch_on_one_platform_refuses_writes_no_pin_and_leaves_existing_pin_untouched(
        pin_claude, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    # Anthropic's manifest disagrees with the bytes actually served for one platform.
    manifest["platforms"]["linux-x64"]["checksum"] = "0" * 64
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))

    pin_path = tmp_path / "claude-pin.yaml"
    existing = "claude:\n  version: 1.0.0\n  sha256: {darwin-arm64: aa, linux-x64: bb, linux-arm64: cc}\n"
    pin_path.write_text(existing)
    monkeypatch.setattr(pin_claude, "PIN_PATH", pin_path)

    with pytest.raises(pin_claude.PinError, match="linux-x64"):
        pin_claude.pin(VERSION)

    assert pin_path.read_text() == existing, "an existing pin must be left byte-identical on a mismatch"
    assert not marker.exists(), "pin-claude executed the stand-in binary despite a checksum mismatch"


def test_mismatch_with_no_existing_pin_writes_none(pin_claude, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    manifest["platforms"]["darwin-arm64"]["checksum"] = "0" * 64
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    pin_path = tmp_path / "claude-pin.yaml"
    monkeypatch.setattr(pin_claude, "PIN_PATH", pin_path)

    with pytest.raises(pin_claude.PinError):
        pin_claude.pin(VERSION)

    assert not pin_path.exists()
    assert not marker.exists()


def test_main_cli_exits_nonzero_on_mismatch(pin_claude, monkeypatch, tmp_path, capsys):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    manifest["platforms"]["linux-arm64"]["checksum"] = "0" * 64
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    monkeypatch.setattr(pin_claude, "PIN_PATH", tmp_path / "claude-pin.yaml")

    rc = pin_claude.main([VERSION])

    assert rc == 1
    assert not (tmp_path / "claude-pin.yaml").exists()
    assert not marker.exists()


# ---- scratch files are always deleted -------------------------------------

def test_scratch_files_are_deleted_on_success_and_on_mismatch(pin_claude, monkeypatch, tmp_path):
    created = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        if os.path.basename(path).startswith("pin-claude-"):
            created.append(path)
        return fd, path

    monkeypatch.setattr(pin_claude.tempfile, "mkstemp", tracking_mkstemp)

    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    monkeypatch.setattr(pin_claude, "PIN_PATH", tmp_path / "claude-pin.yaml")

    pin_claude.pin(VERSION)

    assert len(created) == len(PLATFORMS), "expected one scratch file per platform verified"
    for path in created:
        assert not os.path.exists(path), f"scratch file left behind after a successful pin: {path}"

    created.clear()
    manifest["platforms"]["linux-x64"]["checksum"] = "0" * 64
    with pytest.raises(pin_claude.PinError):
        pin_claude.pin(VERSION)

    assert created, "expected scratch files to have been created on the mismatch run too"
    for path in created:
        assert not os.path.exists(path), f"scratch file left behind after a refused pin: {path}"


# ---- a bad manifest is refused rather than silently accepted --------------

def test_manifest_missing_a_platform_is_refused(pin_claude, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    binaries = _binaries(marker)
    manifest = _manifest_for(binaries)
    del manifest["platforms"]["linux-arm64"]
    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _fake_urlopen(manifest, binaries))
    monkeypatch.setattr(pin_claude, "PIN_PATH", tmp_path / "claude-pin.yaml")

    with pytest.raises(pin_claude.PinError, match="linux-arm64"):
        pin_claude.pin(VERSION)

    assert not (tmp_path / "claude-pin.yaml").exists()
    assert not marker.exists()


# ---- a fetch failure refuses the pin and leaves an existing one untouched -

def test_fetch_failure_exits_one_writes_no_pin_and_leaves_existing_pin_untouched(
        pin_claude, monkeypatch, tmp_path, capsys):
    """Neither a network error nor a non-200 response (both surface to
    pin-claude as a urllib error — HTTPError is a URLError) should ever write
    a pin (code reviewer finding on #734: this path had no test)."""
    def _urlopen(url, timeout=None):
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _urlopen)

    pin_path = tmp_path / "claude-pin.yaml"
    existing = "claude:\n  version: 1.0.0\n  sha256: {darwin-arm64: aa, linux-x64: bb, linux-arm64: cc}\n"
    pin_path.write_text(existing)
    monkeypatch.setattr(pin_claude, "PIN_PATH", pin_path)

    rc = pin_claude.main([VERSION])

    assert rc == 1
    assert pin_path.read_text() == existing, "an existing pin must be left byte-identical on a fetch failure"


def test_non_200_response_exits_one_and_writes_no_pin(pin_claude, monkeypatch, tmp_path):
    def _urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _urlopen)
    pin_path = tmp_path / "claude-pin.yaml"
    monkeypatch.setattr(pin_claude, "PIN_PATH", pin_path)

    rc = pin_claude.main([VERSION])

    assert rc == 1
    assert not pin_path.exists()


# ---- only Anthropic's own hosts are trusted, checked at every hop ---------

def test_redirect_to_a_disallowed_host_is_refused(pin_claude, monkeypatch, tmp_path):
    """A redirect from downloads.claude.ai to any other host must be refused
    at the hop where it is seen — never followed to find out whether it
    serves a hash-matching manifest (security review on #734, point 1)."""
    requested = []

    def _urlopen(url, timeout=None):
        requested.append(url)
        if "evil.example" in url:
            # The attacker's own host, serving content that would otherwise
            # look fine on its own — proving the host check, not content
            # validation, is what has to catch this.
            return _FakeResponse(json.dumps({"platforms": {}}).encode())
        raise urllib.error.HTTPError(
            url, 302, "Found",
            {"Location": "https://evil.example/claude-code-releases/9.9.9/manifest.json"},
            None)

    monkeypatch.setattr(pin_claude.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(pin_claude, "PIN_PATH", tmp_path / "claude-pin.yaml")

    with pytest.raises(pin_claude.PinError, match="evil.example"):
        pin_claude.pin(VERSION)

    assert not any("evil.example" in u for u in requested), (
        "pin-claude fetched content from the disallowed host instead of refusing the redirect")


def test_build_opener_ignores_an_environment_proxy(pin_claude, monkeypatch):
    """HTTPS_PROXY/https_proxy in the caller's environment must not be able
    to route the fetch through a proxy (security review on #734, point 2).
    `ProxyHandler()` with no arguments reads those variables via
    `getproxies()`; pin-claude's opener must use an explicit empty mapping
    instead, so it never does."""
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker-proxy.invalid:9999")
    monkeypatch.setenv("https_proxy", "http://attacker-proxy.invalid:9999")

    # Positive control: a plain ProxyHandler() DOES pick up the environment.
    # If this fails, the environment isn't being read at all and the
    # assertion below would prove nothing.
    vulnerable = urllib.request.build_opener(urllib.request.ProxyHandler())
    assert any(type(h).__name__ == "ProxyHandler" for h in vulnerable.handle_open.get("https", [])), (
        "positive control failed: a bare ProxyHandler() should read HTTPS_PROXY "
        "from the environment")

    fixed = pin_claude._build_opener()
    assert not any(type(h).__name__ == "ProxyHandler" for h in fixed.handle_open.get("https", [])), (
        "pin-claude's opener honors an environment-configured proxy")


# ---- the CA bundle is pinned to a fixed path, never the environment -------

def test_ca_bundle_path_ignores_environment_cert_vars(pin_claude, monkeypatch):
    """SSL_CERT_FILE/SSL_CERT_DIR in the caller's environment must not be
    able to swap in a different trust root for the fetch (security review on
    #734, point 1) — `_ca_bundle_path` must pick its path from
    `sys.platform` alone, never read either variable."""
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/attacker-cert.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/attacker-certs")

    path = pin_claude._ca_bundle_path()

    assert str(path) not in ("/tmp/attacker-cert.pem", "/tmp/attacker-certs")
    assert path == pin_claude._CA_BUNDLE_BY_PLATFORM[pin_claude.sys.platform]


def test_ca_bundle_path_fails_closed_on_an_unknown_platform(pin_claude, monkeypatch):
    """No fixed path is known for this platform — refuse rather than fall
    back to whatever the environment or OpenSSL's own defaults would pick."""
    monkeypatch.setattr(pin_claude.sys, "platform", "freebsd13")

    with pytest.raises(pin_claude.PinError, match="freebsd13"):
        pin_claude._ca_bundle_path()


def test_ca_bundle_path_fails_closed_when_the_fixed_path_is_missing(pin_claude, monkeypatch, tmp_path):
    """The platform is known but nothing lives at its fixed CA path on this
    machine — still refuse rather than silently verifying against no CAs or
    falling back to the environment."""
    monkeypatch.setitem(
        pin_claude._CA_BUNDLE_BY_PLATFORM, pin_claude.sys.platform, tmp_path / "no-such-bundle.pem")

    with pytest.raises(pin_claude.PinError, match=pin_claude.sys.platform):
        pin_claude._ca_bundle_path()


def test_build_ssl_context_never_reads_default_cert_sources(pin_claude, monkeypatch):
    """`_build_ssl_context` must load CAs only from `_ca_bundle_path()` —
    never `SSLContext.load_default_certs()` or `.set_default_verify_paths()`,
    since both of those read SSL_CERT_FILE/SSL_CERT_DIR from the environment
    (security review on #734, point 1). Prove it by making either one raise
    if called, rather than just checking the result afterward."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("_build_ssl_context must not read environment-configured cert sources")

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", _forbidden)
    monkeypatch.setattr(ssl.SSLContext, "set_default_verify_paths", _forbidden)

    ctx = pin_claude._build_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_build_ssl_context_verifies_and_checks_hostname(pin_claude):
    ctx = pin_claude._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# ---- the fetch actually dispatches through the pinned opener --------------

class _FakeLowLevelResponse:
    """A stand-in for what `urllib.request.AbstractHTTPHandler.do_open`
    would hand back — needs `.code`/`.msg`/`.info()` too, since urllib's own
    `HTTPErrorProcessor` (always installed by `build_opener`) reads those
    before `_fetch` ever sees the response, unlike the `_FakeResponse` the
    rest of this file uses to stand in for `urlopen` itself."""

    def __init__(self, body: bytes):
        self._body = body
        self.code = 200
        self.msg = "OK"

    def info(self):
        return {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeRedirectResponse:
    """A stand-in for what `AbstractHTTPHandler.do_open` hands back for a 3xx
    response — needs `.code`/`.msg`/`.info()` (an `email.message.Message`,
    for `HTTPRedirectHandler.http_error_302`'s case-insensitive `"location"
    in headers` / `headers["location"]`), not the plain dict `_FakeResponse`
    uses, since that handler is what's under test here."""

    def __init__(self, code: int, location: str):
        self.code = code
        self.msg = "Found"
        self._headers = email.message_from_string(f"Location: {location}\r\n\r\n")

    def info(self):
        return self._headers

    def read(self):
        return b""

    def close(self):
        pass


def test_build_opener_refuses_a_redirect_driven_through_the_real_handler_chain(
        pin_claude, monkeypatch):
    """The existing `test_redirect_to_a_disallowed_host_is_refused` above
    monkeypatches `urllib.request.urlopen` wholesale, which — like every
    other HTTP test in this file except
    `test_fetch_dispatches_through_the_installed_opener_not_a_bypassed_default`
    — bypasses the opener `_build_opener()` builds, so it would keep passing
    even if `_NoFollowRedirectHandler()` were dropped from `_build_opener`
    entirely (code review on #734, point 1). This test builds the opener with
    `_build_opener()` and drives a real 302 response through its handler
    chain at the `do_open` layer, one level below `urlopen`, so the installed
    `_NoFollowRedirectHandler` is what has to refuse it."""
    calls = []

    def _fake_do_open(self, http_class, req, **kwargs):
        calls.append(req.full_url)
        if len(calls) == 1:
            return _FakeRedirectResponse(302, "https://evil.example/manifest.json")
        raise AssertionError(
            f"opener followed the redirect instead of refusing it: {calls}")

    monkeypatch.setattr(
        pin_claude.urllib.request.AbstractHTTPHandler, "do_open", _fake_do_open)

    opener = pin_claude._build_opener()

    with pytest.raises(pin_claude.urllib.error.HTTPError):
        opener.open("https://downloads.claude.ai/claude-code-releases/9.9.9/manifest.json")

    assert len(calls) == 1, "opener must not have made a second request to follow the redirect"


# `_build_opener()` installs three handlers. Each has (or, for the redirect
# handler, now has) a test above that fails if that one handler alone were
# removed from `_build_opener`, proved by actually removing it and watching
# the test go red (see the PR body for the plant/restore transcript):
#
#   - `ProxyHandler({})`                       -> test_build_opener_ignores_an_environment_proxy
#     (removing it makes `build_opener` substitute a default `ProxyHandler()`,
#     which reads `HTTPS_PROXY`/`https_proxy` — that test's positive control
#     is exactly that default behavior, so the fixed opener matches it once
#     the explicit empty handler is gone)
#   - `_NoFollowRedirectHandler()`              -> test_build_opener_refuses_a_redirect_driven_through_the_real_handler_chain
#     (removing it makes `build_opener` substitute a default
#     `HTTPRedirectHandler`, which auto-follows — the second `do_open` call
#     in that test's fake would fire and raise)
#   - `HTTPSHandler(context=_SSL_CONTEXT)`      -> test_fetch_dispatches_through_the_installed_opener_not_a_bypassed_default
#     (removing it makes `build_opener` substitute a default `HTTPSHandler()`
#     with no pinned context, so `do_open`'s `context` kwarg would no longer
#     be `pin_claude._SSL_CONTEXT`)


def test_fetch_dispatches_through_the_installed_opener_not_a_bypassed_default(
        pin_claude, monkeypatch):
    """Every other HTTP test in this file monkeypatches
    `urllib.request.urlopen` wholesale, which also bypasses whatever opener
    is actually installed — so none of them would notice if `main()` ever
    stopped installing `_build_opener()`'s chain before calling `pin()`,
    taking the empty ProxyHandler, the no-follow redirect handler and the
    pinned-CA HTTPSHandler with it (code review on #734, point 2). This test
    leaves `urlopen` itself alone, installs the opener the same way `main()`
    does, and intercepts one layer lower, at the call every handler in the
    chain ultimately reaches to open the connection, so the real, installed
    opener actually runs `_fetch`'s request."""
    calls = []

    def _fake_do_open(self, http_class, req, **kwargs):
        calls.append((req, kwargs))
        return _FakeLowLevelResponse(b'{"platforms": {}}')

    monkeypatch.setattr(
        pin_claude.urllib.request.AbstractHTTPHandler, "do_open", _fake_do_open)
    urllib.request.install_opener(pin_claude._build_opener())

    body = pin_claude._fetch(
        "https://downloads.claude.ai/claude-code-releases/9.9.9/manifest.json")

    assert body == b'{"platforms": {}}'
    assert len(calls) == 1, f"expected exactly one real connection attempt, got {len(calls)}"
    _req, kwargs = calls[0]
    assert kwargs.get("context") is pin_claude._SSL_CONTEXT, (
        "fetch did not verify against pin-claude's pinned SSLContext — the opener "
        "main() installs before calling pin() is being bypassed")


# ---- an unknown platform or a missing CA bundle fails cleanly, not with a
# ---- bare traceback (code review on #734, point 2) -------------------------

def _load_fresh_pin_claude():
    """A second, independent `bin/pin-claude` module instance — not the
    session-scoped `pin_claude` fixture, which every other test in this file
    shares. `_build_ssl_context()` runs at module import time, so the only
    way to exercise what happens when *that* raises is to trigger a fresh
    import with the failing condition already in place, the same as running
    `pin-claude` for real on a machine that hits it."""
    path = Path(__file__).resolve().parent.parent / "bin" / "pin-claude"
    loader = importlib.machinery.SourceFileLoader("pin_claude_fresh", str(path))
    spec = importlib.util.spec_from_loader("pin_claude_fresh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---- loading the module must not touch the process-wide opener -----------

def test_loading_pin_claude_does_not_install_an_opener():
    """Importing bin/pin-claude must not change `urllib.request`'s
    process-wide installed opener — this module is loaded in-process by the
    session-scoped `pin_claude` fixture (and by anything else that imports
    it), not only when the CLI actually runs. Before this fix, the import
    installed `_build_opener()`'s chain unconditionally, which silently
    replaced lib/agent-platform/vault_plane.py's own process-wide redirect
    guard whenever this module loaded after it — failing
    test_vault_plane.py::test_the_guard_is_the_process_default (CI run
    36864054315, job 110375125484, round 5 of #734)."""
    before = urllib.request._opener

    _load_fresh_pin_claude()

    assert urllib.request._opener is before, (
        "loading bin/pin-claude changed the process-wide installed opener")


def test_main_fails_cleanly_on_an_unknown_platform(monkeypatch, capsys):
    """On a platform `_CA_BUNDLE_BY_PLATFORM` has no entry for,
    `_build_ssl_context()` used to raise `PinError` straight out of the
    import — before this fix, `_load_fresh_pin_claude()` itself would raise
    here instead of returning a module, and `main()` would never even get a
    chance to run. Now the import catches it into `_INIT_ERROR`, and `main()`
    reports it the same way it reports every other `PinError`: a
    `pin-claude: ...` line on stderr and exit 1."""
    monkeypatch.setattr(sys, "platform", "freebsd13")

    fresh = _load_fresh_pin_claude()

    assert fresh._INIT_ERROR is not None
    rc = fresh.main(["9.9.9"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("pin-claude: "), f"expected the tool's normal error prefix, got: {err!r}"
    assert "freebsd13" in err


def test_main_fails_cleanly_when_the_ca_bundle_file_is_missing(pin_claude, monkeypatch, capsys):
    """Same failure mode, the other way it's reached: a known platform whose
    fixed CA bundle path has nothing at it on this machine."""
    real_is_file = Path.is_file
    known_bundle_paths = {str(p) for p in pin_claude._CA_BUNDLE_BY_PLATFORM.values()}

    def _fake_is_file(self):
        return False if str(self) in known_bundle_paths else real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _fake_is_file)

    fresh = _load_fresh_pin_claude()

    assert fresh._INIT_ERROR is not None
    rc = fresh.main(["9.9.9"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("pin-claude: "), f"expected the tool's normal error prefix, got: {err!r}"
