"""Keep one short, managed paragraph in a Hermes profile's SOUL.md.

SOUL.md is the profile's standing instructions, written by the person (the crew screen's "dedicated
to") on top of Hermes' own stock text. GACA adds a single marked block to it, the same for every crew
member, telling the agent it is built to work with GACA and should reach for the GACA skills and API.
It deliberately does not list the skills: a list goes stale, and each skill's own description is what
tells Hermes when to load it.

The block sits between `<!-- gaca:begin -->` and `<!-- gaca:end -->`. Only text between the markers is
ever rewritten, so it can be updated everywhere at once and everything the person wrote is left alone.
A SOUL.md that does not exist yet is not created (Hermes seeds one with its stock persona; a file holding
only this block would replace that persona). Written like skills are: no symlinks, regular files only.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .skill_sync import _write_atomic

BEGIN = "<!-- gaca:begin -->"
END = "<!-- gaca:end -->"
TEXT = (
    "You are built to work with GACA, the app your person uses. Use the GACA skills and API whenever they "
    "can do the job (alerts, reminders, pages, the briefing) instead of working around them, or just "
    "promising to remember something."
)
BLOCK = f"{BEGIN}\n{TEXT}\n{END}"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
_MAX = 40_000


class SoulBlockError(Exception):
    """SOUL.md is not something we will read or write."""


def strip(soul: str) -> str:
    """`soul` without the managed block, so what is left is only what a person wrote."""
    return _BLOCK.sub("", soul)


def apply(directory: Path, *, dry_run: bool = False) -> str:
    """Put the block into `<directory>/SOUL.md`. Returns `added`, `updated`, `unchanged`, `absent` (no
    SOUL.md, nothing done) or `damaged` (a marker without its partner: left for a person to fix)."""
    path = directory / "SOUL.md"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        raise SoulBlockError(f"{path} cannot be read safely: {error}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SoulBlockError(f"{path} is not a regular file")
        if info.st_size > _MAX:
            raise SoulBlockError(f"{path} is too large")
        raw = os.read(fd, _MAX + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SoulBlockError(f"{path} is not UTF-8") from None

    found = _BLOCK.findall(text)
    if len(found) > 1 or (not found and (BEGIN in text or END in text)) or (found and text.count(BEGIN) != 1):
        return "damaged"
    if found:
        if found[0] == BLOCK:
            return "unchanged"
        new, status = _BLOCK.sub(lambda _m: BLOCK, text, count=1), "updated"
    else:
        new, status = text.rstrip("\n") + ("\n\n" if text.strip() else "") + BLOCK + "\n", "added"
    if not dry_run:
        _write_atomic(path, new.encode("utf-8"), stat.S_IMODE(info.st_mode))
    return status
