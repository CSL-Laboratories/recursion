"""Bounded, tenant-filtered observer journal.

Events carry a global, gapless sequence. Capacity is bounded; when an event a
caller still needs has been dropped, the poll result says ``reset`` so a
consumer cannot mistake a gap for a quiet period. Events hold digests and fixed
tokens only: prompts, raw engine output and credential material never enter
the journal. There is no wall-clock field, so a recorded transcript is
reproducible byte for byte.
"""

from __future__ import annotations

import threading
from typing import Any

MAX_POLL_EVENTS = 200
MAX_WAIT_SECONDS = 30.0
DEFAULT_CAPACITY = 4096


class EventJournal:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._next_seq = 1
        self._dropped_through = 0

    def append(
        self,
        tenant: str,
        kind: str,
        *,
        session: str | None = None,
        engine: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            self._events.append(
                {
                    "seq": seq,
                    "kind": kind,
                    "tenant": tenant,
                    "engine": engine,
                    "session": session,
                    "data": dict(data or {}),
                }
            )
            overflow = len(self._events) - self._capacity
            if overflow > 0:
                self._dropped_through = self._events[overflow - 1]["seq"]
                del self._events[:overflow]
            self._cond.notify_all()
            return seq

    def head(self) -> int:
        with self._cond:
            return self._next_seq - 1

    def poll(
        self,
        tenant: str,
        after: int,
        *,
        limit: int = 100,
        wait_s: float = 0.0,
    ) -> dict[str, Any]:
        """Events for ``tenant`` with ``seq > after``.

        ``next_after`` is the highest sequence examined, not only the highest
        returned, so a quiet tenant still advances past other tenants' events.
        """

        limit = max(1, min(int(limit), MAX_POLL_EVENTS))
        wait_s = max(0.0, min(float(wait_s), MAX_WAIT_SECONDS))
        with self._cond:
            if wait_s and not self._visible(tenant, after):
                self._cond.wait_for(lambda: self._visible(tenant, after), timeout=wait_s)
            reset = after < self._dropped_through
            start = max(after, self._dropped_through)
            out: list[dict[str, Any]] = []
            examined = start
            for event in self._events:
                if event["seq"] <= start:
                    continue
                if len(out) >= limit:
                    break
                examined = event["seq"]
                if event["tenant"] == tenant:
                    out.append(_public(event))
            else:
                examined = max(examined, self._next_seq - 1)
            return {"events": out, "next_after": examined, "reset": reset}

    def _visible(self, tenant: str, after: int) -> bool:
        return any(e["seq"] > after and e["tenant"] == tenant for e in self._events)


def _public(event: dict[str, Any]) -> dict[str, Any]:
    copied = {k: v for k, v in event.items() if k != "tenant"}
    copied["data"] = dict(event["data"])
    return copied
