"""Is there an owner yet? Public, so an app pointed at an address can tell it is a GACA server.

Nothing else here: a phone becomes allowed to use the server by pairing (routers/devices.py).
"""
from __future__ import annotations

from fastapi import APIRouter

from ..db import users as users_db
from ..schemas.setup import SetupStatus

router = APIRouter(prefix="/setup", tags=["setup"])


@router.get("", response_model=SetupStatus)
async def setup_status():
    from ..main import app  # version lives on the app; imported late to avoid a cycle

    return SetupStatus(version=app.version, claimed=await users_db.count() > 0)
