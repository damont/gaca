"""Hermes -> neutral profile translation.

The only module that knows how Hermes names the facts about a profile. Each
function takes one raw Hermes response (as JSON) and returns the neutral piece of
schemas/crew_profile.py; the adapter fetches, this maps. Every function tolerates
missing/odd fields — a Hermes upgrade that adds or drops a key must degrade one
section, never break the whole profile.

Raw shapes, confirmed live against hermes-agent 0.20.5:

    GET /health/detailed -> {"status","version","gateway_state","platforms":{"telegram":{"state":"connected",...}}}
    GET /api/model/options -> {"model","provider","providers":[...]}
    GET /v1/skills       -> {"data":[{"name","description","category"}]}
    GET /v1/toolsets     -> {"data":[{"name","label","description","enabled","configured","tools"}]}
    GET /api/jobs        -> {"jobs":[{"id","name","schedule":{"kind","expr","display"},"enabled","state",
                              "next_run_at","last_run_at","last_status","no_agent"}]}
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from ...schemas.crew_profile import (
    ProfileChannel,
    ProfileJob,
    ProfileModel,
    ProfileToolset,
    SkillCategory,
)
from ...utils.cron_text import describe_cron


def _dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def engine_label(health: dict) -> str | None:
    version = health.get("version")
    return f"Hermes {version}" if version else "Hermes"


def is_online(health: dict) -> bool:
    return health.get("status") == "ok" or health.get("gateway_state") == "running"


def channels(health: dict) -> list[ProfileChannel]:
    """Hermes calls these "platforms"; to a person they're how the profile is reached."""
    out: list[ProfileChannel] = []
    for name, info in sorted((health.get("platforms") or {}).items()):
        if not isinstance(info, dict):
            continue
        raw_state = info.get("state")
        state = "connected" if raw_state == "connected" else "error" if info.get("error_code") else "disconnected"
        out.append(ProfileChannel(
            name=_channel_name(name),
            state=state,
            needs_attention=bool(info.get("needs_attention")) or state == "error",
        ))
    return out


def _channel_name(raw: str) -> str:
    # "api_server" is how GACA itself talks to the profile — name it for people.
    return {"api_server": "GACA app"}.get(raw, raw.replace("_", " ").title())


def model(options: dict) -> ProfileModel | None:
    name, provider = options.get("model"), options.get("provider")
    if not name and not provider:
        return None
    current = next((p for p in options.get("providers") or [] if p.get("is_current")), None)
    return ProfileModel(
        provider=(current or {}).get("name") or provider,
        name=name,
    )


def skill_summary(skills_response: dict) -> tuple[int, list[SkillCategory]]:
    skills = [s for s in skills_response.get("data") or [] if isinstance(s, dict)]
    counts = Counter((s.get("category") or "general") for s in skills)
    categories = [SkillCategory(name=name, count=n) for name, n in counts.most_common()]
    # most_common is stable on ties but not alphabetical; make ties deterministic.
    categories.sort(key=lambda c: (-c.count, c.name))
    return len(skills), categories


def toolsets(toolsets_response: dict) -> list[ProfileToolset]:
    """Only toolsets that are actually on — what it *can* do, not the catalog."""
    return [
        ProfileToolset(
            name=t.get("name", ""),
            label=_strip_emoji(t.get("label") or t.get("name", "")),
            description=t.get("description") or "",
        )
        for t in toolsets_response.get("data") or []
        if isinstance(t, dict) and t.get("enabled") and t.get("name")
    ]


def _strip_emoji(label: str) -> str:
    """Hermes labels start with an emoji ("🔍 Web Search"); the app has its own icons."""
    return re.sub(r"^[^\w]+", "", label) or label


def jobs(jobs_response: dict) -> list[ProfileJob]:
    out: list[ProfileJob] = []
    for j in jobs_response.get("jobs") or []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        schedule = j.get("schedule") or {}
        expr = schedule.get("expr") or schedule.get("display") or j.get("schedule_display") or ""
        text = describe_cron(expr) if schedule.get("kind", "cron") == "cron" and expr else (
            schedule.get("display") or j.get("schedule_display") or "One time"
        )
        out.append(ProfileJob(
            id=str(j["id"]),
            name=j.get("name") or str(j["id"]),
            schedule=text,
            enabled=bool(j.get("enabled", True)) and not j.get("paused_at"),
            next_run=_dt(j.get("next_run_at")),
            last_run=_dt(j.get("last_run_at")),
            last_status=j.get("last_status"),
            kind="script" if j.get("no_agent") else "agent",
        ))
    # Soonest first; jobs with nothing scheduled sink to the bottom.
    out.sort(key=lambda j: (j.next_run is None, j.next_run.timestamp() if j.next_run else 0.0))
    return out
