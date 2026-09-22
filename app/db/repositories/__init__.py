"""Read-oriented repository functions for future specialist agents.

Every function here takes an active ``Session`` as its first argument and
performs read-only queries -- no writes, no business-rule calculations
(those belong in app/rules/ in a later phase).
"""
