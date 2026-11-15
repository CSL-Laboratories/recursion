"""Lane ROUTING's registrant: what ``control/hooks.py`` (SPINE's seams) calls once, at composition.

``register(hooks)`` registers routing's part of a start (seam S6) under the name ``routing``, with
``chooses_engine=True``, so it runs first and may choose the engine of a start that names none. It only
registers: it opens nothing, starts no thread and reads no state. Everything is read when a start asks.

The registrant, ``normalize(principal, request)``, is a pure decision with no side effect: no file is
written, no event emitted, nothing reserved. For a NEW start (a retry is matched on the raw intent first and
never comes here):

- A request that names its ``engine`` and carries no ``hand_pick`` is not routed: ``{}``, and the start
  launches that engine's ``routes.json`` entry as it does today. Nothing is read.
- A request with an ``engine`` and a ``hand_pick`` of another engine is refused, ``request_invalid``,
  detail ``engine_and_hand_pick_differ`` (the Liaison's ruling of 4:20 PM, Nov 14, 2026).
- With no ``routing.json`` routing is off: ``{}``, and the start is today's (a request that names no engine
  answers ``request_invalid``, detail ``engine``, from the start path).
- Otherwise ``routing.json`` and ``routes.json`` are read from the daemon's config folder
  (``$XDG_CONFIG_HOME/recursion``, as ``compose_control`` reads ``routes.json``), detection is made, and
  ``decide()`` chooses: a hand pick wins, then a rule, then the default. A refusal is ``request_invalid``
  with routing's code and reason in the detail (the Liaison's 3:11 PM form, until SPINE's schema revision
  gives routing a code of its own); a routing file that cannot be used refuses
  ``routing_config_invalid:<code>``. A routed start answers ``route_entry``, the ``routes.json`` entry of the
  chosen engine exactly as ``load_routes`` gives it, with the decision's model and, for a rule or the
  default, the routing file's own limits for that route, and ``route_decision`` (common.v1).

What the registrant cannot see (questions.md Q14 and Q18). It is called with the principal and the request
only. So detection here reads the loaded routes, the daemon's own pre-launch refusals and the engines the
composition has, and leaves the breaker to the starter (an open breaker refuses the launch there). The key
store's metadata is not given to it, so the catalog has no API keys: a routing file with an API-key login
refuses ``routing_config_invalid:unknown_connection`` until KEYS gives the registrant its metadata.
``run.start`` carries no ``task_kind`` or ``tags`` yet (SPINE's next schema revision), and every rule has a
condition on one of them, so no rule matches a start yet: a start gets its hand pick or the default.
"""
from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path

from recursion_core.routing import config as rc
from recursion_core.routing import detect as rd
from recursion_core.routing import router

NAME = "routing"
FIELDS = ("hand_pick",)
KEYS = ("route_entry", "route_decision")


def config_dir(environ: Mapping | None = None) -> Path:
    """The daemon's config folder, as ``compose_control`` finds ``routes.json``."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "recursion"


def _refusal(detail: str):
    from recursion_core.control.hooks import Refusal
    return Refusal("request_invalid", detail)


def route_entry(config: rc.Config, decision: Mapping) -> dict:
    """The ``routes.json`` entry of the decided engine, as ``load_routes`` gives the starter, with the decision's
    model and, when the decision came from a rule or the default and the routing file gives that route
    limits, those limits inside the route."""
    entry = copy.deepcopy(config.routes[decision["engine"]])
    entry["route"]["model"] = decision["model"]
    limits, source = router._route_limits(config, decision)
    if source == "routing":
        entry["route"]["limits"] = limits
    return entry


def make_normalize(hooks, *, environ: Mapping | None = None):
    """The registrant for ``hooks``: it reads the engine table's names when a start asks, after composition."""

    def normalize(principal, request):
        engine, hand = request.get("engine"), request.get("hand_pick")
        if engine is not None and hand is None:
            return {}
        if engine is not None and isinstance(hand, Mapping) and hand.get("engine") != engine:
            return _refusal("engine_and_hand_pick_differ")
        engines = tuple(hooks.engines.names())
        try:
            config = rc.load(config_dir(environ), {}, engines=engines)
        except rc.ConfigError as exc:
            return _refusal(f"routing_config_invalid:{exc.code}")
        except rc.RoutingUnreadable:
            return _refusal("routing_config_invalid:unreadable")
        if config is None:
            return {}
        detection = rd.detect(config, route_refusal=rd.daemon_route_refusal(None), active=engines)
        decision = router.decide(config, detection, {} if hand is None else {"hand_pick": hand})
        if not decision["ok"]:
            reason = decision.get("reason")
            return _refusal(decision["code"] if reason is None else f'{decision["code"]}:{reason}')
        return {"route_entry": route_entry(config, decision), "route_decision": router.route_decision(decision)}

    return normalize


def register(hooks) -> None:
    hooks.start_registrant(NAME, FIELDS, KEYS, make_normalize(hooks), chooses_engine=True)
