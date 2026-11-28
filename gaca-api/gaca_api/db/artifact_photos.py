"""Data access for `artifact_photos`: metadata in `doc`, the photo in `data`, a small copy in `thumb`."""
from __future__ import annotations

from datetime import datetime

from ..models.artifact_photo import ArtifactPhotoDoc
from .client import connection


async def insert(doc: ArtifactPhotoDoc, data: bytes, thumb: bytes) -> None:
    conn = connection()
    await conn.execute(
        "INSERT INTO artifact_photos (id, artifact_id, created_at, doc, data, thumb) VALUES (?, ?, ?, ?, ?, ?)",
        (doc.id, doc.artifact_id, doc.created_at.isoformat(), doc.model_dump_json(), data, thumb),
    )
    await conn.commit()


async def find(artifact_id: str, photo_id: str, *, thumb: bool) -> tuple[ArtifactPhotoDoc, bytes] | None:
    column = "thumb" if thumb else "data"
    async with connection().execute(
        f"SELECT doc, {column} AS bytes FROM artifact_photos WHERE id = ? AND artifact_id = ?", (photo_id, artifact_id)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return ArtifactPhotoDoc.model_validate_json(row["doc"]), bytes(row["bytes"])


async def list_for(artifact_id: str) -> list[ArtifactPhotoDoc]:
    async with connection().execute(
        "SELECT doc FROM artifact_photos WHERE artifact_id = ? ORDER BY created_at, id", (artifact_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return [ArtifactPhotoDoc.model_validate_json(r["doc"]) for r in rows]


async def usage(artifact_id: str) -> tuple[int, int]:
    """(how many photos, their total stored bytes) for one artifact."""
    async with connection().execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(data) + LENGTH(thumb)), 0) AS bytes "
        "FROM artifact_photos WHERE artifact_id = ?", (artifact_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return int(row["n"]), int(row["bytes"])


async def delete(artifact_id: str, photo_ids: list[str]) -> int:
    if not photo_ids:
        return 0
    conn = connection()
    marks = ",".join("?" * len(photo_ids))
    cursor = await conn.execute(
        f"DELETE FROM artifact_photos WHERE artifact_id = ? AND id IN ({marks})", (artifact_id, *photo_ids)
    )
    await conn.commit()
    return cursor.rowcount


async def older_than(artifact_id: str, cutoff: datetime) -> list[str]:
    async with connection().execute(
        "SELECT id FROM artifact_photos WHERE artifact_id = ? AND created_at < ?", (artifact_id, cutoff.isoformat())
    ) as cursor:
        return [r["id"] for r in await cursor.fetchall()]


async def data_text(artifact_id: str) -> str:
    """Everything the page has stored (its state and every document), as one string, to look for photo ids in."""
    conn = connection()
    parts: list[str] = []
    async with conn.execute("SELECT state FROM artifacts WHERE id = ?", (artifact_id,)) as cursor:
        row = await cursor.fetchone()
        if row is not None:
            parts.append(row["state"])
    async with conn.execute("SELECT data FROM artifact_docs WHERE artifact_id = ?", (artifact_id,)) as cursor:
        parts.extend(r["data"] for r in await cursor.fetchall())
    return "\n".join(parts)
