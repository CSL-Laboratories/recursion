"""Installer-signed authority profiles (lane K3, grill Q28) with a durable spend ledger.

A profile (``csl-authority-profile/vNext``) is the order permission's authority: one
plugin artifact; one scope (account, venue, contract family, adapter and environment);
the provider and model version the proposing run must be bound to; the permitted
actions (effect kinds); a validity window; and spending budgets in integer minor units.
Every field is required and matched exactly, so an unknown value fails closed rather
than selecting a more permissive profile (PRODUCT.md 4.1). Not built yet from the 4.1
field set: legal perimeter, information restrictions, stop and recovery owners,
evidence dossier and human receipt. The installer signs the canonical profile bytes with a key made
on the user's machine. The composition root injects only the installer's *public* key;
nothing a plugin supplies can become the anchor, and a key on CSL's published list is
refused as an anchor (the profile is signed on the user's machine, never by CSL).

Budgets are enforced against a SQLite ledger opened in WAL mode with
``synchronous=FULL``. Each charge runs in one ``BEGIN IMMEDIATE`` transaction, so
threads, processes and restarts all see one total:

* split effects add up against ``total_minor`` (N small charges over budget refuse);
* each charge has a unique idempotency key, so a replayed effect is refused, not
  re-charged;
* a ``profile_id`` is bound to its first signed bytes, so re-signing or editing a
  profile under the same id cannot reset its spend;
* the daemon never creates the ledger; ``create_ledger`` is the installer's step. A
  missing ledger refuses, so deleting the file does not silently reset spend.

Same-uid limit (B17, stated, not solved): every guard above lives in the daemon's
process and files, which in-process plugin code running as the same uid can rewrite
(replace the anchor attribute, patch ``verify``, plant an entry in the per-instance
verify cache, edit or recreate the ledger). The
tests record which of those mutants succeed. Containment needs a separate OS user or
process for the anchor and ledger (F11, F21); F21 is not done.

2026-11-11 same-uid fallback (O69, ``policy.SAME_UID_FALLBACK_POLICY``; Lucas,
2026-11-08, t1418u; not accepted text in CSL-Laboratories/csl-research
contracts/PRODUCT.md 055cc7a5c793b7135e5d0565dc4198cf96230531, SHA-256
e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf, receipt
CSLR-D-011). Implementation explanation, not a quote:
an in-process paper-order plugin holds paper-order permission. Under that
fallback the signed authority profile is tamper evidence, not a boundary. Paper only
(PRODUCT.md 5.2 / CP0-v2, order authority stays paper-first; O69 is the signed-profile item);
live capital stays separately authorised (PRODUCT.md 5.2). This module has one
``order`` permission. ``scope.environment`` is signed into the profile and the effect's
approval digest and must match exactly, so a profile signed for ``paper`` cannot charge
a ``live`` effect; but the core takes no view on which environment names are allowed,
so "paper only" is still what the installer signs, not a value ``charge`` refuses.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import re
import sqlite3
import threading
from collections import OrderedDict
from typing import Callable, ContextManager

from .approval import SCOPE_FIELDS
from .breaker import ConnectionBreaker
from .canonical import MAX_INT, canonical, parse
from .errors import Refused
from .signing import key_id, parse_public_key, verify

__all__ = ["PROFILE_SCHEMA", "Authority", "create_ledger"]

PROFILE_SCHEMA = "csl-authority-profile/vNext"
_FIELDS = frozenset({"schema", "profile_id", "issuer", "plugin_id", "artifact_sha256", "scope",
                     "provider", "model_version", "actions", "permission", "not_before",
                     "expires_at", "budgets"})
_BUDGETS = frozenset({"currency", "max_effect_minor", "total_minor"})
_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")
_SCOPE_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# A profile names its provider in the id grammar ``ConnectionBreaker.bind_run`` holds a provider to
# (``breaker._ID``), and its model version by the rule ``bind_run`` records one by, which is asked of
# the store and not written again here. So whatever a run can be bound to (``vendor/model``,
# ``model@rev``, ``opus[1m]``) can be named in a profile, and nothing else can.
_PROVIDER_VALUE = re.compile(r"[A-Za-z0-9_.:@/-]{1,128}")
_KIND = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_LEDGER_MAGIC = "csl-authority-ledger/v0"
_VERIFY_CACHE_SIZE = 256  # positive signature verdicts kept per Authority


def _int(value: object, low: int = 0) -> bool:
    return type(value) is int and value >= low


# Each charge sums prior charges for its profile; index that lookup (no full scan).
_CHARGES_INDEX = "CREATE INDEX IF NOT EXISTS charges_by_profile ON charges (profile_id)"


def _model_version_ok(value: object) -> bool:
    """Whether a profile may name this model version: exactly when a run can be bound to it."""
    return ConnectionBreaker.model_recordable(value)


def _file_identity(path: str) -> tuple:
    """The file a path names now: its device and inode, which a link pointed elsewhere or a file put in its
    place changes, unless the file system gives the replacement the freed inode back (the last charge the
    authority saw, below, tells those apart when the ledger held one)."""
    status = os.stat(path)
    return (status.st_dev, status.st_ino)


# The ledger's latest charge, and one charge by its row id: its row id and values. Charges are only ever added.
_LAST_CHARGE = "SELECT rowid, idempotency_key, profile_id, amount_minor FROM charges ORDER BY rowid DESC LIMIT 1"
_CHARGE_AT = "SELECT rowid, idempotency_key, profile_id, amount_minor FROM charges WHERE rowid = ?"


def _last_charge(db) -> tuple | None:
    """The latest charge the ledger holds now (row id and values), or None. The authority keeps the one it read
    last, at construction and at the start of each charge's transaction: a reopen must find that one again."""
    return db.execute(_LAST_CHARGE).fetchone()


# A last-charge read that raised. Not a row, and not "the ledger held no charge": a reopen must not trust either.
_WITNESS_UNKNOWN = object()


def create_ledger(path: str, anchor_public_key: str) -> None:
    """Installer step: create the ledger bound to one anchor. Refuses if it exists."""
    parse_public_key(anchor_public_key)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute("CREATE TABLE profiles (profile_id TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        db.execute("CREATE TABLE charges (idempotency_key TEXT PRIMARY KEY, profile_id TEXT NOT NULL,"
                   " amount_minor INTEGER NOT NULL CHECK (amount_minor > 0))")
        db.execute(_CHARGES_INDEX)
        db.execute("INSERT INTO meta VALUES ('magic', ?), ('anchor', ?)",
                   (_LEDGER_MAGIC, key_id(anchor_public_key)))
    finally:
        db.close()


class Authority:
    def __init__(self, anchor_public_key: str, ledger_path: str, clock: Callable[[], int],
                 csl_key_ids: frozenset = frozenset()) -> None:
        parse_public_key(anchor_public_key)
        self._anchor_id = key_id(anchor_public_key)
        if self._anchor_id in csl_key_ids:
            raise Refused("anchor_is_csl_key")
        self._anchor = anchor_public_key
        self._clock = clock
        # Resolved once, here, to the file itself: a relative path names the ledger of the working directory of
        # now, and a link names whatever it points at when it is followed. A reopen after a failed rollback must
        # reach this same file, whatever the working directory is then and wherever a link points by then, so
        # the resolved path and the file's identity are kept, and so is the last charge the ledger holds (read
        # again at the start of each charge's transaction), which the reopen must find on the connection it opens
        # (``_connect`` says why). The identity is the path's file just before the open and just after it, the
        # same file both times, or the ledger is ``ledger_invalid``: a file put in the place while it opens is
        # not taken for the one it opened. A bytes or path-like name is taken, as ``create_ledger`` takes it; a
        # name no file can have (a NUL in it, say) is ``ledger_missing``.
        try:
            self._path = os.path.realpath(os.fsdecode(ledger_path))
        except (OSError, ValueError):
            raise Refused("ledger_missing") from None
        try:
            before = _file_identity(self._path)
        except OSError:
            raise Refused("ledger_missing") from None
        self._db = self._connect(self._path, before=before)
        try:
            self._identity = _file_identity(self._path)
        except OSError:
            self._db.close()
            raise Refused("ledger_missing") from None
        if self._identity != before:
            self._db.close()
            raise Refused("ledger_invalid")
        try:
            self._witness = _last_charge(self._db)
        except sqlite3.Error:
            self._db.close()
            raise Refused("ledger_invalid") from None
        self._unusable = False  # set once a failed rollback or ``close`` leaves no usable connection
        self._lock = threading.Lock()
        # Positive Ed25519 verdicts for this instance's fixed anchor, keyed by the exact
        # (signature hex, canonical bytes) pair; LRU, bounded, own lock (free-threaded safe).
        self._verified: OrderedDict[tuple[str, bytes], None] = OrderedDict()
        self._verified_lock = threading.Lock()

    def _verify(self, signature: object, raw: bytes) -> bool:
        """``verify`` against the anchor, remembering only successes for these exact bytes.

        Only the signature verdict is cached. Parsing, field checks, the window, the
        call's plugin/scope/model/action/amount, idempotency, the ``profile_id`` binding
        and spend are still decided per charge from the private parse of ``raw``.
        """
        key = (signature, raw) if type(signature) is str else None
        if key is not None:
            with self._verified_lock:
                if key in self._verified:
                    self._verified.move_to_end(key)
                    return True
        if not verify(self._anchor, signature, raw):
            return False
        if key is not None:
            with self._verified_lock:
                self._verified[key] = None
                self._verified.move_to_end(key)
                while len(self._verified) > _VERIFY_CACHE_SIZE:
                    self._verified.popitem(last=False)
        return True

    def _check_profile(self, envelope: object) -> tuple[dict, str]:
        if type(envelope) is not dict or set(envelope) != {"profile", "signature"}:
            raise Refused("profile_envelope")
        caller_profile = envelope["profile"]
        try:
            raw = canonical(caller_profile)
        except Refused:
            raise Refused("profile_noncanonical") from None
        if not self._verify(envelope["signature"], raw):
            raise Refused("profile_signature_invalid")
        # Every later check, the result and the ledger read a private parse of the exact
        # signed bytes, never the caller's dict: another plugin thread can still change that
        # dict after the check (3.14t, GIL off). The one exception is the ``actions`` shape
        # rule below, which can only refuse.
        try:
            profile = parse(raw)
        except Refused:  # pragma: no cover - canonical() output always round-trips
            raise Refused("profile_noncanonical") from None
        if type(profile) is not dict or set(profile) != _FIELDS or profile["schema"] != PROFILE_SCHEMA:
            raise Refused("profile_fields")
        if profile["issuer"] != "installer" or profile["permission"] != "order":
            raise Refused("profile_issuer_or_permission")
        if type(profile["profile_id"]) is not str or not _ID.fullmatch(profile["profile_id"]):
            raise Refused("profile_id")
        scope = profile["scope"]
        if (type(scope) is not dict or set(scope) != SCOPE_FIELDS
                or any(type(v) is not str or not _SCOPE_VALUE.fullmatch(v) for v in scope.values())):
            raise Refused("profile_scope")
        if (type(profile["provider"]) is not str or not _PROVIDER_VALUE.fullmatch(profile["provider"])
                or not _model_version_ok(profile["model_version"])):
            raise Refused("profile_model")
        # TODO(CSL-738 follow-up): the tuple refusal is a shape rule kept by owner call
        # Nov 11 2:20am PST; review it and possibly change it later (the signed bytes of a
        # tuple and a list are identical, so the private parse cannot tell them apart).
        # One read and one type() check of the caller's object: a racing list<->tuple swap
        # or removal can only change this refusal, never what is charged, which the
        # private parse alone decides.
        if type(caller_profile.get("actions")) is not list:
            raise Refused("profile_actions")
        actions = profile["actions"]
        if (type(actions) is not list or not actions or actions != sorted(set(actions))
                or any(type(a) is not str or not _KIND.fullmatch(a) for a in actions)):
            raise Refused("profile_actions")
        budgets = profile["budgets"]
        if (type(budgets) is not dict or set(budgets) != _BUDGETS
                or type(budgets["currency"]) is not str or not re.fullmatch(r"[A-Z]{3}", budgets["currency"])
                or not _int(budgets["max_effect_minor"], 1) or not _int(budgets["total_minor"], 1)
                or budgets["max_effect_minor"] > budgets["total_minor"]):
            raise Refused("profile_budgets")
        if not _int(profile["not_before"]) or not _int(profile["expires_at"]) \
                or profile["expires_at"] <= profile["not_before"]:
            raise Refused("profile_window")
        return profile, hashlib.sha256(raw).hexdigest()

    def _connect(self, ledger_path: str, identity: tuple | None = None,
                 witness: tuple | None = None, *, before: tuple | None = None) -> sqlite3.Connection:
        """The ledger, opened and checked to be this anchor's; refused, with nothing left open, if not.

        The daemon never creates the ledger, here or when a failed rollback opens it again: a path with no file
        is ``ledger_missing`` before anything is opened, and the open itself is read-write only (SQLite's
        ``mode=rw``), so a file removed after that check is refused too and is never created empty.

        With ``identity`` (the reopen's), the ledger opened must be the one the authority holds, and nothing is
        written to it before that is known (the first open adds the charges index; the reopen adds nothing).
        The first open is passed the identity stat'ed before it and compares the path again after the meta check
        and before that index: a different file is ``ledger_invalid``, the connection is shut, and the index is
        not written onto it.
        Two checks, each ``ledger_invalid`` with the connection shut: the path must still name the file of
        ``identity`` (its device and inode), and the opened connection itself must hold ``witness``, the last
        charge the authority saw, unchanged (charges are only ever added). The second check reads the
        connection, not the path, so it holds when a file is swapped in for the open and back out before the
        stat, and when the file system gives a file put in the ledger's place the freed inode back: such a
        ledger, even of the same anchor, lacks that charge. The witness is read at the start of each charge's
        transaction, under its write lock and before its guard is entered, so every charge committed before a
        failure that leads here is at or before it; a ledger that held no charge then has none to lose. A copy
        of the ledger made after that charge holds it, as it holds every charge, and so does a ledger rewritten
        to keep it: an actor who can copy or rewrite the ledger is the same-uid limit the module states (B17)."""
        if not os.path.isfile(ledger_path):
            raise Refused("ledger_missing")
        db = None
        try:
            uri = pathlib.Path(os.path.abspath(ledger_path)).as_uri() + "?mode=rw"
            db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=30000")
            meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error:
            if db is not None:
                db.close()
            raise Refused("ledger_invalid") from None
        if meta.get("magic") != _LEDGER_MAGIC or meta.get("anchor") != self._anchor_id:
            db.close()
            raise Refused("ledger_anchor_mismatch")
        if identity is None:
            # ``before`` is the path's identity just before this open. Compare again before the index write: that
            # write commits at once, so a file put in the place before connect would otherwise keep the index.
            if before is not None:
                try:
                    current = _file_identity(ledger_path)
                except OSError:
                    db.close()
                    raise Refused("ledger_missing") from None
                if current != before:
                    db.close()
                    raise Refused("ledger_invalid")
            try:
                db.execute(_CHARGES_INDEX)  # ledgers created before the index gain it here
            except sqlite3.Error:
                db.close()
                raise Refused("ledger_invalid") from None
            return db
        try:
            same = _file_identity(ledger_path) == identity
            if same and witness is not None:
                same = db.execute(_CHARGE_AT, (witness[0],)).fetchone() == witness
        except (OSError, sqlite3.Error):
            same = False
        if not same:
            db.close()
            raise Refused("ledger_invalid")
        return db

    def _roll_back(self) -> None:
        """Roll a charge's write transaction back. If ROLLBACK itself raises, the connection may still hold
        that transaction, and every later charge would wait for it and fail: so the connection is closed,
        which discards what the transaction wrote, and the ledger is opened again by ``_connect``: the same
        ledger the authority opened at construction (its resolved path, that file's identity, and the last
        charge the authority saw, found on the connection opened, so a link pointed elsewhere or a ledger put in
        its place is refused, as ``_connect`` says), with every check of the first open (the ledger must still
        exist; nothing is created). A last-charge read that did not return leaves the witness unknown, and this
        open is then not attempted: the ledger stays out of use. This is about the module's own connection: an object
        someone takes from the private ``_db`` (a cursor, say) is outside what the repair promises.

        Until that open succeeds the ledger is out of use, and every later charge is refused
        ``ledger_invalid`` at once and writes nothing. If the close itself raises, the connection is kept,
        since it may still hold the transaction, and nothing is opened beside it; if the ledger cannot be
        opened again, the closed connection is kept. The rollback's error propagates from here when the
        reopen succeeds or is refused with a code; any other error of the reopen (an interruption, say)
        propagates in its place, with the ledger out of use all the same. What the caller of ``charge`` sees
        is said there."""
        try:
            self._db.execute("ROLLBACK")
        except BaseException:
            self._unusable = True
            closed = False
            with contextlib.suppress(Exception):
                self._db.close()
                closed = True
            if closed and self._witness is not _WITNESS_UNKNOWN:
                with contextlib.suppress(Refused):
                    self._db = self._connect(self._path, self._identity, self._witness)
                    self._unusable = False
            raise

    def _require_usable(self) -> None:
        """``ledger_invalid`` once a failed rollback or ``close`` has left this authority no usable ledger."""
        if self._unusable:
            raise Refused("ledger_invalid")

    def charge(self, envelope: object, *, plugin_id: str, artifact_sha256: str, scope: dict,
               kind: str, provider: str, model_version: str, currency: str, amount_minor: object,
               idempotency_key: str, commit_guard: ContextManager[object] | None = None) -> dict:
        """Charge one effect against the profile. Refuses before writing anything if any check fails.

        ``commit_guard``, when given, is a context manager entered once the ledger's write
        transaction has begun, after the signature and every check of this call that needs no
        ledger read, and held until that transaction has committed or rolled back. If entering it
        raises, or anything raises before ``COMMIT``, the transaction rolls back and nothing is
        written. Its exit runs after ``COMMIT`` or ``ROLLBACK``: if it raises after a ``COMMIT``,
        the charge stands and the error reaches the caller, and a retry with the same idempotency
        key is refused ``effect_replayed``, never charged twice. If its exit raises on the way out
        of a refusal or an error, its error reaches the caller in their place, with the refusal or
        error as its context, and nothing is written. If its exit swallows a refusal or an error,
        ``charge`` raises it all the same: a guard can refuse a charge, never turn a refusal into a
        success. If the rollback itself raises, its error reaches the caller in place of the charge's,
        unless the guard's exit swallows it, when the charge's own error is raised (a reopen that fails other
        than with a refusal raises its own error instead, as ``_roll_back`` says); the connection is
        closed (which discards what the transaction wrote) and the ledger opened again, so no later charge
        inherits that transaction. If the last-charge read did not return, the ledger is not opened again.
        If that cannot be done (the close raises, the witness is unknown, or the ledger cannot be opened
        again), every later charge is refused ``ledger_invalid`` at once, and so is every charge after
        ``close``. A guard must call neither ``charge`` nor ``close``: the ledger's lock is held while it runs,
        so either call would wait for itself. The core passes its commit section with the halt recheck (R4-1),
        whose exit only releases the fence; so signature verification never runs under it and a halt never
        waits for one.
        """
        profile, digest = self._check_profile(envelope)
        now = self._clock()
        if type(now) is not int:
            raise Refused("clock_invalid")
        if now < profile["not_before"]:
            raise Refused("profile_not_yet_valid")
        if now >= profile["expires_at"]:
            raise Refused("profile_expired")
        if (profile["plugin_id"], profile["artifact_sha256"]) != (plugin_id, artifact_sha256):
            raise Refused("profile_plugin_mismatch")
        if profile["scope"] != scope:
            raise Refused("profile_scope_mismatch")
        if (profile["provider"], profile["model_version"]) != (provider, model_version):
            raise Refused("profile_model_mismatch")
        if kind not in profile["actions"]:
            raise Refused("profile_action_not_permitted")
        if currency != profile["budgets"]["currency"]:
            raise Refused("profile_currency_mismatch")
        if not _int(amount_minor, 1) or amount_minor > MAX_INT:
            raise Refused("amount_invalid")
        if amount_minor > profile["budgets"]["max_effect_minor"]:
            raise Refused("budget_max_effect_exceeded")
        if type(idempotency_key) is not str or not _ID.fullmatch(idempotency_key):
            raise Refused("idempotency_key_invalid")
        failure: BaseException | None = None
        with self._lock, contextlib.ExitStack() as guarded:
            self._require_usable()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # First, under the write lock and before the guard (whose entry may refuse): every charge committed
                # until now is at or before this one, and a reopen after a failure from here on must find it.
                # Unknown until the read returns, so a read that raises cannot reopen on an older witness.
                self._witness = _WITNESS_UNKNOWN
                self._witness = _last_charge(self._db)
                if commit_guard is not None:
                    guarded.enter_context(commit_guard)
                row = self._db.execute("SELECT digest FROM profiles WHERE profile_id=?",
                                       (profile["profile_id"],)).fetchone()
                if row is None:
                    self._db.execute("INSERT INTO profiles VALUES (?, ?)", (profile["profile_id"], digest))
                elif row[0] != digest:
                    raise Refused("profile_id_rebound")
                if self._db.execute("SELECT 1 FROM charges WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone():
                    raise Refused("effect_replayed")
                spent = self._db.execute("SELECT COALESCE(SUM(amount_minor), 0) FROM charges"
                                         " WHERE profile_id=?", (profile["profile_id"],)).fetchone()[0]
                if spent + amount_minor > profile["budgets"]["total_minor"]:
                    raise Refused("budget_total_exceeded")
                inserted = self._db.execute("INSERT INTO charges VALUES (?, ?, ?)",
                                            (idempotency_key, profile["profile_id"], amount_minor)).lastrowid
                self._db.execute("COMMIT")
                self._witness = (inserted, idempotency_key, profile["profile_id"], amount_minor)
            except BaseException as error:
                failure = error
                self._roll_back()
                raise
        if failure is not None:  # only a guard whose exit swallowed it gets here: nothing was written
            raise failure
        return {"profile_id": profile["profile_id"], "spent_minor": spent + amount_minor,
                "remaining_minor": profile["budgets"]["total_minor"] - spent - amount_minor}

    def close(self) -> None:
        """Shut the ledger's connection. The authority is out of use first, so every later charge is refused
        ``ledger_invalid``, even when the connection's own close raises (that error reaches the caller)."""
        with self._lock:
            self._unusable = True
            self._db.close()
