"""Whether this process can start a real tmux server, and why not if it cannot.

A dispatched run's Bash is sandboxed (lib/agent-platform/run_sandbox.py). The sandbox
refuses every Unix socket it has not been told to allow, and Option A (Slate a-ow0q4o)
does not open tmux to it: no `allowAllUnixSockets`, no `excludedCommands` for tmux. So
a real tmux server cannot start inside a run, and the tests that need one skip there.
CI runs them on its Linux runner, where no sandbox applies.

The signal is a probe, not an environment variable: a run could unset a variable to
make these tests run, but it cannot make the socket creation succeed. Only tmux's own
"Operation not permitted" refusal counts as the sandbox. A missing tmux, or any other
failure, is not a sandbox, so those tests run and fail as they always have.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile

_REFUSED = "Operation not permitted"


@functools.lru_cache(maxsize=1)
def sandbox_refusal() -> str | None:
    """A reason to skip, or None when a real tmux server can start here."""
    if shutil.which("tmux") is None:
        return None
    probe_dir = tempfile.mkdtemp(prefix="tmux-probe-")
    socket = os.path.join(probe_dir, "probe")
    try:
        started = subprocess.run(["tmux", "-S", socket, "start-server"],
                                 capture_output=True, text=True, timeout=30)
        subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True, timeout=30)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    if started.returncode != 0 and _REFUSED in started.stderr:
        return ("a dispatched run's sandbox refuses tmux sockets (tmux: "
                f"'{_REFUSED}'); Option A does not open tmux to the sandbox, so this runs in CI")
    return None
