"""Lane ROUTING's registrant: what ``control/hooks.py`` (SPINE's seams) calls once, at composition.

``register(hooks)`` registers routing's part of a start (seam S6) under the name ``routing``, with
``chooses_engine=True``, so it runs first and may choose the engine of a start that names none. It only
registers: it opens nothing, starts no thread and reads no state. Everything is read when a start asks.

The registrant, ``normalize(principal, request)``, is a pure decision with no side effect: no file is
written, no event emitted, nothing reserved. For a NEW start (a retry is matched on the raw intent first and
never comes here):

- A request that names its ``engine`` and carries no ``hand_pick`` is not routed: ``{}``, and the start
  launches that engine's ``routes.json`` entry as it does today. Nothing is read.
- A ``hand_pick`` is read first, once, as ``decide()`` reads it (with ``router._task``, the router's own reader,
  so there is one rule for what a hand pick is: that private name and its ``_Refuse`` are part of this module's
  contract with ``router.py``, lane ROUTING's own), whether or not routing is on and whether or not the request
  names an engine. A ``hand_pick`` that is null is taken as absent, as the start path's schema has it (it gives
  an object or nothing). One that is not a hand pick (not an object of exactly ``engine`` and ``model``,
  an engine routing does not know, a model that is not a model id, or one that cannot be read) is refused,
  ``request_invalid``, detail ``bad_hand_pick:<reason>``.
- A request with an ``engine`` and a ``hand_pick`` of another engine is refused, ``request_invalid``,
  detail ``engine_and_hand_pick_differ`` (the Liaison's ruling of 4:20 PM, Nov 14, 2026), whether or not
  routing is on (its ruling of 4:53 PM, Nov 15, 2026): such a start contradicts itself, and no launch can honor it.
- With no ``routing.json`` routing is off: ``{}``, and the start is today's (a request that names no engine
  answers ``request_invalid``, detail ``engine``, from the start path). The exception is a hand pick that asks
  for more than the request's own engine: one that names a model (today's start launches ``routes.json``'s
  model), or one whose engine the request does not name (today's start has no engine to launch). It is refused,
  ``request_invalid``, detail ``hand_pick_without_routing``, and never dropped silently. A hand pick of the named
  engine whose model is null asks for nothing beyond that engine (questions.md Q21).
- Otherwise ``routing.json`` and ``routes.json`` are read from the daemon's config folder
  (``$XDG_CONFIG_HOME/recursion``, as ``compose_control`` reads ``routes.json``), detection is made, and
  ``decide()`` chooses: a hand pick wins, then a rule, then the default. A refusal is ``request_invalid``
  with routing's code and reason in the detail (the Liaison's 3:11 PM form, until SPINE's schema revision
  gives routing a code of its own); a routing file that cannot be used refuses
  ``routing_config_invalid:<code>``, and a file, or a chosen ``routes.json`` entry, nested deeper than it can be
  read or copied refuses ``routing_config_invalid:unreadable``. A routed start answers ``route_entry``, the ``routes.json`` entry of the
  chosen engine exactly as ``load_routes`` gives it, with the decision's model (in the route, and in its
  profile's own model selector when the route has one) and, for a rule or the default, the routing file's own
  limits for that route, and ``route_decision`` (common.v1).

"Exactly today's start" is read so (the Liaison's ruling of 4:53 PM, Nov 15, 2026): a request that carries NO hand
pick behaves exactly as today's start does when routing.json is absent (the same launch, the same reads, nothing
new); a request that carries a hand pick is a new kind of request, and it never silently loses what it asked for.

What the registrant cannot see (questions.md Q14, Q18 and Q22). It is called with the principal and the
request only. So detection here reads the loaded routes, the daemon's own pre-launch refusals and the engines
the composition has, and leaves the breaker to the starter: an open breaker, and a route whose model or
connection the breaker store cannot record (``route_unbindable``, which answers nothing without a breaker),
refuse the launch there (``launch_refused``), as a profile route's own guard refuses a picked model of
another provider than the profile's (``model_provider_mismatch``). Nor is it given the composition's environment: it reads the
config folder from the process's own environment at each start (``config_dir``), which is where
``compose_control`` reads ``routes.json`` when it is given no ``environ``, as the daemon gives none. A
composition made with another ``environ`` would read ``routes.json`` from there and ``routing.json`` from
here. The key store's metadata is not given to it, so the catalog has no API keys: a routing file with an API-key login
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


def route_entry(config: rc.Config, decision: object) -> dict | None:
    """The ``routes.json`` entry of the decided engine, as ``load_routes`` gives the starter, with the decision's
    model and, when the decision came from a rule or the default and the routing file gives that route
    limits, those limits inside the route. A profile route's own model selector (``route.profile.model``) gets
    the same model, so the profile's guard binds the model the decision chose; the config's entry is not
    changed (the entry is a copy). None for anything ``router.route_decision`` answers None for (what a routed
    decision is, is one rule, kept there), for a decision whose engine this config has no route for, and for a
    chosen entry nested deeper than it can be copied (the registrant then refuses
    ``routing_config_invalid:unreadable``). A failure in routing's own work after that, the limits lookup, is
    raised, never laid on the operator's file; a ``BaseException`` passes through."""
    try:
        if router.route_decision(decision) is None:  # not a routed decision that decide publishes: no entry either
            return None
        engine, model = decision["engine"], decision["model"]
        if engine not in config.routes:
            return None
        entry = copy.deepcopy(config.routes[engine])
    except Exception:  # noqa: BLE001 - a decision that cannot be read, or an entry nested deeper than it can be copied
        return None
    entry["route"]["model"] = model
    profile = entry["route"].get("profile")
    if isinstance(profile, dict) and "model" in profile:
        profile["model"] = model  # the profile's selector agrees with the decision (astra R2)
    limits, source = router._route_limits(config, decision)  # routing's own work: a failure here is raised
    if source == "routing":
        entry["route"]["limits"] = limits
    return entry


def make_normalize(hooks, *, environ: Mapping | None = None):
    """The registrant for ``hooks``: it reads the engine table's names when a start asks, after composition."""

    def normalize(principal, request):
        engine, hand = router._plain(request.get("engine")), request.get("hand_pick")  # the engine as its plain text
        if engine is not None and hand is None:
            return {}
        picked = None
        if hand is not None:  # read once, as decide() reads it, and refused as it refuses, routing on or off
            try:
                picked = router._task({"hand_pick": hand})[2]
            except router._Refuse as refused:
                return _refusal(f"{refused.code}:{refused.reason}")
            if engine is not None and picked["engine"] != engine:
                return _refusal("engine_and_hand_pick_differ")
        engines = tuple(hooks.engines.names())
        try:
            config = rc.load(config_dir(environ), {}, engines=engines)
        except rc.ConfigError as exc:
            return _refusal(f"routing_config_invalid:{exc.code}")
        except rc.RoutingUnreadable:
            return _refusal("routing_config_invalid:unreadable")
        if config is None:
            if picked is not None and (picked["model"] is not None or engine is None):  # nothing asked for is dropped
                return _refusal("hand_pick_without_routing")
            return {}
        detection = rd.detect(config, route_refusal=rd.daemon_route_refusal(None), active=engines)
        decision = router.decide(config, detection, {} if picked is None else {"hand_pick": picked})
        if not decision["ok"]:
            reason = decision.get("reason")
            return _refusal(decision["code"] if reason is None else f'{decision["code"]}:{reason}')
        record = router.route_decision(decision)  # first: route_entry answers None whenever this does
        if record is None:  # never for decide's own routed answers: no start launches half told
            raise RuntimeError("routing chose a route it cannot describe")
        entry = route_entry(config, decision)
        if entry is None:  # the chosen routes.json entry cannot be copied (a value nested too deep): unusable config
            return _refusal("routing_config_invalid:unreadable")
        return {"route_entry": entry, "route_decision": record}

    return normalize


def register(hooks) -> None:
    hooks.start_registrant(NAME, FIELDS, KEYS, make_normalize(hooks), chooses_engine=True)
