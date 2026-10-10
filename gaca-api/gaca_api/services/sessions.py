"""Session business logic. No FastAPI, no HTTP — just orchestration."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..db import artifacts as artifacts_db
from ..db import messages as messages_db
from ..db import sessions as sessions_db
from ..models.message import MessageDoc
from ..models.session import SessionDoc
from ..schemas.message import ArtifactRefOut, MessageOut
from ..schemas.session import SessionDetail, SessionOut
from ..utils import artifact_links
from .exceptions import NotFound


async def create(
    user_id: str, title: str | None, crew: str, *, alert_id: str | None = None
) -> SessionDoc:
    session = SessionDoc(user_id=user_id, title=title, crew=crew, alert_id=alert_id)
    await sessions_db.insert(session)
    return session


SUMMARY_MAX = 60
PREVIEW_MAX = 100


@dataclass(frozen=True)
class Labels:
    """How a client should label a session: a headline and a last-message line."""
    summary: str | None
    preview: str | None


def tidy(text: str, limit: int) -> str | None:
    """One line, markdown noise dropped, cut at a word boundary with an ellipsis."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


def _photo_label(message: MessageDoc) -> str | None:
    """A message with no text but photos still deserves a label."""
    n = len(message.attachments)
    return None if n == 0 else ("Photo" if n == 1 else f"{n} photos")


def labels_for(
    title: str | None,
    first_user: MessageDoc | None,
    last: MessageDoc | None,
) -> Labels:
    """The headline and preview for a session. All labeling rules live here so
    every client shows the same thing: an explicit title wins; otherwise the
    opening question; the preview is the latest message, prefixed "You: " when
    it's the user's."""
    summary = tidy(title, SUMMARY_MAX) if title else None
    if summary is None and first_user is not None:
        summary = tidy(artifact_links.strip(first_user.content), SUMMARY_MAX) or _photo_label(first_user)
    preview = None
    if last is not None:
        preview = tidy(artifact_links.strip(last.content), PREVIEW_MAX) or _photo_label(last)
        if preview and last.role == "user":
            preview = f"You: {preview}"
    return Labels(summary, preview)


async def list_for_user(user_id: str) -> list[tuple[SessionDoc, Labels]]:
    """This user's sessions, most recently active first — the Chat tab's
    list — each with its labels. See db/sessions.py:find_by_user for what
    "active" means."""
    sessions = await sessions_db.find_by_user(user_id)
    edges = await messages_db.find_edges_by_user(user_id)
    return [(s, labels_for(s.title, *edges.get(s.id, (None, None)))) for s in sessions]


async def ensure_owned(user_id: str, session_id: str) -> SessionDoc:
    """The session, if it exists and belongs to this user — else NotFound.

    404, not 403, same reasoning as alerts: a 403 would confirm the id exists,
    which someone else's caller has no business learning.
    """
    session = await sessions_db.find(session_id)
    if session is None or session.user_id != user_id:
        raise NotFound("session", session_id)
    return session


async def get_with_messages(
    user_id: str, session_id: str
) -> tuple[SessionDoc, list[MessageDoc], Labels]:
    session = await ensure_owned(user_id, session_id)
    messages = await messages_db.find_by_session(session_id)
    first_user = next((m for m in messages if m.role == "user"), None)
    last = messages[-1] if messages else None
    return session, messages, labels_for(session.title, first_user, last)


async def messages_out(user_id: str, messages: list[MessageDoc]) -> list[MessageOut]:
    """The transcript as clients get it, with each message's artifact links
    resolved to chips. All the transcript's artifacts are looked up in one query,
    scoped to the session's owner — a link to someone else's artifact resolves
    exactly like a deleted one (missing), never to their title."""
    links = {m.id: artifact_links.find(m.content) for m in messages}
    ids = list({link.artifact_id for found in links.values() for link in found})
    owned = {row.doc.id: row.doc for row in await artifacts_db.find_by_ids(user_id, ids)}

    def ref(link: artifact_links.ArtifactLink) -> ArtifactRefOut:
        doc = owned.get(link.artifact_id)
        if doc is None:
            return ArtifactRefOut(id=link.artifact_id, title=link.label or "Page", missing=True)
        return ArtifactRefOut(id=doc.id, title=doc.title, description=doc.description, crew=doc.crew)

    return [
        MessageOut.model_validate(m).model_copy(update={"artifacts": [ref(l) for l in links[m.id]]})
        for m in messages
    ]


async def get_detail(user_id: str, session_id: str) -> SessionDetail:
    """GET /sessions/{id}: the session, its labels, and its rendered transcript."""
    session, messages, labels = await get_with_messages(user_id, session_id)
    return SessionDetail(
        **SessionOut.model_validate(session).model_dump(exclude={"summary", "preview"}),
        summary=labels.summary,
        preview=labels.preview,
        messages=await messages_out(user_id, messages),
    )
