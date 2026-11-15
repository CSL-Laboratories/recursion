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
read: one malformed entry makes the whole snapshot unusable, and every task is refused. The refusal's
``code`` still names what was refused (``hand_pick_engine_unavailable``, ``rule_engine_unavailable`` or
``default_engine_unavailable``); its ``reason`` is ``detection_malformed`` (a missing snapshot:
``detection_unavailable``). An engine the snapshot marks routable but ``routes.json`` does not configure is
``route_not_configured``; a route whose model is named neither by the routing file nor by ``routes.json``
is ``route_no_model``, because the route service refuses a route without a model. ``detect.detect`` builds
the snapshot from the loaded routes, the daemon's own refusals, the breaker and the key store.

Two more answers read the same inputs: ``explain`` (the decision, and every rule's and the default's part
in it, for ``routing show``), with ``explain_answer`` (QUOTA's ``route.explain`` answer, in its published
shape only), and ``next_after_limit`` (after a limit on the decided route: the next route the user listed
for it, or ask; never anything the user did not list).

The task carries ``task_kind`` (a string), ``tags`` (a list of strings) and ``hand_pick``
(``{engine, model}``, model null for the one routes.json configures), the words of the control schema's
``route.explain`` params; any other key is ignored. A decision reads nothing else: no clock, no file,
no earlier result. A limit or an error an engine returned can never change the next decision.
"""

from __future__ import annotations

import copy
import math
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
    if not isinstance(detection, Mapping):
        return None, "detection_unavailable"
    out: dict[str, dict] = {}
    try:
        if "engines" not in detection:
            return None, "detection_unavailable"
        engines = detection["engines"]
        if not isinstance(engines, Mapping):  # the except below gives the same answer; this says it in words
            return None, "detection_malformed"
        for eng, entry in engines.items():
            if not _entry_ok(eng, entry):
                return None, "detection_malformed"
            out[eng] = {"routable": entry["routable"], "reason": entry.get("reason")}
    except Exception:  # noqa: BLE001 - a snapshot that cannot be read is malformed, never partly used
        return None, "detection_malformed"
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
    """The task read once: ``(task_kind, tags, hand_pick)``, or ``_Refuse`` with a code-word reason.

    A ``task_kind`` or ``tags`` that is present must be a non-empty string or a list of them: a present
    null is refused, not read as absent. A ``hand_pick`` that is present has exactly ``engine`` and ``model``
    (common.v1, an object: a present null is refused as ``explain_answer`` refuses it), the model a model id
    or null. A task, or a hand pick, that cannot be read is refused, never raised through.
    """
    if not isinstance(task, Mapping):
        raise _Refuse("bad_task", "task")
    try:
        present = {name: name in task for name in ("task_kind", "tags", "hand_pick")}
        kind, tags, hand = task.get("task_kind"), task.get("tags"), task.get("hand_pick")
    except Exception:  # noqa: BLE001 - a task that cannot be read is refused, never raised through
        raise _Refuse("bad_task", "task") from None
    if present["task_kind"] and not (isinstance(kind, str) and kind):
        raise _Refuse("bad_task", "task_kind")
    if present["tags"] and not (isinstance(tags, list) and all(isinstance(t, str) and t for t in tags)):
        raise _Refuse("bad_task", "tags")
    if not present["hand_pick"]:
        return kind, frozenset(tags or ()), None
    try:
        engine, model = hand.get("engine"), hand.get("model")
        shaped = set(hand) == {"engine", "model"} and isinstance(engine, str) and engine in ENGINES
    except Exception:  # noqa: BLE001 - null, a string or a hand pick that cannot be read: not a hand pick
        shaped = False
    if not shaped:
        raise _Refuse("bad_hand_pick", "hand_pick")
    if model is not None and not _model_ok(model):
        raise _Refuse("bad_hand_pick", "hand_pick_model")
    return kind, frozenset(tags or ()), {"engine": engine, "model": model}


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
    configures for the engine when the route names none; a route with neither is not routable
    (``route_no_model``), so a routed decision always names a model.
    """
    try:
        kind, tags, hp = _task(task)
    except _Refuse as bad:
        return _bad_task(bad)
    return _decide(config, detection, kind, tags, hp)


def _bad_task(bad: _Refuse) -> dict:
    return {"ok": False, "code": bad.code, "reason": bad.reason, "engine": None, "rule": None,
            "candidates": [], "hand_pick": None}


def _decide(config: Config, detection: object, kind: str | None, tags: frozenset, hp: dict | None) -> dict:
    """``decide`` on a task already read by ``_task``."""
    entries, err = snapshot(detection)
    candidates: list[dict] = []

    def model_of(model: str | None, engine: str) -> str | None:
        return model if model is not None else _configured_model(config, engine)

    def why_not(engine: str, model: str | None) -> str | None:
        why = _state(config, entries, err, engine)
        return "route_no_model" if why is None and model is None else why

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
        why = why_not(eng, model)
        if why is not None:
            # The hand pick wins: it is honoured or refused, never replaced.
            candidates.append({"source": "hand_pick", "engine": eng, "model": model, "skipped": why})
            return refused("hand_pick_engine_unavailable", why, eng, None)
        return chosen("hand_pick", None, "hand_pick", eng, model)

    for rule in config.rules:
        if not _matches(rule, kind, tags):
            continue
        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)
        why = why_not(eng, model)
        if why is None:
            return chosen("config", rule.id, f"rule:{rule.id}", eng, model)
        candidates.append({"source": f"rule:{rule.id}", "engine": eng, "model": model, "skipped": why})
        if config.undetected_rule == "refuse":
            return refused("rule_engine_unavailable", why, eng, rule.id)

    eng, model = config.default.engine, model_of(config.default.model, config.default.engine)
    why = why_not(eng, model)
    if why is None:
        return chosen("default", None, "default", eng, model)
    candidates.append({"source": "default", "engine": eng, "model": model, "skipped": why})
    return refused("default_engine_unavailable", why, eng, None)


def route_decision(decision: Mapping) -> dict | None:
    """The control schema's ``route_decision`` (common.v1: chosen, reason, candidates, hand_pick) for a routed
    decision, or None for a refusal. This is what the run record keeps and the ``route.decision`` event
    carries; the start path that stores it is a later slice. Anything else (an object that is not a routed
    decision, one whose ``ok`` is not exactly True, one whose members are missing or of a type ``decide`` never
    gives, a subclass of ``str`` among them, or one that cannot be read) answers None, never an ``Exception``; a
    ``BaseException`` such as ``KeyboardInterrupt`` stops the caller."""
    try:
        if not isinstance(decision, Mapping) or decision.get("ok") is not True:
            return None
        if any(key not in decision for key in ("reason", "engine", "model", "connection", "candidates", "hand_pick")):
            return None  # carries ok without decide's members: not a routed decision
        reason, engine, model = decision["reason"], decision["engine"], decision["model"]
        connection, candidates, hand = decision["connection"], decision["candidates"], decision["hand_pick"]
        if type(reason) is not str or reason not in REASONS or type(engine) is not str or type(model) is not str:
            return None  # members of a type decide never gives: not a routed decision
        # decide's connection always names its kind and id, plain str; next_after_limit refuses one without them.
        if (not isinstance(connection, Mapping) or type(connection.get("kind")) is not str
                or type(connection.get("id")) is not str):
            return None  # a connection that is not a mapping (a list of pairs would pass dict())
        if not isinstance(candidates, (list, tuple)):
            return None  # candidates that are not a list (a generator would be used up and read as [])
        if not all(isinstance(c, Mapping) for c in candidates):
            return None  # a candidate that is not a mapping
        if not (hand is None or isinstance(hand, Mapping)):
            return None  # a hand pick that is neither null nor a mapping (a list of pairs would pass dict())
        return {
            "chosen": {"engine": engine, "model": model, "connection": dict(connection)},
            "reason": reason,
            "candidates": [{"engine": c["engine"], "model": c["model"], "skipped": c["skipped"]} for c in candidates],
            "hand_pick": None if hand is None else dict(hand),
        }
    except Exception:  # noqa: BLE001 - a decision that cannot be read is not a routed decision
        return None


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
    the task is read once, and the rows and the decision are both made from that one reading.
    """
    try:
        kind, tags, hp = _task(task)
    except _Refuse as bad:
        kind, tags, decision = None, frozenset(), _bad_task(bad)
    else:
        decision = _decide(config, detection, kind, tags, hp)
    model_of = {c["source"]: c["model"] for c in decision["candidates"]}
    skipped_of = {c["source"]: c["skipped"] for c in decision["candidates"]}
    hand = decision["hand_pick"] is not None
    bad_task = decision.get("code") in ("bad_task", "bad_hand_pick")
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


# The members of route.explain's params (methods.v1 params.route.explain, #368 at 16dea486).
EXPLAIN_PARAMS = ("session_id", "engine", "hand_pick", "task_kind", "tags")


def explain_answer(config: Config, detection: object, params: object) -> dict:
    """``route.explain``'s answer in its published shape and nothing more (methods.v1 ``answer.route.explain``,
    #368 at 16dea486): ``{"code": "ok", "decision": <route_decision>}`` for a routed task, else
    ``{"code": "request_invalid", "detail": ...}``. The detail is routing's own code, with ``:`` and the reason
    when there is one, as the Liaison ruled for routing's refusals at 3:11 PM, Nov 14, until SPINE's schema
    revision gives them a code of their own. ``explain()``'s rule-by-rule outcomes are not returned (the
    Liaison's ruling of 4:20 PM): they wait for that same revision.

    The params' ``task_kind``, ``tags`` and ``hand_pick`` are the task; ``session_id`` is the caller's to
    resolve and is not read here. A member the method does not have is refused with its name as the detail
    (``params`` when the name is not a code word); params that cannot be read are refused ``params``, and a
    hand pick that cannot be read ``hand_pick``, never raised through.
    ``hand_pick`` carries both ``engine`` and ``model`` (common.v1). An ``engine`` with a ``hand_pick`` of
    another engine is refused, ``engine_and_hand_pick_differ`` (the Liaison's ruling of 4:20 PM); with a hand
    pick of the same engine, the hand pick decides. An ``engine`` alone is refused, ``engine_named_not_routed``:
    a start that names its engine is not routed (it launches that engine's ``routes.json`` entry), so there
    is no routing decision to explain (questions.md Q16).
    """
    if not isinstance(params, Mapping):
        return {"code": "request_invalid", "detail": "params"}
    try:
        names = list(params)
        given = {name: params[name] for name in EXPLAIN_PARAMS if name in params}
    except Exception:  # noqa: BLE001 - params that cannot be read are refused, never raised through
        return {"code": "request_invalid", "detail": "params"}
    for name in names:
        if name not in EXPLAIN_PARAMS:
            return {"code": "request_invalid", "detail": name if isinstance(name, str) and _CODE.fullmatch(name) else "params"}
    hand = given.get("hand_pick")
    try:
        hand_engine = hand["engine"] if isinstance(hand, Mapping) and set(hand) == {"engine", "model"} else None
    except Exception:  # noqa: BLE001 - a hand pick that cannot be read is not a hand pick
        hand_engine = None
    if "hand_pick" in given and hand_engine is None:
        return {"code": "request_invalid", "detail": "hand_pick"}
    if "engine" in given:
        if hand is None:
            return {"code": "request_invalid", "detail": "engine_named_not_routed"}
        if hand_engine != given["engine"]:
            return {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"}
    task = {key: given[key] for key in ("task_kind", "tags", "hand_pick") if key in given}
    decision = decide(config, detection, task)
    if not decision["ok"]:
        detail = decision["code"] if decision.get("reason") is None else f'{decision["code"]}:{decision["reason"]}'
        return {"code": "request_invalid", "detail": detail}
    return {"code": "ok", "decision": route_decision(decision)}


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


_MISSING = object()   # a member the caller's object does not have
_ANY = object()       # an attempt that names only its connection: any route on it was tried


def _is_time(value: object) -> bool:
    """A number of seconds a wait can end at: a plain int or float (never a bool or another subclass, whose
    conversion could raise) whose value is a finite float. NaN and the infinities are not (``contract.vendor_reset``
    refuses them too and strict JSON cannot carry them), nor is an int too large for a float. Never raises."""
    if type(value) is not int and type(value) is not float:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float: no finite time
        return False


def _plain(value: object) -> object:
    """A str as its plain text (``str.__str__`` copies a subclass without calling any method of it), so that
    comparing or formatting it later cannot raise; any other value as it is."""
    return str.__str__(value) if issubclass(type(value), str) else value


def _connection_word(value: object) -> object:
    """An attempt's connection as ``routes.limits.describe`` writes it, ``kind:id``; a ``{kind, id}`` mapping is
    read as the same word. A str is read as its plain text."""
    if isinstance(value, Mapping):
        return f"{_plain(value.get('kind'))}:{_plain(value.get('id'))}"
    return _plain(value)


def _attempt(entry: object) -> tuple | None:
    """One ``limit_notice.attempts`` entry, read once: ``(engine, model, connection word)``. An entry as
    ``describe`` writes it is a mapping. An entry that is a bare ``kind:id`` word, or a mapping that names its
    connection and neither an engine nor a model, names only its connection, and every listed route on that
    connection counts as tried. So a notice built by hand, in any of those shapes, cannot hide a route
    already tried. Each word is read as its plain text, a subclass of ``str`` too, so that comparing it cannot
    raise and it still names the route it spells. An entry whose parts are not words names no listed route,
    and is not an attempt; nor is any other entry. (An entry that cannot be read at all is dropped by the
    caller, like these.)"""
    if isinstance(entry, Mapping):
        engine, model = _plain(entry.get("engine")), _plain(entry.get("model"))
        word = _connection_word(entry.get("connection"))
        if not isinstance(word, str) or not all(part is None or isinstance(part, str) for part in (engine, model)):
            return None  # parts that are not words: compared later, they could name nothing (or raise)
        if engine is None and model is None:
            return (_ANY, _ANY, word)  # names only its connection, as a bare word does
        return (engine, model, word)
    if isinstance(entry, str):
        return (_ANY, _ANY, _plain(entry))
    return None


def _same_route(route: Mapping, seen: tuple) -> bool:
    """``seen`` is an attempt read once: ``(engine, model, connection word)``, ``_ANY`` matching anything."""
    mine = (route["engine"], route["model"], contract.connection_key(route))
    return all(theirs is _ANY or ours == theirs for ours, theirs in zip(mine, seen))


def next_after_limit(config: Config, decision: Mapping, limit: Mapping, detection: object = None) -> dict:
    """What a limit on a decided route leads to: the next route the user listed, or ask; never anything else.

    Four arguments (the brief writes two; the limits are read from the config, so it is passed too):
    ``config``, the Config the decision was made with (for a run in flight, that one, not a reloaded file);
    ``decision``, ``decide``'s own answer (a ``route_decision``, anything else without ``ok``, an ``ok`` that is
    neither True nor False, a routed mapping without decide's ``reason``, ``rule``, ``engine``, ``model`` and
    ``connection``, one whose words are not decide's own plain ``str`` (a subclass of ``str`` is not), or one
    that cannot be read answers ``no_decision``; a refusal answers ``no_route``); ``limit``, the route result that ended the run (its ``code`` and, when the route
    service made one, its ``limit_notice`` with the ``attempts`` already made); ``detection``, optionally a
    snapshot. ``wait`` waits until the limit's ``reset_at``, or the result's ``retry_at`` when ``reset_at`` is
    absent, null or not a finite time, else null; ``routes.limits.notice`` keeps the raw value. The answer::

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
    # The decision, read once inside a guard into plain data: nothing below reads the caller's object again.
    # A member it does not have reads as _MISSING; the presence check and the type check each answer for
    # their own rule, so each one is what refuses the decision it is about.
    try:
        if not isinstance(decision, Mapping) or "ok" not in decision:
            return {"action": "none", "reason": "no_decision"}
        ok = decision["ok"]
        if ok is False:
            return {"action": "none", "reason": "no_route"}
        connection = decision.get("connection")
        if (ok is not True or any(key not in decision for key in ("reason", "rule", "engine", "model"))
                or not isinstance(connection, Mapping) or not {"kind", "id"} <= set(connection)):
            return {"action": "none", "reason": "no_decision"}  # routed in name only: not decide's answer
        read = {key: decision[key] if key in decision else _MISSING for key in ("engine", "model", "reason", "rule")}
        conn = ({key: connection[key] if key in connection else _MISSING for key in ("kind", "id")}
                if isinstance(connection, Mapping) else {"kind": _MISSING, "id": _MISSING})
        # The values compared or formatted below must be decide's own words, plain str (a subclass could raise, or
        # answer anything, when compared or formatted), checked here, where comparing one that raises is still
        # inside the guard.
        odd = ((read["reason"] is not _MISSING and (type(read["reason"]) is not str or read["reason"] not in REASONS))
               or (read["rule"] is not _MISSING and read["rule"] is not None and type(read["rule"]) is not str)
               or any(conn[key] is not _MISSING and type(conn[key]) is not str for key in ("kind", "id")))
    except Exception:  # noqa: BLE001 - a decision that cannot be read is not decide's answer
        return {"action": "none", "reason": "no_decision"}
    if odd:
        return {"action": "none", "reason": "no_decision"}  # a reason, rule or connection decide never gives
    if ((read["engine"] is not _MISSING and type(read["engine"]) is not str)
            or (read["model"] is not _MISSING and read["model"] is not None and type(read["model"]) is not str)):
        return {"action": "none", "reason": "no_decision"}  # an engine or model decide never gives
    decision = {**read, "connection": conn}
    # The limit result, read once: its code, the vendor's signal, the route service's notice and retry time.
    try:
        if isinstance(limit, Mapping):
            code, signal, notice, retry_at = (limit.get("code"), limit.get("limit"), limit.get("limit_notice"),
                                              limit.get("retry_at"))
        else:
            code = signal = notice = retry_at = None
        reset_at = signal.get("reset_at") if isinstance(signal, Mapping) else None
        tried = notice.get("attempts") if isinstance(notice, Mapping) else None
        if isinstance(tried, str):
            tried = [tried]  # one bare kind:id word, never its characters
        attempts = []
        for entry in (tried if isinstance(tried, (list, tuple)) else ()):
            try:
                seen = _attempt(entry)
            except Exception:  # noqa: BLE001 - an entry that cannot be read is not an attempt, as other non-attempts
                seen = None
            if seen is not None:
                attempts.append(seen)
    except Exception:  # noqa: BLE001 - a limit result that cannot be read is not a limit: no blind retry
        return {"action": "none", "reason": "not_a_limit"}
    if type(code) is not str or code not in route_limits.LIMIT_CODES:  # the route service's own code, plain str
        return {"action": "none", "reason": "not_a_limit"}
    limits, source = _route_limits(config, decision)
    choices = [route_limits.describe(r) for r in limits["fallback"]]
    if decision["reason"] == "hand_pick":
        return {"action": "ask", "reason": "hand_pick", "choices": choices}
    if limits["on_limit"] == "ask":
        return {"action": "ask", "reason": "on_limit_ask", "choices": choices}
    if limits["on_limit"] == "wait":
        until = reset_at
        if not _is_time(until):  # absent, null or not a time: the route service's retry time, if it is one
            until = retry_at if _is_time(retry_at) else None
        return {"action": "wait", "until": until, "choices": choices}
    attempts.append((decision["engine"], decision["model"],
                     f"{decision['connection']['kind']}:{decision['connection']['id']}"))
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
