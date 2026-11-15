"""Publish a page to the gallery, for the app. Two routes that work for a private page and for a cloud page alike
(see services/cloud_publish.py). Paired device only: the person's tap is the confirmation, so an API token
(Hermes) is refused. Sharing, invites and members are NOT here (routers/cloud_gacabits.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from ..models.user import UserDoc
from ..services import cloud_forward, cloud_publish
from ..utils.auth import get_session_user

router = APIRouter(prefix="/artifacts", tags=["cloud-publish"])


@router.post("/{artifact_id}/publish-check")
async def publish_check(
    artifact_id: str, body: cloud_publish.Listing, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Run the publish checks on this page and show what would be published. Body: the listing text (`title`,
    `description`, `include_data_notes`, `tags`, `category`, `license`). Answers the report, its `content_hash` and
    the `preview` (the page and its shapes with no data). Nothing is stored or published."""
    await cloud_publish.check(user.id, artifact_id, body, cloud_forward.clean_request_id(x_request_id))


@router.post("/{artifact_id}/publish", status_code=201)
async def publish(
    artifact_id: str, body: cloud_publish.PublishBody, user: UserDoc = Depends(get_session_user),
    x_request_id: str | None = Header(default=None),
) -> None:
    """Publish what the person confirmed: the listing text plus `visibility`, `confirmed_preview: true` and the
    `preview_hash` from `publish-check`. Answers the template (it starts `checking`). Publishing the same page again
    makes a new version of the same template."""
    await cloud_publish.publish(user.id, artifact_id, body, cloud_forward.clean_request_id(x_request_id))
