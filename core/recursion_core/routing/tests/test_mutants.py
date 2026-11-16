"""Mutation check: break the routing code one way at a time and show the routing tests catch each break.

Ported from the vault prototype's mutants.py (20261107-csl729-routing-config-q45-01, section 8.4). Kept:
M1, M2, M4 and M6 as the prototype wrote them, against this code. M3 ("a limit result retried on a second
login of the same vendor") mutated the prototype's ``run_task``, which dispatched; dispatch is not ported
(the daemon launches), so M3 is re-expressed against the rule that now carries it: a fallback the user
listed is billed only through a configured login of its engine. M5 and M7 mutated the loopback gate of
``dispatch`` and are not ported. M8 and M9 are new: the two loader rules this port adds or keeps alone.

Each mutant runs in a child interpreter (the same executable and ``-X`` options): the module is imported,
its source with the one patch is executed into the module's own namespace, and the routing test modules
run against it. A control run with no patch must pass, so a failure comes from the patch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import recursion_core

CORE = Path(recursion_core.__file__).resolve().parents[1]
TEST_MODULES = ("recursion_core.routing.tests.test_config", "recursion_core.routing.tests.test_router",
                "recursion_core.routing.tests.test_detect", "recursion_core.routing.tests.test_explain_and_limits",
                "recursion_core.routing.tests.test_hooks")
CONFIG, ROUTER, DETECT = "recursion_core.routing.config", "recursion_core.routing.router", "recursion_core.routing.detect"
HOOKS = "recursion_core.routing.hooks"

MUTANTS = {
    "M1 rules evaluated before the hand pick": (
        ROUTER,
        "    if hp is not None:\n",
        "    if hp is not None and not any(_matches(r, kind, tags) for r in config.rules):\n",
        {"test_hand_pick_overrides_matching_rule_and_default", "test_hand_pick_beats_every_rule_order_and_default",
         "test_exhaustive_every_routable_subset_routes_only_inside_it"}),
    "M2 an unavailable default falls back to any routable engine": (
        ROUTER,
        '    return refused("default_engine_unavailable"',
        '    for alt in config.engine_login:\n'
        '        if _state(config, entries, err, alt) is None:\n'
        '            return chosen("default", None, "default", alt, model_of(None, alt))\n'
        '    return refused("default_engine_unavailable"',
        {"test_undetected_default_refuses_and_never_picks_another_engine"}),
    "M3 a limit fallback may name a login the user did not configure (re-expressed)": (
        CONFIG,
        "            if lg is None or _connection(other) != (lg.kind, lg.connection_id):\n",
        "            if False:\n",
        {"test_fallback_routes_are_only_configured_logins_of_their_engine", "test_loader_cross_field_rules"}),
    "M4 the snapshot filtered entry by entry, not validated as a whole": (
        ROUTER,
        '            if not _entry_ok(eng, entry):\n                return None, "detection_malformed"\n',
        '            if not _entry_ok(eng, entry):\n                continue\n',
        {"test_adversarial_snapshots_refuse_everything_without_raising"}),
    "M6 vendor and kind taken from the operator's label, the catalog not checked": (
        CONFIG,
        '        if (meta["vendor"], meta["kind"]) != (lg.vendor, lg.kind):\n',
        "        if False:\n",
        {"test_claude_subscription_relabelled_as_another_vendor_is_refused",
         "test_second_login_under_another_label_is_refused"}),
    "M8 the login bound to an engine is not checked against the connection routes.json launches": (
        CONFIG,
        '        if _connection(entry.get("route") if isinstance(entry, Mapping) else None) != (lg.kind, lg.connection_id):\n',
        "        if False:\n",
        {"test_loader_cross_field_rules"}),
    "M9 the Claude subscription allowed outside claude_code": (
        CONFIG,
        '        if lg.vendor == "anthropic" and lg.kind == "subscription" and set(lg.engines) != {"claude_code"}:\n',
        "        if False:\n",
        {"test_engine_vendor_compatibility", "test_loader_cross_field_rules"}),
    # Slice 2.
    "M10 a hand-picked run switched to a listed fallback at a limit": (
        ROUTER,
        '    if decision["reason"] == "hand_pick":\n        return {"action": "ask", "reason": "hand_pick", "choices": choices}\n',
        "",
        {"test_a_hand_pick_is_never_switched_away_from"}),
    "M11 any failure treated as a limit (a blind retry)": (
        ROUTER,
        "    if type(code) is not str or code not in route_limits.LIMIT_CODES:",
        "    if code is None:",
        {"test_only_a_limit_leads_anywhere"}),
    "M12 a route already tried offered again": (
        ROUTER,
        "        if any(_same_route(route, seen) for seen in attempts):\n",
        "        if False:\n",
        {"test_the_next_listed_route_in_the_users_order", "test_never_a_route_the_user_did_not_list"}),
    "M13 an open breaker read as routable": (
        DETECT,
        '        if state == "open":\n            return "breaker_open"\n',
        "",
        {"test_an_open_or_unreadable_breaker_fences_its_connection", "test_decide_on_a_detected_snapshot_skips_a_fenced_rule"}),
    "M14 a key route routable with no key present or opted in": (
        DETECT,
        '    if kind == "api_key":\n',
        "    if False:\n",
        {"test_a_key_route_needs_its_key_present_and_opted_in"}),
    # From the first cross-family read of #371 (two Codex reviews): each guard a removal left green.
    "M15 the routing file's owner not checked": (
        CONFIG,
        "if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:",
        "if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:",
        {"test_a_file_of_another_user_is_refused"}),
    "M16 tags_all read as any of the tags": (
        ROUTER,
        "    if rule.tags_all is not None and not rule.tags_all <= tags:\n",
        "    if rule.tags_all is not None and not (rule.tags_all & tags):\n",
        {"test_tags_all_needs_every_tag"}),
    "M17 the default's explicit model ignored": (
        ROUTER,
        "eng, model = config.default.engine, model_of(config.default.model, config.default.engine)",
        "eng, model = config.default.engine, model_of(None, config.default.engine)",
        {"test_an_explicit_model_wins_over_the_configured_one"}),
    "M18 an empty condition list accepted": (
        CONFIG,
        "return isinstance(value, list) and len(value) > 0 and all(",
        "return isinstance(value, list) and all(",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M19 an unknown condition accepted": (
        CONFIG,
        "if not isinstance(when, Mapping) or not when or set(when) - WHEN_KEYS:",
        "if not isinstance(when, Mapping) or not when:",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M20 a connection id of any length accepted": (
        CONFIG,
        "return isinstance(value, str) and 0 < len(value) <= 200 and",
        "return isinstance(value, str) and 0 < len(value) and",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M21 a version of true or 1.0 accepted": (
        CONFIG,
        'if type(raw.get("version")) is not int or raw["version"] != VERSION:',
        'if raw.get("version") != VERSION:',
        {"test_values_a_schema_cannot_tell_apart_are_refused_by_the_loader"}),
    "M22 a present null routing file read as absent": (
        CONFIG,
        "    if parsed is None:\n        raise ConfigError(",
        "    if parsed is None:\n        return None\n        raise ConfigError(",
        {"test_a_present_null_file_is_refused_never_read_as_absent",
         "test_load_reads_routes_json_with_load_routes_and_routing_json_beside_it"}),
    "M23 loaded limits left shared with the caller": (
        CONFIG,
        'limits = copy.deepcopy(route_limits.validate_limits(raw["limits"]))',
        'limits = route_limits.validate_limits(raw["limits"])',
        {"test_loaded_limits_share_nothing_with_the_callers_objects"}),
    "M24 a member named twice read as the last one": (
        CONFIG,
        "parsed = json.loads(text, object_pairs_hook=_no_duplicate_members)",
        "parsed = json.loads(text)",
        {"test_a_member_named_twice_is_refused"}),
    # Slice 2, after #371 passed its reads: route.explain's published answer, and a key store that keeps keys of
    # providers routing does not list.
    "M25 an engine with a hand pick of another engine: the hand pick taken silently": (
        ROUTER,
        "        if type(engine) is not str or type(hand_engine) is not str or hand_engine != engine:\n"
        '            return {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"}\n',
        "",
        {"test_an_engine_and_a_hand_pick"}),
    "M26 explain's rule outcomes returned beside the decision": (
        ROUTER,
        '    return {"code": "ok", "decision": route_decision(decision)}',
        '    return {"code": "ok", "decision": route_decision(decision), "rules": explain(config, detection, task)["rules"]}',
        {"test_a_routed_task_answers_ok_and_the_route_decision_only"}),
    "M27 a refused hand pick answered with the config's route": (
        ROUTER,
        '    if not decision["ok"]:\n        detail = ',
        '    if not decision["ok"] and "hand_pick" in task:\n'
        '        decision = decide(config, detection, {k: v for k, v in task.items() if k != "hand_pick"})\n'
        '    if not decision["ok"]:\n        detail = ',
        {"test_the_h33_example_is_refused_never_answered_by_the_config",
         "test_a_refused_decision_answers_request_invalid_with_routings_code"}),
    "M28 a key of a provider outside the vendor list refused": (
        CONFIG,
        '        put(key_id, meta["provider"], "api_key", "key store")\n',
        '        if meta["provider"] not in VENDORS:\n'
        '            raise ConfigError("key_metadata_invalid", key_id)\n'
        '        put(key_id, meta["provider"], "api_key", "key store")\n',
        {"test_a_key_of_a_provider_outside_the_vendor_list_leaves_loading_alone"}),
    # Slice 3: the start registrant (seam S6).
    "M29 a start that names its engine routed all the same": (
        HOOKS,
        "        if engine is not None and hand is None:\n            return {}\n",
        "",
        {"test_a_named_engine_is_not_routed_and_nothing_is_read"}),
    "M30 an engine with a hand pick of another engine let through": (
        HOOKS,
        '            if engine is not None and (type(engine) is not str or picked["engine"] != engine):  # plain text only\n'
        '                return _refusal("engine_and_hand_pick_differ")\n',
        "",
        {"test_an_engine_and_a_hand_pick_of_another_engine_is_refused"}),
    "M31 a refused decision let the start go on as today's": (
        HOOKS,
        '        if not decision["ok"]:\n            reason = decision.get("reason")\n',
        '        if not decision["ok"]:\n            return {}\n            reason = decision.get("reason")\n',
        {"test_a_hand_pick_of_an_engine_that_is_not_routable_is_refused_and_nothing_is_chosen"}),
    "M32 the routing file's limits left out of the chosen entry": (
        HOOKS,
        '    if source == "routing":\n        entry["route"]["limits"] = limits\n',
        "",
        {"test_no_engine_named_runs_the_default"}),
    "M33 the decision's model left out of the chosen entry": (
        HOOKS,
        '    entry["route"]["model"] = model\n',
        "",
        {"test_a_hand_pick_wins_over_the_default"}),
    # The first reads of #399 (05a5d13d): each guard they found unprotected or missing.
    "M34 a null vendor reset read as no deadline": (
        ROUTER,
        "        if not _is_time(until):  # absent, null or not a time: the route service's retry time, if it is one\n",
        '        if "reset_at" not in (signal if isinstance(signal, Mapping) else {}):\n',
        {"test_wait_uses_breaker_retry_when_vendor_reset_is_null"}),
    "M35 the decided primary not counted as tried": (
        ROUTER,
        '    attempts.append((decision["engine"]',
        '    (lambda _: None)((decision["engine"]',
        {"test_primary_not_reoffered_without_notice"}),
    "M36 the model left out of a tried route's identity": (
        ROUTER,
        "    return all(theirs is _ANY or ours == theirs for ours, theirs in zip(mine, seen))\n",
        "    return all(theirs is _ANY or ours == theirs for n, (ours, theirs) in enumerate(zip(mine, seen)) if n != 1)\n",
        {"test_same_engine_and_connection_other_model_still_allowed"}),
    "M37 a route with no model anywhere routed": (
        ROUTER,
        '        return "route_no_model" if why is None and model is None else why\n',
        "        return why\n",
        {"test_a_route_with_no_model_anywhere_is_not_routable"}),
    "M38 a bad hand pick explained as rule rows": (
        ROUTER,
        'bad_task = decision.get("code") in ("bad_task", "bad_hand_pick")',
        'bad_task = decision.get("code") in ("bad_task",)',
        {"test_a_bad_hand_pick_is_explained_as_not_reached"}),
    "M39 explain reading the task a second time": (
        ROUTER,
        "    else:\n        decision = _decide(config, detection, kind, tags, hp)\n",
        "    else:\n        decision = decide(config, detection, task)\n",
        {"test_explain_reads_the_task_once"}),
    "M40 a present null tags read as absent": (
        ROUTER,
        '    if present["tags"]:\n',
        "    if tags is not None:\n",
        {"test_a_present_null_condition_is_refused_not_read_as_absent"}),
    "M41 a hand pick without its model member taken": (
        ROUTER,
        'shaped = set(hand) == {"engine", "model"} and',
        'shaped = set(hand) <= {"engine", "model"} and',
        {"test_bad_task_or_hand_pick_is_refused"}),
    "M42 a snapshot that cannot be walked raising instead of refusing": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a snapshot that cannot be read is malformed, never partly used\n",
        "    except ZeroDivisionError:\n",
        {"test_a_snapshot_that_cannot_be_walked_is_malformed"}),
    "M43 a decision that is not decide's answered as no route": (
        ROUTER,
        '        if not isinstance(decision, Mapping) or "ok" not in decision:\n'
        '            return {"action": "none", "reason": "no_decision"}\n'
        '        ok = decision["ok"]\n',
        '        if not isinstance(decision, Mapping):\n'
        '            return {"action": "none", "reason": "no_decision"}\n'
        '        ok = decision.get("ok", False)\n',
        {"test_next_after_limit_takes_decides_answer_only"}),
    "M44 a refusal detail that is not a code word": (
        ROUTER,
        'raise _Refuse("bad_hand_pick", "hand_pick_model")',
        'raise _Refuse("bad_hand_pick", "hand_pick.model")',
        {"test_every_refusal_detail_is_code_words"}),
    # The second reads of #399 (d857ebb5): sol C1-C3 (their M45-M47 as proposed), DeepSeek R1-R7.
    "M45 a matching rule with no model routed": (
        ROUTER,
        "        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)\n"
        "        why = why_not(eng, model)\n",
        "        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)\n"
        "        why = _state(config, entries, err, eng)\n",
        {"test_a_rule_without_model_is_skipped_or_refused"}),
    "M46 bytes accepted as active engine ids": (
        DETECT,
        "    if isinstance(active, (str, bytes)):",
        "    if isinstance(active, str):",
        {"test_small_interface_gaps"}),
    "M47 EXPLAIN_PARAMS missing from star exports": (
        "recursion_core.routing",
        '    "EXPLAIN_PARAMS",\n',
        "",
        {"test_small_interface_gaps"}),
    "M48 a present null hand pick read as none": (
        ROUTER,
        '    if not present["hand_pick"]:\n',
        "    if hand is None:\n",
        {"test_a_null_hand_pick_is_refused_alike_everywhere", "test_a_present_null_condition_is_refused_not_read_as_absent"}),
    # Round 7 re-expressed M49: the attempts list is now copied in a guard of its own, so a null one cannot raise
    # either way; what the type check still decides is that a mapping or a set names no attempt.
    "M49 attempts that are not a list or tuple read as one": (
        ROUTER,
        "list(tried) if isinstance(tried, (list, tuple)) else []",
        "list(tried)",
        {"test_attempts_in_a_mapping_or_a_set_are_no_attempts"}),
    "M50 a decision routed in name only used": (
        ROUTER,
        '            return {"action": "none", "reason": "no_decision"}  # routed in name only: not decide\'s answer\n',
        "            pass\n",
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M51 an invalid routes.json limits block used as it is": (
        ROUTER,
        '            return route_limits.validate_limits(raw), "routes"\n',
        '            return raw, "routes"\n',
        {"test_an_invalid_routes_json_limits_block_falls_back_to_ask"}),
    "M52 a listed route walked twice": (
        ROUTER,
        '    for place, route in enumerate(limits["fallback"], 1):\n',
        '    for place, route in enumerate(limits["fallback"] + limits["fallback"][:1], 1):\n',
        {"test_an_exhausted_list_names_each_listed_route_once_in_order"}),
    "M53 a task that cannot be read raising instead of refusing": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a task that cannot be read is refused, never raised through\n",
        "    except ZeroDivisionError:\n",
        {"test_a_task_that_cannot_be_read_is_refused"}),
    # M54, re-pointed in the fourth answer: route_decision's body is now one guard, so removing its isinstance
    # alone answers None through the guard; the mutant removes the guard instead (None and an unreadable object).
    "M54 route_decision raising on what it cannot read": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a decision that cannot be read is not a routed decision\n",
        "    except ZeroDivisionError:\n",
        {"test_route_decision_of_an_unreadable_or_wrong_typed_decision_is_none"}),
    # The third reads of #399 (1bf3254d): sol 1-2 (its M55-M60 as proposed), DeepSeek N1-N5.
    "M55 reason missing from a routed decision accepted": (
        ROUTER,
        '("reason", "rule", "engine", "model")',
        '("rule", "engine", "model")',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M56 engine missing from a routed decision accepted": (
        ROUTER,
        '("reason", "rule", "engine", "model")',
        '("reason", "rule", "model")',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M57 model missing from a routed decision accepted": (
        ROUTER,
        '("reason", "rule", "engine", "model")',
        '("reason", "rule", "engine")',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M58 a non-mapping connection accepted": (
        ROUTER,
        'not isinstance(connection, Mapping) or any(conn[key] is _MISSING for key in ("kind", "id"))',
        'connection is None or any(conn[key] is _MISSING for key in ("kind", "id"))',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M59 connection kind missing from a routed decision accepted": (
        ROUTER,
        'any(conn[key] is _MISSING for key in ("kind", "id"))',
        'any(conn[key] is _MISSING for key in ("id",))',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M60 an unreadable hand pick raises": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - null, a string or a hand pick that cannot be read: not a hand pick",
        "    except (AttributeError, TypeError):",
        {"test_a_hand_pick_that_cannot_be_read_is_refused"}),
    "M61 a detection object read before the guard": (
        ROUTER,
        '    try:\n        if "engines" not in detection:\n',
        '    if "engines" not in detection:\n        return None, "detection_unavailable"\n'
        '    try:\n        if "engines" not in detection:\n',
        {"test_a_detection_object_that_cannot_be_read_refuses_every_task"}),
    "M62 params that cannot be read raising": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - params that cannot be read are refused, never raised through\n",
        "    except ZeroDivisionError:\n",
        {"test_explain_answer_refuses_params_it_cannot_read"}),
    "M63 an engine decide never gives used as a key": (
        ROUTER,
        '        return {"action": "none", "reason": "no_decision"}  # an engine or model decide never gives\n',
        "        pass\n",
        {"test_a_decision_with_an_engine_decide_never_gives_is_no_decision"}),
    # M64, re-pointed in the fourth answer: a missing member now answers None through the guard's KeyError as
    # well, so the mutant removes the type check, which a list reason or model passes through without raising.
    "M64 route_decision publishing members of a type decide never gives": (
        ROUTER,
        "            return None  # members of a type decide never gives: not a routed decision\n",
        "            pass\n",
        {"test_route_decision_of_an_unreadable_or_wrong_typed_decision_is_none"}),
    "M65 a limit result that cannot be read raising": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a limit result that cannot be read is not a limit: no blind retry\n",
        "    except ZeroDivisionError:\n",
        {"test_a_limit_result_that_cannot_be_read_is_not_a_limit"}),
    # The fourth reads of #399 (477fa308): DeepSeek C-D, F and I (A-B are M54 and M64, E is M65's wider test).
    "M66 next_after_limit raising on a decision it cannot read": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a decision that cannot be read is not decide's answer\n",
        "    except ZeroDivisionError:\n",
        {"test_next_after_limit_answers_no_decision_for_a_decision_it_cannot_read",
         "test_next_after_limit_reads_each_member_inside_the_guard",
         "test_a_connection_that_iterates_but_cannot_be_indexed_is_no_decision"}),
    "M67 a missing snapshot called malformed": (
        ROUTER,
        '    if not isinstance(detection, Mapping):\n        return None, "detection_unavailable"\n',
        "",
        {"test_a_missing_snapshot_is_detection_unavailable"}),
    "M68 an attempt's structured connection not read as its word": (
        ROUTER,
        "    if isinstance(value, Mapping):\n        return f\"{_plain(value.get('kind'))}:{_plain(value.get('id'))}\"\n",
        "",
        {"test_an_attempt_with_a_structured_connection_counts_as_tried"}),
    # The fifth read of #399 (30073846): DeepSeek A-G (A and B are pinned by M66's wider killers).
    "M69 a connection that is not a mapping published": (
        ROUTER,
        "            return None  # not a mapping: one that only iterates and indexes like one is not decide's\n",
        "            pass\n",
        {"test_route_decision_publishes_only_what_json_takes"}),
    "M70 candidates that are not a list published": (
        ROUTER,
        "            return None  # candidates that are not a list (a generator would be used up and read as [])\n",
        "            pass\n",
        {"test_route_decision_refuses_members_dict_would_accept"}),
    "M71 a candidate that is not a mapping published": (
        ROUTER,
        "            return None  # a candidate that is not a mapping\n",
        "            pass\n",
        {"test_route_decision_refuses_members_dict_would_accept"}),
    "M72 a hand pick that is not a mapping published": (
        ROUTER,
        "            return None  # a hand pick that is neither null nor a mapping (nor one that only iterates and indexes)\n",
        "            pass\n",
        {"test_route_decision_publishes_only_what_json_takes"}),
    "M73 a bare connection word in attempts not counted as tried": (
        ROUTER,
        "    if isinstance(entry, str):\n        return (_ANY, _ANY, _plain(entry))\n",
        "",
        {"test_a_bare_connection_word_in_attempts_counts_as_tried"}),
    "M74 a wait until that is not a time passed through": (
        ROUTER,
        "        if not _is_time(until):  # absent",
        "        if until is None:  # absent",
        {"test_a_wait_until_is_a_time_or_the_retry_time"}),
    "M75 route_decision taking ok by truthiness": (
        ROUTER,
        'decision.get("ok") is not True:',
        'not decision.get("ok"):',
        {"test_ok_must_be_exactly_true_or_false"}),
    "M76 next_after_limit taking ok by truthiness": (
        ROUTER,
        "(ok is not True or any(read[key] is _MISSING",
        "(any(read[key] is _MISSING",
        {"test_ok_must_be_exactly_true_or_false"}),
    "M77 one attempts word read as its characters": (
        ROUTER,
        "        if isinstance(tried, str):\n            tried = [tried]  # one bare kind:id word, never its characters\n",
        "",
        {"test_a_bare_connection_word_in_attempts_counts_as_tried"}),
    # The sixth read of #399 (7bbd59be): DeepSeek F1-F6 (F4 is a sentence).
    "M78 a reason, rule or connection decide never gives used": (
        ROUTER,
        '        return {"action": "none", "reason": "no_decision"}  # a reason, rule or connection decide never gives\n',
        "        pass\n",
        {"test_values_that_cannot_be_compared_or_formatted_are_no_decision"}),
    "M79 an unreadable attempts entry discarding the limit": (
        ROUTER,
        "            except Exception:  # noqa: BLE001 - an entry that cannot be read is not an attempt, as other non-attempts\n",
        "            except ZeroDivisionError:\n",
        {"test_an_unreadable_attempts_entry_is_dropped_not_the_limit"}),
    "M80 a wait until of NaN or an infinity": (
        ROUTER,
        "        return math.isfinite(value)\n",
        "        return True\n",
        {"test_a_wait_until_is_finite"}),
    "M81 route_decision publishing a connection without kind and id": (
        ROUTER,
        '        if conn is None or type(conn["kind"]) is not str or type(conn["id"]) is not str:\n'
        "            return None  # a connection without its kind and id, with another member, or not plain words\n",
        "",
        {"test_route_decision_needs_the_connections_kind_and_id", "test_route_decision_needs_plain_words"}),
    "M82 an attempt naming only its connection not counted as tried": (
        ROUTER,
        "        if engine is None and model is None:\n            return (_ANY, _ANY, word)  # names only its connection, as a bare word does\n",
        "",
        {"test_an_attempt_naming_only_its_connection_counts_as_tried"}),
    "M83 an attempt whose parts are not words compared": (
        ROUTER,
        "            return None  # parts that are not words: compared later, they could name nothing (or raise)\n",
        "            pass\n",
        {"test_an_attempt_whose_parts_are_not_words_is_not_an_attempt"}),
    # The sixth read's F1 and F5 one step further (a str subclass passes isinstance), and F3's own check, which
    # raised on an int too large for a float or one whose conversion raises.
    "M84 a time of a number subclass converted": (
        ROUTER,
        "    if type(value) is not int and type(value) is not float:\n",
        "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n",
        {"test_a_wait_until_is_finite"}),
    "M85 an int too large for a float raising": (
        ROUTER,
        "    except OverflowError:  # an int too large for a float: no finite time\n",
        "    except ZeroDivisionError:\n",
        {"test_a_wait_until_is_finite"}),
    "M86 a reason that is a str subclass used": (
        ROUTER,
        '(type(read["reason"]) is not str or read["reason"] not in REASONS)',
        '(read["reason"] not in REASONS)',
        {"test_a_decision_word_that_is_a_str_subclass_is_no_decision"}),
    "M87 a rule that is a str subclass compared": (
        ROUTER,
        'read["rule"] is not None and type(read["rule"]) is not str)',
        'read["rule"] is not None and not isinstance(read["rule"], str))',
        {"test_a_decision_word_that_is_a_str_subclass_is_no_decision"}),
    "M88 a connection kind or id that is a str subclass formatted": (
        ROUTER,
        'and type(conn[key]) is not str for key in ("kind", "id")',
        'and not isinstance(conn[key], str) for key in ("kind", "id")',
        {"test_a_decision_word_that_is_a_str_subclass_is_no_decision"}),
    "M89 an engine that is a str subclass compared": (
        ROUTER,
        '(read["engine"] is not _MISSING and type(read["engine"]) is not str)',
        '(read["engine"] is not _MISSING and not isinstance(read["engine"], str))',
        {"test_a_decision_word_that_is_a_str_subclass_is_no_decision"}),
    "M90 a model that is a str subclass compared": (
        ROUTER,
        'read["model"] is not None and type(read["model"]) is not str)',
        'read["model"] is not None and not isinstance(read["model"], str))',
        {"test_a_decision_word_that_is_a_str_subclass_is_no_decision"}),
    "M91 an attempt's str subclass compared as it is": (
        ROUTER,
        "    return str.__str__(value) if issubclass(type(value), str) else value\n",
        "    return value\n",
        {"test_an_attempt_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M92 a limit code that is a str subclass compared": (
        ROUTER,
        "    if type(code) is not str or code not in route_limits.LIMIT_CODES:",
        "    if code not in route_limits.LIMIT_CODES:",
        {"test_a_limit_code_that_is_a_str_subclass_is_not_a_limit"}),
    "M93 route_decision publishing words that are str subclasses": (
        ROUTER,
        "        if type(reason) is not str or reason not in REASONS or type(engine) is not str or type(model) is not str:\n",
        "        if reason not in REASONS or not isinstance(engine, str) or not isinstance(model, str):\n",
        {"test_route_decision_needs_plain_words"}),
    # Round 7 (ece6c127): DeepSeek F1-F3, each rule this answer adds removed one at a time.
    "M94 a task kind that is a str subclass kept as it is": (
        ROUTER,
        '    kind = _plain(kind) if present["task_kind"] else None\n',
        '    kind = kind if present["task_kind"] else None\n',
        {"test_a_task_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M95 a tag that is a str subclass kept as it is": (
        ROUTER,
        "words = [_plain(t) for t in tags] if isinstance(tags, list) else None",
        "words = list(tags) if isinstance(tags, list) else None",
        {"test_a_task_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M96 tags that cannot be read raising": (
        ROUTER,
        "        except Exception:  # noqa: BLE001 - tags that cannot be read are refused, never raised through\n",
        "        except ZeroDivisionError:\n",
        {"test_tags_that_cannot_be_read_are_refused"}),
    "M97 a hand pick's words that are str subclasses kept as they are": (
        ROUTER,
        'engine, model = _plain(hand.get("engine")), _plain(hand.get("model"))',
        'engine, model = hand.get("engine"), hand.get("model")',
        {"test_a_task_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M98 a detection engine that is a str subclass compared as it is": (
        ROUTER,
        "            eng = _plain(eng)\n",
        "",
        {"test_a_detection_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M99 a detection reason that is a str subclass kept as it is": (
        ROUTER,
        '"reason": _plain(entry.get("reason"))',
        '"reason": entry.get("reason")',
        {"test_a_detection_word_that_is_a_str_subclass_is_read_as_its_text"}),
    "M100 explain_answer's names compared as they are": (
        ROUTER,
        "names = [_plain(name) for name in params]",
        "names = list(params)",
        {"test_explain_answer_never_compares_a_name_or_an_engine_it_has_not_checked"}),
    "M101 explain_answer comparing an engine it has not checked": (
        ROUTER,
        "        if type(engine) is not str or type(hand_engine) is not str or hand_engine != engine:\n",
        '        if hand_engine != given["engine"]:\n',
        {"test_explain_answer_never_compares_a_name_or_an_engine_it_has_not_checked"}),
    "M102 explain_answer's hand-pick engine kept as it is": (
        ROUTER,
        'hand_engine = _plain(hand["engine"]) if',
        'hand_engine = hand["engine"] if',
        {"test_explain_answer_never_compares_a_name_or_an_engine_it_has_not_checked"}),
    "M103 route_decision publishing a connection or hand pick with another member": (
        ROUTER,
        "    if len(keys) != len(names) or not all(type(key) is str for key in keys) or set(keys) != set(names):\n",
        "    if not set(names) <= set(keys):\n",
        {"test_route_decision_publishes_only_what_json_takes"}),
    "M104 route_decision publishing a candidate row that is not plain words": (
        ROUTER,
        '        if not all(type(r["engine"]) is str and _word_or_null(r["model"]) and _word_or_null(r["skipped"])\n'
        "                   for r in rows):\n",
        "        if False:\n",
        {"test_route_decision_publishes_only_what_json_takes"}),
    "M105 route_decision publishing a hand pick that is not plain words": (
        ROUTER,
        '            if hand is None or type(hand["engine"]) is not str or not _word_or_null(hand["model"]):\n',
        "            if hand is None:\n",
        {"test_route_decision_publishes_only_what_json_takes"}),
    "M106 an attempts list that cannot be read discarding the limit": (
        ROUTER,
        "        except Exception:  # noqa: BLE001 - a list or tuple subclass whose iterating raises holds no attempt\n",
        "        except ZeroDivisionError:\n",
        {"test_an_unreadable_attempts_entry_is_dropped_not_the_limit"}),
    # Round 8 (5feab1b1), as ruling 7 has it: what F1's corrected sentence says, and F2's rule.
    "M107 an absent task member left unread, against what the sentence says": (
        ROUTER,
        'kind, tags, hand = task.get("task_kind"), task.get("tags"), task.get("hand_pick")',
        'kind, tags, hand = (task.get(name) if present[name] else None for name in ("task_kind", "tags", "hand_pick"))',
        {"test_a_task_whose_reading_raises_is_refused_even_for_an_absent_member"}),
    "M108 two keys that read as one engine, the last one kept": (
        ROUTER,
        "            if eng in out:  # two keys that read as one engine: which entry is meant cannot be told\n"
        '                return None, "detection_malformed"\n',
        "",
        {"test_two_keys_that_read_as_one_engine_make_the_snapshot_unusable"}),
    # The fifth Codex read (gpt-6.1-sol, 477fa308..8984581e): its changes 1-3.
    "M109 a member's presence asked again after its value was read": (
        ROUTER,
        'any(read[key] is _MISSING for key in ("reason", "rule", "engine", "model"))',
        'any(key not in decision for key in ("reason", "rule", "engine", "model"))',
        {"test_a_primary_cannot_be_reoffered_when_a_member_disappears"}),
    "M110 route_decision publishing a connection id that is a str subclass": (
        ROUTER,
        'or type(conn["id"]) is not str:',
        'or not isinstance(conn["id"], str):',
        {"test_route_decision_refuses_a_connection_id_str_subclass"}),
    "M111 next_after_limit taking a reason decide never gives": (
        ROUTER,
        '(type(read["reason"]) is not str or read["reason"] not in REASONS)',
        '(type(read["reason"]) is not str)',
        {"test_next_after_limit_refuses_unknown_reason"}),
    # #419's first DeepSeek read (f30bc965), slice 3's own (H for routing's hooks, apart from #399's M numbers).
    "H1 a hand pick's model dropped when routing is off": (
        HOOKS,
        '            if picked is not None and (picked["model"] is not None or engine is None):  # nothing asked for is dropped\n'
        '                return _refusal("hand_pick_without_routing")\n',
        "",
        {"test_with_no_routing_file_a_hand_pick_is_never_dropped"}),
    "H2 a hand pick read by the registrant's own reads, not as decide() reads it": (
        HOOKS,
        '                picked = router._task({"hand_pick": hand})[2]\n',
        '                picked = {"engine": hand.get("engine"), "model": hand.get("model")}\n',
        {"test_a_hand_pick_that_is_not_one_is_refused_alike_with_or_without_an_engine"}),
    "H3 the engines not taken from the composition's table": (
        HOOKS,
        "        engines = tuple(hooks.engines.names())\n",
        "        engines = None\n",
        {"test_engine_registered_after_routing_is_used"}),
    "H4 the table's names not handed to the loader": (
        HOOKS,
        "config = rc.load(config_dir(environ), {}, engines=engines)",
        "config = rc.load(config_dir(environ), {})",
        {"test_engine_registered_after_routing_is_used"}),
    "H5 the caller's engine table not handed to load_routes": (
        CONFIG,
        "load_routes(folder / ROUTES_FILE) if engines is None else load_routes(folder / ROUTES_FILE, tuple(engines))",
        "load_routes(folder / ROUTES_FILE)",
        {"test_engine_registered_after_routing_is_used"}),
    "H6 a routing.json nested too deep escaping the loader": (
        CONFIG,
        "    except (OSError, ValueError, RecursionError) as exc:  # RecursionError: nested deeper than the parser goes\n",
        "    except (OSError, ValueError) as exc:\n",
        {"test_a_file_nested_deeper_than_the_parser_goes_is_unreadable"}),
    "H7 a routes.json nested too deep escaping the loader": (
        CONFIG,
        "    except RecursionError:  # load_routes' own json.loads, on a routes.json nested deeper than the parser goes\n"
        '        raise RoutingUnreadable("routes.json: nested deeper than the JSON parser goes") from None\n',
        "",
        {"test_a_file_nested_deeper_than_the_parser_goes_is_unreadable"}),
    "H8 one string taken for an engine table": (
        CONFIG,
        "    if isinstance(engines, (str, bytes)):\n"
        '        raise TypeError("engines is a collection of engine names, not one string")\n',
        "",
        {"test_load_refuses_one_string_for_its_engines"}),
    "H9 a decision that is not routed given an entry": (
        HOOKS,
        '        if router.route_decision(decision) is None:  # not a routed decision that decide publishes: no entry either\n'
        '            return None\n',
        "",
        {"test_route_entry_answers_none_for_anything_not_a_routed_decision",
         "test_R7_incomplete_routed_decision_must_not_get_an_entry"}),
    "H10 a route that cannot be described launched all the same": (
        HOOKS,
        "        if record is None:  # never for decide's own routed answers: no start launches half told\n"
        '            raise RuntimeError("routing chose a route it cannot describe")\n',
        "",
        {"test_a_route_that_cannot_be_described_is_never_launched"}),
    # #419's first astra read (f30bc965): R2 and R3's survivors (its A5, A6, A7).
    "H11 the engine table taken as it stood when routing registered": (
        HOOKS,
        '    def normalize(principal, request):\n'
        '        engine, hand = router._plain(request.get("engine")), request.get("hand_pick")  # the engine as its plain text\n'
        '        if engine is not None and hand is None:\n'
        '            return {}\n'
        '        picked = None\n'
        '        if hand is not None:  # read once, as decide() reads it, and refused as it refuses, routing on or off\n'
        '            try:\n'
        '                picked = router._task({"hand_pick": hand})[2]\n'
        '            except router._Refuse as refused:\n'
        '                return _refusal(f"{refused.code}:{refused.reason}")\n'
        '            if engine is not None and (type(engine) is not str or picked["engine"] != engine):  # plain text only\n'
        '                return _refusal("engine_and_hand_pick_differ")\n'
        '        engines = tuple(hooks.engines.names())\n',
        '    early = tuple(hooks.engines.names())  # the table as it stood when routing registered\n'
        '\n'
        '    def normalize(principal, request):\n'
        '        engine, hand = router._plain(request.get("engine")), request.get("hand_pick")  # the engine as its plain text\n'
        '        if engine is not None and hand is None:\n'
        '            return {}\n'
        '        picked = None\n'
        '        if hand is not None:  # read once, as decide() reads it, and refused as it refuses, routing on or off\n'
        '            try:\n'
        '                picked = router._task({"hand_pick": hand})[2]\n'
        '            except router._Refuse as refused:\n'
        '                return _refusal(f"{refused.code}:{refused.reason}")\n'
        '            if engine is not None and (type(engine) is not str or picked["engine"] != engine):  # plain text only\n'
        '                return _refusal("engine_and_hand_pick_differ")\n'
        '        engines = early\n',
        {"test_engine_registered_after_routing_is_used"}),
    "H12 the daemon's pre-launch refusals left out of detection": (
        HOOKS,
        "route_refusal=rd.daemon_route_refusal(None)",
        "route_refusal=None",
        {"test_daemon_prelaunch_refusal_remains_a_routing_refusal"}),
    "H13 the entry sharing the config's own dictionaries": (
        HOOKS,
        "        entry = copy.deepcopy(config.routes[engine])\n",
        "        entry = config.routes[engine]\n",
        {"test_entry_is_detached_from_configuration_and_other_entries"}),
    "H14 a profile route's model selector left as configured": (
        HOOKS,
        '        profile["model"] = model  # the profile\'s selector agrees with the decision (astra R2)\n',
        "        pass\n",
        {"test_selected_model_reaches_profile_as_well_as_outer_route"}),
    # #419's first sol read (f30bc965): its A9 and A12, and R3's registrant side.
    "H15 detection not told which engines the composition has": (
        HOOKS,
        "active=engines)",
        "active=None)",
        {"test_normalizer_enforces_active_composition_engines"}),
    "H16 a rule's own limits left out of the chosen entry": (
        HOOKS,
        '    if source == "routing":\n',
        '    if source == "routing" and decision.get("reason") == "default":\n',
        {"test_route_entry_carries_rule_limits"}),
    "H17 an entry that cannot be copied launched without one": (
        HOOKS,
        "        if entry is None:  # the chosen routes.json entry cannot be copied (a value nested too deep): unusable config\n"
        '            return _refusal("routing_config_invalid:unreadable")\n',
        "",
        {"test_a_route_that_cannot_be_described_is_never_launched"}),
    # #419's second reads (c2566d39): astra r2 1 and sol r2 1.
    "H18 a routing.json value too deep for load_config's copy escaping the loader": (
        CONFIG,
        "    except RecursionError:  # a routing.json value that parses but is nested deeper than validation's copy goes\n"
        '        raise RoutingUnreadable("routing.json: nested deeper than it can be copied") from None\n',
        "    except ZeroDivisionError:\n        raise\n",
        {"test_nested_fallback_profile_load_is_typed", "test_R3_routing_limits_copy_depth_has_a_typed_refusal"}),
    # #419's DeepSeek round 2 (c2566d39): F1-F4.
    "H19 a failure in routing's own limits lookup laid on the operator's file": (
        HOOKS,
        '    limits, source = router._route_limits(config, {"reason": reason, "rule": rule, "engine": engine})  # raised\n',
        "    try:\n        limits, source = router._route_limits(config, decision)\n    except Exception:\n        return None\n",
        {"test_a_failure_in_routings_own_limits_lookup_is_not_laid_on_the_file"}),
    "H20 a hand pick of an engine the request does not name dropped when routing is off": (
        HOOKS,
        '(picked["model"] is not None or engine is None)',
        'picked["model"] is not None',
        {"test_with_no_routing_file_a_hand_pick_is_never_dropped"}),
    "H21 a model selector added to a profile that has none": (
        HOOKS,
        '    if isinstance(profile, dict) and "model" in profile:\n',
        "    if isinstance(profile, dict):\n",
        {"test_a_profile_without_a_model_selector_is_given_none"}),
    "H22 the request's engine compared as it came": (
        HOOKS,
        'router._plain(request.get("engine"))',
        'request.get("engine")',
        {"test_hostile_engine_is_typed_even_with_a_hand_pick"}),
    # #419's DeepSeek round 3 (f8ff7b4b), under ruling 7: F1 and F2.
    "H23 a request engine that is not a word compared all the same": (
        HOOKS,
        '(type(engine) is not str or picked["engine"] != engine)',
        'picked["engine"] != engine',
        {"test_a_non_str_engine_with_a_hand_pick_is_typed"}),
    "H24 a rule decision that names no rule given an entry": (
        HOOKS,
        ' or (reason == "config" and type(rule) is not str)',
        "",
        {"test_a_rule_decision_without_its_rule_gets_no_entry"}),
    # The Nov 15 integration sweep against master (gpt-6.1-sol): its F2.
    "M112 rules after the chosen or refused route explained by their own conditions": (
        ROUTER,
        "        elif done:  # the route was chosen or refused before this rule: routing never looked at its conditions\n"
        '            row.update(outcome="not_reached", reason=None)\n',
        "",
        {"test_f2_rules_after_a_chosen_rule_are_not_reached",
         "test_rules_after_a_refused_rule_are_not_reached_whatever_their_conditions"}),
}

# Removals from the published schema, each caught by the schema tests (which need jsonschema: hosted CI and
# the 3.12 interpreter have it, the free-threaded one does not).
SCHEMA = "schema"
SCHEMA_MUTANTS = {
    "S1 the top-level members not required": (
        SCHEMA, '  "required": ["version", "logins", "default", "rules"],\n', "",
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S2 a login's members not required": (
        SCHEMA, '"required": ["id", "vendor", "kind", "connection_id", "engines"],', '"required": [],',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S3 an unknown condition allowed": (
        SCHEMA, '"additionalProperties": false,\n          "minProperties": 1,', '"minProperties": 1,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S4 an empty tags_all allowed": (
        SCHEMA, '"tags_all": {"type": "array", "minItems": 1,', '"tags_all": {"type": "array", "minItems": 0,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S5 a login binding one engine twice allowed": (
        SCHEMA, '"uniqueItems": true,', '"uniqueItems": false,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S6 a fallback connection's kind and id not required": (
        SCHEMA, '"required": ["kind", "id"],', '"required": [],',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S7 a line break allowed at the end of an id": (
        SCHEMA, r'"pattern": "^[a-z0-9][a-z0-9._-]{0,63}$",' + "\n      " + r'"not": {"pattern": "[\\r\\n]"}',
        r'"pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S8 a profile engine's fallback with no profile allowed": (
        SCHEMA, '"then": {"required": ["profile"], "properties": {"profile": {"type": "object"}}}', '"then": {}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S9 a custody key fallback with no credential_env allowed": (
        SCHEMA, '"required": ["credential_env"], ', "",
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    # S10 to S15 answer the second reads of #371 (b3bdb121): each removal had left every lane test green.
    "S10 an api_key_helper allowed on a subscription connection": (
        SCHEMA, '"connection": {"properties": {"kind": {"const": "api_key"}}}', '"connection": {"properties": {}}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S11 a credential_env that is not a string allowed": (
        SCHEMA, '"credential_env": {"type": "string", "minLength": 1}', '"credential_env": {"minLength": 1}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S12 an explicit custody key source exempt from credential_env": (
        SCHEMA, '"not": {"required": ["key_source"], "properties": {"key_source": {"const": "api_key_helper"}}}',
        '"not": {"required": ["key_source"]}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S13 a passthrough method that is not a string allowed": (
        SCHEMA, '"method": {"type": "string", "minLength": 1}', '"method": {"minLength": 1}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S14 an empty passthrough method allowed": (
        SCHEMA, '"method": {"type": "string", "minLength": 1}', '"method": {"type": "string"}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S15 a helper key fallback made to name a credential_env": (
        SCHEMA,
        ',\n            "not": {"required": ["key_source"], "properties": {"key_source": {"const": "api_key_helper"}}}',
        "",
        {"test_a_helper_key_fallback_needs_no_credential_env"}),
}

try:
    import jsonschema  # noqa: F401 - only whether it is there
except ImportError:  # pragma: no cover - depends on the interpreter
    HAVE_JSONSCHEMA = False
else:
    HAVE_JSONSCHEMA = True

CHILD = r"""
import importlib, io, json, os, sys, tempfile, unittest
from pathlib import Path
job = json.loads(sys.stdin.read())
if job["module"] == "schema":
    from recursion_core.routing import config
    source = config.SCHEMA_FILE.read_text(encoding="utf-8")
    assert source.count(job["old"]) == 1, "the mutant's anchor is not in the schema exactly once"
    fd, name = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(source.replace(job["old"], job["new"]))
    json.loads(Path(name).read_text(encoding="utf-8"))  # the mutant is still a JSON document
    config.SCHEMA_FILE = Path(name)
else:
    module = importlib.import_module(job["module"])
    with open(module.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert source.count(job["old"]) == 1, "the mutant's anchor is not in the source exactly once"
    exec(compile(source.replace(job["old"], job["new"]), module.__file__, "exec"), module.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromNames(job["tests"])
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
if job["module"] == "schema":
    os.unlink(name)
failed = {t.id().split(" ")[0].rsplit(".", 1)[-1] for t, _ in result.failures + result.errors}
print(json.dumps({"ran": result.testsRun, "failed": sorted(failed)}))
"""


def run_child(module: str, old: str, new: str) -> dict:
    options = [arg for key, value in sys._xoptions.items() for arg in ("-X", key if value is True else f"{key}={value}")]
    env = dict(os.environ, PYTHONPATH=str(CORE))
    job = json.dumps({"module": module, "old": old, "new": new, "tests": list(TEST_MODULES)})
    proc = subprocess.run([sys.executable, *options, "-B", "-c", CHILD], input=job, capture_output=True, text=True, env=env,
                          cwd=str(CORE))
    if proc.returncode != 0:
        raise AssertionError(f"the child run failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class MutantTests(unittest.TestCase):
    def test_control_run_with_no_patch_passes(self) -> None:
        anchor = MUTANTS["M1 rules evaluated before the hand pick"][1]
        out = run_child(ROUTER, anchor, anchor)
        self.assertGreater(out["ran"], 20)
        self.assertEqual(out["failed"], [])

    def test_every_kept_mutant_is_caught(self) -> None:
        for name, (module, old, new, killers) in MUTANTS.items():
            with self.subTest(name):
                out = run_child(module, old, new)
                self.assertTrue(out["failed"], f"{name}: no test failed")
                self.assertTrue(killers <= set(out["failed"]), f"{name}: failed {out['failed']}, expected {sorted(killers)}")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_every_schema_mutant_is_caught(self) -> None:
        anchor = SCHEMA_MUTANTS["S1 the top-level members not required"][1]
        self.assertEqual(run_child(SCHEMA, anchor, anchor)["failed"], [])  # the schema path's own control
        for name, (module, old, new, killers) in SCHEMA_MUTANTS.items():
            with self.subTest(name):
                out = run_child(module, old, new)
                self.assertTrue(killers <= set(out["failed"]), f"{name}: failed {out['failed']}, expected {sorted(killers)}")


if __name__ == "__main__":
    unittest.main()
