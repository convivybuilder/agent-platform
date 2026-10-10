"""The Library-owned tool deny-list the dispatch path enforces.

Orient shipped the WRITE surface and nothing here read it, so an operator could
set a denial and be restricting nothing — with the page looking exactly as it
looks when it works.
"""
from __future__ import annotations

import json
import pathlib
import sys

import issue_client
import policy_client as P
import pytest
import vault_plane

# The autouse conftest fixture replaces `P.resolve` with an offline stub for every
# test. This keeps the real function for the tests that exercise the resolver itself.
_real_resolve = P.resolve


class _Opener:
    """Stands in for `issue_client.pinned_opener()`: `open(req, timeout=)` calls `fn`."""

    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def _patch_opener(monkeypatch, fn):
    """The policy fetch reads the Library through the pinned opener, so the stub goes there."""
    monkeypatch.setattr(issue_client, "pinned_opener", lambda: _Opener(fn))


_LIVE_SHAPE = {
    "agent": "reviewer", "crew": "convivy-orient", "model": "claude-opus-5",
    "explicit": {},
    "connector_servers": ["claude_ai_Gmail", "claude_ai_Slack"],
    "exfiltration_builtins": ["DesignSync", "RemoteTrigger", "WebFetch"],
    "default": "allow", "version": 0,
}


def test_an_empty_policy_denies_nothing():
    """version 0 with no entries is the state today; it must not invent denials."""
    assert P.explicit_denials(_LIVE_SHAPE) == []


@pytest.mark.parametrize("verdict", ["deny", "denied", "DENY", " Deny ", "false", "block"])
def test_an_explicit_denial_is_carried(verdict):
    rec = dict(_LIVE_SHAPE, explicit={"Bash": verdict})
    assert P.explicit_denials(rec) == ["Bash"]


@pytest.mark.parametrize("verdict", ["allow", "allowed", "true", "", "maybe", None])
def test_anything_that_is_not_a_denial_is_NOT_denied(verdict):
    """An explicit ALLOW must not become a denial through a truthiness test, and
    an unrecognised verdict is ignored rather than guessed at."""
    rec = dict(_LIVE_SHAPE, explicit={"Read": verdict})
    assert P.explicit_denials(rec) == []


def test_the_categorical_lists_are_NOT_returned_as_explicit_denials():
    """`explicit_denials` is operator entries only. The categorical classes come
    from `categorical_denials`, so each set can be asserted on its own."""
    out = P.explicit_denials(dict(_LIVE_SHAPE, explicit={"Bash": "deny"}))
    assert out == ["Bash"]
    for categorical in ("WebFetch", "RemoteTrigger", "DesignSync"):
        assert categorical not in out


def test_every_crew_is_denied_the_categorical_classes_the_record_names():
    """Unconditional: the record names no crew scope, so nothing here is keyed
    on whether the dispatch is private. A public-crew worker gets the same set."""
    assert P.categorical_denials(_LIVE_SHAPE) == [
        "DesignSync", "RemoteTrigger", "WebFetch",
        "mcp__claude_ai_Gmail__*", "mcp__claude_ai_Slack__*",
    ]


def test_an_explicit_allow_removes_only_its_own_target():
    """An allow is the grant the agents whose purpose is the tool hold. It exempts
    exactly the target it names; every other categorical class stays denied."""
    rec = dict(_LIVE_SHAPE, explicit={"WebFetch": "allow"})
    out = P.categorical_denials(rec)
    assert "WebFetch" not in out
    assert set(out) == {"DesignSync", "RemoteTrigger",
                        "mcp__claude_ai_Gmail__*", "mcp__claude_ai_Slack__*"}


def test_an_explicit_allow_on_a_connector_wildcard_removes_that_server_only():
    rec = dict(_LIVE_SHAPE, explicit={"mcp__claude_ai_Gmail__*": "allow"})
    out = P.categorical_denials(rec)
    assert "mcp__claude_ai_Gmail__*" not in out
    assert "mcp__claude_ai_Slack__*" in out and "WebFetch" in out


def test_an_explicit_allow_on_a_connector_server_name_removes_that_server_only():
    """The record names a connector server by its bare name, so an allow keyed by
    that name exempts the server's wildcard. Without this, a Library allow for
    Slack would be silently ignored and Slack reads stay denied."""
    rec = dict(_LIVE_SHAPE, explicit={"claude_ai_Slack": "allow"})
    out = P.categorical_denials(rec)
    assert "mcp__claude_ai_Slack__*" not in out
    assert "mcp__claude_ai_Gmail__*" in out and "WebFetch" in out


@pytest.mark.parametrize("verdict", ["maybe", "", None, "grant"])
def test_an_unrecognised_verdict_on_a_categorical_target_grants_nothing(verdict):
    """Only an allow verdict exempts a target. Anything else leaves it denied, so
    a garbled verdict cannot widen a worker's tools."""
    rec = dict(_LIVE_SHAPE, explicit={"WebFetch": verdict})
    assert "WebFetch" in P.categorical_denials(rec)


def test_a_malformed_explicit_block_does_not_exempt_any_categorical_class():
    assert "WebFetch" in P.categorical_denials(dict(_LIVE_SHAPE, explicit=["WebFetch"]))


def test_a_malformed_explicit_block_denies_nothing_rather_than_crashing():
    assert P.explicit_denials(dict(_LIVE_SHAPE, explicit=["Bash"])) == []
    assert P.explicit_denials({}) == []


def test_unavailable_RAISES_rather_than_returning_empty(monkeypatch):
    """An empty record and 'the Library could not answer' are the same VALUE and
    completely different FACTS. Conflating them is the exact defect this replaces
    — a surface whose working and broken states look identical."""
    def _boom(req, *a, **k):
        raise OSError("network down")
    _patch_opener(monkeypatch, _boom)
    monkeypatch.setattr(P, "token", lambda: "t")
    with pytest.raises(P.PolicyUnavailable):
        _real_resolve("reviewer", "convivy-orient", "claude-opus-5")


def test_no_token_is_also_unavailable_not_permissive(monkeypatch):
    monkeypatch.setattr(P, "token", lambda: None)
    with pytest.raises(P.PolicyUnavailable):
        _real_resolve("reviewer", "convivy-orient", "claude-opus-5")


class _Body:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _library_answers(monkeypatch, body: bytes):
    monkeypatch.setattr(P, "token", lambda: "t")
    _patch_opener(monkeypatch, lambda req, timeout=None: _Body(body))


@pytest.mark.parametrize("body", [
    b"{}",                                                                # no class fields
    b"[]",                                                                # not an object
    b'"WebFetch"',                                                        # not an object
    b"null",                                                              # not an object
    b'{"error": "internal"}',                                             # error body, 200
    b'{"exfiltration_builtins": "WebFetch", "connector_servers": []}',   # string, not list
    b'{"exfiltration_builtins": ["WebFetch"], "connector_servers": "claude_ai_Slack"}',
    b'{"exfiltration_builtins": ["WebFetch"]}',                           # no connector_servers
    b'{"exfiltration_builtins": [1], "connector_servers": []}',           # non-string element
    b'{"exfiltration_builtins": [], "connector_servers": null}',
    b'{"exfiltration_builtins": [], "connector_servers": []}',                # no explicit
    b'{"exfiltration_builtins": [], "connector_servers": [], "explicit": "Bash"}',
    b'{"exfiltration_builtins": [], "connector_servers": [], "explicit": ["Bash"]}',
])
def test_a_200_that_is_not_a_usable_record_is_no_answer(monkeypatch, body):
    """A 200 with a missing or mistyped class field, or without an `explicit` object, is
    not an answer. Read as zero classes or as no operator entries, it would lift the
    operator's denials for that dispatch."""
    _library_answers(monkeypatch, body)
    with pytest.raises(P.PolicyUnavailable):
        _real_resolve("reviewer", "convivy-orient", "claude-opus-5")


def test_a_200_with_both_class_lists_is_returned_unchanged(monkeypatch):
    """The positive control for the malformed shapes above: an empty list is a
    usable list, so a record with no connectors still resolves."""
    rec = dict(_LIVE_SHAPE, connector_servers=[])
    _library_answers(monkeypatch, json.dumps(rec).encode())
    assert _real_resolve("reviewer", "convivy-orient", "claude-opus-5") == rec


def test_a_200_with_explicit_entries_still_applies_them(monkeypatch):
    """The positive control for the `explicit` check: a valid record that carries
    operator entries resolves unchanged, and its denial reaches `explicit_denials`."""
    rec = dict(_LIVE_SHAPE, explicit={"Grep": "deny", "WebFetch": "allow"})
    _library_answers(monkeypatch, json.dumps(rec).encode())
    resolved = _real_resolve("reviewer", "convivy-orient", "claude-opus-5")
    assert resolved == rec
    assert P.explicit_denials(resolved) == ["Grep"]


@pytest.mark.parametrize("name,safe", [
    ("WebSearch", True), ("Bash", True), ("mcp__ops__issue_get", True),
    ("Bash(git log:*)", True), ("Bash(*)", True),
    ("mcp__claude_ai_Gmail__*", True), ("mcp__claude_ai_Google_Calendar__*", True),
    ("mcp__claude_ai_*", False),
    ("--append-system-prompt", False), ("-x", False), ("", False),
    ("Web Search", False), ("Tool;rm -rf /", False), ("--mcp-config", False),
    # The one wildcard admitted is a whole connector server. Anything else with a
    # `*` is refused, so a categorical entry cannot carry a pattern of its choosing.
    ("*", False), ("Bash*", False), ("mcp__*", False), ("mcp__a__b*", False),
    ("mcp__a__*b", False), ("mcp__a__**", False), ("-mcp__a__*", False),
    # Only `mcp__<server>__*` admits a wildcard. Any other glob is refused, so a record
    # cannot carry a pattern of its choosing.
    ("mcp__x*", False), ("mcp__claude_ai*", False),
    ("mcp__claude_ai_Slack*", False), ("mcp__claude_ai_*x", False),
    # A hyphenated server name is a real connector (the Chrome extension's tools
    # are `mcp__claude-in-chrome__*`), and its wildcard denial is admitted. The
    # anchor still refuses a value that would begin with `-`, hyphen or not.
    ("mcp__claude-in-chrome__*", True), ("mcp__my-server__*", True),
    ("mcp__-server__*", False), ("-mcp__my-server__*", False),
])
def test_only_a_usable_tool_identifier_reaches_argv(name, safe):
    """These are the first NETWORK-SOURCED strings ever to reach `claude -p`'s
    argv — every other member of --disallowedTools is a hardcoded literal.

    That flag is variadic, so its collection stops at the next token beginning
    with `-`. A policy record whose `explicit` key were `--append-system-prompt`
    would terminate the list early and be re-parsed as a NEW FLAG to an
    unattended agent running under bypassPermissions: argument injection into the
    very process this policy constrains (security review, PR #487).

    REFUSED, NOT ESCAPED — no quoting makes a leading dash safe inside a variadic
    flag's collection, so the only correct answer is not to pass it.
    """
    assert P.is_safe_tool_name(name) is safe


def test_an_unusable_name_is_not_silently_carried():
    rec = dict(_LIVE_SHAPE, explicit={"--append-system-prompt": "deny",
                                      "WebSearch": "deny"})
    assert P.explicit_denials(rec) == ["WebSearch"]


# CQ-331 round 6: each way the Library fails to answer says what was observed, so the
# refusal the caller prints names the timeout, the refused connection or the bad response.
@pytest.mark.parametrize("exc,observed", [
    (TimeoutError("read timed out"), "timed out after 6s"),
    (P.urllib.error.URLError(TimeoutError("timed out")), "timed out after 6s"),
    (P.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
     "connection failed: [Errno 111] Connection refused"),
    (P.urllib.error.HTTPError("u", 500, "boom", {}, None), "bad response: HTTP 500"),
    (P.http.client.IncompleteRead(b'{"explicit"'), "bad response:"),
])
def test_an_unanswered_policy_fetch_says_what_was_observed(monkeypatch, exc, observed):
    def _raise(req, timeout=None):
        raise exc
    monkeypatch.setattr(P, "token", lambda: "t")
    _patch_opener(monkeypatch, _raise)
    with pytest.raises(P.PolicyUnavailable) as got:
        _real_resolve("reviewer", "convivy-orient", "claude-opus-5")
    assert observed in str(got.value)


def test_a_non_json_answer_is_a_bad_response(monkeypatch):
    _library_answers(monkeypatch, b"<html>502 bad gateway</html>")
    with pytest.raises(P.PolicyUnavailable) as got:
        _real_resolve("reviewer", "convivy-orient", "claude-opus-5")
    assert str(got.value).startswith("bad response:")


def test_the_policy_fetch_goes_to_the_fixed_address_with_the_library_headers(monkeypatch):
    """The fetch uses the definition fetch's address, headers and opener, so the same
    request reaches the Library whatever plane the caller's environment names."""
    seen = {}

    def _answer(req, timeout=None):
        seen["req"] = req
        return _Body(json.dumps(_LIVE_SHAPE).encode())
    _patch_opener(monkeypatch, _answer)
    _real_resolve("reviewer", "convivy-orient", "claude-opus-5", tok="t")
    req = seen["req"]
    headers = {k.lower(): v for k, v in req.header_items()}
    assert req.full_url.startswith(vault_plane.DEFAULT_LIBRARY_URL + "/policy/resolve?")
    assert headers["authorization"] == "Bearer t"
    assert headers[vault_plane.PROTOCOL_HEADER.lower()] == vault_plane.PROTOCOL_VERSION


class _Listener:
    """A real loopback HTTP server that records what reaches it and answers with `body`."""

    def __init__(self, body: bytes):
        import http.server
        import threading
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def listeners():
    made = []

    def make(body: bytes):
        made.append(_Listener(body))
        return made[-1]
    yield make
    for x in made:
        x.close()


# The probe runs the REAL resolver in a fresh process whose environment is set before any
# import (vault_plane reads LIBRARY_BASE_URL at import). Only the fixed address and the
# token file are pointed at the test listener, as test_issue_client.py does.
_RESOLVE_PROBE = """
import sys, json, pathlib
sys.path.insert(0, {lib!r})
import vault_plane, signal_client
import policy_client as P
vault_plane.DEFAULT_LIBRARY_URL = {target!r}
signal_client.TOKEN_FILE = pathlib.Path({tokfile!r})
print(json.dumps(P.resolve("reviewer", "convivy-orient", "claude-opus-5", timeout=5)))
"""

_QUERY = "/policy/resolve?agent=reviewer&crew=convivy-orient&model=claude-opus-5"


def _resolve_in_subprocess(env_extra, target, tmp_path):
    import os
    import subprocess
    lib = str(pathlib.Path(__file__).resolve().parents[1] / "lib" / "agent-platform")
    tokfile = tmp_path / "token"
    tokfile.write_text("tok-from-file\n")
    drop = ("LIBRARY_BASE_URL", "VAULTS_ON_LIBRARY", "ORIENT_BASE_URL", "AGENT_SIGNAL_TOKEN",
            "no_proxy", "NO_PROXY")
    env = {k: v for k, v in os.environ.items()
           if k not in drop and "proxy" not in k.lower()}
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         _RESOLVE_PROBE.format(lib=lib, target=target, tokfile=str(tokfile))],
        env=env, capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


_LIBRARY_RECORD = json.dumps(_LIVE_SHAPE).encode()


@pytest.mark.parametrize("var", ["http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"])
def test_the_policy_fetch_ignores_every_proxy_variable(var, listeners, tmp_path):
    target, attacker = listeners(_LIBRARY_RECORD), listeners(_LIBRARY_RECORD)
    got = _resolve_in_subprocess({var: attacker.url}, target.url, tmp_path)
    assert attacker.requests == [], "the policy fetch, and the bearer token, went to the proxy"
    assert got == _LIVE_SHAPE
    assert [r["path"] for r in target.requests] == [_QUERY]
    assert target.requests[0]["headers"].get("Authorization") == "Bearer tok-from-file"
    assert target.requests[0]["headers"].get("Library-Protocol") == "1"


def test_the_policy_fetch_ignores_the_callers_base_urls_and_plane(listeners, tmp_path):
    """Every variable that can name a plane or an address, and the proxy, set at once to
    one listener. The fetch reaches the fixed address and the listener receives nothing."""
    target, attacker = listeners(_LIBRARY_RECORD), listeners(_LIBRARY_RECORD)
    env = {"LIBRARY_BASE_URL": attacker.url, "ORIENT_BASE_URL": attacker.url,
           "VAULTS_ON_LIBRARY": "0", "http_proxy": attacker.url}
    got = _resolve_in_subprocess(env, target.url, tmp_path)
    assert attacker.requests == [], "the policy fetch went to a caller-named listener"
    assert got == _LIVE_SHAPE
    assert [r["path"] for r in target.requests] == [_QUERY]


@pytest.mark.parametrize("env_extra_fn", [
    lambda a: {"LIBRARY_BASE_URL": a},
    lambda a: {"VAULTS_ON_LIBRARY": "0", "ORIENT_BASE_URL": a},
])
def test_the_policy_fetch_ignores_the_callers_base_urls(env_extra_fn, listeners, tmp_path):
    target, attacker = listeners(_LIBRARY_RECORD), listeners(_LIBRARY_RECORD)
    got = _resolve_in_subprocess(env_extra_fn(attacker.url), target.url, tmp_path)
    assert attacker.requests == [], "the policy fetch went to a caller-named listener"
    assert got == _LIVE_SHAPE
    assert len(target.requests) == 1


def test_a_live_answer_from_the_fixed_address_still_applies(listeners, tmp_path):
    rec = dict(_LIVE_SHAPE, explicit={"Grep": "deny"})
    target = listeners(json.dumps(rec).encode())
    got = _resolve_in_subprocess({}, target.url, tmp_path)
    assert got == rec
    assert P.explicit_denials(got) == ["Grep"]
