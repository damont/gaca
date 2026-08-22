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


class AgentNotConfigured(Exception):
    """The caller hasn't submitted their own agent_base_url/agent_api_key yet.

    Mapped to HTTP 400. A normal, expected state for a new user — not a
    startup or wiring bug — since every operator brings their own agent
    rather than sharing one configured for the whole app.
    """

    def __init__(self) -> None:
        super().__init__(
            "agent not configured — submit agent_base_url and agent_api_key "
            "via PUT /api/auth/agent-config first"
        )
