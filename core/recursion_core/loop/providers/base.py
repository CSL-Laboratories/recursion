"""The provider interface every model client implements (lane LOOP; LOOP-OAI implements it too).

One call: ``stream_turn(request, on_event=..., cancel=...)`` sends the conversation and the tool list, streams
the model's reply, and returns a ``TurnResponse``: the assistant's parts in order (text, tool calls, and any
vendor-only block that must be sent back unchanged), a vendor-neutral stop reason, the model that served the
call, and the usage of that one call with each measurement marked. Failures are typed exceptions, never text
the caller has to read: ``LimitError`` (with the vendor's retry time when it gives one), ``OverloadError``,
``AuthError`` and the rest below.

Vendor specifics stay behind this interface: wire formats, headers, model names and stop-reason spellings are
the provider module's business. A provider never reads a key from anywhere but what it was constructed with,
and never puts a key into an exception, an event or a return value.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
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
            if self.value is None or isinstance(self.value, bool) or self.value < 0:
                raise ValueError("a known measurement has a value of 0 or more")
            if self.provenance not in (OBSERVED, MODELLED, INFERRED):
                raise ValueError("measurement provenance")

    def to_wire(self) -> dict:
        return {"value": self.value, "quality": self.quality, "provenance": self.provenance}


def observed(value: int | float) -> Measurement:
    return Measurement(value, KNOWN, OBSERVED)


def unknown() -> Measurement:
    return Measurement(None, UNKNOWN, UNAVAILABLE)


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

    def to_wire(self) -> dict:
        return {name: getattr(self, name).to_wire() for name in USAGE_FIELDS}


USAGE_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
                "cost_usd")

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


@dataclass(frozen=True)
class ToolCall:
    """The model asked for a tool. ``input`` is the parsed object, or None when the text the vendor sent was
    not a JSON object (then ``input_error`` says why and ``raw_input`` keeps what was received)."""

    call_id: str
    name: str
    input: Mapping[str, Any] | None
    raw_input: str = ""
    input_error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    text: str
    is_error: bool = False


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
    """The turn was interrupted. Not a provider fault."""


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
    code = "provider_error"
    retryable = False

    def __init__(self, detail: str = "", *, status: int | None = None, vendor_type: str | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.vendor_type = vendor_type
        self.request_id = request_id


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


def tool_specs(specs: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    """A deterministic tool list (sorted by name), so a provider's prompt cache sees the same prefix."""

    return tuple(sorted(specs, key=lambda s: s.name))
