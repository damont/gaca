"""Hermes skill files -> neutral SkillInfo, for when Hermes can't list its own skills.

`GET /v1/skills` is the normal source. It 500s on every Hermes release from v2026.9.11
through at least v0.21.7 (its handler calls `_find_all_skills(include_editorial=True)`, a
keyword the function doesn't take), so gaca-api falls back to reading the profile's
`skills/` folder itself, the way Hermes does: each `SKILL.md` is a skill, its frontmatter
`name` (else the folder name) and `description` (else the first non-heading body line), and
the folder above it, when there is one, its category. Hermes' own listing includes disabled
skills, so this does too.
"""
from __future__ import annotations

from ...schemas.skill import SkillInfo

_MAX_DESCRIPTION = 1024


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Top-level `key: value` pairs of a `---` block, and the body after it. Only the plain
    scalars Hermes skills use for name/description; anything else is ignored."""
    if not text.startswith("---"):
        return {}, text
    head, sep, body = text[3:].partition("\n---")
    if not sep:
        return {}, text
    values: dict[str, str] = {}
    for line in head.splitlines():
        if not line or line[0] in " \t#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values, body.split("\n", 1)[1] if "\n" in body else ""


def to_skill_info(parts: list[str], text: str) -> SkillInfo:
    """One skill from its folder (relative to the profile's skills/, e.g. ["gaca", "gaca-alerts"])
    and its SKILL.md text."""
    meta, body = _frontmatter(text)
    description = meta.get("description", "")
    if not description:
        description = next((line for line in (l.strip() for l in body.splitlines())
                            if line and not line.startswith("#")), "")
    return SkillInfo(
        name=(meta.get("name") or parts[-1])[:128],
        description=description[:_MAX_DESCRIPTION],
        category=parts[0] if len(parts) > 1 else None,
    )


def to_skills(files: list[dict]) -> list[SkillInfo]:
    """`[{"path": [...], "text": "..."}]` from the provisioner -> skills, sorted as Hermes sorts
    them (category, then name)."""
    skills = [to_skill_info(list(f["path"]), f["text"]) for f in files if f.get("path")]
    return sorted(skills, key=lambda s: (s.category or "", s.name))
