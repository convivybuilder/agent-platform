# Deck's session→theme mapping

Deck's pane for a crew's session should look like that crew's iTerm session.
Deck ships a palette, not a mapping: its ten themes are named for colors
(`ember`, `gold`, `azure`, …) rather than for crews, because a table keyed to
Convivy's session names would be unreadable and uneditable for anyone else
running Deck — they would install it, match nothing, and fall off the end of a
table of ours.

## Where the mapping lives

This file, `theme.json`, carries only settings that are not per-crew —
`appearance` today. The per-crew mapping (which session takes which shipped
theme, and each session's rail label) is rendered from the registry's
`co_deck_theme` and `co_label` fields at install time, on the crew machine
(`bin/render-co-style`, called from `install.sh` phase 5, CQ-590), into a
generated `sessions`/`railNames` pair outside any repository — alongside the
`iterm/co-*.json` profiles that moved there the same way. Neither this file
nor the registry names a crew session directly in a way the Builder release
name scan would flag; `co_deck_theme` and `co_label` are ordinary registry
fields set per unit.

A unit with neither field set gets no entry in either map, the same as an
operator who hasn't picked one yet. Adjust a session's theme by right-clicking
its rail row in Deck, which writes to the installed file directly, independent
of the registry.

A session with no entry at all is not broken — Deck hashes the session name
into its catalog, so it gets a stable, distinct color with no configuration.

## Install behavior

tools/install-crew-ssh-config.sh, run on the laptop, merges the rendered seed
into `~/.config/convivy/deck/theme.json` **key by key, on every run**, through
`lib/agent-platform/deck_config.py`'s `merge_file`: a key already present in
that file is left untouched, and a key missing from it is filled in from this
seed, whether the file is new or already exists.

This differs from the iTerm profiles, which are overwritten every run, and the
difference is deliberate: Deck's theme picker writes to this same path, so
replacing the whole file would silently discard every choice made in the app.
Merging per key rather than per file is also why a present file is no longer
skipped outright: Deck writes this file on any preference change, so a file
could already exist with no `sessions` block at all, and skipping on mere
existence meant that file never received this mapping.

To reset a key to what ships here, remove that key (or the whole file) from
`~/.config/convivy/deck/theme.json` and re-run the installer; an absent key is
always filled.
