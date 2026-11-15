// How the desktop's window reaches the control API.
//
// This build is inert: it has no proxy, so every call answers control_api_unavailable, not delivered, and
// nothing leaves the window. The next slice adds the Rust proxy (one command, control_call(method, params),
// and one follow channel); it is the window's only way to the daemon. The window never opens a connection,
// never speaks HTTP to the daemon, and sends nothing when it closes: closing ends observation only.

import { HELD, METHODS } from "./actions.js";

export const INERT_DETAIL = "desktop_inert: this build has no proxy, so nothing is sent";

export class RefusedHere extends Error {}

export function inertTransport() {
  return Object.freeze({
    kind: "inert",
    async call() {
      return { code: "control_api_unavailable", delivered: false, detail: INERT_DETAIL };
    },
  });
}

function sameJson(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Answers from recorded exchanges ({method, params, answer}), for checks and demonstrations. A call no
// exchange matches is a fault of the caller, not an answer.
export function replayTransport(exchanges) {
  const calls = [];
  return {
    kind: "replay",
    calls,
    async call(method, params) {
      calls.push({ method, params });
      const hit = exchanges.find((e) => e.method === method && sameJson(e.params, params));
      if (!hit) throw new RefusedHere(`no recorded exchange for ${method}`);
      return structuredClone(hit.answer);
    },
  };
}

// The one way a view sends: only a method the desktop may call, never a held action or a held method under
// another action's name, and only through the transport. action is the actions.json name, built is the
// builder's {method, params}.
export async function send(transport, action, built) {
  for (const name of [action, built.method]) {
    if (Object.prototype.hasOwnProperty.call(HELD, name)) throw new RefusedHere(`${name} is held: ${HELD[name]}`);
  }
  if (!METHODS.includes(built.method)) throw new RefusedHere(`${built.method} is not a method the desktop may call`);
  return transport.call(built.method, built.params);
}
