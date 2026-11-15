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
