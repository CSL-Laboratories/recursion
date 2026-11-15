// Drawing. Every value from the daemon, an engine or a plugin is set as text (textContent or a text node)
// after the sanitizer's rules; nothing here parses markup. Each function takes the document to draw with,
// so the checks can hand it a stand-in.
//
// The approval region is the core's: only renderApproval draws in it, only from the opened request's
// rendered lines, and no theme or panel reaches it (applyTheme writes to the workspace element alone).

import { field, sanitize } from "./sanitize.js";
import { conditionsFor, deliveryNote, stopUnconfirmed } from "./model.js";

function el(doc, tag, attrs = {}, text = undefined) {
  const node = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function isMapping(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// method: the method the answer is to (model.js conditionsFor); a run method's answer reads as on the CLI.
export function renderConditions(doc, box, answer, method = "run.list") {
  const items = conditionsFor(method, answer).map(([label, , text]) => [label, text]);
  const list = el(doc, "ul", { class: "conditions" });
  for (const [label, text] of items) {
    const item = el(doc, "li", { "data-label": label });
    item.appendChild(el(doc, "strong", {}, label));
    item.appendChild(doc.createTextNode(" " + text));
    list.appendChild(item);
  }
  // A refusal on the launch path names its reason in start_error (the CLI prints it within the whole answer).
  if (isMapping(answer) && isMapping(answer.start_error)) {
    list.appendChild(el(doc, "li", { "data-label": "START_ERROR" },
      `start error: ${field(answer.start_error.code)} ${field(answer.start_error.detail ?? "")}`.trim()));
  }
  const note = deliveryNote(answer);
  if (note) list.appendChild(el(doc, "li", { "data-label": "DELIVERY" }, note));
  box.replaceChildren(list);
  return items;
}

// onAction(name, input): the view asks; the app confirms a Stop before anything is sent.
export function renderRuns(doc, box, answer, onAction) {
  const runs = isMapping(answer) && Array.isArray(answer.runs) ? answer.runs.filter(isMapping) : [];
  const table = el(doc, "table", { class: "runs" });
  const head = el(doc, "tr");
  for (const name of ["Run", "State", "Engine", "Tenant", ""]) head.appendChild(el(doc, "th", { scope: "col" }, name));
  table.appendChild(head);
  for (const run of runs) {
    const row = el(doc, "tr");
    row.appendChild(el(doc, "td", {}, field(run.run_id)));
    const state = stopUnconfirmed(run) ? `${field(run.state)} (stop not confirmed)` : field(run.state);
    row.appendChild(el(doc, "td", {}, state));
    row.appendChild(el(doc, "td", {}, field(run.engine)));
    row.appendChild(el(doc, "td", {}, field(run.tenant)));
    const cell = el(doc, "td");
    const open = el(doc, "button", { type: "button" }, "Open");
    open.addEventListener("click", () => onAction("run.open", { run_id: run.run_id }));
    const stop = el(doc, "button", { type: "button", class: "stop" }, "Stop");
    stop.addEventListener("click", () => onAction("run.stop", { run_id: run.run_id }));
    cell.appendChild(open);
    cell.appendChild(stop);
    row.appendChild(cell);
    table.appendChild(row);
  }
  box.replaceChildren(runs.length ? table : el(doc, "p", { class: "empty" }, "No runs to show."));
  return runs.length;
}

export const FOLLOW_TIMED_OUT = "No new lines: the wait ended before anything was written.";
export const EPOCH_ADVANCED = "The daemon restarted since this place in the log was read; what was kept from before may be stale.";

export function renderTranscript(doc, list, answer) {
  const lines = isMapping(answer) && Array.isArray(answer.lines) ? answer.lines.filter(isMapping) : [];
  const items = lines.map((line) => {
    const trusted = line.trust === "trusted";
    const item = el(doc, "li", { class: "line", "data-trust": trusted ? "trusted" : "untrusted" });
    item.appendChild(el(doc, "span", { class: "meta" }, `${field(line.seq)} ${field(line.source)}`));
    if (!trusted) item.appendChild(el(doc, "span", { class: "tag" }, "untrusted"));
    item.appendChild(el(doc, "span", { class: "text" }, sanitize(String(line.text ?? ""))));
    return item;
  });
  if (isMapping(answer) && answer.epoch_advanced === true) items.unshift(el(doc, "li", { class: "note" }, EPOCH_ADVANCED));
  if (isMapping(answer) && answer.timed_out === true && !lines.length) items.push(el(doc, "li", { class: "note" }, FOLLOW_TIMED_OUT));
  list.replaceChildren(...items);
  return lines.length;
}

// session.list: one row per session, with Open and Stop going back to the app (which confirms a Stop).
export function renderSessions(doc, box, answer, onAction) {
  const sessions = isMapping(answer) && Array.isArray(answer.sessions) ? answer.sessions.filter(isMapping) : [];
  const table = el(doc, "table", { class: "sessions" });
  const head = el(doc, "tr");
  for (const name of ["Session", "State", "Engine", "Model", "Folder", "Turn", ""]) head.appendChild(el(doc, "th", { scope: "col" }, name));
  table.appendChild(head);
  for (const s of sessions) {
    const route = isMapping(s.route) ? s.route : {};
    const workspace = isMapping(s.workspace) ? s.workspace : {};
    const row = el(doc, "tr");
    for (const value of [s.session_id, s.state, route.engine, route.model, workspace.path, s.active_turn_id]) {
      row.appendChild(el(doc, "td", {}, field(value)));
    }
    const cell = el(doc, "td");
    const open = el(doc, "button", { type: "button" }, "Open");
    open.addEventListener("click", () => onAction("session.open", { session_id: s.session_id }));
    const stop = el(doc, "button", { type: "button", class: "stop" }, "Stop");
    stop.addEventListener("click", () => onAction("session.stop", { session_id: s.session_id, scope: "self" }));
    cell.appendChild(open);
    cell.appendChild(stop);
    row.appendChild(cell);
    table.appendChild(row);
  }
  box.replaceChildren(sessions.length ? table : el(doc, "p", { class: "empty" }, "No sessions to show."));
  return sessions.length;
}

// session.status: the session's turns, each with its own state and outcome.
export function renderSession(doc, box, answer) {
  const session = isMapping(answer) && isMapping(answer.session) ? answer.session : null;
  if (!session) {
    box.replaceChildren(el(doc, "p", { class: "empty" }, "No session is open."));
    return 0;
  }
  const turns = Array.isArray(session.turns) ? session.turns.filter(isMapping) : [];
  const list = el(doc, "ol", { class: "turns" });
  for (const t of turns) {
    list.appendChild(el(doc, "li", {}, `${field(t.turn_id)} ${field(t.state)} outcome=${field(t.outcome)}`));
  }
  box.replaceChildren(el(doc, "p", {}, `${field(session.session_id)} ${field(session.state)} revision ${field(session.revision)}`), list);
  return turns.length;
}

// workspace.diff: the files a turn changed, as counts. The patch itself is not drawn here.
export function renderDiff(doc, box, answer) {
  const diff = isMapping(answer) && isMapping(answer.diff) ? answer.diff : null;
  if (!diff) {
    box.replaceChildren(el(doc, "p", { class: "empty" }, "No diff to show."));
    return 0;
  }
  const files = Array.isArray(diff.files) ? diff.files.filter(isMapping) : [];
  const list = el(doc, "ul", { class: "diff" });
  for (const f of files) {
    list.appendChild(el(doc, "li", {}, `${field(f.status)} ${field(f.path)} +${field(f.insertions)} -${field(f.deletions)}`));
  }
  const parts = [el(doc, "p", {}, `against ${field(diff.base_ref)}`), list];
  if (diff.truncated === true) parts.push(el(doc, "p", { class: "note" }, "The list was cut short by the daemon."));
  box.replaceChildren(...parts);
  return files.length;
}

// route.list: what each engine can do now, with the daemon's own blocker text.
export function renderRoutes(doc, list, answer) {
  const routes = isMapping(answer) && Array.isArray(answer.routes) ? answer.routes.filter(isMapping) : [];
  list.replaceChildren(...routes.map((r) => el(doc, "li", {},
    `${field(r.id)} ${r.configured === true ? "configured" : "not configured"} ${field(r.maturity)}` +
    (r.blocker ? `: ${field(r.blocker)}` : ""))));
  return routes.length;
}

export const APPROVAL_EMPTY = "No approval request is open.";

// approval: the record approval.open answered, or null. Only its rendered lines are drawn: never its effect
// or its display entries, which no surface reads.
export function renderApproval(doc, region, approval, { held = null, onAnswer = () => {} } = {}) {
  const parts = [el(doc, "h2", {}, "Approval request (drawn by the core)")];
  if (!isMapping(approval) || !Array.isArray(approval.rendered)) {
    parts.push(el(doc, "p", { class: "empty" }, APPROVAL_EMPTY));
    region.replaceChildren(...parts);
    return 0;
  }
  const list = el(doc, "ol", { class: "rendered" });
  for (const line of approval.rendered.filter(isMapping)) {
    const trusted = line.trust === "trusted";
    const item = el(doc, "li", { "data-trust": trusted ? "trusted" : "untrusted" });
    if (!trusted) item.appendChild(el(doc, "span", { class: "tag" }, "untrusted"));
    item.appendChild(el(doc, "span", { class: "text" }, field(line.text)));
    list.appendChild(item);
  }
  parts.push(list);
  const buttons = el(doc, "div", { class: "answers" });
  for (const answer of ["approve", "deny"]) {
    const button = el(doc, "button", { type: "button", "data-answer": answer }, answer === "approve" ? "Approve" : "Deny");
    if (held) button.setAttribute("disabled", "");
    else button.addEventListener("click", () => onAnswer(answer));
    buttons.appendChild(button);
  }
  parts.push(buttons);
  if (held) parts.push(el(doc, "p", { class: "held" }, `Answering from the desktop is not enabled yet (${held}).`));
  region.replaceChildren(...parts);
  return list.children.length;
}

export function renderApprovalList(doc, list, answer) {
  const rows = isMapping(answer) && Array.isArray(answer.approvals) ? answer.approvals.filter(isMapping) : [];
  // A summary is not the request: it names the request and its deadline, nothing that could be read as the prompt.
  list.replaceChildren(...rows.map((row) => el(doc, "li", {},
    `${field(row.approval_id)} ${field(row.state)} until ${field(row.expires_at)}`)));
  return rows.length;
}

// The host's own statements, drawn first: they say what plugins can and cannot do on this daemon.
export const HOST_FIELDS = Object.freeze([
  ["approval_surface", "approvals"], ["containment", "containment"], ["authority_profile", "authority profile"],
  ["verification", "verification"], ["approvals_drawn", "approvals drawn"], ["core_poisoned", "core poisoned"],
]);

export function renderPlugins(doc, list, answer) {
  const rows = isMapping(answer) && Array.isArray(answer.plugins) ? answer.plugins.filter(isMapping) : [];
  const items = [];
  for (const [key, label] of HOST_FIELDS) {
    if (isMapping(answer) && key in answer) items.push(el(doc, "li", { class: "host" }, `${label}: ${field(answer[key])}`));
  }
  for (const row of rows) {
    const name = row.plugin_id ?? row.source;
    let text = `${field(name)} ${field(row.state)}`;
    if (row.code) text += `: ${field(row.code)} ${field(row.detail ?? "")}`.trimEnd();
    if (row.trust) text += ` (${field(row.trust)})`;
    const item = el(doc, "li", { class: "plugin", "data-state": field(row.state) }, text);
    const findings = Array.isArray(row.findings) ? row.findings.filter(isMapping) : [];
    if (findings.length) {
      const sub = el(doc, "ul");
      for (const f of findings) sub.appendChild(el(doc, "li", {}, `${field(f.severity)} ${field(f.stage)} ${field(f.code)} ${field(f.detail)}`));
      item.appendChild(sub);
    }
    items.push(item);
  }
  if (!rows.length) items.push(el(doc, "li", { class: "empty" }, "No plugin is loaded."));
  list.replaceChildren(...items);
  return rows.length;
}

// Theme tokens: the desktop's own names until lane PAGE publishes the theme API. A theme is applied whole
// or not at all, only to the workspace element, only as colours, and only if its text keeps WCAG 2.2 AA
// contrast (4.5:1 for text, 3:1 for the accent).
export const THEME_TOKENS = Object.freeze(["bg", "fg", "muted", "accent", "border"]);
const COLOUR = /^#[0-9a-fA-F]{6}$/;

function channel(v) {
  const c = v / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function luminance(hex) {
  const n = Number.parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

export function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

export function applyTheme(workspace, tokens) {
  if (!isMapping(tokens)) return { applied: false, reason: "a theme is a set of named colours" };
  for (const [name, value] of Object.entries(tokens)) {
    if (!THEME_TOKENS.includes(name)) return { applied: false, reason: `unknown token ${field(name)}` };
    if (typeof value !== "string" || !COLOUR.test(value)) return { applied: false, reason: `${name} is not a colour` };
  }
  for (const name of THEME_TOKENS) if (!(name in tokens)) return { applied: false, reason: `${name} is missing` };
  if (contrast(tokens.fg, tokens.bg) < 4.5) return { applied: false, reason: "text contrast below 4.5:1" };
  if (contrast(tokens.muted, tokens.bg) < 4.5) return { applied: false, reason: "muted text contrast below 4.5:1" };
  if (contrast(tokens.accent, tokens.bg) < 3) return { applied: false, reason: "accent contrast below 3:1" };
  for (const name of THEME_TOKENS) workspace.style.setProperty(`--theme-${name}`, tokens[name]);
  return { applied: true };
}
