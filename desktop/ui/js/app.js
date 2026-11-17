// The desktop window: wires the views to the actions through the transport. In this build the transport is
// inert, so the window shows what it would do and that nothing was sent. It registers no handler for closing:
// closing the window ends observation and sends nothing, and every run keeps going.
//
// startWindow takes the document and the transport, so desktop/tests/js/check_app.mjs runs this very module
// against a stand-in of index.html and a recording transport. The page calls it once, on the last line.

import { ActionError, HELD, makeActions } from "./actions.js";
import { listAllRuns } from "./model.js";
import { field } from "./sanitize.js";
import { inertTransport, RefusedHere, send } from "./transport.js";
import {
  applyLayout, applyTheme, renderApproval, renderApprovalList, renderConditions, renderPlugins, renderRoutes,
  renderRuns, renderSession, renderSessions, renderTranscript,
} from "./views.js";

export function startWindow(doc, transport, actions = makeActions(), appearance = null) {
  const $ = (id) => doc.getElementById(id);
  // Operator documents only. This build does not load a plugin theme or a plugin layout, and it fetches nothing.
  const chosen = appearance !== null && typeof appearance === "object" && !Array.isArray(appearance) ? appearance : null;
  applyTheme($("workspace"), chosen === null ? null : chosen.theme ?? null);
  applyLayout($("workspace"), chosen === null ? null : chosen.layout ?? null, {
    knownPanels: chosen !== null && Array.isArray(chosen.panels) ? chosen.panels : undefined,
  });

  // The status line shows the local refusal ("Not sent: ...") of one call of one action. Each call is
  // numbered. A newer call of the same action makes that refusal obsolete and takes it off the line when it
  // begins, and an older call's refusal that arrives after a newer call of its action began is not shown. A
  // call never clears or shows another action's refusal.
  let callCount = 0;
  const latestCall = new Map();
  let shownAction = null;

  function say(text, action) {
    $("status-line").textContent = text;
    shownAction = action;
  }

  // A call that is not sent answers null and says why in the status line. `refused`, when given, receives
  // that reason for this call alone ({ text }): the status line is shared, so a caller with several calls in
  // flight must not read it back.
  async function ask(action, input = {}, refused = null) {
    const call = ++callCount;
    latestCall.set(action, call);
    if (shownAction === action) say("", null);
    const notSent = (err) => {
      const text = `Not sent: ${err.message}.`;
      if (latestCall.get(action) === call) say(text, action);
      if (refused !== null) refused.text = text;
      return null;
    };
    let built;
    try {
      built = actions[action](input);
    } catch (err) {
      if (err instanceof ActionError) return notSent(err);
      throw err;
    }
    try {
      return await send(transport, action, built);
    } catch (err) {
      if (err instanceof RefusedHere) return notSent(err);
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

  function askToStop(action, input, text, label) {
    pendingStop = { action, input };
    $("stop-confirm-text").textContent = text;
    $("stop-confirm-yes").textContent = label;
    $("stop-confirm").hidden = false;
  }

  function onRunAction(action, input) {
    if (action === "run.stop") {
      askToStop(action, input, `Stop run ${input.run_id}? The run is asked to stop through the control API.`, "Stop the run");
      return;
    }
    return ask(action, input).then((answer) => answer && renderConditions(doc, $("runs-conditions"), answer, "run.status"));
  }

  function onSessionAction(action, input) {
    if (action === "session.stop") {
      askToStop(action, input, `Stop session ${input.session_id}? It is asked to stop through the control API.`,
        "Stop the session");
      return;
    }
    return ask(action, input).then((answer) => {
      if (!answer) return;
      renderConditions(doc, $("sessions-conditions"), answer, "session.status");
      renderSession(doc, $("session-detail"), answer);
    });
  }

  // A Stop's answer is drawn in the bar its confirmation is in (#stop-answers), never in a view's own box. The confirmation
  // is shared by every view, so a Stop can be confirmed, or its answer can arrive, while another view is
  // shown; a view's box would then be hidden, and the view's next refresh would replace it. Each answer stays,
  // newest first, until the operator clears them, and a Stop that was not sent says why there as well, so no
  // Stop's outcome is dropped without a word.
  function showStopAnswer(asked, answer, refusal) {
    const what = asked.action === "run.stop" ? `run ${field(asked.input.run_id)}` : `session ${field(asked.input.session_id)}`;
    const entry = doc.createElement("div");
    entry.setAttribute("class", "stop-answer");
    const head = doc.createElement("p");
    const box = doc.createElement("div");
    if (refusal !== undefined) head.textContent = `Stop of ${what}: ${refusal}`;
    else {
      const code = answer !== null && typeof answer === "object" && !Array.isArray(answer) ? answer.code : null;
      head.textContent = `Stop of ${what}, answer code ${field(code)}`;
      renderConditions(doc, box, answer, asked.action);
    }
    entry.appendChild(head);
    entry.appendChild(box);
    const list = $("stop-answers-list");
    list.replaceChildren(entry, ...list.childNodes);
    $("stop-answers").hidden = false;
  }

  function clearStopAnswers() {
    $("stop-answers-list").replaceChildren();
    $("stop-answers").hidden = true;
  }

  async function confirmStop() {
    const asked = pendingStop;
    pendingStop = null;
    $("stop-confirm").hidden = true;
    if (asked === null) return;
    const refused = {};
    const answer = await ask(asked.action, asked.input, refused);
    showStopAnswer(asked, answer, refused.text);
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
    if (answer) {
      renderConditions(doc, $("start-conditions"), answer, "run.start");
      acknowledgeStart(answer);
    }
  }

  // A started run says so where the start was asked: its answer has no condition to show, and an operator who
  // sees nothing may start it again.
  function acknowledgeStart(answer) {
    if (answer.code !== "started") return;
    const run = answer.run !== null && typeof answer.run === "object" ? answer.run : {};
    const item = doc.createElement("li");
    item.setAttribute("data-label", "STARTED");
    const label = doc.createElement("strong");
    label.textContent = "STARTED";
    item.appendChild(label);
    let words = ` run=${field(run.run_id)} started`;
    if (Object.prototype.hasOwnProperty.call(answer, "duplicate")) words += ` duplicate=${field(answer.duplicate)}`;
    item.appendChild(doc.createTextNode(words));
    $("start-conditions").children[0].appendChild(item);
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
  $("stop-answers-clear").addEventListener("click", clearStopAnswers);
  $("start-run").addEventListener("click", startRun);
  renderApproval(doc, $("core-approval"), null, { held: HELD["approval.answer"] });
  return show("runs");
}

startWindow(document, inertTransport());
