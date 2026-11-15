// The window itself: ui/js/app.js run against a stand-in of ui/index.html. Loading the page starts the window
// on the inert transport; a Stop is sent only after the operator confirms it, the confirmation shows in
// whichever view asked, and a cancelled Stop is never sent; a start sends the CLI's request, and refused input
// sends nothing; Open, refresh and the Start view draw what the daemon answered. No browser: the stand-in
// document is support.mjs's, and the transport records what would have gone to the proxy. Every control is
// pressed only when it and everything around it is shown.

import assert from "node:assert/strict";
import { test } from "node:test";

import { makeActions } from "../../ui/js/actions.js";
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

// An operator can press only what is shown.
async function press(doc, node) {
  assert.ok(doc.isShown(node), `${node.getAttribute("id") ?? node.textContent} is pressed while it is not shown`);
  await node.click();
}

const byId = (doc, id) => doc.getElementById(id);
const nav = (doc, view) => doc.querySelectorAll("nav button").find((b) => b.dataset.view === view);

function button(doc, boxId, label) {
  const hit = byId(doc, boxId).find((n) => n.tagName === "BUTTON" && n.textContent === label);
  assert.ok(hit.length, `${boxId} has a ${label} button`);
  return hit[0];
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
  assert.equal(byId(loaded, "stop-confirm").hidden, true);
  for (const b of loaded.querySelectorAll("nav button")) assert.equal(b.listeners.get("click")?.length, 1);
});

test("every view the navigation names exists, and choosing one shows it alone", async () => {
  const doc = windowDocument();
  await startWindow(doc, replayTransport([]));
  const named = doc.querySelectorAll("nav button").map((b) => b.dataset.view);
  assert.deepEqual(doc.querySelectorAll("main > section").map((s) => s.dataset.view), named);
  for (const b of doc.querySelectorAll("nav button")) {
    await press(doc, b);
    assert.deepEqual(views(doc), [b.dataset.view]);
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

test("a run's Stop asks first, in view, and sends nothing; cancelling sends nothing; confirming sends that run's Stop once", async () => {
  const { doc, transport, runId, list } = runsWindow();
  await startWindow(doc, transport);
  // The window reads every page, as the CLI's list does: the recorded page names a next one.
  assert.deepEqual(sent(transport, "run.list").map((c) => c.params), [{}, { after: list.next_after }]);
  const confirm = byId(doc, "stop-confirm");
  assert.equal(confirm.hidden, true);

  await press(doc, button(doc, "runs-table", "Stop"));
  assert.ok(doc.isShown(byId(doc, "stop-confirm-yes")), "the confirmation shows in the runs view");
  assert.match(text(doc, "stop-confirm-text"), new RegExp(`^Stop run ${runId}\\?`));
  assert.equal(text(doc, "stop-confirm-yes"), "Stop the run");
  assert.deepEqual(sent(transport, "run.stop"), []);

  await press(doc, byId(doc, "stop-confirm-no"));
  assert.equal(confirm.hidden, true);
  await byId(doc, "stop-confirm-yes").click();
  assert.deepEqual(sent(transport, "run.stop"), [], "a cancelled Stop is not sent by a later confirm");

  await press(doc, button(doc, "runs-table", "Stop"));
  await press(doc, byId(doc, "stop-confirm-yes"));
  assert.equal(confirm.hidden, true);
  assert.deepEqual(sent(transport, "run.stop"), [{ method: "run.stop", params: { run_id: runId } }]);
  // The answer was pending: shown as not confirmed, never as stopped.
  assert.match(text(doc, "runs-conditions"), /UNCERTAIN stop not confirmed for run=/);
  await byId(doc, "stop-confirm-yes").click();
  assert.equal(sent(transport, "run.stop").length, 1, "a second confirm sends nothing more");
});

function sessionsWindow(extra = []) {
  const list = recorded("h27-session-list-ok.json");
  const sessionId = list.sessions[0].session_id;
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: { code: "ok", runs: [] } },
    { method: "session.list", params: { state: "live" }, answer: list },
    { method: "session.status", params: { session_id: sessionId }, answer: recorded("h28-session-status-ok.json") },
    { method: "session.stop", params: { command_id: "desktop-check.1", session_id: sessionId, scope: "self" },
      answer: recorded("h13-session-stop-of-a-subtree-still-pending.json") },
    ...extra,
  ]);
  return { doc: windowDocument(), transport, sessionId };
}

test("a session's Stop asks first, in the sessions view, and sends that session's Stop only once confirmed", async () => {
  const { doc, transport, sessionId } = sessionsWindow();
  await startWindow(doc, transport, makeActions(() => "desktop-check.1"));
  await press(doc, nav(doc, "sessions"));
  await press(doc, button(doc, "sessions-list", "Stop"));
  assert.ok(doc.isShown(byId(doc, "stop-confirm-yes")), "the confirmation shows in the sessions view");
  assert.match(text(doc, "stop-confirm-text"), new RegExp(`^Stop session ${sessionId}\\?`));
  assert.equal(text(doc, "stop-confirm-yes"), "Stop the session");
  assert.deepEqual(sent(transport, "session.stop"), []);
  await press(doc, byId(doc, "stop-confirm-no"));
  await byId(doc, "stop-confirm-yes").click();
  assert.deepEqual(sent(transport, "session.stop"), [], "a cancelled session Stop is not sent by a later confirm");
  await press(doc, button(doc, "sessions-list", "Stop"));
  await press(doc, byId(doc, "stop-confirm-yes"));
  assert.equal(sent(transport, "session.stop").length, 1);
  assert.match(text(doc, "sessions-conditions"), /UNCERTAIN session\.stop for session=ses-7f3a9c2e51d04b6a not confirmed/);
  assert.match(text(doc, "sessions-conditions"), /reason=cgroup_populated: stop requested, not confirmed/);
});

test("opening a session draws what session.status answered, and a refresh of the view clears it", async () => {
  const { doc, transport, sessionId } = sessionsWindow();
  await startWindow(doc, transport);
  await press(doc, nav(doc, "sessions"));
  assert.match(text(doc, "session-detail"), /^No session is open\.$/);
  await press(doc, button(doc, "sessions-list", "Open"));
  assert.deepEqual(sent(transport, "session.status"), [{ method: "session.status", params: { session_id: sessionId } }]);
  assert.match(text(doc, "session-detail"), new RegExp(`^${sessionId} live revision 7`));
  assert.match(text(doc, "session-detail"), /turn-000a ended outcome=done/);
  await press(doc, nav(doc, "runs"));
  await press(doc, nav(doc, "sessions"));
  assert.match(text(doc, "session-detail"), /^No session is open\.$/);
});

test("the start view draws the engines route.list answered", async () => {
  const routes = recorded("c15-route-list.json");
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: { code: "ok", runs: [] } },
    { method: "route.list", params: {}, answer: routes },
  ]);
  const doc = windowDocument();
  await startWindow(doc, transport);
  await press(doc, nav(doc, "start"));
  assert.equal(sent(transport, "route.list").length, 1);
  assert.equal(byId(doc, "routes-list").children.length, routes.routes.length);
  assert.match(text(doc, "routes-list"), /engine:codex not configured beta: route_not_configured/);
});

test("a start sends the CLI's request from the form, and input the schema refuses sends nothing", async () => {
  const transport = replayTransport([
    { method: "run.list", params: {}, answer: { code: "ok", runs: [] } },
    { method: "route.list", params: {}, answer: { code: "ok", routes: [] } },
    { method: "run.start", params: { tenant: "t1", engine: "codex", prompt: "hi" }, answer: recorded("c01-run-start-started.json") },
  ]);
  const doc = windowDocument();
  await startWindow(doc, transport);
  await press(doc, nav(doc, "start"));
  await press(doc, byId(doc, "start-run"));
  assert.deepEqual(sent(transport, "run.start"), []);
  assert.match(text(doc, "status-line"), /^Not sent: tenant must be non-empty text\.$/);
  byId(doc, "start-tenant").value = "t1";
  byId(doc, "start-engine").value = "codex";
  byId(doc, "start-prompt").value = "hi";
  await press(doc, byId(doc, "start-run"));
  assert.deepEqual(sent(transport, "run.start"), [{ method: "run.start", params: { tenant: "t1", engine: "codex", prompt: "hi" } }]);
});
