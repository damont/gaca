"""Provisioning: gaca-api acting on a user's Hermes box directly (SSH),
rather than only calling a profile's own scoped HTTP gateway.

Kept as its own package, parallel to `agent/` (the HTTP-only port to a
single crew member's gateway) — this is a different, much broader kind of
access (a shell on the box), used only for creating/discovering crew
members, never for chat or delegation.
"""
