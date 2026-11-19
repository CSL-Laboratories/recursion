"""The inputs and the record of the canonical writers compared (schemas/canonical/).

Five writers of canonical JSON live on master, in two forms:

* W1 ``csl-cj/v0``: ``recursion_core.plugin_security.canonical.canonical`` and its reader ``parse``;
* W2 ``csl-cj/v0``: ``canonical_v0`` in ``conformance/rules.py``;
* W3 ``rfc8785/v1``: ``text_of`` in ``conformance/rules.py``, over what its ``read_strict`` reads;
* W4 ``rfc8785/v1``: ``recursion_core.control.methods.canonical_text``;
* W5 no form named: ``canonical_json`` in ``eval/record.py``.

This module builds the inputs (the 50 vectors of research prompt 157, kept as data in
``schemas/canonical/vectors/rprompt157.json``, and a corpus made here from a set seed, kept in
``schemas/canonical/vectors/corpus.json``), runs every input through every writer and reader that
takes it, and makes the record kept in ``schemas/canonical/results/``. It only measures: nothing here
decides which bytes are right. ``conformance/`` and ``eval/`` are loaded by file path and never edited.

How an input reaches each writer:

* W1 ``parse`` gets the input's bytes as they are.
* W3 gets the input's text (its bytes read as UTF-8); ``read_strict`` reads it and ``text_of`` writes
  what it read. Bytes that are not UTF-8 have no text (``no_text:not_utf8``).
* W1 ``canonical``, W2, W4 and W5 take a value, not text. Their value is what the standard library's
  ``json.loads`` gives for the text, with a key named twice refused: an object with a duplicate key has
  no value (``no_value:duplicate_key``), as text that is not JSON (``no_value:not_json``) or nests past
  the reader (``no_value:too_deep``) has none.

What is recorded per input and per writer is one string: ``hex:<the bytes in hex>``, ``refused:<code>``
(with the writer's detail in brackets where it gives one), ``error:<exception class>`` for a writer
that raises something other than its refusal, ``no_value:...`` or ``no_text:...``, ``=input`` when the
bytes are the input's own bytes, or ``=<writer>`` when the result is the same as that of a writer
named earlier in ``ORDER``.

Write the corpus and the record again (from the repository root, ``core`` on PYTHONPATH)::

    python -m recursion_core.plugin_security.tests.canonical_record --corpus schemas/canonical/vectors/corpus.json
    python -m recursion_core.plugin_security.tests.canonical_record --results schemas/canonical/results/master-<head>.json --head <head>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
VECTORS = ROOT / "schemas" / "canonical" / "vectors" / "rprompt157.json"
CORPUS = ROOT / "schemas" / "canonical" / "vectors" / "corpus.json"
RESULTS = ROOT / "schemas" / "canonical" / "results"
SEED = 20261118
RANDOM_DOUBLES = 2000
RANDOM_VALUES = 20000
ORDER = ("W3", "W4", "W1", "W1p", "W2", "W5")
DEL_LISTING = "expected difference: U+007F, refused by #577"
HARNESS_LISTING = ("a harness effect, not a writer's: Python's digit limit makes the harness's own json.loads give "
                   "no_value:not_json to W1, W2, W4 and W5")
# The two vectors research prompt 157 gives by a formula instead of input_hex, as it writes them.
FORMULAS = {
    "b'['*150 + b'0' + b']'*150": b"[" * 150 + b"0" + b"]" * 150,
    "b'{\"a\":'*1100 + b'0' + b'}'*1100": b'{"a":' * 1100 + b"0" + b"}" * 1100,
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _writers() -> dict:
    from recursion_core.control import methods
    # By module path: the package itself names a function ``canonical`` that hides the module.
    cj = importlib.import_module("recursion_core.plugin_security.canonical")
    from recursion_core.plugin_security.errors import Refused as CjRefused

    rules = _load("canonical_record_conformance_rules", ROOT / "conformance" / "rules.py")
    record = _load("canonical_record_eval_record", ROOT / "eval" / "record.py")

    def refused_cj(exc) -> str:
        return f"refused:{exc.code}" + (f" ({exc.detail})" if exc.detail else "")

    def w1(value):
        try:
            return cj.canonical(value)
        except CjRefused as exc:
            return refused_cj(exc)

    def w1p(raw):
        try:
            return cj.canonical(cj.parse(raw))
        except CjRefused as exc:
            return refused_cj(exc)

    def w2(value):
        try:
            return rules.canonical_v0(value)
        except rules.Refused as exc:
            return f"refused:{exc.code}"

    def w3(text):
        try:
            return rules.text_of(rules.read_strict(text)).encode("utf-8")
        except rules.Refused as exc:
            return f"refused:{exc.code}"

    def w4(value):
        try:
            return methods.canonical_text(value).encode("utf-8")
        except methods.NoCanonicalText as exc:
            return f"refused:{exc}"

    def w5(value):
        return record.canonical_json(value)

    return {"W1": w1, "W1p": w1p, "W2": w2, "W3": w3, "W4": w4, "W5": w5}


WRITERS: dict = {}       # filled on first use; a check may put a scratch writer in its place


def writers() -> dict:
    if not WRITERS:
        WRITERS.update(_writers())
    return WRITERS


# ------------------------------------------------------------------------------------------- inputs
def vector_lines(path: Path = VECTORS) -> list:
    """The 50 vector lines of the file, as they stand in research prompt 157 (line ends aside)."""
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line.startswith('  {"id":"V')]


def vectors(path: Path = VECTORS) -> list:
    """(id, input bytes, the vector itself) for V01 to V50."""
    out = []
    for item in json.loads(path.read_text(encoding="utf-8"))["vectors"]:
        raw = bytes.fromhex(item["input_hex"]) if "input_hex" in item else FORMULAS[item["input_formula"]]
        out.append((item["id"], raw, item))
    return out


def _double(rng: random.Random) -> float:
    while True:
        value = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if value == value and value not in (float("inf"), float("-inf")):
            return value


def _text(value, ascii_only: bool) -> str:
    return json.dumps(value, ensure_ascii=ascii_only, separators=(",", ":"))


_ASCII = [chr(i) for i in range(0x20, 0x7F)] * 3 + [chr(i) for i in range(0x20)] + ["\x7f"]


def _random_value(rng: random.Random, depth: int = 0):
    """A value in the csl-cj/v0 domain: ASCII text (controls and U+007F included), whole numbers within
    2^53 - 1, true, false, null, arrays and objects with ASCII keys, nesting kept small."""
    kind = rng.randrange(8 if depth < 3 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        edge = rng.randrange(6)
        if edge == 0:
            return rng.choice((0, 1, -1, 2**53 - 1, -(2**53 - 1), 2**31, -(2**31), 2**32))
        return rng.randrange(-(2**53 - 1), 2**53) if edge == 1 else rng.randrange(-1000, 1000)
    if kind in (3, 4):
        return "".join(rng.choice(_ASCII) for _ in range(rng.randrange(9)))
    if kind == 5:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    out = {}
    for _ in range(rng.randrange(5)):
        out["".join(rng.choice(_ASCII) for _ in range(rng.randrange(1, 6)))] = _random_value(rng, depth + 1)
    return out


def build_corpus() -> dict:
    """The generated corpus: the same list on every run and every interpreter (set seed, no network)."""
    rng = random.Random(SEED)
    items = []

    def add(group: str, text: str) -> None:
        items.append({"id": f"C{len(items) + 1:05d}", "group": group, "text": text})

    # Accented text, raw UTF-8 and escaped, as a value and as a key.
    words = [
        "\u00e0\u00e1\u00e2\u00e3\u00e4\u00e5\u00e6\u00e7\u00e8\u00e9\u00ea\u00eb\u00ec\u00ed\u00ee\u00ef\u00f0\u00f1\u00f2\u00f3\u00f4\u00f5\u00f6\u00f8\u00f9\u00fa\u00fb\u00fc\u00fd\u00fe\u00ff", "\u00c0\u00c1\u00c2\u00c3\u00c4\u00c5\u00c6\u00c7\u00c8\u00c9\u00ca\u00cb\u00cc\u00cd\u00ce\u00cf\u00d0\u00d1\u00d2\u00d3\u00d4\u00d5\u00d6\u00d8\u00d9\u00da\u00db\u00dc\u00dd\u00de\u00df", "caf\u00e9", "cafe\u0301",
        "\u00c5ngstr\u00f6m", "A\u030angstro\u0308m", "\u1e9b\u0323", "\u017f\u0323\u0307",
        "\u03b1\u03b2\u03b3\u03b4\u03b5 \u03a9\u03bc\u03ad\u03b3\u03b1 \u03ac", "\u03b1\u0301", "\u6f22\u5b57 \u304b\u306a \u30ab\u30ca \ud55c\uae00", "\U0001f600", "a\U0001f600b",
        "\u2028", "\u2029", "line\u2028para\u2029end", "\x7f", "a\x7fb",
    ] + [chr(i) for i in range(0x20)] + ["".join(chr(i) for i in range(0x20))]
    for word in words:
        for ascii_only in (False, True):
            add("accented", _text({"s": word}, ascii_only))
            add("accented", _text({word: 1}, ascii_only))
    # Keys above U+FFFF beside keys from U+E000 to U+FFFF (UTF-16 order and code point order part here).
    for pair in (("\U0001f600", "\ue000"), ("\U00010000", "\uffff"), ("\U0001f600", "\ufb33"), ("\U0010ffff", "\uffff"),
                 ("\U0001d11e", "\uf8ff"), ("\U00010000", "\ue000", "\ud7ff")):
        for ascii_only in (False, True):
            add("astral_keys", _text({key: index for index, key in enumerate(pair)}, ascii_only))
            add("astral_keys", _text({key: index for index, key in enumerate(reversed(pair))}, ascii_only))
    # Fractions: every number of call.profile p02, then the named cases, then random finite doubles.
    for token in ("1.0", "1e-7", "1E3", "-0.0", "0.1", "1.5e-300", "123456.789e3", "9007199254740991", "-12",
                  "100.50"):
        add("fractions_p02", token)
    for token in (repr(0.1 + 0.2), "1e-6", "1e-7", "1e21", "999999999999999900000", "5e-324",
                  "1.7976931348623157e308", "1424953923781206.25", "4.50", "4.9406564584124654e-324", "2e-1074",
                  "0.000001", "0.0000001", "1e+21", "1E-7", "123e-20"):
        add("fractions", token)
    for _ in range(RANDOM_DOUBLES):
        add("random_doubles", repr(_double(rng)))
    # Big numbers.
    for token in ("9007199254740991", "-9007199254740991", "9007199254740992", "-9007199254740992",
                  "9007199254740993", "-9007199254740993", "9223372036854775807", "-9223372036854775808",
                  "18446744073709551615", "18446744073709551616", "9007199254740993.0", "9007199254740991.0",
                  "1e20", "1e1000000", "-1e1000000"):
        add("big_numbers", token)
    # Minus zero in every spelling.
    for token in ("-0", "-0.0", "-0e0", "-0.0e+5", "-0E-0", "[-0]", "{\"z\":-0.0}"):
        add("minus_zero", token)
    # 20,000 random values in the csl-cj/v0 domain: every other one with its keys in the order drawn, the
    # rest with them sorted (so both the writers' sort and the reader's refusal of unsorted text are met).
    for index in range(RANDOM_VALUES):
        value = _random_value(rng)
        add("random_cj_v0", _text(value, True) if index % 2 else json.dumps(value, separators=(",", ":"), sort_keys=True))
    # Added after the rest (so every earlier input keeps its place and the seed's draws are the same):
    # the non-finite names as text, nesting from 33 to 65, and integers past Python's 4,300-digit limit.
    for token in ("NaN", "Infinity", "-Infinity", "[NaN]", '{"x":-Infinity}'):
        add("non_finite_text", token)
    for token in ("[" * 33 + "0" + "]" * 33, "[" * 32 + '{"a":1}' + "]" * 32, "[" * 64 + '{"a":1}' + "]" * 64,
                  "[" * 65 + "0" + "]" * 65):
        add("deep_33_to_65", token)
    for token in ("1" + "0" * 4300, "9" * 5000):
        add("long_integers", token)
    return {"generator": "core/recursion_core/plugin_security/tests/canonical_record.py", "seed": SEED,
            "count": len(items), "inputs": items}


def corpus(path: Path = CORPUS) -> list:
    """(id, input bytes, the corpus entry) for every input of the corpus file."""
    return [(item["id"], item["text"].encode("utf-8"), item) for item in json.loads(path.read_text(encoding="utf-8"))["inputs"]]


def _lines(data: dict, big: str) -> str:
    """JSON text with one top-level key per line and one entry of ``big`` per line (ASCII, so it diffs well)."""
    parts = []
    for key in sorted(data):
        value = data[key]
        if key != big:
            parts.append(json.dumps(key) + ": " + json.dumps(value, ensure_ascii=True, sort_keys=True))
        elif isinstance(value, dict):
            inner = ",\n".join(json.dumps(name) + ": " + json.dumps(item, ensure_ascii=True, sort_keys=True)
                               for name, item in value.items())
            parts.append(json.dumps(key) + ": {\n" + inner + "\n}")
        else:
            inner = ",\n".join(json.dumps(item, ensure_ascii=True, sort_keys=True) for item in value)
            parts.append(json.dumps(key) + ": [\n" + inner + "\n]")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def corpus_text(data: dict) -> str:
    return _lines(data, "inputs")


# --------------------------------------------------------------------------------------------- runs
def _no_duplicates(pairs):
    out = {}
    for key, item in pairs:
        if key in out:
            raise KeyError("duplicate_key")
        out[key] = item
    return out


def value_of(text):
    """The value the value writers take, or the reason there is none."""
    if text is None:
        return None, "no_value:not_utf8"
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates), None
    except KeyError:
        return None, "no_value:duplicate_key"
    except RecursionError:
        return None, "no_value:too_deep"
    except ValueError:
        return None, "no_value:not_json"


def _outcome(result, raw: bytes) -> str:
    if isinstance(result, bytes):
        return "=input" if result == raw else "hex:" + result.hex()
    return result


def _guarded(call, arg) -> str:
    try:
        return call(arg)
    except Exception as exc:          # a writer that raises past its own refusal: recorded, never hidden
        return f"error:{type(exc).__name__}"


def holds_del(value) -> bool:
    """Whether a text or key anywhere in the value holds U+007F (walked without recursion: V49 nests 1100 deep)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x7f" in item:
                return True
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
    return False


def run_one(raw: bytes) -> dict:
    """Every writer's outcome for one input, unabbreviated."""
    table = writers()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    value, missing = value_of(text)
    out = {"W1p": _outcome(_guarded(table["W1p"], raw), raw)}
    out["W3"] = "no_text:not_utf8" if text is None else _outcome(_guarded(table["W3"], text), raw)
    for name in ("W1", "W2", "W4", "W5"):
        out[name] = missing if missing else _outcome(_guarded(table[name], value), raw)
    out["_del"] = bool(not missing and holds_del(value))
    return out


def _bytes_of(outcome: str, raw: bytes):
    if outcome == "=input":
        return raw
    if outcome.startswith("hex:"):
        return bytes.fromhex(outcome[4:])
    return None


def abbreviated(full: dict) -> dict:
    out = {}
    for index, name in enumerate(ORDER):
        same = next((earlier for earlier in ORDER[:index] if full[earlier] == full[name]), None)
        out[name] = "=" + same if same else full[name]
    return out


PAIRS = (("W1", "W2"), ("W3", "W4"), ("W1", "W3"), ("W1", "W4"), ("W2", "W3"), ("W1", "W5"), ("W3", "W5"),
         ("W4", "W5"), ("W2", "W5"))


def kind_of(a: str, b: str) -> str:
    """How two outcomes of one input part: both bytes and different, or only one gives bytes."""
    ab, bb = a.startswith(("hex:", "=input")), b.startswith(("hex:", "=input"))
    if ab and bb:
        return "different bytes"
    if ab:
        return "only the first gives bytes"
    if bb:
        return "only the second gives bytes"
    return ""


def run_all(inputs: list) -> dict:
    """The full outcome of every input, keyed by id, plus what the named checks read."""
    full = {}
    for ident, raw, _ in inputs:
        full[ident] = run_one(raw)
    return full


def checks(full: dict, inputs: list) -> dict:
    """The four named checks over a run: the inputs that break each (empty when it holds), and the
    U+007F listing of check (c)."""
    raws = {ident: raw for ident, raw, _ in inputs}
    a, b, c, listed = [], [], [], []
    for ident, out in full.items():
        raw = raws[ident]
        if not _same_or_both_refuse(out["W1"], out["W2"], raw):
            a.append(ident)
        if not _same_or_both_refuse(out["W3"], out["W4"], raw):
            b.append(ident)
        w1 = _bytes_of(out["W1"], raw)
        if w1 is not None:
            if out["_del"]:
                listed.append(ident)
            elif w1 != _bytes_of(out["W3"], raw):
                c.append(ident)
    return {"a": a, "b": b, "c": c, "del_listed": listed}


def _same_or_both_refuse(x: str, y: str, raw: bytes) -> bool:
    if x.startswith("error:") or y.startswith("error:"):
        return False                  # a writer that raises neither refuses nor gives bytes: the check fails
    bx, by = _bytes_of(x, raw), _bytes_of(y, raw)
    return bx == by


def over_digit_limit(raw: bytes) -> bool:
    """Whether the input is an integer longer than the interpreter lets ``int`` read from text."""
    limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else 0
    digits = raw.strip().lstrip(b"-")
    return bool(limit) and digits.isdigit() and len(digits) > limit


def differences(full: dict) -> dict:
    """For each pair of writers, the inputs on which they part, by kind (the record names them all)."""
    out = {}
    for first, second in PAIRS:
        kinds: dict = {}
        for ident, item in full.items():
            if item[first] == item[second]:
                continue
            kind = kind_of(item[first], item[second])
            if kind:
                kinds.setdefault(kind, []).append(ident)
        out[f"{first} {second}"] = kinds
    # The reader W1p takes exactly the bytes W1 writes for their own value; any other input it refuses.
    out["W1p takes other than the bytes W1 writes"] = [
        ident for ident, item in full.items() if (item["W1p"] == "=input") != (item["W1"] == "=input")]
    return out


def record(inputs: list, head: str, vectors_commit: str) -> dict:
    full = run_all(inputs)
    found = checks(full, inputs)
    return {
        "about": "What every canonical writer on master gives for each input; see "
                 "core/recursion_core/plugin_security/tests/canonical_record.py for how an input reaches each "
                 "writer and how an outcome is written. A record, not a judgement: no outcome here is called right.",
        "master": head,
        "vectors": {"file": "schemas/canonical/vectors/rprompt157.json", "csl_research_commit": vectors_commit},
        "corpus": {"file": "schemas/canonical/vectors/corpus.json", "seed": SEED},
        "order": list(ORDER),
        "checks": {"a W1 W2 same bytes or both refuse": found["a"], "b W3 W4 same bytes or both refuse": found["b"],
                   "c W1 equals W3 where W1 gives bytes, U+007F aside": found["c"]},
        DEL_LISTING: found["del_listed"],
        HARNESS_LISTING: [ident for ident, raw, _ in inputs
                          if full[ident]["W1"] == "no_value:not_json" and over_digit_limit(raw)],
        "differences": differences(full),
        "outcomes": {ident: abbreviated(item) for ident, item in full.items()},
    }


def record_text(data: dict) -> str:
    return _lines(data, "outcomes")


def all_inputs() -> list:
    return vectors() + corpus()


def vectors_commit() -> str:
    return json.loads(VECTORS.read_text(encoding="utf-8"))["source"]["commit"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", help="write the generated corpus to this file")
    parser.add_argument("--results", help="write the record of a fresh run to this file")
    parser.add_argument("--head", help="the master commit the record is of (with --results)")
    args = parser.parse_args(argv)
    if args.corpus:
        Path(args.corpus).write_text(corpus_text(build_corpus()), encoding="utf-8", newline="\n")
    if args.results:
        if not args.head:
            parser.error("--results needs --head")
        Path(args.results).write_text(record_text(record(all_inputs(), args.head, vectors_commit())),
                                      encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
