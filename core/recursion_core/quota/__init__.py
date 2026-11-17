"""Lane QUOTA: the dollar reservation, the usage ledger, ``budget show`` and ``route explain`` (F9). See README.md.

What is here now: ``reservation`` (admission at the breaker's admission: ``admit``, ``release``, the restart
sweep, and one settle row per reservation id) and ``tables`` (the lane's one declaration through the event
store's transaction helper). The usage-event feed, the meters and the two read verbs come in later slices.

Nothing in this package imports an engine, the daemon or the registry, and nothing here runs at import.
"""
