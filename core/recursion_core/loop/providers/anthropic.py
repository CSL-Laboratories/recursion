"""Anthropic Messages API client on the standard library: streaming, tool use, prompt caching (lane LOOP).

Why the standard library and not the vendor SDK: the core has one dependency (``cryptography``) and adding one
is Lucas's call; the brief's question on it recommends (a), the standard library behind the interface, and this
lane keeps building (a) until he answers.

What this module takes from the provider's documentation (the claude-api reference, read Nov 14, 2026), not
from memory:
- ``POST {base}/v1/messages`` with ``x-api-key``, ``anthropic-version: 2026-10-21`` and JSON; ``stream: true``
  gives ``message_start``, ``content_block_start/delta/stop``, ``message_delta``, ``message_stop``, ``ping``
  and ``error`` events; deltas are ``text_delta``, ``input_json_delta`` (``partial_json``), ``thinking_delta``
  and ``signature_delta``.
- Version 2026-10-21 refuses ``temperature``, ``top_p`` and ``top_k``: none is sent. Thinking is not
  configured (current models run adaptive thinking when it is omitted); ``output_config.effort`` is sent only
  when the route sets one.
- Thinking blocks are sent back unchanged, in place (preserved thinking binds them to the conversation), so
  the loop's history is append-only and every vendor block is kept as an ``OpaquePart``.
- Prompt caching: one top-level ``cache_control: {"type": "ephemeral"}``, which caches the longest prefix; the
  tool list is sorted and the system prompt holds nothing that changes per call.
- ``eager_input_streaming: true`` on each tool when the base URL is Anthropic's own (the docs: a proxy may
  refuse the field), and every tool input is then parsed strictly here and validated by the registry before
  anything runs.
- Errors: 429 ``rate_limit_error`` (with ``retry-after``), 529 ``overloaded_error``, 401/403 auth and
  permission, 402 billing, 400/404/413 request, 5xx server.
- Fallbacks to another model on a refusal (``fallbacks``) are NOT sent: a model the route did not name would
  serve the call, and Lucas's Nov 8 rule for limits is "never a silent switch" (routes/limits.py). A refusal
  ends the turn with a typed code instead.

The key is held in this object only. It goes into one request header, never into an exception, a return value,
an event or a log. Vendor text that holds it is scrubbed before it leaves this module: every error detail
(an event's name or a block's type included), the reply's text parts as one text (a key split across two text
blocks is found in their join), tool inputs, and streamed text (a fragment that could be the start of the key
is held back until the next fragment, in this block or the next text block, or the end of the message). A
thinking or other vendor block that holds it is refused (``ProtocolError``), since changing it would break its
signature, and so is a tool call whose id or name holds it.

A call that fails or is interrupted after the stream began still reports what it measured: the exception's
``usage`` holds the counts the stream had given (an output count is a lower bound until ``message_stop``) and
``served_model`` the model ``message_start`` named.
"""

from __future__ import annotations

import dataclasses
import email.utils
import http.client
import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.sse import SSEError, iter_events

NAME = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2026-10-21"
ENV_KEY = "ANTHROPIC_API_KEY"
ENV_BASE_URL = "ANTHROPIC_BASE_URL"
FIRST_PARTY_HOST = "api.anthropic.com"
MAX_ERROR_BODY = 1024 * 1024

_STOP = {
    "end_turn": b.END_TURN, "tool_use": b.TOOL_USE, "max_tokens": b.MAX_TOKENS, "refusal": b.REFUSAL,
    "pause_turn": b.PAUSE, "stop_sequence": b.STOP_SEQUENCE, "model_context_window_exceeded": b.CONTEXT_FULL,
}

ConnectionFactory = Callable[[str, str, int, float], http.client.HTTPConnection]


class ConfigError(ValueError):
    """The provider's configuration is refused before any request is made."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_base_url(url: str) -> tuple[str, str, int, str]:
    """Return (scheme, host, port, path prefix). HTTPS only, except to a loopback address."""

    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise ConfigError("base URL is empty or has whitespace")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ConfigError("base URL scheme must be https")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("base URL carries credentials")
    if parts.query or parts.fragment:
        raise ConfigError("base URL has a query or fragment")
    host = parts.hostname or ""
    if not host:
        raise ConfigError("base URL has no host")
    if parts.scheme == "http" and not _is_loopback(host):
        raise ConfigError("plain http is refused except to a loopback address: the key would cross the network in "
                          "clear text")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise ConfigError("base URL port") from None
    return parts.scheme, host, port, parts.path.rstrip("/")


def _default_connection(scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _timestamp(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date. None if absent or unreadable."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = moment.timestamp() - now
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return None
    return max(0.0, seconds)


class AnthropicProvider:
    name = NAME

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, api_version: str = API_VERSION,
                 betas: tuple[str, ...] = (), connect_timeout: float = 30.0, read_timeout: float = 600.0,
                 eager_input_streaming: bool | None = None, connection_factory: ConnectionFactory | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        if (not isinstance(api_key, str) or len(api_key) < 8
                or any(ch.isspace() or not 0x21 <= ord(ch) <= 0x7E for ch in api_key)):
            # Shorter than 8 is no key, and the redactor could not remove it safely; a header value must be
            # printable ASCII with no space or line break.
            raise ConfigError(f"{ENV_KEY} is not a single printable token of 8 characters or more")
        self._key = api_key
        self._scheme, self._host, self._port, prefix = parse_base_url(base_url)
        self._path = prefix + "/v1/messages"
        self._version = api_version
        for beta in betas:
            if not beta or any(ch.isspace() or ch == "," for ch in beta):
                raise ConfigError("beta name")
        self._betas = tuple(betas)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._eager = (self._host == FIRST_PARTY_HOST) if eager_input_streaming is None else eager_input_streaming
        self._connect = connection_factory or _default_connection
        self._clock = clock

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kw: Any) -> "AnthropicProvider":
        key = env.get(ENV_KEY, "")
        if not key:
            raise ConfigError(f"{ENV_KEY} is not set in the engine's environment")
        return cls(key, base_url=env.get(ENV_BASE_URL) or DEFAULT_BASE_URL, **kw)

    def __repr__(self) -> str:
        return f"AnthropicProvider(host={self._host!r}, port={self._port})"

    @property
    def secrets(self) -> tuple[str, ...]:
        """What the loop's redactor must remove from anything it writes."""

        return (self._key,)

    # -- request -----------------------------------------------------------------------------------------

    def build_body(self, request: b.TurnRequest) -> dict:
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "stream": True,
            "cache_control": {"type": "ephemeral"},
            "messages": [m for m in (self._message(msg) for msg in request.messages) if m["content"]],
        }
        if request.system:
            body["system"] = [{"type": "text", "text": request.system}]
        if request.tools:
            tools = []
            for spec in request.tools:
                tool: dict[str, Any] = {"name": spec.name, "description": spec.description,
                                        "input_schema": dict(spec.input_schema)}
                if self._eager:
                    tool["eager_input_streaming"] = True
                tools.append(tool)
            body["tools"] = tools
        if request.effort:
            body["output_config"] = {"effort": request.effort}
        return body

    def _message(self, message: b.Message) -> dict:
        content: list[dict] = []
        for part in message.parts:
            if isinstance(part, b.TextPart):
                if part.text:
                    content.append({"type": "text", "text": part.text})
            elif isinstance(part, b.ToolCall):
                content.append({"type": "tool_use", "id": part.call_id, "name": part.name,
                                "input": dict(part.input) if part.input is not None else {}})
            elif isinstance(part, b.ToolResult):
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": part.call_id,
                                         "content": part.text if part.text else "(no output)"}
                if part.is_error:
                    block["is_error"] = True
                content.append(block)
            elif isinstance(part, b.OpaquePart):
                if part.vendor == NAME:
                    content.append(dict(part.data))
        return {"role": message.role, "content": content}

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": self._version,
            "x-api-key": self._key,
        }
        if self._betas:
            headers["anthropic-beta"] = ",".join(self._betas)
        return headers

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "[redacted]") if self._key and self._key in text else text

    def _scrub_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {self._scrub(str(k)): self._scrub_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_obj(v) for v in value]
        return value

    def _holds_key(self, value: Any) -> bool:
        return self._key in json.dumps(value, ensure_ascii=False)

    def _scrub_response(self, response: b.TurnResponse) -> b.TurnResponse:
        """The reply's text parts scrubbed as one text, so a key split across two text blocks is caught."""

        where = [k for k, part in enumerate(response.parts) if isinstance(part, b.TextPart)]
        parts = list(response.parts)
        for k, text in zip(where, b.scrub_across([parts[k].text for k in where], (self._key,))):
            parts[k] = b.TextPart(text)
        return dataclasses.replace(response, parts=tuple(parts))

    def _held_back(self, text: str) -> int:
        """How many characters at the end of ``text`` could be the start of the key (held until more comes)."""

        for k in range(min(len(self._key) - 1, len(text)), 0, -1):
            if text.endswith(self._key[:k]):
                return k
        return 0

    # -- call --------------------------------------------------------------------------------------------

    def stream_turn(self, request: b.TurnRequest, *, on_event: b.OnEvent | None = None,
                    cancel: b.CancelToken | None = None) -> b.TurnResponse:
        cancel = cancel or b.CancelToken()
        cancel.raise_if_cancelled()
        payload = json.dumps(self.build_body(request), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        conn = self._connect(self._scheme, self._host, self._port, self._connect_timeout)
        # The socket is kept here: once a reply says "connection: close", http.client hands the socket to the
        # response and sets conn.sock to None, and an interrupt must still be able to shut it down.
        held: list[socket.socket | None] = [None]

        def abort() -> None:
            sock = held[0] or getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        unregister = cancel.on_cancel(abort)
        try:
            try:
                conn.request("POST", self._path, body=payload, headers=self._headers())
                held[0] = conn.sock
                if conn.sock is not None:
                    conn.sock.settimeout(self._read_timeout)
                cancel.raise_if_cancelled()  # an interrupt that came while connecting
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                cancel.raise_if_cancelled()
                raise b.TransportError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
            request_id = response.getheader("request-id")
            if response.status != 200:
                raise self._http_error(response, request_id)
            ctype = (response.getheader("content-type") or "").split(";")[0].strip().lower()
            if ctype != "text/event-stream":
                raise b.ProtocolError(f"expected an event stream, got {ctype or 'no content type'}",
                                      status=200, request_id=request_id)
            return self._read_stream(response, request_id, on_event, cancel)
        finally:
            unregister()
            try:
                conn.close()
            except OSError:
                pass

    def _http_error(self, response: http.client.HTTPResponse, request_id: str | None) -> b.ProviderError:
        status = response.status
        try:
            raw = response.read(MAX_ERROR_BODY)
        except (OSError, http.client.HTTPException):
            raw = b""
        vendor_type, message = None, ""
        try:
            doc = json.loads(raw.decode("utf-8"))
            err = doc.get("error") if isinstance(doc, dict) else None
            if isinstance(err, dict):
                vendor_type = err.get("type") if isinstance(err.get("type"), str) else None
                message = err.get("message") if isinstance(err.get("message"), str) else ""
        except (UnicodeDecodeError, ValueError):
            message = ""
        detail = self._scrub(f"HTTP {status}" + (f" {vendor_type}" if vendor_type else "") +
                             (f": {message}" if message else ""))
        kw = {"status": status, "vendor_type": vendor_type, "request_id": request_id}
        retry_after = parse_retry_after(response.getheader("retry-after"), self._clock())
        return self._classify(status, vendor_type, detail, retry_after, kw)

    def _classify(self, status: int | None, vendor_type: str | None, detail: str, retry_after: float | None,
                  kw: dict) -> b.ProviderError:
        if status == 429 or vendor_type == "rate_limit_error":
            reset_at = _timestamp(self._clock() + retry_after) if retry_after is not None else None
            return b.LimitError(detail, retry_after_s=retry_after, reset_at=reset_at, **kw)
        if status == 529 or vendor_type == "overloaded_error":
            return b.OverloadError(detail, **kw)
        if status in (401, 403) or vendor_type in ("authentication_error", "permission_error"):
            return b.AuthError(detail, **kw)
        if status == 402 or vendor_type == "billing_error":
            return b.BillingError(detail, **kw)
        if status in (400, 404, 413) or vendor_type in ("invalid_request_error", "not_found_error",
                                                       "request_too_large"):
            return b.RequestError(detail, **kw)
        if (status is not None and status >= 500) or vendor_type == "api_error":
            return b.ServerError(detail, **kw)
        return b.ProviderError(detail, **kw)

    def _read_stream(self, response: http.client.HTTPResponse, request_id: str | None,
                     on_event: b.OnEvent | None, cancel: b.CancelToken) -> b.TurnResponse:
        def read(n: int) -> bytes:
            try:
                return response.read1(n)
            except (OSError, http.client.HTTPException) as exc:
                cancel.raise_if_cancelled()
                raise b.TransportError(self._scrub(f"stream read failed: {type(exc).__name__}: {exc}"),
                                       request_id=request_id) from None

        state = _StreamState(request_id)
        try:
            try:
                for event in iter_events(read):
                    cancel.raise_if_cancelled()
                    try:
                        data = json.loads(event.data)
                    except ValueError:
                        raise b.ProtocolError(f"event {event.event!r} is not JSON", request_id=request_id) from None
                    if not isinstance(data, dict):
                        raise b.ProtocolError(f"event {event.event!r} is not an object", request_id=request_id)
                    if state.feed(data, on_event, self) == "stop":
                        break
            except SSEError as exc:
                cancel.raise_if_cancelled()
                raise b.ProtocolError(str(exc), request_id=request_id) from None
            cancel.raise_if_cancelled()
            return self._scrub_response(state.finish())
        except (b.ProviderError, b.Cancelled) as exc:
            if isinstance(exc, b.ProviderError):
                # Its detail may quote the vendor (an event's name, a block's type): scrubbed here, once for all.
                exc.detail = self._scrub(exc.detail)
                exc.args = (exc.detail,)
            # The call still happened: what the stream had measured goes with the failure.
            if exc.usage is None:
                exc.usage = state.partial_usage()
            if exc.served_model is None:
                exc.served_model = state.model
            raise


class _StreamState:
    """Builds one TurnResponse from the documented stream events."""

    def __init__(self, request_id: str | None) -> None:
        self.request_id = request_id
        self.model: str | None = None
        self.started = False
        self.stopped = False
        self.stop_reason: str | None = None
        self.usage: dict[str, int] = {}
        self.blocks: dict[int, dict] = {}
        self.order: list[int] = []
        self.done: dict[int, b.Part] = {}
        self.unsent = ""  # streamed text held back because it could be the start of the key (across blocks)
        self.last_text = 0  # the index of the last text block, for what is still held when the message ends

    def _protocol(self, what: str) -> b.ProtocolError:
        return b.ProtocolError(what, request_id=self.request_id)

    def _take_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage[key] = value  # message_delta's counts are cumulative: the last one wins

    def feed(self, data: dict, on_event: b.OnEvent | None, provider: AnthropicProvider) -> str | None:
        kind = data.get("type")
        if kind == "ping":
            return None
        if kind == "error":
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            vendor_type = err.get("type") if isinstance(err.get("type"), str) else None
            message = err.get("message") if isinstance(err.get("message"), str) else ""
            detail = provider._scrub(f"stream error {vendor_type or 'unknown'}" + (f": {message}" if message else ""))
            raise provider._classify(None, vendor_type, detail, None,
                                     {"status": None, "vendor_type": vendor_type, "request_id": self.request_id})
        if kind == "message_start":
            message = data.get("message")
            if not isinstance(message, dict):
                raise self._protocol("message_start without a message")
            if self.started:
                raise self._protocol("a second message_start")
            self.started = True
            self.model = message.get("model") if isinstance(message.get("model"), str) else None
            self._take_usage(message.get("usage"))
            return None
        if not self.started:
            raise self._protocol(f"{kind!r} before message_start")
        if kind == "content_block_start":
            index, block = data.get("index"), data.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict) or index in self.blocks:
                raise self._protocol("content_block_start")
            self.blocks[index] = {"block": dict(block), "text": [], "json": [], "thinking": [], "signature": []}
            self.order.append(index)
            return None
        if kind == "content_block_delta":
            index, delta = data.get("index"), data.get("delta")
            entry = self.blocks.get(index) if isinstance(index, int) else None
            if entry is None or index in self.done or not isinstance(delta, dict):
                raise self._protocol("content_block_delta for no open block")
            dtype = delta.get("type")
            if dtype == "text_delta" and isinstance(delta.get("text"), str):
                entry["text"].append(delta["text"])
                if on_event is not None and delta["text"]:
                    # What an earlier text block held back is carried over: a key can be split across blocks.
                    pending = provider._scrub(self.unsent + delta["text"])
                    keep = provider._held_back(pending)
                    self.unsent = pending[len(pending) - keep:] if keep else ""
                    pending = pending[:len(pending) - keep]
                    self.last_text = index
                    if pending:
                        on_event(b.TextDelta(index, pending))
            elif dtype == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                entry["json"].append(delta["partial_json"])
            elif dtype == "thinking_delta" and isinstance(delta.get("thinking"), str):
                entry["thinking"].append(delta["thinking"])
            elif dtype == "signature_delta" and isinstance(delta.get("signature"), str):
                entry["signature"].append(delta["signature"])
            # Other delta types (citations, for one) carry nothing this loop uses; they are skipped.
            return None
        if kind == "content_block_stop":
            index = data.get("index")
            entry = self.blocks.get(index) if isinstance(index, int) else None
            if entry is None or index in self.done:
                raise self._protocol("content_block_stop for no open block")
            self.done[index] = self._finish_block(entry, provider)
            return None
        if kind == "message_delta":
            delta = data.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                self.stop_reason = delta["stop_reason"]
            self._take_usage(data.get("usage"))
            return None
        if kind == "message_stop":
            self.stopped = True
            if on_event is not None and self.unsent:
                on_event(b.TextDelta(self.last_text, provider._scrub(self.unsent)))
                self.unsent = ""
            return "stop"
        return None  # an event type this client does not know is skipped, as the stream format allows

    def _finish_block(self, entry: dict, provider: AnthropicProvider) -> b.Part:
        block = entry["block"]
        btype = block.get("type")
        if btype == "text":
            return b.TextPart((block.get("text") or "") + "".join(entry["text"]))  # scrubbed with the whole reply
        if btype == "tool_use":
            call_id, name = block.get("id"), block.get("name")
            problem = b.call_id_problem(call_id, name)
            if problem:
                raise self._protocol(f"tool_use block: {problem}")
            if provider._key in call_id or provider._key in name:
                # An id pairs the call with its result and a name picks the tool: neither can be scrubbed.
                raise self._protocol("a tool_use block's id or name holds the connection's key; it is not kept")
            raw = provider._scrub("".join(entry["json"]))
            if not raw.strip():
                start = block.get("input")
                return b.ToolCall(call_id, name, provider._scrub_obj(dict(start)) if isinstance(start, dict) else {},
                                  raw)
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                return b.ToolCall(call_id, name, None, raw, f"input is not valid JSON: {exc.msg}")
            if not isinstance(parsed, dict):
                return b.ToolCall(call_id, name, None, raw, "input is not a JSON object")
            clean = provider._scrub_obj(parsed)
            if clean != parsed:  # the key was there, spelt with JSON escapes the text scrub cannot see
                raw = json.dumps(clean, ensure_ascii=False)
            return b.ToolCall(call_id, name, clean, raw)
        if btype == "thinking":
            data = dict(block)
            data["thinking"] = (block.get("thinking") or "") + "".join(entry["thinking"])
            data["signature"] = (block.get("signature") or "") + "".join(entry["signature"])
        else:
            data = dict(block)  # redacted_thinking and anything else: sent back as received
        if provider._holds_key(data):
            # It cannot be scrubbed without breaking its signature, and it must not be sent back as it is.
            raise self._protocol(f"a {btype or 'vendor'} block holds the connection's key; it is not kept")
        return b.OpaquePart(NAME, data)

    def partial_usage(self) -> b.Usage | None:
        """What the stream had measured when the call ended early (None when nothing). Input and cache counts
        come with message_start and are exact; the output count keeps growing until message_stop."""

        if not self.usage:
            return None

        def measure(key: str, final: bool) -> b.Measurement:
            if key not in self.usage:
                return b.unknown()
            return b.observed(self.usage[key]) if final else b.at_least(self.usage[key])

        return b.Usage(
            input_tokens=measure("input_tokens", True),
            output_tokens=measure("output_tokens", self.stopped),
            reasoning_tokens=b.unknown(),
            cache_read_tokens=measure("cache_read_input_tokens", True),
            cache_write_tokens=measure("cache_creation_input_tokens", True),
            cost_usd=b.unknown(),
        )

    def finish(self) -> b.TurnResponse:
        if not self.stopped:
            raise b.TransportError("the stream ended before message_stop", request_id=self.request_id)
        open_blocks = [i for i in self.order if i not in self.done]
        if open_blocks:
            raise self._protocol("message_stop with a content block still open")
        vendor = self.stop_reason
        stop = _STOP.get(vendor or "", b.OTHER)

        def measure(key: str) -> b.Measurement:
            return b.observed(self.usage[key]) if key in self.usage else b.unknown()

        usage = b.Usage(
            input_tokens=measure("input_tokens"),
            output_tokens=measure("output_tokens"),
            reasoning_tokens=b.unknown(),  # thinking is counted inside output_tokens and not split out
            cache_read_tokens=measure("cache_read_input_tokens"),
            cache_write_tokens=measure("cache_creation_input_tokens"),
            cost_usd=b.unknown(),  # the API reports tokens, not dollars
        )
        parts = tuple(self.done[i] for i in sorted(self.order))
        return b.TurnResponse(parts, stop, usage, served_model=self.model, vendor_stop_reason=vendor,
                              request_id=self.request_id)
