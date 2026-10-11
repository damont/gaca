"""Paired devices: pair a new one with the code from `gaca-api pair`, list them, unpair one.

Pairing is the only public endpoint here (the code is the proof). Everything else needs a paired
device — see services/pairing.py for the model.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..models.user import UserDoc
from ..schemas.devices import DeviceInfo, PairRequest, PairResponse
from ..security import rate_limit
from ..services import pairing as pairing_service
from ..services.pairing import PairingRefused
from ..services import tokens as token_service
from ..services.exceptions import NotFound
from ..utils.auth import get_session_user, oauth2_scheme
from ..utils import tokens as token_utils

router = APIRouter(prefix="/devices", tags=["devices"])

_STATUS = {
    "no_code": status.HTTP_403_FORBIDDEN,
    "expired": status.HTTP_403_FORBIDDEN,
    "burned": status.HTTP_403_FORBIDDEN,
    "wrong_code": status.HTTP_401_UNAUTHORIZED,
}


@router.post("/pair", response_model=PairResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("pair"))])
async def pair(data: PairRequest):
    """Trade the one-time code for a device credential."""
    try:
        doc, secret = await pairing_service.pair(data.code, data.name)
    except PairingRefused as error:
        raise HTTPException(status_code=_STATUS.get(error.reason, 400), detail=error.detail)
    return PairResponse(access_token=secret, device_id=doc.id)


async def _current_device_id(token: str) -> str | None:
    if not token_utils.looks_like_device(token):
        return None
    found = await token_service.authenticate(token)
    return found[1].id if found else None


@router.get("", response_model=list[DeviceInfo])
async def list_devices(user: UserDoc = Depends(get_session_user), token: str = Depends(oauth2_scheme)):
    mine = await _current_device_id(token)
    return [
        DeviceInfo(id=d.id, name=d.name, created_at=d.created_at, last_used_at=d.last_used_at, current=d.id == mine)
        for d in await token_service.list_devices(user)
    ]


# Registered before "/{device_id}" so "me" is not read as an id.
@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def unpair_this_device(user: UserDoc = Depends(get_session_user), token: str = Depends(oauth2_scheme)):
    """The calling device forgets this server (the app does this when it switches servers)."""
    device_id = await _current_device_id(token)
    if device_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This credential is not a paired device.")
    await token_service.revoke(user, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpair_device(device_id: str, user: UserDoc = Depends(get_session_user)):
    if device_id not in {d.id for d in await token_service.list_devices(user)}:
        raise NotFound("device", device_id)
    await token_service.revoke(user, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
