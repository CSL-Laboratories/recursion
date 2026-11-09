"""Domain-neutral approval request and prompt text (O74; PRODUCT.md 4.2, I-42).

The core draws every approval (grill Q24). An ``ApprovalRequest`` holds:

* the effect's canonical bytes and their SHA-256 digest. The digest is always computed
  here from the effect; a caller cannot pass one in, and a request whose stored digest
  does not match its bytes refuses to construct. The prompt and the approval scope bind
  to that digest, so a plugin cannot show one digest and have another approved;
* display fields: ``(label, value)`` pairs the caller supplies. The core treats them as
  opaque, labelled, untrusted text. It never parses them, and they never change the
  digest;
* the expiry (integer UNIX seconds) and the requester principal.

``render_prompt`` writes a fixed header in a fixed sequence, then the caller fields, each on
its own line behind a ``| `` gutter with control, bidi and zero-width characters shown as
``<U+XXXX>`` markers. A caller field therefore cannot forge a header line. The core-written
text names no consumer domain; ``CORE_TEXT`` lists every fixed string so tests can check.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from .canonical import canonical, digest as canonical_digest
from .errors import Refused

__all__ = ["CORE_TEXT", "MAX_FIELDS", "MAX_VALUE_CHARS", "RESERVED_LABELS",
           "ApprovalRequest", "render_prompt", "sanitize"]

MAX_FIELDS = 32
MAX_VALUE_CHARS = 512
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PRINCIPAL = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

# Labels a caller may not use, so no caller field reads like a header line.
RESERVED_LABELS = frozenset({"digest", "sha256", "requester", "expires", "expiry",
                             "request", "request_id", "approve", "deny", "choice"})

_HEADER = "RECURSION APPROVAL REQUEST"
_FIELDS_OPEN = "-- caller fields: untrusted, shown as given, not interpreted by Recursion --"
_FIELDS_CLOSE = "-- end of caller fields --"
_NO_FIELDS = "(no caller fields)"
_CHOICES = "Approve only if the digest above is the one you expect. 1 = approve, 2 = deny. " \
           "Type the number and Enter; nothing defaults to approve."

CORE_TEXT = (_HEADER, "request id: ", "digest (sha256): ", "requester: ", "expires: ",
             _FIELDS_OPEN, _FIELDS_CLOSE, _NO_FIELDS, _CHOICES, "| ", ": ")

# Characters shown as markers: C0/C1 controls, bidi controls and isolates, zero-width
# characters, line and paragraph separators, and the BOM.
_MARK = frozenset(
    [*range(0x00, 0x20), *range(0x7F, 0xA0), 0x061C, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
     *range(0x202A, 0x202F), 0x2028, 0x2029, 0x2060, *range(0x2066, 0x206A), 0xFEFF])


def sanitize(text: str) -> str:
    """Return ``text`` with every marked or format (Cf) character shown as ``<U+XXXX>``."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in _MARK or unicodedata.category(ch) in ("Cf", "Cc", "Zl", "Zp"):
            out.append(f"<U+{cp:04X}>")
        else:
            out.append(ch)
    return "".join(out)


def _check_fields(fields: object) -> tuple[tuple[str, str], ...]:
    if type(fields) not in (list, tuple):
        raise Refused("display_fields", "not a sequence")
    if len(fields) > MAX_FIELDS:
        raise Refused("display_fields", "too many")
    seen: set[str] = set()
    out = []
    for pair in fields:
        if type(pair) not in (list, tuple) or len(pair) != 2:
            raise Refused("display_fields", "not a (label, value) pair")
        label, value = pair
        if type(label) is not str or not _LABEL.fullmatch(label):
            raise Refused("display_label", "label")
        if label in RESERVED_LABELS:
            raise Refused("display_label", "reserved")
        if label in seen:
            raise Refused("display_label", "duplicate")
        if type(value) is not str or len(value) > MAX_VALUE_CHARS:
            raise Refused("display_value", "value")
        seen.add(label)
        out.append((label, value))
    return tuple(out)


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    effect_bytes: bytes
    digest: str
    display: tuple[tuple[str, str], ...]
    expires_at: int
    requester: str

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or not _REQUEST_ID.fullmatch(self.request_id):
            raise Refused("request_id")
        if type(self.effect_bytes) is not bytes:
            raise Refused("effect_bytes")
        if type(self.digest) is not str or not _HEX64.fullmatch(self.digest):
            raise Refused("digest_form")
        # Recompute: the digest is the core's, never the caller's.
        if hashlib.sha256(self.effect_bytes).hexdigest() != self.digest:
            raise Refused("digest_mismatch")
        if _check_fields(self.display) != self.display:
            raise Refused("display_fields")
        if type(self.expires_at) is not int or self.expires_at < 0:
            raise Refused("expires_at")
        if type(self.requester) is not str or not _PRINCIPAL.fullmatch(self.requester):
            raise Refused("requester")

    @classmethod
    def create(cls, effect: object, display: object, requester: str, expires_at: int,
               request_id: str | None = None) -> "ApprovalRequest":
        """Build a request; the digest is taken over ``effect``'s canonical bytes."""
        try:
            raw = canonical(effect)
        except Refused:
            raise Refused("effect_noncanonical") from None
        return cls(request_id=request_id or secrets.token_hex(16), effect_bytes=raw,
                   digest=canonical_digest(effect), display=_check_fields(display),
                   expires_at=expires_at, requester=requester)


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_prompt(request: ApprovalRequest) -> str:
    """Return the prompt text the core draws for ``request``."""
    if type(request) is not ApprovalRequest:
        raise Refused("request_type")
    lines = [
        _HEADER,
        f"request id: {request.request_id}",
        f"digest (sha256): {request.digest}",
        f"requester: {request.requester}",
        f"expires: {_iso(request.expires_at)} ({request.expires_at})",
        _FIELDS_OPEN,
    ]
    if request.display:
        lines.extend(f"| {label}: {sanitize(value)}" for label, value in request.display)
    else:
        lines.append(_NO_FIELDS)
    lines.append(_FIELDS_CLOSE)
    lines.append(_CHOICES)
    return "\n".join(lines) + "\n"
