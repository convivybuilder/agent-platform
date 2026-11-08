"""`agentctl crew create <id>` — one command that makes a working crew.

issue_01M4AB9X62112MCG8B5501CMKV (slice 3 of issue_01M2NYBBY0FXCDHWWD8XBR9RR3).

THE ACCEPTANCE TEST is `test_a_throwaway_crew_passes_crew_doctor_with_no_other_edit`.
It runs the real command, the real `crew register`, the real `knowledge-ctx use`,
the real `co --resolve` and the real doctor, against:

  - a temporary registry file (PROJECTS_JSON, a copy of the checked-in file) that
    every READ in this sandbox goes through `projects.py` to reach — CQ·564 moved
    `crew register`'s WRITE off this file entirely, onto `_StubLibrary`'s own
    `POST /registry/crews` below, which stores the accepted row into this same
    file so the two sides of the sandbox keep seeing one answer, the way the
    Library's own single store does in production,
  - a temporary crews root, pinned through a temporary session-agent.env (the
    machine's root is read from that file and has no environment override, so
    the sandbox reaches `co` and `knowledge-ctx` through their own root seams:
    `co --root-config`, and a wrapper that sets `builder_root.CONFIG`),
  - a temporary knowledge checkout, where the simulated merge writes the profile,
  - a temporary Deck crews.d,
  - a stub Library on a loopback port (the one the `vault_plane` default points
    at is never reached), serving `GET /profiles*`, the one document read and
    `POST /registry/crews`,
  - a registry-writer token file under the sandbox's own `$HOME`, and a stub for
    `crew_registration._repo_ci_workflows` so registering the crew needs no real
    (or fake) GitHub App call,
  - a private tmux socket, with the pane's `co` replaced by a sleeping stub.

The run is two invocations, because that is what the Library's load makes true:
the first leaves steps 5 to 7 pending and the doctor fails on the profile site;
the second, after the stub Library starts serving the profile, finishes the crew
and the doctor passes. Nothing else is edited between them.

NO TEST REACHES THE REAL LIBRARY, GITHUB OR THE DEFAULT TMUX SERVER. The proposal
is replaced by a function that writes the profile into the temp checkout, and
every tmux call goes through `CREW_TMUX_SOCKET`, which the fixture makes unique.
The fixture kills only this test's named sessions. It never runs `kill-server`.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib" / "agent-platform"))
import crew_create as CC  # noqa: E402
import crew_doctor as CD  # noqa: E402
import crew_registration as CR  # noqa: E402
import projects  # noqa: E402
import vault_plane as VP  # noqa: E402

CREW = "throwaway-crew"
DISPLAY = "Throwaway Crew"
DOC_ID = "platform/agents/reviewer"
DOC_BODY = "# Reviewer\n\nReviews a diff for correctness and security.\n"
DOC_SHA = hashlib.sha256(DOC_BODY.encode("utf-8")).hexdigest()
TOKEN = "crew-create-test-token"


# --- the stub Library -------------------------------------------------------

class _StubLibrary:
    """Serves the routes the crew path reads — a profile, the profile
    listing, a document body — and, now that CQ·564 moved `crew register`'s
    write onto the Library, `POST /registry/crews` too. `loaded` is the
    Library's load: until it flips, `GET /profiles/<crew>/daytime` answers
    404, which is what a merged but not yet loaded profile looks like (epic
    comment 2842).

    `registry_path` is this stub's OWN backing store for the registration
    route — a real Library persists crews somewhere of its own; this test's
    stand-in persists them into the same file `PROJECTS_JSON` already points
    `projects.py`'s reads at, so a read right after a registration in this
    sandbox sees it, the same guarantee production gets from both routes
    being the one real store."""

    def __init__(self, crew: str, registry_path: Path):
        self.crew = crew
        self.loaded = False
        self.requests: list[str] = []
        self.registry_path = registry_path
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                code, body = outer.answer(self.path)
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw_in = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw_in or b"{}")
                except ValueError:
                    payload = {}
                outer.requests.append(self.path)
                code, body = outer.answer_post(self.path, payload)
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer_post(self, path: str, payload: dict) -> tuple[int, dict]:
        if path != "/registry/crews":
            return 404, {"error": "not found"}
        slug = payload.get("slug")
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if not slug or slug in data:
            return 409, {"error": f"{slug!r} already claimed"}
        row = {k: v for k, v in payload.items() if k != "slug"}
        row["updated_at"] = "2026-10-26T00:00:00Z"
        row["updated_by"] = "person_01TESTBOSUNWRITER00000000"
        data[slug] = row
        self.registry_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 201, {**row, "slug": slug}

    def answer(self, path: str) -> tuple[int, dict]:
        parts = [urllib.parse.unquote(p) for p in path.split("?")[0].split("/") if p]
        if parts == ["profiles"]:
            rows = [{"project": self.crew, "variant": "daytime"}] if self.loaded else []
            return 200, {"profiles": rows}
        if len(parts) == 3 and parts[0] == "profiles":
            if not self.loaded or parts[1] != self.crew or parts[2] != "daytime":
                return 404, {"error": "not found"}
            return 200, {
                "project": self.crew,
                "variant": "daytime",
                "count": 1,
                "documents": [{"id": DOC_ID, "body_sha256": DOC_SHA}],
                "rules": [{"position": 0, "matches": 1}],
                "claude_md_extras": "",
            }
        if parts == ["reference", "documents"]:
            # `knowledge-ctx use` lists the whole corpus before it individuates
            # (`all_agent_and_skill_ids`, CQ·371) — one request per kind, with
            # no id segment. Gated on `self.loaded` like the single-document
            # read below: the reviewer doc the profile references isn't in the
            # corpus until the simulated merge lands either.
            kind = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "").get(
                "kind", [""])[0]
            docs = [{"id": DOC_ID}] if (kind == "agent" and self.loaded) else []
            return 200, {"documents": docs, "truncated": False}
        if parts[:2] == ["reference", "documents"]:
            # knowledge-ctx leaves "/" unescaped in a document id, so the id is the rest of the path.
            if "/".join(parts[2:]) == DOC_ID and self.loaded:
                return 200, {
                    "id": DOC_ID,
                    "source_ref": "projects/platform/agents/reviewer.md",
                    "kind": "agent",
                    "title": "Reviewer",
                    "body": DOC_BODY,
                    "body_sha256": DOC_SHA,
                    "trigger": "Review a diff.",
                }
        return 404, {"error": "not found"}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# --- the sandbox -----------------------------------------------------------

def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    """Every tmux command the test issues: private socket, TMUX stripped."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    return subprocess.run(["tmux", "-L", socket, *args], capture_output=True,
                          text=True, env=env, timeout=30, check=False)


def _write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch, agentctl):
    root = tmp_path / "Convivy"
    root.mkdir()
    registry = tmp_path / "projects.json"
    cfg = tmp_path / "session-agent.env"
    cfg.write_text(f"CREWS_ROOT={root}\nPROJECTS_REGISTRY={registry}\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / ".library").mkdir(parents=True)
    client_tokens = home / ".library" / "client-tokens.json"
    client_tokens.write_text(
        json.dumps({TOKEN: {"name": "bosun"}}), encoding="utf-8")
    index = home / ".knowledge-index" / "index.db"
    index.parent.mkdir(parents=True)
    con = sqlite3.connect(index)
    con.executescript((REPO / "lib" / "knowledge" / "schema.sql").read_text(encoding="utf-8"))
    con.execute("INSERT INTO docs (id, source, source_ref, kind, project, title, status, body, "
                "body_hash, updated_at) VALUES (?, 'files', ?, 'agent', 'platform', 'Reviewer', "
                "'current', ?, ?, '2026-10-06T00:00:00Z')",
                (DOC_ID, "projects/platform/agents/reviewer.md", DOC_BODY, DOC_SHA))
    con.commit()
    con.close()

    knowledge = root / "knowledge"
    (knowledge / "projects").mkdir(parents=True)

    registry.write_text(
        (REPO / "tests" / "fixtures" / "rich_registry.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    crews_dir = tmp_path / "deck" / "crews.d"
    crews_dir.mkdir(parents=True)

    stubs = tmp_path / "stubs"
    _write_exec(stubs / "co", "#!/bin/sh\nexec sleep 600\n")
    driver = _write_exec(tmp_path / "wrap" / "driver.py", (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO / 'lib' / 'agent-platform')!r})\n"
        "import builder_root as BR\n"
        f"BR.CONFIG = Path({str(cfg)!r})\n"
        "script = sys.argv[1]\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(script, run_name='__main__')\n"))
    # `co` resolves PROJECTS_REGISTRY from `cfg`'s `--root-config` (set above), so it
    # reads the same temp registry this fixture builds. It still gets a checkout of its
    # own — a copy of bin/co, with lib and builder-root shared back — because sibling
    # lookups (`../builder-root`, `../lib/agent-platform`) resolve against that copy.
    co_checkout = tmp_path / "co-checkout"
    (co_checkout / "bin").mkdir(parents=True)
    (co_checkout / "config").mkdir()
    shutil.copy2(REPO / "bin" / "co", co_checkout / "bin" / "co")
    (co_checkout / "bin" / "builder-root").symlink_to(REPO / "bin" / "builder-root")
    (co_checkout / "lib").symlink_to(REPO / "lib")
    (co_checkout / "config" / "projects.json").symlink_to(registry)
    co_wrap = _write_exec(tmp_path / "wrap" / "co", (
        "#!/bin/sh\n"
        f"exec {str(co_checkout / 'bin' / 'co')!r} --root-config {str(cfg)!r} \"$@\"\n"))
    kctx_wrap = _write_exec(tmp_path / "wrap" / "knowledge-ctx", (
        "#!/bin/sh\n"
        f"exec {sys.executable!r} {str(driver)!r} {str(REPO / 'bin' / 'knowledge-ctx')!r} \"$@\"\n"))

    library = _StubLibrary(CREW, registry)
    socket = f"crew-create-{uuid.uuid4().hex[:8]}"
    # Every subprocess (knowledge-ctx, co) reads its Library address from the environment,
    # so the stub is named there too. Without this, knowledge-ctx reached the live Library.
    monkeypatch.setenv("LIBRARY_BASE_URL", library.url)
    # `knowledge-ctx`'s client-token lookup resolves through `BR.real_account_home()`
    # (the passwd home), not `$HOME` (issue_01M6A7CBACJG5XTR1JK4WH6PRQ) — deliberately,
    # so a dispatched run's redirected HOME can't shadow the operator's real credential.
    # This sandbox's `home` is neither, so the subprocess needs the override env var to
    # find the planted token file at all.
    monkeypatch.setenv("LIBRARY_CLIENT_TOKENS", str(client_tokens))

    # The registry-writer credential `crew_registration._post_crew_row` reads
    # (CQ·564) — under this sandbox's own $HOME, never the real
    # ~/.orient-control.
    (home / ".orient-control").mkdir(parents=True)
    (home / ".orient-control" / "agent-platform-registry-writer-token").write_text(
        "crew-create-test-writer-token\n", encoding="utf-8")

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PATH", f"{stubs}:/opt/homebrew/bin:/usr/bin:/bin")
    monkeypatch.setenv("AGENT_SIGNAL_TOKEN", TOKEN)
    monkeypatch.setenv("CREW_TMUX_SOCKET", socket)
    monkeypatch.setenv("PROJECTS_JSON", str(registry))

    monkeypatch.setattr(projects, "PROJECTS_JSON", registry)
    projects._load_raw.cache_clear()
    monkeypatch.setattr(projects, "ROOT_OVERRIDE", root)
    monkeypatch.setattr(agentctl, "KNOWLEDGE_ROOT", knowledge)
    monkeypatch.setattr(CD, "DEFAULT_INDEX_DB", index)
    monkeypatch.setattr(VP, "base_url", lambda: library.url)
    # `crew_registration._post_crew_row` builds its URL through
    # `vault_plane.library_url`, which reads `LIBRARY_BASE_URL` directly
    # rather than through `base_url()` — both need the stub's address.
    monkeypatch.setattr(VP, "LIBRARY_BASE_URL", library.url)
    # No real (or fake) GitHub App call: CQ·564's ci_workflows fill is a
    # live GitHub read this sandbox has no need to exercise.
    monkeypatch.setattr(CR, "_repo_ci_workflows", lambda repo, row, **kw: ["ci.yml"])
    monkeypatch.setattr(CD, "_own_bin", lambda name, base: {
        "co": co_wrap, "knowledge-ctx": kctx_wrap}[name])

    def simulated_merge(rel, content, title, rationale):
        """The proposal's merge, simulated: the profile lands in the checkout."""
        target = knowledge / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"https://github.com/convivy/knowledge/pull/0 ({title})"

    monkeypatch.setattr(agentctl, "_propose_crew_profile", simulated_merge)

    box = argparse.Namespace(
        root=root, cfg=cfg, home=home, knowledge=knowledge, registry=registry,
        crews_dir=crews_dir, socket=socket, library=library,
        working=root / CREW, agentctl=agentctl)
    yield box

    for _, name in _session_names(socket):
        _tmux(socket, "kill-session", "-t", name)
    library.close()
    projects._load_raw.cache_clear()


def _session_names(socket: str) -> list[tuple[str, str]]:
    cp = _tmux(socket, "list-sessions", "-F", "#{session_id} #{session_name}")
    if cp.returncode != 0:
        return []
    return [tuple(line.split(" ", 1)) for line in cp.stdout.splitlines() if line]


def _create_args(box, **over) -> argparse.Namespace:
    base = dict(slug=CREW, display=DISPLAY, dir=str(box.working),
                registry=str(box.registry), crews_dir=str(box.crews_dir))
    base.update(over)
    return argparse.Namespace(**base)


# --- the acceptance test ----------------------------------------------------

# The doctor's line for `check_co_resolves`, which runs the real `co --resolve`
# against the finished crew's directory.
CO_SITE = "co -r resolves the crew"

# Both tests below need the doctor's `check_co_resolves` to actually run `bin/co`, a
# `#!/bin/zsh` script, through `sandbox`'s `co_wrap` → `co_checkout/bin/co`. `ci1`
# (`convivy-linux`) does not install zsh — deliberately, to match GitHub's hosted
# image (platform/runbooks/standing-up-the-linux-ci-box) — so the exec there fails
# before `co` ever runs, and the doctor reports the wrapper's own shell error
# ("wrap/co: 2: exec: <path>: not found") instead of either "no crew resolves" or
# a pass. CI run 37581139332 hit exactly this on `test_a_second_run_finishes_...`.
# `test_co_rollover_real_tmux.py` diagnosed the same host gap first; this mirrors
# its `skipif`.
_NO_ZSH_REASON = (
    "no zsh on this host — the doctor's co site runs bin/co, a zsh script, through "
    "the sandbox's co_wrap, and ci1 (convivy-linux) does not install zsh (platform/"
    "runbooks/standing-up-the-linux-ci-box; see CI run 37581139332 for this exact "
    "failure, and tests/test_co_rollover_real_tmux.py for the same host gap)")


def _create_twice(box, capsys):
    """The two runs that finish a crew: the first before the Library serves its profile,
    the second after the Library's load. Only the Library's load changes between them."""
    cmd = box.agentctl.cmd_crew_create
    rc_first = cmd(_create_args(box))
    first = capsys.readouterr().out
    assert rc_first != 0
    for step in ("step 1 register — done", "step 2 directory — done",
                 "step 3 deck spec — done", "step 4 profile proposal — done",
                 "step 5 knowledge-ctx use — pending", "step 6 load profile — pending",
                 "step 7 tmux session — pending", "step 8 crew doctor — running"):
        assert step in first, first
    # Unserved, `knowledge-ctx list` names no profile, so the doctor stops at the project
    # site, reporting it UNKNOWN, which is a failing state, not a pass.
    assert "UNKNOWN" in first and "no profiles defined for project throwaway-crew" in first
    assert _session_names(box.socket) == [], "a pending crew started a session"
    assert not (box.working / ".claude" / ".active-profile").exists()

    box.library.loaded = True
    rc = cmd(_create_args(box))
    return rc, capsys.readouterr().out


@pytest.mark.skipif(shutil.which("zsh") is None, reason=_NO_ZSH_REASON)
def test_a_second_run_finishes_every_step_and_the_doctor_passes(sandbox, capsys):
    box = sandbox
    rc, out = _create_twice(box, capsys)

    assert rc == 0, out
    assert "step 1 register — done: already registered" in out
    assert "step 4 profile proposal — done: " in out and "in the knowledge checkout" in out
    assert "step 5 knowledge-ctx use — done: individuated" in out
    assert "step 6 load profile — done" in out
    assert "step 7 tmux session — done: started" in out
    assert "step 8 crew doctor — running" in out

    doctor = [line for line in out.splitlines() if line.lstrip().startswith(
        ("PRESENT", "MISSING", "UNKNOWN", "N/A"))]
    missing = [line for line in doctor if line.lstrip().startswith("MISSING")]
    assert missing == [], out
    co_lines = [line for line in doctor if CO_SITE in line]
    assert len(co_lines) == 1 and co_lines[0].lstrip().startswith("PRESENT"), out
    assert "crew=" in co_lines[0], out
    assert all(line.lstrip().startswith("PRESENT") or "N/A" in line for line in doctor), out

    # The rest of the finished crew is in place, checked on disk and in the session.
    assert (box.working / ".git").is_dir()
    assert (box.working / ".claude" / ".active-profile").read_text().strip() == f"{CREW}/daytime"
    assert any(box.working.joinpath(".claude", "agents").glob("*.md"))
    spec = json.loads((box.crews_dir / f"{CREW}.json").read_text(encoding="utf-8"))
    assert spec["id"] == CREW and spec["workingDirectory"] == str(box.working.resolve())
    sid = next(sid for sid, name in _session_names(box.socket) if name == CREW)
    assert spec["sessionId"] == sid


@pytest.mark.skipif(shutil.which("zsh") is None, reason=_NO_ZSH_REASON)
def test_a_throwaway_crew_passes_crew_doctor_with_no_other_edit(sandbox, capsys):
    """The acceptance bar of issue_01M4AB9X62112MCG8B5501CMKV: the second run's doctor
    pass, including the co site (`PRESENT  co -r resolves the crew`), with no edit
    to the crew beyond the two create runs."""
    box = sandbox
    rc, out = _create_twice(box, capsys)
    assert rc == 0, out
    assert box.agentctl.cmd_crew_doctor(argparse.Namespace(
        crew=CREW, all_crews=False, variant="daytime")) == 0



# --- refusals --------------------------------------------------------------

def test_a_refusal_at_a_middle_step_reports_the_earlier_steps_and_exits_nonzero(sandbox, capsys,
                                                                                  monkeypatch):
    box = sandbox

    def refused(rel, content, title, rationale):
        raise CC.ProposeError("the Library refused the proposal: HTTP 409 duplicate")

    monkeypatch.setattr(box.agentctl, "_propose_crew_profile", refused)
    rc = box.agentctl.cmd_crew_create(_create_args(box))
    out = capsys.readouterr().out

    assert rc == 1
    assert "step 1 register — done" in out
    assert "step 2 directory — done" in out
    assert "step 3 deck spec — done" in out
    assert "step 4 profile proposal — refused: the Library refused the proposal" in out
    assert "stopped at step 4 profile proposal" in out
    for later in ("step 5", "step 6", "step 7", "step 8"):
        assert later not in out, out
    assert _session_names(box.socket) == []


def test_a_rerun_refuses_a_registry_row_it_would_not_write(sandbox, capsys):
    box = sandbox
    registry = json.loads(box.registry.read_text(encoding="utf-8"))
    registry[CREW] = {"class": "project", "display": "Something Else", "owner": "convivy",
                      "home_dir": f"<root>/{CREW}", "dirs": [], "repos": [CREW],
                      "aliases": [], "crew": CREW, "lifecycle": "active", "products": [],
                      "kb": "central", "kb_project": CREW, "task_text_public": False}
    box.registry.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    projects._load_raw.cache_clear()

    rc = box.agentctl.cmd_crew_create(_create_args(box))
    out = capsys.readouterr().out
    assert rc == 1
    assert "step 1 register — refused" in out and "display" in out
    assert not (box.crews_dir / f"{CREW}.json").exists()


# --- step 1's own comparison: the fields the Library adds on write ---------

def _unused(*a, **kw):
    raise AssertionError("step 1 register must not reach this")


def _register_env(emit):
    return CC.Env(crews_dir=Path("/unused"), knowledge_ctx=Path("/unused"),
                 knowledge_root=Path("/unused"), library_loader=Path("/unused"),
                 host="unused-host", propose=_unused, served=_unused, doctor=_unused,
                 emit=emit)


_BUILT_ROW = {"class": "project", "display": "Sample Crew", "owner": "convivy",
             "home_dir": "<root>/sample-crew", "dirs": [], "repos": ["sample-crew"],
             "aliases": [], "crew": "sample-crew", "lifecycle": "active", "products": [],
             "kb": "central", "kb_project": "sample-crew", "task_text_public": False,
             "ci_workflows": {"sample-crew": ["ci.yml"]}}


def test_a_stored_row_differing_only_in_fields_the_library_adds_is_already_registered(monkeypatch):
    """§6.8: a 201 is "the row, as stored, with `slug`, `updated_at` and `updated_by`
    added". A rerun's built row never carries those three, so a stored row that
    differs only in them is the same registration, not a different one."""
    stored = {**_BUILT_ROW, "slug": "sample-crew", "updated_at": "2026-10-26T00:00:00Z",
             "updated_by": "person_01SAMPLEWRITER0000000000"}
    monkeypatch.setattr(projects, "get", lambda slug: stored)
    out = []
    result = CC._step_register(_register_env(out.append), "sample-crew", object(), _BUILT_ROW)
    assert result is None
    assert any("already registered" in line for line in out), out


def test_a_stored_row_differing_in_another_field_still_refuses(monkeypatch):
    stored = {**_BUILT_ROW, "display": "A Different Display Name",
             "updated_at": "2026-10-26T00:00:00Z", "updated_by": "person_01SAMPLEWRITER0000000000"}
    monkeypatch.setattr(projects, "get", lambda slug: stored)
    out = []
    result = CC._step_register(_register_env(out.append), "sample-crew", object(), _BUILT_ROW)
    assert result == 1
    refusal = next(line for line in out if "refused" in line)
    assert "display" in refusal and "updated_at" not in refusal and "updated_by" not in refusal


def test_a_working_directory_outside_the_crews_root_is_refused_before_any_write(sandbox, capsys,
                                                                                tmp_path):
    box = sandbox
    outside = tmp_path / "elsewhere" / CREW
    rc = box.agentctl.cmd_crew_create(_create_args(box, dir=str(outside)))
    out = capsys.readouterr().out
    assert rc == 1
    assert "step 0 preflight — refused" in out
    assert "register" not in out
    assert not outside.exists()
    assert not box.crews_dir.joinpath(f"{CREW}.json").exists()


# --- the tmux and proposal seams -------------------------------------------

def test_tmux_is_run_on_the_socket_with_TMUX_stripped(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(CC.subprocess, "run", fake_run)
    CC._tmux(["list-sessions"], "private-sock")
    assert seen["cmd"] == ["tmux", "-L", "private-sock", "list-sessions"]
    assert "TMUX" not in seen["env"] and "TMUX_PANE" not in seen["env"]


def test_the_default_profile_gives_the_crew_its_review_agents_and_scout():
    yaml = pytest.importorskip("yaml")
    body = yaml.safe_load(CC.default_profile("cart"))
    assert body["profile"] == "cart/daytime"
    audience = body["include"][0]["audience_contains"]
    assert audience == ["reviewer", "security-reviewer", "scout"]
    assert body["include"][0]["kind"] == "agent"


def test_the_proposal_is_recorded_so_a_rerun_does_not_propose_twice(sandbox, capsys, monkeypatch):
    box = sandbox
    calls = []

    def recording(rel, content, title, rationale):
        calls.append(rel)
        return "https://github.com/convivy/knowledge/pull/7"

    monkeypatch.setattr(box.agentctl, "_propose_crew_profile", recording)
    box.agentctl.cmd_crew_create(_create_args(box))
    out_first = capsys.readouterr().out
    assert calls == [CC.profile_path(CREW)]
    assert "step 4 profile proposal — done" in out_first

    # The proposal has not merged, so the knowledge checkout has no profile yet.
    box.agentctl.cmd_crew_create(_create_args(box))
    out_second = capsys.readouterr().out
    assert calls == [CC.profile_path(CREW)], "a re-run proposed the profile again"
    assert "was proposed as https://github.com/convivy/knowledge/pull/7" in out_second
