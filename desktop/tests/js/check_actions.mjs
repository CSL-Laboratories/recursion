// Each desktop action's builder makes exactly the golden request of fixtures/actions.json, and the send path
// passes only methods the desktop may call and never a held action.

import assert from "node:assert/strict";
import { test } from "node:test";

import { ActionError, HELD, METHODS, SURFACE, commandIdMinter, makeActions } from "../../ui/js/actions.js";
import { inertTransport, INERT_DETAIL, RefusedHere, replayTransport, send } from "../../ui/js/transport.js";
import { fixture } from "./support.mjs";

const ACTIONS = fixture("actions.json");
const ROWS = fixture("spine-rows.json");
const OPENED = {
  approval_id: "apr-00c4",
  digest: "39f84aee4acb31972adc920f7acfa0fcc4f4df94e3c178c7f60f425ae12eda77",
  revision: 1,
  rendered: [{ text: "Tool call waiting for an answer", trust: "trusted" }],
};
const SUMMARY = { approval_id: "apr-00c4", state: "pending", revision: 1, expires_at: "2026-11-14T12:00:00Z" };

// The input a view would give each builder to ask for the golden request.
const INPUTS = {
  "runs.refresh": {},
  "runs.refresh.next": { after: "run-1" },
  "run.open": { run_id: "run-1" },
  "run.stop": { run_id: "run-1" },
  "run.start": { tenant: "t1", engine: "codex", prompt: "hi" },
  "run.start.keyed": { tenant: "t1", engine: "codex", prompt: "two words", client_key: "k1" },
  "routes.refresh": {},
  "plugins.refresh": {},
  "sessions.refresh": { state: "live", limit: 20 },
  "session.open": { session_id: "ses-7f3a9c2e51d04b6a" },
  "session.start": { tenant: "t1", workspace: { path: "/home/dev/projects/atlas", mode: "worktree" }, engine: "claude_code",
    model: null, prompt: "Fix the failing test." },
  "session.turn": { session_id: "ses-7f3a9c2e51d04b6a", text: "Now update the changelog.", expected_revision: 6 },
  "session.interrupt": { session_id: "ses-7f3a9c2e51d04b6a", expected_turn_id: "turn-000c" },
  "session.stop": { session_id: "ses-7f3a9c2e51d04b6a", scope: "subtree" },
  "transcript.read": { cursor: { seq: 41, session_id: "ses-7f3a9c2e51d04b6a", epoch: 3 }, limit: 200 },
  "transcript.follow": { cursor: { epoch: 1, session_id: "run:run-5b1f0c2d9a7e4f36b8c1d2e3f4a5b6c7", seq: 0 }, wait_ms: 20000 },
  "approvals.refresh": { state: "pending" },
  "approval.open": { approval_id: "apr-00c4" },
  "approval.answer": { approval: OPENED, answer: "deny" },
  "prefs.read": {},
  "diff.read": { session_id: "ses-7f3a9c2e51d04b6a", turn_id: "turn-000b" },
};

// The minter the golden requests were written with: random part 0123456789abcdef01234567, first command.
function fixedActions() {
  const bytes = [0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0x01, 0x23, 0x45, 0x67];
  return makeActions(commandIdMinter((b) => b.set(bytes)));
}

test("every action of actions.json has a builder and an input, and nothing else does", () => {
  const names = ACTIONS.actions.map((a) => a.action).sort();
  assert.deepEqual(Object.keys(makeActions()).sort(), names);
  assert.deepEqual(Object.keys(INPUTS).sort(), names);
});

test("each builder makes the golden request, key for key and in order", () => {
  for (const action of ACTIONS.actions) {
    const built = fixedActions()[action.action](structuredClone(INPUTS[action.action]));
    assert.deepEqual(built, { method: action.method, params: action.params }, action.action);
    assert.equal(JSON.stringify(built.params), JSON.stringify(action.params), `${action.action}: key order`);
  }
});

test("the command id is a fresh random part per process and then a counter", () => {
  const mint = commandIdMinter((b) => b.fill(0xab));
  assert.equal(mint(), `desktop-${"ab".repeat(12)}.1`);
  assert.equal(mint(), `desktop-${"ab".repeat(12)}.2`);
  const a = commandIdMinter()();
  const b = commandIdMinter()();
  assert.match(a, /^desktop-[0-9a-f]{24}\.1$/);
  assert.notEqual(a, b);
  assert.match(ACTIONS.command_id_example, /^[A-Za-z0-9._:-]{8,128}$/);
});

test("the allowed methods are exactly the methods the actions use, each with a row the page may call", () => {
  assert.deepEqual([...METHODS].sort(), [...new Set(ACTIONS.actions.map((a) => a.method))].sort());
  for (const method of METHODS) {
    assert.equal(ROWS.rows[method].page_allowed, true, method);
    assert.notEqual(ROWS.rows[method].scope, "manage", method);
  }
});

test("the held actions are exactly those with a schema gap, and the surface is the desktop's", () => {
  const gaps = ACTIONS.actions.filter((a) => a.schema_gap).map((a) => a.action).sort();
  assert.deepEqual(Object.keys(HELD).sort(), gaps);
  assert.equal(SURFACE, "desktop");
});

test("an answer is built only from the opened request, never from a summary", () => {
  const actions = fixedActions();
  assert.throws(() => actions["approval.answer"]({ approval: SUMMARY, answer: "approve" }), ActionError);
  // A summary with a digest and a revision copied into it is still not the request as it was shown.
  const dressed = { ...SUMMARY, digest: OPENED.digest };
  assert.throws(() => actions["approval.answer"]({ approval: dressed, answer: "approve" }), ActionError);
  assert.throws(() => actions["approval.answer"]({ approval: { ...OPENED, rendered: [] }, answer: "approve" }), ActionError);
  assert.throws(() => actions["approval.answer"]({ approval: { ...OPENED, digest: "nope" }, answer: "approve" }), ActionError);
  assert.throws(() => actions["approval.answer"]({ approval: OPENED, answer: "maybe" }), ActionError);
  assert.throws(() => actions["approval.answer"]({ approval: { ...OPENED, revision: 0 }, answer: "deny" }), ActionError);
});

test("input the schema refuses is refused before anything is sent", () => {
  const actions = fixedActions();
  const bad = [
    ["run.open", { run_id: "run 1" }],
    ["run.stop", { run_id: "" }],
    ["run.stop", { run_id: "x".repeat(65) }],
    ["run.start", { tenant: "", engine: "codex", prompt: "hi" }],
    ["run.start", { tenant: "t1", engine: "codex", prompt: "" }],
    ["run.start.keyed", { tenant: "t1", engine: "codex", prompt: "hi" }],
    ["session.open", { session_id: "run-1" }],
    ["session.turn", { session_id: "ses-1", text: "x", expected_revision: 0 }],
    ["session.stop", { session_id: "ses-1", scope: "everything" }],
    ["transcript.follow", { cursor: { epoch: 1, session_id: "ses-1", seq: 0 }, wait_ms: 30001 }],
    ["transcript.read", { cursor: { epoch: 1, session_id: "ses-1", seq: 0, extra: 1 } }],
    ["transcript.read", { cursor: { epoch: 1, session_id: "bogus", seq: 0 } }],
    ["sessions.refresh", { state: "running" }],
    ["session.start", { tenant: "t1", engine: "Claude Code" }],
  ];
  for (const [name, input] of bad) assert.throws(() => actions[name](input), ActionError, `${name} ${JSON.stringify(input)}`);
});

test("the inert transport sends nothing and says so", async () => {
  const transport = inertTransport();
  const answer = await send(transport, "runs.refresh", fixedActions()["runs.refresh"]());
  assert.deepEqual(answer, { code: "control_api_unavailable", delivered: false, detail: INERT_DETAIL });
});

test("send refuses a held action and a method the desktop may not call, and calls nothing", async () => {
  const transport = replayTransport([]);
  await assert.rejects(send(transport, "approval.open", fixedActions()["approval.open"]({ approval_id: "apr-1" })), RefusedHere);
  await assert.rejects(send(transport, "anything", { method: "workspace.publish", params: {} }), RefusedHere);
  await assert.rejects(send(transport, "anything", { method: "run.kill", params: {} }), RefusedHere);
  assert.deepEqual(transport.calls, []);
});

test("the replay transport answers a recorded exchange and refuses any other", async () => {
  const transport = replayTransport([{ method: "run.list", params: {}, answer: { code: "ok", runs: [] } }]);
  assert.deepEqual(await send(transport, "runs.refresh", { method: "run.list", params: {} }), { code: "ok", runs: [] });
  await assert.rejects(send(transport, "run.open", { method: "run.status", params: { run_id: "run-1" } }), RefusedHere);
});
