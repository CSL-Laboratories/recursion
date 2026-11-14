"""The routing decision: ``decide(config, detection, task)``, a pure function with no history input.

The order (grill Q45, ported from the vault prototype 20261107-csl729-routing-config-q45-01, section 3):

1. A hand pick on the task wins over every rule and the default. If its engine is not routable the
   task is REFUSED with the reason; it is never sent anywhere else.
2. Otherwise the first rule that matches the task and whose engine is routable. A matching rule whose
   engine is not routable is listed as a skipped candidate with its reason, and the next rule is
   tried (``undetected_rule: skip``), or the task is refused (``undetected_rule: refuse``).
3. Otherwise the default. If its engine is not routable the task is refused; no other engine is
   ever picked in its place.

``detection`` is plain data: ``{"engines": {"<engine>": {"routable": bool, "reason": <code or null>}}}``.
An engine absent from it is ``not_detected``. The snapshot is validated as a whole before any entry is
read: one malformed entry makes the whole snapshot unusable, and every task is refused with
``detection_malformed`` (a missing one: ``detection_unavailable``). An engine the snapshot marks
routable but ``routes.json`` does not configure is ``route_not_configured``. ``detect.detect`` builds
the snapshot from the loaded routes, the daemon's own refusals, the breaker and the key store.

Two more answers read the same inputs: ``explain`` (the decision, and every rule's and the default's part
in it, for ``routing show`` and QUOTA's ``route.explain``) and ``next_after_limit`` (after a limit on the
decided route: the next route the user listed for it, or ask; never anything the user did not list).

The task carries ``task_kind`` (a string), ``tags`` (a list of strings) and ``hand_pick``
(``{engine, model}``, model null for the one routes.json configures), the words of the control schema's
``route.explain`` params; any other key is ignored. A decision reads nothing else: no clock, no file,
no earlier result. A limit or an error an engine returned can never change the next decision.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from recursion_core.routes import contract
from recursion_core.routes import limits as route_limits
from recursion_core.routing.config import ENGINES, Config, _model_ok

REASONS = ("hand_pick", "config", "default")   # common.v1 route_decision.reason
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")     # common.v1 code_word


class _Refuse(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code, self.reason = code, reason


def _entry_ok(engine: object, entry: object) -> bool:
    if not isinstance(engine, str) or engine not in ENGINES or not isinstance(entry, Mapping):
        return False
    routable, reason = entry.get("routable"), entry.get("reason")
    if not isinstance(routable, bool):
        return False
    if routable:
        return reason is None
    return isinstance(reason, str) and _CODE.fullmatch(reason) is not None


def snapshot(detection: object) -> tuple[dict | None, str | None]:
    """``(engine -> {"routable", "reason"}, None)`` for a well-formed snapshot, or ``(None, code)``.

    Well formed: a mapping whose ``engines`` is a mapping from known engine ids to entries, each with a
    boolean ``routable``, a ``reason`` that is null (or absent) exactly when the entry is routable and
    otherwise a typed code. Other keys are allowed and ignored.
    """
    if not isinstance(detection, Mapping) or "engines" not in detection:
        return None, "detection_unavailable"
    engines = detection["engines"]
    if not isinstance(engines, Mapping):
        return None, "detection_malformed"
    out: dict[str, dict] = {}
    for eng, entry in engines.items():
        if not _entry_ok(eng, entry):
            return None, "detection_malformed"
        out[eng] = {"routable": entry["routable"], "reason": entry.get("reason")}
    return out, None


def _state(config: Config, entries: dict | None, err: str | None, engine: str) -> str | None:
    """None when ``engine`` is routable, else the typed reason it is not."""
    if entries is None:
        return err
    entry = entries.get(engine)
    if entry is None:
        return "not_detected"
    if not entry["routable"]:
        return entry["reason"]
    if engine not in config.routes:
        return "route_not_configured"
    return None


def _task(task: object) -> tuple[str | None, frozenset, dict | None]:
    if not isinstance(task, Mapping):
        raise _Refuse("bad_task", "task")
    kind = task.get("task_kind")
    if kind is not None and not (isinstance(kind, str) and kind):
        raise _Refuse("bad_task", "task_kind")
    tags = task.get("tags")
    if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) and t for t in tags)):
        raise _Refuse("bad_task", "tags")
    hand = task.get("hand_pick")
    if hand is None:
        return kind, frozenset(tags or ()), None
    if (not isinstance(hand, Mapping) or set(hand) - {"engine", "model"} or not isinstance(hand.get("engine"), str)
            or hand["engine"] not in ENGINES):
        raise _Refuse("bad_hand_pick", "hand_pick must be {engine, model}")
    model = hand.get("model")
    if model is not None and not _model_ok(model):
        raise _Refuse("bad_hand_pick", "hand_pick.model")
    return kind, frozenset(tags or ()), {"engine": hand["engine"], "model": model}


def _matches(rule, kind: str | None, tags: frozenset) -> bool:
    if rule.task_kind is not None and kind not in rule.task_kind:
        return False
    if rule.tags_all is not None and not rule.tags_all <= tags:
        return False
    return True


def _configured_model(config: Config, engine: str) -> str | None:
    entry = config.routes.get(engine)
    route = entry.get("route") if isinstance(entry, Mapping) else None
    model = route.get("model") if isinstance(route, Mapping) else None
    return model if isinstance(model, str) and model else None


def decide(config: Config, detection: object, task: object) -> dict:
    """One decision for one task. Pure: the same three inputs always give the same answer.

    Routed::

        {"ok": True, "reason": "hand_pick" | "config" | "default", "rule": <rule id or None>,
         "engine", "model", "login", "vendor", "connection": {"kind", "id"},
         "candidates": [{"source", "engine", "model", "skipped": None | <code>}], "hand_pick"}

    Refused (nothing may be launched)::

        {"ok": False, "code", "reason", "engine", "rule", "candidates", "hand_pick"}

    ``candidates`` lists every route that was considered, in order: a matching rule passed over, then
    the one chosen (``skipped: None``). ``model`` is the route's own model, or the one routes.json
    configures for the engine when the route names none.
    """
    entries, err = snapshot(detection)
    candidates: list[dict] = []
    try:
        kind, tags, hp = _task(task)
    except _Refuse as bad:
        return {"ok": False, "code": bad.code, "reason": bad.reason, "engine": None, "rule": None,
                "candidates": candidates, "hand_pick": None}

    def model_of(model: str | None, engine: str) -> str | None:
        return model if model is not None else _configured_model(config, engine)

    def chosen(reason: str, rule: str | None, source: str, engine: str, model: str | None) -> dict:
        lg = config.logins[config.engine_login[engine]]
        candidates.append({"source": source, "engine": engine, "model": model, "skipped": None})
        return {"ok": True, "reason": reason, "rule": rule, "engine": engine, "model": model, "login": lg.id,
                "vendor": lg.vendor, "connection": {"kind": lg.kind, "id": lg.connection_id},
                "candidates": candidates, "hand_pick": hp}

    def refused(code: str, why: str, engine: str, rule: str | None) -> dict:
        return {"ok": False, "code": code, "reason": why, "engine": engine, "rule": rule,
                "candidates": candidates, "hand_pick": hp}

    if hp is not None:
        eng, model = hp["engine"], model_of(hp["model"], hp["engine"])
        if eng not in config.engine_login:
            candidates.append({"source": "hand_pick", "engine": eng, "model": model, "skipped": "no_login"})
            return refused("hand_pick_engine_without_login", "no_login", eng, None)
        why = _state(config, entries, err, eng)
        if why is not None:
            # The hand pick wins: it is honoured or refused, never replaced.
            candidates.append({"source": "hand_pick", "engine": eng, "model": model, "skipped": why})
            return refused("hand_pick_engine_unavailable", why, eng, None)
        return chosen("hand_pick", None, "hand_pick", eng, model)

    for rule in config.rules:
        if not _matches(rule, kind, tags):
            continue
        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)
        why = _state(config, entries, err, eng)
        if why is None:
            return chosen("config", rule.id, f"rule:{rule.id}", eng, model)
        candidates.append({"source": f"rule:{rule.id}", "engine": eng, "model": model, "skipped": why})
        if config.undetected_rule == "refuse":
            return refused("rule_engine_unavailable", why, eng, rule.id)

    eng, model = config.default.engine, model_of(config.default.model, config.default.engine)
    why = _state(config, entries, err, eng)
    if why is None:
        return chosen("default", None, "default", eng, model)
    candidates.append({"source": "default", "engine": eng, "model": model, "skipped": why})
    return refused("default_engine_unavailable", why, eng, None)


def route_decision(decision: Mapping) -> dict | None:
    """The control schema's ``route_decision`` (common.v1: chosen, reason, candidates, hand_pick) for a routed
    decision, or None for a refusal. This is what the run record keeps and the ``route.decision`` event
    carries; the start path that stores it is a later slice."""
    if not decision.get("ok"):
        return None
    return {
        "chosen": {"engine": decision["engine"], "model": decision["model"],
                   "connection": dict(decision["connection"])},
        "reason": decision["reason"],
        "candidates": [{"engine": c["engine"], "model": c["model"], "skipped": c["skipped"]}
                       for c in decision["candidates"]],
        "hand_pick": None if decision["hand_pick"] is None else dict(decision["hand_pick"]),
    }


# -- explain: the decision and every rule's part in it ------------------------------------------------------
def explain(config: Config, detection: object, task: object) -> dict:
    """The decision for ``task`` and why: every rule and the default, each with its outcome and reason.

    ``{"decision": decide(...), "route_decision": route_decision(...) or None, "hand_pick": ...,
    "rules": [{"id", "engine", "model", "outcome", "reason"}], "default": {"engine", "model", "outcome", "reason"}}``

    Outcomes: ``chosen``; ``skipped`` (it matched, its engine is not routable, the next was tried; the
    reason is detection's); ``refused`` (it matched, its engine is not routable, and the config refuses);
    ``no_match`` (the reason names the condition that failed: ``task_kind`` or ``tags_all``);
    ``not_reached`` (a route was chosen or refused before it, or the task itself was refused); ``hand_pick``
    (the task carries a hand pick, so no rule and no default is considered). The decision is ``decide``'s:
    this reads it and never decides again in another way.
    """
    decision = decide(config, detection, task)
    model_of = {c["source"]: c["model"] for c in decision["candidates"]}
    skipped_of = {c["source"]: c["skipped"] for c in decision["candidates"]}
    hand = decision["hand_pick"] is not None
    bad_task = decision.get("code") in ("bad_task", "bad_hand_pick")
    try:
        kind, tags, _ = _task(task)
    except _Refuse:
        kind, tags = None, frozenset()
    done = hand or bad_task
    rules = []
    for rule in config.rules:
        source = f"rule:{rule.id}"
        configured = rule.route.model if rule.route.model is not None else _configured_model(config, rule.route.engine)
        row = {"id": rule.id, "engine": rule.route.engine, "model": model_of.get(source, configured)}
        if hand:
            row.update(outcome="hand_pick", reason=None)
        elif bad_task:
            row.update(outcome="not_reached", reason=decision["code"])
        elif rule.task_kind is not None and kind not in rule.task_kind:
            row.update(outcome="no_match", reason="task_kind")
        elif rule.tags_all is not None and not rule.tags_all <= tags:
            row.update(outcome="no_match", reason="tags_all")
        elif done:
            row.update(outcome="not_reached", reason=None)
        elif source in skipped_of and skipped_of[source] is None:
            row.update(outcome="chosen", reason=None)
            done = True
        elif not decision["ok"] and decision.get("rule") == rule.id:
            row.update(outcome="refused", reason=skipped_of.get(source))
            done = True
        else:
            row.update(outcome="skipped", reason=skipped_of.get(source))
        rules.append(row)
    configured = (config.default.model if config.default.model is not None
                  else _configured_model(config, config.default.engine))
    default = {"engine": config.default.engine, "model": model_of.get("default", configured)}
    if hand:
        default.update(outcome="hand_pick", reason=None)
    elif done:
        default.update(outcome="not_reached", reason=decision["code"] if bad_task else None)
    elif decision["ok"]:
        default.update(outcome="chosen", reason=None)
    else:
        default.update(outcome="refused", reason=skipped_of.get("default"))
    return {"decision": decision, "route_decision": route_decision(decision), "hand_pick": decision["hand_pick"],
            "rules": rules, "default": default}


# -- next_after_limit: what a limit on the decided route leads to ---------------------------------------------
def _route_limits(config: Config, decision: Mapping) -> tuple[dict, str]:
    """``(limits, source)`` for a routed decision: the routing file's own limits for the route it chose, else
    those of the ``routes.json`` entry the engine launches with, else the user's default (show and ask)."""
    route = None
    if decision["reason"] == "config":
        route = next((r.route for r in config.rules if r.id == decision["rule"]), None)
    elif decision["reason"] == "default":
        route = config.default
    if route is not None and route.limits is not None:
        return copy.deepcopy(dict(route.limits)), "routing"
    entry = config.routes.get(decision["engine"])
    raw = entry.get("route", {}).get("limits") if isinstance(entry, Mapping) else None
    if raw is not None:
        try:
            return route_limits.validate_limits(raw), "routes"
        except contract.RouteError:
            pass  # the route service refuses such a route before it runs; nothing is switched to
    return route_limits.validate_limits(route_limits.DEFAULT_LIMITS), "default"


def _same_route(route: Mapping, seen: Mapping) -> bool:
    mine = (route["engine"], route["model"], contract.connection_key(route))
    return mine == (seen.get("engine"), seen.get("model"), seen.get("connection"))


def next_after_limit(config: Config, decision: Mapping, limit: Mapping, detection: object = None) -> dict:
    """What a limit on a decided route leads to: the next route the user listed, or ask; never anything else.

    ``limit`` is the route result that ended the run (its ``code`` and, when the route service made one, its
    ``limit_notice`` with the ``attempts`` already made). The answer::

        {"action": "none", "reason"}                    not a limit (no blind retry), or no route was decided
        {"action": "ask", "reason", "choices"}          show the limit and let the user choose
        {"action": "wait", "until", "choices"}          the user's on_limit is wait
        {"action": "switch", "place", "route", "source", "skipped", "choices"}   the next listed route

    A switch is only ever to a route in the user's own fallback list for the decided route (the loader has
    already refused a list that names a login not configured for its engine), in the user's order, passing
    over a route already tried and, when ``detection`` is given, one whose engine is not routable. The
    switch is a new run that the caller (SESSIONS) starts; this function starts nothing. A hand pick is
    never switched away from on its own: the user chose that route for this task, so the answer is ask.
    """
    if not decision.get("ok"):
        return {"action": "none", "reason": "no_route"}
    code = limit.get("code") if isinstance(limit, Mapping) else None
    if code not in route_limits.LIMIT_CODES:
        return {"action": "none", "reason": "not_a_limit"}
    limits, source = _route_limits(config, decision)
    choices = [route_limits.describe(r) for r in limits["fallback"]]
    if decision["reason"] == "hand_pick":
        return {"action": "ask", "reason": "hand_pick", "choices": choices}
    if limits["on_limit"] == "ask":
        return {"action": "ask", "reason": "on_limit_ask", "choices": choices}
    if limits["on_limit"] == "wait":
        signal = limit.get("limit") if isinstance(limit.get("limit"), Mapping) else {}
        return {"action": "wait", "until": signal.get("reset_at", limit.get("retry_at")), "choices": choices}
    notice = limit.get("limit_notice") if isinstance(limit.get("limit_notice"), Mapping) else {}
    attempts = [a for a in notice.get("attempts", ()) if isinstance(a, Mapping)]
    attempts.append({"engine": decision["engine"], "model": decision["model"],
                     "connection": f"{decision['connection']['kind']}:{decision['connection']['id']}"})
    entries, err = snapshot(detection) if detection is not None else (None, None)
    skipped = []
    for place, route in enumerate(limits["fallback"], 1):
        if any(_same_route(route, seen) for seen in attempts):
            skipped.append({"place": place, **route_limits.describe(route), "skipped": "already_tried"})
            continue
        if detection is not None:
            why = _state(config, entries, err, route["engine"])
            if why is not None:
                skipped.append({"place": place, **route_limits.describe(route), "skipped": why})
                continue
        return {"action": "switch", "place": place, "route": copy.deepcopy(route), "source": source,
                "skipped": skipped, "choices": choices}
    return {"action": "ask", "reason": "fallback_exhausted", "skipped": skipped, "choices": choices}
