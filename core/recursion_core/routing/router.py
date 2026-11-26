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
for it, or ask; never anything the user did not list), with ``next_steps`` (the words a person reads for each
of its answers: what stopped the walk and what the person can do).

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
from datetime import datetime, timezone

from recursion_core.routes import contract
from recursion_core.routes import limits as route_limits
from recursion_core.routing.config import ENGINES, Config, _model_ok
from recursion_core.routing.detect import NOT_CONSULTED, _code

REASONS = ("hand_pick", "config", "default")   # common.v1 route_decision.reason
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")     # common.v1 code_word


class _Refuse(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code, self.reason = code, reason


def _entry_ok(engine: object, entry: object) -> bool:
    """One detection entry, already read by ``snapshot`` into ``{"routable", "reason"}``, its words plain text."""
    if type(engine) is not str or engine not in ENGINES or not isinstance(entry, Mapping):
        return False
    routable, reason = entry.get("routable"), entry.get("reason")
    if not isinstance(routable, bool):
        return False
    if routable:
        return reason is None
    return type(reason) is str and _CODE.fullmatch(reason) is not None


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
            # Each entry read once, its engine and reason as plain text: a subclass of str could raise, or answer
            # anything, when it is hashed, compared or formatted later, outside this guard.
            eng = _plain(eng)
            if isinstance(entry, Mapping):
                entry = {"routable": entry.get("routable"), "reason": _plain(entry.get("reason"))}
            if not _entry_ok(eng, entry):
                return None, "detection_malformed"
            if eng in out:  # two keys that read as one engine: which entry is meant cannot be told
                return None, "detection_malformed"
            out[eng] = entry
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
    or null. A task, or a hand pick, that cannot be read is refused, never raised through. Every word is read
    as its plain text, a subclass of ``str`` too (its hash or its comparing could raise, or answer anything,
    later, outside a guard). A member that is not present is taken as absent, but it is still read (with ``get``,
    inside the same guard as the presence checks), so a task whose ``get`` of a member raises is refused
    ``bad_task:task`` even when that member is not there (tags that cannot be iterated are ``bad_task:tags``).
    """
    if not isinstance(task, Mapping):
        raise _Refuse("bad_task", "task")
    try:
        present = {name: name in task for name in ("task_kind", "tags", "hand_pick")}
        kind, tags, hand = task.get("task_kind"), task.get("tags"), task.get("hand_pick")
    except Exception:  # noqa: BLE001 - a task that cannot be read is refused, never raised through
        raise _Refuse("bad_task", "task") from None
    kind = _plain(kind) if present["task_kind"] else None
    if present["task_kind"] and not (type(kind) is str and kind):
        raise _Refuse("bad_task", "task_kind")
    words: list | None = []
    if present["tags"]:
        try:  # the list copied once, in a guard of its own: a list subclass's iterating could raise
            words = [_plain(t) for t in tags] if isinstance(tags, list) else None
        except Exception:  # noqa: BLE001 - tags that cannot be read are refused, never raised through
            words = None
        if words is None or not all(type(t) is str and t for t in words):
            raise _Refuse("bad_task", "tags")
    if not present["hand_pick"]:
        return kind, frozenset(words), None
    try:
        engine, model = _plain(hand.get("engine")), _plain(hand.get("model"))
        shaped = set(hand) == {"engine", "model"} and type(engine) is str and engine in ENGINES
    except Exception:  # noqa: BLE001 - null, a string or a hand pick that cannot be read: not a hand pick
        shaped = False
    if not shaped:
        raise _Refuse("bad_hand_pick", "hand_pick")
    if model is not None and not _model_ok(model):
        raise _Refuse("bad_hand_pick", "hand_pick_model")
    return kind, frozenset(words), {"engine": engine, "model": model}


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


def _members(value: Mapping, names: tuple) -> dict | None:
    """``value``'s members read into a plain dict when its keys are exactly ``names``, each a plain ``str``; else
    None. The keys are type-checked before they are compared, so a key's own comparing never runs."""
    keys = list(value)
    if len(keys) != len(names) or not all(type(key) is str for key in keys) or set(keys) != set(names):
        return None
    return {name: value[name] for name in names}


def _word_or_null(value: object) -> bool:
    return value is None or type(value) is str


def route_decision(decision: Mapping) -> dict | None:
    """The control schema's ``route_decision`` (common.v1: chosen, reason, candidates, hand_pick) for a routed
    decision, or None for a refusal. This is what the run record keeps and the ``route.decision`` event
    carries; nothing stores it yet. Anything else (an object that is not a routed
    decision, one whose ``ok`` is not exactly True, one whose members are missing or of a type ``decide`` never
    gives, a subclass of ``str`` among them, or one that cannot be read) answers None, never an ``Exception``; a
    ``BaseException`` such as ``KeyboardInterrupt`` stops the caller. Every member published is rebuilt from the
    values read here, each a plain ``str`` or, where ``decide`` gives null, null: a connection of exactly
    ``kind`` and ``id``, candidate rows of ``engine``, ``model`` and ``skipped``, and a hand pick of exactly
    ``engine`` and ``model``. So ``json.dumps`` takes every record this answers."""
    try:
        if not isinstance(decision, Mapping) or decision.get("ok") is not True:
            return None
        if any(key not in decision for key in ("reason", "engine", "model", "connection", "candidates", "hand_pick")):
            return None  # carries ok without decide's members: not a routed decision
        reason, engine, model = decision["reason"], decision["engine"], decision["model"]
        connection, candidates, hand = decision["connection"], decision["candidates"], decision["hand_pick"]
        if type(reason) is not str or reason not in REASONS or type(engine) is not str or type(model) is not str:
            return None  # members of a type decide never gives: not a routed decision
        # decide's connection names exactly its kind and id, plain str; next_after_limit refuses one without them.
        if not isinstance(connection, Mapping):
            return None  # not a mapping: one that only iterates and indexes like one is not decide's
        conn = _members(connection, ("kind", "id"))
        if conn is None or type(conn["kind"]) is not str or type(conn["id"]) is not str:
            return None  # a connection without its kind and id, with another member, or not plain words
        if not isinstance(candidates, (list, tuple)):
            return None  # candidates that are not a list (a generator would be used up and read as [])
        if not all(isinstance(c, Mapping) for c in candidates):
            return None  # a candidate that is not a mapping
        rows = [{"engine": c["engine"], "model": c["model"], "skipped": c["skipped"]} for c in candidates]
        if not all(type(r["engine"]) is str and _word_or_null(r["model"]) and _word_or_null(r["skipped"])
                   for r in rows):
            return None  # a candidate row whose words are not plain (decide gives a null model or skipped, never more)
        if not (hand is None or isinstance(hand, Mapping)):
            return None  # a hand pick that is neither null nor a mapping (nor one that only iterates and indexes)
        if hand is not None:
            hand = _members(hand, ("engine", "model"))
            if hand is None or type(hand["engine"]) is not str or not _word_or_null(hand["model"]):
                return None  # a hand pick without exactly its engine and model, or not plain words
        return {
            "chosen": {"engine": engine, "model": model, "connection": conn},
            "reason": reason,
            "candidates": rows,
            "hand_pick": hand,
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
    ``not_reached`` (a route was chosen or refused before it, whatever its own conditions, or the task itself
    was refused); ``hand_pick``
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
        elif done:  # the route was chosen or refused before this rule: routing never looked at its conditions
            row.update(outcome="not_reached", reason=None)
        elif rule.task_kind is not None and kind not in rule.task_kind:
            row.update(outcome="no_match", reason="task_kind")
        elif rule.tags_all is not None and not rule.tags_all <= tags:
            row.update(outcome="no_match", reason="tags_all")
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
    when there is one, until the control schema gives routing's refusals a code of their own.
    ``explain()``'s rule-by-rule outcomes are not returned: the published answer has no place for them until
    that schema revision.

    The params' ``task_kind``, ``tags`` and ``hand_pick`` are the task; ``session_id`` is the caller's to
    resolve and is not read here. A member the method does not have is refused with its name as the detail
    (``params`` when the name is not a code word); params that cannot be read are refused ``params``, and a
    hand pick that cannot be read ``hand_pick``, never raised through.
    ``hand_pick`` carries both ``engine`` and ``model`` (common.v1). An ``engine`` with a ``hand_pick`` of
    another engine is refused, ``engine_and_hand_pick_differ``; with a hand
    pick of the same engine, the hand pick decides. An ``engine`` alone is refused, ``engine_named_not_routed``:
    a start that names its engine is not routed (it launches that engine's ``routes.json`` entry), so there
    is no routing decision to explain.
    """
    if not isinstance(params, Mapping):
        return {"code": "request_invalid", "detail": "params"}
    try:
        names = [_plain(name) for name in params]  # as plain text: compared below, outside this guard
        given = {name: params[name] for name in EXPLAIN_PARAMS if name in params}
    except Exception:  # noqa: BLE001 - params that cannot be read are refused, never raised through
        return {"code": "request_invalid", "detail": "params"}
    for name in names:
        if type(name) is not str or name not in EXPLAIN_PARAMS:  # a name that is not a str is never compared
            return {"code": "request_invalid", "detail": name if type(name) is str and _CODE.fullmatch(name) else "params"}
    hand = given.get("hand_pick")
    try:
        hand_engine = _plain(hand["engine"]) if isinstance(hand, Mapping) and set(hand) == {"engine", "model"} else None
    except Exception:  # noqa: BLE001 - a hand pick that cannot be read is not a hand pick
        hand_engine = None
    if "hand_pick" in given and hand_engine is None:
        return {"code": "request_invalid", "detail": "hand_pick"}
    if "engine" in given:
        if hand is None:
            return {"code": "request_invalid", "detail": "engine_named_not_routed"}
        engine = _plain(given["engine"])
        if type(engine) is not str or type(hand_engine) is not str or hand_engine != engine:
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
    """A finite number of seconds the vendor named (a time already past included: this module has no clock, so
    it cannot tell): a plain int or float (never a bool or another subclass, whose conversion could raise) whose
    value is a finite float. NaN and the infinities are not (``contract.vendor_reset``
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
    connection counts as tried. A mapping that leaves out only its engine or only its model (absent or null)
    reads the part it leaves out as any: every listed route it could name on that connection counts as tried.
    So a notice built by hand, in any of those shapes, cannot hide a route already tried. Each word is read as its plain text, a subclass of ``str`` too, so that comparing it cannot
    raise and it still names the route it spells. An entry whose parts are not words names no listed route,
    and is not an attempt; nor is any other entry, nor one that cannot be read at all. ``next_after_limit``
    reads a history holding any entry that is not an attempt as unreadable: ask, never a switch."""
    if isinstance(entry, Mapping):
        engine, model = _plain(entry.get("engine")), _plain(entry.get("model"))
        word = _connection_word(entry.get("connection"))
        if not isinstance(word, str) or not all(part is None or isinstance(part, str) for part in (engine, model)):
            return None  # parts that are not words: compared later, they could name nothing (or raise)
        if engine is None and model is None:
            return (_ANY, _ANY, word)  # names only its connection, as a bare word does
        if engine is None:
            return (_ANY, model, word)  # a model on a connection: any engine that runs it there was tried
        if model is None:
            return (engine, _ANY, word)  # an engine on a connection: any model of it there was tried
        return (engine, model, word)
    if isinstance(entry, str):
        return (_ANY, _ANY, _plain(entry))
    return None


def _same_route(route: Mapping, seen: tuple) -> bool:
    """``seen`` is an attempt read once: ``(engine, model, connection word)``, ``_ANY`` matching anything."""
    mine = (route["engine"], route["model"], contract.connection_key(route))
    return all(theirs is _ANY or ours == theirs for ours, theirs in zip(mine, seen))


def next_after_limit(config: Config, decision: Mapping, limit: Mapping, detection: object, *,
                     route_refusal: object) -> dict:
    """What a limit on a decided route leads to: the next route the user listed, or ask; never anything else.

    Five arguments, none of them defaulted, so that no call leaves a check out by leaving an argument out (a call
    without ``detection`` or ``route_refusal`` raises ``TypeError`` naming it). The limits are read from the
    config, so the config is passed too:
    ``config``, the Config the decision was made with (for a run in flight, that one, not a reloaded file);
    ``decision``, ``decide``'s own answer (a ``route_decision``, anything else without ``ok``, an ``ok`` that is
    neither True nor False, a routed mapping without decide's ``reason``, ``rule``, ``engine``, ``model`` and
    ``connection``, one whose words are not decide's own plain ``str`` (a subclass of ``str`` is not), one
    whose ``model`` is null (``decide`` never routes without a model, and ``route_decision`` refuses it too), or one
    that cannot be read answers ``no_decision``; a refusal answers ``no_route``); ``limit``, the route result that ended the run (its ``code`` and, when the route
    service made one, its ``limit_notice`` with the ``attempts`` already made, a list, as ``routes.limits.notice``
    writes it: a notice that is absent or null means the route service made none, and only the decided route
    counts as tried; a notice that is there but is not a mapping or holds no list of attempts, a list that cannot
    be read, or an entry that is not an attempt is an unreadable history, which answers ``ask`` with the reason
    ``attempts_unreadable`` and never a switch, because a route already tried and limited could be offered again);
    ``detection``, the snapshot ``detect.detect`` gives: a listed route whose engine it does not call routable is
    passed over with its reason (``key_not_opted_in``, ``key_state_unknown``, ``route_not_configured`` and the
    others), and a snapshot that is null or cannot be read passes over every listed route
    (``detection_unavailable``, ``detection_malformed``), so a call that has no snapshot gets ask, never a switch
    to a route whose key state it has not seen; ``route_refusal``, keyword only, the daemon's own pre-launch
    refusal of a route entry (what ``detect.daemon_route_refusal(breaker)`` gives), or ``detect.NOT_CONSULTED``
    from a caller that has no daemon to ask (then no daemon refusal is read; the snapshot still is): a listed
    route it refuses, taken as the entry the daemon would launch, is passed over and skipped with its code
    (``launch_refused``, or ``route_unbindable``'s; an answer that is not a code word is ``route_refused``, as
    ``detect`` reads it), and one whose refusal raises is skipped ``detection_source_failed``. A
    ``route_refusal`` passed as None is never read as no daemon: every listed route not passed over before it
    is skipped ``source_not_given``, so the answer is ask. ``wait`` waits until the limit's ``reset_at``, or the result's ``retry_at`` when ``reset_at`` is
    absent, null or not a finite time, else null; ``routes.limits.notice`` keeps the raw value. A time already
    past is passed through as the vendor gave it (``contract.vendor_reset`` would make it None; this module has
    no clock), so a surface that waits clamps it to now and never sleeps a negative interval. The answer::

        {"action": "none", "reason"}                    not a limit (no blind retry), or no route was decided
        {"action": "ask", "reason", "choices"}          show the limit and let the user choose
        {"action": "ask", "reason", "skipped", "choices"}   no listed route can be switched to, or the history
                                                        of routes tried cannot be read
        {"action": "wait", "until", "choices"}          the user's on_limit is wait
        {"action": "switch", "place", "route", "source", "skipped", "choices"}   the next listed route

    A switch is only ever to a route in the user's own fallback list for the decided route (the loader has
    already refused a list that names a login not configured for its engine), in the user's order, passing
    over a route already tried, one whose engine the snapshot does not call routable and one the daemon's
    refusal names. The switch is a new run that the caller starts; this function starts nothing. A hand pick is
    never switched away from on its own: the user chose that route for this task, so the answer is ask.
    ``next_steps(answer)`` gives the words a person reads for any answer.
    """
    # The decision, read inside one guard into plain data (each member once for presence and once for its value):
    # nothing below reads the caller's object again, and the checks read these copies, never the object. A member
    # that goes between its presence and its value raises inside the guard (no_decision); it never reads as absent.
    # The connection's members are walked once, and only once it is known to be a mapping (one that is not is never
    # walked; one that cannot be walked is not decide's).
    # A member it does not have reads as _MISSING; the presence check and the type check each answer for
    # their own rule, so each one is what refuses the decision it is about.
    try:
        if not isinstance(decision, Mapping) or "ok" not in decision:
            return {"action": "none", "reason": "no_decision"}
        ok = decision["ok"]
        if ok is False:
            return {"action": "none", "reason": "no_route"}
        connection = decision.get("connection")
        read = {key: decision[key] if key in decision else _MISSING for key in ("engine", "model", "reason", "rule")}
        if (ok is not True or any(read[key] is _MISSING for key in ("reason", "rule", "engine", "model"))
                or not isinstance(connection, Mapping)):
            return {"action": "none", "reason": "no_decision"}  # routed in name only: not decide's answer
        names = set(connection)  # the connection's members, walked once, now that it is known to be a mapping
        conn = {key: connection[key] if key in names else _MISSING for key in ("kind", "id")}
        if any(conn[key] is _MISSING for key in ("kind", "id")):
            return {"action": "none", "reason": "no_decision"}  # a connection without its kind and its id
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
    if read["model"] is None:  # as route_decision reads it: decide never routes without a model, and a null
        # one would count the decided route as tried as no listed route, so that route could be offered again
        return {"action": "none", "reason": "no_decision"}
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
        try:  # the list copied once, in a guard of its own
            entries = list(tried) if isinstance(tried, (list, tuple)) else []
        except Exception:  # noqa: BLE001 - a list or tuple subclass whose iterating raises holds no attempt
            entries = None  # that can be read, so the history it holds is unreadable
        # The route service writes a notice with a list of attempts (routes.limits.notice). A notice that is there
        # but is not a mapping or holds no list, a list that cannot be copied, or an entry that is not an attempt
        # makes the history unreadable: it is never read as nothing tried. It is judged on the copy just made (only
        # a list or a tuple is copied; anything else copies as nothing), so what is read is exactly what was copied.
        # Only a notice that is absent or null (the route service made none) leaves the decided route alone tried.
        unreadable = notice is not None and (entries is None or (not entries and not isinstance(tried, (list, tuple))))
        for entry in entries or ():
            try:
                seen = _attempt(entry)
            except Exception:  # noqa: BLE001 - an entry that cannot be read is not an attempt, as other non-attempts
                seen = None
            if seen is None:
                unreadable = True  # not an attempt, so the history holding it is not one the route service wrote
            else:
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
    if unreadable:  # what was already tried cannot be told, so a switch could offer a limited route again
        return {"action": "ask", "reason": "attempts_unreadable", "skipped": [], "choices": choices}
    attempts.append((decision["engine"], decision["model"],
                     f"{decision['connection']['kind']}:{decision['connection']['id']}"))
    # Always read: a null snapshot or one that cannot be read passes over every listed route (fail closed).
    engines, err = snapshot(detection)
    skipped = []
    for place, route in enumerate(limits["fallback"], 1):
        if any(_same_route(route, seen) for seen in attempts):
            skipped.append({"place": place, **route_limits.describe(route), "skipped": "already_tried"})
            continue
        why = _state(config, engines, err, route["engine"])
        if why is not None:
            skipped.append({"place": place, **route_limits.describe(route), "skipped": why})
            continue
        if route_refusal is None:  # passed as None: the daemon's refusal was not read, so the route is not taken
            skipped.append({"place": place, **route_limits.describe(route), "skipped": "source_not_given"})
            continue
        if route_refusal is not NOT_CONSULTED:
            try:  # the daemon's own pre-launch refusal of this route, as the entry it would launch
                why = route_refusal({"route": copy.deepcopy(route)})
            except Exception:  # noqa: BLE001 - a refusal source that raises: the route is not taken, as detect has it
                why = "detection_source_failed"
            why = _code(why, "route_refused") if type(why) is str else why  # a word that is no code: as detect reads it
            if why is not None:
                skipped.append({"place": place, **route_limits.describe(route),
                                "skipped": why if type(why) is str else "detection_source_failed"})
                continue
        return {"action": "switch", "place": place, "route": copy.deepcopy(route), "source": source,
                "skipped": skipped, "choices": choices}
    return {"action": "ask", "reason": "fallback_exhausted", "skipped": skipped, "choices": choices}


# -- next_steps: the words a person reads for an answer of next_after_limit -----------------------------------
# What passed a listed route over, and what the person can do about it. A code not named here (a refusal code of
# the daemon's, say) gets the general sentence, which still names the code and what to do.
_SKIP_STEPS = {
    "already_tried": "{route} was already tried for this task: list another route to go on",
    "key_not_opted_in": "{route} spends from an API key that is not opted in to spending: opt the key in, "
                        "or list another route",
    "key_absent": "{route} needs an API key the key store does not hold: add the key and opt it in, "
                  "or list another route",
    "key_state_unknown": "{route} spends from an API key whose opt-in could not be read: make the key store "
                         "readable and opt the key in, or list another route",
    "route_not_configured": "{route} has no entry in routes.json: configure the engine, or list another route",
    "route_no_model": "{route} names no model and routes.json gives none: name a model, or list another route",
    "route_no_connection": "{route} names no connection in routes.json: configure its connection, "
                           "or list another route",
    "not_detected": "{route} was not found on this machine: install the engine and sign in, or list another route",
    "detection_unavailable": "{route} was not tried because no detection of this machine was given: "
                             "run detection, or choose a route yourself",
    "detection_malformed": "{route} was not tried because the detection of this machine could not be read: "
                           "run detection again, or choose a route yourself",
    "detection_source_failed": "{route} was not tried because one of detection's sources failed: look at the "
                               "daemon's log, or list another route",
    "breaker_open": "{route} was not tried because its connection's breaker is open after a failure there, and "
                    "an open breaker refuses every start: wait for its cooldown to end, or list another route",
    "breaker_unknown": "{route} was not tried because its connection's state could not be read: wait until it "
                       "reads, or list another route",
    "engine_not_active": "{route} uses an engine this daemon does not run: turn the engine on, "
                         "or list another route",
    "recursion_loop_needs_key": "{route} needs an API key connection: configure one with its key opted in, "
                                "or list another route",
    "launch_refused": "{route} would be refused by the daemon at launch: change its routes.json entry, "
                      "or list another route",
    "route_refused": "{route} would be refused by the daemon before launch: change its routes.json entry, "
                     "or list another route",
    "source_not_given": "{route} was not tried because a check it needs (the daemon's refusal or its breaker) "
                        "was passed as None, so it was not read: pass that check, or NOT_CONSULTED where no "
                        "daemon runs, or list another route",
}
_SKIP_GENERAL = "{route} was passed over ({code}): see why with routing show, or list another route"
_ANSWER_STEPS = {
    "fallback_exhausted": "No route listed for this limit can be switched to now: choose a route, wait for the "
                          "limit to reset, or list another route",
    "attempts_unreadable": "The list of routes already tried for this task could not be read, so no route is "
                           "switched to on its own: choose a route, or wait for the limit to reset",
    "on_limit_ask": "The routing settings say to ask at a limit: choose a route, or wait for the limit to reset",
    "hand_pick": "This task was given its route by hand, so it is never switched away from on its own: choose a "
                 "route, or wait for the limit to reset",
    "not_a_limit": "The run did not end at a usage limit, so nothing is switched or retried: look at the run's "
                   "own error",
    "no_route": "No route was decided for this task, so there is nothing to switch from: look at why routing "
                "refused it",
    "no_decision": "What was passed is not a routing decision, so nothing is switched: pass the decision routing "
                   "made",
}


def _when(until: object) -> str:
    """A wait time as a person reads it: the date and time in UTC, said so; a number no date can hold, as its
    seconds after the epoch, in UTC too."""
    try:
        return datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError, TypeError):
        return f"{until} seconds after 1970-01-01 00:00 UTC"


def _route_words(row: Mapping) -> str:
    return f"{row.get('engine')} ({row.get('model')}) on {row.get('connection')}"


def next_steps(answer: Mapping) -> list[str]:
    """The words a person reads for an answer of ``next_after_limit``: one sentence for every listed route that was
    passed over (what passed it over and what the person can do), then one for the answer itself. A switch names
    the route it goes to; ask, wait and none name what stopped the walk and what the person can do next."""
    lines = []
    for row in answer.get("skipped") or ():
        code = row.get("skipped")
        lines.append(_SKIP_STEPS.get(code, _SKIP_GENERAL).format(route=_route_words(row), code=code))
    action = answer.get("action")
    if action == "switch":
        lines.append(f"Switching to {_route_words(route_limits.describe(answer['route']))}, the next route listed "
                     "for this limit: cancel the new run to stop it")
    elif action == "wait":
        until = answer.get("until")
        lines.append("The routing settings say to wait at a limit: "
                     + (f"it resets at {_when(until)}; wait, or choose a route" if until is not None
                        else "no reset time was given; wait, or choose a route"))
    else:
        reason = answer.get("reason")
        lines.append(_ANSWER_STEPS.get(reason, f"No route is switched to ({reason}): choose a route"))
    return lines
