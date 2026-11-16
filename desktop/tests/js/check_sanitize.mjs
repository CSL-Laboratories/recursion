// Engine text reaches the window as the TUI shows it: every control and invisible character a visible marker.

import assert from "node:assert/strict";
import { test } from "node:test";

import { OutputSanitizer, field, pyStr, sanitize } from "../../ui/js/sanitize.js";
import { fixture } from "./support.mjs";

test("each text is shown as the TUI shows it", () => {
  for (const vector of fixture("sanitize.json").vectors) {
    assert.equal(sanitize(vector.text), vector.sanitize, vector.name);
    assert.equal(field(vector.text), vector.field, vector.name);
  }
});

test("splitting a text anywhere gives the same result", () => {
  for (const vector of fixture("sanitize.json").vectors) {
    const chars = [...vector.text];
    for (const cut of [1, 2, 3, Math.floor(chars.length / 2), chars.length - 1]) {
      if (cut <= 0 || cut >= chars.length) continue;
      const s = new OutputSanitizer();
      const got = s.feed(chars.slice(0, cut).join("")) + s.feed(chars.slice(cut).join("")) + s.flush();
      assert.equal(got, vector.sanitize, `${vector.name} cut at ${cut}`);
    }
  }
});

test("values that are not text read as Python's str() of them", () => {
  assert.equal(pyStr(null), "None");
  assert.equal(pyStr(undefined), "None");
  assert.equal(pyStr(true), "True");
  assert.equal(pyStr(5), "5");
  assert.equal(pyStr({ a: [1, "b"] }), "{'a': [1, 'b']}");
});

// fixtures/pystr.json is written by Python and checked against str() and the TUI's field() by check_vectors.py.
test("a parsed answer value reads as Python's str() of it: floats, nested text, quotes and escapes", () => {
  for (const vector of fixture("pystr.json").vectors) {
    const value = JSON.parse(vector.json);
    assert.equal(pyStr(value), vector.str, vector.name);
    assert.equal(field(value), vector.field, vector.name);
  }
});

test("where JSON.parse has dropped what Python reads, the desktop shows what the fixture names, not Python's text", () => {
  // 2 and 2.0 are one number after JSON.parse, so the desktop cannot know a whole float was a float: this is
  // the known difference, named in fixtures/pystr.json "lost", not a reading of Python's rule.
  const lost = fixture("pystr.json").lost;
  assert.ok(lost.some((v) => v.json === "2.0"));
  for (const vector of lost) {
    assert.equal(pyStr(JSON.parse(vector.json)), vector.desktop, vector.name);
    assert.notEqual(vector.desktop, vector.python, vector.name);
  }
});

test("a whole number reads as its integer digits whatever its size: no float suffix, no exponent", () => {
  // Devin's second thread on #390: 9007199254740992 is held exactly and must not read "9007199254740992.0".
  assert.equal(pyStr(JSON.parse("9007199254740992")), "9007199254740992");
  assert.equal(pyStr(2 ** 53 + 2), "9007199254740994");
  assert.equal(pyStr(JSON.parse("1e21")), "1000000000000000000000");
  assert.equal(pyStr(-(2 ** 70)), "-1180591620717411303424");
  assert.equal(field({ revision: 2 ** 53 }), "{'revision': 9007199254740992}");
});

test("numbers no JSON text can carry still read as Python writes them", () => {
  assert.equal(pyStr(NaN), "nan");
  assert.equal(pyStr(Infinity), "inf");
  assert.equal(pyStr(-Infinity), "-inf");
});
