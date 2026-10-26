"""Reference executor: the conformance subject, run against the AT1 fake driver.

Order of checks, each fail-closed and each before any process is spawned:
zero-tools policy, engine health breaker, actual argv inspection, pinned cwd,
secret refusal, then session ownership (Busy, custody) via the driver.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from scripts.v3.route_adapter.errors import Busy, CustodyDenied
from scripts.v3.route_adapter.fake import FakeRouteDriver
from scripts.v3.route_contract import argv as argv_mod
from scripts.v3.route_contract.errors import (
    RouteRefusal,
    ToolPolicyRefused,
    Unsupported,
)
from scripts.v3.route_contract.health import HealthPort, limit_signal, no_nonfinite
from scripts.v3.route_contract.model import (
    STATUS_LIMIT_HIT,
    STATUS_NEEDS_HUMAN,
    STATUS_OK,
    STATUS_TIMEOUT,
    ExecutionRequest,
    ExecutionResult,
    sha256_digest,
)
from scripts.v3.route_contract.transport import ProcResult, Transport

REPAIR_REASON_GENERIC = "output did not match the required schema"
_REPAIR_PREFIX = "\n\nYour previous output was rejected: "
_REPAIR_SUFFIX = "\nReturn ONLY corrected JSON that matches the required schema; no prose."
_FENCE = re.compile(r"\A```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```\s*\Z", re.S)

Validate = Callable[[Any], "str | None"]


def repair_prompt_for(prompt: str, reason: object) -> str:
    # Ignore validator text: it can quote private response values.
    del reason
    return prompt + _REPAIR_PREFIX + REPAIR_REASON_GENERIC + _REPAIR_SUFFIX


def object_output_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "object"


def _output_bytes(proc: ProcResult) -> int:
    return len((proc.stdout or "").encode("utf-8")) + len((proc.stderr or "").encode("utf-8"))


def _strip_fence(text: str) -> str:
    stripped = (text or "").strip()
    match = _FENCE.match(stripped)
    return match.group("body").strip() if match else stripped


def _loads(text: str) -> Any:
    return json.loads(_strip_fence(text), parse_constant=no_nonfinite)


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return _loads(value)
    except ValueError:
        return value


def _parse_codex(stdout: str, fallback: str | None) -> tuple[Any, str | None, Any]:
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line, parse_constant=no_nonfinite)
        except ValueError:
            return None, fallback, None
        if isinstance(event, dict):
            events.append(event)
    if not events:
        return None, fallback, None
    session, body, has_error = fallback, None, False
    for event in events:
        session = (
            event.get("thread_id") or event.get("session_id")
            or event.get("threadId") or event.get("sessionId") or session
        )
        if (
            str(event.get("type", "")).lower() in {"error", "turn.failed"}
            or event.get("is_error")
            or str(event.get("status", "")).lower() in {"error", "failed"}
        ):
            has_error = True
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() in {"error", "turn.failed"} or item.get("is_error"):
            has_error = True
        if str(item.get("type", "")).lower() == "agent_message" and "text" in item:
            body = _decode(item.get("text"))
    envelope: dict[str, Any] = {"is_error": has_error}
    if has_error:
        envelope["subtype"] = "error_codex_event"
    return envelope, session, body


def _parse_output(stdout: str, engine: str, fallback: str | None) -> tuple[Any, str | None, Any]:
    text = (stdout or "").strip()
    if not text:
        return None, fallback, None
    if engine == argv_mod.ENGINE_CODEX:
        return _parse_codex(text, fallback)
    try:
        envelope = _loads(text)
    except ValueError:
        return None, fallback, None
    session, body = fallback, envelope
    if isinstance(envelope, dict):
        session = envelope.get("session_id") or envelope.get("sessionId") or session
        if "result" in envelope:
            body = envelope["result"]
            if isinstance(body, str):
                body = _decode(body)
    return envelope, session, body


class ReferenceAdapter:
    """Executes a zero-tools request through a transport and the fake driver.

    ``kind`` is ``fake``: the transport is a scripted or local-helper process,
    never a provider. Live adapters come from ``live_adapter`` and are always
    ``unsupported`` in v0.
    """

    kind = "fake"

    def __init__(
        self,
        engine_id: str,
        *,
        transport: Transport,
        driver: FakeRouteDriver,
        health: HealthPort,
        validate: Validate,
        secret_values: argv_mod.SecretSource = None,
        pinned_cwd: str | None = None,
    ) -> None:
        if engine_id not in argv_mod.ENGINES:
            raise Unsupported()
        self.engine_id = engine_id
        self._transport = transport
        self._driver = driver
        self._health = health
        self._validate = validate
        self._secret_values = secret_values
        self._pinned_cwd = pinned_cwd

    def support(self) -> dict[str, object]:
        return {
            "type": "AdapterSupport@0",
            "engine_id": self.engine_id,
            "adapter_kind": "fake",
            "state": "supported",
            "reason": "fake-driver",
            "live_calls": False,
        }

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.engine_id != self.engine_id:
            raise Unsupported()
        if not object_output_schema(request.output_schema):
            raise Unsupported()
        if request.allowed_tools:
            raise ToolPolicyRefused()
        if self._health.is_open(self.engine_id):
            return ExecutionResult(
                status=STATUS_LIMIT_HIT,
                error="breaker_open",
                dispatched=False,
                usage_state=self._health.state(self.engine_id),
            )
        argv = argv_mod.build_argv(
            self.engine_id,
            cli_model=request.cli_model,
            max_turns=request.max_turns,
            schema_path=request.schema_path,
        )
        self._preflight(argv, request.prompt, request.cwd)
        try:
            operation = self._driver.start(
                request.session, request.credential, caller_tenant=request.session.tenant
            )
        except Busy:
            raise RouteRefusal("busy") from None
        except CustodyDenied:
            raise RouteRefusal("custody_denied") from None
        released = False
        try:
            self._driver.send(operation, request.prompt)
            result = self._run(request, argv)
            result.events = self._driver.observe(operation)
            if result.status == STATUS_OK:
                self._driver.complete(operation)
            else:
                self._driver.cancel(operation)
            released = True
            return result
        finally:
            if not released:
                self._driver.cancel(operation)

    def _preflight(self, argv: list[str], prompt: str, cwd: str | None) -> None:
        argv_mod.inspect_argv(argv)
        argv_mod.assert_cwd(cwd, pinned_cwd=self._pinned_cwd)
        argv_mod.assert_no_secret(argv, prompt, self._secret_values)

    def _call(self, request: ExecutionRequest, argv: list[str], prompt: str) -> ProcResult:
        return self._transport.run(
            argv, prompt, request.timeout_s, request.cwd, request.max_output_bytes
        )

    def _fault(
        self, proc: ProcResult, error: str, state: str, status: str, *, repaired: bool
    ) -> ExecutionResult:
        usage_state = self._health.fault(self.engine_id, state, error)
        termination = None
        if proc.timed_out or proc.oversized:
            termination = proc.termination or {
                "type": "ProcessTerminationObservation@1",
                "tree_status": "unknown",
                "remaining_process_refs": [],
                "effect_conclusion": "not-evaluated",
            }
        return ExecutionResult(
            status=status,
            error=error,
            usage_state=usage_state,
            repaired=repaired,
            output_digest=sha256_digest(proc.stdout) if proc.stdout else None,
            health_event={
                "type": "RouteHealthEvent@0",
                "engine_id": self.engine_id,
                "state": state,
                "error": error,
            },
            termination=termination,
        )

    def _checked(
        self, request: ExecutionRequest, prompt: str, proc: ProcResult, *, repaired: bool
    ) -> tuple[ExecutionResult | None, Any, str | None, Any]:
        parsed, session, body = None, None, None
        error, state, status = "limit_or_error", "exhausted", STATUS_LIMIT_HIT
        if proc.oversized or _output_bytes(proc) > request.max_output_bytes:
            error, status = "output_too_large", STATUS_LIMIT_HIT
        elif proc.timed_out:
            error, status = "timeout", STATUS_TIMEOUT
        elif proc.returncode != 0 or limit_signal(proc.stdout or "", proc.stderr or "", prompt):
            pass
        elif not (proc.stdout or "").strip():
            error, state = "empty_output", "degraded"
        else:
            parsed, session, body = _parse_output(proc.stdout, self.engine_id, None)
            if not (
                isinstance(parsed, dict)
                and (parsed.get("is_error") or str(parsed.get("subtype", "")).startswith("error"))
            ):
                self._health.record_ok(self.engine_id)
                return None, parsed, session, body
            error, state = "vendor_error_envelope", "degraded"
        if repaired:
            error = "repair_" + ("vendor_error" if error == "vendor_error_envelope" else error)
        return self._fault(proc, error, state, status, repaired=repaired), parsed, session, body

    def _run(self, request: ExecutionRequest, argv: list[str]) -> ExecutionResult:
        proc = self._call(request, argv, request.prompt)
        fault, parsed, session, body = self._checked(request, request.prompt, proc, repaired=False)
        if fault is not None:
            return fault
        reason = (
            "output was not valid JSON" if parsed is None else self._validate(body)
        )
        if reason is None and not isinstance(body, dict):
            reason = "output was not a JSON object"
        if reason is None:
            return self._ok(proc, body, session, repaired=False)
        repair_prompt = repair_prompt_for(request.prompt, reason)
        self._preflight(argv, repair_prompt, request.cwd)
        proc2 = self._call(request, argv, repair_prompt)
        fault, parsed, session2, body2 = self._checked(request, repair_prompt, proc2, repaired=True)
        if fault is not None:
            return fault
        reason2 = "output was not valid JSON" if parsed is None else self._validate(body2)
        if reason2 is None and not isinstance(body2, dict):
            reason2 = "output was not a JSON object"
        if reason2 is None:
            return self._ok(proc2, body2, session2 or session, repaired=True)
        return ExecutionResult(
            status=STATUS_NEEDS_HUMAN,
            error="repair_still_invalid",
            repaired=True,
            session_id=session2 or session,
            output_digest=sha256_digest(proc2.stdout),
        )

    def _ok(
        self, proc: ProcResult, body: Any, session: str | None, *, repaired: bool
    ) -> ExecutionResult:
        if not isinstance(body, dict):
            raise TypeError("ok result requires a JSON object body")
        return ExecutionResult(
            status=STATUS_OK,
            validated=body,
            session_id=session,
            repaired=repaired,
            output_digest=sha256_digest(proc.stdout),
        )
