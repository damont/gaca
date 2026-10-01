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


class InvalidRequest(Exception):
    """The request is well-formed JSON but semantically invalid given the
    current state of the resource it targets — e.g. a `suggested_action_index`
    that doesn't exist on this particular alert. A schema-level `Literal` or
    `Field` constraint can't catch this because it depends on data pydantic
    never sees at validation time. Mapped to HTTP 422, same as a schema miss.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class CrewNotConfigured(Exception):
    """The caller has no usable crew member — or box access — to reach yet.

    Mapped to HTTP 400. A normal, expected state for a new user — not a
    startup or wiring bug — since every operator brings their own crew
    rather than sharing one configured for the whole app.

    Every raise site passes its own accurate `message`: "no agent connected
    at all" and "connected, but gaca-api has no access to create a new
    crew member" are different problems with different fixes, and a single
    generic string for both once hid the real cause from a caller entirely
    (it also went stale after the agent/crew split — it named
    `PUT /api/crew/host`, an endpoint that hadn't existed for hours — see
    build-plan.md §15). `message` is optional only so a low-level caller
    with no more specific context than "nothing to reach" still gets a
    reasonable default.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "no agent connected yet — connect one first via "
            "POST /api/agent/connect"
        )


class PayloadTooLarge(Exception):
    """An upload is bigger than the API accepts. Mapped to HTTP 413."""
