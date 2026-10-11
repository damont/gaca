"""Resolve artifact ids to the small `ArtifactRefOut` a client draws as a chip.

Shared by everything that points at an artifact — links in chat messages, an alert's
`artifact_id` — so they all resolve the same way: one query for however many ids, scoped
to the owner, and an id that isn't theirs (deleted, a bad id, someone else's) comes back
`missing` rather than vanishing or leaking another account's title.
"""
from __future__ import annotations

from ..db import artifacts as artifacts_db
from ..schemas.artifact import ArtifactRefOut


async def resolve(user_id: str, wanted: dict[str, str | None]) -> dict[str, ArtifactRefOut]:
    """{artifact_id: label} -> {artifact_id: ref}. `label` is what to title a missing one
    (a link's own text); None falls back to "Page"."""
    owned = {row.doc.id: row.doc for row in await artifacts_db.find_by_ids(user_id, list(wanted))}
    refs: dict[str, ArtifactRefOut] = {}
    for artifact_id, label in wanted.items():
        doc = owned.get(artifact_id)
        refs[artifact_id] = (
            ArtifactRefOut(id=doc.id, title=doc.title, description=doc.description, crew=doc.crew)
            if doc is not None
            else ArtifactRefOut(id=artifact_id, title=label or "Page", missing=True)
        )
    return refs
