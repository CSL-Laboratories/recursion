// The desktop's control requests: one builder per action of desktop/tests/fixtures/actions.json.
//
// Each builder returns {method, params} and nothing else: the request the CLI sends for the same action
// (proven for the run verbs by desktop/tests/check_actions.py), or the request SPINE's draft fixture shows,
// with the differences actions.json names. The proxy, not this file, frames it as
// {"op": "control", "method", "params"}. A builder refuses input the method's schema refuses, so nothing is
// sent that the daemon must refuse for its shape; it adds no field of its own.

export const SURFACE = "desktop";

// The methods the desktop's actions use: the only ones its proxy is to pass. Each has a row in SPINE's draft
// table that the page may call and that is not of the manage scope (desktop/tests/fixtures/spine-rows.json).
export const METHODS = Object.freeze([
  "run.list", "run.status", "run.stop", "run.start", "route.list", "plugin.list",
  "session.list", "session.status", "session.start", "session.turn", "session.interrupt", "session.stop",
  "events.read", "events.follow", "approval.list", "approval.open", "approval.answer",
  "surface.prefs.get", "workspace.diff",
]);

// Built but not sent: approval.v1 has no "desktop" surface yet (lane DESKTOP questions.md, Q3).
export const HELD = Object.freeze({
  "approval.open": "approval_surface_undecided",
  "approval.answer": "approval_surface_undecided",
});

const RUN_ID = /^[A-Za-z0-9_-]{1,64}$/;
const SESSION_ID = /^ses-[A-Za-z0-9_-]{1,60}$/;
const TURN_ID = /^turn-[A-Za-z0-9_-]{1,59}$/;
const APPROVAL_ID = /^apr-[A-Za-z0-9_-]{1,60}$/;
const LOG_ID = /^(ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64})$/;
const ENGINE = /^[a-z][a-z0-9_]{0,31}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SESSION_STATES = new Set(["idle", "live", "stop_pending", "ended"]);
const APPROVAL_STATES = new Set(["pending", "approved", "denied", "expired", "consumed", "vetoed"]);

export class ActionError extends Error {}

function need(ok, what) {
  if (!ok) throw new ActionError(what);
}

function text(value, what) {
  need(typeof value === "string" && value.length > 0, `${what} must be non-empty text`);
  return value;
}

function id(value, pattern, what) {
  need(typeof value === "string" && pattern.test(value), `${what} is not a valid ${what}`);
  return value;
}

function count(value, min, max, what) {
  need(Number.isInteger(value) && value >= min && value <= max, `${what} must be a whole number from ${min} to ${max}`);
  return value;
}

function cursor(value) {
  need(value !== null && typeof value === "object" && !Array.isArray(value), "cursor must be the cursor last given");
  const keys = Object.keys(value).sort().join(",");
  need(keys === "epoch,seq,session_id", "cursor must be the cursor last given, unchanged");
  // common.v1 epoch: 1 or more (a fresh log is read from epoch 1, seq 0).
  count(value.epoch, 1, Number.MAX_SAFE_INTEGER, "cursor.epoch");
  count(value.seq, 0, Number.MAX_SAFE_INTEGER, "cursor.seq");
  id(value.session_id, LOG_ID, "log id");
  return { epoch: value.epoch, session_id: value.session_id, seq: value.seq };
}

function request(method, params) {
  return { method, params };
}

function hexBytes(n, random) {
  const bytes = new Uint8Array(n);
  random(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// command_id: a random part made once per process (96 bits), then a counter (common.v1 command_id).
export function commandIdMinter(random = (b) => globalThis.crypto.getRandomValues(b)) {
  const prefix = `desktop-${hexBytes(12, random)}`;
  let n = 0;
  return () => {
    n += 1;
    return `${prefix}.${n}`;
  };
}

// The CLI's order, and client_key only when one is given (control/cli.py).
function startRun({ tenant, engine, prompt, client_key }) {
  const params = { tenant: text(tenant, "tenant"), engine: text(engine, "engine"), prompt: text(prompt, "prompt") };
  if (client_key) params.client_key = text(client_key, "client key");
  return request("run.start", params);
}

export function makeActions(mint = commandIdMinter()) {
  return Object.freeze({
    "runs.refresh": () => request("run.list", {}),
    "runs.refresh.next": ({ after }) => request("run.list", { after: id(after, RUN_ID, "run id") }),
    "run.open": ({ run_id }) => request("run.status", { run_id: id(run_id, RUN_ID, "run id") }),
    "run.stop": ({ run_id }) => request("run.stop", { run_id: id(run_id, RUN_ID, "run id") }),
    "run.start": startRun,
    "run.start.keyed": (input) => {
      need(Boolean(input.client_key), "a keyed start needs a client key");
      return startRun(input);
    },
    "routes.refresh": () => request("route.list", {}),
    "plugins.refresh": () => request("plugin.list", {}),
    "sessions.refresh": ({ state, limit }) => {
      const params = {};
      if (state !== undefined) {
        need(SESSION_STATES.has(state), "state is not a session state");
        params.state = state;
      }
      if (limit !== undefined) params.limit = count(limit, 1, 1000, "limit");
      return request("session.list", params);
    },
    "session.open": ({ session_id }) => request("session.status", { session_id: id(session_id, SESSION_ID, "session id") }),
    "session.start": ({ tenant, workspace, engine, model = null, prompt }) => {
      const params = { command_id: mint(), tenant: text(tenant, "tenant") };
      if (workspace !== undefined) {
        need(workspace !== null && typeof workspace === "object", "workspace must name a folder");
        const ws = { path: text(workspace.path, "workspace path") };
        if (workspace.mode !== undefined) {
          need(workspace.mode === "direct" || workspace.mode === "worktree", "workspace mode is direct or worktree");
          ws.mode = workspace.mode;
        }
        if (workspace.base_ref !== undefined) ws.base_ref = text(workspace.base_ref, "base ref");
        params.workspace = ws;
      }
      if (engine !== undefined) {
        need(model === null || (typeof model === "string" && model.length > 0), "model must be a name or null");
        params.hand_pick = { engine: id(engine, ENGINE, "engine name"), model };
      }
      if (prompt !== undefined) params.prompt = text(prompt, "prompt");
      return request("session.start", params);
    },
    "session.turn": ({ session_id, text: words, expected_revision }) => request("session.turn", {
      command_id: mint(),
      session_id: id(session_id, SESSION_ID, "session id"),
      text: text(words, "text"),
      expected_revision: count(expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected revision"),
    }),
    "session.interrupt": ({ session_id, expected_turn_id }) => request("session.interrupt", {
      command_id: mint(),
      session_id: id(session_id, SESSION_ID, "session id"),
      expected_turn_id: id(expected_turn_id, TURN_ID, "turn id"),
    }),
    "session.stop": ({ session_id, scope }) => {
      need(scope === "self" || scope === "subtree", "scope is self or subtree");
      return request("session.stop", { command_id: mint(), session_id: id(session_id, SESSION_ID, "session id"), scope });
    },
    "transcript.read": ({ cursor: at, limit }) => {
      const params = { cursor: cursor(at) };
      if (limit !== undefined) params.limit = count(limit, 1, 1000, "limit");
      params.projection = "transcript";
      return request("events.read", params);
    },
    "transcript.follow": ({ cursor: at, wait_ms }) => request("events.follow", {
      cursor: cursor(at),
      projection: "transcript",
      wait_ms: count(wait_ms, 0, 30000, "wait"),
    }),
    "approvals.refresh": ({ state = "pending" } = {}) => {
      need(APPROVAL_STATES.has(state), "state is not an approval state");
      return request("approval.list", { state });
    },
    "approval.open": ({ approval_id }) => request("approval.open", {
      command_id: mint(),
      approval_id: id(approval_id, APPROVAL_ID, "approval id"),
      surface: SURFACE,
    }),
    // Answers only the record approval.open gave: its digest and revision. A summary from approval.list has
    // neither the lines that were shown nor the digest, and is refused.
    "approval.answer": ({ approval, answer }) => {
      need(approval !== null && typeof approval === "object", "an answer needs the opened request");
      need(Array.isArray(approval.rendered) && approval.rendered.length > 0, "an answer needs the request as it was shown");
      need(answer === "approve" || answer === "deny", "the answer is approve or deny");
      return request("approval.answer", {
        command_id: mint(),
        approval_id: id(approval.approval_id, APPROVAL_ID, "approval id"),
        digest: id(approval.digest, SHA256, "digest"),
        answer,
        expected_revision: count(approval.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
        surface: SURFACE,
      });
    },
    "prefs.read": () => request("surface.prefs.get", { surface: SURFACE }),
    "diff.read": ({ session_id, turn_id }) => {
      const params = { session_id: id(session_id, SESSION_ID, "session id") };
      if (turn_id !== undefined) params.turn_id = id(turn_id, TURN_ID, "turn id");
      return request("workspace.diff", params);
    },
  });
}
