"""Response shape for the public server-status endpoint (routers/setup.py)."""
from __future__ import annotations

from pydantic import BaseModel


class SetupStatus(BaseModel):
    name: str = "gaca-api"
    version: str
    # True once the first device has paired (that is what creates the one owner).
    claimed: bool
