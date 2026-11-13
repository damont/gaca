"""In-process state about artifacts that are in the middle of a cloud transfer.

`MOVING` holds the ids being moved to the cloud right now. While an id is in it, every WRITE to
that artifact (services/artifacts.py `_owned_for_write`) answers `423 artifact_moving`: the move
reads the artifact once, uploads it, then swaps the local rows for a pointer, and a write made
in between would be silently lost. Reads are unaffected.

It is memory, not a database row, on purpose: if the server dies mid-move the lock dies with it,
so a crash can never leave a page read-only forever. (What a crash does leave is the artifact
intact locally, and maybe a copy in the cloud: a retry of the move finds it and carries on.)

`BUSY` is the wider set (move, bring home, undo): one transfer operation per artifact at a time.
"""
from __future__ import annotations

from contextlib import contextmanager

from .exceptions import CloudError

MOVING: set[str] = set()
BUSY: set[str] = set()


@contextmanager
def busy(artifact_id: str, *, moving: bool = False):
    """Claim the artifact for one transfer operation, or 409 if another is running."""
    if artifact_id in BUSY:
        raise CloudError(
            "Another move or bring-home of this page is already running. Wait for it to finish.",
            status=409, code="transfer_in_progress")
    BUSY.add(artifact_id)
    if moving:
        MOVING.add(artifact_id)
    try:
        yield
    finally:
        BUSY.discard(artifact_id)
        MOVING.discard(artifact_id)


def lock_writes(artifact_id: str) -> None:
    MOVING.add(artifact_id)


def unlock_writes(artifact_id: str) -> None:
    MOVING.discard(artifact_id)
