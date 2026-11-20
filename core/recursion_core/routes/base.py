"""Shared run loop for route adapters (CSL-797, candidate). See README.md."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.host import PLATFORM_UNSUPPORTED, platform_supported
from recursion_core.routes.launch import EngineProcess, Launcher, ReadTimeout

# credentials(connection_id, env_name) -> secret value. Owned by the core's custody layer (A3), injected.
Credentials = Callable[[str, str], str]


class Deadline(Exception):
    """Recursion's own deadline passed. Request-local: never an engine fault (PRODUCT.md S4)."""


class RouteAdapter:
    """One engine behind the route contract. Subclasses build argv/env and drive the protocol."""

    engine = ""

    def __init__(self, launcher: Launcher, clock: Callable[[], float], credentials: Credentials,
                 parent_env: Mapping[str, str], *, wall_clock: Callable[[], float] | None = None) -> None:
        self._launcher, self._clock, self._credentials = launcher, clock, credentials
        self._parent_env = dict(parent_env)  # explicit input; never os.environ
        # ``clock`` may be monotonic. Vendor epochs are translated with ``wall_clock`` (Unix seconds).
        self._wall = wall_clock if wall_clock is not None else time.time

    def vendor_reset(self, epoch: object) -> float | None:
        """Vendor ``resetsAt`` (Unix epoch) as an absolute reading of the breaker clock."""

        return c.vendor_reset(epoch, self._clock, self._wall)

    def run(self, request: dict, deadline_at: float) -> dict:
        rid = request["request_id"]
        if not platform_supported():
            # Lucas, Nov 12 ("Yes, as recommended", decision sheet D9): "Windows refuses uniformly at the
            # shared adapter boundary, before any path or connector is looked at" (R3-7). Nothing is built:
            # no argv, no environment, no credential lookup, and the launcher is never called.
            return c.result(rid, c.LAUNCH_REFUSED, detail=PLATFORM_UNSUPPORTED)
        try:
            argv, env = self.argv(request), self.env(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        # STARTED CHILDREN ARE FENCED ON EVERY WAY OUT OF THIS METHOD (an override of ``run`` owns its own
        # cleanup). Once the launcher has returned a process:
        # - A typed exit (deadline, protocol error, engine io) fences with its own reason and answers.
        # - Any other Exception fences with UNEXPECTED_FENCE. When that fence succeeds, the method answers:
        #   a RouteError keeps its own code and detail; any other Exception is engine_error and the detail is
        #   the type name, never the message. An interruption (a BaseException that is not an Exception) still
        #   leaves as the same object. When that fence raises, the ORIGINAL exception leaves, with the fence
        #   failure recorded on it.
        # - If a typed exit's fence raises, the ORIGINAL typed exception leaves instead of the answer (its type
        #   is what callers decide on); the fence is tried once more as UNEXPECTED_FENCE, and the first
        #   failure is noted on the original.
        # - A fence failure never replaces the exception that leaves. When no fence attempt succeeded, the
        #   exception that leaves carries the failures in CLEANUP_FAILURES (and a note): the child may still
        #   be running, so a caller that finds the attribute must not treat the exit as clean. The attribute is
        #   set even when the exception's own __setattr__ refuses it (BaseException's own setter is used then).
        # - A typed exit whose fence fails gives no typed answer: its exception reaches the caller
        #   (RouteService._run records engine_error with the breaker and re-raises). An interruption raised BY a
        #   fence is recorded as a cleanup failure like any other, and the retry still follows.
        # What this method cannot cover is an asynchronous exception (a signal handler's) delivered between
        # the launcher's return and the store into ``process``, or inside the outer handler before its fence
        # has run; the process then exists only in the launcher's own record of the run (daemon/launcher.py
        # publishes it with RunContext.launched). One delivered after a typed fence succeeded and before
        # ``fenced`` is set fences twice, which is harmless; setting the flag first would leave a child
        # unfenced instead.
        process = None
        fenced = False
        events: list[dict] = []
        try:
            try:
                process = self._launcher(argv, env=env, shell=False)
            except c.RouteError as err:
                # The launcher's own typed refusal (for example the child-environment policy refusing a
                # value). Nothing was started, so this is a refusal before launch, not an engine fault.
                return c.result(rid, err.code, detail=err.detail)
            except OSError as err:
                return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
            if self._clock() >= deadline_at:
                # The launch spent the deadline. Do not write the prompt. The child is fenced.
                err = Deadline()
                done = _typed_fence(process, "deadline", err)
                fenced = True
                if not done:
                    raise err
                return c.result(rid, c.DEADLINE_EXCEEDED, detail="recursion deadline")
            try:
                return self.drive(process, request, deadline_at, events)
            except Deadline as err:
                # For this request only: the breaker hears nothing, unless this fence fails (then the Deadline
                # leaves and RouteService._run records engine_error).
                done = _typed_fence(process, "deadline", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
            except _Protocol as err:
                done = _typed_fence(process, "protocol_error", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
            except OSError as err:
                # A child that exits before the request is written raises BrokenPipeError. That stays a
                # typed result, and the process is fenced, instead of escaping the route boundary.
                done = _typed_fence(process, "io_error", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
        except BaseException as err:
            # Inline here, not a helper: a helper's entry is one more point where an interruption could land
            # before the fence. (The typed exits' _typed_fence is entered with ``fenced`` still False, so an
            # interruption at its entry reaches this handler, which fences.)
            if process is not None and not fenced:
                try:
                    process.fence(UNEXPECTED_FENCE)
                except BaseException as fence_err:  # noqa: BLE001 - recorded; the original exception is what leaves
                    _record_cleanup_failure(err, fence_err, still_running=True)
                else:
                    # An Exception is answered once the child is fenced. A RouteError keeps its code. Any
                    # other Exception is engine_error and the detail is the type name, never the message.
                    # An interruption still leaves, so a caller can tell that case.
                    if isinstance(err, c.RouteError):
                        return c.result(rid, err.code, events=events, detail=err.detail)
                    if isinstance(err, Exception):
                        return c.result(rid, c.ENGINE_ERROR, events=events, detail=type(err).__name__)
            raise

    # -- subclass hooks -----------------------------------------------------------------------
    def argv(self, request: dict) -> list[str]:
        raise NotImplementedError

    def env(self, request: dict) -> dict[str, str]:
        raise NotImplementedError

    def drive(self, process: EngineProcess, request: dict, deadline_at: float, events: list[dict]) -> dict:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------------------------
    def credential(self, request: dict, allowed: tuple[str, ...]) -> tuple[str, str] | None:
        """The route's one credential, only when its connection is an API key it selects explicitly."""

        conn = request["route"]["connection"]
        if conn["kind"] != "api_key":
            return None
        name = conn["credential_env"]
        if name not in allowed:
            raise c.RouteError(c.LAUNCH_REFUSED, f"credential_env {name} not allowed for {self.engine}")
        try:
            value = self._credentials(conn["id"], name)
        except KeyError:
            value = ""
        if not isinstance(value, str) or not value:
            raise c.RouteError(c.LAUNCH_REFUSED, f"no credential for {conn['id']}")
        return name, value

    def read_message(self, process: EngineProcess, deadline_at: float) -> dict | None:
        """Next JSON object from stdout, ``None`` at EOF. Raises ``Deadline`` or ``_Protocol``."""

        remaining = deadline_at - self._clock()
        if remaining <= 0:
            raise Deadline()
        try:
            line = process.read_line(remaining)
        except ReadTimeout:
            raise Deadline() from None
        if line is None:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            raise _Protocol("stdout line is not JSON") from None
        if not isinstance(message, dict):
            raise _Protocol("stdout line is not a JSON object")
        return message


class _Protocol(Exception):
    pass


# The fence reason for a started child whose run leaves ``RouteAdapter.run`` by an exception that none of
# the typed exits answers (the same word lane ENGINE-E5's one-shot adapter uses for its own copy of the loop).
UNEXPECTED_FENCE = "unexpected"

# The attribute an exception leaving ``RouteAdapter.run`` carries when no fence of the started child succeeded:
# a tuple of the fence failures, oldest first. Its presence means the child may still be running.
CLEANUP_FAILURES = "cleanup_failures"
RUNNING_NOTE = "no fence of the started child succeeded: it may still be running"


def _force_attribute(err: BaseException, name: str, value: object) -> None:
    """Set ``name`` on ``err``; if its own ``__setattr__`` refuses (a frozen dataclass, a hook), set it the way
    ``BaseException`` itself does. Never raises."""

    try:
        setattr(err, name, value)
        return
    except BaseException:  # noqa: BLE001 - the type refused; its base can still take it
        pass
    try:
        BaseException.__setattr__(err, name, value)
    except BaseException:  # noqa: BLE001 - nothing more can be done; the exception still leaves unchanged
        pass


def _note(err: BaseException, text: str) -> None:
    """``err.add_note(text)``; if that raises and ``err`` has no notes yet, the note list is made as
    ``_force_attribute`` sets an attribute. Notes ``err`` already holds are never replaced. Never raises."""

    try:
        err.add_note(text)
        return
    except BaseException:  # noqa: BLE001 - see below
        pass
    try:
        if "__notes__" not in vars(err):
            _force_attribute(err, "__notes__", [text])
    except BaseException:  # noqa: BLE001 - an exception that takes no note still leaves unchanged
        pass


def _record_cleanup_failure(err: BaseException, fence_err: BaseException, *, still_running: bool) -> None:
    """Note ``fence_err`` on ``err``; when ``still_running``, also add it to ``err``'s CLEANUP_FAILURES, even on an
    exception whose own ``__setattr__`` refuses attributes (Astra's C1 in round 2). Never raises."""

    if still_running:
        try:
            failures = (*getattr(err, CLEANUP_FAILURES, ()), fence_err)
        except BaseException:  # noqa: BLE001 - an attribute that cannot be read is replaced
            failures = (fence_err,)
        _force_attribute(err, CLEANUP_FAILURES, failures)
    _note(err, f"fencing the child also failed: {type(fence_err).__name__}")
    try:
        if still_running and RUNNING_NOTE not in getattr(err, "__notes__", ()):
            _note(err, RUNNING_NOTE)
    except BaseException:  # noqa: BLE001 - notes that cannot be read are left as they are
        pass


def _typed_fence(process: EngineProcess, reason: str, err: BaseException) -> bool:
    """A typed exit's fence. True when it succeeded. Otherwise the fence is tried once more as
    UNEXPECTED_FENCE, every failure is recorded on ``err`` (``_record_cleanup_failure``), and False is
    returned so the caller raises ``err`` itself."""

    try:
        process.fence(reason)
        return True
    except BaseException as first:  # noqa: BLE001 - recorded on the typed exception, which then leaves
        failed = first
    try:
        process.fence(UNEXPECTED_FENCE)
    except BaseException as second:  # noqa: BLE001 - recorded on the typed exception, which then leaves
        _record_cleanup_failure(err, failed, still_running=True)
        _record_cleanup_failure(err, second, still_running=True)
        return False
    _record_cleanup_failure(err, failed, still_running=False)
    return False


def scalar(value: object) -> object:
    """Copy only scalar typed fields into events; never nested model payload or a non-finite number."""

    if isinstance(value, bool) or isinstance(value, str) or value is None or type(value) is int:
        return value
    if type(value) is float and value == value and value not in (float("inf"), float("-inf")):
        return value
    return None
