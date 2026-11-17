"""Email drafts written by an agent, and how to read them back out of its reply.

An agent is asked to write a draft in one fixed shape, so the API can pull out the parts and an
app can show a draft card with an "open in mail" button:

    To: Pat Alvarez <pat@example.com>
    Subject: Re: Lease renewal terms

    Hi Pat, …

The header lines come first, then a blank line, then the body. Nothing may follow the body.
"""
from __future__ import annotations

import re

from ..models.alert import AlertDraft

# What an agent is told, wherever a draft can be asked for (an action run, or a chat about an alert).
FORMAT_INSTRUCTION = (
    "Write the email as plain text in exactly this shape, and nothing before or after it:\n"
    "To: <name and address of the recipient, from the email you are replying to>\n"
    "Subject: <subject>\n"
    "\n"
    "<the body, signed with the person's first name>"
)

_HEADER = re.compile(r"^(to|subject):[ \t]*(.*)$", re.IGNORECASE)


def _unfence(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```[a-zA-Z]*\n(.*?)\n?```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def parse_draft(text: str, *, strict: bool = False) -> AlertDraft | None:
    """The draft in `text`, or None.

    `strict` (used on a free chat reply) demands a `Subject:` header, so an ordinary answer that
    happens to mention an email is never taken for a draft. Lenient (an action that was asked
    for a draft) falls back to treating the whole reply as the body.
    """
    text = _unfence(text or "")
    if not text:
        return None
    lines = text.splitlines()
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        match = _HEADER.match(lines[i].strip())
        if not match:
            break
        fields[match.group(1).lower()] = match.group(2).strip()
        i += 1
    if "subject" not in fields:
        if strict:
            return None
        return AlertDraft(body=text)
    while i < len(lines) and not lines[i].strip():
        i += 1
    body = "\n".join(lines[i:]).strip()
    if not body:
        return None
    return AlertDraft(to=fields.get("to") or None, subject=fields.get("subject") or None, body=body)


def extract_draft(text: str) -> tuple[str, AlertDraft | None]:
    """(the words before the draft, the draft) in a chat reply that may open with a sentence like
    "Here's a shorter version:". Strict, as for `parse_draft`: with no `Subject:` header there is
    no draft and the whole text comes back as the words."""
    lines = (text or "").splitlines()
    for start, line in enumerate(lines):
        if _HEADER.match(line.strip()):
            draft = parse_draft("\n".join(lines[start:]), strict=True)
            if draft is not None:
                return "\n".join(lines[:start]).strip(), draft
            break
    return (text or "").strip(), None
