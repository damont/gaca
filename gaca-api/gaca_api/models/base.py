"""Shared helpers for database models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def new_id() -> str:
    """A fresh string primary key (used as Mongo's _id)."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
