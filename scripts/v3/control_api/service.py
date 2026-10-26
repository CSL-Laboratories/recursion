"""Control API core: authenticated JSON-RPC 2.0 over an injected route adapter set.

Transport-free. ``ControlService.handle`` takes the raw request body and the
``Authorization`` header value and returns a JSON-RPC response object, so the
HTTP layer, the tests and the recorder all exercise one dispatch path.

Every method needs a valid bearer token; the tenant comes from the token, never
from a parameter. A run is a Route Contract v0 execution: zero-tools, prompt on
stdin, digest-only records. Live adapters stay ``unsupported`` (CSL-460), so no
provider can be reached through this service.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema

from scripts.v3.control_api import errors as err
from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE, Principal, TokenRegistry
from scripts.v3.control_api.events import EventJournal
from scripts.v3.route_adapter.session import SessionKey
from scripts.v3.route_adapter.credentials import CredentialRef
from scripts.v3.route_contract.errors import RouteRefusal
from scripts.v3.route_contract.model import ExecutionRequest, canonical_json, sha256_digest

API_VERSION = "control-api@0"
MAX_BODY_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 32 * 1024
MAX_SESSIONS_PER_TENANT = 64
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 120.0

_SESSION_ID = re.compile(r"\A[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_ENGINE_ID = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")


class RouteAdapterLike(Protocol):
    """Structural type of what the service needs from an adapter."""

    engine_id: str

    def support(self) -> dict[str, object]: ...

    def execute(self, request: ExecutionRequest) -> Any: ...


def make_validator(schema: dict[str, Any]) -> Callable[[Any], str | None]:
    """Reason string for the first schema violation, or None when valid."""

    validator = jsonschema.Draft202012Validator(schema)

    def validate(body: Any) -> str | None:
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        return errors[0].message if errors else None

    return validate


@dataclass
class _Session:
    engine: str
    session: str
    runs: int = 0
    in_flight: bool = False
    closed: bool = False
    last_status: str | None = None
    last_output_digest: str | None = None


@dataclass
class _Tenant:
    sessions: dict[tuple[str, str], _Session] = field(default_factory=dict)
    request_seq: int = 0


class ControlService:
    def __init__(
        self,
        *,
        adapters: Mapping[str, RouteAdapterLike],
        tokens: TokenRegistry,
        output_schema: dict[str, Any],
        credential_provider: str,
        credential_name: str,
        cli_model: str,
        journal: EventJournal | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("at least one adapter is required")
        for engine, adapter in adapters.items():
            if not _ENGINE_ID.match(engine) or adapter.engine_id != engine:
                raise ValueError("adapter registry key must equal its engine_id")
        self._adapters = dict(adapters)
        self._tokens = tokens
        self._schema = output_schema
        self._credential_provider = credential_provider
        self._credential_name = credential_name
        self._cli_model = cli_model
        self._journal = journal or EventJournal()
        self._lock = threading.Lock()
        self._tenants: dict[str, _Tenant] = {}
        self._methods: dict[str, tuple[str, Callable[[Principal, dict[str, Any]], Any]]] = {
            "service.info": (SCOPE_OBSERVE, self._service_info),
            "route.list": (SCOPE_OBSERVE, self._route_list),
            "session.open": (SCOPE_CONTROL, self._session_open),
            "session.run": (SCOPE_CONTROL, self._session_run),
            "session.close": (SCOPE_CONTROL, self._session_close),
            "session.get": (SCOPE_OBSERVE, self._session_get),
            "events.poll": (SCOPE_OBSERVE, self._events_poll),
        }

    @property
    def journal(self) -> EventJournal:
        return self._journal

    def principal_for(self, authorization: str | None) -> Principal:
        return self._tokens.authenticate(authorization)

    def handle(self, body: bytes | str, authorization: str | None) -> dict[str, Any]:
        """One JSON-RPC exchange. Never raises; faults become error objects.

        Authentication is checked before the body is parsed, so an
        unauthenticated caller learns nothing about the method table.
        """

        request_id: Any = None
        try:
            principal = self._tokens.authenticate(authorization)
            raw = body.encode("utf-8") if isinstance(body, str) else body
            if len(raw) > MAX_BODY_BYTES:
                raise err.TooLarge("body_too_large")
            try:
                message = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
            except (ValueError, UnicodeDecodeError):
                raise err.ParseFailure() from None
            if not isinstance(message, dict):
                raise err.InvalidRequest("batch_unsupported")
            request_id = message.get("id")
            if not (request_id is None or isinstance(request_id, (str, int))) or isinstance(
                request_id, bool
            ):
                request_id = None
                raise err.InvalidRequest()
            if set(message) - {"jsonrpc", "id", "method", "params"} or message.get("jsonrpc") != "2.0":
                raise err.InvalidRequest()
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise err.InvalidRequest()
            entry = self._methods.get(method)
            if entry is None:
                raise err.MethodNotFound()
            scope, handler = entry
            principal.require(scope)
            result = handler(principal, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except err.ControlError as error:
            return {"jsonrpc": "2.0", "id": request_id, "error": error.to_error()}
        except Exception:  # noqa: BLE001 - nothing from the fault is echoed to the caller
            return {"jsonrpc": "2.0", "id": request_id, "error": err.ControlError().to_error()}

    def _tenant(self, tenant: str) -> _Tenant:
        return self._tenants.setdefault(tenant, _Tenant())

    def _service_info(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params)
        return {
            "api_version": API_VERSION,
            "tenant": principal.tenant,
            "scopes": sorted(principal.scopes),
            "engines": sorted(self._adapters),
            "live_calls": False,
            "tool_profile": "zero-tools",
        }

    def _route_list(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params)
        return {"routes": [self._adapters[name].support() for name in sorted(self._adapters)]}

    def _session_open(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, engine=str, session=str)
        engine, session = self._engine(params), _session_id(params)
        with self._lock:
            tenant = self._tenant(principal.tenant)
            existing = tenant.sessions.get((engine, session))
            if existing is not None:
                if existing.closed:
                    raise err.SessionClosed()
                return _session_view(existing)
            active = sum(1 for item in tenant.sessions.values() if not item.closed)
            if active >= MAX_SESSIONS_PER_TENANT:
                raise err.RunRefused("session_limit", data={"code": "session_limit"})
            state = _Session(engine=engine, session=session)
            tenant.sessions[(engine, session)] = state
            view = _session_view(state)
        self._journal.append(principal.tenant, "session.opened", session=session, engine=engine)
        return view

    def _session_get(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, engine=str, session=str)
        engine, session = self._engine(params), _session_id(params)
        with self._lock:
            return _session_view(self._existing(principal.tenant, engine, session))

    def _session_close(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, engine=str, session=str)
        engine, session = self._engine(params), _session_id(params)
        with self._lock:
            state = self._existing(principal.tenant, engine, session)
            if state.closed:
                return _session_view(state)
            if state.in_flight:
                raise err.SessionBusy()
            state.closed = True
            view = _session_view(state)
        self._journal.append(principal.tenant, "session.closed", session=session, engine=engine)
        return view

    def _session_run(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(params, optional={"timeout_s": (int, float)}, engine=str, session=str, prompt=str)
        engine, session = self._engine(params), _session_id(params)
        prompt = params["prompt"]
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise err.InvalidParams("prompt_size")
        try:
            prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise err.InvalidParams("prompt_encoding") from None
        timeout_s = params.get("timeout_s", DEFAULT_TIMEOUT_S)
        if isinstance(timeout_s, bool) or not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise err.InvalidParams("timeout_range")
        adapter = self._adapters[engine]
        with self._lock:
            state = self._existing(principal.tenant, engine, session)
            if state.closed:
                raise err.SessionClosed()
            if state.in_flight:
                raise err.SessionBusy()
            state.in_flight = True
            tenant = self._tenant(principal.tenant)
            tenant.request_seq += 1
            request_id = f"req-{tenant.request_seq}"
        try:
            request = ExecutionRequest(
                request_id=request_id,
                route_id=f"route-{engine}",
                engine_id=engine,
                route_descriptor_digest=sha256_digest(canonical_json(adapter.support())),
                process_requirement_id="proc-req-control-api",
                workload_id="control-api",
                session=SessionKey(principal.tenant, f"route-{engine}", session),
                credential=CredentialRef(
                    tenant_id=principal.tenant,
                    provider=self._credential_provider,
                    name=self._credential_name,
                ),
                cli_model=self._cli_model,
                prompt=prompt,
                output_schema=self._schema,
                timeout_s=float(timeout_s),
            )
            request_record = request.to_record()
            self._journal.append(
                principal.tenant,
                "run.started",
                session=session,
                engine=engine,
                data={"request_id": request_id, "prompt_digest": request_record["prompt_digest"]},
            )
            try:
                outcome = adapter.execute(request)
            except RouteRefusal as refusal:
                self._journal.append(
                    principal.tenant,
                    "run.refused",
                    session=session,
                    engine=engine,
                    data={"request_id": request_id, "code": refusal.code},
                )
                if refusal.code == "busy":
                    raise err.SessionBusy() from None
                raise err.RunRefused(data={"code": refusal.code, "request_id": request_id}) from None
            result_record = outcome.to_record(request)
            with self._lock:
                state.runs += 1
                state.last_status = result_record["status"]
                state.last_output_digest = result_record["output_digest"]
            self._journal.append(
                principal.tenant,
                "run.finished",
                session=session,
                engine=engine,
                data={
                    "request_id": request_id,
                    "status": result_record["status"],
                    "error": result_record["error"],
                    "repaired": result_record["repaired"],
                    "output_digest": result_record["output_digest"],
                },
            )
            return {
                "request": request_record,
                "result": result_record,
                "adapter_events": list(outcome.events),
            }
        finally:
            with self._lock:
                state.in_flight = False

    def _events_poll(self, principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
        _only(
            params,
            optional={"limit": int, "wait_s": (int, float)},
            after=int,
        )
        after = params["after"]
        if after < 0:
            raise err.InvalidParams("after_range")
        return self._journal.poll(
            principal.tenant,
            after,
            limit=params.get("limit", 100),
            wait_s=params.get("wait_s", 0.0),
        )

    def _engine(self, params: dict[str, Any]) -> str:
        engine = params["engine"]
        if engine not in self._adapters:
            raise err.InvalidParams("unknown_engine")
        return engine

    def _existing(self, tenant: str, engine: str, session: str) -> _Session:
        state = self._tenants.get(tenant, _Tenant()).sessions.get((engine, session))
        if state is None:
            raise err.UnknownSession()
        return state


def _no_constant(constant: str) -> None:
    raise ValueError(f"non-finite JSON constant {constant!r}")


def _session_id(params: dict[str, Any]) -> str:
    session = params["session"]
    if not _SESSION_ID.match(session) or len(session) > 64:
        raise err.InvalidParams("session_id")
    return session


def _session_view(state: _Session) -> dict[str, Any]:
    return {
        "engine": state.engine,
        "session": state.session,
        "state": "closed" if state.closed else ("busy" if state.in_flight else "open"),
        "runs": state.runs,
        "last_status": state.last_status,
        "last_output_digest": state.last_output_digest,
    }


def _only(
    params: dict[str, Any],
    *,
    optional: Mapping[str, type | tuple[type, ...]] | None = None,
    **required: type | tuple[type, ...],
) -> None:
    """Closed parameter set: unknown keys, missing keys and wrong types are refused."""

    optional = optional or {}
    unknown = set(params) - set(required) - set(optional)
    if unknown:
        raise err.InvalidParams("unknown_param")
    for name, kind in required.items():
        if name not in params:
            raise err.InvalidParams("missing_param")
        _typed(params[name], kind)
    for name, kind in optional.items():
        if name in params:
            _typed(params[name], kind)


def _typed(value: Any, kind: type | tuple[type, ...]) -> None:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:
        raise err.InvalidParams("param_type")
    if not isinstance(value, kinds):
        raise err.InvalidParams("param_type")
