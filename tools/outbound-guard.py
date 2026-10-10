#!/usr/bin/python3
"""PreToolUse hook: nothing this system runs may send to anyone but the operator.

The operator's rule (2026-09-11): the system never sends email, calendar invites or
messages to other people. A prompt saying so is not enough -- on 2026-09-11 the
calendar model was told what the event was and chose, on its own, to put
the shared rental inbox and an invented address on the guest list, and Google
mailed both. So the rule lives here, where the harness enforces it
whatever the model decides.

Two surfaces, both added at the operator's request:
- Connector tools (mcp__*): guests, sends, replies, forwards, shares, RSVPs.
- Bash: texting through Messages, and sending mail from the command line.
  Mail an APP sends to its own users is allowed on purpose (the operator, 2026-09-11), so
  a script file that imports smtplib is not scanned for it -- only mail code
  written inline in the command itself.

This is the machine-wide copy, run from agent-platform's primary checkout
(tools/, like every other hook in ~/.claude/settings.json; that checkout only
ever sits on main). EA keeps its own at EA/bin/outbound-guard.py for
detect-plans. Reads the tool call as JSON on stdin; exit 2 blocks the call and
hands the reason back to the model, exit 0 lets it through.

The operator's own addresses are per-machine operator data, read from
~/.config/convivy/outbound-guard.env (OWN_ADDRESSES=a@x, b@y) off the passwd
home. A missing or empty file leaves the set empty, so every connector
recipient counts as outside and the block message names the file.

It fails CLOSED on a connector call it cannot read, because a guard that errors
open is the same as no guard. On Bash it fails closed only when the command
mentions something it guards, so a bug here cannot take away every shell
command on the machine.

WHAT A MISSING SCRIPT DOES IS DECIDED BY THE REGISTRATION, not by this file.
`/usr/bin/python3 <missing file>` exits 2, which Claude Code reads as a block
(found by the steward, 2026-09-11). Registered that way on the connector
matcher, a vanished guard refuses every connector call, which is loud and
costs little. Registered that way on Bash it would refuse every shell command
in every session, including the one needed to put it back, so the Bash entry
wraps the call in a presence test: `[ -f "$f" ] || exit 0` in front of
`exec /usr/bin/python3 "$f"` lets every command through while the file is
absent, and a variant that greps stdin for send-shaped words blocks only those,
saying the guard is missing. Both pass a real exit 2 through unchanged. Either
way, DO NOT MOVE OR RENAME THIS FILE while settings.json points at it.
"""
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

# The account's home from the passwd database, not $HOME: a process that sets
# HOME must not be able to point this guard at an address list it wrote.
_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)

# The operator's own mailboxes, as per-machine data. One key, OWN_ADDRESSES, comma- or
# space-separated, parsed as DATA and never sourced, the same doctrine
# session_lifecycle applies to session-agent.env. The shared rental inbox stays
# OUT of it: guests' mail lands there, so an invite there reaches someone other
# than the operator, which is how this hook came to exist.
OWN_CONFIG = os.path.join(_HOME, ".config", "convivy", "outbound-guard.env")
_OWN_KEY = "OWN_ADDRESSES"


def own_addresses():
    """The operator's addresses from OWN_CONFIG, lowercased; empty when unset.

    An absent file, an absent key and a value holding no address all read the
    same: the empty set, under which every recipient is outside. Tokens that are
    not an address are dropped rather than trusted.
    """
    try:
        with open(OWN_CONFIG) as fh:
            text = fh.read()
    except OSError:
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != _OWN_KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return {tok.lower() for tok in re.split(r"[,\s]+", value) if ADDRESS.fullmatch(tok)}
    return set()

# Tools whose whole purpose is to reach another person. Blocked outright, with no
# recipient check: a reply or an RSVP goes to whoever sent the original, and that
# address never appears in the call.
ALWAYS_BLOCK = {"send_message", "reply", "forward", "share_file", "respond_to_event",
                # Slack connector, matching agent-platform#525. The draft is in
                # because nobody has confirmed a Slack draft stays private to the operator.
                # slack_add_reaction is left out on purpose: a reaction is not a
                # mail, an invitation or a message.
                "slack_send_message", "slack_send_message_draft", "slack_schedule_message",
                "slack_create_conversation", "slack_create_canvas", "slack_update_canvas"}

# Exact names missed slack_send_message, because `send_message` only matched
# Gmail's spelling. Any action NAMED as a send of a message, mail or text, on
# any server, is blocked too, so the next connector's spelling is covered.
SEND_ACTION = re.compile(r"send_(?:message|email|mail|sms|text)|schedule_(?:message|email|mail)")

# A draft sits in the operator's own mailbox until they send it themself, so its
# recipients reach no one. Exempt from the recipient check below and from nothing else. This
# rests on the Gmail connector being signed in as own1@example.test
# (checked 2026-09-11); if it is ever re-authorised as the shared rental inbox,
# a draft becomes readable by others and this exemption has to go.
DRAFTS = {"create_draft", "update_draft"}

# Any parameter whose NAME looks like a recipient is checked, rather than a fixed
# list of today's parameter names, so a connector that renames `attendees` to
# `guests` next month is still covered.
RECIPIENT_KEY = re.compile(r"attendee|guest|recipient|^to$|^cc$|^bcc$|emailaddress", re.I)
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A calendar write lands on whichever calendar `calendarId` names, and an event
# written onto someone else's calendar reaches them as surely as an invite: the
# shared rental inbox's calendar is one field away from the incident above. So
# on the two calendar WRITES only, `calendarId` is checked like a recipient.
# Reads (list_events, get_event) may name any calendar, and "primary" is the
# operator's own, which holds no address and so passes.
CALENDAR_WRITES = {"create_event", "update_event"}
CALENDAR_TARGET_KEY = re.compile(r"^calendar_?id$", re.I)

LOG = os.path.join(_HOME, ".local", "state", "agent-platform", "outbound-guard.log")
SELF = os.path.realpath(__file__)

# --- Bash ---------------------------------------------------------------------

# Messages is scripted through AppleScript or JXA, so the app name inside a
# `tell application` or `Application(...)` together with a send is the tell.
# Naming the app alone is not enough: reading buddies or chats sends nothing.
# A backslash may sit before each quote when the script is itself inside a quoted
# string -- python3 -c "... 'tell application \"Messages\" ...'" -- so allow one.
MESSAGES_APP = re.compile(
    r"""application\s+(?:id\s+)?\\*["'](?:Messages|com\.apple\.(?:MobileSMS|iChat))\\*["']"""
    r"""|Application\(\s*\\*["'](?:Messages|com\.apple\.(?:MobileSMS|iChat))\\*["']\s*\)""",
    re.I)
SEND = re.compile(r"\bsend\b", re.I)

# Mail transfer agents and mail clients that send from a shell. `mail`/`mailx`
# are handled apart, because bare `mail` only reads the local mailbox.
MAIL_COMMANDS = {"sendmail", "msmtp", "ssmtp", "mutt", "neomutt", "swaks", "himalaya", "aerc"}
HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh"}
MAIL_URL = re.compile(
    r"smtps?://|--mail-rcpt"
    r"|googleapis\.com/(?:upload/)?gmail/v1/users/[^/\s]+/(?:messages|drafts)/send", re.I)
INLINE_MAIL = re.compile(r"smtplib|Net::SMTP|nodemailer", re.I)
INTERPRETER = re.compile(r"^(?:python[0-9.]*|perl|ruby|node|deno|bun|osascript|bash|sh|zsh)$")
SHELLS = {"bash", "sh", "zsh"}
WRAPPERS = {"sudo", "env", "exec", "command", "nohup", "time", "nice", "caffeinate",
            "xargs", "timeout", "gtimeout", "arch"}
SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")"}
REDIRECT = re.compile(r"^[0-9]*(?:<<-?|<<<|<|>>|>|&>|>&|<&|&>>)$")

# Words that make a command worth a closer look. Used only to decide whether an
# analysis error should block (see the module docstring).
TRIGGER = re.compile(r"messages|mobilesms|ichat|mail|smtp|shortcuts|osascript|nodemailer|"
                     r"mutt|swaks|himalaya|aerc", re.I)


def log(line):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(f"[{datetime.now(timezone.utc).astimezone():%Y-%m-%dT%H:%M:%S}] {line}\n")
    except OSError:
        pass


def block(tool, reason, detail=""):
    log(f"BLOCKED {tool}: {reason}" + (f" :: {detail[:160]!r}" if detail else ""))
    print(f"Blocked by outbound-guard: {reason}. This machine never sends mail, "
          f"invitations or messages to anyone but the operator: no connector sends, no texting "
          f"through Messages, no mail from the command line. Do the rest of the task "
          f"without it.", file=sys.stderr)
    sys.exit(2)


def recipients(node, under_recipient_key=False):
    """Every address sitting under a recipient-shaped key, at any depth."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += recipients(value, under_recipient_key or bool(RECIPIENT_KEY.search(key)))
    elif isinstance(node, list):
        for item in node:
            found += recipients(item, under_recipient_key)
    elif isinstance(node, str) and under_recipient_key:
        found += ADDRESS.findall(node)
    return found


def tokens(command):
    # Newlines and backticks end a command just as `;` does. Swapping them in
    # before lexing also splits heredoc bodies into lines, which is harmless:
    # a body line is only ever treated as a command word here, never run.
    text = command.replace("\n", " ; ").replace("`", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        # No comment handling. With newlines already turned into `;`, a `#`
        # would swallow the whole rest of the text -- a script's shebang line
        # hid every command after it. A comment's words then become ordinary
        # tokens, and a comment never starts with a command word, so the cost
        # is nothing.
        lex.commenters = ""
        return list(lex)
    except ValueError:  # unbalanced quotes: fall back to a crude split
        return re.findall(r"[;|&()]+|[^\s;|&()]+", text)


def segments(command):
    current, skip_next = [], False
    for tok in tokens(command):
        if skip_next:
            skip_next = False
            continue
        if tok in SEPARATORS or (tok and set(tok) <= set(";|&()")):
            if current:
                yield current
            current = []
        elif REDIRECT.match(tok):
            skip_next = True  # the redirect target is a filename, not a command
        else:
            current.append(tok)
    if current:
        yield current


def command_words(words):
    """Drop leading VAR=value assignments and wrappers like sudo, env, timeout."""
    words = list(words)
    while words:
        base = os.path.basename(words[0])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
            continue
        if base not in WRAPPERS:
            break
        words.pop(0)
        while words and words[0].startswith("-"):
            flag = words.pop(0)
            takes_value = ((base == "sudo" and flag in ("-u", "-g", "-C", "-h", "-p"))
                           or (base == "xargs" and flag in ("-I", "-n", "-P", "-L", "-s", "-E", "-d")))
            if takes_value and words:
                words.pop(0)
        if base in ("timeout", "gtimeout") and words and re.match(r"^[0-9.]+[smhd]?$", words[0]):
            words.pop(0)
    return words


def executed_file(words, cwd):
    """The script file this command runs, if any: `python3 x.py`, `./x.sh`."""
    cmd, args = os.path.basename(words[0]), words[1:]
    if INTERPRETER.match(cmd):
        if any(a in ("-c", "-e", "-m") for a in args):
            return None  # inline code or a module; the command text is checked instead
        candidates = [a for a in args if not a.startswith("-")]
    elif "/" in words[0]:
        candidates = [words[0]]
    else:
        return None
    direct = not INTERPRETER.match(cmd)
    for cand in candidates:
        path = os.path.expanduser(cand)
        if not os.path.isabs(path):
            path = os.path.join(cwd or os.getcwd(), path)
        # Run directly, a file must be executable to run at all. Checking that
        # keeps heredoc and prose lines that merely begin with a path from
        # being mistaken for a script.
        if os.path.isfile(path) and (not direct or os.access(path, os.X_OK)):
            return os.path.realpath(path)
    return None


def file_text(path, is_osascript):
    # A file this process cannot read is one the command cannot run as the operator
    # either, and raising here would turn into a block on any command that also
    # mentions mail. Replaying a week of real commands hit this twice, on a
    # heredoc line that began with /etc/kcpassword.
    try:
        if path == SELF or os.path.getsize(path) > 2_000_000:
            return ""
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "ignore")
    if is_osascript and data.startswith(b"FasdUAS"):  # a compiled .scpt
        try:
            text += subprocess.run(["/usr/bin/osadecompile", path], capture_output=True,
                                   text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            pass
    return text


def texts_through_messages(text):
    return bool(MESSAGES_APP.search(text) and SEND.search(text))


def bash_reason(command, cwd, depth=0):
    """Why this command must not run, or None."""
    if re.search(r"\bosascript\b", command) and texts_through_messages(command):
        return "it would send a text through Messages"

    interpreter_seen = False
    for seg in segments(command):
        words = command_words(seg)
        if not words:
            continue
        cmd, args = os.path.basename(words[0]), words[1:]

        # Follow the command's own directory changes, or `cd dir && python3 x.py`
        # resolves x.py against the session's directory and never finds it.
        if cmd in ("cd", "pushd"):
            target = os.path.expanduser(next((a for a in args if not a.startswith("-")), "~"))
            cwd = target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)
            continue

        if cmd in MAIL_COMMANDS:
            return f"'{cmd}' sends mail from the command line"
        if cmd in ("mail", "mailx") and any(a in ("-s", "-t") or "@" in a for a in args):
            return f"'{cmd}' sends mail from the command line"
        if cmd == "shortcuts" and args[:1] == ["run"]:
            return "a Shortcut can send a message, and the guard cannot see inside one"
        if cmd in HTTP_CLIENTS and MAIL_URL.search(" ".join(args)):
            return f"'{cmd}' would send mail through SMTP or the Gmail API"
        if depth < 3:
            inner = None
            if cmd in SHELLS and "-c" in args and args.index("-c") + 1 < len(args):
                inner = args[args.index("-c") + 1]
            elif cmd == "eval":
                inner = " ".join(args)
            if inner:
                reason = bash_reason(inner, cwd, depth + 1)
                if reason:
                    return reason
        if INTERPRETER.match(cmd):
            interpreter_seen = True

        path = executed_file(words, cwd)
        if path:
            text = file_text(path, cmd == "osascript")
            if texts_through_messages(text):
                return f"{os.path.basename(path)} would send a text through Messages"
            is_shell = cmd in SHELLS or path.endswith((".sh", ".zsh", ".bash"))
            if is_shell and depth < 3:
                reason = bash_reason(text, os.path.dirname(path), depth + 1)
                if reason:
                    return f"{os.path.basename(path)}: {reason}"

    if interpreter_seen and (INLINE_MAIL.search(command) or MAIL_URL.search(command)):
        return "code in the command would send mail"
    return None


def check_command(tool, command, cwd):
    """Block `command` if it sends, else return. Fails closed only on a command
    that mentions something this guard watches (see the module docstring)."""
    try:
        reason = bash_reason(command, cwd)
    except Exception as exc:
        reason = (f"could not check the command ({exc})"
                  if TRIGGER.search(command) else None)
    if reason:
        block(tool, reason, command)


def main():
    try:
        call = json.load(sys.stdin)
        tool = call["tool_name"]
        args = call.get("tool_input") or {}
    except Exception as exc:  # fail closed
        block("?", f"could not read the tool call ({exc})")

    if tool == "Bash":
        check_command("Bash", args.get("command") or "", call.get("cwd"))
        sys.exit(0)

    # Connector tools are named mcp__<server>__<action>; judge by the action so
    # the same rule holds for any server that offers it.
    if not tool.startswith("mcp__"):
        # Claude registers this guard on the Bash and connector matchers only, so
        # nothing else reaches here from Claude. Codex's bridge registers it with
        # no matcher, and Codex's shell tool is not named Bash: reading "not Bash"
        # as "not a command" would let every Codex command through, and nothing
        # would say so. codex_tool_shape names the shell shapes it can read and
        # refuses the command-capable ones it cannot.
        try:
            sys.path.insert(0, os.path.dirname(SELF))
            import codex_tool_shape
            kind, command, cwd, why = codex_tool_shape.classify(call)
        except Exception as exc:  # fail closed, this branch is Codex-only
            block(tool, f"could not classify the tool call ({exc})")
        if kind == "unnormalizable":
            block(tool, why)
        if kind == "shell":
            check_command(tool, command, cwd or call.get("cwd"))
        sys.exit(0)
    action = tool.rsplit("__", 1)[-1]

    if action in ALWAYS_BLOCK or SEND_ACTION.search(action):
        block(tool, f"'{action}' sends to another person")

    if action not in DRAFTS:
        own = own_addresses()
        found = recipients(args)
        if action in CALENDAR_WRITES and isinstance(args, dict):
            found += [a for k, v in args.items()
                      if CALENDAR_TARGET_KEY.match(str(k)) and isinstance(v, str)
                      for a in ADDRESS.findall(v)]
        outside = sorted({a.lower() for a in found} - own)
        if outside:
            if not own:
                block(tool, f"it would send to {', '.join(outside)}, and no own "
                            f"addresses are configured in {OWN_CONFIG}")
            block(tool, f"it would send to {', '.join(outside)}")

    sys.exit(0)


if __name__ == "__main__":
    main()
