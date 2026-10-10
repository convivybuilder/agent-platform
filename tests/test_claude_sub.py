"""bin/claude-sub — the machine-wide Claude subscription switch.

Since 2026-09-07 the login Keychain has held TWO items under the service
`Claude Code-credentials`: acct "operator" (the one Claude Code itself reads
and refreshes) and a stale acct "unknown" whose token expired the same day.
Every call this tool made was unqualified (`-s` only, no `-a`), so `security`
silently returned whichever item it found first — the stale one in practice.
`status`/`current` then mislabelled the account, and `use <slot>` wrote the
target slot's token into the stale item and verified against that SAME item,
reporting `verified` while the credential Claude Code actually reads never
changed (issue_01M35FJXZYQR0M6GZZ0AG5F7PH).

These tests stub `security` entirely with an in-memory FakeKeychain that can
hold more than one item per service — no test here touches the real
Keychain, and no test prints or asserts on a credential value beyond
comparing opaque blob strings the test itself constructed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="session")
def cs():
    from conftest import _load_cli
    return _load_cli("claude-sub")


class FakeKeychain:
    """An in-memory stand-in for the login Keychain's generic-password items.
    Items are kept in insertion order so a test can control which one
    `security find-generic-password -s <service>` (unqualified) would return
    first — that ordering is exactly what made the stale acct "unknown" win
    on the real machine.
    """

    def __init__(self):
        self.items = []  # [{"svce": ..., "acct": ..., "blob": ...}, ...]
        self.foreign = []  # raw attribute-line pairs for dump() only — see below

    def add(self, svce, acct, blob):
        self.items.append({"svce": svce, "acct": acct, "blob": blob})

    def add_foreign_null_acct(self, svce):
        """A block `security dump-keychain` shows for an item claude-sub
        never looks up (not one of its own), whose acct attribute has no
        value — rendered `"acct"<blob>=<NULL>`, not a quoted string."""
        self.foreign.append((f'    "svce"<blob>="{svce}"',
                              '    "acct"<blob>=<NULL>'))

    def add_foreign_null_svce(self, acct):
        """Same shape, on the other attribute: `"svce"<blob>=<NULL>`."""
        self.foreign.append(('    "svce"<blob>=<NULL>',
                              f'    "acct"<blob>="{acct}"'))

    def add_foreign_hex_acct(self, svce, acct_text):
        """A block whose acct attribute holds a non-printable value:
        `security` renders it hex-encoded, followed by its quoted printable
        form — `"acct"<blob>=0x<HEX>  "text"`."""
        hexed = acct_text.encode("utf-8").hex().upper()
        self.foreign.append((f'    "svce"<blob>="{svce}"',
                              f'    "acct"<blob>=0x{hexed}  "{acct_text}"'))

    def find(self, svce, acct=None):
        for item in self.items:
            if item["svce"] == svce and (acct is None or item["acct"] == acct):
                return item
        return None

    def upsert(self, svce, acct, blob):
        item = self.find(svce, acct)
        if item is not None:
            item["blob"] = blob
            return
        self.add(svce, acct, blob)

    def add_new(self, svce, acct, blob):
        """`add-generic-password` without `-U`: fails if an item already
        matches (service, account) exactly."""
        if self.find(svce, acct) is not None:
            return False
        self.add(svce, acct, blob)
        return True

    def delete(self, svce, acct=None):
        before = len(self.items)
        self.items = [i for i in self.items
                      if not (i["svce"] == svce and (acct is None or i["acct"] == acct))]
        return len(self.items) != before

    def dump(self):
        out = []
        for item in self.items:
            out.append('keychain: "/Library/Keychains/login.keychain-db"')
            out.append("version: 512")
            out.append('class: "genp"')
            out.append("attributes:")
            out.append(f'    "acct"<blob>="{item["acct"]}"')
            out.append(f'    "svce"<blob>="{item["svce"]}"')
        for svce_line, acct_line in self.foreign:
            out.append('keychain: "/Library/Keychains/login.keychain-db"')
            out.append("version: 512")
            out.append('class: "genp"')
            out.append("attributes:")
            out.append(acct_line)
            out.append(svce_line)
        return "\n".join(out) + ("\n" if out or self.foreign else "")


def _parse_flags(tokens):
    flags = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-s":
            flags["s"] = tokens[i + 1]
            i += 2
        elif tok == "-a":
            flags["a"] = tokens[i + 1]
            i += 2
        elif tok == "-U":
            flags["U"] = True
            i += 1
        elif tok == "-w":
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                flags["w_present"] = True
                flags["w_value"] = tokens[i + 1]
                i += 2
            else:
                flags["w_present"] = True
                i += 1
        else:
            i += 1
    return flags


def fake_security_run(keychain):
    """Return a stand-in for the module's `subprocess.run`, dispatching on
    `security`'s subcommands against `keychain`. Mirrors the real CLI's
    contract closely enough for claude-sub's own call sites: `-w` alone
    (find) prints the password only, `-w <blob>` (add) supplies one, and
    `dump-keychain` never prints a password either way — matching the ban on
    running `security -g`/`-w` for real."""

    def run(cmd, capture_output=True, text=True):
        if cmd[0] != "security":
            # `auth_status_email`'s `claude auth status` fallback — no
            # fixture models a live `claude` session, so report "no answer"
            # exactly like an unauthenticated/absent binary would.
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        sub = cmd[1]
        flags = _parse_flags(cmd[2:])
        svce = flags.get("s")
        acct = flags.get("a")

        if sub == "find-generic-password":
            item = keychain.find(svce, acct)
            if item is None:
                return SimpleNamespace(returncode=1, stdout="",
                                        stderr="security: item not found\n")
            if flags.get("w_present"):
                return SimpleNamespace(returncode=0, stdout=item["blob"] + "\n", stderr="")
            body = (
                'keychain: "/Library/Keychains/login.keychain-db"\n'
                "version: 512\n"
                'class: "genp"\n'
                "attributes:\n"
                f'    "acct"<blob>="{item["acct"]}"\n'
                f'    "svce"<blob>="{item["svce"]}"\n'
            )
            return SimpleNamespace(returncode=0, stdout=body, stderr="")

        if sub == "add-generic-password":
            blob = flags.get("w_value")
            if flags.get("U"):
                keychain.upsert(svce, acct, blob)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            ok = keychain.add_new(svce, acct, blob)
            if ok:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="",
                                    stderr="security: item already exists\n")

        if sub == "delete-generic-password":
            ok = keychain.delete(svce, acct)
            if ok:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="",
                                    stderr="security: item not found\n")

        if sub == "dump-keychain":
            return SimpleNamespace(returncode=0, stdout=keychain.dump(), stderr="")

        raise AssertionError(f"unstubbed security subcommand: {sub}")

    return run


def _blob(refresh_token, tier="max20x", expires_in_ms=999_999_999_999):
    return json.dumps({"claudeAiOauth": {
        "refreshToken": refresh_token,
        "expiresAt": expires_in_ms,
        "rateLimitTier": tier,
    }})


LIVE_ACCOUNT = "operator"


@pytest.fixture
def kc(cs, monkeypatch, tmp_path):
    """A fresh FakeKeychain wired in as claude-sub's only view of `security`,
    with the live macOS user pinned so `live_account()` is deterministic, and
    the mirrored credential file redirected off the real `~/.claude/`."""
    keychain = FakeKeychain()
    monkeypatch.setattr(cs, "subprocess", SimpleNamespace(run=fake_security_run(keychain)))
    monkeypatch.setattr(cs, "getpass", SimpleNamespace(getuser=lambda: LIVE_ACCOUNT))
    monkeypatch.setattr(cs, "CRED_FILE", str(tmp_path / "credentials.json"))
    return keychain


# MARK: - Fix 1: `-a` scoping reads the right item despite the stale duplicate


def test_status_reports_the_current_users_slot_despite_the_stale_duplicate(cs, kc, capsys):
    # Insertion order matters: "unknown" first, matching the real machine —
    # an unqualified `find-generic-password -s ...` (the pre-fix call) would
    # return this one, so `status` would fail to match any slot.
    kc.add(cs.LIVE_SERVICE, "unknown", _blob("stale-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("live-refresh-token"))
    kc.add(cs.slot_service("a"), "operator@example.com", _blob("live-refresh-token"))

    cs.cmd_status()

    out = capsys.readouterr().out
    assert "slot a" in out
    assert "could not identify active slot" not in out


# MARK: - Fix 1: `use` writes/deletes only the current user's live item


def test_use_writes_only_the_current_users_item_leaving_the_stale_one_untouched(cs, kc):
    kc.add(cs.LIVE_SERVICE, "unknown", _blob("stale-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("old-live-refresh-token"))
    kc.add(cs.slot_service("b"), "other@example.com", _blob("target-refresh-token"))

    cs.cmd_use("b")

    stale = kc.find(cs.LIVE_SERVICE, "unknown")
    live = kc.find(cs.LIVE_SERVICE, LIVE_ACCOUNT)
    assert stale is not None and stale["blob"] == _blob("stale-refresh-token")
    assert live is not None and live["blob"] == _blob("target-refresh-token")
    # Exactly the two items that existed before `use` — no third item added,
    # no delete of anything under LIVE_SERVICE.
    live_items = [i for i in kc.items if i["svce"] == cs.LIVE_SERVICE]
    assert len(live_items) == 2


# MARK: - Fix 2: refuse only the shape `-a` cannot resolve


def test_status_refuses_when_two_items_share_service_and_account(cs, kc):
    # A true (service, account) collision: `-a` cannot tell these two apart,
    # unlike the real "unknown" + current-user duplicate above. A slot is
    # present so a `SystemExit` here can only be the duplicate guard, never
    # `cmd_status`'s unrelated "no slots found" check.
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("first-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("second-refresh-token"))
    kc.add(cs.slot_service("a"), "operator@example.com", _blob("first-refresh-token"))

    with pytest.raises(SystemExit):
        cs.cmd_status()


def test_refusal_names_the_account_and_count(cs, kc, capsys):
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("first-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("second-refresh-token"))

    with pytest.raises(SystemExit):
        cs.cmd_current()

    err = capsys.readouterr().err
    assert "2" in err
    assert LIVE_ACCOUNT in err
    assert cs.LIVE_SERVICE in err


def test_different_account_duplicate_does_not_trigger_the_refusal(cs, kc):
    # The real-world shape (acct "unknown" beside the current user) is
    # exactly what fix 1's `-a` scoping resolves; the guard must not block
    # this case; see refuse_if_live_service_duplicated's docstring for why.
    kc.add(cs.LIVE_SERVICE, "unknown", _blob("stale-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("live-refresh-token"))

    cs.refuse_if_live_service_duplicated()  # must not raise


# MARK: - Fix 3: NULL/hex-valued attributes on unrelated keychain items
# don't crash the walk that looks for LIVE_SERVICE duplicates.
#
# `security dump-keychain` lists every item in the whole login keychain, not
# just claude-sub's own — WiFi, other apps' generic-password items, and so
# on. This machine's own keychain carries 8 <NULL>-valued acct/svce
# attributes against 99 quoted ones. `_keychain_item_pairs` (used by every
# command but `list`) has to walk past those without raising.


def test_status_survives_unrelated_items_with_null_and_hex_attributes(cs, kc, capsys):
    kc.add_foreign_null_acct("com.apple.airport.preferences")
    kc.add_foreign_null_svce("some-other-app-account")
    kc.add_foreign_hex_acct("com.example.other-app", "\x00binary-account\x01")
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("live-refresh-token"))
    kc.add(cs.slot_service("a"), "operator@example.com", _blob("live-refresh-token"))

    cs.cmd_status()  # must not raise IndexError

    out = capsys.readouterr().out
    assert "slot a" in out


def test_refusal_still_fires_on_a_real_duplicate_amid_null_and_hex_items(cs, kc):
    kc.add_foreign_null_acct("com.apple.airport.preferences")
    kc.add_foreign_null_svce("some-other-app-account")
    kc.add_foreign_hex_acct("com.example.other-app", "\x00binary-account\x01")
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("first-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("second-refresh-token"))
    kc.add(cs.slot_service("a"), "operator@example.com", _blob("first-refresh-token"))

    with pytest.raises(SystemExit):
        cs.cmd_status()


def test_refusal_does_not_fire_on_null_and_hex_items_alone(cs, kc):
    # The foreign NULL/hex items must never themselves be counted as
    # LIVE_SERVICE duplicates — they carry unrelated service names.
    kc.add_foreign_null_acct("com.apple.airport.preferences")
    kc.add_foreign_null_svce("some-other-app-account")
    kc.add_foreign_hex_acct("com.example.other-app", "\x00binary-account\x01")
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("live-refresh-token"))

    cs.refuse_if_live_service_duplicated()  # must not raise


# MARK: - _attr_value: direct coverage of the two branches the round-2
# reviewer flagged as correct-but-unpinned — an embedded `="` inside a
# quoted value, an embedded escaped quote inside one, and the hex form with
# no trailing quoted rendering.


def test_attr_value_quoted_string_with_an_embedded_equals_quote(cs):
    # `line.split("=", 1)` splits only on the FIRST "=" (the one right after
    # `<blob>`), so a value that itself contains the substring `="` must
    # survive intact rather than being cut short at that inner split point.
    line = '    "acct"<blob>="a="b"'
    assert cs._attr_value(line) == 'a="b'


def test_attr_value_quoted_string_with_an_embedded_escaped_quote(cs):
    # A value containing a mid-string escaped quote (`\"`) must come back
    # unchanged apart from the outer quote pair `rstrip('"')` removes — only
    # the single true closing quote at the very end is stripped.
    line = '    "acct"<blob>="say \\"hi\\" folks"'
    assert cs._attr_value(line) == 'say \\"hi\\" folks'


def test_attr_value_hex_form_with_no_trailing_quoted_text(cs):
    # `0x<HEX>` with nothing after it (no space, no quoted rendering) —
    # `partition(" ")` then yields an empty `quoted`, so the hex bytes
    # themselves must be decoded rather than treated as unrecognized.
    line = '    "acct"<blob>=0x68656C6C6F'
    assert cs._attr_value(line) == "hello"


# MARK: - Fix 4: `toggle` must reach the duplicate guard before its own
# unguarded read of the (possibly ambiguous) live item can misreport a
# different failure. At 6ec28ed, `cmd_toggle` dropped the guard call and
# read `LIVE_SERVICE` first; with a true (service, account) duplicate whose
# blob matches no configured slot, `active_slot()` returned None and toggle
# died with "cannot identify the active slot" instead of the duplicate
# refusal — contradicting both the code comment and the PR description.


def test_toggle_refuses_on_a_real_duplicate_before_misreporting_the_active_slot(cs, kc, capsys):
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("first-refresh-token"))
    kc.add(cs.LIVE_SERVICE, LIVE_ACCOUNT, _blob("second-refresh-token"))
    kc.add(cs.slot_service("a"), "a@example.com", _blob("slot-a-token"))
    kc.add(cs.slot_service("b"), "b@example.com", _blob("slot-b-token"))

    with pytest.raises(SystemExit):
        cs.cmd_toggle()

    err = capsys.readouterr().err
    assert "cannot identify the active slot" not in err
    assert cs.LIVE_SERVICE in err
    assert LIVE_ACCOUNT in err
