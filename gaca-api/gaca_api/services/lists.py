"""Lists business logic. No FastAPI, no HTTP.

The rules every client should share live here:

* Names are matched loosely — "the Costco run" finds a list called "Costco" — but never
  guessed: if two lists (or two items) fit equally well the caller is told, not made to
  pick for it.
* An item can't be on a list twice (the database enforces it). Adding something that is
  checked off un-checks it, which is what "we need milk again" means.
* Everything is scoped to the calling user; someone else's list is a 404.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ..db import lists as lists_db
from ..models.base import utcnow
from ..models.lists import ListDoc, ListItemDoc
from ..schemas.lists import (
    MAX_ITEMS_PER_LIST,
    AddResult,
    Ambiguous,
    ByTextResult,
    Duplicate,
    ItemIn,
    ItemOut,
    ItemPatch,
    ListCreate,
    ListDetail,
    ListMatch,
    ListPatch,
    ListSummaries,
    ListSummary,
)
from .exceptions import InvalidRequest, NotFound

FUZZY_MIN = 0.8  # how alike two names must be to count as the same thing
SIMILAR_MIN = 0.88  # looser than "same", tighter than "unrelated": worth a heads-up


# ── Matching ─────────────────────────────────────────────────────────────────
def norm(text: str) -> str:
    """Lowercase, '&' -> 'and', punctuation gone (but '2%' stays), spaces collapsed."""
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^\w%\s]", " ", text).split())


def _stem(word: str) -> str:
    """Just enough plural folding that batteries == battery and eggs == egg."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def key(text: str) -> str:
    """The normalized identity of a name: two names with the same key are the same name."""
    return " ".join(_stem(w) for w in norm(text).split())


@dataclass(frozen=True)
class Resolution:
    ids: list[str]  # 0 = nothing fits, 1 = a clear match, 2+ = ambiguous

    @property
    def one(self) -> str | None:
        return self.ids[0] if len(self.ids) == 1 else None


def resolve(query: str, candidates: list[tuple[str, str]]) -> Resolution:
    """Which candidate (id, text) does `query` mean? Tiers, best first: identical after
    normalizing; sharing words (see below); a close fuzzy match. When more than one fits and
    nothing clearly wins, all are returned and the caller is told it's ambiguous."""
    qk = key(query)
    q_tokens = set(qk.split())
    if not q_tokens:
        return Resolution([])
    keyed = [(cid, key(text)) for cid, text in candidates]

    exact = [cid for cid, ck in keyed if ck == qk]
    if exact:
        return Resolution(exact)

    # Word containment. Two directions, treated differently on purpose:
    #   * the query CONTAINS a whole name ("the costco run" ⊇ "costco"): that name is clearly meant;
    #     if several are contained, the one with the most words is the most specific.
    #   * the query is PART of a name ("christmas" ⊂ "christmas 2026" and "christmas eve dinner"):
    #     one such name is a fine match, but several is a real ambiguity, so say so.
    contains_name, part_of_name = [], []
    for cid, ck in keyed:
        c_tokens = set(ck.split())
        if not c_tokens:
            continue
        if c_tokens <= q_tokens:
            contains_name.append((len(c_tokens), cid))
        elif q_tokens <= c_tokens:
            part_of_name.append(cid)
    if contains_name and not part_of_name:
        longest = max(n for n, _ in contains_name)
        return Resolution([cid for n, cid in contains_name if n == longest])
    if part_of_name or contains_name:
        return Resolution(part_of_name + [cid for _, cid in contains_name])

    fuzzy = sorted(
        ((difflib.SequenceMatcher(None, qk, ck).ratio(), cid) for cid, ck in keyed), reverse=True
    )
    fuzzy = [(r, cid) for r, cid in fuzzy if r >= FUZZY_MIN]
    if not fuzzy:
        return Resolution([])
    if len(fuzzy) == 1 or fuzzy[0][0] - fuzzy[1][0] >= 0.08:
        return Resolution([fuzzy[0][1]])
    return Resolution([cid for r, cid in fuzzy if fuzzy[0][0] - r < 0.08])


def _similar(new_key: str, existing: list[ListItemDoc]) -> str | None:
    """An existing item that looks related to a new one but isn't the same."""
    new_tokens = set(new_key.split())
    for item in existing:
        k = key(item.text)
        tokens = set(k.split())
        if tokens and (new_tokens <= tokens or tokens <= new_tokens):
            return item.text
        if difflib.SequenceMatcher(None, new_key, k).ratio() >= SIMILAR_MIN:
            return item.text
    return None


# ── Shaping ──────────────────────────────────────────────────────────────────
def _out(item: ListItemDoc, similar_to: str | None = None) -> ItemOut:
    return ItemOut(
        id=item.id, text=item.text, quantity=item.quantity, note=item.note,
        category=item.category, for_person=item.for_person, url=item.url, price=item.price,
        done=item.done, created_at=item.created_at, similar_to=similar_to,
    )


def _summary(doc: ListDoc, counts: dict[str, tuple[int, int]]) -> ListSummary:
    open_n, done_n = counts.get(doc.id, (0, 0))
    return ListSummary(
        id=doc.id, name=doc.name, kind=doc.kind, notes=doc.notes,
        open_count=open_n, done_count=done_n, archived=doc.archived, updated_at=doc.updated_at,
    )


def _ordered(items: list[ListItemDoc]) -> list[ListItemDoc]:
    """Open items first, grouped by category (uncategorized last); then checked-off, latest first."""
    open_items = sorted(
        (i for i in items if not i.done),
        key=lambda i: (i.category is None, (i.category or "").lower(), i.created_at),
    )
    done_items = sorted((i for i in items if i.done), key=lambda i: i.done_at or i.created_at, reverse=True)
    return open_items + done_items


async def _owned(user_id: str, list_id: str) -> ListDoc:
    doc = await lists_db.find_list(user_id, list_id)
    if doc is None:
        raise NotFound("list", list_id)
    return doc


async def _touch(doc: ListDoc) -> None:
    doc.updated_at = utcnow()
    await lists_db.update_list(doc, key(doc.name))


# ── Lists ────────────────────────────────────────────────────────────────────
async def list_lists(user_id: str, include_archived: bool = False) -> ListSummaries:
    docs = await lists_db.find_lists(user_id, include_archived)
    counts = await lists_db.counts(user_id)
    return ListSummaries(lists=[_summary(d, counts) for d in docs])


async def create_list(user_id: str, body: ListCreate) -> ListSummary:
    doc = ListDoc(user_id=user_id, name=body.name, kind=body.kind, notes=body.notes)
    if not key(doc.name):
        raise InvalidRequest("a list name needs letters or numbers")
    try:
        await lists_db.insert_list(doc, key(doc.name))
    except lists_db.DuplicateError:
        raise InvalidRequest(f"you already have a list called {doc.name!r}") from None
    return _summary(doc, {})


async def match_lists(user_id: str, query: str) -> ListMatch:
    docs = await lists_db.find_lists(user_id)
    counts = await lists_db.counts(user_id)
    resolved = resolve(query, [(d.id, d.name) for d in docs])
    by_id = {d.id: d for d in docs}
    if resolved.one:
        return ListMatch(match=_summary(by_id[resolved.one], counts))
    return ListMatch(candidates=[_summary(by_id[i], counts) for i in resolved.ids])


async def get_list(user_id: str, list_id: str) -> ListDetail:
    doc = await _owned(user_id, list_id)
    items = _ordered([i for i, _ in await lists_db.find_items(list_id)])
    summary = _summary(doc, {doc.id: (sum(not i.done for i in items), sum(i.done for i in items))})
    return ListDetail(**summary.model_dump(), items=[_out(i) for i in items])


async def update_list(user_id: str, list_id: str, patch: ListPatch) -> ListSummary:
    doc = await _owned(user_id, list_id)
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if value is None and field in ("name", "kind", "archived"):
            continue  # those can't be cleared; only notes can
        setattr(doc, field, value.strip() if isinstance(value, str) else value)
    if not key(doc.name):
        raise InvalidRequest("a list name needs letters or numbers")
    doc.updated_at = utcnow()
    try:
        await lists_db.update_list(doc, key(doc.name))
    except lists_db.DuplicateError:
        raise InvalidRequest(f"you already have a list called {doc.name!r}") from None
    return _summary(doc, await lists_db.counts(user_id))


async def delete_list(user_id: str, list_id: str) -> None:
    if not await lists_db.delete_list(user_id, list_id):
        raise NotFound("list", list_id)


# ── Items ────────────────────────────────────────────────────────────────────
async def add_items(user_id: str, list_id: str, items: list[ItemIn]) -> AddResult:
    doc = await _owned(user_id, list_id)
    existing = [i for i, _ in await lists_db.find_items(list_id)]
    by_key = {key(i.text): i for i in existing}
    result = AddResult(list=_summary(doc, {}))

    for item in items:
        new_key = key(item.text)
        if not new_key:
            raise InvalidRequest(f"{item.text!r} needs letters or numbers")
        current = by_key.get(new_key)
        if current is not None:
            if current.done:
                current.done, current.done_at, current.updated_at = False, None, utcnow()
                for field in ("quantity", "note", "category", "for_person", "url", "price"):
                    if getattr(item, field) is not None:
                        setattr(current, field, getattr(item, field))
                await lists_db.update_item(current, new_key)
                result.reopened.append(_out(current))
            else:
                result.duplicates.append(Duplicate(text=item.text, existing=_out(current)))
            continue
        if len(by_key) >= MAX_ITEMS_PER_LIST:
            raise InvalidRequest(f"a list can hold at most {MAX_ITEMS_PER_LIST} items")
        new = ListItemDoc(list_id=list_id, user_id=user_id, **item.model_dump())
        similar = _similar(new_key, list(by_key.values()))
        try:
            await lists_db.insert_item(new, new_key)
        except lists_db.DuplicateError:  # lost a race with another writer: it's there now
            result.duplicates.append(Duplicate(text=item.text, existing=_out(new)))
            continue
        by_key[new_key] = new
        result.added.append(_out(new, similar))

    if result.added or result.reopened:
        await _touch(doc)
    items_now = list(by_key.values())
    result.list = _summary(doc, {doc.id: (sum(not i.done for i in items_now), sum(i.done for i in items_now))})
    return result


async def patch_item(user_id: str, list_id: str, item_id: str, patch: ItemPatch) -> ItemOut:
    doc = await _owned(user_id, list_id)
    item = await lists_db.find_item(list_id, item_id)
    if item is None:
        raise NotFound("list item", item_id)
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if field in ("text", "done") and value is None:
            continue
        if field == "done":
            item.done, item.done_at = value, (utcnow() if value else None)
        else:
            setattr(item, field, value.strip() if isinstance(value, str) else value)
    if not key(item.text):
        raise InvalidRequest("item text needs letters or numbers")
    item.updated_at = utcnow()
    try:
        await lists_db.update_item(item, key(item.text))
    except lists_db.DuplicateError:
        raise InvalidRequest(f"{item.text!r} is already on this list") from None
    await _touch(doc)
    return _out(item)


async def delete_item(user_id: str, list_id: str, item_id: str) -> None:
    doc = await _owned(user_id, list_id)
    if await lists_db.find_item(list_id, item_id) is None:
        raise NotFound("list item", item_id)
    await lists_db.delete_items(list_id, [item_id])
    await _touch(doc)


async def _by_text(
    user_id: str, list_id: str, texts: list[str], *, among_done: bool | None
) -> tuple[ListDoc, ByTextResult, list[ListItemDoc]]:
    """Resolve each text to at most one item. `among_done` limits which items are candidates
    (True: checked-off only, False: open only, None: all). Ambiguous and unknown texts are
    reported and left alone."""
    doc = await _owned(user_id, list_id)
    pool = [
        i for i, _ in await lists_db.find_items(list_id)
        if among_done is None or i.done == among_done
    ]
    by_id = {i.id: i for i in pool}
    result, chosen, seen = ByTextResult(), [], set()
    for text in texts:
        resolved = resolve(text, [(i.id, i.text) for i in pool])
        if resolved.one:
            if resolved.one not in seen:
                seen.add(resolved.one)
                chosen.append(by_id[resolved.one])
        elif resolved.ids:
            result.ambiguous.append(Ambiguous(query=text, candidates=[_out(by_id[i]) for i in resolved.ids]))
        else:
            result.not_found.append(text)
    return doc, result, chosen


async def check_by_text(user_id: str, list_id: str, texts: list[str], done: bool) -> ByTextResult:
    """Check off (done=True) or un-check (done=False) items by name."""
    doc, result, chosen = await _by_text(user_id, list_id, texts, among_done=not done)
    for item in chosen:
        item.done, item.done_at, item.updated_at = done, (utcnow() if done else None), utcnow()
        await lists_db.update_item(item, key(item.text))
        result.matched.append(_out(item))
    if chosen:
        await _touch(doc)
    return result


async def remove_by_text(user_id: str, list_id: str, texts: list[str]) -> ByTextResult:
    doc, result, chosen = await _by_text(user_id, list_id, texts, among_done=None)
    if chosen:
        await lists_db.delete_items(list_id, [i.id for i in chosen])
        result.matched = [_out(i) for i in chosen]
        await _touch(doc)
    return result


async def clear_done(user_id: str, list_id: str) -> int:
    """Remove every checked-off item — the end of a shopping trip."""
    doc = await _owned(user_id, list_id)
    done = [i.id for i, _ in await lists_db.find_items(list_id) if i.done]
    if done:
        await lists_db.delete_items(list_id, done)
        await _touch(doc)
    return len(done)
