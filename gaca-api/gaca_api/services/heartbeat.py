"""A user's heartbeat settings. No FastAPI, no HTTP."""
from __future__ import annotations

from ..db import users as users_db
from ..models.heartbeat import HeartbeatSettings
from ..models.user import UserDoc
from ..schemas.heartbeat import HeartbeatSettingsUpdate

# Fields where "send null" means "clear it", as opposed to "I did not mention it".
_CLEARABLE = {"paused_until", "timezone"}


def get(user: UserDoc) -> HeartbeatSettings:
    return user.heartbeat


async def update(user: UserDoc, body: HeartbeatSettingsUpdate) -> HeartbeatSettings:
    changes = {
        name: value
        for name, value in body.model_dump().items()
        if name in body.model_fields_set and (value is not None or name in _CLEARABLE)
    }
    # Re-validate the merged result as a whole, so a bad combination can never be stored.
    merged = HeartbeatSettings.model_validate({**user.heartbeat.model_dump(), **changes})
    await users_db.replace(user.model_copy(update={"heartbeat": merged}))
    return merged
