"""A scripted provider for tests and for the engine's direct-process conformance runs. No network, no key.

A script is a list of steps, one per provider call. Each step is either a ``TurnResponse`` (or a callable that
builds one from the request), an exception to raise, or, when loaded from JSON, one of these objects:

    {"text": "...", "tool_calls": [{"id": "...", "name": "...", "input": {...}}],
     "stop_reason": "end_turn" | "tool_use" | ..., "model": "...",
     "usage": {"input_tokens": 12, "output_tokens": 3, ...}}       counts left out are unknown
    {"raw_tool_call": {"id": "...", "name": "...", "raw": "<text that is not JSON>"}, ...}
    {"error": {"kind": "limit" | "overload" | "auth" | "billing" | "request" | "server" | "transport",
               "detail": "...", "retry_after_s": 30, "reset_at": "2026-11-14T12:00:00.000Z"}}
    {"block_until_cancelled": true}        the call waits until the turn is interrupted

Every request the provider receives is kept in ``requests`` (in memory) and, when a record path is given,
appended to it as one JSON line, so a test can read what the loop sent without a model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Union

from recursion_core.loop.providers import base as b

NAME = "fake"

Step = Union[b.TurnResponse, BaseException, Callable[[b.TurnRequest], b.TurnResponse], Mapping[str, Any]]

_ERRORS = {
    "limit": b.LimitError, "overload": b.OverloadError, "auth": b.AuthError, "billing": b.BillingError,
    "request": b.RequestError, "server": b.ServerError, "transport": b.TransportError,
    "protocol": b.ProtocolError,
}


class ScriptExhausted(b.ProviderError):
    code = "provider_script_exhausted"


def _usage(doc: Mapping[str, Any] | None) -> b.Usage:
    doc = doc or {}
    values = {}
    for name in b.USAGE_FIELDS:
        value = doc.get(name)
        values[name] = b.observed(value) if value is not None else b.unknown()
    return b.Usage(**values)


def step_from_json(doc: Mapping[str, Any]) -> Step:
    if "error" in doc:
        err = doc["error"]
        cls = _ERRORS[err["kind"]]
        if cls is b.LimitError:
            return b.LimitError(err.get("detail", ""), retry_after_s=err.get("retry_after_s"),
                                reset_at=err.get("reset_at"), status=429)
        return cls(err.get("detail", ""))
    if doc.get("block_until_cancelled"):
        return dict(doc)
    parts: list[b.Part] = []
    if doc.get("text"):
        parts.append(b.TextPart(doc["text"]))
    for call in doc.get("tool_calls", ()):
        parts.append(b.ToolCall(call["id"], call["name"], dict(call.get("input", {})),
                                json.dumps(call.get("input", {}))))
    if "raw_tool_call" in doc:
        raw = doc["raw_tool_call"]
        parts.append(b.ToolCall(raw["id"], raw["name"], None, raw["raw"], "input is not valid JSON"))
    stop = doc.get("stop_reason") or (b.TOOL_USE if any(isinstance(p, b.ToolCall) for p in parts) else b.END_TURN)
    return b.TurnResponse(tuple(parts), stop, _usage(doc.get("usage")), served_model=doc.get("model", "fake-model"),
                          vendor_stop_reason=stop)


def request_to_json(request: b.TurnRequest) -> dict:
    def part(p: b.Part) -> dict:
        if isinstance(p, b.TextPart):
            return {"text": p.text}
        if isinstance(p, b.ToolCall):
            return {"tool_call": {"id": p.call_id, "name": p.name, "input": p.input}}
        if isinstance(p, b.ToolResult):
            return {"tool_result": {"id": p.call_id, "text": p.text, "is_error": p.is_error}}
        return {"opaque": p.vendor}

    return {
        "model": request.model,
        "system": request.system,
        "tools": [s.name for s in request.tools],
        "max_output_tokens": request.max_output_tokens,
        "messages": [{"role": m.role, "parts": [part(p) for p in m.parts]} for m in request.messages],
    }


class FakeProvider:
    name = NAME

    def __init__(self, script: Sequence[Step], *, record_path: str | Path | None = None) -> None:
        self._script = list(script)
        self._lock = threading.Lock()
        self.requests: list[b.TurnRequest] = []
        self._record = Path(record_path) if record_path is not None else None
        self.calls_started = threading.Event()

    @classmethod
    def from_file(cls, path: str | Path, *, record_path: str | Path | None = None) -> "FakeProvider":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        steps = doc["steps"] if isinstance(doc, dict) else doc
        return cls([step_from_json(s) for s in steps], record_path=record_path)

    @property
    def secrets(self) -> tuple[str, ...]:
        return ()

    def stream_turn(self, request: b.TurnRequest, *, on_event: b.OnEvent | None = None,
                    cancel: b.CancelToken | None = None) -> b.TurnResponse:
        cancel = cancel or b.CancelToken()
        with self._lock:
            self.requests.append(request)
            if self._record is not None:
                with self._record.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(request_to_json(request), sort_keys=True) + "\n")
            step = self._script.pop(0) if self._script else None
        self.calls_started.set()
        cancel.raise_if_cancelled()
        if step is None:
            raise ScriptExhausted("the fake provider's script has no step for this call")
        if isinstance(step, Mapping) and step.get("block_until_cancelled"):
            cancel.wait()
            raise b.Cancelled()
        if isinstance(step, Mapping):
            step = step_from_json(step)
        if isinstance(step, BaseException):
            raise step
        response = step(request) if callable(step) else step
        if on_event is not None:
            for index, p in enumerate(response.parts):
                if isinstance(p, b.TextPart) and p.text:
                    on_event(b.TextDelta(index, p.text))
        cancel.raise_if_cancelled()
        return response
