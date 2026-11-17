// How the desktop reads control API answers: the CLI's and the TUI's rules (core/recursion_core/tui/protocol.py).
//
// The same labels and words (REFUSED, UNCERTAIN, STALE, UNRECONCILED, UNAVAILABLE), so a condition reads the
// same on every surface and without colour; and the same walk over run.list's pages, so every run is read.
// Held to desktop/tests/fixtures/conditions.json and walks.json, both checked against the TUI's functions.

import { field, pyStr } from "./sanitize.js";

export const UNAVAILABLE = "control_api_unavailable";
export const SUCCESS = new Set(["ok", "started", "stopped"]);
export const UNCERTAIN_CODES = new Set(["pending"]);
export const LABELS = Object.freeze(["UNAVAILABLE", "UNRECONCILED", "STALE", "REFUSED", "UNCERTAIN"]);
export const LIST_FOLLOW_MAX_PAGES = 100000;
export const LIST_INCOMPLETE = "list_incomplete";

function isMapping(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function has(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function get(obj, key, fallback) {
  return has(obj, key) ? obj[key] : fallback;
}

function runsOf(answer) {
  const runs = answer.runs;
  if (Array.isArray(runs)) return runs.filter(isMapping);
  return isMapping(answer.run) ? [answer.run] : [];
}

// A Stop was asked for this run and not confirmed.
export function stopUnconfirmed(run) {
  if (run.state === "stopped") return false;
  return run.state === "stop_pending" || isMapping(run.stop);
}

// [label, subject, text] for each condition one answer exposes. Nothing the answer says is dropped. This is
// the TUI's rule for an answer to a run method, held to its vectors; a view reads every answer through
// conditionsFor below, which hands only run answers to it.
export function keyedConditions(answer) {
  const code = get(answer, "code", null);
  const found = [];
  if (code === UNAVAILABLE) {
    let text = `control API unavailable: ${field(get(answer, "detail", "no answer"))}`;
    if (answer.delivered === "unknown") {
      text += "; delivery unknown: the command may already have been carried out";
    }
    found.push(["UNAVAILABLE", "api", text]);
  } else if (UNCERTAIN_CODES.has(code)) {
    found.push(["UNCERTAIN", `run=${pyStr(get(answer, "run_id", null))}`,
      `stop not confirmed for run=${field(get(answer, "run_id", null))}: ` +
      `reason=${field(get(answer, "reason", "unknown"))}; run may still be running`]);
  } else if (!SUCCESS.has(code)) {
    const what = get(answer, "detail", get(answer, "run_id", ""));
    found.push(["REFUSED", "answer", `${field(code)} ${field(what)}`.trim()]);
  }
  if (answer.stale === true) found.push(["STALE", "answer", "API marked this answer stale"]);
  for (const run of runsOf(answer)) {
    if (stopUnconfirmed(run)) {
      const reason = isMapping(run.stop) ? get(run.stop, "reason", null) : null;
      found.push(["UNRECONCILED", `run=${pyStr(get(run, "run_id", null))}`,
        `run=${field(get(run, "run_id", null))} state=${field(get(run, "state", null))} ` +
        `reason=${field(reason)}: stop requested, cleanup not confirmed`]);
    }
  }
  if (answer.reconciliation === "unreconciled") found.push(["UNRECONCILED", "api", "API reports unreconciled state"]);
  return found;
}

// The session an answer is about: session.stop names it, the other session methods return the record.
function sessionOf(answer) {
  if (typeof answer.session_id === "string") return answer.session_id;
  return isMapping(answer.session) && typeof answer.session.session_id === "string" ? answer.session.session_id : null;
}

// The Stop results (stop.result.v1) an answer carries: session.stop's one per run, and the Stop
// session.interrupt fell back to.
function stopResults(answer) {
  const found = Array.isArray(answer.runs) ? answer.runs.filter(isMapping) : [];
  if (isMapping(answer.stop)) found.push(answer.stop);
  return found;
}

// stop.result.v1: "stopped" was attested, "ended" had ended before the Stop; any other code, known or not,
// is a run that is not stopped.
const STOP_SETTLED = new Set(["stopped", "ended"]);

// The conditions of an answer to `method`. A run method's answer is read exactly as the CLI and the TUI read
// it. The CLI has no command for the other methods, so their answers are read by the desktop's own rule,
// with the same labels and no run-only words: a pending answer is not confirmed and names its session and
// why, any code but ok is refused, every Stop result in it that is not settled is a Stop not confirmed (with
// its run, state and reason), and so is every run it lists as pending.
export function conditionsFor(method, answer) {
  if (typeof method === "string" && method.startsWith("run.")) return keyedConditions(isMapping(answer) ? answer : {});
  if (!isMapping(answer)) return [["REFUSED", "answer", "the answer is not an object"]];
  const code = get(answer, "code", null);
  const session = sessionOf(answer);
  const found = [];
  if (code === UNAVAILABLE) {
    found.push(["UNAVAILABLE", "api", `control API unavailable: ${field(get(answer, "detail", "no answer"))}`]);
  } else if (UNCERTAIN_CODES.has(code)) {
    // Why: the answer's detail or reason, else what became of an interrupt (fell_back_to_stop), else pending.
    const outcome = isMapping(answer.interrupt) && has(answer.interrupt, "outcome") ? answer.interrupt.outcome : undefined;
    let why = "pending";
    if (has(answer, "detail")) why = field(answer.detail);
    else if (has(answer, "reason")) why = field(answer.reason);
    else if (outcome !== undefined) why = `interrupt ${field(outcome)}`;
    found.push(session === null
      ? ["UNCERTAIN", "answer", `${field(method)} not confirmed: ${why}`]
      : ["UNCERTAIN", `session=${pyStr(session)}`, `${field(method)} for session=${field(session)} not confirmed: ${why}`]);
  } else if (code !== "ok") {
    found.push(["REFUSED", "answer", `${field(code)} ${field(get(answer, "detail", ""))}`.trim()]);
  }
  if (answer.stale === true) found.push(["STALE", "answer", "API marked this answer stale"]);
  const named = new Set();
  for (const stop of stopResults(answer)) {
    const runId = get(stop, "run_id", null);
    named.add(runId);
    if (STOP_SETTLED.has(stop.code)) continue;
    found.push(["UNCERTAIN", `run=${pyStr(runId)}`,
      `run=${field(runId)} state=${field(get(stop, "state", null))} reason=${field(get(stop, "reason", null))}: ` +
      "stop requested, not confirmed"]);
  }
  if (Array.isArray(answer.pending)) {
    for (const runId of answer.pending) {
      if (!named.has(runId)) found.push(["UNCERTAIN", `run=${pyStr(runId)}`, `run=${field(runId)}: stop requested, not confirmed`]);
    }
  }
  return found;
}

// Read every run: run.list, then run.list {after: next_after} until a page has none. call(method, params)
// returns the answer or a promise of it.
export async function listAllRuns(call, params = {}) {
  const base = { ...params };
  let answer = await call("run.list", { ...base });
  if (!isMapping(answer) || answer.code !== "ok" || !has(answer, "next_after")) return answer;
  const joined = {};
  for (const [k, v] of Object.entries(answer)) if (k !== "next_after") joined[k] = v;
  const runs = [];
  const seen = new Set();
  let cursor = null;
  let pages = 1;
  for (;;) {
    const page = answer.runs;
    if (!Array.isArray(page)) return { code: LIST_INCOMPLETE, detail: "runs_not_a_list", pages };
    for (const run of page) {
      const runId = isMapping(run) ? run.run_id : undefined;
      if (typeof runId !== "string") return { code: LIST_INCOMPLETE, detail: "run_without_id", pages };
      if (seen.has(runId)) return { code: LIST_INCOMPLETE, detail: `run_listed_twice:${runId}`, pages };
      seen.add(runId);
      runs.push(run);
    }
    if (answer.stale === true) joined.stale = true;
    if (!has(answer, "next_after")) {
      joined.runs = runs;
      return joined;
    }
    const nxt = answer.next_after;
    const last = page.length ? page[page.length - 1].run_id : undefined;
    if (!page.length || typeof nxt !== "string" || nxt !== last || (cursor !== null && !(nxt > cursor))) {
      return { code: LIST_INCOMPLETE, detail: `cursor_not_advancing:${pyStr(nxt)}`, pages };
    }
    if (pages >= LIST_FOLLOW_MAX_PAGES) return { code: LIST_INCOMPLETE, detail: "too_many_pages", pages };
    cursor = nxt;
    answer = await call("run.list", { ...base, after: cursor });
    pages += 1;
    if (!isMapping(answer)) return { code: LIST_INCOMPLETE, detail: "answer_not_object", pages };
    if (answer.code !== "ok") return answer;
  }
}

// What a surface may say about one request's delivery (desktop/tests/fixtures/delivery.json). A Stop that was
// not confirmed is never "stopped".
export function deliveryNote(answer) {
  if (!isMapping(answer) || answer.code !== UNAVAILABLE) return null;
  if (answer.delivered === false) return "not delivered: the control API did not get this request";
  if (answer.delivered === "unknown") return "delivery unknown: it may have been carried out; read the status before trying again";
  if (answer.delivered === true) return "refused by the daemon before the control API";
  return "control API unavailable";
}
