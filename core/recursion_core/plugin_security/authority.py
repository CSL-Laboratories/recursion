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

import hashlib
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from typing import Callable

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
        if not os.path.isfile(ledger_path):
            raise Refused("ledger_missing")
        self._db = None
        try:
            self._db = sqlite3.connect(ledger_path, isolation_level=None, check_same_thread=False,
                                       timeout=30)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=30000")
            meta = dict(self._db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error:
            if self._db is not None:
                self._db.close()
            raise Refused("ledger_invalid") from None
        if meta.get("magic") != _LEDGER_MAGIC or meta.get("anchor") != self._anchor_id:
            self._db.close()
            raise Refused("ledger_anchor_mismatch")
        try:
            self._db.execute(_CHARGES_INDEX)  # ledgers created before the index gain it here
        except sqlite3.Error:
            self._db.close()
            raise Refused("ledger_invalid") from None
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

    def charge(self, envelope: object, *, plugin_id: str, artifact_sha256: str, scope: dict,
               kind: str, provider: str, model_version: str, currency: str, amount_minor: object,
               idempotency_key: str) -> dict:
        """Charge one effect against the profile. Refuses before writing anything if any check fails."""
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
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
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
                self._db.execute("INSERT INTO charges VALUES (?, ?, ?)",
                                 (idempotency_key, profile["profile_id"], amount_minor))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {"profile_id": profile["profile_id"], "spent_minor": spent + amount_minor,
                "remaining_minor": profile["budgets"]["total_minor"] - spent - amount_minor}

    def close(self) -> None:
        with self._lock:
            self._db.close()
