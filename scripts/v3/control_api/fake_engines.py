"""Fake Claude-Code-style and Codex-style engines behind the Control API.

``FakeEngineTransport`` is an in-process stand-in for a provider CLI. It never
spawns a process, opens a socket or reads a credential. It first runs the real
zero-tools argv inspector on whatever the adapter built, so a fake run proves
the adapter launched exactly the zero-tools profile for that engine, then
replies in that engine's own output shape: a ``--output-format json`` envelope
for the Claude path and ``--json`` event lines for the Codex path.

Output is a pure function of the prompt, so a recorded run is reproducible.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.v3.control_api.auth import TokenRegistry
from scripts.v3.control_api.events import EventJournal
from scripts.v3.control_api.service import ControlService, make_validator
from scripts.v3.route_adapter.credentials import CredentialRef, LocalCredentialResolver
from scripts.v3.route_adapter.fake import FakeRouteDriver
from scripts.v3.route_adapter.session import SessionOwner
from scripts.v3.route_contract import argv as argv_mod
from scripts.v3.route_contract.errors import ToolPolicyRefused
from scripts.v3.route_contract.executor import ReferenceAdapter
from scripts.v3.route_contract.health import InMemoryHealth
from scripts.v3.route_contract.transport import ProcResult

FAKE_MODEL = "fake-model"
FAKE_PROVIDER = "fake"
FAKE_CREDENTIAL = "route-ref"
ENGINES = (argv_mod.ENGINE_CLAUDE, argv_mod.ENGINE_CODEX)

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary"],
    "properties": {
        "verdict": {"enum": ["pass", "fail", "needs_human"]},
        "summary": {"type": "string", "maxLength": 200},
    },
}

BEHAVIORS = frozenset(
    {"ok", "not_json", "wrong_schema", "limit", "vendor_error", "timeout"}
)


def _digest8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


@dataclass
class FakeEngineTransport:
    """Scripted per-call behaviors; the default behavior is ``ok``."""

    engine: str
    script: Sequence[str] = ()
    calls: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.engine not in ENGINES:
            raise ValueError("unknown engine")
        unknown = set(self.script) - BEHAVIORS
        if unknown:
            raise ValueError(f"unknown behaviors: {sorted(unknown)}")
        self._pending = list(self.script)
        self._lock = threading.Lock()

    def run(
        self,
        argv: list[str],
        input_text: str,
        timeout_s: float,
        cwd: str | None,
        max_output_bytes: int | None = None,
    ) -> ProcResult:
        argv_mod.inspect_argv(argv)
        if Path(argv[0]).name != self.engine:
            raise ToolPolicyRefused()
        with self._lock:
            behavior = self._pending.pop(0) if self._pending else "ok"
            self.calls.append(
                {
                    "engine": self.engine,
                    "behavior": behavior,
                    "argv_profile": "zero-tools",
                    "prompt_digest": "sha256:" + hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                    "prompt_on_argv": any(input_text and input_text in token for token in argv),
                }
            )
        return self._reply(behavior, input_text)

    def _reply(self, behavior: str, prompt: str) -> ProcResult:
        session = f"fake-{self.engine}-{_digest8(prompt)}"
        if behavior == "limit":
            return ProcResult(1, "", "usage limit reached (fake)", False)
        if behavior == "vendor_error":
            return self._wrap(session, {"is_error": True, "subtype": "error_during_execution"}, "")
        if behavior == "timeout":
            return ProcResult(
                124,
                "",
                "[timeout]",
                True,
                {
                    "type": "ProcessTerminationObservation@1",
                    "tree_status": "terminated",
                    "remaining_process_refs": [],
                    "effect_conclusion": "not-evaluated",
                },
            )
        if behavior == "not_json":
            body_text = "I reviewed it and it looks fine."
        elif behavior == "wrong_schema":
            body_text = json.dumps({"verdict": "maybe"})
        else:
            body_text = json.dumps(
                {
                    "verdict": "pass",
                    "summary": f"fake {self.engine} reviewed prompt {_digest8(prompt)}",
                },
                sort_keys=True,
            )
        return self._wrap(session, {"is_error": False, "subtype": "success"}, body_text)

    def _wrap(self, session: str, flags: dict[str, object], body_text: str) -> ProcResult:
        if self.engine == argv_mod.ENGINE_CLAUDE:
            envelope = {"type": "result", "session_id": session, "result": body_text, **flags}
            return ProcResult(0, json.dumps(envelope, sort_keys=True), "", False)
        lines = [{"type": "thread.started", "thread_id": session}]
        if flags["is_error"]:
            lines.append({"type": "error", "message": "fake vendor error"})
        else:
            lines.append(
                {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": body_text}}
            )
        lines.append({"type": "turn.completed"})
        return ProcResult(0, "\n".join(json.dumps(line, sort_keys=True) for line in lines), "", False)


@dataclass
class FakeStack:
    """A wired fake service. ``issue`` mints a token and provisions a fake credential."""

    service: ControlService
    tokens: TokenRegistry
    transports: dict[str, FakeEngineTransport]
    journal: EventJournal
    _resolver: LocalCredentialResolver
    _material: str

    def issue(self, principal_id: str, tenant: str, scopes: set[str] | frozenset[str]) -> str:
        token = self.tokens.issue(principal_id, tenant, scopes)
        ref = CredentialRef(tenant, FAKE_PROVIDER, FAKE_CREDENTIAL)
        if ref not in self._resolver.public_refs():
            self._resolver.register(ref, self._material)
        return token


def build_fake_stack(
    *,
    scripts: Mapping[str, Sequence[str]] | None = None,
    journal: EventJournal | None = None,
) -> FakeStack:
    """Both engines over one fake driver. Credential material is random per stack."""

    scripts = scripts or {}
    unknown = set(scripts) - set(ENGINES)
    if unknown:
        raise ValueError(f"unknown engines: {sorted(unknown)}")
    material = "fake-" + secrets.token_urlsafe(24)
    resolver = LocalCredentialResolver()
    driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
    health = InMemoryHealth()
    validate = make_validator(VERDICT_SCHEMA)
    transports = {
        engine: FakeEngineTransport(engine, script=tuple(scripts.get(engine, ())))
        for engine in ENGINES
    }
    adapters = {
        engine: ReferenceAdapter(
            engine,
            transport=transports[engine],
            driver=driver,
            health=health,
            validate=validate,
            output_schema=VERDICT_SCHEMA,
            secret_values=[material],
        )
        for engine in ENGINES
    }
    tokens = TokenRegistry()
    journal = journal or EventJournal()
    service = ControlService(
        adapters=adapters,
        tokens=tokens,
        output_schema=VERDICT_SCHEMA,
        credential_provider=FAKE_PROVIDER,
        credential_name=FAKE_CREDENTIAL,
        cli_model=FAKE_MODEL,
        journal=journal,
    )
    return FakeStack(service, tokens, transports, journal, resolver, material)
