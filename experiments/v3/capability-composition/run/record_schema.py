"""Strict result-record validation for the R-PC suite (strengthens Q05).

Replaces sparse ``got.get(k) == v`` subset matching: required keys must be present, absent is
distinct from ``None``, and extra keys are rejected unless explicitly allowed. stdlib-only.
"""
from __future__ import annotations

from dataclasses import dataclass

MISSING = object()


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: object = object
    nullable: bool = False
    required: bool = True


@dataclass(frozen=True)
class RecordSpec:
    fields: tuple[FieldSpec, ...]
    allow_extra: bool = False


@dataclass(frozen=True)
class Mismatch:
    field: str
    reason: str
    expected: object = MISSING
    actual: object = MISSING


def validate_record(got, expected, spec: RecordSpec) -> list[Mismatch]:
    mismatches: list[Mismatch] = []
    names = {f.name for f in spec.fields}

    for field in spec.fields:
        if field.required and field.name not in got:
            mismatches.append(Mismatch(field.name, "missing"))
            continue
        if field.name in got:
            value = got[field.name]
            if value is None:
                if not field.nullable:
                    mismatches.append(Mismatch(field.name, "null"))
            elif field.type is not object and not isinstance(value, field.type):
                mismatches.append(Mismatch(field.name, "type", field.type, type(value)))

    if not spec.allow_extra:
        for key in got:
            if key not in names:
                mismatches.append(Mismatch(key, "extra"))

    for key, value in expected.items():
        if key not in got:
            mismatches.append(Mismatch(key, "missing", value, MISSING))
            continue
        if value is None:
            if got[key] is not None:
                mismatches.append(Mismatch(key, "null", None, got[key]))
        elif got[key] != value:
            mismatches.append(Mismatch(key, "value", value, got[key]))

    return mismatches


def record_conforms(got, expected, spec: RecordSpec) -> bool:
    return not validate_record(got, expected, spec)


def render_mismatches(mismatches) -> str:
    return "; ".join(f"{m.field}:{m.reason}" for m in mismatches)
