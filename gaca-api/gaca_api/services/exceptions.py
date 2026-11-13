"""Domain errors raised by the service layer.

Services know nothing about HTTP. They raise these; a handler in main.py maps
them to status codes, keeping HTTP concerns out of the business logic.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import ValidationError


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


class Conflict(Exception):
    """A write lost a race: the caller's `expected_version` is no longer current, or
    the thing it tried to create already exists. Mapped to HTTP 409 — refetch, then retry."""


class BadRequest(Exception):
    """The request asks for something that doesn't exist as an operation — e.g. an
    artifact page calling a method the API doesn't perform. Mapped to HTTP 400."""


class RequestInvalid(Exception):
    """A body the service validated itself (not FastAPI) failed schema validation —
    e.g. an artifact page call's args checked against the matching endpoint's body
    model. Carries pydantic's errors so the 422 reads exactly like the one that
    endpoint would have sent."""

    def __init__(self, error: ValidationError) -> None:
        super().__init__(str(error))
        self.errors = error.errors(include_url=False)


# ── the cloud link (services/cloud_*.py) ─────────────────────────────────────
class CloudError(Exception):
    """Something about reaching or using the GACA Cloud went wrong in a way this server can
    name. Carries its own HTTP status and a stable `code`; the message never contains the
    cloud's address or the server token. Mapped by one handler in main.py."""

    status = 502
    code = "cloud_error"

    detail: object = None  # a structured explanation (a dict); when None the handler repeats the message
    headers: dict[str, str] | None = None

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None, detail: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        if detail is not None:
            self.detail = detail
        if headers:
            self.headers = headers


class CloudUnavailable(CloudError):
    """The cloud could not be reached, timed out, or answered with a server error: 502."""

    status = 502
    code = "cloud_unavailable"

    def __init__(self, message: str = "The cloud could not be reached right now. Try again in a moment.") -> None:
        super().__init__(message)


class CloudNotLinked(CloudError):
    """This server has no (working) cloud link. 409."""

    status = 409
    code = "cloud_not_linked"

    def __init__(self, message: str = "This server is not linked to a cloud account.") -> None:
        super().__init__(message)


class ArtifactMoving(Exception):
    """The artifact is being moved to the cloud right now and is read-only until that finishes or
    fails (writes would be lost in the move). Mapped to HTTP 423 `artifact_moving`."""

    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(f"artifact {artifact_id!r} is being moved to the cloud")


class CloudReply(Exception):
    """The cloud's own answer, to be handed to the client as it is (status, body, a few
    headers). Raised for every forwarded answer, errors and successes alike, so a route can
    leave early; main.py turns it into the response."""

    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        super().__init__(f"cloud answered {status}")
        self.status = status
        self.body = body
        self.headers = headers or {}
