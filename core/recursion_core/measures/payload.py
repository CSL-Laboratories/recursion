"""The measure payload: run_id, at and a list of measures, each with class, name, value, unit,
source (daemon, engine or estimate) and certainty (known or unknown).

An unknown has value None, never 0; a known has a finite number from 0 to 2**64 - 1, never a
bool (NaN, infinity, a negative number and a larger one are refused). A measure has exactly the
six fields, and run_id, at, class, name and unit are non-empty strings. Anything else is refused.
No event is written here and no schema is touched: the event type that will carry this payload is
a later slice.
"""
import math
import numbers

U64_MAX = 2 ** 64 - 1  # no figure this payload carries can honestly be larger
SOURCES = ('daemon', 'engine', 'estimate')
CERTAINTIES = ('known', 'unknown')
FIELDS = ('class', 'name', 'value', 'unit', 'source', 'certainty')


class MeasureRefused(ValueError):
    """A measure or payload that does not hold the rules above."""


def _text(value, what):
    if not isinstance(value, str) or not value:
        raise MeasureRefused(f'measure_refused: {what} must be a non-empty string')
    return value


def _finite_non_negative(value):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return False
    if not isinstance(value, numbers.Integral) and not math.isfinite(value):
        return False  # an int is always finite; math.isfinite would overflow on a huge one
    return 0 <= value <= U64_MAX


def _checked(measure):
    if not isinstance(measure, dict) or set(measure) != set(FIELDS):
        raise MeasureRefused(f'measure_refused: a measure has exactly the fields {", ".join(FIELDS)}')
    for key in ('class', 'name', 'unit'):
        _text(measure[key], key)
    if measure['source'] not in SOURCES:
        raise MeasureRefused(f'measure_refused: source {measure["source"]!r} is not one of '
                             f'{", ".join(SOURCES)}')
    certainty, value = measure['certainty'], measure['value']
    if certainty not in CERTAINTIES:
        raise MeasureRefused(f'measure_refused: certainty {certainty!r} is not known or unknown')
    if certainty == 'unknown' and value is not None:
        raise MeasureRefused('measure_refused: an unknown measure has value None, never a number')
    if certainty == 'known' and not _finite_non_negative(value):
        raise MeasureRefused('measure_refused: a known measure has a finite number of zero or more')
    return {key: measure[key] for key in FIELDS}


def build_measure_payload(run_id, at, measures):
    """One payload for one run at one instant; the measures are copied, never changed."""
    return {'run_id': _text(run_id, 'run_id'), 'at': _text(at, 'at'),
            'measures': [_checked(m) for m in measures]}
