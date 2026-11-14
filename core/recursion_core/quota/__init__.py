"""Lane QUOTA: the dollar reservation, the usage ledger, ``budget show`` and ``route explain`` (F9). See README.md.

What is here now (slice 1): ``reservation`` (admission at the breaker's admission: ``admit``, ``release``) and
``tables`` (the lane's one declaration through the event store's transaction helper). The ledger, the meters
and the two read verbs come in later slices.

Nothing in this package imports an engine, the daemon or the registry, and nothing here runs at import.
"""
