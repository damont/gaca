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

# Copied from hermes_cli/default_soul.py (hermes-agent 0.20.5). Compared whitespace-normalized,
# so wrapping and trailing newlines don't matter.
STOCK_SOUL = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)


# The stock paragraph, matched however it happens to be wrapped.
_STOCK_PATTERN = re.compile(r"\s*" + r"\s+".join(re.escape(word) for word in STOCK_SOUL.split()))


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


def custom_instructions(soul: str | None) -> str | None:
    """The part of SOUL.md someone actually wrote, or None if it's only Hermes' boilerplate."""
    if not soul:
        return None
    text = re.sub(r"<!--.*?-->", "", soul, flags=re.S)  # the old scaffold is all comments
    text = re.sub(r"^\s*#\s*Hermes Agent Persona\s*$", "", text, flags=re.M | re.I)
    stock = _STOCK_PATTERN.match(text)
    if stock:
        text = text[stock.end():]  # keep whatever was written after it, line structure intact
    return text.strip() or None


def to_purpose_info(files: dict) -> PurposeInfo:
    """`files` is what provisioning.ssh.read_profile_files returned for one profile."""
    return PurposeInfo(
        description=description(files.get("profile_yaml")),
        instructions=custom_instructions(files.get("soul")),
    )
