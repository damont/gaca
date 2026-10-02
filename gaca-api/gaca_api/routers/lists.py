"""HTTP layer for lists. Thin: validate, call the service, return. Every route needs auth
and only ever sees the caller's own lists."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..models.user import UserDoc
from ..schemas.lists import (
    AddItems,
    AddResult,
    ByText,
    ByTextResult,
    ClearResult,
    ItemOut,
    ItemPatch,
    ListCreate,
    ListDetail,
    ListMatch,
    ListPatch,
    ListSummaries,
    ListSummary,
)
from ..services import lists as list_service
from ..utils.auth import get_current_user

router = APIRouter(prefix="/lists", tags=["lists"])


@router.get("")
async def list_lists(
    include_archived: bool = False, user: UserDoc = Depends(get_current_user)
) -> ListSummaries:
    """Your lists, most recently changed first, with open/checked-off counts."""
    return await list_service.list_lists(user.id, include_archived)


@router.post("", status_code=201)
async def create_list(body: ListCreate, user: UserDoc = Depends(get_current_user)) -> ListSummary:
    """Create a list. `422` if you already have one by that name (names are matched loosely:
    "Costco" and "costco" are the same)."""
    return await list_service.create_list(user.id, body)


@router.get("/match")
async def match_list(
    q: str = Query(min_length=1, description="What the user called the list, e.g. 'the costco run'"),
    user: UserDoc = Depends(get_current_user),
) -> ListMatch:
    """Which list does this name mean? `match` when one clearly fits; otherwise `candidates`
    (several fit equally — ask which) or neither (none fit — maybe create one)."""
    return await list_service.match_lists(user.id, q)


@router.get("/{list_id}")
async def get_list(list_id: str, user: UserDoc = Depends(get_current_user)) -> ListDetail:
    """A list with its items: open ones first (grouped by category), then checked-off ones."""
    return await list_service.get_list(user.id, list_id)


@router.patch("/{list_id}")
async def update_list(
    list_id: str, body: ListPatch, user: UserDoc = Depends(get_current_user)
) -> ListSummary:
    """Rename, change kind or notes, or archive/unarchive."""
    return await list_service.update_list(user.id, list_id, body)


@router.delete("/{list_id}", status_code=204)
async def delete_list(list_id: str, user: UserDoc = Depends(get_current_user)) -> None:
    """Delete a list and all its items. (Archive instead to keep it out of the way.)"""
    await list_service.delete_list(user.id, list_id)


@router.post("/{list_id}/items", status_code=201)
async def add_items(
    list_id: str, body: AddItems, user: UserDoc = Depends(get_current_user)
) -> AddResult:
    """Add one or more items. Items already on the list are reported under `duplicates`
    (nothing changes); checked-off ones come back as `reopened`. An added item that looks
    related to an existing one carries `similar_to`."""
    return await list_service.add_items(user.id, list_id, body.items)


@router.patch("/{list_id}/items/{item_id}")
async def patch_item(
    list_id: str, item_id: str, body: ItemPatch, user: UserDoc = Depends(get_current_user)
) -> ItemOut:
    """Edit an item or check/un-check it (`done`)."""
    return await list_service.patch_item(user.id, list_id, item_id, body)


@router.delete("/{list_id}/items/{item_id}", status_code=204)
async def delete_item(list_id: str, item_id: str, user: UserDoc = Depends(get_current_user)) -> None:
    await list_service.delete_item(user.id, list_id, item_id)


@router.post("/{list_id}/items/check")
async def check_items(
    list_id: str, body: ByText, user: UserDoc = Depends(get_current_user)
) -> ByTextResult:
    """Check off (`done: true`, the default) or un-check items by name — no ids needed.
    A name that fits several items equally is reported under `ambiguous` and left alone."""
    return await list_service.check_by_text(user.id, list_id, body.texts, body.done)


@router.post("/{list_id}/items/remove")
async def remove_items(
    list_id: str, body: ByText, user: UserDoc = Depends(get_current_user)
) -> ByTextResult:
    """Remove items by name (checked or not). Ambiguous names are left alone."""
    return await list_service.remove_by_text(user.id, list_id, body.texts)


@router.post("/{list_id}/clear-done")
async def clear_done(list_id: str, user: UserDoc = Depends(get_current_user)) -> ClearResult:
    """Delete every checked-off item (the end of a shopping trip)."""
    return ClearResult(removed=await list_service.clear_done(user.id, list_id))
