"""Thin Control API client over loopback HTTP. Used by the CLI, tests and recorder."""

from __future__ import annotations

import http.client
import json
import os
import stat
from typing import Any
from urllib.parse import urlsplit

from scripts.v3.control_api import errors as err
from scripts.v3.control_api.http_server import LOOPBACK_HOSTS

TOKEN_FILE_MODE_MASK = 0o077
_PLAIN_ERROR_CODES = {
    "body_too_large": err.TOO_LARGE,
    "too_many_connections": err.BUSY,
    "host_denied": err.FORBIDDEN,
    "origin_denied": err.FORBIDDEN,
    "unsupported_media_type": err.INVALID_REQUEST,
    "length_required": err.INVALID_REQUEST,
    "bad_length": err.INVALID_REQUEST,
    "bad_query": err.INVALID_PARAMS,
    "not_found": err.METHOD_NOT_FOUND,
    "method_not_allowed": err.INVALID_REQUEST,
}


class TokenFileRefused(ValueError):
    """The token file is missing, not a regular file, or readable by others."""


class ControlRpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{code} {message}")


def read_token_file(path: str | os.PathLike[str]) -> str:
    """Read a bearer token. Refuses links and group- or world-accessible files.

    The file is opened with ``O_NOFOLLOW`` (when available) and checked via
    ``fstat`` on that descriptor so a swap after ``lstat`` cannot redirect the
    read.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError:
        raise TokenFileRefused("token_file_missing") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TokenFileRefused("token_file_not_regular")
        if os.name != "nt" and info.st_mode & TOKEN_FILE_MODE_MASK:
            raise TokenFileRefused("token_file_mode")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_token_file(path: str | os.PathLike[str], token: str) -> None:
    """Create a new owner-only token file. An existing file is never overwritten."""

    descriptor = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")


class ControlClient:
    def __init__(self, url: str, token: str | None, *, timeout_s: float = 60.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or parts.port is None:
            raise ValueError("the Control API client talks to loopback only")
        self._host, self._port = parts.hostname, parts.port
        self._token = token
        self._timeout_s = timeout_s
        self._next_id = 0

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_s)
        try:
            sent = {"Host": f"{self._host}:{self._port}"}
            if self._token is not None:
                sent["Authorization"] = f"Bearer {self._token}"
            if body is not None:
                sent["Content-Type"] = "application/json"
            sent.update(headers or {})
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def exchange(self, method: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """One JSON-RPC call. Returns the HTTP status and the decoded response object."""

        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        status, _, raw = self.request("POST", "/v1/rpc", json.dumps(message).encode("utf-8"))
        return status, json.loads(raw.decode("utf-8"))

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        status, response = self.exchange(method, params)
        error = response.get("error")
        if error is not None:
            raise _rpc_error(error)
        return response["result"]

    def poll(self, after: int = 0, *, limit: int = 100, wait_s: float = 0.0) -> dict[str, Any]:
        query = f"after={int(after)}&limit={int(limit)}&wait={float(wait_s)}"
        status, _, raw = self.request("GET", f"/v1/events?{query}")
        response = json.loads(raw.decode("utf-8"))
        if "error" in response:
            raise _rpc_error(response["error"])
        return response["result"]


def _rpc_error(error: Any) -> ControlRpcError:
    if isinstance(error, str):
        return ControlRpcError(_PLAIN_ERROR_CODES.get(error, err.INVALID_REQUEST), error, None)
    if not isinstance(error, dict):
        return ControlRpcError(err.INTERNAL_ERROR, "internal_error", None)
    message = error.get("message")
    if not isinstance(message, str):
        message = "internal_error"
    code = error.get("code")
    if not isinstance(code, int):
        code = _PLAIN_ERROR_CODES.get(message, err.INTERNAL_ERROR)
    data = error.get("data")
    return ControlRpcError(code, message, data if isinstance(data, dict) else None)
