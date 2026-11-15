# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""THE TURN INVARIANT of Antigravity's ACP drafts (``AcpDrafts``), held over many turns at once.

Many turns over several sessions, through the real session path (``open_acp`` and the shared client
against the fake agent), with every kind of exit in sequences drawn from fixed seeds. One ``AcpDrafts``
serves every session of a run. After every turn, the drafts that turn added must hold:

* exactly one ``result``, with the turn's own code;
* exactly one ``input.accepted`` when the agent answered the prompt, naming the turn, and none otherwise;
* every tool call of the turn ended exactly once, within the turn;
* nothing of another turn (turn N's calls never end in turn N+1) and nothing of another session.

The exits: the answer (with a call ended or left open), the agent answering cancelled, the caller's cancel,
the deadline with and without a line read, the agent going away with and without a line read, a protocol
error, a line it cannot parse, a stray response line, ``on_line`` and ``on_update`` raising, ``on_ask``
raising (the wrapped wiring), a raise injected in each wrapped callback, and the driver's closes
(``ended``, ``abandoned``, ``start_turn``) called again and out of order. Three wirings: ``open_acp(drafts=)``,
the drafts' own bound callbacks, and callbacks wrapped so that the session cannot see the drafts (the
driver then calls ``start_turn`` before each prompt). It uses only the drafts' documented calls, so it runs
unchanged on every earlier head of this slice.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_turns
"""

from __future__ import annotations

import collections
import random
import re
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity import acp
from recursion_core.routes.engines.fixtures.antigravity.fake_acp_agent import OPTIONS_ALL, FakeAcpAgent

CWD = "/var/tmp/workspace"
FAR = 1e12
VALUE = "12345"                 # made up: the value the drafts mask; an update whose number spells it is refused
SEEDS = (11, 23, 37, 41, 53, 67)
TURNS = 170                     # per seed and wiring: about three thousand turns in all
WIRINGS = ("drafts=", "bound", "wrapped")
ROUTE = {"engine": "antigravity", "model": "gemini-3.6-flash", "auth_method": "gemini-api-key",
         "connection": {"kind": "api_key", "id": "conn-A", "credential_env": "GEMINI_API_KEY"}}

NOT_JSON_RPC = ("raw", '{"jsonrpc":"1.0","id":99}')
UNPARSEABLE = ("raw", "{not json")
NOT_UTF8 = ("raw", '{"jsonrpc":"2.0","method":"x","params":{"t":"' + chr(0xDCFF) + '"}}')
INJECT_LINE = ("raw", '{"jsonrpc":"2.0","method":"x/inject-a-raise"}')
STRAY = ("raw", '{"jsonrpc":"2.0","id":9999,"result":{"stopReason":"end_turn"}}')
STOPPED = (("wait_cancel",), ("end", "cancelled"))


def opened(cid: str):
    return ("update", {"sessionUpdate": "tool_call", "toolCallId": cid, "title": "run", "kind": "execute",
                       "status": "in_progress", "rawInput": {"n": 1}})


def done(cid: str):
    return ("update", {"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "completed",
                       "content": [{"type": "content", "content": {"type": "text", "text": "out"}}]})


def spells_value(cid: str):
    return ("update", {"sessionUpdate": "tool_call", "toolCallId": cid, "status": "in_progress",
                       "rawInput": {"n": 12345.0}})


# name -> (steps for call id prefix p, seconds to the deadline or None, ends the session, wirings or None = all)
EXITS = {
    "the answer": (lambda p: [opened(p + "a"), done(p + "a"), ("chunk", "hi"), ("end", "end_turn")], None, False, None),
    "the answer, a call left open": (lambda p: [opened(p + "a"), ("end", "end_turn")], None, False, None),
    "the agent answers cancelled": (lambda p: [opened(p + "a"), ("end", "cancelled")], None, False, None),
    "the caller's cancel": (lambda p: [opened(p + "a-cancel"), *STOPPED], None, False, None),
    "the deadline, a line read": (lambda p: [opened(p + "a"), *STOPPED], 10.0, False, None),
    "the deadline, no line read": (lambda p: [*STOPPED], 10.0, False, None),
    "the agent goes away, a line read": (lambda p: [opened(p + "a"), ("die",)], None, True, None),
    "the agent goes away, no line read": (lambda p: [("die",)], None, True, None),
    "a protocol error": (lambda p: [opened(p + "a"), NOT_JSON_RPC, *STOPPED], None, False, None),
    "a line it cannot parse": (lambda p: [opened(p + "a"), UNPARSEABLE, *STOPPED], None, False, None),
    "a stray response line": (lambda p: [opened(p + "a"), STRAY, ("stale_response",), done(p + "a"),
                                         ("end", "end_turn")], None, False, None),
    "on_line raises": (lambda p: [opened(p + "a"), NOT_UTF8, *STOPPED], None, False, None),
    "on_update raises": (lambda p: [opened(p + "a"), spells_value(p + "b"), *STOPPED], None, False, None),
    "on_ask raises": (lambda p: [opened(p + "a"), ("ask", {"toolCallId": p + "a", "title": "run", "kind": "execute",
                                                           "status": "pending"}, OPTIONS_ALL), ("end", "cancelled")],
                      None, False, ("wrapped",)),
    "a raise injected in on_line": (lambda p: [opened(p + "a"), INJECT_LINE, *STOPPED], None, False, ("wrapped",)),
    "a raise injected in on_update": (lambda p: [opened(p + "a"), opened(p + "b-inject"), *STOPPED], None, False,
                                      ("wrapped",)),
}
# The driver's closes, called again and out of order after the turn's own close.
EXTRAS = ("ended again", "abandoned again", "start_turn again", "ended of the turn before", "nothing")


def code_of(err: BaseException) -> str:
    return err.code if isinstance(err, acp.AcpError) else c.ENGINE_ERROR


class Injected(RuntimeError):
    pass


class Run:
    """One seed on one wiring: sessions one after another, one AcpDrafts for all of them."""

    def __init__(self, seed: int, wiring: str) -> None:
        self.rng = random.Random(seed * 7919 + WIRINGS.index(wiring))
        self.wiring = wiring
        self.drafts = ag.AcpDrafts(redact=(VALUE,))
        self.failures: list = []
        self.exits = [name for name, (_s, _d, _e, only) in EXITS.items() if only is None or wiring in only]
        self.label = f"seed {seed} {wiring}"

    def session(self, index: int, names: list[str]):
        scripts, holder = [], []
        for turn, name in enumerate(names, 1):
            scripts.append(EXITS[name][0](f"s{index}t{turn}c"))
        agent = FakeAcpAgent(scripts)
        read = agent.read_line

        def read_line(timeout_s):                       # the caller's cancel, sent once its marker is read
            line = read(timeout_s)
            if line is not None and "-cancel" in line and holder:
                holder[0].cancel()
            return line

        agent.read_line = read_line
        d = self.drafts
        if self.wiring == "drafts=":
            s = ag.open_acp(agent, agent.clock, ROUTE, CWD, FAR, drafts=d)
        elif self.wiring == "bound":
            s = ag.open_acp(agent, agent.clock, ROUTE, CWD, FAR, on_line=d.on_line, on_update=d.on_update,
                            on_ask=d.on_ask)
        else:
            def on_line(line):
                if "inject-a-raise" in line:
                    raise Injected("on_line")
                d.on_line(line)

            def on_update(params):
                if "inject" in str(params.get("update", {}).get("toolCallId", "")):
                    raise Injected("on_update")
                d.on_update(params)

            def on_ask(ask):
                raise Injected("on_ask")

            s = ag.open_acp(agent, agent.clock, ROUTE, CWD, FAR, on_line=lambda line: on_line(line),
                            on_update=lambda params: on_update(params), on_ask=lambda ask: on_ask(ask))
        holder.append(s)
        return agent, s

    def close(self, where, what, fn, *args) -> None:
        """One of the driver's closes; a close that raises is a broken turn, recorded, and the run goes on."""
        try:
            fn(*args)
        except Exception as error:
            self.failures.append((*where, f"the driver's {what} raised {type(error).__name__}"))

    def turn(self, index, agent, s, number, name, previous):
        d = self.drafts
        where = (self.label, f"session {index}", f"turn {number}", name)
        deadline_in = EXITS[name][1]
        deadline = FAR if deadline_in is None else agent.clock() + deadline_in
        before = len(d.drafts)
        start_turn = getattr(d, "start_turn", None)
        if self.wiring == "wrapped" and start_turn is not None:
            self.close(where, "start_turn", start_turn)
        out = err = None
        ids = (f"turn-s{index}t{number}", f"dlv-s{index}t{number}")
        try:
            out = s.prompt(f"turn {number}", deadline)
        except Exception as error:
            err = error
            # The wrapped wiring needs the driver's abandoned; with the session wired it is optional (the
            # session tells every end), so a driver may leave it out, and the turn must still end.
            if hasattr(d, "abandoned") and (self.wiring == "wrapped" or self.rng.random() < 0.5):
                self.close(where, "abandoned", d.abandoned, code_of(error), "the driver's except")
        else:
            self.close(where, "ended", d.ended, out, *ids)
        extra = self.rng.choice(EXTRAS)
        if extra == "ended again" and out is not None:
            self.close(where, "second ended", d.ended, out, *ids)
        elif extra == "abandoned again" and hasattr(d, "abandoned"):
            self.close(where, "second abandoned", d.abandoned, c.ENGINE_ERROR, "a second close")
        elif extra == "start_turn again" and start_turn is not None:
            self.close(where, "second start_turn", start_turn)
        elif extra == "ended of the turn before" and previous is not None:
            self.close(where, "ended of the turn before", d.ended, previous)
        self.check(where + (extra,), index, number, d.drafts[before:], out, err)
        return out

    def check(self, where, index, number, window, out, err) -> None:
        mine = f"s{index}t{number}c"
        results = [w for w in window if w["type"] == "result"]
        if len(results) != 1:
            self.failures.append((*where, f"{len(results)} results"))
        else:
            want = out.code if out is not None else code_of(err)
            if results[0]["payload"]["code"] != want:
                self.failures.append((*where, f"result code {results[0]['payload']['code']!r}, not {want!r}"))
        accepted = [w for w in window if w["type"] == "input.accepted"]
        due = out is not None and out.stop_reason is not None
        if len(accepted) != (1 if due else 0):
            self.failures.append((*where, f"{len(accepted)} input.accepted ({'due' if due else 'not due'})"))
        elif accepted and accepted[0]["payload"]["turn_id"] != f"turn-s{index}t{number}":
            self.failures.append((*where, "input.accepted names another turn"))
        calls = [w["payload"]["call_id"] for w in window if w["type"] == "tool.call"]
        ends = [w["payload"]["call_id"] for w in window if w["type"] == "tool.result"]
        for cid in set(calls) | set(ends):
            if not cid.startswith(mine):
                self.failures.append((*where, f"a draft of call {cid} (another turn or session)"))
        for cid in calls:
            if ends.count(cid) != 1:
                self.failures.append((*where, f"call {cid} ended {ends.count(cid)} times in its turn"))
        for cid in ends:
            if cid not in calls:
                self.failures.append((*where, f"call {cid} ended in a turn that did not begin it"))

    def go(self) -> None:
        count, index = 0, 0
        while count < TURNS:
            index += 1
            names = []
            for _ in range(self.rng.randint(1, 12)):
                names.append(self.rng.choice(self.exits))
                if EXITS[names[-1]][2]:
                    break
            agent, s = self.session(index, names)
            previous = None
            for number, name in enumerate(names, 1):
                previous = self.turn(index, agent, s, number, name, previous) or previous
            count += len(names)


class TurnInvariantTest(unittest.TestCase):
    def test_every_turn_of_every_session_ends_once_with_its_own_drafts(self) -> None:
        failures, unsupported = [], []
        for wiring in WIRINGS:
            for seed in SEEDS:
                run = Run(seed, wiring)
                try:
                    run.go()
                except TypeError as err:                 # a wiring an earlier head does not offer
                    if "drafts" in str(err) and wiring == "drafts=":
                        unsupported.append(wiring)
                        break
                    raise
                failures += run.failures
        kinds = collections.Counter(f"{f[0].split()[2]}: {re.sub(r's[0-9]+t[0-9]+c[a-z-]*', '<call>', f[-1])}"
                                    for f in failures)
        self.assertEqual(failures, [], f"\n{len(failures)} broken turns; by kind: {dict(kinds)}\nfirst: "
                         + "\n".join(" | ".join(f) for f in failures[:12])
                         + (f"\nwirings this head does not offer: {unsupported}" if unsupported else ""))


if __name__ == "__main__":
    unittest.main()
