#!/bin/bash
# scripts/ci/bwrap-userns-diagnose.sh — agent-platform PR #1111 (CQ·637).
#
# Runs as a step of ci.yml's `python` job, on the `convivy-linux` self-hosted
# runner (a garm-managed VM; OS release, container-or-VM status and ambient
# root are all unconfirmed as of this writing), right after "install
# bubblewrap" and before `pip install -r requirements-dev.txt` / pytest.
# tests/test_run_sandbox.py's three real-bwrap tests (requires_bwrap, under
# AGENTCTL_REQUIRE_BWRAP=1) failed here with "bwrap: setting up uid map:
# Permission denied" — bwrap ran but could not create the user namespace it
# needs, after a prior round's network-namespace failure on this same runner
# was already fixed by dropping --unshare-net/--unshare-all (see
# tests/test_run_sandbox.py's own _BWRAP_UNSHARE_FLAGS comment).
#
# Two independent, known causes of exactly that bwrap failure:
#   1. Ubuntu 24.04 restricts unprivileged user namespaces by default via the
#      AppArmor LSM (sysctl kernel.apparmor_restrict_unprivileged_userns=1),
#      which refuses an unprivileged clone(CLONE_NEWUSER) outright.
#   2. A container that refuses a nested user namespace regardless of that
#      sysctl (no CAP_SYS_ADMIN in the outer namespace, or a seccomp profile
#      that blocks the clone flag).
# This step cannot tell which applies without seeing the runner, so it tries
# the remediation for (1) first, falls back to bwrap's own setuid-root mode
# (its supported path for a host with no usable unprivileged user namespaces
# at all, which also covers (2)), and reports every step either way.
#
# NEVER FAILS THIS JOB. Every remediation here is best-effort and every probe
# result is printed, not asserted; `pytest -rs` under AGENTCTL_REQUIRE_BWRAP=1
# (the next step) stays the one gate; an unusable bwrap still fails the real
# tests with bwrap's own stderr. This runner is a garm-managed, ephemeral VM
# (destroyed after the job), so nothing here needs to be undone.
set -u

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

echo "== bwrap / user-namespace diagnosis (CQ·637) =="

echo "-- id / kernel / OS --"
id
uname -a
if [ -r /etc/os-release ]; then
    cat /etc/os-release
else
    echo "/etc/os-release: not present"
fi
if command -v systemd-detect-virt >/dev/null 2>&1; then
    echo "systemd-detect-virt: $(systemd-detect-virt 2>&1)"
else
    echo "systemd-detect-virt: not present"
fi
echo

echo "-- relevant sysctls (printed only where the kernel exposes them) --"
for key in kernel.apparmor_restrict_unprivileged_userns \
           kernel.unprivileged_userns_clone \
           user.max_user_namespaces; do
    val=$(sysctl -n "$key" 2>/dev/null)
    if [ -n "$val" ]; then
        echo "$key = $val"
    else
        echo "$key: not present on this kernel"
    fi
done
echo

bwrap_bin=$(command -v bwrap)
if [ -z "$bwrap_bin" ]; then
    echo "bwrap: not on PATH (the 'install bubblewrap' step above should have put it there) — nothing further to probe"
    exit 0
fi
echo "bwrap resolved to: $bwrap_bin"
echo

# Same namespace flags tests/test_run_sandbox.py's own bwrap invocations use
# (_BWRAP_UNSHARE_FLAGS) when bwrap is not setuid-root — see that module's
# comment for why --unshare-net/--unshare-all are never among them.
unshare_flags_unprivileged=(--unshare-user --unshare-ipc --unshare-pid --unshare-uts)
# Dropping --unshare-user once bwrap is setuid-root: that mode unshares the
# user namespace itself before dropping privileges and refuses the option
# outright ("Specifying --unshare-user is not allowed for setuid-root
# bwrap"), since it is already implied. test_run_sandbox.py's own
# _bwrap_unshare_flags makes the same substitution, keyed off the same
# setuid bit, so its real tests stay in sync with whatever this step leaves
# bwrap in.
unshare_flags_setuid=(--unshare-ipc --unshare-pid --unshare-uts)

probe() {
    echo "+ $bwrap_bin $* --die-with-parent --ro-bind / / --dev /dev --proc /proc true"
    "$bwrap_bin" "$@" --die-with-parent --ro-bind / / --dev /dev --proc /proc true
    rc=$?
    echo "(exit $rc)"
    return "$rc"
}

echo "-- probe 1: unprivileged user namespace --"
if probe "${unshare_flags_unprivileged[@]}"; then
    echo "bwrap probe: PASS (unprivileged user namespaces work; no remediation needed)"
    exit 0
fi
echo "bwrap probe: FAILED"
echo

restrict=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)
if [ "$restrict" = "1" ]; then
    # Cause (1) above: Ubuntu 24.04's AppArmor LSM refuses an unprivileged
    # clone(CLONE_NEWUSER) while this sysctl reads 1. Flipping it to 0 is the
    # documented remediation (AppArmor's own userns-restriction release
    # notes); this runner is ephemeral, so there is nothing to restore after
    # the job ends.
    echo "-- kernel.apparmor_restrict_unprivileged_userns=1: clearing it and re-probing --"
    if as_root sysctl -w kernel.apparmor_restrict_unprivileged_userns=0; then
        echo "-- probe 2: unprivileged user namespace, after clearing the AppArmor restriction --"
        if probe "${unshare_flags_unprivileged[@]}"; then
            echo "bwrap probe: PASS (the AppArmor restriction was the cause)"
            exit 0
        fi
        echo "bwrap probe: still FAILED"
    else
        echo "could not set kernel.apparmor_restrict_unprivileged_userns (sysctl -w failed)"
    fi
else
    echo "kernel.apparmor_restrict_unprivileged_userns is not 1 (reads '${restrict:-<absent>}'); skipping that remediation"
fi
echo

# Cause (2) above, or an AppArmor fix that did not take: bwrap's own setuid-
# root mode does not need unprivileged user namespaces at all — it uses its
# root privilege to create the user namespace itself before dropping it, the
# supported path for exactly this host shape. This runner is ephemeral, so
# the setuid bit does not outlive the job.
echo "-- making $bwrap_bin setuid-root and re-probing --"
if as_root chmod u+s "$bwrap_bin"; then
    ls -l "$bwrap_bin"
    echo "-- probe 3: setuid-root bwrap (no --unshare-user: setuid bwrap refuses it as already implied) --"
    if probe "${unshare_flags_setuid[@]}"; then
        echo "bwrap probe: PASS (setuid-root bwrap works)"
        exit 0
    fi
    echo "bwrap probe: still FAILED after setuid-root"
else
    echo "could not make $bwrap_bin setuid-root (chmod failed)"
fi

echo
echo "bwrap is still unusable on this runner after every remediation this step knows. Leaving it as-is; the pytest step next (AGENTCTL_REQUIRE_BWRAP=1) is the real gate and will fail with bwrap's own stderr."
exit 0
