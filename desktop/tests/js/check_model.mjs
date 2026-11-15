// The desktop reads answers as the CLI and the TUI do: the same conditions, the same walk over run.list's
// pages, and nothing said about delivery that the answer does not say.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LABELS, LIST_FOLLOW_MAX_PAGES, LIST_INCOMPLETE, deliveryNote, keyedConditions, listAllRuns,
} from "../../ui/js/model.js";
import { fixture } from "./support.mjs";

test("each answer shows the conditions the TUI shows, label, subject and text", () => {
  const doc = fixture("conditions.json");
  assert.deepEqual([...LABELS], doc.labels);
  for (const vector of doc.vectors) assert.deepEqual(keyedConditions(vector.answer), vector.conditions, vector.name);
});

test("each walk makes the CLI's calls and shows the CLI's list", async () => {
  for (const vector of fixture("walks.json").vectors) {
    const calls = [];
    const call = async (method, params) => {
      calls.push([method, { ...params }]);
      return structuredClone(vector.pages[calls.length - 1]);
    };
    assert.deepEqual(await listAllRuns(call), vector.result, vector.name);
    assert.deepEqual(calls, vector.calls, vector.name);
  }
});

test("a walk that never ends stops at the TUI's page ceiling and says the list is incomplete", async () => {
  // Every page names one new run and a next page; the stand-in stops offering pages a little past the ceiling,
  // so a walk without the ceiling ends with a whole list instead of hanging.
  let calls = 0;
  const call = async (method, params) => {
    calls += 1;
    const runId = `run-${String(calls).padStart(8, "0")}`;
    if (calls > LIST_FOLLOW_MAX_PAGES + 5) return { code: "ok", runs: [] };
    assert.equal(params.after, calls === 1 ? undefined : `run-${String(calls - 1).padStart(8, "0")}`);
    return { code: "ok", runs: [{ run_id: runId }], next_after: runId };
  };
  assert.equal(LIST_FOLLOW_MAX_PAGES, 100000);
  assert.deepEqual(await listAllRuns(call), { code: LIST_INCOMPLETE, detail: "too_many_pages", pages: LIST_FOLLOW_MAX_PAGES });
  assert.equal(calls, LIST_FOLLOW_MAX_PAGES);
});

test("the delivery note follows the answer's delivered value and nothing else", () => {
  for (const { case: name, answer } of fixture("delivery.json").cases) {
    const note = deliveryNote(answer);
    if (answer.code !== "control_api_unavailable") assert.equal(note, null, name);
    else if (answer.delivered === false) assert.match(note, /^not delivered/, name);
    else if (answer.delivered === "unknown") assert.match(note, /^delivery unknown/, name);
    else assert.match(note, /^refused by the daemon/, name);
  }
});

test("a Stop that was not answered is never shown as stopped", () => {
  const never = fixture("delivery.json").cases.find((c) => c.case === "never_answered").answer;
  const shown = keyedConditions(never).map(([, , text]) => text).join(" ");
  assert.doesNotMatch(shown, /\bstopped\b/);
  assert.match(deliveryNote(never), /may have been carried out/);
});
