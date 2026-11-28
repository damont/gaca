"""How many model tokens the crew used, and what could make it cheaper (GET /api/usage).

Neutral terms, like the automations schema: a crew member's scheduled work is an `automation`,
everything else (chats from the app, one-off runs) is grouped by where it came from. Token counts
are the engine's own. `prompt_tokens` = tokens sent to the model (paid in full + served from the
provider's cache); `cached_tokens` is the cached part of that.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UsageLine(BaseModel):
    crew: str
    kind: str  # "automation" | "chat"
    automation_id: str | None = None
    name: str  # the automation's name, or where the chats came from ("app", "cli"…)
    runs: int = 0
    silent_runs: int = 0  # runs that ended with nothing to say
    calls: int = 0  # model calls
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    share: float = 0.0  # of everything the crew used in the window, 0..1
    model: str | None = None
    schedule: str | None = None
    has_prerun: bool = False  # the automation already has a pre-run check/monitor script
    previous_total_tokens: int | None = None  # the same line in the window before


class UsageCost(BaseModel):
    """What the window would have cost on a pay-per-token provider, for comparison with a flat plan."""
    label: str
    input_per_million: float
    cached_per_million: float
    output_per_million: float
    usd: float
    usd_per_month: float


class UsageFinding(BaseModel):
    """One thing that would cut tokens without changing what the crew does."""
    rule: str  # "prerun" | "review" | "heartbeat_prerun"
    crew: str
    automation_id: str | None = None
    name: str
    title: str
    detail: str
    estimated_weekly_savings: int = 0  # tokens


class UsageReport(BaseModel):
    since: datetime
    until: datetime
    days: int
    total_tokens: int = 0
    previous_total_tokens: int | None = None
    lines: list[UsageLine] = Field(default_factory=list)
    cost: UsageCost | None = None
    findings: list[UsageFinding] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)  # crew members whose usage couldn't be read
