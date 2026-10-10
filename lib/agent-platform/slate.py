"""slate — the Slate store: one file of asks and notes per crew.

`platform/decisions/0091-the-corkboard-is-a-per-crew-file-on-the-crews-host`
replaces the operator's habit of copying a co's questions into Apple Notes with one
sheet per crew in Deck's cockpit. An **ask** is a question the co needs the
operator to answer; a **note** is something the operator wants to raise with
the co at its next break point. This module owns the one file per crew both
sides read and write: `bin/co-ask` (the co's command, this slice) and the
`session-agent slate*` verbs (Deck's side, CQ-358, built on this module's API).

LOCATION AND KEY. `<state_root()>/<key>.json`, where `key` is the crew's tmux
session name run through `crew_key.session_key` — the same reduction
`bin/co-working-on` and `session_state.working_on` already key by, so a card
posted under a session name is found under that same session name everywhere
else that name is used. `state_root()` is a function, not a frozen constant,
so it re-reads `$HOME` on every call: the eager module-import pass in
`tests/conftest.py` would otherwise bake in the real home directory before any
test got a chance to point `$HOME` elsewhere, and the concurrency test needs a
*second OS process* (not just this one, monkeypatched) to resolve the same
overridden root.

WHY `~/.local/state`, NOT `~/.cache`. Cards are the operator's own data —
unsent answers and draft notes — and must survive a cache clean the way
`co-working-on`'s disposable "what am I doing" line does not need to.

LOCKING AND DURABILITY. Every write takes an exclusive `fcntl.flock` on a
sibling `.lock` file, so `bin/co-ask` and `session-agent` can never interleave
a read-modify-write and drop a card. A write lands via a temp file in the same
directory, `fsync`ed, `os.replace`d into place, with the directory itself
`fsync`ed afterward — the three steps that make the replace itself durable, not
just atomic. A read takes a shared lock, so it can never observe a half-written
file (`os.replace` already rules that out) racing a concurrent writer's choice
of what to write.

A CORRUPT FILE IS NEVER OVERWRITTEN. `load()` and `mutate()` both raise
`SlateCorruptError` before yielding anything to write back, so the bad bytes
stay on disk for the operator to look at rather than being silently replaced
by a fresh empty board.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import secrets
import stat
import string
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import crew_key as CK  # shared sanitiser + tmux lookup; agent-platform CQ-357

# Re-exported under this module's own name so `bin/co-ask` has one import for
# everything Slate-related, rather than two for a lookup this store's own API
# doesn't otherwise need (every function below takes an already-sanitised
# `key`, the same split `bin/co-working-on` draws between resolving a tmux
# name and writing under it).
tmux_session_name = CK.tmux_session_name
session_key = CK.session_key

SCHEMA_VERSION = 1

ASK_STATES = ("open", "answered", "resolved")
NOTE_STATES = ("draft", "sent")

# (from, to) pairs allowed for an ask. `("answered", "answered")` is the
# re-send case the ADR names explicitly: the operator may edit and resend an
# answer while the ask sits answered, without that being a transition at all —
# `answer_ask` below handles it as a no-op on `state`, listed here only so the
# refusal table reads as the complete picture of what "answer" may do.
_ASK_ANSWER_FROM = ("open", "answered")
_ASK_RESOLVE_FROM = ("open", "answered")
_NOTE_SEND_FROM = ("draft",)

_ID_ALPHABET = string.ascii_lowercase + string.digits
_ID_RANDOM_CHARS = 6
_ID_GEN_ATTEMPTS = 100


class SlateError(Exception):
    """Base class for every error this module raises."""


class SlateCorruptError(SlateError):
    """The board file exists but its bytes could not be read as a Slate board.

    Raised by `load()` and by `mutate()` before the block body ever runs — the
    file is never overwritten while this stands.
    """


class SlateNotFoundError(SlateError):
    """No card with the given id exists on this board."""


class SlateTransitionError(SlateError):
    """The requested change is not an allowed transition for that card."""


class SlateBoardTooLargeError(SlateError):
    """The write would leave the board over `_BOARD_MAX_BYTES`, which every
    later read refuses. Raised by `_atomic_write` before anything is written,
    so the board on disk is left as it was."""


def state_root() -> Path:
    """Where Slate board files live. See the module docstring for why this is
    a function, re-read on every call, rather than a module-level constant."""
    return Path.home() / ".local" / "state" / "convivy" / "slate"


def board_path(key: str) -> Path:
    return state_root() / f"{key}.json"


def lock_path(key: str) -> Path:
    return state_root() / f"{key}.json.lock"


def _ensure_dir() -> None:
    """The board directory, created at 0700 — and tightened to 0700 even when
    it already existed looser. Modes are set explicitly rather than via
    `mkdir`'s `mode=`, which only applies when a directory is actually
    created and is masked by umask either way."""
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)


@contextmanager
def _locked(key: str, flock_mode: int) -> Iterator[None]:
    """Hold an flock of `flock_mode` (`fcntl.LOCK_EX` or `fcntl.LOCK_SH`) on
    `key`'s lock file for the duration of the block.

    The lock file is opened (and created, at 0600, if absent) before the mode
    is applied, and explicitly `fchmod`ed to 0600 regardless — the same
    "never rely on umask, and tighten what's already there" rule `_ensure_dir`
    follows, applied to the one other file this module creates outside the
    atomic-write path.
    """
    _ensure_dir()
    lp = lock_path(key)
    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, flock_mode)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _empty_board(key: str) -> dict:
    return {"version": SCHEMA_VERSION, "session": key, "cards": []}


# A ceiling on every board read, for the same two failures
# `session_state._read_working_on`'s `_WORKING_ON_MAX_BYTES` exists to stop
# (platform/decisions/0091; found by `security-reviewer` on #752, the same
# class `deck-security-reviewer` found on #434 against that other file): an
# unbounded `read_bytes()` lets a board-sized-for-abuse file stall whatever
# called in, and `json.loads` recurses per nesting level, so a few hundred
# bytes of `[[[[…` raises `RecursionError` — neither `OSError` nor
# `ValueError` — well before any byte ceiling would even see it as large.
# 1 MiB, not that file's 4096: a board is a growing list of cards, not one
# small recording, and this only needs to be well below "stalls a poll",
# not tight against legitimate use.
_BOARD_MAX_BYTES = 1_048_576


def _read_raw(path: Path) -> dict | None:
    """The board at `path`, or `None` if it does not exist.

    Raises `SlateCorruptError` for anything else that stops this from being a
    valid board — unparseable JSON, JSON that isn't an object, an object with
    no `cards` list, a path that cannot be read as a plain file (a directory,
    a permission-denied file, anything else `OSError` covers), a file over
    `_BOARD_MAX_BYTES`, or JSON nested deep enough to raise `RecursionError`
    while it is parsed — so a caller can tell "nothing recorded yet" from
    "something is wrong here" and never silently treat the second as the
    first, and so that every one of those is this same documented refusal
    rather than an uncaught traceback out of whatever called in.

    Refuses anything that is not a regular file WITHOUT blocking on the open
    (round 3, `security-reviewer` finding on #752, the same class
    `security-reviewer` found against `telemetry.read_cost_partial` on
    agent-platform#592): `load()` and `mutate()` are the two callers, and
    both this directory (0700, writable by every process running as the
    operator's account — the same threat model round 2 already accepts) and
    `mutate()`'s own exclusive flock mean a board replaced by a named pipe
    with no writer would otherwise hang `open(path, "rb")` forever, freezing
    `session_states()`'s whole poll of every crew it batches in one call
    (not just the one poisoned board) and, through `mutate`, the crew's lock
    along with it. `os.lstat` decides first, without opening — a FIFO,
    socket, device, symlink or directory here raises `SlateCorruptError`
    immediately rather than reaching `open()` at all. The open that follows
    is `O_NOFOLLOW` (refuses a symlink substituted at the last instant) and
    `O_NONBLOCK` (an open of a FIFO without it can itself block waiting for a
    writer — the lstat check alone doesn't close the race between the lstat
    and the open, since the path can be swapped to a FIFO in between);
    `fstat` on the resulting descriptor re-checks `S_ISREG` before any read,
    closing that race.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SlateCorruptError(f"{path}: unreadable ({e})") from e
    if not stat.S_ISREG(st.st_mode):
        raise SlateCorruptError(f"{path}: not a regular file")
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SlateCorruptError(f"{path}: unreadable ({e})") from e
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SlateCorruptError(f"{path}: not a regular file")
        raw = os.read(fd, _BOARD_MAX_BYTES + 1)
    except OSError as e:
        raise SlateCorruptError(f"{path}: unreadable ({e})") from e
    finally:
        os.close(fd)
    if len(raw) > _BOARD_MAX_BYTES:
        raise SlateCorruptError(f"{path}: over {_BOARD_MAX_BYTES} bytes")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise SlateCorruptError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(value, dict) or not isinstance(value.get("cards"), list):
        raise SlateCorruptError(f"{path}: not a Slate board object")
    return value


def load(key: str) -> dict:
    """The board for `key`, under a shared lock. A missing file reads as an
    empty board; a corrupt one raises `SlateCorruptError`."""
    with _locked(key, fcntl.LOCK_SH):
        value = _read_raw(board_path(key))
    return value if value is not None else _empty_board(key)


def _encode_board(path: Path, board: dict) -> str:
    """The text `_atomic_write` writes for `board`, refused if that text is over
    `_BOARD_MAX_BYTES`. `prepare_send` calls this on the board with the send
    applied, so a send that could not persist is refused before delivery."""
    data = json.dumps(board, indent=2) + "\n"
    size = len(data.encode("utf-8"))
    if size > _BOARD_MAX_BYTES:
        raise SlateBoardTooLargeError(
            f"{path}: the board would be {size} bytes, over the "
            f"{_BOARD_MAX_BYTES}-byte limit; nothing was written")
    return data


def _atomic_write(path: Path, board: dict) -> None:
    """Write `board` to `path`: a temp file in the same directory, fsynced,
    `os.replace`d into place, with the directory itself fsynced afterward so
    the replace survives a crash, not merely looks atomic to a concurrent
    reader. The temp file is created at 0600 explicitly (not via umask), and
    `os.replace` carries that mode to the final path regardless of what mode
    (if any) previously lived there — the mechanism that tightens a
    pre-existing looser-permissioned board file on its next legitimate write.

    Every write goes through here, so this is the one place the size ceiling
    is enforced on the way out. A board over `_BOARD_MAX_BYTES` is refused
    before the temp file is created, because `_read_raw` would refuse that
    file on every later read and the crew's Slate would stay unreachable.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = _encode_board(path, board)
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # `fd` is owned by this call until `os.fdopen` below wraps it; past
        # that point the file object's own close (end of the `with`) is what
        # releases it, and closing it again here would be a double close.
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@contextmanager
def mutate(key: str) -> Iterator[dict]:
    """Load the board for `key` under an exclusive lock, yield it for in-place
    mutation, and write it back atomically once the block exits normally.

    A corrupt file raises `SlateCorruptError` before the block body ever
    runs. Any exception raised INSIDE the block — including every
    `SlateTransitionError`/`SlateNotFoundError` the card-transition functions
    below raise — propagates without the write ever happening, so a refused
    transition costs nothing on disk. This is the primitive CQ-358's
    `session-agent slate*` verbs build their own card edits on, alongside the
    functions below.
    """
    with _locked(key, fcntl.LOCK_EX):
        value = _read_raw(board_path(key))
        board = value if value is not None else _empty_board(key)
        yield board
        board["session"] = key
        _atomic_write(board_path(key), board)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_card(board: dict, card_id: str) -> dict | None:
    for card in board["cards"]:
        if card.get("id") == card_id:
            return card
    return None


def get_card(key: str, card_id: str) -> dict:
    """The card `card_id` on `key`'s board, or raise `SlateNotFoundError`.

    The read `session-agent`'s CQ-358 verbs use to return `{"card": {...}}`
    after a mutation.
    """
    card = _find_card(load(key), card_id)
    if card is None:
        raise SlateNotFoundError(f"no card {card_id!r}")
    return card


def _apply_answer(card: dict, answer: str, now: str) -> None:
    """The field writes of an answer. `answer_ask` and `prepare_send` both call
    this, so both paths make the same field writes. The size `prepare_send`
    checks equals the size `answer_ask` writes when the board is unchanged
    between the two calls."""
    card["answer"] = answer
    card["state"] = "answered"
    card["answered_at"] = now
    card["updated_at"] = now


def _apply_send(card: dict, now: str) -> None:
    """The field writes of a note send, shared by `send_note` and `prepare_send`
    for the same reason `_apply_answer` is."""
    card["state"] = "sent"
    card["sent_at"] = now
    card["updated_at"] = now


def _gen_id(prefix: str, existing: set[str]) -> str:
    for _ in range(_ID_GEN_ATTEMPTS):
        candidate = f"{prefix}-" + "".join(
            secrets.choice(_ID_ALPHABET) for _ in range(_ID_RANDOM_CHARS))
        if candidate not in existing:
            return candidate
    raise SlateError(f"could not generate a unique {prefix}- id")


# --- asks ----------------------------------------------------------------


def post_ask(key: str, question: str) -> str:
    """Append a new, open ask card with `question` and return its id.

    Refuses an empty or whitespace-only question with `ValueError` — this is
    the one validation `bin/co-ask`'s own argument handling defers to, so the
    rule lives once.
    """
    question = question.strip()
    if not question:
        raise ValueError("a question must not be empty")
    with mutate(key) as board:
        existing_ids = {c["id"] for c in board["cards"]}
        card_id = _gen_id("a", existing_ids)
        now = _now_iso()
        board["cards"].append({
            "id": card_id, "kind": "ask", "state": "open",
            "question": question, "answer_draft": None, "answer": None,
            "outcome": None,
            "created_at": now, "updated_at": now,
            "answered_at": None, "resolved_at": None,
        })
    return card_id


def answer_ask(key: str, card_id: str, answer: str) -> None:
    """Record the operator's `answer` on an open or answered ask.

    Re-sending on an already-`answered` ask is allowed and leaves it
    `answered` — the ADR's "the operator can edit the answer and send again"
    — so this is the one ask transition whose `to` state can equal its `from`
    state.
    """
    answer = answer.strip()
    if not answer:
        raise ValueError("an answer must not be empty")
    with mutate(key) as board:
        card = _find_card(board, card_id)
        if card is None:
            raise SlateNotFoundError(f"no card {card_id!r}")
        if card["kind"] != "ask":
            raise SlateTransitionError(f"{card_id} is a note, not an ask")
        if card["state"] not in _ASK_ANSWER_FROM:
            raise SlateTransitionError(
                f"cannot answer {card_id} from state {card['state']!r}")
        _apply_answer(card, answer, _now_iso())


def resolve_ask(key: str, card_id: str, outcome: str) -> None:
    """Close an open or answered ask with a one-line `outcome`."""
    outcome = outcome.strip()
    if not outcome:
        raise ValueError("an outcome must not be empty")
    with mutate(key) as board:
        card = _find_card(board, card_id)
        if card is None:
            raise SlateNotFoundError(f"no card {card_id!r}")
        if card["kind"] != "ask":
            raise SlateTransitionError(f"{card_id} is a note, not an ask")
        if card["state"] not in _ASK_RESOLVE_FROM:
            raise SlateTransitionError(
                f"cannot resolve {card_id} from state {card['state']!r}")
        now = _now_iso()
        card["state"] = "resolved"
        card["outcome"] = outcome
        card["resolved_at"] = now
        card["updated_at"] = now


def open_asks(key: str) -> list[dict]:
    """Every ask card that is not resolved, oldest first — the shape
    `bin/co-ask list` prints. Cards are already stored oldest first, so this
    is a filter, not a sort."""
    board = load(key)
    return [c for c in board["cards"]
            if c.get("kind") == "ask" and c.get("state") != "resolved"]


def ask_counts(key: str) -> dict:
    """`{"asks_open": n, "asks_answered": n}` for `key`'s board — the counts
    `session-agent state`/`states` carry (platform/decisions/0091). A missing
    board is `load()`'s own empty board, so both read 0 with no special case
    here. A corrupt board propagates `SlateCorruptError`; the caller
    (`session_state.slate_counts`) is the one that turns that into
    `slate_error` and 0/0, so the rail's poll never fails on a bad board.
    """
    cards = load(key)["cards"]
    return {
        "asks_open": sum(1 for c in cards
                          if c.get("kind") == "ask" and c.get("state") == "open"),
        "asks_answered": sum(1 for c in cards
                              if c.get("kind") == "ask" and c.get("state") == "answered"),
    }


# --- notes -----------------------------------------------------------------


def create_note(key: str, text: str) -> str:
    """Append a new draft note and return its id. The co never calls this —
    `bin/co-ask` has no note verb — it exists for CQ-358's `slate-note`."""
    text = text.strip()
    if not text:
        raise ValueError("a note must not be empty")
    with mutate(key) as board:
        existing_ids = {c["id"] for c in board["cards"]}
        card_id = _gen_id("n", existing_ids)
        now = _now_iso()
        board["cards"].append({
            "id": card_id, "kind": "note", "state": "draft",
            "text": text, "created_at": now, "updated_at": now, "sent_at": None,
        })
    return card_id


def send_note(key: str, card_id: str) -> None:
    """Deliver a draft note, marking it sent. Delivery itself (`session-agent
    send`) is CQ-358's concern; this only records that it happened."""
    with mutate(key) as board:
        card = _find_card(board, card_id)
        if card is None:
            raise SlateNotFoundError(f"no card {card_id!r}")
        if card["kind"] != "note":
            raise SlateTransitionError(f"{card_id} is an ask, not a note")
        if card["state"] not in _NOTE_SEND_FROM:
            raise SlateTransitionError(
                f"cannot send {card_id} from state {card['state']!r}")
        _apply_send(card, _now_iso())


# --- CQ-358: session-agent's slate* verbs build on the above -----------------


def _is_visible(card: dict) -> bool:
    """Whether a card shows without `--all`: a resolved ask or a sent note is
    hidden, everything else shows. The file keeps both either way."""
    return not (
        (card.get("kind") == "ask" and card.get("state") == "resolved")
        or (card.get("kind") == "note" and card.get("state") == "sent")
    )


def list_cards(key: str, include_all: bool = False) -> list[dict]:
    """Every card on `key`'s board, oldest first — the shape `session-agent
    slate` returns."""
    board = load(key)
    if include_all:
        return board["cards"]
    return [c for c in board["cards"] if _is_visible(c)]


def edit_card(key: str, card_id: str, text: str) -> None:
    """Replace a draft note's `text`, or an open/answered ask's
    `answer_draft` — `session-agent slate-edit`'s one verb, dispatched here on
    the card's own `kind` so there is one lock acquisition and one place that
    knows which field a card's kind edits, rather than the caller picking a
    function by a kind it would have to look up first under a separate lock.

    Refuses a sent note or a resolved ask. Unlike `post_ask`/`create_note`/
    `answer_ask`, the text is stored exactly as given, not stripped and not
    checked for emptiness: the ADR names only the two state refusals, and a
    draft the operator is still typing is allowed to pass through empty.
    """
    with mutate(key) as board:
        card = _find_card(board, card_id)
        if card is None:
            raise SlateNotFoundError(f"no card {card_id!r}")
        if card["kind"] == "note":
            if card["state"] not in _NOTE_SEND_FROM:
                raise SlateTransitionError(
                    f"cannot edit {card_id} from state {card['state']!r}")
            card["text"] = text
        else:
            if card["state"] not in _ASK_ANSWER_FROM:
                raise SlateTransitionError(
                    f"cannot edit {card_id} from state {card['state']!r}")
            card["answer_draft"] = text
        card["updated_at"] = _now_iso()


def delete_note(key: str, card_id: str) -> None:
    """Delete a draft note. Refuses an ask of any state, or a note that has
    already been sent."""
    with mutate(key) as board:
        card = _find_card(board, card_id)
        if card is None:
            raise SlateNotFoundError(f"no card {card_id!r}")
        if card["kind"] != "note":
            raise SlateTransitionError(f"{card_id} is an ask, not a note")
        if card["state"] not in _NOTE_SEND_FROM:
            raise SlateTransitionError(
                f"cannot delete {card_id} from state {card['state']!r}")
        board["cards"].remove(card)


def ask_message(question: str, card_id: str, answer: str) -> str:
    """The exact text `session-agent slate-send` delivers for an answered ask
    (platform/decisions/0091)."""
    return f"[slate {card_id}] Your question: {question}\nMy answer: {answer}"


def note_message(card_id: str, text: str) -> str:
    """The exact text `session-agent slate-send` delivers for a sent note."""
    return f"[slate {card_id}] Note from the operator:\n{text}"


def prepare_send(key: str, card_id: str) -> tuple[dict, str]:
    """The card to send and the exact message to deliver for it, validated as
    sendable but NOT YET mutated.

    Delivery (`session_input.send_message`) is an external side effect this
    module must never hold its own file lock across, so this function only
    reads and validates; `finalize_send` performs the actual transition once
    delivery has succeeded. Raises `SlateNotFoundError`/`SlateTransitionError`
    for the same refusals `send_note`/`answer_ask` would raise on the
    transition itself, and — for an ask only — `ValueError` when
    `answer_draft` has no usable text, the same check `answer_ask` already
    makes on that same transition, run here before a message ever reaches the
    pane rather than after. It also sizes the board with the send applied,
    through the same `_encode_board` check `_atomic_write` makes, and raises
    `SlateBoardTooLargeError` if that write would be refused, so a transition
    that cannot be persisted is never delivered first.
    """
    board = load(key)
    card = _find_card(board, card_id)
    if card is None:
        raise SlateNotFoundError(f"no card {card_id!r}")
    now = _now_iso()
    after = copy.deepcopy(board)
    target = _find_card(after, card_id)
    if card["kind"] == "note":
        if card["state"] not in _NOTE_SEND_FROM:
            raise SlateTransitionError(
                f"cannot send {card_id} from state {card['state']!r}")
        message = note_message(card_id, card["text"])
        _apply_send(target, now)
    else:
        if card["state"] not in _ASK_ANSWER_FROM:
            raise SlateTransitionError(
                f"cannot send {card_id} from state {card['state']!r}")
        answer = (card.get("answer_draft") or "").strip()
        if not answer:
            raise ValueError("an answer must not be empty")
        message = ask_message(card["question"], card_id, answer)
        _apply_answer(target, answer, now)
    # `mutate` writes the session key before it sizes the board, so this does too.
    after["session"] = key
    _encode_board(board_path(key), after)
    return card, message


def finalize_send(key: str, card_id: str, kind: str, answer: str | None = None) -> dict:
    """Record that `card_id` was just delivered, and return the updated card.
    Called only once delivery has succeeded — see `prepare_send`.

    `answer` is the exact text `prepare_send` put in the message, passed
    through rather than re-read from the board, so what gets persisted as
    `answer` can never drift from what the co actually received even if the
    operator edited the draft again in the window between the two calls.
    """
    if kind == "note":
        send_note(key, card_id)
    else:
        answer_ask(key, card_id, answer)
    return get_card(key, card_id)
