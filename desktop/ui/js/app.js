// The desktop window: wires the views to the actions through the transport. In this build the transport is
// inert, so the window shows what it would do and that nothing was sent. It registers no handler for closing:
// closing the window ends observation and sends nothing, and every run keeps going.
//
// startWindow takes the document and the transport, so desktop/tests/js/check_app.mjs runs this very module
// against a stand-in of index.html and a recording transport. The page calls it once, on the last line.

import { ActionError, HELD, makeActions } from "./actions.js";
import { listAllRuns } from "./model.js";
import { inertTransport, RefusedHere, send } from "./transport.js";
import {
  renderApproval, renderApprovalList, renderConditions, renderPlugins, renderRoutes, renderRuns, renderSession,
  renderSessions, renderTranscript,
} from "./views.js";

export function startWindow(doc, transport, actions = makeActions()) {
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

  // A Stop is asked for only after the operator confirms it here; nothing is sent before. The confirmation
  // sits in the core bar every view shares (index.html), so it shows whichever view asked.
  let pendingStop = null;

  function askToStop(action, input, text, label, conditionsBox) {
    pendingStop = { action, input, conditionsBox };
    $("stop-confirm-text").textContent = text;
    $("stop-confirm-yes").textContent = label;
    $("stop-confirm").hidden = false;
  }

  function onRunAction(action, input) {
    if (action === "run.stop") {
      askToStop(action, input, `Stop run ${input.run_id}? The run is asked to stop through the control API.`, "Stop the run",
        "runs-conditions");
      return;
    }
    return ask(action, input).then((answer) => answer && renderConditions(doc, $("runs-conditions"), answer, "run.status"));
  }

  function onSessionAction(action, input) {
    if (action === "session.stop") {
      askToStop(action, input, `Stop session ${input.session_id}? It is asked to stop through the control API.`, "Stop the session",
        "sessions-conditions");
      return;
    }
    return ask(action, input).then((answer) => {
      if (!answer) return;
      renderConditions(doc, $("sessions-conditions"), answer, "session.status");
      renderSession(doc, $("session-detail"), answer);
    });
  }

  async function confirmStop() {
    const asked = pendingStop;
    pendingStop = null;
    $("stop-confirm").hidden = true;
    if (asked === null) return;
    const answer = await ask(asked.action, asked.input);
    const method = asked.action === "run.stop" ? "run.stop" : "session.stop";
    if (answer) renderConditions(doc, $(asked.conditionsBox), answer, method);
  }

  function cancelStop() {
    pendingStop = null;
    $("stop-confirm").hidden = true;
  }

  async function startRun() {
    const input = {
      tenant: $("start-tenant").value,
      engine: $("start-engine").value,
      prompt: $("start-prompt").value,
      client_key: $("start-client-key").value || undefined,
    };
    const answer = await ask("run.start", input);
    if (answer) renderConditions(doc, $("start-conditions"), answer, "run.start");
  }

  async function refreshStart() {
    const answer = await ask("routes.refresh");
    if (answer) {
      renderConditions(doc, $("start-conditions"), answer, "route.list");
      renderRoutes(doc, $("routes-list"), answer);
    }
  }

  async function refreshSessions() {
    const answer = await ask("sessions.refresh", { state: "live" });
    if (answer) {
      renderConditions(doc, $("sessions-conditions"), answer, "session.list");
      renderSessions(doc, $("sessions-list"), answer, onSessionAction);
    }
    renderSession(doc, $("session-detail"), null);
    renderTranscript(doc, $("transcript"), null);
  }

  async function refreshApprovals() {
    const answer = await ask("approvals.refresh", {});
    if (answer) {
      renderConditions(doc, $("approvals-conditions"), answer, "approval.list");
      renderApprovalList(doc, $("approvals-list"), answer);
    }
  }

  async function refreshPlugins() {
    const answer = await ask("plugins.refresh");
    if (answer) {
      renderConditions(doc, $("plugins-conditions"), answer, "plugin.list");
      renderPlugins(doc, $("plugins-list"), answer);
    }
  }

  const REFRESH = {
    runs: refreshRuns, start: refreshStart, sessions: refreshSessions, approvals: refreshApprovals, plugins: refreshPlugins,
  };

  function show(view) {
    for (const section of doc.querySelectorAll("main > section")) section.hidden = section.dataset.view !== view;
    for (const button of doc.querySelectorAll("nav button")) {
      if (button.dataset.view === view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    return REFRESH[view] ? REFRESH[view]() : undefined;
  }

  for (const button of doc.querySelectorAll("nav button")) button.addEventListener("click", () => show(button.dataset.view));
  $("runs-refresh").addEventListener("click", refreshRuns);
  $("stop-confirm-yes").addEventListener("click", confirmStop);
  $("stop-confirm-no").addEventListener("click", cancelStop);
  $("start-run").addEventListener("click", startRun);
  renderApproval(doc, $("core-approval"), null, { held: HELD["approval.answer"] });
  return show("runs");
}

startWindow(document, inertTransport());
