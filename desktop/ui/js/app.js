// The desktop window: wires the views to the actions through the transport. In this build the transport is
// inert, so the window shows what it would do and that nothing was sent. It registers no handler for closing:
// closing the window ends observation and sends nothing, and every run keeps going.

import { ActionError, HELD, makeActions } from "./actions.js";
import { listAllRuns } from "./model.js";
import { inertTransport, RefusedHere, send } from "./transport.js";
import {
  renderApproval, renderApprovalList, renderConditions, renderPlugins, renderRuns, renderTranscript,
} from "./views.js";

const doc = document;
const transport = inertTransport();
const actions = makeActions();
const $ = (id) => doc.getElementById(id);

function say(text) {
  $("status-line").textContent = text;
}

async function ask(action, input = {}) {
  let built;
  try {
    built = actions[action](input);
  } catch (err) {
    if (err instanceof ActionError) {
      say(`Not sent: ${err.message}.`);
      return null;
    }
    throw err;
  }
  try {
    return await send(transport, action, built);
  } catch (err) {
    if (err instanceof RefusedHere) {
      say(`Not sent: ${err.message}.`);
      return null;
    }
    throw err;
  }
}

async function refreshRuns() {
  const answer = await listAllRuns((method, params) =>
    ask(params.after === undefined ? "runs.refresh" : "runs.refresh.next", params));
  renderConditions(doc, $("runs-conditions"), answer);
  renderRuns(doc, $("runs-table"), answer, onRunAction);
}

let pendingStop = null;

function onRunAction(action, input) {
  if (action === "run.stop") {
    pendingStop = input.run_id;
    $("stop-confirm-text").textContent = `Stop run ${pendingStop}? The run is asked to stop through the control API.`;
    $("stop-confirm").hidden = false;
    return;
  }
  ask(action, input).then((answer) => answer && renderConditions(doc, $("runs-conditions"), answer));
}

async function confirmStop() {
  const runId = pendingStop;
  pendingStop = null;
  $("stop-confirm").hidden = true;
  if (runId === null) return;
  const answer = await ask("run.stop", { run_id: runId });
  if (answer) renderConditions(doc, $("runs-conditions"), answer);
}

async function startRun() {
  const input = {
    tenant: $("start-tenant").value,
    engine: $("start-engine").value,
    prompt: $("start-prompt").value,
    client_key: $("start-client-key").value || undefined,
  };
  const answer = await ask("run.start", input);
  if (answer) renderConditions(doc, $("start-conditions"), answer);
}

async function refreshSessions() {
  const answer = await ask("sessions.refresh", { state: "live" });
  if (answer) renderConditions(doc, $("sessions-conditions"), answer);
  renderTranscript(doc, $("transcript"), null);
}

async function refreshApprovals() {
  const answer = await ask("approvals.refresh", {});
  if (answer) {
    renderConditions(doc, $("approvals-conditions"), answer);
    renderApprovalList(doc, $("approvals-list"), answer);
  }
}

async function refreshPlugins() {
  const answer = await ask("plugins.refresh");
  if (answer) {
    renderConditions(doc, $("plugins-conditions"), answer);
    renderPlugins(doc, $("plugins-list"), answer);
  }
}

const REFRESH = { runs: refreshRuns, sessions: refreshSessions, approvals: refreshApprovals, plugins: refreshPlugins };

function show(view) {
  for (const section of doc.querySelectorAll("main > section")) section.hidden = section.dataset.view !== view;
  for (const button of doc.querySelectorAll("nav button")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (REFRESH[view]) REFRESH[view]();
}

function boot() {
  for (const button of doc.querySelectorAll("nav button")) button.addEventListener("click", () => show(button.dataset.view));
  $("runs-refresh").addEventListener("click", refreshRuns);
  $("stop-confirm-yes").addEventListener("click", confirmStop);
  $("stop-confirm-no").addEventListener("click", () => {
    pendingStop = null;
    $("stop-confirm").hidden = true;
  });
  $("start-run").addEventListener("click", startRun);
  renderApproval(doc, $("core-approval"), null, { held: HELD["approval.answer"] });
  show("runs");
}

boot();
