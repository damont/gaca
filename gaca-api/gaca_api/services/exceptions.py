"""Domain errors raised by the service layer.

Services know nothing about HTTP. They raise these; a handler in main.py maps
them to status codes, keeping HTTP concerns out of the business logic.
"""
from __future__ import annotations


class NotFound(Exception):
    """A requested resource doesn't exist. Mapped to HTTP 404."""

    def __init__(self, resource: str, id: str) -> None:
        self.resource = resource
        self.id = id
        super().__init__(f"{resource} {id!r} not found")


class InvalidTransition(Exception):
    """A resource can't move from its current state to the requested one.

    Mapped to HTTP 409, not 400: the request was well formed and would have been
    valid a moment ago. It usually means the caller lost a race — an agent
    settled a delegation, or the same alert was triaged on another device. The
    right client response is to refetch and re-render, not to retry.
    """

    def __init__(self, resource: str, id: str, current: str, requested: str) -> None:
        self.resource = resource
        self.id = id
        self.current = current
        self.requested = requested
        super().__init__(
            f"{resource} {id!r} cannot move from {current!r} to {requested!r}"
        )
