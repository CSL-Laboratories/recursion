#!/usr/bin/env python3
"""Small fail-closed grammar for authenticated Lucas Phase-8 commands."""

from __future__ import annotations

import re


NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don'?t|doesn'?t|never|not|cannot|can'?t|won'?t|"
    r"shouldn'?t|must\s+not|shall\s+not)\b",
    re.IGNORECASE,
)
OPPOSITE_PATTERN = {
    "pilot-plan-approve": re.compile(
        r"\b(?:reject\w*|refus\w*|revok\w*|deni\w*|declin\w*|unapprov\w*)\b",
        re.IGNORECASE,
    ),
    "pilot-window-open": re.compile(
        r"\b(?:clos\w*|reclos\w*|unopen\w*|shut\w*|revok\w*|withdraw\w*|rescind\w*)\b",
        re.IGNORECASE,
    ),
    "pilot-window-close": re.compile(
        r"\b(?:open\w*|reopen\w*|unclos\w*|authoriz\w*|arm\w*)\b",
        re.IGNORECASE,
    ),
}
# Apostrophes keep contractions intact. Dots, slashes, and hyphens keep authority identifiers
# intact so a word embedded in (for example) ``project-window-close`` is never treated as prose.
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/'-]{0,127}")
AUTHORITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def project_window_open_terms(body: str) -> list[str] | None:
    """Extract the one project-aware window-open form without interpreting prose.

    This is intentionally narrower than :func:`require_affirmative_command`: legacy
    three-bound commands remain supported at the actuator boundary, but they do not carry a
    project from which a deterministic Head-Hermes decision can be shaped.  Returning ``None``
    leaves every near-miss on the existing model-plus-actuator path; it never grants authority.
    """
    if not isinstance(body, str):
        return None
    normalized_body = body.replace("\u2018", "'").replace("\u2019", "'")
    tokens = [match.group(0).rstrip(".") for match in TOKEN.finditer(normalized_body)]
    if len(tokens) != 11:
        return None
    grammar = {
        0: "open", 1: "pilot", 2: "window", 4: "for", 6: "goal",
        8: "for", 10: "seconds",
    }
    if any(tokens[index].lower() != expected for index, expected in grammar.items()):
        return None
    window_id, project_id, goal_id, seconds = tokens[3], tokens[5], tokens[7], tokens[9]
    if not all(AUTHORITY_ID.fullmatch(value) for value in (project_id, goal_id, window_id)):
        return None
    if not seconds.isdigit() or len(seconds) > 5:
        return None
    try:
        bounded_seconds = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 1 <= bounded_seconds <= 86400:
        return None
    terms = [project_id, goal_id, window_id, seconds]
    try:
        require_affirmative_command(normalized_body, "pilot-window-open", terms)
    except ValueError:
        return None
    return terms


def _sequences(verb: str, terms: list[str]) -> list[list[str]]:
    # An @ prefix marks case-insensitive grammar words. Authority-bound identifiers and digests
    # remain byte-exact and therefore carry no prefix.
    if verb == "pilot-plan-approve" and len(terms) == 2:
        plan_id, digest = terms
        return [
            ["@approve", plan_id, digest],
            ["@approve", "@pilot", "@plan", plan_id, "@with", "@digest", digest],
            ["@approve", "@plan", plan_id, "@digest", digest],
        ]
    if verb == "pilot-window-open" and len(terms) == 4:
        project_id, goal_id, window_id, seconds = terms
        return [
            ["@open", "@pilot", "@window", window_id, "@for", project_id,
             "@goal", goal_id, "@for", seconds, "@seconds"],
            *_sequences(verb, [goal_id, window_id, seconds]),
        ]
    if verb == "pilot-window-open" and len(terms) == 3:
        goal_id, window_id, seconds = terms
        return [
            ["@authorize", goal_id, window_id, seconds],
            ["@open", goal_id, window_id, seconds],
            ["@authorize", "@pilot", "@window", window_id, "@for", "@goal", goal_id,
             "@for", seconds, "@seconds"],
            ["@open", "@pilot", "@window", window_id, "@for", "@goal", goal_id,
             "@for", seconds, "@seconds"],
        ]
    if verb == "pilot-window-close" and len(terms) == 1:
        window_id = terms[0]
        return [["@close", window_id], ["@close", "@pilot", "@window", window_id]]
    return []


def require_affirmative_command(body: str, verb: str, terms: list[str]) -> None:
    """Require one exact affirmative command and reject negated/opposite language."""
    if not isinstance(body, str) or not isinstance(verb, str) \
            or not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise ValueError("instruction arguments have invalid types")
    if verb not in OPPOSITE_PATTERN:
        raise ValueError("unsupported Phase 8 authority verb")
    # Telegram/user input commonly carries typographic apostrophes. Fold them before both the
    # command match and ambiguity scan so a pasted ``don’t`` cannot bypass fail-closed negation.
    normalized_body = body.replace("\u2018", "'").replace("\u2019", "'")
    token_matches = list(TOKEN.finditer(normalized_body))
    tokens = [match.group(0).rstrip(".") for match in token_matches]
    sequences = _sequences(verb, [str(term) for term in terms])
    match: tuple[int, list[str]] | None = None
    for sequence in sequences:
        for index in range(len(tokens) - len(sequence) + 1):
            # The four-term form has the canonical project available. Preserve historical
            # three-term commands only when the entire instruction is exactly one reviewed
            # command shape; otherwise stray prose could smuggle a conflicting project claim
            # around a legacy subsequence.
            if verb == "pilot-window-open" and len(terms) == 4 \
                    and (index != 0 or len(tokens) != len(sequence)):
                continue
            if all(actual.lower() == expected[1:] if expected.startswith("@") else actual == expected
                   for actual, expected in zip(tokens[index:index + len(sequence)], sequence)):
                match = (index, sequence)
                break
        if match is not None:
            break
    if match is None:
        raise ValueError("instruction does not name the exact Phase 8 command and bounds")

    # Do not scan authority values as natural-language verbs. Mask only the exact token spans
    # occupying the matched command; repeated ids and every other byte of prose remain visible
    # to the original whole-language fail-closed regexes.
    scan = list(normalized_body)
    start, sequence = match
    for offset, expected in enumerate(sequence):
        if not expected.startswith("@"):
            span_start, span_end = token_matches[start + offset].span()
            scan[span_start:span_end] = " " * (span_end - span_start)
    ambiguity_text = "".join(scan)
    if NEGATION_PATTERN.search(ambiguity_text) or OPPOSITE_PATTERN[verb].search(ambiguity_text):
        raise ValueError("instruction is negated or ambiguous for the requested Phase 8 operation")
