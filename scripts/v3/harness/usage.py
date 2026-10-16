"""Token and cost figures from provider streams.

Missing figures stay unknown. They are not stored as zero. Codex
``turn.completed`` usage is a cumulative session total. ``codex_session_meter``
keeps the last such event and does not sum the stream. The product ceilings
below are unchanged by that meter.
"""

from __future__ import annotations

import json


# Codex exec JSON reports cumulative session input, not the current context
# window. A documented session reached 6.9M input tokens against a 272k window
# (https://github.com/openai/codex/issues/17539). A 1_000_000 ceiling stops
# those runs. These defaults are session ceilings, not window sizes.
DEFAULT_MAX_INPUT_TOKENS = 50_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_000_000

# Last ``turn.completed`` usage in one ``codex exec`` stream. That object is
# the session total, not one API call. See docs/v3/harness/CSL-81.md.
CODEX_SESSION_METER = "cumulative-session-total"


def empty_usage() -> dict:
    return {"cost": None, "input_tokens": None, "output_tokens": None}


def default_budgets() -> dict:
    return {
        "max_cost": None,
        "max_input_tokens": DEFAULT_MAX_INPUT_TOKENS,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    }


def parse_usage(text: str) -> dict:
    """Return the last usage object a stream actually reported."""

    found = empty_usage()
    for payload in _objects(text):
        parsed = _from_event(payload)
        if parsed is not None:
            found = parsed
    return found


def codex_session_meter(text: str) -> dict:
    """Return the last ``turn.completed`` usage in one Codex exec stream.

    The reading is a cumulative session total (``CODEX_SESSION_METER``).
    Events in this stream are not summed. A stream with no such usage stays
    unknown. A later ``turn.completed`` that omits usage keeps the earlier
    total as a lower bound on spend. A later turn that reports only some
    fields replaces those fields and keeps the earlier value for the rest.
    Callers may add totals from separate exec processes that do not
    resume or fork the same session.
    """

    found = empty_usage()
    for payload in _objects(text):
        if payload.get("type") != "turn.completed":
            continue
        parsed = _from_event(payload)
        # The total is cumulative, so an earlier reading is a lower bound on
        # spend. A later turn without usage must not erase it. A later turn
        # that reports only some fields replaces those fields only.
        if parsed is not None:
            found = _keep_reported(found, parsed)
    return found


def _objects(text: str):
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _keep_reported(previous: dict, parsed: dict) -> dict:
    """Replace only the fields a later reading actually reported."""

    merged = dict(previous)
    for key in ("input_tokens", "output_tokens", "cost"):
        if parsed.get(key) is not None:
            merged[key] = parsed[key]
    return merged


def add_usage(total: dict, extra: dict) -> dict:
    """Add known figures. An unknown figure does not contribute a zero."""

    for key in ("cost", "input_tokens", "output_tokens"):
        if extra.get(key) is None:
            continue
        total[key] = extra[key] if total.get(key) is None else total[key] + extra[key]
    return total


def call_fits(spent: dict, budgets: dict, prompt: str) -> bool:
    """False when starting this call would pass a known ceiling.

    A known figure already at its ceiling refuses the call. The prompt is not
    measured: character length is not a lower bound on token count, but a
    nonempty prompt costs at least one input token, so an input allowance
    below one refuses the call even when prior usage is unknown. The
    post-call check still enforces the ceiling.
    """

    if _at_or_over(spent, budgets):
        return False
    limit = budgets.get("max_input_tokens")
    if limit is not None and prompt:
        used = spent.get("input_tokens")
        if limit - (0 if used is None else used) < 1:
            return False
    return True


def exceeded(spent: dict, budgets: dict) -> bool:
    """True when a known figure is strictly above its ceiling."""

    pairs = (
        ("input_tokens", "max_input_tokens"),
        ("output_tokens", "max_output_tokens"),
        ("cost", "max_cost"),
    )
    for spent_key, limit_key in pairs:
        limit = budgets.get(limit_key)
        value = spent.get(spent_key)
        if limit is not None and value is not None and value > limit:
            return True
    return False


def _at_or_over(spent: dict, budgets: dict) -> bool:
    """True when a known figure is already at or above its ceiling."""

    pairs = (
        ("input_tokens", "max_input_tokens"),
        ("output_tokens", "max_output_tokens"),
        ("cost", "max_cost"),
    )
    for spent_key, limit_key in pairs:
        limit = budgets.get(limit_key)
        value = spent.get(spent_key)
        if limit is not None and value is not None and value >= limit:
            return True
    return False


def _from_event(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        if _looks_like_usage(payload):
            usage = payload
        else:
            return None
    parsed = empty_usage()
    parsed["input_tokens"] = _int_field(usage, "input_tokens", "prompt_tokens", "inputTokens")
    parsed["output_tokens"] = _int_field(usage, "output_tokens", "completion_tokens", "outputTokens")
    parsed["cost"] = _number_field(usage, "cost", "total_cost_usd")
    if parsed["cost"] is None:
        parsed["cost"] = _number_field(payload, "total_cost_usd", "cost")
    if parsed["input_tokens"] is None and parsed["output_tokens"] is None and parsed["cost"] is None:
        return None
    return parsed


def _looks_like_usage(payload: dict) -> bool:
    keys = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "inputTokens", "outputTokens")
    return any(key in payload for key in keys)


def _int_field(payload: dict, *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0 and value.is_integer():
            return int(value)
    return None


def _number_field(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value >= 0:
            return float(value)
    return None
