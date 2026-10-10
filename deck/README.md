# Deck's session→theme mapping

`theme.json` maps Convivy's session names onto themes that ship with Deck, so
a Deck pane looks like the iTerm session for the same project.

## Why it lives here and not in Deck

Deck ships a palette, not a mapping. Its ten themes are named for colors
(`ember`, `gold`, `azure`, …) rather than for products, because a table keyed
to Convivy's session names would be unreadable and uneditable for anyone else
running Deck — they would install it, match nothing, and fall off the end of a
table of ours.

So Convivy's mapping ships with Convivy's platform, next to the
`iterm/co-*.json` profiles it mirrors and installed by the same script. Deck
itself knows nothing about `vivy` or `sous-chef`.

## How themes are chosen

Each entry is the shipped theme whose accent hue is nearest that session's
iTerm cursor color, so the two surfaces agree.

Thirteen sessions into ten themes means five themes are shared by two sessions
each:

| Theme | Sessions |
|---|---|
| `amber` | `nyh-park-villa`, `sous-chef` |
| `azure` | `svc`, `vivy` |
| `gold` | `convivy-commercial`, `deck` |
| `indigo` | `ask`, `selector` |
| `teal` | `nyh-etc`, `write-better` |

Adjust any of them by right-clicking the rail row in Deck, which writes to the
same file.

A session absent from this file is not broken — Deck hashes the session name
into its catalog, so it gets a stable, distinct color with no configuration.

## Install behavior

The operator's own ssh-alias installer copies this to
`~/.config/convivy/deck/theme.json` **only if that file does not already
exist.**

This differs from the iTerm profiles, which are overwritten every run, and the
difference is deliberate: Deck's theme picker writes to this same path. Copying
over it would silently discard every choice made in the app. Seeding an absent
file is a starting point; overwriting a present one destroys user state.

To reset to what ships here, delete `~/.config/convivy/deck/theme.json` and
re-run the installer.
