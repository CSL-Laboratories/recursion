# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.
"""A fake ``pi`` worker for tests. SYNTHETIC: written by hand from Pi's protocol pages, not captured.

The shapes are those ``routes/engines/pi.py`` cites (earendil-works/pi@bb65e3e4, docs/json.md,
docs/rpc.md, docs/rpc-commands.md, docs/message-types.md). No Pi binary was run to make it.

``FakePi`` has the shape of ``routes.launch.EngineProcess``: ``send(line)``, ``close_stdin()``,
``read_line(timeout_s)`` (a line, ``None`` at EOF, ``ReadTimeout`` when nothing more will come until
something is sent) and ``fence(reason)``. It keeps no thread and no clock.

* ``send(text)`` does what the production ``UnitEngineProcess.send`` does: it writes ``text`` and one
  LF. The fake then reads its stdin as Pi's ``attachJsonlLineReader`` does (rpc/jsonl.ts): records on
  LF, a CR before it stripped, an EMPTY record passed on, so a text that already ended with an LF
  reaches the fake as two records and the second gets Pi's parse error.
* ``FakePi("json", lines=[...])`` replays a one-shot run's stdout and then ends. It reads no stdin, as
  ``pi -p`` given its prompt in argv does not: a send raises ``BrokenPipeError``.
* ``FakePi("rpc")`` answers commands the way Pi's RPC mode does: ``prompt`` (``started`` when idle;
  ``queued`` with ``streamingBehavior: "steer"`` while a run is going; refused without it), ``abort``
  (the run ends ``aborted``, and the response comes after ``agent_settled``, as Pi's abort waits for
  idle), ``get_state``, an ``extension_ui_response`` (no response), a record that is not JSON
  (``command: "parse"``, no id), and anything else (``Unknown command``). Each reply is the next of
  ``replies``. ``session_id`` is what a worker started with ``--session <id>`` reports, so a second
  fake given the first one's id stands for a resumed session.
* A queued steer is held apart from the output the fake has scheduled, as Pi holds it
  (agent-session.ts ``_queueSteer``, ``pendingMessageCount``; agent.ts ``steer``, ``abort``;
  agent-loop.ts). It is taken one at a time where Pi polls its steering queue: when a turn ends and the
  run would end (a turn of its own before ``agent_end``), and when a new run starts (its user message
  right after the prompt's, in the same turn). An abort does not empty the queue: the aborted run ends
  without polling it, and the next prompt's run delivers it. ``pendingMessageCount`` counts the steers
  whose user message has not started.
"""

from __future__ import annotations

import json
from collections import deque

from recursion_core.routes.launch import ReadTimeout

SESSION_ID = "019a5b2e-1111-7000-8000-00000000e801"
SESSION_FILE = "/state/pi/sessions/--work-project--/2026-11-14T22-00-00-000Z_" + SESSION_ID + ".jsonl"
PROVIDER, MODEL, API = "openai", "model-synthetic", "openai-responses"
T0 = 1794693600000   # 2026-11-14T22:00:00Z in milliseconds, as Pi's message timestamps are


def line(record: dict) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def usage(input_tokens: int = 0, output_tokens: int = 0, *, cache_read: int = 0, cache_write: int = 0,
          reasoning: int | None = None, cost: float = 0) -> dict:
    # Pi writes numbers with JSON.stringify: a whole cost is ``0``, never ``0.0``.
    out = {"input": input_tokens, "output": output_tokens, "cacheRead": cache_read, "cacheWrite": cache_write,
           "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
           "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost}}
    if reasoning is not None:
        out["reasoning"] = reasoning
    return out


def user(text: str, at: int) -> dict:
    return {"role": "user", "content": text, "timestamp": at}


def assistant(text: str, at: int, *, stop: str = "stop", used: dict | None = None, calls=(),
              error: str | None = None) -> dict:
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}
        for call_id, name, arguments in calls]
    message = {"role": "assistant", "content": content, "api": API, "provider": PROVIDER, "model": MODEL,
               "usage": used if used is not None else usage(), "stopReason": stop, "timestamp": at}
    if error is not None:
        message["errorMessage"] = error
    return message


def reply_events(prompt: str, reply: str, at: int, *, used: dict | None = None) -> list[dict]:
    """One assistant turn answering ``prompt``: its user message, the streamed reply, the turn's end."""
    final = assistant(reply, at + 100, used=used if used is not None else usage(1000, 20, cost=0.0021))
    partial = dict(final, content=[], stopReason="pending", usage=usage())
    return [
        {"type": "turn_start"},
        {"type": "message_start", "message": user(prompt, at)},
        {"type": "message_end", "message": user(prompt, at)},
        {"type": "message_start", "message": partial},
        {"type": "message_update", "usage": final["usage"],
         "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": reply}},
        {"type": "message_end", "message": final},
        {"type": "turn_end", "message": final, "toolResults": []},
    ]


def run_events(prompt: str, reply: str, at: int, *, used: dict | None = None) -> list[dict]:
    """A whole run: one turn, then the run's end and settle."""
    turn = reply_events(prompt, reply, at, used=used)
    final = turn[-1]["message"]
    return ([{"type": "agent_start"}] + turn
            + [{"type": "agent_end", "messages": [user(prompt, at), final], "willRetry": False},
               {"type": "agent_settled", "aborted": False}])


class FakePi:
    def __init__(self, mode: str, *, lines=(), replies=None, session_id: str = SESSION_ID,
                 session_file: str = SESSION_FILE) -> None:
        if mode not in ("json", "rpc"):
            raise ValueError(mode)
        self.mode, self.session_id, self.session_file = mode, session_id, session_file
        self.sent: list[str] = []
        self.commands: list[dict] = []
        self.fenced: str | None = None
        self.stdin_closed = False
        self._out: deque[str] = deque(lines)    # what is written to stdout next
        self._run: deque[str] = deque()         # the rest of the run in progress, after _out
        self._running = False
        self._aborted = False                   # the run in progress was aborted: it polls no steer
        self._steers: deque[str] = deque()      # steers Pi took whose user message has not started
        self._replies = iter(replies) if replies is not None else (f"reply {n}" for n in range(1, 10**6))
        self._at = T0
        self.messages = 0

    # -- EngineProcess ----------------------------------------------------------------------------------
    def send(self, text: str) -> None:
        if self.mode == "json":
            raise BrokenPipeError("a one-shot pi reads no stdin")
        if self.stdin_closed or self.fenced is not None:
            raise BrokenPipeError("stdin is closed")
        self.sent.append(text)
        for raw in (text + "\n").split("\n")[:-1]:     # the LF the production send adds
            self._command(raw[:-1] if raw.endswith("\r") else raw)

    def close_stdin(self) -> None:
        # Pi shuts down at the end of its stdin; the run in progress is not finished.
        self.stdin_closed = True
        self._run.clear()

    def read_line(self, timeout_s: float):
        if self.fenced is not None:
            return None
        if self._out:
            return self._out.popleft()
        if self._run:
            if self._steers and not self._aborted and json.loads(self._run[0])["type"] == "agent_end":
                self._deliver_steer()                 # the turn ended and the run would end: Pi polls
            out = self._run.popleft()
            record = json.loads(out)
            message = record.get("message") if record["type"] == "message_start" else None
            if message and message.get("role") == "user" and message.get("content") in self._steers:
                self._steers.remove(message["content"])   # Pi drops it from the queue as it starts
            if record["type"] == "agent_settled":
                self._running = False
            return out
        if self.mode == "json" or self.stdin_closed:
            return None
        raise ReadTimeout()

    def fence(self, reason: str) -> None:
        self.fenced = reason
        self._out.clear()
        self._run.clear()

    # -- the RPC commands --------------------------------------------------------------------------------
    def _respond(self, command: dict, success: bool, *, data=None, error=None, later: bool = False) -> None:
        record = {"type": "response", "command": command.get("type"), "success": success}
        if "id" in command:
            record = {"id": command["id"], **record}
        if data is not None:
            record["data"] = data
        if error is not None:
            record["error"] = error
        (self._run if later else self._out).append(line(record))

    def _command(self, raw: str) -> None:
        try:
            command = json.loads(raw)
        except ValueError:
            reason = "Unexpected end of JSON input" if not raw.strip() else "not valid JSON"
            self._out.append(line({"type": "response", "command": "parse", "success": False,
                                   "error": f"Failed to parse command: {reason}"}))
            return
        self.commands.append(command)
        kind = command.get("type")
        if kind == "prompt":
            self._prompt(command)
        elif kind == "abort":
            if self._running:
                self._abort()
            self._respond(command, True, later=self._running)
        elif kind == "get_state":
            self._respond(command, True, data={
                "thinkingLevel": "medium", "isStreaming": self._running, "isCompacting": False,
                "steeringMode": "one-at-a-time", "followUpMode": "one-at-a-time",
                "sessionFile": self.session_file, "sessionId": self.session_id,
                "autoCompactionEnabled": True, "messageCount": self.messages,
                "pendingMessageCount": len(self._steers)})
        elif kind == "extension_ui_response":
            pass
        else:
            self._respond(command, False, error=f"Unknown command: {kind}")

    def _prompt(self, command: dict) -> None:
        text = command.get("message")
        if self._running:
            if command.get("streamingBehavior") != "steer":
                self._respond(command, False, error="Agent is already processing. Specify streamingBehavior "
                                                    "('steer' or 'followUp') to queue the message.")
                return
            self._respond(command, True, data={"disposition": "queued"})
            self._steers.append(text)          # held until Pi's loop polls its steering queue
            return
        self._respond(command, True, data={"disposition": "started"})
        self._running, self._aborted = True, False
        self._at += 10_000
        self.messages += 2
        events = run_events(text, next(self._replies), self._at)
        if self._steers:
            # A new run polls the steering queue first (agent-loop.ts runLoop): one queued steer's user
            # message follows the prompt's in the run's first turn, and the reply answers both.
            steer = user(self._steers[0], self._at + 1)
            events[4:4] = [{"type": "message_start", "message": steer}, {"type": "message_end", "message": steer}]
            self.messages += 1
        self._run.extend(line(e) for e in events)

    def _deliver_steer(self) -> None:
        """The run's turn ended and a steer is queued: Pi takes it into a turn of its own before the run ends."""
        self._at += 1_000
        self.messages += 2
        turn = reply_events(self._steers[0], next(self._replies), self._at)
        self._run.extendleft(line(r) for r in reversed(turn))

    def _abort(self) -> None:
        """End the run now: the reply being streamed stops ``aborted``, then the run settles aborted. The
        queued steers stay queued (Pi's abort does not clear them)."""
        self._aborted = True
        records = [json.loads(r) for r in self._run]
        streaming = next((r["message"] for r in records
                          if r["type"] == "message_end" and r["message"].get("role") == "assistant"), None)
        self._run.clear()
        if streaming is not None:
            stopped = dict(streaming, content=[], stopReason="aborted", errorMessage="Request was aborted",
                           usage=usage())
            self._run.extend(line(r) for r in (
                {"type": "message_end", "message": stopped},
                {"type": "turn_end", "message": stopped, "toolResults": []}))
        self._run.extend(line(r) for r in (
            {"type": "agent_end", "messages": [], "willRetry": False},
            {"type": "agent_settled", "aborted": True}))
