"""Turn a cron expression into plain English for display.

Covers the shapes people actually schedule (daily, weekly, every N minutes/hours,
monthly, yearly); anything else falls back to the raw expression rather than
guessing. Lives in the API so every client shows the same wording.
"""
from __future__ import annotations

import re

_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _clock(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{minute:02d} {suffix}"


def _ints(field: str) -> list[int] | None:
    """'0,7' -> [0, 7]; None if it isn't a plain list of numbers."""
    return [int(x) for x in field.split(",")] if re.fullmatch(r"\d+(,\d+)*", field) else None


def describe_cron(expr: str) -> str:
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts

    if (m := re.fullmatch(r"\*/(\d+)", minute)) and (hour, dom, month, dow) == ("*", "*", "*", "*"):
        return f"Every {m.group(1)} minutes"
    if minute.isdigit() and (m := re.fullmatch(r"\*/(\d+)", hour)) and (dom, month, dow) == ("*", "*", "*"):
        return f"Every {m.group(1)} hours"
    if minute.isdigit() and hour == "*" and (dom, month, dow) == ("*", "*", "*"):
        return "Every hour" if minute == "0" else f"Every hour at :{int(minute):02d}"

    minutes, hours = _ints(minute), _ints(hour)
    if not minutes or not hours:
        return expr
    times = ", ".join(_clock(h, m) for h in hours for m in minutes)

    if (dom, month, dow) == ("*", "*", "*"):
        return f"Every day at {times}"
    if (dom, month) == ("*", "*") and (days := _ints(dow)) and all(d <= 7 for d in days):
        names = [_DAYS[d % 7] for d in days]
        return f"Every {', '.join(names)} at {times}"
    if dow == "*" and month == "*" and dom.isdigit():
        return f"Monthly on day {dom} at {times}"
    if dow == "*" and dom.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
        return f"Every {_MONTHS[int(month) - 1]} {dom} at {times}"
    return expr
