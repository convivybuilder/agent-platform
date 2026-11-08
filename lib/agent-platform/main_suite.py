"""main-suite — nightly main-branch test runner for every opted-in product.

Runs each opted-in product's full test suite (serial ordering-pollution gate +
3-shard parallel matrix) against its own origin/main in a dedicated clone at
~/.local/state/main-suite/<slug>/checkout. Silent on pass per product; logs an
alert signal on failure, and never a work item (platform/decisions/0073).
Designed to catch combination drift from the day's merges before the next
day's work builds on a potentially broken main.

A product opts in by setting a non-empty `main_suite_test_db` field on its
project-registry unit (its postgres test-database name) — see
`_product_configs()`. A unit with no such field is not tested; today exactly
one does, since every other fleet repo already has push:[main] and has no
reason to opt in. Everything else the job needs for that product — the git
remote, the venv to run pytest from — is derived from fields the registry
already carries (`repos`, `home_dir`) rather than hand-maintained here, so
adding a second product costs a registry edit, never a code change.
"""
from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import git_refs
import projects
import registry as R
from signal_client import post_alert, sanitise, sanitise_body

STATE_DIR = Path.home() / ".local" / "state" / "main-suite"

# Each opted-in product's local DB convention:
# postgresql+asyncpg://$(USER)@localhost/<main_suite_test_db> — NOT CI's
# service-container URL (postgres:postgres@localhost), which is for a
# disposable container. The host that runs this job uses the operator's own
# local Postgres.
def _test_db_url(db_name: str) -> str:
    return f"postgresql+asyncpg://{os.environ.get('USER', '')}@localhost:5432/{db_name}"


JWT_SECRET = "main-suite-harness-not-a-real-secret"

# The host that runs this job is assumed to have 10 physical cores. Reserve
# 2 for the OS and background launchd tasks; use 8 for pytest-xdist workers
# per shard. CI pins -n 4 because the hosted ubuntu-latest image has 4 vCPU
# (ci1's count is not verified) but only 2 physical cores (-n auto
# reads 2, halving throughput, so CI overrides to 4 explicitly). Same reasoning
# here — the physical count is what matters, and 8 of 10 is the right
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


def _product_configs() -> dict[str, dict]:
    """slug -> {remote, venv, db_url} for every registry unit opted into
    main-suite: a non-empty `main_suite_test_db` field (the postgres test
    database name) — the one new registry field this job reads. Everything
    else here is derived from fields the registry already carries: the git
    remote from `repos`[0] via `projects.repo_github_slug`, and the venv from
    `home_dir` via `projects.registry_home` (the same checkout the operator
    already has installed and `pip install -r requirements.txt`-ed, reused
    across runs rather than built fresh in the dedicated clone).

    Raises whatever `projects.load()` raises on an unreadable registry — the
    caller's job to turn into a harness error, never a silent pass."""
    configs: dict[str, dict] = {}
    for slug, unit in projects.load().items():
        db_name = unit.get("main_suite_test_db")
        if not isinstance(db_name, str) or not db_name:
            continue
        repo = unit["repos"][0]
        configs[slug] = {
            "remote": f"git@github.com:{projects.repo_github_slug(repo)}.git",
            "venv": projects.registry_home(unit["home_dir"]) / ".venv",
            "db_url": _test_db_url(db_name),
        }
    return configs


def ensure_checkout(checkout_dir: Path, remote_url: str) -> None:
    """Create a dedicated clone of the product if it does not already exist.

    The clone lives at ~/.local/state/main-suite/<slug>/checkout — outside
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

    Shards run sequentially — there is one shared test database and no
    parallel contention to worry about. -n 8 uses 8 pytest-xdist workers per
    shard (the host is assumed to have 10 physical cores; 2 reserved for
    OS/background).
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


def _alert_stream(slug: str) -> str:
    """The alert-store stream a product's main-suite signals post to:
    `<slug>-main-suite` — distinct per product so two products breaking in the
    same week don't compete for one dedup_key, and today's one live product
    keeps the exact stream name it has always posted to."""
    return f"{slug}-main-suite"


def _weekly_once_key(slug: str) -> str:
    """Weekly dedup_key bucket for `slug`'s failure signal.

    The store's dedup_key is unique for all time, so a bare key would answer a
    reintroduced break `duplicate` forever. A weekly bucket prevents that: a new
    break in a later week gets a new key and logs a fresh signal. Cap: at most one
    signal per week of active breakage, per product.
    """
    week = _utcnow().strftime("%G-W%V")
    return f"{slug}-main-suite-broken-{week}"


def _emit_harness_alert(slug: str, msg: str, run_log: Path | None = None) -> None:
    """Post a plane alert when the runner itself cannot run `slug`'s suite.

    Best-effort: a failing post_alert is logged to run_log (if provided) and
    swallowed so it cannot change the caller's exit code.

    Weekly dedup_key mirrors _weekly_once_key's rationale: a persistently broken
    runner emits at most one alert per week per product, not one per night.
    """
    week = _utcnow().strftime("%G-W%V")
    try:
        # msg carries exception text from the git/venv/alembic steps — external
        # output, so it is sanitised before it lands on the alerts page.
        post_alert(
            _alert_stream(slug),
            f"{slug} main-suite harness error: {sanitise(msg)}",
            product="platform",
            dedup_key=f"{slug}-main-suite-harness-error-{week}",
        )
    except Exception as exc:
        if run_log is not None:
            _log(f"WARNING: could not post harness alert: {exc}", run_log)


def _registry_harness_alert(msg: str, run_log: Path | None = None) -> None:
    """Post a plane alert when the product registry itself cannot be read —
    there is no product slug to attach the alert to, since the harness could
    not learn which products to even consider. Weekly dedup_key and
    best-effort posting, same rationale as `_emit_harness_alert`."""
    week = _utcnow().strftime("%G-W%V")
    try:
        post_alert(
            "main-suite",
            f"main-suite harness error: {sanitise(msg)}",
            product="platform",
            dedup_key=f"main-suite-registry-unreadable-{week}",
        )
    except Exception as exc:
        if run_log is not None:
            _log(f"WARNING: could not post harness alert: {exc}", run_log)


def signal_failure(
    slug: str, sha: str, results: list[RunResult], log_dir: Path,
) -> str:
    """Log an alert signal for `slug`'s failing suite. Idempotent via dedup_key.

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

    title = f"{slug} main-suite failure at {sha[:12]}"
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
        _alert_stream(slug),
        sanitise_body(f"{title}\n\n" + "\n".join(lines)),
        product=projects.signal_product(slug),
        dedup_key=_weekly_once_key(slug),
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


def run_one(
    slug: str,
    *,
    checkout_dir: Path,
    venv: Path,
    remote_url: str,
    db_url: str,
    log_dir: Path,
) -> int:
    """Run one product's main-suite. Returns 0=pass, 1=test failure, 2=harness
    error.

    A harness error (2) means the suite could not be run at all — bad checkout,
    missing venv, failed migration, network unreachable. It is distinct from the
    suite running and tests failing (1), and from a clean run (0).
    """
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
        ensure_checkout(checkout_dir, remote_url)
    except RuntimeError as e:
        _log(f"HARNESS ERROR: could not create checkout: {e}", run_log)
        _emit_harness_alert(slug, f"could not create checkout: {e}", run_log)
        return 2

    try:
        sha = reset_to_origin_main(checkout_dir)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        _log(f"HARNESS ERROR: could not reset to origin/main: {e}", run_log)
        _emit_harness_alert(slug, f"could not reset to origin/main: {e}", run_log)
        return 2
    _log(f"testing {slug} main at {sha}", run_log)

    if not venv.exists():
        _log(
            f"HARNESS ERROR: venv not found at {venv}; "
            f"run: cd {venv.parent} && python -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt",
            run_log,
        )
        _emit_harness_alert(slug, f"venv not found at {venv}", run_log)
        return 2

    datestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    alembic_cp = run_alembic(venv, checkout_dir, db_url)
    if alembic_cp.returncode != 0:
        _log(
            f"HARNESS ERROR: alembic upgrade head failed:\n{alembic_cp.stderr}",
            run_log,
        )
        _emit_harness_alert(slug, "alembic upgrade head failed", run_log)
        return 2

    serial = run_serial_gate(venv, checkout_dir, db_url, log_dir, datestamp)
    _log(
        f"serial gate: exit={serial.exit_code} failures={len(serial.failed_tests)}",
        run_log,
    )

    shards: list[RunResult] = []
    for group in (1, 2, 3):
        shard = run_shard(venv, checkout_dir, db_url, log_dir, datestamp, group)
        _log(
            f"shard {group}/3: exit={shard.exit_code} failures={len(shard.failed_tests)}",
            run_log,
        )
        shards.append(shard)

    all_results = [serial] + shards
    if any(r.exit_code != 0 for r in all_results):
        signal_status = signal_failure(slug, sha, all_results, log_dir)
        _log(f"logged failure signal: {signal_status}", run_log)
        return 1

    _log(f"{slug} main at {sha}: all tests passed", run_log)
    return 0


def run() -> int:
    """Main entry point. Reads the project registry for every product opted
    into the nightly main-branch suite (`_product_configs()`) and runs
    `run_one()` for each, under its own state subdirectory. Returns the worst
    exit code across all of them: 2 (harness error) beats 1 (test failure)
    beats 0 (clean) — numerically `max()`, since the codes are already ordered
    by severity.

    An unreadable registry is itself a harness error — never a silent pass —
    and returns 2 without running any suite. A registry with no product opted
    in returns 0: nothing to test is not a failure.
    """
    log_dir = STATE_DIR / "logs"
    run_log = log_dir / "runner.log"
    try:
        configs = _product_configs()
    except (OSError, json.JSONDecodeError, projects.RegistryUnavailable, ValueError) as e:
        log_dir.mkdir(parents=True, exist_ok=True)
        _log(f"HARNESS ERROR: could not read the project registry: {e}", run_log)
        _registry_harness_alert(f"could not read the project registry: {e}", run_log)
        return 2

    if not configs:
        log_dir.mkdir(parents=True, exist_ok=True)
        _log(
            "no product opted into main-suite (no registry unit sets "
            "main_suite_test_db); nothing to run",
            run_log,
        )
        return 0

    worst = 0
    for slug, cfg in configs.items():
        product_dir = STATE_DIR / slug
        rc = run_one(
            slug,
            checkout_dir=product_dir / "checkout",
            venv=cfg["venv"],
            remote_url=cfg["remote"],
            db_url=cfg["db_url"],
            log_dir=product_dir / "logs",
        )
        worst = max(worst, rc)
    return worst
