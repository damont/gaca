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
    # The owner's setting: may their agents (Hermes) use the cloud? Off until switched on in the app.
    agent_access: bool = False
    # False when this server has no cloud address (GACA_CLOUD_URL empty): linking cannot work, say so up front.
    configured: bool = True
    # A link in progress, so the app can show the same code again after leaving the screen. Paired device only
    # (never an API token), and only while the code is still valid.
    user_code: str | None = None
    verification_uri: str | None = None
    expires_at: datetime | None = None
    expires_in: int | None = None
    interval: int | None = None
    # Whether the credential asking may use cloud pages right now (the app: yes; a token: has `cloud`).
    cloud_access: bool = True


class AgentAccess(BaseModel):
    enabled: bool


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
    # True while this page is being moved to the cloud (it is read-only until that ends).
    moving: bool = False
    # The id of the template last published from this page (cloud pages the owner published; else null).
    published_template_id: str | None = None
    # Where it is in a cloud transfer. A cloud page: the cloud's own status (active, read_only,
    # brought_home ...). A local page: null, or `moving`, `move_failed` (the last move did not finish;
    # nothing was lost) or `bring_home_unconfirmed` (it came home but the cloud has not been told yet).
    cloud_status: str | None = None


class ArtifactListMerged(BaseModel):
    artifacts: list[ArtifactListItem] = []
    # True when the cloud could not be asked and the cloud rows come from this server's last look.
    stale: bool = False


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Required (true) when `move-preview` says something is not carried over: the person has seen the list.
    acknowledge_not_carried: bool = False


class BringHomeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Required (true) when other members share the page: bringing it home cuts them off.
    acknowledge_members: bool = False


class MoveResult(BaseModel):
    """What a finished move to the cloud answers: the page as a cloud page, and that it was checked."""
    artifact: ArtifactListItem
    verified: bool
    resumed: bool = False  # a copy from an earlier try was found in the cloud, checked and used
    backup_expires_at: datetime
    not_carried: dict[str, Any] = Field(default_factory=dict)  # what the cloud did not keep (see move-preview)


class BringHomeResult(BaseModel):
    artifact: ArtifactListItem
    # False when the page is home but the cloud has not been told yet (it is told again later).
    confirmed: bool
    undo_until: str | None = None
    warning: str | None = None
    members_lost_access: int = 0  # other members who lost access when it came home


class UndoResult(BaseModel):
    home: Literal["local", "cloud"]
    undone: bool
    artifact: dict[str, Any]
