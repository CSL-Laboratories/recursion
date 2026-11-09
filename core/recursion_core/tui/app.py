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
from recursion_core.tui.sanitize import OutputSanitizer, cell_width, field, wrap
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
        self.width = self.height = 0
        self.resize(width, height)
        self.stale_after_s = stale_after_s
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
        self._uncertain: dict[str, str] = {}  # per run, until the API lists that run with no unconfirmed Stop
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
        open_stop = {f"run={r.get('run_id')}": p.stop_unconfirmed(r) for r in self.runs}
        for subject in list(self._uncertain):  # a later answer about another run does not clear it
            if not open_stop.get(subject, True):
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
        """One full-screen frame of exactly ``self.height`` rows, none wider than ``self.width`` cells.

        It first logs any condition edge (one may appear with time alone). Every current condition stays
        on screen, and a condition's text or run id is wrapped onto more rows, never cut at the width:
        each condition in full when they fit; else one row per label with its subjects (run ids) named
        whole on the rows below it; else, when even those rows outnumber the terminal's, every label's row
        with as many names as fit (UNCERTAIN first) and a row saying how many are not named; else (a
        terminal only a few rows tall) one row naming every label. Runs, output, the notice, the header
        and the status row give up their rows first; conditions, the error row and the prompt last."""

        self.sync_conditions()
        w, h = self.width, self.height

        def clip(text: str) -> str:  # a clipped row ends in "…"; the transcript has it all
            parts = wrap(text, w) if text else [""]
            if len(parts) == 1:
                return parts[0]
            return wrap(text, w - 1)[0] + "…" if w > 1 else "…"

        def flow(text: str, style: str | None) -> list[tuple[str, str | None]]:
            # all of ``text`` on as many rows as it needs, broken at a space when one fits (later rows
            # indented): a run id is never cut off at the width
            pad = "  " if w > 2 else ""
            out, rest = [], text
            while rest or not out:
                lead = pad if out else ""
                chunk = wrap(rest, w - len(lead))[0]
                if len(chunk) < len(rest) and " " in chunk[1:] and (out or " " in chunk[:chunk.rindex(" ")]):
                    # (the first row never breaks right after its "LABEL:", so the label keeps its text)
                    chunk = chunk[:chunk.rindex(" ") + 1]
                out.append(((lead + chunk).rstrip(" ") or lead + chunk, style))
                rest = rest[len(chunk):].lstrip(" ")
            return out

        def names(label: str, found: list[tuple[str, str]]) -> list[tuple[str, str | None, int]]:
            # each subject (a run id, or "api"/"answer"/"view") whole, packed on indented rows;
            # the int is how many subjects that row completes, so a cut list can count what it left out
            packed: list[tuple[str, int]] = []
            cur, n = "", 0
            for subject, _ in found:
                name = field(subject[4:] if subject.startswith("run=") else subject)
                joined = f"{cur}, {name}" if cur else f"  {name}"
                if cur and sum(cell_width(c) for c in joined) + 1 > w:  # room for the trailing ","
                    packed.append((cur + ",", n))
                    joined, n = f"  {name}", 0
                cur, n = joined, n + 1
            packed.append((cur, n))
            out = []
            for row, count in packed:
                part = flow(row, label)
                out += [(text, style, count if i == len(part) - 1 else 0) for i, (text, style) in enumerate(part)]
            return out

        keyed = list(self.shown.items())
        detail = [r for (label, _), (_, text) in keyed for r in flow(f"{label}: {text}", label)]
        by_label: dict[str, list[tuple[str, str]]] = {}
        for (label, subject), (_, text) in keyed:
            by_label.setdefault(label, []).append((subject, text))
        # one head row per label, then (for several conditions, or any run's) the subjects named in full
        heads = {label: (f"{label}: {found[0][1]}" if len(found) == 1 else
                         f"{label}: {len(found)} active, listed below: {found[0][1]}", label)
                 for label, found in by_label.items()}
        listed = {label: names(label, found) if len(found) > 1 or found[0][0].startswith("run=") else []
                  for label, found in by_label.items()}
        grouped = [row for label in by_label for row in (heads[label], *((t, s) for t, s, _ in listed[label]))]
        summary = [(f"{','.join(by_label)}: {len(keyed)} conditions active (terminal too small for detail; "
                    f"the transcript lists them)", next(iter(by_label), None))]  # labels first: they survive clipping
        fixed = 5  # header, status, error, notice, prompt
        budget = max(1, h - 2)  # header, status and notice give way; the error row and the prompt stay
        cond = next((tier for tier in (detail, grouped) if tier and len(tier) + fixed <= h), None)
        if cond is None and keyed and len(grouped) <= budget:
            cond = grouped
        if cond is None and keyed and len(by_label) <= budget:
            cond = self._squeeze(by_label, heads, listed, budget)
        if cond is None:
            cond = summary if keyed else [("conditions: none", None)]
        rem = max(0, h - fixed - len(cond))
        pad = min(max(0, CONDITION_ROWS - len(cond)), max(0, rem - (RUN_ROWS + 3)))  # keep the layout steady
        rem -= pad
        run_rows = min(RUN_ROWS, rem - 1) if rem >= 2 else 0
        rem -= run_rows + (1 if run_rows else 0)

        mode = "watch_only" if self.watch_only else "live"
        # (priority, text, style) in screen order; when the terminal is too short the highest number goes first
        rows: list[tuple[int, str, str | None]] = [
            (3, f"Recursion TUI | surface={p.SURFACE} | view={mode} | runs={len(self.runs)} | control API client",
             "header"),
            (2, self.status_row(), None),
            *((0, text, style) for text, style in cond),
            *((0, "", None) for _ in range(pad)),
        ]
        if run_rows:
            rows.append((5, "runs:", None))
            ids = [str(r.get("run_id")) for r in self.runs]
            start = max(0, min(ids.index(self.selected) - run_rows + 1 if self.selected in ids else 0, len(ids)))
            for run in self.runs[start:start + run_rows]:
                rid = str(run.get("run_id"))
                mark = ">" if rid == self.selected else " "
                att = " attached" if rid == self.attached else ""
                rows.append((5, f"{mark} {field(rid)} state={field(run.get('state'))} "
                                f"engine={field(run.get('engine'))} tenant={field(run.get('tenant'))}{att}",
                             "selected" if rid == self.selected else None))
            rows.extend((5, "", None) for _ in range(run_rows - len(self.runs[start:start + run_rows])))
        if rem:
            out = self.outputs.get(self.attached or "")
            rows.append((6, f"-- output run={field(self.attached) if self.attached else 'none'} (untrusted engine "
                            f"text, display only)" + (f" skipped_events={self.skipped_events}"
                                                      if self.skipped_events else "") + " --", None))
            lines = out.tail(rem - 1, max(1, w - 2)) if out else []
            rows.extend((6, "│ " + line, None) for line in lines)
            rows.extend((6, "│", None) for _ in range(rem - 1 - len(lines)))
        rows += [(1, self.error_row(), "error" if self.last_error else None), (4, self.notice, None),
                 (1, self.prompt(), "prompt")]
        while len(rows) > h:  # a terminal shorter than the layout: drop the least needed row
            rows.remove(max(reversed(rows), key=lambda r: r[0]))
        return [(clip(text), style) for _, text, style in rows]

    @staticmethod
    def _squeeze(by_label: dict, heads: dict, listed: dict, budget: int) -> list[Row]:
        """More named subjects than the terminal has rows: every label keeps its head row, and the rows
        left go to UNCERTAIN first (an unconfirmed Stop), then UNRECONCILED, then the rest. A label that
        cannot name all its subjects says on screen how many it left out and where to read them."""

        spare = budget - len(by_label)
        give: dict[str, int] = {}
        for label in sorted(by_label, key=lambda lb: (lb != p.UNCERTAIN, lb != p.UNRECONCILED)):
            give[label] = min(len(listed[label]), spare)
            spare -= give[label]
        out: list[Row] = []
        for label in by_label:
            rows, k = listed[label], give[label]
            out.append(heads[label])
            if k >= len(rows):
                out.extend((text, style) for text, style, _ in rows)
                continue
            shown = rows[:k - 1] if k else []
            out.extend((text, style) for text, style, _ in shown)
            if k:
                missing = len(by_label[label]) - sum(n for _, _, n in shown)
                out.append((f"  … {missing} more not named here (terminal too small; --plain or a larger "
                            f"terminal names every one)", label))
        return out

    def resize(self, width: int, height: int) -> bool:
        """Take the terminal's current size; True if it changed. No floor: the frame fits any size."""

        size = (max(int(width), 1), max(int(height), 1))
        changed = size != (self.width, self.height)
        self.width, self.height = size
        return changed

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
    ``size``, when given, is polled every tick, so a resized terminal gets a frame that fits it.
    """

    def __init__(self, app: TuiApp, api, *, sink: Callable[[list[Row]], None] | None = None,
                 tick_s: float = 1 / 60, drain_budget_s: float = 0.008, event_cap: int = 50_000,
                 render: Callable[[], list[Row]] | None = None,
                 size: Callable[[], tuple[int, int]] | None = None) -> None:
        self.app, self._api, self._sink, self._size = app, api, sink, size
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
        if self._size is not None:  # 4. one repaint, at the terminal's size now
            app.resize(*self._size())
        self.frame = self._render()
        self.frames += 1
        if self._sink is not None:
            self._sink(self.frame)
