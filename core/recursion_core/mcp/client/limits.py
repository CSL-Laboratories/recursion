"""The client's limits on an outside MCP server, each one named constant in one place.

An outside server is untrusted input. None of these numbers is measured; they are the lane's first values
and bind only once the owner merges the contract lines on the client and on the limits of outside servers.
They are meant to become a person's own setting later, and that is not a change of this file alone:
``session`` copies each value when it is imported, so a value replaced here afterwards changes nothing. A
value replaced on the name in ``session`` is read when its limit is checked, which holds for the sizes and
counts; two of the times are also the default deadlines of four signatures there (``open``, ``list_tools``,
``begin_call``, ``call_tool``), held at import, so a raised time is not used by a call that keeps its
default. A later settings reader therefore changes those checks and defaults in ``session`` (or hands a
session its own values). The list is not final either: a later limit is one more name here and one more
check in ``session``.

A limit that fires is a typed result the caller receives and a record handed to ``on_record``; it cuts off
only that server's session (see ``session``, LIMITS).
"""

# ``initialize`` must be answered within this many seconds, both requests together when the client asks a
# second time for the older revision; past it the server is given up (``session.SERVER_START_TIMEOUT``).
INITIALIZE_DEADLINE_S = 5.0

# Each request the client sends (``initialize``, ``tools/list``, ``tools/call``) has this many seconds for
# its answer; past it the call is abandoned (``notifications/cancelled``) and answers ``CALL_TIMEOUT``. A
# progress report on a ``tools/call`` gives the call this deadline again, up to ``CALL_CEILING_S``.
CALL_DEADLINE_S = 60.0

# A ``tools/call`` that progress reports keep alive ends this many seconds after it was made, whatever the
# reports say (``session.CALL_TIMEOUT``, its record's ``reason`` ``session.CALL_CUT_CEILING``). A first
# value, not measured; it is meant to become a person's own setting per server, with this default, in a
# later row of the one settings file.
CALL_CEILING_S = 1800.0

# One line from the server longer than this (its newline included) ends the session: the client reads at
# most this many bytes plus one, and never the rest of the line (``session.MESSAGE_TOO_LARGE``).
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

# Everything a model reads of one tool result shares this many bytes: its text first (every string ``text``
# of its content items and of their ``resource``, or a ``content`` that is one string; UTF-8), cut where the
# budget ends, then ``structuredContent``, its annotations and its ``_meta`` (compact JSON, UTF-8), each left
# out whole when what is left does not hold it. The cut is marked in the result (``session.RESULT_CUT``).
MAX_TOOL_RESULT_TEXT_BYTES = 256 * 1024

# A tool list longer than this is listed to this many tools, typed (``session.TOOL_LIST_CUT``).
MAX_TOOLS = 512

# A server that breaks a limit this many times within ``BREACH_WINDOW_S`` in one client session is cut off
# for the rest of that session (``session.SERVER_CUT_OFF``); a new session starts the count again. Only what a
# cut does not already mend counts (see ``session``, LIMITS: breaches).
MAX_BREACHES = 3

# A breach older than this many seconds no longer counts toward ``MAX_BREACHES``. A first value, not measured;
# it and ``MAX_BREACHES`` are meant to become a person's own setting per server, with these defaults, in a
# later row of the one settings file.
BREACH_WINDOW_S = 600.0

# What the client keeps of a server's text (an error's ``message``, a refused request's ``method``, a revision
# it does not speak) is cut to this many characters before anything else is done with it, and only then
# escaped (``session._server_text``). A first value, not measured.
MAX_SERVER_TEXT_CHARS = 200

# The answers the client owes a server (``ping`` answers and refusals) and has not yet written are held to this
# many encoded bytes; an answer that would pass it is dropped (``session.ANSWERS_DROPPED``). The value of the
# Control MCP server's own bound on the answers it owes a client (``MAX_PENDING_REPLY_BYTES`` in
# ``recursion_core.control.mcp.replies``). A first value, not measured.
MAX_UNSENT_ANSWER_BYTES = 1024 * 1024

# One listing of tools asks for at most this many pages (512 tools at the smallest useful page of 8); past it
# the listing ends, cut (``session.TOOL_LIST_CUT``, reason ``session.LIST_CUT_PAGES``). A first value, not
# measured.
MAX_LIST_PAGES = 64
