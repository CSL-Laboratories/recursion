// The views draw text only, the approval region draws only the opened request's rendered lines, and no theme
// reaches the approval region.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";

import {
  APPROVAL_EMPTY, THEME_TOKENS, applyTheme, contrast, renderApproval, renderApprovalList, renderConditions,
  renderRuns, renderTranscript,
} from "../../ui/js/views.js";
import * as views from "../../ui/js/views.js";
import { fakeDocument as doc, fixture, windowDocument } from "./support.mjs";

const GUI_FIXTURES = new URL("../../../schemas/gui/v1/fixtures/", import.meta.url);
const THEME_REASONS = new Set([
  "none", "not_json", "not_integer", "duplicate_key", "too_large", "not_object", "unknown_key", "missing_key",
  "schema", "name", "title", "tokens", "token_unknown", "token_missing", "colour", "density", "radius", "contrast",
  "no_workspace",
]);
const LAYOUT_REASONS = new Set([
  "none", "not_json", "not_integer", "duplicate_key", "too_large", "not_object", "unknown_key", "missing_key",
  "schema", "name", "title", "columns", "slots", "slot", "panel", "column", "span", "order", "collapsed",
  "no_workspace",
]);
const THEME_PROP = /^--theme-(?:bg|fg|muted|accent|border|density|radius)$/;
const THEME_VALUE = /^(?:#[0-9a-f]{6}|compact|normal|roomy|(?:0|2|4|8)px)$/;
const CORE_IDS = ["core-approval", "stop-confirm", "stop-answers", "link-state", "stop-confirm-yes", "stop-confirm-no"];

function listGui(dir, kind) {
  return readdirSync(new URL(`${kind}/${dir}/`, GUI_FIXTURES)).filter((name) => name.endsWith(".json")).sort();
}

function readGui(dir, name, kind) {
  if (kind) return readFileSync(new URL(`${kind}/${dir}/${name}`, GUI_FIXTURES), "utf8");
  for (const candidate of ["theme", "layout"]) {
    try {
      return readFileSync(new URL(`${candidate}/${dir}/${name}`, GUI_FIXTURES), "utf8");
    } catch (err) {
      if (err.code !== "ENOENT") throw err;
    }
  }
  throw new Error(`no fixture ${dir}/${name}`);
}

function schemaOf(raw) {
  try {
    const value = JSON.parse(raw);
    if (value !== null && typeof value === "object" && typeof value.schema === "string") return value.schema;
  } catch {
    // A duplicate member still names its schema in the text.
  }
  if (raw.includes("\"gui.layout/")) return "gui.layout/1";
  if (raw.includes("\"gui.theme/")) return "gui.theme/1";
  return "";
}

function guiFiles(dir, kind) {
  const prefix = kind === "theme" ? "gui.theme/" : "gui.layout/";
  return listGui(dir, kind).filter((name) => schemaOf(readGui(dir, name, kind)).startsWith(prefix));
}

function applyLayout(workspace, document, options) {
  if (typeof views.applyLayout !== "function") throw new Error("applyLayout is absent");
  return views.applyLayout(workspace, document, options);
}

function styleEntries(node) {
  return [...node.style.props.entries()];
}

function coreNodes(win) {
  return [
    ...CORE_IDS.map((id) => win.getElementById(id)),
    ...win.root.find((node) => node.tagName === "HEADER" || node.tagName === "FOOTER"),
  ];
}

function coreSnapshot(win) {
  return coreNodes(win).map((node) => ({
    id: node.getAttribute("id"),
    tag: node.tagName,
    hidden: node.hidden,
    text: node.textContent,
    style: styleEntries(node),
    attrs: [...node.attributes.entries()].sort(),
  }));
}

function sectionSnapshot(win) {
  return win.querySelectorAll("main > section").map((section) => ({
    view: section.getAttribute("data-view"),
    hidden: section.hidden,
    style: styleEntries(section),
    column: section.getAttribute("data-column"),
    collapsed: section.getAttribute("data-collapsed"),
  }));
}

const ESC = String.fromCharCode(0x1b);
const RLO = String.fromCodePoint(0x202e);
const OPENED = {
  approval_id: "apr-00c4",
  digest: "39f84aee4acb31972adc920f7acfa0fcc4f4df94e3c178c7f60f425ae12eda77",
  revision: 1,
  effect: { kind: "engine_tool", tool: "Bash", input: { command: "EFFECT-SHOULD-NOT-SHOW" } },
  display: [{ label: "Why", value: "DISPLAY-SHOULD-NOT-SHOW", trust: "untrusted" }],
  rendered: [
    { text: "Tool call waiting for an answer", trust: "trusted" },
    { text: `"command": "<script>alert(1)</script> ${ESC}]52;c;eA==${String.fromCharCode(7)}"`, trust: "untrusted" },
    { text: `directory: "file${RLO}txt.exe"`, trust: "untrusted" },
  ],
};
const THEME = { bg: "#ffffff", fg: "#111111", muted: "#555555", accent: "#0055aa", border: "#cccccc" };
const THEME_DOC = { schema: "gui.theme/1", name: "check", title: "Check", tokens: THEME };

function region() {
  return doc.createElement("aside");
}

test("the approval region draws the rendered lines and nothing else of the request", () => {
  const box = region();
  assert.equal(renderApproval(doc, box, OPENED, {}), 3);
  const shown = box.textContent;
  assert.doesNotMatch(shown, /EFFECT-SHOULD-NOT-SHOW|DISPLAY-SHOULD-NOT-SHOW/);
  assert.match(shown, /Tool call waiting for an answer/);
  const lines = box.find((n) => n.tagName === "LI");
  assert.deepEqual(lines.map((l) => l.getAttribute("data-trust")), ["trusted", "untrusted", "untrusted"]);
  assert.deepEqual(lines.map((l) => l.find((n) => n.getAttribute("class") === "tag").length), [0, 1, 1]);
});

test("engine text in the approval region stays text: no markup, no escape, no bidi control", () => {
  const box = region();
  renderApproval(doc, box, OPENED, {});
  const shown = box.textContent;
  assert.match(shown, /<script>alert\(1\)<\/script>/); // drawn as text, never parsed: the stand-in refuses markup
  assert.ok(!shown.includes(ESC));
  assert.ok(!shown.includes(RLO));
  assert.match(shown, /OSC 52/);
  assert.match(shown, /U\+202E/);
});

test("a held answer is drawn disabled, says why, and cannot be clicked into a call", () => {
  const box = region();
  const answered = [];
  renderApproval(doc, box, OPENED, { held: "approval_surface_undecided", onAnswer: (a) => answered.push(a) });
  const buttons = box.find((n) => n.tagName === "BUTTON");
  assert.deepEqual(buttons.map((b) => b.getAttribute("data-answer")), ["approve", "deny"]);
  for (const button of buttons) {
    assert.ok(button.hasAttribute("disabled"));
    button.click();
  }
  assert.deepEqual(answered, []);
  assert.match(box.textContent, /approval_surface_undecided/);
});

test("an answer button calls back with its answer when the answer is not held", () => {
  const box = region();
  const answered = [];
  renderApproval(doc, box, OPENED, { onAnswer: (a) => answered.push(a) });
  for (const button of box.find((n) => n.tagName === "BUTTON")) button.click();
  assert.deepEqual(answered, ["approve", "deny"]);
});

test("a summary is not drawn in the approval region", () => {
  const box = region();
  assert.equal(renderApproval(doc, box, { approval_id: "apr-1", state: "pending", revision: 1 }), 0);
  assert.match(box.textContent, new RegExp(APPROVAL_EMPTY));
  const list = doc.createElement("ul");
  renderApprovalList(doc, list, { code: "ok", approvals: [{ approval_id: "apr-1", state: "pending", expires_at: "t" }] });
  assert.equal(list.textContent, "apr-1 pending until t");
});

test("a theme is applied to the workspace only, whole, and only with readable contrast", () => {
  const workspace = doc.createElement("main");
  const approval = region();
  assert.deepEqual(applyTheme(workspace, THEME_DOC), { applied: true });
  assert.deepEqual([...workspace.style.props.keys()].sort(), THEME_TOKENS.map((t) => `--theme-${t}`).sort());
  assert.equal(approval.style.props.size, 0);
  const refused = [
    { ...THEME, fg: "#eeeeee" },
    { ...THEME, muted: "#dddddd" },
    { ...THEME, accent: "#f0f0f0" },
    { ...THEME, bg: "url(x)" },
    { ...THEME, extra: "#000000" },
    { bg: "#ffffff", fg: "#000000" },
    "dark",
  ];
  for (const tokens of refused) {
    const fresh = doc.createElement("main");
    assert.equal(applyTheme(fresh, tokens).applied, false, JSON.stringify(tokens));
    assert.equal(fresh.style.props.size, 0);
  }
});

test("contrast follows WCAG: black on white is 21:1", () => {
  assert.equal(Math.round(contrast("#000000", "#ffffff") * 100) / 100, 21);
  assert.equal(contrast("#777777", "#777777"), 1);
});

test("runs are drawn as text, an unconfirmed Stop is said, and Stop goes back to the app", () => {
  const box = doc.createElement("div");
  const asked = [];
  const answer = { code: "ok", runs: [
    { run_id: "run-1", state: "running", engine: "codex", tenant: `t${ESC}[31m1` },
    { run_id: "run-2", state: "stop_pending", engine: "codex", tenant: "t1", stop: { reason: "x" } },
  ] };
  assert.equal(renderRuns(doc, box, answer, (name, input) => asked.push([name, input])), 2);
  assert.ok(!box.textContent.includes(ESC));
  assert.match(box.textContent, /stop_pending \(stop not confirmed\)/);
  const stops = box.find((n) => n.tagName === "BUTTON" && n.textContent === "Stop");
  stops[0].click();
  assert.deepEqual(asked, [["run.stop", { run_id: "run-1" }]]);
});

test("conditions show the TUI's labels, and a delivery note when the answer says how it was delivered", () => {
  const box = doc.createElement("div");
  for (const { case: name, answer } of fixture("delivery.json").cases) {
    renderConditions(doc, box, answer);
    const labels = box.find((n) => n.tagName === "LI").map((n) => n.getAttribute("data-label"));
    if (answer.code === "control_api_unavailable") assert.deepEqual(labels, ["UNAVAILABLE", "DELIVERY"], name);
  }
});

test("transcript lines are text, and an untrusted line is marked", () => {
  const list = doc.createElement("ol");
  const answer = { code: "ok", lines: [
    { seq: 1, source: "engine", trust: "untrusted", text: `hello${ESC}[2J\nworld` },
    { seq: 2, source: "core", trust: "trusted", text: "turn ended" },
  ] };
  assert.equal(renderTranscript(doc, list, answer), 2);
  assert.ok(!list.textContent.includes(ESC));
  assert.match(list.textContent, /hello.*CSI.*\nworld/s);
  assert.deepEqual(list.children.map((c) => c.getAttribute("data-trust")), ["untrusted", "trusted"]);
});

test("N10 a gui.theme/1 is applied on the workspace only, and alpha and url( are refused", () => {
  const win = windowDocument();
  const workspace = win.getElementById("workspace");
  const before = coreSnapshot(win);
  const sections = sectionSnapshot(win);
  assert.equal(applyTheme(workspace, readGui("good", "g01-dusk.json")).applied, true);
  assert.deepEqual(coreSnapshot(win), before);
  for (const section of win.querySelectorAll("main > section")) assert.deepEqual(styleEntries(section), []);
  const keys = [...workspace.style.props.keys()];
  assert.ok(keys.length >= 5);
  for (const key of keys) {
    assert.match(key, THEME_PROP, key);
    assert.match(workspace.style.props.get(key), THEME_VALUE, key);
  }
  assert.equal(workspace.style.props.get("--theme-bg"), "#101418");
  for (const name of ["b08-alpha-hex.json", "b03-url.json"]) {
    const kept = styleEntries(workspace);
    const refused = applyTheme(workspace, readGui("bad", name));
    assert.equal(refused.applied, false, name);
    assert.ok(THEME_REASONS.has(refused.reason), `${name} ${refused.reason}`);
    assert.deepEqual(styleEntries(workspace), kept, name);
    assert.deepEqual(coreSnapshot(win), before, name);
  }
  assert.deepEqual(sectionSnapshot(win).map((row) => [row.view, row.hidden]), sections.map((row) => [row.view, row.hidden]));
});

test("shared good theme fixtures apply whole, as theme properties only", () => {
  const files = guiFiles("good", "theme");
  assert.ok(files.length >= 6, files.join(","));
  for (const name of files) {
    const workspace = doc.createElement("main");
    const approval = region();
    const result = applyTheme(workspace, readGui("good", name));
    assert.equal(result.applied, true, name);
    assert.deepEqual(result, { applied: true }, name);
    const keys = [...workspace.style.props.keys()];
    assert.ok(keys.length >= 5, name);
    for (const key of keys) {
      assert.match(key, THEME_PROP, `${name} ${key}`);
      assert.match(String(workspace.style.props.get(key)), THEME_VALUE, `${name} ${key}`);
      assert.equal(String(workspace.style.props.get(key)), String(workspace.style.props.get(key)).toLowerCase(), name);
    }
    assert.equal(approval.style.props.size, 0, name);
    assert.equal(workspace.style.props.has("outline"), false, name);
    assert.equal(workspace.style.props.has("min-width"), false, name);
    assert.equal(workspace.style.props.has("min-height"), false, name);
  }
});

test("shared bad theme fixtures are refused, write nothing, and name a theme reason", () => {
  const files = guiFiles("bad", "theme");
  assert.ok(files.length >= 22, String(files.length));
  const expectReason = {
    "b01-unknown-token.json": "token_unknown",
    "b02-core-token.json": "token_unknown",
    "b03-url.json": "colour",
    "b04-var.json": "colour",
    "b05-semicolon.json": "colour",
    "b06-brace.json": "colour",
    "b07-named-colour.json": "colour",
    "b08-alpha-hex.json": "colour",
    "b09-expression.json": "colour",
    "b10-fg-contrast.json": "contrast",
    "b11-muted-contrast.json": "contrast",
    "b12-accent-contrast.json": "contrast",
    "b13-extra-top-key.json": "unknown_key",
    "b14-missing-colour.json": "token_missing",
    "b15-bad-density.json": "density",
    "b16-bad-radius.json": "radius",
    "b17-wrong-schema.json": "schema",
    "b18-three-digit.json": "colour",
    "b19-core-star.json": "token_unknown",
    "b20-radius-float.json": "not_integer",
    "b21-duplicate-member.json": "duplicate_key",
    "b22-title-control.json": "title",
  };
  for (const name of files) {
    const workspace = doc.createElement("main");
    const result = applyTheme(workspace, readGui("bad", name));
    assert.equal(result.applied, false, name);
    assert.ok(THEME_REASONS.has(result.reason), `${name} ${result.reason}`);
    assert.equal(workspace.style.props.size, 0, name);
    if (expectReason[name]) assert.equal(result.reason, expectReason[name], name);
  }
});

test("N1 to N3 a bad theme leaves the core region untouched", () => {
  const win = windowDocument();
  const before = coreSnapshot(win);
  const workspace = win.getElementById("workspace");
  const names = {
    "b01-unknown-token.json": "token_unknown",
    "b02-core-token.json": "token_unknown",
    "b19-core-star.json": "token_unknown",
    "b03-url.json": "colour",
    "b04-var.json": "colour",
    "b05-semicolon.json": "colour",
    "b06-brace.json": "colour",
    "b07-named-colour.json": "colour",
    "b08-alpha-hex.json": "colour",
    "b09-expression.json": "colour",
    "b18-three-digit.json": "colour",
    "b10-fg-contrast.json": "contrast",
    "b11-muted-contrast.json": "contrast",
    "b12-accent-contrast.json": "contrast",
  };
  for (const [name, reason] of Object.entries(names)) {
    const result = applyTheme(workspace, readGui("bad", name));
    assert.equal(result.applied, false, name);
    assert.equal(result.reason, reason, name);
    assert.equal(workspace.style.props.size, 0, name);
    assert.deepEqual(coreSnapshot(win), before, name);
  }
});

test("N4 every good theme leaves approvals, Stop, disclaimers and the floor untouched", () => {
  const page = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
  assert.match(page, /Closing this window stops nothing/);
  assert.match(page, /class="beta"/);
  const files = guiFiles("good", "theme");
  for (const name of files) {
    const win = windowDocument();
    const before = coreSnapshot(win);
    const hidden = sectionSnapshot(win).map((row) => [row.view, row.hidden]);
    const workspace = win.getElementById("workspace");
    assert.equal(applyTheme(workspace, readGui("good", name)).applied, true, name);
    assert.deepEqual(coreSnapshot(win), before, name);
    assert.deepEqual(sectionSnapshot(win).map((row) => [row.view, row.hidden]), hidden, name);
    assert.equal(win.root.find((node) => node.tagName === "FOOTER").length, 1, name);
    assert.equal(win.root.find((node) => node.tagName === "HEADER").length, 1, name);
    assert.equal(win.getElementById("stop-confirm").hidden, true, name);
    assert.equal(win.getElementById("core-approval").hidden, false, name);
    for (const key of workspace.style.props.keys()) assert.match(key, THEME_PROP, `${name} ${key}`);
  }
});

test("a second theme replaces the first, and a refused theme leaves the first in place", () => {
  const workspace = doc.createElement("main");
  assert.equal(applyTheme(workspace, readGui("good", "g03-compact.json")).applied, true);
  assert.equal(workspace.style.props.get("--theme-density"), "compact");
  assert.equal(workspace.style.props.get("--theme-radius"), "2px");
  assert.equal(applyTheme(workspace, readGui("good", "g01-dusk.json")).applied, true);
  assert.equal(workspace.style.props.has("--theme-density"), false);
  assert.equal(workspace.style.props.has("--theme-radius"), false);
  assert.equal(workspace.style.props.get("--theme-bg"), "#101418");
  const kept = styleEntries(workspace);
  assert.equal(applyTheme(workspace, readGui("bad", "b08-alpha-hex.json")).applied, false);
  assert.deepEqual(styleEntries(workspace), kept);
  assert.equal(applyTheme(workspace, null).applied, false);
  assert.deepEqual(styleEntries(workspace), kept);
});

test("a bare colour map is not a gui.theme/1 document", () => {
  const workspace = doc.createElement("main");
  const result = applyTheme(workspace, THEME);
  assert.equal(result.applied, false);
  assert.equal(result.reason, "unknown_key");
  assert.equal(workspace.style.props.size, 0);
});

test("a style write that throws leaves no partial theme and does not drop an earlier one", () => {
  const workspace = doc.createElement("main");
  assert.equal(applyTheme(workspace, readGui("good", "g01-dusk.json")).applied, true);
  const kept = styleEntries(workspace);
  const real = workspace.style.setProperty.bind(workspace.style);
  let thrown = false;
  workspace.style.setProperty = (name, value) => {
    if (name === "--theme-muted" && !thrown) {
      thrown = true;
      throw new Error("interrupted");
    }
    return real(name, value);
  };
  assert.throws(() => applyTheme(workspace, readGui("good", "g02-paper.json")), /interrupted/);
  assert.deepEqual(styleEntries(workspace), kept);
});

test("shared good layout fixtures apply, and an unregistered panel does not", () => {
  const page = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
  assert.match(page, /Closing this window stops nothing/);
  assert.match(page, /class="beta"/);
  const files = guiFiles("good", "layout");
  assert.deepEqual(files, [
    "h01-two-columns.json", "h02-no-panels.json", "h03-one-panel-in-column-2.json", "h04-three-columns-and-a-span.json",
  ]);
  for (const name of files) {
    const win = windowDocument();
    const before = coreSnapshot(win);
    const hidden = sectionSnapshot(win).map((row) => [row.view, row.hidden]);
    const workspace = win.getElementById("workspace");
    const needsPanel = name === "h03-one-panel-in-column-2.json" || name === "h04-three-columns-and-a-span.json";
    if (needsPanel) {
      assert.equal(applyLayout(workspace, readGui("good", name)).applied, false, `${name} without a registry`);
      assert.deepEqual(coreSnapshot(win), before, name);
    }
    const result = applyLayout(workspace, readGui("good", name), { knownPanels: ["fixture.panel"] });
    assert.equal(result.applied, true, name);
    assert.deepEqual(result, { applied: true }, name);
    assert.deepEqual(coreSnapshot(win), before, name);
    assert.deepEqual(sectionSnapshot(win).map((row) => [row.view, row.hidden]), hidden, name);
    assert.equal(win.getElementById("stop-confirm").hidden, true, name);
    assert.equal(win.getElementById("core-approval").hidden, false, name);
    assert.equal(win.root.find((node) => node.tagName === "FOOTER").length, 1, name);
    for (const node of [workspace, ...win.querySelectorAll("main > section")]) {
      for (const key of node.style.props.keys()) {
        assert.ok(key === "display" || key === "grid-template-columns" || key === "grid-column", `${name} ${key}`);
      }
    }
  }
  const wide = doc.createElement("main");
  assert.equal(applyLayout(wide, readGui("good", "h01-two-columns.json")).applied, true);
  assert.equal(wide.getAttribute("data-columns"), "2");
  assert.equal(wide.getAttribute("data-draw-columns"), "2");
  assert.equal(wide.style.props.get("grid-template-columns"), "repeat(2, minmax(0, 1fr))");
  const narrow = doc.createElement("main");
  const narrowWin = windowDocument();
  const narrowBefore = coreSnapshot(narrowWin);
  assert.equal(applyLayout(narrowWin.getElementById("workspace"), readGui("good", "h01-two-columns.json"), { viewportWidth: 375 }).applied, true);
  assert.equal(narrowWin.getElementById("workspace").getAttribute("data-draw-columns"), "1");
  assert.equal(narrowWin.getElementById("workspace").style.props.get("grid-template-columns"), "minmax(0, 1fr)");
  assert.deepEqual(coreSnapshot(narrowWin), narrowBefore);
  assert.equal(applyLayout(narrow, readGui("good", "h02-no-panels.json")).applied, true);
  assert.equal(narrow.getAttribute("data-columns"), "1");
});

test("shared bad layout fixtures are refused and do not hide Stop, disclaimers or the approval region", () => {
  const files = guiFiles("bad", "layout");
  assert.ok(files.length >= 10, String(files.length));
  const expectReason = {
    "b01-stop.json": "panel",
    "b02-approval.json": "panel",
    "b03-header.json": "panel",
    "b04-footer.json": "panel",
    "b05-z-index.json": "unknown_key",
    "b06-pixels.json": "unknown_key",
    "b07-overlay.json": "unknown_key",
    "b08-hide-core.json": "unknown_key",
    "b09-columns-zero.json": "columns",
    "b10-span-past-columns.json": "span",
  };
  for (const name of files) {
    const win = windowDocument();
    const before = coreSnapshot(win);
    const sections = sectionSnapshot(win);
    const workspace = win.getElementById("workspace");
    const result = applyLayout(workspace, readGui("bad", name), { knownPanels: ["fixture.panel"] });
    assert.equal(result.applied, false, name);
    assert.ok(LAYOUT_REASONS.has(result.reason), `${name} ${result.reason}`);
    if (expectReason[name]) assert.equal(result.reason, expectReason[name], name);
    assert.deepEqual(coreSnapshot(win), before, name);
    assert.deepEqual(sectionSnapshot(win), sections, name);
    assert.equal(workspace.getAttribute("data-columns"), null, name);
    assert.equal(workspace.style.props.size, 0, name);
  }
});

test("a layout arranges workspace sections and cannot hide the core or the floor", () => {
  const win = windowDocument();
  const before = coreSnapshot(win);
  const workspace = win.getElementById("workspace");
  const document = {
    schema: "gui.layout/1",
    name: "desk",
    title: "Desktop sections",
    columns: 2,
    slots: [
      { panel: "runs", column: 1, span: 1, order: 1, collapsed: false },
      { panel: "sessions", column: 2, span: 1, order: 0, collapsed: true },
      { panel: "approvals", column: 1, span: 1, order: 0, collapsed: false },
    ],
  };
  assert.equal(applyLayout(workspace, document).applied, true);
  assert.deepEqual(coreSnapshot(win), before);
  const byView = Object.fromEntries(win.querySelectorAll("main > section").map((section) => [section.dataset.view, section]));
  assert.equal(byView.sessions.getAttribute("data-collapsed"), "true");
  assert.equal(byView.sessions.hidden, true);
  assert.equal(byView.runs.hidden, false);
  assert.equal(byView.runs.getAttribute("data-column"), "1");
  assert.equal(byView.approvals.getAttribute("data-column"), "1");
  assert.equal(byView.sessions.style.props.get("grid-column"), "2 / span 1");
  assert.equal(win.getElementById("core-approval").style.props.size, 0);
  assert.equal(win.getElementById("stop-confirm").hidden, true);
  const order = win.querySelectorAll("main > section").map((section) => section.dataset.view);
  assert.deepEqual(order.slice(0, 3), ["approvals", "runs", "sessions"]);
  const refused = { ...document, slots: [{ panel: "approval", column: 1, span: 1, order: 0, collapsed: false }] };
  const placed = sectionSnapshot(win);
  assert.equal(applyLayout(workspace, refused).applied, false);
  assert.deepEqual(sectionSnapshot(win), placed);
  assert.deepEqual(coreSnapshot(win), before);
});

test("a refused layout leaves an applied layout in place, and a narrow width does not hide Stop", () => {
  const win = windowDocument();
  const workspace = win.getElementById("workspace");
  const before = coreSnapshot(win);
  assert.equal(applyLayout(workspace, readGui("good", "h01-two-columns.json")).applied, true);
  const kept = workspace.getAttribute("data-columns");
  assert.equal(applyLayout(workspace, readGui("bad", "b01-stop.json")).applied, false);
  assert.equal(workspace.getAttribute("data-columns"), kept);
  assert.deepEqual(coreSnapshot(win), before);
  assert.equal(applyLayout(workspace, null).applied, false);
  assert.equal(workspace.getAttribute("data-columns"), kept);
});

test("each fixture directory holds one schema", () => {
  for (const kind of ["theme", "layout"]) {
    const other = kind === "theme" ? "gui.layout/1" : "gui.theme/1";
    for (const dir of ["good", "bad"]) {
      const names = listGui(dir, kind);
      assert.ok(names.length > 0, `${kind}/${dir}`);
      for (const name of names) {
        const raw = readGui(dir, name, kind);
        const prefix = kind === "theme" ? "gui.theme/" : "gui.layout/";
        assert.equal(raw.includes(other), false, `${kind}/${dir}/${name}`);
        assert.equal(schemaOf(raw).startsWith(prefix), true, `${kind}/${dir}/${name}`);
      }
    }
  }
});

test("h01 spans the one shown section when it places no desktop section", () => {
  const win = windowDocument();
  const workspace = win.getElementById("workspace");
  assert.equal(applyLayout(workspace, readGui("good", "h01-two-columns.json")).applied, true);
  const shown = win.querySelectorAll("main > section").filter((section) => section.hidden !== true);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].getAttribute("data-view"), "runs");
  assert.equal(shown[0].style.props.get("grid-column"), "1 / span 2");
  assert.equal(workspace.style.props.get("grid-template-columns"), "repeat(2, minmax(0, 1fr))");
});

test("a second layout throw still restores every section", () => {
  const win = windowDocument();
  const workspace = win.getElementById("workspace");
  const first = {
    schema: "gui.layout/1", name: "desk", title: "Desktop sections", columns: 2,
    slots: [
      { panel: "runs", column: 1, span: 1, order: 0, collapsed: false },
      { panel: "sessions", column: 2, span: 1, order: 0, collapsed: false },
    ],
  };
  assert.equal(applyLayout(workspace, first).applied, true);
  const real = workspace.style.setProperty.bind(workspace.style);
  workspace.style.setProperty = (name, value) => {
    if (name === "grid-template-columns") throw new Error("interrupted");
    return real(name, value);
  };
  const second = {
    schema: "gui.layout/1", name: "next", title: "Plugins", columns: 1,
    slots: [{ panel: "plugins", column: 1, span: 1, order: 0, collapsed: false }],
  };
  assert.throws(() => applyLayout(workspace, second), /interrupted/);
  const byView = Object.fromEntries(win.querySelectorAll("main > section").map((section) => [section.dataset.view, section]));
  assert.equal(byView.runs.getAttribute("data-column"), "1");
  assert.equal(byView.sessions.getAttribute("data-column"), "2");
  assert.equal(byView.plugins.getAttribute("data-column"), null);
  assert.equal(byView.plugins.style.props.get("grid-column"), undefined);
  assert.equal(workspace.getAttribute("data-layout"), "desk");
  assert.equal(workspace.getAttribute("data-columns"), "2");
});

test("core region names are refused when the caller lists them", () => {
  for (const name of ["stop", "approval", "header", "footer"]) {
    const workspace = doc.createElement("main");
    const document = {
      schema: "gui.layout/1", name: "core", title: "Core", columns: 1,
      slots: [{ panel: name, column: 1, span: 1, order: 0, collapsed: false }],
    };
    const result = applyLayout(workspace, document, { knownPanels: [name] });
    assert.equal(result.applied, false, name);
    assert.equal(result.reason, "panel", name);
    assert.equal(workspace.getAttribute("data-columns"), null, name);
  }
});

test("columns above three are refused and a slot writes layout span and order", () => {
  const empty = doc.createElement("main");
  const wide = { schema: "gui.layout/1", name: "wide", title: "Wide", columns: 4, slots: [] };
  const refused = applyLayout(empty, wide);
  assert.equal(refused.applied, false);
  assert.equal(refused.reason, "columns");
  assert.equal(empty.getAttribute("data-columns"), null);
  const win = windowDocument();
  const workspace = win.getElementById("workspace");
  const document = {
    schema: "gui.layout/1", name: "span", title: "Span", columns: 2,
    slots: [{ panel: "runs", column: 1, span: 2, order: 4, collapsed: false }],
  };
  assert.equal(applyLayout(workspace, document).applied, true);
  const runs = win.querySelectorAll("main > section").find((section) => section.dataset.view === "runs");
  assert.equal(workspace.getAttribute("data-layout"), "span");
  assert.equal(runs.getAttribute("data-span"), "2");
  assert.equal(runs.getAttribute("data-order"), "4");
});
