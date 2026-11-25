// The views draw text only, the approval region draws only the opened request's rendered lines, and no theme
// reaches the approval region.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  APPROVAL_EMPTY, THEME_TOKENS, applyTheme, contrast, renderApproval, renderApprovalList, renderConditions,
  renderRuns, renderTranscript,
} from "../../ui/js/views.js";
import { fakeDocument as doc, fixture } from "./support.mjs";

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
  assert.deepEqual(applyTheme(workspace, THEME), { applied: true });
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
