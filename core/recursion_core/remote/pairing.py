"""Pairing-state models (lane REMOTE, first slice): a one-time pairing offer and the limit on failed tries.

Pure and in memory: nothing here mints a token, writes a file or answers a request, and no daemon reads it.
The device store and the phone door of this lane's later slices keep these rules; the one token
authority (mint, hash, revoke) is lane CONTROL's credentials, not this file. What a successful
redemption returns is a ``PairingGrant``: what to ask that authority for, never a credential.

The rules, each with a test that fails without it:

* A device's scopes are a non-empty subset of read and stop (watch and Stop). This package has no
  passkey, so run and approve are refused. Manage is never granted by pairing
  (PRODUCT.md 1017-1018, O34; grill Q40), and an unknown scope is refused.
* An offer is bound when it is issued to its scopes, its label and its deadline. Redeeming it cannot
  widen or change any of them: the redeemer sends only the offer id and the code.
* The code is 128 random bits, shown once. Only its SHA-256 is kept, and it is compared in constant
  time. (A QR code is enrollment, not authorization: the code proves only that its holder saw it.)
* The deadline is two minutes after issue and is never extended. At or after it the offer is expired.
* An offer is redeemed at most once, also when redemptions race; a reused code is refused.
* Five wrong codes lock the offer for good; a locked or cancelled offer redeems nothing.
* At most eight offers are open at once.
* Failed tries are limited per source in a sliding window, and all sources together too.

Nothing here asks for a second factor, at pairing or per approval: there is no passkey in this package,
so a device is never given a scope that would need one.

What this package keeps is the settled values of record 20261115-csl238-harness-dr-09-phone-pairing-01,
named once below: a pairing lasts at most 30 days, and 7 days if unused; a dangerous action needs a
fresh assertion per action, valid 60 seconds; without a passkey a device may only watch and Stop.
There is no passkey here, so every device is passkey-less. A 90-day cap, a 15-minute step-up and a
TOTP fallback are not these values and are not built.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

# Settled pairing values of csl-research record 20261115-csl238-harness-dr-09-phone-pairing-01.
# One block, and the only place these numbers are named. A pairing lasts at most 30 days, and
# 7 days if unused. A dangerous action needs a fresh assertion per action, valid 60 seconds.
# Without a passkey a paired device may watch and Stop only. This package has no passkey, so
# every device is passkey-less and DEVICE_SCOPES is read and stop only. No action here was
# marked dangerous; run and approve are treated as the dangerous actions.
PAIRING_MAX_AGE_S = 30 * 24 * 60 * 60
PAIRING_UNUSED_AGE_S = 7 * 24 * 60 * 60
DANGEROUS_ASSERTION_TTL_S = 60.0
DEVICE_SCOPES = ("read", "stop")
DANGEROUS_ACTIONS = ("run", "approve")
MANAGE = "manage"
OFFER_TTL_S = 120.0
CODE_BYTES = 16
MAX_WRONG_CODES = 5
MAX_OPEN_OFFERS = 8

OPEN, REDEEMED, EXPIRED, LOCKED, CANCELLED = "open", "redeemed", "expired", "locked", "cancelled"
# Printable ASCII, no leading or trailing space: no control or bidirectional characters in a label.
_LABEL = re.compile(r"[!-~](?:[ -~]{0,62}[!-~])?")
# The pairing origin, whole: https://, a DNS name (labels of letters, digits and inner hyphens) or a
# bracketed IPv6 address, an optional port of 1 to 5 digits, an optional final slash. Nothing else: no
# user, path, query, fragment, whitespace or control character.
_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ORIGIN = re.compile(rf"https://(?P<host>{_DNS_LABEL}(?:\.{_DNS_LABEL})*|\[(?P<ipv6>[0-9A-Fa-f:.]+)\])"
                     r"(?::(?P<port>[0-9]{1,5}))?/?")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class PairingRefused(Exception):
    """A pairing step was refused. ``code`` is a typed word; ``detail`` is for the operator."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def device_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """A paired device's scopes, checked and in canonical order.

    ``manage``, and the dangerous actions ``run`` and ``approve``, are ``scope_refused``.
    There is no passkey in this package, so a device may only watch (``read``) and Stop
    (``stop``).
    """
    if isinstance(scopes, (str, bytes)):
        raise PairingRefused("scope_invalid", "scopes are a list of words, not one string")
    try:
        items = tuple(scopes)
    except TypeError:
        raise PairingRefused("scope_invalid", "scopes are a list of words") from None
    if not items:
        raise PairingRefused("scope_invalid", "a device needs at least one scope")
    for scope in items:
        if scope == MANAGE:
            raise PairingRefused("scope_refused", "pairing never grants manage")
        if scope in DANGEROUS_ACTIONS:
            raise PairingRefused("scope_refused", "without a passkey a device may only watch and stop")
        if scope not in DEVICE_SCOPES:
            raise PairingRefused("scope_invalid", f"unknown scope {scope!r}")
    if len(set(items)) != len(items):
        raise PairingRefused("scope_invalid", "a scope is named twice")
    return tuple(scope for scope in DEVICE_SCOPES if scope in items)


def pairing_lapsed(paired_at: float, last_accepted_at: float | None, now: float) -> bool:
    """True at the 30-day cap, or at 7 days with no accepted call since pairing or since the last one.

    ``last_accepted_at is None`` means there has been no accepted call since pairing, so the
    unused bound is measured from ``paired_at``. At the bound the pairing has lapsed.
    """
    if now >= paired_at + PAIRING_MAX_AGE_S:
        return True
    idle_from = paired_at if last_accepted_at is None else last_accepted_at
    return now >= idle_from + PAIRING_UNUSED_AGE_S


def device_label(label: object) -> str:
    """A device's name as the owner typed it: 1 to 64 printable ASCII characters, no edge spaces."""
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise PairingRefused("label_invalid", "a label is 1 to 64 printable ASCII characters")
    return label


def _urlsafe(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _digest(code: str) -> bytes:
    return hashlib.sha256(code.encode("ascii")).digest()


def pairing_link(origin: str, offer_id: str, code: str) -> str:
    """What the QR code carries: the overlay's HTTPS origin, then the offer id and the code in the fragment.

    The fragment is never sent in a request line, so neither the overlay's proxy nor a server log sees
    the code. The origin must be ``https://host[:port]`` with nothing else: a DNS name or a bracketed IPv6
    address, and a port from 1 to 65535 when one is given. The ``/pair`` path is provisional: the phone door
    that answers it is a later slice.
    """
    match = _ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
    if match is None or len(match.group("host")) > 255:
        raise PairingRefused("origin_invalid", "the pairing origin is https://host[:port] and nothing else")
    if match.group("ipv6") is not None:
        try:
            ipaddress.IPv6Address(match.group("ipv6"))
        except ValueError:
            raise PairingRefused("origin_invalid", "the bracketed address is not an IPv6 address") from None
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        raise PairingRefused("origin_invalid", "a port is 1 to 65535")
    for value in (offer_id, code):
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise PairingRefused("offer_invalid", "offer ids and codes are URL-safe base64")
    authority = match.group("host") + (f":{port}" if port is not None else "")
    return f"https://{authority}/pair#offer={offer_id}&code={code}"


@dataclass(frozen=True)
class IssuedOffer:
    """What the owner is shown once when an offer is issued; the code is not kept anywhere else."""

    offer_id: str
    code: str
    scopes: tuple[str, ...]
    label: str
    deadline: float


@dataclass(frozen=True)
class PairingGrant:
    """A redeemed offer: what to ask the token authority for. It is not a credential."""

    offer_id: str
    device_id: str
    label: str
    scopes: tuple[str, ...]
    redeemed_at: float


@dataclass
class _Offer:
    offer_id: str
    code_digest: bytes
    scopes: tuple[str, ...]
    label: str
    issued_at: float
    deadline: float
    state: str = OPEN
    wrong: int = 0
    redeemed_at: float | None = None


class AttemptLimiter:
    """Failed tries in a sliding window: at most ``per_source`` from one source, ``overall`` from all.

    Not thread-safe on its own; ``OfferBook`` calls it under its lock. At most ``max_sources`` sources are
    tracked; the least recently seen is dropped first, and the overall limit still holds for all of them.
    """

    def __init__(self, *, per_source: int = 5, overall: int = 30, window_s: float = 60.0,
                 max_sources: int = 1024) -> None:
        if per_source < 1 or overall < 1 or window_s <= 0 or max_sources < 1:
            raise ValueError("limits must be positive")
        self.per_source, self.overall, self.window_s, self.max_sources = per_source, overall, window_s, max_sources
        self._by_source: OrderedDict[str, deque[float]] = OrderedDict()
        self._all: deque[float] = deque()

    def _prune(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self.window_s:
            times.popleft()

    def allow(self, source: str, now: float) -> bool:
        self._prune(self._all, now)
        if len(self._all) >= self.overall:
            return False
        times = self._by_source.get(source)
        if times is None:
            return True
        self._prune(times, now)
        return len(times) < self.per_source

    def record_failure(self, source: str, now: float) -> None:
        self._all.append(now)
        times = self._by_source.pop(source, None)
        if times is None:
            times = deque()
        times.append(now)
        self._by_source[source] = times  # most recently seen last
        while len(self._by_source) > self.max_sources:
            self._by_source.popitem(last=False)


@dataclass
class OfferBook:
    """The open pairing offers, in memory. Every decision is made under one lock.

    ``clock`` is a monotonic clock (seconds); ``random_bytes(n)`` is the random source. Both are
    injectable so that tests set the time and the codes instead of waiting or guessing.
    """

    clock: Callable[[], float] = time.monotonic
    random_bytes: Callable[[int], bytes] = secrets.token_bytes
    ttl_s: float = OFFER_TTL_S
    max_wrong: int = MAX_WRONG_CODES
    max_open: int = MAX_OPEN_OFFERS
    limiter: AttemptLimiter = field(default_factory=AttemptLimiter)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._offers: dict[str, _Offer] = {}

    def issue(self, *, scopes: Iterable[str], label: str) -> IssuedOffer:
        """A new offer for a device with exactly ``scopes``; its code is returned once and kept only hashed."""
        checked = device_scopes(scopes)
        name = device_label(label)
        with self._lock:
            now = self.clock()
            if sum(1 for offer in self._offers.values() if self._state(offer, now) == OPEN) >= self.max_open:
                raise PairingRefused("pairing_busy", f"at most {self.max_open} offers are open at once")
            offer_id = _urlsafe(self.random_bytes(9))
            code = _urlsafe(self.random_bytes(CODE_BYTES))
            if offer_id in self._offers:
                raise PairingRefused("pairing_busy", "offer id collision")
            self._offers[offer_id] = _Offer(offer_id, _digest(code), checked, name, now, now + self.ttl_s)
            return IssuedOffer(offer_id, code, checked, name, now + self.ttl_s)

    def redeem(self, offer_id: object, code: object, *, source: str) -> PairingGrant:
        """Redeem an offer once. Every refusal but the rate limit counts as a failed try from ``source``.

        ``source`` is the door's name for where the try came from (the overlay peer's address, say): a string.
        """
        if not isinstance(source, str):
            raise PairingRefused("source_invalid", "a source is a string")
        with self._lock:
            now = self.clock()
            if not self.limiter.allow(source, now):
                raise PairingRefused("pairing_rate_limited")
            offer = self._offers.get(offer_id) if isinstance(offer_id, str) else None
            if offer is None:
                self._fail(source, now, "pairing_code_invalid")
            state = self._state(offer, now)
            if state != OPEN:
                self._fail(source, now, {REDEEMED: "pairing_code_used", EXPIRED: "pairing_code_expired",
                                         LOCKED: "pairing_offer_locked", CANCELLED: "pairing_offer_cancelled"}[state])
            if not self._code_matches(offer, code):
                offer.wrong += 1
                if offer.wrong >= self.max_wrong:
                    offer.state = LOCKED
                self._fail(source, now, "pairing_code_invalid")
            # The device id is drawn before the offer is marked redeemed, inside the window a racing redemption
            # would use; tests/test_pairing.py stands in that window through ``random_bytes``.
            device_id = "dev_" + _urlsafe(self.random_bytes(12))
            offer.state, offer.redeemed_at = REDEEMED, now
            return PairingGrant(offer.offer_id, device_id, offer.label, offer.scopes, now)

    def cancel(self, offer_id: str) -> bool:
        """End an open offer; ``False`` when it was not open."""
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None or self._state(offer, self.clock()) != OPEN:
                return False
            offer.state = CANCELLED
            return True

    def state(self, offer_id: str) -> str | None:
        with self._lock:
            offer = self._offers.get(offer_id)
            return None if offer is None else self._state(offer, self.clock())

    def _state(self, offer: _Offer, now: float) -> str:
        if offer.state == OPEN and now >= offer.deadline:
            return EXPIRED
        return offer.state

    @staticmethod
    def _code_matches(offer: _Offer, code: object) -> bool:
        # How long a posted code may be is bounded by the door's request size, not here.
        if not isinstance(code, str) or not code.isascii():
            return False
        return hmac.compare_digest(_digest(code), offer.code_digest)

    def _fail(self, source: str, now: float, code: str) -> None:
        self.limiter.record_failure(source, now)
        raise PairingRefused(code)


@dataclass
class _Assertion:
    assertion_id: str
    action: str
    issued_at: float
    used: bool = False


@dataclass
class AssertionBook:
    """A fresh assertion for one dangerous action, valid for ``DANGEROUS_ASSERTION_TTL_S`` seconds.

    No action in this package was marked dangerous. ``run`` and ``approve`` are treated as the
    dangerous actions. Each assertion authorizes its own action once, and only while ``now`` is
    still before ``issued_at + DANGEROUS_ASSERTION_TTL_S`` (at 60 seconds, and later, it is
    refused). Replaying it, including for a second action, is refused. The assertion does not
    grant a scope: a passkey-less device still may not run or approve.

    ``clock`` is a monotonic clock (seconds). Tests inject one; nothing here reads the wall clock.
    """

    clock: Callable[[], float] = time.monotonic
    random_bytes: Callable[[int], bytes] = secrets.token_bytes

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, _Assertion] = {}

    def issue(self, action: str) -> str:
        """A new assertion id for one dangerous action. Watch and Stop take none."""
        if not isinstance(action, str) or action not in DANGEROUS_ACTIONS:
            raise PairingRefused("action_not_dangerous", "only a dangerous action takes an assertion")
        with self._lock:
            now = self.clock()
            assertion_id = _urlsafe(self.random_bytes(9))
            if assertion_id in self._by_id:
                raise PairingRefused("assertion_busy", "assertion id collision")
            self._by_id[assertion_id] = _Assertion(assertion_id, action, now)
            return assertion_id

    def accept(self, assertion_id: object, action: str) -> None:
        """Consume ``assertion_id`` for ``action`` once, inside the 60-second window.

        The clock is read under the lock, before the assertion is marked used, so a second
        caller either waits and is then refused or does not run inside this one.
        """
        if not isinstance(action, str):
            raise PairingRefused("action_invalid", "an action is a string")
        with self._lock:
            now = self.clock()
            record = self._by_id.get(assertion_id) if type(assertion_id) is str else None
            if record is None:
                raise PairingRefused("assertion_invalid")
            if now >= record.issued_at + DANGEROUS_ASSERTION_TTL_S:
                raise PairingRefused("assertion_expired")
            if record.used:
                raise PairingRefused("assertion_replayed")
            if record.action != action:
                raise PairingRefused("assertion_wrong_action")
            record.used = True
