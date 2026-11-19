"""The client's bounds, each one named constant in one place.

None of these numbers is measured; they are the lane's first values and bind only once the owner merges
the contract lines on the client and on the limits of outside servers. This slice uses two of them.
"""

# One line from the server longer than this (its newline included) ends the session: the client reads at
# most this many bytes plus one, and never the rest of the line (``session.MESSAGE_TOO_LARGE``).
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

# Each request the client sends (``initialize``, ``tools/list``, ``tools/call``) has this many seconds for
# its answer; past it the call is abandoned (``notifications/cancelled``) and answers ``CALL_TIMEOUT``.
CALL_DEADLINE_S = 60.0
