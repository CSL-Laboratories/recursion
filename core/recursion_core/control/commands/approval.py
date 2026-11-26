"""``recursion-control approval list|open|answer``: the approval queue from the command line (lane APPROVALS).

* ``approval list [--session LOG] [--state STATE] [--after ID]``: summaries, as JSON, 100 to a page. This shows
  no prompt. An answer with more after it carries ``next_after``: the ``--after`` of the next page.
* ``approval open APPROVAL_ID``: prints the request's rendered lines, in full and in order, as the core drew
  them (``trusted`` lines as they are, ``untrusted`` ones after a marker), then its revision and digest. The
  opening is recorded when the command asks the core to open the request, before its lines are written: an
  opening is kept even when the lines then cannot be written (a closed pipe), and an answer from the command
  line is then taken for it.
* ``approval answer APPROVAL_ID approve|deny --revision N --digest DIGEST``: answers what ``open`` showed.
  The revision and the digest are the ones ``open`` printed; an answer for any other is refused.

Each call carries a fresh command id. Exit status: 0 ok, 1 an API error or a refusal, as the CLI's own.
"""

from __future__ import annotations

import json
import secrets

__all__ = ["NAMES", "dispatch", "parser"]

NAMES = ("approval",)
_UNTRUSTED = "  | "  # the marker before a line that carries an engine's or a plugin's text


def parser(family) -> None:
    command = family.add_parser("approval", help="list, show and answer approval requests (beta)")
    actions = command.add_subparsers(dest="approval_action", required=True)
    listing = actions.add_parser("list", help="summaries of approval requests; shows no prompt")
    listing.add_argument("--session", default=None, help="only this log (a session id, or run:<run_id>)")
    listing.add_argument("--state", default=None,
                         choices=("pending", "approved", "denied", "expired", "consumed", "vetoed"))
    listing.add_argument("--after", default=None, help="the next_after a list printed: the page after it")
    opening = actions.add_parser("open", help="show one request's lines (the opening is recorded when it is asked for, before they print)")
    opening.add_argument("approval_id")
    answering = actions.add_parser("answer", help="answer a request that 'open' showed")
    answering.add_argument("approval_id")
    answering.add_argument("answer", choices=("approve", "deny"))
    answering.add_argument("--revision", type=int, required=True, help="the revision 'open' printed")
    answering.add_argument("--digest", required=True, help="the digest 'open' printed")


def _command_id() -> str:
    return "cli-" + secrets.token_hex(16)


def _print_lines(out, approval: dict) -> None:
    for line in approval["rendered"]:
        out.write((line["text"] if line["trust"] == "trusted" else _UNTRUSTED + line["text"]) + "\n")
    out.write("revision: %d\ndigest: %s\nstate: %s\n" % (approval["revision"], approval["digest"], approval["state"]))


def dispatch(args, client) -> int:
    action = args.approval_action
    if action == "list":
        params = {key: value for key, value in (("session_id", args.session), ("state", args.state),
                                                ("after", getattr(args, "after", None))) if value is not None}
        answer = client.call("approval.list", params)
        args.out.write(json.dumps(answer, sort_keys=True) + "\n")
        return 0 if answer.get("code") == "ok" else 1
    if action == "open":
        answer = client.call("approval.open", {"command_id": _command_id(), "approval_id": args.approval_id,
                                               "surface": "cli"})
        if answer.get("code") != "ok":
            args.err.write(json.dumps(answer, sort_keys=True) + "\n")
            return 1
        _print_lines(args.out, answer["approval"])
        return 0
    answer = client.call("approval.answer", {"command_id": _command_id(), "approval_id": args.approval_id,
                                             "surface": "cli", "expected_revision": args.revision,
                                             "digest": args.digest, "answer": args.answer})
    stream = args.out if answer.get("code") == "ok" else args.err
    shown = {key: value for key, value in answer.items() if key != "approval"}
    if answer.get("code") == "ok":
        shown["state"] = answer["approval"]["state"]
    stream.write(json.dumps(shown, sort_keys=True) + "\n")
    return 0 if answer.get("code") == "ok" else 1
