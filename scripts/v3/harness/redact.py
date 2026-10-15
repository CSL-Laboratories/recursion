"""Replace token-shaped text before it is written to a log or printed."""

from __future__ import annotations

import re


REDACTED = "[redacted]"
_SECRET_KEY = re.compile(
    r"(?i)^(api[_-]?key|token|secret|password|authorization|credential|xai[_-]?api[_-]?key)$"
)
_TOKEN = re.compile(
    "|".join(
        (
            r"sk-[A-Za-z0-9_-]{8,}",
            r"xai-[A-Za-z0-9_-]{8,}",
            r"ghp_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{16,}",
            r"AIza[0-9A-Za-z_-]{20,}",
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        )
    ),
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)


def redact_text(text: str) -> str:
    """Hide recognizable token shapes. Ordinary words stay."""

    text = _BEARER.sub(REDACTED, text)
    return _TOKEN.sub(REDACTED, text)


def redact(value: object) -> object:
    """Return a copy with secret-shaped strings replaced."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.fullmatch(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(item)
        return cleaned
    return value
