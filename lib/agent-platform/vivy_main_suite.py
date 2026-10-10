"""vivy-main-suite — nightly main-branch test runner for vivy.

Runs vivy's full test suite (serial ordering-pollution gate + 3-shard parallel
matrix) against origin/main in a dedicated clone at
~/.local/state/vivy-main-suite/checkout. Silent on pass; logs an alert signal
on failure, and never a work item (platform/decisions/0073). Designed to catch combination drift from the day's merges before
the 21:00 preflight and overnight pipeline spend the night opening PRs against
a potentially broken main.

Extend PRODUCTS when a second repo drops its push:[main] trigger: add an
entry to PRODUCT_CONFIG and wire run() to iterate them. Only vivy is wired
today; every other fleet repo already has push:[main].
"""
from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import registry as R
from signal_client import post_alert, sanitise, sanitise_body
import builder_root as BR
import git_refs

# Only vivy lacks push:[main]; extend this list when another repo needs
# overnight main coverage. Each entry maps to a key in PRODUCT_CONFIG below.
# Adding an entry here is not sufficient — run() must also be wired to iterate it.
PRODUCTS = ["vivy"]

PRODUCT_CONFIG: dict[str, dict] = {
    "vivy": {
        "remote": "git@github.com:convivy/vivy.git",
        "venv": BR.root() / "vivy" / ".venv",
    },
}

STATE_DIR = Path.home() / ".local" / "state" / "vivy-main-suite"
CHECKOUT_DIR = STATE_DIR / "checkout"
LOG_DIR = STATE_DIR / "logs"

# vivy's local DB convention (Makefile line 1): postgresql+asyncpg://$(USER)@...
# NOT CI's service-container URL (postgres:postgres@localhost), which is for a
# disposable container. The mini uses the user's own local Postgres.
TEST_DB_URL = f"postgresql+asyncpg://{os.environ.get('USER', '')}@localhost:5432/vivy_test"
JWT_SECRET = "vivy-main-suite-not-a-real-secret"

_HARNESS_ALERT_STREAM = "vivy-main-suite"

# The mini has 10 physical cores (M-series Mac mini). Reserve 2 for the OS and
# background launchd tasks; use 8 for pytest-xdist workers per shard. CI pins
# -n 4 because the hosted ubuntu-latest image has 4 vCPU (ci1's count is not
# verified) but only 2 physical cores (-n auto
# reads 2, halving throughput, so CI overrides to 4 explicitly). Same reasoning
# here — the mini's physical count is what matters, and 8 of 10 is the right
# share for a background overnight job.
N_SHARD_WORKERS = 8


@dataclass
class RunResult:
    exit_code: int
    failed_tests: list[str] = field(default_factory=list)
    log_path: Path | None = None
    stage: str = ""


def check_live_runs() -> list[dict]:
    """Return currently active agent runs from the platform registry."""
    return R.list_runs()


def ensure_checkout(checkout_dir: Path, remote_url: str) -> None:
    """Create a dedicated clone of vivy if it does not already exist.

    The clone lives at ~/.local/state/vivy-main-suite/checkout — outside
    <root>/worktrees/ (so never subject to worktrees-cleanup) and outside
    <root> (so never discovered as a primary). A git worktree on `main`
    would conflict with the primary checkout's branch; a standalone clone has
    no branch contention.
    """
    if checkout_dir.exists():
        return
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(
        ["git", "clone", remote_url, str(checkout_dir)],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"git clone failed: {cp.stderr.strip()}")


def reset_to_origin_main(checkout_dir: Path) -> str:
    """Fetch origin/main and hard-reset to it. Returns the resolved 40-char SHA.

    Hard-resetting before each run prevents any state from a previous run
    (a leftover .coverage, a modified file, a failed migration) from leaking
    into the current run.
    """
    subprocess.run(
        ["git", "fetch", "origin", "main"],
        cwd=str(checkout_dir), capture_output=True, check=True,
    )
    # The suite runs whatever this commit holds, so the ref is resolved exactly
    # (git_refs) and a missing one refuses rather than resetting to a tag.
    main_sha = git_refs.origin_main_sha(checkout_dir)
    if main_sha is None:
        raise RuntimeError(f"{git_refs.ORIGIN_MAIN_REF} does not exist in {checkout_dir}; not resetting")
    subprocess.run(
        ["git", "reset", "--hard", main_sha],
        cwd=str(checkout_dir), capture_output=True, check=True,
    )
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(checkout_dir), capture_output=True, text=True, check=True,
    )
    return cp.stdout.strip()


def run_alembic(
    venv: Path, checkout_dir: Path, db_url: str,
) -> subprocess.CompletedProcess:
    """Run alembic upgrade head against the test database."""
    return subprocess.run(
        [str(venv / "bin" / "alembic"), "upgrade", "head"],
        cwd=str(checkout_dir),
        capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": db_url, "JWT_SECRET": JWT_SECRET},
    )


def _extract_failed_tests(junitxml: Path) -> list[str]:
    """Parse a JUnit XML for failing test names as classname::name strings."""
    try:
        tree = ET.parse(junitxml)
    except (ET.ParseError, OSError):
        return []
    failed = []
    for tc in tree.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            cls = tc.get("classname", "")
            name = tc.get("name", "")
            failed.append(f"{cls}::{name}" if cls else name)
    return failed


def run_serial_gate(
    venv: Path, checkout_dir: Path, db_url: str,
    log_dir: Path, datestamp: str,
) -> RunResult:
    """Serial ordering-pollution gate: full suite, unsharded, -q.

    Must NOT be sharded — the gate exists to catch cross-test state-pollution
    that only appears in natural collection order. Sharding would split the
    collection and defeat that purpose.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    xml_path = log_dir / f"serial-{datestamp}.xml"
    log_path = log_dir / f"serial-{datestamp}.log"
    cmd = [
        str(venv / "bin" / "python"), "-m", "pytest", "-q",
        "--cov=app", "--cov-report=json",
        f"--junitxml={xml_path}",
    ]
    env = {
        **os.environ,
        "TEST_DATABASE_URL": db_url,
        "DATABASE_URL": db_url,
        "JWT_SECRET": JWT_SECRET,
    }
    with log_path.open("w") as lf:
        cp = subprocess.run(cmd, cwd=str(checkout_dir), env=env,
                            stdout=lf, stderr=subprocess.STDOUT)
    return RunResult(
        exit_code=cp.returncode,
        failed_tests=_extract_failed_tests(xml_path),
        log_path=log_path,
        stage="serial",
    )


def run_shard(
    venv: Path, checkout_dir: Path, db_url: str,
    log_dir: Path, datestamp: str, group: int,
) -> RunResult:
    """One shard of the parallel matrix (group 1, 2, or 3 of 3).

    Shards run sequentially on the mini — there is one shared test database and
    no parallel contention to worry about. -n 8 uses 8 pytest-xdist workers per
    shard (10 physical cores on the mini; 2 reserved for OS/background).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    xml_path = log_dir / f"shard-{group}-{datestamp}.xml"
    log_path = log_dir / f"shard-{group}-{datestamp}.log"
    cmd = [
        str(venv / "bin" / "python"), "-m", "pytest", "-v",
        "--splits", "3", "--group", str(group),
        "-n", str(N_SHARD_WORKERS),
        f"--junitxml={xml_path}",
    ]
    env = {
        **os.environ,
        "TEST_DATABASE_URL": db_url,
        "DATABASE_URL": db_url,
        "JWT_SECRET": JWT_SECRET,
    }
    with log_path.open("w") as lf:
        cp = subprocess.run(cmd, cwd=str(checkout_dir), env=env,
                            stdout=lf, stderr=subprocess.STDOUT)
    return RunResult(
        exit_code=cp.returncode,
        failed_tests=_extract_failed_tests(xml_path),
        log_path=log_path,
        stage=f"shard-{group}",
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _weekly_once_key() -> str:
    """Weekly dedup_key bucket for the failure signal.

    The store's dedup_key is unique for all time, so a bare key would answer a
    reintroduced break `duplicate` forever. A weekly bucket prevents that: a new
    break in a later week gets a new key and logs a fresh signal. Cap: at most one
    signal per week of active breakage.
    """
    week = _utcnow().strftime("%G-W%V")
    return f"vivy-main-suite-broken-{week}"


def _emit_harness_alert(msg: str, run_log: Path | None = None) -> None:
    """Post a plane alert when the runner itself cannot run.

    Best-effort: a failing post_alert is logged to run_log (if provided) and
    swallowed so it cannot change the caller's exit code.

    Weekly dedup_key mirrors _weekly_once_key's rationale: a persistently broken
    runner emits at most one alert per week, not one per night.
    """
    week = _utcnow().strftime("%G-W%V")
    try:
        # msg carries exception text from the git/venv/alembic steps — external
        # output, so it is sanitised before it lands on the alerts page.
        post_alert(
            _HARNESS_ALERT_STREAM,
            f"vivy-main-suite harness error: {sanitise(msg)}",
            product="platform",
            dedup_key=f"vivy-main-suite-harness-error-{week}",
        )
    except Exception as exc:
        if run_log is not None:
            _log(f"WARNING: could not post harness alert: {exc}", run_log)


def signal_failure(
    sha: str, results: list[RunResult], log_dir: Path,
) -> str:
    """Log an alert signal for the failing suite. Idempotent via dedup_key.

    The dedup_key is a weekly bucket (_weekly_once_key): a persistent break logs
    one signal per week rather than one per night, and a reintroduced break after
    a fix gets a fresh key in the new week. The title carries the failing SHA so
    the body is useful even when the store answers "duplicate". Returns the
    signal_client status.
    """
    all_failed: list[str] = []
    for r in results:
        all_failed.extend(r.failed_tests)
    unique_failed = list(dict.fromkeys(all_failed))[:20]

    title = f"vivy main-suite failure at {sha[:12]}"
    lines: list[str] = [f"SHA: `{sha}`", ""]
    if unique_failed:
        lines.append("Failed tests:")
        for t in unique_failed:
            lines.append(f"- `{t}`")
        lines.append("")
    failed_stages = [r.stage for r in results if r.exit_code != 0]
    if failed_stages:
        lines.append(f"Failed stages: {', '.join(failed_stages)}")
        lines.append("")
    lines.append(f"Logs: `{log_dir}`")

    return post_alert(
        _HARNESS_ALERT_STREAM,
        sanitise_body(f"{title}\n\n" + "\n".join(lines)),
        product="vivy",
        dedup_key=_weekly_once_key(),
        meta={"severity": "high"},
    )


def _log(msg: str, run_log: Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    try:
        with run_log.open("a") as f:
            print(line, file=f)
    except OSError:
        pass


def run(
    *,
    checkout_dir: Path = CHECKOUT_DIR,
    venv: Path | None = None,
    remote_url: str | None = None,
    db_url: str | None = None,
    log_dir: Path = LOG_DIR,
) -> int:
    """Main entry point. Returns 0=pass, 1=test failure, 2=harness error.

    A harness error (2) means the suite could not be run at all — bad checkout,
    missing venv, failed migration, network unreachable. It is distinct from the
    suite running and tests failing (1), and from a clean run (0).
    """
    _venv = venv or PRODUCT_CONFIG["vivy"]["venv"]
    _remote = remote_url or PRODUCT_CONFIG["vivy"]["remote"]
    _db_url = db_url or TEST_DB_URL

    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / "runner.log"

    # Check for live runs. Proceed regardless: the dedicated clone shares no
    # state with the agent worktree system, so live runs do not contend with
    # this runner. Log them so the run artifact records the system state.
    live = check_live_runs()
    if live:
        run_ids = ", ".join(r.get("run_id", "?") for r in live)
        _log(
            f"proceeding with {len(live)} live run(s) active ({run_ids}); "
            "dedicated clone, no worktree contention",
            run_log,
        )

    try:
        ensure_checkout(checkout_dir, _remote)
    except RuntimeError as e:
        _log(f"HARNESS ERROR: could not create checkout: {e}", run_log)
        _emit_harness_alert(f"could not create checkout: {e}", run_log)
        return 2

    try:
        sha = reset_to_origin_main(checkout_dir)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        _log(f"HARNESS ERROR: could not reset to origin/main: {e}", run_log)
        _emit_harness_alert(f"could not reset to origin/main: {e}", run_log)
        return 2
    _log(f"testing vivy main at {sha}", run_log)

    if not _venv.exists():
        _log(
            f"HARNESS ERROR: venv not found at {_venv}; "
            f"run: cd {_venv.parent} && python -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt",
            run_log,
        )
        _emit_harness_alert(f"venv not found at {_venv}", run_log)
        return 2

    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    alembic_cp = run_alembic(_venv, checkout_dir, _db_url)
    if alembic_cp.returncode != 0:
        _log(
            f"HARNESS ERROR: alembic upgrade head failed:\n{alembic_cp.stderr}",
            run_log,
        )
        _emit_harness_alert("alembic upgrade head failed", run_log)
        return 2

    serial = run_serial_gate(_venv, checkout_dir, _db_url, log_dir, datestamp)
    _log(
        f"serial gate: exit={serial.exit_code} failures={len(serial.failed_tests)}",
        run_log,
    )

    shards: list[RunResult] = []
    for group in (1, 2, 3):
        shard = run_shard(_venv, checkout_dir, _db_url, log_dir, datestamp, group)
        _log(
            f"shard {group}/3: exit={shard.exit_code} failures={len(shard.failed_tests)}",
            run_log,
        )
        shards.append(shard)

    all_results = [serial] + shards
    if any(r.exit_code != 0 for r in all_results):
        signal_status = signal_failure(sha, all_results, log_dir)
        _log(f"logged failure signal: {signal_status}", run_log)
        return 1

    _log(f"vivy main at {sha}: all tests passed", run_log)
    return 0
