"""The client's bounds, each one named constant in one place.

None of these numbers is measured; they are the lane's first values and bind only once the owner merges
the contract lines on the client and on the limits of outside servers. This slice uses two of them, and
the length of what the client keeps of a server's text.
"""

# One line from the server longer than this (its newline included) ends the session: the client reads at
# most this many bytes plus one, and never the rest of the line (``session.MESSAGE_TOO_LARGE``).
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

# Each request the client sends (``initialize``, ``tools/list``, ``tools/call``) has this many seconds for
# its answer; past it the call is abandoned (``notifications/cancelled``) and answers ``CALL_TIMEOUT``.
CALL_DEADLINE_S = 60.0

# What the client keeps of a server's text (an error's ``message``, a refused request's ``method``, a revision
# it does not speak) is cut to this many characters before anything else is done with it, and only then
# escaped (``session._server_text``). A first value, not measured.
MAX_SERVER_TEXT_CHARS = 200
