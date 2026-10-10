"""`bin/gateway-mint-keys` — one tagged virtual key per consumer (E17·1).

The mint runs as a subprocess against a stub /key/generate server on loopback, with
HOME and the env file in a temp dir. Nothing here reaches the live gateway, the live
postgres or the real ~/.config/convivy. The fake master key and fake keys are the only
secrets in play, and the tests assert none of them reaches stdout or stderr.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MINT = REPO / "bin" / "gateway-mint-keys"

MASTER = "sk-test-master-" + "m" * 40
CONSUMER_ENTRIES = {
    "agentctl": "litellm-agentctl-virtual-key",
    "co": "litellm-co-virtual-key",
    "opencode": "litellm-opencode-virtual-key",
}


def _key(consumer):
    return f"sk-test-minted-{consumer}-" + "k" * 30


class _StubGateway(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        })
        consumer = (body.get("metadata") or {}).get("consumer")
        key = self.server.keys.get(consumer)
        if key is None:
            self.send_response(500)
            self.end_headers()
            return
        payload = json.dumps({"key": key}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def gateway():
    server = HTTPServer(("127.0.0.1", 0), _StubGateway)
    server.requests = []
    server.keys = {consumer: _key(consumer) for consumer in CONSUMER_ENTRIES}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(tmp_path):
    """A temp HOME holding an env file with the fake master key, mode 0600."""
    h = tmp_path / "home"
    conf = h / ".config" / "convivy"
    conf.mkdir(parents=True)
    env = conf / "llm-gateway.env"
    env.write_text(f"LITELLM_MASTER_KEY={MASTER}\n")
    os.chmod(env, 0o600)
    return h


def _keys_file(home):
    return home / ".config" / "convivy" / "litellm-keys.json"


def _seed_keys(home, entries):
    _keys_file(home).write_text(json.dumps(entries, indent=2))


def _mint(home, gateway, **extra_env):
    # The env is built from scratch, so a developer's own LLM_GATEWAY_* settings cannot leak in.
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra_env}
    url = f"http://127.0.0.1:{gateway.server_address[1]}"
    return subprocess.run(
        [sys.executable, str(MINT), "--base-url", url],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _assert_no_secret(result, *secrets):
    for secret in (MASTER, *secrets):
        assert secret not in result.stdout, "a key was printed to stdout"
        assert secret not in result.stderr, "a key was printed to stderr"


def test_sends_one_request_per_consumer_tagged_with_that_consumer(home, gateway):
    result = _mint(home, gateway)
    assert result.returncode == 0, result.stderr
    assert len(gateway.requests) == len(CONSUMER_ENTRIES)
    tags = sorted(r["body"]["metadata"]["consumer"] for r in gateway.requests)
    assert tags == sorted(CONSUMER_ENTRIES), "each consumer tag is sent exactly once"
    for req in gateway.requests:
        assert req["path"] == "/key/generate"
        assert req["auth"] == f"Bearer {MASTER}", "the master key authenticates the mint"


def test_each_key_lands_in_its_matching_account_key_entry(home, gateway):
    assert _mint(home, gateway).returncode == 0
    data = json.loads(_keys_file(home).read_text())
    for consumer, entry in CONSUMER_ENTRIES.items():
        value = data[entry]
        assert set(value) == {"account", "key"}, "entries keep the {account, key} shape"
        assert value["key"] == _key(consumer), f"{entry} holds the key minted for {consumer}"
        assert value["account"], f"{entry} names an account"


def test_keys_file_is_written_at_mode_0600_even_over_a_looser_file(home, gateway):
    _seed_keys(home, {})
    os.chmod(_keys_file(home), 0o644)
    assert _mint(home, gateway).returncode == 0
    assert stat.S_IMODE(os.stat(_keys_file(home)).st_mode) == 0o600


def test_leaves_no_temp_file_next_to_the_keys_file(home, gateway):
    assert _mint(home, gateway).returncode == 0
    names = sorted(p.name for p in _keys_file(home).parent.iterdir())
    assert names == ["litellm-keys.json", "llm-gateway.env"]


def test_unrelated_entries_are_preserved(home, gateway):
    other = {"account": "someone", "key": "sk-test-other-service-" + "o" * 30}
    _seed_keys(home, {"litellm-other-service": other})
    assert _mint(home, gateway).returncode == 0
    assert json.loads(_keys_file(home).read_text())["litellm-other-service"] == other


def test_refuses_a_key_equal_to_the_master_key_and_writes_nothing(home, gateway):
    before = {"litellm-co-virtual-key": {"account": "a", "key": "sk-test-before"}}
    _seed_keys(home, before)
    gateway.keys["co"] = MASTER
    result = _mint(home, gateway)
    assert result.returncode == 1
    assert "equals the master key" in result.stderr
    assert json.loads(_keys_file(home).read_text()) == before, "nothing was written"
    _assert_no_secret(result)


def test_refuses_a_key_equal_to_another_minted_key(home, gateway):
    gateway.keys["opencode"] = _key("co")
    result = _mint(home, gateway)
    assert result.returncode == 1
    assert "opencode" in result.stderr
    assert not _keys_file(home).exists(), "a refusal leaves no keys file behind"
    _assert_no_secret(result, _key("co"))


def test_refuses_a_key_equal_to_an_unrelated_entrys_key_field(home, gateway):
    # The wrapper differs (different account), so only the key-field digest can see
    # the collision. A comparison of whole entry values would let it through.
    _seed_keys(home, {"litellm-other-service": {"account": "elsewhere", "key": _key("co")}})
    gateway.keys["co"] = _key("co")
    result = _mint(home, gateway)
    assert result.returncode == 1
    assert "entry litellm-other-service" in result.stderr
    assert json.loads(_keys_file(home).read_text()) == {
        "litellm-other-service": {"account": "elsewhere", "key": _key("co")},
    }
    _assert_no_secret(result, _key("co"))


def test_entries_sharing_an_account_are_not_collisions(home, gateway):
    _seed_keys(home, {"litellm-other-service": {"account": "operator", "key": "sk-test-unrelated"}})
    assert _mint(home, gateway).returncode == 0


def test_a_gateway_error_writes_nothing(home, gateway):
    gateway.keys.pop("agentctl")
    result = _mint(home, gateway)
    assert result.returncode == 1
    assert "agentctl" in result.stderr and "HTTP 500" in result.stderr
    assert not _keys_file(home).exists()
    _assert_no_secret(result)


def test_success_output_names_consumers_and_hash_prefixes_only(home, gateway):
    result = _mint(home, gateway)
    assert result.returncode == 0
    for consumer, entry in CONSUMER_ENTRIES.items():
        digest = hashlib.sha256(_key(consumer).encode("utf-8")).hexdigest()[:12]
        assert f"minted {consumer}: sha256 {digest} -> {entry}" in result.stdout
    _assert_no_secret(result, *(_key(c) for c in CONSUMER_ENTRIES))


# A consumer list other than this deployment's, so a mint that still hardcodes the three
# shipped names fails on its assertions.
OTHER_CONSUMERS = [
    {"consumer": "billing-bot", "entry": "litellm-billing-bot-key"},
    {"consumer": "dashboard", "entry": "litellm-dashboard-key"},
    {"consumer": "scheduler", "entry": "litellm-scheduler-key"},
    {"consumer": "reporter", "entry": "litellm-reporter-key"},
]


def _declare(path, declared):
    path.write_text(json.dumps(declared))
    return str(path)


def test_the_declaration_sets_the_consumers_and_their_entries(home, gateway, tmp_path):
    names = [item["consumer"] for item in OTHER_CONSUMERS]
    gateway.keys = {name: _key(name) for name in names}
    declaration = _declare(tmp_path / "consumers.json", OTHER_CONSUMERS)
    result = _mint(home, gateway, LLM_GATEWAY_CONSUMERS_FILE=declaration)
    assert result.returncode == 0, result.stderr
    assert sorted(r["body"]["metadata"]["consumer"] for r in gateway.requests) == sorted(names)
    data = json.loads(_keys_file(home).read_text())
    assert set(data) == {item["entry"] for item in OTHER_CONSUMERS}, \
        "only the declared entries are written"
    for item in OTHER_CONSUMERS:
        assert data[item["entry"]]["key"] == _key(item["consumer"])
    _assert_no_secret(result, *(_key(n) for n in names))


@pytest.mark.parametrize("declared", [
    [],
    {"consumer": "co", "entry": "litellm-co-virtual-key"},
    [{"consumer": "co"}],
    [{"consumer": "co", "entry": "litellm-co-virtual-key"},
     {"consumer": "co", "entry": "litellm-other-key"}],
    [{"consumer": "co", "entry": "litellm-co-virtual-key"},
     {"consumer": "dashboard", "entry": "litellm-co-virtual-key"}],
], ids=["empty", "not-a-list", "no-entry", "repeated-consumer", "repeated-entry"])
def test_a_malformed_declaration_refuses_before_any_request(home, gateway, tmp_path, declared):
    declaration = _declare(tmp_path / "consumers.json", declared)
    result = _mint(home, gateway, LLM_GATEWAY_CONSUMERS_FILE=declaration)
    assert result.returncode == 1
    assert declaration in result.stderr, "the refusal names the declaration"
    assert gateway.requests == [], "no key is requested from a declaration that does not parse"
    assert not _keys_file(home).exists()


def test_a_missing_declaration_refuses(home, gateway, tmp_path):
    result = _mint(home, gateway, LLM_GATEWAY_CONSUMERS_FILE=str(tmp_path / "absent.json"))
    assert result.returncode == 1
    assert "cannot read consumer declaration" in result.stderr
    assert gateway.requests == []


def test_config_dir_moves_the_env_file_and_the_keys_file(home, gateway, tmp_path):
    # A different master key under the default directory: a mint that ignores the override
    # would authenticate with it, and the stub records the Authorization header it sent.
    default_env = home / ".config" / "convivy" / "llm-gateway.env"
    default_env.write_text("LITELLM_MASTER_KEY=sk-test-wrong-default-master-" + "w" * 20 + "\n")
    cfg = tmp_path / "elsewhere"
    cfg.mkdir()
    (cfg / "llm-gateway.env").write_text(f"LITELLM_MASTER_KEY={MASTER}\n")
    os.chmod(cfg / "llm-gateway.env", 0o600)
    result = _mint(home, gateway, LLM_GATEWAY_CONFIG_DIR=str(cfg))
    assert result.returncode == 0, result.stderr
    assert gateway.requests, "the mint reached the gateway"
    assert all(r["auth"] == f"Bearer {MASTER}" for r in gateway.requests)
    assert sorted(p.name for p in cfg.iterdir()) == ["litellm-keys.json", "llm-gateway.env"]
    assert not _keys_file(home).exists(), "nothing is written to the default directory"
    _assert_no_secret(result)


VKEY = REPO / "lib" / "agent-platform" / "gateway-vkey"
AGENTCTL = REPO / "bin" / "agentctl"
# gateway-vkey is POSIX sh, so run it under dash where it exists, as its own test does.
_SH = shutil.which("dash") or "sh"


def _env_for(home, **extra):
    return {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}


def _vkey(env, service):
    return subprocess.run([_SH, str(VKEY), service], capture_output=True, text=True,
                          timeout=30, env=env)


def _agentctl_view(env):
    """(GATEWAY_KEYS_FILE, the agentctl key) as agentctl resolves them at import."""
    script = (
        "import importlib.machinery, importlib.util\n"
        f"loader = importlib.machinery.SourceFileLoader('ac', {str(AGENTCTL)!r})\n"
        "spec = importlib.util.spec_from_loader('ac', loader)\n"
        "ac = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(ac)\n"
        "print(ac.GATEWAY_KEYS_FILE)\n"
        "print(ac._read_keys_file('litellm-agentctl-virtual-key'))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    path, key = proc.stdout.splitlines()[:2]
    return path, key


def test_the_mint_and_both_readers_resolve_one_directory(home, gateway, tmp_path):
    cfg = tmp_path / "elsewhere"
    cfg.mkdir()
    (cfg / "llm-gateway.env").write_text(f"LITELLM_MASTER_KEY={MASTER}\n")
    os.chmod(cfg / "llm-gateway.env", 0o600)
    # A decoy under the default directory: a reader that ignores the override returns it.
    _seed_keys(home, {"litellm-agentctl-virtual-key":
                      {"account": "decoy", "key": "sk-test-decoy-" + "d" * 30}})
    os.chmod(_keys_file(home), 0o600)
    assert _mint(home, gateway, LLM_GATEWAY_CONFIG_DIR=str(cfg)).returncode == 0
    assert (cfg / "litellm-keys.json").exists(), "the mint writes the override directory"
    env = _env_for(home, LLM_GATEWAY_CONFIG_DIR=str(cfg))
    proc = _vkey(env, "litellm-agentctl-virtual-key")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _key("agentctl"), "gateway-vkey reads the key the mint wrote"
    path, key = _agentctl_view(env)
    assert path == str(cfg / "litellm-keys.json"), "agentctl resolves the directory the mint wrote"
    assert key == _key("agentctl")


def test_without_the_setting_both_readers_use_the_default_directory(home, gateway):
    assert _mint(home, gateway).returncode == 0
    env = _env_for(home)
    proc = _vkey(env, "litellm-agentctl-virtual-key")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _key("agentctl")
    path, key = _agentctl_view(env)
    assert path == str(home / ".config" / "convivy" / "litellm-keys.json")
    assert key == _key("agentctl")


@pytest.mark.parametrize("bad_key", [12345, 1.5, ["sk-test-list"], {"value": "sk-test-object"}, True],
                         ids=["int", "float", "list", "object", "bool"])
def test_a_non_string_key_is_refused_by_consumer_with_no_traceback(home, gateway, bad_key):
    gateway.keys["agentctl"] = bad_key
    result = _mint(home, gateway)
    assert result.returncode == 1
    assert "agentctl" in result.stderr, "the refusal names the consumer"
    assert "Traceback" not in result.stderr, "a malformed answer is a refusal, not a crash"
    assert not _keys_file(home).exists(), "nothing is written when a key is refused"
    _assert_no_secret(result)


def test_a_symlink_at_the_keys_file_is_replaced_and_its_target_is_left_alone(home, gateway, tmp_path):
    # Pins the atomic replace: os.replace swaps the link itself, so nothing writes through it.
    target = tmp_path / "elsewhere.json"
    sentinel = {"sentinel": {"account": "someone", "key": "sk-test-sentinel"}}
    target.write_text(json.dumps(sentinel))
    os.symlink(target, _keys_file(home))
    result = _mint(home, gateway)
    assert result.returncode == 0
    assert json.loads(target.read_text()) == sentinel, "the link's target is not written through"
    assert not _keys_file(home).is_symlink(), "the link is replaced by the keys file"
    written = json.loads(_keys_file(home).read_text())
    assert written["litellm-agentctl-virtual-key"]["key"] == _key("agentctl")
    _assert_no_secret(result)
