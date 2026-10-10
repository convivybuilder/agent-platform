# Headless-machine hygiene

The mini has no monitor and is reached only over Tailscale. Two things in here
exist because that combination turns ordinary desktop behaviour into an outage.

## `install-headless-browser`

Registers a URL handler that logs the URL instead of opening it.

Claude Code's OAuth flow calls its `openBrowser` path. With no `http`/`https`
handler registered, macOS falls back to Safari. On 2026-07-17 that launched
Safari on a machine with no screen; it never exited, and by 2026-08-13 it held
**29 processes and 5.7GB of compressed memory**. Swap hit 6.1GB of 7.2GB and
macOS began SIGKILLing processes — including the tmux session shells that are
the only way in. It read as "tmux keeps dropping".

Auth still works after this: the link goes to
`~/.local/state/headless-browser/urls.log` and you open it on a machine with a
screen.

## `../bin/tailscale-watchdog` + `../launchd/com.convivy.tailscale-watchdog.plist`

Restarts Tailscale if it stops, and kills Safari if it appears.

Tailscale is the only route to this machine: it sits on `172.16.0.59` and, until
2026-08-13, no peer advertised that subnet. A reboot with a pending Tailscale
system-extension uninstall — which is the state the machine was in — could have
come back with no way to reach it.

The watchdog never changes Tailscale's configuration. Recovery is "start it
again", never `--reset` or any flag that could re-point the node or drop its
auth: a watchdog that repairs connectivity by reconfiguring the network can lock
you out faster than the fault it treats.

## What these do not cover

Neither helps if the Tailscale **system extension** fails to activate at boot,
because then there is no backend to talk to. The cover for that is a subnet route
on the gateway (`gl-be6500`, `172.16.0.0/24`), so the machine stays reachable at
its LAN address even when its own Tailscale never comes up.
