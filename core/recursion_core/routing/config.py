"""The routing config: ``routing.json`` beside ``routes.json``, and the cross-field rules its schema cannot state.

The owner's grill Q45 answer (Nov 6, 2026): "It should be default + customizable + hand picked. Hand picked
wins". A routing config has:

* ``logins``: each one names a connection (``connection_id``, the route contract's
  ``route.connection.id``), its canonical ``vendor``, its ``kind`` (``subscription`` or ``api_key``),
  and the engines it is bound to. No secret is in the file;
* ``default``: the route a start gets when no rule matches and the user picked nothing;
* ``rules``: ordered; ``{id, when: {task_kind?, tags_all?}, route}``; the first matching rule whose
  engine is routable wins (``router.decide``);
* ``undetected_rule``: what a matching rule whose engine is not routable does: ``skip`` (the default:
  try the next rule, then the default) or ``refuse``;
* per route, optionally, ``limits``: the user's fallback order and ``on_limit``, in the shape
  ``routes.limits`` defines and validates (that function is called here, not copied).

The per-task hand pick is not in the config. It lives on the task, and it always wins
(``router.decide``).

The loader checks, before any entry is used, the rules a JSON Schema cannot state:

* a login's ``vendor`` and ``kind`` match the credential CATALOG, which is built from what the daemon
  already knows and never from the routing file: the key store's metadata (lane KEYS: a key id names a
  route's connection id, with its ``provider``) for API keys, and the connections ``routes.json``
  configures for sign-ins (``catalog``). A label the operator typed is never trusted on its own;
* one login per (vendor, kind), one login per connection, one login per engine: a second login of one
  vendor cannot be configured, so nothing can rotate onto it;
* the Claude subscription is bound to ``claude_code`` and nothing else (grill Q12, PRODUCT.md 3.3:
  "We can use Anthropics signed in subscription for their CLI, but we need to use their API Key for
  other harnesses"). No other vendor/engine pairing is refused here: the owner's Nov 8 answer lets a
  subscription run through other harnesses' sign-ins, and ``subscription_routes`` decides which;
* the login bound to an engine is the connection ``routes.json`` launches that engine with, so a
  decision never names one login while the launch uses another;
* every fallback route the user listed (in ``routing.json`` or in the ``routes.json`` entry an engine
  would launch with) is billed through a configured login bound to that fallback's engine: a limit can
  only ever move a task to a login the user configured.

The catalog's own provider and kind are not attested here. If the key store's metadata mislabels a
key, the loader cannot tell (``test_residual_a_mislabelled_catalog_is_not_detected``).
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from recursion_core.routes import contract
from recursion_core.routes import limits as route_limits

CONFIG_FILE = "routing.json"            # beside routes.json: $XDG_CONFIG_HOME/recursion/routing.json
VERSION = 1
SCHEMA_FILE = Path(__file__).resolve().parent / "schema" / "routing_config.v1.json"
# The engines a route may name: the route contract's own ids (claude_code, codex, pi, hermes, opencode).
ENGINES = contract.ENGINES
# Canonical vendor ids, matched exactly: "Anthropic" or "anthropic-ai" is refused, never merged.
VENDORS = ("anthropic", "openai", "xai", "google", "cursor", "opencode")
LOGIN_KINDS = ("subscription", "api_key")
UNDETECTED_RULES = ("skip", "refuse")
TOP_LEVEL = frozenset({"version", "logins", "default", "rules", "undetected_rule"})
LOGIN_KEYS = frozenset({"id", "vendor", "kind", "connection_id", "engines"})
ROUTE_KEYS = frozenset({"engine", "model", "limits"})
RULE_KEYS = frozenset({"id", "when", "route"})
WHEN_KEYS = frozenset({"task_kind", "tags_all"})
# The vendor of a subscription sign-in, read from the engine routes.json configures it on.
SIGN_IN_VENDOR = MappingProxyType({"claude_code": "anthropic", "codex": "openai"})
# A harness profile's sign-in label (subscription_routes.policy.SUBSCRIPTION_SIGN_INS) and its vendor.
PROFILE_SIGN_IN_VENDOR = MappingProxyType({"chatgpt": "openai", "grok": "xai"})
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# A model the breaker store can record (daemon/control_adapter.py route_unbindable): 1 to 128 printable
# ASCII characters with no space.
_MODEL = re.compile(r"[\x21-\x7e]{1,128}")


class ConfigError(ValueError):
    """The routing config cannot be used. ``code`` is a typed word; nothing downstream reads ``detail``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class RoutingUnreadable(Exception):
    """The routing file exists but is not one the daemon may trust or parse."""


@dataclass(frozen=True)
class Login:
    id: str
    vendor: str
    kind: str
    connection_id: str
    engines: tuple[str, ...]


@dataclass(frozen=True)
class Route:
    engine: str
    model: str | None = None              # None: the model routes.json configures for the engine
    limits: Mapping | None = None         # None: the limits routes.json configures for the engine


@dataclass(frozen=True)
class Rule:
    id: str
    task_kind: frozenset | None
    tags_all: frozenset | None
    route: Route


@dataclass(frozen=True)
class Config:
    logins: Mapping                       # login id -> Login
    engine_login: Mapping                 # engine -> the id of its one bound login
    default: Route
    rules: tuple
    undetected_rule: str
    routes: Mapping                       # engine -> the routes.json entry (load_routes' shape), copied


def _id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _model_ok(value: object) -> bool:
    return isinstance(value, str) and _MODEL.fullmatch(value) is not None


def _connection_id_ok(value: object) -> bool:
    # The control schema's connection label: 1 to 200 characters, no line break (common.v1 connection.id).
    return isinstance(value, str) and 0 < len(value) <= 200 and "\r" not in value and "\n" not in value


def _texts(value: object) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(isinstance(v, str) and v for v in value)


def _connection(route: object) -> tuple[str, str] | None:
    """``(kind, id)`` of a route's connection, or None when it names none a login could match."""
    conn = route.get("connection") if isinstance(route, Mapping) else None
    if not isinstance(conn, Mapping):
        return None
    kind, ident = conn.get("kind"), conn.get("id")
    if not isinstance(kind, str) or not isinstance(ident, str):
        return None
    return kind, ident


def catalog(routes: object, keys: object) -> dict:
    """The credential catalog: connection id -> ``{"vendor", "kind"}``. Metadata only, never a secret.

    ``keys`` is the key store's metadata (lane KEYS), key id -> a mapping with ``provider``; every key
    is an ``api_key`` connection of that provider. ``routes`` is ``load_routes``' answer; a sign-in
    (``subscription``) connection it configures, on an engine's own route or in that route's
    ``limits.fallback`` list, gets its vendor from the engine (``SIGN_IN_VENDOR``) or, on a harness,
    from its profile's sign-in label (``PROFILE_SIGN_IN_VENDOR``). A sign-in whose vendor cannot be read
    is left out, so no login can name it, and an ``api_key`` connection in routes.json is not
    catalogued from there: only the key store says what a key is. One connection id with two different
    (vendor, kind) readings is refused whole.
    """
    if not isinstance(keys, Mapping):
        raise ConfigError("no_key_catalog", "the key store's metadata is required (an empty mapping when none)")
    if not isinstance(routes, Mapping):
        raise ConfigError("no_routes", "the loaded routes are required (an empty mapping when none)")
    out: dict[str, dict] = {}

    def put(ident: str, vendor: str, kind: str, where: str) -> None:
        seen = out.get(ident)
        if seen is not None and (seen["vendor"], seen["kind"]) != (vendor, kind):
            raise ConfigError("catalog_conflict", f"{ident}: {seen['vendor']} {seen['kind']} and {vendor} {kind} ({where})")
        out[ident] = {"vendor": vendor, "kind": kind}

    for key_id, meta in keys.items():
        if not isinstance(key_id, str) or not isinstance(meta, Mapping) or not isinstance(meta.get("provider"), str):
            raise ConfigError("key_metadata_invalid", repr(key_id) if isinstance(key_id, str) else "key id")
        put(key_id, meta["provider"], "api_key", "key store")
    for engine, entry in routes.items():
        route = entry.get("route") if isinstance(entry, Mapping) else None
        if not isinstance(route, Mapping):
            continue
        lim = route.get("limits")
        fallback = lim.get("fallback") if isinstance(lim, Mapping) else None
        # The engine's own route, then every route its fallback list names: all are routes.json's sign-ins.
        for candidate in (route, *(fallback if isinstance(fallback, list) else ())):
            conn = _connection(candidate)
            if conn is None or conn[0] != "subscription":
                continue
            eng = candidate.get("engine", engine) if candidate is not route else engine
            vendor = SIGN_IN_VENDOR.get(eng) if isinstance(eng, str) else None
            if vendor is None:
                profile = candidate.get("profile")
                label = profile.get("provider") if isinstance(profile, Mapping) else None
                vendor = PROFILE_SIGN_IN_VENDOR.get(label) if isinstance(label, str) else None
            if vendor is not None:
                put(conn[1], vendor, "subscription", f"routes.json {engine}")
    return out


def _route(raw: object, where: str) -> Route:
    if not isinstance(raw, Mapping) or set(raw) - ROUTE_KEYS or "engine" not in raw:
        raise ConfigError("bad_route", f"{where} must be {{engine, model?, limits?}}")
    engine = raw["engine"]
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ConfigError("unknown_engine", f"{where}.engine")
    model = raw.get("model")
    if model is not None and not _model_ok(model):
        raise ConfigError("bad_model", f"{where}.model")
    limits = None
    if "limits" in raw:
        try:
            # The validator copies a route and its connection one level deep only; a profile or any other
            # nested value would still be the caller's. The loaded config owns a copy of all of it.
            limits = copy.deepcopy(route_limits.validate_limits(raw["limits"]))
        except contract.RouteError as err:
            raise ConfigError("bad_limits", f"{where}.limits: {err.detail}") from None
        except (TypeError, ValueError, AttributeError):  # a value the limits validator could not read
            raise ConfigError("bad_limits", f"{where}.limits") from None
    return Route(engine, model, limits)


def _login(raw: object, i: int) -> Login:
    if not isinstance(raw, Mapping) or set(raw) != LOGIN_KEYS:
        raise ConfigError("bad_login", f"logins[{i}] must be {{id, vendor, kind, connection_id, engines}}")
    if not _id(raw["id"]):
        raise ConfigError("bad_login", f"logins[{i}].id")
    if raw["kind"] not in LOGIN_KINDS:
        raise ConfigError("bad_login", f"logins[{i}].kind")
    if not isinstance(raw["vendor"], str) or raw["vendor"] not in VENDORS:
        raise ConfigError("unknown_vendor", f"logins[{i}].vendor")
    if not _connection_id_ok(raw["connection_id"]):
        raise ConfigError("bad_login", f"logins[{i}].connection_id")
    engines = raw["engines"]
    if not isinstance(engines, list) or not engines:
        raise ConfigError("bad_login", f"logins[{i}] binds no engine")
    for eng in engines:
        if not isinstance(eng, str) or eng not in ENGINES:
            raise ConfigError("unknown_engine", f"logins[{i}].engines")
    if len(set(engines)) != len(engines):
        raise ConfigError("bad_login", f"logins[{i}].engines repeats an engine")
    return Login(raw["id"], raw["vendor"], raw["kind"], raw["connection_id"], tuple(engines))


def _rule(raw: object, i: int) -> Rule:
    if not isinstance(raw, Mapping) or set(raw) != RULE_KEYS:
        raise ConfigError("bad_rule", f"rules[{i}] must be {{id, when, route}}")
    if not _id(raw["id"]):
        raise ConfigError("bad_rule", f"rules[{i}].id")
    when = raw["when"]
    if not isinstance(when, Mapping) or not when or set(when) - WHEN_KEYS:
        raise ConfigError("bad_rule", f"rules[{i}].when")
    for name in when:
        if not _texts(when[name]):
            raise ConfigError("bad_rule", f"rules[{i}].when.{name}")
    task_kind = frozenset(when["task_kind"]) if "task_kind" in when else None
    tags_all = frozenset(when["tags_all"]) if "tags_all" in when else None
    return Rule(raw["id"], task_kind, tags_all, _route(raw["route"], f"rules[{i}].route"))


def load_config(raw: object, routes: object, keys: object) -> Config:
    """Validate a parsed ``routing.json`` against the loaded routes and the key metadata; raise ``ConfigError``.

    ``routes`` is ``daemon.control_adapter.load_routes``' answer (engine -> ``{route, deadline_s, binary}``);
    ``keys`` is the key store's metadata (key id -> ``{"provider", ...}``), an empty mapping when there is
    none. Nothing of the config is used until all of it has passed.
    """
    cat = catalog(routes, keys)
    if not isinstance(raw, Mapping):
        raise ConfigError("bad_config", "the routing config is not an object")
    extra = set(raw) - TOP_LEVEL
    if extra:
        raise ConfigError("unknown_field", ", ".join(sorted(map(str, extra))))
    if type(raw.get("version")) is not int or raw["version"] != VERSION:  # not True, not 1.0
        raise ConfigError("bad_version", f"version must be {VERSION}")
    raw_logins = raw.get("logins")
    if not isinstance(raw_logins, list):
        raise ConfigError("bad_config", "logins must be a list")
    logins: dict[str, Login] = {}
    engine_login: dict[str, str] = {}
    by_connection: dict[str, str] = {}
    by_vendor_kind: dict[tuple[str, str], str] = {}
    for i, item in enumerate(raw_logins):
        lg = _login(item, i)
        if lg.id in logins:
            raise ConfigError("duplicate_login_id", lg.id)
        if lg.connection_id in by_connection:
            raise ConfigError("connection_reused", f"{lg.id} and {by_connection[lg.connection_id]}")
        by_connection[lg.connection_id] = lg.id
        # The vendor and kind must match the catalog, not only the operator's label: a Claude
        # subscription labelled "openai" is refused here.
        meta = cat.get(lg.connection_id)
        if meta is None:
            raise ConfigError("unknown_connection", lg.id)
        if (meta["vendor"], meta["kind"]) != (lg.vendor, lg.kind):
            raise ConfigError("credential_mismatch", f"{lg.id} says {lg.vendor} {lg.kind}; the catalog has "
                              f"{meta['vendor']} {meta['kind']}")
        # No alternation: one login per (vendor, kind), so a second one of a vendor cannot be rotated in.
        if (lg.vendor, lg.kind) in by_vendor_kind:
            raise ConfigError("second_login_same_vendor", f"{lg.id} and {by_vendor_kind[(lg.vendor, lg.kind)]} "
                              f"are both {lg.vendor} {lg.kind} logins")
        by_vendor_kind[(lg.vendor, lg.kind)] = lg.id
        for eng in lg.engines:
            if eng in engine_login:
                raise ConfigError("engine_bound_twice", f"{eng}: {engine_login[eng]} and {lg.id}")
            engine_login[eng] = lg.id
        # Grill Q12: the Claude subscription runs only inside Claude Code.
        if lg.vendor == "anthropic" and lg.kind == "subscription" and set(lg.engines) != {"claude_code"}:
            raise ConfigError("claude_subscription_outside_claude_code", lg.id)
        logins[lg.id] = lg
    if not logins:
        raise ConfigError("no_logins", "at least one login is required")
    # The login bound to an engine is the connection routes.json launches that engine with.
    for eng, login_id in engine_login.items():
        entry = routes.get(eng)
        if entry is None:
            continue  # not configured on this machine: never routable (router.decide)
        lg = logins[login_id]
        if _connection(entry.get("route") if isinstance(entry, Mapping) else None) != (lg.kind, lg.connection_id):
            raise ConfigError("login_route_mismatch", f"{eng}: routes.json launches another connection than {lg.id}")
    default = _route(raw.get("default"), "default")
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list):
        raise ConfigError("bad_config", "rules must be a list")
    rules = []
    for i, item in enumerate(raw_rules):
        rule = _rule(item, i)
        if any(r.id == rule.id for r in rules):
            raise ConfigError("duplicate_rule_id", rule.id)
        rules.append(rule)
    for where, route in (("default", default), *((f"rule {r.id}", r.route) for r in rules)):
        if route.engine not in engine_login:
            raise ConfigError("engine_without_login", f"{where}: {route.engine}")
    _fallbacks_are_logins(default, rules, routes, logins, engine_login)
    policy = raw.get("undetected_rule", "skip")
    if policy not in UNDETECTED_RULES:
        raise ConfigError("bad_undetected_rule", "undetected_rule")
    copied = {eng: json.loads(json.dumps(entry)) for eng, entry in routes.items()}
    return Config(MappingProxyType(logins), MappingProxyType(engine_login), default, tuple(rules), policy,
                  MappingProxyType(copied))


def _fallbacks_are_logins(default: Route, rules: list, routes: Mapping, logins: Mapping, engine_login: Mapping) -> None:
    """Every fallback route a decision could launch with is billed through a configured login of its engine.

    A route's fallbacks are its own ``limits`` when the routing file gives them, otherwise those of the
    ``routes.json`` entry the engine launches with (only ``on_limit: fallback`` lists are launched from,
    but an ``ask`` list is offered to the user as a switch, so every list is checked).
    """
    seen: list[tuple[str, Mapping]] = []
    for where, route in (("default", default), *((f"rule {r.id}", r.route) for r in rules)):
        if route.limits is not None:
            seen.append((where, route.limits))
    for eng in engine_login:
        entry = routes.get(eng)
        route = entry.get("route") if isinstance(entry, Mapping) else None
        lim = route.get("limits") if isinstance(route, Mapping) else None
        if isinstance(lim, Mapping):
            seen.append((f"routes.json {eng}", lim))
    for where, lim in seen:
        fallback = lim.get("fallback")
        for place, other in enumerate(fallback if isinstance(fallback, list) else (), 1):
            engine = other.get("engine") if isinstance(other, Mapping) else None
            login_id = engine_login.get(engine) if isinstance(engine, str) else None
            lg = logins.get(login_id) if login_id is not None else None
            if lg is None or _connection(other) != (lg.kind, lg.connection_id):
                raise ConfigError("fallback_not_a_login", f"{where}: fallback {place}")


def read_routing_file(path: os.PathLike | str) -> object | None:
    """The parsed routing file, or None when there is NO file. Raises ``RoutingUnreadable`` or ``ConfigError``.

    The same three checks ``load_routes`` makes on ``routes.json`` (a regular file, owned by the
    daemon's user, not writable by group or others), made here on the open descriptor itself, which a
    symlink cannot reach (``O_NOFOLLOW``) and a FIFO cannot block (``O_NONBLOCK``). None means only
    "absent": a file that holds JSON ``null`` is refused ``bad_config``, never read as routing turned off,
    and a file that names one member twice is refused as unreadable, never read as "the last one wins".
    """
    if not hasattr(os, "geteuid"):
        raise RoutingUnreadable("the routing file needs an owner check, which this platform lacks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.fspath(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RoutingUnreadable(f"routing file unreadable: {type(exc).__name__}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RoutingUnreadable("routing file must be an operator-owned regular file, not group/other writable")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            text = fh.read().decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_no_duplicate_members)
    except (OSError, ValueError) as exc:
        raise RoutingUnreadable(f"routing file unreadable: {type(exc).__name__}") from None
    finally:
        os.close(fd)
    if parsed is None:
        raise ConfigError("bad_config", "the routing file holds null, not an object; remove the file to turn routing off")
    return parsed


def _no_duplicate_members(pairs: list) -> dict:
    """A JSON object whose members are all named once; a repeated name raises (``json.loads`` keeps the last)."""
    out: dict = {}
    for name, value in pairs:
        if name in out:
            raise ValueError("duplicate member")
        out[name] = value
    return out


def load(config_dir: os.PathLike | str, keys: object) -> Config | None:
    """Read ``routes.json`` (with ``load_routes`` itself) and ``routing.json`` beside it, and validate them.

    None only when there is no routing file. Raises ``RoutingUnreadable`` (either file) or ``ConfigError``
    (a routing file that is present but holds null or anything else that is not a valid config).
    """
    from recursion_core.daemon.control_adapter import ROUTES_FILE, RoutesUnreadable, load_routes  # Linux only

    folder = Path(config_dir)
    try:
        routes = load_routes(folder / ROUTES_FILE)
    except RoutesUnreadable as exc:
        raise RoutingUnreadable(f"routes.json: {exc}") from None
    raw = read_routing_file(folder / CONFIG_FILE)
    if raw is None:
        return None
    return load_config(raw, routes, keys)
