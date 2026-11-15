// The window itself: ui/js/app.js run against a stand-in of ui/index.html. Loading the page starts the window
// on the inert transport; a Stop is sent only after the operator confirms it, and a cancelled one never; a
// start sends the CLI's request, and refused input sends nothing. No browser: the stand-in document is
// support.mjs's, and the transport records what would have gone to the proxy.

import assert from "node:assert/strict";
import { test } from "node:test";

import { INERT_DETAIL, replayTransport } from "../../ui/js/transport.js";
import { APPROVAL_EMPTY } from "../../ui/js/views.js";
import { fixture, windowDocument } from "./support.mjs";

// The page's own start: app.js's last line runs against this document when the module loads.
const loaded = windowDocument();
globalThis.document = loaded;
const { startWindow } = await import("../../ui/js/app.js");

const ANSWERS = fixture("answers.json");
const recorded = (file) => structuredClone(ANSWERS.answers.find((a) => a.file === file).answer);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const text = (doc, id) => doc.getElementById(id).textContent;
const sent = (transport, method) => transport.calls.filter((c) => c.method === method);

function views(doc) {
  return doc.querySelectorAll("main > section").filter((s) => !s.hidden).map((s) => s.dataset.view);
}

test("loading the page starts the window: the runs view, the core's empty approval region, nothing sent", async () => {
  await settle();
  assert.deepEqual(views(loaded), ["runs"]);
  const current = loaded.querySelectorAll("nav button").filter((b) => b.getAttribute("aria-current") === "page");
  assert.deepEqual(current.map((b) => b.dataset.view), ["runs"]);
  assert.match(text(loaded, "runs-conditions"), /UNAVAILABLE control API unavailable: desktop_inert/);
  assert.match(text(loaded, "runs-conditions"), /not delivered/);
  assert.ok(text(loaded, "runs-conditions").includes(INERT_DETAIL));
  assert.ok(text(loaded, "core-approval").includes(APPROVAL_EMPTY));
  for (const button of loaded.querySelectorAll("nav button")) assert.equal(button.listeners.get("click")?.length, 1);
});

test("every view the navigation names exists, and choosing one shows it alone", async () => {
  const doc = windowDocument();
  const transport = replayTransport([]);
  await startWindow(doc, transport);
  const named = doc.querySelectorAll("nav button").map((b) => b.dataset.view);
  assert.deepEqual(doc.querySelectorAll("main > section").map((s) => s.dataset.view), named);
  for (const button of doc.querySelectorAll("nav button")) {
    await button.click();
    assert.deepEqual(views(doc), [button.dataset.view]);
  }
});

function runsWindow() {
  const list = recorded("c09-run-list-one-page.json");
  const runId = list.runs[0].run_id;
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: list },
    { method: "run.list", params: { after: list.next_after }, answer: { code: "ok", runs: [] } },
    { method: "run.stop", params: { run_id: runId }, answer: recorded("c12-run-stop-pending.json") },
  ]);
  return { doc: windowDocument(), transport, runId, list };
}

function button(doc, boxId, label) {
  const hit = doc.getElementById(boxId).find((n) => n.tagName === "BUTTON" && n.textContent === label);
  assert.ok(hit.length, `${boxId} has a ${label} button`);
  return hit[0];
}

test("a run's Stop asks first and sends nothing; cancelling sends nothing; confirming sends that run's Stop once", async () => {
  const { doc, transport, runId, list } = runsWindow();
  await startWindow(doc, transport);
  // The window reads every page, as the CLI's list does: the recorded page names a next one.
  assert.deepEqual(sent(transport, "run.list").map((c) => c.params), [{}, { after: list.next_after }]);
  const confirm = doc.getElementById("stop-confirm");
  assert.equal(confirm.hidden, true);

  await button(doc, "runs-table", "Stop").click();
  assert.equal(confirm.hidden, false);
  assert.match(text(doc, "stop-confirm-text"), new RegExp(`^Stop run ${runId}\\?`));
  assert.deepEqual(sent(transport, "run.stop"), []);

  await doc.getElementById("stop-confirm-no").click();
  assert.equal(confirm.hidden, true);
  await doc.getElementById("stop-confirm-yes").click();
  assert.deepEqual(sent(transport, "run.stop"), [], "a cancelled Stop is not sent by a later confirm");

  await button(doc, "runs-table", "Stop").click();
  await doc.getElementById("stop-confirm-yes").click();
  assert.equal(confirm.hidden, true);
  assert.deepEqual(sent(transport, "run.stop"), [{ method: "run.stop", params: { run_id: runId } }]);
  // The answer was pending: shown as not confirmed, never as stopped.
  assert.match(text(doc, "runs-conditions"), /UNCERTAIN stop not confirmed for run=/);
  await doc.getElementById("stop-confirm-yes").click();
  assert.equal(sent(transport, "run.stop").length, 1, "a second confirm sends nothing more");
});

test("a session's Stop asks first too, and sends that session's Stop only once confirmed", async () => {
  const list = recorded("h27-session-list-ok.json");
  const sessionId = list.sessions[0].session_id;
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: { code: "ok", runs: [] } },
    { method: "session.list", params: { state: "live" }, answer: list },
    { method: "session.stop", params: { command_id: "desktop-check.1", session_id: sessionId, scope: "self" },
      answer: recorded("h13-session-stop-of-a-subtree-still-pending.json") },
  ]);
  const doc = windowDocument();
  const { makeActions } = await import("../../ui/js/actions.js");
  await startWindow(doc, transport, makeActions(() => "desktop-check.1"));
  await doc.querySelectorAll("nav button").find((b) => b.dataset.view === "sessions").click();
  await button(doc, "sessions-list", "Stop").click();
  assert.match(text(doc, "stop-confirm-text"), new RegExp(`^Stop session ${sessionId}\\?`));
  assert.deepEqual(sent(transport, "session.stop"), []);
  await doc.getElementById("stop-confirm-yes").click();
  assert.equal(sent(transport, "session.stop").length, 1);
  assert.match(text(doc, "sessions-conditions"), /UNCERTAIN session\.stop for session=ses-7f3a9c2e51d04b6a not confirmed/);
  assert.match(text(doc, "sessions-conditions"), /reason=cgroup_populated: stop requested, not confirmed/);
});

test("a start sends the CLI's request from the form, and input the schema refuses sends nothing", async () => {
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: { code: "ok", runs: [] } },
    { method: "run.start", params: { tenant: "t1", engine: "codex", prompt: "hi" }, answer: recorded("c01-run-start-started.json") },
  ]);
  const doc = windowDocument();
  await startWindow(doc, transport);
  await doc.getElementById("start-run").click();
  assert.deepEqual(sent(transport, "run.start"), []);
  assert.match(text(doc, "status-line"), /^Not sent: tenant must be non-empty text\.$/);
  doc.getElementById("start-tenant").value = "t1";
  doc.getElementById("start-engine").value = "codex";
  doc.getElementById("start-prompt").value = "hi";
  await doc.getElementById("start-run").click();
  assert.deepEqual(sent(transport, "run.start"), [{ method: "run.start", params: { tenant: "t1", engine: "codex", prompt: "hi" } }]);
});
