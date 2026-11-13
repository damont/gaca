"""Request/response shapes for the cloud link (routers/cloud.py) and the merged artifact list.

Nothing here carries a cloud credential. `ArtifactListItem` extends `schemas.artifact.ArtifactOut`
WITHOUT touching it (that file is mirrored by gaca-cloud): every existing field is unchanged and
the cloud-aware ones are added.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import artifact as s


class LinkStart(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a "cloud_url" in the body is simply ignored: see services/cloud_client.py
    scope: Literal["read", "manage"] = Field(
        default="manage", description="What this server may do as the account. `read` lists/reads/exports only.")


class LinkStarted(BaseModel):
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class LinkPoll(BaseModel):
    status: Literal["pending", "linked", "expired", "denied"]
    interval: int | None = None
    retry_in: int | None = None
    handle: str | None = None
    account_id: str | None = None
    scope: str | None = None
    linked_at: datetime | None = None
    state: str | None = None


class LinkStatus(BaseModel):
    """Never contains the token (or the cloud's address)."""
    linked: bool
    state: Literal["none", "pending", "linked", "invalid"]
    handle: str | None = None
    account_id: str | None = None
    linked_at: datetime | None = None
    scope: str | None = None


class Unlinked(BaseModel):
    unlinked: bool
    cloud_revoked: bool
    message: str


class InviteToken(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(min_length=1, max_length=512, description="The secret part of an invite link.")


class TemplateGet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    target: Literal["server_bundle"] = "server_bundle"


class ArtifactListItem(s.ArtifactOut):
    """An artifact row as listed: everything `ArtifactOut` has, plus where it lives and who has it.
    `schemas` is loosened to plain dicts because a cloud gacabit's shapes are the cloud's to validate."""
    schemas: dict[str, Any] = Field(default_factory=dict)  # type: ignore[assignment]
    home: Literal["local", "cloud"] = "local"
    role: str = "owner"
    shared_by: str | None = None
    member_count: int = 1


class ArtifactListMerged(BaseModel):
    artifacts: list[ArtifactListItem] = []
    # True when the cloud could not be asked and the cloud rows come from this server's last look.
    stale: bool = False
