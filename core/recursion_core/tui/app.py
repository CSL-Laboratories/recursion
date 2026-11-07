"""The TUI as a control API client (CSL-799, candidate). See README.md.

``TuiApp`` holds view state only: the last answers it was given, which run is selected, a bounded
copy of engine output and an append-only transcript. It owns no session, run, approval or plugin
state. Opening reads ``run.list``; attaching reads ``run.status``; closing sends nothing. Every
request goes through the injected ``api.call(method, params)``, the same surface the CLI uses.

``Loop`` runs a ``TuiApp`` on its own thread for a real terminal: key presses are handled before
queued engine output, output draining is time-sliced, and ``run.stop`` has its own request lane, so a
Stop never waits behind an output flood or a slow read and never needs an input lease.
"""

from __future__ import annotations

import collections
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping

from recursion_core.tui import protocol as p
from recursion_core.tui.sanitize import OutputSanitizer, field, wrap
from recursion_core.tui.view import Row, color_enabled

Dispatch = Callable[[str, dict, Callable[[str, dict, dict], None]], None]
OUTPUT_LINES = 2000  # per run; older engine lines leave the view (the run's own record is elsewhere)
RUN_ROWS = 5
CONDITION_ROWS = 4


def _fmt(params: Mapping) -> str:
    return " ".join(f"{k}={field(v)}" for k, v in sorted(params.items()))


class _Output:
    def __init__(self, cap: int) -> None:
        self.lines: collections.deque[str] = collections.deque(maxlen=cap)
        self.partial = ""
        self.sanitizer = OutputSanitizer()
        self.events = 0
        self.dropped = 0

    def feed(self, text: str | bytes) -> None:
        self.events += 1
        clean = self.partial + self.sanitizer.feed(text)
        *done, self.partial = clean.split("\n")
        self.lines.extend(done)

    def tail(self, rows: int, width: int) -> list[str]:
        out: list[str] = []
        for line in reversed([*self.lines, self.partial] if self.partial else self.lines):
            out[:0] = wrap(line, width)
            if len(out) >= rows:
                break
        return out[-rows:] if rows else []


class TuiApp:
    def __init__(self, api, *, clock: Callable[[], float], environ: Mapping[str, str] | None = None,
                 width: int = 100, height: int = 30, stale_after_s: float = 10.0,
                 dispatch: Dispatch | None = None, output_lines: int = OUTPUT_LINES) -> None:
        self._api, self._clock = api, clock
        self.dispatch: Dispatch = dispatch or self._inline
        self.color = color_enabled(environ or {})
        self.width, self.height, self.stale_after_s = max(width, 20), max(height, 16), stale_after_s
        self._output_lines = output_lines
        self.runs: list[dict] = []
        self.selected: str | None = None
        self.attached: str | None = None
        self.snapshot_at: float | None = None
        self.refresh_failed: str | None = None
        self.last: dict | None = None
        self.last_method: str | None = None
        self.answer_conditions: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.transcript: list[str] = []
        self.mode, self.line, self.stop_target = "keys", "", None
        self.notice = ""
        self.closed = False
        self.skipped_events = 0  # engine output events a Loop dropped from its presentation queue
        self.outputs: dict[str, _Output] = {}
        self.in_flight: dict[str, int] = collections.defaultdict(int)

    # -- lifecycle: opening and attaching are reads; closing sends nothing ----------------------
    def open(self) -> None:
        self._note(f"open surface={p.SURFACE}: reads run.list; starts nothing")
        self.request("run.list", {})

    def attach(self, run_id: str) -> None:
        self.attached = self.selected = run_id
        self._note(f"attach run={field(run_id)}: reads run.status; starts nothing")
        self.request("run.status", {"run_id": run_id})

    def refresh(self) -> None:
        self.request("run.list", {})
        if self.attached:
            self.request("run.status", {"run_id": self.attached})

    def close(self) -> None:
        self.closed = True
        self._note("closed: no request sent; runs keep going and can be reattached")

    # -- requests -------------------------------------------------------------------------------
    def request(self, method: str, params: dict) -> None:
        if method not in p.ALLOWED_METHODS:  # defensive: nothing outside the CLI's method set leaves the TUI
            raise ValueError(method)
        if method == "run.start" and self.watch_only:
            self._error(f"refused locally: start while watch_only ({self._stale_text()}); refresh first")
            return
        self.in_flight[method] += 1
        self._note(f"request {method} {_fmt(params)}".rstrip())
        self.dispatch(method, params, self.on_answer)

    def _inline(self, method: str, params: dict, done: Callable[[str, dict, dict], None]) -> None:
        done(method, params, call_api(self._api, method, params))

    def on_answer(self, method: str, params: dict, answer: dict) -> None:
        self.in_flight[method] = max(0, self.in_flight[method] - 1)
        self.last, self.last_method = answer, method
        code = answer.get("code")
        if method == "run.list":
            if code == "ok" and isinstance(answer.get("runs"), list):
                self.runs = [r for r in answer["runs"] if isinstance(r, dict)]
                self.snapshot_at, self.refresh_failed = self._clock(), None
                ids = [str(r.get("run_id")) for r in self.runs]
                if self.selected not in ids:
                    self.selected = ids[0] if ids else None
            else:
                self.refresh_failed = f"run.list answered {field(code)}"
        elif code == "ok" and isinstance(answer.get("run"), dict):
            self._merge(answer["run"])
        elif code in ("stopped", "pending") and isinstance(answer.get("run_id"), str):
            for run in self.runs:
                if run.get("run_id") == answer["run_id"]:
                    run["state"] = "stopped" if code == "stopped" else "stop_pending"
                    run["stop"] = {**(run.get("stop") or {}), "reason": answer.get("reason")}
        elif code == "started" and isinstance(answer.get("run"), dict):
            self._merge(answer["run"])
        self.answer_conditions = p.answer_conditions(answer)
        labels = ",".join(sorted({label for label, _ in self.answer_conditions})) or "none"
        parts = [f"answer {method} status={field(code)}", self._answer_fields(answer), f"surface={p.SURFACE}",
                 f"watch_only={str(self.watch_only).lower()}", f"conditions={labels}"]
        self._note(" ".join(x for x in parts if x))
        if method == "run.stop":
            self.notice = f"stop run={field(params.get('run_id'))}: control API answered {field(code)}"
        for label, text in self.answer_conditions:
            if label in (p.REFUSED, p.UNAVAILABLE_LABEL):
                self._error(f"{label}: {method} status={field(code)} {field(text)}")

    def _merge(self, run: dict) -> None:
        for i, known in enumerate(self.runs):
            if known.get("run_id") == run.get("run_id"):
                self.runs[i] = run
                return
        self.runs.append(run)

    @staticmethod
    def _answer_fields(answer: Mapping) -> str:
        keys = [k for k in ("run_id", "state", "reason", "detail", "repeat", "duplicate") if k in answer]
        return " ".join(f"{k}={field(answer[k])}" for k in keys)

    # -- engine output (no control API stream exists yet; the caller feeds events here) ----------
    def on_event(self, event: Mapping) -> None:
        run_id = str(event.get("run_id"))
        out = self.outputs.get(run_id)
        if out is None:
            out = self.outputs[run_id] = _Output(self._output_lines)
        out.feed(event.get("text", ""))

    # -- keys -----------------------------------------------------------------------------------
    def press(self, key: str) -> None:
        if self.mode == "confirm_stop":
            target, self.mode, self.stop_target = self.stop_target, "keys", None
            if key in ("y", "Y"):
                self.stop(target)
            else:
                self.notice = f"stop of {field(target)} cancelled; nothing sent"
            return
        if self.mode == "command":
            if key == "enter":
                line, self.mode, self.line = self.line, "keys", ""
                self.command(line)
            elif key == "esc":
                self.mode, self.line = "keys", ""
            elif key == "backspace":
                self.line = self.line[:-1]
            elif len(key) == 1 and key.isprintable():
                self.line += key
            return
        if key in ("j", "down", "k", "up"):
            self._move(1 if key in ("j", "down") else -1)
        elif key in ("a", "enter") and self.selected:
            self.attach(self.selected)
        elif key == "r":
            self.refresh()
        elif key == "s":
            if self.selected:  # the target is fixed now; a later refresh or output cannot change it
                self.mode, self.stop_target = "confirm_stop", self.selected
            else:
                self.notice = "no run selected; nothing to stop"
        elif key == ":":
            self.mode, self.line = "command", ""
        elif key == "q":
            self.close()

    def command(self, line: str) -> None:
        """A command line in the CLI's grammar: ``list``, ``status RUN``, ``stop RUN``, ``start ...``."""

        try:
            method, params = p.parse_command(line)
        except p.CommandError as exc:
            self._error(f"usage error: {field(exc)}; nothing sent")
            return
        if method == "run.status":
            self.attached = self.selected = params["run_id"]
        self.request(method, params)

    def stop(self, run_id: str) -> None:
        self.notice = f"stop requested for {field(run_id)}; waiting for the control API answer"
        self.request("run.stop", {"run_id": run_id})

    def _move(self, step: int) -> None:
        ids = [str(r.get("run_id")) for r in self.runs]
        if ids:
            i = ids.index(self.selected) if self.selected in ids else 0
            self.selected = ids[(i + step) % len(ids)]

    # -- derived state --------------------------------------------------------------------------
    @property
    def stale(self) -> bool:
        if self.refresh_failed or any(label == p.STALE for label, _ in self.answer_conditions):
            return True
        return self.snapshot_at is not None and self._clock() - self.snapshot_at > self.stale_after_s

    @property
    def watch_only(self) -> bool:
        """Stale forces watch_only: start is refused locally; reads and stop stay available."""

        return self.stale

    def _stale_text(self) -> str:
        reasons = []
        if self.refresh_failed:
            reasons.append(f"refresh failed: {self.refresh_failed}")
        if any(label == p.STALE for label, _ in self.answer_conditions):
            reasons.append("API marked its answer stale")
        if self.snapshot_at is not None and self._clock() - self.snapshot_at > self.stale_after_s:
            reasons.append(f"snapshot age {self._clock() - self.snapshot_at:.1f}s > {self.stale_after_s:.1f}s")
        return "; ".join(reasons)

    def conditions(self) -> list[tuple[str, str]]:
        found = [c for c in self.answer_conditions if c[0] != p.STALE]
        if self.stale:
            found.append((p.STALE, f"watch_only (start disabled; stop and reads work): {self._stale_text()}"))
        for cond in p.answer_conditions({"code": "ok", "runs": self.runs}):  # what the snapshot itself says
            if cond not in found:
                found.append(cond)
        return sorted(found, key=lambda c: p.LABELS.index(c[0]))

    # -- frame ----------------------------------------------------------------------------------
    def frame(self) -> list[Row]:
        w = self.width
        rows: list[Row] = []

        def put(text: str, style: str | None = None) -> None:  # a clipped row ends in "…"; the transcript has it all
            parts = wrap(text, w) if text else [""]
            rows.append((parts[0] if len(parts) == 1 else wrap(text, w - 1)[0] + "\u2026", style))

        mode = "watch_only" if self.watch_only else "live"
        put(f"Recursion TUI | surface={p.SURFACE} | view={mode} | runs={len(self.runs)} | control API client",
            "header")
        if self.last is None:
            put("status=none (no answer yet)")
        else:
            put(" ".join(x for x in (f"status={field(self.last.get('code'))}", f"method={self.last_method}",
                                     self._answer_fields(self.last)) if x))
        conds = self.conditions()
        shown = conds[:CONDITION_ROWS]
        for label, text in shown:
            put(f"{label}: {text}", label)
        if len(conds) > CONDITION_ROWS:
            rows.pop()
            put(f"(+{len(conds) - CONDITION_ROWS + 1} more conditions, listed in the transcript)", shown[-1][0])
        for _ in range(CONDITION_ROWS - len(shown)):
            put("conditions: none" if not conds and _ == 0 else "")
        put("runs:")
        ids = [str(r.get("run_id")) for r in self.runs]
        start = max(0, min(ids.index(self.selected) - RUN_ROWS + 1 if self.selected in ids else 0, len(ids)))
        for run in self.runs[start:start + RUN_ROWS]:
            rid = str(run.get("run_id"))
            mark = ">" if rid == self.selected else " "
            att = " attached" if rid == self.attached else ""
            put(f"{mark} {field(rid)} state={field(run.get('state'))} engine={field(run.get('engine'))} "
                f"tenant={field(run.get('tenant'))}{att}", "selected" if rid == self.selected else None)
        for _ in range(RUN_ROWS - len(self.runs[start:start + RUN_ROWS])):
            put("")
        out = self.outputs.get(self.attached or "")
        put(f"-- output run={field(self.attached) if self.attached else 'none'} (untrusted engine text, "
            f"display only)" + (f" skipped_events={self.skipped_events}" if self.skipped_events else "") + " --")
        out_rows = self.height - len(rows) - 3
        lines = out.tail(out_rows, w - 2) if out else []
        for line in lines:
            put("│ " + line)
        for _ in range(out_rows - len(lines)):
            put("│")
        if self.errors:
            put(f"error {len(self.errors)}: {self.errors[-1]}", "error")
        else:
            put("errors: none")
        put(self.notice)
        put(self._prompt(), "prompt")
        return rows

    def _prompt(self) -> str:
        if self.mode == "confirm_stop":
            return f"recursion: stop run {field(self.stop_target)}? y sends run.stop, any other key cancels"
        if self.mode == "command":
            return f"recursion: {self.line}"
        return "recursion: j/k select  a attach  r refresh  s stop  : command  q close (runs keep going)"

    # -- transcript and errors (append-only: an error is never redrawn away) ---------------------
    def _note(self, line: str) -> None:
        self.transcript.append(line)

    def _error(self, line: str) -> None:
        self.errors.append(line)
        self._note(f"error {len(self.errors)}: {line}")


def call_api(api, method: str, params: dict) -> dict:
    """One control API call. A transport failure becomes a typed answer, not a crash."""

    try:
        answer = api.call(method, dict(params))
    except Exception as err:  # the API client's own failure is shown as unavailable, never as success
        return {"code": p.UNAVAILABLE, "detail": type(err).__name__}
    if not isinstance(answer, dict):
        return {"code": p.UNAVAILABLE, "detail": "answer_not_object"}
    return json.loads(json.dumps(answer, default=str))  # a private copy: the view never aliases API state


class Loop:
    """Runs a TuiApp on one thread for a real terminal (or a load test).

    Keys and API answers are handled before engine output each tick; output draining stops after
    ``drain_budget_s`` so one tick stays short under any flood; ``run.stop`` has its own lane.
    """

    def __init__(self, app: TuiApp, api, *, sink: Callable[[list[Row]], None] | None = None,
                 tick_s: float = 1 / 60, drain_budget_s: float = 0.008, event_cap: int = 50_000) -> None:
        self.app, self._api, self._sink = app, api, sink
        self.tick_s, self.drain_budget_s, self.event_cap = tick_s, drain_budget_s, event_cap
        self._keys: queue.SimpleQueue = queue.SimpleQueue()
        self._answers: queue.SimpleQueue = queue.SimpleQueue()
        self._events: collections.deque = collections.deque()
        self._events_lock = threading.Lock()
        self.dropped_events = 0
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._lanes = {"stop": queue.SimpleQueue(), "other": queue.SimpleQueue()}
        self._threads: list[threading.Thread] = []
        self.frame: list[Row] = []
        self.frames = 0
        app.dispatch = self._dispatch

    # thread-safe inputs
    def press(self, key: str) -> None:
        self._keys.put(key)
        self._wake.set()

    def post_event(self, event: Mapping) -> None:
        with self._events_lock:
            if len(self._events) >= self.event_cap:  # presentation copy only; dropping is counted and shown
                self._events.popleft()
                self.dropped_events += 1
            self._events.append(event)

    def _dispatch(self, method: str, params: dict, done) -> None:
        self._lanes["stop" if method == "run.stop" else "other"].put((method, params, done))

    def _lane(self, q: queue.SimpleQueue) -> None:
        while True:
            item = q.get()
            if item is None:
                return
            method, params, done = item
            self._answers.put((done, method, params, call_api(self._api, method, params)))
            self._wake.set()

    def start(self) -> None:
        for lane in self._lanes.values():
            t = threading.Thread(target=self._lane, args=(lane,), daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        self._threads.append(t)

    def shutdown(self) -> None:
        self._halt.set()
        self._wake.set()
        for lane in self._lanes.values():
            lane.put(None)
        for t in self._threads:
            t.join(timeout=5)

    def run(self) -> None:
        self.app.open()
        while not self._halt.is_set() and not self.app.closed:
            self._wake.wait(self.tick_s)
            self._wake.clear()
            self.tick()

    def tick(self) -> None:
        app = self.app
        while True:  # 1. keys first: Stop never waits behind output
            try:
                app.press(self._keys.get_nowait())
            except queue.Empty:
                break
        while True:  # 2. API answers
            try:
                done, method, params, answer = self._answers.get_nowait()
            except queue.Empty:
                break
            done(method, params, answer)
        deadline = time.monotonic() + self.drain_budget_s  # 3. engine output, time-sliced
        while time.monotonic() < deadline:
            with self._events_lock:
                batch = [self._events.popleft() for _ in range(min(256, len(self._events)))]
            if not batch:
                break
            for event in batch:
                app.on_event(event)
            if not self._keys.empty():
                break
        if self.dropped_events:
            app.skipped_events = self.dropped_events
        self.frame = app.frame()  # 4. one repaint
        self.frames += 1
        if self._sink is not None:
            self._sink(self.frame)
