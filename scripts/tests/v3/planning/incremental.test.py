#!/usr/bin/env python3
"""Golden and randomized digest parity between naive and incremental reduce()."""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from scripts.tests.v3.planning.reduce_reference import reduce_naive  # noqa: E402
from scripts.v3.planning.replay.kernel import genesis, reduce  # noqa: E402
from scripts.v3.planning.validation.canonical import digest, seal  # noqa: E402


def _intent_event(state: dict, template_record: dict, rec_id: str, text: str,
                  sequence: int, logical_time: int) -> dict:
    record = copy.deepcopy(template_record)
    record["id"] = rec_id
    record["payload"]["intent_text"] = text
    record["created_logical_time"] = logical_time
    record["causation_id"] = f"CMD-RAND-{sequence:04d}"
    record = seal(record)
    event = {
        "event_schema_version": "m1a-event-1",
        "event_id": f"EV-RAND-{sequence:04d}",
        "stream_id": state["stream_id"],
        "sequence": sequence,
        "previous_event_digest": state["last_event_digest"],
        "logical_time": logical_time,
        "command_id": f"CMD-RAND-{sequence:04d}",
        "command_digest": "ab" * 32,
        "event_type": "OwnerIntentRecorded",
        "primary_record": record,
        "related_records": [],
    }
    event["state_delta_digest"] = digest({
        "event_type": event["event_type"],
        "primary_record": event["primary_record"],
        "related_records": event["related_records"],
    })
    return seal(event)


def _compare_stream(start: dict, events: list[dict], *, label: str) -> int:
    naive = start
    incremental = start
    steps = 0
    for index, event in enumerate(events, 1):
        naive = reduce_naive(naive, event)
        incremental = reduce(incremental, event)
        assert naive["content_digest"] == incremental["content_digest"], (
            f"{label} digest step {index}"
        )
        assert naive == incremental, f"{label} state step {index}"
        steps += 1
    return steps


def _fixture_steps() -> tuple[int, int, dict]:
    streams = 0
    steps = 0
    template_record = None
    genesis_record = None
    for path in sorted(FIXTURE_DIR.glob("F-M1A-*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for case in bundle["cases"]:
            events = list(case.get("initial_events") or []) + list(case.get("expected_events") or [])
            if not events:
                continue
            start = genesis(case["genesis"])
            steps += _compare_stream(
                start, events, label=f"{path.name}:{case.get('case_id')}",
            )
            streams += 1
            if template_record is None:
                for event in events:
                    if event["event_type"] == "OwnerIntentRecorded":
                        template_record = event["primary_record"]
                        genesis_record = case["genesis"]
                        break
    return streams, steps, {"record": template_record, "genesis": genesis_record}


def _random_steps(template: dict, rng: random.Random) -> tuple[int, int]:
    streams = 0
    steps = 0
    start = genesis(template["genesis"])
    record = template["record"]
    for stream_index in range(8):
        length = rng.randint(1, 12)
        naive = start
        incremental = start
        events = []
        for offset in range(length):
            sequence = naive["last_event_sequence"] + 1
            event = _intent_event(
                naive, record,
                rec_id=f"INTENT-R{stream_index:02d}{offset:02d}",
                text=f"randomized intent {rng.randrange(1_000_000)}",
                sequence=sequence,
                logical_time=sequence,
            )
            events.append(event)
            naive = reduce_naive(naive, event)
            incremental = reduce(incremental, event)
            assert naive["content_digest"] == incremental["content_digest"]
            assert naive == incremental
            steps += 1
        # Prefix property: a random prefix of the same stream matches independently.
        cut = rng.randint(1, length)
        steps += _compare_stream(
            start, events[:cut], label=f"random-{stream_index}-prefix-{cut}",
        )
        streams += 1
    return streams, steps


def main() -> int:
    fixture_streams, fixture_steps, template = _fixture_steps()
    assert template["record"] is not None, "no OwnerIntentRecorded template in fixtures"
    rng = random.Random(336)
    random_streams, random_steps = _random_steps(template, rng)
    print(
        f"M1A_INCREMENTAL_PASS fixture_streams={fixture_streams} "
        f"fixture_steps={fixture_steps} random_streams={random_streams} "
        f"random_steps={random_steps}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"M1A_INCREMENTAL_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
