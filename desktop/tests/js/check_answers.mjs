// Every view draws SPINE's recorded answers (fixtures/answers.json, from #368) as text, with the conditions
// each answer carries, and the approval region draws only an opened request's rendered lines.

import assert from "node:assert/strict";
import { test } from "node:test";

import { conditionsFor, keyedConditions } from "../../ui/js/model.js";
import {
  EPOCH_ADVANCED, FOLLOW_TIMED_OUT, renderApproval, renderApprovalList, renderConditions, renderDiff, renderPlugins,
  renderRoutes, renderRuns, renderSession, renderSessions, renderTranscript,
} from "../../ui/js/views.js";
import { fakeDocument as doc, fixture } from "./support.mjs";

const ANSWERS = fixture("answers.json");
const ACTIONS = fixture("actions.json");
const METHOD = Object.fromEntries(ACTIONS.actions.map((a) => [a.action, a.method]));
const ESC = String.fromCharCode(0x1b);

function recorded(file) {
  const hit = ANSWERS.answers.find((a) => a.file === file);
  assert.ok(hit, `${file} is recorded`);
  return hit.answer;
}

// The view that draws each action's answer, beside the conditions every answer shows.
const VIEWS = {
  "runs.refresh": (box, a) => renderRuns(doc, box, a, () => {}),
  "run.open": (box, a) => renderRuns(doc, box, { code: a.code, runs: a.run ? [a.run] : [] }, () => {}),
  "routes.refresh": (box, a) => renderRoutes(doc, box, a),
  "plugins.refresh": (box, a) => renderPlugins(doc, box, a),
  "sessions.refresh": (box, a) => renderSessions(doc, box, a, () => {}),
  "session.open": (box, a) => renderSession(doc, box, a),
  "session.start": (box, a) => renderSession(doc, box, a),
  "session.turn": (box, a) => renderSession(doc, box, a),
  "session.interrupt": (box, a) => renderSession(doc, box, a),
  "transcript.read": (box, a) => renderTranscript(doc, box, a),
  "transcript.follow": (box, a) => renderTranscript(doc, box, a),
  "approvals.refresh": (box, a) => renderApprovalList(doc, box, a),
  "approval.open": (box, a) => renderApproval(doc, box, a.approval ?? null, { held: "approval_surface_undecided" }),
  "approval.answer": (box, a) => renderApproval(doc, box, a.approval ?? null, { held: "approval_surface_undecided" }),
  "diff.read": (box, a) => renderDiff(doc, box, a),
};

test("the recorded answers are SPINE's at the head the actions name, each for its action's method", () => {
  assert.equal(ANSWERS.spine_head, ACTIONS.spine_head);
  for (const a of ANSWERS.answers) {
    assert.equal(a.method, METHOD[a.action], a.file);
    assert.match(a.sha256, /^[0-9a-f]{64}$/);
  }
});

test("every recorded answer is drawn, with its conditions, as text only", () => {
  for (const { action, file, answer } of ANSWERS.answers) {
    const box = doc.createElement("div");
    renderConditions(doc, box, answer, METHOD[action]);
    const view = VIEWS[action];
    if (view) view(doc.createElement("div"), answer);
    assert.ok(!box.textContent.includes(ESC), file);
  }
});

test("a run answer reads as the CLI reads it; any other answer by the desktop's rule, without run-only words", () => {
  for (const { action, file, answer } of ANSWERS.answers) {
    const method = METHOD[action];
    const got = conditionsFor(method, answer);
    if (method.startsWith("run.")) assert.deepEqual(got, keyedConditions(answer), file);
    else assert.ok(got.every(([, , text]) => !/run may still be running|=None/.test(text)), file);
    const refused = answer.code !== "ok" && !["started", "stopped", "pending"].includes(answer.code);
    assert.equal(got.some(([label, subject]) => label === "REFUSED" && subject === "answer"), refused, file);
  }
});

test("an interrupt that fell back to a Stop names its session, why, and the run whose Stop is not confirmed", () => {
  const answer = recorded("h55-interrupt-on-a-driver-that-cannot-interrupt.json");
  const got = conditionsFor("session.interrupt", answer);
  assert.deepEqual(got, [
    ["UNCERTAIN", "session=ses-7f3a9c2e51d04b6a",
      "session.interrupt for session=ses-7f3a9c2e51d04b6a not confirmed: interrupt fell_back_to_stop"],
    ["UNCERTAIN", "run=run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7",
      "run=run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7 state=pending reason=cgroup_populated: stop requested, not confirmed"],
  ]);
  // A delivered interrupt is confirmed: no condition at all.
  assert.deepEqual(conditionsFor("session.interrupt", recorded("h23-session-interrupt-ok.json")), []);
});

test("a subtree Stop still pending names its session and each unfinished run with its reason, and no stopped run", () => {
  const answer = recorded("h13-session-stop-of-a-subtree-still-pending.json");
  const got = conditionsFor("session.stop", answer);
  assert.deepEqual(got, [
    ["UNCERTAIN", "session=ses-7f3a9c2e51d04b6a", "session.stop for session=ses-7f3a9c2e51d04b6a not confirmed: pending"],
    ["UNCERTAIN", "run=run-0a1b2c3d4e5f60718293a4b5c6d7e8f9",
      "run=run-0a1b2c3d4e5f60718293a4b5c6d7e8f9 state=pending reason=cgroup_populated: stop requested, not confirmed"],
  ]);
  assert.deepEqual(answer.pending, ["run-0a1b2c3d4e5f60718293a4b5c6d7e8f9"]);
  // A run listed as pending with no Stop result of its own is still named; a code no client knows is not stopped.
  const bare = conditionsFor("session.stop", { code: "pending", session_id: "ses-1", pending: ["run-9"],
    runs: [{ code: "settling", run_id: "run-8", state: "settling" }, { code: "ended", run_id: "run-7", state: "ended" }] });
  assert.deepEqual(bare.map(([, subject]) => subject), ["session=ses-1", "run=run-8", "run=run-9"]);
});

test("a launch refusal shows its start error, as the CLI's printed answer does", () => {
  const box = doc.createElement("div");
  renderConditions(doc, box, recorded("c04-run-start-no-route-for-the-engine.json"), "run.start");
  const labels = box.find((n) => n.tagName === "LI").map((n) => n.getAttribute("data-label"));
  assert.deepEqual(labels, ["REFUSED", "START_ERROR"]);
  assert.match(box.textContent, /start error: route_not_configured pi/);
});

test("the plugin view says what the host allows before any plugin, and names a refused plugin by its source", () => {
  const empty = doc.createElement("ul");
  assert.equal(renderPlugins(doc, empty, recorded("c16-plugin-list.json")), 0);
  assert.match(empty.textContent, /approvals: none: no surface can approve or veto yet/);
  assert.match(empty.textContent, /containment: none: plugins run in the daemon process and uid/);
  assert.match(empty.textContent, /No plugin is loaded\./);
  const two = doc.createElement("ul");
  assert.equal(renderPlugins(doc, two, recorded("h51-plugin-list-with-a-loaded-and-a-refused-plugin.json")), 2);
  const plugins = two.children.filter((n) => n.getAttribute("class") === "plugin");
  assert.deepEqual(plugins.map((p) => p.getAttribute("data-state")), ["loaded", "refused"]);
  assert.match(plugins[0].textContent, /^notes\.search loaded \(signed\)/);
  assert.match(plugins[1].textContent, /old-thing refused: plugin_file_missing manifest\.json/);
  assert.match(plugins[1].textContent, /refuse daemon plugin_file_missing/);
});

test("sessions, turns, diffs and routes draw the recorded rows", () => {
  const sessions = doc.createElement("div");
  const asked = [];
  assert.equal(renderSessions(doc, sessions, recorded("h27-session-list-ok.json"), (n, i) => asked.push([n, i])), 1);
  assert.match(sessions.textContent, /ses-7f3a9c2e51d04b6a.*live.*claude_code/s);
  sessions.find((n) => n.tagName === "BUTTON" && n.textContent === "Stop")[0].click();
  assert.deepEqual(asked, [["session.stop", { session_id: "ses-7f3a9c2e51d04b6a", scope: "self" }]]);
  const turns = doc.createElement("div");
  assert.equal(renderSession(doc, turns, recorded("h28-session-status-ok.json")), 3);
  assert.match(turns.textContent, /turn-000c running outcome=None/);
  const diff = doc.createElement("div");
  assert.equal(renderDiff(doc, diff, recorded("h36-workspace-diff-ok.json")), 2);
  assert.match(diff.textContent, /modified src\/io\.py \+12 -3/);
  assert.equal(renderDiff(doc, doc.createElement("div"), recorded("h05-a-method-the-daemon-has-not-built-yet.json")), 0);
  const routes = doc.createElement("ul");
  assert.ok(renderRoutes(doc, routes, recorded("c15-route-list.json")) >= 2);
  assert.match(routes.textContent, /engine:codex not configured beta: route_not_configured/);
});

test("a follow that waited out says so, and a transcript line from the engine is marked", () => {
  const waited = doc.createElement("ol");
  assert.equal(renderTranscript(doc, waited, recorded("h12-events-follow-that-timed-out.json")), 0);
  assert.equal(waited.textContent, FOLLOW_TIMED_OUT);
  const read = doc.createElement("ol");
  assert.equal(renderTranscript(doc, read, recorded("h11-events-read-as-a-transcript.json")), 1);
  assert.deepEqual(read.children.map((c) => c.getAttribute("data-trust")), ["untrusted"]);
  const restarted = doc.createElement("ol");
  renderTranscript(doc, restarted, { code: "ok", lines: [], epoch_advanced: true, timed_out: false });
  assert.equal(restarted.textContent, EPOCH_ADVANCED);
});

test("the opened request draws its nine rendered lines and none of its display entries", () => {
  const answer = recorded("h07-approval-open.json");
  const region = doc.createElement("aside");
  assert.equal(renderApproval(doc, region, answer.approval, { held: "approval_surface_undecided" }), answer.approval.rendered.length);
  assert.equal(answer.approval.rendered.length, 9);
  for (const entry of answer.approval.display) assert.ok(!region.textContent.includes(entry.label), entry.label);
  const list = doc.createElement("ul");
  renderApprovalList(doc, list, recorded("h10-approval-list-returns-summaries.json"));
  assert.equal(list.textContent, "apr-00c4 pending until 2026-11-13T07:49:30.000Z");
});

test("a non-run answer that is pending says why, a stale one says so, and one that is not an object is refused", () => {
  assert.deepEqual(conditionsFor("session.turn", { code: "pending", detail: "queued behind turn-000c" }),
    [["UNCERTAIN", "answer", "session.turn not confirmed: queued behind turn-000c"]]);
  assert.deepEqual(conditionsFor("session.turn", { code: "pending", reason: "cgroup_populated" }),
    [["UNCERTAIN", "answer", "session.turn not confirmed: cgroup_populated"]]);
  assert.deepEqual(conditionsFor("session.turn", { code: "pending", detail: "first", reason: "second" })[0][2],
    "session.turn not confirmed: first");
  assert.deepEqual(conditionsFor("session.list", { code: "ok", stale: true, sessions: [] }),
    [["STALE", "answer", "API marked this answer stale"]]);
  for (const odd of [null, "ok", 7, ["ok"]]) {
    assert.deepEqual(conditionsFor("session.list", odd), [["REFUSED", "answer", "the answer is not an object"]], String(odd));
  }
});

test("every statement the host makes about plugins is drawn, in the host's order, before any plugin", () => {
  const answer = recorded("h51-plugin-list-with-a-loaded-and-a-refused-plugin.json");
  const list = doc.createElement("ul");
  renderPlugins(doc, list, answer);
  const host = list.children.filter((n) => n.getAttribute("class") === "host").map((n) => n.textContent);
  assert.deepEqual(host, [
    `approvals: ${answer.approval_surface}`,
    `containment: ${answer.containment}`,
    `authority profile: ${answer.authority_profile}`,
    `verification: ${answer.verification}`,
    `approvals drawn: ${answer.approvals_drawn}`,
    "core poisoned: None",
  ]);
  assert.equal(list.children.findIndex((n) => n.getAttribute("class") === "plugin"), host.length);
});

test("a session row opens that session; its record shows its revision and each turn's outcome; a diff shows its base and a cut list", () => {
  const sessions = recorded("h27-session-list-ok.json");
  const box = doc.createElement("div");
  const asked = [];
  renderSessions(doc, box, sessions, (n, i) => asked.push([n, i]));
  box.find((n) => n.tagName === "BUTTON" && n.textContent === "Open")[0].click();
  assert.deepEqual(asked, [["session.open", { session_id: sessions.sessions[0].session_id }]]);

  const status = recorded("h28-session-status-ok.json");
  const turns = doc.createElement("div");
  renderSession(doc, turns, status);
  assert.equal(turns.children[0].textContent,
    `${status.session.session_id} ${status.session.state} revision ${status.session.revision}`);
  assert.deepEqual(turns.children[1].children.map((li) => li.textContent),
    status.session.turns.map((t) => `${t.turn_id} ${t.state} outcome=${t.outcome === null ? "None" : t.outcome}`));

  const diff = recorded("h36-workspace-diff-ok.json");
  const whole = doc.createElement("div");
  renderDiff(doc, whole, diff);
  assert.equal(whole.children[0].textContent, `against ${diff.diff.base_ref}`);
  assert.ok(!whole.textContent.includes("cut short"));
  const cut = doc.createElement("div");
  renderDiff(doc, cut, { ...diff, diff: { ...diff.diff, truncated: true } });
  assert.equal(cut.children.at(-1).textContent, "The list was cut short by the daemon.");
});
