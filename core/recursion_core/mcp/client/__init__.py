"""The core's MCP client of tool servers: the protocol core over a pair of byte streams (``session.py``).

Standard library only. It starts no server process and ends none. Its bounds are in ``limits.py``.
"""

from .limits import CALL_DEADLINE_S, MAX_MESSAGE_BYTES, MAX_SERVER_TEXT_CHARS
from .session import (
    BAD_ANSWER,
    CALL_ABANDONED,
    CALL_TIMEOUT,
    CLIENT_SHUT,
    INITIALIZE_REFUSED,
    LATEST_PROTOCOL,
    LEGACY_PROTOCOL,
    MESSAGE_TOO_LARGE,
    OUTPUT_FAILED,
    PROTOCOL_UNSUPPORTED,
    SERVER_GONE,
    SERVER_ERROR,
    SERVER_REQUEST_REFUSED,
    McpClient,
    McpClientError,
    McpServerError,
    McpSessionEnded,
    ToolCall,
)

__all__ = [
    "BAD_ANSWER",
    "CALL_ABANDONED",
    "CALL_DEADLINE_S",
    "CALL_TIMEOUT",
    "CLIENT_SHUT",
    "INITIALIZE_REFUSED",
    "LATEST_PROTOCOL",
    "LEGACY_PROTOCOL",
    "MAX_MESSAGE_BYTES",
    "MAX_SERVER_TEXT_CHARS",
    "MESSAGE_TOO_LARGE",
    "OUTPUT_FAILED",
    "PROTOCOL_UNSUPPORTED",
    "SERVER_GONE",
    "SERVER_ERROR",
    "SERVER_REQUEST_REFUSED",
    "McpClient",
    "McpClientError",
    "McpServerError",
    "McpSessionEnded",
    "ToolCall",
]
