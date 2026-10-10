"""Owner-managed API tokens: the credential Hermes skills use to post alerts, lists, briefings
and artifacts. Needs a real sign-in session — an API token cannot create or revoke tokens.
See utils/tokens.py for what a token may call."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from ..models.user import UserDoc
from ..schemas.tokens import TokenCreate, TokenCreated, TokenInfo
from ..services import tokens as token_service
from ..utils.auth import get_session_user

router = APIRouter(prefix="/tokens", tags=["tokens"])


@router.get("", response_model=list[TokenInfo])
async def list_tokens(user: UserDoc = Depends(get_session_user)):
    return await token_service.list_tokens(user)


@router.post("", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(data: TokenCreate, user: UserDoc = Depends(get_session_user)):
    """Mint a token. The secret is in this response and nowhere else, ever."""
    doc, secret = await token_service.create(user, data.name, list(data.scopes), data.expires_in_days)
    return TokenCreated(**doc.model_dump(), token=secret)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(token_id: str, user: UserDoc = Depends(get_session_user)):
    await token_service.revoke(user, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
