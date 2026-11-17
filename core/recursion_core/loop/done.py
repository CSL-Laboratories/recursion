"""The harness owns DONE: the model's finish is a candidate, the task's acceptance check decides.

From the contract (F11, PROPOSED: "The harness should own DONE through an acceptance step") and vault record
20261028-recursion-frontier-dr-completion-control-02: stopped talking, saying it is done and the task being
complete are three different things; the model proposes, Recursion decides; budget exhaustion is never done.

When the model ends its reply and the task has an acceptance check (a shell command set for the run), the
loop runs it in the workspace, without asking (it is the task's check, not the model's call):

- it passes (exit 0): the turn ends ``done``;
- it fails: the model is shown the failure and the turn goes on, up to ``rounds`` checks in all;
- it fails ``rounds`` times: the turn ends ``paused``, with the reason in the result.

With no acceptance check the turn ends ``candidate_done``: the model says it is done and nothing has checked.
Each check is written as an ``acceptance`` line. The round limit does not reset when the model calls tools
(the record's point against Claude Code's continuation cap, which resets on any tool call).
"""

from __future__ import annotations

from dataclasses import dataclass

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.shell import Ran, run_command

DEFAULT_ROUNDS = 3
DEFAULT_TIMEOUT_S = 600.0
SHOWN_BYTES = 8 * 1024


@dataclass(frozen=True)
class Acceptance:
    command: str
    rounds: int = DEFAULT_ROUNDS
    timeout_s: float = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("an acceptance check needs a command")
        if self.rounds < 1:
            raise ValueError("acceptance rounds must be 1 or more")


def tail(text: str, limit: int = SHOWN_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[len(data) - limit:].decode("utf-8", errors="ignore")
    return f"[the first {len(data) - limit} bytes are not shown]\n{cut}"


def run_check(acceptance: Acceptance, cwd: str, cancel: b.CancelToken) -> Ran:
    return run_command(acceptance.command, cwd, cancel, acceptance.timeout_s)


def passed(ran: Ran) -> bool:
    return ran.exit_code == 0 and not ran.timed_out and not ran.cancelled


def feedback(acceptance: Acceptance, ran: Ran, used: int) -> str:
    status = "timed out" if ran.timed_out else f"exited {ran.exit_code}"
    return (f"[harness] The task's acceptance check `{acceptance.command}` {status}, so the work is not done "
            f"(check {used} of {acceptance.rounds}). Its output:\n{tail(ran.output)}\n"
            f"Keep working until the check passes, or say what stops you.")
