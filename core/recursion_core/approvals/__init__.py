"""The approval queue (lane APPROVALS): one queue, drawn by the core, for engine tool calls and plugin effects.

``effects`` is the one accessor that reads an effect: it binds the effect's canonical bytes, its digest and
its rendered lines from one value. Nothing else in the core reads an effect to draw or digest it.
"""
