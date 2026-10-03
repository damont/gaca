"""Shape an engine's raw purpose info into what GACA shows — engine-neutral.

The one-line summary is GACA's own rule, so every client shows the same thing no
matter which engine a crew member runs on: use the description someone wrote if there
is one; otherwise take the opening sentence of the instructions; otherwise there is no
summary because the crew member is a general assistant.
"""
from __future__ import annotations

import re

from ..schemas.crew_profile import ProfilePurpose, PurposeInfo

SUMMARY_MAX = 200


def _tidy(text: str) -> str:
    """One line of plain text: markdown emphasis and link syntax dropped, spaces collapsed."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"[*_`]+", "", text)
    return " ".join(text.split())


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,;:-") + "…"


def derive_summary(instructions: str) -> str | None:
    """The opening sentence of a set of instructions, as a short third-person line.

    Skips headings, comments and code; takes the first paragraph's first sentence; and
    turns "You are the dedicated X" into "The dedicated X" since this is read ABOUT
    the crew member, not to it.
    """
    text = re.sub(r"<!--.*?-->", " ", instructions, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    paragraph: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if paragraph:
                break
            continue
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    flat = _tidy(" ".join(paragraph))
    if not flat:
        return None
    sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat, maxsplit=1)[0]
    sentence = re.sub(r"^you are (?=(?:an?|the) )", "", sentence, flags=re.I)
    sentence = sentence[0].upper() + sentence[1:]
    return _cut(sentence.rstrip(), SUMMARY_MAX)


def build_purpose(info: PurposeInfo) -> ProfilePurpose:
    instructions = (info.instructions or "").strip() or None
    description = _tidy(info.description or "")
    if description:
        return ProfilePurpose(
            summary=_cut(description, SUMMARY_MAX),
            summary_source="description",
            instructions=instructions,
            is_generic=False,
        )
    if instructions:
        summary = derive_summary(instructions)
        return ProfilePurpose(
            summary=summary,
            summary_source="instructions" if summary else None,
            instructions=instructions,
            is_generic=False,
        )
    return ProfilePurpose(is_generic=True)
