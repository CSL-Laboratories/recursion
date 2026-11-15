"""JSON-RPC 2.0 message shapes as MCP 2025-06-18 uses them (basic/index, "Messages").

* A request has ``jsonrpc: "2.0"``, a string ``method``, an ``id`` that is a string or a number (MCP: never
  null; a JSON boolean is not a number), and ``params`` that, when present, is an object.
* A notification is a request without an ``id``; it is never answered.
* A response (``result`` or ``error``, no ``method``) is something this server never asked for: it sends
  no requests, so a response from the client is ignored.
* 2025-06-18 removed JSON-RPC batching: an array is refused as one invalid request.

An error whose request id cannot be read carries ``id: null`` (JSON-RPC 2.0, section 5).

Request ids are compared as JSON values, not as Python values: ``"1"`` and ``1`` are different requests,
and ``true`` is never the request ``1`` (``id_key``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def valid_id(value: Any) -> bool:
    """A string, or a finite number that is not a boolean."""

    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def id_key(value: Any) -> tuple[str, Any]:
    """A key that keeps a string id apart from a numeric one of the same spelling. Call only on a valid id."""

    return ("s", value) if isinstance(value, str) else ("n", value)


def result(request_id: Any, value: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    body: dict = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


@dataclass(frozen=True, slots=True)
class Request:
    id: Any
    method: str
    params: dict


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: dict


@dataclass(frozen=True, slots=True)
class Response:
    """A message from the client that answers something; this server sends no requests."""


@dataclass(frozen=True, slots=True)
class Invalid:
    """Refused before dispatch: answer ``error(id, code, message)``."""

    id: Any
    code: int
    message: str


def classify(message: Any) -> Request | Notification | Response | Invalid:
    if isinstance(message, list):
        return Invalid(None, INVALID_REQUEST, "Invalid Request: JSON-RPC batches are not supported in 2025-06-18")
    if not isinstance(message, dict):
        return Invalid(None, INVALID_REQUEST, "Invalid Request: a message must be a JSON object")
    raw_id = message.get("id")
    readable_id = raw_id if "id" in message and valid_id(raw_id) else None
    if message.get("jsonrpc") != "2.0":
        return Invalid(readable_id, INVALID_REQUEST, 'Invalid Request: jsonrpc must be "2.0"')
    if "method" not in message:
        if "result" in message or "error" in message:
            return Response()
        return Invalid(readable_id, INVALID_REQUEST, "Invalid Request: no method")
    method = message["method"]
    if not isinstance(method, str) or not method:
        return Invalid(readable_id, INVALID_REQUEST, "Invalid Request: method must be a non-empty string")
    params = message.get("params", {})
    if "id" not in message:
        return Notification(method, params if isinstance(params, dict) else {})
    if readable_id is None:
        return Invalid(None, INVALID_REQUEST, "Invalid Request: id must be a string or a number, never null")
    if not isinstance(params, dict):
        return Invalid(readable_id, INVALID_PARAMS, "Invalid params: params must be an object")
    return Request(readable_id, method, params)
