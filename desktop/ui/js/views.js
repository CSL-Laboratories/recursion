// Drawing. Every value from the daemon, an engine or a plugin is set as text (textContent or a text node)
// after the sanitizer's rules; nothing here parses markup. Each function takes the document to draw with,
// so the checks can hand it a stand-in.
//
// The approval region is the core's: only renderApproval draws in it, only from the opened request's
// rendered lines, and no theme or panel reaches it (applyTheme writes to the workspace element alone).

import { field, sanitize } from "./sanitize.js";
import { answerConditions, deliveryNote, stopUnconfirmed } from "./model.js";

function el(doc, tag, attrs = {}, text = undefined) {
  const node = doc.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function isMapping(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function renderConditions(doc, box, answer) {
  const items = answerConditions(isMapping(answer) ? answer : {});
  const list = el(doc, "ul", { class: "conditions" });
  for (const [label, text] of items) {
    const item = el(doc, "li", { "data-label": label });
    item.appendChild(el(doc, "strong", {}, label));
    item.appendChild(doc.createTextNode(" " + text));
    list.appendChild(item);
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
  list.replaceChildren(...items);
  return items.length;
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

export function renderPlugins(doc, list, answer) {
  const rows = isMapping(answer) && Array.isArray(answer.plugins) ? answer.plugins.filter(isMapping) : [];
  list.replaceChildren(...rows.map((row) => el(doc, "li", {},
    `${field(row.plugin_id ?? row.name)} ${field(row.state ?? row.status)}`)));
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
