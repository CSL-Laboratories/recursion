"""The client's limits on an outside MCP server, each one named constant in one place.

An outside server is untrusted input. None of these numbers is measured; they are the lane's first values
and bind only once the owner merges the contract lines on the client and on the limits of outside servers.
They are meant to become a person's own setting later: a settings reader replaces a value here (or the
name in ``session``, where it is read when the limit is checked) without touching the logic. The list is
not final either: a later limit is one more name here and one more check in ``session``.

A limit that fires is a typed result the caller receives and a record handed to ``on_record``; it cuts off
only that server's session (see ``session``, LIMITS).
"""

# ``initialize`` must be answered within this many seconds, both requests together when the client asks a
# second time for the older revision; past it the server is given up (``session.SERVER_START_TIMEOUT``).
INITIALIZE_DEADLINE_S = 5.0

# Each request the client sends (``initialize``, ``tools/list``, ``tools/call``) has this many seconds for
# its answer; past it the call is abandoned (``notifications/cancelled``) and answers ``CALL_TIMEOUT``.
CALL_DEADLINE_S = 60.0

# One line from the server longer than this (its newline included) ends the session: the client reads at
# most this many bytes plus one, and never the rest of the line (``session.MESSAGE_TOO_LARGE``).
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

# The text of one tool result (its ``text`` content items, UTF-8) is cut at this many bytes and the cut is
# marked in the result (``session.RESULT_CUT``).
MAX_TOOL_RESULT_TEXT_BYTES = 256 * 1024

# A tool list longer than this is listed to this many tools, typed (``session.TOOL_LIST_CUT``).
MAX_TOOLS = 512

# A server that breaks a limit this many times in one client session is cut off for the rest of that
# session (``session.SERVER_CUT_OFF``); a new session starts the count again.
MAX_BREACHES = 3
