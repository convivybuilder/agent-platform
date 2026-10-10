"""bin/agent-postgres-provision and launchd/com.convivy.agent-postgres.plist must
agree on which PostgreSQL to run (issue_01M4DYRS74G8SKZRXRA5015NP5).

THE INCIDENT. The first live run on macstudio on 2026-10-08 failed twice.
First, the provisioner's `_initdb`/`_pg_ctl` defaulted to `command -v`, which
resolved to PATH's newest Homebrew postgresql@N (18), while the plist execs
postgres from postgresql@15 — the server refused the data directory ("The
data directory was initialized by PostgreSQL version 18, which is not
compatible with this version 15.19"). Second, once both sides were forced to
15, the plist set no LC_ALL and the server died at startup ("postmaster
became multithreaded during startup... Set the LC_ALL environment variable").

This pins both fixes structurally rather than by reading either file's prose:
the provisioner's default binary prefix must be the exact directory the
plist's own `postgres` runs from, so a version bump that touches only one of
them fails here instead of shipping a second split; and the plist must carry
the same LC_ALL Homebrew's own postgresql 15 plist (label sh.brew.postgresql,
version 15) sets for the same binary.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROVISION_SCRIPT = REPO / "bin" / "agent-postgres-provision"
PLIST = REPO / "launchd" / "com.convivy.agent-postgres.plist"


def _provisioner_prefix() -> str:
    text = PROVISION_SCRIPT.read_text()
    m = re.search(r'^AGENT_POSTGRES_PREFIX="([^"]+)"', text, re.MULTILINE)
    assert m, "bin/agent-postgres-provision has no AGENT_POSTGRES_PREFIX=\"...\" assignment"
    return m.group(1)


def _plist_postgres_prefix() -> str:
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    argv = data["ProgramArguments"]
    postgres_bin = Path(argv[0])
    assert postgres_bin.name == "postgres", argv
    assert postgres_bin.parent.name == "bin", argv
    return str(postgres_bin.parent.parent)


def test_the_provisioners_default_binary_prefix_is_the_plists_postgres_prefix():
    assert _provisioner_prefix() == _plist_postgres_prefix()


def test_the_provisioner_defaults_initdb_and_pg_ctl_under_that_prefix():
    text = PROVISION_SCRIPT.read_text()
    assert 'echo "$AGENT_POSTGRES_PREFIX/bin/initdb"' in text
    assert 'echo "$AGENT_POSTGRES_PREFIX/bin/pg_ctl"' in text


def test_the_overrides_still_take_priority_over_the_prefix():
    text = PROVISION_SCRIPT.read_text()
    assert '[ -n "${AGENT_POSTGRES_INITDB:-}" ]' in text
    assert '[ -n "${AGENT_POSTGRES_PG_CTL:-}" ]' in text


def test_the_plist_sets_lc_all_matching_homebrews_own_postgresql_15_service():
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    assert data.get("EnvironmentVariables", {}).get("LC_ALL") == "en_US.UTF-8"
