"""Hermes -> neutral purpose translation.

The only module that knows how Hermes records "what is this profile for":

  * `<profile>/profile.yaml` -> `description`: an optional one-liner Hermes itself uses
    (its task router falls back to the profile name when it's empty).
  * `<profile>/SOUL.md`: the persona / standing instructions. A new profile gets Hermes'
    stock "general assistant" paragraph (hermes_cli/default_soul.py: DEFAULT_SOUL_MD) or,
    from older installers, a comment-only scaffold — neither says anything specific, so
    both count as "no instructions". Anything written beyond that does.

Shapes confirmed live against hermes-agent 0.20.5: `default`, `david` and `trading` hold the
stock paragraph; `toolshed` has a custom persona; `toolshed` and `trading` have descriptions.
"""
from __future__ import annotations

import re

import yaml

from ...schemas.crew_profile import PurposeInfo

# Hermes' untouched-default persona texts, known by version. The installed Hermes reports its own
# (see ssh._READ_PROFILE_SCRIPT) and that is what's trusted first; these are a safety net for
# when it can't, and a record of what has shipped. They DO change between releases: 0.20.5
# seeded the "intelligent AI assistant" paragraph, 0.21.5 seeds the "Be direct" one and
# upgrades untouched copies of the old one in place.
KNOWN_STOCK_SOULS = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations.",
    "You are Hermes Agent, built by Nous Research. Be direct: match the length of your reply to the "
    "weight of the ask — a one-line question gets a one-line answer, and finished work gets a short "
    "report of what changed, what's verified, and what's left, never a replay of the process. No "
    "filler (\"Great question,\" \"I'd be happy to\"), no restating the request back, no "
    "re-summarizing what you already said, no narrating tool calls the user can see. Plain claims "
    "over adjectives; when unsure, say so plainly. Agree because it's right, not because the user "
    "said it. Depth is earned — give it when the user asks for detail, teaches, or the stakes "
    "demand it, not by default.",
)
STOCK_SOUL = KNOWN_STOCK_SOULS[0]  # kept for older tests/imports


def _stock_pattern(text: str) -> re.Pattern:
    """The stock paragraph, matched however it happens to be wrapped."""
    return re.compile(r"\s*" + r"\s+".join(re.escape(word) for word in text.split()))


def description(profile_yaml: str | None) -> str | None:
    """`description` from a profile.yaml, or None if absent, empty or unreadable."""
    if not profile_yaml:
        return None
    try:
        data = yaml.safe_load(profile_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or not data.get("description"):
        return None
    return " ".join(str(data["description"]).split()) or None


def custom_instructions(soul: str | None, stock: tuple[str, ...] | list[str] = ()) -> str | None:
    """The part of SOUL.md someone actually wrote, or None if it's only Hermes' boilerplate.

    `stock` is the installed Hermes' own untouched-default texts; the versions we know of are
    always checked too. Anything after a stock paragraph is kept, line structure intact.
    """
    if not soul:
        return None
    text = re.sub(r"<!--.*?-->", "", soul, flags=re.S)  # the old scaffold is all comments
    text = re.sub(r"^\s*#\s*Hermes Agent Persona\s*$", "", text, flags=re.M | re.I)
    # Longest first, so a text that extends another is matched whole.
    for candidate in sorted({*stock, *KNOWN_STOCK_SOULS}, key=len, reverse=True):
        # A scaffold made only of comments leaves nothing to compare; skip empty candidates.
        if not candidate.strip() or not re.sub(r"<!--.*?-->", "", candidate, flags=re.S).strip():
            continue
        match = _stock_pattern(re.sub(r"<!--.*?-->", "", candidate, flags=re.S)).match(text)
        if match:
            text = text[match.end():]
            break
    return text.strip() or None


def to_purpose_info(files: dict) -> PurposeInfo:
    """`files` is what provisioning.ssh.read_profile_files returned for one profile."""
    return PurposeInfo(
        description=description(files.get("profile_yaml")),
        instructions=custom_instructions(files.get("soul"), files.get("stock") or ()),
    )
