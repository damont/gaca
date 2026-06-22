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
