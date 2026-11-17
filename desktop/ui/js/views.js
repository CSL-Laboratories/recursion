// Drawing. Every value from the daemon, an engine or a plugin is set as text (textContent or a text node)
// after the sanitizer's rules; nothing here parses markup. Each function takes the document to draw with,
// so the checks can hand it a stand-in.
//
// The approval region is the core's: only renderApproval draws in it, only from the opened request's
// rendered lines. applyTheme and applyLayout write only to the workspace element and, for a layout,
// its own sections. Approvals, Stop, disclaimers and the floor are outside that element.

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

// gui.theme/1 and gui.layout/1, the same documents the page reads. A document is applied whole or not
// at all. Tokens are the five colours, plus optional density and radius. Contrast stays at 4.5:1 for
// text and 3:1 for the accent. Nothing here fetches, and nothing here is taken from a plugin package.
export const THEME_TOKENS = Object.freeze(["bg", "fg", "muted", "accent", "border"]);
const THEME_SCHEMA = "gui.theme/1";
const LAYOUT_SCHEMA = "gui.layout/1";
const THEME_DOC_KEYS = new Set(["schema", "name", "title", "tokens"]);
const THEME_TOKEN_KEYS = new Set(["bg", "fg", "muted", "accent", "border", "density", "radius"]);
const LAYOUT_DOC_KEYS = new Set(["schema", "name", "title", "columns", "slots"]);
const SLOT_KEYS = new Set(["panel", "column", "span", "order", "collapsed"]);
const DENSITY = new Set(["compact", "normal", "roomy"]);
const RADIUS = new Set([0, 2, 4, 8]);
const CORE_VIEWS = new Set(["routes", "reads", "interventions"]);
const CORE_REGIONS = new Set(["stop", "approval", "header", "footer"]);
const WORKSPACE_VIEWS = Object.freeze(["runs", "sessions", "start", "approvals", "plugins"]);
const THEME_PROPS = Object.freeze([
  "--theme-bg", "--theme-fg", "--theme-muted", "--theme-accent", "--theme-border", "--theme-density", "--theme-radius",
]);
const LAYOUT_PROPS = Object.freeze(["display", "grid-template-columns"]);
const SECTION_PROPS = Object.freeze(["grid-column"]);
const LAYOUT_ATTRS = Object.freeze(["data-column", "data-span", "data-order", "data-collapsed"]);
const WORKSPACE_ATTRS = Object.freeze(["data-columns", "data-layout", "data-draw-columns"]);
const NAME = /^[a-z][a-z0-9_-]{0,63}$/;
const COLOUR = /^#[0-9A-Fa-f]{6}$/;
const PANEL = /^[A-Za-z0-9_.-]{1,96}$/;
const THEME_MAX = 8192;
const LAYOUT_MAX = 65536;
const JSON_MAX_DEPTH = 32;
const NARROW_PX = 768;

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

function refused(reason) {
  return { applied: false, reason };
}

function ownKeys(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  return Object.keys(value);
}

function byteLength(text) {
  if (typeof TextEncoder === "function") return new TextEncoder().encode(text).length;
  return text.length;
}

// JSON text, refused on a duplicate member or a number that is not an integer. json.parse keeps the
// last duplicate and turns 4.0 into 4, so the fixtures that pin those two defects have to be read here.
function readJson(text, maxBytes) {
  if (typeof text !== "string") return { ok: false, reason: "not_json" };
  if (byteLength(text) > maxBytes) return { ok: false, reason: "too_large" };
  try {
    scanJson(text);
  } catch (err) {
    return { ok: false, reason: err && err.reason ? err.reason : "not_json" };
  }
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false, reason: "not_json" };
  }
}

function scanJson(text) {
  let i = 0;
  let depth = 0;
  const n = text.length;
  const fail = (reason) => {
    const err = new Error(reason);
    err.reason = reason;
    throw err;
  };
  const ws = () => {
    while (i < n) {
      const c = text[i];
      if (c !== " " && c !== "\n" && c !== "\r" && c !== "\t") return;
      i += 1;
    }
  };
  const parseString = () => {
    if (text[i] !== "\"") fail("not_json");
    i += 1;
    let out = "";
    while (i < n) {
      const c = text[i];
      i += 1;
      if (c === "\"") return out;
      if (c === "\\") {
        if (i >= n) fail("not_json");
        const e = text[i];
        i += 1;
        if (e === "\"" || e === "\\" || e === "/") out += e;
        else if (e === "b") out += "\b";
        else if (e === "f") out += "\f";
        else if (e === "n") out += "\n";
        else if (e === "r") out += "\r";
        else if (e === "t") out += "\t";
        else if (e === "u") {
          const hex = text.slice(i, i + 4);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail("not_json");
          out += String.fromCharCode(Number.parseInt(hex, 16));
          i += 4;
        } else fail("not_json");
      } else if (c.charCodeAt(0) < 0x20) fail("not_json");
      else out += c;
    }
    fail("not_json");
  };
  const parseNumber = () => {
    if (text[i] === "-") i += 1;
    if (text[i] === "0") i += 1;
    else if (text[i] >= "1" && text[i] <= "9") {
      while (text[i] >= "0" && text[i] <= "9") i += 1;
    } else fail("not_json");
    let integer = true;
    if (text[i] === ".") {
      integer = false;
      i += 1;
      if (!(text[i] >= "0" && text[i] <= "9")) fail("not_json");
      while (text[i] >= "0" && text[i] <= "9") i += 1;
    }
    if (text[i] === "e" || text[i] === "E") {
      integer = false;
      i += 1;
      if (text[i] === "+" || text[i] === "-") i += 1;
      if (!(text[i] >= "0" && text[i] <= "9")) fail("not_json");
      while (text[i] >= "0" && text[i] <= "9") i += 1;
    }
    if (!integer) fail("not_integer");
  };
  const parseLiteral = (word) => {
    if (text.slice(i, i + word.length) !== word) fail("not_json");
    i += word.length;
  };
  const parseValue = () => {
    ws();
    if (i >= n) fail("not_json");
    const c = text[i];
    if (c === "\"") return parseString();
    if (c === "{") return parseObject();
    if (c === "[") return parseArray();
    if (c === "t") return parseLiteral("true");
    if (c === "f") return parseLiteral("false");
    if (c === "n") return parseLiteral("null");
    if (c === "-" || (c >= "0" && c <= "9")) return parseNumber();
    return fail("not_json");
  };
  const parseObject = () => {
    if (depth >= JSON_MAX_DEPTH) fail("not_json");
    depth += 1;
    i += 1;
    const keys = new Set();
    ws();
    if (text[i] === "}") {
      i += 1;
      depth -= 1;
      return;
    }
    while (true) {
      ws();
      const key = parseString();
      if (keys.has(key)) fail("duplicate_key");
      keys.add(key);
      ws();
      if (text[i] !== ":") fail("not_json");
      i += 1;
      parseValue();
      ws();
      if (text[i] === "}") {
        i += 1;
        depth -= 1;
        return;
      }
      if (text[i] !== ",") fail("not_json");
      i += 1;
    }
  };
  const parseArray = () => {
    if (depth >= JSON_MAX_DEPTH) fail("not_json");
    depth += 1;
    i += 1;
    ws();
    if (text[i] === "]") {
      i += 1;
      depth -= 1;
      return;
    }
    while (true) {
      parseValue();
      ws();
      if (text[i] === "]") {
        i += 1;
        depth -= 1;
        return;
      }
      if (text[i] !== ",") fail("not_json");
      i += 1;
    }
  };
  parseValue();
  ws();
  if (i !== n) fail("not_json");
}

function documentValue(document, maxBytes) {
  if (typeof document === "string") return readJson(document, maxBytes);
  if (!isMapping(document)) return { ok: false, reason: "not_object" };
  return { ok: true, value: document };
}

function readProp(style, name) {
  if (style && typeof style.getPropertyValue === "function") {
    const value = style.getPropertyValue(name);
    return value ? String(value) : "";
  }
  if (style && style.props instanceof Map && style.props.has(name)) return String(style.props.get(name));
  return "";
}

function clearProp(style, name) {
  if (style && typeof style.removeProperty === "function") style.removeProperty(name);
}

function canStyle(node) {
  return Boolean(node && node.style && typeof node.style.setProperty === "function");
}

function writeProps(style, managed, entries) {
  const previous = new Map();
  for (const name of managed) previous.set(name, readProp(style, name));
  try {
    for (const name of managed) clearProp(style, name);
    for (const [name, value] of entries) style.setProperty(name, value);
  } catch (err) {
    for (const name of managed) {
      const old = previous.get(name) || "";
      try {
        if (old) style.setProperty(name, old);
        else clearProp(style, name);
      } catch {
        // Report the original failure. A second failure while putting the old value back stops nothing else.
      }
    }
    throw err;
  }
}

function themeTitleOk(title) {
  if (typeof title !== "string" || title.length < 1 || title.length > 100) return false;
  for (let i = 0; i < title.length; i += 1) {
    const code = title.charCodeAt(i);
    if (code < 32 || code === 127) return false;
  }
  return true;
}

function layoutTitleOk(title) {
  return typeof title === "string" && title.length >= 1 && title.length <= 100 && title.trim().length > 0;
}

function parseThemeValue(data) {
  const keys = ownKeys(data);
  if (keys === null) return { ok: false, reason: "not_object" };
  if (keys.some((key) => !THEME_DOC_KEYS.has(key))) return { ok: false, reason: "unknown_key" };
  if (![...THEME_DOC_KEYS].every((key) => keys.includes(key))) return { ok: false, reason: "missing_key" };
  if (data.schema !== THEME_SCHEMA) return { ok: false, reason: "schema" };
  if (typeof data.name !== "string" || NAME.test(data.name) !== true) return { ok: false, reason: "name" };
  if (!themeTitleOk(data.title)) return { ok: false, reason: "title" };
  const tokenKeys = ownKeys(data.tokens);
  if (tokenKeys === null) return { ok: false, reason: "tokens" };
  if (tokenKeys.some((key) => !THEME_TOKEN_KEYS.has(key))) return { ok: false, reason: "token_unknown" };
  if (!THEME_TOKENS.every((key) => tokenKeys.includes(key))) return { ok: false, reason: "token_missing" };
  const colours = {};
  for (const name of THEME_TOKENS) {
    const value = data.tokens[name];
    if (typeof value !== "string" || COLOUR.test(value) !== true) return { ok: false, reason: "colour" };
    colours[name] = value.toLowerCase();
  }
  let density = null;
  if (tokenKeys.includes("density")) {
    density = data.tokens.density;
    if (typeof density !== "string" || !DENSITY.has(density)) return { ok: false, reason: "density" };
  }
  let radius = null;
  if (tokenKeys.includes("radius")) {
    radius = data.tokens.radius;
    if (typeof radius !== "number" || Number.isSafeInteger(radius) !== true || !RADIUS.has(radius)) {
      return { ok: false, reason: "radius" };
    }
  }
  if (contrast(colours.fg, colours.bg) < 4.5 || contrast(colours.muted, colours.bg) < 4.5) {
    return { ok: false, reason: "contrast" };
  }
  if (contrast(colours.accent, colours.bg) < 3) return { ok: false, reason: "contrast" };
  const entries = THEME_TOKENS.map((name) => [`--theme-${name}`, colours[name]]);
  if (density !== null) entries.push(["--theme-density", density]);
  if (radius !== null) entries.push(["--theme-radius", `${radius}px`]);
  return { ok: true, entries };
}

export function applyTheme(workspace, document) {
  if (document == null) return refused("none");
  const read = documentValue(document, THEME_MAX);
  if (!read.ok) return refused(read.reason);
  const parsed = parseThemeValue(read.value);
  if (!parsed.ok) return refused(parsed.reason);
  if (!canStyle(workspace)) return refused("no_workspace");
  writeProps(workspace.style, THEME_PROPS, parsed.entries);
  return { applied: true };
}

function knownPanels(workspace, options) {
  const ids = new Set(CORE_VIEWS);
  for (const view of WORKSPACE_VIEWS) ids.add(view);
  const children = workspace && workspace.children ? workspace.children : [];
  for (const child of children) {
    if (!child || typeof child.getAttribute !== "function") continue;
    const view = child.getAttribute("data-view");
    const panel = child.getAttribute("data-panel");
    if (typeof view === "string" && view) ids.add(view);
    if (typeof panel === "string" && panel) ids.add(panel);
  }
  const extra = options && Array.isArray(options.knownPanels) ? options.knownPanels : [];
  for (const id of extra) if (typeof id === "string" && id) ids.add(id);
  for (const region of CORE_REGIONS) ids.delete(region);
  return ids;
}

function viewportWidth(workspace, options) {
  if (options && typeof options.viewportWidth === "number") return options.viewportWidth;
  const view = workspace && workspace.ownerDocument && workspace.ownerDocument.defaultView;
  if (view && typeof view.innerWidth === "number") return view.innerWidth;
  return null;
}

function parseLayoutValue(data, known) {
  const keys = ownKeys(data);
  if (keys === null) return { ok: false, reason: "not_object" };
  if (keys.some((key) => !LAYOUT_DOC_KEYS.has(key))) return { ok: false, reason: "unknown_key" };
  if (![...LAYOUT_DOC_KEYS].every((key) => keys.includes(key))) return { ok: false, reason: "missing_key" };
  if (data.schema !== LAYOUT_SCHEMA) return { ok: false, reason: "schema" };
  if (typeof data.name !== "string" || NAME.test(data.name) !== true) return { ok: false, reason: "name" };
  if (!layoutTitleOk(data.title)) return { ok: false, reason: "title" };
  if (typeof data.columns !== "number" || Number.isSafeInteger(data.columns) !== true || data.columns < 1 || data.columns > 3) {
    return { ok: false, reason: "columns" };
  }
  if (!Array.isArray(data.slots)) return { ok: false, reason: "slots" };
  const seen = new Set();
  const slots = [];
  for (let index = 0; index < data.slots.length; index += 1) {
    const slot = data.slots[index];
    const slotKeys = ownKeys(slot);
    if (slotKeys === null) return { ok: false, reason: "slot" };
    if (slotKeys.some((key) => !SLOT_KEYS.has(key))) return { ok: false, reason: "unknown_key" };
    if (![...SLOT_KEYS].every((key) => slotKeys.includes(key))) return { ok: false, reason: "slot" };
    const panel = slot.panel;
    if (typeof panel !== "string" || PANEL.test(panel) !== true || CORE_REGIONS.has(panel) || !known.has(panel) || seen.has(panel)) {
      return { ok: false, reason: "panel" };
    }
    seen.add(panel);
    const column = slot.column;
    const span = slot.span;
    if (typeof column !== "number" || Number.isSafeInteger(column) !== true || column < 1 || column > data.columns) {
      return { ok: false, reason: "column" };
    }
    if (typeof span !== "number" || Number.isSafeInteger(span) !== true || span < 1 || span > 3 || column + span - 1 > data.columns) {
      return { ok: false, reason: "span" };
    }
    if (typeof slot.order !== "number" || Number.isSafeInteger(slot.order) !== true) return { ok: false, reason: "order" };
    if (typeof slot.collapsed !== "boolean") return { ok: false, reason: "collapsed" };
    slots.push({ panel, column, span, order: slot.order, collapsed: slot.collapsed, index });
  }
  slots.sort((a, b) => a.column - b.column || a.order - b.order || a.index - b.index);
  return { ok: true, name: data.name, columns: data.columns, slots };
}

function elementChildren(node) {
  if (!node || !node.childNodes) return [];
  return [...node.childNodes].filter((child) => child && child.nodeType === 1);
}

function sectionId(section) {
  if (!section || typeof section.getAttribute !== "function") return "";
  return section.getAttribute("data-panel") || section.getAttribute("data-view") || "";
}

function snapshotAttrs(node, names) {
  const out = new Map();
  for (const name of names) out.set(name, node.getAttribute(name));
  return out;
}

function restoreAttrs(node, snap) {
  for (const [name, value] of snap) {
    if (value === null) node.removeAttribute(name);
    else node.setAttribute(name, value);
  }
}

function snapshotProps(style, names) {
  const out = new Map();
  for (const name of names) out.set(name, readProp(style, name));
  return out;
}

function restoreProps(style, snap) {
  for (const [name, value] of snap) {
    if (value) style.setProperty(name, value);
    else clearProp(style, name);
  }
}

export function applyLayout(workspace, document, options = undefined) {
  if (document == null) return refused("none");
  const read = documentValue(document, LAYOUT_MAX);
  if (!read.ok) return refused(read.reason);
  const parsed = parseLayoutValue(read.value, knownPanels(workspace, options));
  if (!parsed.ok) return refused(parsed.reason);
  if (!canStyle(workspace) || typeof workspace.setAttribute !== "function" || typeof workspace.getAttribute !== "function") {
    return refused("no_workspace");
  }
  const width = viewportWidth(workspace, options);
  const narrow = typeof width === "number" && width < NARROW_PX;
  const drawColumns = narrow ? 1 : parsed.columns;
  const sections = elementChildren(workspace).filter((child) => child.tagName === "SECTION" || sectionId(child));
  const byId = new Map();
  for (const section of sections) {
    const id = sectionId(section);
    if (id && !byId.has(id)) byId.set(id, section);
  }
  const placed = [];
  const used = new Set();
  for (const slot of parsed.slots) {
    const section = byId.get(slot.panel);
    if (section && !used.has(section)) {
      placed.push(section);
      used.add(section);
    }
  }
  const rest = elementChildren(workspace).filter((child) => !used.has(child));
  const previousOrder = workspace.childNodes ? [...workspace.childNodes] : null;
  const previousWorkspaceAttrs = snapshotAttrs(workspace, WORKSPACE_ATTRS);
  const previousWorkspaceProps = snapshotProps(workspace.style, LAYOUT_PROPS);
  const previousSections = elementChildren(workspace).map((section) => ({
    section,
    attrs: snapshotAttrs(section, LAYOUT_ATTRS),
    props: canStyle(section) ? snapshotProps(section.style, SECTION_PROPS) : new Map(),
  }));
  const restore = () => {
    try {
      restoreAttrs(workspace, previousWorkspaceAttrs);
      for (const row of previousSections) {
        restoreAttrs(row.section, row.attrs);
        if (canStyle(row.section)) restoreProps(row.section.style, row.props);
      }
      if (previousOrder && typeof workspace.replaceChildren === "function") workspace.replaceChildren(...previousOrder);
      restoreProps(workspace.style, previousWorkspaceProps);
    } catch {
      // The original failure is reported by the caller.
    }
  };
  try {
    for (const row of previousSections) {
      for (const name of LAYOUT_ATTRS) row.section.removeAttribute(name);
      if (canStyle(row.section)) clearProp(row.section.style, "grid-column");
    }
    for (const slot of parsed.slots) {
      const section = byId.get(slot.panel);
      if (!section) continue;
      section.setAttribute("data-column", String(slot.column));
      section.setAttribute("data-span", String(slot.span));
      section.setAttribute("data-order", String(slot.order));
      section.setAttribute("data-collapsed", slot.collapsed ? "true" : "false");
      if (canStyle(section)) {
        section.style.setProperty("grid-column", narrow ? "auto" : `${slot.column} / span ${slot.span}`);
      }
    }
    const desktopPlaced = parsed.slots.some((slot) => WORKSPACE_VIEWS.includes(slot.panel) && byId.has(slot.panel));
    if (!desktopPlaced) {
      for (const section of sections) {
        if (section.hidden === true || !canStyle(section)) continue;
        section.style.setProperty("grid-column", `1 / span ${drawColumns}`);
      }
    }
    workspace.setAttribute("data-columns", String(parsed.columns));
    workspace.setAttribute("data-layout", parsed.name);
    workspace.setAttribute("data-draw-columns", String(drawColumns));
    workspace.style.setProperty("display", "grid");
    workspace.style.setProperty("grid-template-columns", narrow ? "minmax(0, 1fr)" : `repeat(${drawColumns}, minmax(0, 1fr))`);
    if (typeof workspace.replaceChildren === "function") {
      const keptText = previousOrder ? previousOrder.filter((node) => !node || node.nodeType !== 1) : [];
      workspace.replaceChildren(...placed, ...rest, ...keptText);
    }
  } catch (err) {
    restore();
    throw err;
  }
  return { applied: true };
}
