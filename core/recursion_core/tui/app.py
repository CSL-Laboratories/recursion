"""The TUI as a control API client (CSL-799, candidate). See README.md.

``TuiApp`` holds view state only: the last answers it was given, which run is selected, a bounded
copy of engine output and one ordered event log (``events.EventLog``). It owns no session, run,
approval or plugin state. Opening reads ``run.list``; attaching reads ``run.status``; closing sends
nothing. Every request goes through the injected ``api.call(method, params)``, the same surface the
CLI uses; ``run.list`` follows ``next_after`` to the last page as the CLI does (``protocol.list_all_runs``).

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
from recursion_core.tui import sessions as s
from recursion_core.tui.events import LOG_CAP, Event, EventLog
from recursion_core.tui.keymap import BY_ID, PLUGIN_PREFIX, Keymap, Pending, Unbound, show_sequence
from recursion_core.tui.keys import Key, Paste
from recursion_core.tui.sanitize import OutputSanitizer, cell_width, field, printable_only, wrap
from recursion_core.tui.view import Row, color_enabled

Dispatch = Callable[[str, dict, Callable[[str, dict, dict], None]], None]
OUTPUT_LINES = 2000  # per run; older engine lines leave the view (the run's own record is elsewhere)
PARTIAL_CAP = 8192  # unfinished line with no newline; older cells are marked, not stored
RUN_ROWS = 5
HELP_ROWS = 3  # entries the open keys show at least; they take rows from the run list for them
CONDITION_ROWS = 4
_TRUNCATED = "⟦truncated⟧"
_CURSOR = "▏"  # where the command line's cursor is, drawn only when it is not at the end


def _fmt(params: Mapping) -> str:
    return " ".join(f"{k}={field(v)}" for k, v in sorted(params.items()))


class _Output:
    def __init__(self, cap: int, run_id: str) -> None:
        self.lines: collections.deque[str] = collections.deque(maxlen=cap)
        self.partial = ""
        self.partial_cut = False
        self.sanitizer = OutputSanitizer()
        self.events = 0
        self.dropped = 0
        self.label = f"output run={field(run_id)}: "
        # The display rows of each retained line at one width, kept beside the lines, so a frame need not wrap
        # the whole history to count it or show its newest rows (review 2 of #404, Astra 3). They are counted
        # again only when the width changes.
        self._width = 0
        self._rows: collections.deque[int] = collections.deque(maxlen=cap)

    def feed(self, text: str | bytes) -> tuple[str, list[str]]:
        """Sanitize one chunk; return it and the lines it finished."""

        self.events += 1
        chunk = self.sanitizer.feed(text)
        # The truncation marker is display text, not part of the engine line, so a later chunk joins
        # the retained tail (bot review 4503920972).
        was_cut = self.partial_cut
        body = self.partial[len(_TRUNCATED):] if was_cut else self.partial
        *done, partial = (body + chunk).split("\n")
        if was_cut and done:
            done[0] = _TRUNCATED + done[0]
        keep = PARTIAL_CAP - len(_TRUNCATED)
        if len(partial) > keep:
            self.dropped += len(partial) - keep
            partial = _TRUNCATED + partial[-keep:]
            self.partial_cut = True
        else:
            self.partial_cut = False
        self.partial = partial
        self.lines.extend(done)
        if self._width:  # the counts stay beside the lines: the same maxlen drops the same oldest ones
            self._rows.extend(len(wrap(line, self._width)) for line in done)
        return chunk, done

    def extend(self, rows: list[str]) -> None:
        """Rows that are display text already (a transcript's), kept as finished lines, counted as they are."""

        self.lines.extend(rows)
        if self._width:
            self._rows.extend(len(wrap(row, self._width)) for row in rows)

    def source(self) -> list[str]:
        """Every line kept, the unfinished one last."""

        return [*self.lines, self.partial] if self.partial else list(self.lines)

    def tail(self, rows: int, width: int, offset: int = 0) -> list[str]:
        """``rows`` display rows ending ``offset`` display rows above the newest (the pager's view). The pager
        counts wrapped rows, not lines, so every row of a long line can be reached (review 1 of #404, R2).
        Only the lines that hold those rows are wrapped; the others are counted from ``counts`` (review 2 of
        #404, Astra 3)."""

        if not rows:
            return []
        src, counts = self.source(), self.counts(width)
        top = sum(counts)  # the display rows kept; it walks down to each line's first row
        end = max(0, top - offset)
        start = max(0, end - rows)
        out: list[str] = []
        for line, n in zip(reversed(src), reversed(counts), strict=True):
            first = top - n
            if first < end:  # this line holds some of the rows shown
                out[:0] = wrap(line, width)[max(start, first) - first:end - first]
            if first <= start:
                break
            top = first
        return out

    def counts(self, width: int) -> list[int]:
        """The display rows of each line ``source()`` gives, at ``width``."""

        if width != self._width:
            self._width = width
            self._rows = collections.deque((len(wrap(line, width)) for line in self.lines), maxlen=self.lines.maxlen)
        out = list(self._rows)
        if self.partial:  # at most PARTIAL_CAP characters, so it is counted each time
            out.append(len(wrap(self.partial, width)))
        return out

    def total_rows(self, width: int) -> int:
        return sum(self.counts(width))




class TuiApp:
    def __init__(self, api, *, clock: Callable[[], float], environ: Mapping[str, str] | None = None,
                 width: int = 100, height: int = 30, stale_after_s: float = 10.0,
                 dispatch: Dispatch | None = None, output_lines: int = OUTPUT_LINES,
                 log_cap: int = LOG_CAP, keymap: Keymap | None = None) -> None:
        self._api, self._clock = api, clock
        self.keymap = (keymap or Keymap.default()).for_client()  # its own pending sequence (review 1 of #404)
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
        self.refresh_failed: str | None = None  # last run.list read failed
        self.status_refresh_failed: str | None = None  # last run.status read failed
        self.list_stale = False  # last successful run.list was marked stale; only a fresh list clears it
        self.last: dict | None = None
        self.last_method: str | None = None
        self._answer_keyed: list[tuple[str, str, str]] = []
        self.answer_conditions: list[tuple[str, str]] = []
        self.shown: dict[tuple[str, str], tuple[str, str]] = {}  # conditions as last logged
        self._uncertain: dict[str, str] = {}  # per run, until the API lists that run with no unconfirmed Stop
        self.errors: list[str] = []
        self.last_error: Event | None = None  # kept here too, so a bounded log cannot drop it
        self.mode, self.line, self.stop_target = "keys", "", None
        self.cursor = 0  # the command line's insertion point, 0..len(line)
        self.line_kind = ":"  # what opened the line: ":" or "/" (a command) or "search" (the pager's search)
        self.focus = "runs"  # "runs" (the run list) or "output" (the output pager)
        self.scroll = 0  # the pager's view, in display rows above the newest; 0 follows new output
        self.search = ""  # the pager's last search
        self.help_lines: list[str] = []
        self.help_scroll = 0  # the first help row shown in full screen
        self._out_rows = 10  # rows the output pane had in the last frame (a half page is half of it)
        # Burst input seen since the line was opened, or on any key of the sequence that armed a Stop
        # (review of #372, R1): the next Enter or y does not act on it, however it arrives itself.
        self._line_burst = self._stop_burst = False
        self._seq_burst = False  # a key of the keymap's pending sequence came with burst input
        self._stop_keys: tuple[str, ...] = ("s",)  # the sequence that armed the Stop, for its notice
        self.notice = ""
        self.closed = False
        # Held while the app's state changes, and by close(); never while reading a caller's answer or
        # logging engine output, so close() cannot wait on a stalled thread (reviews of a1f16a20, C4).
        self._lock = threading.RLock()
        self._state = "live"
        self.skipped_events = 0  # engine output events a Loop dropped from its presentation queue
        self.outputs: dict[str, _Output] = {}
        self.in_flight: dict[str, int] = collections.defaultdict(int)
        # Sessions (slice 2, part 2): the last session.list, and each opened transcript with its rows. The
        # output pane shows a transcript while ``pane`` names its session, else the attached run's output.
        self.sessions: list[dict] = []
        self.transcripts: dict[str, s.Transcript] = {}
        self.transcript_rows: dict[str, _Output] = {}
        self.pane: str | None = None

    # -- the event log --------------------------------------------------------------------------
    def _emit(self, event_type: str, source: str, text: str, speech: str | None = None,
              state: str | None = None) -> Event | None:
        ms = int(round((self._clock() - self._t0) * 1000))
        # The final event seals the log in the same step: any later append, even one a busy thread had
        # already begun, is refused by the log itself, so the final event stays last.
        append = self.log.append_last if event_type == "close" else self.log.append
        return append(ms, event_type, source, state or self._view_state(), text,
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
            return "search_line" if self.line_kind == "search" else "command_line"
        if self.mode == "help":
            return "help"
        if self.focus == "output":
            return f"output run={field(self.attached)}"
        return f"runs run={field(self.selected)}" if self.selected else "runs"

    # -- lifecycle: opening and attaching are reads; closing sends nothing ----------------------
    def open(self) -> None:
        self._emit("open", "operator", f"open surface={p.SURFACE}: reads run.list; starts nothing")
        for note in self.keymap.notes:  # a binding file that was not used, or a binding it skipped
            self._error(f"key bindings: {field(note)}")
        self.request("run.list", {})

    def attach(self, run_id: str) -> None:
        self._attach_view(run_id)
        self._emit("attach", "operator", f"attach run={field(run_id)}: reads run.status; starts nothing")
        self.request("run.status", {"run_id": run_id})

    def _attach_view(self, run_id: str) -> None:
        """The run the output pane shows. Another run starts at its newest rows with no search: an offset or
        a search kept from the last run would show rows that are not there (reviews 1 of #404, M2 and R4)."""

        if run_id != self.attached or self.pane is not None:  # a transcript's view is not the run's
            self.scroll, self.search = 0, ""
        self.attached = self.selected = run_id
        self.pane = None  # the output pane goes back to the run's output

    def refresh(self) -> None:
        self.request("run.list", {})
        if self.attached:
            self.request("run.status", {"run_id": self.attached})

    def close(self) -> None:
        """Closing sends nothing. After it the app takes no key, answer or output: nothing is logged,
        sent or changed, so a thread that is still busy cannot add to the log or send a request."""

        with self._lock:  # a state change already under way finishes first; none starts after this
            self.closed = True  # a second call changes nothing: the sealed log refuses its event
            self._emit("close", "operator", "closed: no request sent; runs keep going and can be reattached")

    # -- requests -------------------------------------------------------------------------------
    def request(self, method: str, params: dict) -> None:
        # Defensive: nothing outside the CLI's method set, and the session reads, leaves the TUI.
        if method not in p.ALLOWED_METHODS and method not in s.SESSION_METHODS:
            raise ValueError(method)
        with self._lock:
            if self.closed:
                return
            if method == "run.start" and self.watch_only:
                self._error(f"refused locally: start while watch_only ({self._stale_text()}); refresh first")
                return
            self.in_flight[method] += 1
            self._emit("request", "operator", f"request {method} {_fmt(params)}".rstrip())
            self.dispatch(method, params, self.on_answer)

    def _inline(self, method: str, params: dict, done: Callable[[str, dict, dict], None]) -> None:
        done(method, params, call_api(self._api, method, params))

    def on_answer(self, method: str, params: dict, answer: dict) -> None:
        code = answer.get("code")  # read before taking the lock: the answer is the caller's object
        with self._lock:
            if self.closed:  # the TUI ended while the answer was read: it changes nothing
                return
            self._apply_answer(method, params, answer, code)

    def _apply_answer(self, method: str, params: dict, answer: dict, code) -> None:
        self.in_flight[method] = max(0, self.in_flight[method] - 1)
        self.last, self.last_method = answer, method
        # A Stop answer confirms only the run it was sent for (ADV2-11): one that names another run,
        # or none, changes no run and is shown as a mismatch, never as this run's Stop.
        mismatch = (method == "run.stop" and code in ("stopped", "pending")
                    and answer.get("run_id") != params.get("run_id"))
        if mismatch:
            pass
        elif method == "run.list":
            if code == "ok" and isinstance(answer.get("runs"), list):
                self.runs = [r for r in answer["runs"] if isinstance(r, dict)]
                self.snapshot_at, self.refresh_failed = self._clock(), None
                # A stale list stays stale until a later list is not. A status answer must not clear it
                # (bot review 4503920848).
                self.list_stale = answer.get("stale") is True
                ids = [str(r.get("run_id")) for r in self.runs]
                if self.selected not in ids:
                    self.selected = ids[0] if ids else None
            else:
                self.refresh_failed = f"run.list answered {field(code)}"
        elif method == "run.status":
            # An unavailable status stays failed until a later status succeeds. A refusal is an answer,
            # not a failed read, and a successful list must not clear it (bot review 4503920903).
            if code == "ok":
                self.status_refresh_failed = None
                if isinstance(answer.get("run"), dict):
                    self._merge(answer["run"])
            elif code == p.UNAVAILABLE:
                self.status_refresh_failed = f"run.status answered {field(code)}"
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
        # Only an attested stop clears it: the API lists the run as `stopped`, which the registry stores
        # only with an attestation. A listed run with no stop record is not proof: a pending Stop whose
        # write to the registry failed (recorded=false) leaves the run `running` with no stop (R2 finding
        # 6). A later answer about another run, or a run absent from the list, does not clear it either.
        attested = {f"run={r.get('run_id')}" for r in self.runs if r.get("state") == "stopped"}
        for subject in list(self._uncertain):
            if subject in attested:
                del self._uncertain[subject]
        # A stopped answer for a run that is not in this snapshot still confirms that Stop.
        # The cleanup above keeps it when the run is absent (bot review 4504133355).
        if method == "run.stop" and code == "stopped" and not mismatch and isinstance(answer.get("run_id"), str):
            self._uncertain.pop(f"run={answer['run_id']}", None)
        labels = ",".join(sorted({label for label, _ in self.answer_conditions})) or "none"
        said = " ".join(x for x in (f"answer {method} status={field(code)}", self._answer_fields(answer)) if x)
        # The full line keeps the view state for the transcript; speech leaves it to the condition edges.
        self._emit("answer", "recursion", f"{said} surface={p.SURFACE} watch_only={str(self.watch_only).lower()} "
                                         f"conditions={labels}", speech=said)
        for label, text in self.answer_conditions:
            if label in (p.REFUSED, p.UNAVAILABLE_LABEL):
                self._error(f"{label}: {method} status={field(code)} {field(text)}")
        if mismatch:
            self._error(f"stop run={field(params.get('run_id'))}: stop_answer_run_mismatch: the control API "
                        f"answered {field(code)} for run={field(answer.get('run_id'))}; this Stop is not confirmed")
        elif method == "run.stop":  # the answer line above already said it
            self._notice(f"stop run={field(params.get('run_id'))}: control API answered {field(code)}", speech="")
        self.sync_conditions()
        if code == "ok" and method == s.LIST and isinstance(answer.get("sessions"), list):
            self.sessions = [x for x in answer["sessions"] if isinstance(x, dict)]
            rows = [s.session_row(x) for x in self.sessions] or ["no sessions listed"]
            self._emit("sessions", "recursion", "\n".join([f"sessions: {len(self.sessions)}", *rows]))
        elif code == "ok" and method == s.READ:
            self._transcript_page(params, answer)

    def _transcript_page(self, params: dict, answer: dict) -> None:
        """One page of a transcript: each new line logged once (so every renderer prints the same rows), its
        rows kept for the output pane, and the next page asked for while the answer says there is more."""

        sid = (params.get("cursor") or {}).get("session_id")
        transcript = self.transcripts.get(sid)
        if transcript is None:  # not a transcript this TUI opened
            return
        out = self.transcript_rows.setdefault(sid, _Output(self._output_lines, sid))
        added = 0
        for line in transcript.apply(answer):
            rows = s.rows(line)
            out.extend(rows)
            added += sum(len(wrap(row, self._pane_width())) for row in rows)
            source = "engine" if line.get("trust") == "untrusted" else "recursion"
            self._emit("transcript", source, "\n".join(rows),
                       speech="\n".join(f"transcript session={field(sid)}: {row}" for row in rows))
        if self.scroll and self.pane == sid:  # a scrolled pager stays on the rows it shows; the frame clamps it
            self.scroll += added
        if answer.get("epoch_advanced") is True:
            self._notice(f"transcript session={field(sid)}: the daemon restarted; read on from seq "
                         f"{transcript.cursor['seq']} (epoch {transcript.cursor['epoch']})")
        if answer.get("more") is True and transcript.pages < s.MAX_PAGES:
            transcript.pages += 1
            self.request(s.READ, s.read_params(transcript.cursor))
        elif answer.get("more") is True:
            self._notice(f"transcript session={field(sid)}: more lines after seq {transcript.cursor['seq']}; "
                         f"the command transcript {field(sid)} reads on")

    def open_transcript(self, session_id: str) -> None:
        """Show a session's transcript in the output pane, reading it from the cursor (from the start the
        first time; on again from where the last read ended)."""

        transcript = self.transcripts.setdefault(session_id, s.Transcript(session_id))
        transcript.pages = 0
        if self.pane != session_id:  # another pane: start at its newest rows; reading on keeps the view
            self.pane, self.scroll = session_id, 0
        self.transcript_rows.setdefault(session_id, _Output(self._output_lines, session_id))
        self.request(s.READ, s.read_params(transcript.cursor))

    def _pane_output(self) -> _Output | None:
        """What the output pane and its pager show: the open transcript, else the attached run's output."""

        if self.pane is not None:
            return self.transcript_rows.get(self.pane)
        return self.outputs.get(self.attached or "")

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
        run_id, text = str(event.get("run_id")), event.get("text", "")  # the caller's object, read unlocked
        with self._lock:
            if self.closed:
                return
            if not isinstance(text, (str, bytes, bytearray)):  # said, not raised (review 2 of #404, DeepSeek L6)
                self._error(f"output for run={field(run_id)} skipped: its text is a {type(text).__name__}, not text")
                return
            out = self.outputs.get(run_id)
            if out is None:
                out = self.outputs[run_id] = _Output(self._output_lines, run_id)
            before = out.partial
            chunk, done = out.feed(text)
            if self.scroll and self.pane is None and run_id == self.attached:
                # A scrolled pager stays on the rows it shows: it moves up by the display rows this chunk
                # added at the end, a partial line begun, grown or finished included (review 1 of #404, R3).
                # The frame and the pager keep it within the rows retained.
                w = self._pane_width()
                rows = lambda text: len(wrap(text, w)) if text else 0  # noqa: E731
                added = sum(len(wrap(line, w)) for line in done) + rows(out.partial) - rows(before)
                self.scroll = max(0, self.scroll + added)
            speech, state = "\n".join(out.label + line for line in done), self._state
        # Logged already sanitized, outside the lock; the linear renderer says each finished line once,
        # marked as output. If the TUI ended meanwhile, the sealed log refuses it.
        self._emit("output", "engine", chunk, speech=speech, state=state)

    # -- keys -----------------------------------------------------------------------------------
    def press(self, key: Key, *, burst: bool = False) -> None:
        """One key (a name from ``keys.KeyDecoder``, or a ``Paste``). ``burst`` means it came in one
        read with other input: an unbracketed paste, or typing faster than the client reads. Such a
        key cannot confirm a Stop or send the command line, because pasted text would otherwise act; nor
        can the next key, when the Stop was armed or the line typed with burst input. This is a heuristic
        for an unbracketed paste: input that arrives one key per read cannot be told from typing. Anything
        that is not a key name or a ``Paste`` is said as an error and does nothing (review 2 of #404, DeepSeek
        L3)."""

        with self._lock:
            if not isinstance(key, (str, Paste)):
                if not self.closed:
                    self._error(f"key ignored: a {type(key).__name__} is not a key name or a paste; nothing done")
                return
            self._press(key, burst)

    def _press(self, key: Key, burst: bool) -> None:
        if self.closed:
            return
        if key == "ctrl+c":  # the terminal sends Ctrl-C as a key (raw mode); it closes, from any mode
            self.close()
            return
        if self.mode == "confirm_stop":
            target, self.mode, self.stop_target = self.stop_target, "keys", None
            armed_by_burst, self._stop_burst = self._stop_burst, False
            if key in ("y", "Y") and not (burst or armed_by_burst):
                self.stop(target)
            elif key in ("y", "Y"):
                armed = show_sequence(self._stop_keys, self.keymap.leader)
                self._notice(f"stop of {field(target)} cancelled: the {armed} or the y came in one read with other "
                             f"input (a paste?); nothing sent; press {armed} and then y again")
            else:
                self._notice(f"stop of {field(target)} cancelled; nothing sent")
            return
        if self.mode == "command":
            self._edit(key, burst)
            return
        if isinstance(key, Paste):
            self.keymap.pending = ()
            if self.mode == "help":
                self._notice("paste ignored: the keys view takes keys, not text; Esc leaves it; nothing sent")
            else:
                place = "the output pager" if self.focus == "output" else "the run list"
                self._notice(f"paste ignored: {place} takes keys, not text; press : to paste a command; nothing sent")
            return
        if self.mode == "help":
            result = self.keymap.feed(key, "help")
            if isinstance(result, Unbound) and result.keys[-1] == "esc":
                result = self._cancelled(result.keys, "help")
            page = max(1, self._out_rows)
            step = {"help.down": 1, "help.up": -1, "help.page_down": page, "help.page_up": -page}.get(result)
            if result == "help.close":
                self.mode = "keys"
            elif step is not None:  # the frame keeps it within the rows (review 1 of #404, R5)
                self.help_scroll = max(0, self.help_scroll + step)
            elif result == "help.top":
                self.help_scroll = 0
            elif result == "help.bottom":
                self.help_scroll = max(0, len(self._help_rows()) - self._out_rows)
            return
        # A sequence is burst when any of its keys was: Ctrl+X read with other input arms a Stop that the
        # next y does not confirm, however the k arrives. A key read again on its own, after it broke a
        # prefix that names nothing (Ctrl+D, then s), is not part of that prefix and keeps its own mark.
        before = self.keymap.pending
        result = self.keymap.feed(key, self.focus)
        if isinstance(result, (Pending, Unbound)):
            seq = result.keys
        else:
            seq = self.keymap.last if result is not None else (key,)
        if before and seq[:-1] == before:  # this key went on with the pending sequence
            burst = burst or self._seq_burst
        self._seq_burst = burst and isinstance(result, Pending)
        if isinstance(result, Pending):
            return  # the prompt row shows what can follow
        if isinstance(result, Unbound) and result.keys[-1] == "esc":
            then = self._cancelled(result.keys, self.focus)
            if then is not None:
                self._act(then, "esc", False)
            return
        if isinstance(result, Unbound):
            self._notice(f"{show_sequence(result.keys, self.keymap.leader)} is not bound; nothing done")
            return
        if result is not None:
            self._act(result, key, burst)

    def _cancelled(self, keys: tuple[str, ...], context: str) -> str | None:
        """Esc ended a pending sequence: say so, and give back what Esc alone does in ``context``, so Esc
        still leaves the line, the help or the pager (review 1 of #404, M4). Esc is bound to nothing that
        sends a request (the keymap refuses it)."""

        then = self.keymap.view(context).get(("esc",))
        self._notice(f"{show_sequence(keys[:-1], self.keymap.leader)} cancelled; "
                     f"{'nothing done' if then is None else 'nothing sent'}")
        return then

    def _act(self, action: str, key: str, burst: bool) -> None:
        """One action from the keymap. Every action that sends goes through the same path as the CLI's
        command; the Stop action opens the confirmation and never sends by itself. ``burst`` is the mark
        of the keys that chose the action: a line or a Stop they open keeps it."""

        if action == "app.quit":
            self.close()
        elif action == "help.show":
            self.show_help()
        elif action == "line.command":
            self.mode, self.line, self.cursor, self._line_burst = "command", "", 0, burst
            self.line_kind = "/" if key == "/" else ":"
        elif action == "line.start":
            self.mode, self.line, self.line_kind, self._line_burst = "command", "start ", ":", burst
            self.cursor = len(self.line)
        elif action in ("runs.next", "runs.prev"):
            self._move(1 if action == "runs.next" else -1)
        elif action == "run.attach":
            if self.selected:
                self.attach(self.selected)
        elif action == "runs.refresh":
            self.refresh()
        elif action == "run.stop":
            if self.selected:  # the target is fixed now; a later refresh or output cannot change it
                self.mode, self.stop_target, self._stop_burst = "confirm_stop", self.selected, burst
                self._stop_keys = self.keymap.last
            else:
                self._notice("no run selected; nothing to stop")
        elif action == "conditions.show":
            self.show_conditions()
        elif action == "sessions.show":
            self.request(s.LIST, dict(s.LIST_PARAMS))
        elif action == "focus.output":
            self.focus = "output"
        elif action == "focus.runs":
            self.focus = "runs"
        elif action.startswith("pager."):
            self._page(action, burst)
        elif action.startswith(PLUGIN_PREFIX):
            self._notice(f"{field(action)}: no plugin host in this build; nothing sent")

    def _edit(self, key: Key, burst: bool) -> None:
        """The command line (":" or "/", or the pager's search): left, right, Home and End move the cursor;
        Backspace deletes before it and Delete at it; typed and pasted text goes in at it; Esc leaves the
        line; Enter sends it. The keys come from the keymap's ``command`` context. A line opened, typed or
        pasted with burst input is not sent by the next Enter, nor is one whose Enter is burst."""

        line, at = self.line, self.cursor
        if isinstance(key, Paste):
            self.keymap.pending = ()  # a paste breaks a pending sequence, as it does in the run list
            self._line_burst = self._line_burst or burst
            text = printable_only(key.text)  # a pasted line break is a space: a paste never sends the line
            self.line, self.cursor = line[:at] + text + line[at:], at + len(text)
            if key.dropped:
                self._notice(f"paste cut: {key.dropped} characters past the first {len(key.text)} were not kept")
            return
        action = self.keymap.feed(key, "command")
        if burst and action != "line.submit":
            self._line_burst = True
        if isinstance(action, Unbound) and action.keys[-1] == "esc":
            action = self._cancelled(action.keys, "command")  # and then Esc leaves the line
        if isinstance(action, (Pending, Unbound)):
            return
        if action == "line.submit" and (burst or self._line_burst):
            self._line_burst = False  # said once; the next Enter, on its own, sends the line
            send = self._first_key("line.submit", "command")  # the keys that just sent it are bound to it
            self._notice(f"the command line was not sent: it or its {send} came in one read with other input "
                         f"(a paste?); press {send} to send it")
        elif action == "line.submit":
            kind, self.mode, self.line, self.cursor = self.line_kind, "keys", "", 0
            if kind == "search":
                self._search(line, step=0)
            else:
                self.command(line)
        elif action == "line.cancel":
            self.mode, self.line, self.cursor = "keys", "", 0  # the next line starts with a fresh mark
        elif action == "line.backspace":
            if at:
                self.line, self.cursor = line[:at - 1] + line[at:], at - 1
        elif action == "line.delete":
            self.line = line[:at] + line[at + 1:]
        elif action == "line.left":
            self.cursor = max(0, at - 1)
        elif action == "line.right":
            self.cursor = min(len(line), at + 1)
        elif action == "line.home":
            self.cursor = 0
        elif action == "line.end":
            self.cursor = len(line)
        elif action is None and len(key) == 1 and key.isprintable():
            self.line, self.cursor = line[:at] + key + line[at:], at + 1

    # -- the output pager -------------------------------------------------------------------------
    def _pane_width(self) -> int:
        return max(1, self.width - 2)  # the output pane's rows start with "│ "

    def _help_rows(self) -> list[str]:
        return [row for text in self.help_lines for row in wrap(text, self._pane_width())]

    def _top_scroll(self) -> int:
        """The pager's largest offset: the oldest retained display row is the pane's newest (as before, in
        rows instead of lines, so a long line's first row is reachable too)."""

        out = self._pane_output()
        return max(0, out.total_rows(self._pane_width()) - 1) if out else 0

    def _page(self, action: str, burst: bool = False) -> None:
        """Pager steps in display rows (a long line's rows are each reachable; review 1 of #404, R2)."""

        half = max(1, self._out_rows // 2)
        step = {"pager.down": -1, "pager.up": 1, "pager.half_down": -half, "pager.half_up": half}.get(action)
        if step is not None:
            self.scroll = min(max(0, self.scroll + step), self._top_scroll())
        elif action == "pager.top":
            self.scroll = self._top_scroll()
        elif action == "pager.bottom":
            self.scroll = 0
        elif action == "pager.search":
            self.mode, self.line, self.cursor, self.line_kind, self._line_burst = "command", "", 0, "search", burst
        elif action in ("pager.next", "pager.prev"):
            if self.search:
                self._search(self.search, step=1 if action == "pager.next" else -1)
            else:
                self._notice("no search yet: press / in the output pager")

    def _search(self, pattern: str, step: int) -> None:
        """Find ``pattern`` (case-insensitive when it has no capital letter) in the attached run's output.
        A new search starts at the newest line shown and goes to older lines; ``n`` goes on to older ones,
        ``N`` back to newer ones, both wrapping around. The match's last display row becomes the pager's
        newest row."""

        self.search = pattern
        out = self._pane_output()
        src = out.source() if out else []
        if not pattern or not src:
            self._notice(f"search {field(pattern)}: no output to search")
            return
        fold = pattern == pattern.lower()
        needle = pattern.lower() if fold else pattern
        n, w = len(src), self._pane_width()
        counts = [len(wrap(line, w)) for line in src]
        below = 0  # display rows under the pane's newest row
        shown = n - 1
        while shown > 0 and below + counts[shown] <= self.scroll:  # the line holding the pane's newest row
            below += counts[shown]
            shown -= 1
        if step == 0:  # a new search: the newest line shown first, then older ones
            visit = [(shown - i) % n for i in range(n)]
        elif step == 1:  # n: on to older lines
            visit = [(shown - i) % n for i in range(1, n + 1)]
        else:  # N: back to newer lines
            visit = [(shown + i) % n for i in range(1, n + 1)]
        for idx in visit:
            if needle in (src[idx].lower() if fold else src[idx]):
                self.scroll = sum(counts[idx + 1:])  # the match's last row is the pane's newest
                self._notice(f"search {field(pattern)}: line {idx + 1} of {n}")
                return
        self._notice(f"search {field(pattern)}: not found in the {n} lines kept")

    # -- help -------------------------------------------------------------------------------------
    def show_help(self) -> None:
        """The keys in force, from the keymap, and the commands; logged, so every renderer says them."""

        km, lines = self.keymap, []
        for focus, title in (("runs", "run list"), ("output", "output pager (Tab from the run list)"),
                             ("command", "command line (: or /)")):
            lines.append(f"keys, {title}:")
            seen = set()
            for seq, action in km.view(focus).items():
                if action in seen:
                    continue
                seen.add(action)
                keys = ", ".join(km.keys_for(action, focus))
                lines.append(f"  {keys}: {BY_ID[action].help if action in BY_ID else action}")
        lines += ["stop always asks: y sends run.stop, any other key cancels",
                  "commands (: or /, and --plain): list (or runs), status RUN, stop RUN, "
                  "start --tenant T --engine E --prompt P [--client-key K], conditions, sessions, "
                  "transcript SESSION, help (or keys), quit",
                  "Ctrl+C closes the TUI from any mode; closing the TUI never stops a run; Esc never sends anything"]
        self.help_lines, self.help_scroll = lines, 0
        self.mode = "help"
        self._emit("help", "recursion", "\n".join(lines))

    def command(self, line: str) -> None:
        """A command line in the CLI's grammar: ``list``, ``status RUN``, ``stop RUN``, ``start ...``,
        ``conditions``, and the TUI's own ``help`` (``keys``) and ``quit``, which send nothing. A leading
        ``/`` is accepted (slash commands), and ``runs`` is another name for ``list``. ``sessions`` reads
        ``session.list`` and ``transcript SESSION`` reads that session's transcript (``events.read``)."""

        line = line.strip()
        if line.startswith("/"):
            line = line[1:]
        word, _, rest = line.partition(" ")
        if word in ("help", "keys") and not rest.strip():
            self.show_help()
            return
        if word in ("quit", "q") and not rest.strip():
            self.close()
            return
        if word == "sessions":
            if rest.strip():
                self._error("usage error: sessions takes no arguments; nothing sent")
            else:
                self.request(s.LIST, dict(s.LIST_PARAMS))
            return
        if word == "transcript":
            if len(rest.split()) != 1:
                self._error("usage error: transcript SESSION; nothing sent")
            else:
                self.open_transcript(rest.strip())
            return
        if word == "runs":
            line = "list" + (" " + rest if rest else "")
        try:
            method, params = p.parse_command(line)
        except p.CommandError as exc:
            self._error(f"usage error: {field(exc)}; nothing sent")
            return
        if method == p.CONDITIONS:
            self.show_conditions()
            return
        if method == "run.status":
            self._attach_view(params["run_id"])
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
        if (self.refresh_failed or self.status_refresh_failed or self.list_stale
                or any(label == p.STALE for label, _ in self.answer_conditions)):
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
        if self.status_refresh_failed:
            reasons.append(f"refresh failed: {self.status_refresh_failed}")
        if self.list_stale:
            reasons.append("run.list answer was stale")
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

        with self._lock:
            if not self.closed:
                self._sync_conditions()

    def _sync_conditions(self) -> None:
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
        """One full-screen frame (``_frame``), made under the app's lock: engine output another thread hands
        in meanwhile waits for it, so the lines it reads cannot change under it (review 2 of #404, DeepSeek
        L5). It only reads and wraps; it calls nothing outside the app."""

        with self._lock:
            return self._frame()

    def _frame(self) -> list[Row]:
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
                # break only at a space after a word, never inside leading indentation (a name row's "  "
                # would otherwise become a blank row that spends the condition budget; bot review 4511719145)
                cuts = [i for i, ch in enumerate(chunk) if ch == " " and chunk[:i].strip()]
                if len(chunk) < len(rest) and cuts and (out or chunk.startswith(" ") or " " in chunk[:cuts[-1]]):
                    # (the first row never breaks right after its "LABEL:", so the label keeps its text)
                    chunk = chunk[:cuts[-1] + 1]
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
            cond = self._squeeze(by_label, heads, listed, budget, w)
        if cond is None:
            cond = summary if keyed else [("conditions: none", None)]
        rem = max(0, h - fixed - len(cond))
        pad = min(max(0, CONDITION_ROWS - len(cond)), max(0, rem - (RUN_ROWS + 3)))  # keep the layout steady
        rem -= pad
        run_rows = min(RUN_ROWS, rem - 1) if rem >= 2 else 0
        if self.mode == "help":  # the open keys take rows from the run list until HELP_ROWS of them show
            want = HELP_ROWS + 1  # and their header row (review 2 of #404, Astra 2)
            while run_rows and rem - run_rows - 1 < want:
                run_rows -= 1
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
        if rem and self.mode == "help":  # the keys, in the output pane's place, until Esc, q or ?
            self._out_rows = rem - 1
            every = self._help_rows()
            self.help_scroll = min(self.help_scroll, max(0, len(every) - (rem - 1)))  # every row can be reached
            lines = every[self.help_scroll:self.help_scroll + rem - 1]
            more = (f"; rows {self.help_scroll + 1}-{self.help_scroll + len(lines)} of {len(every)}, j/k and "
                    f"Page Up/Down scroll" if len(every) > rem - 1 else "")
            if not lines:  # no row for an entry: name no range (review 2 of #404, Astra 2)
                more = "; no room to show them here"
            rows.append((6, f"-- keys (Esc, q or ? closes{more}; the transcript has them too) --", None))
            rows.extend((6, "│ " + line, None) for line in lines)
            rows.extend((6, "│", None) for _ in range(rem - 1 - len(lines)))
        elif rem:
            self._out_rows = rem - 1
            out = self._pane_output()
            self.scroll = min(self.scroll, self._top_scroll())  # retention may have dropped the rows it was on
            pager = ""
            if self.focus == "output" or self.scroll:
                pager = (f" [pager: {self.scroll} rows up, G follows]" if self.scroll else " [pager: following]")
            if self.pane is not None:
                head = (f"-- transcript session={field(self.pane)} (the daemon's lines; engine text untrusted, "
                        f"display only)")
            else:
                head = (f"-- output run={field(self.attached) if self.attached else 'none'} (untrusted engine "
                        f"text, display only)" + (f" skipped_events={self.skipped_events}"
                                                  if self.skipped_events else ""))
            rows.append((6, head + pager + " --", None))
            lines = out.tail(rem - 1, max(1, w - 2), self.scroll) if out else []
            rows.extend((6, "│ " + line, None) for line in lines)
            rows.extend((6, "│", None) for _ in range(rem - 1 - len(lines)))
        # An empty "errors: none" row gives way before the prompt, and on a tie the prompt is kept, so a Stop
        # confirmation or a typed command stays visible on a two-row terminal (bot review 4511540462).
        # A real error outranks the condition rows: at two rows the frame keeps `error N:` and the prompt,
        # and the condition text stays in the log (bot review 4517702948).
        rows += [(-1 if self.last_error else 2, self.error_row(), "error" if self.last_error else None),
                 (4, self.notice, None), (-2 if self.last_error else 1, self.prompt(), "prompt")]
        while len(rows) > h:  # a terminal shorter than the layout: drop the least needed row
            rows.remove(max(reversed(rows), key=lambda r: (r[0], r[2] != "prompt")))
        return [(clip(text), style) for _, text, style in rows]

    @staticmethod
    def _squeeze(by_label: dict, heads: dict, listed: dict, budget: int, width: int) -> list[Row]:
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
            if rows and k == 0:  # no row left under it: the head itself names what fits (bot 4511719327, 4517703119)
                out.append((TuiApp._ids_on_one_row(label, by_label[label], width), label))
                continue
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

    @staticmethod
    def _ids_on_one_row(label: str, found: list[tuple[str, str]], width: int) -> str:
        """One head row and no name rows under it. Whole run ids come first; the omitted count is how
        many of those ids do not fit on the row (bot review 4517703119). A clipped id is not counted
        as named. The full condition text stays in the event log."""

        names = [field(subject[4:] if subject.startswith("run=") else subject) for subject, _ in found]
        n = len(names)
        line = f"{label}: {n} not named here"
        for count in range(1, n + 1):
            omitted = n - count
            body = ", ".join(names[:count])
            candidate = f"{label}: {body}" if omitted == 0 else f"{label}: {body} ({omitted} more not named here)"
            if sum(cell_width(c) for c in candidate) > width:
                break
            line = candidate
        return line

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
            # strictly fewer cells than the terminal, even at widths 1 and 2 (bot review 4511540618)
            if w <= 0:
                return ""
            parts = wrap(text, w) if text else [""]
            if len(parts) == 1:
                return parts[0]
            return (wrap(text, w - 1)[0] if w > 1 else "") + "…"

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
        km = self.keymap
        if km.pending:  # a sequence waits for its next key: say what can follow
            focus = "command" if self.mode == "command" else "help" if self.mode == "help" else self.focus
            nexts = "  ".join(f"{show_sequence((k,), km.leader)} {BY_ID[a].label if a in BY_ID else a}"
                              for k, a in km.continuations(focus))
            return f"recursion: {show_sequence(km.pending, km.leader)} ... {nexts}  Esc cancels"
        if self.mode == "command":
            at = self.cursor
            shown = self.line if at >= len(self.line) else self.line[:at] + _CURSOR + self.line[at:]
            if self.line_kind == "search":
                return f"search older lines: {shown}"
            return f"recursion: {'/' if self.line_kind == '/' else ''}{shown}"
        if self.mode == "help":
            return "recursion: keys shown; j/k and Page Up/Down scroll; Esc, q or ? closes"
        if self.focus == "output":
            return self._hints("output pager", (("pager.down", "pager.up", "line"), ("pager.half_down", "pager.half_up",
                               "half page"), ("pager.top", "pager.bottom", "top/newest"), ("pager.search", None, "search"),
                               ("pager.next", "pager.prev", "next/previous"), ("focus.runs", None, "run list")))
        return self._hints("", (("runs.next", "runs.prev", "select"), ("run.attach", None, "attach"),
                                ("runs.refresh", None, "refresh"), ("run.stop", None, "stop"),
                                ("line.command", None, "command"), ("help.show", None, "keys"),
                                ("app.quit", None, "close (runs keep going)")))

    def _hints(self, title: str, parts: tuple) -> str:
        """The prompt row's key hints, from the keymap in force: the shortest key of each action."""

        focus = self.focus
        out = []
        for first, second, label in parts:
            keys = [self._first_key(a, focus) for a in (first, second) if a]
            if all(keys):
                out.append(f"{'/'.join(keys)} {label}")
        return "recursion: " + "  ".join(([title] if title else []) + out)

    def _first_key(self, action: str, focus: str) -> str | None:
        seqs = [s for s, a in self.keymap.view(focus).items() if a == action]
        if not seqs:
            return None
        return show_sequence(min(seqs, key=lambda s: (len(s), len(show_sequence(s, self.keymap.leader)))),
                             self.keymap.leader)

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
        # run.list follows next_after to the last page, as the CLI's list does (ADV1-5).
        answer = p.list_all_runs(api.call, params) if method == "run.list" else api.call(method, dict(params))
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
        self._paint_lock = threading.Lock()  # a paint and shutdown() never overlap; none starts after it
        self.join_timeout_s = 5.0
        self._lanes = {"stop": queue.SimpleQueue(), "other": queue.SimpleQueue()}
        self._threads: list[threading.Thread] = []
        self.frame: list[Row] = []
        self.frames = 0
        app.dispatch = self._dispatch

    # thread-safe inputs
    def press(self, key: Key, *, burst: bool = False) -> None:
        self._keys.put((key, burst))
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

    def shutdown(self) -> bool:
        """Stop painting at once, then stop the threads. When this returns no paint is under way and
        none will start, even if a thread is still inside a slow call; True when every thread ended."""

        with self._paint_lock:
            self._halt.set()
        self._wake.set()
        for lane in self._lanes.values():
            lane.put(None)
        for t in self._threads:
            t.join(timeout=self.join_timeout_s)
        return not any(t.is_alive() for t in self._threads)

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
                key, burst = self._keys.get_nowait()
            except queue.Empty:
                break
            app.press(key, burst=burst)
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
            with self._paint_lock:
                if not self._halt.is_set():  # after shutdown() nothing is painted over the restored terminal
                    self._sink(self.frame)
