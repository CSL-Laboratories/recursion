"""The TUI as a control API client (CSL-799, candidate). See README.md.

``TuiApp`` holds view state only: the last answers it was given, which run is selected, a bounded
copy of engine output and one ordered event log (``events.EventLog``). It owns no session, run,
approval or plugin state. Opening reads ``run.list``; attaching reads ``run.status``; closing sends
nothing. Every request goes through the injected ``api.call(method, params)``, the same surface the
CLI uses.

Every request, answer, condition edge, engine output chunk, notice and error is appended to the log
before anything shows it. The full-screen frame, the inline painter and the linear (screen-reader)
renderer all read that log. A condition is logged only when it appears, changes or clears; the
``conditions`` command logs the current set on demand.

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
from recursion_core.tui.events import LOG_CAP, Event, EventLog
from recursion_core.tui.sanitize import OutputSanitizer, field, wrap
from recursion_core.tui.view import Row, color_enabled

Dispatch = Callable[[str, dict, Callable[[str, dict, dict], None]], None]
OUTPUT_LINES = 2000  # per run; older engine lines leave the view (the run's own record is elsewhere)
RUN_ROWS = 5
CONDITION_ROWS = 4


def _fmt(params: Mapping) -> str:
    return " ".join(f"{k}={field(v)}" for k, v in sorted(params.items()))


class _Output:
    def __init__(self, cap: int, run_id: str) -> None:
        self.lines: collections.deque[str] = collections.deque(maxlen=cap)
        self.partial = ""
        self.sanitizer = OutputSanitizer()
        self.events = 0
        self.dropped = 0
        self.label = f"output run={field(run_id)}: "

    def feed(self, text: str | bytes) -> tuple[str, list[str]]:
        """Sanitize one chunk; return it and the lines it finished."""

        self.events += 1
        chunk = self.sanitizer.feed(text)
        *done, self.partial = (self.partial + chunk).split("\n")
        self.lines.extend(done)
        return chunk, done

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
                 dispatch: Dispatch | None = None, output_lines: int = OUTPUT_LINES,
                 log_cap: int = LOG_CAP) -> None:
        self._api, self._clock = api, clock
        self.dispatch: Dispatch = dispatch or self._inline
        self.color = color_enabled(environ or {})
        self.width, self.height, self.stale_after_s = max(width, 20), max(height, 16), stale_after_s
        self._output_lines = output_lines
        self.log = EventLog(log_cap)
        self._t0 = clock()
        self.runs: list[dict] = []
        self.selected: str | None = None
        self.attached: str | None = None
        self.snapshot_at: float | None = None
        self.refresh_failed: str | None = None
        self.last: dict | None = None
        self.last_method: str | None = None
        self._answer_keyed: list[tuple[str, str, str]] = []
        self.answer_conditions: list[tuple[str, str]] = []
        self.shown: dict[tuple[str, str], tuple[str, str]] = {}  # conditions as last logged
        self._uncertain: dict[str, str] = {}  # per run, until the API shows that run is no longer stop_pending
        self.errors: list[str] = []
        self.last_error: Event | None = None  # kept here too, so a bounded log cannot drop it
        self.mode, self.line, self.stop_target = "keys", "", None
        self.notice = ""
        self.closed = False
        self._state = "live"
        self.skipped_events = 0  # engine output events a Loop dropped from its presentation queue
        self.outputs: dict[str, _Output] = {}
        self.in_flight: dict[str, int] = collections.defaultdict(int)

    # -- the event log --------------------------------------------------------------------------
    def _emit(self, event_type: str, source: str, text: str, speech: str | None = None,
              state: str | None = None) -> Event:
        ms = int(round((self._clock() - self._t0) * 1000))
        return self.log.append(ms, event_type, source, state or self._view_state(), text,
                               text if speech is None else speech, self._focus())

    @property
    def transcript(self) -> list[str]:
        """Every logged line except engine output, in order (append-only)."""

        return [e.text for e in self.log if e.event_type != "output"]

    def _view_state(self) -> str:
        self._state = "closed" if self.closed else "watch_only" if self.watch_only else "live"
        return self._state

    def _focus(self) -> str:
        if self.mode == "confirm_stop":
            return f"confirm_stop run={field(self.stop_target)}"
        if self.mode == "command":
            return "command_line"
        return f"runs run={field(self.selected)}" if self.selected else "runs"

    # -- lifecycle: opening and attaching are reads; closing sends nothing ----------------------
    def open(self) -> None:
        self._emit("open", "operator", f"open surface={p.SURFACE}: reads run.list; starts nothing")
        self.request("run.list", {})

    def attach(self, run_id: str) -> None:
        self.attached = self.selected = run_id
        self._emit("attach", "operator", f"attach run={field(run_id)}: reads run.status; starts nothing")
        self.request("run.status", {"run_id": run_id})

    def refresh(self) -> None:
        self.request("run.list", {})
        if self.attached:
            self.request("run.status", {"run_id": self.attached})

    def close(self) -> None:
        self.closed = True
        self._emit("close", "operator", "closed: no request sent; runs keep going and can be reattached")

    # -- requests -------------------------------------------------------------------------------
    def request(self, method: str, params: dict) -> None:
        if method not in p.ALLOWED_METHODS:  # defensive: nothing outside the CLI's method set leaves the TUI
            raise ValueError(method)
        if method == "run.start" and self.watch_only:
            self._error(f"refused locally: start while watch_only ({self._stale_text()}); refresh first")
            return
        self.in_flight[method] += 1
        self._emit("request", "operator", f"request {method} {_fmt(params)}".rstrip())
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
        self._answer_keyed = p.keyed_conditions(answer)
        self.answer_conditions = [(label, text) for label, _, text in self._answer_keyed]
        self._uncertain.update({subject: text for label, subject, text in self._answer_keyed if label == p.UNCERTAIN})
        states = {f"run={r.get('run_id')}": r.get("state") for r in self.runs}
        for subject in list(self._uncertain):  # a later answer about another run does not clear it
            if states.get(subject, "stop_pending") != "stop_pending":
                del self._uncertain[subject]
        labels = ",".join(sorted({label for label, _ in self.answer_conditions})) or "none"
        said = " ".join(x for x in (f"answer {method} status={field(code)}", self._answer_fields(answer)) if x)
        # The full line keeps the view state for the transcript; speech leaves it to the condition edges.
        self._emit("answer", "recursion", f"{said} surface={p.SURFACE} watch_only={str(self.watch_only).lower()} "
                                         f"conditions={labels}", speech=said)
        for label, text in self.answer_conditions:
            if label in (p.REFUSED, p.UNAVAILABLE_LABEL):
                self._error(f"{label}: {method} status={field(code)} {field(text)}")
        if method == "run.stop":  # the answer line above already said it
            self._notice(f"stop run={field(params.get('run_id'))}: control API answered {field(code)}", speech="")
        self.sync_conditions()

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
            out = self.outputs[run_id] = _Output(self._output_lines, run_id)
        chunk, done = out.feed(event.get("text", ""))
        # Logged already sanitized; the linear renderer says each finished line once, marked as output.
        self._emit("output", "engine", chunk, speech="\n".join(out.label + line for line in done),
                   state=self._state)

    # -- keys -----------------------------------------------------------------------------------
    def press(self, key: str) -> None:
        if self.mode == "confirm_stop":
            target, self.mode, self.stop_target = self.stop_target, "keys", None
            if key in ("y", "Y"):
                self.stop(target)
            else:
                self._notice(f"stop of {field(target)} cancelled; nothing sent")
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
                self._notice("no run selected; nothing to stop")
        elif key == ":":
            self.mode, self.line = "command", ""
        elif key == "q":
            self.close()

    def command(self, line: str) -> None:
        """A command line in the CLI's grammar: ``list``, ``status RUN``, ``stop RUN``, ``start ...``,
        ``conditions``."""

        try:
            method, params = p.parse_command(line)
        except p.CommandError as exc:
            self._error(f"usage error: {field(exc)}; nothing sent")
            return
        if method == p.CONDITIONS:
            self.show_conditions()
            return
        if method == "run.status":
            self.attached = self.selected = params["run_id"]
        self.request(method, params)

    def stop(self, run_id: str) -> None:
        self._notice(f"stop requested for {field(run_id)}; waiting for the control API answer")
        self.request("run.stop", {"run_id": run_id})

    def show_conditions(self) -> None:
        """The ``conditions`` command: log every current condition again, for a user who missed one."""

        self.sync_conditions()
        rows = [f"{label}: {text}" for label, text in self.shown.values()]
        head = f"conditions now: {len(rows)}" if rows else "conditions now: none"
        self._emit("conditions", "recursion", "\n".join([head, *rows]))

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
        # No running numbers here: the text names the condition, so it is logged once, not every tick.
        reasons = []
        if self.refresh_failed:
            reasons.append(f"refresh failed: {self.refresh_failed}")
        if any(label == p.STALE for label, _ in self.answer_conditions):
            reasons.append("API marked its answer stale")
        if self.snapshot_at is not None and self._clock() - self.snapshot_at > self.stale_after_s:
            reasons.append(f"snapshot older than {self.stale_after_s:.1f}s")
        return "; ".join(reasons)

    def _current(self) -> dict[tuple[str, str], tuple[str, str]]:
        found: dict[tuple[str, str], tuple[str, str]] = {}
        for label, subject, text in self._answer_keyed:
            if label not in (p.STALE, p.UNCERTAIN):
                found.setdefault((label, subject), (label, text))
        for subject, text in self._uncertain.items():
            found[(p.UNCERTAIN, subject)] = (p.UNCERTAIN, text)
        if self.stale:
            found[(p.STALE, "view")] = (p.STALE, f"watch_only (start disabled; stop and reads work): "
                                                 f"{self._stale_text()}")
        for label, subject, text in p.keyed_conditions({"code": "ok", "runs": self.runs}):  # the snapshot's own
            found.setdefault((label, subject), (label, text))
        return dict(sorted(found.items(), key=lambda kv: p.LABELS.index(kv[0][0])))

    def conditions(self) -> list[tuple[str, str]]:
        return list(self._current().values())

    def sync_conditions(self) -> None:
        """Log each condition that appeared, changed or cleared since the last sync; nothing else."""

        now = self._current()
        for key, (label, text) in self.shown.items():
            if key not in now:
                self._emit("condition", "recursion", f"{label} cleared, was: {text}")
        for key, (label, text) in now.items():
            old = self.shown.get(key)
            if old is None:
                self._emit("condition", "recursion", f"{label}: {text}")
            elif old[1] != text:
                self._emit("condition", "recursion", f"{label} changed: {text}")
        self.shown = now
        self._view_state()

    # -- frame ----------------------------------------------------------------------------------
    def frame(self) -> list[Row]:
        """One full-screen frame. It first logs any condition edge (one may appear with time alone)."""

        self.sync_conditions()
        w = self.width
        rows: list[Row] = []

        def put(text: str, style: str | None = None) -> None:  # a clipped row ends in "…"; the transcript has it all
            parts = wrap(text, w) if text else [""]
            rows.append((parts[0] if len(parts) == 1 else wrap(text, w - 1)[0] + "…", style))

        mode = "watch_only" if self.watch_only else "live"
        put(f"Recursion TUI | surface={p.SURFACE} | view={mode} | runs={len(self.runs)} | control API client",
            "header")
        put(self.status_row())
        conds = list(self.shown.values())
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
        put(self.error_row(), "error" if self.last_error else None)
        put(self.notice)
        put(self.prompt(), "prompt")
        return rows

    def status_row(self) -> str:
        if self.last is None:
            return "status=none (no answer yet)"
        return " ".join(x for x in (f"status={field(self.last.get('code'))}", f"method={self.last_method}",
                                    self._answer_fields(self.last)) if x)

    def error_row(self) -> str:
        """The latest error stays until a newer error replaces it."""

        return self.last_error.text if self.last_error else "errors: none"

    def live_rows(self) -> list[Row]:
        """The inline mode's live region: status, current condition labels, the error that stays,
        the notice and the input line. Each row is clipped one cell short of the width (no autowrap)."""

        self.sync_conditions()
        w = self.width - 1

        def clip(text: str) -> str:
            parts = wrap(text, w) if text else [""]
            return parts[0] if len(parts) == 1 else wrap(text, w - 1)[0] + "…"

        labels = ",".join(dict.fromkeys(label for label, _ in self.shown.values())) or "none"
        mode = "watch_only" if self.watch_only else "live"
        rows: list[Row] = [
            (clip(f"surface={p.SURFACE} view={mode} runs={len(self.runs)} | {self.status_row()}"), "header"),
            (clip(f"conditions now: {labels}"), None),
            (clip(self.error_row()), "error" if self.last_error else None),
        ]
        if self.notice:
            rows.append((clip(self.notice), None))
        rows.append((clip(self.prompt()), "prompt"))
        return rows

    def prompt(self) -> str:
        if self.mode == "confirm_stop":
            return f"recursion: stop run {field(self.stop_target)}? y sends run.stop, any other key cancels"
        if self.mode == "command":
            return f"recursion: {self.line}"
        return "recursion: j/k select  a attach  r refresh  s stop  : command  q close (runs keep going)"

    # -- notices and errors (logged; an error is never redrawn away) -----------------------------
    def _notice(self, text: str, speech: str | None = None) -> None:
        self.notice = text
        self._emit("notice", "recursion", text, speech=speech)

    def _error(self, line: str) -> None:
        self.errors.append(line)
        self.last_error = self._emit("error", "recursion", f"error {len(self.errors)}: {line}")


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
                 tick_s: float = 1 / 60, drain_budget_s: float = 0.008, event_cap: int = 50_000,
                 render: Callable[[], list[Row]] | None = None) -> None:
        self.app, self._api, self._sink = app, api, sink
        self._render = render or app.frame  # inline mode paints from the log and needs no frame
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
        self.frame = self._render()  # 4. one repaint
        self.frames += 1
        if self._sink is not None:
            self._sink(self.frame)
