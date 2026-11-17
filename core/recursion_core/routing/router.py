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
routable but ``routes.json`` does not configure is ``route_not_configured``. (This slice takes the
snapshot as an argument; ``detect.py``, which builds it from the loaded routes, the breaker and the
key store, is the next slice.)

The task carries ``task_kind`` (a string), ``tags`` (a list of strings) and ``hand_pick``
(``{engine, model}``, model null for the one routes.json configures), the words of the control schema's
``route.explain`` params; any other key is ignored. A decision reads nothing else: no clock, no file,
no earlier result. A limit or an error an engine returned can never change the next decision.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

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
