"""PR-1 gateway config + plist secret-guard tests (Phase C, ARCH-102).

Guarantees pinned here:
  - no secret-shaped literals in gateway/config.yaml (api_key fields use os.environ/)
  - no secret-shaped literals in the tracked .plist.example
  - the live plist (com.convivy.llm-gateway.plist) is gitignored + not tracked
  - the .example execs the PINNED launcher (ARCH-116), which is bash-3.2 safe
    (ARCH-082), binds loopback, and refuses a secrets file looser than 0600 (ARCH-342)
"""
from __future__ import annotations

import json
import plistlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
GATEWAY_CONFIG = REPO / "gateway" / "config.yaml"
CONSUMERS = REPO / "gateway" / "consumers.json"
REQUIREMENTS = REPO / "gateway" / "requirements.txt"
PLIST_EXAMPLE = REPO / "launchd" / "com.convivy.llm-gateway.plist.example"
PLIST_LIVE = REPO / "launchd" / "com.convivy.llm-gateway.plist"
LAUNCHER = REPO / "gateway" / "llm-gateway-run.sh"
# Where install.sh pins the gateway runtime (ARCH-116). The plist must exec from here,
# never from the repo working tree. Coupled to the plist's literal path; becomes a
# template in the launchd migration slice, not before.
GATEWAY_LIB = "__HOME__/.local/lib/llm-gateway"

# Regex covering common API key shapes (OpenAI, Anthropic, DeepSeek, Google, Moonshot).
# The guard is conservative: any 16+ char run after "sk-" trips it.
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_\-]{16,}"  # OpenAI / Anthropic / DeepSeek style
    r"|sk-proj-"
    r"|sk-ant-"
    r"|gsk_[A-Za-z0-9]{10,}"   # Google AI Studio
    r"|AIza[A-Za-z0-9\-_]{35}" # Google API key
)


def _walk_strings(obj):
    """Yield all string leaf values from a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)


def test_config_yaml_has_no_secret_literals():
    """gateway/config.yaml must not contain any secret-shaped string literals."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    for s in _walk_strings(config):
        assert not SECRET_RE.search(s), (
            f"secret-shaped literal found in gateway/config.yaml: {s[:20]}..."
        )


def test_config_yaml_api_keys_use_env_refs():
    """Every api_key field in the model_list must reference os.environ/, not a literal."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    for entry in config.get("model_list", []):
        params = entry.get("litellm_params", {})
        api_key = params.get("api_key", "")
        assert api_key.startswith("os.environ/"), (
            f"api_key for model '{entry.get('model_name')}' must use os.environ/ "
            f"reference, got: {api_key!r}"
        )


def test_database_url_is_read_from_the_environment():
    """Virtual keys live in the database, so LiteLLM needs database_url (E17·1).

    The URL names a database and may carry a credential, so it comes from
    llm-gateway.env through os.environ/, and no postgres URL is written into the file.
    """
    text = GATEWAY_CONFIG.read_text()
    config = yaml.safe_load(text)
    assert config.get("general_settings", {}).get("database_url") == "os.environ/DATABASE_URL", (
        "general_settings.database_url must read DATABASE_URL from the environment; "
        "without it /key/generate answers 500 DB not connected"
    )
    assert not re.search(r"postgres(ql)?://", text), (
        "gateway/config.yaml must not hold a database URL literal"
    )


def test_virtual_key_procedure_names_the_mint_and_declaration_not_keychain():
    """The bootstrap comment is the procedure an operator follows. It must name the
    mint and the consumer declaration it reads, and must not name the retired `pa`
    consumer or Keychain storage (E17·1)."""
    text = GATEWAY_CONFIG.read_text()
    assert "bin/gateway-mint-keys" in text
    assert "gateway/consumers.json" in text
    assert "litellm-pa-virtual-key" not in text, "the pa consumer is not minted"
    assert "Keychain" not in text, "virtual keys are stored in litellm-keys.json, not Keychain"


def test_shipped_consumer_declaration_is_this_deployments_three():
    """The mint reads gateway/consumers.json. This deployment's three consumers and their
    keys-file entries are declared there, and the mint takes its list from that file."""
    declared = json.loads(CONSUMERS.read_text())
    assert [(c["consumer"], c["entry"]) for c in declared] == [
        ("agentctl", "litellm-agentctl-virtual-key"),
        ("co", "litellm-co-virtual-key"),
        ("opencode", "litellm-opencode-virtual-key"),
    ]


def test_prisma_is_pinned_as_one_explicit_requirement():
    """LiteLLM's database code imports prisma. It is pinned to the version litellm
    declares, as one line, and no other prisma line is present (E17·1)."""
    lines = [line.strip() for line in REQUIREMENTS.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert lines.count("prisma==0.11.0") == 1
    assert [line for line in lines if line.startswith("prisma")] == ["prisma==0.11.0"]


def test_config_drops_unsupported_params():
    """claude -p sends Anthropic-only params; the gateway must drop_params so a
    non-Anthropic provider doesn't 400 on context_management/thinking/etc."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    assert config.get("litellm_settings", {}).get("drop_params") is True, (
        "litellm_settings.drop_params must be true or gateway-routed calls 400 "
        "on Anthropic-specific request params"
    )


def test_router_settings_retry_policy_pins_the_backpressure_invariants():
    """ARCH-303 gateway backpressure. Two invariants are load-bearing and must
    never silently regress: retries fire for rate limits (the whole point), and
    a 400 does NOT retry (ARCH-130's DeepSeek json_schema 400-storm must fail
    fast, not multiply). Pin both, plus that num_retries is set at all."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    rs = config.get("router_settings", {})
    assert rs.get("num_retries", 0) >= 1, (
        "router_settings.num_retries must be >=1 or a gateway 429/5xx passes "
        "straight through to the worker as a plain death (ARCH-303)"
    )
    policy = rs.get("retry_policy", {})
    assert policy.get("RateLimitErrorRetries", 0) >= 1, (
        "retry_policy.RateLimitErrorRetries must be >=1 — retrying a 429 is the "
        "point of the ARCH-303 backpressure change"
    )
    assert policy.get("BadRequestErrorRetries") == 0, (
        "retry_policy.BadRequestErrorRetries must be 0 or a DeepSeek json_schema "
        "400 retries and re-triggers the ARCH-130 400-storm"
    )


def test_mimo_routes_via_chat_completions_on_anthropic_bridge():
    """MiMo is OpenAI-compatible (chat/completions only). The /v1/messages anthropic
    bridge that `claude -p` uses routes the `openai` provider to the Responses API
    (/v1/responses) by default — which Xiaomi doesn't serve → 404 (ARCH-210 Fix 1).
    `use_chat_completions_url_for_anthropic_messages: true` forces chat/completions.
    Without both this flag AND the openai/ provider, MiMo is unreachable for agents."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    assert config.get("litellm_settings", {}).get(
        "use_chat_completions_url_for_anthropic_messages") is True, (
        "litellm_settings.use_chat_completions_url_for_anthropic_messages must be "
        "true or OpenAI-compatible models (MiMo) 404 on the /v1/messages bridge"
    )
    mimo = next(e for e in config["model_list"] if e["model_name"] == "mimo")
    assert mimo["litellm_params"]["model"].startswith("openai/"), (
        "mimo must use the openai/ provider (openai_like is unmapped on the "
        "anthropic /v1/messages bridge)"
    )


def test_deepseek_has_pinned_pricing():
    """DeepSeek's API now returns model `deepseek-v4-flash`, unmapped in LiteLLM's
    price map → completion_cost() errors → usd:0 in the spend log. The deepseek
    deployment must pin model_info pricing (incl. the cache-read tier) so cost is
    computed regardless of the echoed model name."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    deepseek = next(e for e in config["model_list"] if e["model_name"] == "deepseek")
    mi = deepseek.get("model_info", {})
    for key in ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost"):
        assert isinstance(mi.get(key), (int, float)) and mi[key] > 0, (
            f"deepseek model_info.{key} must be a positive cost"
        )


def test_kimi_code_has_pinned_pricing_incl_cache_read():
    """k2.7-code is too new for LiteLLM's price map, so its pricing must be pinned
    or completion_cost() errors → usd:0. The cache-read tier is required (ARCH-181):
    agent loops are cache-read-dominated, so omitting it billed cache hits at the
    full input rate and inflated the model's measured cost."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    kimi = next(e for e in config["model_list"] if e["model_name"] == "kimi-code")
    mi = kimi.get("model_info", {})
    for key in ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost"):
        assert isinstance(mi.get(key), (int, float)) and mi[key] > 0, (
            f"kimi-code model_info.{key} must be a positive cost"
        )
    # cache-read must be cheaper than a cache miss, else it isn't a discount
    assert mi["cache_read_input_token_cost"] < mi["input_cost_per_token"]


@pytest.mark.parametrize("model_name", ["mimo", "glm"])
def test_trial_model_has_pinned_pricing_incl_cache_read(model_name):
    """The ARCH-201 trial models (mimo = MiMo-V2.5-Pro, glm = GLM-5.2) are absent
    from LiteLLM 1.86.2's price map, so their pricing must be pinned or
    completion_cost() errors → usd:0 (the deepseek-v4-flash failure mode). The
    cache-read tier is required (ARCH-181): agent loops are cache-read-dominated,
    so an unset tier bills cache hits at $0 and biases the trial in the model's
    favor against the deepseek baseline it's being measured against."""
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    entry = next(e for e in config["model_list"] if e["model_name"] == model_name)
    mi = entry.get("model_info", {})
    for key in ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost"):
        assert isinstance(mi.get(key), (int, float)) and mi[key] > 0, (
            f"{model_name} model_info.{key} must be a positive cost"
        )
    # cache-read must be cheaper than a cache miss, else it isn't a discount
    assert mi["cache_read_input_token_cost"] < mi["input_cost_per_token"]


def test_plist_example_has_no_secret_literals():
    """The tracked .plist.example must have empty-string placeholders, not secrets."""
    with PLIST_EXAMPLE.open("rb") as f:
        plist = plistlib.load(f)
    env_vars = plist.get("EnvironmentVariables", {})
    for key, val in env_vars.items():
        if key == "PYTHONPATH":
            continue  # path, not a secret
        assert not SECRET_RE.search(str(val)), (
            f"secret-shaped literal in .plist.example EnvironmentVariables[{key!r}]"
        )
        # An empty-placeholder branch for the six key names used to live here. It is
        # dead now that ARCH-342 removed them from the .example entirely, and the
        # stronger rule — no credential-shaped key at all, placeholder or not — is
        # test_no_plist_carries_a_credential in test_plist_no_tunables.py.


def test_live_plist_is_gitignored():
    """The live (secret-bearing) plist must be matched by .gitignore and not tracked.

    Defends the 'no populated plist in git' guarantee. This test passes even when
    the live file doesn't exist on disk (it's the operator's bootstrap step).
    """
    # check-ignore exits 0 if the path is ignored, 1 if not
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(PLIST_LIVE)],
        cwd=str(REPO), capture_output=True,
    )
    assert result.returncode == 0, (
        "launchd/com.convivy.llm-gateway.plist is NOT gitignored — "
        "add it to .gitignore before merging"
    )

    # also assert it's not tracked (ls-files --error-unmatch exits 1 when not tracked)
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(PLIST_LIVE)],
        cwd=str(REPO), capture_output=True,
    )
    assert tracked.returncode != 0, (
        "launchd/com.convivy.llm-gateway.plist is tracked in git — "
        "remove it with `git rm --cached`"
    )


def _launcher_code() -> str:
    """The launcher with whole-line comments stripped.

    Every assertion about the launcher must run against this, not the raw text. The
    launcher's header names the very things these tests search for — the banned
    bash-4 constructs, the mode check — so a raw substring match reads its prose as
    if it were code. That cuts both ways: it fails on correct code, and a future
    comment mentioning `stat` would give the mode-check assertion a false pass.
    """
    return "\n".join(
        line for line in LAUNCHER.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_launcher_execs_litellm():
    """The launcher's exec target must be litellm.

    Carried over from the old test_plist_example_program_arguments_is_no_shell, which
    asserted `"litellm" in binary`. When the plist stopped naming litellm directly
    that sanity check had nowhere to live, and without it a launcher that exec'd
    /bin/false --host 127.0.0.1 would satisfy every other guard here.
    """
    src = _launcher_code()
    assert re.search(r"^exec\s+\"?\$LITELLM", src, re.M), (
        f"{LAUNCHER.name} must end in `exec \"$LITELLM\" ...` — exec so launchd "
        f"tracks the litellm pid directly and KeepAlive restarts the right process."
    )
    assert re.search(r'LITELLM=.*litellm', src), (
        f"{LAUNCHER.name} must resolve LITELLM to the venv litellm binary"
    )


def test_plist_example_execs_the_pinned_launcher():
    """ProgramArguments[0] must be the launcher in the PINNED lib dir.

    This test used to assert the opposite — that args[0] was the venv litellm binary
    and never a shell. ARCH-342 moved the six provider keys out of the plist into a
    0600 file, and something has to source that file before exec'ing litellm, so a
    launcher is now deliberate. What still has to hold is that the plist execs the
    *installed* copy (ARCH-116): pointing it at the repo working tree would make
    whatever branch is checked out production on the next crash-restart.
    """
    with PLIST_EXAMPLE.open("rb") as f:
        plist = plistlib.load(f)
    args = plist.get("ProgramArguments", [])
    assert args, "ProgramArguments must not be empty"
    assert args == [f"{GATEWAY_LIB}/llm-gateway-run.sh"], (
        f"ProgramArguments should be exactly the pinned launcher "
        f"[{GATEWAY_LIB}/llm-gateway-run.sh], got: {args!r}. It must not point into "
        f"the repo working tree (ARCH-116) and needs no other flags — the launcher "
        f"carries them."
    )


def test_launcher_is_bash_3_2_safe():
    """The launcher must avoid bash-4 constructs (ARCH-082).

    The mini ships bash 3.2 and that is the interpreter launchd will use. This is the
    guard the old no-shell assertion was really standing in for: once a shell wrapper
    exists, the bash-4 risk is real and has to be pinned directly. `bash -n` does NOT
    catch `declare -A`, so match on the constructs.
    """
    src = _launcher_code()
    bash4 = {
        "declare -A": "associative arrays are bash 4",
        "mapfile": "mapfile is bash 4",
        "readarray": "readarray is bash 4",
        # NB: `${!var}` indirect expansion is deliberately NOT listed. It is bash 2+,
        # so flagging it would reject valid 3.2 code. Only `${!arr[@]}` over an
        # associative array is 4+, and `declare -A` above already catches that.
        ",,}": "lowercase expansion is bash 4",
        "^^}": "uppercase expansion is bash 4",
    }
    found = [f"{c} ({why})" for c, why in bash4.items() if c in src]
    assert not found, (
        f"{LAUNCHER.name} uses bash-4 construct(s): {found}. The mini runs bash 3.2 "
        f"(ARCH-082)."
    )


def test_launcher_binds_loopback():
    """The launcher's exec line must bind 127.0.0.1.

    Loopback binding moved from ProgramArguments into the launcher along with the
    rest of the invocation, so the guard follows it. Catches drift to 0.0.0.0 or a
    hostname that would expose the proxy beyond localhost.
    """
    src = _launcher_code()
    assert "--host 127.0.0.1" in src, (
        f"{LAUNCHER.name} must invoke litellm with --host 127.0.0.1; the gateway "
        f"holds six provider keys and must never bind beyond loopback."
    )
    assert "0.0.0.0" not in src, f"{LAUNCHER.name} must not bind 0.0.0.0"


def test_launcher_refuses_a_loose_secrets_file():
    """The launcher must check the secrets file's mode, not just its existence.

    THE BUG THIS PINS. The defect being fixed was a 0644 file holding six live API
    keys. Without a mode check, the next person to create the file by hand
    reintroduces exactly that, and nothing complains.
    """
    src = _launcher_code()
    assert "stat" in src and "600" in src, (
        f"{LAUNCHER.name} must reject a secrets file looser than 0600"
    )
    assert "exit 78" in src, (
        f"{LAUNCHER.name} should exit 78 (EX_CONFIG) on a bad secrets file, so a "
        f"misconfiguration is distinguishable from a crash in launchd's logs"
    )


def test_launcher_refuses_an_unsourceable_or_empty_secrets_file():
    """A file that exists at 0600 can still fail to deliver credentials.

    Two cases the mode check does not cover: sourcing fails (a syntax error from a
    half-finished edit), or it succeeds and defines nothing (truncated, emptied).
    Either way the gateway would come up clean and then answer every request with an
    upstream auth error, which reads like a provider outage and costs an hour to
    trace. The launcher must refuse both.
    """
    src = _launcher_code()
    assert re.search(r"if\s+!\s+\.\s+\"\$ENV_FILE\"", src), (
        f"{LAUNCHER.name} must check the exit status of sourcing $ENV_FILE; a bare "
        f"`. \"$ENV_FILE\"` continues to exec on a syntax error"
    )
    assert "LITELLM_MASTER_KEY" in src, (
        f"{LAUNCHER.name} must verify at least one required key is non-empty after "
        f"sourcing, so an empty-but-valid file cannot start a credential-less gateway"
    )


def test_plist_example_parses_cleanly():
    """The .plist.example must be valid XML plist (plutil equivalent check)."""
    with PLIST_EXAMPLE.open("rb") as f:
        plist = plistlib.load(f)
    assert plist.get("Label") == "com.convivy.llm-gateway"
    assert plist.get("RunAtLoad") is True
    assert plist.get("KeepAlive") is True


def test_router_has_no_kimi_fallback():
    """ARCH-186: kimi is cut — no gateway fallback may route to kimi (or kimi-code).

    The `deepseek → kimi` rescue (ARCH-130, for DeepSeek's 2026-06-11 json_schema
    400-storm) moved to the HARNESS: deepseek now fails over to sonnet
    (subscription-billed claude-direct), because the gateway has no Anthropic key
    and a gateway hop would bill the API. The `model_group_alias` is still
    load-bearing — agentctl sends the resolved "deepseek/deepseek-v4-flash" string,
    which LiteLLM must map onto the real "deepseek" group.
    """
    config = yaml.safe_load(GATEWAY_CONFIG.read_text())
    router = config.get("router_settings", {})
    alias = router.get("model_group_alias", {})
    assert alias.get("deepseek/deepseek-v4-flash") == "deepseek", (
        "agentctl's resolved model string must still alias onto the deepseek group"
    )
    merged: dict = {}
    for entry in router.get("fallbacks", []):
        merged.update(entry)
    for src, targets in merged.items():
        assert "kimi" not in targets and "kimi-code" not in targets, (
            f"ARCH-186: {src} must not fall back to kimi (kimi is cut)")
