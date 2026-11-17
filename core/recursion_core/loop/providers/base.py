"""The provider interface every model client implements (lane LOOP; LOOP-OAI implements it too).

One call: ``stream_turn(request, on_event=..., cancel=...)`` sends the conversation and the tool list, streams
the model's reply, and returns a ``TurnResponse``: the assistant's parts in order (text, tool calls, and any
vendor-only block that must be sent back unchanged), a vendor-neutral stop reason, the model that served the
call, and the usage of that one call with each measurement marked. Failures are typed exceptions, never text
the caller has to read: ``LimitError`` (with the vendor's retry time when it gives one), ``OverloadError``,
``AuthError`` and the rest below.

Vendor specifics stay behind this interface: wire formats, headers, model names and stop-reason spellings are
the provider module's business. A provider never reads a key from anywhere but what it was constructed with,
and never puts a key into an exception, an event or a return value: text the vendor sends back that holds the
key is scrubbed (streamed text too, holding back a fragment that could be the start of the key until the next
fragment shows it is not, across text blocks too), the text parts of one reply are scrubbed as one text
(``scrub_across``: a key split across two text blocks is found in their join), and a vendor block that cannot
be changed without breaking it (a signed thinking block), or a tool call whose id or name holds the key (an id
pairs a call with its result and cannot be scrubbed), is refused with ``ProtocolError``, never altered.

A failed or interrupted call still has a usage record: every ``ProviderError`` and ``Cancelled`` carries
``usage`` (a ``Usage`` of what the provider had measured when the call ended: counts the vendor had reported
are ``known``, a count that was still growing is a ``lower_bound``, the rest ``unknown``; None when nothing was
measured, which the loop records as all unknown, never as 0) and ``served_model`` (None when unknown).

Values a provider returns keep the wire's limits (event.v1): token counts are whole numbers of 0 or more,
every measurement is finite, a call id is 1 to 128 characters and a tool name 1 to 256, neither with a CR or a
LF. A provider refuses a reply that breaks them with ``ProtocolError``; the dataclasses below refuse such
values with ``ValueError``. A tool input nested more than ``MAX_INPUT_DEPTH`` objects or arrays deep is kept as
input that did not parse (``input_error`` is ``INPUT_TOO_DEEP``) and its raw text is dropped: no tool takes one,
walking it could reach Python's recursion limit wherever the loop scrubs, checks or writes it, and text that was
never decoded and scrubbed could still spell a secret with JSON escapes. So is an input holding NaN or an
infinity (``INPUT_NOT_FINITE``), which Python's decoder takes but JSON cannot write, or an integer too long for
Python to write. Text parts and tool results hold text; the dataclasses refuse anything else. Text UTF-8
cannot encode (a lone surrogate, which a
``\\ud800`` escape gives Python's decoder) can be neither written on a line nor sent in a request:
``holds_unencodable`` finds it, the loop refuses a reply that holds it, and a user message that holds it is
refused before it is taken.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, Union

# ---------------------------------------------------------------------------------------------------------
# Measurements (the shape of event.v1's token_count and dollar_amount: a value is never 0 because unknown).

KNOWN, LOWER_BOUND, UNKNOWN = "known", "lower_bound", "unknown"
OBSERVED, MODELLED, INFERRED, UNAVAILABLE = "observed", "modelled", "inferred", "unavailable"


@dataclass(frozen=True)
class Measurement:
    value: int | float | None
    quality: str
    provenance: str

    def __post_init__(self) -> None:
        if self.quality not in (KNOWN, LOWER_BOUND, UNKNOWN):
            raise ValueError("measurement quality")
        if self.quality == UNKNOWN:
            if self.value is not None or self.provenance != UNAVAILABLE:
                raise ValueError("an unknown measurement has no value and provenance 'unavailable'")
        else:
            if (self.value is None or isinstance(self.value, bool) or not isinstance(self.value, (int, float))
                    or not math.isfinite(self.value) or self.value < 0):
                raise ValueError("a known measurement has a finite value of 0 or more")
            if self.provenance not in (OBSERVED, MODELLED, INFERRED):
                raise ValueError("measurement provenance")

    def to_wire(self) -> dict:
        return {"value": self.value, "quality": self.quality, "provenance": self.provenance}


def observed(value: int | float) -> Measurement:
    return Measurement(value, KNOWN, OBSERVED)


def unknown() -> Measurement:
    return Measurement(None, UNKNOWN, UNAVAILABLE)


def at_least(value: int | float) -> Measurement:
    """A count that was still growing when it was read (a call that ended early): the true value is at least
    this."""

    return Measurement(value, LOWER_BOUND, OBSERVED)


@dataclass(frozen=True)
class Usage:
    """One provider call's usage. ``input_tokens`` counts the uncached input only, as Anthropic reports it;
    cache reads and writes are their own fields. ``reasoning_tokens`` is the part of ``output_tokens`` the
    vendor reports as reasoning (unknown when it does not split it out)."""

    input_tokens: Measurement = field(default_factory=unknown)
    output_tokens: Measurement = field(default_factory=unknown)
    reasoning_tokens: Measurement = field(default_factory=unknown)
    cache_read_tokens: Measurement = field(default_factory=unknown)
    cache_write_tokens: Measurement = field(default_factory=unknown)
    cost_usd: Measurement = field(default_factory=unknown)

    def __post_init__(self) -> None:
        for name in USAGE_FIELDS:
            m = getattr(self, name)
            if not isinstance(m, Measurement):
                raise ValueError(f"{name} is not a measurement")
            if name in TOKEN_FIELDS and m.value is not None and not isinstance(m.value, int):
                raise ValueError(f"{name} is a count of tokens: a whole number")

    def to_wire(self) -> dict:
        return {name: getattr(self, name).to_wire() for name in USAGE_FIELDS}


TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")
USAGE_FIELDS = TOKEN_FIELDS + ("cost_usd",)

# ---------------------------------------------------------------------------------------------------------
# The conversation, vendor-neutral.


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class TextPart:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("a text part's text is text")


MAX_CALL_ID = 128
MAX_TOOL_NAME = 256
MAX_INPUT_DEPTH = 64
INPUT_TOO_DEEP = f"input is nested more than {MAX_INPUT_DEPTH} levels deep"
INPUT_NOT_FINITE = "input holds a number JSON cannot write (NaN, an infinity, or an integer too long for Python)"
REFUSED_INPUT = (INPUT_TOO_DEEP, INPUT_NOT_FINITE)  # input errors whose raw text is dropped and whose reason is shown


def nests_deeper(value: Any, limit: int) -> bool:
    """Whether ``value`` (anything JSON can write) nests objects or arrays more than ``limit`` deep (a flat
    object is 1). Walked without recursion, so a value too deep for Python's own recursion is measured too."""

    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            children = list(item.values())
        elif isinstance(item, (list, tuple)):
            children = list(item)
        else:
            continue
        if depth >= limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _walk_container(stack: list, seen: set[int], item: Any, *, keys: bool) -> None:
    """Push a container's children once. A container already seen is skipped, so a cycle ends."""

    if isinstance(item, Mapping):
        ident = id(item)
        if ident in seen:
            return
        seen.add(ident)
        if keys:
            stack.extend(item.keys())
        stack.extend(item.values())
    elif isinstance(item, (list, tuple)):
        ident = id(item)
        if ident in seen:
            return
        seen.add(ident)
        stack.extend(item)


def holds_nonfinite(value: Any) -> bool:
    """Whether ``value`` holds a float that is NaN or infinite, which JSON cannot write. Walked without recursion.
    A container already seen is not walked again, so a cycle ends. Call it on a value already known not to nest
    too deep (``nests_deeper``)."""

    stack = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return True
        _walk_container(stack, seen, item, keys=False)
    return False


def holds_unencodable(value: Any) -> bool:
    """Whether ``value`` holds text UTF-8 cannot encode (a lone surrogate), in a string or an object's key. Walked
    without recursion. A container already seen is not walked again, so a cycle ends."""

    stack = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
            continue
        _walk_container(stack, seen, item, keys=True)
    return False


def call_id_problem(call_id: object, name: object) -> str | None:
    """What is wrong with a vendor's call id and tool name for the wire (event.v1), or None."""

    if not isinstance(call_id, str) or not 1 <= len(call_id) <= MAX_CALL_ID or "\r" in call_id or "\n" in call_id:
        return f"a tool call id must be 1 to {MAX_CALL_ID} characters without a line break"
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_TOOL_NAME or "\r" in name or "\n" in name:
        return f"a tool name must be 1 to {MAX_TOOL_NAME} characters without a line break"
    return None


@dataclass(frozen=True)
class ToolCall:
    """The model asked for a tool. ``input`` is the parsed object, or None when what the vendor sent was not a
    JSON object, ``null`` included (then ``input_error`` says why, and ``raw_input`` keeps what was received,
    except where it was refused before it could be decoded and scrubbed: then it is empty)."""

    call_id: str
    name: str
    input: Mapping[str, Any] | None
    raw_input: str = ""
    input_error: str | None = None

    def __post_init__(self) -> None:
        problem = call_id_problem(self.call_id, self.name)
        if problem:
            raise ValueError(problem)
        if self.input is not None and not isinstance(self.input, Mapping):
            raise ValueError("a tool call's input is a JSON object, or None when it did not parse as one")


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    text: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("a tool result's text is text")


@dataclass(frozen=True)
class OpaquePart:
    """A vendor-only block (a thinking block and its signature, for example) kept byte for byte so it can be
    sent back unchanged to the same vendor. Other providers drop it. Never shown, never logged."""

    vendor: str
    data: Mapping[str, Any]


Part = Union[TextPart, ToolCall, ToolResult, OpaquePart]


@dataclass(frozen=True)
class Message:
    role: str  # "user" or "assistant"
    parts: tuple[Part, ...]

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("message role")


@dataclass(frozen=True)
class TurnRequest:
    model: str
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 64000
    effort: str | None = None


# Vendor-neutral stop reasons. A provider maps its own spellings onto these; anything else is "other".
END_TURN, TOOL_USE, MAX_TOKENS, REFUSAL, PAUSE, STOP_SEQUENCE, CONTEXT_FULL, OTHER = (
    "end_turn", "tool_use", "max_tokens", "refusal", "pause", "stop_sequence", "context_full", "other")
STOP_REASONS = (END_TURN, TOOL_USE, MAX_TOKENS, REFUSAL, PAUSE, STOP_SEQUENCE, CONTEXT_FULL, OTHER)


@dataclass(frozen=True)
class TurnResponse:
    parts: tuple[Part, ...]
    stop_reason: str
    usage: Usage
    served_model: str | None = None
    vendor_stop_reason: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if self.stop_reason not in STOP_REASONS:
            raise ValueError("stop reason")

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(p for p in self.parts if isinstance(p, ToolCall))

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


# Events a provider may report while it streams. They are for showing progress; the TurnResponse is the
# record. A callback must not raise; if it does, the provider lets the exception end the call.
@dataclass(frozen=True)
class TextDelta:
    index: int
    text: str


StreamEvent = TextDelta
OnEvent = Callable[[StreamEvent], None]

# ---------------------------------------------------------------------------------------------------------
# Cancellation: set from another thread (an interrupt) while a call or a tool is running.


class Cancelled(Exception):
    """The turn was interrupted. Not a provider fault. A provider sets ``usage`` and ``served_model`` to what it
    had measured of the interrupted call (None when nothing)."""

    usage: "Usage | None" = None
    served_model: str | None = None


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks.values())
        for fn in callbacks:
            try:
                fn()
            except Exception:  # noqa: BLE001 - a callback that fails must not stop the others
                pass

    def on_cancel(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Run ``fn`` when cancelled (now, if already cancelled). Returns a function that unregisters it."""

        if not callable(fn):
            raise TypeError("on_cancel needs something it can call")
        with self._lock:
            if not self._event.is_set():
                key = self._next
                self._next += 1
                self._callbacks[key] = fn

                def remove() -> None:
                    with self._lock:
                        self._callbacks.pop(key, None)

                return remove
        fn()
        return lambda: None

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

# ---------------------------------------------------------------------------------------------------------
# Typed failures. ``code`` is a code word for the turn's result; ``detail`` is vendor text with any secret
# removed by the provider before it gets here, and is untrusted.


class ProviderError(Exception):
    """``usage`` is what the provider had measured of the failed call (None when nothing), ``served_model`` the
    model the vendor named before it failed (None when it named none)."""

    code = "provider_error"
    retryable = False

    def __init__(self, detail: str = "", *, status: int | None = None, vendor_type: str | None = None,
                 request_id: str | None = None, usage: Usage | None = None,
                 served_model: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.vendor_type = vendor_type
        self.request_id = request_id
        self.usage = usage
        self.served_model = served_model


class LimitError(ProviderError):
    """A typed limit from the vendor (HTTP 429 or its stream equivalent). ``retry_after_s`` is the vendor's
    own retry time in seconds when it sent one; ``reset_at`` is that moment as an event.v1 timestamp."""

    code = "plan_limit"
    retryable = True

    def __init__(self, detail: str = "", *, retry_after_s: float | None = None, reset_at: str | None = None,
                 **kw: Any) -> None:
        super().__init__(detail, **kw)
        self.retry_after_s = retry_after_s
        self.reset_at = reset_at


class OverloadError(ProviderError):
    code = "provider_overloaded"
    retryable = True


class AuthError(ProviderError):
    code = "provider_auth"


class BillingError(ProviderError):
    code = "provider_billing"


class RequestError(ProviderError):
    code = "provider_request"


class ServerError(ProviderError):
    code = "provider_server"
    retryable = True


class TransportError(ProviderError):
    code = "provider_transport"
    retryable = True


class ProtocolError(ProviderError):
    """The vendor's stream could not be read as its documented format."""

    code = "provider_protocol"


class Provider(Protocol):
    name: str

    def stream_turn(self, request: TurnRequest, *, on_event: OnEvent | None = None,
                    cancel: CancelToken | None = None) -> TurnResponse:
        ...


def scrub_across(texts: Sequence[str], secrets: Iterable[str], mark: str = "[redacted]") -> list[str]:
    """Remove every secret from ``texts`` as if they were one text, so a secret split across two of them (two
    text blocks of one reply) is found in their join. Each stretch of secret becomes one ``mark``, in the text
    where it begins; its pieces in later texts are dropped. With no secret found, the texts come back as they
    were. ``texts`` and ``secrets`` are each a sequence of strings: one string is refused (it would be read as
    its characters), and so is anything in them that is not a string; an empty secret is skipped."""

    if isinstance(texts, (str, bytes)) or isinstance(secrets, (str, bytes)):
        raise TypeError("texts and secrets are each a sequence of strings, not one string")
    texts, secrets = list(texts), list(secrets)
    if not all(isinstance(t, str) for t in texts) or not all(isinstance(s, str) for s in secrets):
        raise TypeError("every text and every secret is a string")
    joined = "".join(texts)
    spans = []
    for secret in secrets:
        if not secret:
            continue
        at = joined.find(secret)
        while at >= 0:
            spans.append((at, at + len(secret)))
            at = joined.find(secret, at + 1)
    if not spans:
        return list(texts)
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out: list[str] = []
    begin = 0
    for text in texts:
        finish = begin + len(text)
        pieces: list[str] = []
        cursor = begin
        for start, end in merged:
            if end <= begin or start >= finish:
                continue
            if start > cursor:
                pieces.append(joined[cursor:start])
            if start >= begin:
                pieces.append(mark)  # the stretch begins in this text
            cursor = max(cursor, min(end, finish))
        if cursor < finish:
            pieces.append(joined[cursor:finish])
        out.append("".join(pieces))
        begin = finish
    return out


def tool_specs(specs: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    """A deterministic tool list (sorted by name), so a provider's prompt cache sees the same prefix."""

    return tuple(sorted(specs, key=lambda s: s.name))
