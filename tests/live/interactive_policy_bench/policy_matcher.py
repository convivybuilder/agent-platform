"""The allow-list matcher shared by hook_allowlist.py (the live decision) and
bench.py (the offline `check-hook` self-test) — one copy, so the bench can
never believe a stricter or looser rule than the hook actually runs.

ADR-0051 measured a DENYLIST hook (`tools/block-pr-merge.sh`'s predecessor)
against nine bypass shapes and five passed, `time gh pr merge 419` and
`false || git push origin main` among them: matching a command line for a
denied token loses to a caller-composed input, because a leading wrapper word
or a chained command steps around a plain substring match.

An ALLOW-list built on `re.fullmatch` doesn't have that failure mode. Requiring
the WHOLE (normalized) command line to fullmatch one exact granted pattern
means anything chained onto a granted prefix — `&&`, `;`, a bare newline, a
subshell, a `$(...)`/backtick substitution — introduces a character the
pattern's argument tail excludes, so the match fails over the ENTIRE string
and the command is denied, with no separate step needed to detect "compound"
or "subshell" as its own case. Denial is the default for anything the pattern
can't fully consume, which is the posture ADR-0048 asks for in the outage
case, generalized here to "unparseable/ambiguous" rather than only
"unreachable".
"""
from __future__ import annotations

import re

# Excluded from every granted command's argument tail. `;&|` are compound-command
# separators (including the doubled `&&`/`||`, which fullmatch already rejects
# once the single-char class can't consume the second `&`/`|`); `()` open a
# subshell or a `$(...)` substitution; `` ` `` opens the other substitution form;
# `<>` are redirection, which can exfiltrate or overwrite past what an allowed
# read-only command implies. A raw newline is a command separator too, so it is
# normalized to `;` before matching (see `normalize`) rather than added here —
# leaving it here as well would just be the same exclusion twice.
_UNSAFE_TAIL_CHARS = r";&|<>()`$"
_SAFE_TAIL = rf"[^{_UNSAFE_TAIL_CHARS}]*"


def normalize(command: str) -> str:
    """Tabs fold to spaces; a bare newline folds to `;`, the real separator it
    is — collapsing it to a space instead would erase the boundary between two
    commands and let the second one hide inside what looks like one long
    argument list (the same reasoning `tools/block-pr-merge.sh` documents for
    its own normalization)."""
    return command.replace("\t", " ").replace("\n", ";")


def build_allow_regexes(prefixes: list[str]) -> list[re.Pattern]:
    """One anchored pattern per granted command prefix (e.g. "git rev-parse",
    extracted from a `Bash(git rev-parse:*)` roster grant). Anchored at both
    ends; `fullmatch` in `decide()` makes the trailing `$` redundant but it
    stays for a reader who greps the pattern on its own."""
    regexes = []
    for prefix in prefixes:
        escaped = re.escape(prefix)
        regexes.append(re.compile(rf"^{escaped}(\s{_SAFE_TAIL})?$"))
    return regexes


def decide(command: str, allow_regexes: list[re.Pattern]) -> tuple[bool, str]:
    """True + the matched prefix's pattern, or False + a reason, never a
    partial credit — an unrecognized shape is denied by default, not asked
    about, which is the property slice 1's bench is trying the hook for."""
    if not command:
        return False, "empty command"
    normalized = normalize(command)
    for rx in allow_regexes:
        if rx.fullmatch(normalized):
            return True, f"matched allow-listed pattern {rx.pattern!r}"
    return False, f"no allow-listed pattern fullmatches {normalized!r}"


_FRONTMATTER = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)


def extract_allow_prefixes(agent_markdown: str) -> list[str]:
    """Pull every `Bash(<prefix>:*)` roster grant out of an agent file's
    YAML FRONTMATTER only (between the two leading `---` lines), in file
    order, de-duplicated. Reads the real individuated file rather than a
    hand-typed copy of its two grants, so the bench's allow-list can't drift
    from what the product-manager was actually given.

    Confined to the frontmatter deliberately: the body prose of a real agent
    file routinely quotes its own grants back for documentation (e.g.
    product-manager.md's "Gating" section literally writes `Bash(git
    rev-parse:*)`), and a body that ever writes the LITERAL string
    `Bash(...)` as a documentation placeholder — as this bench's own test
    fixture does — would otherwise mint a spurious allow-listed prefix
    `"..."` that grants nothing real but silently widens what `decide()`
    reports as granted."""
    match = _FRONTMATTER.match(agent_markdown)
    scope = match.group(1) if match else agent_markdown
    prefixes: list[str] = []
    for m in re.finditer(r"Bash\(([^)]+)\)", scope):
        raw = m.group(1).strip()
        prefix = raw[:-2] if raw.endswith(":*") else raw
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes
