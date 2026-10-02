"""Data access for the `lists` and `list_items` tables."""
from __future__ import annotations

import aiosqlite

from ..models.lists import ListDoc, ListItemDoc
from .client import connection


class DuplicateError(Exception):
    """The unique index said no: that list name / item already exists."""


async def _execute(sql: str, params: tuple) -> None:
    conn = connection()
    try:
        await conn.execute(sql, params)
        await conn.commit()
    except aiosqlite.IntegrityError as error:
        await conn.rollback()
        raise DuplicateError(str(error)) from error


# ── lists ────────────────────────────────────────────────────────────────────
async def insert_list(doc: ListDoc, name_key: str) -> None:
    await _execute(
        "INSERT INTO lists (id, user_id, name_key, archived, updated_at, doc) VALUES (?, ?, ?, ?, ?, ?)",
        (doc.id, doc.user_id, name_key, int(doc.archived), doc.updated_at.isoformat(), doc.model_dump_json()),
    )


async def update_list(doc: ListDoc, name_key: str) -> None:
    await _execute(
        "UPDATE lists SET name_key = ?, archived = ?, updated_at = ?, doc = ? WHERE id = ?",
        (name_key, int(doc.archived), doc.updated_at.isoformat(), doc.model_dump_json(), doc.id),
    )


async def find_list(user_id: str, list_id: str) -> ListDoc | None:
    async with connection().execute(
        "SELECT doc FROM lists WHERE id = ? AND user_id = ?", (list_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
    return ListDoc.model_validate_json(row["doc"]) if row else None


async def find_lists(user_id: str, include_archived: bool = False) -> list[ListDoc]:
    sql = "SELECT doc FROM lists WHERE user_id = ?"
    if not include_archived:
        sql += " AND archived = 0"
    async with connection().execute(sql + " ORDER BY updated_at DESC, id", (user_id,)) as cursor:
        rows = await cursor.fetchall()
    return [ListDoc.model_validate_json(r["doc"]) for r in rows]


async def delete_list(user_id: str, list_id: str) -> bool:
    conn = connection()
    cursor = await conn.execute("DELETE FROM lists WHERE id = ? AND user_id = ?", (list_id, user_id))
    if cursor.rowcount:
        await conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def counts(user_id: str) -> dict[str, tuple[int, int]]:
    """{list_id: (open, done)} for all of a user's lists, in one query."""
    async with connection().execute(
        "SELECT list_id, SUM(done = 0) AS open, SUM(done = 1) AS done FROM list_items "
        "WHERE user_id = ? GROUP BY list_id",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {r["list_id"]: (r["open"] or 0, r["done"] or 0) for r in rows}


# ── items ────────────────────────────────────────────────────────────────────
async def insert_item(doc: ListItemDoc, item_key: str) -> None:
    await _execute(
        "INSERT INTO list_items (id, list_id, user_id, item_key, done, created_at, doc) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc.id, doc.list_id, doc.user_id, item_key, int(doc.done), doc.created_at.isoformat(), doc.model_dump_json()),
    )


async def update_item(doc: ListItemDoc, item_key: str) -> None:
    await _execute(
        "UPDATE list_items SET item_key = ?, done = ?, doc = ? WHERE id = ?",
        (item_key, int(doc.done), doc.model_dump_json(), doc.id),
    )


async def find_items(list_id: str) -> list[tuple[ListItemDoc, str]]:
    """Every item on a list with its key, oldest first."""
    async with connection().execute(
        "SELECT doc, item_key FROM list_items WHERE list_id = ? ORDER BY created_at, id", (list_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return [(ListItemDoc.model_validate_json(r["doc"]), r["item_key"]) for r in rows]


async def find_item(list_id: str, item_id: str) -> ListItemDoc | None:
    async with connection().execute(
        "SELECT doc FROM list_items WHERE id = ? AND list_id = ?", (item_id, list_id)
    ) as cursor:
        row = await cursor.fetchone()
    return ListItemDoc.model_validate_json(row["doc"]) if row else None


async def delete_items(list_id: str, item_ids: list[str]) -> None:
    conn = connection()
    for item_id in item_ids:
        await conn.execute("DELETE FROM list_items WHERE id = ? AND list_id = ?", (item_id, list_id))
    await conn.commit()


async def count_items(list_id: str) -> int:
    async with connection().execute("SELECT COUNT(*) AS n FROM list_items WHERE list_id = ?", (list_id,)) as cursor:
        return (await cursor.fetchone())["n"]
