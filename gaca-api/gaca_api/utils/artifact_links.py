"""Artifact links inside message text: `[label](gaca://artifact/<id>)` or a bare
`gaca://artifact/<id>`.

An agent puts these in a reply to point at a page it made; clients show them as
chips below the message rather than inline. Both halves of that — the text with
the links taken out, and the list of what they pointed at — are worked out here,
so no client has to parse message text. The rule was ported verbatim from the
Android client (which used to do this itself); keep it in lockstep with
the API contract (gaca-planning/alerts/api-contract.md) if it ever changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Markdown form first so its label is captured; the bare form catches the rest.
# Ids are our 32-char hex ids (models/base.py:new_id) — anything else isn't a link.
_LINK = re.compile(
    r"\[([^\]\n]{1,120})\]\(gaca://artifact/([0-9a-f]{32})\)|gaca://artifact/([0-9a-f]{32})"
)


@dataclass(frozen=True)
class ArtifactLink:
    artifact_id: str
    label: str | None  # the markdown link text; None for a bare link


def strip(text: str) -> str:
    """The text with every artifact link removed, and the gaps that leaves tidied:
    trailing spaces before a newline dropped, runs of blank lines collapsed to one."""
    text = _LINK.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def find(text: str) -> list[ArtifactLink]:
    """Each linked artifact once, in order of first appearance. A later labeled
    link to the same artifact doesn't replace the first one's (missing) label —
    first appearance wins outright, matching how the chips are ordered."""
    seen: dict[str, ArtifactLink] = {}
    for match in _LINK.finditer(text):
        label, labeled_id, bare_id = match.groups()
        artifact_id = labeled_id or bare_id
        if artifact_id not in seen:
            seen[artifact_id] = ArtifactLink(artifact_id, label if labeled_id else None)
    return list(seen.values())
