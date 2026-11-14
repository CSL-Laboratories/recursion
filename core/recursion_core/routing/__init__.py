"""Routing: a default engine, a routing config the user writes, and a per-task hand pick that always wins.

Lane ROUTING: the config (``config``: the ``routing.json`` file, its schema in
``schema/routing_config.v1.json``, and the loader's cross-field rules), the pure decision
(``router.decide``, with ``explain`` and ``next_after_limit``) and detection (``detect``). Nothing here is
wired into the daemon yet: no start path calls it, nothing is registered, nothing is launched, written or
emitted. There is no learned router (grill Q45).
"""

from recursion_core.routing.config import (CONFIG_FILE, ENGINES, RECURSION_LOOP, VENDORS, Config, ConfigError, Login,
                                           Route, RoutingUnreadable, Rule, catalog, load, load_config, read_routing_file)
# ``detect.detect`` is reached through its module: re-exporting the function here would hide the module.
from recursion_core.routing.detect import breaker_view, daemon_route_refusal, key_state_from
from recursion_core.routing.router import decide, explain, next_after_limit, route_decision, snapshot

__all__ = [
    "CONFIG_FILE",
    "ENGINES",
    "RECURSION_LOOP",
    "VENDORS",
    "Config",
    "ConfigError",
    "Login",
    "Route",
    "RoutingUnreadable",
    "Rule",
    "breaker_view",
    "catalog",
    "daemon_route_refusal",
    "decide",
    "explain",
    "key_state_from",
    "load",
    "load_config",
    "next_after_limit",
    "read_routing_file",
    "route_decision",
    "snapshot",
]
