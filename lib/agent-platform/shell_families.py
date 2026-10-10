"""Reduce a shell command string to the program families it can run.

**This is an allow-list, and that shape is the point, not an oversight.**
`platform/decisions/0051` found that matching a command line to decide whether
to DENY it is a denylist over input the caller composes: nine shapes were fed
to the old merge-block hook and five passed, each by a wrapper word or a `||`
the hook's patterns never enumerated. A denylist loses that game structurally
— the author must anticipate every spelling; the caller needs only one that
was missed.

This module survives that ruling by inverting what "unknown" means. It does
not try to enumerate every dangerous shape and deny those; it tries to reduce
the command to the closed set of programs it can confidently prove will run,
and reports whether that reduction fully succeeded. A caller building a policy
on top of this treats an incomplete reduction — `complete=False` — as NOT
GRANTED, the same as any family absent from an explicit allow-list. Getting a
construct wrong here means failing to prove innocence, not failing to prove
guilt, which is the direction a fail-closed control needs to fail in.

`reduce_to_leaves()` returns `(leaves, complete)`; `reduce_to_families()` is a
thin projection of the same reduction onto program names, sharing the one
parser rather than running a second one:

  - `leaves` is an ordered tuple of `Leaf` — every command the shell grammar
    itself launches, in the order bash would run them, after unwrapping
    wrapper words (`env`, `sudo`, `time`, …) and recursing through `eval`,
    `bash -c`, a subshell, a function body and a command substitution. Each
    `Leaf` carries four fields, populated separately rather than folded
    together: `argv` (the words the program itself receives — flags and all,
    with every assignment and redirection removed, from any position),
    `assignments` (every `NAME=VALUE` that reaches the leaf's own process
    environment, leading or via an `env` wrapper, in exec order, propagated
    into every leaf of a shell recursion the assignment's process spawns),
    `env_modifiers` (an `env` flag that clears, unsets or redirects the
    environment rather than adding to it — `-i`, `-u NAME`, `--chdir DIR` —
    with a flag this module cannot model, such as `-S`, failing the whole
    leaf closed instead of being guessed at), and `redirections` (every
    `operator, target` pair the shell sets up around the leaf, any position,
    including a here-string and an fd dup). See `Leaf`'s own docstring below
    for the full field-by-field contract.
  - `families` (`reduce_to_families()`) is `{leaf.family for leaf in leaves}`
    — every leaf's program name, deduplicated, everything else discarded.
    This is the shape every caller before round 4 already uses.
  - `complete` is `True` only if every command the shell grammar launches was
    reduced to a `Leaf` with its full `argv`, `assignments`, `env_modifiers`
    and `redirections`. `False` means some construct — an unbalanced quote, a
    construct this module does not model, or a variable/substitution
    standing where a program name goes — could not be resolved, and
    `leaves`/`families` may be partial even when non-empty.

**`complete=True` is a claim about parsing, not about safety: it says this
module found every leaf and captured its `argv`, `assignments`,
`env_modifiers` and `redirections` in full, and nothing more.** It makes no
claim about what a leaf's program DOES with that `argv`, nor about whether
its `assignments`/`env_modifiers`/`redirections` are safe to grant — only
that they were found. An open-ended class of ordinary programs define
argument grammar that itself launches a further command, and no enumeration
of program names closes this class: `find / -exec rm -rf {} \\;` (find's
`-exec`), `git -c alias.x='!rm -rf /' x` and `git -c core.sshCommand=...
fetch` (git's `-c`-configured alias/pager/ssh hooks), `tar --to-command=...`
and `--checkpoint-action=exec=...` (tar's own command hooks), `rsync -e
'...'` (rsync's remote-shell command), `ssh -o ProxyCommand='...'` (run
locally, before any connection), `vim -c '!...'` (an ex shell escape), `gdb
-ex 'shell ...'`, `sed '1e ...'` (GNU sed's `e` command) and `npx` / `npm
run` / `make` (each runs code read from a file this module never opens).
None of these are shell recursion, a function definition, or any other
construct this module models specially — each reduces to an ordinary
`Leaf(prog, argv, ...)` with `complete=True`, exactly like `git status`.

**A caller granting a family by bare program name, or a roster pattern like
`Bash(git:*)`, is granting that program's entire argument grammar with no
shell boundary around it.** The governance epic's resolver (slice 2), built
on this module's `leaves`, carries three obligations, each closing a
distinct round-4/round-5 bypass class:

1. It must `fullmatch` its grant pattern against each leaf's full `argv` —
   e.g. `Bash(git rev-parse:*)` checked word-for-word against `argv`, not
   merely against `family` — and deny outright whenever `complete` is
   `False`.
2. It must deny any leaf carrying a non-empty `assignments` or
   `env_modifiers` unless the grant explicitly names those exact
   assignment/modifier targets — a bare `Bash(git status:*)` grant says
   nothing about what environment the leaf runs with, so an unreviewed
   `LD_PRELOAD`, `BASH_ENV` or `GIT_CONFIG_*` riding in on `assignments`
   must not pass silently.
3. It must treat each of a leaf's `redirections` as an effect to judge, not
   as inert trailing argv: a read-only grant does not imply a write
   redirection is safe, and a grant covering `argv` says nothing about
   where the leaf's stdout, stderr or stdin end up pointed.

This module parses text. It never executes anything, and it calls no
subprocess.

Handled: the separators `;`, `&&`, `||`, `|`, newline and a trailing `&`
(`|&`, bash's stdout+stderr pipe shorthand for `2>&1 |`, is not specially
recognized — round 7 confirmed by test that it splits as a bare `|` segment
separator followed by a bare `&` that matches neither fd-dup adjacency check
in `_split_segments`, leaving an empty segment between the two real ones;
`_reduce` skips the empty segment, so both commands the pipe actually runs
still resolve to their own leaves, same as `;` would produce, even though
this module never models the fd-merging effect itself);
grouping via `( … )`, `$( … )` and backticks (recursed into, since a command
substitution's inner command really does run); shell recursion through
`bash -c` and every other shell name `_SHELL_RECURSE_PATTERNS` covers
(`sh`, `zsh`, `dash`, `ash`, `ksh`, `mksh`, `yash`, `fish`, `busybox`, and
version-suffixed `bash`/`zsh`) with a quoted inner command; the wrapper words
`time`, `env`, `command`, `exec`, `nohup`, `nice`, `xargs`, `sudo` and
`builtin`, which do not themselves run anything and are unwrapped to find the
real command; `eval`, whose argument is re-parsed as a fresh command line;
the interpreter one-liners `python -c`, `node -e`, `deno eval`, `bun -e`,
`perl -e`, `ruby -e`, `php -r`, `lua -e`, `Rscript -e`, `julia -e`,
`expect -c` and `osascript -e` — including versioned/unversioned/alternate
spellings of each interpreter name (`python3.11`, `nodejs`, `pypy3`,
`ruby3.2`, `perl5.34`, …) — each reported as
its own `<interpreter>:inline` family rather than folded into the plain
interpreter name, since a caller's policy has to decide about inline code
separately from the interpreter binary itself, and only when the inline flag
is the first token after the interpreter name (real argv parsing stops
reading flags at the first non-option argument, so a script file followed by
a same-named flag in its own arguments is not the interpreter's flag); a
bash/POSIX function definition (`name () compound-command` or
`function name [()] compound-command`), which shadows any external command of
the same name for the rest of the string — once one appears, a later bare
invocation of that name no longer means the program a caller granted, so the
whole string is reported unreducible (`complete=False`), while the body is
still recursed into for whatever families it names; every bash reserved word
(`if`/`then`/`elif`/`else`/`fi`, `for`/`while`/`until`/`do`/`done`,
`case`/`esac`/`in`, `select`, `coproc`, plus `{`/`}`/`!`/`[[`/`]]`, which fail
closed by construction rather than by an explicit check), each of which
introduces a compound-command block this module's flat segment splitter does
not parse and so is never trusted as an ordinary program name; the builtins
`source`/`.`, `trap`, `enable`, `hash`, `alias`, `unalias`, `declare`,
`typeset`, `set`, `shopt`, `fc`, `mapfile` and `readarray`, none of which
this module attempts to model, so all fail closed unconditionally;
`awk`/`gawk`/`mawk`/`nawk`, whose
program argument has no distinguishing flag the way `-c`/`-e` mark inline
code elsewhere, so every spelling fails closed unconditionally; assignments
that reach the leaf's own environment, whether leading (`FOO=1 git status`)
or supplied through an `env` wrapper (`env FOO=1 git status`), captured on
`Leaf.assignments` rather than folded into `argv` or silently discarded, and
propagated into every leaf a shell recursion spawns under that assignment
(`env BASH_ENV=/tmp/x bash -c "git status"` — `BASH_ENV` is sourced before
the `-c` body runs, so it reaches `git`'s own leaf, not only `bash`'s); an
`env` flag that changes the launched process's environment wholesale rather
than adding to it (`-i`/`--ignore-environment`, `-u NAME`/`--unset NAME`,
`-C DIR`/`--chdir DIR`), captured on `Leaf.env_modifiers`, with `-S`
deliberately left unmodeled (its own argument-splitting grammar is not
parsed here) so it fails closed rather than being guessed at; and a
redirection at any position in the command — leading, trailing or between
arguments (`git status > /tmp/x`, `git status 2>&1`, a here-string) —
captured as an `(operator, target)` pair on `Leaf.redirections` and removed
from `argv`, since it is the shell, not the program, that interprets it. A
heredoc (`<<`, `<<-`) is NOT one of these, despite sharing `<<<`'s (a
here-string's) prefix: its body is a multi-line span of shell DATA this
module does not parse, not a single target it could record on
`redirections`, so `<<`/`<<-` — round 10 — fail the whole reduction closed
unconditionally instead, and the body's lines are swallowed during
segmentation rather than tokenized as further commands.

**Round 5** closed two classes both round-4 reviews found in the same shape:
a leaf's `argv` used to carry a trailing/leading redirection as if it were an
ordinary argument (`git status > ~/.ssh/authorized_keys` reduced to
`argv=('git','status','>','$HOME/.ssh/authorized_keys')`, which a
`Bash(git status:*)` prefix grant matches while the shell performs a write),
and an assignment that reaches the exec'd program's environment was dropped
entirely rather than surfaced (`env LD_PRELOAD=/tmp/evil.so git status`,
`FOO=1 git status`, `env BASH_ENV=/tmp/evil.sh bash -c "git status"`, `env
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='!id' git x`
all reduced to a bare, assignment-free `git` leaf). Every site that consumes
a token now routes assignments and redirections to `Leaf.assignments` /
`Leaf.env_modifiers` / `Leaf.redirections` instead of discarding them:

- `_resolve_leaf` extracts every redirection in the whole token list, at
  whatever position it occurs, before any other parsing runs
  (`_extract_redirections`) — this is the one site redirections are ever
  removed from a token stream.
- `_resolve_leaf_inner`'s leading-token loop collects a run of leading
  `NAME=VALUE` tokens into `assignments` instead of dropping them, then
  attaches them to whatever leaf(s) the rest of the line resolves to.
- `_resolve_wrapper`'s `env` branch collects `env`'s own leading
  `NAME=VALUE` tokens into `assignments`, and its recognized
  environment-clearing flags (`-i`, `-u`/`--unset`, `-C`/`--chdir`,
  `--ignore-environment`) into `env_modifiers`, attaching both to the
  wrapped command's leaf(s) — `sudo`, `xargs`, `nohup`, `nice`, `time`,
  `command` and `exec` carry no assignment/environment grammar of their own,
  so nothing is captured at those wrapper sites; a bare `NAME=VALUE` inside
  their `rest` falls through to the wrapped command's own leading-token loop
  above, the same site that already handles it.
- `_resolve_shell_c` recurses the `-c` body through `_reduce`, which runs the
  same leading-token and `env`-wrapper logic on the body's own tokens — an
  assignment or redirection made BEFORE the shell recursion (leading, or via
  `env`) is attached by the caller that unwrapped down to
  `_resolve_shell_c`, so it reaches every leaf the body reduces to, matching
  real environment inheritance.
- `_resolve_eval` re-joins and re-feeds its argument through `_reduce`, the
  same pipeline as any other segment, so assignments/redirections inside the
  eval'd text are captured there; an assignment preceding `eval` itself is
  attached by `_resolve_leaf_inner`'s own leading-token loop.
- `export` and `readonly` (with or without `-x`) join `UNMODELED_BUILTINS`:
  `export FOO=bar; git status` used to resolve both segments individually to
  `complete=True` with `families={'export','git'}`, hiding that `FOO`
  reaches `git`'s environment across the `;` — the same channel
  `set -a`/`declare -x`/`typeset -x` already failed closed on before this
  round. Every one of these now forces the WHOLE reduction `complete=False`,
  since `_reduce`'s segment loop ANDs `complete` across every segment.

Anything this module does not model with confidence — an unbalanced quote, a
process substitution (`<(...)`/`>(...)`), a bare variable in program position,
an unrecognized flag on a wrapper word — makes the surrounding leaf
unreducible rather than guessed at.

**Round 6** closed the redirection class round 5 left open: a redirection
that performs a real effect but attaches to no leaf used to be dropped
silently, reporting `complete=True` as if every effect had been found and
captured. `_resolve_leaf` now checks this directly rather than trusting
`_resolve_leaf_inner`'s leaf count:

- A bare redirection with no command at all (`> ~/.ssh/authorized_keys`,
  `>> f`, `< f`, `2> f`, `&> f`, a here-string alone) still opens, truncates,
  appends to or reads its target — the shell performs this before it even
  looks for a command to run. It used to reduce to `leaves=(), complete=True`
  (the same shape as an inert `FOO=1` assignment, which really does launch
  nothing). It now fails closed: `leaves=(), complete=False`.
- An assignment followed by nothing but a redirect (`FOO=1 > f`) hits the
  same gap — `_resolve_leaf_inner`'s assignment-only path correctly reports
  no leaf, but `_resolve_leaf` used to read that as "nothing happened" rather
  than "something happened that isn't a leaf." Same fix, same fail-closed
  result.
- `exec 3>file` and other `exec`-with-only-redirections forms, which open a
  descriptor for every LATER command in the same script rather than one this
  module can attach it to, already forced `complete=False` before this round
  (`_resolve_wrapper`'s own `exec`-with-empty-`rest` fallthrough), and still
  do — confirmed by test, not changed.
- A redirection whose TARGET is an unresolved command/process-substitution
  placeholder (`git status > $(echo /tmp/x)`) used to attach the raw
  placeholder to the leaf's `redirections` and still report `complete=True`,
  exactly the guess-at-what-a-substitution-resolves-to this module refuses to
  make in program position. `_resolve_leaf` now fails the whole leaf closed
  whenever a redirection's target carries the substitution placeholder,
  mirroring `_resolve_leaf_inner`'s existing program-position check.
- A trailing redirection on a compound command (`{ …; } > f`, `( … ) > f`,
  `while …; done > f`) and a redirection following a reserved word (`then
  > f`) were already fail-closed before this round, each for a reason
  specific to that shape rather than the general fix above: a subshell with
  trailing text after its closing paren is rejected as malformed by
  `_try_subshell` before any redirection logic runs; `{`/`}` and a bare
  reserved word both fail `_PROGRAM_TOKEN_RE` or dispatch through
  `RESERVED_WORDS`, which forces `complete=False` unconditionally. Confirmed
  by test, not changed.
- A redirection inside a `bash -c` body or a subshell was already attached to
  the inner leaf correctly (`_resolve_shell_c` and `_try_subshell` both
  recurse the inner text through the same `_reduce`/`_resolve_leaf` pipeline
  that handles a top-level redirection). Confirmed by test, not changed.

**Round 7** closed the class both round-6 reviews found underneath the
redirection fix, from the opposite direction: `_REDIR_OP_RE` is an
ENUMERATION of the operator spellings this module knows, and round 6 (like
every round before it) still treated anything the enumeration missed as
ordinary argv text rather than as an unrecognized shell metacharacter. Two
concrete misses reached `complete=True`: a bash "variable redirect", which
names the file descriptor instead of numbering it (`git status
{fd}>~/.ssh/authorized_keys` — real bash since 4.1, no option needed — left
the whole `{fd}>...` token sitting unexamined in `argv`, a live bypass of a
`Bash(git status:*)` grant); and `<>` (open for both read and write), which
the old regex partially matched as a bare `<` with a bogus `>` target,
losing the real filename into `argv` alongside it (`git status <> file` fed
"file" back to `git` as an ordinary argument, invisible to a resolver
checking `redirections`). `>|` (the noclobber-override write) was raised as
a related gap; it never reached a live bypass, because `_split_segments`
was already misreading its `|` as an unrelated pipe separator, splitting the
command into two nonsensical leaves that a resolver rejected by accident
rather than by design.

The fix **inverts the check rather than extending the enumeration** (the
enumeration will always be incomplete against the next unlisted operator
spelling — round 6 said so of its own leftovers, and it was right): a token
carrying a `<` or `>` that reached it unquoted and unescaped
(`_tokenize`'s new `bare_redir` flag, since a quoted `"a > b"` or an escaped
`\\>` is inert text the shell never treats as an operator, and must keep
passing through as before) now MUST fully parse as a redirection this
module understands, in `_extract_redirections`, or the whole extraction
fails closed — the token is kept for whatever transparency it offers, same
as every other fail-closed leaf in this module, but `complete` drops to
`False` rather than staying `True` over an unexamined metacharacter. This
closes not only the two concrete misses above but the whole class they are
instances of: an fd-name that isn't a valid identifier, an operator this
module has no entry for, or an operator glued to a preceding word with no
separating whitespace (`status>file` as one token — this tokenizer only
splits on whitespace, so a real shell metacharacter with nothing but a
letter before it was never checked at all) all now fail closed instead of
riding through as an ordinary argument.

`_REDIR_OP_RE` itself gained three recognized forms, on the same footing as
every operator already there: `{name}>`/`{name}>>`/`{name}<` (the bash
variable-redirect prefix, `\\{[A-Za-z_][A-Za-z0-9_]*\\}` — the exact
identifier grammar bash accepts there, nothing looser), `<>`, and `>|`.
`_split_segments` gained the matching adjacency check for `>|`'s `|`
(mirroring the existing `>&`/`&>` check for `&`), so the operator reaches
`_extract_redirections` as one token instead of being split into a bogus
pipe first.

Two round-6-reviewer suggestions, both now covered by test rather than left
implicit: a bare heredoc (`<<EOF`) and a bare fd-dup (`2>&1`, `3<&0`) with no
command were already fail-closed by round 6's general "redirection with no
leaf" rule (confirmed, not changed by this round — a heredoc/fd-dup token
parses to a `(operator, target)` pair same as any other, so it hits that
rule exactly the same way an ordinary `> f` does); `|&` (bash's
stdout+stderr pipe shorthand) is not specially recognized and is now
documented, in the module docstring's separator list above, as splitting
into a bare `|` plus an empty segment rather than being silently mishandled.
A function definition's compound body can carry a trailing redirection too
(`foo() ( git status ) > f`) — `_try_function_def` already drops whatever
follows the closing bracket unexamined, and that stays true; the reason it
is safe is now stated at that line: the name-shadowing hazard already forces
`complete=False` unconditionally, so no caller anywhere would have read a
weaker verdict had the redirection been captured instead of dropped.

**Round 8** closed the class both round-7 reviews found underneath the
inversion fix, from a third direction: round 7 taught `_extract_redirections`
to consult `_tokenize`'s bare/quoted flag only when `_REDIR_OP_RE` failed to
match the token's dequoted text — so whenever a QUOTED or ESCAPED token
happened to dequote to something the regex matched anyway, the flag was
never consulted at all, and the match was trusted outright. `printf hi
'{fd}'>file` dequotes to the exact same `{fd}>file` text as the unquoted
`printf hi {fd}>file`, and round 7's check could not tell them apart: both
matched `_REDIR_OP_RE` in full, so both were read as the bash
variable-redirect form, silently dropping the quoted `{fd}` from `argv` as
if it had never been an argument at all. Confirmed against real bash
(`bash -c`): the quoted form passes `printf` the literal arguments `hi` and
`{fd}` and separately performs a plain, unprefixed `>` redirect to `file`
(no `$fd` variable is ever set); only the unquoted form is the real
variable-redirect, passing `printf` just `hi` and setting `$fd`.

**Fix:** `_tokenize`'s single per-token flag becomes a per-character mask —
`bare_masks[i][j]` is `True` only if character `j` of token `i` reached the
token through the plain, unquoted, unescaped path. `_extract_redirections`
now judges `_REDIR_OP_RE`'s own matched groups by where their characters
actually came from, not by the shape of the dequoted string it matched
against, closing the same "decide from dequoted text" mistake round 7's own
fix was written to avoid, one layer deeper than round 7 reached:

- The operator group (`<`, `>>`, `<<<`, …) must be entirely bare to be a
  real redirection at all. Entirely quoted/escaped (`echo '>'`, `echo
  'a>b'`, `echo a\\>b`, `git commit -m "a > b"`) means nothing here was ever
  an operator — the token keeps its literal text in `argv`, unchanged.
- Given a bare operator, its fd-prefix (a bare digit run or a bare
  `{name}`) is honored as part of the operator only if the prefix is ALSO
  entirely bare. A quoted prefix (`'{fd}'>file`) is peeled off as its own
  ordinary argument token instead, immediately followed by the bare
  operator/target this module already knew how to record — the recorded
  operator is the bare symbol alone (`>`, not `{fd}>`), matching what the
  shell actually runs.
- A span (prefix or operator) with some bare and some quoted/escaped
  characters is a shape this module cannot fully account for and fails the
  whole extraction closed — the same fail-closed answer round 7 already
  gives an operator `_REDIR_OP_RE` cannot parse at all despite a bare
  `<`/`>` being present (`status>file`, glued with no separating
  whitespace).

Every other consumer of `_tokenize`'s output was checked for the same
dequote-then-match shape. `_resolve_leaf`'s process-substitution guard
(`"<(" in t or ">(" in t`, checked against already-dequoted `tokens`) has
the same shape but not the same defect: it only ever forces
`complete=False`, never grants, and quoting or escaping either character
defeats real bash's own process-substitution recognition too — so a quoted
`<(` that trips this check was never going to be real process substitution
either way. Left unchanged; the reason is now stated at that line.
`_ASSIGNMENT_RE.match(tok)` (in `_resolve_leaf_inner` and `_resolve_wrapper`)
matches the same way against dequoted text and, confirmed against real bash,
has an analogous gap in the opposite family (a quoted assignment-shaped
token like `'FOO'=bar` is not a real assignment to bash at all — it is the
literal program name, which then fails `_PROGRAM_TOKEN_RE`) — a distinct,
unfixed bypass in the assignment family, not the redirection family this
round and rounds 6-7 cover; left for a later round, same as `sudo`, `xargs`,
`cd`, and ANSI-C `$'...'` quoting — `_tokenize` has no `$'...'` branch, so a
token like `$'a\\x3eb'` falls through the plain-character path for the `$`
and the ordinary single-quote path for everything between the quotes,
dequoting to `$a\\x3eb` (the backslash-escape literal, not bash's own
translation of `\\x3e` to a real `>` byte) instead of real bash's `$a>b`.
Round 8's reviewer traced this as an accuracy gap in what the dequoted text
IS, not a quoting-boundary gap in what counts as bare: every character
between the quotes is still marked quoted regardless of what it decodes to,
so this cannot mis-mark a bare operator as quoted or a quoted one as bare —
the class round 7 and round 8 both closed. Left unmodeled for a later round.

**Round 9** closed the class the round-8 reviewer and security reviewer both
found underneath the per-character mask: `_extract_redirections` accepts a
redirection's TARGET — `_REDIR_OP_RE`'s own `(.*)$` group when the target is
glued to the operator's token, or the next token outright when the operator
stands alone — as one opaque string and never re-scans it for a further bare
`<`/`>` of its own. Real bash's word scanner does not stop reading at the
first redirection; the target it reads for one operator ends exactly where
the next bare metacharacter starts, so two real redirects glued with no
intervening whitespace (`git status
>/tmp/allowed>~/.ssh/authorized_keys`) are two separate operations in real
bash (`> /tmp/allowed` then `> ~/.ssh/authorized_keys`, the second the one
that actually determines where stdout lands) but were captured here as one
bogus pair, `('>', '/tmp/allowed>~/.ssh/authorized_keys')`, whose target
string matches no real path and so evades a path-based check without ever
tripping the general "operator glued to a preceding word" fail-closed rule
round 7 built for the operator side of the same token — that rule never ran
on the TARGET side. The next-token form of the same shape (`git status >
/tmp/a>/tmp/b`) had a second, independent way in: it reads `tokens[i + 1]`
directly, bypassing the `m is None` bare-bracket check that every other
token in the list is subject to. Both sites now re-scan the accepted target
text, using the same per-character bare mask round 8 introduced, and fail
the whole extraction closed if a bare `<`/`>` remains in it — a quoted or
escaped angle bracket in the target (`> "a>b"`) still carries no bare one
and is accepted as the literal string, unchanged.

**Round 10** closed the class round 9's reviewer set found underneath the
target re-scan, from a different construct entirely: a heredoc's BODY is not
a redirection target at all, and `_split_segments`'s flat, bare-newline
segment split had no idea a heredoc operator changes what the following
lines mean. `cat <<EOF\nrm -rf /\nEOF` split into three top-level segments on
its two newlines — `cat <<EOF`, `rm -rf /`, `EOF` — the same way `;` would.
The first resolved to a `cat` leaf with `<<EOF` read as an ordinary
redirection (`_REDIR_OP_RE` already matched `<<` before this round, the same
alternation slot as `<<<`, and nothing distinguished the two); the second, a
line that is real bash DATA — text `cat` merely reads from its own stdin,
never executed as a command — resolved to a live, `complete=True`
`Leaf('rm', ('rm', '-rf', '/'))`, because nothing about a bare identifier in
program position is a reserved word, a wrapper, or any other construct this
module fails closed on. `complete` is an AND across every segment
(`_reduce`'s loop), so this bogus leaf did not even need to survive alone —
it rode to `complete=True` on `cat`'s own segment being individually
"complete", with nothing to force the reduction closed the way a case/esac
keyword or a bare `{`/`}` already does for a torn function or case body (see
below).

**The property this closes:** the module never resolves text that is not a
command. A heredoc's body is data, and this module cannot see where it ends
without tracking the delimiter, so a command containing a heredoc operator
(`<<` or `<<-`, quoted or bare delimiter) fails closed rather than being
parsed line by line.

**Fix, in two parts, at the two places the module docstring's own summary
above says heredoc detection could live:**

- `_split_segments` now recognizes a bare `<<`/`<<-` directly in its raw-text
  scan (the only place with access to the RAW multi-line string before
  segmentation ever happens), reads the delimiter word right there — quoted
  or bare, it decides nothing for segmentation, only for parameter expansion
  inside a body this module never parses either way — and, once the current
  line's trailing newline is reached, swallows every line up to and
  including the delimiter's own terminator line (tab-stripped for `<<-`) as
  one unit. None of those lines is ever split into a segment, so none can be
  independently resolved as a command. A `<<<` here-string is excluded
  (matched and consumed as its own 3-character unit first, so the scan never
  re-examines its tail as a fresh `<<`) and keeps working exactly as before.
  Multiple heredocs on one line are swallowed in the order their operators
  appeared, matching real bash. A terminator line never found before the
  string runs out fails closed the same way an unbalanced quote or paren
  already does.
- `_extract_redirections` now checks a bare-operator match's own operator
  text for `<<`/`<<-` before doing anything else with it — no fd-prefix
  parsing, no target extraction, nothing folded onto `redirections` — and
  fails the whole extraction closed unconditionally. A heredoc's second word
  is a delimiter, not a target file `redirections` could accurately
  describe, and this module has no confident way to tell a benign heredoc
  body from an adversarial one, so it does not try: every command using a
  heredoc is denied, not just the ones whose body happens to look dangerous.

**Why failing closed beats modeling delimiters:** even a fully correct
delimiter-tracking parser would still have to decide, per PROGRAM, whether
that program treats its heredoc body as inert data (`cat`, `grep`) or as
commands to execute (`sh`, `bash`, anything else that reads its stdin as a
script) — a distinction this module has already refused to make for
ordinary argument grammar (`find -exec`, `git -c`, `tar --to-command`, …, in
the module docstring's `complete=True` paragraph above) and heredocs are the
same class of problem, not a new one: no enumeration of "programs that
execute stdin" is any more complete than the argument-grammar enumeration
this module already declines to attempt. Modeling multiple heredocs, `<<-`'s
tab-stripping, and quoted-vs-unquoted delimiters correctly (real bash
suppresses parameter expansion inside the body only for a quoted delimiter)
would all be spent proving a body is SAFE, when the module's job is only to
prove a command is fully understood. **What a caller loses:** a command
using a heredoc — even one as ordinary as `cat <<EOF\nhello\nEOF` — is never
granted, since this module cannot distinguish that from an adversarial body
it was never asked to trust.

**Two other multi-line constructs `_split_segments`'s flat newline split
already tears the same way, checked for the same live-bypass shape and
found NOT to be one, for reasons already true before this round:** a
function body across lines (`foo() {\\ngit status\\n}`) tears into `foo() {`,
`git status`, `}` exactly like the heredoc case did, and the middle segment
resolves to a live `git` leaf the same way `rm` did above — but a bare
`{`/`}` never satisfies `_PROGRAM_TOKEN_RE` (already stated in this
docstring's "Handled:" paragraph), so those torn segments force the OVERALL
`complete` closed regardless, the same AND-across-segments mechanism that
made the heredoc case dangerous now working in the caller's favor instead.
A case block (`case $x in\\n  a)\\n    git status\\n    ;;\\nesac`) tears the
same way and is protected the same way — by construction, not by a
heredoc-specific check — via the `case`/`esac` reserved-word dispatch (and,
independently, a case pattern's own bare `)` already underflows
`_split_segments`'s paren-depth counter, which was never heredoc-aware to
begin with). Neither was changed by this round; both are confirmed by test.
Two further multi-line shapes never reach the newline-split logic at all
and so were never at risk: a double-quoted argument spanning lines (the
`in_double` state consumes every character, including a literal newline,
before the depth-0 separator checks run) and a backslash-continued line (the
backslash-escape branch consumes the backslash and the newline together,
checked before the depth-0 checks too) — both confirmed by test, unchanged.

**A stale claim corrected:** the round-6 paragraph above states that a bare
heredoc with no command (`<<EOF` alone) was "already fail-closed by round
6's general 'redirection with no leaf' rule... a heredoc/fd-dup token parses
to a `(operator, target)` pair same as any other." That was accurate for the
no-command case at the time, but described `<<` as an ordinary
operator-with-a-target — exactly the treatment this round removes. The
no-command case still fails closed after this round, now via this round's
own unconditional check rather than the no-leaf rule; the observable result
for that shape is unchanged, but the mechanism description above is
historical, not current.
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Tuple

__all__ = ["reduce_to_families", "reduce_to_leaves", "Leaf"]


class Leaf(NamedTuple):
    """One command the shell grammar itself launches.

    `family` is its program name, or an `<interpreter>:inline` tag.

    `argv` is the words the program ITSELF receives, in exec order, after
    unwrapping wrapper words — flags and all, including flags whose OWN
    grammar launches a further command (`-c`, `-exec`, `-e`, …) — with every
    assignment and redirection removed, from any position. `argv` never
    contains a `NAME=VALUE` token or a redirection operator/target: those
    live on `assignments` and `redirections` below, because they don't reach
    the program as arguments even though they do reach it in other ways.

    `assignments` is every `NAME=VALUE` that reaches THIS leaf's own process
    environment — a leading assignment (`FOO=1 git status`), one supplied
    through an `env` wrapper (`env FOO=1 git status`), or one propagated down
    from an outer assignment whose process this leaf recurses through (`env
    BASH_ENV=/tmp/x bash -c "git status"` — real bash sources `BASH_ENV`
    before running the `-c` body, so it reaches `git`'s leaf too, not only
    `bash`'s). **These are not inert: they change what the program does.**
    `LD_PRELOAD`, `BASH_ENV` and `GIT_CONFIG_*` are exactly the shape a
    resolver must not wave through because `argv` alone looked innocuous.

    `env_modifiers` is a recognized `env` flag that changes the launched
    process's environment wholesale rather than adding a name to it — `-i`/
    `--ignore-environment`, `-u NAME`/`--unset NAME`, `-C DIR`/`--chdir DIR`.
    A flag this module does not model (`-S`'s own argument-splitting grammar,
    for instance) is never guessed at: it fails the whole leaf closed
    (`complete=False`) instead of being silently skipped.

    `redirections` is every `(operator, target)` pair the shell sets up
    around this leaf, at any position in the command — leading, trailing or
    between arguments — including a here-string (`<<<`) and an fd dup
    (`2>&1`). A resolver must judge these as effects, not skip them: a grant
    that only covers `argv` says nothing about whether a write redirection
    is acceptable.

    This module makes no claim about what a leaf's program does with `argv`,
    nor about whether its `assignments`/`env_modifiers`/`redirections` are
    safe to grant — only that all four were found. See `complete`'s
    docstring at the top of this file.
    """
    family: str
    argv: Tuple[str, ...]
    assignments: Tuple[str, ...] = ()
    env_modifiers: Tuple[str, ...] = ()
    redirections: Tuple[Tuple[str, str], ...] = ()


_PLACEHOLDER = "\x00SHELL_FAMILIES_SUBST\x00"

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Longer operators first (`<<<` before `<<-` before `<<`, `&>>` before `&>`,
# `>|` before `>`, `<>` before `<`) — `re.match` takes the first alternative
# that matches at the current position, not the longest, so a shorter
# operator listed first would swallow the leading characters of a longer one
# and leave its own tail (e.g. a bare `<` off a `<<<` here-string, the `-` of
# `<<-` misread as a fresh token, or the `|` of `>|` misread as a pipe)
# misread as part of the target or a separator.
#
# The leading group is either a bare fd number (`2>`) or, round 7, a bash
# "variable redirect" naming the fd instead of numbering it (`{fd}>file` —
# real bash, no option needed since 4.1, opens `file` and stores the
# descriptor number in `$fd`): `\{[A-Za-z_][A-Za-z0-9_]*\}` matches the exact
# brace-wrapped identifier grammar bash accepts there, nothing looser. `>|`
# (the noclobber-override write) and `<>` (open for both read and write) are
# also round 7: both are ordinary bash operators this module now models in
# full, on the same footing as every operator already here. `<<` and `<<-`
# (round 10, a heredoc and its tab-stripping variant) are matched here so
# `_extract_redirections` can RECOGNIZE the operator and intercept it — see
# the round-10 check there — never so it can be treated as an ordinary
# operator with a real target the way `<<<` (a here-string, unrelated
# despite the shared prefix) is.
_REDIR_OP_RE = re.compile(
    r"^(\d*|\{[A-Za-z_][A-Za-z0-9_]*\})"
    r"(<<<|&>>|<<-|>>|<<|>&|<&|&>|>\||<>|>|<)(.*)$"
)
_PROGRAM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

WRAPPER_WORDS = {
    "time", "env", "command", "exec", "nohup", "nice", "xargs", "sudo",
    "builtin",
}

# Shell interpreters recognized by NAME PATTERN, not exact string — a real
# system carries several spellings a fixed 3-name set misses: `dash`
# (Debian/Ubuntu's actual `/bin/sh`), `ash` (BusyBox's, Alpine's default),
# `ksh`/`mksh`/`yash`/`fish`, and version-suffixed `bash`/`zsh` binaries.
# `_resolve_shell_c` still requires a literal `-c` with a following argument
# to recurse; any of these names used any other way — no `-c`, or busybox's
# own `busybox sh -c ...` applet-dispatch shape, which this module's `-c`
# parsing does not model — already falls closed through
# `_resolve_shell_c`'s own fallthrough, so adding a name here can only add a
# correctly-modeled `-c` recursion, never a bare confident grant.
_SHELL_RECURSE_PATTERNS: List[re.Pattern] = [
    re.compile(r"^bash(\d+(\.\d+)*)?$"),
    re.compile(r"^sh$"),
    re.compile(r"^zsh(\d+(\.\d+)*)?$"),
    re.compile(r"^dash$"),
    re.compile(r"^ash$"),
    re.compile(r"^ksh(93)?$"),
    re.compile(r"^mksh$"),
    re.compile(r"^yash$"),
    re.compile(r"^fish$"),
    re.compile(r"^busybox$"),
]


def _is_shell_recurse(prog: str) -> bool:
    return any(p.match(prog) for p in _SHELL_RECURSE_PATTERNS)

# Interpreter name PATTERNS, for versioned/unversioned binary spellings
# (python, python3, python3.9, python3.11, ruby3.2, perl5.34, …), not exact
# strings — round 3 added `pythonw`/`pypy`, `nodejs` (Debian/Ubuntu's actual
# binary name absent a `node` symlink), `deno` (inline flag is the `eval`
# subcommand, not a `-` flag — the table entry below still works, since
# dispatch only compares `rest[0]` to the stored string), `bun`, `php`
# (`-r`, not `-c` — php's own `-c` means a config-file path) and `lua`/
# `luajit`. Round 4 added `Rscript -e`, `julia -e` and `expect -c`, the
# round-3 security-reviewer's non-blocking precision note (both flags
# verified against each interpreter's own documented usage). `tclsh` — named
# in the same note — is deliberately NOT added: tclsh's own argument grammar
# has no inline-code flag at all (`tclsh ?-encoding enc? ?fileName? ?arg
# ...?` — the first non-option word is always a script FILENAME, so `tclsh
# -c '...'` tries to open a file literally named `-c`, not run `...`).
# Fabricating a `-c`/`-e` entry for it would tag a real invocation with a
# flag tclsh does not recognize; it falls through to the plain-program
# branch instead, same as any other argument-grammar program (see the
# module docstring's `complete=True` paragraph) — its leaf still carries the
# program's real argv in full, so nothing about it is silently dropped.
# Checked in order; the first match wins. A prog not matching any pattern
# here falls through to the plain-program branch, same as before, unless it
# is separately listed in `_ALWAYS_UNREDUCIBLE_PROGRAMS` below.
_INTERPRETER_NAME_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^(python|pythonw|pypy)(2|3)?(\.\d+)?$"), "-c"),
    (re.compile(r"^node(js)?(\d+(\.\d+)*)?$"), "-e"),
    (re.compile(r"^deno$"), "eval"),
    (re.compile(r"^bun$"), "-e"),
    (re.compile(r"^perl(\d+(\.\d+)*)?$"), "-e"),
    (re.compile(r"^ruby(\d+(\.\d+)*)?$"), "-e"),
    (re.compile(r"^php(\d+(\.\d+)*)?$"), "-r"),
    (re.compile(r"^lua(jit)?(\d+(\.\d+)*)?$"), "-e"),
    (re.compile(r"^osascript$"), "-e"),
    (re.compile(r"^Rscript$"), "-e"),
    (re.compile(r"^julia$"), "-e"),
    (re.compile(r"^expect$"), "-c"),
]


def _interpreter_inline_flag(prog: str) -> Optional[str]:
    for pattern, flag in _INTERPRETER_NAME_PATTERNS:
        if pattern.match(prog):
            return flag
    return None


# Per wrapper word: {flag: takes_a_separate_value}. A flag not listed here
# makes the wrapper's whole leaf unreducible rather than guessed past — this
# is where "do not guess" is enforced for wrapper words specifically.
#
# `env`'s own flags split into two groups, only one of which
# `_resolve_wrapper` records on `Leaf.env_modifiers` (round 5): `-i`/
# `--ignore-environment`, `-u`/`--unset NAME` and `-C`/`--chdir DIR` change
# the ENVIRONMENT OR CWD the launched program actually runs with, so they are
# real modifiers a resolver must see; `-0`/`--null` only changes the
# delimiter of `env`'s OWN environment-listing output (irrelevant whenever a
# command follows, which is the only case that produces a leaf at all), so it
# is modeled (kept off the unrecognized-flag fail-closed path) but never
# recorded as a modifier. `-S` is deliberately left OUT of this table
# entirely: it takes a single string and re-splits it into env's own argv
# using shell-like word-splitting rules this module does not implement, so
# rather than guess at that split it falls through to the unrecognized-flag
# branch below and fails the whole leaf closed.
WRAPPER_FLAGS: Dict[str, Dict[str, bool]] = {
    "time": {},
    "nohup": {},
    "command": {"-p": False, "-v": False, "-V": False},
    "exec": {"-a": True, "-c": False, "-l": False},
    "env": {
        "-i": False,
        "-0": False,
        "-u": True,
        "-C": True,
        "--ignore-environment": False,
        "--null": False,
        "--unset": True,
        "--chdir": True,
    },
    "nice": {"-n": True},
    "sudo": {
        "-u": True,
        "-g": True,
        "-p": True,
        "-C": True,
        "-h": True,
        "-i": False,
        "-n": False,
        "-A": False,
        "-b": False,
        "-E": False,
        "-k": False,
        "-K": False,
        "-s": False,
        "-v": False,
        "-l": False,
        "-H": False,
    },
    "xargs": {
        "-I": True,
        "-n": True,
        "-P": True,
        "-L": True,
        "-s": True,
        "-E": True,
        "-a": True,
        "-d": True,
        "-0": False,
        "-r": False,
        "-t": False,
        "-x": False,
        "-o": False,
        "-p": False,
    },
}


# --- reserved words, coprocesses, and unmodeled name-resolution builtins ----
#
# The plain-program fallback at the bottom of `_resolve_leaf_inner` (`return
# {prog}, True`) grants confidence to ANY word in program position this
# module has not affirmatively classified elsewhere, discarding the rest of
# the tokens unexamined. Every round-3 bypass — bash compound-command
# keywords, `coproc`, unlisted shell/interpreter spellings, `alias` — is that
# one root cause wearing five different words, not five separate defects.
# The sets below are what closes it: every word that reaches this fallback
# must first be checked against one of them.

# Bash reserved words (from `compgen -k` in real bash), split by shape.
# `_KEYWORDS_WITH_TRAILING_COMMAND` are immediately followed by a real
# command in the grammar, so that remainder is recursed into for whatever
# transparency it offers (`complete` is still forced False). The rest
# introduce a block this module's flat, `;`-splitting segmenter cannot parse
# into a nested command at all, so no extraction is attempted — just
# `complete=False`. `{`, `}`, `!`, `[[`, `]]` are also reserved words but
# need no entry here: none of their characters satisfy `_PROGRAM_TOKEN_RE`,
# so they already fail closed by construction, before any name lookup runs.
# `function` and `time` are also reserved words, both handled earlier and
# more precisely than a blanket failure: `function` by `_try_function_def`
# (segment level, before tokenizing ever runs), `time` by `WRAPPER_WORDS` (a
# correct transparent unwrap — `time real-cmd` really does run `real-cmd`).
# `coproc` is handled separately, in `_resolve_leaf_inner` itself, since —
# unlike the rest of this set — real bash usage names an actual command
# right after it, worth a best-effort partial extraction.
_KEYWORDS_WITH_TRAILING_COMMAND = frozenset({
    "if", "elif", "while", "until", "do", "then", "else",
})
_OTHER_RESERVED_WORDS = frozenset({
    "fi", "case", "esac", "for", "select", "in", "done",
})
RESERVED_WORDS = _KEYWORDS_WITH_TRAILING_COMMAND | _OTHER_RESERVED_WORDS

# Builtins that run, source, or redefine code or name resolution in ways this
# module does not model: `source`/`.` run an unread file's contents; `trap`
# registers a command string that runs later, outside this string's visible
# flow; `enable -f` loads a shared object as a new builtin; `hash -p` remaps
# a name to an arbitrary path — the same name-shadowing hazard as a function
# definition; `alias`/`unalias` shadow a name the same way, live whenever the
# invoking shell has alias expansion on; `declare -f`/`typeset -f` and `fc
# -s` can re-surface or re-run code not visible in this string; `set` and
# `shopt` change the shell's own parsing/expansion behavior, including
# `shopt -s expand_aliases` — what turns the `alias` hazard live even in a
# non-interactive shell; `mapfile`/`readarray -C callback` evaluates
# `callback` once per `-c quantum` lines read from stdin, found during the
# round-3 bypass hunt and confirmed against real bash (`printf 'a\nb\n' |
# mapfile -C "touch /tmp/proof" -c 1 arr` creates the file); `export` and
# `readonly` (with or without `-x`) export a name into the environment for
# every LATER command in the same string, not just the leaf they appear on
# — round 5's bypass class: `export LD_PRELOAD=x; git status` used to
# resolve both segments individually to `complete=True`, hiding that
# `LD_PRELOAD` reaches `git`'s environment across the `;`, the same channel
# `set -a`/`declare -x`/`typeset -x` (already in this set) already closed.
# None of these can be told apart from their harmless uses (`declare -a`,
# `set -e`, a `trap` that only logs, a plain `mapfile arr`, an `export -p`
# listing) from the string alone, so every one fails closed unconditionally
# rather than being partially parsed.
UNMODELED_BUILTINS = frozenset({
    "source", ".", "trap", "enable", "hash", "alias", "unalias",
    "declare", "typeset", "set", "shopt", "fc", "mapfile", "readarray",
    "export", "readonly",
})

# awk's grammar takes its program as a positional argument, not behind a
# distinguishing flag the way `-c`/`-e` mark inline code for the interpreters
# in `_INTERPRETER_NAME_PATTERNS`, and it runs one on essentially every
# invocation that doesn't pass `-f`/`--file` naming an external script.
# Telling those two shapes apart with confidence is not attempted here;
# every spelling fails closed unconditionally.
_ALWAYS_UNREDUCIBLE_PROGRAMS = frozenset({"awk", "gawk", "mawk", "nawk"})


def reduce_to_families(command: str) -> Tuple[FrozenSet[str], bool]:
    """Reduce `command` to (families it can run, whether that was complete).

    A thin projection of `reduce_to_leaves()` onto program names — see the
    module docstring for what `complete` does and does not claim.
    """
    leaves, complete = _reduce(command)
    return frozenset(leaf.family for leaf in leaves), complete


def reduce_to_leaves(command: str) -> Tuple[Tuple[Leaf, ...], bool]:
    """Reduce `command` to (every Leaf the shell grammar launches, in exec
    order, whether that reduction was complete). See the module docstring —
    `complete=True` says every leaf was found with its full `argv`; it makes
    no claim about what a leaf's program does with that `argv`.
    """
    leaves, complete = _reduce(command)
    return tuple(leaves), complete


def _reduce(command: str) -> Tuple[List[Leaf], bool]:
    """The one parser both public functions project. Returns (leaves in exec
    order, whether the reduction was complete)."""
    if command is None:
        return [], False

    subs, cleaned = _extract_substitutions(command)
    if cleaned is None:
        return [], False

    leaves: List[Leaf] = []
    complete = True

    for inner in subs:
        f, c = _reduce(inner)
        leaves.extend(f)
        complete = complete and c

    segments, ok = _split_segments(cleaned)
    if not ok:
        return leaves, False

    for raw_seg in segments:
        seg = raw_seg.strip()
        if not seg:
            continue

        func_def = _try_function_def(seg)
        if func_def is not None:
            f, c = func_def
            leaves.extend(f)
            complete = complete and c
            continue

        subshell = _try_subshell(seg)
        if subshell is not None:
            if subshell[0] == "malformed":
                complete = False
            else:
                _, f, c = subshell
                leaves.extend(f)
                complete = complete and c
            continue

        tokenized = _tokenize(seg)
        if tokenized is None:
            complete = False
            continue
        if not tokenized:
            continue
        tokens = [tok for tok, _ in tokenized]
        bare_masks = [mask for _, mask in tokenized]

        f, c = _resolve_leaf(tokens, bare_masks)
        leaves.extend(f)
        complete = complete and c

    return leaves, complete


# --- command-substitution extraction (runs first, over the whole string) ----


def _match_dollar_paren(s: str, start: int) -> int:
    """`start` is just past `$(`. Return the index of the matching `)`, or -1."""
    depth = 1
    i, n = start, len(s)
    in_single = False
    in_double = False
    while i < n:
        c = s[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return -1


def _extract_substitutions(s: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Pull every `$( … )` / backtick span out of `s`, anywhere it occurs
    (including inside double quotes, where command substitution is still
    live). Each span's inner text is returned for recursive reduction; the
    span itself is replaced with a placeholder so the surrounding text keeps
    its shape for segment-splitting and tokenizing. A single top-level
    single-quoted region suppresses expansion, matching real shell semantics.
    Returns (None, None) on an unbalanced substitution.
    """
    subs: List[str] = []
    out: List[str] = []
    i, n = 0, len(s)
    in_single = False
    while i < n:
        c = s[i]
        if in_single:
            out.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == "'":
            in_single = True
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            end = _match_dollar_paren(s, i + 2)
            if end == -1:
                return None, None
            subs.append(s[i + 2:end])
            out.append(_PLACEHOLDER)
            i = end + 1
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j == -1:
                return None, None
            subs.append(s[i + 1:j])
            out.append(_PLACEHOLDER)
            i = j + 1
            continue
        out.append(c)
        i += 1
    if in_single:
        return None, None
    return subs, "".join(out)


# --- top-level segment splitting (; & && || | newline, paren-aware) --------


def _read_heredoc_delimiter(s: str, j: int) -> Optional[Tuple[str, int]]:
    """`s[j]` is the first character after a bare `<<`/`<<-` operator (and
    any whitespace already skipped by the caller). Read the delimiter word —
    quote-aware, the same as `_tokenize`'s per-character loop, since a
    delimiter's own quoting is exactly as significant here (`<<'EOF'`,
    `<<"EOF"`, `<<\\EOF` are all the literal delimiter `EOF`) — and return
    (dequoted delimiter, index just past it). Returns None on an unbalanced
    quote or an empty delimiter (`<<` followed immediately by whitespace or
    newline), both real bash syntax errors this module fails closed on the
    same as any other unbalanced construct.
    """
    n = len(s)
    chars: List[str] = []
    while j < n and s[j] not in " \t\n":
        c = s[j]
        if c == "'":
            k = s.find("'", j + 1)
            if k == -1:
                return None
            chars.append(s[j + 1:k])
            j = k + 1
            continue
        if c == '"':
            k = j + 1
            piece: List[str] = []
            while k < n and s[k] != '"':
                if s[k] == "\\" and k + 1 < n and s[k + 1] in ('"', "\\", "$", "`"):
                    piece.append(s[k + 1])
                    k += 2
                    continue
                piece.append(s[k])
                k += 1
            if k >= n:
                return None
            chars.append("".join(piece))
            j = k + 1
            continue
        if c == "\\" and j + 1 < n:
            chars.append(s[j + 1])
            j += 2
            continue
        chars.append(c)
        j += 1
    delimiter = "".join(chars)
    if not delimiter:
        return None
    return delimiter, j


def _split_segments(s: str) -> Tuple[Optional[List[str]], bool]:
    """Split on top-level separators, respecting quotes and paren depth.
    Parens are kept in the segment text (a segment may itself be a subshell);
    only the separator characters are consumed. Returns (None, False) on
    unbalanced quotes or parens.

    Round 10: a bare `<<`/`<<-` (a heredoc, as opposed to `<<<`, a
    here-string this loop never treats specially) is recognized here, not
    left to the token-level operator regex, because only this raw-text scan
    can see and SWALLOW the multi-line body that follows — the lines up to
    and including the delimiter's own terminator line are shell DATA, not
    further top-level commands, and must never reach the ordinary
    bare-newline segment split below. See the module docstring's Round 10
    section for what this closes and why the reduction still fails closed
    even once the body is correctly swallowed.
    """
    segments: List[str] = []
    buf: List[str] = []
    i, n = 0, len(s)
    in_single = False
    in_double = False
    depth = 0
    pending_heredocs: List[Tuple[str, bool]] = []
    while i < n:
        c = s[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                buf.append(s[i:i + 2])
                i += 2
                continue
            buf.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(s[i:i + 2])
            i += 2
            continue
        if c == "(":
            depth += 1
            buf.append(c)
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth < 0:
                return None, False
            buf.append(c)
            i += 1
            continue
        if depth == 0:
            if s[i:i + 3] == "<<<":
                # A here-string, unrelated to a heredoc despite the shared
                # `<<` prefix — consumed here as one 3-character unit, not
                # left to fall through the plain-character path one `<` at a
                # time: falling through one character at a time would leave
                # the SECOND and THIRD characters (`<<`, followed by
                # whatever comes after) to be re-examined by the heredoc
                # check just below on the very next loop iteration, wrongly
                # reading a real here-string's tail as a fresh heredoc
                # operator.
                buf.append("<<<")
                i += 3
                continue
            if s[i:i + 2] == "<<":
                # A heredoc (`<<`) or its tab-stripping variant (`<<-`).
                # Read the operator, then its delimiter word, right here —
                # the delimiter's own quoting decides nothing about
                # SEGMENTATION (only about parameter expansion inside the
                # body, which this module never parses either way), so it is
                # read the same way regardless.
                j = i + 2
                strip_tabs = False
                if j < n and s[j] == "-":
                    strip_tabs = True
                    j += 1
                while j < n and s[j] in " \t":
                    j += 1
                parsed = _read_heredoc_delimiter(s, j)
                if parsed is None:
                    return None, False
                delimiter, j = parsed
                pending_heredocs.append((delimiter, strip_tabs))
                buf.append(s[i:j])
                i = j
                continue
            if s[i:i + 2] == "&&":
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            if s[i:i + 2] == "||":
                segments.append("".join(buf))
                buf = []
                i += 2
                continue
            if c == "&":
                # A bare `&` is the background/control operator this loop
                # splits on — UNLESS it is part of a redirection operator
                # rather than standing alone: `>&`/`<&` (an fd dup, e.g. the
                # `&` in `2>&1`) has it immediately after `>`/`<` already in
                # `buf`, and `&>`/`&>>` (a combined stdout+stderr redirect)
                # has a `>` immediately after it. Either way it must stay in
                # the token stream for `_extract_redirections` to parse,
                # rather than being consumed here as a segment separator.
                if (buf and buf[-1] in (">", "<")) or (
                    i + 1 < n and s[i + 1] == ">"
                ):
                    buf.append(c)
                    i += 1
                    continue
                segments.append("".join(buf))
                buf = []
                i += 1
                continue
            if c == "|" and buf and buf[-1] == ">":
                # `>|` (the noclobber-override write) has its `|` immediately
                # after a `>` already in `buf`, the same adjacency test as
                # the `&>`/`>&` case above — round 7. A real pipe's `|` never
                # sits directly against a `>` with nothing between them (the
                # redirect's target always intervenes), so this cannot
                # misread a genuine `cmd > file | other` pipe: `buf[-1]`
                # there is the last character of `file`, not `>`.
                buf.append(c)
                i += 1
                continue
            if c == "\n" and pending_heredocs:
                # This newline ends the line carrying one or more heredoc
                # operators — real bash reads each pending body, in the
                # order its operator appeared, starting on the NEXT line.
                # End the current segment here (same as an ordinary bare
                # newline), then swallow every body: none of those lines is
                # a further top-level command, whatever they look like.
                segments.append("".join(buf))
                buf = []
                i += 1
                for delimiter, strip_tabs in pending_heredocs:
                    terminated = False
                    while i <= n:
                        line_end = s.find("\n", i)
                        end = n if line_end == -1 else line_end
                        line = s[i:end]
                        check_line = line.lstrip("\t") if strip_tabs else line
                        if check_line == delimiter:
                            terminated = True
                            i = n if line_end == -1 else line_end + 1
                            break
                        if line_end == -1:
                            i = n
                            break
                        i = line_end + 1
                    if not terminated:
                        # No terminator line before the string ran out — an
                        # unbalanced heredoc, a real bash syntax error too.
                        # Same fail-closed answer as an unbalanced quote.
                        return None, False
                pending_heredocs = []
                continue
            if c in (";", "\n", "|"):
                segments.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(c)
        i += 1
    if in_single or in_double or depth != 0 or pending_heredocs:
        return None, False
    segments.append("".join(buf))
    return segments, True


def _try_subshell(seg: str) -> Optional[Tuple]:
    """If `seg` is exactly a parenthesized group, recurse into its content.
    Returns None if `seg` doesn't start with `(`; `("malformed",)` if it
    starts with `(` but isn't a clean whole-segment wrap (stray trailing text,
    or no matching close); `("ok", families, complete)` on a clean subshell.
    """
    if not seg.startswith("("):
        return None
    depth = 0
    in_single = False
    in_double = False
    i, n = 0, len(seg)
    end = -1
    while i < n:
        c = seg[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
            i += 1
            continue
        i += 1
    if end == -1 or in_single or in_double:
        return ("malformed",)
    if seg[end + 1:].strip() != "":
        return ("malformed",)
    leaves, complete = _reduce(seg[1:end])
    return ("ok", leaves, complete)


# --- function definitions: name shadowing, never trusted after this point --

_FUNC_DEF_PAREN_RE = re.compile(r"^([^\s()]+)\s*\(\s*\)\s*(.*)$", re.DOTALL)
_FUNC_KEYWORD_RE = re.compile(r"^function\s+([^\s()]+)\s*(?:\(\s*\))?\s*(.*)$", re.DOTALL)

# `time` is a shell KEYWORD, not a command — unlike every other WRAPPER_WORDS
# entry (`env`, `sudo`, `exec`, …, each an ordinary program that cannot take
# a compound command as an argument, so `env git () ( … )` is a bash syntax
# error), the grammar lets `time [-p]` precede any command, including a
# function definition: `time git () ( echo pwned ); git status` is valid
# bash, actually runs the shadowing definition, and reduced to
# `{'git'}, complete=True` before this was stripped (found in the round-2
# bypass hunt, confirmed with `bash -c`).
_TIME_PREFIX_RE = re.compile(r"^time(\s+-p)?\s+")


def _find_matching_bracket(s: str, start: int, open_c: str, close_c: str) -> int:
    """`s[start] == open_c`. Return the index of the matching `close_c`,
    quote-aware, or -1 if there is none.
    """
    depth = 0
    i, n = start, len(s)
    in_single = False
    in_double = False
    while i < n:
        c = s[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == open_c:
            depth += 1
            i += 1
            continue
        if c == close_c:
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return -1


def _try_function_def(seg: str) -> Optional[Tuple[List[Leaf], bool]]:
    """If `seg` is a bash/POSIX function definition — `name () compound-command`
    or `function name [()] compound-command`, where `compound-command` is a
    `{ … }` or `( … )` body, optionally preceded by the `time` keyword — the
    name is no longer the program a caller granted: it shadows any external
    command of the same name for the rest of the string, so nothing
    downstream of a definition can be trusted to mean what it says. This
    always reports the definition (and everything at or after it)
    unreducible; the returned leaves carry the body's own reduction, for
    whatever transparency that offers, never as a substitute for `complete`.
    Returns None if `seg` is not this shape, so the caller falls through to
    ordinary leaf resolution.
    """
    stripped = seg.strip()
    tm = _TIME_PREFIX_RE.match(stripped)
    if tm is not None:
        stripped = stripped[tm.end():]
    m = _FUNC_DEF_PAREN_RE.match(stripped)
    if m is None:
        m = _FUNC_KEYWORD_RE.match(stripped)
    if m is None:
        return None
    name, body = m.group(1), m.group(2)
    if not _PROGRAM_TOKEN_RE.match(name):
        return None
    body = body.lstrip()
    if not body:
        # A signature with no body on this segment. Bash allows the
        # compound-command body to start on a later line — this module's
        # segment splitting (on a bare newline, same as `;`) has already cut
        # it off by the time this segment reaches here — and a bare `name
        # ()`/`function name` is never valid bash on its own (the body is
        # mandatory), so whatever follows is either the unseen body of a
        # shadowing definition or a syntax error that runs nothing. Neither
        # case can be trusted, so this fails closed rather than falling
        # through to the catch-all as an ordinary invocation of `name`.
        return [], False
    if body[0] not in "{(":
        return None
    open_c, close_c = ("{", "}") if body[0] == "{" else ("(", ")")
    end = _find_matching_bracket(body, 0, open_c, close_c)
    if end == -1:
        return [], False
    leaves, _ = _reduce(body[1:end])
    # Whatever trails the closing bracket (`foo() ( git status ) > f` — a
    # redirection on the function's own compound-command body, real bash
    # grammar) is never inspected here — round 7, security-reviewer:round6.
    # Capturing it would tell a caller nothing this function definition
    # doesn't already: `complete` is forced `False` on the line below
    # regardless of what that trailing text is, because the name-shadowing
    # hazard this function exists to catch is unconditional, not contingent
    # on a redirection being present too. Dropping it silently is safe for
    # exactly that reason — no code path anywhere treats this `False` as a
    # weaker denial than a redirection-aware one would be.
    return leaves, False


# --- word tokenizing (quote/backslash aware, no operator splitting) --------


def _tokenize(s: str) -> Optional[List[Tuple[str, Tuple[bool, ...]]]]:
    """Split `s` into whitespace-separated words, honoring single quotes
    (literal), double quotes (backslash-escapes `"` `\\` `$` `` ` ``) and a
    bare backslash escape outside quotes. Returns None on an unbalanced quote.

    Each token pairs its dequoted text with a `bare_mask` — round 8 — one
    `bool` per OUTPUT character, `True` only for a character that reached the
    token through the PLAIN, unquoted, unescaped path (the loop's final
    branch below), never through the single-quote, double-quote or
    backslash-escape branches. Round 7 tracked this as a single per-token
    flag set only for a `<`/`>` character; that let `_extract_redirections`
    match `_REDIR_OP_RE` against the token's DEQUOTED text and trust the
    match whenever it succeeded, which is wrong whenever part of what the
    regex matched came from a quote — `printf hi '{fd}'>file` dequotes to
    the same `{fd}>file` text as the unquoted `printf hi {fd}>file`, but real
    bash only treats the second as the variable-redirect form; in the first,
    `{fd}` is quoted, so it is an ordinary literal argument sitting next to a
    plain, unprefixed `>` (both confirmed against real bash). A per-character
    mask is what lets the caller tell those two identical-looking strings
    apart: it must judge each character `_REDIR_OP_RE` matched by where that
    character actually came from, not by the shape of the dequoted string.
    """
    tokens: List[Tuple[str, Tuple[bool, ...]]] = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        buf: List[str] = []
        mask: List[bool] = []
        while i < n and not s[i].isspace():
            c = s[i]
            if c == "'":
                j = s.find("'", i + 1)
                if j == -1:
                    return None
                for ch in s[i + 1:j]:
                    buf.append(ch)
                    mask.append(False)
                i = j + 1
                continue
            if c == '"':
                j = i + 1
                while j < n and s[j] != '"':
                    if s[j] == "\\" and j + 1 < n and s[j + 1] in ('"', "\\", "$", "`"):
                        buf.append(s[j + 1])
                        mask.append(False)
                        j += 2
                        continue
                    buf.append(s[j])
                    mask.append(False)
                    j += 1
                if j >= n:
                    return None
                i = j + 1
                continue
            if c == "\\" and i + 1 < n:
                buf.append(s[i + 1])
                mask.append(False)
                i += 2
                continue
            buf.append(c)
            mask.append(True)
            i += 1
        tokens.append(("".join(buf), tuple(mask)))
    return tokens


# --- leaf resolution: assignments/redirections, wrappers, eval, -c, inline -


def _span_quote_state(mask: Tuple[bool, ...], start: int, end: int) -> str:
    """Classify `mask[start:end]` (the characters `_REDIR_OP_RE` matched for
    one candidate operator, prefix or operator group alike): `"bare"` if
    every character in the span reached its token unquoted and unescaped,
    `"quoted"` if none did, `"mixed"` if some did and some didn't, `"empty"`
    for a zero-width span (an empty fd-prefix — `\\d*` matching no digits —
    always classifies this way, and is treated the same as `"bare"` by every
    caller, since there is no character there to be quoted at all).
    """
    if start == end:
        return "empty"
    chunk = mask[start:end]
    if all(chunk):
        return "bare"
    if not any(chunk):
        return "quoted"
    return "mixed"


def _has_bare_angle_bracket(tok: str, mask: Tuple[bool, ...]) -> bool:
    return any(mask[j] for j, ch in enumerate(tok) if ch in "<>")


def _extract_redirections(
    tokens: List[str],
    bare_masks: List[Tuple[bool, ...]],
) -> Tuple[List[str], List[Tuple[str, str]], bool]:
    """Scan `tokens` for a redirection operator at ANY position (leading,
    trailing or between arguments — real bash allows all three) and pull
    each one out as an `(operator, target)` pair, returning the remaining
    tokens with every redirection removed. The target is either attached to
    the operator's own token (`>>file`, `2>&1`) or, when the operator token
    stands alone, the very next token (`> file`) — an operator with nothing
    after it (`ok=False`) is a malformed redirection this module refuses to
    guess at rather than silently drop.

    `bare_masks[i]` (round 8, from `_tokenize`) marks, per character of
    `tokens[i]`, whether that character reached the token unquoted and
    unescaped. Round 7 tracked only a single per-token flag ("does this
    token contain a bare `<`/`>` ANYWHERE") and, whenever `_REDIR_OP_RE`
    matched the token's dequoted text at all, trusted the match without ever
    consulting that flag — so a token whose would-be fd-prefix was quoted
    but whose operator was bare still matched `_REDIR_OP_RE` in full on the
    dequoted string, and was accepted as a real prefixed redirection: real
    bash disagrees (confirmed against `bash -c`) — `printf hi '{fd}'>file`
    passes `printf` the literal argument `{fd}` (the quotes defeat the
    variable-redirect recognition) and separately performs a plain,
    unprefixed `>` redirect to `file`, while `printf hi {fd}>file`
    (unquoted) is the real variable-redirect form and passes `printf` only
    `hi`. Judging by DEQUOTED SHAPE alone cannot tell those two apart; this
    now judges by where `_REDIR_OP_RE`'s own matched characters came from,
    per group:

    - the operator group (`<`, `>>`, `<<<`, …) must be entirely bare to be a
      real redirection at all; if every one of its characters is instead
      quoted/escaped, nothing here was ever an operator — the token is an
      ordinary argument (`echo '>'`, `echo 'a>b'`, `echo a\\>b` all keep
      their literal text, exactly as before this round).
    - given a bare operator, its fd-prefix group (a bare digit run or a bare
      `{name}`) is honored as part of the operator only if it is ALSO
      entirely bare; a quoted prefix (`'{fd}'>file`) is instead peeled off
      as its own ordinary argument token, immediately followed by the
      operator/target this module already knows how to record — the
      operator recorded is the bare symbol alone (`>`, not `{fd}>`), since
      that is what the shell itself actually runs.
    - a span (prefix or operator) with SOME bare and SOME quoted/escaped
      characters is a shape this module cannot fully account for and fails
      the whole extraction closed, same as an operator `_REDIR_OP_RE` cannot
      parse at all despite a bare `<`/`>` being present in the token
      (`status>file` glued with no separating whitespace, round 7).
    - a TARGET (`remainder`, the attached-token case, or the next token when
      the operator stands alone) is accepted only if it carries no further
      bare `<`/`>` of its own — round 9. `_REDIR_OP_RE`'s `(.*)$` target
      group and the next-token grab both take the rest of their text as one
      opaque string and never re-scan it, so `git status
      >/tmp/allowed>~/.ssh/authorized_keys` used to record a single bogus
      pair `('>', '/tmp/allowed>~/.ssh/authorized_keys')` instead of the two
      real redirects real bash performs there (`> /tmp/allowed` then, since
      the word scanner stops at the next bare `>`, `> ~/.ssh/authorized_keys`)
      — a target string matching no real path, silently hiding the actual
      write. A target's own bare `<`/`>` now fails the whole extraction
      closed instead, the same as an unparseable operator does; a target
      whose bracket is quoted or escaped (`> "a>b"`) still carries no bare
      one and is accepted as the literal string, unchanged.
    - a bare `<<`/`<<-` — round 10 — is a heredoc, not an ordinary
      redirection: its second word is a delimiter naming where its
      multi-line BODY ends, not a target this function could accurately
      record, and that body has already been swallowed out of the token
      stream entirely by `_split_segments` before this function ever runs.
      Recognized here only to fail the whole extraction closed immediately,
      before any fd-prefix or target logic runs on it — no `(operator,
      target)` pair is ever recorded for one. `<<<` (a here-string) is
      unaffected; it is a different, ordinary operator that happens to share
      a prefix.
    """
    clean: List[str] = []
    redirs: List[Tuple[str, str]] = []
    ok = True
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        mask = bare_masks[i]
        m = _REDIR_OP_RE.match(tok)
        if m is None:
            if _has_bare_angle_bracket(tok, mask):
                ok = False
            clean.append(tok)
            i += 1
            continue

        p_start, p_end = m.span(1)
        op_start, op_end = m.span(2)
        remainder = m.group(3)

        op_state = _span_quote_state(mask, op_start, op_end)
        if op_state != "bare":
            # The operator's own characters aren't (all) bare, so this match
            # against dequoted text is not a real redirection here. `mixed`
            # is the round-7 unaccountable-shape case; `quoted` means the
            # whole matched span is inert text, but the token might still
            # carry a genuine bare `<`/`>` this particular match missed
            # (there is no such case in this module's current test set, but
            # nothing rules one out), so it gets the same whole-token check
            # the `m is None` branch above uses rather than being waved
            # through unconditionally.
            if op_state == "mixed" or _has_bare_angle_bracket(tok, mask):
                ok = False
            clean.append(tok)
            i += 1
            continue

        if tok[op_start:op_end] in ("<<", "<<-"):
            # Round 10: a heredoc. Its second word is a DELIMITER, not a
            # target — the shell reads a multi-line body afterward, which
            # `_split_segments` has already swallowed out of the token
            # stream entirely rather than let it reach here as further
            # segments. There is nothing to attach as a `redirections` pair
            # (a delimiter is not a file this leaf reads or writes), so this
            # is not folded into the ordinary operator/target machinery
            # below at all — the whole extraction fails closed instead, the
            # same fail-closed answer this module gives any other construct
            # it does not model with confidence.
            ok = False
            clean.append(tok)
            i += 1
            continue

        p_state = _span_quote_state(mask, p_start, p_end)
        if p_state == "mixed":
            ok = False
            clean.append(tok)
            i += 1
            continue

        if p_state == "quoted":
            # The fd-prefix text is quoted, so real bash does not treat it
            # as part of the operator — it is an ordinary literal argument
            # glued (in this one token) right before a real, unprefixed
            # redirection.
            if p_start != p_end:
                clean.append(tok[p_start:p_end])
            operator = tok[op_start:op_end]
        else:
            operator = tok[p_start:op_end]

        if remainder:
            r_start, r_end = m.span(3)
            if _has_bare_angle_bracket(remainder, mask[r_start:r_end]):
                # Two real redirects glued into one token with no separating
                # whitespace (`>/tmp/a>/tmp/b`) — `(.*)$` swallowed both as a
                # single target; refuse to guess which one real bash would
                # actually apply last.
                ok = False
                clean.append(tok)
                i += 1
                continue
            redirs.append((operator, remainder))
            i += 1
            continue
        if i + 1 >= n:
            return clean, redirs, False
        next_tok = tokens[i + 1]
        if _has_bare_angle_bracket(next_tok, bare_masks[i + 1]):
            # Same glued shape, split across whitespace instead of within one
            # token (`> /tmp/a>/tmp/b`) — the operator itself is fine, but its
            # target token is really a target plus a second operator this
            # loop would otherwise never get to see, since a direct grab of
            # `tokens[i + 1]` bypasses the `m is None` bare-bracket check
            # every other token in this list goes through.
            ok = False
            clean.append(tok)
            i += 1
            continue
        redirs.append((operator, next_tok))
        i += 2
    return clean, redirs, ok


def _attach_effects(
    leaves: List[Leaf],
    assignments: List[str],
    env_modifiers: List[str],
    redirections: List[Tuple[str, str]],
) -> List[Leaf]:
    """Prepend `assignments`/`env_modifiers`/`redirections` found at THIS
    level of unwrapping onto every leaf produced below it — every leaf of a
    shell recursion inherits the environment and redirections its parent
    process was given, so a single `env`/leading assignment ahead of `bash
    -c "a; b"` must reach both `a` and `b`, not just `bash` itself.
    """
    if not assignments and not env_modifiers and not redirections:
        return leaves
    return [
        leaf._replace(
            assignments=tuple(assignments) + leaf.assignments,
            env_modifiers=tuple(env_modifiers) + leaf.env_modifiers,
            redirections=tuple(redirections) + leaf.redirections,
        )
        for leaf in leaves
    ]


def _resolve_leaf(
    tokens: List[str], bare_masks: Optional[List[Tuple[bool, ...]]] = None
) -> Tuple[List[Leaf], bool]:
    # `bare_masks` is only ever supplied by `_reduce`, on the full token list
    # for a freshly tokenized segment — the one place `_tokenize` has quote
    # information to compute it from. Every recursive call from within this
    # module's own dispatch (`_resolve_wrapper`, the `coproc`/reserved-word
    # trailing-command paths) passes a SLICE of tokens already extracted from
    # here once, so no bare `<`/`>` this module hasn't already judged can
    # remain in it; the all-`False` default is exactly the "nothing left to
    # find" state those calls are always in, not a weaker check.
    if bare_masks is None:
        bare_masks = [tuple(False for _ in tok) for tok in tokens]
    # A process substitution is a construct this module does not model; fail
    # closed rather than silently drop the command it wraps. Checked against
    # the ORIGINAL tokens, before redirection extraction, since `<(`/`>(`
    # would otherwise be misread as an ordinary `<`/`>` redirection. This is
    # a substring check over already-dequoted text, the same shape round 8
    # closed in `_extract_redirections` below — but here it cannot become a
    # bypass the way that one did: it only ever forces `complete=False`
    # (never grants), and quoting/escaping either character defeats real
    # bash's own process-substitution recognition too, so a quoted `<(` that
    # trips this check was never going to be real process substitution
    # either way — the check is over-conservative on that input, not wrong
    # in the direction that matters. Left unchanged.
    procsub_ok = not any(("<(" in t or ">(" in t) for t in tokens)
    clean_tokens, redirections, redir_ok = _extract_redirections(tokens, bare_masks)
    # A command/process-substitution placeholder standing as a redirection's
    # TARGET (`git status > $(echo /tmp/x)`) is exactly as unresolved as one
    # standing in program position (`_resolve_leaf_inner`'s own `_PLACEHOLDER`
    # check below) — the shell still performs the redirection, to a target
    # this module never resolved, so recording it as if it were a real path
    # would be guessing. Fail the whole leaf closed instead.
    if any(_PLACEHOLDER in target for _, target in redirections):
        return [], False
    leaves, complete = _resolve_leaf_inner(clean_tokens)
    if redirections and not leaves:
        # A redirection with no leaf to attach it to is still a real
        # file-descriptor effect the shell performs — opening, truncating or
        # appending its target — even though no program launches: a bare
        # `> ~/.ssh/authorized_keys`, an assignment followed by nothing but a
        # redirect (`FOO=1 > f`), or a wrapper like `exec 3>file` with no
        # trailing command. Reporting `complete=True` here (as this used to)
        # would claim every effect was found and captured while silently
        # dropping the one effect that actually happened. Fail closed instead.
        return leaves, False
    leaves = _attach_effects(leaves, [], [], redirections)
    return leaves, (complete and procsub_ok and redir_ok)


def _resolve_leaf_inner(tokens: List[str]) -> Tuple[List[Leaf], bool]:
    # Redirections are already stripped by `_resolve_leaf` before this runs,
    # so the only thing a leading run of tokens can be here is a run of
    # `NAME=VALUE` assignments — captured, not discarded, and attached below
    # to whatever leaf(s) the rest of the line resolves to.
    idx, n = 0, len(tokens)
    assignments: List[str] = []
    while idx < n:
        tok = tokens[idx]
        if _ASSIGNMENT_RE.match(tok):
            assignments.append(tok)
            idx += 1
            continue
        break

    if idx >= n:
        # Assignment-only: sets a shell variable, launches nothing, so there
        # is no leaf to attach the assignments to.
        return [], True

    prog_token = tokens[idx]
    rest = tokens[idx + 1:]

    if _PLACEHOLDER in prog_token:
        return [], False

    prog = _basename(prog_token)
    if not _PROGRAM_TOKEN_RE.match(prog):
        return [], False

    leaves, complete = _dispatch_leaf(prog, tokens, idx, rest)
    if assignments:
        leaves = _attach_effects(leaves, assignments, [], [])
    return leaves, complete


def _dispatch_leaf(
    prog: str, tokens: List[str], idx: int, rest: List[str]
) -> Tuple[List[Leaf], bool]:
    """`tokens[idx]` is the program token (its basename is `prog`); `rest` is
    everything after it. Isolated from `_resolve_leaf_inner` so the
    leading-assignment capture there wraps every branch below uniformly,
    rather than each branch having to remember to merge it in itself.
    """
    if prog in WRAPPER_WORDS:
        return _resolve_wrapper(prog, rest)
    if prog == "eval":
        return _resolve_eval(rest)
    if prog == "coproc":
        # `coproc [NAME] command` — the real command follows, optionally
        # after a bare NAME identifier this module cannot reliably tell
        # apart from the command itself (both are plain words in the same
        # position). Best-effort: resolve `rest` as if no NAME were given,
        # for transparency only — `complete` is forced False regardless, so
        # a NAME-shaped misread never grants anything.
        f, _c = _resolve_leaf(rest) if rest else ([], False)
        return f, False
    if prog in RESERVED_WORDS:
        if prog in _KEYWORDS_WITH_TRAILING_COMMAND and rest:
            f, _c = _resolve_leaf(rest)
            return f, False
        return [], False
    if prog in UNMODELED_BUILTINS:
        return [], False
    if prog in _ALWAYS_UNREDUCIBLE_PROGRAMS:
        return [], False
    if _is_shell_recurse(prog):
        return _resolve_shell_c(rest)
    inline_flag = _interpreter_inline_flag(prog)
    if inline_flag is not None:
        # Real argv parsing stops reading flags at the first non-option
        # argument, so the flag only means "inline code" as the very first
        # token after the interpreter name. `prog script.py -c` runs
        # script.py, with "-c" merely passed through as the script's own
        # argv[1] — not the interpreter's flag — so that must be unreducible,
        # not misread as reviewed inline code.
        if rest and rest[0] == inline_flag:
            leaf = Leaf("{}:inline".format(prog), tuple(tokens[idx:]))
            return [leaf], True
        return [], False

    # Every other recognized token in program position is an ordinary leaf:
    # this module does not know, and does not guess, what the program's own
    # argument grammar does with `rest` (see the module docstring's
    # `complete=True` paragraph) — `argv` carries it in full so a caller-side
    # resolver can decide, rather than this module silently discarding it.
    return [Leaf(prog, tuple(tokens[idx:]))], True


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _resolve_wrapper(prog: str, rest: List[str]) -> Tuple[List[Leaf], bool]:
    flags = WRAPPER_FLAGS.get(prog, {})
    idx, n = 0, len(rest)
    env_assignments: List[str] = []
    env_modifiers: List[str] = []
    while idx < n:
        tok = rest[idx]
        if prog == "nice" and re.match(r"^-\d+$", tok):
            idx += 1
            continue
        if tok in flags:
            takes_value = flags[tok]
            idx += 1
            if takes_value:
                if idx >= n:
                    return [], False
                value = rest[idx]
                idx += 1
                if prog == "env" and tok in ("-u", "--unset", "-C", "--chdir"):
                    env_modifiers.append("{} {}".format(tok, value))
            elif prog == "env" and tok in ("-i", "--ignore-environment"):
                env_modifiers.append(tok)
            continue
        if prog == "env" and _ASSIGNMENT_RE.match(tok):
            env_assignments.append(tok)
            idx += 1
            continue
        if tok.startswith("-"):
            return [], False
        break

    if idx >= n:
        if prog == "env":
            # No trailing command: `env`'s own invocation (printing the
            # environment) is what actually runs, so it is a leaf in its own
            # right — `rest` here is only the flags already consumed above,
            # and any assignments/modifiers env's own flags set apply to
            # env's own process.
            leaf = Leaf(
                "env", tuple([prog] + rest),
                tuple(env_assignments), tuple(env_modifiers), (),
            )
            return [leaf], True
        return [], False

    leaves, complete = _resolve_leaf(rest[idx:])
    leaves = _attach_effects(leaves, env_assignments, env_modifiers, [])
    return leaves, complete


def _resolve_eval(rest: List[str]) -> Tuple[List[Leaf], bool]:
    if not rest:
        return [], False
    if any(_PLACEHOLDER in t for t in rest):
        return [], False
    joined = " ".join(rest)
    if joined.strip() == "":
        return [], False
    return _reduce(joined)


def _resolve_shell_c(rest: List[str]) -> Tuple[List[Leaf], bool]:
    i, n = 0, len(rest)
    while i < n:
        tok = rest[i]
        if re.fullmatch(r"-[A-Za-z]+", tok):
            if "c" in tok:
                if i + 1 >= n:
                    return [], False
                return _reduce(rest[i + 1])
            i += 1
            continue
        if tok == "--":
            i += 1
            continue
        return [], False
    return [], False
